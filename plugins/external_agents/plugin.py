"""Bundled external-agents plugin.

Daemon-wide, read-only. Lets an operator point relaydeck at an existing
**Hermes Agent** or **OpenClaw** runtime (a repo or config home) and have
relaydeck recognize it, report its health + risk posture, and surface it in the
dashboard/CLI next to the agents relaydeck actually runs — without onboarding,
mutating, or copying secrets from the external runtime.

What it deliberately is NOT (per RESEARCH/PLAN docs):
  - not a Hermes/OpenClaw onboarding, model setup, or migration path,
  - not a messaging-channel / gateway / dashboard clone,
  - not a runner: relaydeck never starts these runtimes. The one relaydeck
    runtime primitive stays "the agent relaydeck runs"; an external agent is an
    observed peer runtime (see `models.ExternalAgent`).

Surfaces (web-primary, CLI at parity):
  - Dashboard "External" lens (rail slot) — `static/panel.js`.
  - `relaydeck external detect|add|list|show|health|remove`.
  - HTTP under `/api/plugins/external/...`.

v1 is read-only: detection + filesystem/CLI posture. Structured task handoff
(Hermes MCP, OpenClaw Gateway WS) and a relaydeck MCP server are later phases —
the spec/store are intentionally serializable so adapters can land additively.
"""

from __future__ import annotations

import contextlib
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from relaydeck.sdk import Plugin, PluginContext, PluginEventBus, PluginHost

from . import detector, probes, store
from .models import (
    DEFAULT_TRANSPORT,
    KINDS,
    T_UNKNOWN,
    ExternalAgent,
    ProbeReport,
    now_iso,
    slug_id,
)

PLUGIN_NAME = "external"
logger = logging.getLogger(__name__)


# ── Registered-workspace reader (for candidate auto-scan) ───────────


def _registered_workspace_paths(config_home: Path) -> list[str]:
    config_path = config_home / "config.toml"
    if not config_path.exists():
        return []
    try:
        from relaydeck.config import load_config_file
        data = load_config_file(config_path)
    except Exception:
        return []
    out: list[str] = []
    for block in data.get("workspace", []) or []:
        if isinstance(block, dict) and block.get("path"):
            out.append(str(Path(block["path"]).expanduser()))
    return out


# ── Pure registry helpers (shared by API + CLI fallback + tests) ────


def build_agent(
    path: str,
    *,
    kind: str = "auto",
    name: str | None = None,
    workspace: str | None = None,
) -> ExternalAgent:
    """Detect + construct an ExternalAgent spec from a path. Pure: no disk
    write, no subprocess beyond detector's injectable `which`. Raises
    ValueError if kind=auto and nothing is detected, or on an explicit kind
    mismatch the caller should know about (we still allow forcing)."""
    det = detector.detect(path)
    resolved_kind = kind
    if kind == "auto":
        if not det.matched:
            raise ValueError(
                f"could not detect a hermes/openclaw runtime at {path!r} "
                f"(pass --kind hermes|openclaw to force)"
            )
        resolved_kind = det.kind
    elif kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")

    root = det.root or str(Path(path).expanduser())
    agent_id = slug_id(name) if name else slug_id(Path(root).name or resolved_kind)
    # When a kind is forced on an undetected path, detection didn't supply a
    # transport ("unknown") — fall back to the kind's preferred native
    # transport so a forced agent still carries useful metadata.
    transport = det.recommended_transport
    if transport == T_UNKNOWN:
        transport = DEFAULT_TRANSPORT.get(resolved_kind, T_UNKNOWN)
    return ExternalAgent(
        id=agent_id,
        kind=resolved_kind,
        name=name or Path(root).name or resolved_kind,
        root=root,
        config_home=det.config_home,
        workspace=workspace,
        transport=transport,
        confidence=det.confidence,
        signals=det.signals,
    )


