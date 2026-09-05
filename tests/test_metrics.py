"""Daemon scrape-loop metrics: RED counters, domain gauges, bulkhead.

Each test builds its Metrics against a throwaway CollectorRegistry
(injected, never the process-global REGISTRY), so there is no cross-test
counter state to reset — isolation by construction instead of teardown.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from enphase_agent.daemon import scrape_once
from enphase_agent.errors import AuthError, CircuitOpen, StaleStateError
from enphase_agent.ledger import Ledger
from enphase_agent.metrics import Metrics, build_metrics
from enphase_agent.models import BatteryMode, SystemState

STATE_TS = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def make_state(**overrides: object) -> SystemState:
    defaults: dict[str, object] = dict(
        production_w=1500.0,
        consumption_w=800.0,
        battery_soc=0.55,
        battery_mode=BatteryMode.SAVINGS,
        reserve_soc=0.20,
        storm_guard=False,
        ts=STATE_TS,
        stale=False,
    )
    defaults.update(overrides)
    return SystemState(**defaults)  # type: ignore[arg-type]


class FakeAdapter:
    """Stands in for EnphaseAdapter: scripted get_state, scripted breaker."""

    def __init__(
        self,
        *,
        state: SystemState | None = None,
        error: Exception | None = None,
        circuit_open: bool = False,
    ) -> None:
        self._state = state or make_state()
        self._error = error
        self._circuit_open = circuit_open

    async def get_state(self) -> SystemState:
        if self._error is not None:
            raise self._error
        return self._state

    def is_circuit_open(self) -> bool:
        return self._circuit_open


@pytest.fixture
def metrics() -> Metrics:
    return build_metrics(CollectorRegistry())


def sample(m: Metrics, name: str, labels: dict[str, str] | None = None) -> float | None:
    return m.registry.get_sample_value(name, labels or {})


async def test_success_bumps_rate_and_updates_gauges(metrics: Metrics) -> None:
    adapter = FakeAdapter(state=make_state(production_w=2100.0, battery_soc=0.42))
    await scrape_once(adapter, metrics, now_fn=lambda: STATE_TS)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_scrape_total", {"outcome": "success"}) == 1.0
    assert sample(metrics, "enphase_scrape_total", {"outcome": "error"}) == 0.0
    assert sample(metrics, "enphase_production_watts") == 2100.0
    assert sample(metrics, "enphase_consumption_watts") == 800.0
    assert sample(metrics, "enphase_battery_soc_ratio") == pytest.approx(0.42)
    assert sample(metrics, "enphase_battery_reserve_ratio") == pytest.approx(0.20)
    assert sample(metrics, "enphase_storm_guard_active") == 0.0
    assert sample(metrics, "enphase_scrape_duration_seconds_count") == 1.0


async def test_auth_error_counted_and_loop_survives(metrics: Metrics) -> None:
    adapter = FakeAdapter(error=AuthError("Enlighten rejected credentials"))

    await scrape_once(adapter, metrics)  # type: ignore[arg-type]
    await scrape_once(adapter, metrics)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_scrape_errors_total", {"kind": "auth"}) == 2.0
    assert sample(metrics, "enphase_scrape_total", {"outcome": "error"}) == 2.0
    assert sample(metrics, "enphase_scrape_total", {"outcome": "success"}) == 0.0


async def test_circuit_open_counted_and_gauge_flips(metrics: Metrics) -> None:
    adapter = FakeAdapter(error=CircuitOpen("breaker is open"), circuit_open=True)
    await scrape_once(adapter, metrics)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_scrape_errors_total", {"kind": "circuit_open"}) == 1.0
    assert sample(metrics, "enphase_circuit_breaker_open") == 1.0


async def test_circuit_gauge_resets_when_breaker_closes(metrics: Metrics) -> None:
    await scrape_once(FakeAdapter(circuit_open=True, error=CircuitOpen("open")), metrics)  # type: ignore[arg-type]
    await scrape_once(FakeAdapter(circuit_open=False), metrics)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_circuit_breaker_open") == 0.0


async def test_stale_error_counted(metrics: Metrics) -> None:
    adapter = FakeAdapter(error=StaleStateError("too old"))
    await scrape_once(adapter, metrics)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_scrape_errors_total", {"kind": "stale"}) == 1.0


async def test_unknown_error_lands_in_other(metrics: Metrics) -> None:
    adapter = FakeAdapter(error=RuntimeError("gateway ate the request"))
    await scrape_once(adapter, metrics)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_scrape_errors_total", {"kind": "other"}) == 1.0


async def test_battery_mode_is_one_hot(metrics: Metrics) -> None:
    adapter = FakeAdapter(state=make_state(battery_mode=BatteryMode.SAVINGS))
    await scrape_once(adapter, metrics)  # type: ignore[arg-type]

    values = {
        mode.value: sample(metrics, "enphase_battery_mode", {"mode": mode.value})
        for mode in BatteryMode
    }
    assert values == {"self_consumption": 0.0, "savings": 1.0, "full_backup": 0.0}


async def test_mode_change_moves_the_one(metrics: Metrics) -> None:
    await scrape_once(FakeAdapter(state=make_state(battery_mode=BatteryMode.SAVINGS)), metrics)  # type: ignore[arg-type]
    await scrape_once(
        FakeAdapter(state=make_state(battery_mode=BatteryMode.FULL_BACKUP)), metrics  # type: ignore[arg-type]
    )

    assert sample(metrics, "enphase_battery_mode", {"mode": "savings"}) == 0.0
    assert sample(metrics, "enphase_battery_mode", {"mode": "full_backup"}) == 1.0


async def test_energy_gauges_publish_when_state_has_values(metrics: Metrics) -> None:
    state = make_state(
        production_wh_today=12_000,
        production_wh_7d=90_000,
        production_wh_lifetime=5_000_000,
        consumption_wh_today=9_000,
        consumption_wh_7d=70_000,
        consumption_wh_lifetime=4_200_000,
        battery_energy_available_wh=5_500,
        battery_energy_capacity_wh=10_080,
    )
    await scrape_once(FakeAdapter(state=state), metrics)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_energy_produced_today_watt_hours") == 12_000.0
    assert sample(metrics, "enphase_energy_produced_7d_watt_hours") == 90_000.0
    assert sample(metrics, "enphase_energy_produced_lifetime_watt_hours") == 5_000_000.0
    assert sample(metrics, "enphase_energy_consumed_today_watt_hours") == 9_000.0
    assert sample(metrics, "enphase_energy_consumed_7d_watt_hours") == 70_000.0
    assert sample(metrics, "enphase_energy_consumed_lifetime_watt_hours") == 4_200_000.0
    assert sample(metrics, "enphase_battery_energy_available_watt_hours") == 5_500.0
    assert sample(metrics, "enphase_battery_energy_capacity_watt_hours") == 10_080.0


async def test_none_fields_are_absent_not_zero(metrics: Metrics) -> None:
    # make_state leaves every optional field at None — a gateway without CT
    # meters. The series must be ABSENT from exposition, not a phantom 0.
    await scrape_once(FakeAdapter(state=make_state()), metrics)  # type: ignore[arg-type]

    exposition = generate_latest(metrics.registry).decode()
    for name in (
        "enphase_energy_produced_today_watt_hours",
        "enphase_energy_consumed_today_watt_hours",
        "enphase_energy_produced_lifetime_watt_hours",
        "enphase_battery_energy_available_watt_hours",
        "enphase_grid_import_watts",
        "enphase_grid_export_watts",
    ):
        assert sample(metrics, name) is None
        assert f"\n{name} " not in exposition


async def test_intermittent_none_keeps_last_value(metrics: Metrics) -> None:
    # Graceful degradation: an upstream field that vanishes for one scrape
    # keeps the last-known value (freshness is state_age's job), and never
    # snaps to zero.
    await scrape_once(FakeAdapter(state=make_state(production_wh_today=12_000)), metrics)  # type: ignore[arg-type]
    await scrape_once(FakeAdapter(state=make_state(production_wh_today=None)), metrics)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_energy_produced_today_watt_hours") == 12_000.0


async def test_grid_split_gauges_import_side(metrics: Metrics) -> None:
    state = make_state(grid_import_watts=650.0, grid_export_watts=0.0)
    await scrape_once(FakeAdapter(state=state), metrics)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_grid_import_watts") == 650.0
    assert sample(metrics, "enphase_grid_export_watts") == 0.0


async def test_grid_split_gauges_export_side(metrics: Metrics) -> None:
    state = make_state(grid_import_watts=0.0, grid_export_watts=1200.0)
    await scrape_once(FakeAdapter(state=state), metrics)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_grid_import_watts") == 0.0
    assert sample(metrics, "enphase_grid_export_watts") == 1200.0


async def test_state_age_reflects_clock_gap(metrics: Metrics) -> None:
    adapter = FakeAdapter(state=make_state(ts=STATE_TS))
    now = STATE_TS + timedelta(seconds=42)
    await scrape_once(adapter, metrics, now_fn=lambda: now)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_state_age_seconds") == pytest.approx(42.0)


async def test_writes_last_24h_publishes_from_ledger_counts(
    metrics: Metrics, ledger: Ledger, clock
) -> None:
    """Audit-trail-as-materialized-view: rows written by "another process"
    (here: directly into the ledger, bypassing this process's counter) show
    up on the daemon's gauge after one scrape."""
    now = clock.now
    clock.now = now - timedelta(hours=30)
    await ledger.record(action="set_mode", outcome="success")  # outside the window
    clock.now = now - timedelta(hours=2)
    await ledger.record(action="set_mode", outcome="success")
    await ledger.record(action="set_mode", outcome="success")
    await ledger.record(action="set_reserve", outcome="rejected")
    clock.now = now

    await scrape_once(FakeAdapter(), metrics, now_fn=clock, ledger=ledger)  # type: ignore[arg-type]

    name = "enphase_writes_last_24h"
    assert sample(metrics, name, {"action": "set_mode", "outcome": "success"}) == 2.0
    assert sample(metrics, name, {"action": "set_reserve", "outcome": "rejected"}) == 1.0
    # Zero-filled across the closed vocabulary once the ledger has been read.
    assert sample(metrics, name, {"action": "storm_guard", "outcome": "error"}) == 0.0
    # This process's own counter never saw those writes — that's the gap
    # the gauge closes.
    assert (
        sample(metrics, "enphase_writes_total", {"action": "set_mode", "outcome": "success"}) == 0.0
    )


async def test_writes_last_24h_refreshes_even_when_scrape_fails(
    metrics: Metrics, ledger: Ledger, clock
) -> None:
    await ledger.record(action="set_mode", outcome="success")
    adapter = FakeAdapter(error=CircuitOpen("open"), circuit_open=True)
    await scrape_once(adapter, metrics, now_fn=clock, ledger=ledger)  # type: ignore[arg-type]

    assert (
        sample(metrics, "enphase_writes_last_24h", {"action": "set_mode", "outcome": "success"})
        == 1.0
    )


async def test_writes_last_24h_absent_without_ledger(metrics: Metrics) -> None:
    await scrape_once(FakeAdapter(), metrics)  # type: ignore[arg-type]

    assert (
        sample(metrics, "enphase_writes_last_24h", {"action": "set_mode", "outcome": "success"})
        is None
    )
    assert "enphase_writes_last_24h{" not in generate_latest(metrics.registry).decode()


async def test_writes_last_24h_survives_ledger_read_failure(
    metrics: Metrics, ledger: Ledger, clock
) -> None:
    # Graceful degradation on the runtime path: the gauge keeps its last
    # value and the scrape still completes.
    await ledger.record(action="set_mode", outcome="success")
    await scrape_once(FakeAdapter(), metrics, now_fn=clock, ledger=ledger)  # type: ignore[arg-type]
    await ledger.close()
    await scrape_once(FakeAdapter(), metrics, now_fn=clock, ledger=ledger)  # type: ignore[arg-type]

    assert (
        sample(metrics, "enphase_writes_last_24h", {"action": "set_mode", "outcome": "success"})
        == 1.0
    )
    assert sample(metrics, "enphase_scrape_total", {"outcome": "success"}) == 2.0
