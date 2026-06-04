"""
CLI — the primary surface for relaydeck.

Every operation is natively driven from the CLI:
    relaydeck serve                    Start the daemon + dashboard
    relaydeck agent create <id> ...    Create an agent
    relaydeck agent list               List all agents
    relaydeck agent start <id>         Start an agent
    relaydeck agent stop <id>          Stop an agent
    relaydeck agent rm <id>            Delete an agent
    relaydeck agent send <id> <msg>    Send a message to an agent
    relaydeck workspace add <path>     Register a workspace
    relaydeck workspace list           List workspaces
    relaydeck workspace switch <name>  Switch active workspace
    relaydeck preset list              List model/provider presets
    relaydeck preset create <name>     Create a model preset
    relaydeck recipe list              List available recipes
    relaydeck recipe show <name>       Show a recipe's content
    relaydeck usage [agent_id]         Show usage/metering stats
    relaydeck doctor                   Self-diagnostic
    relaydeck init [path]              Register a workspace + scaffold

The CLI talks to the same orchestrator loop as the web API.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
logger = logging.getLogger(__name__)


def _get_config_home() -> Path:
    return Path.home() / ".relaydeck"


def _truthy(v: str | None) -> bool:
    """Permissive truthiness for env-var flags. `1`/`true`/`yes`/`on` → True."""
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


# Noisy access-log endpoints. Match by substring on the request line
# (e.g. `GET /api/agents?workspace=demo HTTP/1.1`); 2xx/3xx responses
# only — errors always log. SSE streams (`/api/events`, `/state/stream`)
# log once at open and aren't worth filtering. The dashboard polls
# `/api/agents` and the per-plugin state probes ~1/s per open tab; this
# accounts for ~95% of access-log volume in real sessions.
_NOISY_ACCESS_PATHS = (
    "GET /api/agents ",
    "GET /api/agents?",
    "GET /api/plugins ",
    "GET /api/plugins?",
    "GET /api/plugins/handover/state",
    "GET /api/plugins/usage-limits/state",
    "GET /api/preferences ",
    "GET /api/workers ",
    "GET /api/usage ",
    "GET /api/usage?",
    "GET /healthz ",
)


def _install_access_log_filter() -> None:
    """Filter uvicorn.access lines for routine dashboard polls.

    Uvicorn formats access records as a single string in `getMessage()`
    like `127.0.0.1:54562 - "GET /api/agents?workspace=demo HTTP/1.1" 200 OK`.
    We drop the record when the request line matches one of our known
    poll endpoints AND the status is 2xx/3xx. Anything else (POST /
    PATCH / DELETE / non-2xx) always flows through.
    """
    import logging as _logging
    import re as _re

    class _AccessFilter(_logging.Filter):
        _STATUS_RE = _re.compile(r'"\s+(\d{3})\b')

        def filter(self, record: _logging.LogRecord) -> bool:  # noqa: A003
            try:
                msg = record.getMessage()
            except Exception:
                return True
            if not any(p in msg for p in _NOISY_ACCESS_PATHS):
                return True
            m = self._STATUS_RE.search(msg)
            if not m:
                return True
            code = int(m.group(1))
            # Drop only successful polls; surface anything weird.
            return not (200 <= code < 400)

    _logging.getLogger("uvicorn.access").addFilter(_AccessFilter())


# ── Main CLI group ───────────────────────────────────────────────────


@click.group()
@click.version_option(package_name="relaydeck", prog_name="relaydeck")
def main():
    """relaydeck — local-first fleet OS for CLI coding agents.

    CLI-first. Everything you can do in the web dashboard, you can do here.
    """
    pass


# ── relaydeck attach ──────────────────────────────────────────────────────


@main.command()
@click.argument("agent_id")
@click.option("--detach-key", default="ctrl-b,d",
              help="Detach sequence (default: Ctrl-B then D, tmux-style).")
def attach(agent_id: str, detach_key: str):
    """Attach to a running agent's PTY (like `tmux attach`).

    Routes keystrokes into the agent's CLI and mirrors its output to
    your terminal. The daemon owns the PTY, so attach/detach is
    transparent — you can attach, detach, re-attach from another
    shell, watch the same session from a teammate's terminal (with
    a token), or compose this into `relaydeck workspace view`.

    The detach sequence (default Ctrl-B then D) is the only way out;
    Ctrl-C reaches the agent's CLI verbatim, which is usually what
    you want when running a harness like pi or claude. Resizing your
    local terminal forwards a resize event to the agent.

    A relaydeck-native (`type: relaydeck`) agent is a PTY harness too — its child
    is the `relaydeck chat` REPL — so attach mirrors that shared session like
    any other harness. For a fresh standalone session, use `relaydeck chat`.
    """
    from relaydeck.transports.attach import attach_main
    sys.exit(attach_main(agent_id, detach_key=detach_key))


# ── relaydeck chat ────────────────────────────────────────────────────────


@main.command("chat")
@click.argument("agent_id")
@click.option("-m", "--message", default=None,
              help="Send a single message and print the reply, then exit "
                   "(non-interactive / scriptable).")
def chat(agent_id: str, message: str | None):
    """Chat with a relaydeck-native (`type: relaydeck`) agent from the terminal.

    Posts to the daemon `/chat` endpoint, which runs a pi turn (`pi -p --mode
    json --continue`) with the operator prompt + fleet extension. Interactive
    REPL by default; `-m` sends one message and exits. When the agent is
    running, the Terminal tab shows the same pi session.

    Examples:
      relaydeck chat supervisor                 # interactive
      relaydeck chat supervisor -m "status?"    # one-shot
    """
    if message is not None:
        resp = _chat_request(agent_id, message)
        if not resp.get("ok"):
            console.print(f"[red]✗[/] {resp.get('error', 'chat failed')}")
            raise SystemExit(1)
        click.echo(resp.get("reply", ""))
        for t in (resp.get("tools") or []):
            console.print(f"[dim]→ used {', '.join(t.get('calls', []))}[/]")
        return
    _chat_repl(agent_id)


# ── relaydeck view ────────────────────────────────────────────────────────


@main.command()
@click.option("--workspace", "-w", default=None,
              help="Workspace to focus on first (defaults to active).")
def view(workspace: str | None):
    """Built-in TUI viewer for the whole fleet.

    One window, no tmux required: workspaces sidebar on the left,
    focused-agent PTY pane on the right, message tail below. Press
    Ctrl-B D to quit (tmux muscle memory). The PTY child
    keeps running in the daemon — this is a viewer, not a session.

    Documented as ONE of several viewers — `relaydeck workspace view`
    still supports the tmux and ghostty backends. `relaydeck view` is
    the built-in default.
    """
    from relaydeck.transports.view import run_view
    sys.exit(run_view(workspace=workspace))


@main.command("open")
@click.argument("path", type=click.Path(), required=False)
@click.option("--name", default=None,
              help="Workspace name when registering a new directory "
                   "(default: the directory's basename).")
@click.option("--plugin", "-p", "plugins_opt", multiple=True,
              help="Plugin to enable if registering (repeatable; "
                   "default: messaging + skills).")
@click.option("--web", is_flag=True, default=False,
              help="Open the web dashboard in a browser instead of the TUI.")
@click.option("--no-view", "no_view", is_flag=True, default=False,
              help="Ensure the workspace + daemon, print context, but don't "
                   "launch a viewer (good for scripts / the orchestrate skill).")
@click.option("--no-register", "no_register", is_flag=True, default=False,
              help="Fail instead of auto-registering an unowned directory.")
def open_cmd(path, name, plugins_opt, web, no_view, no_register):
    """Context-aware on-ramp: open a workspace and start watching it.

    The one gesture that takes you from a directory to a live command
    center. Given a PATH (default: the current directory) it:

    \b
      1. finds the workspace that owns PATH — or registers it as a new one
         (auto, unless --no-register);
      2. makes sure the daemon is up (starts it if not);
      3. opens the viewer — the built-in TUI, or the web dashboard (--web).

    \b
    relaydeck open                 # this directory → register if needed → TUI
    relaydeck open ~/code/api      # a specific repo
    relaydeck open . --web         # open the dashboard in a browser
    relaydeck open . --no-view     # just ensure workspace+daemon (scripts)

    The daemon persists after you detach — `open` is the front door, not a
    session. Inside a managed agent it still works, but consider whether you
    mean to nest a fleet (see the relaydeck-orchestrate skill).
    """
    import webbrowser

    from relaydeck.daemon import daemon_status, start_daemon
    from relaydeck.state import (
        _resolve_workspace_from_cwd,
        get_daemon_bind_host,
        get_daemon_url,
    )

    p = Path(path or ".").resolve()
    if not p.is_dir():
        console.print(f"[red]✗[/] not a directory: {p}")
        raise SystemExit(2)

    # 1. Find-or-register. Strict path ownership (cwd-ancestry), NOT the
    # durable-default fallback — an unowned dir must register, not silently
    # attach to an unrelated workspace.
    ws = _resolve_workspace_from_cwd(p)
    if ws is None:
        if no_register:
            console.print(
                f"[red]✗[/] no workspace owns {p}. "
                "Register it with [bold]relaydeck init[/] or drop --no-register."
            )
            raise SystemExit(1)
        plugins = list(plugins_opt) or ["messaging", "skills"]
        _workspace_add_impl(str(p), name, plugins)
        ws = _resolve_workspace_from_cwd(p) or (name or p.name)
    else:
        console.print(f"[dim]·[/] workspace [bold]{ws}[/] owns {p}")

    # 2. Ensure the daemon is up (idempotent).
    home = _get_config_home()
    host = get_daemon_bind_host() or "127.0.0.1"
    status = daemon_status(home, host=host, port=8765)
    if not status["running"]:
        console.print("[dim]·[/] daemon down — starting it…")
        result = start_daemon(home, host=host, port=8765, wait_seconds=5.0)
        if result.get("exited_during_startup"):
            console.print(
                f"[red]✗[/] daemon exited during startup "
                f"(rc={result.get('returncode')}). See "
                "[bold]relaydeck daemon logs[/]."
            )
            raise SystemExit(1)
        console.print(f"[green]✓[/] daemon ready (pid {result['pid']})")
    else:
        console.print(f"[dim]·[/] daemon already running ({status['state']})")

    dashboard = get_daemon_url()

    # 3. Open the viewer.
    if no_view:
        console.print(
            f"[green]✓[/] [bold]{ws}[/] ready — dashboard {dashboard}\n"
            f"  [dim]watch it with[/] relaydeck view -w {ws}  "
            f"[dim]·[/]  relaydeck open . --web"
        )
        return
    if web:
        console.print(f"[green]✓[/] opening dashboard → {dashboard}")
        try:
            webbrowser.open(dashboard)
        except Exception as exc:
            console.print(f"[yellow]·[/] couldn't launch a browser ({exc}); "
                          f"open {dashboard} yourself.")
        return
    from relaydeck.transports.view import run_view
    sys.exit(run_view(workspace=ws))


# ── relaydeck serve ───────────────────────────────────────────────────────


def _no_cache_staticfiles(directory: str):
    """A `StaticFiles` that tags responses `Cache-Control: no-cache,
    must-revalidate`.

    Used for BOTH the broad `/static` mount and every plugin's
    `/static/plugins/<name>/` mount. The browser still gets the
    ETag/Last-Modified 304 fast-path, but it revalidates on every load
    instead of serving a stale copy from its heuristic cache. Plugin
    mounts previously used a bare `StaticFiles` (no `Cache-Control`), so a
    plugin's `panel.js`/`tile.js` could keep rendering an OLD cached module
    after a daemon restart — e.g. the telegram lens still clipping its
    Plugin Settings card after a reload because the pre-fix module was
    served from cache. fastapi is imported lazily so importing this module
    (for the CLI) doesn't pull fastapi at startup.
    """
    from fastapi.staticfiles import StaticFiles

    class _NoCacheStatic(StaticFiles):
        def file_response(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp

    return _NoCacheStatic(directory=directory)


@main.command()
@click.option("--host", default=None,
              help="Bind address. Default: persisted preference from "
                   "`relaydeck daemon start --host`, else 127.0.0.1.")
@click.option("--port", default=8765, help="Bind port")
@click.option("--reload", is_flag=True, help="Enable auto-reload (dev)")
@click.option("--tls-cert", "tls_cert", default=None, type=click.Path(exists=True),
              help="Path to TLS certificate (PEM). Pair with --tls-key.")
@click.option("--tls-key", "tls_key", default=None, type=click.Path(exists=True),
              help="Path to TLS private key (PEM). Pair with --tls-cert.")
@click.option("--tls-self-signed", is_flag=True,
              help="Generate (or reuse) a localhost self-signed cert in "
                   "~/.relaydeck/runtime/tls/. Dev only.")
def serve(host: str | None, port: int, reload: bool,
          tls_cert: str | None, tls_key: str | None,
          tls_self_signed: bool):
    """Start the relaydeck daemon + web dashboard.

    Boots the orchestrator, loads all plugins, syncs agent specs,
    autostarts flagged agents, and serves the dashboard at the
    given host:port.

    TLS: pass `--tls-cert/--tls-key` for production, or
    `--tls-self-signed` for a one-line dev setup that prints the
    cert fingerprint so you can verify the dashboard URL.
    """
    import uvicorn

    from relaydeck.auth import get_or_create_token, read_token
    from relaydeck.metrics import configure_json_logging, init_builtin_series
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.plugin import PluginContext, get_registry
    from relaydeck.state import get_daemon_bind_host, set_daemon_bind_host
    from relaydeck.transports.api import create_app

    # Host resolution + preference persistence. Operators who want the
    # dashboard reachable from other devices set `--host 0.0.0.0` (or
    # a specific iface) once; that's persisted in state.yaml so future
    # restarts don't need the flag. The CLI default is None so we can
    # distinguish "operator chose 127.0.0.1" from "operator chose
    # nothing". Pass `--host 127.0.0.1` to reset to loopback.
    if host is None:
        host = get_daemon_bind_host() or "127.0.0.1"
    else:
        set_daemon_bind_host(host)

    home = _get_config_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "runtime").mkdir(exist_ok=True)

    # Observability bootstrap. JSON logs activate on
    # `RELAYDECK_LOG_FORMAT=json` — log shippers (vector / fluentbit) want
    # one event per line with structured fields. Default stays human-
    # readable text for interactive dev. Built-in Prometheus series
    # are pre-registered so /metrics has documented metrics from
    # boot even before any first event ticks a counter.
    if os.environ.get("RELAYDECK_LOG_FORMAT", "").lower() == "json":
        configure_json_logging()
    init_builtin_series()

    # Quiet the access log's poll noise. The dashboard hits a handful
    # of endpoints once per second per open tab; left raw, the log is
    # 99% repetitive 200s — 24 MB and 350k lines in a few days. Drop
    # 2xx/3xx GETs to well-known idle endpoints; keep everything else
    # (mutations, errors, redirects elsewhere, /api/events SSE).
    # Opt back into full traces with `RELAYDECK_LOG_HTTP_VERBOSE=1`.
    if not _truthy(os.environ.get("RELAYDECK_LOG_HTTP_VERBOSE")):
        _install_access_log_filter()

    # Mint a daemon token if one isn't already on disk. First-boot path
    # writes ~/.relaydeck/auth-token mode 0600; subsequent boots reuse
    # it. The CLI in this same process picks the token up from the file
    # — no env var hand-off needed.
    first_time = read_token() is None
    token = get_or_create_token()

    # Load plugins. `relaydeck/__init__.py:main()` already loaded them so the
    # CLI subcommands could be registered before click parsed argv —
    # avoid re-loading or every plugin's on_load (and its workers) fires twice.
    registry = get_registry(home)
    ctx = PluginContext(config_home=home, workspace_path=None)
    if not registry.all():
        registry.load_all(ctx)

    # Boot orchestrator
    orch = get_orchestrator(home)
    # Wire plugin event bus to orchestrator for agent lifecycle events
    if registry.event_bus:
        orch.set_event_bus(registry.event_bus)
    orch.start()

    # Make the vendor-integration registry available to /api/integrations.
    # The CLI subcommands register lazily; the daemon registers at boot so
    # the dashboard's Settings → Integrations tab can list + toggle them.
    from relaydeck import integrations as _integrations
    _integrations.register_builtin_integrations()

    # Start the DB maintenance worker — runs prune_old_events +
    # wal_checkpoint on a long interval so the relaydeck.db file stays
    # bounded on long-running daemons. Cheap when idle; no-op when
    # there's nothing to prune or checkpoint.
    _start_db_maintenance_worker(home)

    # Warm the models.dev metadata cache off the boot path so pricing +
    # capability enrichment is populated without the first request blocking
    # on the network. Fail-open: a models.dev outage never delays boot.
    import threading as _threading

    def _warm_models_dev() -> None:
        try:
            from relaydeck import models_dev
            models_dev.warm(home)
        except Exception:
            pass
    _threading.Thread(target=_warm_models_dev, name="models-dev-warm", daemon=True).start()

    # Emit workspace.added events so plugins (file-watcher, etc.) react
    if registry.event_bus:
        from relaydeck.config import load_workspace_registry
        from relaydeck.plugin import Event
        for ws in load_workspace_registry():
            registry.event_bus.emit(Event(
                type="workspace.added",
                data={"name": ws.name, "path": str(ws.path)},
                source_plugin="orchestrator",
            ))

    # Register plugin CLI commands on the main group
    _register_plugin_cli(registry, main)

    # Create FastAPI app
    app = create_app(home)

    # Register plugin API routes, mount static dirs, collect UI manifests.
    ui_manifest: dict[str, list] = {"tabs": [], "header_chips": [], "agent_tiles": [], "widgets": [], "tui": []}
    for entry in registry.all():
        try:
            entry.instance.register_api_routes(app)
        except Exception:
            pass
        # Mount the plugin's static dir if it has one. We prefer the
        # registry entry's path (always pointing at the plugin's directory)
        # over plugin.static_dir() — the latter fails when plugins assign
        # methods to a bare RelaydeckPlugin() instance instead of subclassing.
        try:
            entry_path = Path(entry.path) if entry.path else None
            candidates = []
            if entry_path:
                if entry_path.is_dir():
                    candidates.append(entry_path / "static")
                else:
                    candidates.append(entry_path.parent / "static")
            override = entry.instance.static_dir()
            if override:
                candidates.append(override)
            for static in candidates:
                if static and static.is_dir():
                    mount = f"/static/plugins/{entry.name}"
                    app.mount(mount, _no_cache_staticfiles(str(static)), name=f"plugin-{entry.name}")
                    break
        except Exception:
            pass
        try:
            manifest = entry.instance.register_ui() or {}
        except Exception:
            manifest = {}
        for tab in manifest.get("tabs", []) or []:
            t: dict[str, Any] = dict(tab)
            t["plugin"] = entry.name
            mod = t.get("module")
            if mod and not str(mod).startswith(("/", "http://", "https://")):
                t["module"] = f"/static/plugins/{entry.name}/{mod}"
            if "order" not in t:
                t["order"] = 100
            ui_manifest["tabs"].append(t)
        for chip in manifest.get("header_chips", []) or []:
            c = dict(chip)
            c["plugin"] = entry.name
            mod = c.get("module")
            if mod and not str(mod).startswith(("/", "http://", "https://")):
                c["module"] = f"/static/plugins/{entry.name}/{mod}"
            ui_manifest["header_chips"].append(c)
        for tile in manifest.get("agent_tiles", []) or []:
            tl: dict[str, Any] = dict(tile)
            tl["plugin"] = entry.name
            mod = tl.get("module")
            if mod and not str(mod).startswith(("/", "http://", "https://")):
                tl["module"] = f"/static/plugins/{entry.name}/{mod}"
            if "order" not in tl:
                tl["order"] = 100
            ui_manifest["agent_tiles"].append(tl)
        for widget in manifest.get("widgets", []) or []:
            wd: dict[str, Any] = dict(widget)
            wd["plugin"] = entry.name
            wd.setdefault("source", entry.name)
            mod = wd.get("module")
            if mod and not str(mod).startswith(("/", "http://", "https://")):
                wd["module"] = f"/static/plugins/{entry.name}/{mod}"
            ui_manifest["widgets"].append(wd)
        # Terminal-TUI tabs (`relaydeck view`). Unlike web `module`s, these
        # carry an `endpoint` the view client GETs for the tab's content —
        # mount it under the plugin's API namespace.
        for tt in manifest.get("tui", []) or []:
            d: dict[str, Any] = dict(tt)
            d["plugin"] = entry.name
            ep = str(d.get("endpoint") or "tui").strip("/")
            d["endpoint"] = f"/api/plugins/{entry.name}/{ep}"
            if "order" not in d:
                d["order"] = 100
            ui_manifest["tui"].append(d)
    ui_manifest["tabs"].sort(key=lambda t: t.get("order", 100))
    ui_manifest["agent_tiles"].sort(key=lambda t: t.get("order", 100))
    ui_manifest["tui"].sort(key=lambda t: t.get("order", 100))

    # Broad /static mount — registered AFTER plugin static dirs so that
    # `/static/plugins/<name>/<file>` requests reach the plugin-specific
    # mount first, then fall through to web/static/. The general mount
    # serves the dashboard's own modules (app.js, lenses/, tiles/, …).
    #
    # Uses the same `_no_cache_staticfiles` wrapper as the plugin mounts
    # above so dashboard assets carry `Cache-Control: no-cache,
    # must-revalidate`. Browsers still get the ETag/Last-Modified 304 fast
    # path, but they re-validate on every navigation — so a daemon restart
    # that ships new JS shows up without a manual hard-refresh.
    from relaydeck.web_runtime import web_static_dir
    web_dir = web_static_dir()
    if web_dir.is_dir():
        app.mount("/static", _no_cache_staticfiles(str(web_dir)), name="static")

    # Set orchestrator + ui manifest on app state
    app.state.orchestrator = orch
    app.state.ui_manifest = ui_manifest
    # Plugin registry so workspace API endpoints can fire plugin events
    # (workspace.added/removed/updated) on the bus.
    app.state.plugin_registry = registry

    # Resolve TLS configuration. The three knobs are mutually exclusive
    # for the cert source but not for the bind path — once we know the
    # cert + key we pass them to uvicorn the same way regardless of
    # which knob produced them.
    tls_cert_path, tls_key_path, tls_fingerprint = _resolve_tls(
        home, tls_cert, tls_key, tls_self_signed,
    )
    scheme = "https" if tls_cert_path else "http"

    # Advertise our URL to other relaydeck CLI processes via state.yaml so
    # `relaydeck workspace message` can POST against the right port. Use
    # 127.0.0.1 when bound to 0.0.0.0 — remote clients should set
    # RELAYDECK_DAEMON_URL explicitly. Records the CA path for self-signed
    # so sibling CLIs verify against it (system trust doesn't know it).
    try:
        from relaydeck.state import set_daemon_ca, set_daemon_url
        advertised_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        set_daemon_url(f"{scheme}://{advertised_host}:{port}")
        # Pin the self-signed CA explicitly; for operator-supplied certs
        # we assume they're publicly trusted (don't pin) but operators
        # can still set RELAYDECK_DAEMON_CA out-of-band for private CAs.
        if tls_self_signed and tls_cert_path:
            set_daemon_ca(str(tls_cert_path))
        elif tls_cert_path is None:
            set_daemon_ca(None)
    except Exception:
        pass

    boot_lines = [
        "[bold green]relaydeck daemon started[/]",
        f"Dashboard: [link={scheme}://{host}:{port}]{scheme}://{host}:{port}[/]",
        f"Plugins loaded: {len(registry.all())}",
    ]
    if tls_cert_path:
        boot_lines.append(f"TLS: [green]on[/] · cert {tls_cert_path}")
        if tls_fingerprint:
            boot_lines.append(
                f"SHA-256 fingerprint: [dim]{tls_fingerprint}[/]"
            )
    if first_time:
        boot_lines.append(
            "[yellow]Generated auth token[/] at ~/.relaydeck/auth-token (mode 0600).\n"
            "Other CLI processes on this machine will pick it up automatically.\n"
            f"For remote access, set RELAYDECK_AUTH_TOKEN={token[:8]}…"
        )
    console.print(Panel.fit("\n".join(boot_lines), title="relaydeck"))

    uvicorn_kwargs: dict[str, Any] = {
        "host": host, "port": port, "reload": reload, "log_level": "info",
        # SSE streams (`/api/events`, `/api/agents/{id}/state/stream`) are
        # infinite generators; without a graceful-shutdown cap, uvicorn
        # waits for them to close on SIGTERM and `relaydeck daemon stop` falls
        # through to SIGKILL after its 5s wait. Cap at 3s so SIGTERM
        # actually wins — clients reconnect transparently.
        "timeout_graceful_shutdown": 3,
    }
    if tls_cert_path and tls_key_path:
        uvicorn_kwargs["ssl_certfile"] = str(tls_cert_path)
        uvicorn_kwargs["ssl_keyfile"] = str(tls_key_path)
    try:
        uvicorn.run(app, **uvicorn_kwargs)
    finally:
        # uvicorn returns on SIGTERM/SIGINT. Tear down agent threads
        # and PTY children so they don't outlive the daemon process.
        # orch.stop() is safe to call even if start() never ran.
        try:
            orch.stop()
        except Exception:
            logger.warning("orchestrator stop failed during shutdown", exc_info=True)


# ── relaydeck daemon ──────────────────────────────────────────────────────


@main.group()
def daemon():
    """Background lifecycle wrapper around `relaydeck serve`.

    `relaydeck serve` runs in the foreground. The subcommands here detach
    the daemon so it survives the shell, plus give `stop`/`status`
    handles for it. Reach for a systemd/launchd unit if you want a
    real service manager; this is the "just give me a backgrounded
    daemon" path.
    """


@daemon.command("start")
@click.option("--host", default=None,
              help="Bind address. Default: persisted preference (set by a "
                   "previous `--host` invocation), else 127.0.0.1. Pass "
                   "`--host 127.0.0.1` explicitly to reset to loopback-only.")
@click.option("--port", default=8765, help="Bind port")
@click.option("--wait", "wait_seconds", default=5.0, type=float,
              help="Seconds to wait for /healthz before reporting")
def daemon_start(host: str | None, port: int, wait_seconds: float):
    """Spawn `relaydeck serve` detached. Idempotent — re-running while the
    daemon is alive reports the existing pid instead of double-starting."""
    from relaydeck.daemon import start_daemon
    from relaydeck.state import get_daemon_bind_host, set_daemon_bind_host

    # Same precedence as `relaydeck serve`: explicit flag > persisted pref >
    # loopback default. Persisting here means `relaydeck daemon start` and
    # `relaydeck serve` share one preference; setting once via either is
    # remembered across processes.
    if host is None:
        host = get_daemon_bind_host() or "127.0.0.1"
    else:
        set_daemon_bind_host(host)

    home = _get_config_home()
    result = start_daemon(home, host=host, port=port, wait_seconds=wait_seconds)

    if result.get("exited_during_startup"):
        console.print(
            f"[red]✗[/] daemon exited during startup "
            f"(rc={result.get('returncode')}). "
            f"Last lines of log:"
        )
        log_path = result.get("log_path")
        if log_path and Path(log_path).exists():
            tail = Path(log_path).read_text().splitlines()[-10:]
            for line in tail:
                console.print(f"  [dim]│[/] {line}")
        sys.exit(1)

    pid = result["pid"]
    if result.get("already_running"):
        console.print(f"[yellow]●[/] daemon already running (pid {pid})")
    else:
        verb = "ready" if result["healthy"] else "started (not healthy yet)"
        console.print(f"[green]✓[/] daemon {verb} (pid {pid})")
    console.print(f"  [dim]log:[/] {result['log_path']}")


@daemon.command("restart")
@click.option("--host", default=None,
              help="Bind address. Default: persisted preference, else "
                   "127.0.0.1 (same precedence as `daemon start`).")
@click.option("--port", default=8765, help="Bind port")
@click.option("--wait", "wait_seconds", default=5.0, type=float,
              help="Seconds to wait for /healthz before reporting")
@click.option("--timeout", "timeout", default=5.0, type=float,
              help="Seconds to wait for graceful stop before SIGKILL")
def daemon_restart(host: str | None, port: int, wait_seconds: float, timeout: float):
    """Stop the running daemon (if any) and start a fresh one.

    Needed after editing daemon-side Python (poller, plugin, API handlers):
    that code is loaded once at process start, so it only reloads on a real
    restart. (Static web assets are re-read from disk per request and DON'T
    need this.) Safe to run when the daemon is down — it just starts one."""
    from relaydeck.daemon import start_daemon, stop_daemon
    from relaydeck.state import get_daemon_bind_host, set_daemon_bind_host

    home = _get_config_home()

    stop = stop_daemon(home, timeout=timeout)
    if stop["was_running"]:
        if stop.get("signal") == "kill":
            console.print(
                f"[yellow]●[/] daemon (pid {stop['pid']}) didn't exit on "
                f"SIGTERM; sent SIGKILL"
            )
        else:
            console.print(f"[dim]·[/] stopped daemon (pid {stop['pid']})")

    # Same host precedence as `daemon start`.
    if host is None:
        host = get_daemon_bind_host() or "127.0.0.1"
    else:
        set_daemon_bind_host(host)

    result = start_daemon(home, host=host, port=port, wait_seconds=wait_seconds)
    if result.get("exited_during_startup"):
        console.print(
            f"[red]✗[/] daemon exited during startup "
            f"(rc={result.get('returncode')}). Last lines of log:"
        )
        log_path = result.get("log_path")
        if log_path and Path(log_path).exists():
            for line in Path(log_path).read_text().splitlines()[-10:]:
                console.print(f"  [dim]│[/] {line}")
        sys.exit(1)

    verb = "ready" if result["healthy"] else "started (not healthy yet)"
    console.print(f"[green]✓[/] daemon {verb} (pid {result['pid']})")
    console.print(f"  [dim]log:[/] {result['log_path']}")


@daemon.command("stop")
@click.option("--timeout", "timeout", default=5.0, type=float,
              help="Seconds to wait for graceful exit before SIGKILL")
def daemon_stop(timeout: float):
    """Send SIGTERM to the running daemon. Escalates to SIGKILL after
    `--timeout` seconds."""
    from relaydeck.daemon import stop_daemon
    result = stop_daemon(_get_config_home(), timeout=timeout)
    if not result["was_running"]:
        console.print("[dim]·[/] daemon was not running")
    elif result["signal"] == "kill":
        console.print(
            f"[yellow]●[/] daemon (pid {result['pid']}) didn't exit on "
            f"SIGTERM; sent SIGKILL"
        )
    else:
        console.print(f"[green]✓[/] daemon stopped (pid {result['pid']})")

    # Operator might be about to `rm -rf ~/.relaydeck`. If a vendor hook
    # integration is wired into ~/.claude/settings.json, the registration
    # outlives the script and Claude Code will fire stop-hook errors on
    # every session until cleanup. Surface it now — only one dim line, only
    # when the leak is real. Fires on ALL exit paths (clean stop, SIGKILL,
    # was-not-running) because the wipe-precondition use case includes
    # double-stop calls from wrapper scripts. Best-effort: integration probes
    # hit the user's ~/.claude/, so swallow any error (FS perms, JSON
    # corruption) to keep daemon-stop's output stable.
    try:
        from relaydeck.integrations import installed_hook_integrations
        names = installed_hook_integrations()
        if names:
            console.print(
                f"  [dim]hooks registered:[/] {', '.join(names)} "
                f"[dim]· run[/] [cyan]relaydeck integration cleanup-all[/] "
                f"[dim]before deleting ~/.relaydeck[/]"
            )
    except Exception:
        pass


@daemon.command("status")
@click.option("--host", default="127.0.0.1", help="Bind address to probe")
@click.option("--port", default=8765, help="Bind port to probe")
def daemon_status_cmd(host: str, port: int):
    """Report daemon state by probing both the PID file and /healthz.

    States:
      managed - this wrapper's pid + healthy HTTP
      foreign - no pid file but /healthz responds (a `relaydeck serve` or
                systemd / docker daemon outside our wrapper)
      sick    - pid is alive but HTTP isn't responding
      down    - neither pid nor HTTP
    """
    from relaydeck.daemon import daemon_status
    s = daemon_status(_get_config_home(), host=host, port=port)
    state = s["state"]

    if state == "down":
        console.print("[red]●[/] daemon not running")
        console.print(f"  [dim]pid file:[/] {s['pid_file']}")
        console.print("  [dim]hint:[/] [bold]relaydeck daemon start[/]")
        sys.exit(1)

    if state == "foreign":
        console.print(
            "[yellow]●[/] daemon responding on "
            f"http://{host}:{port} but no pid file"
        )
        console.print(
            "  [dim]→ started outside `relaydeck daemon start` "
            "(foreground relaydeck serve, systemd, etc.)[/]"
        )
        console.print(
            "  [dim]stop with whatever started it; "
            "`relaydeck daemon stop` won't find it[/]"
        )
        return

    # state == managed or sick
    health_tag = (
        "[green]healthy[/]" if s["healthy"]
        else "[yellow]NOT responding[/] [dim](process up but HTTP down)[/]"
    )
    console.print(f"[green]●[/] daemon running (pid {s['pid']}) {health_tag}")
    console.print(f"  [dim]log:[/]      {s['log_file']}")
    console.print(f"  [dim]pid file:[/] {s['pid_file']}")


@daemon.command("logs")
@click.option("-n", "lines", default=40, type=int,
              help="Number of trailing lines to print")
@click.option("-f", "follow", is_flag=True, help="Tail -f the daemon log")
def daemon_logs(lines: int, follow: bool):
    """Tail the daemon log file (~/.relaydeck/daemon.log)."""
    from relaydeck.daemon import log_file_path
    log_path = log_file_path(_get_config_home())
    if not log_path.exists():
        console.print(f"[dim]·[/] no log yet at {log_path}")
        return
    if follow:
        # subprocess.run because tail-f semantics are non-trivial to
        # reimplement portably and `tail` is on every supported OS.
        import subprocess
        subprocess.run(["tail", "-n", str(lines), "-f", str(log_path)])
        return
    tail = log_path.read_text().splitlines()[-lines:]
    for line in tail:
        console.print(line, markup=False, highlight=False)


def _resolve_tls(
    home: Path,
    tls_cert: str | None,
    tls_key: str | None,
    tls_self_signed: bool,
) -> tuple[Path | None, Path | None, str | None]:
    """Return (cert_path, key_path, fingerprint) given the three CLI
    knobs. Mutually exclusive validation lives here so the `serve`
    body stays linear. Returns (None, None, None) for plain HTTP.

    `fingerprint` is populated only for the self-signed path —
    operator-provided certs are assumed publicly trusted; printing a
    fingerprint there would just be noise."""
    if tls_self_signed and (tls_cert or tls_key):
        raise click.UsageError(
            "--tls-self-signed is mutually exclusive with --tls-cert/--tls-key"
        )
    if bool(tls_cert) != bool(tls_key):
        raise click.UsageError("--tls-cert and --tls-key must be passed together")

    if tls_self_signed:
        from relaydeck.tls import ensure_self_signed, fingerprint

        cert_path, key_path = ensure_self_signed(home)
        return cert_path, key_path, fingerprint(cert_path)

    if tls_cert and tls_key:
        return Path(tls_cert), Path(tls_key), None

    return None, None, None


def _start_db_maintenance_worker(home: Path) -> None:
    """Register a low-frequency worker that prunes old `events` rows
    and checkpoints the WAL. Runs every 5 minutes — long enough to
    not interfere with anything, short enough that on a busy daemon
    the WAL stays well under a megabyte between sweeps."""
    import time

    from relaydeck.automation_runs import prune_runs
    from relaydeck.db import (
        DEFAULT_BUS_RETENTION_DAYS,
        DEFAULT_EVENT_RETENTION_DAYS,
        DEFAULT_MESSAGE_RETENTION_DAYS,
        db_status,
        prune_agent_messages,
        prune_bus_events,
        prune_old_events,
        vacuum_db,
        wal_checkpoint,
    )
    from relaydeck.model_invocations import prune_invocations
    from relaydeck.workers import register_worker

    db_path = home / "runtime" / "relaydeck.db"
    # model_invocations (per worker `model` call, with truncated-but-large
    # prompt/response excerpts) and automation_runs (one row per loop tick)
    # both grow unbounded otherwise — neither was being pruned anywhere on
    # the daemon path. 30 days matches the manual `relaydeck automation
    # prune` / `prune_invocations` defaults.
    _INVOCATION_RETENTION_DAYS = 30
    _RUN_RETENTION_DAYS = 30
    # Pruning frees pages onto SQLite's freelist but never shrinks the file;
    # only VACUUM returns that space to the OS. VACUUM locks + rewrites the
    # whole file, so gate it hard: run only when reclaimable space is large
    # (≥64 MiB) AND at most once a day. A healthy small DB never trips this,
    # so the common case stays free; it's a safety valve for churned DBs.
    # `relaydeck db vacuum` is the on-demand override.
    _VACUUM_MIN_FREE_BYTES = 64 * 1024 * 1024
    _VACUUM_MIN_INTERVAL_S = 24 * 3600.0
    _vac = {"last": 0.0}  # closure state: monotonic ts of last vacuum

    def _tick(worker):
        try:
            deleted = prune_old_events(
                str(db_path), retention_days=DEFAULT_EVENT_RETENTION_DAYS,
            )
            if deleted:
                worker.log(f"pruned {deleted} events older than "
                           f"{DEFAULT_EVENT_RETENTION_DAYS}d")
        except Exception as exc:
            worker.log(f"prune failed: {exc}", level="warn")
        try:
            deleted = prune_agent_messages(
                str(db_path), retention_days=DEFAULT_MESSAGE_RETENTION_DAYS,
            )
            if deleted:
                worker.log(f"pruned {deleted} delivered/failed messages older "
                           f"than {DEFAULT_MESSAGE_RETENTION_DAYS}d")
        except Exception as exc:
            worker.log(f"message prune failed: {exc}", level="warn")
        try:
            deleted = prune_bus_events(
                str(db_path), retention_days=DEFAULT_BUS_RETENTION_DAYS,
            )
            if deleted:
                worker.log(f"pruned {deleted} bus events older than "
                           f"{DEFAULT_BUS_RETENTION_DAYS}d (acked floor)")
        except Exception as exc:
            worker.log(f"bus prune failed: {exc}", level="warn")
        try:
            deleted = prune_invocations(
                older_than_days=_INVOCATION_RETENTION_DAYS, db_path=str(db_path),
            )
            if deleted:
                worker.log(f"pruned {deleted} model invocations older than "
                           f"{_INVOCATION_RETENTION_DAYS}d")
        except Exception as exc:
            worker.log(f"invocation prune failed: {exc}", level="warn")
        try:
            deleted = prune_runs(
                older_than_days=_RUN_RETENTION_DAYS, db_path=str(db_path),
            )
            if deleted:
                worker.log(f"pruned {deleted} automation runs older than "
                           f"{_RUN_RETENTION_DAYS}d")
        except Exception as exc:
            worker.log(f"run prune failed: {exc}", level="warn")
        try:
            stats = wal_checkpoint(str(db_path), mode="TRUNCATE")
            if stats["checkpointed"]:
                worker.log(f"checkpointed {stats['checkpointed']} frames")
        except Exception as exc:
            worker.log(f"checkpoint failed: {exc}", level="warn")
        # Reclaim freelist space to the OS — but only when it's worth the
        # exclusive-lock file rewrite (≥64 MiB free) and at most once a day.
        try:
            free = db_status(db_path).get("free_bytes", 0)
            now = time.monotonic()
            if (free >= _VACUUM_MIN_FREE_BYTES
                    and now - _vac["last"] >= _VACUUM_MIN_INTERVAL_S):
                v = vacuum_db(db_path)
                _vac["last"] = now
                worker.log(
                    f"vacuumed: reclaimed {v['reclaimed_bytes'] // 1024} KB "
                    f"({v['before_bytes'] // 1024} → {v['after_bytes'] // 1024} KB)"
                )
        except Exception as exc:
            worker.log(f"vacuum failed: {exc}", level="warn")

    register_worker(
        name="db.maintenance",
        plugin="relaydeck",
        target=_tick,
        interval_s=300.0,  # 5 minutes
        config={"db_path": str(db_path), "interval_s": 300.0},
        description=(
            "Every 5 minutes, prunes expired events / delivered messages / "
            "acked bus rows and TRUNCATE-checkpoints the WAL. Reclaims "
            "freelist space via VACUUM only when ≥64 MiB is reclaimable and "
            "at most once a day. A tick logs only the counts it actually "
            "pruned — nothing pruned, nothing logged, the healthy steady state."
        ),
    )


def _register_plugin_cli(registry, cli_group: click.Group) -> None:
    """Register CLI commands from loaded plugins."""
    for entry in registry.all():
        try:
            entry.instance.register_cli(cli_group)
        except Exception:
            pass


# ── relaydeck agent ───────────────────────────────────────────────────────


@main.group()
def agent():
    """Manage agents: create, list, start, stop, send messages."""
    pass


@agent.command("create")
@click.argument("agent_id")
@click.option("--type", "-t", default="harness", help="Agent type")
@click.option("--name", "-n", help="Human-readable name")
@click.option("--workspace", "-w", help="Workspace name")
@click.option("--auto-start/--no-auto-start", default=False)
@click.option("--config", "-c", multiple=True, help="Config key=value pairs")
@click.option("--purpose", default="",
              help="One-line 'what this agent is for' — visible to peers in `relaydeck agent list`")
@click.option("--tag", "tags", multiple=True,
              help="Tag (repeatable) — peers can find this agent via `relaydeck agent find --tag <x>`")
@click.option("--system-prompt", "system_prompt", default="",
              help="Free-form text appended to the agent's system prompt at spawn")
@click.option("--system-prompt-file", "system_prompt_file",
              type=click.Path(exists=True, dir_okay=False), default=None,
              help="Read system_prompt body from a file (contents inlined into the spec)")
@click.option("--identity/--no-identity", "identity", default=True,
              help="Inject the auto identity preamble (purpose + peers) — default on")
def agent_create(agent_id: str, type: str, name: str | None, workspace: str | None,
                 auto_start: bool, config: tuple[str, ...],
                 purpose: str, tags: tuple[str, ...],
                 system_prompt: str, system_prompt_file: str | None,
                 identity: bool):
    """Create a new agent definition.

    If `--workspace` is omitted, the agent is bound to the
    workspace inferred from your cwd (or to whatever `relaydeck
    workspace set` resolved to when cwd doesn't match any
    registered workspace). Pass `--workspace ""` to create a
    workspaceless agent.
    """
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.state import get_current_workspace

    # Resolve workspace: explicit flag wins; otherwise infer from
    # cwd / state.yaml. Pass an empty string to opt out explicitly.
    if workspace is None:
        workspace = get_current_workspace() or None
        if workspace:
            console.print(
                f"[dim]· binding to workspace [bold]{workspace}[/] "
                f"(pass --workspace explicitly to override, or "
                f"--workspace='' for none)[/]"
            )
    elif workspace == "":
        workspace = None

    orch = get_orchestrator(_get_config_home())
    config_dict = {}
    for kv in config:
        if "=" in kv:
            k, v = kv.split("=", 1)
            config_dict[k] = v
        else:
            console.print(
                f"[red]✗[/] --config expects key=value, got {kv!r}"
            )
            sys.exit(2)

    if system_prompt_file is not None:
        try:
            system_prompt = Path(system_prompt_file).read_text()
        except OSError as exc:
            console.print(f"[red]✗[/] cannot read {system_prompt_file}: {exc}")
            sys.exit(1)

    try:
        orch.create_agent(
            agent_id=agent_id,
            agent_type=type,
            name=name or agent_id,
            workspace=workspace,
            config=config_dict,
            auto_start=auto_start,
            purpose=purpose,
            tags=list(tags),
            system_prompt=system_prompt,
            inject_identity_preamble=identity,
        )
    except ValueError as exc:
        # Existing id, invalid type, etc. — orchestrator gives a
        # human message; surface it verbatim instead of a traceback.
        console.print(f"[red]✗[/] {exc}")
        sys.exit(1)
    console.print(f"[green]✓[/] Agent [bold]{agent_id}[/] created")


def _stdout_isatty() -> bool:
    # Indirection point so tests can flip the "interactive" decision
    # without juggling sys.stdout. CliRunner replaces stdout with a
    # StringIO that has no isatty in any useful sense.
    return sys.stdout.isatty()


def _print_agent_spec(spec, agent_id: str) -> None:
    console.print(f"[bold]{agent_id}[/]")
    console.print(f"  purpose:  {spec.purpose or '[dim](not set)[/]'}")
    console.print("  tags:     " + (", ".join(spec.tags) if spec.tags else "[dim](none)[/]"))
    console.print(
        "  identity: "
        + ("[green]on[/]" if spec.inject_identity_preamble else "[yellow]off[/]")
        + " (auto-generated preamble)"
    )
    sp = spec.system_prompt or ""
    if sp:
        from rich.markup import escape
        console.print(f"  system_prompt ({len(sp)} chars):")
        for line in sp.splitlines()[:8]:
            # `[...]` is rich's markup delimiter; escape so a
            # system_prompt mentioning e.g. `[relay from=...]`
            # renders literally instead of being swallowed.
            console.print(f"    [dim]│[/] {escape(line)}")
        if len(sp.splitlines()) > 8:
            console.print("    [dim]│ ... (truncated; full text in YAML)[/]")
    else:
        console.print("  system_prompt: [dim](not set)[/]")


_EDITABLE_HEADER = """\
# relaydeck agent edit: {id}
#
# Edit the fields below; save+quit to apply. Lines starting with '#'
# are ignored. Quit your editor without saving (or empty the file) to
# abort. To leave $EDITOR/vi: ':wq' to save, ':q!' to abort.
#
# Read-only header (rename/recreate the agent to change these):
#   id:        {id}
#   type:      {type}
#   workspace: {workspace}
#
"""


def _editable_subset(spec) -> str:
    import yaml as _yaml
    body = _yaml.dump({
        "purpose": spec.purpose or "",
        "tags": list(spec.tags),
        "inject_identity_preamble": bool(spec.inject_identity_preamble),
        "system_prompt": spec.system_prompt or "",
    }, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return _EDITABLE_HEADER.format(
        id=spec.id, type=spec.type, workspace=spec.workspace or "(none)"
    ) + body


def _edit_spec_in_editor(spec) -> dict | None:
    """Drop into $EDITOR (defaults to vi) on the editable subset of a
    spec's YAML. Returns a dict of changed fields (purpose / tags /
    system_prompt / inject_identity_preamble) with None for unchanged
    ones. Returns None if the user aborted (no save / empty file /
    parse error)."""
    import yaml as _yaml
    template = _editable_subset(spec)
    try:
        edited = click.edit(template, extension=".yaml", require_save=True)
    except click.UsageError as exc:
        console.print(f"[red]✗[/] couldn't launch editor: {exc}")
        console.print(
            "[dim]Set $EDITOR or use --show / --purpose / "
            "--system-prompt-file flags instead.[/]"
        )
        return None
    if edited is None or not edited.strip():
        return None
    try:
        data = _yaml.safe_load(edited)
    except _yaml.YAMLError as exc:
        console.print(f"[red]✗[/] invalid YAML: {exc}")
        return None
    if not isinstance(data, dict):
        console.print("[red]✗[/] expected a YAML mapping at top level")
        return None

    updates: dict = {}
    new_purpose = data.get("purpose")
    if isinstance(new_purpose, str) and new_purpose != (spec.purpose or ""):
        updates["purpose"] = new_purpose
    new_tags_raw = data.get("tags")
    if isinstance(new_tags_raw, list):
        new_tags = [str(t) for t in new_tags_raw]
        if new_tags != list(spec.tags):
            updates["tags"] = new_tags
    new_sp = data.get("system_prompt")
    if isinstance(new_sp, str) and new_sp != (spec.system_prompt or ""):
        updates["system_prompt"] = new_sp
    new_identity = data.get("inject_identity_preamble")
    if isinstance(new_identity, bool) and new_identity != bool(spec.inject_identity_preamble):
        updates["inject_identity_preamble"] = new_identity
    return updates


@agent.command("edit")
@click.argument("agent_id")
@click.option("--purpose", default=None,
              help="Replace the agent's one-line purpose")
@click.option("--add-tag", "add_tags", multiple=True,
              help="Add a tag (repeatable)")
@click.option("--remove-tag", "rm_tags", multiple=True,
              help="Remove a tag (repeatable)")
@click.option("--set-tag", "set_tags", multiple=True,
              help="Replace the tag list outright (repeatable)")
@click.option("--system-prompt", "system_prompt", default=None,
              help="Replace the agent's system_prompt append text")
@click.option("--system-prompt-file", "system_prompt_file",
              type=click.Path(exists=True, dir_okay=False), default=None,
              help="Read system_prompt body from a file (its contents are inlined)")
@click.option("--identity/--no-identity", "identity", default=None,
              help="Toggle auto-generated identity preamble (purpose + peers)")
@click.option("--show", is_flag=True, default=False,
              help="Print current state instead of opening an editor")
def agent_edit(agent_id: str, purpose: str | None,
               add_tags: tuple[str, ...], rm_tags: tuple[str, ...],
               set_tags: tuple[str, ...],
               system_prompt: str | None,
               system_prompt_file: str | None,
               identity: bool | None,
               show: bool):
    """Update an agent's cross-orchestration meta + prompt config.

    Agents introduce themselves to peers via `--purpose` and `--tag`.
    `--system-prompt` adds free-form text to the agent's system prompt
    at next spawn (via pi/claude-code's --append-system-prompt or
    codex's model_instructions_file). `--identity` toggles the
    auto-generated "you are X, your peers..." preamble.

    Running with no flags opens $EDITOR (defaulting to vi) on the
    editable fields of the agent's YAML. `--show` prints the current
    state instead. Agents can self-introduce from inside their PTY:
      relaydeck agent edit $RELAYDECK_AGENT_ID --purpose "..." --add-tag X
    """
    from relaydeck.orchestrator import get_orchestrator

    orch = get_orchestrator(_get_config_home())
    spec = orch._load_spec(agent_id)
    if spec is None:
        console.print(f"[red]✗[/] Agent [bold]{agent_id}[/] not found")
        sys.exit(1)

    if set_tags:
        new_tags = list(set_tags)
    elif add_tags or rm_tags:
        current = list(spec.tags)
        for t in rm_tags:
            current = [x for x in current if x != t]
        for t in add_tags:
            if t not in current:
                current.append(t)
        new_tags = current
    else:
        new_tags = None  # unchanged

    # --system-prompt-file overrides --system-prompt if both passed.
    if system_prompt_file is not None:
        try:
            system_prompt = Path(system_prompt_file).read_text()
        except OSError as exc:
            console.print(f"[red]✗[/] cannot read {system_prompt_file}: {exc}")
            sys.exit(1)

    no_mutation = (purpose is None and new_tags is None
                   and system_prompt is None and identity is None)
    if no_mutation:
        if show or not _stdout_isatty():
            # Non-interactive (piped, --show, or non-TTY shell) — print
            # the current state so callers can `relaydeck agent edit X | grep`.
            _print_agent_spec(spec, agent_id)
            return
        # Interactive: drop into $EDITOR on the editable subset.
        updates = _edit_spec_in_editor(spec)
        if updates is None:
            console.print("[dim](no changes)[/]")
            return
        purpose = updates.get("purpose")
        new_tags = updates.get("tags")
        system_prompt = updates.get("system_prompt")
        identity = updates.get("inject_identity_preamble")
        if all(v is None for v in (purpose, new_tags, system_prompt, identity)):
            console.print("[dim](no changes)[/]")
            return

    orch.update_agent_meta(
        agent_id, purpose=purpose, tags=new_tags,
        system_prompt=system_prompt,
        inject_identity_preamble=identity,
    )
    console.print(f"[green]✓[/] Agent [bold]{agent_id}[/] updated")
    if purpose is not None:
        console.print(f"  purpose: {purpose or '[dim](cleared)[/]'}")
    if new_tags is not None:
        console.print("  tags:    " + (", ".join(new_tags) if new_tags else "[dim](none)[/]"))
    if system_prompt is not None:
        chars = len(system_prompt)
        console.print(
            "  system_prompt: "
            + (f"updated ({chars} chars)" if system_prompt else "[dim](cleared)[/]")
        )
    if identity is not None:
        console.print("  identity: " + ("[green]on[/]" if identity else "[yellow]off[/]"))
    if any(v is not None for v in (purpose, new_tags, system_prompt, identity)):
        console.print(
            "[dim]Note: running agents must be restarted to pick up the new prompt config.[/]"
        )


@agent.command("find")
@click.option("--tag", "tags", multiple=True,
              help="Agent must have this tag (repeatable — all must match)")
@click.option("--purpose", "purpose_re", default=None,
              help="Regex or substring to match against the purpose field")
@click.option("--workspace", "-w", default=None,
              help="Scope to one workspace (defaults to the cwd-resolved one)")
@click.option("-A", "--all-workspaces", "all_workspaces", is_flag=True, default=False,
              help="Search across every workspace (overrides cwd scope).")
def agent_find(tags: tuple[str, ...], purpose_re: str | None,
               workspace: str | None, all_workspaces: bool):
    """Discover peer agents by purpose or tag — the cross-orchestration
    lookup. Returns the same table as `relaydeck agent list` filtered to
    matches. Designed to be the first thing an agent runs when it
    needs to delegate ("who reviews PRs?").

    Defaults to the cwd-inferred workspace. Pass `-A` to search
    every workspace, or `--workspace` to target a specific one.
    """
    import re

    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.state import get_current_workspace

    orch = get_orchestrator(_get_config_home())
    agents = orch.list_agents()

    # Same workspace scoping shape as `agent list`: explicit flag >
    # --all-workspaces > cwd inference.
    if workspace:
        effective_ws = workspace
    elif all_workspaces:
        effective_ws = None
    else:
        effective_ws = get_current_workspace()

    if effective_ws:
        agents = [a for a in agents if (a.get("workspace") or "") == effective_ws]
    for tag in tags:
        agents = [a for a in agents if tag in (a.get("tags") or [])]
    if purpose_re:
        try:
            pat = re.compile(purpose_re, re.IGNORECASE)
            agents = [a for a in agents if pat.search(a.get("purpose") or "")]
        except re.error:
            pl = purpose_re.lower()
            agents = [a for a in agents if pl in (a.get("purpose") or "").lower()]

    if not agents:
        console.print("[dim]No matching agents.[/]")
        return

    table = Table(show_header=True, header_style="dim")
    table.add_column("ID", style="cyan")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Workspace")
    table.add_column("Purpose")
    table.add_column("Tags", style="dim")
    status_styles = {"running": "green", "stopped": "dim",
                     "errored": "red", "pending": "yellow"}
    for a in agents:
        s = a.get("status") or ""
        tag_str = ",".join(a.get("tags") or []) or "—"
        table.add_row(
            a["id"], a.get("type", ""),
            f"[{status_styles.get(s, '')}]{s}[/]",
            a.get("workspace") or "—",
            a.get("purpose") or "[dim]—[/]",
            tag_str,
        )
    console.print(table)


@agent.command("list")
@click.option("--status", "status_filter", default=None,
              help="Filter by status: running, stopped, errored, pending")
@click.option("--workspace", "workspace_filter", default=None,
              help="Filter to one workspace (defaults to the cwd-resolved workspace)")
@click.option("-A", "--all-workspaces", "all_workspaces", is_flag=True, default=False,
              help="Show agents across every workspace (overrides the default "
                   "cwd-scoped behavior).")
@click.option("-q", "--quiet", is_flag=True, default=False,
              help="Print just agent IDs, one per line. "
                   "Pipe-friendly: `relaydeck agent list -q --status stopped | xargs relaydeck agent start`")
def agent_list(
    status_filter: str | None,
    workspace_filter: str | None,
    all_workspaces: bool,
    quiet: bool,
):
    """List agents and their status.

    By default, lists agents in the **current workspace** — resolved
    the same way as every other workspace-scoped command:

      1. `--workspace` flag (explicit override)
      2. `RELAYDECK_WORKSPACE` env var
      3. cwd is inside a registered workspace's path
      4. `relaydeck workspace set` durable default
      5. first registered workspace

    To see every agent regardless of workspace, pass `-A` /
    `--all-workspaces`. This is rarely what you want during day-to-day
    work but handy for `relaydeck doctor`-style global health checks.

    Useful shapes:
      relaydeck agent list                        # current workspace (cwd-inferred)
      relaydeck agent list --status stopped       # current workspace, stopped only
      relaydeck agent list --workspace myapi      # explicit different workspace
      relaydeck agent list -A                     # every workspace
      relaydeck agent list -q --status stopped    # IDs only, scriptable
    """
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.state import get_current_workspace

    orch = get_orchestrator(_get_config_home())
    agents = orch.list_agents()

    # Workspace scoping. Explicit `--workspace` always wins. Otherwise
    # cwd inference picks the default. `--all-workspaces` opts out
    # entirely (`workspace_filter=None` reaches the filter step).
    if workspace_filter:
        effective_ws = workspace_filter
    elif all_workspaces:
        effective_ws = None
    else:
        effective_ws = get_current_workspace()

    if status_filter:
        agents = [a for a in agents if a.get("status") == status_filter]
    if effective_ws:
        agents = [a for a in agents if (a.get("workspace") or "") == effective_ws]

    if quiet:
        # Pure IDs, one per line — `xargs` ergonomic.
        for a in agents:
            console.print(a["id"], highlight=False)
        return

    if not agents:
        # Distinguish "no match in this scope" from "nothing defined
        # anywhere" so the operator knows whether to broaden the
        # filter or actually create an agent.
        if status_filter and effective_ws:
            console.print(
                f"[dim]No agents in workspace [bold]{effective_ws}[/] "
                f"with status [bold]{status_filter}[/]. "
                f"Try [bold]relaydeck agent list -A[/] to see other workspaces.[/]"
            )
        elif effective_ws:
            console.print(
                f"[dim]No agents in workspace [bold]{effective_ws}[/]. "
                f"Pass [bold]-A[/] to see every workspace, or create one with "
                f"[bold]relaydeck agent create <id>[/].[/]"
            )
        elif status_filter:
            console.print("[dim]No agents match those filters.[/]")
        else:
            console.print(
                "[dim]No agents defined. Create one with "
                "[bold]relaydeck agent create <id>[/].[/]"
            )
        return

    # Title carries the scope so the table is self-explaining. Without
    # this an operator could glance at `relaydeck agent list` and miss that
    # they're looking at one workspace's agents, not all of them.
    if effective_ws:
        title = f"Agents · {effective_ws}"
    else:
        title = "Agents · all workspaces"
    table = Table(title=title)
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Status")          # process-level: running/stopped/...
    table.add_column("Doing")           # semantic: working/idle/awaiting-input/...
    if not effective_ws:
        # Workspace column only useful when we're showing multiple
        # workspaces — otherwise it's the same value on every row.
        table.add_column("Workspace")
    table.add_column("Purpose")

    process_styles = {
        "running": "green",
        "stopped": "dim",
        "errored": "red",
        "pending": "yellow",
    }
    # Semantic-status colors — orthogonal to the process state.
    # `awaiting-input` is the loudest because that's the one that
    # blocks forward progress and the operator should act on.
    semantic_styles = {
        "working":          "cyan",
        "awaiting-input":   "yellow bold",
        "complete-unread":  "magenta",
        "idle":             "green",
    }

    for a in agents:
        status = a["status"]
        style = process_styles.get(status, "")
        semantic = a.get("semantic_status")
        sstyle = semantic_styles.get(semantic or "", "dim")
        sem_text = f"[{sstyle}]{semantic}[/]" if semantic else "[dim]—[/]"
        row = [
            a["id"], a["name"], a["type"],
            f"[{style}]{status}[/]",
            sem_text,
        ]
        if not effective_ws:
            row.append(a.get("workspace", "-") or "-")
        row.append(a.get("purpose") or "[dim]—[/]")
        table.add_row(*row)

    console.print(table)


# Outcomes of `_post_to_daemon`. We distinguish three cases so the
# caller can decide between "fall back to local" (transport down) and
# "surface the real error" (daemon answered with 4xx/5xx). Conflating
# these reintroduces the CLI-local spawn path that round 3 was trying
# to retire: a 409 from start verification would otherwise look the
# same as a connection-refused and we'd spawn the doomed agent in
# the CLI process anyway.
_POST_OK = "ok"
_POST_TRANSPORT_FAILED = "transport_failed"   # daemon not reachable
_POST_DAEMON_ERROR = "daemon_error"           # daemon responded 4xx/5xx


def _daemon_auth_headers() -> dict[str, str]:
    """Bearer header for CLI → daemon calls. Empty dict if no token is
    on disk yet (first-run before `relaydeck serve` has minted one). The
    daemon will 401 in that case, which is the correct behavior — we
    don't fake-auth past the boundary."""
    from relaydeck.auth import read_token
    t = read_token()
    return {"Authorization": f"Bearer {t}"} if t else {}


def _daemon_ssl_context() -> Any:
    """Build the ssl context for CLI urllib calls when the daemon URL
    is HTTPS. Reads `state.yaml.daemon_ca` (set by `relaydeck serve
    --tls-self-signed`) so the dev path verifies against the pinned
    cert; for operator-supplied certs we use the system trust store.
    Returns None for plain HTTP (urllib treats it as a no-op there)."""
    from relaydeck.state import get_daemon_ca, get_daemon_url

    if not get_daemon_url().startswith("https://"):
        return None
    import ssl
    ca = get_daemon_ca()
    if ca:
        return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


def _get_from_daemon(path: str, *, timeout: float = 5.0) -> tuple[str, Any]:
    """GET {path} on the daemon. Mirrors `_post_to_daemon` — returns
    `(outcome, payload_or_error)` where outcome is `_POST_OK`,
    `_POST_TRANSPORT_FAILED`, or `_POST_DAEMON_ERROR`.

    Used by every read-only CLI command that needs live daemon
    state (workers list / logs, gateway channels, etc.). Centralizes
    daemon URL resolution + Bearer auth + TLS context so a single
    bug doesn't bite the four call sites separately — which is
    exactly what happened with the `8777` hardcode.
    """
    import json as _json
    import urllib.error
    import urllib.request

    from relaydeck.state import get_daemon_url

    url = get_daemon_url().rstrip("/") + path
    headers = _daemon_auth_headers()
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_daemon_ssl_context()) as r:
            body = r.read()
        if not body:
            return _POST_OK, None
        try:
            return _POST_OK, _json.loads(body)
        except _json.JSONDecodeError:
            return _POST_OK, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body
        try:
            parsed = _json.loads(body)
            if isinstance(parsed, dict) and "detail" in parsed:
                detail = str(parsed["detail"])
        except (_json.JSONDecodeError, ValueError):
            pass
        return _POST_DAEMON_ERROR, f"HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return _POST_TRANSPORT_FAILED, f"{type(exc).__name__}: {exc}"


