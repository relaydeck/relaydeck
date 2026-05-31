"""
Messaging durability tests.

Lock in the delivery-state machine: attempt counter, retry cap,
crash/restart survival, idempotent mark_injected, additive migration
behavior.

These complement the broader test_messaging.py suite which focuses on
the end-user CLI / plugin / API behavior; this file targets the
durability primitives in `relaydeck/messages.py` directly so they remain
honest as the surface around them evolves.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from relaydeck import messages
from relaydeck.db import open_db
from relaydeck.messages import (
    MAX_DELIVERY_ATTEMPTS,
    STATE_DELIVERED,
    STATE_FAILED,
    STATE_QUEUED,
    drain_pending,
    insert_message,
    list_failed,
    list_one,
    mark_injected,
    record_delivery_failure,
)


@pytest.fixture
def db_path(tmp_path) -> str:
    """Fresh SQLite DB per test, opened to run migrations once."""
    p = tmp_path / "relaydeck.db"
    conn = open_db(str(p))
    conn.close()
    return str(p)


# ── Schema additivity ────────────────────────────────────────────────


def test_pre_migration_db_gets_new_columns(tmp_path):
    """A DB created before migration 5 must pick up the new columns
    when reopened. Simulate by hand-creating the table without them."""
    p = tmp_path / "pre.db"
    conn = sqlite3.connect(str(p))
    conn.execute(
        """CREATE TABLE agent_messages (
            id TEXT PRIMARY KEY, from_id TEXT NOT NULL, to_id TEXT NOT NULL,
            body TEXT NOT NULL, rendered_body TEXT, in_reply_to TEXT,
            ts REAL NOT NULL, injected_at REAL, workspace TEXT
        )"""
    )
    conn.commit()
    conn.close()

    # Reopen via the real path — migrations should run additively.
    conn = open_db(str(p))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_messages)")}
    conn.close()
    assert {"attempts", "last_error", "delivery_state"} <= cols


def test_new_message_starts_in_queued_state(db_path):
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)
    msg = list_one(msg_id, db_path=db_path)
    assert msg is not None
    assert msg.delivery_state == STATE_QUEUED
    assert msg.attempts == 0
    assert msg.last_error is None
    assert msg.injected_at is None


# ── State transitions ────────────────────────────────────────────────


def test_mark_injected_sets_delivered(db_path):
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)
    mark_injected(msg_id, "rendered text", db_path=db_path)
    msg = list_one(msg_id, db_path=db_path)
    assert msg.delivery_state == STATE_DELIVERED
    assert msg.injected_at is not None
    assert msg.rendered_body == "rendered text"


def test_mark_injected_is_idempotent(db_path):
    """Calling twice doesn't corrupt state — important if drain races
    with the orchestrator's send path."""
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)
    mark_injected(msg_id, "first", db_path=db_path)
    first_ts = list_one(msg_id, db_path=db_path).injected_at
    mark_injected(msg_id, "second", db_path=db_path)
    msg = list_one(msg_id, db_path=db_path)
    assert msg.delivery_state == STATE_DELIVERED
    assert msg.rendered_body == "second"  # latest write wins, by design
    # ts may equal or exceed the first (clock resolution); never NULL again
    assert msg.injected_at >= first_ts


def test_failure_increments_attempts(db_path):
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)
    state = record_delivery_failure(msg_id, "PTY closed", db_path=db_path)
    assert state == STATE_QUEUED
    msg = list_one(msg_id, db_path=db_path)
    assert msg.attempts == 1
    assert msg.last_error == "PTY closed"


def test_attempts_cap_transitions_to_failed(db_path):
    """After MAX_DELIVERY_ATTEMPTS failed attempts the message must
    transition to terminal `failed` and be excluded from drain."""
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)
    state = STATE_QUEUED
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        state = record_delivery_failure(msg_id, "fail", db_path=db_path)
    assert state == STATE_FAILED
    msg = list_one(msg_id, db_path=db_path)
    assert msg.delivery_state == STATE_FAILED
    assert msg.attempts == MAX_DELIVERY_ATTEMPTS
    # Excluded from drain
    assert drain_pending("alice", db_path=db_path) == []


def test_custom_max_attempts(db_path):
    """The cap is a parameter so plugins can tune it without forking."""
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)
    s = record_delivery_failure(msg_id, "x", db_path=db_path, max_attempts=2)
    assert s == STATE_QUEUED
    s = record_delivery_failure(msg_id, "x", db_path=db_path, max_attempts=2)
    assert s == STATE_FAILED


def test_failure_for_unknown_msg_does_not_raise(db_path):
    """Defensive: caller doesn't need to first-check existence."""
    state = record_delivery_failure("msg_nonexistent", "boom", db_path=db_path)
    assert state == STATE_QUEUED


