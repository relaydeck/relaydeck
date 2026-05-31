"""
relaydeck/skills_cache.py — the SQLite mirror of the skills inventory.

The filesystem (SKILL.md + ownership sidecars) and upstream tools
(codex) are the source of truth for skill *content*; this module is a
queryable cache so the dashboard Skills lens, `relaydeck skills list`, and the
rescan worker don't re-walk the tree on every request. `skill_links` is
the one piece relaydeck genuinely owns — operator-created symlink/copy/
reference imports of an external skill into a workspace.

Pure data layer over `relaydeck.db.open_db`; no plugin/daemon coupling.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from relaydeck.db import open_db
from relaydeck.skills import SkillRef

_CACHE_COLS = (
    "id", "name", "display_name", "description", "source_type",
    "owner_plugin", "workspace", "path", "realpath", "symlink_target",
    "hash", "valid", "errors_json", "warnings_json", "frontmatter_json",
    "size", "mtime", "last_scanned_at", "updated_at",
)


# ── skills_cache ─────────────────────────────────────────────────────


def _row_to_dict(row: Any) -> dict[str, Any]:
    d = {k: row[k] for k in row.keys()}
    d["valid"] = bool(d.get("valid"))
    d["errors"] = json.loads(d.pop("errors_json") or "[]")
    d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
    d["frontmatter"] = json.loads(d.pop("frontmatter_json") or "{}")
    d["injectable"] = d.get("source_type") in (
        "workspace", "runtime-plugin"
    )
    return d


def upsert_skill_cache(db_path: str | Path, ref: SkillRef) -> None:
    """Insert or replace one cache row from a discovered SkillRef."""
    now = time.time()
    conn = open_db(db_path)
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO skills_cache "
            f"({', '.join(_CACHE_COLS)}) "
            f"VALUES ({', '.join('?' for _ in _CACHE_COLS)})",
            (
                ref.id, ref.name, ref.display_name or ref.name,
                ref.description, ref.source_type, ref.owner_plugin,
                ref.workspace, ref.path, ref.realpath, ref.symlink_target,
                ref.content_hash, 1 if ref.valid else 0,
                json.dumps(ref.errors), json.dumps(ref.warnings),
                json.dumps(ref.frontmatter), ref.size, ref.mtime,
                now, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def replace_skill_cache(db_path: str | Path, refs: list[SkillRef]) -> int:
    """Reconcile the cache to exactly `refs`: upsert each, then drop any
    row whose id is no longer present. Returns the number of rows pruned.
    This is what the rescan worker calls on each full sweep."""
    now = time.time()
    seen = [r.id for r in refs]
    conn = open_db(db_path)
    try:
        for ref in refs:
            conn.execute(
                f"INSERT OR REPLACE INTO skills_cache "
                f"({', '.join(_CACHE_COLS)}) "
                f"VALUES ({', '.join('?' for _ in _CACHE_COLS)})",
                (
                    ref.id, ref.name, ref.display_name or ref.name,
                    ref.description, ref.source_type, ref.owner_plugin,
                    ref.workspace, ref.path, ref.realpath,
                    ref.symlink_target, ref.content_hash,
                    1 if ref.valid else 0, json.dumps(ref.errors),
                    json.dumps(ref.warnings), json.dumps(ref.frontmatter),
                    ref.size, ref.mtime, now, now,
                ),
            )
        if seen:
            placeholders = ", ".join("?" for _ in seen)
            cur = conn.execute(
                f"DELETE FROM skills_cache WHERE id NOT IN ({placeholders})",
                seen,
            )
        else:
            cur = conn.execute("DELETE FROM skills_cache")
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def list_skills_cache(
    db_path: str | Path,
    *,
    workspace: str | None = None,
    source_type: str | None = None,
    owner_plugin: str | None = None,
    valid: bool | None = None,
) -> list[dict[str, Any]]:
    """Query the cache with optional filters, newest-updated first."""
    where: list[str] = []
    params: list[Any] = []
    if workspace is not None:
        where.append("workspace = ?")
        params.append(workspace)
    if source_type is not None:
        where.append("source_type = ?")
        params.append(source_type)
    if owner_plugin is not None:
        where.append("owner_plugin = ?")
        params.append(owner_plugin)
    if valid is not None:
        where.append("valid = ?")
        params.append(1 if valid else 0)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    conn = open_db(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM skills_cache{clause} "
            f"ORDER BY source_type, name",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_skill_cache(db_path: str | Path, skill_id: str) -> dict[str, Any] | None:
    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM skills_cache WHERE id = ?", (skill_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


# ── skill_links ──────────────────────────────────────────────────────


def _link_row(row: Any) -> dict[str, Any]:
    d = {k: row[k] for k in row.keys()}
    d["enabled"] = bool(d.get("enabled"))
    return d


def create_skill_link(
    db_path: str | Path,
    workspace: str,
    alias: str,
    target_path: str,
    *,
    target_id: str | None = None,
    mode: str = "symlink",
    enabled: bool = True,
) -> dict[str, Any]:
    """Record an operator import (symlink | copy | reference). Raises
    ValueError if (workspace, alias) is already linked."""
    link_id = "skl_" + uuid.uuid4().hex[:12]
    now = time.time()
    conn = open_db(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM skill_links WHERE workspace = ? AND alias = ?",
            (workspace, alias),
        ).fetchone()
        if existing:
            raise ValueError(f"skill link {workspace}/{alias} already exists")
        conn.execute(
            "INSERT INTO skill_links "
            "(id, workspace, alias, target_path, target_id, mode, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (link_id, workspace, alias, target_path, target_id, mode,
             1 if enabled else 0, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": link_id, "workspace": workspace, "alias": alias,
        "target_path": target_path, "target_id": target_id, "mode": mode,
        "enabled": enabled, "created_at": now,
    }


def list_skill_links(
    db_path: str | Path, workspace: str | None = None
) -> list[dict[str, Any]]:
    conn = open_db(db_path)
    try:
        if workspace is not None:
            rows = conn.execute(
                "SELECT * FROM skill_links WHERE workspace = ? ORDER BY alias",
                (workspace,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM skill_links ORDER BY workspace, alias"
            ).fetchall()
        return [_link_row(r) for r in rows]
    finally:
        conn.close()


def get_skill_link(
    db_path: str | Path, workspace: str, alias: str
) -> dict[str, Any] | None:
    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM skill_links WHERE workspace = ? AND alias = ?",
            (workspace, alias),
        ).fetchone()
        return _link_row(row) if row else None
    finally:
        conn.close()


def delete_skill_link(db_path: str | Path, workspace: str, alias: str) -> bool:
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM skill_links WHERE workspace = ? AND alias = ?",
            (workspace, alias),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()
