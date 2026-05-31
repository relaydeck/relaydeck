"""
Agent-to-agent messaging primitive.

Durable SQLite-backed inbox per agent. Sender may be `"user"`, another
agent's `agent_id`, or a plugin name. Recipient is always an agent.

The data layer lives here so multiple callers (orchestrator HTTP
handlers, harness drain-on-start, the messaging plugin's CLI) all use
the same helpers. The user-facing surface — CLI, HTTP endpoints,
settings, skill injection — lives in `plugins/messaging/`.

Closest prior art is Google's A2A protocol (task store + envelope with
sender, recipient, reply_ref). Differences: relaydeck uses a single
`in_reply_to` field (not `referenceTaskIds[]`), and persistence is
local SQLite (not a remote task service).
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck.db import open_db

logger = logging.getLogger(__name__)


# Built-in default. Override with per-message `format=`, per-agent
# `agent.config["message_format"]`, or plugin setting `default_format`.
#
# The `[relay ...]` prefix is a deliberate marker: it tells the
# receiving agent's model "this is a peer message, consult the
# relaydeck-cli skill", and gives a stable parseable shape to extract
# `from` (who sent it) and `id` (for `--in-reply-to`). The skill
# documents the format so a confused harness has somewhere to look.
DEFAULT_FORMAT = "[relay from={from} id={id}] {body}"


# After this many failed PTY-write attempts a message stops being
# retried. It transitions to `delivery_state='failed'` and is excluded
# from `drain_pending` so the agent doesn't get pummeled with the same
# undeliverable payload on every restart. Operators can query the
# `failed` state via `list_failed()` and decide whether to surface,
# requeue, or discard. The default is generous (a transient PTY hiccup
# burns 1-2 attempts; a genuinely-broken target burns through 10 over
# multiple restart cycles before going terminal).
MAX_DELIVERY_ATTEMPTS = 10

# Delivery state machine:
#   queued      → fresh message, eligible for drain
#   delivered   → successfully written to PTY (injected_at is set)
#   failed      → attempts >= MAX_DELIVERY_ATTEMPTS, terminal
#
# `injected_at` is the legacy field — it remains the source of truth
# for "did it land" and stays NULL for both queued and failed. Adding
# delivery_state is purely additive so operators can distinguish "still
# trying" from "given up".
STATE_QUEUED = "queued"
STATE_DELIVERED = "delivered"
STATE_FAILED = "failed"


@dataclass
class Message:
    """One row from agent_messages."""

    id: str
    from_id: str
    to_id: str
    body: str
    ts: float
    rendered_body: str | None = None
    in_reply_to: str | None = None
    injected_at: float | None = None
    workspace: str | None = None
    attempts: int = 0
    last_error: str | None = None
    delivery_state: str = STATE_QUEUED

    def to_dict(self) -> dict[str, Any]:
        """Public payload shape used by API responses and plugin events.

        `from` and `to` instead of `from_id`/`to_id` to match the design
        in FOUNDATIONAL_ROADMAP.md and the convention in other agent
        frameworks.
        """
        return {
            "id": self.id,
            "from": self.from_id,
            "to": self.to_id,
            "body": self.body,
            "rendered_body": self.rendered_body,
            "in_reply_to": self.in_reply_to,
            "ts": self.ts,
            "injected_at": self.injected_at,
            "workspace": self.workspace,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "delivery_state": self.delivery_state,
        }


def _default_db_path() -> str:
    return str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db")


def _row_to_message(row: sqlite3.Row) -> Message:
    # Pre-migration rows lack the durability columns. Tolerate them via
    # `keys()` rather than KeyError so a fresh-on-disk DB upgraded mid-
    # stream by another process still reads cleanly.
    keys = set(row.keys())
    return Message(
        id=row["id"],
        from_id=row["from_id"],
        to_id=row["to_id"],
        body=row["body"],
        rendered_body=row["rendered_body"],
        in_reply_to=row["in_reply_to"],
        ts=row["ts"],
        injected_at=row["injected_at"],
        workspace=row["workspace"],
        attempts=int(row["attempts"]) if "attempts" in keys and row["attempts"] is not None else 0,
        last_error=row["last_error"] if "last_error" in keys else None,
        delivery_state=row["delivery_state"] if "delivery_state" in keys else STATE_QUEUED,
    )


def _short_id() -> str:
    """`msg_` + 12 hex chars. Short enough to paste in CLI, long enough
    to be collision-free at the volumes a single daemon will see."""
    return "msg_" + uuid.uuid4().hex[:12]


def new_broadcast_id() -> str:
    """Shared id for every fan-out row of one fleet broadcast."""
    return f"bc_{_short_id().removeprefix('msg_')}"


# ── Inserts and updates ─────────────────────────────────────────────


def insert_message(
    from_id: str,
    to_id: str,
    body: str,
    *,
    in_reply_to: str | None = None,
    workspace: str | None = None,
    broadcast_id: str | None = None,
    db_path: str | None = None,
) -> str:
    """Insert a new message; return its id. `injected_at` is NULL until
    the orchestrator (or the harness drain path) writes the rendered
    body to a live PTY.

    `broadcast_id` groups the fan-out rows of one fleet broadcast; pass a
    shared id for every recipient of a workspace-wide message so the
    reply-owed nudge treats it as an announcement, not N separate 1:1 asks.
    Leave None for a direct (targeted) message — it still owes a reply."""
    msg_id = _short_id()
    conn = open_db(db_path or _default_db_path())
    try:
        conn.execute(
            """INSERT INTO agent_messages
               (id, from_id, to_id, body, in_reply_to, ts, workspace, broadcast_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, from_id, to_id, body, in_reply_to, time.time(), workspace,
             broadcast_id),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        from relaydeck.metrics import record_message_state
        record_message_state("queued")
    except Exception:
        pass
    return msg_id


