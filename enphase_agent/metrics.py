"""Prometheus metric declarations — RED method + cardinality discipline.

Two families, deliberately separated:

* Domain gauges describe the battery (production, SoC, mode). They answer
  "is the house OK".
* Service metrics follow the RED method — Rate (`enphase_scrape_total`),
  Errors (`enphase_scrape_errors_total`), Duration
  (`enphase_scrape_duration_seconds`) — and answer "is the agent OK".

Cardinality discipline: every label value comes from a closed vocabulary
declared in this module. No free-form strings ever reach a label — one
rogue `reason` label with a timestamp in it and Prometheus grows a new
time series per scrape. `Metrics.record_write` enforces the vocabulary at
the call site instead of trusting callers.

Metrics are built by a factory against an injectable registry so tests get
a throwaway `CollectorRegistry` per test (no cross-test state leak); the
process-wide singleton `METRICS` lives on the default registry that
`prometheus_client.start_http_server` serves.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily

from .models import BatteryMode, SystemState

SCRAPE_OUTCOMES: frozenset[str] = frozenset({"success", "error"})
ERROR_KINDS: frozenset[str] = frozenset({"auth", "circuit_open", "stale", "other"})
WRITE_ACTIONS: frozenset[str] = frozenset({"set_mode", "set_reserve", "storm_guard"})
WRITE_OUTCOMES: frozenset[str] = frozenset({"success", "rejected", "error"})

# Local-LAN calls to an embedded gateway: p50 well under a second, with a
# long tail when the gateway is choking. Buckets bracket both regimes.
_SCRAPE_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _f(value: int | None) -> float | None:
    return None if value is None else float(value)


class LazyGauge:
    """Gauge that is absent from exposition until first `set()`.

    prometheus_client's unlabeled Gauge exports 0.0 from the moment it is
    registered — for the optional energy accumulators (CT-metered gateways
    only) that initial zero is a lie, and "no data" must stay queryable as
    an absent series (`absent(...)`), not a phantom 0 Wh. Once set, the
    last value persists across a transiently missing upstream field — same
    graceful-degradation contract as the adapter's stale cache, with
    `enphase_state_age_seconds` as the freshness signal.
    """

    def __init__(self, name: str, documentation: str, registry: CollectorRegistry) -> None:
        self._name = name
        self._documentation = documentation
        self._value: float | None = None
        registry.register(self)

    def set(self, value: float) -> None:
        self._value = float(value)

    def describe(self) -> Iterable[GaugeMetricFamily]:
        # Lets the registry learn our name at register() time without
        # collect() emitting a sample before the first real value.
        return [GaugeMetricFamily(self._name, self._documentation)]

    def collect(self) -> Iterable[GaugeMetricFamily]:
        if self._value is None:
            return []
        return [GaugeMetricFamily(self._name, self._documentation, value=self._value)]


@dataclass(frozen=True, slots=True)
class Metrics:
    """All collectors for one registry, so the whole set is injectable."""

    production_watts: Gauge
    consumption_watts: Gauge
    battery_soc_ratio: Gauge
    battery_reserve_ratio: Gauge
    battery_mode: Gauge
    storm_guard_active: Gauge
    state_age_seconds: Gauge
    scrape_total: Counter
    scrape_errors_total: Counter
    scrape_duration_seconds: Histogram
    circuit_breaker_open: Gauge
    writes_total: Counter
    # Energy accumulators — Gauges, not Counters, on purpose. The lifetime
    # figure is monotonic at the source, but a Counter's inc() contract fits
    # a value we own, not one we mirror; a monotonically-rising Gauge still
    # works with rate()/increase(), and `_total` on a Gauge would mislead.
    # today resets at midnight and 7d is a rolling window — textbook Gauges.
    energy_produced_today_wh: LazyGauge
    energy_produced_7d_wh: LazyGauge
    energy_produced_lifetime_wh: LazyGauge
    energy_consumed_today_wh: LazyGauge
    energy_consumed_7d_wh: LazyGauge
    energy_consumed_lifetime_wh: LazyGauge
    battery_energy_available_wh: LazyGauge
    battery_energy_capacity_wh: LazyGauge
    grid_import_watts: LazyGauge
    grid_export_watts: LazyGauge
    registry: CollectorRegistry = field(default_factory=CollectorRegistry)

    def set_battery_mode(self, active: BatteryMode) -> None:
        """Info-style gauge: exactly one mode label carries 1, the rest 0,
        so PromQL can select the active mode without string matching."""
        for mode in BatteryMode:
            self.battery_mode.labels(mode=mode.value).set(1.0 if mode is active else 0.0)

    def record_scrape(self, outcome: str) -> None:
        if outcome not in SCRAPE_OUTCOMES:
            raise ValueError(f"unknown scrape outcome {outcome!r}")
        self.scrape_total.labels(outcome=outcome).inc()

    def record_scrape_error(self, kind: str) -> None:
        if kind not in ERROR_KINDS:
            raise ValueError(f"unknown error kind {kind!r}")
        self.scrape_errors_total.labels(kind=kind).inc()

    def publish_optional_state(self, state: SystemState) -> None:
        """Publish the gauges whose upstream fields may be absent. None means
        skip — the series stays absent (or keeps its last value) instead of
        emitting a fake zero. `grid_import`/`grid_export` are the directional
        split of one signed net-consumption reading; by construction at most
        one is nonzero."""
        pairs: tuple[tuple[LazyGauge, float | None], ...] = (
            (self.energy_produced_today_wh, _f(state.production_wh_today)),
            (self.energy_produced_7d_wh, _f(state.production_wh_7d)),
            (self.energy_produced_lifetime_wh, _f(state.production_wh_lifetime)),
            (self.energy_consumed_today_wh, _f(state.consumption_wh_today)),
            (self.energy_consumed_7d_wh, _f(state.consumption_wh_7d)),
            (self.energy_consumed_lifetime_wh, _f(state.consumption_wh_lifetime)),
            (self.battery_energy_available_wh, _f(state.battery_energy_available_wh)),
            (self.battery_energy_capacity_wh, _f(state.battery_energy_capacity_wh)),
            (self.grid_import_watts, state.grid_import_watts),
            (self.grid_export_watts, state.grid_export_watts),
        )
        for gauge, value in pairs:
            if value is not None:
                gauge.set(value)

    def record_write(self, action: str, outcome: str) -> None:
        """The audit-trail counter the policy layer bumps. Vocabulary is
        enforced here so a future caller can't mint labels per invocation."""
        if action not in WRITE_ACTIONS:
            raise ValueError(f"unknown write action {action!r}")
        if outcome not in WRITE_OUTCOMES:
            raise ValueError(f"unknown write outcome {outcome!r}")
        self.writes_total.labels(action=action, outcome=outcome).inc()


