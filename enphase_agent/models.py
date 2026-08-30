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
    # Energy accumulators (watt-hours). Optional because they only exist on
    # CT-metered gateways — None means "the gateway didn't report it", and
    # the metrics layer treats None as "don't publish", never as zero.
    production_wh_today: int | None = None
    production_wh_7d: int | None = None
    production_wh_lifetime: int | None = None
    consumption_wh_today: int | None = None
    consumption_wh_7d: int | None = None
    consumption_wh_lifetime: int | None = None
    battery_energy_available_wh: int | None = None
    battery_energy_capacity_wh: int | None = None
    # Directional gauges over signed values: the gateway reports one signed
    # net-consumption power, but signed metrics are hard to stack, hard to
    # color-threshold, and confuse rate() downstream. Split into two
    # always-non-negative gauges; at most one is nonzero at a time.
    grid_import_watts: float | None = None
    grid_export_watts: float | None = None