def probe_and_store(config_home: Path, agent: ExternalAgent) -> ProbeReport:
    """Run read-only probes, cache the report on the agent, persist."""
    report = probes.run_probes(agent)
    agent.last_probe = report.to_dict()
    agent.updated_at = now_iso()
    store.save_agent(config_home, agent)
    return report


def list_candidates(config_home: Path) -> list[dict]:
    """Auto-detected runtimes on this host not already registered."""
    registered_roots = {a.root for a in store.list_agents(config_home)}
    out = []
    for det in detector.scan_candidates(_registered_workspace_paths(config_home)):
        if det.root in registered_roots:
            continue
        out.append(det.to_dict())
    return out


# ── New-agent catalog contribution ──────────────────────────────────

_EXTERNAL_BLURB = {
    "hermes": "Hermes Agent (external runtime) — launch its chat TUI here.",
    "openclaw": "OpenClaw (external runtime) — launch its chat TUI here.",
}


def _external_type_cards(config_home: Path | str) -> list[dict]:
    """Linked Hermes/OpenClaw runtimes as new-agent type cards.

    Registered into the core harness catalog at on_load so the new-agent
    modal lists one card per linked external runtime. Returns [] if nothing
    is linked — the modal simply shows no external cards then."""
    from relaydeck.orchestrator import known_agent_types

    known = set(known_agent_types())
    out: list[dict] = []
    try:
        agents = store.list_agents(config_home)
    except Exception:
        return []
    for a in agents:
        kind = getattr(a, "kind", "")
        if kind not in _EXTERNAL_BLURB:
            continue
        out.append({
            "type": kind,
            "label": getattr(a, "name", None) or kind,
            "blurb": _EXTERNAL_BLURB[kind],
            "icon": "cpu",
            "kind": "external",
            "available": kind in known,
            "launch_options": [],
            "external_id": getattr(a, "id", ""),
            "external_kind": kind,
        })
    return out


# ── Plugin ──────────────────────────────────────────────────────────