def _post_to_daemon(path: str) -> tuple[str, str | dict]:
    """POST {path} on the daemon. Returns (outcome, payload_or_error).

    `outcome` is one of `_POST_OK`, `_POST_TRANSPORT_FAILED`, or
    `_POST_DAEMON_ERROR`. The caller MUST distinguish the latter two:
    only `_POST_TRANSPORT_FAILED` warrants the CLI-local fallback;
    `_POST_DAEMON_ERROR` should be surfaced verbatim so the user sees
    the real start-verification or validation failure instead of an
    opaque "daemon unreachable" + a doomed CLI-local spawn.
    """
    import json
    import urllib.error
    import urllib.request

    from relaydeck.state import get_daemon_url

    url = get_daemon_url().rstrip("/") + path
    headers = {"Content-Type": "application/json", **_daemon_auth_headers()}
    req = urllib.request.Request(url, data=b"", method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10, context=_daemon_ssl_context()) as r:
            return _POST_OK, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        # Daemon IS reachable but rejected the call. Extract the
        # FastAPI `detail` if present, falling back to the raw body.
        body = exc.read().decode("utf-8", errors="replace")
        detail = body
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and "detail" in parsed:
                detail = str(parsed["detail"])
        except (json.JSONDecodeError, ValueError):
            pass
        return _POST_DAEMON_ERROR, f"HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return _POST_TRANSPORT_FAILED, f"{type(exc).__name__}: {exc}"


def _json_to_daemon(method: str, path: str, body: dict | None = None,
                    *, timeout: float = 30.0) -> tuple[str, Any]:
    """Send a JSON-body request (POST/PUT/DELETE) to the daemon. Returns
    `(outcome, payload_or_error)` like `_post_to_daemon` — distinguishing
    transport-down (fall back to local) from a real daemon 4xx/5xx."""
    import json
    import urllib.error
    import urllib.request

    from relaydeck.state import get_daemon_url

    url = get_daemon_url().rstrip("/") + path
    headers = {"Content-Type": "application/json", **_daemon_auth_headers()}
    data = json.dumps(body or {}).encode() if body is not None else b""
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_daemon_ssl_context()) as r:
            raw = r.read()
            return _POST_OK, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        b = exc.read().decode("utf-8", errors="replace")
        detail = b
        try:
            parsed = json.loads(b)
            if isinstance(parsed, dict) and "detail" in parsed:
                detail = str(parsed["detail"])
        except (json.JSONDecodeError, ValueError):
            pass
        return _POST_DAEMON_ERROR, f"HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return _POST_TRANSPORT_FAILED, f"{type(exc).__name__}: {exc}"


def _chat_request(agent_id: str, text: str) -> dict:
    """POST one chat turn to the relaydeck-native endpoint. Returns the parsed
    response ({ok, reply, sent, model} or {ok: False, error}). Module-level
    + named so tests can monkeypatch it without a live daemon. The timeout
    is generous because the turn waits on a full model completion."""
    import json
    import urllib.error
    import urllib.request

    from relaydeck.state import get_daemon_url

    url = get_daemon_url().rstrip("/") + "/api/plugins/relaydeck-native/chat"
    data = json.dumps({"agent_id": agent_id, "text": text}).encode()
    headers = {"Content-Type": "application/json", **_daemon_auth_headers()}
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180, context=_daemon_ssl_context()) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {exc.code}: {body}"}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"daemon unreachable: {exc}"}


def _chat_new_request(agent_id: str) -> tuple[bool, str]:
    """POST the `/new` session reset for a relaydeck-native agent. Returns
    (ok, error)."""
    outcome, resp = _post_to_daemon(
        f"/api/plugins/relaydeck-native/{agent_id}/chat/new")
    if outcome == _POST_OK:
        return True, ""
    return False, str(resp)


