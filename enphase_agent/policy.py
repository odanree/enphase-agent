"""Guardrails around the adapter: daily bulkhead, reserve bounds, HITL gate.

Policy owns "should we"; the adapter owns "how". Keeping safety rules out of
the ACL means an Enphase API change can never silently loosen them.

Every attempt — allowed, rejected, or failed — is recorded through an
injected `record_write(action, outcome)` callable (default: the real
`enphase_writes_total` counter). The audit trail lives at the policy layer
because that's the only chokepoint every write must pass through; tests
inject a stub so they never touch process-global counters.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .adapter import EnphaseAdapter
from .errors import PolicyRejected
from .metrics import METRICS
from .models import BatteryMode

WriteRecorder = Callable[[str, str], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    ) -> None:
        self._adapter = adapter
        self._now = now_fn
        self._record = record_write if record_write is not None else METRICS.record_write
        # In-memory ledger for now: a process restart resets the count.
        # SQLite persistence is the next PR — deliberately out of this slice.
        self._mode_changes: list[datetime] = []

    async def set_battery_mode(
        self, mode: BatteryMode, reason: str, *, confirm: bool = False
    ) -> None:
        """HITL gate on FULL_BACKUP plus a daily change bulkhead — batteries
        don't enjoy being mode-flapped by a buggy scheduler."""
        try:
            if mode is BatteryMode.FULL_BACKUP and not confirm:
                raise PolicyRejected(
                    "FULL_BACKUP requires confirm=True (human-in-the-loop gate)"
                )
            today = self._now().date()
            changes_today = sum(1 for t in self._mode_changes if t.date() == today)
            if changes_today >= self.MAX_MODE_CHANGES_PER_DAY:
                raise PolicyRejected(
                    f"bulkhead: {self.MAX_MODE_CHANGES_PER_DAY} mode changes already today"
                )
            await self._adapter.set_battery_mode(mode, reason)
        except PolicyRejected:
            self._record("set_mode", "rejected")
            raise
        except Exception:
            self._record("set_mode", "error")
            raise
        self._record("set_mode", "success")
        self._mode_changes.append(self._now())

    async def set_reserve_soc(self, pct: float, reason: str) -> None:
        """Bounds check: below 10% risks the backup floor, above 80% starves
        self-consumption."""
        try:
            lo, hi = self.RESERVE_BOUNDS
            if not lo <= pct <= hi:
                raise PolicyRejected(
                    f"reserve {pct:.0%} outside allowed bounds [{lo:.0%}, {hi:.0%}]"
                )
            await self._adapter.set_reserve_soc(pct, reason)
        except PolicyRejected:
            self._record("set_reserve", "rejected")
            raise
        except Exception:
            self._record("set_reserve", "error")
            raise
        self._record("set_reserve", "success")
