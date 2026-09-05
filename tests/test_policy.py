from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from prometheus_client import CollectorRegistry

from enphase_agent.errors import LedgerError, PolicyRejected
from enphase_agent.metrics import build_metrics
from enphase_agent.models import BatteryMode
from enphase_agent.policy import BatteryPolicy


@pytest.fixture
def fake_adapter() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def writes() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def policy(fake_adapter, clock, writes) -> BatteryPolicy:
    # Stub recorder: keeps tests off the process-global Prometheus counters.
    # No ledger → exercises the in-memory bulkhead fallback.
    return BatteryPolicy(
        fake_adapter, now_fn=clock, record_write=lambda a, o, **kw: writes.append((a, o))
    )


@pytest.fixture
def ledger_policy(fake_adapter, clock, writes, ledger) -> BatteryPolicy:
    return BatteryPolicy(
        fake_adapter,
        now_fn=clock,
        record_write=lambda a, o, **kw: writes.append((a, o)),
        ledger=ledger,
    )


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


async def test_write_audit_records_every_outcome(policy, fake_adapter, writes):
    await policy.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    with pytest.raises(PolicyRejected):
        await policy.set_battery_mode(BatteryMode.FULL_BACKUP, reason="storm")
    fake_adapter.set_reserve_soc.side_effect = RuntimeError("gateway down")
    with pytest.raises(RuntimeError):
        await policy.set_reserve_soc(0.30, reason="test")

    assert writes == [
        ("set_mode", "success"),
        ("set_mode", "rejected"),
        ("set_reserve", "error"),
    ]


async def test_no_ledger_uses_in_memory_bulkhead(fake_adapter, clock, writes):
    """Ledger-less construction keeps the original per-instance semantics:
    the count lives on the object, so a fresh instance starts at zero."""
    first = BatteryPolicy(fake_adapter, now_fn=clock, record_write=lambda a, o, **kw: None)
    for _ in range(4):
        await first.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    with pytest.raises(PolicyRejected, match="bulkhead"):
        await first.set_battery_mode(BatteryMode.SAVINGS, reason="test")

    second = BatteryPolicy(fake_adapter, now_fn=clock, record_write=lambda a, o, **kw: None)
    await second.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    assert fake_adapter.set_battery_mode.await_count == 5


# --- ledger-backed policy ---------------------------------------------------


async def test_bulkhead_persists_across_process_boundaries(fake_adapter, clock, ledger):
    """Persistent bulkhead across process boundaries: the daily count is a
    ledger query, so a brand-new BatteryPolicy over the same DB (what a
    container restart or the next one-shot CLI process is) still sees the
    four changes already spent today."""
    first = BatteryPolicy(
        fake_adapter, now_fn=clock, record_write=lambda a, o, **kw: None, ledger=ledger
    )
    for mode in [BatteryMode.SAVINGS, BatteryMode.SELF_CONSUMPTION] * 2:
        await first.set_battery_mode(mode, reason="test")

    second = BatteryPolicy(
        fake_adapter, now_fn=clock, record_write=lambda a, o, **kw: None, ledger=ledger
    )
    with pytest.raises(PolicyRejected, match="bulkhead"):
        await second.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    assert fake_adapter.set_battery_mode.await_count == 4
    assert await ledger.count_since(action="set_mode", outcome="success", since=clock.now) == 4


async def test_ledger_bulkhead_resets_next_day(ledger_policy, clock):
    for _ in range(4):
        await ledger_policy.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    clock.advance(days=1)
    await ledger_policy.set_battery_mode(BatteryMode.SAVINGS, reason="test")


async def test_ledger_bulkhead_counts_only_successes(ledger_policy, fake_adapter, ledger):
    # Rejections and errors are audited but must not consume the budget.
    with pytest.raises(PolicyRejected):
        await ledger_policy.set_battery_mode(BatteryMode.FULL_BACKUP, reason="no confirm")
    fake_adapter.set_battery_mode.side_effect = RuntimeError("gateway down")
    with pytest.raises(RuntimeError):
        await ledger_policy.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    fake_adapter.set_battery_mode.side_effect = None

    for _ in range(4):
        await ledger_policy.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    assert len(await ledger.recent()) == 6


async def test_success_fans_out_to_metrics_and_ledger(fake_adapter, clock, ledger):
    """Write-through instrumentation: one policy call site, two sinks —
    exactly one ledger row AND one counter increment."""
    metrics = build_metrics(CollectorRegistry())
    policy = BatteryPolicy(
        fake_adapter, now_fn=clock, record_write=metrics.record_write, ledger=ledger
    )
    await policy.set_battery_mode(BatteryMode.SAVINGS, reason="peak tariff")

    rows = await ledger.recent()
    assert len(rows) == 1
    assert (rows[0].action, rows[0].outcome) == ("set_mode", "success")
    assert rows[0].target == "savings"
    assert rows[0].reason == "peak tariff"
    assert rows[0].error_class is None
    assert (
        metrics.registry.get_sample_value(
            "enphase_writes_total", {"action": "set_mode", "outcome": "success"}
        )
        == 1.0
    )


async def test_rejection_and_error_rows_carry_cause(ledger_policy, fake_adapter, ledger):
    with pytest.raises(PolicyRejected):
        await ledger_policy.set_reserve_soc(0.05, reason="cli")
    fake_adapter.set_reserve_soc.side_effect = RuntimeError("gateway down")
    with pytest.raises(RuntimeError):
        await ledger_policy.set_reserve_soc(0.30, reason="cli")

    error_row, rejected_row = await ledger.recent()
    assert rejected_row.outcome == "rejected"
    assert rejected_row.target == "0.05"
    assert "bounds" in (rejected_row.reason or "")
    assert error_row.outcome == "error"
    assert error_row.error_class == "RuntimeError"
    assert error_row.reason == "cli"


async def test_ledger_record_failure_does_not_block_write(
    ledger_policy, fake_adapter, ledger, writes, monkeypatch
):
    """Bulkhead: the audit sink failing must not fail the control action —
    the adapter write completes and the metrics counter still fires."""
    monkeypatch.setattr(ledger, "record", AsyncMock(side_effect=RuntimeError("disk full")))
    await ledger_policy.set_battery_mode(BatteryMode.SAVINGS, reason="test")

    fake_adapter.set_battery_mode.assert_awaited_once()
    assert writes == [("set_mode", "success")]


async def test_ledger_read_failure_fails_closed(ledger_policy, fake_adapter, ledger, writes):
    """The bulkhead *check* is a control decision, not audit: if the budget
    can't be verified, the write is refused rather than assumed free."""
    await ledger.close()
    with pytest.raises(LedgerError):
        await ledger_policy.set_battery_mode(BatteryMode.SAVINGS, reason="test")
    fake_adapter.set_battery_mode.assert_not_awaited()
    assert writes == [("set_mode", "error")]
