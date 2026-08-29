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

import pybreaker
from pyenphase import Envoy
from pyenphase.exceptions import EnvoyAuthenticationError

# TODO(verify): EnvoyStorageMode import path and member names against the
# pyenphase release pinned in pyproject (pyenphase.models.tariff as of 1.x).
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
        self._email = email
        self._password = password
        # pyenphase discovers the serial during setup(); kept because the
        # entrez token request is scoped per-serial.
        # TODO(verify): whether Envoy.authenticate needs the serial passed
        # explicitly or reads it from setup() discovery.
        self._serial = serial
        self._envoy: Envoy = envoy if envoy is not None else Envoy(host)
        self._now = now_fn
        self._authenticated = False
        self._cache: SystemState | None = None
        self._limiter = _TokenBucket(min_call_interval)
        # Auth failures are excluded from the breaker: a 401 is a trust-boundary
        # problem, not an availability signal — it must fail fast, not trip us open.
        self._breaker = pybreaker.CircuitBreaker(
            fail_max=BREAKER_FAIL_MAX,
            reset_timeout=BREAKER_RESET_TIMEOUT_S,
            exclude=[EnvoyAuthenticationError],
        )

    async def get_state(self) -> SystemState:
        """Fresh read when the gateway answers; graceful degradation to the
        cached snapshot (stale-flagged past STALE_AFTER) when it doesn't."""
        try:
            await self._call(self._envoy.update)
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
        state = self._to_state(self._envoy.data)
        self._cache = state
        return state

    async def set_battery_mode(self, mode: BatteryMode, reason: str) -> None:
        """Idempotent: a write is only spent when the mode actually differs."""
        state = await self._require_fresh_state()
        if state.battery_mode is mode:
            logger.info("set_battery_mode no-op (already %s): %s", mode.name, reason)
            return
        logger.info(
            "set_battery_mode %s -> %s: %s", state.battery_mode.name, mode.name, reason
        )
        # TODO(verify): Envoy.set_storage_mode is what the HA integration uses
        # for mode writes — confirm name/signature on the pinned release.
        await self._call(self._envoy.set_storage_mode, _MODE_TO_ENPHASE[mode])

    async def set_reserve_soc(self, pct: float, reason: str) -> None:
        """Fraction in, integer percent out — the unit translation stays in the ACL."""
        await self._require_fresh_state()
        logger.info("set_reserve_soc -> %.0f%%: %s", pct * 100, reason)
        # TODO(verify): Envoy.set_reserve_soc name/signature (integer percent?).
        await self._call(self._envoy.set_reserve_soc, round(pct * 100))

    async def enable_storm_guard(self, enabled: bool, reason: str) -> None:
        """Idempotent for the same reason mode writes are."""
        state = await self._require_fresh_state()
        if state.storm_guard == enabled:
            logger.info("enable_storm_guard no-op (already %s): %s", enabled, reason)
            return
        logger.info("enable_storm_guard -> %s: %s", enabled, reason)
        # TODO(verify): pyenphase may not expose storm guard writes at all; the
        # method we want is Envoy.set_storm_guard(enabled: bool). If absent,
        # this is the one place we'd add a raw /service/... cloud call.
        await self._call(self._envoy.set_storm_guard, enabled)

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
        except pybreaker.CircuitBreakerError as exc:
            raise CircuitOpen(str(exc)) from exc
        except EnvoyAuthenticationError as exc:
            # Token expired mid-session: force a re-auth on the next call.
            self._authenticated = False
            raise AuthError(str(exc)) from exc

    async def _ensure_auth(self) -> None:
        """Fail-fast at the trust boundary: bad credentials surface on the first
        call, not deep inside a control loop hours later."""
        if self._authenticated:
            return
        try:
            await self._envoy.setup()
            # TODO(verify): authenticate() kwarg names (username/password) and
            # whether a cached entrez JWT can be passed via token=... to skip
            # the Enlighten login round-trip.
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
        # TODO(verify): attribute names against pyenphase's EnvoyData — based on
        # the shapes the Home Assistant enphase_envoy integration consumes
        # (system_production/system_consumption watts_now, encharge_aggregate
        # state_of_charge, tariff.storage_settings mode/reserved_soc).
        storage = data.tariff.storage_settings
        return SystemState(
            production_w=float(data.system_production.watts_now),
            consumption_w=float(data.system_consumption.watts_now),
            battery_soc=float(data.encharge_aggregate.state_of_charge) / 100.0,
            battery_mode=_MODE_FROM_ENPHASE[storage.mode],
            reserve_soc=float(storage.reserved_soc) / 100.0,
            # TODO(verify): storm guard read location; getattr fallback keeps a
            # missing attribute from looking like an outage.
            storm_guard=bool(getattr(storage, "storm_guard", False)),
            ts=self._now(),
            stale=False,
        )
