"""
Automation run history — the durable audit trail of "what fired, when,
what it did, did it work" for relaydeck's automations.

This is the persisted execution log that workers deliberately don't keep.
A `Worker` is a live thread (see `relaydeck/workers.py`) — it has a tick count
and a log ring buffer, but nothing survives a daemon restart. A loop
agent fires actions and emits events, but those events scroll away. The
missing piece is a record an operator can come back to tomorrow and ask:
*did the 9am health check run? what did it cost? which tick failed?*

Run records answer that. A run is the EXECUTION record of one trigger
firing (one loop tick, one scheduled fire). It is intentionally distinct
from a `tasks` row (`relaydeck/tasks.py`): a task is plugin-owned *domain*
work (a PR being reviewed, an issue being triaged); a run is the
*execution* of the automation that may have created or touched a task.
Conflating the two would give us two parallel run-tracking surfaces.

`automation_id` is the producer's identity — a loop agent's id today,
any future scheduled-automation id later. The data layer lives here so
the CLI (`relaydeck automation runs`), a future HTTP/dashboard surface, and
the loop agent all share one set of helpers, mirroring `relaydeck/tasks.py`.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck.db import open_db

# Terminal statuses an automation run can settle into. `running` is the
# only non-terminal state; a run left `running` after a daemon crash is a
# stale row an operator can read as "started but never finished".
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"  # some actions succeeded, some failed


@dataclass
class AutomationRun:
    id: str
    automation_id: str
    automation_type: str = ""
    workspace: str | None = None
    trigger_type: str = ""
    trigger_event_id: str | None = None
    status: str = STATUS_RUNNING
    started_at: float = 0.0
    finished_at: float | None = None
    duration_ms: int | None = None
    action_count: int = 0
    error_count: int = 0
    error: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "automation_id": self.automation_id,
            "automation_type": self.automation_type,
            "workspace": self.workspace,
            "trigger_type": self.trigger_type,
            "trigger_event_id": self.trigger_event_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "action_count": self.action_count,
            "error_count": self.error_count,
            "error": self.error,
            "detail": self.detail,
        }


def _default_db_path() -> str:
    return str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db")


def _short_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


def _row_to_run(row: sqlite3.Row) -> AutomationRun:
    return AutomationRun(
        id=row["id"],
        automation_id=row["automation_id"],
        automation_type=row["automation_type"] or "",
        workspace=row["workspace"],
        trigger_type=row["trigger_type"] or "",
        trigger_event_id=row["trigger_event_id"],
        status=row["status"] or STATUS_RUNNING,
        started_at=float(row["started_at"] or 0.0),
        finished_at=(float(row["finished_at"]) if row["finished_at"] is not None else None),
        duration_ms=(int(row["duration_ms"]) if row["duration_ms"] is not None else None),
        action_count=int(row["action_count"] or 0),
        error_count=int(row["error_count"] or 0),
        error=row["error"],
        detail=row["detail"],
    )


def start_run(
    automation_id: str,
    *,
    automation_type: str = "",
    workspace: str | None = None,
    trigger_type: str = "",
    trigger_event_id: str | None = None,
    detail: str | None = None,
    db_path: str | None = None,
) -> AutomationRun:
    """Open a run in `running` state and return it. Pair with
    `finish_run` once the actions complete. Keeping start/finish split
    means a crash mid-execution leaves a visible `running` row rather
    than dropping the attempt silently."""
    now = time.time()
    run = AutomationRun(
        id=_short_id(),
        automation_id=automation_id,
        automation_type=automation_type,
        workspace=workspace,
        trigger_type=trigger_type,
        trigger_event_id=trigger_event_id,
        status=STATUS_RUNNING,
        started_at=now,
        detail=detail,
    )
    conn = open_db(db_path or _default_db_path())
    try:
        conn.execute(
            """INSERT INTO automation_runs
               (id, automation_id, automation_type, workspace, trigger_type,
                trigger_event_id, status, started_at, action_count,
                error_count, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.id, automation_id, automation_type, workspace, trigger_type,
                trigger_event_id, STATUS_RUNNING, now, 0, 0, detail,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return run


def finish_run(
    run_id: str,
    *,
    status: str,
    action_count: int = 0,
    error_count: int = 0,
    error: str | None = None,
    detail: str | None = None,
    db_path: str | None = None,
) -> AutomationRun | None:
    """Settle a run into a terminal status, stamping `finished_at` and
    computing `duration_ms` from the recorded `started_at`. Returns the
    updated run, or None if the id is unknown."""
    conn = open_db(db_path or _default_db_path())
    try:
        row = conn.execute(
            "SELECT started_at FROM automation_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        now = time.time()
        started = float(row["started_at"] or now)
        duration_ms = int(max(0.0, now - started) * 1000)
        sets = [
            "status = ?", "finished_at = ?", "duration_ms = ?",
            "action_count = ?", "error_count = ?", "error = ?",
        ]
        params: list[Any] = [status, now, duration_ms, action_count, error_count, error]
        if detail is not None:
            sets.append("detail = ?")
            params.append(detail)
        params.append(run_id)
        conn.execute(
            f"UPDATE automation_runs SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
    finally:
        conn.close()
    return get_run(run_id, db_path=db_path)


def get_run(run_id: str, *, db_path: str | None = None) -> AutomationRun | None:
    conn = open_db(db_path or _default_db_path())
    try:
        row = conn.execute(
            "SELECT * FROM automation_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _row_to_run(row) if row else None
    finally:
        conn.close()


def list_runs(
    *,
    automation_id: str | None = None,
    status: str | None = None,
    workspace: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> list[AutomationRun]:
    """List runs newest-first, filtered by any combination of
    automation / status / workspace."""
    clauses: list[str] = []
    params: list[Any] = []
    if automation_id is not None:
        clauses.append("automation_id = ?")
        params.append(automation_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if workspace is not None:
        clauses.append("workspace = ?")
        params.append(workspace)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = open_db(db_path or _default_db_path())
    try:
        rows = conn.execute(
            f"SELECT * FROM automation_runs{where} "
            f"ORDER BY started_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [_row_to_run(r) for r in rows]
    finally:
        conn.close()


def list_automation_ids(*, db_path: str | None = None) -> list[dict[str, Any]]:
    """One row per automation that has ever run: id, type, run count, and
    the latest run's status/started_at. Powers `relaydeck automation list`
    without loading every run into memory."""
    conn = open_db(db_path or _default_db_path())
    try:
        rows = conn.execute(
            """SELECT automation_id,
                      COUNT(*)             AS runs,
                      MAX(started_at)      AS last_started_at
               FROM automation_runs
               GROUP BY automation_id
               ORDER BY last_started_at DESC"""
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            last = conn.execute(
                "SELECT automation_type, workspace, status, started_at, "
                "duration_ms, error_count "
                "FROM automation_runs WHERE automation_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (r["automation_id"],),
            ).fetchone()
            out.append({
                "automation_id": r["automation_id"],
                "automation_type": (last["automation_type"] if last else "") or "",
                "workspace": (last["workspace"] if last else None),
                "runs": int(r["runs"] or 0),
                "last_started_at": (float(r["last_started_at"]) if r["last_started_at"] is not None else None),
                "last_status": (last["status"] if last else None),
                "last_duration_ms": (int(last["duration_ms"]) if last and last["duration_ms"] is not None else None),
                "last_error_count": (int(last["error_count"]) if last and last["error_count"] is not None else 0),
            })
        return out
    finally:
        conn.close()


def prune_runs(
    *,
    older_than_days: float = 30.0,
    db_path: str | None = None,
) -> int:
    """Drop terminal (finished) runs older than `older_than_days`.
    `running` rows are never pruned by age — a stale `running` row is a
    breadcrumb for a crash, not garbage. Returns the number deleted."""
    cutoff = time.time() - (older_than_days * 86400.0)
    conn = open_db(db_path or _default_db_path())
    try:
        cur = conn.execute(
            "DELETE FROM automation_runs "
            "WHERE status != ? AND finished_at IS NOT NULL AND finished_at < ?",
            (STATUS_RUNNING, cutoff),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()
