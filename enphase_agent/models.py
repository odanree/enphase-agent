"""Our shapes, not Enphase's. Everything above the adapter speaks these."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class BatteryMode(Enum):
    """Our mode names; the adapter owns the mapping to Enphase's."""

    SELF_CONSUMPTION = "self_consumption"
    SAVINGS = "savings"
    FULL_BACKUP = "full_backup"


@dataclass(frozen=True, slots=True)
class SystemState:
    """Snapshot of the system. SoC/reserve are fractions (0.0-1.0), not percent —
    one unit convention above the ACL, conversions stay inside the adapter."""

    production_w: float
    consumption_w: float
    battery_soc: float
    battery_mode: BatteryMode
    reserve_soc: float
    storm_guard: bool
    ts: datetime
    stale: bool = False
