"""
Skills manager — the materialization sync driver + inventory rescan that
back the bundled `skills` plugin.

`sync_all` is the generic consumer of `[plugin.skills]`: for every loaded
plugin that declares skills it resolves the target workspaces, reads the
source SKILL.md (honoring an optional per-plugin content override), and
reconciles `runtime/skills/` via `relaydeck.skills` primitives (ownership
sidecars, idempotent writes, orphan cleanup). This is what lets messaging
and telegram drop their hand-rolled materialization.

Provider hooks (optional, duck-typed on the plugin instance):
  - `skill_target_workspaces(all_workspaces) -> Iterable[str] | None`
    Return the workspaces this plugin's skills should ship to. None →
    fall back to the default (workspaces whose `agent.toml` lists the
    plugin, for workspace-scoped plugins; empty otherwise). Telegram
    overrides this to return its routed workspaces.
  - `skill_content(skill_name, source_text) -> str | None`
    Override the materialized body. None → use the manifest file as-is.
    Messaging uses this for its `skill_content` setting override.

`rescan` refreshes the `skills_cache` mirror and emits `skills.changed` /
`skills.invalid` / `skills.removed` so the dashboard and other plugins
can react without re-walking the tree.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from relaydeck import skills as relaydeck_skills
from relaydeck import skills_cache
from relaydeck.db import open_db

from . import analysis, imports

logger = logging.getLogger("plugins.skills")

CATALOG_WORKSPACE = "_catalog"


def _workspace_plugins(config_home: Path, workspace: str) -> list[str]:
    toml_path = config_home / "workspaces" / workspace / "agent.toml"
    if not toml_path.exists():
        return []
    try:
        from relaydeck.config import load_config_file
        data = load_config_file(toml_path)
        plugins = data.get("workspace", {}).get("plugins", [])
        return [str(p) for p in plugins] if isinstance(plugins, list) else []
    except Exception:
        return []


def _all_workspaces(config_home: Path) -> list[str]:
    try:
        from relaydeck.config import load_workspace_registry
        return [w.name for w in load_workspace_registry(config_home)]
    except Exception:
        return []


def _agents_by_workspace(db_path: str | Path) -> dict[str, dict[str, Any]]:
    conn = open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT workspace, status, type FROM agents WHERE workspace IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        ws = row["workspace"]
        if not ws:
            continue
        item = out.setdefault(ws, {"total": 0, "running": 0, "types": {}})
        item["total"] += 1
        if row["status"] == "running":
            item["running"] += 1
        typ = row["type"] or "?"
        item["types"][typ] = item["types"].get(typ, 0) + 1
    return out


def _usage_by_workspace(db_path: str | Path) -> dict[str, dict[str, Any]]:
    conn = open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT a.workspace AS workspace, "
            "COUNT(DISTINCT u.agent_id) AS agents_with_usage, "
            "COALESCE(SUM(COALESCE(NULLIF(u.total_tokens,0), "
            "u.prompt_tokens + u.completion_tokens)),0) AS total_tokens, "
            "COALESCE(SUM(u.cost_usd),0) AS cost_usd, "
            "COALESCE(SUM(u.request_count),0) AS request_count, "
            "MAX(u.ts) AS last_usage_at "
            "FROM usage_records u JOIN agents a ON a.id = u.agent_id "
            "WHERE a.workspace IS NOT NULL "
            "GROUP BY a.workspace"
        ).fetchall()
    finally:
        conn.close()
    return {
        row["workspace"]: {
            "agents_with_usage": int(row["agents_with_usage"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "cost_usd": float(row["cost_usd"] or 0),
            "request_count": int(row["request_count"] or 0),
            "last_usage_at": float(row["last_usage_at"] or 0),
        }
        for row in rows
        if row["workspace"]
    }


def _skill_group_key(row: dict[str, Any]) -> str:
    if row.get("source_type") == relaydeck_skills.SOURCE_RUNTIME_PLUGIN:
        return f"runtime:{row.get('owner_plugin') or '?'}:{row.get('name')}"
    if row.get("source_type") == relaydeck_skills.SOURCE_WORKSPACE:
        return f"workspace:{row.get('workspace')}:{row.get('name')}"
    return f"id:{row.get('id')}"


def inventory_overview(db_path: str | Path) -> dict[str, Any]:
    """Fleet-level activation/footprint summary from the skills cache.

    "Active" here means relaydeck-injectable and valid: the skill is in
    a workspace injection directory and will be visible to agents on
    their next start. Running agents may need a restart to pick up edits.
    """
    rows = skills_cache.list_skills_cache(db_path)
    links = skills_cache.list_skill_links(db_path)
    link_by_ws_alias = {(l["workspace"], l["alias"]): l for l in links}
    agents = _agents_by_workspace(db_path)
    usage = _usage_by_workspace(db_path)
    exact_usage = skills_cache.skill_usage_rollup(db_path)
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        alias = Path(row.get("path") or "").name
        link = link_by_ws_alias.get((row.get("workspace"), alias))
        if link:
            row["import_link"] = link
        key = _skill_group_key(row)
        g = groups.setdefault(key, {
            "key": key,
            "name": row.get("name") or alias,
            "display_name": row.get("display_name") or row.get("name") or alias,
            "description": row.get("description") or "",
            "source_type": row.get("source_type"),
            "owner_plugin": row.get("owner_plugin"),
            "valid": True,
            "injectable": bool(row.get("injectable")),
            "token_estimate": 0,
            "body_chars": 0,
            "deployments": [],
            "workspaces": set(),
            "agent_total": 0,
            "agent_running": 0,
            "usage_workspaces": set(),
            "usage_tokens": 0,
            "usage_cost_usd": 0.0,
            "usage_requests": 0,
            "usage_agents": 0,
            "last_usage_at": 0.0,
            "exact_uses": 0,
            "exact_usage_tokens": 0,
            "exact_usage_cost_usd": 0.0,
            "exact_last_used_at": 0.0,
            "managed_import": False,
        })
        g["valid"] = bool(g["valid"] and row.get("valid"))
        g["token_estimate"] += int(row.get("token_estimate") or 0)
        g["body_chars"] += int(row.get("body_chars") or 0)
        if link:
            g["managed_import"] = True
        ws = row.get("workspace")
        if ws:
            g["workspaces"].add(ws)
            a = agents.get(ws, {})
            g["agent_total"] += int(a.get("total") or 0)
            g["agent_running"] += int(a.get("running") or 0)
            if ws not in g["usage_workspaces"]:
                g["usage_workspaces"].add(ws)
                u = usage.get(ws, {})
                g["usage_tokens"] += int(u.get("total_tokens") or 0)
                g["usage_cost_usd"] += float(u.get("cost_usd") or 0)
                g["usage_requests"] += int(u.get("request_count") or 0)
                g["usage_agents"] += int(u.get("agents_with_usage") or 0)
                g["last_usage_at"] = max(
                    float(g["last_usage_at"] or 0),
                    float(u.get("last_usage_at") or 0),
                )
            exact = exact_usage.get((ws, row.get("name") or alias))
            if exact:
                g["exact_uses"] += int(exact.get("uses") or 0)
                g["exact_usage_tokens"] += int(exact.get("total_tokens") or 0)
                g["exact_usage_cost_usd"] += float(exact.get("cost_usd") or 0)
                g["exact_last_used_at"] = max(
                    float(g["exact_last_used_at"] or 0),
                    float(exact.get("last_used_at") or 0),
                )
        g["deployments"].append(row)

    out_groups = []
    for g in groups.values():
        out_groups.append({
            **g,
            "workspaces": sorted(g["workspaces"]),
            "usage_workspaces": sorted(g["usage_workspaces"]),
            "deployment_count": len(g["deployments"]),
        })
    out_groups.sort(key=lambda g: (str(g["source_type"]), str(g["name"])))
    injectable = [r for r in rows if r.get("injectable")]
    active = [r for r in injectable if r.get("valid")]
    return {
        "summary": {
            "skills": len(rows),
            "unique": len(out_groups),
            "injectable": len(injectable),
            "active": len(active),
            "invalid": sum(1 for r in rows if not r.get("valid")),
            "token_estimate": sum(int(r.get("token_estimate") or 0) for r in active),
            "managed_imports": len(links),
            "usage_tokens_by_skill_exposure": sum(
                int(u.get("total_tokens") or 0) for u in usage.values()
            ),
            "exact_skill_uses": sum(int(g.get("exact_uses") or 0) for g in out_groups),
            "exact_skill_usage_tokens": sum(
                int(g.get("exact_usage_tokens") or 0) for g in out_groups
            ),
        },
        "groups": out_groups,
        "links": links,
        "agents_by_workspace": agents,
        "usage_by_workspace": usage,
        "activation_note": (
            "Active means valid + injectable; already-running harnesses pick up "
            "skill changes on restart. Exact usage comes from explicit "
            "skill_usage_events. Workspace LLM tokens counts agent usage in "
            "workspaces where a skill is deployed — context only, not per-skill proof."
        ),
    }


def _resolve_targets(
    inst: Any, plugin_name: str, config_home: Path, all_ws: list[str]
) -> list[str]:
    hook = getattr(inst, "skill_target_workspaces", None)
    if callable(hook):
        try:
            res = hook(list(all_ws))
            if res is not None:
                return [w for w in res if w in all_ws]
        except Exception as exc:
            logger.debug("skills: %s.skill_target_workspaces failed: %s", plugin_name, exc)
    # Default: a workspace-scoped plugin ships to the workspaces that
    # opted into it; a daemon-wide plugin must opt in via the hook.
    if getattr(inst, "workspace_scoped", False):
        return [ws for ws in all_ws if plugin_name in _workspace_plugins(config_home, ws)]
    return []


def _skill_content_override(inst: Any, skill_name: str, source_text: str) -> str:
    hook = getattr(inst, "skill_content", None)
    if callable(hook):
        try:
            override = hook(skill_name, source_text)
            if isinstance(override, str) and override.strip():
                return override
        except Exception as exc:
            logger.debug("skills: skill_content hook failed for %s: %s", skill_name, exc)
    return source_text


def _plugin_dir(entry: Any) -> Path:
    p = Path(entry.path)
    return p if p.is_dir() else p.parent


def sync_plugin(config_home: Path, entry: Any, all_ws: list[str]) -> dict[str, int]:
    """Reconcile one loaded plugin's declared skills. `entry` is a
    PluginRegistry PluginEntry."""
    manifest = getattr(entry, "manifest", None)
    declared = dict(getattr(manifest, "skills", {}) or {})
    if not declared:
        return {"written": 0, "unchanged": 0, "conflict": 0, "removed": 0}

    inst = entry.instance
    plugin_dir = _plugin_dir(entry)
    targets = _resolve_targets(inst, entry.name, config_home, all_ws)

    skills_map: dict[str, str] = {}
    for skill_name, rel in declared.items():
        src = plugin_dir / rel
        try:
            text = src.read_text()
        except OSError as exc:
            logger.warning("skills: %s declares %s but %s unreadable: %s",
                           entry.name, skill_name, src, exc)
            continue
        skills_map[skill_name] = _skill_content_override(inst, skill_name, text)

    return relaydeck_skills.sync_plugin_skills(
        config_home, entry.name, skills_map, targets, all_ws
    )


def sync_all(config_home: Path, *, registry: Any = None) -> dict[str, int]:
    """Materialize/reconcile every loaded plugin's declared skills across
    all registered workspaces. Idempotent; safe on any lifecycle event."""
    if registry is None:
        from relaydeck.plugin import get_registry
        registry = get_registry(config_home)
    all_ws = _all_workspaces(config_home)
    agg = {"written": 0, "unchanged": 0, "conflict": 0, "removed": 0}
    for entry in registry.all():
        try:
            rep = sync_plugin(config_home, entry, all_ws)
        except Exception:
            logger.exception("skills: sync failed for plugin %s", getattr(entry, "name", "?"))
            continue
        for k in agg:
            agg[k] += rep.get(k, 0)
    return agg


def remove_plugin_skills(config_home: Path, plugin_name: str) -> int:
    """Remove all relaydeck-managed skills owned by a plugin (called when a
    contributing plugin is unloaded/disabled). Works purely from the
    ownership sidecars, so it doesn't need the plugin's manifest."""
    return relaydeck_skills.remove_all_plugin_skills(
        config_home, plugin_name, _all_workspaces(config_home)
    )


# ── inventory rescan (skills_cache mirror + events) ──────────────────


def rescan(
    config_home: Path,
    db_path: str | Path,
    *,
    emit: Any = None,
    include_codex: bool = True,
    include_claude: bool = True,
) -> dict[str, Any]:
    """Refresh the skills_cache mirror from the filesystem and emit
    change events. Returns a summary dict.

    `emit` is an optional `emit(event_type, data)` callable (the plugin
    passes `host.events.emit`-style hook); when None, events are skipped.
    """
    prev = {r["id"]: r.get("hash", "") for r in skills_cache.list_skills_cache(db_path)}
    refs = relaydeck_skills.discover_all_skills(
        config_home, include_codex=include_codex, include_claude=include_claude
    )
    refs = [analysis.enrich_ref(ref) for ref in refs]
    seen_ids = set()
    changed = 0
    invalid = 0

    for ref in refs:
        seen_ids.add(ref.id)
        if ref.id not in prev or prev[ref.id] != ref.content_hash:
            changed += 1
            if emit:
                _safe_emit(emit, "skills.changed", _event_payload(ref))
        if not ref.valid and emit:
            invalid += 1
            _safe_emit(emit, "skills.invalid", _event_payload(ref))

    removed_ids = [sid for sid in prev if sid not in seen_ids]
    if emit:
        for sid in removed_ids:
            _safe_emit(emit, "skills.removed", {"id": sid})

    skills_cache.replace_skill_cache(db_path, refs)
    return {
        "total": len(refs),
        "changed": changed,
        "invalid": invalid,
        "removed": len(removed_ids),
    }


# ── operator import (link/copy/reference) ────────────────────────────


def _safe_skill_dest(config_home: Path, workspace: str, alias: str) -> Path:
    """Build the link destination and assert it stays directly under the
    workspace's `skills/` dir. Belt-and-suspenders on top of the
    component validation in the callers.

    Only the `skills/` root is resolved — NOT the leaf. The leaf is a
    skill alias we manage as a symlink (pointing outside the skills dir
    by design); resolving it would follow that symlink to its target and
    spuriously fail the containment check. Since `alias` is validated as
    a single path component, `dest.parent == root` is sufficient."""
    root = (config_home / "workspaces" / workspace / "skills").resolve()
    dest = root / alias
    if dest.parent != root:
        raise ValueError(f"refusing to operate outside workspace skills dir: {alias!r}")
    return dest


def _catalog_skill_dest(config_home: Path, alias: str) -> Path:
    relaydeck_skills.validate_path_component(alias, kind="alias")
    root = (config_home / "plugin-data" / "skills" / "catalog").resolve()
    dest = root / alias
    if dest.parent != root:
        raise ValueError(f"refusing to operate outside skill catalog: {alias!r}")
    return dest


def register_catalog_skill(
    config_home: Path,
    db_path: str | Path,
    target_path: str,
    alias: str,
    *,
    target_id: str | None = None,
    source_url: str | None = None,
    source_ref: str | None = None,
    source_subpath: str | None = None,
) -> dict[str, Any]:
    """Register a managed skill in the fleet catalog (not yet workspace-injected)."""
    relaydeck_skills.validate_path_component(alias, kind="alias")
    target = Path(target_path).expanduser().resolve()
    if not (target / "SKILL.md").is_file():
        raise ValueError(f"{target} has no SKILL.md")
    dest = _catalog_skill_dest(config_home, alias)
    if dest.exists() or dest.is_symlink():
        raise ValueError(f"catalog skill {alias!r} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(target)
    try:
        return skills_cache.create_skill_link(
            db_path,
            CATALOG_WORKSPACE,
            alias,
            str(target),
            mode="symlink",
            target_id=target_id,
            source_url=source_url,
            source_ref=source_ref,
            source_subpath=source_subpath,
            review_status="imported",
            review_summary="",
        )
    except Exception:
        if dest.is_symlink():
            dest.unlink()
        raise


def deploy_catalog_skill(
    config_home: Path,
    db_path: str | Path,
    alias: str,
    workspace: str,
    mode: str = "symlink",
) -> dict[str, Any]:
    """Inject a catalog skill into one workspace."""
    link = skills_cache.get_skill_link(db_path, CATALOG_WORKSPACE, alias)
    if link is None:
        raise ValueError(f"catalog skill {alias!r} not found")
    catalog_dest = _catalog_skill_dest(config_home, alias)
    return link_skill(
        config_home,
        db_path,
        workspace,
        str(catalog_dest.resolve()),
        alias,
        mode,
        target_id=link.get("target_id"),
        source_url=link.get("source_url"),
        source_ref=link.get("source_ref"),
        source_subpath=link.get("source_subpath"),
        link_status=str(link.get("review_status") or "imported"),
    )


def link_skill(
    config_home: Path,
    db_path: str | Path,
    workspace: str,
    target_path: str,
    alias: str,
    mode: str = "symlink",
    *,
    target_id: str | None = None,
    source_url: str | None = None,
    source_ref: str | None = None,
    source_subpath: str | None = None,
    link_status: str = "linked",
    link_summary: str = "",
) -> dict[str, Any]:
    """Import an external skill into a workspace. `mode`:
      - symlink  : workspaces/<ws>/skills/<alias> -> target (injected)
      - copy     : copy the dir in (injected, locally editable)
      - reference: record the link only, leave the filesystem untouched
                   (shown in inventory, NOT injected)
    Raises ValueError on bad input. Returns the created link row.
    """
    import shutil

    # Validate before touching the filesystem: the HTTP route passes
    # `workspace`/`alias` straight from request input, so an unsanitized
    # `../x` or absolute path could otherwise escape the workspace.
    relaydeck_skills.validate_path_component(workspace, kind="workspace")
    relaydeck_skills.validate_path_component(alias, kind="alias")

    target = Path(target_path).expanduser().resolve()
    if not (target / "SKILL.md").is_file():
        raise ValueError(f"{target} has no SKILL.md")
    dest = _safe_skill_dest(config_home, workspace, alias)
    if mode not in ("symlink", "copy", "reference"):
        raise ValueError(f"unknown mode {mode!r}")
    if mode != "reference":
        if dest.exists():
            raise ValueError(f"{dest} already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if mode == "symlink":
            dest.symlink_to(target)
        else:
            shutil.copytree(target, dest)
    try:
        return skills_cache.create_skill_link(
            db_path, workspace, alias, str(target), mode=mode,
            target_id=target_id,
            source_url=source_url, source_ref=source_ref,
            source_subpath=source_subpath, review_status=link_status,
            review_summary=link_summary,
        )
    except Exception:
        if mode != "reference":
            if dest.is_symlink():
                dest.unlink()
            elif dest.is_dir():
                shutil.rmtree(dest)
        raise


def unlink_skill(
    config_home: Path, db_path: str | Path, workspace: str, alias: str
) -> bool:
    """Remove a linked skill (symlink or copied dir) + its link record.
    Returns True if anything was removed."""
    import shutil

    relaydeck_skills.validate_path_component(workspace, kind="workspace")
    relaydeck_skills.validate_path_component(alias, kind="alias")
    # Catalog entries live under plugin-data/skills/catalog/<alias>, not in a
    # real workspace's skills/ dir — target the right path so removing a
    # managed skill doesn't orphan its catalog symlink.
    dest = (_catalog_skill_dest(config_home, alias)
            if workspace == CATALOG_WORKSPACE
            else _safe_skill_dest(config_home, workspace, alias))
    removed_fs = False
    if dest.is_symlink():
        dest.unlink()
        removed_fs = True
    elif dest.is_dir():
        shutil.rmtree(dest)
        removed_fs = True
    removed_db = skills_cache.delete_skill_link(db_path, workspace, alias)
    return removed_fs or removed_db


def fetch_git_skill(
    config_home: Path,
    source_url: str,
    *,
    source_ref: str | None = None,
    source_subpath: str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    return imports.fetch_git_skill(
        config_home,
        source_url,
        source_ref=source_ref,
        source_subpath=source_subpath,
        refresh=refresh,
    )


def read_skill_metadata(path: str | Path) -> dict[str, Any]:
    return imports.read_skill_metadata(path)


def import_git_skill(
    config_home: Path,
    db_path: str | Path,
    *,
    workspace: str,
    source_url: str,
    alias: str | None = None,
    mode: str = "symlink",
    source_ref: str | None = None,
    source_subpath: str | None = None,
    refresh: bool = False,
    require_valid: bool = True,
) -> dict[str, Any]:
    return imports.import_git_skill(
        config_home,
        db_path,
        source_url=source_url,
        workspace=workspace,
        source_ref=source_ref,
        source_subpath=source_subpath,
        refresh=refresh,
        alias=alias,
        mode=mode,
        require_valid=require_valid,
    )


def refresh_managed_imports(
    config_home: Path,
    db_path: str | Path,
    *,
    min_interval_s: float = 3600.0,
    force: bool = False,
) -> dict[str, Any]:
    return imports.refresh_managed_imports(
        config_home, db_path, min_interval_s=min_interval_s, force=force
    )


def resolve_import_source(
    config_home: Path,
    source_text: str,
    *,
    refresh: bool = False,
    skill_filter: str | None = None,
) -> dict[str, Any]:
    return imports.resolve_import_source(
        config_home, source_text, refresh=refresh, skill_filter=skill_filter
    )


def import_resolved_skills(
    config_home: Path,
    db_path: str | Path,
    *,
    resolved: dict[str, Any],
    selections: list[dict[str, Any]],
    workspace: str | None = None,
    deploy_to: str | list[str] | None = "all",
    mode: str = "symlink",
    require_valid: bool = True,
) -> dict[str, Any]:
    return imports.import_resolved_skills(
        config_home,
        db_path,
        resolved=resolved,
        selections=selections,
        workspace=workspace,
        deploy_to=deploy_to,
        mode=mode,
        require_valid=require_valid,
    )


def _event_payload(ref: relaydeck_skills.SkillRef) -> dict[str, Any]:
    return {
        "id": ref.id, "name": ref.name, "workspace": ref.workspace,
        "source_type": ref.source_type, "owner_plugin": ref.owner_plugin,
        "valid": ref.valid, "path": ref.path,
    }


def _safe_emit(emit: Any, event_type: str, data: dict[str, Any]) -> None:
    try:
        emit(event_type, data)
    except Exception as exc:
        logger.debug("skills: emit %s failed: %s", event_type, exc)