def _chat_repl(agent_id: str) -> None:
    """Interactive terminal chat loop against a relaydeck-native agent.

    Continues the prior conversation by default (history persists on the
    daemon); `/new` starts a fresh session."""
    console.print(
        f"[dim]Chatting with [bold]{agent_id}[/] [green](continuing session)[/]. "
        f"Type your message; [bold]/new[/] for a fresh session; /exit (or Ctrl-D) to quit.[/]"
    )
    while True:
        try:
            text = input("\033[36myou ▸\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not text:
            continue
        if text in ("/exit", "/quit", "/q"):
            return
        if text in ("/new", "/reset", "/clear"):
            ok, err = _chat_new_request(agent_id)
            if ok:
                console.print("[dim]  ✓ new session — earlier turns won't be in context anymore.[/]")
            else:
                console.print(f"[red]✗[/] {err}")
            continue
        import random
        verb = random.choice(["thinking", "pondering", "cooking", "musing", "noodling"])
        with console.status(
            f"[magenta]{agent_id}[/] is {verb}…",
            spinner="dots", spinner_style="magenta",
        ):
            resp = _chat_request(agent_id, text)
        if not resp.get("ok"):
            console.print(f"[red]✗[/] {resp.get('error', 'chat failed')}")
            continue
        console.print(f"[bold cyan]{agent_id}[/] ▸ {resp.get('reply', '')}")
        for t in (resp.get("tools") or []):
            console.print(f"[dim]  · used {', '.join(t.get('calls', []))}[/]")


def _warn_daemon_unreachable(reason: str, fallback_consequence: str) -> None:
    """Loud banner for "daemon is down, we're falling back to direct
    DB / CLI-local." Direct-DB writes can desync from a daemon that's
    actually running on another machine or a stale lock; CLI-local
    agents die with the shell. Operators have to *see* this happened —
    a `[yellow]Warning:[/]` blends in with INFO logs."""
    console.print(
        f"[bold red on default]⚠  daemon unreachable[/] "
        f"[dim]({reason})[/]"
    )
    console.print(f"   [yellow]→[/] {fallback_consequence}")
    console.print(
        "   [dim]start it with[/] [bold cyan]relaydeck daemon start[/]"
        " [dim](backgrounded)[/] [dim]or[/] [bold cyan]relaydeck serve[/]"
        " [dim](foreground)[/]"
    )


@agent.command("start")
@click.argument("agent_ids", nargs=-1, required=False)
@click.option("--status", "status_filter", default=None,
              help="Start every agent in this status (e.g. --status stopped). "
                   "Scoped to the cwd-inferred workspace unless --workspace or "
                   "--all-workspaces is passed.")
@click.option("--workspace", "workspace_filter", default=None,
              help="Used with --status to scope the batch to one workspace.")
@click.option("-A", "--all-workspaces", "all_workspaces", is_flag=True, default=False,
              help="Apply --status across every workspace (overrides cwd scope).")
def agent_start(agent_ids: tuple[str, ...], status_filter: str | None,
                workspace_filter: str | None, all_workspaces: bool):
    """Start one or more agents.

    Three calling shapes:

        relaydeck agent start alice                       # one
        relaydeck agent start alice bob carol             # several
        relaydeck agent start --status stopped            # all stopped agents
        relaydeck agent start --status stopped --workspace myapi
                                                     # all stopped in `myapi`

    Routes through the daemon HTTP so the harness PTY child is owned
    by the daemon process (which persists), not the CLI process
    (which exits the moment this command returns — taking the
    child's PTY master fd with it via SIGHUP and killing the child
    immediately).

    Fallback semantics (per-id):
      - daemon unreachable: warn, fall back to CLI-local start. The
        agent dies when this CLI exits.
      - daemon reachable but rejects: print the real reason, mark
        the id as failed, continue with the rest of the batch.

    Exit codes:
      0 — every requested agent started cleanly
      1 — at least one failed (daemon error or unknown spec); the
          ones that *did* start are still up
    """
    ids = _resolve_agent_ids(
        agent_ids, status_filter, workspace_filter, all_workspaces=all_workspaces,
    )
    if not ids:
        # _resolve_agent_ids has already exited(2) for the
        # missing-selector and conflicting-flag cases. Reaching here
        # means a valid selector matched zero rows — that's a quiet
        # "nothing to do", not a usage error.
        console.print("[dim]No agents match those filters.[/]")
        return

    failures = 0
    for agent_id in ids:
        if not _start_one(agent_id):
            failures += 1
    if failures:
        sys.exit(1)


def _start_one(agent_id: str) -> bool:
    """Start one agent. Returns True on success. Mirrors the original
    single-id `agent_start` behavior so batch invocation is just a
    loop."""
    ws_hint = _workspace_hint(agent_id)
    outcome, resp = _post_to_daemon(f"/api/agents/{agent_id}/start")
    if outcome == _POST_OK:
        console.print(f"[green]✓[/] Agent [bold]{agent_id}[/] started{ws_hint}")
        return True

    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {agent_id}: {resp}")
        return False

    # _POST_TRANSPORT_FAILED → fall back to CLI-local with a banner.
    _warn_daemon_unreachable(
        resp,
        "the agent will be spawned in THIS CLI process and die when "
        "this shell exits.",
    )
    from relaydeck.orchestrator import get_orchestrator
    orch = get_orchestrator(_get_config_home())
    try:
        orch.start_agent(agent_id)
        console.print(f"[green]✓[/] Agent [bold]{agent_id}[/] started (CLI-local){ws_hint}")
        return True
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]✗[/] {agent_id}: {e}")
        return False


def _workspace_hint(agent_id: str) -> str:
    """Return ` [workspace foo]` for `agent_id` if its workspace
    differs from the cwd-inferred default — otherwise empty.

    The point is to make cross-workspace ops visible without
    cluttering the same-workspace common case. Composing
    `relaydeck agent start test0` (in workspace demo) followed by
    `relaydeck workspace view` (cwd = relaydeck workspace) used to look
    silently correct then surprise the user; this hint surfaces
    the cross-workspace bit at start time so the next command
    obviously needs to scope to the right place."""
    try:
        from relaydeck.orchestrator import get_orchestrator
        from relaydeck.state import get_current_workspace
        orch = get_orchestrator(_get_config_home())
        row = orch.get_agent(agent_id)
        if not row:
            return ""
        agent_ws = row.get("workspace") or ""
        cwd_ws = get_current_workspace() or ""
        if agent_ws and agent_ws != cwd_ws:
            return f" [dim](workspace [bold]{agent_ws}[/])[/]"
    except Exception:
        pass
    return ""


def _resolve_agent_ids(
    explicit: tuple[str, ...],
    status_filter: str | None,
    workspace_filter: str | None,
    all_workspaces: bool = False,
) -> list[str]:
    """Resolve the set of agent ids the user wants to act on.

    The selection model is intentionally two-axis to avoid foot-guns:

      Selector  — WHICH agents:
                    explicit ids  OR  --status <s>

      Scope     — within WHICH workspace(s):
                    default        = cwd-inferred workspace
                    --workspace X  = explicit
                    -A             = every workspace

    A selector is REQUIRED for any batch. Scope flags alone do NOT
    constitute a selection — `relaydeck agent start -A` used to mean
    "start every agent everywhere", which costs real tokens with a
    single flag. Now it returns an error and the user must say
    *what* they want acted on (`--status stopped`) in addition to
    *where* (`-A`).

    Conflicts (exit 2):
      - Explicit ids combined with --status (two selectors) or -A
        (contradictory scope intent).
      - --workspace and -A together (contradictory scope).
      - Scope flags without any selector.

    Explicit ids + --workspace is NOT a conflict: agent ids are
    globally unique, so the scope is a harmless no-op rather than an
    ambiguity — the ids win and --workspace is ignored.
    """
    # 1. Conflict checks — explicit ids are self-selecting. --status is a
    #    second SELECTOR and -A is a contradictory scope, so either with
    #    explicit ids is ambiguous. --workspace, however, is a pure scope
    #    filter that's redundant once ids are given (ids are global), so
    #    we allow it and ignore it rather than erroring.
    if explicit and (status_filter or all_workspaces):
        console.print(
            "[red]✗[/] Pass either explicit agent ids OR "
            "--status/--all-workspaces, not both."
        )
        sys.exit(2)
    if workspace_filter and all_workspaces:
        console.print(
            "[red]✗[/] --workspace and --all-workspaces are mutually exclusive."
        )
        sys.exit(2)

    # 2. Explicit ids short-circuit. The user named the agents
    #    directly; no scope filtering applies. (We don't even
    #    verify the workspace they're in — if the user typed
    #    `agent start alice` they mean alice.)
    if explicit:
        return list(explicit)

    # 3. Require an explicit selector. A scope flag (-A or
    #    --workspace) alone is NOT enough — that was the
    #    `relaydeck agent start -A → started everything` foot-gun.
    if not status_filter:
        console.print(
            "[red]✗[/] No selector. Pass agent ids, or use [bold]--status[/] "
            "(e.g. `--status stopped`).\n"
            "[dim]Scope flags (--workspace, -A) only modify which workspaces "
            "the selector applies to; they don't select agents on their own.[/]"
        )
        sys.exit(2)

    # 4. Apply selector + scope.
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.state import get_current_workspace
    orch = get_orchestrator(_get_config_home())
    agents = [a for a in orch.list_agents() if a.get("status") == status_filter]

    if workspace_filter:
        effective_ws = workspace_filter
    elif all_workspaces:
        effective_ws = None  # opt-out: cross-workspace
    else:
        effective_ws = get_current_workspace()  # default: cwd

    if effective_ws:
        agents = [a for a in agents if (a.get("workspace") or "") == effective_ws]
    return [a["id"] for a in agents]


@agent.command("stop")
@click.argument("agent_ids", nargs=-1, required=False)
@click.option("--status", "status_filter", default=None,
              help="Stop every agent in this status (e.g. --status running). "
                   "Scoped to the cwd-inferred workspace unless --workspace "
                   "or --all-workspaces is passed.")
@click.option("--workspace", "workspace_filter", default=None,
              help="Used with --status to scope to one workspace.")
@click.option("-A", "--all-workspaces", "all_workspaces", is_flag=True, default=False,
              help="Apply --status across every workspace (overrides cwd scope).")
def agent_stop(agent_ids: tuple[str, ...], status_filter: str | None,
               workspace_filter: str | None, all_workspaces: bool):
    """Stop one or more running agents.

    Calling shapes mirror `agent start`:

        relaydeck agent stop alice
        relaydeck agent stop alice bob carol
        relaydeck agent stop --status running
        relaydeck agent stop --status running --workspace myapi

    Routes through the daemon HTTP — the live agent threads live
    there. A local-orchestrator stop in the CLI would only update
    the DB row, not actually SIGTERM the running PTY child.

    Same fallback split as `agent_start`: transport failure → local
    fallback (with warning); daemon error → surface verbatim and
    continue with the rest of the batch.
    """
    ids = _resolve_agent_ids(
        agent_ids, status_filter, workspace_filter, all_workspaces=all_workspaces,
    )
    if not ids:
        # _resolve_agent_ids has already exited(2) for the
        # missing-selector and conflicting-flag cases. Reaching here
        # means a valid selector matched zero rows — that's a quiet
        # "nothing to do", not a usage error.
        console.print("[dim]No agents match those filters.[/]")
        return

    failures = 0
    for agent_id in ids:
        if not _stop_one(agent_id):
            failures += 1
    if failures:
        sys.exit(1)


def _stop_one(agent_id: str) -> bool:
    ws_hint = _workspace_hint(agent_id)
    outcome, resp = _post_to_daemon(f"/api/agents/{agent_id}/stop")
    if outcome == _POST_OK:
        console.print(f"[yellow]●[/] Agent [bold]{agent_id}[/] stopped{ws_hint}")
        return True

    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {agent_id}: {resp}")
        return False

    _warn_daemon_unreachable(
        resp,
        "writing directly to the DB. If the agent is actually running "
        "in a daemon process we can't reach, this WILL NOT terminate it.",
    )
    from relaydeck.orchestrator import get_orchestrator
    orch = get_orchestrator(_get_config_home())
    try:
        orch.stop_agent(agent_id)
        console.print(f"[yellow]●[/] Agent [bold]{agent_id}[/] stopped (CLI-local){ws_hint}")
        return True
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]✗[/] {agent_id}: {e}")
        return False


@agent.command("rm")
@click.argument("agent_id")
@click.confirmation_option(prompt="Are you sure you want to delete this agent?")
def agent_rm(agent_id: str):
    """Delete an agent permanently."""
    from relaydeck.orchestrator import get_orchestrator

    orch = get_orchestrator(_get_config_home())
    try:
        orch.delete_agent(agent_id)
    except ValueError as exc:
        console.print(f"[red]✗[/] {exc}")
        sys.exit(1)
    console.print(f"[red]✗[/] Agent [bold]{agent_id}[/] deleted")


@agent.command("restart")
@click.argument("agent_ids", nargs=-1, required=False)
@click.option("--status", "status_filter", default=None,
              help="Restart every agent in this status (e.g. --status errored).")
@click.option("--workspace", "workspace_filter", default=None,
              help="Used with --status to scope to one workspace.")
@click.option("-A", "--all-workspaces", "all_workspaces", is_flag=True, default=False,
              help="Apply --status across every workspace (overrides cwd scope).")
def agent_restart(agent_ids: tuple[str, ...], status_filter: str | None,
                  workspace_filter: str | None, all_workspaces: bool):
    """Stop then start one or more agents.

    Same calling shapes as `agent start` / `agent stop`. Useful for
    picking up spec changes (purpose, system_prompt, tags) that
    only take effect at spawn, and for clearing zombies where the
    DB says `running` but the daemon has no live PTY.

    Per-id semantics: if stop succeeds and start fails the exit
    code reflects the start failure — the agent ends up stopped.
    The two phases share the same fallback rules as the underlying
    `start` / `stop` commands.
    """
    ids = _resolve_agent_ids(
        agent_ids, status_filter, workspace_filter, all_workspaces=all_workspaces,
    )
    if not ids:
        # _resolve_agent_ids has already exited(2) for the
        # missing-selector and conflicting-flag cases. Reaching here
        # means a valid selector matched zero rows — that's a quiet
        # "nothing to do", not a usage error.
        console.print("[dim]No agents match those filters.[/]")
        return

    failures = 0
    for agent_id in ids:
        # Stop first; tolerate a "not running" stop because the user
        # asked for a restart and that should still result in the
        # agent being up afterwards. Only count the START failure
        # toward the exit code.
        _stop_one(agent_id)
        if not _start_one(agent_id):
            failures += 1
    if failures:
        sys.exit(1)


@agent.command("send")
@click.argument("agent_id")
@click.argument("message")
@click.option("--role", "-r", default="user", help="Message role (user/system)")
@click.option("--from", "from_id", default=None, help="Sender id (default: RELAYDECK_AGENT_ID or user)")
def agent_send(agent_id: str, message: str, role: str, from_id: str | None):
    """Send a message to a running agent's session."""
    import os

    from relaydeck.db import open_db
    from relaydeck.messages import enqueue_workspace_messages
    from relaydeck.state import get_current_workspace

    del role  # reserved; `from` is resolved below
    workspace = get_current_workspace()
    if not workspace:
        db_path = str(_get_config_home() / "runtime" / "relaydeck.db")
        conn = open_db(db_path)
        try:
            row = conn.execute(
                "SELECT workspace FROM agents WHERE id = ?", (agent_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row or not row["workspace"]:
            console.print(
                "[red]No workspace context.[/] Set one with "
                "[bold]relaydeck workspace set <name>[/]."
            )
            raise SystemExit(2)
        workspace = row["workspace"]

    sender = (from_id or os.environ.get("RELAYDECK_AGENT_ID") or "").strip() or "user"
    payload = {"body": message, "agent": agent_id, "from": sender}
    outcome, resp = _json_to_daemon(
        "POST", f"/api/workspaces/{workspace}/messages", payload,
    )
    if outcome == _POST_OK and isinstance(resp, dict):
        if resp.get("injected"):
            console.print(
                f"[green]✓[/] injected to {agent_id}: {resp['injected'][0]}"
            )
        elif resp.get("pending"):
            console.print(
                f"[yellow]·[/] persisted (not yet delivered to PTY): "
                f"{resp['pending'][0]}"
            )
        else:
            ids = resp.get("ids") or []
            console.print(f"[green]✓[/] queued: {ids[0] if ids else '?'}")
        return
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {resp}")
        raise SystemExit(1)
    console.print(
        f"[yellow]Warning:[/] daemon unreachable ({resp}). Enqueuing."
    )
    msg_ids = enqueue_workspace_messages(
        workspace, message, from_id=sender, agent=agent_id,
    )
    if msg_ids:
        console.print(f"[green]✓[/] enqueued: {msg_ids[0]}")
        return
    console.print(
        "[red]✗[/] could not enqueue (workspace or agent not found)."
    )
    raise SystemExit(1)


@agent.command("screen")
@click.argument("agent_id")
@click.option("--cols", default=200, type=int,
              help="Render width in columns (default 200).")
@click.option("--rows", default=50, type=int,
              help="Render height in rows (default 50).")
def agent_screen(agent_id: str, cols: int, rows: int):
    """Print the agent's current screen as plain text.

    The daemon runs the agent's PTY byte history through a real
    terminal emulator (pyte) and returns the rendered grid —
    not raw ANSI. Useful for one agent to inspect another:

      \\b
      relaydeck agent screen reviewer
      → (what reviewer's TUI is currently showing)

    Exits with code 2 if the agent is unknown, 3 if the agent
    has no live PTY (process stopped), 0 on success.
    """
    outcome, payload = _get_from_daemon(
        f"/api/agents/{agent_id}/screen?cols={cols}&rows={rows}",
        timeout=5,
    )
    if outcome == _POST_OK:
        # `_get_from_daemon` parses JSON when possible. Our endpoint
        # returns plain text so the fallback path through
        # `body.decode()` already handled it for us.
        text = payload if isinstance(payload, str) else ""
        console.print(text, highlight=False, markup=False)
        return
    if outcome == _POST_DAEMON_ERROR:
        if "not found" in str(payload).lower():
            console.print(f"[red]✗[/] {payload}")
            sys.exit(2)
        if "not running" in str(payload).lower():
            console.print(f"[red]✗[/] {payload}")
            _print_last_harness_exit_hint(agent_id)
            sys.exit(3)
        console.print(f"[red]✗[/] {payload}")
        sys.exit(1)
    console.print(f"[red]✗[/] daemon unreachable: {payload}")
    sys.exit(3)


@agent.command("viewed")
@click.argument("agent_id")
def agent_viewed(agent_id: str):
    """Mark an agent's result as read — the read-transition.

    When the semantic-status engine sees an agent finish a turn it flags it
    `complete-unread` ("a result is waiting"). This command clears that to
    `idle`, the same thing the dashboard does when you focus the agent. It's
    narrow + idempotent: only `complete-unread` is cleared, so running this on a
    working / idle agent is a harmless no-op.
    """
    outcome, payload = _post_to_daemon(f"/api/agents/{agent_id}/viewed", {}, timeout=5)
    if outcome == _POST_OK:
        changed = bool(payload.get("changed")) if isinstance(payload, dict) else False
        console.print(
            f"[green]✓[/] marked [bold]{agent_id}[/] read."
            if changed else f"[dim]{agent_id} had no unread result.[/]"
        )
        return
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {payload}")
        sys.exit(2 if "not found" in str(payload).lower() else 1)
    console.print(f"[red]✗[/] daemon unreachable: {payload}")
    sys.exit(3)


def _print_last_harness_exit_hint(agent_id: str) -> None:
    """When `relaydeck agent screen` is called on a stopped agent we have
    no PTY to render, but the operator usually wants the same thing
    a screen would tell them: 'what was the last sign of life?'
    Surface the most recent harness.exit payload (return code +
    log_path) so they can chase the crash without knowing each
    harness's log layout."""
    try:
        from relaydeck.orchestrator import get_orchestrator
        orch = get_orchestrator(_get_config_home())
        events = orch.get_events(agent_id, limit=200)
    except Exception:
        return
    exits = [e for e in events if e.get("type") == "harness.exit"]
    if not exits:
        return
    last = exits[-1]
    raw = last.get("payload")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {}
    else:
        payload = raw or {}
    rc = payload.get("returncode", "?")
    log_path = payload.get("log_path")
    console.print(f"  [dim]last exit: rc={rc}[/]")
    if log_path:
        console.print(f"  [dim]log:[/] {log_path}")


@agent.command("wait")
@click.argument("agent_id")
@click.option("--status", "want_status", default=None,
              help="Wait until this semantic status is reached "
                   "(working/awaiting-input/complete-unread/idle).")
@click.option("--not-status", "want_not_status", default=None,
              help="Wait until the agent leaves this semantic status.")
@click.option("--timeout", default=300.0, type=float,
              help="Seconds to wait before giving up (default 300; "
                   "0 returns immediately if state already matches).")
def agent_wait(
    agent_id: str,
    want_status: str | None,
    want_not_status: str | None,
    timeout: float,
):
    """Wait until AGENT_ID reaches (or leaves) a semantic status.

    Exit codes:
      0  — target state reached
      1  — timeout
      2  — usage error or agent not found
      3  — transport error

    Examples:
      relaydeck agent wait reviewer --status idle --timeout 5m
      relaydeck agent wait reviewer --not-status working
      relaydeck agent wait reviewer --status awaiting-input  # block until it prompts

    Subscribes to the SSE event stream — no polling. If the
    agent is already in the target state when the command starts,
    returns immediately (exit 0) without ever opening the stream.
    """
    if want_status is None and want_not_status is None:
        console.print(
            "[red]✗[/] one of --status or --not-status is required"
        )
        sys.exit(2)
    if want_status is not None and want_not_status is not None:
        console.print(
            "[red]✗[/] --status and --not-status are mutually exclusive"
        )
        sys.exit(2)

    from relaydeck.db import SEMANTIC_STATES
    for s in (want_status, want_not_status):
        if s is not None and s not in SEMANTIC_STATES:
            console.print(
                f"[red]✗[/] invalid status {s!r}; "
                f"expected one of {list(SEMANTIC_STATES)}"
            )
            sys.exit(2)

    def _matches(current: str | None) -> bool:
        if want_status is not None:
            return current == want_status
        return current != want_not_status

    # Fast path: ask the daemon what the current state is. If it
    # already matches, return without opening a stream.
    outcome, payload = _get_from_daemon(f"/api/agents/{agent_id}", timeout=3)
    if outcome == _POST_OK and isinstance(payload, dict):
        current = payload.get("semantic_status")
        if _matches(current):
            console.print(
                f"[green]✓[/] {agent_id} already at "
                f"semantic_status={current!r}"
            )
            return
    elif outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {payload}")
        sys.exit(2 if "not found" in str(payload).lower() else 3)
    elif outcome == _POST_TRANSPORT_FAILED:
        console.print(f"[red]✗[/] daemon unreachable: {payload}")
        sys.exit(3)

    if timeout <= 0:
        console.print("[yellow]·[/] not at target state (timeout=0)")
        sys.exit(1)

    # Slow path: open the SSE stream and wait for a matching event.
    _wait_for_status(agent_id, _matches, timeout)


def _wait_for_status(
    agent_id: str,
    matches: Any,                # Callable[[str | None], bool]
    timeout: float,
) -> None:
    """Open the agent's state-stream SSE and return on the first
    matching event. Exits the CLI on timeout / transport failure."""
    import json
    import ssl
    import time as _time
    import urllib.error
    import urllib.request

    from relaydeck.auth import read_token
    from relaydeck.state import get_daemon_ca, get_daemon_url

    daemon_url = get_daemon_url().rstrip("/")
    url = f"{daemon_url}/api/agents/{agent_id}/state/stream"
    headers = {"Accept": "text/event-stream"}
    tok = read_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    ctx: ssl.SSLContext | None = None
    if daemon_url.startswith("https://"):
        ca = get_daemon_ca()
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()

    deadline = _time.monotonic() + timeout
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, context=ctx, timeout=min(timeout, 5))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        console.print(f"[red]✗[/] HTTP {exc.code}: {body}")
        sys.exit(3)
    except (urllib.error.URLError, OSError) as exc:
        console.print(f"[red]✗[/] daemon unreachable: {exc}")
        sys.exit(3)

    with resp:
        buf = ""
        for raw in resp:
            if _time.monotonic() > deadline:
                console.print(
                    f"[yellow]·[/] timed out after {timeout:g}s "
                    f"waiting for {agent_id}"
                )
                sys.exit(1)
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if buf:
                    try:
                        data = json.loads(buf)
                    except json.JSONDecodeError:
                        data = None
                    if data and matches(data.get("to")):
                        console.print(
                            f"[green]✓[/] {agent_id} → "
                            f"semantic_status={data.get('to')!r}"
                        )
                        return
                buf = ""
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                if buf:
                    buf += "\n"
                buf += line[5:].lstrip()

    # Stream ended before our match — connection dropped cleanly
    # from the server side. Treat as timeout so scripts can retry.
    console.print("[yellow]·[/] stream ended before target reached")
    sys.exit(1)


def _event_payload_obj(raw: Any) -> Any:
    """Normalize an event payload from either DB history or live bus events.

    History endpoints return the `events.payload` JSON column as text; SSE bus
    events already carry decoded objects. Keep both CLI event readers rendering
    the same readable object instead of double-encoding historical payloads.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {"_raw": raw}
    return raw or {}


@agent.command("events")
@click.argument("agent_id")
@click.option("--follow", "-f", is_flag=True, help="Follow events in real-time")
@click.option("--since", "since_id", type=int, default=0,
              help="Only show events with id > N (use a value from a "
                   "prior line to resume).")
@click.option("--type", "type_filter", default=None,
              help="Substring match on event type (e.g. 'harness.exit', "
                   "'usage', 'agent.').")
@click.option("--limit", "limit", type=int, default=50,
              help="Max events to show (non-follow mode). Default 50.")
def agent_events(agent_id: str, follow: bool, since_id: int,
                 type_filter: str | None, limit: int):
    """View events for an agent.

    Examples:
      \\b
      relaydeck agent events alice
      relaydeck agent events alice --type harness.exit       # only crashes
      relaydeck agent events alice --since 540 --type usage  # tail by id
      relaydeck agent events alice -f --type agent.          # follow status changes
    """
    from relaydeck.orchestrator import get_orchestrator

    orch = get_orchestrator(_get_config_home())

    def _matches_type(t: str | None) -> bool:
        return type_filter is None or (t is not None and type_filter in t)

    def _print_event(ev: dict) -> None:
        payload = _event_payload_obj(ev.get("payload"))
        if not payload:
            prefix = f"#{ev['id']} " if ev.get("id") else ""
            console.print(f"  [dim]{prefix}[/][cyan]{ev['type']}[/]")
        else:
            prefix = f"#{ev['id']} " if ev.get("id") else ""
            payload_str = json.dumps(payload, default=str)
            console.print(
                f"  [dim]{prefix}[/][cyan]{ev['type']}[/] {payload_str[:120]}"
            )

    if follow:
        import urllib.error
        import urllib.request

        from relaydeck.state import get_daemon_url

        if since_id:
            outcome, resp = _get_from_daemon(f"/api/agents/{agent_id}/events")
            if outcome == _POST_OK and isinstance(resp, list):
                for ev in resp[-limit:]:
                    if int(ev.get("id") or 0) > since_id and _matches_type(ev.get("type")):
                        _print_event(ev)
            elif outcome == _POST_DAEMON_ERROR:
                console.print(f"[yellow]·[/] history read failed: {resp}")

        url = (
            get_daemon_url().rstrip("/")
            + f"/api/agents/{agent_id}/events?stream=true"
        )
        req = urllib.request.Request(
            url,
            headers={"Accept": "text/event-stream", **_daemon_auth_headers()},
            method="GET",
        )
        try:
            resp = urllib.request.urlopen(req, context=_daemon_ssl_context())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            console.print(f"[red]✗[/] HTTP {exc.code}: {body}")
            raise SystemExit(1) from None
        except (urllib.error.URLError, OSError) as exc:
            console.print(
                f"[red]✗[/] can't reach the daemon ({type(exc).__name__}: {exc}). "
                "Start it with [bold]relaydeck daemon start[/]."
            )
            raise SystemExit(1) from None

        hint = f" (filter type~={type_filter!r})" if type_filter else ""
        console.print(
            f"[dim]Following events for {agent_id}{hint} via daemon SSE "
            "(Ctrl+C to stop)...[/]"
        )
        buf = ""
        try:
            for line_bytes in resp:
                line = line_bytes.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if buf:
                        try:
                            ev = json.loads(buf)
                        except (TypeError, ValueError):
                            ev = None
                        if isinstance(ev, dict) and _matches_type(ev.get("type")):
                            _print_event(ev)
                    buf = ""
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    if buf:
                        buf += "\n"
                    buf += line[5:].lstrip()
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped.[/]")
        finally:
            with contextlib.suppress(Exception):
                resp.close()
    else:
        events = orch.get_events(agent_id, since_id=since_id, limit=limit)
        events = [e for e in events if _matches_type(e.get("type"))]
        for ev in events:
            _print_event(ev)


@agent.command("unblock")
@click.argument("agent_id")
@click.option("--answer", "-a", default=None,
              help="Type this text and press Enter (answer a [y/N] / prompt).")
@click.option("--enter", "press_enter", is_flag=True, default=False,
              help="Just press Enter (e.g. 'press enter to continue').")
@click.option("--key", "key", default=None,
              help="Send one named key: enter, esc, ctrl-c, tab, up, down, "
                   "left, right, y, n, space, backspace.")
@click.option("--show/--no-show", "show", default=True,
              help="Print the agent's current screen first (default: on).")
def agent_unblock(agent_id: str, answer: str | None, press_enter: bool,
                  key: str | None, show: bool):
    """Answer a running agent that's blocked on a native prompt.

    A "trust this folder? [y/N]", an "accept terms", a "press enter to
    continue", an update notice — any native prompt stalls an unattended
    agent and breaks orchestration. This sends a response straight to its
    PTY so work continues, without attaching a terminal.

    \b
    relaydeck agent unblock alice                # show what it's stuck on
    relaydeck agent unblock alice --answer y      # type y + Enter
    relaydeck agent unblock alice --enter         # press Enter
    relaydeck agent unblock alice --key esc       # dismiss

    With no action flag it ONLY shows the screen — so a dangerous default
    is never accepted by accident; answer explicitly. To auto-answer the
    benign cases fleet-wide, enable the `autopilot` plugin.
    """
    if sum([answer is not None, press_enter, key is not None]) > 1:
        console.print("[red]✗[/] pass at most one of --answer / --enter / --key.")
        raise SystemExit(2)

    if show:
        outcome, payload = _get_from_daemon(
            f"/api/agents/{agent_id}/screen?cols=120&rows=40", timeout=5,
        )
        if outcome == _POST_OK and isinstance(payload, str):
            tail = "\n".join(payload.rstrip().splitlines()[-12:])
            console.print("[dim]── current screen (tail) ──[/]")
            console.print(tail, highlight=False, markup=False)
            console.print("[dim]───────────────────────────[/]")
        elif outcome == _POST_DAEMON_ERROR:
            console.print(f"[yellow]·[/] can't read screen: {payload}")

    if key is not None:
        body, desc = {"key": key}, f"key {key!r}"
    elif press_enter:
        body, desc = {"key": "enter"}, "Enter"
    elif answer is not None:
        body, desc = {"data": answer, "enter": True}, f"{answer!r} + Enter"
    else:
        console.print(
            "[dim]No action sent. Re-run with [bold]--answer <text>[/], "
            "[bold]--enter[/], or [bold]--key <name>[/] to unblock.[/]"
        )
        return

    outcome, resp = _json_to_daemon("POST", f"/api/agents/{agent_id}/input", body)
    if outcome == _POST_OK and isinstance(resp, dict):
        if resp.get("ok"):
            console.print(f"[green]✓[/] sent {desc} to [bold]{agent_id}[/].")
        else:
            console.print(
                f"[yellow]·[/] write to {agent_id} returned not-ok "
                "(PTY may have just closed)."
            )
        return
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {resp}")
        raise SystemExit(1)
    console.print(
        f"[red]✗[/] daemon unreachable ({resp}). "
        "Start it with [bold]relaydeck daemon start[/]."
    )
    raise SystemExit(1)


@agent.command("compact")
@click.argument("agent_id")
def agent_compact(agent_id: str):
    """Compact a running agent's context IN PLACE (KV-safer than a reset).

    Asks the harness to summarize-and-trim its conversation without killing
    the process, so the prompt prefix stays stable and the KV cache mostly
    survives — the move when an agent's context is filling (see
    [bold]relaydeck context status[/]) but you don't want to lose its session.
    If the harness has no in-place compaction, start a fresh session instead
    (`relaydeck agent restart`), after capturing work with `agent result put`.
    """
    outcome, resp = _json_to_daemon("POST", f"/api/agents/{agent_id}/compact", {})
    if outcome == _POST_OK and isinstance(resp, dict):
        if resp.get("ok"):
            console.print(
                f"[green]✓[/] sent {resp.get('command', 'compact')} to "
                f"[bold]{agent_id}[/] — context compacting in place."
            )
        else:
            console.print(
                f"[yellow]·[/] compaction not sent to {agent_id} "
                f"({resp.get('reason', 'unknown')})."
            )
        return
    if outcome == _POST_DAEMON_ERROR:
        # 422 (unsupported harness) lands here with a helpful message.
        console.print(f"[red]✗[/] {resp}")
        raise SystemExit(1)
    console.print(
        f"[red]✗[/] daemon unreachable ({resp}). "
        "Start it with [bold]relaydeck daemon start[/]."
    )
    raise SystemExit(1)


@agent.command("escalate")
@click.argument("agent_id")
@click.option("--message", "-m", default="", help="What the human needs to know.")
def agent_escalate(agent_id: str, message: str):
    """Hand an agent to a HUMAN now (the one-tap follow-up to a hold/alert).

    Emits a HITL escalation so every configured channel (Telegram, web, …)
    pings a person — the move after [bold]autopilot[/] held a prompt or
    [bold]context status[/] went critical and you want a human, not a policy.
    """
    outcome, resp = _json_to_daemon(
        "POST", f"/api/agents/{agent_id}/escalate", {"message": message},
    )
    if outcome == _POST_OK and isinstance(resp, dict):
        if resp.get("ok"):
            console.print(f"[green]✓[/] escalated [bold]{agent_id}[/] to a human.")
        else:
            console.print(
                f"[yellow]·[/] escalation not delivered for {agent_id} "
                "(no channel plugin listening?)."
            )
        return
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {resp}")
        raise SystemExit(1)
    console.print(
        f"[red]✗[/] daemon unreachable ({resp}). "
        "Start it with [bold]relaydeck daemon start[/]."
    )
    raise SystemExit(1)


@agent.command("transcript")
@click.argument("agent_id")
def agent_transcript(agent_id: str):
    """Show an exited agent's persisted last screen (crash recovery).

    The companion to `agent result` for when a worker died before handing
    anything back. Opt-in: set [bold]RELAYDECK_TRANSCRIPT_BYTES[/] (>0) so the
    daemon snapshots the PTY tail on every agent exit.
    """
    outcome, resp = _get_from_daemon(f"/api/agents/{agent_id}/transcript")
    if outcome == _POST_OK and isinstance(resp, dict):
        console.print(resp.get("transcript", ""), highlight=False, markup=False)
        return
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[yellow]·[/] {resp}")
        return
    console.print(
        f"[red]✗[/] daemon unreachable ({resp}). "
        "Start it with [bold]relaydeck daemon start[/]."
    )
    raise SystemExit(1)


# ── Agent results: durable structured hand-back ──────────────────────


def _read_body_arg(body: str | None) -> str:
    """Resolve a --body value: `-` = stdin, `@path` = file contents, else the
    literal string. Empty/omitted → empty string."""
    if body is None:
        return ""
    if body == "-":
        return sys.stdin.read()
    if body.startswith("@"):
        return Path(body[1:]).expanduser().read_text()
    return body


@agent.group("result")
def agent_result():
    """Durable structured results — the reliable "collect results" path.

    An agent hands back a result that SURVIVES its own crash (unlike PTY
    scrollback or a peer message that may never deliver). Latest-wins per
    (agent, --key). From inside a managed agent:

    \b
        relaydeck agent result put "$RELAYDECK_AGENT_ID" \\
          --summary "reviewed PR #42" --body @review.md
    """


@agent_result.command("put")
@click.argument("agent_id")
@click.option("--body", "-b", default=None,
              help="Result body. `-` reads stdin, `@path` reads a file, "
                   "else the literal text.")
@click.option("--key", "-k", default="",
              help="Sub-key / task id (latest-wins per key).")
@click.option("--status", default="ok",
              help="Free-form status: ok | error | partial (default ok).")
@click.option("--summary", "-m", default="", help="Short human summary.")
def agent_result_put(agent_id: str, body: str | None, key: str,
                     status: str, summary: str):
    """Hand back AGENT_ID's structured result (persisted + announced)."""
    text = _read_body_arg(body)
    if not text and not summary:
        console.print("[red]✗[/] provide --body and/or --summary.")
        raise SystemExit(2)
    payload = {"body": text, "key": key, "status": status, "summary": summary}
    outcome, resp = _json_to_daemon("POST", f"/api/agents/{agent_id}/result", payload)
    if outcome == _POST_OK and isinstance(resp, dict) and resp.get("ok"):
        console.print(
            f"[green]✓[/] result #{resp.get('id')} stored for "
            f"[bold]{agent_id}[/]" + (f" (key={key})" if key else "")
        )
        return
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {resp}")
        raise SystemExit(1)
    console.print(
        f"[red]✗[/] daemon unreachable ({resp}). "
        "Start it with [bold]relaydeck daemon start[/]."
    )
    raise SystemExit(1)


