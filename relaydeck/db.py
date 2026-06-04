"""
Session & persistence: SQLite + WAL + FTS5.

All runtime state lives here. The source of truth is always YAML on disk;
SQLite mirrors status, events, and session data for fast querying.
Migrations are strictly additive.

Design rules (carried from xagent):
  - Migrations run on every open_db. New columns/tables only.
  - Indexes on new columns live in _migrate(), not SCHEMA.
  - Events table is write-once, read-heavy (SSE filters on agent_id).
  - Agents table mirrors status (stopped|running|errored), not definition.

## Connection pooling + SQLITE_BUSY retry

`open_db` returns a *pooled* connection. The first call for a given
path creates a fresh sqlite3 connection, runs migrations, and stores
it in a path-keyed pool. Subsequent calls hand out a pooled
connection if one is free; `.close()` returns the connection to the
pool instead of destroying it. This eliminates the per-call open
cost that showed up under high message-bus + harness-drain
concurrency.

Pooled connections also wrap `execute` / `executemany` / `commit`
with a bounded-backoff SQLITE_BUSY retry — concurrent writers used
to surface raw `OperationalError` to callers whenever WAL
checkpointing or another writer held the lock for more than a
millisecond. The pool's `_RETRY_DELAYS` schedule (5/10/20/40/80/160
ms, ~315 ms total) is generous enough to cover routine contention
and tight enough that genuinely stuck writers fail fast.

Connections are created with `check_same_thread=False` so any thread
can pop one from the pool; the pool lock makes sure each connection
is in use by exactly one caller at a time. SQLite serializes its own
writes internally — the pool doesn't need a separate write lock.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,
    label       TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    ts          REAL NOT NULL,
    role        TEXT NOT NULL,           -- system|user|assistant|tool
    content     TEXT,
    tool_call_id TEXT,
    raw         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session_idx ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    ts          REAL NOT NULL,
    name        TEXT NOT NULL,
    arguments   TEXT NOT NULL,
    result      TEXT,
    duration_ms INTEGER,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS tool_calls_session_idx ON tool_calls(session_id, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    ts          REAL NOT NULL,
    type        TEXT NOT NULL,
    payload     TEXT,
    agent_id    TEXT
);
CREATE INDEX IF NOT EXISTS events_session_idx ON events(session_id, id);

CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,       -- pending|running|stopped|errored
    config_yaml     TEXT,
    workspace       TEXT,
    auto_start      INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    last_active_at  REAL,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS agents_status_idx ON agents(status);

CREATE TABLE IF NOT EXISTS usage_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    ts              REAL NOT NULL,
    model           TEXT NOT NULL,
    provider        TEXT NOT NULL,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL,
    request_count   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS usage_agent_idx ON usage_records(agent_id, ts);
CREATE INDEX IF NOT EXISTS usage_model_idx ON usage_records(model, provider, ts);

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
"""


# ── Connection pool ──────────────────────────────────────────────────


# Per-path connection pool. The daemon and the CLI share state through
# `~/.relaydeck/runtime/relaydeck.db`; the test suite touches dozens of
# tmp DBs. A path-keyed pool means each DB gets its own bounded
# connection set without leaking connections to a deleted tmp DB
# across tests (tests can call `_close_all_pools()` if they want).
_MAX_POOL_SIZE = 8

# SQLITE_BUSY retry backoff in seconds. Schedule mirrors what we'd
# want for "transient lock contention" — short enough that interactive
# CLI commands stay snappy, long enough that a busy writer's checkpoint
# completes. After the last delay we give up and re-raise the
# OperationalError.
_RETRY_DELAYS = (0.005, 0.010, 0.020, 0.040, 0.080, 0.160)


class _Pool:
    __slots__ = ("path", "free", "lock", "migrated")

    def __init__(self, path: str) -> None:
        self.path = path
        self.free: list[sqlite3.Connection] = []
        self.lock = threading.Lock()
        self.migrated = False


_POOLS: dict[str, _Pool] = {}
_POOLS_LOCK = threading.Lock()


def _get_pool(path: str) -> _Pool:
    with _POOLS_LOCK:
        pool = _POOLS.get(path)
        if pool is None:
            pool = _Pool(path)
            _POOLS[path] = pool
        return pool


def _create_connection(path: str) -> sqlite3.Connection:
    """Open a fresh connection with our pragmas + row factory.

    `check_same_thread=False` lets the pool hand a connection to any
    thread; the pool's own lock ensures each connection is used by
    exactly one caller at a time.
    """
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # synchronous=NORMAL is safe with WAL and noticeably faster than
    # FULL for transactional inserts; the trade is "lose the last few
    # ms of writes on power loss" which is acceptable for our use
    # case (everything we persist is recoverable from yaml or the user
    # can re-emit it).
    conn.execute("PRAGMA synchronous=NORMAL")
    # busy_timeout is the *kernel-level* fallback if our retry loop
    # doesn't catch a SQLITE_BUSY. Defense in depth.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _retry_on_busy(fn: Callable[[], Any]) -> Any:
    """Run `fn`; if it raises sqlite3.OperationalError with 'locked'
    or 'busy' in the message, sleep + retry per `_RETRY_DELAYS`.

    Other OperationalErrors (syntax, schema mismatch) re-raise
    immediately — only contention is retried.
    """
    last_exc: sqlite3.OperationalError | None = None
    for delay in _RETRY_DELAYS:
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_exc = exc
            time.sleep(delay)
    # Final attempt — let it raise.
    try:
        return fn()
    except sqlite3.OperationalError as exc:
        if last_exc is not None:
            logger.warning("sqlite retry exhausted after %d attempts: %s",
                           len(_RETRY_DELAYS) + 1, exc)
        raise


class _PooledConnection:
    """Drop-in proxy over sqlite3.Connection.

    Wraps execute / executemany / commit with the SQLITE_BUSY retry.
    On .close(), returns the underlying connection to the pool
    instead of actually closing it. Supports the same `with conn:`
    transaction-context shape sqlite3.Connection does — entering
    yields the proxy itself, exiting calls close().
    """

    def __init__(self, conn: sqlite3.Connection, pool: _Pool) -> None:
        self._conn = conn
        self._pool = pool
        self._closed = False

    # Most attributes pass through. Cursor objects (returned by
    # `execute`) hold a reference to the underlying connection, so
    # fetchone/fetchall still work after the call boundary.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return _retry_on_busy(lambda: self._conn.execute(sql, params))

    def executemany(self, sql: str, seq_of_params: Any) -> sqlite3.Cursor:
        return _retry_on_busy(lambda: self._conn.executemany(sql, seq_of_params))

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        return _retry_on_busy(lambda: self._conn.executescript(sql_script))

    def commit(self) -> None:
        _retry_on_busy(self._conn.commit)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # If the connection is in an aborted transaction state,
        # rollback before pooling so the next user starts clean.
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
        except sqlite3.Error:
            # Connection is wedged; don't pool it.
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            return
        with self._pool.lock:
            if len(self._pool.free) < _MAX_POOL_SIZE:
                self._pool.free.append(self._conn)
                return
        # Pool full — close for real.
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> _PooledConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Match sqlite3.Connection.__exit__ behavior: commit on
        # success, rollback on error, then return to the pool.
        if exc is None:
            try:
                self.commit()
            except sqlite3.Error:
                pass
        else:
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
        self.close()