def test_failure_after_delivery_does_not_downgrade_state(db_path):
    """Regression: a delivered message must not be flipped back to
    queued/failed if a late record_delivery_failure call arrives.

    Sequence under race:
      1. orchestrator sees PTY write succeed → mark_injected
      2. harness drain (concurrent) saw an earlier transient failure
         and now calls record_delivery_failure for the SAME id

    Pre-fix, step 2 would increment attempts on the delivered row and
    eventually flip delivery_state to `failed`, undoing step 1. The
    fix guards both writes with `WHERE injected_at IS NULL AND
    delivery_state != 'delivered'`.
    """
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)
    mark_injected(msg_id, "rendered", db_path=db_path)
    # All subsequent failures should be no-ops returning STATE_DELIVERED.
    for _ in range(MAX_DELIVERY_ATTEMPTS + 5):
        result = record_delivery_failure(msg_id, "late", db_path=db_path)
        assert result == STATE_DELIVERED
    msg = list_one(msg_id, db_path=db_path)
    assert msg.delivery_state == STATE_DELIVERED
    assert msg.attempts == 0      # never incremented after delivery
    assert msg.injected_at is not None
    # mark_injected wrote rendered_body; failure path must not touch it.
    assert msg.rendered_body == "rendered"


def test_failure_for_vanished_message_returns_queued(db_path):
    """Distinct from the delivered case: when the message id doesn't
    exist in the table at all, return STATE_QUEUED (the historical
    no-such-row response) so callers can keep their existing branches."""
    state = record_delivery_failure("msg_never_existed", "boom", db_path=db_path)
    assert state == STATE_QUEUED


# ── Drain semantics ──────────────────────────────────────────────────


def test_drain_excludes_failed(db_path):
    a = insert_message("user", "alice", "first", db_path=db_path)
    b = insert_message("user", "alice", "second", db_path=db_path)
    # Burn `a` through the cap
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        record_delivery_failure(a, "boom", db_path=db_path)
    pending = drain_pending("alice", db_path=db_path)
    ids = [m.id for m in pending]
    assert a not in ids
    assert b in ids


def test_drain_excludes_delivered(db_path):
    """Sanity: delivered messages stay out of drain on the next call."""
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)
    assert [m.id for m in drain_pending("alice", db_path=db_path)] == [msg_id]
    mark_injected(msg_id, "rendered", db_path=db_path)
    assert drain_pending("alice", db_path=db_path) == []


def test_drain_oldest_first(db_path):
    import time as _t
    a = insert_message("user", "alice", "first", db_path=db_path)
    _t.sleep(0.005)
    b = insert_message("user", "alice", "second", db_path=db_path)
    pending = drain_pending("alice", db_path=db_path)
    assert [m.id for m in pending] == [a, b]


# ── Crash / restart survival ─────────────────────────────────────────


def test_pending_messages_survive_db_reopen(db_path):
    """Insert, close everything, reopen — the queued message remains
    drainable. Models a daemon crash between insert and PTY write."""
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)
    # Re-open from scratch to model a fresh process boot.
    pending = drain_pending("alice", db_path=db_path)
    assert [m.id for m in pending] == [msg_id]


def test_attempts_persist_across_reopens(db_path):
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)
    record_delivery_failure(msg_id, "boom", db_path=db_path)
    record_delivery_failure(msg_id, "still boom", db_path=db_path)
    # New "process" reads the same DB
    msg = list_one(msg_id, db_path=db_path)
    assert msg.attempts == 2
    assert msg.last_error == "still boom"


# ── Concurrency ──────────────────────────────────────────────────────


def test_concurrent_inserts_do_not_corrupt(db_path):
    """Two threads inserting simultaneously each produce a row with a
    unique id. SQLite write serialization + WAL handles the locking
    for us; this is the regression guard if WAL ever gets disabled."""
    ids: list[str] = []
    lock = threading.Lock()

    def worker(n: int):
        for i in range(20):
            mid = insert_message("user", "alice", f"t{n}-{i}", db_path=db_path)
            with lock:
                ids.append(mid)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == 80
    assert len(set(ids)) == 80  # all unique


def test_concurrent_failures_atomic_counter(db_path):
    """Two threads recording failures on the same message produce
    deterministic attempts == 2, never 1 (lost update)."""
    msg_id = insert_message("user", "alice", "hi", db_path=db_path)

    def fail():
        record_delivery_failure(msg_id, "race", db_path=db_path)

    t1 = threading.Thread(target=fail)
    t2 = threading.Thread(target=fail)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    msg = list_one(msg_id, db_path=db_path)
    assert msg.attempts == 2


# ── Operator visibility ──────────────────────────────────────────────


def test_list_failed_returns_only_terminal(db_path):
    queued = insert_message("user", "alice", "q", db_path=db_path)
    delivered = insert_message("user", "alice", "d", db_path=db_path)
    failing = insert_message("user", "alice", "f", db_path=db_path)
    mark_injected(delivered, "rendered", db_path=db_path)
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        record_delivery_failure(failing, "boom", db_path=db_path)
    failed = list_failed(db_path=db_path)
    assert [m.id for m in failed] == [failing]
    assert queued not in [m.id for m in failed]
    assert delivered not in [m.id for m in failed]


def test_list_failed_scoped_by_workspace(db_path):
    a = insert_message("user", "alice", "x", workspace="ws-a", db_path=db_path)
    b = insert_message("user", "bob", "y", workspace="ws-b", db_path=db_path)
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        record_delivery_failure(a, "boom", db_path=db_path)
        record_delivery_failure(b, "boom", db_path=db_path)
    only_a = list_failed(workspace="ws-a", db_path=db_path)
    assert [m.id for m in only_a] == [a]


# ── WAL pragma ───────────────────────────────────────────────────────


def test_wal_is_enabled(db_path):
    """Concurrent reader+writer requires WAL. Regression guard: if
    someone "fixes" the pragma to OFF or removes it, this catches it."""
    conn = open_db(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()
