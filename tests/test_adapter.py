from __future__ import annotations

import pytest

from enphase_agent.adapter import _MODE_TO_ENPHASE
from enphase_agent.errors import AuthError, StaleStateError
from enphase_agent.models import BatteryMode


async def test_set_mode_is_idempotent(adapter, envoy):
    # Fake starts in SELF_CONSUMPTION; asking for it again must not burn a write.
    await adapter.set_battery_mode(BatteryMode.SELF_CONSUMPTION, reason="test")
    assert not [c for c in envoy.calls if c[0] == "set_storage_mode"]


async def test_set_mode_writes_when_different(adapter, envoy):
    await adapter.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    writes = [c for c in envoy.calls if c[0] == "set_storage_mode"]
    assert writes == [("set_storage_mode", _MODE_TO_ENPHASE[BatteryMode.SAVINGS])]


async def test_bad_auth_fails_fast_on_first_call(adapter, envoy):
    envoy.auth_ok = False
    with pytest.raises(AuthError):
        await adapter.get_state()


async def test_fresh_state_is_not_stale(adapter):
    state = await adapter.get_state()
    assert state.stale is False
    assert state.battery_soc == pytest.approx(0.55)
    assert state.reserve_soc == pytest.approx(0.20)


async def test_stale_flag_after_ten_minutes(adapter, envoy, clock):
    await adapter.get_state()
    clock.advance(minutes=11)
    envoy.update_error = ConnectionError("gateway offline")
    state = await adapter.get_state()
    assert state.stale is True


async def test_writes_refused_on_stale_state(adapter, envoy, clock):
    await adapter.get_state()
    clock.advance(minutes=11)
    envoy.update_error = ConnectionError("gateway offline")
    with pytest.raises(StaleStateError):
        await adapter.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    assert not [c for c in envoy.calls if c[0] == "set_storage_mode"]


async def test_fresh_cache_served_on_transient_failure(adapter, envoy):
    await adapter.get_state()
    envoy.update_error = ConnectionError("blip")
    state = await adapter.get_state()
    # Cache is still inside the 10-min window: degrade gracefully, not stale.
    assert state.stale is False


async def test_breaker_open_degrades_to_stale_and_blocks_writes(adapter, envoy):
    await adapter.get_state()
    envoy.update_error = ConnectionError("gateway offline")
    for _ in range(5):  # fail_max=5 trips the breaker
        await adapter.get_state()
    state = await adapter.get_state()
    assert state.stale is True
    with pytest.raises(StaleStateError):
        await adapter.set_reserve_soc(0.50, reason="test")


async def test_get_state_with_no_cache_propagates_failure(adapter, envoy):
    envoy.update_error = ConnectionError("gateway offline")
    with pytest.raises(ConnectionError):
        await adapter.get_state()


async def test_reserve_written_as_integer_percent(adapter, envoy):
    await adapter.set_reserve_soc(0.35, reason="test")
    assert ("set_reserve_soc", 35) in envoy.calls


async def test_storm_guard_idempotent(adapter, envoy):
    await adapter.enable_storm_guard(False, reason="test")  # already off
    assert not [c for c in envoy.calls if c[0] == "set_storm_guard"]
    await adapter.enable_storm_guard(True, reason="test")
    assert ("set_storm_guard", True) in envoy.calls
