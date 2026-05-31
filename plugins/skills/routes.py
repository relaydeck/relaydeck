"""
`/api/plugins/skills/*` — HTTP surface backing the dashboard Skills lens.

Reads come from the `skills_cache` mirror (kept warm by the rescan
worker) so the lens is cheap to poll; `/workspaces/{ws}` does a live
discovery so the operator can see exactly what an agent in that workspace
will be injected. Mutations (`rescan`/`link`/`unlink`) run in the daemon
and emit `skills.*` events so connected dashboards refresh live.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("plugins.skills")


def register(plugin) -> None:
    host = plugin.host
    config_home = plugin.config_home
    db_path = plugin.db_path

    from relaydeck import skills as _skills
    from relaydeck import skills_cache

    from . import manager

    @host.api.route("/skills", methods=["GET"])
    async def skills_list(
        workspace: str | None = None,
        source: str | None = None,
        owner: str | None = None,
        valid: bool | None = None,
    ):
        """Inventory from the cache mirror, with optional filters."""
        rows = skills_cache.list_skills_cache(
            db_path, workspace=workspace, source_type=source,
            owner_plugin=owner, valid=valid,
        )
        return {"skills": rows}

    @host.api.route("/skills/{skill_id}", methods=["GET"])
    async def skill_detail(skill_id: str):
        row = skills_cache.get_skill_cache(db_path, skill_id)
        if row is None:
            return {"error": "not found", "id": skill_id}
        # Best-effort body preview straight from disk.
        try:
            from pathlib import Path
            body = (Path(row["path"]) / "SKILL.md").read_text()
            _fm, md_body = _skills.parse_skill_md(body)
            row["body_preview"] = md_body[:4000]
        except OSError:
            row["body_preview"] = ""
        return row

    @host.api.route("/workspaces/{workspace}", methods=["GET"])
    async def workspace_skills(workspace: str):
        """Live injection truth for one workspace: user-authored vs
        plugin-runtime skills, and the per-source injection method."""
        refs = _skills.discover_workspace_skills(config_home, workspace)
        user = [r.to_dict() for r in refs if r.source_type == _skills.SOURCE_WORKSPACE]
        runtime = [r.to_dict() for r in refs if r.source_type == _skills.SOURCE_RUNTIME_PLUGIN]
        return {
            "workspace": workspace,
            "user_skills": user,
            "runtime_skills": runtime,
            "injection": {
                "pi": "--skill <dir> per valid skill",
                "codex": "SKILL.md path list in model instructions",
                "claude-code": "runtime skill bodies inlined into --append-system-prompt",
            },
            "note": "running agents pick up changes on next start (restart required)",
        }

    @host.api.route("/rescan", methods=["POST"])
    async def skills_rescan():
        summary = manager.rescan(config_home, db_path, emit=host.events.emit,
                                 include_codex=plugin._include_codex(), include_claude=plugin._include_claude())
        return {"ok": True, **summary}

    @host.api.route("/validate", methods=["POST"])
    async def skills_validate(body: dict | None = None):
        workspace = (body or {}).get("workspace")
        refs = _skills.discover_all_skills(config_home, include_codex=plugin._include_codex(), include_claude=plugin._include_claude())
        if workspace:
            refs = [r for r in refs if r.workspace == workspace]
        invalid = [
            {"id": r.id, "name": r.name, "path": r.path, "errors": r.errors}
            for r in refs if not r.valid
        ]
        return {"total": len(refs), "invalid": invalid, "ok": not invalid}

    @host.api.route("/link", methods=["POST"])
    async def skills_link(body: dict):
        from pathlib import Path
        try:
            workspace = body["workspace"]
            target_path = body["target_path"]
            alias = body.get("alias") or Path(target_path).name
        except KeyError as exc:
            return {"ok": False, "error": f"missing field: {exc}"}
        mode = body.get("mode", "symlink")
        try:
            link = manager.link_skill(config_home, db_path, workspace, target_path, alias, mode)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        manager.rescan(config_home, db_path, emit=host.events.emit,
                       include_codex=plugin._include_codex(), include_claude=plugin._include_claude())
        return {"ok": True, "link": link}

    @host.api.route("/unlink", methods=["POST"])
    async def skills_unlink(body: dict):
        workspace = body.get("workspace")
        alias = body.get("alias")
        if not (workspace and alias):
            return {"ok": False, "error": "workspace and alias required"}
        removed = manager.unlink_skill(config_home, db_path, workspace, alias)
        manager.rescan(config_home, db_path, emit=host.events.emit,
                       include_codex=plugin._include_codex(), include_claude=plugin._include_claude())
        return {"ok": removed}
