"""Pull-based metrics daemon: expose /metrics, scrape the gateway on a loop.

Prometheus pulls from us (`start_http_server`) — the daemon never pushes,
so a dead Prometheus costs nothing and a dead daemon is visible as a
missing target. The scrape loop is bulkheaded per iteration: one bad
`get_state` bumps an error counter and the loop keeps going, because a
metrics daemon that dies on the failure it exists to report is useless.

Read/write path separation: this process only ever reads the gateway.
Writes stay behind CLI + policy; nothing reachable from Grafana or
Prometheus can actuate the battery.

The same separation holds for the audit ledger: the daemon is a reader
only. Each iteration it republishes the ledger's trailing-24h counts as
`enphase_writes_last_24h` — the audit trail as a materialized view — so
writes made by one-shot CLI processes reach Prometheus through the one
process it scrapes.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Callable
from datetime import datetime, timezone

from prometheus_client import start_http_server

from .adapter import EnphaseAdapter
from .errors import AuthError, CircuitOpen, StaleStateError
from .ledger import Ledger
from .metrics import METRICS, Metrics
from .models import SystemState

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8000
DEFAULT_INTERVAL_S = 15.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, AuthError):
        return "auth"
    if isinstance(exc, CircuitOpen):
        return "circuit_open"
    if isinstance(exc, StaleStateError):
        return "stale"
    return "other"


def _publish_state(state: SystemState, metrics: Metrics, now: datetime) -> None:
    metrics.production_watts.set(state.production_w)
    metrics.consumption_watts.set(state.consumption_w)
    metrics.battery_soc_ratio.set(state.battery_soc)
    metrics.battery_reserve_ratio.set(state.reserve_soc)
    metrics.storm_guard_active.set(1.0 if state.storm_guard else 0.0)
    metrics.set_battery_mode(state.battery_mode)
    metrics.publish_optional_state(state)
    # Age comes from the state's own timestamp, not "did the call return" —
    # a cache-served stale read still moves this gauge, which is the point
    # of the freshness SLI.
    metrics.state_age_seconds.set(max(0.0, (now - state.ts).total_seconds()))


async def _publish_ledger(ledger: Ledger, metrics: Metrics) -> None:
    """Refresh the materialized view. Same per-iteration bulkhead as the
    gateway read: a ledger hiccup is logged, the gauge keeps its last value
    (LazyGauge semantics), and the loop continues."""
    try:
        metrics.publish_write_counts(await ledger.counts_by_label_last_24h())
    except Exception as exc:
        logger.warning("ledger read failed; enphase_writes_last_24h not refreshed: %s", exc)


async def scrape_once(
    adapter: EnphaseAdapter,
    metrics: Metrics = METRICS,
    now_fn: Callable[[], datetime] = _utcnow,
    ledger: Ledger | None = None,
) -> None:
    """One loop iteration. Never raises — the bulkhead lives here so the
    caller's loop stays a dumb `while`."""
    started = time.perf_counter()
    try:
        state = await adapter.get_state()
    except Exception as exc:
        metrics.record_scrape("error")
        metrics.record_scrape_error(_error_kind(exc))
        logger.warning("scrape failed (%s): %s", _error_kind(exc), exc)
    else:
        metrics.record_scrape("success")
        _publish_state(state, metrics, now_fn())
    finally:
        metrics.scrape_duration_seconds.observe(time.perf_counter() - started)
        metrics.circuit_breaker_open.set(1.0 if adapter.is_circuit_open() else 0.0)
        # Independent of gateway health on purpose: the audit view must stay
        # current even while the breaker is open.
        if ledger is not None:
            await _publish_ledger(ledger, metrics)


async def run_daemon(
    adapter: EnphaseAdapter,
    interval_s: float = DEFAULT_INTERVAL_S,
    *,
    port: int = DEFAULT_PORT,
    metrics: Metrics = METRICS,
    now_fn: Callable[[], datetime] = _utcnow,
    ledger: Ledger | None = None,
) -> None:
    """Serve /metrics on 0.0.0.0:`port` and scrape every `interval_s`.

    SIGTERM/SIGINT flip a stop event so shutdown is a normal loop exit:
    the in-flight iteration finishes, the adapter's aiohttp session is
    closed, and the process leaves with 0 — `docker stop` never has to
    escalate to SIGKILL.

    `ledger` arrives already opened — the CLI owns its lifecycle (async
    context manager) so a ledger that can't open fails the boot before
    /metrics is ever served, rather than being discovered mid-loop.
    """
    start_http_server(port)
    logger.info("metrics endpoint up on :%d/metrics, scraping every %.0fs", port, interval_s)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows dev host: no loop signal handlers; Ctrl+C still lands
            # as KeyboardInterrupt via asyncio.run. Production is Linux.
            pass

    try:
        while not stop.is_set():
            await scrape_once(adapter, metrics, now_fn, ledger)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                continue
    finally:
        await adapter.close()
        logger.info("daemon stopped cleanly")
