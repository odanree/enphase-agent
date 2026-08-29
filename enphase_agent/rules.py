"""Deterministic rules engine — a strategy table (policy-as-data).

First match wins, so ordering in RULES *is* the precedence policy and stays
auditable in a diff. No LLM in this file, ever: anything that touches the
battery must be replayable and unit-testable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from .adapter import EnphaseAdapter
from .models import BatteryMode

Context = dict[str, Any]

DEFAULT_PEAK_HOURS = range(16, 21)
DEFAULT_OFF_PEAK_HOURS = range(0, 6)
LOW_SOC_FLOOR = 0.15


class Rule(TypedDict):
    name: str
    description: str
    when: Callable[[Context], bool]
    action: Callable[[EnphaseAdapter], Awaitable[None]]


async def _storm_prep(adapter: EnphaseAdapter) -> None:
    await adapter.enable_storm_guard(True, reason="rule:storm-prep")


async def _low_soc_guard(adapter: EnphaseAdapter) -> None:
    await adapter.set_reserve_soc(0.30, reason="rule:low-soc-guard")


async def _peak_discharge(adapter: EnphaseAdapter) -> None:
    await adapter.set_battery_mode(BatteryMode.SAVINGS, reason="rule:peak-discharge")


async def _offpeak_recharge(adapter: EnphaseAdapter) -> None:
    await adapter.set_battery_mode(
        BatteryMode.SELF_CONSUMPTION, reason="rule:offpeak-recharge"
    )


async def _default_self_consumption(adapter: EnphaseAdapter) -> None:
    await adapter.set_battery_mode(BatteryMode.SELF_CONSUMPTION, reason="rule:default")


RULES: list[Rule] = [
    {
        "name": "storm-prep",
        "description": "Storm forecast: arm storm guard, hold charge for the outage",
        "when": lambda ctx: bool(ctx.get("storm_forecast")),
        "action": _storm_prep,
    },
    {
        "name": "low-soc-guard",
        "description": f"SoC below {LOW_SOC_FLOOR:.0%}: raise reserve floor to 30%",
        "when": lambda ctx: float(ctx.get("soc", 1.0)) < LOW_SOC_FLOOR,
        "action": _low_soc_guard,
    },
    {
        "name": "peak-discharge",
        "description": "TOU peak window: discharge against the expensive tariff",
        "when": lambda ctx: ctx["hour"] in ctx.get("peak_hours", DEFAULT_PEAK_HOURS),
        "action": _peak_discharge,
    },
    {
        "name": "offpeak-recharge",
        "description": "Overnight off-peak: self-consumption while rates are cheap",
        "when": lambda ctx: ctx["hour"]
        in ctx.get("off_peak_hours", DEFAULT_OFF_PEAK_HOURS),
        "action": _offpeak_recharge,
    },
    {
        "name": "default-self-consumption",
        "description": "No special condition: plain self-consumption",
        "when": lambda ctx: True,
        "action": _default_self_consumption,
    },
]


def evaluate(context: Context) -> Rule:
    """First-match-wins keeps precedence auditable: reorder RULES, change policy."""
    for rule in RULES:
        if rule["when"](context):
            return rule
    raise AssertionError("RULES must end with a catch-all rule")


@dataclass(frozen=True)
class ScheduledAction:
    hour: int
    rule: str
    description: str
    execute: Callable[[EnphaseAdapter], Awaitable[None]] = field(
        compare=False, repr=False
    )


def plan_day(context: Context) -> list[ScheduledAction]:
    """Coalesced 24h schedule: one entry per rule transition, because the
    interesting hours are the tariff boundaries, not the 20 identical rows
    between them.

    SoC and storm inputs are snapshot values in this slice; hour-by-hour
    forecasting is a later PR.
    """
    plan: list[ScheduledAction] = []
    for hour in range(24):
        rule = evaluate({**context, "hour": hour})
        if plan and plan[-1].rule == rule["name"]:
            continue
        plan.append(
            ScheduledAction(
                hour=hour,
                rule=rule["name"],
                description=rule["description"],
                execute=rule["action"],
            )
        )
    return plan