class ExternalAgentsPlugin(Plugin):
    workspace_scoped = False
    description = (
        "Discover and observe external agent runtimes (Hermes, OpenClaw): "
        "detection, health, and risk posture — read-only."
    )

    def on_load(self, host: PluginHost) -> None:
        # Side-effect-free wiring only: on_load runs in every `relaydeck`
        # CLI invocation too (relaydeck/__init__.py loads plugins before
        # parsing argv), so no workers / network / subprocess here.
        self.host = host
        self.config_home = host.config_home
        # Register the external runtimes as relaydeck harness types so a
        # linked Hermes/OpenClaw can be spawned as a chat agent in any
        # workspace (terminal tile, attach, message). Just records the
        # mapping — the SDK adapter binds it to the orchestrator.
        from .harness import HARNESS_TYPES
        for type_name, cls in HARNESS_TYPES.items():
            host.harnesses.register(type_name, cls)
        # Contribute linked external runtimes to the new-agent type catalog
        # without core importing this plugin's module path.
        from relaydeck.harness_options import register_external_catalog_provider
        register_external_catalog_provider(_external_type_cards)
        self._register_api()
        self._register_cli()

    def on_unload(self) -> None:
        from relaydeck.harness_options import unregister_external_catalog_provider
        unregister_external_catalog_provider(_external_type_cards)

    def _emit(self, event_type: str, agent: ExternalAgent) -> None:
        with contextlib.suppress(Exception):
            self.host.events.emit(event_type, {"id": agent.id, "kind": agent.kind})

    # ── Spawn a linked runtime's chat TUI into a workspace ──────────

    def _unique_agent_id(self, base: str, workspace: str) -> str:
        """Pick a free relaydeck agent id: `<base>`, then `<base>-<ws>`,
        then `<base>-2`, `<base>-3`, … Coerced through `sanitize_agent_id`
        so a generated id always satisfies the agent-id rule that
        `create_agent` enforces."""
        from relaydeck.config import sanitize_agent_id
        base = sanitize_agent_id(base) or "ext"
        for cand in (base, f"{base}-{sanitize_agent_id(workspace)}"):
            if self.host.agents.get(cand) is None:
                return cand
        n = 2
        while self.host.agents.get(f"{base}-{n}") is not None:
            n += 1
        return f"{base}-{n}"

    def _spawn_agent(
        self, ext_id: str, workspace: str,
        *, agent_id: str | None = None, start: bool = True,
    ) -> dict:
        """Create (and optionally start) a relaydeck harness agent that runs
        the linked external runtime's chat TUI in `workspace`. Returns a
        result dict. Raises ValueError (the API maps to 404/400) for an
        unknown external id, unsupported kind, or unknown workspace, and
        RuntimeError (→ 503) if the agent runtime isn't bound yet.

        relaydeck runs the runtime's CLI in the workspace dir using the
        runtime's own config home — it never copies secrets or mutates the
        external config."""
        from .harness import KIND_TO_TYPE

        # `host.agents` is None until an orchestrator is bound to the host
        # (PluginHost wires it lazily via get_orchestrator at load_all). In a
        # running daemon it's always bound; guard so a not-yet-ready host
        # returns a clean 503 instead of an AttributeError.
        if self.host is None or self.host.agents is None:
            raise RuntimeError(
                "agent runtime unavailable — the daemon must be running to spawn agents"
            )
        agent = store.get_agent(self.config_home, ext_id)
        if agent is None:
            raise ValueError(f"no external agent {ext_id!r}")
        agent_type = KIND_TO_TYPE.get(agent.kind)
        if agent_type is None:
            raise ValueError(f"kind {agent.kind!r} cannot be spawned as a chat agent")
        ws = (workspace or "").strip()
        if not ws:
            raise ValueError("workspace is required")
        # "Spawn into a workspace" must mean a real one — otherwise the
        # harness can't resolve a cwd and runs outside the requested
        # workspace. Require it to exist (no lenient empty-registry pass).
        known = {w.get("name") for w in self.host.workspaces.list()}
        if ws not in known:
            raise ValueError(f"unknown workspace {ws!r}")
        new_id = self._unique_agent_id(agent_id or agent.kind, ws)
        self.host.agents.create(
            new_id, agent_type, new_id,
            workspace=ws,
            purpose=f"{agent.kind} chat (external runtime) in {ws}",
            tags=["external", agent.kind],
            inject_identity_preamble=False,
            config={},
        )
        started = False
        if start:
            self.host.agents.start(new_id)
            started = True
        return {
            "ok": True, "agent_id": new_id, "type": agent_type,
            "workspace": ws, "kind": agent.kind, "started": started,
        }

    # ── HTTP API (mounted under /api/plugins/external) ──────────────

    def _register_api(self) -> None:
        host = self.host

        @host.api.route("/agents", methods=["GET"])
        async def list_external():
            agents = [a.to_dict() for a in store.list_agents(self.config_home)]
            return {"agents": agents, "candidates": list_candidates(self.config_home)}

        @host.api.route("/agents", methods=["POST"])
        async def add_external(body: dict):
            from fastapi import HTTPException
            path = str((body or {}).get("path") or "").strip()
            if not path:
                raise HTTPException(400, "path is required")
            kind = str((body or {}).get("kind") or "auto").strip() or "auto"
            name = (body or {}).get("name") or None
            workspace = (body or {}).get("workspace") or None
            try:
                agent = build_agent(path, kind=kind, name=name, workspace=workspace)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if store.exists(self.config_home, agent.id):
                raise HTTPException(
                    409, f"external agent {agent.id!r} already registered",
                )
            if (body or {}).get("probe"):
                probe_and_store(self.config_home, agent)
            else:
                store.save_agent(self.config_home, agent)
            self._emit("external_agent.added", agent)
            return agent.to_dict()

        @host.api.route("/agents/{agent_id}", methods=["GET"])
        async def get_external(agent_id: str):
            from fastapi import HTTPException
            agent = store.get_agent(self.config_home, agent_id)
            if agent is None:
                raise HTTPException(404, f"no external agent {agent_id!r}")
            return agent.to_dict()

        @host.api.route("/agents/{agent_id}", methods=["DELETE"])
        async def remove_external(agent_id: str):
            from fastapi import HTTPException
            agent = store.get_agent(self.config_home, agent_id)
            if agent is None:
                raise HTTPException(404, f"no external agent {agent_id!r}")
            # Only emit + report success once the file is actually gone — a
            # failed unlink would otherwise reappear on the next reload while
            # the client thinks it's removed.
            if not store.delete_agent(self.config_home, agent_id):
                raise HTTPException(500, f"failed to delete external agent {agent_id!r}")
            self._emit("external_agent.removed", agent)
            return {"ok": True, "id": agent_id}

        @host.api.route("/agents/{agent_id}/probe", methods=["POST"])
        async def probe_external(agent_id: str):
            from fastapi import HTTPException
            agent = store.get_agent(self.config_home, agent_id)
            if agent is None:
                raise HTTPException(404, f"no external agent {agent_id!r}")
            report = probe_and_store(self.config_home, agent)
            self._emit("external_agent.health", agent)
            return {"id": agent_id, **report.to_dict()}

        @host.api.route("/detect", methods=["POST"])
        async def detect_external(body: dict):
            from fastapi import HTTPException
            path = str((body or {}).get("path") or "").strip()
            if not path:
                raise HTTPException(400, "path is required")
            return detector.detect(path).to_dict()

        @host.api.route("/candidates", methods=["GET"])
        async def candidates_external():
            return {"candidates": list_candidates(self.config_home)}

        @host.api.route("/agents/{agent_id}/spawn", methods=["POST"])
        async def spawn_external(agent_id: str, body: dict):
            """Launch the linked runtime's chat TUI as a relaydeck agent in a
            workspace. Body: workspace (required), agent_id?, start? (default
            true). Returns the new relaydeck agent id + type."""
            from fastapi import HTTPException
            try:
                return self._spawn_agent(
                    agent_id,
                    str((body or {}).get("workspace") or ""),
                    agent_id=(body or {}).get("agent_id") or None,
                    start=bool((body or {}).get("start", True)),
                )
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc
            except ValueError as exc:
                msg = str(exc)
                status = 404 if msg.startswith("no external agent") else 400
                raise HTTPException(status, msg) from exc

    # ── CLI: `relaydeck external ...` ───────────────────────────────

    def _register_cli(self) -> None:
        host = self.host
        import click
        from rich.console import Console
        from rich.table import Table

        console = Console()

        @host.cli.command("detect", help="Detect whether a path is a Hermes/OpenClaw runtime.")
        @click.argument("path", type=click.Path())
        def _detect(path: str):
            det = detector.detect(path)
            if not det.matched:
                console.print(f"[yellow]No hermes/openclaw runtime detected at[/] {path}")
                for w in det.warnings:
                    console.print(f"  [dim]{w}[/]")
                return
            console.print(
                f"[green]✓[/] [bold]{det.kind}[/] "
                f"(confidence {det.confidence:.2f}, transport {det.recommended_transport})"
            )
            if det.config_home:
                console.print(f"  config home: [cyan]{det.config_home}[/]")
            for s in det.signals:
                console.print(f"  [dim]· {s}[/]")

        @host.cli.command("add", help="Register an existing Hermes/OpenClaw runtime (read-only).")
        @click.argument("path", type=click.Path())
        @click.option("--kind", default="auto", type=click.Choice(["auto", "hermes", "openclaw"]))
        @click.option("--name", default=None, help="Display name + id (defaults to the dir name).")
        @click.option("--workspace", "-w", default=None, help="Associate with a workspace.")
        @click.option("--probe", is_flag=True, help="Run a health probe after adding.")
        def _add(path: str, kind: str, name: str | None, workspace: str | None, probe: bool):
            payload = {
                "path": str(Path(path).expanduser()), "kind": kind,
                "name": name, "workspace": workspace, "probe": probe,
            }
            ok, resp = _post_daemon("/api/plugins/external/agents", payload)
            if ok and isinstance(resp, dict):
                console.print(
                    f"[green]✓[/] registered [bold]{resp.get('id')}[/] "
                    f"({resp.get('kind')}) → {resp.get('root')}"
                )
                return
            # Daemon down (or rejected): fall back to a local write so the
            # CLI works offline. Live dashboards refresh on next heartbeat.
            if _is_conn_error(resp):
                try:
                    agent = build_agent(path, kind=kind, name=name, workspace=workspace)
                except ValueError as exc:
                    console.print(f"[red]✗[/] {exc}")
                    raise SystemExit(1) from exc
                if store.exists(self.config_home, agent.id):
                    console.print(f"[red]✗[/] external agent {agent.id!r} already registered")
                    raise SystemExit(1)
                if probe:
                    probe_and_store(self.config_home, agent)
                else:
                    store.save_agent(self.config_home, agent)
                console.print(
                    f"[green]✓[/] registered [bold]{agent.id}[/] ({agent.kind}) "
                    f"[dim](daemon unreachable — dashboard updates on next refresh)[/]"
                )
                return
            console.print(f"[red]✗[/] {resp}")
            raise SystemExit(1)

        @host.cli.command("list", help="List registered external agents.")
        def _list():
            agents = store.list_agents(self.config_home)
            if not agents:
                console.print(
                    "[dim]No external agents registered. "
                    "Run [bold]relaydeck external add <path>[/] to add one.[/]"
                )
                cands = list_candidates(self.config_home)
                for c in cands:
                    console.print(
                        f"  [yellow]detected[/] {c['kind']} at "
                        f"[cyan]{c['root']}[/] (confidence {c['confidence']:.2f}) "
                        f"— [bold]relaydeck external add {c['root']}[/]"
                    )
                return
            table = Table(title="External agents")
            table.add_column("id", style="cyan")
            table.add_column("kind")
            table.add_column("transport")
            table.add_column("root")
            table.add_column("cli")
            table.add_column("risk", style="yellow")
            for a in agents:
                probe = (a.last_probe or {}).get("health", {}) if a.last_probe else {}
                risk = ", ".join((a.last_probe or {}).get("risk", [])) if a.last_probe else "—"
                table.add_row(
                    a.id, a.kind, a.transport, a.root,
                    probe.get("cli", "?"), risk or "—",
                )
            console.print(table)

        @host.cli.command("show", help="Show one external agent in detail.")
        @click.argument("agent_id")
        def _show(agent_id: str):
            agent = store.get_agent(self.config_home, agent_id)
            if agent is None:
                console.print(f"[red]✗[/] no external agent {agent_id!r}")
                raise SystemExit(1)
            console.print_json(json.dumps(agent.to_dict()))

        @host.cli.command("health", help="Run a read-only health/posture probe.")
        @click.argument("agent_id")
        def _health(agent_id: str):
            ok, resp = _post_daemon(f"/api/plugins/external/agents/{agent_id}/probe", {})
            if ok and isinstance(resp, dict):
                _print_health(console, resp)
                return
            if _is_conn_error(resp):
                agent = store.get_agent(self.config_home, agent_id)
                if agent is None:
                    console.print(f"[red]✗[/] no external agent {agent_id!r}")
                    raise SystemExit(1)
                report = probe_and_store(self.config_home, agent)
                _print_health(console, {"id": agent_id, **report.to_dict()})
                return
            console.print(f"[red]✗[/] {resp}")
            raise SystemExit(1)

        @host.cli.command("remove", help="Unregister an external agent (runtime untouched).")
        @click.argument("agent_id")
        def _remove(agent_id: str):
            ok, resp = _delete_daemon(f"/api/plugins/external/agents/{agent_id}")
            if ok:
                console.print(f"[green]✓[/] removed [bold]{agent_id}[/]")
                return
            if _is_conn_error(resp):
                if store.delete_agent(self.config_home, agent_id):
                    console.print(
                        f"[green]✓[/] removed [bold]{agent_id}[/] "
                        f"[dim](daemon unreachable — dashboard updates on next refresh)[/]"
                    )
                    return
                console.print(f"[red]✗[/] no external agent {agent_id!r}")
                raise SystemExit(1)
            console.print(f"[red]✗[/] {resp}")
            raise SystemExit(1)

        @host.cli.command(
            "spawn",
            help="Launch a linked runtime's chat TUI as an agent in a workspace.",
        )
        @click.argument("agent_id")
        @click.option("--workspace", "-w", required=True,
                      help="Workspace to launch the chat agent in.")
        @click.option("--agent-id", "new_id", default=None,
                      help="Id for the new relaydeck agent (default: the runtime kind).")
        @click.option("--no-start", is_flag=True,
                      help="Create the agent but don't start it yet.")
        def _spawn(agent_id: str, workspace: str, new_id: str | None, no_start: bool):
            ok, resp = _post_daemon(
                f"/api/plugins/external/agents/{agent_id}/spawn",
                {"workspace": workspace, "agent_id": new_id, "start": not no_start},
            )
            if ok and isinstance(resp, dict):
                verb = "started" if resp.get("started") else "created"
                console.print(
                    f"[green]✓[/] {verb} [bold]{resp.get('agent_id')}[/] "
                    f"({resp.get('type')}) in workspace [cyan]{resp.get('workspace')}[/]"
                )
                return
            if _is_conn_error(resp):
                console.print(
                    "[red]✗[/] spawning needs the daemon running (it owns the agent "
                    "runtime). Start it with [bold]relaydeck daemon start[/]."
                )
                raise SystemExit(1)
            console.print(f"[red]✗[/] {resp}")
            raise SystemExit(1)


