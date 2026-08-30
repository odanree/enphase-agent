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

from dataclasses import dataclass, field

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

from .models import BatteryMode

SCRAPE_OUTCOMES: frozenset[str] = frozenset({"success", "error"})
ERROR_KINDS: frozenset[str] = frozenset({"auth", "circuit_open", "stale", "other"})
WRITE_ACTIONS: frozenset[str] = frozenset({"set_mode", "set_reserve", "storm_guard"})
WRITE_OUTCOMES: frozenset[str] = frozenset({"success", "rejected", "error"})

# Local-LAN calls to an embedded gateway: p50 well under a second, with a
# long tail when the gateway is choking. Buckets bracket both regimes.
_SCRAPE_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


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
