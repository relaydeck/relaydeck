"""
Audit log.

Append-only record of sensitive operations: who did what, when, and
with which token. Backs compliance + post-incident review. Stored
in the `audit_events` SQLite table (additive migration in
`relaydeck/db.py`); read by `relaydeck audit tail|search|prune` and
`GET /api/audit`.

## Emission surface

Route handlers call `record(action, target=..., payload=...,
identity=request.state.identity)`. Helpers are also exposed for
non-HTTP callers (e.g. the boot path emitting `auth.token.issued`
when `relaydeck auth issue` mints a token). Failures are logged and
swallowed — the audit log is best-effort, never on the critical
path of an operation. The alternative ("if I can't audit, refuse the
op") is plausible but operationally worse; the table is local SQLite,
so the only failure mode is "disk is full or corrupt" and at that
point the daemon has bigger problems.

## Schema notes

  - `id`            — `aud_<urlsafe>` so log lines are searchable
  - `ts`            — float seconds since epoch
  - `token_id`      — auth_tokens.id or NULL (file-root has no row)
  - `token_label`   — duplicated for fast tail queries (no JOIN)
  - `action`        — dotted action key, e.g. `agent.start`,
                      `vault.write`, `plugin.disabled`
  - `target`        — opaque identifier of the resource acted on
  - `payload`       — JSON-encoded extra metadata (request body
                      summary, before/after diff, etc.)
  - `source_ip`     — request.client.host or None

## Retention

The daemon never auto-prunes. Operators run `relaydeck audit prune
--before <date>` explicitly. The expectation is that operators
either rotate the table to cold storage on their own schedule, or
keep the full history (the schema is cheap — events are dozens of
bytes).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Optional

from relaydeck.auth_tokens import AuthIdentity, file_root_identity
from relaydeck.db import open_db

logger = logging.getLogger(__name__)


# ── Action vocabulary ───────────────────────────────────────────────


# These constants are the audit action names. Keep them dotted +
# kebab-stable so log search continues to work after we add new
# actions. New actions live alongside the route that emits them and
# go through `record(...)`.

class actions:  # noqa: N801 — used as a namespace
    AGENT_CREATE = "agent.create"
    AGENT_START = "agent.start"
    AGENT_STOP = "agent.stop"
    AGENT_REMOVE = "agent.remove"
    AGENT_UPDATE = "agent.update"

    VAULT_READ = "vault.read"
    VAULT_WRITE = "vault.write"
    VAULT_DELETE = "vault.delete"
    VAULT_ROTATE_KEY = "vault.rotate-key"

    TOKEN_ISSUE = "auth.token.issued"
    TOKEN_REVOKE = "auth.token.revoked"
    TOKEN_ROTATE = "auth.token.rotated"

    PLUGIN_ENABLE = "plugin.enabled"
    PLUGIN_DISABLE = "plugin.disabled"
    PLUGIN_SETTINGS_CHANGE = "plugin.settings.changed"

    WORKSPACE_ADD = "workspace.added"
    WORKSPACE_REMOVE = "workspace.removed"

    DAEMON_RESTART = "daemon.restart"

    DATA_WIPE = "data.wipe"


# ── Recording ───────────────────────────────────────────────────────


def record(
    action: str,
    *,
    target: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    identity: Optional[AuthIdentity] = None,
    source_ip: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[str]:
    """Append one audit event. Returns the event id on success or
    None if the write failed (logged + swallowed).

    `identity` defaults to the implicit file-root identity for code
    paths that mutate state outside of an HTTP request (boot-time
    workspace registration, migrations). Pass an explicit
    `AuthIdentity` when you have one — that's the whole point of
    the log.
    """
    if identity is None:
        identity = file_root_identity()
    ev_id = "aud_" + secrets.token_urlsafe(12)
    try:
        payload_json = json.dumps(payload or {}, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("audit: payload serialization failed for %s: %s", action, exc)
        payload_json = "{}"
    conn = open_db(db_path) if db_path else _default_conn()
    try:
        conn.execute(
            """INSERT INTO audit_events
                   (id, ts, token_id, token_label, action, target, payload, source_ip)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ev_id, time.time(),
                # File-root has no DB row — store NULL token_id with the
                # sentinel label so queries on `token_id IS NULL` find
                # the file-root events and `token_label = 'root-file'`
                # surfaces them in the UI.
                None if identity.is_file_root else identity.token_id,
                identity.label,
                action, target, payload_json, source_ip,
            ),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("audit: insert failed for %s: %s", action, exc)
        return None
    finally:
        conn.close()
    return ev_id


# ── Listing / search ────────────────────────────────────────────────


def list_events(
    *,
    since: Optional[float] = None,
    action: Optional[str] = None,
    token_id: Optional[str] = None,
    target: Optional[str] = None,
    limit: int = 100,
    db_path: Optional[str] = None,
) -> list[dict]:
    """Read audit_events with optional filters. Most-recent-first.

    Limit defaults to 100 — `relaydeck audit tail` shows that many.
    `relaydeck audit search` uses higher limits as needed."""
    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if action:
        where.append("action = ?")
        params.append(action)
    if token_id:
        where.append("token_id = ?")
        params.append(token_id)
    if target:
        where.append("target = ?")
        params.append(target)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT id, ts, token_id, token_label, action, target, payload, source_ip "
        "FROM audit_events" + clause + " ORDER BY ts DESC LIMIT ?"
    )
    params.append(int(limit))
    conn = open_db(db_path) if db_path else _default_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        except (TypeError, ValueError):
            d["payload"] = {}
        out.append(d)
    return out


# ── Retention ───────────────────────────────────────────────────────


def prune(*, before: float, db_path: Optional[str] = None) -> int:
    """Delete audit_events older than `before` (Unix timestamp).
    Returns the number of rows deleted. Operator-driven; no
    automatic pruning."""
    conn = open_db(db_path) if db_path else _default_conn()
    try:
        cursor = conn.execute("DELETE FROM audit_events WHERE ts < ?", (before,))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


# ── Helpers ─────────────────────────────────────────────────────────


def _default_conn():
    from pathlib import Path
    return open_db(str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db"))
