"""Audit ledger against real SQLite — schema init, WAL, isolation, queries.

Every test opens an ephemeral on-disk DB (conftest `db_path`), so these
exercise actual SQLite locking and journaling rather than a mock; ":memory:"
would silently skip WAL and prove nothing about the process boundary.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from enphase_agent.errors import LedgerError
from enphase_agent.ledger import Ledger, to_iso_utc


async def _journal_mode(path: Path) -> str:
    # A fresh raw connection: WAL is persisted in the file header, so a
    # second opener reporting "wal" proves the mode survived, not just that
    # our own connection asked for it.
    async with aiosqlite.connect(path) as raw, raw.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row is not None
    return str(row[0]).lower()


# Blocking stat() calls inside async tests are fine — nothing else is on
# the loop — hence the targeted ASYNC240 suppressions.
async def test_open_creates_schema(db_path: Path) -> None:
    assert not os.path.exists(db_path)  # noqa: ASYNC240
    async with Ledger(db_path) as ledger:
        assert await ledger.recent() == []
    assert os.path.exists(db_path)  # noqa: ASYNC240
    assert await _journal_mode(db_path) == "wal"


async def test_open_creates_missing_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "data" / "sub" / "enphase.db"
    async with Ledger(nested):
        pass
    assert os.path.exists(nested)  # noqa: ASYNC240


async def test_to_iso_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_iso_utc(datetime(2026, 8, 29, 12, 0))


async def test_record_persists_and_recent_returns_newest_first(ledger: Ledger, clock) -> None:
    first = await ledger.record(action="set_mode", outcome="success", target="savings")
    clock.advance(minutes=1)
    second = await ledger.record(
        action="set_reserve", outcome="rejected", target="0.05", reason="cli (bounds)"
    )
    clock.advance(minutes=1)
    third = await ledger.record(
        action="set_mode", outcome="error", target="savings", error_class="RuntimeError"
    )
    assert first < second < third

    rows = await ledger.recent()
    assert [r.id for r in rows] == [third, second, first]
    assert rows[0].error_class == "RuntimeError"
    assert rows[1].reason == "cli (bounds)"
    assert rows[2].target == "savings"
    assert rows[2].ts == to_iso_utc(clock.now - timedelta(minutes=2))

    assert [r.id for r in await ledger.recent(limit=2)] == [third, second]


async def test_rows_survive_reopen(db_path: Path, clock) -> None:
    async with Ledger(db_path, now_fn=clock) as ledger:
        await ledger.record(action="set_mode", outcome="success")
    async with Ledger(db_path, now_fn=clock) as ledger:
        assert len(await ledger.recent()) == 1


async def test_count_since_matches_action_outcome_only(ledger: Ledger, clock) -> None:
    cutoff = clock.now
    clock.now = cutoff - timedelta(hours=1)
    await ledger.record(action="set_mode", outcome="success")  # too old
    clock.now = cutoff
    await ledger.record(action="set_mode", outcome="success")  # exactly at cutoff: counts
    clock.advance(minutes=5)
    await ledger.record(action="set_mode", outcome="success")
    await ledger.record(action="set_mode", outcome="rejected")  # wrong outcome
    await ledger.record(action="set_reserve", outcome="success")  # wrong action

    assert await ledger.count_since(action="set_mode", outcome="success", since=cutoff) == 2
    assert await ledger.count_since(action="set_mode", outcome="rejected", since=cutoff) == 1
    assert await ledger.count_since(action="storm_guard", outcome="success", since=cutoff) == 0


async def test_counts_by_label_last_24h_groups_correctly(ledger: Ledger, clock) -> None:
    now = clock.now
    clock.now = now - timedelta(hours=25)
    await ledger.record(action="set_mode", outcome="success")  # outside the window
    clock.now = now - timedelta(hours=23)
    await ledger.record(action="set_mode", outcome="success")
    await ledger.record(action="set_mode", outcome="success")
    await ledger.record(action="set_reserve", outcome="rejected")
    clock.now = now

    assert await ledger.counts_by_label_last_24h() == {
        ("set_mode", "success"): 2,
        ("set_reserve", "rejected"): 1,
    }


async def test_concurrent_reader_during_writer(db_path: Path, clock) -> None:
    """WAL snapshot isolation across connections: a reader opened before a
    writer's transaction sees only committed rows, is not blocked while the
    writer holds the write lock, and sees the new row after commit."""
    async with Ledger(db_path, now_fn=clock) as reader, Ledger(db_path, now_fn=clock) as writer:
        await writer.record(action="set_mode", outcome="success", target="savings")

        async with aiosqlite.connect(db_path, timeout=1.0) as raw:
            await raw.execute("BEGIN IMMEDIATE")
            await raw.execute(
                "INSERT INTO writes (ts, action, outcome) VALUES (?, ?, ?)",
                (to_iso_utc(clock.now), "set_mode", "success"),
            )
            # Uncommitted: invisible to the reader, and the read returns
            # without waiting on the writer's lock.
            assert len(await reader.recent()) == 1
            assert (
                await reader.count_since(action="set_mode", outcome="success", since=clock.now) == 1
            )
            await raw.commit()

        assert len(await reader.recent()) == 2
        assert len(await writer.recent()) == 2


async def test_double_open_is_safe(db_path: Path, clock) -> None:
    """Idempotent init: open() twice on one instance, and a second instance
    over the same file, neither re-creates nor clobbers the table."""
    ledger = Ledger(db_path, now_fn=clock)
    await ledger.open()
    await ledger.record(action="set_mode", outcome="success")
    await ledger.open()
    assert len(await ledger.recent()) == 1

    other = Ledger(db_path, now_fn=clock)
    await other.open()
    assert len(await other.recent()) == 1

    await ledger.close()
    await ledger.close()
    await other.close()


async def test_use_before_open_is_a_ledger_error(db_path: Path) -> None:
    with pytest.raises(LedgerError, match="not open"):
        await Ledger(db_path).recent()


async def test_open_failure_is_a_ledger_error(tmp_path: Path) -> None:
    # A directory where the DB file should be: sqlite can't open it.
    (tmp_path / "enphase.db").mkdir()
    with pytest.raises(LedgerError, match="cannot open ledger"):
        await Ledger(tmp_path / "enphase.db").open()
