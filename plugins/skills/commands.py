"""
`relaydeck skills ...` — CLI surface for the skills plugin.

Read commands (`list`/`show`/`doctor`) work off LIVE discovery
(`relaydeck.skills`), so they're always truthful regardless of how fresh the
daemon's cache is. `rescan` refreshes the cache mirror the lens reads.
`link`/`unlink`/`add` are the operator import actions (symlink/copy a skill
into a workspace). All are local filesystem/DB operations — they don't
touch live agent state, so no daemon round-trip is required (agents pick
up a newly-linked skill on next start).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

_ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _sanitize_alias(alias: str) -> str:
    if not alias or not _ALIAS_RE.match(alias) or alias in (".", ".."):
        raise ValueError(
            f"invalid alias {alias!r}: use letters/digits/._- only (no path separators)"
        )
    return alias


def _bundled_orchestrate_skill_dir() -> Path:
    """Return the bundled relaydeck-orchestrate skill source.

    In a built wheel it is force-included under `relaydeck/bundled_skills`.
    In a source checkout it lives at the repository root under `skills/`.
    """
    import relaydeck

    packaged = (
        Path(relaydeck.__file__).resolve().parent
        / "bundled_skills"
        / "relaydeck-orchestrate"
    )
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "skills" / "relaydeck-orchestrate"


def register(plugin) -> None:
    host = plugin.host
    config_home = plugin.config_home
    db_path = plugin.db_path

    import click
    from rich.console import Console
    from rich.table import Table

    from relaydeck import skills as _skills

    console = Console()

    def _all_refs():
        return _skills.discover_all_skills(
            config_home, include_codex=plugin._include_codex(),
            include_claude=plugin._include_claude(),
        )

    @host.cli.command("list", help="List discovered skills across workspaces + codex.")
    @click.option("--workspace", "-w", default=None, help="Filter by workspace.")
    @click.option("--source", default=None,
                  help="Filter by source (workspace|runtime-plugin|codex-user|codex-system).")
    @click.option("--invalid", is_flag=True, help="Show only invalid skills.")
    @click.option("--injectable", is_flag=True, help="Show only relaydeck-injectable skills.")
    def _list(workspace, source, invalid, injectable):
        refs = _all_refs()
        if workspace:
            refs = [r for r in refs if r.workspace == workspace]
        if source:
            refs = [r for r in refs if r.source_type == source]
        if invalid:
            refs = [r for r in refs if not r.valid]
        if injectable:
            refs = [r for r in refs if r.injectable]
        if not refs:
            console.print("[dim]No skills found.[/]")
            return
        table = Table(title="Skills", show_lines=False)
        table.add_column("name", style="cyan")
        table.add_column("source")
        table.add_column("workspace")
        table.add_column("owner")
        table.add_column("ok", justify="center")
        table.add_column("description")
        for r in sorted(refs, key=lambda x: (x.source_type, x.workspace or "", x.name)):
            ok = "[green]●[/]" if r.valid else "[red]✗[/]"
            desc = (r.description or "")[:60]
            table.add_row(r.name, r.source_type, r.workspace or "-",
                          r.owner_plugin or "-", ok, desc)
        console.print(table)

    @host.cli.command("show", help="Show one skill's full metadata + which agents see it.")
    @click.argument("name_or_id")
    def _show(name_or_id):
        refs = _all_refs()
        match = next((r for r in refs if r.id == name_or_id), None)
        if match is None:
            cands = [r for r in refs if r.name == name_or_id]
            if not cands:
                console.print(f"[red]✗[/] no skill named or id'd [bold]{name_or_id}[/]")
                raise SystemExit(1)
            if len(cands) > 1:
                console.print(f"[yellow]Multiple skills named {name_or_id}:[/]")
                for r in cands:
                    console.print(f"  {r.id}  {r.source_type}  ws={r.workspace or '-'}  {r.path}")
                return
            match = cands[0]
        console.print(f"[bold cyan]{match.name}[/]  [dim]{match.id}[/]")
        console.print(f"  source     : {match.source_type}"
                      f"{' (read-only)' if not match.injectable else ''}")
        console.print(f"  workspace  : {match.workspace or '-'}")
        console.print(f"  owner      : {match.owner_plugin or '-'}")
        console.print(f"  path       : {match.path}")
        if match.symlink_target:
            console.print(f"  symlink →  : {match.symlink_target}")
        console.print(f"  hash       : {match.content_hash}")
        console.print(f"  valid      : {'yes' if match.valid else 'NO'}")
        if match.description:
            console.print(f"  description: {match.description}")
        for e in match.errors:
            console.print(f"  [red]error[/]  : {e}")
        for w in match.warnings:
            console.print(f"  [yellow]warn[/]   : {w}")
        if match.injectable and match.workspace:
            console.print(f"  injected in: {match.workspace} "
                          f"(agents in this workspace see it on next start)")

    @host.cli.command("rescan", help="Refresh the skills inventory cache + emit change events.")
    def _rescan():
        from . import manager
        summary = manager.rescan(config_home, db_path, emit=host.events.emit,
                                 include_codex=plugin._include_codex(),
                                 include_claude=plugin._include_claude())
        console.print(
            f"[green]✓[/] refreshed inventory: {summary['total']} skills "
            f"({summary['changed']} changed, {summary['invalid']} invalid, "
            f"{summary['removed']} removed)"
        )

    @host.cli.command("hubs", help="List curated public skill discovery sources.")
    def _hubs():
        from . import hubs
        table = Table(title="Skill Sources", show_lines=False)
        table.add_column("name", style="cyan")
        table.add_column("kind")
        table.add_column("url")
        table.add_column("import hint")
        for h in hubs.curated_hubs():
            table.add_row(
                h["name"],
                h["kind"],
                h["url"],
                h.get("import_hint") or "-",
            )
        console.print(table)

    @host.cli.command(
        "install-orchestrator",
        help="Install the bundled relaydeck-orchestrate skill for external agents.",
    )
    @click.option("--target", type=click.Choice(["claude", "codex", "both"]),
                  default="claude", show_default=True,
                  help="User skill root to install into.")
    @click.option("--force", is_flag=True,
                  help="Replace an existing relaydeck-orchestrate install.")
    def _install_orchestrator(target, force):
        src = _bundled_orchestrate_skill_dir()
        ok, errors, _warnings = _skills.validate_skill_dir(src)
        if not ok:
            console.print(f"[red]✗[/] bundled orchestrator skill is invalid: {errors}")
            raise SystemExit(1)

        targets = []
        if target in ("claude", "both"):
            targets.append(("claude", _skills.claude_skills_root()))
        if target in ("codex", "both"):
            targets.append(("codex", _skills.codex_skills_root()))

        for label, root in targets:
            dest = root / "relaydeck-orchestrate"
            if dest.exists():
                if not force:
                    console.print(
                        f"[yellow]·[/] {label}: {dest} already exists "
                        "(pass --force to replace)"
                    )
                    continue
                if dest.is_symlink() or dest.is_file():
                    dest.unlink()
                else:
                    shutil.rmtree(dest)
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest)
            console.print(f"[green]✓[/] installed {label} skill → {dest}")

    @host.cli.command("link", help="Import an external skill into a workspace (symlink/copy/reference).")
    @click.argument("target_path")
    @click.option("--workspace", "-w", required=True, help="Destination workspace.")
    @click.option("--alias", default=None, help="Folder name in the workspace (default: target dir name).")
    @click.option("--mode", type=click.Choice(["symlink", "copy", "reference"]),
                  default="symlink", show_default=True)
    def _link(target_path, workspace, alias, mode):
        from . import manager
        target = Path(target_path).expanduser().resolve()
        try:
            alias = _sanitize_alias(alias or target.name)
            manager.link_skill(config_home, db_path, workspace, str(target), alias, mode)
        except ValueError as exc:
            console.print(f"[red]✗[/] {exc}")
            raise SystemExit(1)
        console.print(f"[green]✓[/] linked [bold]{alias}[/] → {target} ({mode}) in {workspace}")
        if mode == "reference":
            console.print("[dim]reference mode records the link only; it is not injected.[/]")

    @host.cli.command("add", help="Discover and import skill(s) from any supported source.")
    @click.argument("source")
    @click.option("--workspace", "-w", default=None, help="Deploy to one workspace only (default: all).")
    @click.option("--catalog-only", is_flag=True, help="Register in managed catalog without deploying.")
    @click.option("--skill", "skill_filter", default=None, help="Pick one skill by name when a source contains many.")
    @click.option("--mode", type=click.Choice(["symlink", "copy", "reference"]),
                  default="symlink", show_default=True)
    @click.option("--refresh", is_flag=True, help="Re-fetch the cached source first.")
    @click.option("--dry-run", is_flag=True, help="Discover only; do not import.")
    def _add(source, workspace, catalog_only, skill_filter, mode, refresh, dry_run):
        from . import manager
        try:
            resolved = manager.resolve_import_source(
                config_home, source, refresh=refresh, skill_filter=skill_filter,
            )
        except ValueError as exc:
            console.print(f"[red]✗[/] {exc}")
            raise SystemExit(1)
        skills = resolved.get("skills") or []
        console.print(f"[dim]{resolved.get('source', {}).get('display', source)}[/]")
        console.print(f"[bold]Found {len(skills)} skill(s)[/]")
        for s in skills:
            ok = "[green]●[/]" if s.get("valid") else "[red]✗[/]"
            console.print(
                f"  {ok} [cyan]{s.get('name')}[/]  "
                f"subpath={s.get('relative_subpath') or '.'}"
            )
        if dry_run:
            return
        selections = [
            {"subpath": s.get("relative_subpath") or s.get("subpath") or "."}
            for s in skills if s.get("valid")
        ]
        if not selections:
            console.print("[red]✗[/] no valid skills to import")
            raise SystemExit(1)
        deploy_to = "none" if catalog_only else ( [workspace] if workspace else "all" )
        try:
            result = manager.import_resolved_skills(
                config_home, db_path, resolved=resolved,
                selections=selections, workspace=workspace, deploy_to=deploy_to, mode=mode,
            )
        except ValueError as exc:
            console.print(f"[red]✗[/] {exc}")
            raise SystemExit(1)
        for item in result.get("imports") or []:
            alias = item.get("alias") or item.get("catalog", {}).get("alias")
            n_ws = len(item.get("deployments") or [])
            if n_ws:
                console.print(f"[green]✓[/] imported [bold]{alias}[/] into {n_ws} workspace(s) ({mode})")
            else:
                console.print(f"[green]✓[/] catalogued [bold]{alias}[/] (not deployed)")
        for err in result.get("errors") or []:
            console.print(f"  [red]✗[/] {err.get('subpath')}: {err.get('error')}")

    @host.cli.command("import-git", help="Clone a Git/GitHub skill and link it into a workspace.")
    @click.argument("source_url")
    @click.option("--workspace", "-w", required=True, help="Destination workspace.")
    @click.option("--alias", default=None, help="Folder name in the workspace.")
    @click.option("--subpath", default=None, help="Skill subdirectory inside the repo.")
    @click.option("--ref", "source_ref", default=None, help="Git branch/tag/ref.")
    @click.option("--mode", type=click.Choice(["symlink", "copy", "reference"]),
                  default="symlink", show_default=True)
    @click.option("--refresh", is_flag=True, help="Re-clone the cached source first.")
    def _import_git(source_url, workspace, alias, subpath, source_ref, mode, refresh):
        from . import manager
        try:
            res = manager.import_git_skill(
                config_home,
                db_path,
                workspace=workspace,
                source_url=source_url,
                alias=alias,
                source_ref=source_ref,
                source_subpath=subpath,
                mode=mode,
                refresh=refresh,
            )
        except ValueError as exc:
            console.print(f"[red]✗[/] {exc}")
            raise SystemExit(1)
        link = res["link"]
        console.print(
            f"[green]✓[/] imported [bold]{link['alias']}[/] into {workspace} ({mode})"
        )

    @host.cli.command("refresh-imports", help="Check managed skill imports for local drift/upstream updates.")
    @click.option("--force", is_flag=True, help="Ignore the check interval.")
    def _refresh_imports(force):
        from . import manager
        res = manager.refresh_managed_imports(
            config_home, db_path, min_interval_s=0 if force else 3600, force=force
        )
        console.print(
            f"[green]✓[/] checked {res['checked']} managed import(s); "
            f"{res['changed']} changed, {res['update_available']} update available, "
            f"{res['skipped']} skipped"
        )
        for err in res.get("errors") or []:
            console.print(f"  [red]{err['workspace']}/{err['alias']}[/]: {err['error']}")

    @host.cli.command("deploy", help="Inject a managed catalog skill into a workspace.")
    @click.argument("alias")
    @click.option("--workspace", "-w", required=True, help="Destination workspace.")
    @click.option("--mode", type=click.Choice(["symlink", "copy", "reference"]),
                  default="symlink", show_default=True)
    def _deploy(alias, workspace, mode):
        from . import manager
        try:
            link = manager.deploy_catalog_skill(config_home, db_path, alias, workspace, mode=mode)
        except Exception as exc:
            console.print(f"[red]✗[/] {exc}")
            raise SystemExit(1)
        console.print(
            f"[green]✓[/] injected [bold]{link['alias']}[/] into {workspace} ({mode}); "
            "active on next agent start"
        )

    @host.cli.command("unlink", help="Remove a linked skill from a workspace.")
    @click.argument("alias")
    @click.option("--workspace", "-w", required=True, help="Workspace the link lives in.")
    def _unlink(alias, workspace):
        from . import manager
        try:
            alias = _sanitize_alias(alias)
        except ValueError as exc:
            console.print(f"[red]✗[/] {exc}")
            raise SystemExit(1)
        if not manager.unlink_skill(config_home, db_path, workspace, alias):
            console.print(f"[yellow]·[/] nothing to unlink at {workspace}/{alias}")
            return
        console.print(f"[green]✓[/] unlinked [bold]{alias}[/] from {workspace}")

    @host.cli.command("doctor", help="Skills health summary across all sources.")
    def _doctor():
        from . import manager
        refs = _all_refs()
        by_source: dict[str, int] = {}
        invalid = 0
        warnings = 0
        for r in refs:
            by_source[r.source_type] = by_source.get(r.source_type, 0) + 1
            if not r.valid:
                invalid += 1
            warnings += len(r.warnings)
        console.print(f"[bold]Skills doctor[/] — {len(refs)} total")
        for src, n in sorted(by_source.items()):
            console.print(f"  {src:16} {n}")
        console.print(f"  {'invalid':16} {invalid}")
        console.print(f"  {'warnings':16} {warnings}")
        try:
            overview = manager.inventory_overview(db_path)
            summary = overview.get("summary") or {}
            console.print(f"  {'active':16} {summary.get('active', 0)}")
            console.print(
            f"  {'workspace LLM tokens':16} "
            f"{int(summary.get('usage_tokens_by_skill_exposure') or 0):,}"
            )
            console.print(f"  {'exact uses':16} {int(summary.get('exact_skill_uses') or 0):,}")
        except Exception:
            pass
        codex_root = _skills.codex_skills_root()
        console.print(f"  codex root      : {codex_root} "
                      f"({'present' if codex_root.is_dir() else 'absent'})")
        claude_root = _skills.claude_skills_root()
        console.print(f"  claude root     : {claude_root} "
                      f"({'present' if claude_root.is_dir() else 'absent'})")