def enqueue_workspace_messages(
    workspace: str,
    body: str,
    *,
    from_id: str = "user",
    agent: str | None = None,
    in_reply_to: str | None = None,
    include_self: bool = False,
    db_path: str | None = None,
) -> list[str]:
    """Persist messages when the daemon is unreachable.

    Mirrors the messaging plugin's durable fallback — queued rows drain
    on the target harness's next start."""
    from relaydeck.config import load_workspace_registry

    registry = {w.name: w for w in load_workspace_registry()}
    if workspace not in registry:
        return []

    path = db_path or _default_db_path()
    conn = open_db(path)
    try:
        rows = conn.execute(
            "SELECT id FROM agents WHERE workspace = ?", (workspace,),
        ).fetchall()
        agent_ids = [r["id"] for r in rows]
    finally:
        conn.close()

    if agent:
        if agent not in agent_ids:
            from relaydeck.config import load_agent_specs
            yaml_ids = {
                s.id for s in load_agent_specs()
                if (s.workspace or "") == workspace
            }
            if agent not in yaml_ids:
                return []
        recipients = [agent]
        broadcast_id = None
    else:
        recipients = list(agent_ids)
        if not include_self and from_id not in ("user",) and not from_id.startswith("plugin:"):
            recipients = [a for a in recipients if a != from_id]
        # A fleet-wide message (no specific target) is one announcement, not
        # N separate 1:1 asks — stamp every fan-out row with a shared id so
        # the reply-owed nudge doesn't pester each recipient to reply.
        broadcast_id = new_broadcast_id()

    return [
        insert_message(
            from_id, to_id, body,
            in_reply_to=in_reply_to,
            workspace=workspace,
            broadcast_id=broadcast_id,
            db_path=path,
        )
        for to_id in recipients
    ]


