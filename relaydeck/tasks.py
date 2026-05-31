"""
Plugin-owned task records — the lifecycle store for units of work a
plugin tracks (a GitHub issue being worked, an open PR awaiting review,
a CI failure assigned to an agent).

Deliberately separate from agent specs: `~/.relaydeck/agents/<id>.yaml`
is the source of truth for *agents*, and the `agents` table mirrors a
subset for queries. A task is not an agent — it's a piece of work that
may be *assigned to* an agent and *scoped to* a workspace. Jamming PR
metadata into agent YAML or generic agent columns would conflate the
two and rot fast.

The data layer lives here so the SDK host (`host.tasks`), the HTTP API
(dashboard querying), and plugins all share one set of helpers. Rows
are namespaced by `plugin` (the owner) and carry an `external_ref`
(upstream identity, e.g. a PR url) so a poller can upsert idempotently
without creating duplicates on every tick.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck.db import open_db

# Open-ended on purpose — plugins pick the vocabulary that fits their
# workflow. These are the conventional ones the dashboard understands.
STATUS_OPEN = "open"
STATUS_IN_PROGRESS = "in_progress"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"


@dataclass
class Task:
    id: str
    plugin: str
    kind: str = ""
    workspace: str | None = None
    agent_id: str | None = None
    external_ref: str | None = None
    title: str = ""
    status: str = STATUS_OPEN
    metadata: dict[str, Any] | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plugin": self.plugin,
            "kind": self.kind,
            "workspace": self.workspace,
            "agent_id": self.agent_id,
            "external_ref": self.external_ref,
            "title": self.title,
            "status": self.status,
            "metadata": self.metadata or {},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _default_db_path() -> str:
    return str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db")


def _short_id() -> str:
    return "task_" + uuid.uuid4().hex[:12]


def _row_to_task(row: sqlite3.Row) -> Task:
    raw_meta = row["metadata"]
    meta: dict[str, Any] | None
    if raw_meta:
        try:
            meta = json.loads(raw_meta)
        except (TypeError, ValueError):
            meta = None
    else:
        meta = None
    return Task(
        id=row["id"],
        plugin=row["plugin"],
        kind=row["kind"] or "",
        workspace=row["workspace"],
        agent_id=row["agent_id"],
        external_ref=row["external_ref"],
        title=row["title"] or "",
        status=row["status"] or STATUS_OPEN,
        metadata=meta,
        created_at=float(row["created_at"] or 0.0),
        updated_at=float(row["updated_at"] or 0.0),
    )


def create_task(
    plugin: str,
    *,
    kind: str = "",
    workspace: str | None = None,
    agent_id: str | None = None,
    external_ref: str | None = None,
    title: str = "",
    status: str = STATUS_OPEN,
    metadata: dict[str, Any] | None = None,
    db_path: str | None = None,
) -> Task:
    """Insert a new task and return it. `plugin` is the owning plugin's
    name — every read helper scopes by it so plugins can't see each
    other's records by accident."""
    now = time.time()
    task = Task(
        id=_short_id(), plugin=plugin, kind=kind, workspace=workspace,
        agent_id=agent_id, external_ref=external_ref, title=title,
        status=status, metadata=metadata, created_at=now, updated_at=now,
    )
    conn = open_db(db_path or _default_db_path())
    try:
        conn.execute(
            """INSERT INTO tasks
               (id, plugin, workspace, agent_id, kind, external_ref,
                title, status, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.id, plugin, workspace, agent_id, kind, external_ref,
                title, status,
                json.dumps(metadata) if metadata else None,
                now, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return task


def get_task(task_id: str, *, db_path: str | None = None) -> Task | None:
    conn = open_db(db_path or _default_db_path())
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None
    finally:
        conn.close()


def find_by_external_ref(
    plugin: str, external_ref: str, *, db_path: str | None = None
) -> Task | None:
    """Look up a task by its upstream identity within a plugin's
    namespace. The idempotency primitive: a poller calls this before
    creating a task so re-seeing the same PR/issue updates rather than
    duplicates."""
    conn = open_db(db_path or _default_db_path())
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE plugin = ? AND external_ref = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (plugin, external_ref),
        ).fetchone()
        return _row_to_task(row) if row else None
    finally:
        conn.close()


def list_tasks(
    *,
    plugin: str | None = None,
    workspace: str | None = None,
    agent_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    db_path: str | None = None,
) -> list[Task]:
    """List tasks newest-updated first, filtered by any combination of
    plugin / workspace / agent / status."""
    clauses: list[str] = []
    params: list[Any] = []
    if plugin is not None:
        clauses.append("plugin = ?")
        params.append(plugin)
    if workspace is not None:
        clauses.append("workspace = ?")
        params.append(workspace)
    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = open_db(db_path or _default_db_path())
    try:
        rows = conn.execute(
            f"SELECT * FROM tasks{where} ORDER BY updated_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [_row_to_task(r) for r in rows]
    finally:
        conn.close()


# Sentinel so update_task can distinguish "leave unchanged" from
# "set to None" for nullable columns.
_UNSET: Any = object()


def update_task(
    task_id: str,
    *,
    status: str | None = None,
    agent_id: Any = _UNSET,
    workspace: Any = _UNSET,
    title: str | None = None,
    kind: str | None = None,
    metadata: dict[str, Any] | None = None,
    merge_metadata: bool = True,
    db_path: str | None = None,
) -> Task | None:
    """Partial update; only provided fields change. `updated_at` always
    bumps. Returns the updated task, or None if it doesn't exist.

    `metadata` merges into the existing blob by default (so a caller can
    set one key without clobbering others); pass `merge_metadata=False`
    to replace. `agent_id`/`workspace` use a sentinel so passing `None`
    explicitly clears them (vs. omitting to leave unchanged)."""
    existing = get_task(task_id, db_path=db_path)
    if existing is None:
        return None

    sets: list[str] = ["updated_at = ?"]
    params: list[Any] = [time.time()]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if kind is not None:
        sets.append("kind = ?")
        params.append(kind)
    if agent_id is not _UNSET:
        sets.append("agent_id = ?")
        params.append(agent_id)
    if workspace is not _UNSET:
        sets.append("workspace = ?")
        params.append(workspace)
    if metadata is not None:
        if merge_metadata and existing.metadata:
            merged = dict(existing.metadata)
            merged.update(metadata)
        else:
            merged = dict(metadata)
        sets.append("metadata = ?")
        params.append(json.dumps(merged))
    params.append(task_id)

    conn = open_db(db_path or _default_db_path())
    try:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()
    return get_task(task_id, db_path=db_path)


def upsert_by_external_ref(
    plugin: str,
    external_ref: str,
    *,
    kind: str = "",
    workspace: str | None = None,
    agent_id: Any = _UNSET,
    title: str | None = None,
    status: str | None = None,
    metadata: dict[str, Any] | None = None,
    db_path: str | None = None,
) -> Task:
    """Create-or-update keyed on `external_ref` within the plugin's
    namespace. The poller idempotency helper: first sighting creates,
    later sightings update in place. Returns the resulting task."""
    found = find_by_external_ref(plugin, external_ref, db_path=db_path)
    if found is None:
        return create_task(
            plugin,
            kind=kind,
            workspace=workspace,
            agent_id=None if agent_id is _UNSET else agent_id,
            external_ref=external_ref,
            title=title or "",
            status=status or STATUS_OPEN,
            metadata=metadata,
            db_path=db_path,
        )
    updated = update_task(
        found.id,
        status=status,
        agent_id=agent_id,
        workspace=workspace if workspace is not None else _UNSET,
        title=title,
        kind=kind or None,
        metadata=metadata,
        db_path=db_path,
    )
    return updated or found


def delete_task(task_id: str, *, db_path: str | None = None) -> bool:
    conn = open_db(db_path or _default_db_path())
    try:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()
