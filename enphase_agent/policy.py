"""Guardrails around the adapter: daily bulkhead, reserve bounds, HITL gate.

Policy owns "should we"; the adapter owns "how". Keeping safety rules out of
the ACL means an Enphase API change can never silently loosen them.

Every attempt — allowed, rejected, or failed — is recorded through an
injected `record_write(...)` callable (default: the real
`enphase_writes_total` counter) AND, when a `Ledger` is wired in, appended
to the durable audit ledger. That's write-through instrumentation: one
call site in the policy layer fans out to both sinks, so the counter and
the ledger can never disagree about what was attempted. The audit trail
lives here because this is the only chokepoint every write must pass
through; tests inject a stub so they never touch process-global counters.

The ledger is also what makes the mode-change bulkhead persistent: the
daily count is a query over today's committed rows, so a container
restart mid-day (or the fact that every CLI invocation is a fresh
process) cannot reset it. Without a ledger the policy falls back to the
original in-memory list — same semantics, no memory across processes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from .adapter import EnphaseAdapter
from .errors import PolicyRejected
from .ledger import Ledger
from .metrics import METRICS
from .models import BatteryMode

logger = logging.getLogger(__name__)


class WriteRecorder(Protocol):
    def __call__(
        self,
        action: str,
        outcome: str,
        *,
        target: str | None = None,
        reason: str | None = None,
        error_class: str | None = None,
    ) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def start_of_local_day(now: datetime) -> datetime:
    """Midnight of `now`'s calendar day in the process's local zone (`TZ` in
    compose), returned UTC-aware so it compares directly against ledger
    timestamps. "Per day" for a household bulkhead means the household's
    day, not UTC's."""
    if now.tzinfo is None:
        raise ValueError("bulkhead clock must be timezone-aware")
    local_midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


class BatteryPolicy:
    """Bulkhead against runaway callers; every rejection names its reason."""

    MAX_MODE_CHANGES_PER_DAY = 4
    RESERVE_BOUNDS = (0.10, 0.80)

    def __init__(
        self,
        adapter: EnphaseAdapter,
        *,
        now_fn: Callable[[], datetime] = _utcnow,
        record_write: WriteRecorder | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self._adapter = adapter
        self._now = now_fn
        self._record: WriteRecorder = (
            record_write if record_write is not None else METRICS.record_write
        )
        self._ledger = ledger
        # In-memory fallback for ledger-less construction (tests, ad-hoc
        # scripts). Not consulted when a ledger is present.
        self._mode_changes: list[datetime] = []

    async def set_battery_mode(
        self, mode: BatteryMode, reason: str, *, confirm: bool = False
    ) -> None:
        """HITL gate on FULL_BACKUP plus a daily change bulkhead — batteries
        don't enjoy being mode-flapped by a buggy scheduler."""
        target = mode.value
        try:
            if mode is BatteryMode.FULL_BACKUP and not confirm:
                raise PolicyRejected(
                    "FULL_BACKUP requires confirm=True (human-in-the-loop gate)"
                )
            if await self._mode_changes_today() >= self.MAX_MODE_CHANGES_PER_DAY:
                raise PolicyRejected(
                    f"bulkhead: {self.MAX_MODE_CHANGES_PER_DAY} mode changes already today"
                )
            await self._adapter.set_battery_mode(mode, reason)
        except PolicyRejected as exc:
            await self._audit("set_mode", "rejected", target=target, reason=f"{reason} ({exc})")
            raise
        except Exception as exc:
            await self._audit(
                "set_mode",
                "error",
                target=target,
                reason=reason,
                error_class=type(exc).__name__,
            )
            raise
        await self._audit("set_mode", "success", target=target, reason=reason)
        if self._ledger is None:
            self._mode_changes.append(self._now())

    async def set_reserve_soc(self, pct: float, reason: str) -> None:
        """Bounds check: below 10% risks the backup floor, above 80% starves
        self-consumption."""
        target = f"{pct:.2f}"
        try:
            lo, hi = self.RESERVE_BOUNDS
            if not lo <= pct <= hi:
                raise PolicyRejected(
                    f"reserve {pct:.0%} outside allowed bounds [{lo:.0%}, {hi:.0%}]"
                )
            await self._adapter.set_reserve_soc(pct, reason)
        except PolicyRejected as exc:
            await self._audit("set_reserve", "rejected", target=target, reason=f"{reason} ({exc})")
            raise
        except Exception as exc:
            await self._audit(
                "set_reserve",
                "error",
                target=target,
                reason=reason,
                error_class=type(exc).__name__,
            )
            raise
        await self._audit("set_reserve", "success", target=target, reason=reason)

    async def _mode_changes_today(self) -> int:
        """Bulkhead read. With a ledger this is the persistent count; a
        ledger read failure propagates and the write is refused — if the
        budget can't be verified it isn't spent (fail-closed), which is the
        opposite of how audit *writes* are treated below."""
        since = start_of_local_day(self._now())
        if self._ledger is not None:
            return await self._ledger.count_since(action="set_mode", outcome="success", since=since)
        return sum(1 for t in self._mode_changes if t >= since)

    async def _audit(
        self,
        action: str,
        outcome: str,
        *,
        target: str | None = None,
        reason: str | None = None,
        error_class: str | None = None,
    ) -> None:
        """Write-through fan-out: metrics counter first, then the ledger.
        A ledger failure is logged and swallowed — bulkhead: an audit sink
        going away must not turn a completed control action into an error
        the caller retries (which would then be a *second* battery write)."""
        self._record(action, outcome, target=target, reason=reason, error_class=error_class)
        if self._ledger is None:
            return
        try:
            await self._ledger.record(
                action=action,
                outcome=outcome,
                target=target,
                reason=reason,
                error_class=error_class,
            )
        except Exception:
            logger.exception(
                "ledger record failed for %s/%s; control action unaffected", action, outcome
            )