def mark_injected(msg_id: str, rendered_body: str,
                  db_path: str | None = None) -> None:
    """Mark a message as best-effort delivered to the receiver's PTY.

    Stores the rendered string verbatim so audit can reconstruct what
    was actually injected, even if `default_format` changes later.
    Sets `delivery_state='delivered'` so the durability admin queries
    can distinguish landed messages from stuck-but-still-queued ones.
    Idempotent — calling twice is a no-op beyond updating the
    timestamp.
    """
    conn = open_db(db_path or _default_db_path())
    try:
        conn.execute(
            "UPDATE agent_messages SET injected_at = ?, rendered_body = ?, "
            "delivery_state = ? WHERE id = ?",
            (time.time(), rendered_body, STATE_DELIVERED, msg_id),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        from relaydeck.metrics import record_message_state
        record_message_state(STATE_DELIVERED)
    except Exception:
        pass


def record_delivery_failure(msg_id: str, error: str,
                            db_path: str | None = None,
                            max_attempts: int = MAX_DELIVERY_ATTEMPTS) -> str:
    """Bump the attempt counter for a failed PTY-write and record the
    error. Returns the new `delivery_state`:
      - `delivered` — the message was actually delivered between the
        caller observing failure and us writing the update; no-op.
      - `queued` — still eligible for retry on the next drain.
      - `failed` — attempts ≥ max_attempts, terminal.

    Both the attempt increment and the terminal-state transition are
    guarded by `WHERE injected_at IS NULL AND delivery_state !=
    'delivered'` so a concurrent `mark_injected` from the orchestrator
    can't be downgraded by a late-arriving failure from the harness
    drain (or vice versa). The two writes serialize through SQLite's
    write lock, but without the guard the *later* write would simply
    overwrite the *earlier* state.

    This is the path the harness drain loop / orchestrator
    send_message_to should call when `send_message` returns False or
    raises. Without it, retries are silent and there's no operator
    visibility into why a message keeps failing.
    """
    conn = open_db(db_path or _default_db_path())
    try:
        # Atomic increment + read so two concurrent failures don't race.
        # The WHERE clause makes this idempotent against a successful
        # delivery: if the message is already delivered, the UPDATE
        # touches no rows and RETURNING is empty.
        cur = conn.execute(
            "UPDATE agent_messages "
            "SET attempts = attempts + 1, last_error = ? "
            "WHERE id = ? "
            "  AND injected_at IS NULL "
            "  AND delivery_state != ? "
            "RETURNING attempts",
            (error[:500], msg_id, STATE_DELIVERED),
        )
        row = cur.fetchone()
        if row is None:
            # Either the message vanished or it was already delivered.
            # Look up the current state so the caller can distinguish.
            conn.commit()
            existing = conn.execute(
                "SELECT delivery_state FROM agent_messages WHERE id = ?",
                (msg_id,),
            ).fetchone()
            if existing is None:
                return STATE_QUEUED  # message vanished — caller logs/ignores
            return str(existing[0])
        attempts = int(row[0])
        if attempts >= max_attempts:
            # Same guard — never transition a delivered message to
            # failed. The UPDATE above already wouldn't have touched
            # the row if it was delivered, so attempts wouldn't have
            # been incremented; this guard is belt-and-suspenders.
            conn.execute(
                "UPDATE agent_messages SET delivery_state = ? "
                "WHERE id = ? AND delivery_state != ?",
                (STATE_FAILED, msg_id, STATE_DELIVERED),
            )
            conn.commit()
            try:
                from relaydeck.metrics import record_message_state
                record_message_state(STATE_FAILED)
            except Exception:
                pass
            return STATE_FAILED
        conn.commit()
        try:
            from relaydeck.metrics import _REGISTRY
            _REGISTRY.counter("relaydeck_message_delivery_attempts_total").add(1.0)
        except Exception:
            pass
        return STATE_QUEUED
    finally:
        conn.close()


def list_failed(workspace: str | None = None,
                limit: int = 50,
                db_path: str | None = None) -> list[Message]:
    """Operator visibility: messages that exceeded MAX_DELIVERY_ATTEMPTS
    and are sitting in the terminal `failed` state. Useful for the
    `relaydeck workspace inbox --failed` admin path."""
    conn = open_db(db_path or _default_db_path())
    try:
        q = "SELECT * FROM agent_messages WHERE delivery_state = ?"
        params: list[Any] = [STATE_FAILED]
        if workspace:
            q += " AND workspace = ?"
            params.append(workspace)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(q, params).fetchall()
        return [_row_to_message(r) for r in rows]
    finally:
        conn.close()


def list_unanswered_peer_messages(
    to_id: str | None = None,
    *,
    workspace: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> list[Message]:
    """Peer messages that owe a durable reply but have none yet.

    A message "owes a reply" (per the `[relay from=… id=…]` contract) when
    ALL of the following hold:

    - it was sent by a **registered fleet agent** — one that exists in the
      `agents` table, so it has an inbox of its own to receive the reply.
      This excludes the operator (`user`), plugins (`plugin:*`), channel
      addresses (`telegram:…`, `discord:…`, …), and agents that have since
      been removed. None of those read an agent inbox, and a reply routed
      to them never threads a row back here — so without this guard they
      would be flagged as unanswered *forever* (e.g. a Telegram chat, whose
      reply goes out through the bot and never lands in `agent_messages`);
    - it is an **initiating** message — `in_reply_to IS NULL`. A message
      that is itself a reply does NOT owe a counter-reply. This breaks the
      ack loop for *threaded* replies — the path the runaway actually took,
      since `relaydeck reply` always threads: without it, two agents acking
      each other ("thanks" → "no problem" → "ok" → …) each owe a reply to
      the other's threaded ack forever. NOTE this is not a total guarantee:
      an ack sent as a fresh *root* message (e.g. `agent send` without
      --in-reply-to) is still flagged, so it could in principle loop; that
      vector is covered by the behavioural layer (the relaydeck-cli skill's
      "don't acknowledge acknowledgements" guidance), not by this query.
      TRADE-OFF: a substantive question or task raised as a reply *inside*
      an existing thread is therefore not surfaced here (neither nudged nor
      shown in `inbox --awaiting-reply`) — only thread-opening messages are.
      Inspect open threads with plain `inbox` / `message show`. Suppressing
      in-thread replies is what keeps the (loop-prone) reply path safe;
      there is no SQL predicate that separates a bare ack from a real
      in-thread ask, so the conservative rule wins for the auto-nudge.
    - it was actually delivered (`injected_at` set);
    - it is not self-sent; and
    - no other message threads off it via `in_reply_to` (i.e. it is still
      unanswered).

    Oldest first, so the caller can act on the most overdue one.

    Scope by recipient (`to_id`, used by the idle-edge nudge) and/or by
    `workspace` (used by the operator-facing `inbox --awaiting-reply` view).
    """
    clauses = [
        # Sender must be a registered fleet agent with an inbox. This excludes
        # the operator (`user`), plugins (`plugin:*`), channel addresses
        # (`telegram:…`), and removed agents — see docstring.
        "EXISTS (SELECT 1 FROM agents a WHERE a.id = m.from_id)",
        "m.from_id != m.to_id",
        "m.injected_at IS NOT NULL",
        # Only an initiating message owes a reply; a reply never owes a
        # counter-reply. Structural ack-loop breaker — see docstring.
        "m.in_reply_to IS NULL",
        # A fleet broadcast is an announcement, not N separate 1:1 asks —
        # its fan-out rows share a broadcast_id and never owe a per-recipient
        # reply (otherwise one workspace message nudges every agent to reply,
        # and the acks pollute the threads). Direct messages have NULL here.
        "m.broadcast_id IS NULL",
        "NOT EXISTS (SELECT 1 FROM agent_messages r WHERE r.in_reply_to = m.id)",
    ]
    params: list[Any] = []
    if to_id is not None:
        clauses.insert(0, "m.to_id = ?")
        params.append(to_id)
    if workspace is not None:
        clauses.append("m.workspace = ?")
        params.append(workspace)
    params.append(int(limit))
    conn = open_db(db_path or _default_db_path())
    try:
        rows = conn.execute(
            "SELECT m.* FROM agent_messages m WHERE "
            + " AND ".join(clauses)
            + " ORDER BY m.ts ASC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_message(r) for r in rows]
    finally:
        conn.close()


# ── Reads ───────────────────────────────────────────────────────────


def list_one(msg_id: str, db_path: str | None = None) -> Message | None:
    conn = open_db(db_path or _default_db_path())
    try:
        row = conn.execute(
            "SELECT * FROM agent_messages WHERE id = ?", (msg_id,)
        ).fetchone()
        return _row_to_message(row) if row else None
    finally:
        conn.close()


def drain_pending(to_id: str, db_path: str | None = None) -> list[Message]:
    """Return every still-pending message for `to_id`, oldest first.

    Excludes messages in the terminal `failed` state (attempts ≥
    MAX_DELIVERY_ATTEMPTS). Caller is expected to push each to the PTY
    and call `mark_injected` on success, or `record_delivery_failure`
    on failure. Index `idx_agent_messages_drain` covers this query.
    """
    conn = open_db(db_path or _default_db_path())
    try:
        rows = conn.execute(
            "SELECT * FROM agent_messages "
            "WHERE to_id = ? "
            "AND injected_at IS NULL "
            "AND delivery_state != ? "
            "ORDER BY ts ASC",
            (to_id, STATE_FAILED),
        ).fetchall()
        return [_row_to_message(r) for r in rows]
    finally:
        conn.close()


def list_inbox(
    to_id: str,
    *,
    unread: bool = False,
    limit: int = 50,
    db_path: str | None = None,
) -> list[Message]:
    """Inbox for one agent, newest first. `unread=True` filters to
    messages not yet pushed to a PTY (injected_at IS NULL)."""
    conn = open_db(db_path or _default_db_path())
    try:
        q = "SELECT * FROM agent_messages WHERE to_id = ?"
        params: list[Any] = [to_id]
        if unread:
            q += " AND injected_at IS NULL"
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(q, params).fetchall()
        return [_row_to_message(r) for r in rows]
    finally:
        conn.close()


def list_workspace_inbox(
    workspace: str,
    *,
    agent: str | None = None,
    unread: bool = False,
    limit: int = 50,
    db_path: str | None = None,
) -> list[Message]:
    """Inbox across all agents in a workspace, newest first. If `agent`
    is given, scopes to that agent within the workspace."""
    conn = open_db(db_path or _default_db_path())
    try:
        q = "SELECT * FROM agent_messages WHERE workspace = ?"
        params: list[Any] = [workspace]
        if agent:
            q += " AND to_id = ?"
            params.append(agent)
        if unread:
            q += " AND injected_at IS NULL"
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(q, params).fetchall()
        return [_row_to_message(r) for r in rows]
    finally:
        conn.close()


# ── Format rendering ────────────────────────────────────────────────


def _select_template(
    *,
    explicit: str | None,
    agent_config: dict | None,
    plugin_default: str | None,
) -> str:
    """Template resolution: per-message > per-agent > plugin > built-in."""
    if explicit:
        return explicit
    if agent_config and isinstance(agent_config.get("message_format"), str):
        return agent_config["message_format"]
    if plugin_default:
        return plugin_default
    return DEFAULT_FORMAT


def render_format(
    msg: Message | dict,
    *,
    format: str | None = None,
    agent_config: dict | None = None,
    plugin_default: str | None = None,
) -> str:
    """Render a message to the string written into the receiver's PTY.

    Supports `{from}`, `{to}`, `{body}`, `{id}`, `{in_reply_to}`,
    `{ts}` placeholders. Unknown placeholders are kept literal (so
    a typo in a template renders the brace literally rather than
    crashing).
    """
    if isinstance(msg, Message):
        env = {
            "id": msg.id,
            "from": msg.from_id,
            "to": msg.to_id,
            "body": msg.body,
            "in_reply_to": msg.in_reply_to or "",
            "ts": f"{msg.ts:.0f}",
        }
    else:
        env = {
            "id": msg.get("id") or "",
            "from": msg.get("from") or msg.get("from_id") or "",
            "to": msg.get("to") or msg.get("to_id") or "",
            "body": msg.get("body") or "",
            "in_reply_to": msg.get("in_reply_to") or "",
            "ts": f"{(msg.get('ts') or 0):.0f}",
        }

    template = _select_template(
        explicit=format,
        agent_config=agent_config,
        plugin_default=plugin_default,
    )

    # Safe formatting: leave unknown {placeholders} literal rather than
    # crashing on a typo'd template.
    class _SafeMap(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    try:
        return template.format_map(_SafeMap(env))
    except Exception as exc:
        logger.warning("render_format failed (template=%r): %s", template, exc)
        return DEFAULT_FORMAT.format_map(_SafeMap(env))
