"""Daemon scrape-loop metrics: RED counters, domain gauges, bulkhead.

Each test builds its Metrics against a throwaway CollectorRegistry
(injected, never the process-global REGISTRY), so there is no cross-test
counter state to reset — isolation by construction instead of teardown.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from prometheus_client import CollectorRegistry

from enphase_agent.daemon import scrape_once
from enphase_agent.errors import AuthError, CircuitOpen, StaleStateError
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


async def test_state_age_reflects_clock_gap(metrics: Metrics) -> None:
    adapter = FakeAdapter(state=make_state(ts=STATE_TS))
    now = STATE_TS + timedelta(seconds=42)
    await scrape_once(adapter, metrics, now_fn=lambda: now)  # type: ignore[arg-type]

    assert sample(metrics, "enphase_state_age_seconds") == pytest.approx(42.0)