@agent_result.command("get")
@click.argument("agent_id")
@click.option("--key", "-k", default=None, help="Filter to a sub-key / task id.")
@click.option("--all", "show_all", is_flag=True, default=False,
              help="Show recent history, not just the latest result.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print raw JSON (for scripts / orchestrating agents).")
def agent_result_get(agent_id: str, key: str | None, show_all: bool, as_json: bool):
    """Read AGENT_ID's result(s) — the durable hand-back."""
    path = f"/api/agents/{agent_id}/result?latest={'false' if show_all else 'true'}"
    if key is not None:
        path += f"&key={key}"
    outcome, resp = _get_from_daemon(path)
    if outcome != _POST_OK or not isinstance(resp, dict):
        console.print(f"[red]✗[/] {resp}")
        raise SystemExit(1)
    rows = resp.get("results") or []
    if as_json:
        console.print(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        console.print(f"[dim]no results for {agent_id}{f' (key={key})' if key else ''}.[/]")
        return
    for r in rows:
        kshow = f" [dim]key={r['key']}[/]" if r.get("key") else ""
        console.print(
            f"[#56516f]#{r.get('id')}[/] [cyan]{r.get('status', 'ok')}[/]{kshow}"
            + (f"  [bold]{r['summary']}[/]" if r.get("summary") else "")
        )
        if r.get("body"):
            console.print(r["body"], highlight=False, markup=False)


# ── Events: emit / broadcast / tail ──────────────────────────────────


def _parse_data_pairs(pairs: tuple[str, ...]) -> dict:
    """Turn repeated `--data key=value` options into a JSON payload. Each
    value is parsed as JSON when it can be (`n=3` → int, `ok=true` → bool,
    `tags=["a"]` → list), else kept as a string — so the common case
    (`--data service=api`) needs no quoting."""
    out: dict = {}
    for raw in pairs:
        if "=" not in raw:
            raise click.BadParameter(f"--data expects key=value, got {raw!r}")
        key, _, val = raw.partition("=")
        key = key.strip()
        if not key:
            raise click.BadParameter(f"--data has an empty key in {raw!r}")
        try:
            out[key] = json.loads(val)
        except (ValueError, TypeError):
            out[key] = val
    return out


def _emit_event_to_daemon(event_type: str, payload: dict, agent_id: str | None) -> None:
    """POST one event to the daemon's /api/events/emit and report. Shared by
    `events emit` and `broadcast`."""
    body: dict = {"type": event_type, "payload": payload}
    if agent_id:
        body["agent_id"] = agent_id
    outcome, resp = _json_to_daemon("POST", "/api/events/emit", body)
    if outcome == _POST_OK and isinstance(resp, dict):
        console.print(
            f"[green]✓[/] emitted [cyan]{resp.get('type', event_type)}[/] "
            f"(id #{resp.get('id', '?')}, from "
            f"{resp.get('agent_id', agent_id or 'operator')})"
        )
        return
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] daemon rejected emit: {resp}")
    else:
        console.print(
            f"[red]✗[/] daemon unreachable ({resp}). "
            "Start it with [bold]relaydeck daemon start[/]."
        )
    raise SystemExit(1)


@main.group("events")
def events_group():
    """Emit and tail events on the live orchestration stream.

    The stream is the same firehose the web dashboard, the `view` TUI, and
    `agent events` consume — `emit` / `broadcast` write to it, `tail`
    follows the whole fleet's events live.
    """


@events_group.command("emit")
@click.argument("event_type")
@click.option("--data", "data_pairs", multiple=True, metavar="KEY=VALUE",
              help="Structured payload field (repeatable). Value parsed as "
                   "JSON when possible, else kept as a string.")
@click.option("--message", "-m", default=None,
              help="Shorthand for --data message=<text>.")
@click.option("--agent", "agent_id", default=None,
              help="Emitter label for the event "
                   "(default: $RELAYDECK_AGENT_ID, else 'operator').")
def events_emit(event_type: str, data_pairs: tuple[str, ...],
                message: str | None, agent_id: str | None):
    """Emit a custom event TYPE onto the live stream.

    \b
    relaydeck events emit deploy.started --data service=api --data version=2.3
    relaydeck events emit build.failed -m "tsc errors in web/"
    """
    import os
    payload = _parse_data_pairs(data_pairs)
    if message is not None:
        payload["message"] = message
    agent_id = agent_id or os.environ.get("RELAYDECK_AGENT_ID")
    _emit_event_to_daemon(event_type, payload, agent_id)


@events_group.command("tail")
@click.option("--type", "type_filter", default=None,
              help="Substring match on event type (e.g. 'agent.', 'deploy').")
@click.option("--agent", "agent_filter", default=None,
              help="Only show events whose agent_id matches.")
@click.option("--limit", "limit", type=int, default=30,
              help="One-shot mode (--agent, no -f): most recent N events.")
@click.option("-f", "--follow", "follow", is_flag=True, default=False,
              help="Stream events live over SSE. Ctrl-C to stop.")
def events_tail(type_filter: str | None, agent_filter: str | None,
                limit: int, follow: bool):
    """Tail the whole fleet's event stream.

    Without -f, prints recent history (requires --agent — there is no
    global history endpoint) and exits; with -f, follows the live
    firehose the dashboard and `view` TUI watch.
    """
    import urllib.error
    import urllib.request

    from relaydeck.state import get_daemon_url

    def _show(ev: dict) -> None:
        t = ev.get("type")
        if type_filter and (t is None or type_filter not in t):
            return
        aid = ev.get("agent_id") or ev.get("agent") or ""
        if agent_filter and aid != agent_filter:
            return
        payload = _event_payload_obj(ev.get("payload"))
        try:
            ps = json.dumps(payload, default=str)
        except (TypeError, ValueError):
            ps = str(payload)
        idp = f"#{ev['id']} " if ev.get("id") else ""
        who = f"[magenta]{aid}[/] " if aid else ""
        console.print(f"  [dim]{idp}[/]{who}[cyan]{t}[/] {ps[:140]}")

    if not follow:
        if not agent_filter:
            console.print(
                "[dim]No global event history; use [bold]-f[/] to follow live, "
                "or pass [bold]--agent <id>[/] for one agent's history.[/]"
            )
            return
        outcome, resp = _get_from_daemon(f"/api/agents/{agent_filter}/events")
        if outcome == _POST_OK and isinstance(resp, list):
            for ev in resp[-limit:]:
                _show(ev)
            return
        console.print(f"[yellow]·[/] {resp}")
        return

    url = get_daemon_url().rstrip("/") + "/api/events"
    req = urllib.request.Request(
        url,
        headers={"Accept": "text/event-stream", **_daemon_auth_headers()},
        method="GET",
    )
    console.print("[dim]Following fleet events (Ctrl+C to stop)...[/]")
    try:
        resp = urllib.request.urlopen(req, context=_daemon_ssl_context())
    except (urllib.error.URLError, OSError) as exc:
        console.print(
            f"[red]✗[/] can't reach the daemon ({type(exc).__name__}: {exc}). "
            "Start it with [bold]relaydeck daemon start[/]."
        )
        raise SystemExit(1)
    buf = ""
    try:
        for line_bytes in resp:
            line = line_bytes.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if buf:
                    try:
                        _show(json.loads(buf))
                    except (TypeError, ValueError):
                        pass
                buf = ""
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                if buf:
                    buf += "\n"
                buf += line[5:].lstrip()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/]")
    finally:
        with contextlib.suppress(Exception):
            resp.close()


@main.command("broadcast")
@click.argument("message")
@click.option("--type", "event_type", default="operator.broadcast",
              help="Event type to emit (default: operator.broadcast).")
@click.option("--data", "data_pairs", multiple=True, metavar="KEY=VALUE",
              help="Extra structured fields (repeatable, JSON-coerced).")
@click.option("--agent", "agent_id", default=None,
              help="Emitter label (default: $RELAYDECK_AGENT_ID, else 'operator').")
def broadcast(message: str, event_type: str, data_pairs: tuple[str, ...],
              agent_id: str | None):
    """Broadcast a one-line MESSAGE to the whole fleet's event stream.

    A friendly wrapper over `events emit`: announces an ambient event the
    dashboard, the `view` TUI, and `events tail` all see. This is NOT inbox
    delivery — to push text into an agent's session use
    [bold]relaydeck agent send[/] / [bold]relaydeck workspace message[/].
    """
    import os
    payload = _parse_data_pairs(data_pairs)
    payload["message"] = message
    agent_id = agent_id or os.environ.get("RELAYDECK_AGENT_ID")
    _emit_event_to_daemon(event_type, payload, agent_id)


def _workspace_add_impl(path: str, name: str | None, plugins: list[str]) -> None:
    """Implementation shared by `workspace add` and `init`."""

    p = Path(path).resolve()
    ws_name = name or p.name

    # Write workspace entry to config.toml
    home = _get_config_home()
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.toml"

    import tomllib
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception:
        data = {}

    workspaces = data.get("workspace", [])
    if any(w["name"] == ws_name for w in workspaces):
        console.print(f"[yellow]⚠[/] Workspace [bold]{ws_name}[/] already registered")
        return

    workspaces.append({"name": ws_name, "path": str(p), "plugins": plugins})
    data["workspace"] = workspaces

    import tomli_w
    config_path.write_text(tomli_w.dumps(data))

    # Create workspace state dir
    ws_state = home / "workspaces" / ws_name
    ws_state.mkdir(parents=True, exist_ok=True)

    # Write agent.toml
    plugin_list = "\n".join(f'  "{pl}",' for pl in plugins)
    ws_state.joinpath("agent.toml").write_text(
        f"[workspace]\nplugins = [\n{plugin_list}\n]\n"
        if plugins else "[workspace]\nplugins = []\n"
    )

    console.print(f"[green]✓[/] Workspace [bold]{ws_name}[/] registered at {p}")
    if plugins:
        console.print(f"    Plugins: [bold]{', '.join(plugins)}[/]")


# ── relaydeck workspace ───────────────────────────────────────────────────


@main.group()
def workspace():
    """Manage workspaces."""
    pass


@workspace.command("view")
@click.option("--workspace", "ws_override", default=None,
              help="Override active workspace.")
@click.option("--viewer", "viewer_name", default=None,
              help="Terminal viewer to use: tmux | ghostty | print | "
                   "<plugin-registered>. Default: auto-detect (tmux preferred).")
@click.option("--list-viewers", "list_viewers", is_flag=True, default=False,
              help="Print the registered viewers and their availability.")
@click.option("--session", "session_name", default=None,
              help="Viewer-specific session/window name. tmux uses it as "
                   "the session id (default: relaydeck-<workspace>).")
@click.option("--print-only", is_flag=True, default=False,
              help="Print the viewer's launch commands without running "
                   "them. Useful for scripting or copy-paste setups.")
@click.option("--include-stopped", is_flag=True, default=False,
              help="Open panes for stopped agents too (default: running only).")
@click.option("--force", is_flag=True, default=False,
              help="Force-rebuild any existing session/window the viewer "
                   "manages. Viewer-specific (tmux kills + recreates).")
@click.option("--save", "save_name", default=None,
              help="After launching, save the resolved flags as a named "
                   "layout. Restore with --restore <name>.")
@click.option("--restore", "restore_name", default=None,
              help="Re-launch a previously saved layout. Other flags "
                   "override the saved values per-call.")
def workspace_view(
    ws_override: str | None,
    viewer_name: str | None,
    list_viewers: bool,
    session_name: str | None,
    print_only: bool,
    include_stopped: bool,
    force: bool,
    save_name: str | None,
    restore_name: str | None,
):
    """Open a multi-pane view of a workspace's running agents.

    One pane per running agent (each runs `relaydeck attach <id>`)
    plus a pane tailing the message bus. The layout engine is
    pluggable: tmux, Ghostty, anything that registers via
    `host.viewers.register(...)`.

    Auto-detect order (highest first): the --viewer flag, the
    RELAYDECK_VIEWER env var, then the first registered viewer that
    reports `is_available() == True` (tmux is preferred, then
    Ghostty, then the print fallback). List what's available with
    [bold]--list-viewers[/].

    Sync invariant: every viewer just composes `relaydeck attach <id>`
    calls. The daemon broadcasts each PTY's bytes to every
    connected client, so a tmux pane, a Ghostty window, and the
    web dashboard's terminal panel — all attached to the same
    agent — see identical streams in real time. Pick whichever
    layout fits your workflow.
    """
    import os
    from relaydeck.config import load_workspace_registry
    from relaydeck.state import get_current_workspace
    from relaydeck.transports import viewers as viewers_mod

    # Make sure built-in viewers are registered before we look at
    # the registry. Idempotent so it's safe to call every command.
    viewers_mod.register_builtin_viewers()
    # Plugin-contributed viewers register at plugin load time, but
    # one-shot CLI commands don't always go through serve's load
    # path; trigger discovery so `host.viewers.register` calls fire.
    _ensure_plugins_loaded()

    if list_viewers:
        _print_viewer_list(viewers_mod.all_viewers())
        return

    # --restore: seed flags from the saved layout, then let any
    # explicit flag override. The override semantics let an
    # operator say "same layout but tmux instead of ghostty" with
    # `--restore mine --viewer tmux`.
    if restore_name:
        from relaydeck import layouts as layouts_mod
        layout = layouts_mod.load(restore_name)
        if layout is None:
            console.print(
                f"[red]✗[/] No saved layout named [bold]{restore_name}[/]. "
                f"List with [bold]relaydeck layout list[/]."
            )
            sys.exit(2)
        ws_override = ws_override or layout.workspace
        viewer_name = viewer_name or layout.viewer
        session_name = session_name or layout.session
        # Bool flags: an explicit True from the CLI wins; we
        # only fall back to the saved value if the CLI didn't set
        # it. (click defaults to False for is_flag, so we can't
        # tell "explicit False" from "default" — that's fine here,
        # the user just re-runs with the flag if they need it.)
        include_stopped = include_stopped or layout.include_stopped
        force = force or layout.force

    ws_name = ws_override or get_current_workspace()
    if not ws_name:
        console.print(
            "[red]No active workspace.[/] Set one with "
            "[bold]relaydeck workspace set <name>[/] or pass [bold]--workspace[/]."
        )
        sys.exit(2)

    if ws_name not in {w.name for w in load_workspace_registry()}:
        console.print(f"[red]Workspace not registered:[/] {ws_name}")
        sys.exit(2)

    agents = _list_agents_for_workspace(ws_name, include_stopped=include_stopped)
    if not agents:
        if include_stopped:
            console.print(f"[yellow]No agents in workspace [bold]{ws_name}[/].[/]")
        else:
            console.print(
                f"[yellow]No running agents in workspace [bold]{ws_name}[/].[/]\n"
                f"[dim]Pass [bold]--include-stopped[/] to open panes for "
                f"stopped agents too.[/]"
            )
        _suggest_other_workspaces_with_running_agents(ws_name)
        sys.exit(1)

    # Pick a viewer. Resolution order:
    #   1. --viewer flag                     (per-command)
    #   2. RELAYDECK_VIEWER env var               (per-shell)
    #   3. plugin setting workspace-view.default_viewer  (durable; not wired yet)
    #   4. auto-detect                       (tmux > ghostty > print)
    requested = viewer_name or os.environ.get("RELAYDECK_VIEWER")
    viewer = _select_viewer(requested, viewers_mod)
    if viewer is None:
        # Requested viewer explicitly missing — print the registry
        # so the operator knows their options instead of just
        # erroring out.
        available = [v.name for v in viewers_mod.all_viewers() if v.is_available()]
        console.print(
            f"[red]✗[/] viewer [bold]{requested}[/] is not registered or not available.\n"
            f"[dim]Available now:[/] {', '.join(available) or '(none)'}"
        )
        sys.exit(2)

    session = session_name or f"relaydeck-{ws_name}"
    ctx = viewers_mod.ViewerContext(
        session_name=session,
        workspace=ws_name,
        agents=agents,
        attach_command_for=lambda aid: f"relaydeck attach {aid}",
        inbox_command=f"relaydeck workspace inbox -f --full --workspace {ws_name}",
        print_only=print_only,
        force=force,
    )
    result = viewer.launch(ctx)

    if not result.success:
        console.print(f"[red]✗[/] {result.error}")
        if result.attach_command:
            console.print(f"[dim]Existing layout:[/] [bold]{result.attach_command}[/]")
        sys.exit(1)

    if result.message:
        console.print(result.message)
    if result.attach_command and not print_only:
        # Direct viewers (Ghostty windows already open) don't have
        # an attach_command to follow up with — only print this
        # affordance when the viewer actually returned one.
        console.print(
            f"[dim]Attach with:[/] [bold]{result.attach_command}[/]"
        )

    # --save: persist the resolved configuration. We save AFTER
    # a successful launch on purpose — a layout that's never
    # actually worked shouldn't earn a slot in `relaydeck layout
    # list`.
    if save_name:
        from relaydeck import layouts as layouts_mod
        layout = layouts_mod.Layout(
            name=save_name,
            workspace=ws_name,
            viewer=viewer.name,
            session=session_name,
            include_stopped=include_stopped,
            force=force,
            agents=[a.id for a in agents],
        )
        try:
            path = layouts_mod.save(layout)
            console.print(f"[green]✓[/] saved layout [bold]{save_name}[/] → {path}")
        except Exception as exc:
            console.print(f"[red]✗[/] could not save layout: {exc}")


def _select_viewer(requested: str | None, viewers_mod: Any) -> Any | None:
    """Resolve the user's viewer choice. Explicit `requested` name
    bypasses auto-detect; we still verify availability there because
    a typo'd `--viewer foo` shouldn't pretend to succeed."""
    if requested:
        v = viewers_mod.get(requested.strip())
        if v is None:
            return None
        try:
            if not v.is_available():
                return None
        except Exception:
            return None
        return v
    return viewers_mod.auto_detect()


def _print_viewer_list(viewers: list) -> None:
    """`--list-viewers` output. One row per registered viewer with
    its availability and description so operators can pick."""
    table = Table(title="Workspace viewers")
    table.add_column("Name", style="cyan")
    table.add_column("Available", style="green")
    table.add_column("Description")
    for v in viewers:
        try:
            avail = "✓" if v.is_available() else "[dim]✗[/]"
        except Exception:
            avail = "[red]error[/]"
        table.add_row(v.name, avail, getattr(v, "description", ""))
    console.print(table)


def _ensure_plugins_loaded() -> None:
    """One-shot CLI commands like `workspace view` need plugin-
    contributed viewers / harnesses available, but `relaydeck serve`
    is what normally loads plugins. This helper triggers plugin
    discovery on demand so a `--viewer kitty-from-some-plugin`
    call from a fresh shell still finds the right registrar."""
    try:
        from relaydeck.plugin import PluginContext, get_registry
        reg = get_registry(_get_config_home())
        if not reg.all():
            reg.load_all(PluginContext(config_home=_get_config_home()))
    except Exception:
        # Plugin load failure shouldn't take down the CLI command —
        # the built-in viewers still cover the common case.
        pass


def _list_agents_for_workspace(ws_name: str, *, include_stopped: bool) -> list[dict]:
    """Return one dict per agent in the workspace. Prefers the daemon
    (HTTP) for live status; falls back to the local orchestrator DB
    when the daemon isn't reachable so `--print-only` still works
    offline."""
    import json
    import urllib.error
    import urllib.request

    from relaydeck.state import get_daemon_url

    url = get_daemon_url().rstrip("/") + "/api/agents"
    req = urllib.request.Request(url, headers=_daemon_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=3, context=_daemon_ssl_context()) as r:
            rows = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        # Fall back to the local orchestrator (read-only path).
        from relaydeck.orchestrator import get_orchestrator
        rows = get_orchestrator().list_agents()

    out = []
    for r in rows:
        if (r.get("workspace") or "") != ws_name:
            continue
        if not include_stopped and (r.get("status") or "stopped") != "running":
            continue
        out.append(r)
    return out


def _suggest_other_workspaces_with_running_agents(skip_ws: str) -> None:
    """When `workspace view` finds nothing in the cwd workspace,
    check other workspaces for running agents and point the user at
    them. The classic "I ran `agent start test0`, then `workspace
    view` here, but the agent lives in workspace demo" trap.

    Silent no-op if nothing else is running — we don't want to be
    chatty when the user genuinely has no agents up anywhere."""
    try:
        from relaydeck.orchestrator import get_orchestrator
        orch = get_orchestrator(_get_config_home())
        elsewhere: dict[str, list[str]] = {}
        for a in orch.list_agents():
            ws = a.get("workspace") or ""
            if not ws or ws == skip_ws:
                continue
            if (a.get("status") or "stopped") == "running":
                elsewhere.setdefault(ws, []).append(a["id"])
    except Exception:
        return

    if not elsewhere:
        return

    # Build a compact "ws → ids" summary. Cap each list at ~5 ids
    # to keep the hint readable on a small terminal.
    lines = []
    for ws, ids in sorted(elsewhere.items()):
        shown = ", ".join(ids[:5])
        if len(ids) > 5:
            shown += f", +{len(ids) - 5} more"
        lines.append(f"  · [bold]{ws}[/] → {shown}")

    console.print(
        "\n[dim]Running agents in other workspaces:[/]\n"
        + "\n".join(lines)
        + "\n[dim]Run with [bold]--workspace <name>[/] to view those, "
        "or [bold]cd[/] into the workspace's directory.[/]"
    )


def _build_tmux_recipe(session: str, ws_name: str, agents: list[dict]) -> list[list[str]]:
    """Compatibility shim — the real recipe builder lives with the
    tmux viewer in `relaydeck.transports.viewers.tmux`. This wrapper
    keeps the function importable for the existing test suite
    (which pins the recipe shape) and any third-party caller that
    constructed an argv list by hand before the viewer refactor."""
    from relaydeck.transports.viewers import ViewerContext
    from relaydeck.transports.viewers.tmux import build_recipe as _build

    return _build(ViewerContext(
        session_name=session,
        workspace=ws_name,
        agents=agents,
        attach_command_for=lambda aid: f"relaydeck attach {aid}",
        inbox_command=f"relaydeck workspace inbox -f --full --workspace {ws_name}",
    ))


@workspace.command("list")
def workspace_list():
    """List registered workspaces.

    Marks the active workspace with `●` and shows how it was
    resolved (cwd / env / set / registry-default) so an operator
    can tell at a glance why a given workspace is in scope.

    The Health column rolls every workspace's agents into one
    state: `errored` > `awaiting-input` > `working` >
    `complete-unread` > `idle` > `stopped` > `empty`. The most
    attention-demanding child wins so a fleet scan immediately
    surfaces the alarm.
    """
    from relaydeck.config import load_workspace_registry
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.state import resolve_workspace_source
    from relaydeck.workspace_health import HEALTH_STYLES, roll_up

    workspaces = load_workspace_registry()
    if not workspaces:
        console.print(
            "[dim]No workspaces registered. Use "
            "[bold]relaydeck workspace add <path>[/] or [bold]relaydeck init <path>[/].[/]"
        )
        return

    active_name, active_source = resolve_workspace_source()
    orch = get_orchestrator(_get_config_home())
    all_agents = orch.list_agents()
    by_ws: dict[str, list[dict]] = {}
    for a in all_agents:
        by_ws.setdefault(a.get("workspace") or "", []).append(a)

    table = Table(title="Workspaces")
    table.add_column("", style="green")
    table.add_column("Name", style="cyan")
    table.add_column("Health")
    table.add_column("Path")
    table.add_column("Plugins")
    table.add_column("", style="dim")  # source-of-active label

    source_label = {
        "env": "via $RELAYDECK_WORKSPACE",
        "cwd": "inferred from cwd",
        "state": "via `workspace set`",
        "registry-default": "registry default",
        "unset": "",
    }.get(active_source, "")

    for w in workspaces:
        plugins_str = ", ".join(w.plugins) if w.plugins else "—"
        is_active = w.name == active_name
        marker = "●" if is_active else ""
        why = source_label if is_active else ""
        health = roll_up(by_ws.get(w.name, []))
        health_style = HEALTH_STYLES.get(health, "dim")
        health_cell = f"[{health_style}]{health}[/]"
        table.add_row(
            marker, w.name, health_cell, str(w.path), plugins_str, why,
        )
    console.print(table)


@workspace.command("set")
@click.argument("name")
def workspace_set(name: str):
    """Set the durable default workspace for this machine.

    Persists to ~/.relaydeck/state.yaml. The full resolution
    order (highest first) is:

      1. `--workspace` flag (per-command)
      2. `RELAYDECK_WORKSPACE` env var (per-shell)
      3. cwd is inside a registered workspace's path — git-style
      4. `relaydeck workspace set <name>` (THIS command; durable default)
      5. first workspace in registry

    A `cd` into a registered workspace's directory therefore
    *overrides* what you set here — that's intentional, and matches
    how `git` uses the nearest `.git` directory. Use `workspace set`
    for the case where cwd doesn't tell relaydeck anything (a fresh shell
    in `/tmp`, scripts run from cron, etc.).
    """
    from relaydeck.config import load_workspace_registry
    from relaydeck.state import set_current_workspace

    workspaces = load_workspace_registry()
    if not any(w.name == name for w in workspaces):
        console.print(
            f"[yellow]Warning:[/] no registered workspace named [bold]{name}[/]. "
            "Saving anyway — register with `relaydeck workspace add <path> --name " + name + "`."
        )
    set_current_workspace(name)
    console.print(f"[green]✓[/] Active workspace: [bold]{name}[/]")


@workspace.command("add")
@click.argument("path", type=click.Path(exists=True))
@click.option("--name", "-n", help="Workspace name (defaults to directory name)")
@click.option("--plugin", "-p", "plugins_opt", multiple=True,
              help="Enable a plugin (repeatable)")
def workspace_add(path: str, name: str | None, plugins_opt: tuple[str, ...]):
    """Register a new workspace."""
    plugins = list(plugins_opt)
    _workspace_add_impl(path, name, plugins)


@workspace.command("rm")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def workspace_rm(name: str, yes: bool):
    """Unregister a workspace. The on-disk directory is NOT removed."""
    import tomllib

    import tomli_w

    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.state import get_current_workspace, set_current_workspace

    home = _get_config_home()
    config_path = home / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    except Exception:
        data = {}
    workspaces = data.get("workspace", [])
    if not any(w.get("name") == name for w in workspaces):
        console.print(f"[red]✗[/] Workspace [bold]{name}[/] not found")
        raise SystemExit(1)

    # Refuse if any agents are still registered to this workspace.
    orch = get_orchestrator(home)
    in_use = [a for a in orch.list_agents() if (a.get("workspace") or "") == name]
    if in_use:
        ids = ", ".join(a["id"] for a in in_use)
        console.print(
            f"[red]✗[/] Workspace [bold]{name}[/] has agents: [yellow]{ids}[/]. "
            f"Delete them first with [bold]relaydeck agent rm <id>[/]."
        )
        raise SystemExit(1)

    if not yes and not click.confirm(f"Unregister workspace {name}?"):
        return

    # Prefer the daemon: it edits the registry AND emits `workspace.removed`,
    # so per-workspace workers (file-watcher, github poller, …) are torn down
    # live. A direct config edit can't do that — the running daemon never hears
    # about the removal, so those workers leak (kept polling, e.g. a 0.5s
    # file-watcher) until the next restart. Fall back to a local edit only when
    # no daemon is running, where there are no workers to leak.
    ok, msg = _remove_workspace_via_daemon(name)
    if ok:
        console.print(f"[green]✓[/] Workspace [bold]{name}[/] unregistered")
        return
    if msg != "unreachable":
        console.print(f"[red]✗[/] daemon refused to remove [bold]{name}[/]: {msg}")
        raise SystemExit(1)

    # No daemon — edit the registry directly.
    data["workspace"] = [w for w in workspaces if w.get("name") != name]
    config_path.write_text(tomli_w.dumps(data))
    if get_current_workspace() == name:
        set_current_workspace("")
    console.print(f"[green]✓[/] Workspace [bold]{name}[/] unregistered")


def _remove_workspace_via_daemon(name: str) -> tuple[bool, str]:
    """DELETE /api/workspaces/{name} on the running daemon. The daemon removes
    it from the registry AND emits `workspace.removed` so subscribers
    (file-watcher, github poller, messaging) tear down their per-workspace
    workers live — otherwise those workers leak until the next restart.

    Returns (ok, message). message == 'unreachable' means no daemon is running,
    so the caller should fall back to a direct config edit."""
    import urllib.error
    import urllib.request
    from urllib.parse import quote

    from relaydeck.state import get_daemon_url

    url = get_daemon_url().rstrip("/") + f"/api/workspaces/{quote(name, safe='')}"
    req = urllib.request.Request(url, headers=_daemon_auth_headers(), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5, context=_daemon_ssl_context()) as r:
            r.read(0)
        return True, "ok"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, f"HTTP {exc.code}: {body}"
    except (urllib.error.URLError, OSError):
        return False, "unreachable"


def _patch_workspace_plugins_via_daemon(name: str, plugins: list[str]) -> tuple[bool, str]:
    """PATCH /api/workspaces/{name} on the running daemon. Returns
    (ok, message). The daemon emits `workspace.updated` on success so
    subscribers (messaging skill materialization, file-watcher) react
    live without waiting for restart."""
    import urllib.error
    import urllib.request
    from urllib.parse import quote

    from relaydeck.state import get_daemon_url

    url = get_daemon_url().rstrip("/") + f"/api/workspaces/{quote(name, safe='')}"
    data = json.dumps({"plugins": plugins}).encode()
    headers = {"Content-Type": "application/json", **_daemon_auth_headers()}
    req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=5, context=_daemon_ssl_context()) as r:
            r.read(0)
        return True, "ok"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, f"HTTP {exc.code}: {body}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _write_workspace_plugins_to_disk(home: Path, name: str,
                                     plugins: list[str]) -> None:
    """Direct fallback when the daemon is unreachable: write both
    config.toml (the registry) and agent.toml (what the harness reads).
    Used by `relaydeck workspace plugins` when no daemon is running, and
    by `relaydeck workspace add` which doesn't talk to the daemon at all."""
    import tomllib

    import tomli_w

    config_path = home / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    except Exception:
        data = {}
    workspaces = data.get("workspace", [])
    idx = next((i for i, w in enumerate(workspaces) if w.get("name") == name), -1)
    if idx >= 0:
        workspaces[idx]["plugins"] = plugins
        data["workspace"] = workspaces
        config_path.write_text(tomli_w.dumps(data))

    plugin_list = "\n".join(f'  "{pl}",' for pl in plugins)
    ws_state = home / "workspaces" / name
    ws_state.mkdir(parents=True, exist_ok=True)
    ws_state.joinpath("agent.toml").write_text(
        f"[workspace]\nplugins = [\n{plugin_list}\n]\n"
        if plugins else "[workspace]\nplugins = []\n"
    )


@workspace.command("plugins")
@click.argument("name")
@click.option("--add", "add_p", multiple=True, help="Enable a plugin (repeatable)")
@click.option("--remove", "rm_p", multiple=True, help="Disable a plugin (repeatable)")
@click.option("--set", "set_p", multiple=True,
              help="Replace plugins list outright (repeatable)")
