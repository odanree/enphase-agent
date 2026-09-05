"""Anti-corruption layer over Enphase's undocumented Enlighten / IQ Gateway API.

This is the ONLY module allowed to import pyenphase or know Enphase's shapes.
Everything upstream (policy, rules, CLI) speaks the dataclasses in models.py,
so an Enphase firmware or API change stays contained to this file.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from pyenphase import Envoy
from pyenphase.exceptions import EnvoyAuthenticationError
from pyenphase.models.tariff import EnvoyStorageMode

from .errors import AuthError, CircuitOpen, StaleStateError
from .models import BatteryMode, SystemState

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)
MIN_CALL_INTERVAL_S = 2.0
BREAKER_FAIL_MAX = 5
BREAKER_RESET_TIMEOUT_S = 300

_MODE_TO_ENPHASE: dict[BatteryMode, EnvoyStorageMode] = {
    BatteryMode.SELF_CONSUMPTION: EnvoyStorageMode.SELF_CONSUMPTION,
    BatteryMode.SAVINGS: EnvoyStorageMode.SAVINGS,
    BatteryMode.FULL_BACKUP: EnvoyStorageMode.BACKUP,
}
_MODE_FROM_ENPHASE: dict[EnvoyStorageMode, BatteryMode] = {
    v: k for k, v in _MODE_TO_ENPHASE.items()
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _opt_int(obj: Any, attr: str) -> int | None:
    """None-tolerant read for pyenphase's optional fields — an absent object
    or attribute becomes an absent SystemState field, never a crash."""
    value = getattr(obj, attr, None) if obj is not None else None
    return None if value is None else int(value)


def _opt_float(obj: Any, attr: str) -> float | None:
    value = getattr(obj, attr, None) if obj is not None else None
    return None if value is None else float(value)


class _AsyncCircuitBreaker:
    """Minimal asyncio-native circuit breaker. Three states: closed passes
    calls through; N consecutive failures open it for reset_timeout seconds;
    the first call after that is a probe (half-open) — success closes, any
    failure reopens. Excluded exception types don't count as failures — a 401
    is a trust-boundary problem, not an availability signal."""

    def __init__(
        self,
        fail_max: int,
        reset_timeout: float,
        exclude: tuple[type[BaseException], ...] = (),
    ) -> None:
        self._fail_max = fail_max
        self._reset_timeout = reset_timeout
        self._exclude = exclude
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        """Snapshot for observability. Lock-free read is fine: single event
        loop, and a one-iteration-stale gauge is harmless."""
        return (
            self._opened_at is not None
            and time.monotonic() - self._opened_at < self._reset_timeout
        )

    async def call_async(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        async with self._lock:
            if self._opened_at is not None:
                if time.monotonic() - self._opened_at < self._reset_timeout:
                    raise CircuitOpen("breaker is open")
                # half-open probe
                self._opened_at = None
        try:
            result = await fn(*args)
        except self._exclude:
            raise
        except Exception:
            async with self._lock:
                self._failures += 1
                if self._failures >= self._fail_max:
                    self._opened_at = time.monotonic()
            raise
        else:
            async with self._lock:
                self._failures = 0
            return result


class _TokenBucket:
    """Token-bucket rate limiter. The IQ Gateway runs a small embedded web
    server — hammering it is how local sessions get dropped, so we enforce a
    floor between outbound calls instead of trusting every caller."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_free = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next_free - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
            self._next_free = now + self._min_interval