def open_db(path: str | Path) -> _PooledConnection:
    """Open (or create) the session database, returning a pooled
    connection.

    On the first call for a given path, schema + migrations run once.
    Later calls re-use a pooled connection. `.close()` returns the
    connection to the pool — DO continue to call it; the pool needs
    to know the connection is available again.

    Type-compatible with sqlite3.Connection for all the call sites
    that just `execute` / `commit` / `close`.
    """
    path_str = str(Path(path))
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)

    pool = _get_pool(path_str)
    with pool.lock:
        conn: sqlite3.Connection | None = None
        if pool.free:
            conn = pool.free.pop()
        if conn is None:
            conn = _create_connection(path_str)
            if not pool.migrated:
                conn.executescript(SCHEMA)
                _migrate(conn)
                pool.migrated = True
    return _PooledConnection(conn, pool)


def _close_all_pools() -> None:
    """Close every pooled connection and forget every pool.

    Test fixtures call this between sessions so leftover connections
    don't pin deleted tmp DB files. Production code shouldn't need to
    call it — daemon shutdown releases everything implicitly via
    process exit.
    """
    with _POOLS_LOCK:
        for pool in _POOLS.values():
            with pool.lock:
                for conn in pool.free:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                pool.free.clear()
        _POOLS.clear()


def _pool_stats() -> dict[str, dict[str, int]]:
    """Snapshot of every active pool — used by `relaydeck db status` and
    the observability metrics endpoint. Returns
    `{path: {"free": int, "max": int}}`.
    """
    out: dict[str, dict[str, int]] = {}
    with _POOLS_LOCK:
        for path, pool in _POOLS.items():
            with pool.lock:
                out[path] = {"free": len(pool.free), "max": _MAX_POOL_SIZE}
    return out


# Public type alias for helpers that take a connection. `open_db`
# returns a `_PooledConnection`; the helpers used to be typed
# `sqlite3.Connection` and the proxy works at runtime, but type
# checkers correctly flag the mismatch. Widening the parameter type
# documents that callers may pass either shape — the proxy's
# __getattr__ forwards every method we actually call.
DBConn = "sqlite3.Connection | _PooledConnection"


# ── Idle maintenance (events prune + WAL checkpoint) ────────────────


# Default: drop event rows older than 30 days. Tunable through the
# `events_retention_days` plugin setting once observability lands;
# until then, callers can pass `retention_days=` directly.
DEFAULT_EVENT_RETENTION_DAYS = 30


def prune_old_events(
    path: str | Path,
    *,
    retention_days: float = DEFAULT_EVENT_RETENTION_DAYS,
) -> int:
    """Delete event rows older than `retention_days` and return the
    count deleted.

    The `events` table is the largest growing table by far in a
    long-running daemon (every PTY byte, every agent lifecycle tick,
    every plugin event). Without bounded growth a multi-month-old
    workspace hits gigabyte-scale relaydeck.db files.

    Returns 0 immediately when retention is non-positive (treated as
    "disabled — keep forever") so operators who want full history can
    opt out cleanly.
    """
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400.0
    conn = open_db(path)
    try:
        cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


# Default: drop terminal (delivered / failed) agent messages after 14
# days. Messages are tiny compared to events, but a long-running fleet
# that chats constantly still accumulates them forever otherwise. Only
# terminal rows are eligible — a `queued` message is undelivered work and
# is never pruned regardless of age.
DEFAULT_MESSAGE_RETENTION_DAYS = 14


