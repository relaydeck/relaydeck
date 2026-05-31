"""
`relaydeck skills ...` — CLI surface for the skills plugin.

Read commands (`list`/`show`/`validate`/`doctor`) work off LIVE discovery
(`relaydeck.skills`), so they're always truthful regardless of how fresh the
daemon's cache is. `rescan` refreshes the cache mirror the lens reads.
`link`/`unlink` are the operator import actions (symlink/copy a skill
into a workspace). All are local filesystem/DB operations — they don't
touch live agent state, so no daemon round-trip is required (agents pick
up a newly-linked skill on next start).
"""

from __future__ import annotations

import re
from pathlib import Path

_ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _sanitize_alias(alias: str) -> str:
    if not alias or not _ALIAS_RE.match(alias) or alias in (".", ".."):
        raise ValueError(
            f"invalid alias {alias!r}: use letters/digits/._- only (no path separators)"
        )
    return alias


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
        # Consumers: which workspaces inject this skill (by name match).
        if match.injectable and match.workspace:
            console.print(f"  injected in: {match.workspace} "
                          f"(agents in this workspace see it on next start)")

    @host.cli.command("validate", help="Validate skills; exit non-zero if any are invalid.")
    @click.option("--workspace", "-w", default=None, help="Limit to one workspace.")
    def _validate(workspace):
        refs = _all_refs()
        if workspace:
            refs = [r for r in refs if r.workspace == workspace]
        bad = [r for r in refs if not r.valid]
        if not bad:
            console.print(f"[green]✓[/] {len(refs)} skill(s) valid.")
            return
        console.print(f"[red]✗ {len(bad)} invalid skill(s):[/]")
        for r in bad:
            console.print(f"  [bold]{r.name}[/] ({r.path})")
            for e in r.errors:
                console.print(f"    - {e}")
        raise SystemExit(1)

    @host.cli.command("rescan", help="Refresh the skills inventory cache + emit change events.")
    def _rescan():
        from . import manager
        summary = manager.rescan(config_home, db_path, emit=host.events.emit,
                                 include_codex=plugin._include_codex(),
                                 include_claude=plugin._include_claude())
        console.print(
            f"[green]✓[/] rescanned: {summary['total']} skills "
            f"({summary['changed']} changed, {summary['invalid']} invalid, "
            f"{summary['removed']} removed)"
        )

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
        console.print(f"  {'invalid':16} {invalid}"
                      + (" [red](review with `relaydeck skills validate`)[/]" if invalid else ""))
        console.print(f"  {'warnings':16} {warnings}")
        codex_root = _skills.codex_skills_root()
        console.print(f"  codex root      : {codex_root} "
                      f"({'present' if codex_root.is_dir() else 'absent'})")
        claude_root = _skills.claude_skills_root()
        console.print(f"  claude root     : {claude_root} "
                      f"({'present' if claude_root.is_dir() else 'absent'})")