def workspace_plugins(name: str, add_p: tuple[str, ...],
                      rm_p: tuple[str, ...], set_p: tuple[str, ...]):
    """Manage a workspace's enabled plugins.

    Routes through the running daemon's PATCH /api/workspaces/{name}
    when it's reachable so workspace.updated fires and subscribers
    (messaging skill materialization, file-watcher) react live. Falls
    back to direct config.toml + agent.toml writes when daemon is
    down — but warns that listener-driven side effects won't happen
    until next start.

    Pass `--set foo --set bar` to replace the list, or combine
    `--add` / `--remove` for incremental edits. With no flags, prints
    the current list.
    """
    import tomllib

    home = _get_config_home()
    config_path = home / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    except Exception:
        data = {}
    workspaces = data.get("workspace", [])
    idx = next((i for i, w in enumerate(workspaces) if w.get("name") == name), -1)
    if idx < 0:
        console.print(f"[red]✗[/] Workspace [bold]{name}[/] not found")
        raise SystemExit(1)

    current: list[str] = list(workspaces[idx].get("plugins", []))

    if not (add_p or rm_p or set_p):
        # Read-only: print current plugins
        if not current:
            console.print(f"[dim]Workspace [bold]{name}[/] has no plugins enabled[/]")
        else:
            console.print(f"[bold]{name}[/] plugins: " + ", ".join(current))
        return

    if set_p:
        new = list(set_p)
    else:
        new = [p for p in current if p not in rm_p]
        for p in add_p:
            if p not in new:
                new.append(p)

    if new == current:
        console.print("[dim]No change[/]")
        return

    # Try the daemon path first so subscribers (messaging skill
    # materialization, etc.) react live to the change. Fall back to
    # direct disk writes if no daemon is running.
    ok, msg = _patch_workspace_plugins_via_daemon(name, new)
    if ok:
        console.print(
            f"[green]✓[/] Workspace [bold]{name}[/] plugins: "
            + (", ".join(new) if new else "(none)")
        )
        console.print(
            "[dim]Note: running agents need a restart to pick up the new plugin list.[/]"
        )
        return

    # Daemon unreachable → direct disk write + warn
    _write_workspace_plugins_to_disk(home, name, new)
    _warn_daemon_unreachable(
        msg,
        "wrote agent.toml + config.toml directly; subscribers "
        "(messaging skill materialization, etc.) won't react until "
        "the daemon restarts.",
    )
    console.print(
        f"[green]✓[/] Workspace [bold]{name}[/] plugins: "
        + (", ".join(new) if new else "(none)")
    )
    console.print(
        "[dim]Running agents need a restart to pick up the new plugin list.[/]"
    )


@workspace.command("info")
@click.argument("name", required=False)
def workspace_info(name: str | None):
    """Show one workspace in detail.

    Without an argument, shows the active workspace (per RELAYDECK_WORKSPACE
    env, state.yaml, or registry fallback). With NAME, shows that
    workspace specifically. Designed to be readable both for humans
    and for an LLM agent parsing the output."""
    from relaydeck.config import load_workspace_registry
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.state import resolve_workspace_source

    active_name, active_source = resolve_workspace_source()
    if not name:
        name = active_name
        if not name:
            console.print(
                "[red]No active workspace.[/] Pass one explicitly or set with "
                "[bold]relaydeck workspace set <name>[/]."
            )
            raise SystemExit(2)

    workspaces = load_workspace_registry()
    w = next((x for x in workspaces if x.name == name), None)
    if w is None:
        console.print(f"[red]✗[/] Workspace [bold]{name}[/] not registered")
        raise SystemExit(1)

    home = _get_config_home()
    orch = get_orchestrator(home)
    agents = [a for a in orch.list_agents() if (a.get("workspace") or "") == name]
    running = [a for a in agents if a.get("status") == "running"]

    is_active = active_name == name
    # Explain *how* this workspace ended up active so the user can
    # debug surprises (e.g. "I thought I was in myapi but cwd is
    # inferring `relaydeck`"). Sources from `resolve_workspace_source`:
    #   env             → RELAYDECK_WORKSPACE env var
    #   cwd             → cwd is inside the workspace's path
    #   state           → relaydeck workspace set <name>
    #   registry-default → only one workspace registered, picked it
    source_label = {
        "env": "(via RELAYDECK_WORKSPACE env)",
        "cwd": "(inferred from cwd)",
        "state": "(via `relaydeck workspace set`)",
        "registry-default": "(only workspace registered)",
        "unset": "",
    }.get(active_source, "")

    # Header block
    active_marker = (
        f"  [green]● active[/] [dim]{source_label}[/]"
        if is_active else "  [dim]inactive[/]"
    )
    console.print(f"[bold cyan]{w.name}[/]" + active_marker)
    console.print(f"  path:    {w.path}")
    console.print(
        "  plugins: "
        + (", ".join(w.plugins) if w.plugins else "[dim](none enabled)[/]")
    )
    console.print(
        f"  agents:  {len(agents)} total, {len(running)} running"
    )

    # Agents block
    if agents:
        console.print()
        table = Table(show_header=True, header_style="dim")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Status")
        table.add_column("Purpose")
        status_styles = {"running": "green", "stopped": "dim",
                         "errored": "red", "pending": "yellow"}
        for a in agents:
            s = a.get("status") or ""
            table.add_row(
                a["id"], a.get("name", ""), a.get("type", ""),
                f"[{status_styles.get(s, '')}]{s}[/]",
                a.get("purpose") or "[dim]—[/]",
            )
        console.print(table)


# ── relaydeck init ────────────────────────────────────────────────────────


@main.command()
@click.argument("path", type=click.Path(), required=False)
@click.option("--plugin", "-p", "plugins_opt", multiple=True,
              help="Enable a plugin (repeatable)")
def init(path: str | None, plugins_opt: tuple[str, ...]):
    """Register a workspace and scaffold its state.

    Alias for `relaydeck workspace add` with sensible defaults.
    If no path given, uses the current directory.
    """
    plugins = list(plugins_opt)
    p = Path(path or ".").resolve()
    name = p.name
    _workspace_add_impl(str(p), name, plugins)


# ── relaydeck worktree ────────────────────────────────────────────────────


@main.group()
def worktree():
    """First-class git worktrees → workspaces.

    A worktree is a separate working tree off a shared repo, on its own
    branch, registered as a relaydeck workspace — so a fleet works several
    branches in parallel without trampling one checkout. Create/remove run
    setup/teardown hooks (a repo's `.relaydeck/worktree.yaml`) so an agent
    lands in a fully provisioned environment. The Workspaces lens is the
    web surface; this CLI is at parity."""
    pass


@worktree.command("list")
@click.option("--repo", help="List raw git worktrees for this repo (incl. unregistered).")
def worktree_list(repo: str | None):
    """List worktree workspaces with branch + git status."""
    outcome, resp = _json_to_daemon(
        "GET", "/api/worktrees" + (f"?repo={repo}" if repo else ""))
    if outcome == _POST_OK and resp is not None:
        rows = resp.get("worktrees") if isinstance(resp, dict) else resp
    else:
        # Local fallback: scan the registry for worktree workspaces.
        from relaydeck import worktrees as wt
        from relaydeck.config import load_workspace_registry
        if repo:
            try:
                rows = wt.list_worktrees(Path(repo).expanduser())
                for r in rows:
                    r["status"] = wt.worktree_status(Path(r.get("path", "")))
            except wt.WorktreeError as exc:
                console.print(f"[red]✗[/] {exc}"); raise SystemExit(1)
        else:
            rows = []
            for w in load_workspace_registry(_get_config_home()):
                wp = Path(w.path)
                if wt.is_worktree(wp):
                    rows.append({"name": w.name, "path": str(w.path),
                                 "status": wt.worktree_status(wp)})
    rows = rows or []
    if not rows:
        console.print("[dim]No worktrees.[/]")
        return
    table = Table(title="Worktrees")
    table.add_column("Name", style="cyan")
    table.add_column("Branch")
    table.add_column("State")
    table.add_column("Path", style="dim")
    for r in rows:
        st = r.get("status") or {}
        flags = []
        if st.get("dirty"):
            flags.append("[yellow]dirty[/]")
        if st.get("ahead"):
            flags.append(f"↑{st['ahead']}")
        if st.get("behind"):
            flags.append(f"↓{st['behind']}")
        table.add_row(r.get("name") or "—", st.get("branch") or r.get("branch") or "—",
                      " ".join(flags) or "[green]clean[/]", r.get("path") or "")
    console.print(table)


@worktree.command("create")
@click.argument("branch")
@click.option("--repo", required=True, help="Path to the source git repo.")
@click.option("--name", help="Workspace name (default: sanitized branch).")
@click.option("--base", help="Base ref to branch from.")
@click.option("--existing", is_flag=True, help="Check out an existing branch (don't create).")
@click.option("--plugin", "plugins", multiple=True, help="Enable a plugin in the new workspace.")
@click.option("--setup", help="Override the setup hook command (else repo's .relaydeck/worktree.yaml).")
@click.option("--no-setup", is_flag=True, help="Skip the setup hook.")
def worktree_create(branch: str, repo: str, name: str | None, base: str | None,
                    existing: bool, plugins: tuple[str, ...], setup: str | None, no_setup: bool):
    """Create a worktree + register it as a workspace + run setup."""
    body = {
        "repo": str(Path(repo).expanduser().resolve()), "branch": branch,
        "name": name, "base": base, "create_branch": not existing,
        "plugins": list(plugins), "setup": setup, "run_setup": not no_setup,
    }
    outcome, resp = _json_to_daemon("POST", "/api/worktrees", body)
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {resp}"); raise SystemExit(1)
    if outcome == _POST_TRANSPORT_FAILED:
        # Daemon down — create locally and warn (subscribers won't react
        # until the daemon is back; mirrors `relaydeck workspace add`).
        from relaydeck import worktrees as wt
        try:
            resp = wt.create_worktree_workspace(
                _get_config_home(), Path(repo).expanduser(), branch,
                name=name, base=base, create_branch=not existing,
                plugins=list(plugins), setup=setup, run_setup=not no_setup)
        except (wt.WorktreeError, ValueError) as exc:
            # ValueError = duplicate name (the worktree is rolled back inside).
            console.print(f"[red]✗[/] {exc}"); raise SystemExit(1)
        console.print("[yellow]![/] daemon unreachable — created on disk; "
                      "restart the daemon to pick it up.")
    console.print(f"[green]✓[/] Worktree [bold]{resp['name']}[/] on [cyan]{resp['branch']}[/] "
                  f"→ {resp['path']}")
    s = resp.get("setup")
    if s:
        if s.get("ok"):
            console.print("  [green]setup hook ok[/]")
        else:
            console.print(f"  [red]setup hook failed[/] (code {s.get('code')}): "
                          f"{(s.get('error') or '').strip()[:200]}")


@worktree.command("remove")
@click.argument("name")
@click.option("--force", is_flag=True, help="Remove even with uncommitted changes.")
@click.option("--no-teardown", is_flag=True, help="Skip the teardown hook.")
@click.option("--keep-dir", is_flag=True, help="Unregister only; leave the working tree on disk.")
def worktree_remove(name: str, force: bool, no_teardown: bool, keep_dir: bool):
    """Tear down a worktree: teardown hook → git worktree remove → unregister."""
    qs = f"?force={str(force).lower()}&run_teardown={str(not no_teardown).lower()}" \
         f"&delete_dir={str(not keep_dir).lower()}"
    outcome, resp = _json_to_daemon("DELETE", f"/api/worktrees/{name}{qs}")
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {resp}"); raise SystemExit(1)
    if outcome == _POST_TRANSPORT_FAILED:
        from relaydeck import worktrees as wt
        resp = wt.remove_worktree_workspace(
            _get_config_home(), name, force=force,
            run_teardown=not no_teardown, delete_dir=not keep_dir)
        if resp.get("error"):
            console.print(f"[red]✗[/] {resp['error']}"); raise SystemExit(1)
        console.print("[yellow]![/] daemon unreachable — removed on disk.")
    t = (resp or {}).get("teardown")
    if t and not t.get("ok"):
        console.print(f"  [yellow]teardown hook failed[/] (code {t.get('code')})")
    console.print(f"[green]✓[/] Worktree [bold]{name}[/] removed")


# ── relaydeck db ──────────────────────────────────────────────────────────


@main.group()
def db():
    """Inspect or maintain the relaydeck SQLite database."""
    pass


@db.command("status")
def db_status_cmd():
    """Print pool stats, WAL size, and row counts for relaydeck.db.

    Useful on a long-running daemon to see whether the events table
    has grown unexpectedly large or the WAL hasn't been checkpointed.
    The maintenance worker handles both automatically; this command
    is the on-demand view.
    """
    from relaydeck.db import db_status

    home = _get_config_home()
    snap = db_status(home / "runtime" / "relaydeck.db")
    console.print(f"[bold]Path:[/] {snap['path']}")
    console.print(f"[bold]DB size:[/] {snap['bytes'] / 1024:.1f} KB")
    console.print(f"[bold]WAL size:[/] {snap['wal_bytes'] / 1024:.1f} KB")
    free = snap.get("free_bytes", 0)
    if free:
        console.print(
            f"[bold]Reclaimable:[/] {free / 1024:.1f} KB "
            "[dim](freed pages — `relaydeck db vacuum` to return to OS)[/]"
        )
    if snap.get("pools"):
        console.print("[bold]Pools:[/]")
        for path, stats in snap["pools"].items():
            console.print(f"  {path}: {stats['free']}/{stats['max']} free")
    if snap.get("rows"):
        table = Table(title="Row counts")
        table.add_column("Table", style="cyan")
        table.add_column("Rows", justify="right")
        for name, count in snap["rows"].items():
            table.add_row(name, f"{count:,}")
        console.print(table)


@db.command("checkpoint")
@click.option("--mode", default="TRUNCATE",
              type=click.Choice(["PASSIVE", "FULL", "RESTART", "TRUNCATE"]),
              help="Checkpoint aggressiveness — TRUNCATE drops the WAL to 0 bytes.")
def db_checkpoint(mode: str):
    """Run a WAL checkpoint right now. The maintenance worker runs
    this every 5 minutes anyway; use this command if you want to
    force a flush before backup or to reclaim WAL space immediately.
    """
    from relaydeck.db import wal_checkpoint

    home = _get_config_home()
    stats = wal_checkpoint(home / "runtime" / "relaydeck.db", mode=mode)
    console.print(
        f"[green]✓[/] Checkpoint ({mode}): "
        f"{stats['checkpointed']}/{stats['log']} frames moved "
        f"{'(busy)' if stats['busy'] else ''}"
    )


@db.command("vacuum")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def db_vacuum(yes: bool):
    """Reclaim freed pages to the filesystem (runs VACUUM).

    Pruning and checkpointing don't shrink the .db file — deleted pages
    go on SQLite's freelist for reuse. VACUUM rewrites the whole file so
    that space is returned to the OS. The maintenance worker does this
    automatically once reclaimable space is large (≥64 MiB); use this
    command to force it now (e.g. after a big one-off prune).

    Note: VACUUM takes an exclusive lock and rewrites the entire file —
    on a large DB it can briefly block all writers.
    """
    from relaydeck.db import db_status, vacuum_db

    home = _get_config_home()
    db_file = home / "runtime" / "relaydeck.db"
    free = db_status(db_file).get("free_bytes", 0)
    if not yes:
        click.confirm(
            f"VACUUM relaydeck.db now? ~{free / 1024:.1f} KB reclaimable; "
            "writers may block briefly.",
            abort=True,
        )
    stats = vacuum_db(db_file)
    console.print(
        f"[green]✓[/] Vacuumed: reclaimed "
        f"[bold]{stats['reclaimed_bytes'] / 1024:.1f} KB[/] "
        f"({stats['before_bytes'] / 1024:.1f} → {stats['after_bytes'] / 1024:.1f} KB)"
    )


@db.command("prune")
@click.option("--days", "-d", type=float, default=None,
              help="Retention in days (default: relaydeck.db's configured retention).")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def db_prune(days: float | None, yes: bool):
    """Delete `events` rows older than the retention window.

    The maintenance worker does this automatically; this command is
    for ad-hoc cleanup or when you've changed retention and want the
    effect to land immediately instead of waiting for the next tick.
    """
    from relaydeck.db import DEFAULT_EVENT_RETENTION_DAYS, prune_old_events

    retention = days if days is not None else DEFAULT_EVENT_RETENTION_DAYS
    if not yes:
        click.confirm(
            f"Delete event rows older than {retention} days?",
            abort=True,
        )
    home = _get_config_home()
    deleted = prune_old_events(
        home / "runtime" / "relaydeck.db", retention_days=retention,
    )
    console.print(f"[green]✓[/] Pruned [bold]{deleted:,}[/] events")


@db.command("wipe")
@click.option("--messages", is_flag=True, help="Wipe peer + chat messages (agent_messages).")
@click.option("--history", is_flag=True,
              help="Wipe activity history (events, usage, LLM calls, automation runs).")
@click.option("--scope", "scopes", multiple=True,
              help="Wipe a specific scope (repeatable): "
                   "messages|events|usage|invocations|runs.")
@click.option("--all", "all_", is_flag=True, help="Wipe messages AND all history.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def db_wipe(messages: bool, history: bool, scopes: tuple[str, ...], all_: bool, yes: bool):
    """DANGER: wipe messages + history wholesale (the dashboard Danger Zone).

    Unlike `db prune` (age-based), this deletes EVERYTHING in the chosen
    scopes. Config, agents, vault, audit log, and the durable bus are
    never touched. Routes through the daemon (so live views refresh +
    it's audited); falls back to a direct DB delete if the daemon's down.
    """
    from relaydeck import maintenance

    chosen = maintenance.resolve_scopes(
        messages=messages or all_, history=history or all_, extra=list(scopes))
    if not chosen:
        console.print("[yellow]Nothing selected.[/] Use --messages / --history / "
                      "--all / --scope <name>. Scopes: "
                      + ", ".join(maintenance.SCOPES))
        raise SystemExit(1)

    # Show what'll be deleted (counts) before confirming.
    home = _get_config_home()
    db_path = str(home / "runtime" / "relaydeck.db")
    counts = maintenance.history_stats(db_path)
    labels = maintenance.scope_labels()
    console.print("[bold red]DANGER:[/] this permanently deletes:")
    for s in chosen:
        console.print(f"  • {labels.get(s, s)}: [bold]{counts.get(s, 0):,}[/] rows")
    if not yes:
        click.confirm("Proceed?", abort=True)

    outcome, resp = _json_to_daemon("POST", "/api/maintenance/wipe", {"scopes": chosen})
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {resp}"); raise SystemExit(1)
    if outcome == _POST_TRANSPORT_FAILED:
        deleted = maintenance.wipe(db_path, chosen)
        console.print("[yellow]![/] daemon unreachable — wiped on disk directly.")
    else:
        deleted = (resp or {}).get("deleted", {})
    total = sum(deleted.values()) if deleted else 0
    console.print(f"[green]✓[/] Wiped [bold]{total:,}[/] rows "
                  f"({', '.join(f'{k}:{v}' for k, v in (deleted or {}).items())})")


# ── relaydeck auth ────────────────────────────────────────────────────────


@main.group()
def auth():
    """Inspect or rotate the daemon auth token."""
    pass


@auth.command("token")
def auth_token():
    """Print the current daemon auth token, raw, to stdout.

    Pipe-friendly (`relaydeck auth token | pbcopy`). Mints one on first
    call if `relaydeck serve` hasn't been run yet. Pair with the dashboard's
    paste-token prompt when opening the UI from a non-loopback browser:
    on the daemon host, run `relaydeck auth token` and paste the output.
    """
    from relaydeck.auth import get_or_create_token
    click.echo(get_or_create_token())


@auth.command("show")
@click.option("--full", is_flag=True, help="Print the full token (default: redacted)")
def auth_show(full: bool):
    """Print the location and a redacted view of the daemon token.

    Use `--full` only when copying to another machine. The CLI on the
    same machine reads the token transparently from
    `~/.relaydeck/auth-token`; you should rarely need to see it.
    """
    from relaydeck.auth import _token_path, read_token
    p = _token_path()
    tok = read_token()
    if not tok:
        console.print("[yellow]No token configured.[/] Run `relaydeck serve` to mint one.")
        sys.exit(1)
    redacted = tok if full else f"{tok[:8]}…{tok[-4:]}"
    console.print(f"[bold]File:[/] {p}")
    console.print(f"[bold]Token:[/] {redacted}")
    if not full:
        console.print("[dim]Use `--full` to print the entire token (treat as a password).[/]")


@auth.command("rotate")
@click.confirmation_option(prompt="Rotate the daemon token? Live clients will get 401 until they re-read the file.")
def auth_rotate():
    """Mint a new daemon token, invalidating the old one.

    Any running CLI processes, dashboards, or `RemoteHost` connections
    holding the old token will start getting 401s and must re-read
    `~/.relaydeck/auth-token`.
    """
    from relaydeck import audit
    from relaydeck.auth import regenerate_token
    new = regenerate_token()
    audit.record(audit.actions.TOKEN_ROTATE, target="root-file")
    console.print(f"[green]✓[/] Rotated. New token: [bold]{new[:8]}…{new[-4:]}[/]")
    console.print("[dim]Restart the dashboard tab to pick up the new token.[/]")


@auth.command("issue")
@click.option("--scope", required=True,
              help="Scope: 'root', 'read-only', 'agent:<id>', 'plugin:<name>'")
@click.option("--label", required=True, help="Human-readable name for this token")
@click.option("--expires", default=None,
              help="Optional expiry: ISO date or `<N>d` / `<N>h` (e.g. 30d, 12h)")
def auth_issue(scope: str, label: str, expires: str | None):
    """Mint a new scoped Bearer token. The plaintext is printed once.

    The token hash is stored in the auth_tokens table; the plaintext
    is shown to you here and never persisted. If you lose it, issue
    a new one and revoke the old. Use this for per-purpose
    credentials (a metrics scraper, a CI runner, a teammate's
    dashboard) instead of sharing the implicit root token.
    """
    from relaydeck import audit
    from relaydeck.auth_tokens import issue_token

    expires_at = _parse_expires(expires) if expires else None
    try:
        token_id, plaintext = issue_token(
            label=label, scope=scope, expires_at=expires_at,
        )
    except ValueError as exc:
        console.print(f"[red]✗[/] {exc}")
        sys.exit(2)
    audit.record(
        audit.actions.TOKEN_ISSUE, target=token_id,
        payload={"label": label, "scope": scope, "expires_at": expires_at},
    )
    console.print(f"[green]✓[/] Issued [bold]{token_id}[/]")
    console.print(f"[bold]Label:[/]  {label}")
    console.print(f"[bold]Scope:[/]  {scope}")
    if expires_at:
        import time as _t
        console.print(f"[bold]Expires:[/] {_t.strftime('%Y-%m-%d %H:%M:%S', _t.localtime(expires_at))}")
    console.print()
    console.print("[bold yellow]Token (shown only once — copy now):[/]")
    console.print(plaintext)


@auth.command("list")
def auth_list():
    """List issued tokens (label, scope, last-used, expiry).

    The implicit root token at `~/.relaydeck/auth-token` is NOT
    shown here — it has no row in the auth_tokens table by design.
    Use `relaydeck auth show` for the on-disk root.
    """
    from rich.table import Table
    from relaydeck.auth_tokens import list_tokens

    rows = list_tokens()
    if not rows:
        console.print("[dim]No scoped tokens issued. The implicit root token "
                      "from `relaydeck serve` is shown by `relaydeck auth show`.[/]")
        return

    table = Table(title="Scoped auth tokens")
    table.add_column("id", style="cyan")
    table.add_column("label")
    table.add_column("scope")
    table.add_column("created", style="dim")
    table.add_column("last used", style="dim")
    table.add_column("expires", style="dim")
    table.add_column("revoked", style="red")

    import time as _t
    def _fmt(ts):
        return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ts)) if ts else "—"

    for r in rows:
        table.add_row(
            r["id"], r["label"], r["scope"],
            _fmt(r["created_at"]), _fmt(r["last_used_at"]),
            _fmt(r["expires_at"]),
            "✓" if r["revoked_at"] else "",
        )
    console.print(table)


@auth.command("revoke")
@click.argument("token_id")
def auth_revoke(token_id: str):
    """Invalidate a scoped token immediately.

    Subsequent requests with the plaintext return 401. The row is
    kept (marked `revoked_at`) for audit history."""
    from relaydeck import audit
    from relaydeck.auth_tokens import revoke_token

    if revoke_token(token_id):
        audit.record(audit.actions.TOKEN_REVOKE, target=token_id)
        console.print(f"[green]✓[/] Revoked {token_id}")
    else:
        console.print(f"[yellow]·[/] No active token with id {token_id}")
        sys.exit(1)


# ── relaydeck layout ────────────────────────────────────────────────────


@main.group(name="layout")
def layout_cli():
    """Saved viewer layouts.

    A layout is a named bundle of `relaydeck workspace view` flags —
    which workspace, which viewer, which session name, etc. Save
    one with [bold]relaydeck workspace view --save NAME[/]; restore
    with [bold]relaydeck workspace view --restore NAME[/]. The
    commands here just inspect / delete the saved set.
    """
    pass


@layout_cli.command("list")
def layout_list():
    """Show every saved layout."""
    from relaydeck import layouts as layouts_mod
    rows = layouts_mod.list_layouts()
    if not rows:
        console.print("[dim]No saved layouts.[/]")
        return
    table = Table(title="Saved layouts")
    table.add_column("Name", style="cyan")
    table.add_column("Workspace")
    table.add_column("Viewer")
    table.add_column("Agents")
    for lay in rows:
        agents = ", ".join(lay.agents[:4])
        if len(lay.agents) > 4:
            agents += f" (+{len(lay.agents) - 4} more)"
        table.add_row(
            lay.name,
            lay.workspace or "[dim]?[/]",
            lay.viewer or "[dim]auto[/]",
            agents or "[dim]—[/]",
        )
    console.print(table)


@layout_cli.command("show")
@click.argument("name")
def layout_show(name: str):
    """Print one layout's full contents."""
    from relaydeck import layouts as layouts_mod
    lay = layouts_mod.load(name)
    if lay is None:
        console.print(f"[red]✗[/] No saved layout named [bold]{name}[/]")
        sys.exit(2)
    console.print(f"[bold]{lay.name}[/]")
    console.print(f"  workspace:       [cyan]{lay.workspace}[/]")
    console.print(f"  viewer:          {lay.viewer or '[dim](auto)[/]'}")
    console.print(f"  session:         {lay.session or '[dim](default)[/]'}")
    console.print(f"  include-stopped: {lay.include_stopped}")
    console.print(f"  force:           {lay.force}")
    if lay.agents:
        console.print(f"  agents at save time: {', '.join(lay.agents)}")


@layout_cli.command("rm")
@click.argument("name")
def layout_rm(name: str):
    """Delete a saved layout."""
    from relaydeck import layouts as layouts_mod
    if layouts_mod.delete(name):
        console.print(f"[green]✓[/] Removed layout [bold]{name}[/]")
    else:
        console.print(f"[dim]·[/] No layout named [bold]{name}[/] to remove")
        sys.exit(1)


# ── relaydeck integration ────────────────────────────────────────────────


@main.group(name="integration")
def integration_cli():
    """Install vendor-side hooks that report agent state.

    For each supported harness (Claude Code, codex, pi, …) relaydeck
    ships a small script that plugs into the harness's native
    hook/extension system. The script POSTs to the daemon when
    the harness fires lifecycle events (a tool call starts,
    permission is requested, the agent finishes a turn), so the
    daemon learns the *observable* state — `working`,
    `awaiting-input`, `idle` — instead of inferring it from
    message content.

    This is what powers the semantic-status column on
    `relaydeck agent list`, the workspace status roll-up, and the
    `relaydeck agent wait` synchronization primitive.
    """
    pass


@integration_cli.command("list")
def integration_list():
    """Show which harnesses have integration hooks installed."""
    from relaydeck import integrations
    integrations.register_builtin_integrations()

    rows = integrations.all_integrations()
    if not rows:
        console.print("[dim]No integrations registered.[/]")
        return

    table = Table(title="Integrations")
    table.add_column("Name", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("State", style="green")
    table.add_column("Description")
    for it in rows:
        try:
            st = integrations.integration_state(it)
        except Exception as exc:
            st = f"error: {exc}"
        if st == "installed":
            state_cell = "[green]✓ installed[/]"
        elif st == "not-installed":
            state_cell = "[dim]· not installed[/]"
        elif st.startswith("orphaned") or st == "outdated":
            state_cell = f"[yellow]⚠ {st}[/]"
        else:
            state_cell = st
        kind = getattr(it, "kind", "hook")
        table.add_row(it.name, kind, state_cell, getattr(it, "description", ""))
    console.print(table)


@integration_cli.command("install")
@click.argument("name")
def integration_install(name: str):
    """Install the integration hook for HARNESS (idempotent)."""
    from relaydeck import integrations
    integrations.register_builtin_integrations()

    it = integrations.get(name)
    if it is None:
        available = ", ".join(i.name for i in integrations.all_integrations())
        console.print(
            f"[red]✗[/] No integration named [bold]{name}[/]. "
            f"Available: {available}"
        )
        sys.exit(2)
    try:
        path = it.install()
    except Exception as exc:
        console.print(f"[red]✗[/] install failed: {exc}")
        sys.exit(1)
    kind = getattr(it, "kind", "hook")
    if kind == "classifier":
        # No file written — `path` is a human explanation.
        console.print(f"[green]✓[/] [bold]{name}[/]: {path}")
    else:
        console.print(f"[green]✓[/] Installed [bold]{name}[/] hook at:\n  {path}")
        console.print(
            "[dim]The hook posts to the daemon on harness lifecycle events. "
            "Restart any running agents of this type to pick up the new hook.[/]"
        )


@integration_cli.command("uninstall")
@click.argument("name")
def integration_uninstall(name: str):
    """Remove the integration hook for HARNESS."""
    from relaydeck import integrations
    integrations.register_builtin_integrations()

    it = integrations.get(name)
    if it is None:
        console.print(f"[red]✗[/] No integration named [bold]{name}[/]")
        sys.exit(2)
    try:
        removed = it.uninstall()
    except Exception as exc:
        console.print(f"[red]✗[/] uninstall failed: {exc}")
        sys.exit(1)
    if removed:
        console.print(f"[green]✓[/] Uninstalled [bold]{name}[/] hook")
    else:
        console.print(f"[dim]·[/] [bold]{name}[/] hook was not installed")


@integration_cli.command("cleanup-all")
def integration_cleanup_all():
    """Remove all vendor hook registrations (run before deleting ~/.relaydeck).

    Strips hook entries from vendor config (e.g. ~/.claude/settings.json)
    so a later `rm -rf ~/.relaydeck` does not leave orphaned hook scripts
    behind. Classifier integrations are no-ops on disk and are skipped.
    """
    from relaydeck import integrations
    removed = integrations.uninstall_hook_integrations_before_config_removal()
    if removed:
        console.print(
            "[green]✓[/] Removed hook integrations: "
            + ", ".join(removed)
        )
    else:
        console.print("[dim]·[/] No hook integrations to clean up")


# ── relaydeck audit ───────────────────────────────────────────────────────


@main.group()
def audit_cli():
    """Inspect the daemon audit log.

    Every sensitive mutation (agent create/start/stop/rm, vault ops,
    token issue/revoke, plugin enable/disable, settings change,
    workspace add/rm) writes an append-only row to `audit_events`.
    Use these commands to answer "who did this, when, and with which
    token".
    """
    pass


# `relaydeck audit` clashes with the click command name if we use `audit` as
# the function name, so the group is registered with name="audit"
# explicitly.
audit_cli.name = "audit"
main.commands["audit"] = audit_cli


@audit_cli.command("tail")
@click.option("--limit", default=100, type=int, help="How many rows to show (default 100)")
@click.option("--action", default=None, help="Filter to one action key, e.g. agent.start")
@click.option("--token-id", "token_id", default=None, help="Filter to one auth_tokens.id")
def audit_tail(limit: int, action: str | None, token_id: str | None):
    """Show the most recent audit events (newest first)."""
    from rich.table import Table
    import time as _t
    from relaydeck import audit as audit_mod

    rows = audit_mod.list_events(action=action, token_id=token_id, limit=limit)
    if not rows:
        console.print("[dim]No audit events.[/]")
        return

    table = Table(title=f"Audit · last {len(rows)}")
    table.add_column("ts", style="dim")
    table.add_column("action")
    table.add_column("target")
    table.add_column("token")
    table.add_column("payload", style="dim")

    for r in rows:
        ts_str = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(r["ts"]))
        payload_str = ""
        if r["payload"]:
            try:
                payload_str = ", ".join(
                    f"{k}={v}" for k, v in r["payload"].items()
                )[:60]
            except Exception:
                payload_str = str(r["payload"])[:60]
        table.add_row(
            ts_str, r["action"], r["target"] or "—",
            r["token_label"] or "—", payload_str,
        )
    console.print(table)


@audit_cli.command("search")
@click.option("--action", default=None)
@click.option("--target", default=None, help="Resource id (e.g. agent id, token id)")
@click.option("--token-id", "token_id", default=None)
@click.option("--since", default=None,
              help="Earliest ts: ISO date OR relative (e.g. 1d, 12h)")
@click.option("--limit", default=500, type=int)
def audit_search(action: str | None, target: str | None,
                 token_id: str | None, since: str | None, limit: int):
    """Filter audit events. All flags AND together; omit a flag to
    leave that dimension wildcarded."""
    import time as _t
    from rich.table import Table
    from relaydeck import audit as audit_mod

    since_ts = _parse_since(since) if since else None
    rows = audit_mod.list_events(
        action=action, target=target, token_id=token_id,
        since=since_ts, limit=limit,
    )
    if not rows:
        console.print("[dim]No matching audit events.[/]")
        return
    table = Table(title=f"Audit · {len(rows)} match(es)")
    table.add_column("ts", style="dim")
    table.add_column("action")
    table.add_column("target")
    table.add_column("token")
    table.add_column("ip", style="dim")
    table.add_column("payload", style="dim")
    for r in rows:
        ts_str = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(r["ts"]))
        payload_str = ""
        if r["payload"]:
            try:
                payload_str = ", ".join(
                    f"{k}={v}" for k, v in r["payload"].items()
                )[:80]
            except Exception:
                payload_str = str(r["payload"])[:80]
        table.add_row(
            ts_str, r["action"], r["target"] or "—",
            r["token_label"] or "—", r["source_ip"] or "—", payload_str,
        )
    console.print(table)