def prune_agent_messages(
    path: str | Path,
    *,
    retention_days: float = DEFAULT_MESSAGE_RETENTION_DAYS,
) -> int:
    """Delete terminal agent_messages older than `retention_days` and
    return the count deleted.

    Terminal = `delivery_state` in (`delivered`, `failed`). A `queued`
    message represents undelivered work — a peer waiting on a reply, an
    inbox item that will drain when its agent next starts — so it is
    NEVER pruned by age. This keeps the inbox from growing without bound
    while guaranteeing we never silently drop a message the recipient
    hasn't seen.

    Returns 0 immediately when retention is non-positive ("keep forever").
    """
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400.0
    conn = open_db(path)
    try:
        cur = conn.execute(
            "DELETE FROM agent_messages "
            "WHERE ts < ? AND delivery_state IN ('delivered', 'failed')",
            (cutoff,),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def wal_checkpoint(path: str | Path, *, mode: str = "TRUNCATE") -> dict[str, int]:
    """Run a WAL checkpoint and return `{busy, log, checkpointed}`.

    `mode="TRUNCATE"` is the most aggressive — drops the WAL file to
    zero bytes after checkpointing. Safe to call on an active DB;
    SQLite holds writers off only long enough to flush. We use it as
    an idle-time GC pass to keep the on-disk WAL small.

    Returns the values from `PRAGMA wal_checkpoint`:
      busy           — non-zero if not all frames could be checkpointed
      log            — total frames in the WAL before checkpoint
      checkpointed   — frames moved to the main DB

    Path: caller is responsible for not running this on every write —
    the worker that calls it should debounce to "once every N seconds
    after the last write."
    """
    valid = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
    if mode not in valid:
        raise ValueError(f"checkpoint mode must be one of {valid}")
    conn = open_db(path)
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        # SQLite returns (busy, log, checkpointed); sqlite3.Row indexes
        # by position too.
        return {
            "busy": int(row[0]),
            "log": int(row[1]),
            "checkpointed": int(row[2]),
        }
    finally:
        conn.close()


def vacuum_db(path: str | Path) -> dict[str, int]:
    """Rebuild the database, returning freelist pages to the filesystem.

    A WAL checkpoint only resets the `-wal` file; the main `.db` keeps
    its high-water mark because deleted/pruned pages go on SQLite's
    freelist for reuse, not back to the OS. VACUUM rewrites the whole
    file so that space is actually reclaimed.

    Returns `{before_bytes, after_bytes, reclaimed_bytes, free_bytes_before}`.

    EXPENSIVE: VACUUM takes an exclusive write lock and rewrites the
    entire file — on a multi-GB DB it can stall every writer for
    seconds. Callers MUST gate it (cadence + a free-page threshold);
    never run it per write or per maintenance tick. We use a dedicated
    autocommit connection (VACUUM cannot run inside a transaction) so
    we never rewrite the file from under a pooled connection.
    """
    p = Path(path)
    before = p.stat().st_size if p.exists() else 0
    conn = sqlite3.connect(str(path), timeout=60.0, isolation_level=None)
    try:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        # Preserve WAL mode across the rebuild (VACUUM can otherwise reset
        # journal_mode on some SQLite builds).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("VACUUM")
        # In WAL mode the rebuilt (smaller) pages land in the -wal file; the
        # main .db only shrinks once they're checkpointed back. TRUNCATE so
        # the reclaimed space shows up as a smaller file — otherwise the whole
        # point of VACUUM (returning bytes to the OS) wouldn't be visible.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    after = p.stat().st_size if p.exists() else 0
    return {
        "before_bytes": before,
        "after_bytes": after,
        "reclaimed_bytes": max(0, before - after),
        "free_bytes_before": page_size * freelist,
    }


# ── Plugin-bus durability helpers ───────────────────────────────────

# Default: keep persisted bus events for 7 days. Bus events are much
# noisier than session events (every assistant_message, every tile
# tick) so the retention bar is lower. Tunable through
# `bus_retention_days` once observability adds the setting; for now
# callers pass `retention_days=` directly.
DEFAULT_BUS_RETENTION_DAYS = 7


def append_bus_event(
    conn: "sqlite3.Connection | _PooledConnection",
    *,
    ts: float,
    type: str,
    data: str | None,
    source_plugin: str,
) -> int:
    """Persist one event to `bus_events` and return its rowid.

    The bus calls this *before* dispatching to subscribers so a crash
    between persist and dispatch is recoverable on next startup. The
    serialized `data` blob is whatever the bus chose (JSON today) —
    this helper is content-agnostic.
    """
    cur = conn.execute(
        "INSERT INTO bus_events (ts, type, data, source_plugin) VALUES (?, ?, ?, ?)",
        (ts, type, data, source_plugin),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def load_bus_cursor(
    conn: "sqlite3.Connection | _PooledConnection",
    subscriber_key: str,
) -> int:
    """Return the last acked event id for a durable subscriber, or 0
    when the subscriber has never run before. 0 is the floor because
    SQLite's AUTOINCREMENT starts at 1, so `id > 0` means "everything"."""
    row = conn.execute(
        "SELECT last_acked_id FROM bus_cursors WHERE subscriber_key = ?",
        (subscriber_key,),
    ).fetchone()
    return int(row[0]) if row else 0


def save_bus_cursor(
    conn: "sqlite3.Connection | _PooledConnection",
    *,
    subscriber_key: str,
    pattern: str,
    last_acked_id: int,
) -> None:
    """Upsert a durable subscriber's cursor. Stamps `updated_at` so
    operators can spot stalled subscribers in `relaydeck db status`."""
    conn.execute(
        """INSERT INTO bus_cursors (subscriber_key, pattern, last_acked_id, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(subscriber_key) DO UPDATE SET
               pattern=excluded.pattern,
               last_acked_id=excluded.last_acked_id,
               updated_at=excluded.updated_at""",
        (subscriber_key, pattern, last_acked_id, time.time()),
    )
    conn.commit()


def read_bus_events_after(
    conn: "sqlite3.Connection | _PooledConnection",
    *,
    after_id: int,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return up to `limit` events with id > after_id, in id order.

    Callers replay these to a durable subscriber and advance the
    cursor as each event's handler returns. `limit` exists so a
    subscriber that's been offline for a month doesn't materialize a
    million-row list in memory — callers loop until the page is short.
    """
    rows = conn.execute(
        "SELECT id, ts, type, data, source_plugin FROM bus_events "
        "WHERE id > ? ORDER BY id ASC LIMIT ?",
        (after_id, limit),
    ).fetchall()
    return [
        {
            "id": int(r[0]),
            "ts": float(r[1]),
            "type": r[2],
            "data": r[3],
            "source_plugin": r[4] or "",
        }
        for r in rows
    ]


def prune_bus_events(
    path: str | Path,
    *,
    retention_days: float = DEFAULT_BUS_RETENTION_DAYS,
) -> int:
    """Drop persisted bus events that are both (a) older than
    `retention_days` AND (b) already acked by every durable subscriber.

    Condition (b) is load-bearing: if a subscriber has been offline
    for 8 days and retention is 7, we still owe it those events.
    `min(last_acked_id)` across all known cursors is the safe floor —
    delete only `id <= floor AND ts < cutoff`. When there are no
    cursors (no durable subscribers yet), age alone is enough.

    Returns the row count deleted; 0 when retention is non-positive
    (treated as "keep forever").
    """
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400.0
    conn = open_db(path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MIN(last_acked_id), 0) FROM bus_cursors"
        ).fetchone()
        floor = int(row[0]) if row else 0
        # When there are no cursors at all, MIN(...) is NULL and the
        # COALESCE returns 0 — in that case "ts < cutoff" is the only
        # constraint, which is what we want for the no-durable-subs case.
        has_cursors = conn.execute(
            "SELECT 1 FROM bus_cursors LIMIT 1"
        ).fetchone() is not None
        if has_cursors:
            cur = conn.execute(
                "DELETE FROM bus_events WHERE ts < ? AND id <= ?",
                (cutoff, floor),
            )
        else:
            cur = conn.execute(
                "DELETE FROM bus_events WHERE ts < ?", (cutoff,),
            )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def db_status(path: str | Path) -> dict[str, Any]:
    """Operator snapshot — pool stats, WAL byte size, table row counts.

    Designed to be cheap (single connection, three SELECT COUNTs) so
    the dashboard / `relaydeck db status` can poll it without performance
    concerns. Run on a long-running daemon to spot growth before it
    becomes painful.
    """
    p = Path(path)
    out: dict[str, Any] = {
        "path": str(p),
        "pools": _pool_stats(),
    }
    if p.exists():
        out["bytes"] = p.stat().st_size
    else:
        out["bytes"] = 0
    wal = p.with_suffix(p.suffix + "-wal")
    out["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
    if not p.exists():
        return out
    conn = open_db(path)
    try:
        # Bounded set of tables to count — avoid touching FTS shadow tables
        # which inflate the picture without telling operators anything useful.
        counts: dict[str, int] = {}
        for name in ("agents", "events", "messages", "agent_messages",
                     "usage_records", "sessions", "tool_calls",
                     "bus_events", "bus_cursors", "tasks",
                     "skills_cache", "skill_links", "automation_runs",
                     "model_invocations"):
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()
                counts[name] = int(row[0])
            except sqlite3.OperationalError:
                # Table doesn't exist yet (fresh DB before SCHEMA?) — skip.
                pass
        out["rows"] = counts
        # Reclaimable space: pages freed by deletes/prunes sit on SQLite's
        # freelist and are reused by later inserts, but the file never
        # shrinks until a VACUUM returns them to the OS. Surfacing this
        # explains why `bytes` doesn't drop after a prune and gives the
        # maintenance worker / operator a threshold to gate VACUUM on.
        try:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            out["free_bytes"] = page_size * freelist
        except (sqlite3.OperationalError, TypeError):
            out["free_bytes"] = 0
    finally:
        conn.close()
    return out


# Bump this whenever a migration block is added to `_migrate` below.
# It's stamped into `PRAGMA user_version` after a successful migration so
# subsequent opens of an already-current DB skip the ~40 idempotent DDL
# statements entirely (a real cold-start win), and so future non-additive
# migrations have a real version anchor to gate on. The migrations
# themselves stay idempotent — `user_version` is an optimization + anchor,
# not the correctness mechanism.
_SCHEMA_VERSION = 19


def _user_version(conn: "sqlite3.Connection | _PooledConnection") -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _is_duplicate_column(exc: sqlite3.OperationalError) -> bool:
    """True only for the benign 'this column already exists' error an
    idempotent `ALTER TABLE ADD COLUMN` raises on an already-migrated DB.
    Any other OperationalError (genuinely malformed DDL, missing table)
    must surface, not be swallowed — masking it would leave a
    half-migrated DB with no signal."""
    return "duplicate column name" in str(exc).lower()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations. Only add columns/indexes that don't exist.

    Each migration block checks for the presence of a column/index
    before adding it, so it's safe to run on existing DBs. After all
    blocks succeed, `PRAGMA user_version` is stamped to `_SCHEMA_VERSION`;
    a DB already at that version short-circuits on the next open. The
    stamp is written LAST, so a migration that fails partway leaves the
    version unbumped and the (idempotent) blocks simply re-run on the
    next open until they complete — replay, not a wedged half-state.
    """
    if _user_version(conn) >= _SCHEMA_VERSION:
        return

    # Migration 1: events_agent_idx (if not created by SCHEMA on existing DBs)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS events_agent_idx ON events(agent_id, id)")
    except sqlite3.OperationalError:
        pass

    # Migration 2: usage_records indexes (redundant with SCHEMA for new DBs,
    # needed for pre-existing DBs that lacked the table)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS usage_agent_idx ON usage_records(agent_id, ts)")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS usage_model_idx ON usage_records(model, provider, ts)")
    except sqlite3.OperationalError:
        pass

    # Migration 4: agents.purpose + agents.tags — cross-orchestration
    # meta. The one-line "what I'm for" and tag list each agent sees
    # in `relaydeck agent list` and similar discovery surfaces.
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN purpose TEXT")
    except sqlite3.OperationalError as exc:
        if not _is_duplicate_column(exc):
            raise
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN tags TEXT")  # JSON-encoded list
    except sqlite3.OperationalError as exc:
        if not _is_duplicate_column(exc):
            raise

    # Migration 3: agent_messages table — inbox primitive for
    # agent-to-agent (and user → agent) messaging. Additive; safe to
    # run on existing DBs.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_messages (
            id            TEXT PRIMARY KEY,
            from_id       TEXT NOT NULL,
            to_id         TEXT NOT NULL,
            body          TEXT NOT NULL,
            rendered_body TEXT,
            in_reply_to   TEXT,
            ts            REAL NOT NULL,
            injected_at   REAL,
            workspace     TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_messages_drain "
        "ON agent_messages(to_id, injected_at, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_messages_ws_ts "
        "ON agent_messages(workspace, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_messages_from_ts "
        "ON agent_messages(from_id, ts)"
    )

    # Migration 5: agent_messages durability columns. Adds explicit
    # delivery state machine + attempt counter + last_error so the
    # platform can answer "why is this message stuck" and stop
    # retrying after a configurable cap. All additive.
    for col, decl in (
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_error", "TEXT"),
        ("delivery_state", "TEXT NOT NULL DEFAULT 'queued'"),
    ):
        try:
            conn.execute(f"ALTER TABLE agent_messages ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError as exc:
            if not _is_duplicate_column(exc):
                raise
    # Index for the "show me failed/stuck messages" admin query
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_messages_state "
            "ON agent_messages(delivery_state, ts)"
        )
    except sqlite3.OperationalError:
        pass

    # Migration 6: auth_tokens table — scoped Bearer tokens. Each row
    # is one named credential the daemon will accept on a request. The
    # plaintext token never lives in the DB; only the sha256 hash does.
    # `label` is the operator-facing name ("dashboard-laptop"),
    # `scope` is one of root / read-only / agent:<id> / plugin:<name>.
    # The existing single-token-on-disk file becomes the implicit
    # root token; this table is for additional named credentials.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS auth_tokens (
            id            TEXT PRIMARY KEY,
            label         TEXT NOT NULL,
            scope         TEXT NOT NULL,
            hashed_token  TEXT NOT NULL UNIQUE,
            created_at    REAL NOT NULL,
            last_used_at  REAL,
            expires_at    REAL,
            revoked_at    REAL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_tokens_hash ON auth_tokens(hashed_token)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_tokens_scope ON auth_tokens(scope)"
    )

    # Migration 7: audit_events table — append-only record of sensitive
    # operations for compliance + post-incident review. `token_id` is
    # a soft FK to auth_tokens.id; nullable because the implicit root
    # token (on-disk file) has no row in auth_tokens. `payload` is
    # JSON-encoded for variable-shape metadata. Retention is operator-
    # driven via `relaydeck audit prune`.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_events (
            id            TEXT PRIMARY KEY,
            ts            REAL NOT NULL,
            token_id      TEXT,
            token_label   TEXT,
            action        TEXT NOT NULL,
            target        TEXT,
            payload       TEXT,
            source_ip     TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_token ON audit_events(token_id, ts)"
    )

    # Migration 8: agents.semantic_status — observable agent state
    # beyond the process-level `status` column. Set by:
    #   - vendor-side hooks (POST /api/agents/{id}/state)
    #   - the semantic-status engine (fallback when no hook is present)
    # Values: working | awaiting-input | complete-unread | idle.
    # NULL is "unknown" — agent process is up but neither path has
    # reported a state yet. The plain `status` column stays the
    # source of truth for "is the process alive".
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN semantic_status TEXT")
    except sqlite3.OperationalError as exc:
        if not _is_duplicate_column(exc):
            raise
    try:
        conn.execute(
            "ALTER TABLE agents ADD COLUMN semantic_status_at REAL"
        )
    except sqlite3.OperationalError as exc:
        if not _is_duplicate_column(exc):
            raise
    try:
        conn.execute(
            "ALTER TABLE agents ADD COLUMN semantic_status_source TEXT"
        )
    except sqlite3.OperationalError as exc:
        if not _is_duplicate_column(exc):
            raise

    # Migration 18: agents.running_config — JSON snapshot of the spawn-time
    # config (prompt hash, enabled plugins, injected skills + content hashes,
    # model, launch flags) captured each time the agent (re)starts. Diffing it
    # against the live desired config powers the "restart to apply" state — the
    # agent process is stale until restarted. NULL = never captured (legacy /
    # never started) → treated as not-pending.
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN running_config TEXT")
    except sqlite3.OperationalError as exc:
        if not _is_duplicate_column(exc):
            raise

    # Migration 9: plugin-event bus durability. `bus_events` is an
    # append-only log of every event passed through PluginEventBus
    # (write-before-dispatch, so crashes between persist and consume
    # are recoverable). `bus_cursors` tracks per-durable-subscriber
    # last-acked id so re-loaded plugins replay only the events they
    # haven't already processed. Auto-incremented `id` doubles as the
    # cursor — monotonic + dense + cheap to index.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bus_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            REAL NOT NULL,
            type          TEXT NOT NULL,
            data          TEXT,
            source_plugin TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bus_events_ts ON bus_events(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bus_events_type ON bus_events(type, id)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bus_cursors (
            subscriber_key TEXT PRIMARY KEY,
            pattern        TEXT NOT NULL,
            last_acked_id  INTEGER NOT NULL DEFAULT 0,
            updated_at     REAL NOT NULL
        )"""
    )

    # Migration 10: tasks — plugin-owned work records (PR/issue
    # lifecycle, etc.). Deliberately NOT folded into agent YAML or the
    # agents table: agent specs are the source of truth for *agents*, not
    # for the units of work a plugin tracks. `plugin` namespaces rows by
    # owner; `external_ref` carries the upstream identity (e.g. a GitHub
    # PR url or "repo#123") for idempotent upserts; `metadata` is a
    # plugin-defined JSON blob. A core table (not a workspace-runtime
    # file) so the dashboard can query across workspaces cheaply.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id            TEXT PRIMARY KEY,
            plugin        TEXT NOT NULL,
            workspace     TEXT,
            agent_id      TEXT,
            kind          TEXT NOT NULL DEFAULT '',
            external_ref  TEXT,
            title         TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'open',
            metadata      TEXT,
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_plugin_ws "
        "ON tasks(plugin, workspace, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_external_ref "
        "ON tasks(plugin, external_ref)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent_id, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at)"
    )

    # Migration 11: skills_cache + skill_links — the skills inventory
    # CACHE and the operator-owned import links. The filesystem (SKILL.md
    # + ownership sidecars) and upstream tools (codex) remain the source
    # of truth for *skill content*; this table is a queryable mirror so
    # the dashboard Skills lens, `relaydeck skills list`, and the rescan worker
    # don't re-walk the tree on every request. `skill_links` is the one
    # piece relaydeck genuinely owns: operator-created symlink/copy/reference
    # imports of an external skill into a workspace.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS skills_cache (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL DEFAULT '',
            display_name    TEXT NOT NULL DEFAULT '',
            description     TEXT NOT NULL DEFAULT '',
            source_type     TEXT NOT NULL DEFAULT '',
            owner_plugin    TEXT,
            workspace       TEXT,
            path            TEXT NOT NULL DEFAULT '',
            realpath        TEXT NOT NULL DEFAULT '',
            symlink_target  TEXT,
            hash            TEXT NOT NULL DEFAULT '',
            valid           INTEGER NOT NULL DEFAULT 1,
            errors_json     TEXT,
            warnings_json   TEXT,
            frontmatter_json TEXT,
            size            INTEGER NOT NULL DEFAULT 0,
            mtime           REAL NOT NULL DEFAULT 0,
            body_chars      INTEGER NOT NULL DEFAULT 0,
            token_estimate  INTEGER NOT NULL DEFAULT 0,
            risk_level      TEXT NOT NULL DEFAULT 'low',
            risk_flags_json TEXT,
            last_scanned_at REAL NOT NULL DEFAULT 0,
            updated_at      REAL NOT NULL DEFAULT 0
        )"""
    )
    for col, decl in {
        "body_chars": "INTEGER NOT NULL DEFAULT 0",
        "token_estimate": "INTEGER NOT NULL DEFAULT 0",
        "risk_level": "TEXT NOT NULL DEFAULT 'low'",
        "risk_flags_json": "TEXT",
    }.items():
        try:
            conn.execute(f"ALTER TABLE skills_cache ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError as exc:
            if not _is_duplicate_column(exc):
                raise
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skills_cache_ws "
        "ON skills_cache(workspace, source_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skills_cache_owner "
        "ON skills_cache(owner_plugin)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skills_cache_valid "
        "ON skills_cache(valid, source_type)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS skill_links (
            id           TEXT PRIMARY KEY,
            workspace    TEXT NOT NULL,
            alias        TEXT NOT NULL,
            target_path  TEXT NOT NULL,
            target_id    TEXT,
            mode         TEXT NOT NULL DEFAULT 'symlink',
            enabled      INTEGER NOT NULL DEFAULT 1,
            source_url   TEXT,
            source_ref   TEXT,
            source_subpath TEXT,
            review_status TEXT NOT NULL DEFAULT 'not-reviewed',
            review_summary TEXT,
            reviewed_at  REAL,
            last_checked_at REAL,
            created_at   REAL NOT NULL DEFAULT 0
        )"""
    )
    for col, decl in {
        "source_url": "TEXT",
        "source_ref": "TEXT",
        "source_subpath": "TEXT",
        "review_status": "TEXT NOT NULL DEFAULT 'not-reviewed'",
        "review_summary": "TEXT",
        "reviewed_at": "REAL",
        "last_checked_at": "REAL",
    }.items():
        try:
            conn.execute(f"ALTER TABLE skill_links ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError as exc:
            if not _is_duplicate_column(exc):
                raise
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_links_ws_alias "
        "ON skill_links(workspace, alias)"
    )

    # Migration 17: skill_usage_events — exact skill-use telemetry when a
    # harness or plugin can prove a specific skill was invoked. This is
    # deliberately separate from the exposure rollup derived from
    # usage_records: exposure says "agents in a workspace with this skill
    # used tokens"; this table says "this skill was actually used", with
    # optional token/cost attribution when the recorder can provide it.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS skill_usage_events (
            id                TEXT PRIMARY KEY,
            ts                REAL NOT NULL,
            skill_id          TEXT,
            skill_name        TEXT NOT NULL DEFAULT '',
            workspace         TEXT,
            agent_id          TEXT,
            source            TEXT NOT NULL DEFAULT '',
            event_type        TEXT NOT NULL DEFAULT 'used',
            confidence        REAL NOT NULL DEFAULT 1.0,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens      INTEGER NOT NULL DEFAULT 0,
            cost_usd          REAL,
            metadata_json     TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_usage_skill "
        "ON skill_usage_events(skill_name, workspace, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_usage_agent "
        "ON skill_usage_events(agent_id, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_usage_ts "
        "ON skill_usage_events(ts)"
    )

    # Migration 12: automation_runs — durable audit trail of automation
    # executions (a loop-agent tick today; scheduled/triggered runs
    # later). Workers stay small + live; this table is the persisted
    # "what fired, when, what it did, did it work" record so a tick
    # survives daemon restart AS HISTORY. Deliberately distinct from
    # `tasks`: a task is plugin-owned DOMAIN work (a PR/issue), a run is
    # the EXECUTION record of a trigger firing. `automation_id` is the
    # producer's identity (a loop agent id for now). Additive; never the
    # source of truth for the automation spec — only its run log.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS automation_runs (
            id               TEXT PRIMARY KEY,
            automation_id    TEXT NOT NULL,
            automation_type  TEXT NOT NULL DEFAULT '',
            workspace        TEXT,
            trigger_type     TEXT NOT NULL DEFAULT '',
            trigger_event_id TEXT,
            status           TEXT NOT NULL DEFAULT 'running',
            started_at       REAL NOT NULL,
            finished_at      REAL,
            duration_ms      INTEGER,
            action_count     INTEGER NOT NULL DEFAULT 0,
            error_count      INTEGER NOT NULL DEFAULT 0,
            error            TEXT,
            detail           TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_automation_runs_aid "
        "ON automation_runs(automation_id, started_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_automation_runs_status "
        "ON automation_runs(status, started_at)"
    )

    # Migration 13: model_invocations — per-call LLM invocation log for
    # worker `model` actions (and any future model caller). Gives the
    # Workers lens real visibility into what a worker asked a model, when,
    # how long it took, and how many tokens it used. Distinct from
    # `usage_records` (the metering rollup fed by harness JSONL tailers):
    # this is the granular per-invocation trace with latency + char counts
    # + ok/error, keyed by the calling agent/worker id. Token counts are
    # REAL when the provider reports them (ollama does), else 0 with
    # tokens_known=0 — never fabricated.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS model_invocations (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id          TEXT NOT NULL,
            ts                REAL NOT NULL,
            model             TEXT NOT NULL DEFAULT '',
            provider          TEXT NOT NULL DEFAULT '',
            latency_ms        INTEGER NOT NULL DEFAULT 0,
            prompt_chars      INTEGER NOT NULL DEFAULT 0,
            completion_chars  INTEGER NOT NULL DEFAULT 0,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens      INTEGER NOT NULL DEFAULT 0,
            tokens_known      INTEGER NOT NULL DEFAULT 0,
            ok                INTEGER NOT NULL DEFAULT 1,
            error             TEXT,
            source            TEXT,
            prompt            TEXT,
            response          TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_inv_agent "
        "ON model_invocations(agent_id, ts)"
    )
    # prompt/response added after the table first shipped — additive
    # columns for existing DBs (the "what did it actually ask/get" view).
    for _col in ("prompt", "response"):
        try:
            conn.execute(f"ALTER TABLE model_invocations ADD COLUMN {_col} TEXT")
        except sqlite3.OperationalError as exc:
            if not _is_duplicate_column(exc):
                raise

    # Migration 14: interactive_prompts — structured human-in-the-loop
    # approvals. An agent raises a question + tap-able choices, fanned out
    # to one or more messaging channels (telegram/discord/web/…); the first
    # human to pick resolves it. `choices` and `deliveries` are JSON blobs
    # (small, bounded; no need for a child table at this scale). `state`
    # is the open→answered|expired|canceled machine; the single conditional
    # UPDATE on it is the resolution race guard. `resume_target` records how
    # the answer flows back to the asker ("relay:<agent_id>" or empty for a
    # blocking caller). Data layer: relaydeck/prompts.py. Additive.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS interactive_prompts (
            id            TEXT PRIMARY KEY,
            agent_id      TEXT NOT NULL DEFAULT '',
            body          TEXT NOT NULL DEFAULT '',
            choices       TEXT NOT NULL DEFAULT '[]',
            created_ts    REAL NOT NULL,
            workspace     TEXT,
            expires_ts    REAL,
            state         TEXT NOT NULL DEFAULT 'open',
            multi         INTEGER NOT NULL DEFAULT 0,
            resume_target TEXT NOT NULL DEFAULT '',
            answer_choice TEXT,
            answer_value  TEXT,
            answered_by   TEXT,
            answered_ts   REAL,
            deliveries    TEXT NOT NULL DEFAULT '[]'
        )"""
    )
    # "open prompts for this workspace/agent" (dashboard + sweeper) and
    # "expire overdue" both scan by state; index it with created_ts for
    # the newest-first listing.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prompts_state "
        "ON interactive_prompts(state, created_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prompts_ws "
        "ON interactive_prompts(workspace, created_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prompts_agent "
        "ON interactive_prompts(agent_id, created_ts)"
    )

    # Migration 15: agent_messages.broadcast_id — groups the fan-out rows
    # of a single fleet broadcast. A broadcast (a workspace-wide message
    # with no specific target) is an announcement, not N separate 1:1 asks,
    # so its rows must NOT each be flagged "owes a reply" by the idle-edge
    # reply-nudge — otherwise one fleet message nudges every recipient to
    # reply, and the resulting acks pollute the threads. Direct (targeted)
    # messages leave this NULL and still owe a reply. NULL = direct / legacy.
    try:
        conn.execute("ALTER TABLE agent_messages ADD COLUMN broadcast_id TEXT")
    except sqlite3.OperationalError as exc:
        if not _is_duplicate_column(exc):
            raise

    # Migration 19: agent_results — a durable place for an agent to hand back
    # a STRUCTURED result that survives its own crash. Before this, "collect
    # results" meant scraping the PTY ring (256KB, never persisted) or peer
    # inbox messages; a crashed agent's output was just gone. A result is
    # latest-wins per (agent_id, key): an agent can post progress under a key
    # and overwrite it, and `agent wait`/`agent result get` read the latest.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_results (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id  TEXT NOT NULL,
            workspace TEXT,
            key       TEXT NOT NULL DEFAULT '',
            status    TEXT NOT NULL DEFAULT 'ok',
            summary   TEXT NOT NULL DEFAULT '',
            body      TEXT NOT NULL DEFAULT '',
            ts        REAL NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_results_agent "
        "ON agent_results(agent_id, key, ts)"
    )

    # Future migrations go here. Each block:
    #   - Checks if the column/table/index exists
    #   - Adds it if missing
    #   - Never renames, drops, or alters existing columns
    #   - REMEMBER to bump `_SCHEMA_VERSION` above by one.

    # Stamp the schema version LAST — only a fully-successful run records
    # it, so a partial failure replays the idempotent blocks next open.
    # `_SCHEMA_VERSION` is an int constant we control (no injection), and
    # PRAGMA doesn't accept bound parameters, so the literal is required.
    conn.execute(f"PRAGMA user_version = {int(_SCHEMA_VERSION)}")
    conn.commit()


