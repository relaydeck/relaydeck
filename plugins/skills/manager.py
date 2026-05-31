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

logger = logging.getLogger("plugins.skills")


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


def link_skill(
    config_home: Path,
    db_path: str | Path,
    workspace: str,
    target_path: str,
    alias: str,
    mode: str = "symlink",
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
    return skills_cache.create_skill_link(
        db_path, workspace, alias, str(target), mode=mode
    )


def unlink_skill(
    config_home: Path, db_path: str | Path, workspace: str, alias: str
) -> bool:
    """Remove a linked skill (symlink or copied dir) + its link record.
    Returns True if anything was removed."""
    import shutil

    relaydeck_skills.validate_path_component(workspace, kind="workspace")
    relaydeck_skills.validate_path_component(alias, kind="alias")
    dest = _safe_skill_dest(config_home, workspace, alias)
    removed_fs = False
    if dest.is_symlink():
        dest.unlink()
        removed_fs = True
    elif dest.is_dir():
        shutil.rmtree(dest)
        removed_fs = True
    removed_db = skills_cache.delete_skill_link(db_path, workspace, alias)
    return removed_fs or removed_db


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