@audit_cli.command("prune")
@click.option("--before", required=True,
              help="Drop rows older than this. ISO date OR relative (e.g. 365d)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def audit_prune(before: str, yes: bool):
    """Delete audit rows older than `--before`. Operator-driven; the
    daemon never auto-prunes."""
    from relaydeck import audit as audit_mod

    cutoff = _parse_since(before)
    if cutoff is None:
        console.print(f"[red]✗[/] Couldn't parse --before {before!r}.")
        sys.exit(2)
    if not yes:
        click.confirm(
            f"Delete audit_events with ts < {cutoff}? This is destructive.",
            abort=True,
        )
    n = audit_mod.prune(before=cutoff)
    console.print(f"[green]✓[/] Pruned {n} audit event(s) older than {before}.")


# ── relaydeck automation ───────────────────────────────────────────────────


@main.group()
def automation_cli():
    """Inspect automation run history.

    An automation run is the durable EXECUTION record of a trigger
    firing — a loop agent tick today (scheduled/event runs later). Where
    `relaydeck workers` shows live threads, this shows what already ran: when
    it fired, how long it took, how many actions ran, and whether any
    failed. Records survive daemon restarts.
    """
    pass


# `automation` would shadow the function name, so register explicitly —
# same trick as the audit group above.
automation_cli.name = "automation"
main.commands["automation"] = automation_cli


def _automation_db_path() -> str:
    return str(_get_config_home() / "runtime" / "relaydeck.db")


def _fmt_ts(ts: float | None) -> str:
    import time as _t
    if not ts:
        return "—"
    return _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(ts))


