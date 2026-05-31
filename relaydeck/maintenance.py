"""
Operator data maintenance — the "danger zone" wipes.

Clears messages + history tables wholesale (vs the age-based `prune_*`
helpers in db.py / automation_runs.py / model_invocations.py, which the
db.maintenance worker runs automatically). These are explicit,
operator-initiated, destructive resets: surfaced in the dashboard
Settings → Danger Zone and `relaydeck db wipe`, both gated behind a
confirmation.

Each scope maps to ONE table from a fixed whitelist (`SCOPES`), so the
table name interpolated into the DELETE is never user-controlled.
Deliberately EXCLUDED, even from the danger zone:
  - `audit_events` — the security record of who did what (incl. these
    wipes); wiping it would erase its own footprint.
  - `bus_events` / `bus_cursors` — durable event log; deleting out from
    under a subscriber breaks at-least-once replay.
  - `agents` / `tasks` / `auth_tokens` / vault — config + identity, not
    "history". Agents are removed via `relaydeck agent rm` (which purges
    that agent's history); secrets via the vault surface.
"""

from __future__ import annotations

import sqlite3
from typing import Any

# scope -> (table, human label). The table is the ONLY thing interpolated
# into SQL, and only ever from this dict — never from request input.
SCOPES: dict[str, tuple[str, str]] = {
    "messages": ("agent_messages", "Peer + chat messages"),
    "events": ("events", "Agent event log"),
    "usage": ("usage_records", "Token / cost usage records"),
    "invocations": ("model_invocations", "LLM call log"),
    "runs": ("automation_runs", "Automation run history"),
}

# Convenience group used by the CLI/UI "history" toggle (everything that
# isn't the message threads themselves).
HISTORY_SCOPES = ("events", "usage", "invocations", "runs")


def history_stats(db_path: str) -> dict[str, int]:
    """Row count per wipe-able scope, so the operator sees what a wipe
    would delete before confirming. A missing table reads as 0."""
    from relaydeck.db import open_db

    counts: dict[str, int] = {}
    conn = open_db(db_path)
    try:
        for scope, (table, _label) in SCOPES.items():
            try:
                counts[scope] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[scope] = 0
    finally:
        conn.close()
    return counts


def wipe(db_path: str, scopes: list[str]) -> dict[str, int]:
    """Delete every row in the chosen scopes' tables. Returns the deleted
    count per scope. Unknown scopes are ignored (the caller validates +
    surfaces that); a missing table is a no-op."""
    from relaydeck.db import open_db

    deleted: dict[str, int] = {}
    conn = open_db(db_path)
    try:
        for scope in scopes:
            if scope not in SCOPES:
                continue
            table = SCOPES[scope][0]
            try:
                cur = conn.execute(f"DELETE FROM {table}")
                deleted[scope] = cur.rowcount if cur.rowcount is not None else 0
            except sqlite3.OperationalError:
                deleted[scope] = 0
        conn.commit()
    finally:
        conn.close()
    return deleted


def resolve_scopes(*, messages: bool = False, history: bool = False,
                   extra: list[str] | None = None) -> list[str]:
    """Map the CLI/UI toggles to concrete scope keys (deduped, ordered)."""
    out: list[str] = []
    if messages:
        out.append("messages")
    if history:
        out.extend(HISTORY_SCOPES)
    for s in (extra or []):
        if s in SCOPES:
            out.append(s)
    seen: set[str] = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def scope_labels() -> dict[str, Any]:
    """{scope: label} for surfaces to render."""
    return {k: v[1] for k, v in SCOPES.items()}


def prepare_config_home_removal() -> list[str]:
    """Uninstall vendor hook integrations before deleting ~/.relaydeck."""
    from relaydeck.integrations import uninstall_hook_integrations_before_config_removal

    return uninstall_hook_integrations_before_config_removal()
