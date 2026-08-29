from __future__ import annotations

from unittest.mock import AsyncMock

from enphase_agent.models import BatteryMode
from enphase_agent.rules import evaluate, plan_day

BASE = {"soc": 0.55, "storm_forecast": False}


def test_first_match_wins_storm_beats_peak():
    rule = evaluate({**BASE, "hour": 17, "storm_forecast": True})
    assert rule["name"] == "storm-prep"


def test_low_soc_guard_fires():
    assert evaluate({**BASE, "hour": 10, "soc": 0.10})["name"] == "low-soc-guard"


def test_peak_hour_discharges():
    assert evaluate({**BASE, "hour": 17})["name"] == "peak-discharge"


def test_offpeak_hour_recharges():
    assert evaluate({**BASE, "hour": 3})["name"] == "offpeak-recharge"


def test_quiet_hour_falls_through_to_default():
    assert evaluate({**BASE, "hour": 10})["name"] == "default-self-consumption"


async def test_peak_action_sets_savings_mode():
    adapter = AsyncMock()
    rule = evaluate({**BASE, "hour": 17})
    await rule["action"](adapter)
    adapter.set_battery_mode.assert_awaited_once_with(
        BatteryMode.SAVINGS, reason="rule:peak-discharge"
    )


async def test_storm_action_arms_storm_guard():
    adapter = AsyncMock()
    rule = evaluate({**BASE, "hour": 17, "storm_forecast": True})
    await rule["action"](adapter)
    adapter.enable_storm_guard.assert_awaited_once_with(True, reason="rule:storm-prep")


def test_plan_day_transitions_at_tariff_boundaries():
    plan = plan_day(dict(BASE))
    assert [(a.hour, a.rule) for a in plan] == [
        (0, "offpeak-recharge"),
        (6, "default-self-consumption"),
        (16, "peak-discharge"),
        (21, "default-self-consumption"),
    ]


def test_plan_day_storm_collapses_to_single_entry():
    plan = plan_day({**BASE, "storm_forecast": True})
    assert [(a.hour, a.rule) for a in plan] == [(0, "storm-prep")]


def test_plan_day_respects_custom_tariff_windows():
    plan = plan_day({**BASE, "peak_hours": range(18, 22), "off_peak_hours": range(0, 7)})
    assert (18, "peak-discharge") in [(a.hour, a.rule) for a in plan]
    assert (22, "default-self-consumption") in [(a.hour, a.rule) for a in plan]
