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

    from . import hubs, manager

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

    @host.api.route("/hubs", methods=["GET"])
    async def skills_hubs():
        return {"hubs": hubs.curated_hubs()}

    @host.api.route("/overview", methods=["GET"])
    async def skills_overview():
        """Fleet activation + footprint summary for the Skills lens."""
        return manager.inventory_overview(db_path)

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

    @host.api.route("/refresh-imports", methods=["POST"])
    async def skills_refresh_imports(body: dict | None = None):
        refresh = manager.refresh_managed_imports(
            config_home,
            db_path,
            min_interval_s=float((body or {}).get("min_interval_s", 3600.0)),
            force=bool((body or {}).get("force", False)),
        )
        summary = manager.rescan(config_home, db_path, emit=host.events.emit,
                                 include_codex=plugin._include_codex(), include_claude=plugin._include_claude())
        return {"ok": True, "refresh": refresh, "rescan": summary}

    @host.api.route("/resolve", methods=["POST"])
    async def skills_resolve(body: dict):
        """Discover skills from any supported source (URL, npm, npx, local path)."""
        source = str(
            body.get("source")
            or body.get("source_url")
            or body.get("target_path")
            or ""
        ).strip()
        if not source:
            return {"ok": False, "error": "source required"}
        try:
            resolved = manager.resolve_import_source(
                config_home,
                source,
                refresh=bool(body.get("refresh")),
                skill_filter=body.get("skill_filter") or body.get("skill"),
            )
            return {"ok": True, **resolved}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    @host.api.route("/import", methods=["POST"])
    async def skills_import_batch(body: dict):
        """Register managed catalog skills and deploy to workspace(s)."""
        selections = body.get("selections") or body.get("skills") or []
        if not selections:
            return {"ok": False, "error": "selections required (list of {subpath, alias?})"}
        resolved = body.get("resolved")
        source_text = str(body.get("source") or body.get("source_url") or "").strip()
        deploy_to = body.get("deploy_to", "all")
        workspace = body.get("workspace")
        try:
            if not resolved:
                if not source_text:
                    return {"ok": False, "error": "source or resolved payload required"}
                resolved = manager.resolve_import_source(
                    config_home,
                    source_text,
                    refresh=bool(body.get("refresh")),
                    skill_filter=body.get("skill_filter") or body.get("skill"),
                )
            result = manager.import_resolved_skills(
                config_home,
                db_path,
                resolved=resolved,
                selections=selections,
                workspace=workspace,
                deploy_to=deploy_to,
                mode=body.get("mode", "symlink"),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        manager.rescan(config_home, db_path, emit=host.events.emit,
                       include_codex=plugin._include_codex(), include_claude=plugin._include_claude())
        return {"ok": True, **result}

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
            link = manager.link_skill(
                config_home, db_path, workspace, target_path, alias, mode,
                source_url=body.get("source_url"),
                source_ref=body.get("source_ref"),
                source_subpath=body.get("source_subpath"),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        manager.rescan(config_home, db_path, emit=host.events.emit,
                       include_codex=plugin._include_codex(), include_claude=plugin._include_claude())
        return {"ok": True, "link": link}

    @host.api.route("/import-git", methods=["POST"])
    async def skills_import_git(body: dict):
        try:
            workspace = body["workspace"]
            source_url = body["source_url"]
        except KeyError as exc:
            return {"ok": False, "error": f"missing field: {exc}"}
        try:
            res = manager.import_git_skill(
                config_home,
                db_path,
                workspace=workspace,
                source_url=source_url,
                alias=body.get("alias") or None,
                mode=body.get("mode", "symlink"),
                source_ref=body.get("source_ref"),
                source_subpath=body.get("source_subpath"),
                refresh=bool(body.get("refresh")),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        manager.rescan(config_home, db_path, emit=host.events.emit,
                       include_codex=plugin._include_codex(), include_claude=plugin._include_claude())
        return {"ok": True, **res}

    @host.api.route("/usage", methods=["GET"])
    async def skills_usage(
        workspace: str | None = None,
        skill: str | None = None,
        agent: str | None = None,
        limit: int = 100,
    ):
        rows = skills_cache.list_skill_usage(
            db_path,
            workspace=workspace,
            skill_name=skill,
            agent_id=agent,
            limit=limit,
        )
        return {"usage": rows}

    @host.api.route("/usage", methods=["POST"])
    async def skills_usage_record(body: dict):
        skill_name = str(body.get("skill_name") or body.get("skill") or "").strip()
        if not skill_name:
            return {"ok": False, "error": "skill_name required"}
        event = skills_cache.record_skill_usage(
            db_path,
            skill_name=skill_name,
            workspace=body.get("workspace"),
            agent_id=body.get("agent_id"),
            skill_id=body.get("skill_id"),
            source=body.get("source", "api"),
            event_type=body.get("event_type", "used"),
            confidence=float(body.get("confidence", 1.0)),
            prompt_tokens=int(body.get("prompt_tokens", 0) or 0),
            completion_tokens=int(body.get("completion_tokens", 0) or 0),
            total_tokens=int(body.get("total_tokens", 0) or 0),
            cost_usd=body.get("cost_usd"),
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )
        try:
            host.events.emit("skills.usage", event)
        except Exception:
            pass
        return {"ok": True, "event": event}

    @host.api.route("/deploy", methods=["POST"])
    async def skills_deploy(body: dict):
        """Inject an already-managed catalog skill into one workspace."""
        alias = str(body.get("alias") or "").strip()
        workspace = str(body.get("workspace") or "").strip()
        if not (alias and workspace):
            return {"ok": False, "error": "alias and workspace required"}
        try:
            link = manager.deploy_catalog_skill(
                config_home, db_path, alias, workspace,
                mode=body.get("mode", "symlink"),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        manager.rescan(config_home, db_path, emit=host.events.emit,
                       include_codex=plugin._include_codex(), include_claude=plugin._include_claude())
        return {"ok": True, "link": link}

    @host.api.route("/catalog", methods=["POST"])
    async def skills_catalog(body: dict):
        """Centralize a discovered skill: register it in the managed catalog
        so it can be injected into any workspace from one place."""
        from pathlib import Path

        path = str(body.get("path") or body.get("target_path") or "").strip()
        if not path:
            return {"ok": False, "error": "path required"}
        try:
            meta = manager.read_skill_metadata(path)
            if not meta.get("valid"):
                return {"ok": False, "error": "invalid skill: " + "; ".join(meta.get("errors") or [])}
            alias = str(body.get("alias") or "").strip() or _skills.validate_path_component(
                Path(path).name, kind="alias"
            )
            link = manager.register_catalog_skill(
                config_home, db_path, path, alias,
                target_id=meta.get("content_hash"),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        deploy_to = body.get("deploy_to")
        deployed: list = []
        if isinstance(deploy_to, list):
            for ws in deploy_to:
                try:
                    deployed.append(manager.deploy_catalog_skill(
                        config_home, db_path, alias, ws, mode=body.get("mode", "symlink")))
                except Exception as exc:
                    deployed.append({"workspace": ws, "error": str(exc)})
        manager.rescan(config_home, db_path, emit=host.events.emit,
                       include_codex=plugin._include_codex(), include_claude=plugin._include_claude())
        return {"ok": True, "link": link, "deployed": deployed}

    @host.api.route("/catalog/{alias}", methods=["GET"])
    async def skills_catalog_detail(alias: str):
        """Per-skill management view: catalog link, parsed metadata, SKILL.md
        preview, and where it is currently injected — works even when the
        skill is injected into zero workspaces (not in the rescan cache)."""
        from pathlib import Path

        link = skills_cache.get_skill_link(db_path, manager.CATALOG_WORKSPACE, alias)
        if link is None:
            return {"error": "not found"}
        path = str(link.get("target_path") or "")
        meta: dict = {}
        body_preview = ""
        try:
            meta = manager.read_skill_metadata(path)
            md = Path(path) / "SKILL.md"
            if md.is_file():
                body_preview = md.read_text()[:4000]
        except Exception:
            pass
        deployed = [
            l for l in skills_cache.list_skill_links(db_path)
            if l.get("alias") == alias and l.get("workspace") != manager.CATALOG_WORKSPACE
        ]
        return {
            "alias": alias, "link": link, "meta": meta,
            "body_preview": body_preview, "deployed": deployed,
        }

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
