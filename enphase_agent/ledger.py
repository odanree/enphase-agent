"""Durable audit ledger — the write path's shared state across process boundaries.

Why SQLite and not the Prometheus counter: `enphase_writes_total` lives in
whichever process bumps it, and battery writes happen in one-shot CLI
processes (`docker compose run --rm enphase-agent set-mode ...`) while
Prometheus scrapes the long-running daemon. Two processes, two counters,
and the one being scraped never sees a write. The ledger is the single
durable state both sides share: the CLI appends rows, the daemon reads
them back and re-exports the last 24h as a gauge — the audit trail as a
materialized view.

It also gives `BatteryPolicy`'s daily mode-change bulkhead a memory that
survives restarts (bulkhead persistence): the count is a query over
today's rows, not a Python list that resets on every CLI invocation.

Concurrency model — WAL for concurrent-reader-single-writer semantics.
`PRAGMA journal_mode=WAL` lets any number of readers proceed while one
writer commits: readers see the last committed snapshot, the writer
appends to the write-ahead log and never blocks them. The process
boundary is: the daemon READS (24h counts for its gauge, nothing else)
and each CLI invocation WRITES one row per write attempt. Two CLI
invocations racing would serialize on SQLite's single write lock for the
few milliseconds a commit takes; the connection `timeout` turns that into
a short wait instead of a "database is locked" error. This is the classic
shared-state contention point, and it is tolerable precisely because
writes are rare — the bulkhead caps them at a handful per day.

Both processes open the same file through the compose `enphase_data`
named volume. WAL depends on a shared-memory index (`-shm`) and fcntl
locks that Docker Desktop's virtiofs / WSL2's 9P do not forward across a
host bind mount — the named volume is what makes this safe, not an
accident of config.

Schema is one table under CREATE IF NOT EXISTS; there is deliberately no
migration framework. The day this table needs a second shape is the day
one earns its place.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .errors import LedgerError

logger = logging.getLogger(__name__)

# How long a writer waits on SQLite's write lock before giving up. Commits
# take milliseconds; anything approaching this means something is wrong.
BUSY_TIMEOUT_S = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS writes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    outcome      TEXT    NOT NULL,
    target       TEXT,
    reason       TEXT,
    error_class  TEXT
);
CREATE INDEX IF NOT EXISTS idx_writes_ts     ON writes(ts);
CREATE INDEX IF NOT EXISTS idx_writes_action ON writes(action, outcome, ts);
"""

_COLUMNS = "id, ts, action, outcome, target, reason, error_class"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso_utc(dt: datetime) -> str:
    """Fixed-width UTC ISO 8601 with millisecond precision and a literal Z.

    Fixed width matters: `ts` is TEXT and every range query is a string
    comparison, which is only correct when the format sorts lexically the
    same way it sorts chronologically. A naive datetime is rejected rather
    than guessed at — silently treating it as local time is how a
    bulkhead window drifts by a timezone offset.
    """
    if dt.tzinfo is None:
        raise ValueError("ledger timestamps must be timezone-aware")
    utc = dt.astimezone(timezone.utc)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond // 1000:03d}Z"


@dataclass(frozen=True, slots=True)
class WriteRow:
    id: int
    ts: str
    action: str
    outcome: str
    target: str | None
    reason: str | None
    error_class: str | None


