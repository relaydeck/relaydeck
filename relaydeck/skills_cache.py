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
    "size", "mtime", "body_chars", "token_estimate", "risk_level",
    "risk_flags_json", "last_scanned_at", "updated_at",
)


# ── skills_cache ─────────────────────────────────────────────────────


def _row_to_dict(row: Any) -> dict[str, Any]:
    d = {k: row[k] for k in row.keys()}
    d["valid"] = bool(d.get("valid"))
    d["errors"] = json.loads(d.pop("errors_json") or "[]")
    d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
    d["frontmatter"] = json.loads(d.pop("frontmatter_json") or "{}")
    d["risk_flags"] = json.loads(d.pop("risk_flags_json", None) or "[]")
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
                ref.body_chars, ref.token_estimate, ref.risk_level,
                json.dumps(ref.risk_flags),
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
                    ref.size, ref.mtime, ref.body_chars, ref.token_estimate,
                    ref.risk_level, json.dumps(ref.risk_flags), now, now,
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
    source_url: str | None = None,
    source_ref: str | None = None,
    source_subpath: str | None = None,
    review_status: str = "not-reviewed",
    review_summary: str = "",
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
            "(id, workspace, alias, target_path, target_id, mode, enabled, "
            "source_url, source_ref, source_subpath, review_status, "
            "review_summary, reviewed_at, last_checked_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (link_id, workspace, alias, target_path, target_id, mode,
             1 if enabled else 0, source_url, source_ref, source_subpath,
             review_status, review_summary, None, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": link_id, "workspace": workspace, "alias": alias,
        "target_path": target_path, "target_id": target_id, "mode": mode,
        "enabled": enabled, "source_url": source_url, "source_ref": source_ref,
        "source_subpath": source_subpath, "review_status": review_status,
        "review_summary": review_summary, "reviewed_at": None,
        "last_checked_at": now, "created_at": now,
    }


def update_skill_link_review(
    db_path: str | Path,
    workspace: str,
    alias: str,
    *,
    status: str,
    summary: str = "",
) -> bool:
    now = time.time()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "UPDATE skill_links SET review_status = ?, review_summary = ?, "
            "reviewed_at = ?, last_checked_at = ? "
            "WHERE workspace = ? AND alias = ?",
            (status, summary, now, now, workspace, alias),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def update_skill_link_state(
    db_path: str | Path,
    workspace: str,
    alias: str,
    *,
    target_id: str | None = None,
    review_status: str | None = None,
    review_summary: str | None = None,
    last_checked_at: float | None = None,
) -> bool:
    """Update mutable managed-import state without changing link identity."""
    sets: list[str] = []
    params: list[Any] = []
    if target_id is not None:
        sets.append("target_id = ?")
        params.append(target_id)
    if review_status is not None:
        sets.append("review_status = ?")
        params.append(review_status)
    if review_summary is not None:
        sets.append("review_summary = ?")
        params.append(review_summary)
    sets.append("last_checked_at = ?")
    params.append(last_checked_at if last_checked_at is not None else time.time())
    params.extend([workspace, alias])
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            f"UPDATE skill_links SET {', '.join(sets)} "
            "WHERE workspace = ? AND alias = ?",
            params,
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


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


# ── skill_usage_events ───────────────────────────────────────────────


def _usage_row(row: Any) -> dict[str, Any]:
    d = {k: row[k] for k in row.keys()}
    d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
    return d


def record_skill_usage(
    db_path: str | Path,
    *,
    skill_name: str,
    workspace: str | None = None,
    agent_id: str | None = None,
    skill_id: str | None = None,
    source: str = "manual",
    event_type: str = "used",
    confidence: float = 1.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost_usd: float | None = None,
    metadata: dict[str, Any] | None = None,
    ts: float | None = None,
) -> dict[str, Any]:
    event_id = "sku_" + uuid.uuid4().hex[:12]
    now = time.time() if ts is None else float(ts)
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO skill_usage_events "
            "(id, ts, skill_id, skill_name, workspace, agent_id, source, "
            "event_type, confidence, prompt_tokens, completion_tokens, "
            "total_tokens, cost_usd, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id, now, skill_id, skill_name, workspace, agent_id,
                source, event_type, float(confidence), int(prompt_tokens),
                int(completion_tokens), int(total_tokens), cost_usd,
                json.dumps(metadata or {}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": event_id,
        "ts": now,
        "skill_id": skill_id,
        "skill_name": skill_name,
        "workspace": workspace,
        "agent_id": agent_id,
        "source": source,
        "event_type": event_type,
        "confidence": float(confidence),
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
        "cost_usd": cost_usd,
        "metadata": metadata or {},
    }


def list_skill_usage(
    db_path: str | Path,
    *,
    workspace: str | None = None,
    skill_name: str | None = None,
    agent_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if workspace is not None:
        where.append("workspace = ?")
        params.append(workspace)
    if skill_name is not None:
        where.append("skill_name = ?")
        params.append(skill_name)
    if agent_id is not None:
        where.append("agent_id = ?")
        params.append(agent_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(int(limit), 500)))
    conn = open_db(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM skill_usage_events{clause} "
            "ORDER BY ts DESC LIMIT ?",
            params,
        ).fetchall()
        return [_usage_row(r) for r in rows]
    finally:
        conn.close()


def skill_usage_rollup(db_path: str | Path) -> dict[tuple[str | None, str], dict[str, Any]]:
    conn = open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT workspace, skill_name, COUNT(*) AS uses, "
            "COALESCE(SUM(COALESCE(NULLIF(total_tokens,0), "
            "prompt_tokens + completion_tokens)),0) AS total_tokens, "
            "COALESCE(SUM(cost_usd),0) AS cost_usd, "
            "MAX(ts) AS last_used_at "
            "FROM skill_usage_events GROUP BY workspace, skill_name"
        ).fetchall()
    finally:
        conn.close()
    return {
        (row["workspace"], row["skill_name"]): {
            "uses": int(row["uses"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "cost_usd": float(row["cost_usd"] or 0),
            "last_used_at": float(row["last_used_at"] or 0),
        }
        for row in rows
    }