class EnphaseAdapter:
    """ACL facade: our vocabulary in, our dataclasses out, Enphase inside."""

    def __init__(
        self,
        host: str,
        email: str,
        password: str,
        serial: str | None = None,
        *,
        envoy: Envoy | None = None,
        now_fn: Callable[[], datetime] = _utcnow,
        min_call_interval: float = MIN_CALL_INTERVAL_S,
    ) -> None:
        self._host = host
        self._email = email
        self._password = password
        # pyenphase discovers the serial during setup(); kept in case a future
        # entrez path wants it explicitly.
        self._serial = serial
        # Envoy() builds an aiohttp connector that requires a running event loop,
        # so we lazy-construct in _ensure_auth. Tests inject a fake via `envoy=`.
        self._envoy: Envoy | None = envoy
        self._now = now_fn
        self._authenticated = False
        self._cache: SystemState | None = None
        self._limiter = _TokenBucket(min_call_interval)
        self._breaker = _AsyncCircuitBreaker(
            fail_max=BREAKER_FAIL_MAX,
            reset_timeout=BREAKER_RESET_TIMEOUT_S,
            exclude=(EnvoyAuthenticationError,),
        )

    async def get_state(self) -> SystemState:
        """Fresh read when the gateway answers; graceful degradation to the
        cached snapshot (stale-flagged past STALE_AFTER) when it doesn't."""
        try:
            envoy = await self._ready_envoy()
            await self._call(envoy.update)
        except AuthError:
            raise
        except CircuitOpen:
            cached = self._cached(force_stale=True)
            if cached is None:
                raise
            return cached
        except Exception:
            cached = self._cached(force_stale=False)
            if cached is None:
                raise
            return cached
        assert self._envoy is not None
        state = self._to_state(self._envoy.data)
        self._cache = state
        return state

    async def set_battery_mode(self, mode: BatteryMode, reason: str) -> bool:
        """Idempotent: a write is only spent when the mode actually differs.

        Returns True when the gateway was actually written to, False on a
        no-op. The signal lets the policy layer skip counting a redundant
        call against the daily bulkhead — otherwise four repeated
        set-mode calls could exhaust the budget without ever touching
        the battery."""
        state = await self._require_fresh_state()
        if state.battery_mode is mode:
            logger.info("set_battery_mode no-op (already %s): %s", mode.name, reason)
            return False
        logger.info(
            "set_battery_mode %s -> %s: %s", state.battery_mode.name, mode.name, reason
        )
        envoy = await self._ready_envoy()
        await self._call(envoy.set_storage_mode, _MODE_TO_ENPHASE[mode])
        return True

    async def set_reserve_soc(self, pct: float, reason: str) -> None:
        """Fraction in, integer percent out — the unit translation stays in the ACL."""
        await self._require_fresh_state()
        logger.info("set_reserve_soc -> %.0f%%: %s", pct * 100, reason)
        envoy = await self._ready_envoy()
        await self._call(envoy.set_reserve_soc, round(pct * 100))

    def is_circuit_open(self) -> bool:
        """Breaker state for the metrics daemon. Exposed here so callers
        never reach into `_AsyncCircuitBreaker` — the ACL boundary covers
        our own internals, not just Enphase's."""
        return self._breaker.is_open

    async def close(self) -> None:
        """Release the underlying aiohttp session. Safe to call on a never-used
        adapter (envoy stays None if _ensure_auth was never reached)."""
        if self._envoy is not None:
            await self._envoy.close()
            self._envoy = None
            self._authenticated = False

    async def __aenter__(self) -> EnphaseAdapter:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _ready_envoy(self) -> Envoy:
        """Lazy-construct + authenticate before returning the client. Ensures
        every write/read path shares one initialization pattern."""
        await self._ensure_auth()
        assert self._envoy is not None
        return self._envoy

    async def enable_storm_guard(self, enabled: bool, reason: str) -> None:
        """Not exposed by pyenphase 4.0.1 — storm guard is an Enlighten cloud
        feature (Envoy.request('/service/...')) that we'll wire in a follow-up.
        Failing loud beats a silent AttributeError deep inside a control loop."""
        raise NotImplementedError(
            "storm_guard writes require a raw Enlighten cloud call — not yet wired"
        )

    async def _require_fresh_state(self) -> SystemState:
        """Stale reads are tolerated; stale writes are not — acting on a 10-min-old
        picture of the battery is how you discharge into a storm."""
        state = await self.get_state()
        if state.stale:
            raise StaleStateError(
                f"refusing write: last known state is stale (ts={state.ts.isoformat()})"
            )
        return state

    async def _call(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        await self._limiter.acquire()
        await self._ensure_auth()
        try:
            return await self._breaker.call_async(fn, *args)
        except EnvoyAuthenticationError as exc:
            # Token expired mid-session: force a re-auth on the next call.
            self._authenticated = False
            raise AuthError(str(exc)) from exc

    async def _ensure_auth(self) -> None:
        """Fail-fast at the trust boundary: bad credentials surface on the first
        call, not deep inside a control loop hours later."""
        if self._envoy is None:
            self._envoy = Envoy(self._host)
        if self._authenticated:
            return
        try:
            await self._envoy.setup()
            await self._envoy.authenticate(username=self._email, password=self._password)
        except EnvoyAuthenticationError as exc:
            raise AuthError(f"Enlighten rejected credentials for {self._email}") from exc
        self._authenticated = True

    def _cached(self, *, force_stale: bool) -> SystemState | None:
        if self._cache is None:
            return None
        stale = force_stale or (self._now() - self._cache.ts) > STALE_AFTER
        return replace(self._cache, stale=stale)

    def _to_state(self, data: Any) -> SystemState:
        """The one translation point from Enphase's shapes to ours."""
        storage = data.tariff.storage_settings
        production = data.system_production
        # These three are `| None` on EnvoyData (CT-metered gateways only),
        # so every read below goes through the None-tolerant helpers — an
        # absent meter must degrade to absent fields, not crash the daemon.
        consumption = getattr(data, "system_consumption", None)
        net = getattr(data, "system_net_consumption", None)
        encharge = getattr(data, "encharge_aggregate", None)
        # pyenphase docs net consumption as "grid import/export"; Enphase's
        # net-consumption CT reports positive = importing from grid.
        # TODO(verify): confirm the sign on live hardware — the pyenphase
        # docstring names the concept but not the sign convention.
        net_w = _opt_float(net, "watts_now")
        return SystemState(
            production_w=float(production.watts_now),
            consumption_w=float(data.system_consumption.watts_now),
            battery_soc=float(data.encharge_aggregate.state_of_charge) / 100.0,
            battery_mode=_MODE_FROM_ENPHASE[storage.mode],
            reserve_soc=float(storage.reserved_soc) / 100.0,
            # pyenphase 4.0.1 doesn't surface storm guard; getattr fallback
            # reports off rather than crashing when the attr is absent.
            storm_guard=bool(getattr(storage, "storm_guard", False)),
            ts=self._now(),
            stale=False,
            production_wh_today=_opt_int(production, "watt_hours_today"),
            production_wh_7d=_opt_int(production, "watt_hours_last_7_days"),
            production_wh_lifetime=_opt_int(production, "watt_hours_lifetime"),
            consumption_wh_today=_opt_int(consumption, "watt_hours_today"),
            consumption_wh_7d=_opt_int(consumption, "watt_hours_last_7_days"),
            consumption_wh_lifetime=_opt_int(consumption, "watt_hours_lifetime"),
            battery_energy_available_wh=_opt_int(encharge, "available_energy"),
            battery_energy_capacity_wh=_opt_int(encharge, "max_available_capacity"),
            grid_import_watts=None if net_w is None else max(net_w, 0.0),
            grid_export_watts=None if net_w is None else max(-net_w, 0.0),
        )