class Ledger:
    """One connection per process over the shared audit DB.

    aiosqlite runs the underlying sqlite3 connection on its own thread and
    hands back awaitables, so ledger I/O never stalls the event loop that
    also owns the gateway's aiohttp session.
    """

    def __init__(self, db_path: str | Path, *, now_fn: Callable[[], datetime] = _utcnow) -> None:
        self._path = Path(db_path)
        self._now = now_fn
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Idempotent init: a second call on an open ledger is a no-op, and
        a second process opening the same file finds the schema already
        there (CREATE IF NOT EXISTS). Any failure here is raised, not
        logged — an audit sink that can't open is a boot-time trust-boundary
        failure, and the caller decides whether that is fatal."""
        if self._conn is not None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(self._path), timeout=BUSY_TIMEOUT_S)
        except (OSError, sqlite3.Error) as exc:
            raise LedgerError(f"cannot open ledger at {self._path}: {exc}") from exc
        try:
            conn.row_factory = aiosqlite.Row
            # WAL is persistent in the file; NORMAL and foreign_keys are
            # per-connection, so all three run on every open. synchronous=
            # NORMAL is durable against process crashes under WAL (only an
            # OS crash can lose the last commit) and skips an fsync per
            # commit compared with FULL.
            async with conn.execute("PRAGMA journal_mode = WAL") as cur:
                row = await cur.fetchone()
            mode = str(row[0]).lower() if row is not None else "unknown"
            if mode != "wal":
                raise LedgerError(
                    f"ledger at {self._path} refused WAL (journal_mode={mode}); "
                    "is the volume a host bind mount?"
                )
            await conn.execute("PRAGMA synchronous = NORMAL")
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.executescript(_SCHEMA)
            await conn.commit()
        except sqlite3.Error as exc:
            await conn.close()
            raise LedgerError(f"cannot initialize ledger at {self._path}: {exc}") from exc
        except LedgerError:
            await conn.close()
            raise
        self._conn = conn

    async def close(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        await conn.close()

    async def record(
        self,
        *,
        action: str,
        outcome: str,
        target: str | None = None,
        reason: str | None = None,
        error_class: str | None = None,
    ) -> int:
        """Append one row and commit; returns the new row id."""
        conn = self._require_open()
        async with conn.execute(
            "INSERT INTO writes (ts, action, outcome, target, reason, error_class) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (to_iso_utc(self._now()), action, outcome, target, reason, error_class),
        ) as cur:
            row_id = cur.lastrowid
        await conn.commit()
        if row_id is None:
            raise LedgerError("INSERT returned no row id")
        return int(row_id)

    async def count_since(self, *, action: str, outcome: str, since: datetime) -> int:
        """Rows matching (action, outcome) at or after `since`. This is the
        bulkhead's read: it hits `idx_writes_action` as a covering index."""
        conn = self._require_open()
        async with conn.execute(
            "SELECT COUNT(*) FROM writes WHERE action = ? AND outcome = ? AND ts >= ?",
            (action, outcome, to_iso_utc(since)),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 0

    async def recent(self, limit: int = 20) -> list[WriteRow]:
        """Newest first. Ordered by ts (what a human means by "recent"), with
        id as the tiebreaker so equal timestamps stay in insertion order."""
        conn = self._require_open()
        async with conn.execute(
            f"SELECT {_COLUMNS} FROM writes ORDER BY ts DESC, id DESC LIMIT ?",
            (int(limit),),
        ) as cur:
            rows = await cur.fetchall()
        return [_to_row(r) for r in rows]

    async def counts_by_label_last_24h(self) -> dict[tuple[str, str], int]:
        """{(action, outcome): count} over a trailing 24h window — the
        aggregate the daemon re-exports as `enphase_writes_last_24h`."""
        conn = self._require_open()
        since = to_iso_utc(self._now() - timedelta(hours=24))
        async with conn.execute(
            "SELECT action, outcome, COUNT(*) FROM writes WHERE ts >= ? GROUP BY action, outcome",
            (since,),
        ) as cur:
            rows = await cur.fetchall()
        return {(str(r[0]), str(r[1])): int(r[2]) for r in rows}

    async def __aenter__(self) -> Ledger:
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _require_open(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise LedgerError("ledger is not open; use `async with Ledger(...)` or await open()")
        return self._conn


def _to_row(r: aiosqlite.Row) -> WriteRow:
    return WriteRow(
        id=int(r["id"]),
        ts=str(r["ts"]),
        action=str(r["action"]),
        outcome=str(r["outcome"]),
        target=r["target"],
        reason=r["reason"],
        error_class=r["error_class"],
    )