# ── Session helpers ──────────────────────────────────────────────────


def ensure_session(conn: "sqlite3.Connection | _PooledConnection", session_id: str, label: str = "") -> None:
    """Insert a session row if it doesn't exist."""
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, started_at, label) VALUES (?, ?, ?)",
        (session_id, time.time(), label),
    )
    conn.commit()


def log_event(
    conn: "sqlite3.Connection | _PooledConnection",
    session_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    agent_id: str | None = None,
) -> int:
    """Log a structured event and return its id."""
    payload_json = json.dumps(payload) if payload else None
    cursor = conn.execute(
        "INSERT INTO events (session_id, ts, type, payload, agent_id) VALUES (?, ?, ?, ?, ?)",
        (session_id, time.time(), event_type, payload_json, agent_id),
    )
    conn.commit()
    return cursor.lastrowid


def put_result(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str,
    body: str,
    *,
    key: str = "",
    status: str = "ok",
    summary: str = "",
    workspace: str | None = None,
) -> int:
    """Persist a structured result an agent hands back, latest-wins per
    (agent_id, key). Returns the new row id. Survives the agent's own crash —
    this is the durable artifact `agent wait` / `agent result get` read."""
    cursor = conn.execute(
        "INSERT INTO agent_results "
        "(agent_id, workspace, key, status, summary, body, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (agent_id, workspace, key or "", status or "ok",
         summary or "", body or "", time.time()),
    )
    conn.commit()
    return cursor.lastrowid