def _print_health(console, resp: dict) -> None:
    health = resp.get("health", {})
    risk = resp.get("risk", [])
    console.print(f"[bold]{resp.get('id', '?')}[/] — {resp.get('summary', '')}")
    for k, v in health.items():
        color = "green" if v in ("ok", "available", "reachable", "installed") else (
            "red" if v in ("missing", "error", "timeout", "closed") else "yellow"
        )
        console.print(f"  {k}: [{color}]{v}[/]")
    if risk:
        console.print(f"  risk: [yellow]{', '.join(risk)}[/]")


# ── Daemon HTTP helpers (CLI runs in a separate process) ────────────


def _is_conn_error(resp) -> bool:
    return isinstance(resp, str) and (
        "URLError" in resp or "ConnectionRefused" in resp or "OSError" in resp
        or "timed out" in resp or "Connection refused" in resp
    )


def _daemon_request(path: str, method: str, payload: dict | None) -> tuple[bool, dict | str]:
    from relaydeck.auth import read_token
    from relaydeck.state import get_daemon_ca, get_daemon_url

    base = get_daemon_url().rstrip("/")
    ctx = None
    if base.startswith("https://"):
        import ssl
        ca = get_daemon_ca()
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    headers = {"Content-Type": "application/json"}
    tok = read_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            raw = r.read()
            return True, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _post_daemon(path: str, payload: dict) -> tuple[bool, dict | str]:
    return _daemon_request(path, "POST", payload)


def _delete_daemon(path: str) -> tuple[bool, dict | str]:
    return _daemon_request(path, "DELETE", None)


PLUGIN = ExternalAgentsPlugin()


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compatibility shim for the legacy PluginRegistry loader (mirrors
    github/file_watcher). The SDK adapter is the real load path; this only
    fires if a test drives `_legacy_on_load` directly."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "events.emit", "events.subscribe", "api.register", "cli.register",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
    )
    PLUGIN.on_load(host)