def _fmt_dur(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


_STATUS_STYLE = {
    "succeeded": "green",
    "partial": "yellow",
    "failed": "red",
    "running": "cyan",
}


def _status_cell(status: str | None) -> str:
    s = status or "—"
    style = _STATUS_STYLE.get(s)
    return f"[{style}]{s}[/]" if style else s


@automation_cli.command("list")
def automation_list():
    """List automations that have run, newest activity first."""
    from rich.table import Table

    from relaydeck import automation_runs as runs_mod

    rows = runs_mod.list_automation_ids(db_path=_automation_db_path())
    if not rows:
        console.print("[dim]No automation runs recorded yet.[/]")
        return
    table = Table(title=f"Automations · {len(rows)}")
    table.add_column("automation", style="cyan")
    table.add_column("type")
    table.add_column("workspace")
    table.add_column("runs", justify="right")
    table.add_column("last run")
    table.add_column("last status")
    table.add_column("dur", justify="right")
    for r in rows:
        table.add_row(
            r["automation_id"],
            r["automation_type"] or "—",
            r["workspace"] or "—",
            str(r["runs"]),
            _fmt_ts(r["last_started_at"]),
            _status_cell(r["last_status"]),
            _fmt_dur(r["last_duration_ms"]),
        )
    console.print(table)


@automation_cli.command("runs")
@click.argument("automation_id")
@click.option("--status", default=None,
              help="Filter by status (running|succeeded|partial|failed)")
@click.option("--limit", default=50, type=int, help="How many runs to show")
def automation_runs_cmd(automation_id: str, status: str | None, limit: int):
    """Show run history for one automation (newest first)."""
    from rich.table import Table

    from relaydeck import automation_runs as runs_mod

    rows = runs_mod.list_runs(
        automation_id=automation_id, status=status, limit=limit,
        db_path=_automation_db_path(),
    )
    if not rows:
        console.print(f"[dim]No runs for {automation_id!r}.[/]")
        return
    table = Table(title=f"Runs · {automation_id} · {len(rows)}")
    table.add_column("started", style="dim")
    table.add_column("trigger")
    table.add_column("status")
    table.add_column("actions", justify="right")
    table.add_column("errors", justify="right")
    table.add_column("dur", justify="right")
    for r in rows:
        trig = r.trigger_type or "—"
        if r.trigger_event_id:
            trig = f"{trig}:{r.trigger_event_id}"
        errs = str(r.error_count) if r.error_count else "[green]0[/]"
        table.add_row(
            _fmt_ts(r.started_at),
            trig,
            _status_cell(r.status),
            str(r.action_count),
            errs,
            _fmt_dur(r.duration_ms),
        )
    console.print(table)


@automation_cli.command("prune")
@click.option("--older-than", "older_than", default="30d",
              help="Drop finished runs older than this (e.g. 30d, 72h)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def automation_prune(older_than: str, yes: bool):
    """Delete finished runs older than `--older-than`. Running rows are
    never pruned by age. Operator-driven; nothing auto-prunes."""
    from relaydeck import automation_runs as runs_mod

    cutoff = _parse_since(older_than)
    if cutoff is None:
        console.print(f"[red]✗[/] Couldn't parse --older-than {older_than!r}.")
        sys.exit(2)
    import time as _t
    days = max(0.0, (_t.time() - cutoff) / 86400.0)
    if not yes:
        click.confirm(
            f"Delete finished automation runs older than {older_than}? "
            "This is destructive.",
            abort=True,
        )
    n = runs_mod.prune_runs(older_than_days=days, db_path=_automation_db_path())
    console.print(f"[green]✓[/] Pruned {n} run(s) older than {older_than}.")


def _parse_since(spec: str) -> float | None:
    """Sibling of _parse_expires but for a backward-pointing relative
    time. Accepts ISO date or `<N>d`/`<N>h`/`<N>m`."""
    import re
    import time as _t
    spec = (spec or "").strip()
    if not spec:
        return None
    m = re.fullmatch(r"(\d+)([dhm])", spec)
    if m:
        n = int(m.group(1))
        scale = {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
        return _t.time() - n * scale
    try:
        import datetime as _dt
        dt = _dt.datetime.fromisoformat(spec)
        return dt.timestamp()
    except ValueError:
        return None


def _parse_expires(spec: str) -> float | None:
    """Parse `--expires` into a Unix timestamp.

    Accepts an ISO-ish date (YYYY-MM-DD) or a relative form
    `<N>d`/`<N>h`/`<N>m`. Returns None on malformed input — the
    caller treats that as "no expiry".
    """
    import re
    import time as _t
    spec = (spec or "").strip()
    if not spec:
        return None
    m = re.fullmatch(r"(\d+)([dhm])", spec)
    if m:
        n = int(m.group(1))
        scale = {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
        return _t.time() + n * scale
    try:
        import datetime as _dt
        dt = _dt.datetime.fromisoformat(spec)
        return dt.timestamp()
    except ValueError:
        return None


# ── relaydeck preset ──────────────────────────────────────────────────────


@main.group()
def preset():
    """Manage model/provider presets."""
    pass


@preset.command("list")
def preset_list():
    """List model presets (a preset is a named provider/model)."""
    from relaydeck.config import load_model_presets

    presets = load_model_presets()
    if not presets:
        console.print("[dim]No presets yet. Connect a provider, then create one: "
                      "relaydeck preset create <name> --provider P --model M[/]")
        return
    table = Table(title="Model Presets")
    table.add_column("Name", style="cyan")
    table.add_column("Provider")
    table.add_column("Model")
    for p in presets:
        table.add_row(p.name, p.provider, p.model)
    console.print(table)


@preset.command("create")
@click.argument("name")
@click.option("--provider", "-p", required=True, help="Provider name (openrouter, anthropic, ollama, etc.)")
@click.option("--model", "-m", required=True, help="Model id")
def preset_create(name: str, provider: str, model: str):
    """Create a model preset (name → provider/model).

    Auth (API key) and endpoint (base URL) belong to the provider —
    configure those with `relaydeck provider`, not here."""
    import yaml

    home = _get_config_home()
    presets_dir = home / "presets"
    presets_dir.mkdir(parents=True, exist_ok=True)
    preset_data = {"name": name, "provider": provider, "model": model}
    (presets_dir / f"{name}.yaml").write_text(
        yaml.dump(preset_data, default_flow_style=False, sort_keys=False))
    console.print(f"[green]✓[/] Preset [bold]{name}[/] created ({provider}/{model})")


@preset.command("edit")
@click.argument("name")
@click.option("--provider", "-p", help="New provider name")
@click.option("--model", "-m", help="New model id")
def preset_edit(name: str, provider: str | None, model: str | None):
    """Edit a preset's provider and/or model."""
    import yaml

    from relaydeck.config import load_model_presets
    home = _get_config_home()
    current = next((x for x in load_model_presets(home) if x.name == name), None)
    if current is None:
        console.print(f"[red]✗[/] Preset [bold]{name}[/] not found")
        raise SystemExit(1)
    new_provider = provider or current.provider
    new_model = model or current.model
    presets_dir = home / "presets"
    presets_dir.mkdir(parents=True, exist_ok=True)
    (presets_dir / f"{name}.yaml").write_text(yaml.dump(
        {"name": name, "provider": new_provider, "model": new_model},
        default_flow_style=False, sort_keys=False))
    console.print(f"[green]✓[/] Preset [bold]{name}[/] updated ({new_provider}/{new_model})")


@preset.command("rm")
@click.argument("name")
def preset_rm(name: str):
    """Delete a model preset."""
    home = _get_config_home()
    path = home / "presets" / f"{name}.yaml"
    if not path.exists():
        console.print(f"[red]✗[/] Preset [bold]{name}[/] not found")
        raise SystemExit(1)
    path.unlink()
    console.print(f"[green]✓[/] Preset [bold]{name}[/] deleted")


# ── relaydeck theme (dashboard themes + appearance) ───────────────────────


def _theme_home() -> Path:
    """Env-aware config root for theme/appearance files (honors
    RELAYDECK_CONFIG_HOME like the themes module)."""
    import os as _os
    override = _os.environ.get("RELAYDECK_CONFIG_HOME")
    return Path(override) if override else _get_config_home()


@main.group()
def theme():
    """Dashboard themes + appearance (colors, type, spacing).

    A theme is a named bundle of design-token overrides; it may
    `extends` another theme. Appearance binds a theme (+ density/glow)
    to the dashboard globally or per workspace. The Appearance lens in
    the web dashboard is the primary surface; this CLI is at parity."""
    pass


@theme.command("list")
def theme_list():
    """List every theme (builtin + user)."""
    from relaydeck import themes

    rows = themes.list_themes()
    table = Table(title="Themes")
    table.add_column("Name", style="cyan")
    table.add_column("Kind")
    table.add_column("Extends")
    table.add_column("Tokens", justify="right")
    table.add_column("Description")
    for t in rows:
        table.add_row(
            t.name, "builtin" if t.builtin else "user",
            t.extends or "—", str(len(t.tokens)),
            t.display_name or t.description or "")
    console.print(table)


@theme.command("show")
@click.argument("name")
@click.option("--resolved", is_flag=True, help="Show the flattened token map (after extends).")
def theme_show(name: str, resolved: bool):
    """Show a theme's metadata + tokens."""
    from relaydeck import themes

    t = themes.get_theme(name)
    if t is None:
        console.print(f"[red]No such theme[/] {name!r}")
        raise SystemExit(1)
    console.print(f"[bold cyan]{t.name}[/] ({'builtin' if t.builtin else 'user'})")
    if t.display_name:
        console.print(f"  {t.display_name}")
    if t.description:
        console.print(f"  [dim]{t.description}[/]")
    if t.extends:
        console.print(f"  extends: [cyan]{t.extends}[/]")
    toks = themes.resolve_theme(name) if resolved else t.tokens
    if not toks:
        console.print("  [dim](no token overrides — renders as :root)[/]")
    for k in sorted(toks):
        console.print(f"  --{k}: {toks[k]}")


@theme.command("create")
@click.argument("name")
@click.option("--extends", "extends_", help="Base theme to inherit from (e.g. base, amber).")
@click.option("--display-name", help="Friendly name shown in the gallery.")
@click.option("--description", help="One-line description.")
@click.option("--set", "set_tokens", multiple=True, metavar="TOKEN=VALUE",
              help="Set a token, e.g. --set acc=#ff8800 (repeatable).")
def theme_create(name: str, extends_: str | None, display_name: str | None,
                 description: str | None, set_tokens: tuple[str, ...]):
    """Create a user theme. Tokens are validated against the contract."""
    from relaydeck import themes

    tokens: dict[str, str] = {}
    for pair in set_tokens:
        if "=" not in pair:
            console.print(f"[red]Bad --set {pair!r}[/] (expected TOKEN=VALUE)")
            raise SystemExit(1)
        k, v = pair.split("=", 1)
        tokens[k.strip().lstrip("-")] = v.strip()
    t = themes.Theme(name=name, display_name=display_name or "",
                     description=description or "", extends=extends_, tokens=tokens)
    try:
        path = themes.save_theme(t)
    except ValueError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise SystemExit(1)
    console.print(f"[green]✓[/] Theme [bold]{name}[/] saved → {path}")


@theme.command("edit")
@click.argument("name")
@click.option("--set", "set_tokens", multiple=True, metavar="TOKEN=VALUE",
              help="Set/override a token (repeatable).")
@click.option("--unset", "unset_tokens", multiple=True, metavar="TOKEN",
              help="Remove a token override (repeatable).")
@click.option("--extends", "extends_", help="Change the base theme.")
def theme_edit(name: str, set_tokens: tuple[str, ...], unset_tokens: tuple[str, ...],
               extends_: str | None):
    """Edit a user theme's tokens / extends (flag-driven)."""
    from relaydeck import themes

    t = themes.get_theme(name)
    if t is None:
        console.print(f"[red]No such theme[/] {name!r}")
        raise SystemExit(1)
    if t.builtin:
        # Editing a builtin forks it into a shadowing user file.
        console.print(f"[dim]{name} is builtin — saving an editable copy that shadows it.[/]")
    tokens = dict(t.tokens)
    for pair in set_tokens:
        if "=" not in pair:
            console.print(f"[red]Bad --set {pair!r}[/]")
            raise SystemExit(1)
        k, v = pair.split("=", 1)
        tokens[k.strip().lstrip("-")] = v.strip()
    for k in unset_tokens:
        tokens.pop(k.strip().lstrip("-"), None)
    t.tokens = tokens
    if extends_ is not None:
        t.extends = extends_ or None
    try:
        themes.save_theme(t)
    except ValueError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise SystemExit(1)
    console.print(f"[green]✓[/] Theme [bold]{name}[/] updated")


@theme.command("rm")
@click.argument("name")
def theme_rm(name: str):
    """Delete a user theme (a pure builtin can't be deleted)."""
    from relaydeck import themes

    if not themes._theme_path(name).exists() and themes.is_builtin(name):
        console.print(f"[red]✗[/] {name!r} is a builtin theme — cannot delete")
        raise SystemExit(1)
    if themes.delete_theme(name):
        console.print(f"[green]✓[/] Theme [bold]{name}[/] deleted")
        # Clear dangling appearance refs unless a builtin of the same name
        # still resolves (a shadowing file was removed → builtin returns).
        if themes.get_theme(name) is None:
            from relaydeck.preferences import clear_appearance_theme
            cleared = clear_appearance_theme(_theme_home(), name)
            if cleared:
                console.print(f"  [dim]appearance fell back for: {', '.join(cleared)}[/]")
                _notify_appearance_changed(None)
    else:
        console.print(f"[yellow]![/] No such theme {name!r}")


@theme.command("export")
@click.argument("name")
@click.option("--out", type=click.Path(), help="Write to this file instead of stdout.")
def theme_export(name: str, out: str | None):
    """Export a theme as YAML (shareable; re-import with `theme import`)."""
    import yaml

    from relaydeck import themes

    t = themes.get_theme(name)
    if t is None:
        console.print(f"[red]No such theme[/] {name!r}")
        raise SystemExit(1)
    body = yaml.safe_dump(t.to_dict(), sort_keys=True, default_flow_style=False)
    if out:
        Path(out).write_text(body)
        console.print(f"[green]✓[/] Exported {name} → {out}")
    else:
        click.echo(body)


@theme.command("import")
@click.argument("path", type=click.Path(exists=True))
@click.option("--name", help="Override the imported theme's name.")
def theme_import(path: str, name: str | None):
    """Import a theme from a YAML file."""
    import yaml

    from relaydeck import themes

    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        console.print("[red]✗[/] File is not a theme mapping")
        raise SystemExit(1)
    if name:
        data["name"] = name
    # Validate the RAW token mapping first — from_dict() silently drops
    # unknown token names, so a shared theme with a typo'd token would
    # import "successfully" but lose it. Fail loudly instead (parity with
    # the HTTP PUT path).
    try:
        themes.validate_tokens(data.get("tokens") or {})
    except ValueError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise SystemExit(1)
    t = themes.Theme.from_dict(data)
    if not t.name:
        console.print("[red]✗[/] Theme has no name (pass --name)")
        raise SystemExit(1)
    try:
        themes.save_theme(t)
    except ValueError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise SystemExit(1)
    console.print(f"[green]✓[/] Imported theme [bold]{t.name}[/]")


@theme.command("set")
@click.argument("name")
@click.option("--workspace", "-w", help="Apply to one workspace instead of globally.")
def theme_set(name: str, workspace: str | None):
    """Set the active theme (globally, or for one workspace)."""
    from relaydeck import themes
    from relaydeck.preferences import set_appearance

    if themes.get_theme(name) is None:
        from relaydeck import dashboard_commands as dash
        console.print(f"[red]No such theme[/] {name!r}")
        console.print(f"[dim]{dash.theme_catalog_hint(config_home=_theme_home())}[/]")
        raise SystemExit(1)
    set_appearance(_theme_home(), {"theme": name}, workspace)
    scope = f"workspace [cyan]{workspace}[/]" if workspace else "globally"
    console.print(f"[green]✓[/] Theme [bold]{name}[/] active {scope}")
    _notify_appearance_changed(workspace)


@theme.command("appearance")
@click.option("--workspace", "-w", help="Show one workspace's resolved appearance.")
def theme_appearance(workspace: str | None):
    """Show the resolved appearance (theme + density + glow) and scope."""
    from relaydeck.preferences import resolve_appearance

    ap = resolve_appearance(_theme_home(), workspace)
    label = workspace or "(global)"
    console.print(f"[bold]Appearance[/] for {label}: scope=[cyan]{ap.get('scope')}[/]")
    for k in ("theme", "density", "glow"):
        console.print(f"  {k}: {ap.get(k)}")


def _notify_appearance_changed(workspace: str | None) -> None:
    """Best-effort: nudge a running daemon so the dashboard updates live.
    The file write already happened; this just emits the bus event. If
    the daemon is down, the dashboard catches up on its next heartbeat."""
    outcome, _ = _post_to_daemon(
        f"/api/appearance/notify?workspace={workspace}" if workspace
        else "/api/appearance/notify")
    if outcome == _POST_TRANSPORT_FAILED:
        console.print("[dim]  (daemon not reachable — dashboard updates on next refresh)[/]")


# ── relaydeck defaults (model roles) ──────────────────────────────────────


@main.group()
def defaults():
    """Default models per job/role (classifier, voice, image, …).

    A role is a semantic slot resolved like any model spec. Text roles
    fall back to a built-in local default until you set one; modality
    roles (voice/image) must be set before a plugin can use them."""
    pass


@defaults.command("list")
def defaults_list():
    """Show every role, the model it resolves to, and where from."""
    from relaydeck.model_roles import role_status

    home = _get_config_home()
    rows = role_status(home)
    table = Table(title="Model Roles (defaults)")
    table.add_column("Role", style="cyan")
    table.add_column("Capability", style="dim")
    table.add_column("Resolves to")
    table.add_column("Source")
    for r in rows:
        eff = r["effective"] or "[red]unset[/]"
        src = r["source"]
        src_disp = {
            "default": "[green]you set it[/]",
            "fallback": "[dim]built-in fallback[/]",
            "unset": "[yellow]needs config[/]",
        }.get(src, src)
        table.add_row(r["name"], r["capability"], str(eff), src_disp)
    console.print(table)
    console.print("[dim]Set one with:[/] relaydeck defaults set <role> <preset|provider/model>")


@defaults.command("get")
@click.argument("role")
def defaults_get(role: str):
    """Print the model a role resolves to."""
    from relaydeck.model_roles import is_role, resolve_role

    if not is_role(role):
        console.print(f"[red]Unknown role[/] {role!r}")
        raise SystemExit(1)
    spec = resolve_role(role, _get_config_home())
    console.print(spec or "[yellow]unset[/] — set it with `relaydeck defaults set ...`")


@defaults.command("set")
@click.argument("role")
@click.argument("spec")
def defaults_set(role: str, spec: str):
    """Set the default model for ROLE to SPEC (preset|alias|provider/model)."""
    from relaydeck.model_roles import is_role, set_role_default
    from relaydeck.sdk import resolve_model

    if not is_role(role):
        from relaydeck.model_roles import builtin_roles
        names = ", ".join(r.name for r in builtin_roles())
        console.print(f"[red]Unknown role[/] {role!r} — one of: {names}")
        raise SystemExit(1)
    home = _get_config_home()
    try:
        set_role_default(role, spec, home)
    except ValueError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise SystemExit(1)
    try:
        provider, model = resolve_model(spec, home)
        console.print(f"[green]✓[/] role [bold]{role}[/] → {spec} ([dim]{provider}/{model}[/])")
    except Exception as exc:
        console.print(f"[green]✓[/] role [bold]{role}[/] → {spec}")
        console.print(f"  [yellow]warning:[/] {exc}")


@defaults.command("unset")
@click.argument("role")
def defaults_unset(role: str):
    """Clear ROLE's default (reverts to fallback / unset)."""
    from relaydeck.model_roles import is_role, unset_role_default

    if not is_role(role):
        console.print(f"[red]Unknown role[/] {role!r}")
        raise SystemExit(1)
    cleared = unset_role_default(role, _get_config_home())
    if cleared:
        console.print(f"[green]✓[/] cleared default for [bold]{role}[/]")
    else:
        console.print(f"[dim]{role} had no default set[/]")


# ── relaydeck recipe ──────────────────────────────────────────────────────


@main.group()
def recipe():
    """Manage recipes (reusable system-prompt addenda)."""
    pass


@recipe.command("list")
def recipe_list():
    """List available recipes."""
    # Built-in recipes
    builtin = Path(__file__).resolve().parent / "plugins" / "recipes"
    user = _get_config_home() / "recipes"

    console.print("[bold]Built-in recipes:[/]")
    if builtin.exists():
        for f in sorted(builtin.glob("*.md")):
            console.print(f"  • {f.stem}")
    else:
        console.print("  [dim](none)[/]")

    console.print("\n[bold]User recipes:[/]")
    if user.exists():
        for f in sorted(user.glob("*.md")):
            console.print(f"  • {f.stem}")
    else:
        console.print("  [dim](none)[/]")


@recipe.command("show")
@click.argument("name")
def recipe_show(name: str):
    """Show a recipe's content."""
    builtin = Path(__file__).resolve().parent / "plugins" / "recipes" / f"{name}.md"
    user = _get_config_home() / "recipes" / f"{name}.md"

    path = None
    if user.exists():
        path = user
    elif builtin.exists():
        path = builtin

    if path is None:
        console.print(f"[red]✗[/] Recipe [bold]{name}[/] not found")
        sys.exit(1)

    content = path.read_text()
    # Parse frontmatter
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            console.print(Panel(parts[2].strip(), title=name))
            return

    console.print(Panel(content, title=name))


# ── relaydeck usage ───────────────────────────────────────────────────────


@main.command()
@click.argument("agent_id", required=False)
def usage(agent_id: str | None):
    """Show token usage and metering stats."""
    from relaydeck.db import get_usage_summary, open_db

    home = _get_config_home()
    db_path = home / "runtime" / "relaydeck.db"

    if not db_path.exists():
        console.print("[dim]No usage data yet.[/]")
        return

    conn = open_db(str(db_path))
    try:
        rows = get_usage_summary(conn, agent_id=agent_id)
    finally:
        conn.close()

    if not rows:
        console.print("[dim]No usage data recorded yet.[/]")
        return

    table = Table(title="Usage")
    table.add_column("Model", style="cyan")
    table.add_column("Provider")
    table.add_column("Requests", justify="right")
    table.add_column("Prompt", justify="right")
    table.add_column("Completion", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Cost", justify="right")

    for r in rows:
        cost = f"${r['total_cost']:.4f}" if r["total_cost"] else "-"
        table.add_row(
            r["model"], r["provider"],
            str(r["requests"]),
            f"{r['total_prompt']:,}",
            f"{r['total_completion']:,}",
            f"{r['total_tokens']:,}",
            cost,
        )

    console.print(table)


# ── relaydeck status ──────────────────────────────────────────────────────


@main.command()
@click.option("--agent", "agent_id", default=None,
              help="View status as a specific agent (default: $RELAYDECK_AGENT_ID env)")
def status(agent_id: str | None):
    """Snapshot of where you are and who's around.

    Two views, picked automatically:

    \b
    - Agent view (RELAYDECK_AGENT_ID set, or --agent passed): "you are X
      in workspace Y" + unread inbox count + sender list + peer agents
      in the same workspace + daemon reachability.
    - User view (no agent context): daemon URL + active workspace +
      agents in active workspace + plugin counts.

    Designed so an agent can run `relaydeck status` as its first action and
    learn its own situation.
    """
    import json
    import urllib.error
    import urllib.request

    from relaydeck.config import load_workspace_registry
    from relaydeck.messages import list_inbox
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.state import get_current_workspace, get_daemon_url

    me = agent_id or os.environ.get("RELAYDECK_AGENT_ID")
    home = _get_config_home()
    orch = get_orchestrator(home)

    # ── Daemon reachability (HEAD-equivalent via /api/agents) ──
    daemon_url = get_daemon_url()
    daemon_up = False
    try:
        # Use the unauthenticated /healthz probe so this check works even
        # if the user's token has rotated and the CLI is reading a stale
        # one — daemon reachability and auth-validity are independent.
        req = urllib.request.Request(daemon_url.rstrip("/") + "/healthz")
        with urllib.request.urlopen(req, timeout=2, context=_daemon_ssl_context()) as r:
            r.read(1)
            daemon_up = True
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        daemon_up = False

    # ── Agent view ──────────────────────────────────────────────
    if me:
        agent_row = orch.get_agent(me)
        if agent_row is None:
            console.print(
                f"[red]✗[/] Agent [bold]{me}[/] is not registered. "
                f"(RELAYDECK_AGENT_ID may point at a deleted agent.)"
            )
            raise SystemExit(1)

        my_ws = agent_row.get("workspace") or "—"
        my_type = agent_row.get("type") or "?"
        my_status = agent_row.get("status") or "?"

        console.print(
            f"[bold cyan]{me}[/]  "
            f"[dim]{my_type}[/]  "
            f"[{('green' if my_status == 'running' else 'dim')}]{my_status}[/]"
            f"  in workspace [bold]{my_ws}[/]"
        )

        # Inbox
        unread = list_inbox(me, unread=True, limit=50, db_path=orch.db_path)
        if unread:
            senders = sorted({m.from_id for m in unread})
            console.print(
                f"  inbox:   [yellow]{len(unread)} unread[/] "
                f"(from {', '.join(senders)})"
            )
        else:
            console.print("  inbox:   [dim]empty[/]")

        # Peers
        peers = [
            a for a in orch.list_agents()
            if (a.get("workspace") or "") == my_ws and a["id"] != me
        ]
        console.print(
            f"  peers:   {len(peers)} other agent" + ("" if len(peers) == 1 else "s")
            + " in this workspace"
        )

        if peers:
            console.print()
            table = Table(show_header=True, header_style="dim")
            table.add_column("ID", style="cyan")
            table.add_column("Type")
            table.add_column("Status")
            table.add_column("Purpose")
            status_styles = {"running": "green", "stopped": "dim",
                             "errored": "red", "pending": "yellow"}
            for p in peers:
                s = p.get("status") or ""
                table.add_row(
                    p["id"], p.get("type", ""),
                    f"[{status_styles.get(s, '')}]{s}[/]",
                    p.get("purpose") or "[dim]—[/]",
                )
            console.print(table)
        console.print()
        console.print(
            "  daemon:  "
            + (f"[green]✓[/] {daemon_url}" if daemon_up
               else f"[red]✗ unreachable[/] ({daemon_url})")
        )
        return

    # ── User view ───────────────────────────────────────────────
    active_ws = get_current_workspace()
    console.print(
        "[bold]relaydeck[/]  "
        + (f"[green]✓ daemon[/] {daemon_url}" if daemon_up
           else f"[red]✗ daemon unreachable[/] ({daemon_url})")
    )

    workspaces = load_workspace_registry()
    if active_ws:
        console.print(f"  active workspace: [bold cyan]{active_ws}[/]")
    elif workspaces:
        console.print(
            f"  active workspace: [dim](unset — defaulting to {workspaces[0].name})[/]"
        )
    else:
        console.print("  active workspace: [dim](no workspaces registered)[/]")

    agents = orch.list_agents()
    scoped = [a for a in agents if (a.get("workspace") or "") == (active_ws or "")]
    running_total = sum(1 for a in agents if a.get("status") == "running")
    console.print(
        f"  agents:  {len(scoped)} in active workspace, {running_total} "
        f"running across all workspaces"
    )

    # Plugins quick summary (without re-importing the full registry).
    try:
        from relaydeck.plugin import get_registry
        from relaydeck.plugin_disabled import disabled_set
        reg = get_registry(home)
        # Don't load — `discovered_all` only returns what was loaded
        # this process. From a fresh CLI invocation that's empty, so
        # walk the disk directly.
        if not reg.discovered_all():
            reg.discover()
        loaded = sum(1 for e in reg.discovered_all() if e.name not in disabled_set())
        total = len(reg.discovered_all())
        disabled = total - loaded
        console.print(
            f"  plugins: {loaded}/{total} loaded"
            + (f", [yellow]{disabled} disabled[/]" if disabled else "")
        )
    except Exception:
        pass

    # Model roles: how many have an operator default, and whether any
    # enabled plugin needs a role that's unconfigured + has no fallback.
    try:
        from relaydeck.model_roles import (
            builtin_roles,
            effective_spec,
            load_role_defaults,
        )
        from relaydeck.plugin_disabled import disabled_set
        set_count = len(load_role_defaults(home))
        console.print(
            f"  roles:   {set_count}/{len(builtin_roles())} configured"
            "  [dim](relaydeck defaults list)[/]"
        )
        unmet: dict[str, list[str]] = {}
        for e in reg.discovered_all():
            if e.name in disabled_set():
                continue
            for role in getattr(getattr(e, "manifest", None),
                                "required_model_roles", ()) or ():
                if effective_spec(role, home) is None:
                    unmet.setdefault(role, []).append(e.name)
        for role, plugins in sorted(unmet.items()):
            console.print(
                f"           [yellow]⚠ role '{role}' needed by "
                f"{', '.join(plugins)} but unset[/] — "
                f"relaydeck defaults set {role} <model>"
            )
    except Exception:
        pass

    if scoped:
        console.print()
        table = Table(show_header=True, header_style="dim")
        table.add_column("ID", style="cyan")
        table.add_column("Type")
        table.add_column("Status")
        status_styles = {"running": "green", "stopped": "dim",
                         "errored": "red", "pending": "yellow"}
        for a in scoped:
            s = a.get("status") or ""
            table.add_row(
                a["id"], a.get("type", ""),
                f"[{status_styles.get(s, '')}]{s}[/]",
            )
        console.print(table)


# ── relaydeck doctor ──────────────────────────────────────────────────────


@main.command()
@click.option("--check", is_flag=True, help="Only check for a newer release; don't upgrade.")
def update(check: bool):
    """Upgrade relaydeck in place to the latest GitHub release (`uv tool
    upgrade`). `--check` just reports whether a newer release exists."""
    import shutil
    import subprocess

    from relaydeck import __version__
    from relaydeck.version_check import check_for_update

    console.print(f"Installed: [bold]{__version__}[/]")
    info = check_for_update(__version__, force=True)
    latest = info.get("latest")
    if latest:
        console.print(f"Latest release: [bold]{latest}[/]")
        if not info.get("update_available"):
            console.print("[green]✓ Already up to date.[/]")
            return
    else:
        console.print("[yellow]![/] Couldn't determine the latest release "
                      "(offline, or none cut yet).")

    if check:
        if info.get("update_available"):
            console.print(f"[yellow]Update available:[/] {__version__} → {latest}. "
                          "Run [cyan]relaydeck update[/] to upgrade.")
        return

    if not shutil.which("uv"):
        console.print("[red]✗ uv not found[/] — install it (https://astral.sh/uv) "
                      "or re-run the relaydeck install script.")
        raise SystemExit(1)

    cmd = os.environ.get(
        "RELAYDECK_UPDATE_CMD", "uv tool install --reinstall relaydeck",
    ).split()
    console.print(f"[dim]$ {' '.join(cmd)}[/]")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        console.print("[red]✗ Upgrade failed.[/]")
        raise SystemExit(rc)
    console.print("[green]✓ Upgraded.[/] Restart the daemon to load it: "
                  "[cyan]relaydeck daemon stop && relaydeck daemon start[/] "
                  "(or use the dashboard's Update banner).")


@main.command()
@click.option(
    "--fix",
    is_flag=True,
    help=(
        "Heal drifted hook integrations: uninstall orphaned halves, "
        "re-render outdated script bodies (idempotent reinstall)."
    ),
)
def doctor(fix: bool):
    """Self-diagnostic. Checks config, daemon health, auth, DB,
    plugins, workspaces, and the active workspace resolution.

    A green wall means you can run any relaydeck command without
    surprises. Yellow / red lines tell you exactly what to fix."""
    import urllib.error
    import urllib.request

    from relaydeck.auth import read_token
    from relaydeck.state import (
        get_daemon_ca,
        get_daemon_url,
        resolve_workspace_source,
    )

    home = _get_config_home()
    issues = []

    # Config home
    if not home.exists():
        issues.append(("warn", "Config directory missing — run `relaydeck serve` to initialize"))
    else:
        console.print(f"[green]✓[/] Config: {home}")

    # cwd + active workspace resolution. The most actionable info
    # for a confused operator is "what workspace will my next relaydeck
    # command target, and why".
    cwd = Path.cwd()
    console.print(f"[dim]·[/] cwd: {cwd}")
    active_ws, active_src = resolve_workspace_source()
    src_label = {
        "env":              "via $RELAYDECK_WORKSPACE",
        "cwd":              "inferred from cwd",
        "state":            "via `relaydeck workspace set`",
        "registry-default": "registry default (only one registered)",
        "unset":            "none — pass --workspace or run `relaydeck workspace set`",
    }.get(active_src, active_src)
    if active_ws:
        console.print(f"[green]✓[/] Active workspace: [bold]{active_ws}[/] [dim]({src_label})[/]")
    else:
        console.print(f"[yellow]○[/] Active workspace: [dim]none — {src_label}[/]")

    # Daemon reachability — most relaydeck commands route through HTTP,
    # so this matters more than any other check.
    daemon_url = get_daemon_url()
    try:
        ctx = _daemon_ssl_context()
        with urllib.request.urlopen(
            daemon_url.rstrip("/") + "/healthz", timeout=2, context=ctx,
        ) as r:
            r.read(1)
        console.print(f"[green]✓[/] Daemon: reachable at {daemon_url}")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        console.print(
            f"[yellow]○[/] Daemon: NOT reachable at {daemon_url}\n"
            f"    [dim]{type(exc).__name__}: {exc}[/]\n"
            f"    [dim]Start with [bold]relaydeck serve[/], or set "
            f"RELAYDECK_DAEMON_URL if it's on a different port.[/]"
        )

    # Auth token
    if read_token():
        console.print("[green]✓[/] Auth token: present")
    else:
        issues.append((
            "warn",
            "Auth token missing — `relaydeck serve` mints one on first boot.",
        ))

    # TLS CA pin
    if get_daemon_ca():
        console.print(f"[green]✓[/] TLS CA pin: {get_daemon_ca()}")

    # Agents
    agents_dir = home / "agents"
    if agents_dir.exists():
        agent_count = len(list(agents_dir.glob("*.yaml")))
        console.print(f"[green]✓[/] Agents: {agent_count} defined")
    else:
        console.print("[dim]○[/] Agents: none defined yet")

    # Database
    db_path = home / "runtime" / "relaydeck.db"
    if db_path.exists():
        from relaydeck.db import open_db
        try:
            conn = open_db(str(db_path))
            agent_rows = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            event_rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            usage_rows = conn.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
            # Zombie detection: agents marked "running" but daemon may
            # have no live PTY. We can't fully verify without asking
            # the daemon for `get_running_instance`, but we can count
            # the row count of agents in `running` state and warn if
            # the daemon is down (which would mean all of them are
            # zombies). Useful for the "tmux session dies" case.
            running_rows = conn.execute(
                "SELECT COUNT(*) FROM agents WHERE status = 'running'"
            ).fetchone()[0]
            conn.close()
            console.print(
                f"[green]✓[/] Database: {agent_rows} agents "
                f"({running_rows} marked running), {event_rows} events, "
                f"{usage_rows} usage records"
            )
        except Exception as e:
            issues.append(("error", f"Database error: {e}"))
    else:
        console.print("[dim]○[/] Database: not initialized yet")

    # Plugins
    from relaydeck.plugin import get_registry
    registry = get_registry(home)
    try:
        discovered = registry.discover()
        console.print(f"[green]✓[/] Plugins discovered: {len(discovered)}")
        for entry in discovered:
            console.print(f"    • {entry.name} ({entry.category}) — {entry.source}")
        # Recommended-bundle coverage: flag any default-bundle plugin that
        # isn't present (advisory — the daemon still boots without it).
        from relaydeck.bundles import DEFAULT_BUNDLE, missing_from_bundle
        present = {e.name for e in discovered}
        missing_bundle = missing_from_bundle(present, DEFAULT_BUNDLE)
        if missing_bundle:
            issues.append((
                "warn",
                f"Default bundle missing {len(missing_bundle)} plugin(s): "
                f"{', '.join(missing_bundle)} — run `relaydeck plugin bundle default`",
            ))
        else:
            console.print("[green]✓[/] Default plugin bundle complete")
    except Exception as e:
        issues.append(("error", f"Plugin discovery error: {e}"))

    # Harness CLIs — which coding-agent binaries are actually installed. A
    # plugin being loaded doesn't mean its CLI is present; an agent can't run
    # until the binary it wraps is on PATH (relaydeck-native wraps pi).
    try:
        from relaydeck.harness_options import build_harness_catalog
        native = [h for h in build_harness_catalog(home) if h.get("kind") == "native"]
        operator = [h for h in build_harness_catalog(home) if h.get("kind") == "operator"]
        installed = [h for h in native if h.get("cli_installed")]
        missing = [h for h in native if not h.get("cli_installed")]
        op_missing = [h for h in operator if not h.get("cli_installed")]
        if installed:
            console.print(
                f"[green]✓[/] Harness CLIs: {len(installed)} installed "
                f"[dim]({', '.join(h['cli'] for h in installed)})[/]"
            )
        else:
            issues.append((
                "warn",
                "No harness CLIs on PATH — agents can't run until you install one "
                "(pi / claude / codex / opencode). relaydeck-native also needs pi.",
            ))
            console.print(
                "[yellow]![/] Harness CLIs: [yellow]none installed[/] — install "
                "pi / claude / codex / opencode before spawning agents"
            )
        if missing:
            console.print(f"    [dim]not found: {', '.join(h['cli'] for h in missing)}[/]")
        if op_missing:
            console.print(
                "    [yellow]relaydeck-native[/] needs [bold]pi[/] on PATH "
                f"[dim]({op_missing[0].get('install_hint', 'npm i -g @mariozechner/pi-coding-agent')})[/]"
            )
    except Exception as e:
        issues.append(("warn", f"Harness CLI check error: {e}"))

    # Model roles (defaults-for-jobs)
    try:
        from relaydeck.model_roles import (
            builtin_roles,
            effective_spec,
            load_role_defaults,
        )
        from relaydeck.plugin_disabled import disabled_set
        set_count = len(load_role_defaults(home))
        console.print(
            f"[green]✓[/] Model roles: {set_count}/{len(builtin_roles())} "
            "configured [dim](relaydeck defaults list)[/]"
        )
        seen: set[str] = set()
        for entry in discovered:
            if entry.name in disabled_set():
                continue
            for role in getattr(getattr(entry, "manifest", None),
                                "required_model_roles", ()) or ():
                if role in seen or effective_spec(role, home) is not None:
                    continue
                seen.add(role)
                # Surface as info, not an `issues` error — an unset
                # modality role is an onboarding nudge, not a broken daemon.
                console.print(
                    f"    [yellow]⚠ role '{role}' is needed by {entry.name} "
                    f"but unset[/] — relaydeck defaults set {role} <model>"
                )
    except Exception:
        pass

    # Local model servers (advisory — an onboarding nudge, never an error)
    try:
        from relaydeck.local_providers import detect_local_providers
        local = detect_local_providers(home)
        if local:
            for c in local:
                tag = "configured" if c.already_configured else "not added yet"
                console.print(
                    f"[green]✓[/] Detected {c.label} at {c.base_url} "
                    f"({c.model_count} models) [dim]— {tag}[/]"
                )
                if not c.already_configured:
                    console.print(
                        f"    [yellow]⚠ {c.label} is running but not added[/] "
                        f"— relaydeck provider detect --add {c.kind}"
                    )
    except Exception:
        pass

    # Workspaces
    from relaydeck.config import load_workspace_registry
    workspaces = load_workspace_registry()
    console.print(f"[green]✓[/] Workspaces: {len(workspaces)} registered")
    for w in workspaces:
        plugins_str = ", ".join(w.plugins) if w.plugins else "none"
        marker = " [green]●[/]" if w.name == active_ws else ""
        console.print(f"    • {w.name}{marker} → {w.path} [plugins: {plugins_str}]")

    # Vendor hook integrations — surface orphaned registrations after a
    # ~/.relaydeck wipe (script gone, ~/.claude/settings.json still wired).
    try:
        from relaydeck import integrations
        from relaydeck.integrations.claude import _HOOK_VERSION

        integrations.register_builtin_integrations()
        if fix:
            fixed = integrations.fix_orphaned_hook_integrations()
            for name, action in fixed:
                if action == "regenerated":
                    console.print(
                        f"[green]✓[/] Re-rendered outdated hook: [bold]{name}[/]"
                    )
                elif action == "uninstalled":
                    console.print(
                        f"[green]✓[/] Auto-uninstalled orphaned hook: [bold]{name}[/]"
                    )
                elif action.endswith("-failed"):
                    # The helper logged the exception text already; surface
                    # the failure in doctor so the operator sees that --fix
                    # tried and couldn't recover (rather than printing
                    # nothing here and re-printing the same warning below
                    # with the same fix hint).
                    op = action[: -len("-failed")]
                    console.print(
                        f"[red]✗[/] Could not {op} [bold]{name}[/] "
                        f"[dim](see daemon log for exception)[/]"
                    )
                    issues.append(
                        ("warn", f"Integration {name}: {op} failed")
                    )
                else:
                    console.print(f"[dim]?[/] {name}: {action}")
        console.print("\n[bold]Integrations:[/]")
        for it in integrations.all_integrations():
            try:
                st = integrations.integration_state(it)
            except Exception as exc:
                console.print(f"  [red]✗[/] {it.name:<12} error: {exc}")
                continue
            kind = getattr(it, "kind", "hook")
            if st == "installed":
                extra = ""
                if it.name == "claude" and kind == "hook":
                    extra = f" (hook v{_HOOK_VERSION})"
                console.print(f"  [green]✓[/] {it.name:<12} installed{extra}")
            elif st == "not-installed":
                console.print(f"  [dim]·[/] {it.name:<12} not installed")
            elif st in integrations._REGENERATE_STATES:
                # Both halves wired correctly; only the script body diverges.
                # The right fix is REINSTALL (idempotent re-render), not
                # uninstall — claude reads the hook script fresh on each
                # event, so an on-disk regenerate is picked up by live agents
                # on their very next hook fire without a respawn. Doctor's
                # branch keys off the SAME set the fix path uses
                # (`_REGENERATE_STATES` / `_ORPHAN_STATES`) so a future state
                # addition can't drift the two surfaces — fix and render
                # agree on the partition by construction.
                console.print(f"  [yellow]⚠[/] {it.name:<12} {st}")
                console.print(
                    f"                 fix: relaydeck integration install {it.name}"
                )
                issues.append(("warn", f"Integration {it.name}: {st}"))
            elif st in integrations._ORPHAN_STATES:
                console.print(f"  [yellow]⚠[/] {it.name:<12} {st}")
                console.print(
                    f"                 fix: relaydeck integration uninstall {it.name}"
                )
                issues.append(("warn", f"Integration {it.name}: {st}"))
            else:
                # Unknown state from a third-party integration or a future
                # protocol expansion. Surface it as a warn so `relaydeck
                # doctor` in CI exits non-zero rather than silently treating
                # an unrecognized state as healthy.
                console.print(f"  [dim]?[/] {it.name:<12} {st}")
                issues.append(
                    ("warn", f"Integration {it.name}: unknown state {st!r}")
                )
    except Exception as e:
        issues.append(("warn", f"Integration check error: {e}"))

    if issues:
        console.print("\n[bold yellow]Issues:[/]")
        for level, msg in issues:
            style = "red" if level == "error" else "yellow"
            console.print(f"  [{style}]⚠[/] {msg}")
        sys.exit(1)
    console.print("\n[bold green]All checks passed.[/]")


# ── relaydeck provider ────────────────────────────────────────────────────


@main.group()
def provider():
    """Inspect model catalogs from provider plugins."""
    pass


def _load_providers_lazy():
    """Make sure provider plugins are loaded for this CLI invocation.
    The serve daemon does this in start-up; for one-off CLI commands we
    need to do it here so list_providers() returns anything."""
    from relaydeck.plugin import PluginContext, get_registry, list_providers
    reg = get_registry(_get_config_home())
    if not reg.all():
        try:
            reg.load_all(PluginContext(config_home=_get_config_home()))
        except Exception:
            pass
    return list_providers()


@provider.command("list")
def provider_list():
    """List registered provider plugins and their catalog size."""
    providers = _load_providers_lazy()
    if not providers:
        console.print("[dim]No provider plugins loaded.[/]")
        return
    import time as _t
    table = Table(title="Providers")
    table.add_column("Name", style="cyan")
    table.add_column("Models", justify="right")
    table.add_column("Refreshed", style="dim")
    table.add_column("Description")
    for p in providers:
        ts = p.last_refresh_ts()
        age = "never" if not ts else _fmt_age(_t.time() - ts)
        table.add_row(p.provider_name, str(len(p.list_models())), age, p.description)
    console.print(table)


def _fmt_age(seconds: float) -> str:
    if seconds < 60: return f"{int(seconds)}s ago"
    if seconds < 3600: return f"{int(seconds/60)}m ago"
    if seconds < 86400: return f"{int(seconds/3600)}h ago"
    return f"{int(seconds/86400)}d ago"


@provider.command("detect")
@click.option("--add", "add_name", metavar="NAME",
              help="Register the detected endpoint of this kind (e.g. --add ollama).")
def provider_detect(add_name: str | None):
    """Detect model servers running locally (Ollama, vLLM, LM Studio).

    Probes the standard local ports and lists what's reachable. Pass
    `--add <kind>` to register that endpoint as a provider."""
    _load_providers_lazy()  # so already-configured detection is accurate
    from relaydeck.local_providers import detect_local_providers
    cands = detect_local_providers(_get_config_home())
    if not cands:
        console.print("[dim]No local model servers detected on the usual ports "
                      "(ollama 11434, vllm 8000, lmstudio 1234).[/]")
        return
    table = Table(title="Detected local model servers")
    table.add_column("Kind", style="cyan")
    table.add_column("Endpoint")
    table.add_column("Models", justify="right")
    table.add_column("Status", style="dim")
    for c in cands:
        status = "configured" if c.already_configured else "not added"
        table.add_row(c.label, c.base_url, str(c.model_count), status)
    console.print(table)

    if not add_name:
        console.print("[dim]Add one with[/] [bold]relaydeck provider detect --add <kind>[/]")
        return
    match = next((c for c in cands if c.kind == add_name or c.suggested_name == add_name), None)
    if match is None:
        console.print(f"[red]✗[/] No detected endpoint of kind [bold]{add_name}[/]")
        raise SystemExit(1)
    if match.already_configured:
        console.print(f"[yellow]○[/] [bold]{match.suggested_name}[/] is already configured.")
        return
    from relaydeck import providers_extra
    from relaydeck.plugin import get_provider
    if get_provider(match.suggested_name) is not None:
        console.print(f"[yellow]○[/] A provider named [bold]{match.suggested_name}[/] already exists.")
        return
    providers_extra.add_custom({
        "name": match.suggested_name, "base_url": match.base_url, "api": match.api,
        "key_env": "" if match.api == "ollama" else f"{match.suggested_name.upper()}_API_KEY",
        "description": f"{match.label} (local)",
    }, _get_config_home())
    console.print(f"[green]✓[/] Added provider [bold]{match.suggested_name}[/] "
                  f"({match.base_url}, {match.model_count} models)")


@provider.command("refresh")
@click.argument("name", required=False)
def provider_refresh(name: str | None):
    """Force-refresh one provider's catalog, or all when no name is given."""
    providers = _load_providers_lazy()
    targets = [p for p in providers if (name is None or p.provider_name == name)]
    if not targets:
        console.print(f"[yellow]No provider matched '{name}'.[/]")
        return
    for p in targets:
        try:
            models = p.refresh()
            console.print(f"[green]✓[/] {p.provider_name}: {len(models)} models")
        except Exception as exc:
            console.print(f"[red]✗[/] {p.provider_name}: {exc}")


@provider.command("models")
@click.argument("name")
@click.option("--limit", "-n", default=40, help="Max rows to display")
@click.option("--grep", "-g", default=None, help="Filter ids by substring")
def provider_models(name: str, limit: int, grep: str | None):
    """List the catalog for one provider."""
    providers = _load_providers_lazy()
    p = next((x for x in providers if x.provider_name == name), None)
    if p is None:
        console.print(f"[red]Provider '{name}' not registered.[/]")
        return
    models = p.list_models()
    if grep:
        models = [m for m in models if grep.lower() in m.id.lower()]
    models = models[:limit]
    if not models:
        console.print("[dim]No models matched.[/]")
        return
    table = Table(title=f"{name} models")
    table.add_column("ID", style="cyan")
    table.add_column("Context", justify="right")
    table.add_column("Prompt $/1M", justify="right")
    table.add_column("Compl  $/1M", justify="right")
    for m in models:
        table.add_row(
            m.id,
            str(m.context_length) if m.context_length else "—",
            f"${m.prompt_price:.2f}" if m.prompt_price is not None else "—",
            f"${m.completion_price:.2f}" if m.completion_price is not None else "—",
        )
    console.print(table)


@provider.command("check")
@click.argument("preset_name")
def provider_check(preset_name: str):
    """Validate a preset's model against its provider's catalog."""
    _load_providers_lazy()
    from relaydeck.config import load_model_presets
    from relaydeck.plugin import get_provider
    p = next((x for x in load_model_presets() if x.name == preset_name), None)
    if p is None:
        console.print(f"[red]Preset '{preset_name}' not found.[/]")
        return
    prov = get_provider(p.provider)
    if prov is None:
        console.print(f"[yellow]⚠[/] Provider '{p.provider}' has no plugin "
                      f"— can't validate. Preset will run as-is.")
        return
    ok, suggestion = prov.validate(p.model)
    if ok:
        console.print(f"[green]✓[/] [bold]{preset_name}[/] · "
                      f"{p.provider}/{p.model} is in the catalog.")
    else:
        console.print(f"[red]✗[/] [bold]{preset_name}[/] · "
                      f"{p.provider}/{p.model} is NOT in the catalog.")
        if suggestion:
            console.print(f"  [yellow]did you mean[/] {p.provider}/{suggestion}?")


# ── relaydeck workers ─────────────────────────────────────────────────────


@main.group()
def workers():
    """Inspect background workers running inside the daemon."""
    pass


def _workers_via_api():
    """Query the running daemon. Workers only exist inside the live
    daemon, not in a one-shot CLI invocation, so we route through
    the same daemon URL + auth + TLS path as every other CLI →
    daemon call. Returns the parsed list on success, or None on
    transport / daemon error (with a printed message)."""
    from relaydeck.state import get_daemon_url

    outcome, payload = _get_from_daemon("/api/workers", timeout=3)
    if outcome == _POST_OK:
        return payload if isinstance(payload, list) else []
    daemon_url = get_daemon_url()
    if outcome == _POST_TRANSPORT_FAILED:
        console.print(
            f"[red]Couldn't reach daemon at {daemon_url}:[/] {payload}\n"
            f"[dim]Start it with [bold]relaydeck serve[/], or set "
            f"RELAYDECK_DAEMON_URL if it's on a different port.[/]"
        )
    else:  # daemon error
        console.print(f"[red]Daemon refused:[/] {payload}")
    return None


def _fmt_age_seconds(secs: float) -> str:
    if secs <= 0: return "—"
    if secs < 60: return f"{int(secs)}s"
    if secs < 3600: return f"{int(secs/60)}m"
    if secs < 86400: return f"{int(secs/3600)}h"
    return f"{int(secs/86400)}d"


@workers.command("list")
def workers_list():
    """List all live workers with status, last tick, and last error."""
    import time as _t
    rows = _workers_via_api()
    if rows is None: return
    if not rows:
        console.print("[dim]No workers registered.[/]")
        return
    now = _t.time()
    table = Table(title=f"Workers ({len(rows)})")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="cyan")
    table.add_column("Plugin")
    table.add_column("Agent", style="dim")
    table.add_column("Status")
    table.add_column("Last tick", justify="right")
    table.add_column("Ticks", justify="right")
    table.add_column("Error", style="red")
    # Sort so crash_loop / errored bubble to the top — those are the
    # rows operators actually need to see.
    def _priority(r):
        return {
            "crash_loop": 0, "errored": 1, "running": 2,
            "stopped": 3, "idle": 4,
        }.get(r["status"], 99)
    rows = sorted(rows, key=_priority)
    for r in rows:
        status = r["status"]
        if status == "running": status_s = f"[green]{status}[/]"
        elif status == "errored": status_s = f"[red]{status}[/]"
        elif status == "crash_loop":
            restarts = r.get("restart_count", 0)
            status_s = f"[bold red]crash_loop[/] [dim]({restarts})[/]"
        elif status == "stopped": status_s = f"[dim]{status}[/]"
        else: status_s = status
        age = _fmt_age_seconds(now - (r.get("last_tick_at") or 0))
        table.add_row(
            r["id"][:8], r["name"], r["plugin"],
            r.get("agent_id") or "", status_s, age + " ago" if age != "—" else "—",
            str(r.get("tick_count", 0)),
            (r.get("last_error") or "")[:50],
        )
    console.print(table)


@workers.command("retry")
@click.argument("worker_id")
def workers_retry(worker_id: str):
    """Re-arm a worker stuck in crash_loop or errored.

    The supervisor stops restarting after a configurable threshold of
    restarts within a sliding window. Once you've fixed the root cause
    (paged a downstream, restored a file, etc.) this command resets
    the counter and starts the worker again.
    """
    rows = _workers_via_api()
    if rows is None:
        return
    matches = [r for r in rows if r["id"].startswith(worker_id)]
    if not matches:
        console.print(f"[red]No worker matched '{worker_id}'.[/]")
        return
    if len(matches) > 1:
        console.print("[yellow]Multiple matches; use a longer prefix:[/]")
        for m in matches:
            console.print(f"  {m['id'][:8]}  {m['name']}")
        return
    wid = matches[0]["id"]
    outcome, resp = _post_to_daemon(f"/api/workers/{wid}/retry")
    if outcome == _POST_OK:
        console.print(f"[green]✓[/] Worker [bold]{matches[0]['name']}[/] re-armed")
    elif outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {resp}")
        sys.exit(1)
    else:
        console.print(f"[red]Daemon unreachable:[/] {resp}")
        sys.exit(1)


@workers.command("logs")
@click.argument("worker_id")
@click.option("--tail", "-n", default=50, help="Number of log lines")
def workers_logs(worker_id: str, tail: int):
    """Print recent log lines from a worker's ring buffer."""
    # Allow short prefix match for convenience: `relaydeck workers logs abc12345`.
    rows = _workers_via_api()
    if rows is None: return
    matches = [r for r in rows if r["id"].startswith(worker_id)]
    if not matches:
        console.print(f"[red]No worker matched '{worker_id}'.[/]")
        return
    if len(matches) > 1:
        console.print("[yellow]Multiple matches; use a longer prefix:[/]")
        for m in matches:
            console.print(f"  {m['id'][:8]}  {m['name']}")
        return
    wid = matches[0]["id"]
    outcome, logs = _get_from_daemon(
        f"/api/workers/{wid}/logs?tail={tail}", timeout=5,
    )
    if outcome != _POST_OK:
        console.print(f"[red]Couldn't fetch logs:[/] {logs}")
        return
    if not isinstance(logs, list) or not logs:
        console.print(f"[dim]No log lines for {matches[0]['name']}.[/]")
        return
    import time as _t
    for entry in logs:
        ts = _t.strftime("%H:%M:%S", _t.localtime(entry["ts"]))
        level = entry.get("level", "info")
        color = {"warn": "yellow", "error": "red"}.get(level, "white")
        console.print(f"[dim]{ts}[/] [{color}]{level:5s}[/] {entry['msg']}")


@workers.command("tail")
@click.argument("worker_id")
@click.option("--interval", "-i", default=1.5, help="Poll interval (seconds)")
def workers_tail(worker_id: str, interval: float):
    """Follow a worker's logs (poll every --interval seconds)."""
    import time as _t
    rows = _workers_via_api()
    if rows is None: return
    matches = [r for r in rows if r["id"].startswith(worker_id)]
    if not matches:
        console.print(f"[red]No worker matched '{worker_id}'.[/]")
        return
    wid = matches[0]["id"]
    console.print(f"[dim]Tailing {matches[0]['name']} (Ctrl+C to stop)…[/]")
    last_ts = 0.0
    try:
        while True:
            outcome, logs = _get_from_daemon(
                f"/api/workers/{wid}/logs?tail=200", timeout=3,
            )
            if outcome == _POST_OK and isinstance(logs, list):
                for entry in logs:
                    if entry["ts"] <= last_ts:
                        continue
                    ts = _t.strftime("%H:%M:%S", _t.localtime(entry["ts"]))
                    level = entry.get("level", "info")
                    color = {"warn": "yellow", "error": "red"}.get(level, "white")
                    console.print(f"[dim]{ts}[/] [{color}]{level:5s}[/] {entry['msg']}")
                    last_ts = entry["ts"]
            elif outcome != _POST_OK:
                # Transport blip or daemon error — print and keep
                # polling. A transient blip shouldn't tear down the
                # tail; the operator pressed Ctrl-C if they want out.
                console.print(f"[dim]…[/] [red]{logs}[/]")
            _t.sleep(interval)
    except KeyboardInterrupt:
        pass


# ── relaydeck plugin ──────────────────────────────────────────────────────


@main.group()
def plugin():
    """Inspect plugins and configure their settings."""
    pass


@plugin.command("list")
@click.option("--mine", is_flag=True,
              help="Only YOUR custom plugins — local/workspace/installed, not the bundled ones.")
def plugin_list(mine: bool):
    """List loaded plugins. The Source/Trust columns tell bundled (shipped)
    apart from your own: `user`/`workspace:*` + `local` trust are the
    private ones you author and manage; `--mine` filters to just those."""
    from relaydeck.plugin import PluginContext, effective_trust_level, get_registry
    from relaydeck.plugin_settings import normalize_schema
    reg = get_registry(_get_config_home())
    if not reg.all():
        try: reg.load_all(PluginContext(config_home=_get_config_home()))
        except Exception: pass
    entries = [e for e in reg.all() if not mine or e.source != "builtin"]
    if mine and not entries:
        console.print(
            "[dim]No custom plugins yet. Scaffold one with "
            "`relaydeck plugin new <name> --local`.[/]"
        )
        return
    table = Table(title="Your plugins" if mine else "Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Category")
    table.add_column("Version", style="dim")
    table.add_column("Source", style="dim")
    table.add_column("Trust", style="dim")
    table.add_column("Settings", justify="right")
    table.add_column("Description")
    for e in entries:
        try:
            n = len(normalize_schema(e.instance.get_settings_schema()))
        except Exception:
            n = 0
        trust = effective_trust_level(e)
        trust_styled = (
            f"[green]{trust}[/]" if trust in ("bundled", "signed", "curated")
            else f"[yellow]{trust}[/]" if trust == "local"
            else f"[red]{trust}[/]"
        )
        table.add_row(
            e.name, e.category, e.version, e.source, trust_styled,
            (str(n) if n else "—"),
            (getattr(e.instance, "description", "") or "")[:50],
        )
    console.print(table)


@plugin.command("bundle")
@click.argument("name", required=False)
def plugin_bundle(name: str | None):
    """List recommended plugin bundles, or show one bundle's status.

    A bundle is a pinned set of official plugins (plugins/bundle.toml). With no
    NAME, lists all bundles. With a NAME (e.g. `default`), shows each plugin in
    the bundle and whether it's present in this install."""
    from relaydeck.bundles import get_bundle, load_bundles
    from relaydeck.plugin import get_registry

    bundles = load_bundles()
    if not bundles:
        console.print("[dim]No bundles defined (plugins/bundle.toml missing).[/]")
        return
    if not name:
        t = Table(title="Plugin bundles")
        t.add_column("Bundle", style="cyan")
        t.add_column("Plugins", justify="right")
        t.add_column("Description")
        for b in bundles.values():
            t.add_row(b.name, str(len(b.plugins)), b.description)
        console.print(t)
        console.print("[dim]Run `relaydeck plugin bundle <name>` for per-plugin status.[/]")
        return
    bundle = get_bundle(name)
    if bundle is None:
        console.print(f"[red]Unknown bundle '{name}'. Known: {', '.join(bundles)}[/]")
        raise SystemExit(1)
    reg = get_registry(_get_config_home())
    present = {e.name for e in reg.discover()}
    t = Table(title=f"Bundle: {name}")
    t.add_column("Plugin", style="cyan")
    t.add_column("Status")
    for p in bundle.plugins:
        ok = p in present
        t.add_row(p, "[green]present[/]" if ok else "[red]missing[/]")
    console.print(t)
    missing = [p for p in bundle.plugins if p not in present]
    if missing:
        console.print(f"[yellow]{len(missing)} missing:[/] {', '.join(missing)}")
    else:
        console.print("[green]All bundle plugins present.[/]")


@plugin.command("show")
@click.argument("name")
def plugin_show(name: str):
    """Show a plugin's metadata + current settings (with source per key)."""
    from relaydeck.plugin import PluginContext, get_registry
    from relaydeck.plugin_settings import get_setting, normalize_schema, value_source
    reg = get_registry(_get_config_home())
    if not reg.all():
        try: reg.load_all(PluginContext(config_home=_get_config_home()))
        except Exception: pass
    entry = next((e for e in reg.all() if e.name == name), None)
    if entry is None:
        console.print(f"[red]Plugin '{name}' not found.[/]")
        return
    from relaydeck.plugin import effective_trust_level
    trust = effective_trust_level(entry)
    console.print(
        f"[bold cyan]{entry.name}[/]  [dim]({entry.category} · "
        f"v{entry.version} · {entry.source} · trust={trust})[/]"
    )
    desc = getattr(entry.instance, "description", "") or ""
    if desc:
        console.print(f"  [dim]{desc}[/]")
    schema = normalize_schema(entry.instance.get_settings_schema())
    if not schema:
        console.print("\n[dim]No configurable settings.[/]")
        return
    console.print("\n[bold]Settings:[/]")
    t = Table(show_header=True)
    t.add_column("Key", style="cyan")
    t.add_column("Type", style="dim")
    t.add_column("Value")
    t.add_column("Source", style="dim")
    t.add_column("Description", style="dim")
    for f in schema:
        v = get_setting(name, f["key"], f.get("default"))
        src = value_source(name, f["key"])
        src_color = {"env": "yellow", "yaml": "green", "default": "dim"}.get(src, "dim")
        t.add_row(
            f["key"], f["type"],
            (str(v) if v not in (None, "") else "[dim]—[/]"),
            f"[{src_color}]{src}[/]",
            (f.get("description") or "")[:50],
        )
    console.print(t)


@plugin.command("info")
@click.argument("name")
def plugin_info(name: str):
    """Show installed-plugin lock/provenance details."""
    from relaydeck.plugin_lock import load_lock

    entry = load_lock(_get_config_home()).get(name)
    if entry is None:
        console.print(f"[yellow]No lockfile entry for {name}.[/]\n")
        plugin_show.callback(name)  # type: ignore[attr-defined]
        return
    table = Table(title=f"Plugin lock · {name}")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    for key, value in entry.to_dict().items():
        table.add_row(key, ", ".join(value) if isinstance(value, list) else str(value))
    console.print(table)


@plugin.command("verify")
@click.argument("paths", nargs=-1)
def plugin_verify(paths: tuple[str, ...]):
    """Verify installed plugin manifests or local plugin project paths."""
    from relaydeck.plugin_manifest import load_manifest

    if paths:
        manifests = []
        for raw in paths:
            manifest_path = _find_plugin_manifest_path(Path(raw))
            manifest = load_manifest(manifest_path)
            _declared_skill_paths(manifest, manifest_path.parent)
            manifests.append(manifest)
        console.print(
            f"[green]✓[/] verified {len(manifests)} local plugin"
            f"{'' if len(manifests) == 1 else 's'}: "
            f"{', '.join(manifest.name for manifest in manifests)}"
        )
        return

    from relaydeck.plugin import get_registry
    from relaydeck.plugin_lock import verify_lock

    reg = get_registry(_get_config_home())
    discovered = reg.discover()
    manifests = [entry.manifest for entry in discovered if entry.manifest is not None]
    entries = verify_lock(_get_config_home(), manifests)
    console.print(
        f"[green]✓[/] verified {len(manifests)} manifest-backed plugin"
        f"{'' if len(manifests) == 1 else 's'}; "
        f"plugins.lock now has {len(entries)} entr"
        f"{'y' if len(entries) == 1 else 'ies'}."
    )


def _plugin_scaffold_files(slug: str, package: str, pattern: str) -> dict[str, str]:
    """Build the per-pattern scaffold content for a new plugin.

    Returns a dict with keys: ``plugin_toml``, ``plugin_py``, ``test_py``,
    and (skill pattern only) ``skill_md``. Shared by the package scaffold
    and the private/local scaffold so both stay in sync — the only
    difference between tiers is which surrounding files (pyproject, CI,
    tests dir) get written, not the plugin code itself.
    """
    category = pattern if pattern in ("harness", "provider", "skill") else "tool"
    capabilities = (
        '["harnesses.register"]'
        if pattern == "harness"
        else "[]"
        if pattern == "provider"
        else '["events.emit"]'
        if pattern == "skill"
        else '["events.subscribe", "kv.read", "kv.write"]'
    )
    workspace_scoped = "true" if pattern == "skill" else "false"
    skills_section = f'\n[plugin.skills]\n{slug} = "SKILL.md"\n' if pattern == "skill" else ""
    plugin_toml = f"""[plugin]
name = "{slug}"
version = "0.1.0"
description = "{pattern} plugin"
author = ""
license = "MIT"
category = "{category}"
host_api_version = 1
workspace_scoped = {workspace_scoped}
declared_capabilities = {capabilities}
{skills_section}"""
    skill_md = ""
    if pattern == "harness":
        plugin_py = f'''from relaydeck.harness import HarnessAgent
from relaydeck.sdk import Plugin, PluginHost


class ExampleAgent(HarnessAgent):
    """Wraps an external CLI as a relaydeck agent."""

    CLI = "{slug}"
    DEFAULT_ARGS: list[str] = []


class ExamplePlugin(Plugin):
    def on_load(self, host: PluginHost) -> None:
        host.harnesses.register("{slug}", ExampleAgent)


PLUGIN = ExamplePlugin()
'''
        test_py = f'''from relaydeck.testing import MockHost
from {package}.plugin import PLUGIN


def test_registers_harness():
    host = MockHost(name="example")
    PLUGIN.on_load(host)
    assert host.harnesses.registrations[0][0] == "{slug}"
'''
    elif pattern == "provider":
        provider_name = slug.replace("-", "_")
        plugin_py = f'''from relaydeck.provider import ModelEntry, ProviderPlugin


class ExampleProvider(ProviderPlugin):
    """Model catalog provider example."""

    name = "{slug}"
    provider_name = "{slug}"
    version = "0.1.0"
    description = "{slug} model catalog"
    key_env = "{provider_name.upper()}_API_KEY"

    def fetch_catalog(self) -> list[ModelEntry]:
        return [
            ModelEntry(
                id="example-model",
                display_name="Example Model",
                context_length=128_000,
            )
        ]


PLUGIN = ExampleProvider()
'''
        test_py = f'''from relaydeck.provider import ProviderPlugin
from {package}.plugin import PLUGIN


def test_provider_catalog():
    assert isinstance(PLUGIN, ProviderPlugin)
    models = PLUGIN.fetch_catalog()
    assert models[0].id == "example-model"
'''
    elif pattern == "skill":
        skill_md = f"""---
name: {slug}
description: Guidance for using the {slug} workflow in relaydeck workspaces.
metadata:
  short-description: {slug} workflow guidance
---

# {slug} workflow

Use this skill when a relaydeck workspace needs the {slug} workflow.

## Steps

1. Inspect the current workspace and plugin settings.
2. Use relaydeck CLI/API commands for durable changes.
3. Report the command output and any follow-up action needed.
"""
        plugin_py = f'''from relaydeck.sdk import Plugin, PluginHost


PLUGIN_NAME = "{slug}"


class ExampleSkillPlugin(Plugin):
    """Ships the {slug} SKILL.md to workspaces that enable this plugin."""

    workspace_scoped = True

    def on_load(self, host: PluginHost) -> None:
        self.host = host

    def refresh_skill(self) -> None:
        """Call after changing dynamic skill content or targets."""
        self.host.events.emit("plugin.skills.changed", {{"plugin": PLUGIN_NAME}})


PLUGIN = ExampleSkillPlugin()
'''
        test_py = f'''from pathlib import Path

from relaydeck.plugin_manifest import load_manifest
from relaydeck.testing import MockHost
from {package}.plugin import PLUGIN


def test_declares_workspace_skill():
    manifest = load_manifest(Path("{package}/plugin.toml"))
    assert manifest.skills == {{"{slug}": "SKILL.md"}}
    assert getattr(PLUGIN, "workspace_scoped", False) is True


def test_refresh_emits_skill_changed(tmp_path):
    events = []
    host = MockHost(
        name="{slug}",
        config_home=tmp_path / ".relaydeck",
        declared_capabilities={{"events.emit", "events.subscribe"}},
    )
    host.events.subscribe("*", events.append)
    PLUGIN.on_load(host)
    PLUGIN.refresh_skill()
    assert events[-1].type == "plugin.skills.changed"
'''
    else:
        plugin_py = """from relaydeck.sdk import Event, Plugin, PluginHost


class ExamplePlugin(Plugin):
    def on_load(self, host: PluginHost) -> None:
        self.host = host
        host.events.subscribe("system.startup", self._on_startup)

    def _on_startup(self, event: Event) -> None:
        self.host.kv.set("last_startup", event.ts)


PLUGIN = ExamplePlugin()
"""
        test_py = f"""from relaydeck.testing import MockHost
from {package}.plugin import PLUGIN


def test_loads():
    host = MockHost(name="example")
    PLUGIN.on_load(host)
    host.events.emit("system.startup", {{}})
    assert host.kv.get("last_startup") is not None
"""
    files = {"plugin_toml": plugin_toml, "plugin_py": plugin_py, "test_py": test_py}
    if skill_md:
        files["skill_md"] = skill_md
    return files


@plugin.command("new")
@click.argument("name")
@click.option("--pattern", default="reactor",
              type=click.Choice([
                  "reactor", "workflow", "harness", "provider", "ui", "cli", "skill",
              ]))
@click.option("--local", "local", is_flag=True,
              help="Scaffold a PRIVATE plugin into ~/.relaydeck/plugins/<name>/ "
                   "(plain dir, no package) — never pushed upstream.")
@click.option("--workspace", "workspace", default=None,
              help="Scaffold a PRIVATE plugin into <workspace>/plugins/<name>/ "
                   "(scoped to one workspace).")
def plugin_new(name: str, pattern: str, local: bool, workspace: str | None):
    """Scaffold a new plugin.

    Default: a publishable package `relaydeck-plugin-<name>/` in the current
    directory — for a community (PyPI) or core (in-repo PR) plugin. With
    `--local` or `--workspace`, scaffold a private plain-directory plugin in
    place (just plugin.py + plugin.toml) — the fastest path for a plugin only
    you use, managed in your own git and never pushed upstream.
    """
    import re

    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    if not slug:
        console.print("[red]Plugin name must contain letters or numbers.[/]")
        raise SystemExit(2)
    package = re.sub(r"[^a-z0-9_]+", "_", slug.replace("-", "_")).strip("_")
    if package[:1].isdigit():
        package = f"plugin_{package}"
    files = _plugin_scaffold_files(slug, package, pattern)

    # ── Private / local tier: a plain directory the loader discovers ──
    if local or workspace:
        if local and workspace:
            console.print("[red]Pass either --local or --workspace, not both.[/]")
            raise SystemExit(2)
        if workspace:
            from relaydeck.config import load_workspace_registry
            ws = next(
                (w for w in load_workspace_registry(_get_config_home())
                 if w.name == workspace),
                None,
            )
            if ws is None:
                console.print(f"[red]No such workspace: {workspace}[/]")
                raise SystemExit(1)
            base = Path(ws.path) / "plugins"
            scope = f"workspace {workspace!r}"
        else:
            base = _get_config_home() / "plugins"
            scope = "this daemon (all workspaces)"
        dest = base / slug
        if dest.exists():
            console.print(f"[red]{dest} already exists.[/]")
            raise SystemExit(1)
        dest.mkdir(parents=True)
        (dest / "plugin.toml").write_text(files["plugin_toml"])
        (dest / "plugin.py").write_text(files["plugin_py"])
        if "skill_md" in files:
            (dest / "SKILL.md").write_text(files["skill_md"])
        console.print(f"[green]✓[/] created private plugin [cyan]{slug}[/] at {dest}")
        console.print(f"[dim]Scope: {scope}. Loads as trust=local — never pushed upstream.[/]")
        console.print("[dim]Reload to pick it up; find it with `relaydeck plugin list --mine`.[/]")
        console.print(
            "[dim]Tip: `git init` the dir for history/backups — just don't push it to "
            "relaydeck/relaydeck or PyPI.[/]"
        )
        return

    # ── Package tier (community / core contribution) ──────────────────
    root = Path(f"relaydeck-plugin-{slug}")
    if root.exists():
        console.print(f"[red]{root} already exists.[/]")
        raise SystemExit(1)
    pkg_dir = root / package
    (root / "tests").mkdir(parents=True)
    (root / ".github" / "workflows").mkdir(parents=True)
    (pkg_dir / "static").mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "py.typed").write_text("")
    plugin_py = files["plugin_py"]
    test_py = files["test_py"]
    (pkg_dir / "plugin.toml").write_text(files["plugin_toml"])
    if "skill_md" in files:
        (pkg_dir / "SKILL.md").write_text(files["skill_md"])
    (pkg_dir / "plugin.py").write_text(plugin_py)
    (root / "tests" / "test_plugin.py").write_text(test_py)
    (root / ".gitignore").write_text(
        """.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
dist/
*.egg-info/
"""
    )
    (root / "README.md").write_text(
        f"""# relaydeck-plugin-{slug}

## Development

```sh
relaydeck plugin dev .          # editable install + run tests against your relaydeck
relaydeck plugin publish-check .
```

`tests/test_plugin.py` imports the `{package}` package, so the project must be
installed (editable) before `pytest` can find it — `relaydeck plugin dev .`
does that for you. Running `uv run pytest` directly only works after the
editable install.

> **Note:** until `relaydeck` is published to PyPI, a clean-env `uv sync`
> can't resolve the `relaydeck` dependency. Develop against a local checkout
> via `relaydeck plugin dev` (which uses your installed relaydeck) or the
> commented `[tool.uv.sources]` block in `pyproject.toml`.

Install locally with:

```sh
relaydeck plugin install --editable .
```
"""
    )
    (root / "RELEASE.md").write_text(
        f"""# Release checklist

- [ ] Update version in `{package}/plugin.toml` and `pyproject.toml`.
- [ ] Run `uv run pytest`.
- [ ] Run `relaydeck plugin publish-check .`.
- [ ] Build with `uv build`.
- [ ] Install the wheel in a clean relaydeck environment.
"""
    )
    (root / ".github" / "workflows" / "ci.yml").write_text(
        """name: ci

on:
  pull_request:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: uv sync
      - run: uv run pytest
      - run: uv run relaydeck plugin publish-check .
"""
    )
    (root / "pyproject.toml").write_text(
        f"""[project]
name = "relaydeck-plugin-{slug}"
version = "0.1.0"
description = "{pattern} plugin for relaydeck"
readme = "README.md"
requires-python = ">=3.12"
dependencies = ["relaydeck>=0.1.0"]

# Until `relaydeck` is published to PyPI, a clean-env `uv sync` can't resolve
# the dependency above. Point uv at your local relaydeck checkout to develop
# against it (uncomment + fix the path), or use `relaydeck plugin dev .`,
# which installs this plugin against your already-installed relaydeck:
# [tool.uv.sources]
# relaydeck = {{ path = "/path/to/relaydeck", editable = true }}

[project.entry-points."relaydeck.plugins"]
{slug} = "{package}.plugin:PLUGIN"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["{package}"]

[dependency-groups]
dev = ["pytest>=8"]
"""
    )
    console.print(f"[green]✓[/] created {root}")


@plugin.command("lint")
@click.argument("path", default=".", required=False)
def plugin_lint(path: str):
    """Validate a plugin.toml manifest."""
    from relaydeck.plugin_manifest import ManifestError, load_manifest

    root = Path(path)
    manifest_path = _find_plugin_manifest_path(root)
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ManifestError) as exc:
        console.print(f"[red]✗[/] {exc}")
        raise SystemExit(1)
    console.print(
        f"[green]✓[/] {manifest.name} v{manifest.version} "
        f"declares {len(manifest.declared_capabilities)} capabilities"
    )


@plugin.command("test")
@click.argument("path", default=".", required=False)
def plugin_test(path: str):
    """Run pytest for a plugin project."""
    import subprocess

    proc = subprocess.run(["uv", "run", "pytest", str(Path(path) / "tests")])
    raise SystemExit(proc.returncode)


def _find_plugin_manifest_path(root: Path) -> Path:
    from relaydeck.plugin_install import PluginInstallError, find_plugin_manifest_path

    try:
        return find_plugin_manifest_path(root)
    except PluginInstallError as exc:
        raise SystemExit(str(exc)) from exc


@plugin.command("install")
@click.argument("src")
@click.option(
    "--editable",
    is_flag=True,
    help="Link a local plugin directory instead of copying it, so source edits apply immediately.",
)
def plugin_install(src: str, editable: bool):
    """Install a plugin from a local path, pinned git URL, package name, or
    curated registry name.

    If SRC matches a curated registry entry (see `relaydeck plugin search`), it
    resolves to that entry's pinned package/git spec before installing."""
    from relaydeck.plugin_install import PluginInstallError, install_plugin_source
    from relaydeck.plugin_registry import get_entry

    curated = get_entry(src)
    if curated is not None:
        resolved = curated.install_spec()
        console.print(
            f"[dim]'{src}' is a curated plugin → installing pinned "
            f"{resolved}[/]"
        )
        src = resolved

    try:
        result = install_plugin_source(
            src,
            _get_config_home(),
            editable=editable,
            install_python_package=_install_python_package,
            install_editable_python_package=_install_editable_python_package,
            package_plugin_manifests=_package_plugin_manifests,
        )
    except PluginInstallError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise SystemExit(1) from exc
    if result.installed_via == "package":
        console.print(f"[green]✓[/] installed package plugin(s): {', '.join(result.names)}")
        return
    if result.installed_via == "editable-package":
        console.print(
            f"[green]✓[/] installed editable package plugin(s): "
            f"{', '.join(result.names)}"
        )
        return
    mode = "linked" if result.installed_via == "editable" else "installed"
    console.print(f"[green]✓[/] {mode} {', '.join(result.names)} to {result.dest}")


def _approve_package_plugin(src: str) -> None:
    from relaydeck.plugin_install import PluginInstallError, approve_package_plugin

    try:
        manifests = approve_package_plugin(
            src,
            _get_config_home(),
            package_plugin_manifests=_package_plugin_manifests,
        )
    except PluginInstallError as exc:
        raise SystemExit(str(exc)) from exc
    names = ", ".join(m.name for m in manifests)
    console.print(f"[green]✓[/] installed package plugin(s): {names}")


@plugin.command("dev")
@click.argument("path", default=".", required=False)
@click.option("--no-test", is_flag=True, help="Skip pytest even when tests/ exists.")
def plugin_dev(path: str, no_test: bool):
    """Set up a local plugin checkout for editable development."""
    import subprocess

    root = Path(path)
    manifest = _find_plugin_manifest_path(root)
    project_root = root if (root / "tests").exists() else manifest.parent.parent
    plugin_lint.callback(str(root))  # type: ignore[attr-defined]
    plugin_install.callback(str(root), True)  # type: ignore[attr-defined]
    tests_dir = project_root / "tests"
    if tests_dir.exists() and not no_test:
        proc = subprocess.run(["uv", "run", "pytest", str(tests_dir)])
        if proc.returncode:
            raise SystemExit(proc.returncode)
    console.print(
        "[green]✓[/] development install ready. Restart the daemon to load code changes."
    )


def _is_git_plugin_source(src: str) -> bool:
    if src.startswith(("git+", "ssh://", "git@")) or src.endswith(".git"):
        return True
    if src.startswith("https://"):
        return src.endswith(".git") or src.startswith("https://github.com/")
    return False


def _is_package_plugin_source(src: str) -> bool:
    if src.endswith(".whl"):
        return True
    name = _package_project_name(src)
    return name.startswith("relaydeck-plugin-")


def _package_project_name(src: str) -> str:
    import re

    value = src.strip()
    path = Path(value)
    if path.exists() and path.is_dir():
        try:
            import tomllib

            project = tomllib.loads((path / "pyproject.toml").read_text()).get("project") or {}
            name = str(project.get("name") or "").strip()
            if name:
                return name.replace("_", "-").lower()
        except Exception:
            pass
    if value.endswith(".whl"):
        return Path(value).name.split("-", 1)[0].replace("_", "-").lower()
    value = value.split("[", 1)[0]
    value = re.split(r"[<>=!~]", value, maxsplit=1)[0]
    return value.strip().replace("_", "-").lower()


def _install_python_package(src: str) -> None:
    import shutil
    import subprocess
    import sys

    uv = shutil.which("uv")
    if uv:
        project = _package_project_name(src)
        cmd = [uv, "pip", "install", "--python", sys.executable]
        if project:
            cmd.extend(["--reinstall-package", project])
        cmd.append(src)
        subprocess.run(cmd, check=True)
        return
    subprocess.run([sys.executable, "-m", "pip", "install", src], check=True)


def _install_editable_python_package(src: str) -> None:
    import shutil
    import subprocess
    import sys

    uv = shutil.which("uv")
    if uv:
        subprocess.run(
            [uv, "pip", "install", "--python", sys.executable, "-e", src],
            check=True,
        )
        return
    subprocess.run([sys.executable, "-m", "pip", "install", "-e", src], check=True)


def _uninstall_python_package(src: str) -> None:
    import shutil
    import subprocess
    import sys

    project = _package_project_name(src)
    if not project:
        return
    uv = shutil.which("uv")
    if uv:
        subprocess.run(
            [uv, "pip", "uninstall", "--python", sys.executable, "-y", project],
            check=True,
        )
        return
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", project], check=True)


def _package_plugin_manifests(src: str):
    from relaydeck.plugin_install import _package_plugin_manifests as manifests

    return manifests(src)


def _parse_git_plugin_source(src: str) -> tuple[str, str]:
    raw = src[4:] if src.startswith("git+") else src
    if ".git@" in raw:
        url, ref = raw.rsplit(".git@", 1)
        return f"{url}.git", ref
    if raw.startswith(("http://", "https://")) and "@" in raw:
        url, ref = raw.rsplit("@", 1)
        return url, ref
    return raw, ""


def _git_rev_parse(path: Path) -> str:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return proc.stdout.strip()


@plugin.command("update")
@click.argument("name", required=False)
def plugin_update(name: str | None):
    """Reinstall local/git plugins from their recorded source."""
    from relaydeck.plugin_lock import load_lock

    entries = load_lock(_get_config_home())
    targets = [name] if name else list(entries)
    for target in targets:
        entry = entries.get(target)
        if entry is None:
            console.print(f"[yellow]No lockfile entry for {target}.[/]")
            continue
        if entry.installed_via in ("editable", "editable-package"):
            console.print(f"[dim]Skipping {target}: editable install already points at source.[/]")
            continue
        if entry.installed_via not in ("local", "git", "package"):
            console.print(f"[yellow]Skipping {target}: unsupported source {entry.installed_via}[/]")
            continue
        plugin_install.callback(entry.source, False)  # type: ignore[attr-defined]


@plugin.command("enable")
@click.argument("name")
def plugin_enable(name: str):
    """Enable a plugin globally."""
    from relaydeck.plugin_disabled import set_disabled

    outcome, resp = _post_to_daemon(f"/api/plugins/{name}/enable")
    if outcome == _POST_OK:
        console.print(f"[green]✓[/] enabled {name} in running daemon.")
        if isinstance(resp, dict) and resp.get("message"):
            console.print(f"[dim]{resp['message']}[/]")
        return
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {resp}")
        raise SystemExit(1)

    set_disabled(name, False)
    console.print(
        f"[yellow]daemon unreachable ({resp});[/] enabled {name} on disk. "
        "Restart daemon to apply."
    )


@plugin.command("disable")
@click.argument("name")
def plugin_disable_cmd(name: str):
    """Disable a plugin globally."""
    from relaydeck.plugin_disabled import set_disabled

    outcome, resp = _post_to_daemon(f"/api/plugins/{name}/disable")
    if outcome == _POST_OK:
        console.print(f"[green]✓[/] disabled {name} in running daemon.")
        if isinstance(resp, dict) and resp.get("message"):
            console.print(f"[dim]{resp['message']}[/]")
        return
    if outcome == _POST_DAEMON_ERROR:
        console.print(f"[red]✗[/] {resp}")
        raise SystemExit(1)

    set_disabled(name, True)
    console.print(
        f"[yellow]daemon unreachable ({resp});[/] disabled {name} on disk. "
        "Restart daemon to apply."
    )


@plugin.command("uninstall")
@click.argument("name")
def plugin_uninstall(name: str):
    """Remove a user-installed plugin and its lockfile entry."""
    from relaydeck.plugin_install import uninstall_plugin

    uninstall_plugin(
        name,
        _get_config_home(),
        uninstall_python_package=_uninstall_python_package,
    )
    console.print(f"[green]✓[/] uninstalled {name}")


@plugin.command("search")
@click.argument("query")
@click.option("--curated-only", is_flag=True, help="Only the curated registry; skip PyPI.")
def plugin_search(query: str, curated_only: bool):
    """Search for installable plugins.

    Curated (relaydeck-recommended) entries from the registry are listed first
    and are pinned — install one by name: `relaydeck plugin install <name>`.
    Then PyPI simple-index names matching `relaydeck-plugin-*` are shown as
    untrusted open-install candidates (approved to `local` on install)."""
    from relaydeck.plugin_registry import search as registry_search

    curated = registry_search(query)
    if curated:
        t = Table(title="Curated (recommended · pinned)")
        t.add_column("Name", style="cyan")
        t.add_column("Version", style="dim")
        t.add_column("Summary")
        for e in curated:
            t.add_row(e.name, e.version or "—", e.summary)
        console.print(t)
        console.print("[dim]Install a curated plugin: `relaydeck plugin install <name>`[/]")
    else:
        console.print("[dim]No curated matches.[/]")
    if curated_only:
        return

    import html.parser
    import urllib.request

    class Parser(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.names: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag != "a":
                return
            href = dict(attrs).get("href", "")
            name = href.strip("/").split("/")[-1]
            if name.startswith("relaydeck-plugin-"):
                self.names.append(name)

    try:
        with urllib.request.urlopen("https://pypi.org/simple/", timeout=10) as resp:
            parser = Parser()
            parser.feed(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        console.print(f"[dim]PyPI search skipped ({type(exc).__name__}).[/]")
        return
    matches = [n for n in sorted(set(parser.names)) if query.lower() in n.lower()]
    if matches:
        console.print("[bold]PyPI[/] [dim](untrusted until approved on install)[/]")
        for name in matches[:25]:
            console.print(f"  {name}")
    elif not curated:
        console.print("[dim]No matching relaydeck-plugin-* packages found.[/]")


@plugin.command("publish-check")
@click.argument("path", default=".", required=False)
def plugin_publish_check(path: str):
    """Validate a plugin project before publishing or sharing it."""
    import shutil
    import subprocess
    import sys
    import tempfile
    import tomllib

    from relaydeck.plugin_manifest import load_manifest

    root = Path(path)
    manifest_path = _find_plugin_manifest_path(root)
    plugin_dir = manifest_path.parent
    project_root = root if (root / "pyproject.toml").exists() else plugin_dir.parent
    manifest = load_manifest(manifest_path)
    if not (plugin_dir / "plugin.py").exists():
        raise SystemExit("plugin.py not found")
    if not (plugin_dir / "py.typed").exists():
        raise SystemExit("py.typed not found beside plugin.py")
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        raise SystemExit("pyproject.toml not found")
    pyproject = tomllib.loads(pyproject_path.read_text())
    project = pyproject.get("project") or {}
    if not str(project.get("name") or "").startswith("relaydeck-plugin-"):
        raise SystemExit("project.name must start with relaydeck-plugin-")
    dependencies = project.get("dependencies") or []
    if not isinstance(dependencies, list):
        raise SystemExit("project.dependencies must be a list")
    dependency_names = {_dependency_name(dep) for dep in dependencies}
    if "relaydeck" not in dependency_names:
        raise SystemExit("project.dependencies must include relaydeck")
    entry_points = (
        project.get("entry-points", {}).get("relaydeck.plugins")
        or pyproject.get("project.entry-points", {}).get("relaydeck.plugins")
        or pyproject.get("project", {}).get("entry-points", {}).get("relaydeck.plugins")
        or {}
    )
    if manifest.name not in entry_points:
        raise SystemExit(
            f'pyproject.toml missing [project.entry-points."relaydeck.plugins"] '
            f"entry for {manifest.name!r}"
        )
    entry_point_value = str(entry_points[manifest.name] or "").strip()
    if not entry_point_value:
        raise SystemExit(f"relaydeck.plugins entry for {manifest.name!r} is empty")
    if not manifest.description:
        raise SystemExit("plugin.description is required for publishing")
    if not manifest.license:
        raise SystemExit("plugin.license is required for publishing")
    if not (project_root / "README.md").exists():
        raise SystemExit("README.md not found")
    if not (project_root / "RELEASE.md").exists():
        raise SystemExit("RELEASE.md not found")
    if not (project_root / ".github" / "workflows" / "ci.yml").exists():
        raise SystemExit(".github/workflows/ci.yml not found")
    skill_paths = _declared_skill_paths(manifest, plugin_dir)
    tests_dir = project_root / "tests"
    if not tests_dir.exists():
        raise SystemExit("tests/ not found")
    if shutil.which("uv") is not None:
        subprocess.run([sys.executable, "-m", "pytest", "tests"], cwd=project_root, check=True)
        with tempfile.TemporaryDirectory(prefix="relaydeck-plugin-build-") as tmp:
            out_dir = Path(tmp)
            subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
                cwd=project_root,
                check=True,
            )
            wheel = next(out_dir.glob("*.whl"), None)
            if wheel is None:
                raise SystemExit("uv build produced no wheel")
            _validate_plugin_wheel(
                wheel,
                manifest_name=manifest.name,
                entry_point_value=entry_point_value,
                skill_paths=tuple(skill_paths),
            )
    console.print(
        f"[green]✓[/] {manifest.name} is ready for local install or package build."
    )


def _dependency_name(spec: object) -> str:
    import re

    raw = str(spec or "").strip()
    raw = raw.split("[", 1)[0]
    raw = raw.split("@", 1)[0]
    return re.split(r"[<>=!~;\s]", raw, maxsplit=1)[0].strip().replace("_", "-").lower()


def _declared_skill_paths(manifest, plugin_dir: Path) -> list[str]:
    skill_paths: list[str] = []
    for skill_name, rel in sorted(manifest.skills.items()):
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise SystemExit(
                f"declared skill {skill_name!r} must use a relative path inside the plugin"
            )
        if not (plugin_dir / rel_path).is_file():
            raise SystemExit(f"declared skill {skill_name!r} file not found: {rel}")
        skill_paths.append(rel_path.as_posix())
    return skill_paths


def _validate_plugin_wheel(
    wheel: Path,
    *,
    manifest_name: str,
    entry_point_value: str,
    skill_paths: tuple[str, ...] = (),
) -> None:
    import configparser
    import zipfile
    from pathlib import PurePosixPath

    module_name = entry_point_value.split(":", 1)[0].strip()
    if not module_name:
        raise SystemExit(f"relaydeck.plugins entry for {manifest_name!r} has no module")
    package_parts = module_name.split(".")[:-1]
    package_dir = "/".join(package_parts)
    manifest_member = "/".join(package_parts + ["plugin.toml"])
    py_typed_member = "/".join(package_parts + ["py.typed"])
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        entry_member = next(
            (name for name in names if name.endswith(".dist-info/entry_points.txt")),
            None,
        )
        if entry_member is None:
            raise SystemExit(f"{wheel.name} has no entry_points.txt")
        parser = configparser.ConfigParser()
        parser.read_string(zf.read(entry_member).decode("utf-8"))
        wheel_entry = parser.get("relaydeck.plugins", manifest_name, fallback="").strip()
        if wheel_entry != entry_point_value:
            raise SystemExit(
                f"{wheel.name} relaydeck.plugins entry for {manifest_name!r} "
                "does not match pyproject.toml"
            )
        if manifest_member not in names:
            raise SystemExit(
                f"{wheel.name} missing {manifest_member}; plugin.toml must ship "
                "beside the entry-point module"
            )
        for rel in skill_paths:
            skill_path = PurePosixPath(rel.replace("\\", "/"))
            if skill_path.is_absolute() or ".." in skill_path.parts:
                raise SystemExit(f"declared skill path must be relative: {rel}")
            skill_member = (
                f"{package_dir}/{skill_path.as_posix()}"
                if package_dir
                else skill_path.as_posix()
            )
            if skill_member not in names:
                raise SystemExit(
                    f"{wheel.name} missing {skill_member}; declared SKILL.md files "
                    "must ship with the plugin package"
                )
        if py_typed_member not in names:
            raise SystemExit(
                f"{wheel.name} missing {py_typed_member}; plugin packages should ship "
                "py.typed beside the entry-point module"
            )


@plugin.command("publish")
def plugin_publish():
    """Validate and build local publishing artifacts for a plugin project."""
    import shutil
    import subprocess

    from relaydeck.plugin_manifest import load_manifest

    manifest_path = _find_plugin_manifest_path(Path("."))
    manifest = load_manifest(manifest_path)
    plugin_publish_check.callback(".")  # type: ignore[attr-defined]
    if shutil.which("uv") is None:
        raise SystemExit("uv not found")
    subprocess.run(["uv", "build"], check=True)
    console.print(
        f"[green]✓[/] {manifest.name} dist/ artifacts were built. Upload with twine when ready."
    )


@plugin.command("set")
@click.argument("name")
@click.argument("key")
@click.argument("value")
def plugin_set(name: str, key: str, value: str):
    """Set one setting on a plugin: `relaydeck plugin set emote preset haiku`."""
    from relaydeck.plugin import PluginContext, get_registry
    from relaydeck.plugin_settings import (
        get_all,
        normalize_schema,
        set_settings,
        validate_values,
    )
    reg = get_registry(_get_config_home())
    if not reg.all():
        try: reg.load_all(PluginContext(config_home=_get_config_home()))
        except Exception: pass
    entry = next((e for e in reg.all() if e.name == name), None)
    if entry is None:
        console.print(f"[red]Plugin '{name}' not found.[/]")
        return
    schema = normalize_schema(entry.instance.get_settings_schema())
    if not any(f["key"] == key for f in schema):
        console.print(f"[red]Unknown setting '{key}' for {name}.[/]")
        keys = [f["key"] for f in schema]
        if keys:
            console.print(f"[dim]Valid keys: {', '.join(keys)}[/]")
        return
    current = get_all(name)
    current[key] = value
    validated = validate_values(schema, current)
    set_settings(name, validated)
    console.print(f"[green]✓[/] {name}.{key} = {value}")
    console.print("[dim]Workers pick up live values on their next tick — no restart needed.[/]")


@plugin.command("unset")
@click.argument("name")
@click.argument("key")
def plugin_unset(name: str, key: str):
    """Clear a setting (falls back to env var or schema default)."""
    from relaydeck.plugin_settings import get_all, set_settings
    current = get_all(name)
    current.pop(key, None)
    set_settings(name, current)
    console.print(f"[green]✓[/] cleared {name}.{key}")


# ── Dashboard (live web dashboard control) ───────────────────────────
# CLI behind the `relaydeck-dashboard` skill: any harness agent can reshape
# the dashboard by shelling out to these, not just the native `dashboard`
# tool. Each write POSTs /api/dashboard/command; the daemon validates and
# emits `dashboard.command`, which the browser applies instantly.


def _dashboard_post(body: dict) -> Any:
    outcome, payload = _json_to_daemon("POST", "/api/dashboard/command", body)
    if outcome != _POST_OK:
        raise click.ClickException(f"dashboard command failed — {payload}")
    return payload


@main.group()
def dashboard():
    """Reshape the live web dashboard (theme, density, glow, widgets, layout).

    Changes apply instantly to every open dashboard via SSE. Requires a
    running daemon."""
    pass


@dashboard.command(name="get")
@click.option("--workspace", "-w", default=None, help="Resolve for this workspace.")
def dashboard_get(workspace: str | None):
    """Show the current resolved appearance (theme/density/glow + widget grid)."""
    from relaydeck import dashboard_commands as dash

    payload = _dashboard_post({"op": "get", "workspace": workspace}) or {}
    ap = payload.get("appearance", {})
    console.print(f"theme={ap.get('theme')} density={ap.get('density')} glow={ap.get('glow')}")
    console.print(dash.format_widget_layout(
        ap.get("dashboard"), scope=ap.get("scope") or (workspace or "global")))
    hint = payload.get("themes") or dash.theme_catalog_hint(config_home=_get_config_home())
    console.print(hint)


@dashboard.command(name="theme")
@click.argument("name")
def dashboard_theme(name: str):
    """Set the dashboard theme (e.g. base, ink, gruvbox-dark). `relaydeck theme list`."""
    _dashboard_post({"op": "theme", "value": name})
    console.print(f"[green]✓[/] theme = {name}")


@dashboard.command(name="density")
@click.argument("value", type=click.Choice(["compact", "comfy", "regular"]))
def dashboard_density(value: str):
    """Set layout density."""
    _dashboard_post({"op": "density", "value": value})
    console.print(f"[green]✓[/] density = {value}")


@dashboard.command(name="glow")
@click.argument("value", type=click.Choice(["on", "off"]))
def dashboard_glow(value: str):
    """Toggle accent glow."""
    _dashboard_post({"op": "glow", "value": value})
    console.print(f"[green]✓[/] glow = {value}")


@dashboard.command(name="add")
@click.argument("widget")
def dashboard_add(widget: str):
    """Add a widget to the Home grid (fleet, usage, agents, feed, workspaces, …)."""
    _dashboard_post({"op": "add_widget", "value": widget})
    console.print(f"[green]✓[/] added widget {widget}")


@dashboard.command(name="remove")
@click.argument("widget")
def dashboard_remove(widget: str):
    """Remove a widget from the Home grid."""
    _dashboard_post({"op": "remove_widget", "value": widget})
    console.print(f"[green]✓[/] removed widget {widget}")


@dashboard.command(name="move")
@click.argument("widget")
@click.argument("x", type=int)
@click.argument("y", type=int)
def dashboard_move(widget: str, x: int, y: int):
    """Move a widget to grid cell (x, y) — 12-col grid, 0-based."""
    _dashboard_post({"op": "move_widget", "value": widget, "x": x, "y": y})
    console.print(f"[green]✓[/] moved {widget} -> ({x},{y})")


@dashboard.command(name="resize")
@click.argument("widget")
@click.argument("w", type=int)
@click.argument("h", type=int)
def dashboard_resize(widget: str, w: int, h: int):
    """Resize a widget to w×h grid cells."""
    _dashboard_post({"op": "resize_widget", "value": widget, "w": w, "h": h})
    console.print(f"[green]✓[/] resized {widget} -> {w}x{h}")


@dashboard.command(name="tidy")
def dashboard_tidy():
    """Auto-arrange widgets to remove gaps."""
    _dashboard_post({"op": "tidy"})
    console.print("[green]✓[/] tidied")


@dashboard.command(name="reset")
def dashboard_reset():
    """Reset the Home grid to the default layout."""
    _dashboard_post({"op": "reset"})
    console.print("[green]✓[/] reset to default layout")


# ── Entry point ──────────────────────────────────────────────────────


if __name__ == "__main__":
    main()