def get_results(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str,
    *,
    key: str | None = None,
    latest: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read results for an agent, newest-first. With `latest=True` returns at
    most one row (the most recent, optionally filtered to `key`); otherwise
    the recent history up to `limit`."""
    sql = "SELECT id, agent_id, workspace, key, status, summary, body, ts FROM agent_results WHERE agent_id = ?"
    params: list[Any] = [agent_id]
    if key is not None:
        sql += " AND key = ?"
        params.append(key)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(1 if latest else limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def upsert_agent(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str,
    agent_type: str,
    name: str,
    workspace: str | None = None,
    config_yaml: str | None = None,
    auto_start: bool = False,
    purpose: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Insert or update an agent's runtime status in the DB."""
    tags_json = json.dumps(list(tags)) if tags is not None else None
    conn.execute(
        """INSERT INTO agents (id, type, name, status, config_yaml, workspace, auto_start,
                               purpose, tags, created_at)
           VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               type=excluded.type,
               name=excluded.name,
               config_yaml=excluded.config_yaml,
               workspace=excluded.workspace,
               auto_start=excluded.auto_start,
               purpose=COALESCE(excluded.purpose, agents.purpose),
               tags=COALESCE(excluded.tags, agents.tags)""",
        (agent_id, agent_type, name, config_yaml, workspace, int(auto_start),
         purpose, tags_json, time.time()),
    )
    conn.commit()


def update_agent_status(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update an agent's runtime (process-level) status."""
    conn.execute(
        "UPDATE agents SET status=?, last_active_at=?, last_error=? WHERE id=?",
        (status, time.time(), error, agent_id),
    )
    conn.commit()


# Valid semantic-status values. NULL means "no signal yet". The
# domain is intentionally tight — adding values requires updating
# the roll-up precedence table and the wait CLI.
SEMANTIC_STATES = ("working", "awaiting-input", "complete-unread", "idle")


def update_semantic_status(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str,
    status: str | None,
    *,
    source: str = "hook",
) -> str | None:
    """Set the agent's observable semantic state.

    `status` must be one of `SEMANTIC_STATES` or None (clears).
    `source` records who set it — typical values: `hook` (vendor-
    side integration), `mood` (message classifier), `daemon`
    (internal lifecycle), `manual` (operator override). Returns
    the prior value so callers can decide whether to emit a
    change event.
    """
    if status is not None and status not in SEMANTIC_STATES:
        raise ValueError(
            f"invalid semantic status {status!r}; "
            f"expected one of {SEMANTIC_STATES} or None"
        )
    prior = conn.execute(
        "SELECT semantic_status FROM agents WHERE id = ?", (agent_id,),
    ).fetchone()
    prior_val = prior["semantic_status"] if prior else None
    conn.execute(
        "UPDATE agents SET semantic_status=?, semantic_status_at=?, "
        "semantic_status_source=? WHERE id=?",
        (status, time.time(), source, agent_id),
    )
    conn.commit()
    return prior_val


def get_semantic_status(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str,
) -> tuple[str | None, float | None, str | None]:
    """Read an agent's `(semantic_status, semantic_status_at, source)`.

    Returns `(None, None, None)` for an unknown agent. The semantic-status
    engine reads the tuple to arbitrate: it defers to a *fresh* authoritative
    producer (a vendor hook / hitl / manual override) and only writes when the
    field is unowned, its own, or the prior producer has gone stale."""
    row = conn.execute(
        "SELECT semantic_status, semantic_status_at, semantic_status_source "
        "FROM agents WHERE id = ?",
        (agent_id,),
    ).fetchone()
    if row is None:
        return None, None, None
    return (
        row["semantic_status"],
        row["semantic_status_at"],
        row["semantic_status_source"],
    )


def record_usage(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str,
    session_id: str,
    model: str,
    provider: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost_usd: float | None = None,
    request_count: int = 1,
) -> None:
    """Record a token usage event for metering."""
    conn.execute(
        "INSERT INTO usage_records (agent_id, session_id, ts, model, provider, "
        "prompt_tokens, completion_tokens, total_tokens, cost_usd, request_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent_id, session_id, time.time(), model, provider,
         prompt_tokens, completion_tokens, total_tokens, cost_usd, request_count),
    )
    conn.commit()


def get_usage_summary(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str | None = None,
    since_ts: float | None = None,
    workspace: str | None = None,
) -> list[dict[str, Any]]:
    """Aggregate usage by model+provider combination.

    When ``workspace`` is given, restrict to usage from agents currently in
    that workspace (usage_records carry only agent_id, so we join through the
    agents table). This keeps the dashboard's per-workspace usage honest — a
    fresh workspace with no agents totals zero instead of the fleet figure.
    """
    query = """
        SELECT model, provider,
               SUM(prompt_tokens) as total_prompt,
               SUM(completion_tokens) as total_completion,
               SUM(COALESCE(NULLIF(total_tokens,0), prompt_tokens + completion_tokens)) as total_tokens,
               SUM(cost_usd) as total_cost,
               SUM(request_count) as requests
        FROM usage_records
        WHERE 1=1
    """
    params: list = []
    if agent_id:
        query += " AND agent_id = ?"
        params.append(agent_id)
    if workspace:
        query += " AND agent_id IN (SELECT id FROM agents WHERE workspace = ?)"
        params.append(workspace)
    if since_ts:
        query += " AND ts >= ?"
        params.append(since_ts)
    query += " GROUP BY model, provider ORDER BY total_tokens DESC"

    return [dict(row) for row in conn.execute(query, params).fetchall()]


def get_agent_stats(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Per-agent rollup for the detail stat strip — real data only.

    Returns tokens (24h, split in/out), cost (24h), distinct model
    count, total + 24h event counts, last event ts, and a 30-minute
    activity spark (event counts in 1-minute buckets). Everything is
    derived from `usage_records` + `events` — no fabricated values.
    The dashboard renders "—" for zero/empty fields.
    """
    now = now if now is not None else time.time()
    day_ago = now - 86400.0

    row = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
        # Per-row total: use the logged total when present, else the
        # prompt+completion split. Doing this per row (not on the
        # aggregate sums) is what makes mixed rows — some with
        # total_tokens=0, some populated — count correctly. Matches the
        # fleet rollup expression.
        "COALESCE(SUM(COALESCE(NULLIF(total_tokens,0), prompt_tokens + completion_tokens)),0), "
        "COALESCE(SUM(cost_usd),0), COUNT(DISTINCT model) "
        "FROM usage_records WHERE agent_id = ? AND ts >= ?",
        (agent_id, day_ago),
    ).fetchone()
    tokens_in = int(row[0] or 0)
    tokens_out = int(row[1] or 0)
    tokens_24h = int(row[2] or 0)
    cost_24h = float(row[3] or 0.0)
    model_count = int(row[4] or 0)

    # Active model — the one from the most recent usage record (what the
    # agent is actually running on, read from the harness usage tail).
    # This can differ from config.model (e.g. /model switches at runtime).
    model_row = conn.execute(
        "SELECT model FROM usage_records WHERE agent_id = ? "
        "ORDER BY ts DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    active_model = model_row[0] if model_row else None

    ev_total = conn.execute(
        "SELECT COUNT(*) FROM events WHERE agent_id = ?", (agent_id,),
    ).fetchone()
    events_total = int(ev_total[0] or 0)

    last_ev = conn.execute(
        "SELECT ts, type FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    last_event_ts = float(last_ev[0]) if last_ev else None
    last_event_type = last_ev[1] if last_ev else None

    # Uptime is derived from the most recent harness.spawn event — the
    # only honest "this process came up at" signal we persist. The
    # client turns spawn_ts into a live-ticking duration when the agent
    # is running, and shows "—" otherwise.
    spawn_ev = conn.execute(
        "SELECT ts FROM events WHERE agent_id = ? AND type = 'harness.spawn' "
        "ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    spawn_ts = float(spawn_ev[0]) if spawn_ev else None

    # 30-minute activity spark, 30 one-minute buckets. One GROUP BY,
    # then densify into a fixed-length array so the client doesn't have
    # to reason about gaps.
    spark_n = 30
    window = now - spark_n * 60.0
    rows = conn.execute(
        "SELECT CAST((? - ts) / 60 AS INT) AS b, COUNT(*) "
        "FROM events WHERE agent_id = ? AND ts >= ? GROUP BY b",
        (now, agent_id, window),
    ).fetchall()
    buckets = [0] * spark_n
    for b, c in rows:
        idx = spark_n - 1 - int(b)   # oldest → newest, left → right
        if 0 <= idx < spark_n:
            buckets[idx] = int(c)

    return {
        "tokens_24h": tokens_24h,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_24h": cost_24h,
        "model_count": model_count,
        "events_total": events_total,
        "last_event_ts": last_event_ts,
        "last_event_type": last_event_type,
        "spawn_ts": spawn_ts,
        "model": active_model,
        "activity": buckets,
    }


def get_fleet_token_rollup(
    conn: "sqlite3.Connection | _PooledConnection",
    *,
    now: float | None = None,
    buckets: int = 24,
) -> dict[str, dict[str, Any]]:
    """Fleet-wide per-agent token totals + a coarse 24h spark, in a
    single GROUP BY so the sidebar doesn't fan out N queries.

    Returns `{agent_id: {"tokens": int, "spark": [int * buckets]}}`.
    `tokens` is the 24h sum (prompt+completion fallback); `spark` is
    `buckets` hourly token buckets oldest→newest. Agents with no usage
    in the window are simply absent from the map.
    """
    now = now if now is not None else time.time()
    window = now - buckets * 3600.0
    rows = conn.execute(
        "SELECT agent_id, CAST((? - ts) / 3600 AS INT) AS b, "
        "SUM(COALESCE(NULLIF(total_tokens,0), prompt_tokens + completion_tokens)) "
        "FROM usage_records WHERE ts >= ? GROUP BY agent_id, b",
        (now, window),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for agent_id, b, toks in rows:
        rec = out.setdefault(agent_id, {"tokens": 0, "spark": [0] * buckets})
        idx = buckets - 1 - int(b)
        if 0 <= idx < buckets:
            rec["spark"][idx] = int(toks or 0)
        rec["tokens"] += int(toks or 0)
    return out


_TOK_EXPR = "COALESCE(NULLIF(total_tokens,0), prompt_tokens + completion_tokens)"


def get_agent_usage_heatmap(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str | None = None,
    *,
    now: float | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Token usage as a calendar heatmap: `days` rows (local calendar days,
    oldest→newest) × 24 hourly cells. Real token sums from `usage_records`
    — empty cells render honest 0. Pass `agent_id=None` for a fleet-wide
    heatmap (all agents). Returns
    `{rows:[{date, cells[24]}], max, total, days, busiest}`.

    Timezone: bucketed in the daemon's local time (so "today" lines up
    with the operator's wall clock); `ts` is epoch seconds.
    """
    import datetime as _dt

    now = now if now is not None else time.time()
    today = _dt.datetime.fromtimestamp(now).date()
    start_date = today - _dt.timedelta(days=days - 1)
    start_ts = _dt.datetime.combine(start_date, _dt.time()).timestamp()

    if agent_id is None:
        rows_raw = conn.execute(
            f"SELECT ts, {_TOK_EXPR} FROM usage_records WHERE ts >= ?",
            (start_ts,),
        ).fetchall()
    else:
        rows_raw = conn.execute(
            f"SELECT ts, {_TOK_EXPR} FROM usage_records "
            "WHERE agent_id = ? AND ts >= ?",
            (agent_id, start_ts),
        ).fetchall()

    grid: dict[str, list[int]] = {}
    for d in range(days):
        grid[(start_date + _dt.timedelta(days=d)).isoformat()] = [0] * 24

    total = 0
    busiest = {"date": None, "hour": None, "tokens": 0}
    for ts, toks in rows_raw:
        dt = _dt.datetime.fromtimestamp(ts)
        di = dt.date().isoformat()
        cells = grid.get(di)
        if cells is None:
            continue
        t = int(toks or 0)
        cells[dt.hour] += t
        total += t
        if cells[dt.hour] > busiest["tokens"]:
            busiest = {"date": di, "hour": dt.hour, "tokens": cells[dt.hour]}

    out_rows = []
    mx = 0
    for d in range(days):
        di = (start_date + _dt.timedelta(days=d)).isoformat()
        cells = grid[di]
        mx = max(mx, max(cells))
        out_rows.append({"date": di, "cells": cells})

    return {
        "rows": out_rows, "max": mx, "total": total,
        "days": days, "busiest": busiest,
    }


def get_agent_session_contexts(
    conn: "sqlite3.Connection | _PooledConnection",
    agent_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Per-session "context usage" for an agent's threads. The context a
    model carries is the `prompt_tokens` it's sent each turn (system prompt
    + accumulated history + tool defs), so the **latest turn's
    prompt_tokens is the current context fill** and the max is the peak.
    Real data from `usage_records` (+ `sessions.label`); no model context
    limit is assumed — callers scale the bar relative to the peak.

    Returns rows newest-active-first:
    `{session_id, label, turns, current_context, peak_context,
      total_tokens, cost_usd, model, first_ts, last_ts}`.
    """
    rows = conn.execute(
        f"""
        SELECT u.session_id AS session_id,
               COUNT(*) AS turns,
               SUM({_TOK_EXPR}) AS total_tokens,
               SUM(u.cost_usd) AS cost_usd,
               MAX(u.prompt_tokens) AS peak_context,
               MIN(u.ts) AS first_ts,
               MAX(u.ts) AS last_ts,
               (SELECT u2.prompt_tokens FROM usage_records u2
                  WHERE u2.agent_id = u.agent_id AND u2.session_id = u.session_id
                  ORDER BY u2.ts DESC LIMIT 1) AS current_context,
               (SELECT u3.model FROM usage_records u3
                  WHERE u3.agent_id = u.agent_id AND u3.session_id = u.session_id
                  ORDER BY u3.ts DESC LIMIT 1) AS model,
               s.label AS label
        FROM usage_records u
        LEFT JOIN sessions s ON s.id = u.session_id
        WHERE u.agent_id = ?
        GROUP BY u.session_id
        ORDER BY last_ts DESC
        LIMIT ?
        """,
        (agent_id, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "session_id": r["session_id"],
            "label": (r["label"] or "") if "label" in r.keys() else "",
            "turns": int(r["turns"] or 0),
            "current_context": int(r["current_context"] or 0),
            "peak_context": int(r["peak_context"] or 0),
            "total_tokens": int(r["total_tokens"] or 0),
            "cost_usd": float(r["cost_usd"] or 0),
            "model": r["model"] or "",
            "first_ts": r["first_ts"],
            "last_ts": r["last_ts"],
        })
    return out


def get_preset_usage_map(
    conn: "sqlite3.Connection | _PooledConnection",
    *,
    now: float | None = None,
    buckets: int = 24,
) -> dict[str, dict[str, Any]]:
    """24h usage rollup keyed by **lowercase model id**, for the Models
    sidebar (each preset is matched by its model). One GROUP BY over
    `usage_records`. Returns
    `{model: {tokens_24h, requests_24h, cost_24h, spark[buckets]}}`.
    Models with no usage in the window are simply absent.
    """
    now = now if now is not None else time.time()
    window = now - buckets * 3600.0
    rows = conn.execute(
        f"SELECT LOWER(model) AS m, CAST((? - ts) / 3600 AS INT) AS b, "
        f"SUM({_TOK_EXPR}), SUM(request_count), SUM(cost_usd) "
        "FROM usage_records WHERE ts >= ? GROUP BY m, b",
        (now, window),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for model, b, toks, reqs, cost in rows:
        rec = out.setdefault(
            model, {"tokens_24h": 0, "requests_24h": 0, "cost_24h": 0.0,
                    "spark": [0] * buckets})
        idx = buckets - 1 - int(b)
        if 0 <= idx < buckets:
            rec["spark"][idx] = int(toks or 0)
        rec["tokens_24h"] += int(toks or 0)
        rec["requests_24h"] += int(reqs or 0)
        rec["cost_24h"] += float(cost or 0.0)
    return out


def get_provider_usage_map(
    conn: "sqlite3.Connection | _PooledConnection",
    *,
    now: float | None = None,
) -> dict[str, dict[str, Any]]:
    """24h usage rollup keyed by **lowercase provider name**, for the
    Providers list/detail. Returns
    `{provider: {tokens_24h, requests_24h, cost_24h}}`."""
    now = now if now is not None else time.time()
    window = now - 86400.0
    rows = conn.execute(
        f"SELECT LOWER(provider) AS p, SUM({_TOK_EXPR}), "
        "SUM(request_count), SUM(cost_usd) "
        "FROM usage_records WHERE ts >= ? GROUP BY p",
        (window,),
    ).fetchall()
    return {
        (p or ""): {
            "tokens_24h": int(toks or 0),
            "requests_24h": int(reqs or 0),
            "cost_24h": float(cost or 0.0),
        }
        for p, toks, reqs, cost in rows
    }


def get_model_stats(
    conn: "sqlite3.Connection | _PooledConnection",
    model: str,
    *,
    provider: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Per-model rollup for the preset-detail screen — real data only,
    from `usage_records`. Returns a 60-minute (1-min bucket) request +
    token series, 24h totals, and `used_by` (agents that actually called
    this model in the last 24h, with call + token counts). Empty/zero
    when there's no recorded usage — never fabricated."""
    now = now if now is not None else time.time()
    mlow = (model or "").lower()
    where = "LOWER(model) = ?"
    params: list = [mlow]
    if provider:
        where += " AND LOWER(provider) = ?"
        params.append(provider.lower())

    # 60-minute series, 1-minute buckets, oldest→newest.
    spark_n = 60
    window = now - spark_n * 60.0
    rows = conn.execute(
        f"SELECT CAST((? - ts) / 60 AS INT) AS b, "
        f"SUM(request_count), SUM({_TOK_EXPR}) "
        f"FROM usage_records WHERE {where} AND ts >= ? GROUP BY b",
        (now, *params, window),
    ).fetchall()
    req_series = [0] * spark_n
    tok_series = [0] * spark_n
    for b, reqs, toks in rows:
        idx = spark_n - 1 - int(b)
        if 0 <= idx < spark_n:
            req_series[idx] = int(reqs or 0)
            tok_series[idx] = int(toks or 0)

    day_ago = now - 86400.0
    agg = conn.execute(
        f"SELECT SUM(request_count), SUM({_TOK_EXPR}), SUM(cost_usd) "
        f"FROM usage_records WHERE {where} AND ts >= ?",
        (*params, day_ago),
    ).fetchone()
    used = conn.execute(
        f"SELECT agent_id, SUM(request_count), SUM({_TOK_EXPR}) "
        f"FROM usage_records WHERE {where} AND ts >= ? "
        "GROUP BY agent_id ORDER BY 2 DESC LIMIT 12",
        (*params, day_ago),
    ).fetchall()
    return {
        "requests_series": req_series,
        "tokens_series": tok_series,
        "requests_24h": int((agg[0] if agg else 0) or 0),
        "tokens_24h": int((agg[1] if agg else 0) or 0),
        "cost_24h": float((agg[2] if agg else 0.0) or 0.0),
        "requests_hour": sum(req_series),
        "tokens_hour": sum(tok_series),
        "used_by": [
            {"agent_id": a, "requests": int(r or 0), "tokens": int(t or 0)}
            for a, r, t in used
        ],
    }