def build_metrics(registry: CollectorRegistry | None = None) -> Metrics:
    """Declare every collector against `registry`.

    Label combinations are pre-seeded to zero so `rate()` sees a series
    from process start instead of appearing only after the first event —
    a counter that materializes mid-incident reads as a rate spike.
    """
    reg = registry if registry is not None else CollectorRegistry()
    m = Metrics(
        production_watts=Gauge(
            "enphase_production_watts", "Solar production right now.", registry=reg
        ),
        consumption_watts=Gauge(
            "enphase_consumption_watts", "Household consumption right now.", registry=reg
        ),
        battery_soc_ratio=Gauge(
            "enphase_battery_soc_ratio", "Battery state of charge (0-1).", registry=reg
        ),
        battery_reserve_ratio=Gauge(
            "enphase_battery_reserve_ratio", "Configured reserve SoC (0-1).", registry=reg
        ),
        battery_mode=Gauge(
            "enphase_battery_mode",
            "Info-style gauge: 1 for the active battery mode, 0 otherwise.",
            labelnames=("mode",),
            registry=reg,
        ),
        storm_guard_active=Gauge(
            "enphase_storm_guard_active", "Storm Guard enabled (0|1).", registry=reg
        ),
        state_age_seconds=Gauge(
            "enphase_state_age_seconds",
            "Seconds since the last successful gateway read — the freshness SLI.",
            registry=reg,
        ),
        scrape_total=Counter(
            "enphase_scrape_total",
            "Scrape loop iterations by outcome (RED: rate).",
            labelnames=("outcome",),
            registry=reg,
        ),
        scrape_errors_total=Counter(
            "enphase_scrape_errors_total",
            "Scrape failures by kind (RED: errors, subclassed).",
            labelnames=("kind",),
            registry=reg,
        ),
        scrape_duration_seconds=Histogram(
            "enphase_scrape_duration_seconds",
            "get_state latency (RED: duration).",
            buckets=_SCRAPE_BUCKETS,
            registry=reg,
        ),
        circuit_breaker_open=Gauge(
            "enphase_circuit_breaker_open",
            "Adapter circuit breaker state (1 = open, calls failing fast).",
            registry=reg,
        ),
        writes_total=Counter(
            "enphase_writes_total",
            "Battery write attempts through the policy layer, by action and outcome.",
            labelnames=("action", "outcome"),
            registry=reg,
        ),
        energy_produced_today_wh=LazyGauge(
            "enphase_energy_produced_today_watt_hours",
            "Solar energy produced since local midnight (gateway accumulator; resets daily).",
            registry=reg,
        ),
        energy_produced_7d_wh=LazyGauge(
            "enphase_energy_produced_7d_watt_hours",
            "Solar energy produced over the previous 7 days, excluding today (rolling window).",
            registry=reg,
        ),
        energy_produced_lifetime_wh=LazyGauge(
            "enphase_energy_produced_lifetime_watt_hours",
            "Lifetime solar energy produced. Monotonic at the source but exported "
            "as a Gauge: we mirror the value, we don't own the increments.",
            registry=reg,
        ),
        energy_consumed_today_wh=LazyGauge(
            "enphase_energy_consumed_today_watt_hours",
            "Household energy consumed since local midnight (CT-metered gateways only).",
            registry=reg,
        ),
        energy_consumed_7d_wh=LazyGauge(
            "enphase_energy_consumed_7d_watt_hours",
            "Household energy consumed over the previous 7 days, excluding today.",
            registry=reg,
        ),
        energy_consumed_lifetime_wh=LazyGauge(
            "enphase_energy_consumed_lifetime_watt_hours",
            "Lifetime household energy consumed (CT-metered gateways only).",
            registry=reg,
        ),
        battery_energy_available_wh=LazyGauge(
            "enphase_battery_energy_available_watt_hours",
            "Usable energy in the battery right now.",
            registry=reg,
        ),
        battery_energy_capacity_wh=LazyGauge(
            "enphase_battery_energy_capacity_watt_hours",
            "Battery nameplate capacity.",
            registry=reg,
        ),
        grid_import_watts=LazyGauge(
            "enphase_grid_import_watts",
            "Power drawn from the grid right now (>= 0). Directional split of the "
            "signed net-consumption reading; its pair is enphase_grid_export_watts.",
            registry=reg,
        ),
        grid_export_watts=LazyGauge(
            "enphase_grid_export_watts",
            "Power pushed to the grid right now (>= 0). At most one of "
            "import/export is nonzero by construction.",
            registry=reg,
        ),
        registry=reg,
    )
    for outcome in sorted(SCRAPE_OUTCOMES):
        m.scrape_total.labels(outcome=outcome)
    for kind in sorted(ERROR_KINDS):
        m.scrape_errors_total.labels(kind=kind)
    for action in sorted(WRITE_ACTIONS):
        for outcome in sorted(WRITE_OUTCOMES):
            m.writes_total.labels(action=action, outcome=outcome)
    for mode in BatteryMode:
        m.battery_mode.labels(mode=mode.value)
    return m


METRICS: Metrics = build_metrics(REGISTRY)
