from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from enphase_agent.errors import PolicyRejected
from enphase_agent.models import BatteryMode
from enphase_agent.policy import BatteryPolicy


@pytest.fixture
def fake_adapter() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def policy(fake_adapter, clock) -> BatteryPolicy:
    return BatteryPolicy(fake_adapter, now_fn=clock)


async def test_bulkhead_rejects_fifth_change_same_day(policy, fake_adapter):
    for mode in [BatteryMode.SAVINGS, BatteryMode.SELF_CONSUMPTION] * 2:
        await policy.set_battery_mode(mode, reason="test")
    with pytest.raises(PolicyRejected, match="bulkhead"):
        await policy.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    assert fake_adapter.set_battery_mode.await_count == 4


async def test_bulkhead_resets_next_day(policy, clock):
    for _ in range(4):
        await policy.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    clock.advance(days=1)
    await policy.set_battery_mode(BatteryMode.SAVINGS, reason="test")


async def test_full_backup_without_confirm_rejected(policy, fake_adapter):
    with pytest.raises(PolicyRejected, match="confirm"):
        await policy.set_battery_mode(BatteryMode.FULL_BACKUP, reason="storm")
    fake_adapter.set_battery_mode.assert_not_awaited()


async def test_full_backup_with_confirm_passes(policy, fake_adapter):
    await policy.set_battery_mode(BatteryMode.FULL_BACKUP, reason="storm", confirm=True)
    fake_adapter.set_battery_mode.assert_awaited_once_with(
        BatteryMode.FULL_BACKUP, "storm"
    )


@pytest.mark.parametrize("pct", [0.05, 0.90])
async def test_reserve_out_of_bounds_rejected(policy, fake_adapter, pct):
    with pytest.raises(PolicyRejected, match="bounds"):
        await policy.set_reserve_soc(pct, reason="test")
    fake_adapter.set_reserve_soc.assert_not_awaited()


@pytest.mark.parametrize("pct", [0.10, 0.45, 0.80])
async def test_reserve_in_bounds_passes(policy, fake_adapter, pct):
    await policy.set_reserve_soc(pct, reason="test")
    fake_adapter.set_reserve_soc.assert_awaited_once_with(pct, "test")
