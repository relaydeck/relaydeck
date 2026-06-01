"""
HTTP API + SSE + WebSocket server.

The web UI is the primary surface. The API serves:
  - Agent CRUD (create, list, start, stop, delete)
  - Workspace management
  - Model presets
  - Event streaming (SSE per agent)
  - WebSocket for harness PTY output
  - Usage/metering queries
  - Plugin-registered routes

Single static/index.html serves the dashboard. No React, no build step.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from relaydeck import audit
from relaydeck.auth import verify_token
from relaydeck.auth_tokens import (
    SCOPE_ROOT,
    AuthIdentity,
    file_root_identity,
    verify_db_token,
)
from relaydeck.config import load_model_presets, load_workspace_registry
from relaydeck.orchestrator import get_orchestrator
from relaydeck.web_runtime import web_static_dir


# ── Auth ─────────────────────────────────────────────────────────────
#
# Every HTTP route except the small public set below requires a Bearer
# token matching the on-disk daemon token. Browsers attach it via the
# `Authorization` header (set by dashboard JS after the bootstrap
# fetch); SSE/WS streams pass it as `?token=` because XHR-style headers
# are awkward in those contexts.
#
# The dashboard shell at `GET /` is public so the browser can load the
# page and immediately fetch `/api/auth/bootstrap` (same-origin guarded)
# to learn the token. Static/asset routes stay public too — they ship
# no privileged data.

_PUBLIC_PATH_PREFIXES = (
    "/assets/",
    "/static/",
)
_PUBLIC_EXACT_PATHS = {
    "/",
    "/healthz",
    "/metrics",
    "/api/auth/bootstrap",
    "/favicon.ico",
}


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT_PATHS:
        return True
    # Provider logos are non-sensitive brand SVGs loaded via plain <img src>
    # (which can't carry the Bearer header) — same trust level as /static/.
    if path.startswith("/api/providers/") and path.endswith("/logo"):
        return True
    return any(path.startswith(pfx) for pfx in _PUBLIC_PATH_PREFIXES)


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    # SSE/WS clients often can't set headers — accept token=… too.
    qp = request.query_params.get("token")
    if qp:
        return qp.strip()
    return None


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _resolve_identity(presented: str | None) -> AuthIdentity | None:
    """Map a presented Bearer token to an `AuthIdentity` or None.

    Order:
      1. on-disk auth-token file (constant-time compare) → file-root identity
      2. auth_tokens table lookup by sha256 hash → scoped identity

    Order matters: the file check is cheap and doesn't touch the DB, so
    the dashboard's hot path stays free of SQLite contention. The
    scoped lookup runs only when the file check misses.
    """
    if not presented:
        return None
    if verify_token(presented):
        return file_root_identity()
    return verify_db_token(presented)


class _AuthMiddleware(BaseHTTPMiddleware):
    """Reject any non-public HTTP request that doesn't present the token.

    WebSocket handshakes bypass this middleware (Starlette routes them
    separately) — each WS handler verifies the token query parameter
    directly on accept.

    Authorization model: every authenticated request is attached an
    `AuthIdentity` on `request.state.identity`. Scope enforcement is
    coarse today — `read-only` tokens are restricted to GET/HEAD/
    OPTIONS verbs; mutating verbs return 403. Agent/plugin scopes
    are accepted but treated like read-only on the existing routes
    until per-route scope declarations land.
    """

    async def dispatch(self, request: Request, call_next):
        if _is_public_path(request.url.path):
            return await call_next(request)

        identity = _resolve_identity(_extract_bearer(request))
        if identity is None:
            return JSONResponse(
                {"detail": "auth required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        request.state.identity = identity

        if identity.scope != SCOPE_ROOT and request.method.upper() not in _SAFE_METHODS:
            return JSONResponse(
                {"detail": f"scope {identity.scope!r} cannot perform {request.method}"},
                status_code=403,
            )

        return await call_next(request)


def _is_loopback_request(request: Request) -> bool:
    """True iff this request came from 127.0.0.1 / ::1 / localhost.

    Used by `/api/auth/bootstrap` so the dashboard can fetch the token
    from a local browser only — never from a remote machine, even if
    the user binds the daemon to 0.0.0.0.

    We require BOTH the TCP peer to be loopback AND the Host header to
    be a loopback name. A remote attacker behind a reverse proxy might
    have the peer look loopback (the proxy itself), but Host would
    carry the public name. A local user with a misconfigured client
    sending Host: example.com isn't loopback either and is rejected —
    fine; that's an edge case where the operator can just use
    ~/.relaydeck/auth-token directly.
    """
    host_header = (request.headers.get("host") or "").split(":")[0].strip().lower()
    # `testserver` is what fastapi.testclient.TestClient injects — accept
    # it so unit tests of this endpoint don't need to hand-stub Host.
    loopback_hosts = {"", "localhost", "127.0.0.1", "::1", "[::1]", "testserver"}
    if host_header not in loopback_hosts:
        return False
    client = request.client
    if client is None:
        return True  # TestClient with no client tuple
    return client.host in ("127.0.0.1", "::1", "localhost", "testclient")

def _summarize_trigger(schedule: Any) -> dict[str, Any] | None:
    """Parse a loop worker's `schedule` into a trigger descriptor for the
    Workers lens. A schedule is just a trigger — interval / cron /
    on_event — so the lens renders them uniformly."""
    if not isinstance(schedule, str) or ":" not in schedule:
        return None
    try:
        from relaydeck.automation import parse_schedule
        kind, value = parse_schedule(schedule)
        return {"kind": kind, "value": value, "raw": schedule}
    except Exception:
        # An invalid schedule (e.g. a malformed cron expression) still
        # surfaces so the operator sees why the worker won't fire.
        return {"kind": "invalid", "value": schedule, "raw": schedule}


# Resolved once at import (not re-imported on every 5s stats call). None if
# the harness module can't be imported, in which case proc_count stays null.
try:
    from relaydeck.harness.base import _descendant_pids as _pid_walker
except Exception:  # pragma: no cover - defensive
    _pid_walker = None


def _collect_runtime_stats(db_path: str, boot_ts: float) -> dict[str, Any]:
    """Daemon vitals for the status bar. Best-effort: every probe is
    independently guarded so a missing `ps` / unreadable DB degrades to a
    null field instead of failing the whole snapshot. Runs in a worker
    thread (see the endpoint) because the `ps` call would otherwise block
    the event loop."""
    now = time.time()
    pid = os.getpid()
    stats: dict[str, Any] = {
        "pid": pid,
        "boot_ts": boot_ts,
        "uptime_s": max(0.0, now - boot_ts),
        "db_size_bytes": None,
        "cpu_percent": None,
        "mem_rss_bytes": None,
        "load_avg": None,
        "proc_count": None,
    }

    try:
        stats["db_size_bytes"] = Path(db_path).stat().st_size
    except OSError:
        pass

    # CPU% + resident set, dependency-free. `ps` RSS is in KiB on both
    # macOS and Linux; %cpu is a recent-usage figure (good enough for a
    # status-bar glance, not a profiler).
    try:
        out = subprocess.run(
            ["ps", "-o", "%cpu=,rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2.0,
        ).stdout.split()
        if len(out) >= 2:
            stats["cpu_percent"] = float(out[0])
            stats["mem_rss_bytes"] = int(out[1]) * 1024
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    try:
        stats["load_avg"] = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        pass

    # Every process the daemon owns (agents + their harness workers) —
    # reuses the harness tree-walk so the count matches what the reaper sees.
    if _pid_walker is not None:
        try:
            stats["proc_count"] = len(_pid_walker(pid))
        except Exception:
            pass

    return stats


def _next_fire_at(trigger: dict[str, Any] | None, last_started_at: Any,
                  agent_status: str | None) -> float | None:
    """Best-effort next-fire time for a countdown. Only running workers
    fire; on_event has no schedule. interval = last start + interval;
    cron = croniter's next from now (server-side — JS has no croniter)."""
    if agent_status != "running" or not trigger:
        return None
    kind = trigger.get("kind")
    if kind == "interval" and last_started_at:
        try:
            return float(last_started_at) + float(trigger.get("value") or 0)
        except (TypeError, ValueError):
            return None
    if kind == "cron":
        try:
            import time as _t

            from croniter import croniter
            return float(croniter(str(trigger.get("value")), _t.time()).get_next(float))
        except Exception:
            return None
    return None


def _sanitize_pty_replay(buf: bytes) -> bytes:
    """Trim unsafe leading bytes from a replay buffer.

    The PTY ring buffer is byte-capped, so it can begin in the middle of
    an ANSI sequence. Full-screen TUIs emit dense CSI/SGR output; when a
    replay starts with the tail of one sequence, xterm renders that tail
    as text. Keep ordinary harnesses byte-for-byte and let sensitive
    harnesses opt into this small cleanup.
    """
    if not buf or buf[0] in (0x1B, 0x0A, 0x0D):
        return buf
    head = buf[:32]
    i = 0
    while i < len(head) and head[i] in b"0123456789;?":
        i += 1
    if i and i < len(head) and 0x40 <= head[i] <= 0x7E:
        return buf[i + 1:]
    esc = buf.find(b"\x1b")
    cr = buf.find(b"\r")
    lf = buf.find(b"\n")
    candidates = [x for x in (esc, cr, lf) if 0 <= x < 256]
    if candidates:
        return buf[min(candidates):]
    return buf


def _summarize_action_kinds(actions: Any) -> list[str]:
    """The attached action kinds on a worker (model / code / script / gh
    / agent.message / bus.emit), in order. These are the optional,
    configurable 'things attached to a worker'."""
    if not isinstance(actions, list):
        return []
    kinds: list[str] = []
    for a in actions:
        if isinstance(a, dict) and len(a) == 1:
            kinds.append(next(iter(a.keys())))
    return kinds


# ── Upload helpers ──────────────────────────────────────────────────


def _prune_uploads(directory: Path, keep: int = 50, max_age_days: int = 7) -> None:
    """Best-effort: keep `keep` most-recent files; remove older than `max_age_days`."""
    now = time.time()
    threshold = now - (max_age_days * 86400)
    files: list[tuple[Path, float]] = []
    for f in directory.iterdir():
        if f.is_file():
            stat = f.stat()
            files.append((f, stat.st_mtime))
    files.sort(key=lambda x: x[1], reverse=True)
    to_remove: set[Path] = set()
    for i, (f, mtime) in enumerate(files):
        if i >= keep or mtime < threshold:
            to_remove.add(f)
    for f in to_remove:
        try:
            f.unlink()
        except OSError:
            pass


def _sweep_all_uploads(uploads_root: Path) -> None:
    """Boot-time best-effort sweep over every agent's upload dir.

    Per-upload pruning (`_prune_uploads`) only fires when an agent
    receives a *new* upload, so a long-idle agent's files never age out.
    This runs once at daemon start to enforce the keep/age policy across
    the whole tree and reclaim dirs left empty (including ones orphaned by
    an agent deleted before its files aged out)."""
    if not uploads_root.is_dir():
        return
    for agent_dir in uploads_root.iterdir():
        if not agent_dir.is_dir():
            continue
        try:
            _prune_uploads(agent_dir)
            if not any(agent_dir.iterdir()):
                agent_dir.rmdir()
        except OSError:
            pass


# ── App factory ──────────────────────────────────────────────────────


def create_app(config_home: Path | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Called by `relaydeck serve` after the orchestrator is booted.
    Plugins register additional routes via register_api_routes().
    """
    from relaydeck import __version__ as _relaydeck_version
    app = FastAPI(title="relaydeck", version=_relaydeck_version)

    # Daemon boot wall-clock — surfaced (as epoch seconds) by
    # /api/runtime-stats so the dashboard status bar can show a live uptime.
    _boot_ts = time.time()

    # CORS: lock to loopback origins. The daemon is a localhost service;
    # a wildcard here would let any webpage in the user's browser make
    # authenticated-cookie-style requests. Auth is Bearer-token, so this
    # is belt-and-suspenders — but it cheaply prevents a malicious page
    # at evil.com from at least learning shapes via preflight.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Token enforcement middleware. Goes BEFORE routes so route handlers
    # can assume the request is authenticated.
    app.add_middleware(_AuthMiddleware)

    orch = get_orchestrator(config_home)
    web_dir = web_static_dir()

    # Boot sweep: enforce the upload keep/age policy across every agent
    # dir and reclaim empties. Per-upload pruning is lazy, so this is the
    # only thing that ages out a long-idle (or deleted) agent's files.
    try:
        _sweep_all_uploads(orch.config_home / "uploads")
    except Exception:
        pass

    # ── Health + auth bootstrap (public) ────────────────────────────

    @app.get("/healthz")
    async def healthz():
        """Unauthenticated probe so external supervisors (systemd,
        process watchers, future load balancer) can liveness-check the
        daemon without holding the token. Carries no privileged data."""
        return {"ok": True, "version": app.version}

    @app.get("/api/runtime-stats")
    async def runtime_stats():
        """SRE-glance vitals for the dashboard status bar: daemon uptime,
        DB size, CPU%, resident memory, system load, and managed-process
        count. Dependency-free (no psutil) — CPU/RSS come from a one-shot
        `ps`, the rest from stdlib. Collected off the event loop so the
        `ps` call can't stall other requests."""
        return await asyncio.to_thread(
            _collect_runtime_stats, orch.db_path, _boot_ts,
        )

    @app.get("/metrics")
    async def metrics():
        """Prometheus exposition. Public by default — metric values
        are aggregate counters/gauges (no per-request data, no
        secrets). Scrape from your usual collector. Refresh the
        agent/worker gauges from live state before each render so
        what gets exposed matches what the dashboard would show.
        """
        from fastapi.responses import PlainTextResponse
        from relaydeck.metrics import (
            registry, set_agents_gauge, set_workers_gauge,
        )
        # Recompute gauges right before serving so /metrics matches
        # the current orchestrator + worker state instead of relying
        # on every transition to fire a record_*. Cheap: a SELECT
        # COUNT GROUP BY status against the agents table + a
        # snapshot of the worker registry.
        try:
            agent_counts: dict[str, int] = {}
            for row in orch.list_agents():
                status = row.get("status") or "unknown"
                agent_counts[status] = agent_counts.get(status, 0) + 1
            set_agents_gauge(agent_counts)
        except Exception:
            pass
        try:
            from relaydeck.workers import get_worker_registry
            worker_counts: dict[str, int] = {}
            for w in get_worker_registry().all():
                worker_counts[w.status] = worker_counts.get(w.status, 0) + 1
            set_workers_gauge(worker_counts)
        except Exception:
            pass
        return PlainTextResponse(
            registry().render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/auth/bootstrap")
    async def auth_bootstrap(request: Request):
        """Hand the dashboard the current token.

        Guarded by loopback Host + client check — a remote browser
        connecting through an SSH tunnel or a reverse proxy CANNOT
        retrieve the token this way. Remote operators must copy
        `~/.relaydeck/auth-token` (or use `RELAYDECK_AUTH_TOKEN`) by hand.
        """
        if not _is_loopback_request(request):
            raise HTTPException(status_code=403, detail="bootstrap is local-only")
        from relaydeck.auth import read_token
        token = read_token()
        if not token:
            raise HTTPException(status_code=503, detail="daemon has no token configured")
        return {"token": token}

    @app.get("/api/auth/verify")
    async def auth_verify(request: Request):
        """Authenticated noop. The dashboard hits this after pulling a
        token from localStorage so a stale/rotated value gets cleared
        and the paste-token prompt reappears instead of every panel
        rendering empty with a sea of 401s in the console.
        """
        ident = getattr(request.state, "identity", None)
        return {"ok": True, "scope": getattr(ident, "scope", None)}

    # ── Dashboard UI preferences ────────────────────────────────────
    #
    # Tile-system assignments, density, accent, last-lens, last-workspace
    # — anything the dashboard wants to persist per-operator that doesn't
    # belong in a workspace config. Stored as a single YAML file at
    # ~/.relaydeck/preferences.yaml (mode 0600). Whole-blob GET/PUT
    # keeps the contract trivial; the dashboard does coalesced writes.

    @app.get("/api/preferences")
    async def get_preferences():
        from relaydeck.preferences import read_preferences
        return read_preferences(home)

    @app.put("/api/preferences")
    async def put_preferences(body: dict[str, Any]):
        from relaydeck.preferences import write_preferences
        write_preferences(home, body or {})
        return {"ok": True}

    # ── Themes + appearance ─────────────────────────────────────────
    #
    # A theme is a named, extendable bundle of design-token overrides
    # (relaydeck/themes.py). Appearance is the resolved view per
    # workspace: theme + density + glow + dashboard layout, with a
    # global default workspaces inherit. Both are operator-facing and
    # fully managed from the dashboard Appearance lens; the `relaydeck
    # theme` CLI is at parity.

    def _theme_payload(t) -> dict[str, Any]:
        from relaydeck import themes
        d = t.to_dict()
        d["builtin"] = t.builtin
        d["resolved"] = themes.resolve_theme(t.name, config_home=home)
        return d

    @app.get("/api/themes/contract")
    async def theme_contract():
        """The authoritative token contract — what a theme may set.
        The editor + the bundled skill render from this."""
        from relaydeck import themes
        cats: dict[str, list[dict[str, str]]] = {}
        for tok in themes.THEME_TOKENS:
            cats.setdefault(tok.category, []).append(
                {"name": tok.name, "label": tok.label,
                 "type": tok.type, "default": tok.default})
        return {"categories": [{"name": k, "tokens": v} for k, v in cats.items()]}

    @app.get("/api/themes")
    async def list_themes_api():
        from relaydeck import themes
        return [_theme_payload(t) for t in themes.list_themes(config_home=home)]

    @app.get("/api/themes/{name}")
    async def get_theme_api(name: str):
        from relaydeck import themes
        t = themes.get_theme(name, config_home=home)
        if t is None:
            raise HTTPException(404, f"No such theme {name!r}")
        return _theme_payload(t)

    @app.put("/api/themes/{name}")
    async def put_theme_api(name: str, body: dict[str, Any], request: Request):
        """Create or update a user theme. Token names are validated
        against the contract; an `extends` cycle is rejected (400)."""
        from relaydeck import themes
        data = dict(body or {})
        data["name"] = name
        # Validate the RAW tokens first so a typo'd token name 400s with
        # feedback rather than being silently dropped by the lenient
        # from_dict (which stays forgiving for on-disk loads).
        try:
            themes.validate_tokens(data.get("tokens") or {})
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        theme = themes.Theme.from_dict(data)
        try:
            themes.save_theme(theme, config_home=home)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit.record(
            audit.actions.PLUGIN_SETTINGS_CHANGE, target=f"theme:{name}",
            payload={"extends": theme.extends, "tokens": len(theme.tokens)},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        _fire_workspace_event("themes.changed", {"name": name})
        return _theme_payload(themes.get_theme(name, config_home=home))

    @app.delete("/api/themes/{name}")
    async def delete_theme_api(name: str, request: Request):
        """Delete a user theme. A pure builtin refuses (409); deleting a
        file that shadows a builtin reverts to the builtin."""
        from relaydeck import themes
        path = themes._theme_path(name, config_home=home)
        if not path.exists() and themes.is_builtin(name):
            raise HTTPException(409, f"{name!r} is a builtin theme — cannot delete")
        removed = themes.delete_theme(name, config_home=home)
        if not removed:
            raise HTTPException(404, f"No such theme {name!r}")
        # If the name no longer resolves (i.e. it wasn't shadowing a
        # builtin), clear any appearance ref pointing at it so the
        # affected scopes fall back instead of dangling.
        cleared: list[str] = []
        if themes.get_theme(name, config_home=home) is None:
            from relaydeck.preferences import clear_appearance_theme
            cleared = clear_appearance_theme(home, name)
        audit.record(
            audit.actions.PLUGIN_SETTINGS_CHANGE, target=f"theme:{name}",
            payload={"deleted": True, "cleared": cleared},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        _fire_workspace_event("themes.changed", {"name": name, "deleted": True})
        if cleared:
            _fire_workspace_event("appearance.changed", {"cleared": cleared})
        return {"name": name, "deleted": True, "cleared": cleared}

    @app.get("/api/appearance")
    async def get_appearance_api(workspace: str | None = None):
        """Resolved appearance (per-workspace → global → default) plus
        the raw blob so the editor can show what's set vs inherited."""
        from relaydeck.preferences import read_appearance, resolve_appearance
        return {
            "resolved": resolve_appearance(home, workspace),
            "raw": read_appearance(home),
            "workspace": workspace,
        }

    @app.post("/api/appearance/notify")
    async def notify_appearance_api(workspace: str | None = None):
        """Re-emit `appearance.changed` so a running dashboard refreshes
        live after a CLI-side appearance write (the file is already on
        disk; this only carries the bus event)."""
        _fire_workspace_event("appearance.changed", {"workspace": workspace, "via": "cli"})
        return {"ok": True}

    @app.put("/api/appearance")
    async def put_appearance_api(body: dict[str, Any], workspace: str | None = None):
        """Patch appearance keys (theme/density/glow/dashboard) globally
        or for one workspace. A key set to null clears the override so it
        falls back to the global value."""
        from relaydeck.preferences import set_appearance
        resolved = set_appearance(home, body or {}, workspace)
        _fire_workspace_event("appearance.changed",
                              {"workspace": workspace, "keys": list((body or {}).keys())})
        return {"resolved": resolved, "workspace": workspace}

    @app.post("/api/dashboard/command")
    async def dashboard_command_api(body: dict[str, Any]):
        """Validate + broadcast a live dashboard command so any agent (via the
        `relaydeck dashboard` CLI / `relaydeck-dashboard` skill) or operator can
        reshape the dashboard — not just the native-harness `dashboard` tool.
        `op=get` returns the resolved appearance; write ops emit
        `dashboard.command`, which the browser applies live (theme/density/glow
        at the app level, widget ops on the Home grid)."""
        from relaydeck import dashboard_commands as dash
        op = (body or {}).get("op", "")
        workspace = (body or {}).get("workspace")
        if op == "get":
            from relaydeck import dashboard_commands as dash
            from relaydeck.preferences import resolve_appearance
            return {
                "appearance": resolve_appearance(home, workspace),
                "themes": dash.theme_catalog_hint(config_home=home),
            }
        known_themes = None
        if op == "theme":
            from relaydeck import themes
            known_themes = {t.name for t in themes.list_themes(config_home=home)}
        try:
            cmd = dash.build_dashboard_command(
                op, (body or {}).get("value"),
                x=(body or {}).get("x"), y=(body or {}).get("y"),
                w=(body or {}).get("w"), h=(body or {}).get("h"),
                known_themes=known_themes,
            )
        except dash.DashboardCommandError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if op in dash.SCALAR_OPS:
            # Persist so `get` reflects it and it survives with no browser open;
            # appearance.changed repaints any connected dashboard.
            from relaydeck.preferences import set_appearance
            key = dash.appearance_key(op)
            set_appearance(home, {key: cmd["value"]}, workspace)
            _fire_workspace_event(
                "appearance.changed",
                {"workspace": workspace, "keys": [key], "via": "dashboard"})
            return {"ok": True, "command": cmd, "persisted": True}
        # Widget/layout ops are live grid mutations a browser applies + persists.
        _fire_workspace_event("dashboard.command", cmd)
        return {"ok": True, "command": cmd, "persisted": False}

    # ── Integrations (vendor telemetry, e.g. claude hooks) ──────
    # Surfaced in the dashboard Settings → Integrations tab. The
    # registry is the same one `relaydeck integration list/install/uninstall`
    # uses; we just expose it over HTTP so the dashboard doesn't have
    # to shell out.

    @app.get("/api/integrations")
    async def list_integrations():
        from relaydeck.integrations import all_integrations, integration_state
        out = []
        for i in all_integrations():
            try:
                state = integration_state(i)
                installed = state == "installed"
            except Exception:
                state = "error"
                installed = False
            # The Integration protocol exposes `name`; the dashboard's
            # Settings card surfaces it under the column "harness" since
            # each integration is named for the harness it targets. Send
            # both keys so any future renamings on either side stay
            # compatible.
            out.append({
                "name": i.name, "harness": i.name,
                "kind": i.kind, "installed": installed,
                "state": state,
                "description": getattr(i, "description", ""),
            })
        return out

    @app.post("/api/integrations/{name}/install")
    async def install_integration(name: str):
        from relaydeck.integrations import get
        integ = get(name)
        if integ is None:
            raise HTTPException(404, f"unknown integration: {name}")
        try:
            msg = integ.install()
        except Exception as exc:
            raise HTTPException(500, f"install failed: {exc}") from exc
        return {"ok": True, "message": msg}

    @app.post("/api/integrations/{name}/uninstall")
    async def uninstall_integration(name: str):
        from relaydeck.integrations import get
        integ = get(name)
        if integ is None:
            raise HTTPException(404, f"unknown integration: {name}")
        try:
            removed = bool(integ.uninstall())
        except Exception as exc:
            raise HTTPException(500, f"uninstall failed: {exc}") from exc
        return {"ok": True, "removed": removed}

    # ── Auth admin (Settings → Auth tab) ──────────────────────────
    # Mirror the `relaydeck auth list/rotate` CLI. The on-disk root token
    # itself is not exposed here (the dashboard already holds it as
    # `window.__relaydeckToken`); we expose the scoped-token list count and
    # the rotate action.

    @app.get("/api/auth/tokens")
    async def list_auth_tokens():
        from relaydeck.auth_tokens import list_tokens
        try:
            return list_tokens(db_path=orch.db_path)
        except Exception:
            return []

    @app.post("/api/auth/rotate")
    async def rotate_auth_token(request: Request):
        from relaydeck import audit
        from relaydeck.auth import regenerate_token
        new = regenerate_token()
        audit.record(
            audit.actions.TOKEN_ROTATE, target="root-file",
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        # Return just a fingerprint, never the full new token — clients
        # re-read it from `relaydeck auth show` or the disk file.
        return {"ok": True, "fingerprint": f"{new[:8]}…{new[-4:]}"}

    # ── Dashboard ────────────────────────────────────────────────

    if web_dir.exists() and (web_dir / "index.html").exists():
        # Daemon-process build stamp. PID changes on every restart so
        # the browser invalidates its module + HTTP cache after every
        # `relaydeck daemon start`. Stable across requests in the same
        # process — clients that don't reload still get a cache hit.
        # The browser-side mechanics:
        #   - All same-origin /static/*.{js,css} URLs in index.html get
        #     ?v=<pid> stamped on them.
        #   - An importmap pointing the ES-module loader at the same
        #     stamped URLs is injected; that way *dynamic imports*
        #     (e.g. `import('./lenses/agents.js')` from app.js) also
        #     resolve to ?v=<pid> URLs and dodge Chromium's module
        #     cache. Without the importmap, ?v= on the entry point
        #     doesn't propagate to its imports.
        #   - /static/ responses get `Cache-Control: no-cache` (see
        #     _NoCacheStaticMiddleware below) so the browser revalidates
        #     each file with ETag — unchanged files still 304, changed
        #     files refetch.
        import os as _os
        import re as _re
        _build_stamp = str(_os.getpid())

        # Auto-discover every core ES module under web/static so each one's
        # bare/relative imports resolve to a ?v=<pid> URL through the
        # importmap (and dodge Chromium's module cache after a restart).
        # Walking the tree means new modules — the Lit foundation, the
        # `@relaydeck/ui` kit, future lenses — are covered automatically
        # instead of drifting from a hand-kept list. Plugin assets live under
        # /static/plugins/<name>/ (their own mounts, imported via stamped())
        # and the vendored Lit bundle is reached through the bare `lit`
        # specifier below — both subtrees are skipped here.
        def _discover_static_modules():
            mods = []
            for root, dirs, files in _os.walk(web_dir):
                dirs[:] = [d for d in dirs if d not in ("plugins", "vendor", "assets")]
                for fn in files:
                    if fn.endswith(".js"):
                        rel = _os.path.relpath(_os.path.join(root, fn), web_dir)
                        mods.append("/static/" + rel.replace(_os.sep, "/"))
            return sorted(mods)

        _STATIC_MODULES = _discover_static_modules()

        # Bare module specifiers — the ecosystem import surface. Core
        # components and third-party plugin UIs alike import Lit and the
        # relaydeck ui-kit by name (resolved here, stamped per restart):
        #   import { LitElement, html, css } from 'lit';
        #   import { RdButton, fields, esc } from '@relaydeck/ui';
        # `lit` is the vendored single-file Lit 3.3.x bundle (offline, no
        # CDN). `@relaydeck/ui` is the kit barrel under /static/uikit/.
        _BARE_MODULES = {
            "lit": "/static/vendor/lit-all.min.js",
            "@relaydeck/ui": "/static/uikit/index.js",
        }

        @app.get("/", response_class=HTMLResponse)
        async def dashboard():
            html = (web_dir / "index.html").read_text()

            # 1. Stamp every same-origin /static/*.{js,css} URL on the
            #    page itself.
            def _stamp(m):
                href = m.group(2)
                sep = "&" if "?" in href else "?"
                return f'{m.group(1)}="{href}{sep}v={_build_stamp}"'
            html = _re.sub(
                r'(href|src)="(/static/[^"]+\.(?:js|css|map))"',
                _stamp, html,
            )

            # 2. Inject an importmap so dynamic imports resolve to the
            #    stamped URLs. The map only needs entries for modules
            #    actually reached through `import()`; the rest go via
            #    the `src="…?v=…"` we stamped above.
            mapping = {p: f"{p}?v={_build_stamp}" for p in _STATIC_MODULES}
            mapping.update({k: f"{v}?v={_build_stamp}" for k, v in _BARE_MODULES.items()})
            import json as _json
            importmap = (
                '<script type="importmap">'
                + _json.dumps({"imports": mapping})
                + "</script>"
            )
            html = html.replace("</head>", importmap + "</head>", 1)
            # Never cache the shell HTML itself. It carries the
            # per-restart ?v=<pid> stamps + importmap; if the browser
            # serves a stale `/` from its heuristic cache, a plain
            # reload would keep loading the OLD asset URLs and the
            # operator sees "my changes aren't showing up". no-store
            # forces the shell to refetch every load, which then pulls
            # the current stamps.
            return HTMLResponse(html, headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            })

        if (web_dir / "assets").exists():
            app.mount("/assets", StaticFiles(directory=str(web_dir / "assets")), name="assets")

        # The broad /static mount that serves app.js, data.js, lenses/*,
        # tiles/* etc. lives in `relaydeck serve` (cli.py) — registered AFTER
        # all plugin static dirs so `/static/plugins/<name>/...` requests
        # reach the plugin-specific mounts first. See cli.py around the
        # plugin static-mount loop.

    # ── Agent endpoints ──────────────────────────────────────────

    @app.get("/api/agents")
    async def list_agents(workspace: str | None = None):
        """List agents, optionally filtered to one workspace.

        The dashboard tracks a "current workspace" client-side and passes
        it here so switching workspaces doesn't bleed agents from the
        previous one into the sidebar. Omit the param to get every agent
        in the daemon's DB (used by /api/usage rollups and by the CLI).
        """
        agents = orch.list_agents()
        if workspace is not None and workspace != "":
            agents = [a for a in agents if (a.get("workspace") or "") == workspace]
        return agents

    # IMPORTANT: /api/agents/find is registered before /api/agents/{agent_id}
    # so FastAPI's route matcher catches the literal `find` first
    # instead of treating it as an agent_id.
    @app.get("/api/agents/find")
    async def find_agents(tag: str | None = None, purpose: str | None = None,
                          workspace: str | None = None):
        """Discover agents by meta. Used by peer agents asking
        "who reviews PRs?" or "which agents are tagged `local-only`?".

        - `tag`: agent must have this tag (exact match)
        - `purpose`: case-insensitive substring/regex match against purpose
        - `workspace`: scope to one workspace

        Multiple filters AND together. Returns the same shape as
        /api/agents.
        """
        import re
        agents = orch.list_agents()
        if workspace is not None and workspace != "":
            agents = [a for a in agents if (a.get("workspace") or "") == workspace]
        if tag:
            agents = [a for a in agents if tag in (a.get("tags") or [])]
        if purpose:
            try:
                pat = re.compile(purpose, re.IGNORECASE)
                agents = [a for a in agents if pat.search(a.get("purpose") or "")]
            except re.error:
                # Treat as plain substring on regex compile failure.
                pl = purpose.lower()
                agents = [a for a in agents if pl in (a.get("purpose") or "").lower()]
        return agents

    # Registered before /api/agents/{agent_id} so `usage-rollup` isn't
    # swallowed as an agent id (same reason `find` is above).
    @app.get("/api/agents/usage-rollup")
    async def agents_usage_rollup():
        """Fleet-wide per-agent 24h token totals + a coarse hourly
        spark, in one query. The Agents sidebar uses this to draw the
        per-row token count + sparkline without N round-trips."""
        from relaydeck.db import get_fleet_token_rollup, open_db
        conn = open_db(orch.db_path)
        try:
            return get_fleet_token_rollup(conn)
        finally:
            conn.close()

    @app.get("/api/usage-heatmap")
    async def fleet_usage_heatmap(days: int = 7):
        """Fleet-wide token-usage calendar heatmap (all agents) for the
        Home 'usage heatmap' widget. Real sums from usage_records."""
        from relaydeck.db import get_agent_usage_heatmap, open_db
        conn = open_db(orch.db_path)
        try:
            return get_agent_usage_heatmap(conn, None, days=max(1, min(31, days)))
        finally:
            conn.close()

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str):
        agent = orch.get_agent(agent_id)
        if not agent:
            raise HTTPException(404, f"Agent {agent_id} not found")
        return agent

    @app.get("/api/agents/{agent_id}/stats")
    async def agent_stats(agent_id: str):
        """Per-agent rollup for the detail stat strip — tokens/cost
        (24h), event totals, last tick, and a 30-minute activity
        spark. Real data from usage_records + events; no fabrication."""
        agent = orch.get_agent(agent_id)
        if not agent:
            raise HTTPException(404, f"Agent {agent_id} not found")
        from relaydeck.db import get_agent_stats, open_db
        conn = open_db(orch.db_path)
        try:
            return get_agent_stats(conn, agent_id)
        finally:
            conn.close()

    @app.get("/api/agents/{agent_id}/prompt-composition")
    async def agent_prompt_composition(agent_id: str):
        """How this agent's system prompt is built: the real components
        (identity preamble, operator system_prompt, each injected skill,
        fleet context) with exact char counts + ~chars/4 token estimates.
        Read-only; no fabrication — see relaydeck/prompt_composition.py."""
        agent = orch.get_agent(agent_id)
        if not agent:
            raise HTTPException(404, f"Agent {agent_id} not found")
        from relaydeck.prompt_composition import compose_prompt_components

        workspace = agent.get("workspace") or None
        purpose = agent.get("purpose") or ""
        system_prompt = ""
        inject = True
        # system_prompt + inject flag are YAML-only (no DB mirror).
        try:
            from relaydeck.config import AgentSpec
            spec_path = orch.config_home / "agents" / f"{agent_id}.yaml"
            if spec_path.exists():
                spec = AgentSpec.from_yaml(spec_path)
                system_prompt = getattr(spec, "system_prompt", "") or ""
                inject = bool(getattr(spec, "inject_identity_preamble", True))
                purpose = getattr(spec, "purpose", "") or purpose
        except Exception:
            pass
        # Peers = other agents in the workspace (live DB), for the preamble.
        peers: list[dict[str, Any]] = []
        if workspace:
            from relaydeck.db import open_db
            try:
                conn = open_db(orch.db_path)
                try:
                    rows = conn.execute(
                        "SELECT id, type, purpose FROM agents "
                        "WHERE workspace = ? AND id != ? ORDER BY id ASC",
                        (workspace, agent_id),
                    ).fetchall()
                    peers = [dict(r) for r in rows]
                finally:
                    conn.close()
            except Exception:
                peers = []
        out = compose_prompt_components(
            orch.config_home,
            agent_id=agent_id,
            workspace=workspace,
            agent_type=agent.get("type") or "",
            purpose=purpose,
            system_prompt=system_prompt,
            peers=peers,
            inject_preamble=inject,
        )
        # Peers are part of the preamble; surface them so the Identity tab
        # can render the "visible in preamble" panel from the same fetch.
        out["peers"] = peers
        out["inject_preamble"] = inject
        return out

    @app.get("/api/agents/{agent_id}/sessions")
    async def agent_sessions(agent_id: str, limit: int = 50):
        """Per-session context usage (the Context tab): each thread's
        current/peak context fill (latest prompt_tokens), turns, total
        tokens, cost, model. Real data from usage_records; no fabricated
        context-window limit."""
        agent = orch.get_agent(agent_id)
        if not agent:
            raise HTTPException(404, f"Agent {agent_id} not found")
        from relaydeck.db import get_agent_session_contexts, open_db
        conn = open_db(orch.db_path)
        try:
            return {"sessions": get_agent_session_contexts(
                conn, agent_id, limit=max(1, min(200, limit)))}
        finally:
            conn.close()

    @app.get("/api/agents/{agent_id}/usage-heatmap")
    async def agent_usage_heatmap(agent_id: str, days: int = 7):
        """Token usage as a calendar heatmap (days × 24 hourly cells) for
        the agent's Context tab. Real sums from usage_records; empty cells
        are honest 0."""
        agent = orch.get_agent(agent_id)
        if not agent:
            raise HTTPException(404, f"Agent {agent_id} not found")
        from relaydeck.db import get_agent_usage_heatmap, open_db
        conn = open_db(orch.db_path)
        try:
            return get_agent_usage_heatmap(conn, agent_id, days=max(1, min(31, days)))
        finally:
            conn.close()

    def _audit_identity(request: Request) -> AuthIdentity:
        """Pull the auth identity attached by the middleware. Falls
        back to the file-root sentinel if a route is invoked outside
        the middleware (shouldn't happen in production; cheap safety
        net for unit tests + future internal callers)."""
        return getattr(request.state, "identity", None) or file_root_identity()

    def _audit_source_ip(request: Request) -> str | None:
        client = request.client
        return client.host if client else None

    def _coerce_string_list(raw: Any, field: str) -> list[str]:
        """Validate JSON fields stored as YAML/DB string lists."""
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise HTTPException(400, f"{field} must be a list of strings")
        if not all(isinstance(tag, str) for tag in raw):
            raise HTTPException(400, f"{field} must be a list of strings")
        return list(raw)

    @app.patch("/api/agents/{agent_id}")
    async def patch_agent(agent_id: str, body: dict[str, Any], request: Request):
        """Update an agent's meta + prompt config.

        Accepts any subset of: `purpose`, `tags`, `system_prompt`,
        `inject_identity_preamble`. Only the keys present in the body
        are touched. Writes the YAML spec (source of truth) and
        mirrors purpose/tags into the DB so peers see the change in
        `relaydeck agent list` immediately. `system_prompt` /
        `inject_identity_preamble` are YAML-only — the harness reads
        them at next spawn.
        """
        accepted = {"purpose", "tags", "system_prompt", "inject_identity_preamble", "config"}
        if not any(k in body for k in accepted):
            raise HTTPException(400, f"one of {sorted(accepted)} required")
        kwargs: dict[str, Any] = {}
        if "purpose" in body:
            kwargs["purpose"] = body.get("purpose")
        if "tags" in body:
            kwargs["tags"] = _coerce_string_list(body.get("tags"), "tags")
        if "system_prompt" in body:
            kwargs["system_prompt"] = str(body.get("system_prompt") or "")
        if "inject_identity_preamble" in body:
            kwargs["inject_identity_preamble"] = bool(
                body.get("inject_identity_preamble"),
            )
        if "config" in body:
            cfg = body.get("config")
            if cfg is not None and not isinstance(cfg, dict):
                raise HTTPException(400, "config must be an object")
            kwargs["config"] = cfg or {}
        try:
            updated = orch.update_agent_meta(agent_id, **kwargs)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        audit.record(
            audit.actions.AGENT_UPDATE,
            target=agent_id, payload={"fields": sorted(kwargs.keys())},
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return updated


    @app.post("/api/agents")
    async def create_agent(body: dict[str, Any], request: Request):
        try:
            agent_id = orch.create_agent(
                agent_id=str(body.get("id") or ""),
                agent_type=body.get("type", "harness"),
                name=body.get("name") or str(body.get("id") or ""),
                workspace=body.get("workspace"),
                config=body.get("config", {}),
                auto_start=body.get("auto_start", False),
                purpose=str(body.get("purpose") or ""),
                tags=_coerce_string_list(body.get("tags"), "tags"),
                system_prompt=str(body.get("system_prompt") or ""),
                inject_identity_preamble=bool(
                    body.get("inject_identity_preamble", True),
                ),
            )
        except ValueError as exc:
            # Bad agent id (format/length) or invalid type — surface the
            # orchestrator's human message instead of a 500.
            raise HTTPException(400, str(exc)) from exc
        audit.record(
            audit.actions.AGENT_CREATE,
            target=agent_id,
            payload={"type": body.get("type", "harness"),
                     "workspace": body.get("workspace")},
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"id": agent_id, "status": "created"}

    @app.get("/api/harnesses")
    async def list_harnesses():
        """Type catalog for the new-agent modal: native harnesses (with an
        `available` flag), the relaydeck operator, and any linked external
        runtimes (Hermes/OpenClaw) — each with its curated launch options
        (yolo/plan/continue/sandbox/extra-args)."""
        from relaydeck.harness_options import build_harness_catalog
        return {"harnesses": build_harness_catalog(orch.config_home)}

    @app.post("/api/agents/preview-prompt")
    async def preview_agent_prompt(body: dict[str, Any]):
        """Preview exactly what gets baked into a prospective agent's
        system prompt: the auto identity preamble (id/purpose/peers),
        the operator's free-form `system_prompt`, and the prompt-shaping
        plugins active in the target workspace. Read-only; no agent is
        created."""
        from relaydeck.config import load_workspace_registry
        from relaydeck.plugin import get_registry
        from relaydeck.harness import compose_identity_preamble
        from relaydeck.workspace_plugins import list_workspace_plugins

        agent_id = str(body.get("agent_id") or body.get("id") or "new-agent")
        workspace = body.get("workspace") or None
        purpose = str(body.get("purpose") or "")
        system_prompt = str(body.get("system_prompt") or "")
        inject = bool(body.get("inject_identity_preamble", True))

        ws_path = None
        if workspace:
            ws_row = next(
                (w for w in load_workspace_registry(orch.config_home)
                 if w.name == workspace),
                None,
            )
            if ws_row:
                ws_path = ws_row.path

        # Peers = other agents already in the workspace (live DB).
        peers: list[dict[str, Any]] = []
        if workspace:
            from relaydeck.db import open_db
            try:
                conn = open_db(orch.db_path)
                try:
                    rows = conn.execute(
                        "SELECT id, type, purpose FROM agents "
                        "WHERE workspace = ? AND id != ? ORDER BY id ASC",
                        (workspace, agent_id),
                    ).fetchall()
                    peers = [dict(r) for r in rows]
                finally:
                    conn.close()
            except Exception:
                peers = []

        preamble = (
            compose_identity_preamble(
                agent_id, workspace, purpose, peers,
                workspace_path=ws_path, config_home=orch.config_home,
            )
            if inject else ""
        )

        # Active prompt-shaping plugins in the target workspace.
        active: list[str] = []
        if workspace:
            ws = next(
                (w for w in load_workspace_registry(orch.config_home)
                 if w.name == workspace),
                None,
            )
            active = list(getattr(ws, "plugins", []) or [])
        injections: list[dict[str, Any]] = []
        if active:
            catalog = {e["name"]: e for e in list_workspace_plugins(get_registry())}
            ws_dir = orch.config_home / "workspaces" / (workspace or "")
            for name in active:
                entry = catalog.get(name, {})
                detail = entry.get("description", "")
                # Cheap, honest counts for the content-bearing gates.
                if name == "skills":
                    n = len(list((ws_dir / "skills").glob("*/SKILL.md")))
                    detail += f"  ({n} skill{'s' if n != 1 else ''} found)"
                elif name == "cognitive":
                    n = len(list((ws_dir / "soul" / "beliefs").glob("*.md")))
                    detail += f"  ({n} belief file{'s' if n != 1 else ''})"
                injections.append({
                    "plugin": name,
                    "active": True,
                    "detail": detail or "(no description)",
                })

        parts = [p for p in (preamble, system_prompt) if p]
        return {
            "preamble": preamble,
            "system_prompt": system_prompt,
            "composed": "\n\n".join(parts),
            "peers": [p["id"] for p in peers],
            "injections": injections,
        }

    @app.post("/api/agents/{agent_id}/start")
    async def start_agent(agent_id: str, request: Request):
        try:
            # start_agent blocks on a spawn-verify window; run it off the
            # event loop so a slow harness start doesn't freeze the dashboard.
            await asyncio.to_thread(orch.start_agent, agent_id)
        except ValueError as e:
            # Bad request: spec missing / unknown agent type.
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            # Start-time verification failure: child died during the
            # verify window, or thread+DB ended up inconsistent. The
            # message has the actual reason — surface it via 409
            # (conflict / cannot fulfill) so the CLI can print the
            # real cause instead of an opaque 500.
            raise HTTPException(409, str(e))
        audit.record(
            audit.actions.AGENT_START, target=agent_id,
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"id": agent_id, "status": "started"}

    @app.post("/api/agents/{agent_id}/stop")
    async def stop_agent(agent_id: str, request: Request):
        # stop_agent blocks up to ~10s joining the agent thread + reaping
        # the process tree. Offload it so the asyncio event loop (and every
        # other dashboard request / SSE stream) stays responsive.
        await asyncio.to_thread(orch.stop_agent, agent_id)
        audit.record(
            audit.actions.AGENT_STOP, target=agent_id,
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"id": agent_id, "status": "stopped"}

    @app.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: str, request: Request, purge_history: bool = True):
        """Delete an agent. Always removes the spec, DB row, and per-agent
        runtime files. `purge_history=true` (default) also clears the
        agent's events/usage/invocations/tasks/messages/automation_runs;
        pass `purge_history=false` to keep that history for audit."""
        orch.delete_agent(agent_id, purge_history=purge_history)
        audit.record(
            audit.actions.AGENT_REMOVE, target=agent_id,
            payload={"purge_history": bool(purge_history)},
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"id": agent_id, "status": "deleted", "purged_history": bool(purge_history)}

    @app.post("/api/agents/{agent_id}/restart")
    async def restart_agent(agent_id: str, request: Request):
        """Stop + start. Convenience wrapper so the dashboard's
        Restart button doesn't have to do two round-trips. Errors
        from either half surface with the same 4xx/5xx the individual
        endpoints would have returned."""
        try:
            await asyncio.to_thread(orch.stop_agent, agent_id)
        except Exception:
            # Stopping an already-stopped agent is fine; don't fail
            # restart just because the orchestrator wasn't holding the
            # process anymore.
            pass
        try:
            await asyncio.to_thread(orch.start_agent, agent_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        audit.record(
            audit.actions.AGENT_START, target=agent_id,
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"id": agent_id, "status": "restarted"}

    @app.post("/api/agents/{agent_id}/uploads")
    async def upload_to_agent(agent_id: str, file: UploadFile):
        """Drag-drop image upload for terminal injection.

        Browser sandbox hides real file paths; agents see
        daemon-local paths. Validates agent exists (404),
        image content-type (415), size ≤ 25 MiB streamed
        (413), and writes to
        <config_home>/uploads/<agent_id>/<uuid8>-<safe_name>.
        """
        if not orch.get_agent(agent_id):
            raise HTTPException(404, f"Agent {agent_id} not found")

        content_type = (file.content_type or "").lower()
        ext = Path(file.filename or "").suffix.lower().lstrip(".")
        _IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "tif", "tiff", "avif"}
        if not content_type.startswith("image/") and ext not in _IMAGE_EXTS:
            raise HTTPException(
                415,
                f"File must be an image (got content-type={content_type!r}, ext={ext!r})",
            )

        # Stream chunks, enforce 25 MiB limit (never buffer unbounded)
        MAX_SIZE = 25 * 1024 * 1024
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SIZE:
                raise HTTPException(
                    413, f"File exceeds 25 MiB limit ({total} bytes)"
                )
            chunks.append(chunk)
        body = b"".join(chunks)

        # Sanitise basename: no path traversal, safe chars only, cap ~80
        raw = Path(file.filename or "image").name
        raw = raw.replace("/", "").replace("\\", "")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
        if len(safe) > 80:
            stem, dot = os.path.splitext(safe) if "." in safe else (safe, "")
            safe = stem[:76] + dot
        uid8 = uuid.uuid4().hex[:8]
        safe_name = f"{uid8}-{safe}"

        dest_dir = orch.config_home / "uploads" / agent_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / safe_name
        dest_path.write_bytes(body)

        try:
            _prune_uploads(dest_dir)
        except Exception:
            pass

        return {
            "path": str(dest_path),
            "name": file.filename or safe_name,
            "bytes": len(body),
            "content_type": file.content_type or "application/octet-stream",
        }

    @app.get("/api/agents/{agent_id}/screen")
    async def agent_screen(agent_id: str, cols: int = 200, rows: int = 50):
        """Plain-text snapshot of the agent's current screen.

        Pulls the harness's ring buffer (the same one that feeds
        the WS terminal replay) and renders it through a pyte
        terminal emulator so cursor moves, alt-screen toggles,
        and color codes become an actual visible grid of cells —
        not just raw ANSI bytes.

        Lets one agent inspect what another is showing
        ("reviewer, what's on coder's screen right now?") without
        spawning a viewer or parsing ANSI escapes by hand.
        """
        from relaydeck.screen import render

        instance: Any = orch.get_running_instance(agent_id)
        if instance is None:
            row = orch.get_agent(agent_id)
            if row is None:
                raise HTTPException(404, f"Agent {agent_id} not found")
            raise HTTPException(
                409,
                f"Agent {agent_id} is not running — no PTY to snapshot",
            )
        buf = bytes(instance.get_pty_buffer() or b"")
        text = render(buf, cols=max(20, cols), rows=max(5, rows))
        # Plain text is simpler for the CLI consumer than JSON;
        # the dashboard can fetch with `Accept: text/plain`.
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(text)

    @app.get("/api/agents/{agent_id}/state/stream")
    async def stream_agent_state(agent_id: str):
        """SSE: live `agent.status_changed` events for one agent.

        Backs `relaydeck agent wait`. Subscribes to the plugin bus for
        `agent.status_changed` and yields one SSE `data:` line per
        transition matching this agent. Heartbeats every 15s so an
        idle stream stays alive through proxies and urllib readers.
        """
        import queue as _queue

        if not orch.get_agent(agent_id):
            raise HTTPException(404, f"Agent {agent_id} not found")

        registry = getattr(app.state, "plugin_registry", None)
        bus = getattr(registry, "event_bus", None) if registry else None
        if bus is None:
            raise HTTPException(503, "plugin bus not available")

        q: _queue.Queue = _queue.Queue()

        def _handler(event):
            data = event.data or {}
            if data.get("agent_id") == agent_id:
                try:
                    q.put_nowait(data)
                except Exception:
                    pass

        bus.subscribe("agent.status_changed", _handler)

        async def gen():
            try:
                while True:
                    try:
                        data = await asyncio.get_event_loop().run_in_executor(
                            None, q.get, True, 15.0,
                        )
                        yield f"data: {json.dumps(data)}\n\n"
                    except _queue.Empty:
                        yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                try:
                    bus.unsubscribe(_handler)
                except Exception:
                    pass

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.post("/api/agents/{agent_id}/state")
    async def set_agent_state(agent_id: str, body: dict[str, Any]):
        """Set an agent's semantic (observable) state.

        Posted by vendor-side integration hooks (Claude Code, codex,
        pi, etc) when the harness fires lifecycle events. The
        operator-visible status (`relaydeck agent list`, dashboard tile,
        workspace roll-up, `agent wait`) reads from this.

        Body:
          {
            "status": "working" | "awaiting-input" |
                      "complete-unread" | "idle" | null,
            "source": "hook" | "mood" | "manual"  (default: "hook")
          }

        Emits `agent.status_changed` on the plugin bus iff the new
        value differs from the prior one — clients that subscribe
        for `relaydeck agent wait` see a single transition, not a flood
        of duplicate-value writes from a chatty hook.
        """
        from relaydeck.db import SEMANTIC_STATES

        if not orch.get_agent(agent_id):
            raise HTTPException(404, f"Agent {agent_id} not found")
        status = body.get("status")
        if status is not None and status not in SEMANTIC_STATES:
            raise HTTPException(
                400,
                f"invalid status {status!r}; "
                f"expected one of {list(SEMANTIC_STATES)} or null",
            )
        source = str(body.get("source") or "hook").strip() or "hook"
        try:
            changed = orch.set_semantic_status(agent_id, status, source=source)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": agent_id, "status": status, "changed": changed}

    @app.post("/api/agents/{agent_id}/viewed")
    async def mark_agent_viewed(agent_id: str):
        """Read-transition: the operator has looked at this agent, so clear a
        `complete-unread` ("result waiting") badge to `idle`.

        The web dashboard POSTs here when an agent becomes the focused view.
        Narrow + idempotent: only `complete-unread` is collapsed (source
        `viewer`); a working / awaiting-input / idle agent is left untouched,
        so viewing a busy agent never hides what it's doing.
        """
        if not orch.get_agent(agent_id):
            raise HTTPException(404, f"Agent {agent_id} not found")
        changed = orch.mark_agent_viewed(agent_id)
        return {"id": agent_id, "changed": changed}

    # ── Audit log (root scope only) ────────────────────────────────

    @app.get("/api/audit")
    async def list_audit_events(
        request: Request,
        since: float | None = None,
        action: str | None = None,
        token_id: str | None = None,
        target: str | None = None,
        limit: int = 100,
    ):
        """List audit events, newest first. Root scope only — a
        read-only token reading the audit log would let any read-only
        credential reconstruct the full mutation history, defeating
        the scope distinction. The middleware lets GETs through for
        read-only tokens by default, so the check is repeated here."""
        ident = _audit_identity(request)
        if ident.scope != SCOPE_ROOT:
            raise HTTPException(403, "audit log requires root scope")
        return audit.list_events(
            since=since, action=action, token_id=token_id,
            target=target, limit=min(int(limit), 1000),
            db_path=orch.db_path,
        )

    # ── Agent inbox (one agent) + workspace messaging ──────────

    @app.get("/api/agents/{agent_id}/inbox")
    async def agent_inbox(agent_id: str, unread: bool = False, limit: int = 50):
        """One agent's inbox, newest first."""
        from relaydeck.messages import list_inbox
        msgs = list_inbox(agent_id, unread=unread, limit=limit, db_path=orch.db_path)
        return [m.to_dict() for m in msgs]

    @app.get("/api/workspaces/{workspace}/inbox")
    async def workspace_inbox(
        workspace: str,
        agent: str | None = None,
        unread: bool = False,
        limit: int = 50,
    ):
        """Inbox across all agents in a workspace (or one agent within
        the workspace if `agent` is given), newest first."""
        from relaydeck.messages import list_workspace_inbox
        msgs = list_workspace_inbox(
            workspace, agent=agent, unread=unread, limit=limit,
            db_path=orch.db_path,
        )
        return [m.to_dict() for m in msgs]

    @app.post("/api/workspaces/{workspace}/messages")
    async def workspace_send_message(workspace: str, body: dict[str, Any]):
        """Send a message to one agent in the workspace (when `agent`
        is given) or broadcast to every agent in the workspace.

        Body: `{body, agent?, from?, in_reply_to?, format?, include_self?}`.
        Returns `{ids, injected, pending, recipients}` — `injected` /
        `pending` are the lists of message ids; `recipients` is a list
        of `{agent_id, status, msg_id, injected}` so the CLI can give
        the user honest feedback ("agent X is stopped" vs "agent X is
        running but message not yet on PTY — will live-drain shortly").

        Broadcast behavior: when no `agent` is given, every agent in
        the workspace receives. If `from` resolves to one of those
        agents and `include_self` is false (default), the sender is
        excluded — agents shouldn't trigger their own inbox loop.
        """
        text = str(body.get("body") or "").strip()
        if not text:
            raise HTTPException(400, "body is required")

        from_id = str(body.get("from") or "user").strip() or "user"
        in_reply_to = body.get("in_reply_to")
        format_override = body.get("format")
        include_self = bool(body.get("include_self") or False)
        target_agent = body.get("agent")

        all_agents = [
            a for a in orch.list_agents()
            if (a.get("workspace") or "") == workspace
        ]
        if not all_agents:
            raise HTTPException(404, f"No agents in workspace: {workspace}")

        if target_agent:
            recipients = [a for a in all_agents if a["id"] == str(target_agent)]
            if not recipients:
                raise HTTPException(
                    404, f"Agent {target_agent} not found in workspace {workspace}",
                )
        else:
            recipients = list(all_agents)
            # Sender is presumably another agent (not "user", not a
            # plugin) → exclude from a broadcast unless explicitly asked.
            if not include_self and from_id not in ("user",) and not from_id.startswith("plugin:"):
                recipients = [a for a in recipients if a["id"] != from_id]

        broadcast_id = None
        if not target_agent:
            from relaydeck.messages import new_broadcast_id
            broadcast_id = new_broadcast_id()

        ids: list[str] = []
        injected: list[str] = []
        pending: list[str] = []
        per_recipient: list[dict[str, Any]] = []
        for r in recipients:
            try:
                msg_id, was_injected = orch.send_message_to(
                    r["id"], text,
                    from_id=from_id,
                    in_reply_to=in_reply_to if in_reply_to else None,
                    broadcast_id=broadcast_id,
                    format=format_override if format_override else None,
                )
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            ids.append(msg_id)
            (injected if was_injected else pending).append(msg_id)
            # send_message_to may have reconciled a zombie row to
            # "stopped"; re-read so the CLI sees the truth, not the
            # status snapshot from the start of this handler.
            current = orch.get_agent(r["id"]) or {}
            per_recipient.append({
                "agent_id": r["id"],
                "status": current.get("status") or r.get("status") or "unknown",
                "msg_id": msg_id,
                "injected": bool(was_injected),
            })

        return {
            "ids": ids,
            "injected": injected,
            "pending": pending,
            "recipients": per_recipient,
        }

    @app.get("/api/workspaces/{workspace}/messages/stream")
    async def stream_workspace_messages(workspace: str):
        """SSE: live `agent.message` events scoped to this workspace.

        Backs `relaydeck workspace inbox -f`. Subscribes to the plugin
        event bus for `agent.message`, filters by `workspace`, and
        yields one SSE `data:` line per delivery. Plugin-bus handlers
        run in worker threads, so we route through a thread-safe
        queue.Queue and `run_in_executor(q.get, ...)`.

        Heartbeats every ~15s to keep idle proxies and the CLI's
        urllib reader happy.
        """
        import queue as _queue

        registry = getattr(app.state, "plugin_registry", None)
        bus = getattr(registry, "event_bus", None) if registry else None
        if bus is None:
            raise HTTPException(503, "plugin bus not available")

        q: _queue.Queue = _queue.Queue()

        def _handler(event):
            data = event.data or {}
            # Filter to this workspace. The `agent.message` payload
            # always carries `workspace` because the orchestrator
            # denormalizes it at insert time.
            if data.get("workspace") == workspace:
                try:
                    q.put_nowait(data)
                except Exception:
                    pass

        bus.subscribe("agent.message", _handler)

        async def gen():
            try:
                while True:
                    try:
                        data = await asyncio.get_event_loop().run_in_executor(
                            None, q.get, True, 15.0,
                        )
                        yield f"data: {json.dumps(data)}\n\n"
                    except _queue.Empty:
                        yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                try:
                    bus.unsubscribe(_handler)
                except Exception:
                    pass

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ── Events (SSE) ─────────────────────────────────────────────

    @app.get("/api/agents/{agent_id}/events")
    async def stream_events(agent_id: str, stream: bool = Query(False)):
        if not stream:
            since = 0
            return orch.get_events(agent_id, since_id=since)

        # SSE streaming
        q = orch.subscribe_events(agent_id)

        async def event_generator():
            try:
                while True:
                    try:
                        event = await asyncio.get_event_loop().run_in_executor(
                            None, q.get, True, 1.0
                        )
                        if event:
                            yield f"data: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        # Send heartbeat
                        yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                orch.unsubscribe_events(agent_id, q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/events")
    async def stream_all_events():
        """Broadcast all agent events."""
        q = orch.subscribe_events("*")

        async def event_generator():
            try:
                while True:
                    try:
                        event = await asyncio.get_event_loop().run_in_executor(
                            None, q.get, True, 1.0
                        )
                        if event:
                            yield f"data: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                orch.unsubscribe_events("*", q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ── WebSocket (harness PTY) ──────────────────────────────────
    #
    # Binary frame protocol — 1-byte type prefix + payload:
    #
    #   Server → client:
    #     0x00 + bytes  PTY output (write to xterm)
    #     0x01 + json   lifecycle / status (e.g. {"event":"pty_closed"})
    #
    #   Client → server:
    #     0x00 + bytes        stdin (write to PTY)
    #     0x01 + "cols rows"  resize (ASCII, space-separated ints)
    #     0x02                ping (no-op; keeps idle proxies happy)
    #
    # On connect we replay the harness's ring buffer (~64 KB) so a
    # refreshing tab sees screen context, not a blank pane.

    @app.websocket("/api/agents/{agent_id}/term")
    async def agent_terminal(websocket: WebSocket, agent_id: str):
        # WS handshake bypasses the HTTP auth middleware (Starlette
        # routes WS differently). Verify the token here before accept()
        # so an unauthenticated client never gets a live PTY pipe.
        token = websocket.query_params.get("token")
        if not verify_token(token):
            await websocket.close(code=4401)  # custom code: "unauthorized"
            return
        await websocket.accept()

        # Duck-typed: harness agents add subscribe_pty / send_input / resize.
        instance: Any = orch.get_running_instance(agent_id)
        if instance is None or not hasattr(instance, "subscribe_pty"):
            try:
                await websocket.send_bytes(b"\x01" + json.dumps(
                    {"event": "agent_not_running", "agent_id": agent_id}
                ).encode("utf-8"))
            except Exception:
                pass
            await websocket.close()
            return

        sub_q = instance.subscribe_pty()

        # Replay buffered bytes so the user sees the current screen state.
        #
        # We prefix the replay with `\e[2J\e[H` (clear screen + cursor
        # home). Two reasons:
        #
        #   1. The dashboard's xterm.js panel starts each session
        #      with whatever was last in the buffer; without a clear,
        #      stale paint from a previous attach can ghost-overlay
        #      the new replay.
        #
        #   2. CLI `relaydeck attach` in a tmux pane: the pane is bigger
        #      than the daemon's spawn-time PTY size, the replay
        #      bytes contain cursor moves sized for the old
        #      geometry. Clearing first means the replay starts on
        #      a known-blank canvas; the harness's SIGWINCH-driven
        #      redraw (triggered by the client's resize bump
        #      immediately after) then paints the current TUI at
        #      the right dimensions.
        #
        # Cost: scrollback above the welcome banner is gone. For an
        # interactive harness reattach that's the right trade-off —
        # users want to see the current screen, not paginate old
        # state at the wrong wrap width.
        buf_bytes = (
            instance.get_pty_buffer()
            if getattr(instance, "REPLAY_PTY_BUFFER", True)
            else b""
        )
        if buf_bytes and getattr(instance, "SANITIZE_PTY_REPLAY", False):
            buf_bytes = _sanitize_pty_replay(bytes(buf_bytes))
        if buf_bytes:
            try:
                await websocket.send_bytes(
                    b"\x00" + b"\x1b[0m\x1b[?25h\x1b[2J\x1b[H" + bytes(buf_bytes)
                )
            except Exception:
                instance.unsubscribe_pty(sub_q)
                return

        stop = asyncio.Event()

        async def pump_out() -> None:
            try:
                while not stop.is_set():
                    try:
                        chunk = await asyncio.to_thread(sub_q.get, True, 0.5)
                    except Exception:
                        continue
                    if chunk is None:
                        try:
                            await websocket.send_bytes(b"\x01" + b'{"event":"pty_closed"}')
                        except Exception:
                            pass
                        break
                    # Coalesce — batch immediately-available chunks into one
                    # frame so we don't WS-send per 4KB read on a busy stream.
                    out = bytearray(chunk)
                    while True:
                        try:
                            more = sub_q.get_nowait()
                        except Exception:
                            break
                        if more is None:
                            await websocket.send_bytes(b"\x00" + bytes(out))
                            await websocket.send_bytes(b"\x01" + b'{"event":"pty_closed"}')
                            return
                        out.extend(more)
                        if len(out) > 32 * 1024:
                            break
                    await websocket.send_bytes(b"\x00" + bytes(out))
            except WebSocketDisconnect:
                pass
            except Exception:
                pass
            finally:
                stop.set()

        async def pump_in() -> None:
            try:
                while not stop.is_set():
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        return
                    data = msg.get("bytes")
                    if not data:
                        continue
                    kind = data[0:1]
                    payload = data[1:]
                    if kind == b"\x00":
                        instance.send_input(payload)
                    elif kind == b"\x01":
                        try:
                            cols_s, rows_s = payload.decode("ascii", "ignore").split()
                            instance.resize(int(cols_s), int(rows_s))
                        except Exception:
                            pass
                    # 0x02 ping — receiving is enough; no action needed.
            except WebSocketDisconnect:
                return
            except Exception:
                return
            finally:
                stop.set()

        try:
            await asyncio.gather(pump_out(), pump_in())
        finally:
            instance.unsubscribe_pty(sub_q)
            try:
                await websocket.close()
            except Exception:
                pass

    # ── Workspace endpoints ──────────────────────────────────────

    # The orchestrator resolves a default config_home if create_app got None.
    home = orch.config_home

    def _read_config_toml() -> dict:
        import tomllib
        p = home / "config.toml"
        if not p.exists():
            return {}
        try:
            return tomllib.loads(p.read_text())
        except Exception:
            return {}

    def _write_config_toml(data: dict) -> None:
        import tomli_w
        p = home / "config.toml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(tomli_w.dumps(data))

    def _agent_toml_path(name: str):
        return home / "workspaces" / name / "agent.toml"

    def _read_agent_plugins(name: str) -> list[str]:
        import tomllib
        p = _agent_toml_path(name)
        if not p.exists():
            return []
        try:
            data = tomllib.loads(p.read_text())
            v = data.get("workspace", {}).get("plugins", [])
            return [str(x) for x in v] if isinstance(v, list) else []
        except Exception:
            return []

    def _write_agent_plugins(name: str, plugins: list[str]) -> None:
        plugin_list = "\n".join(f'  "{pl}",' for pl in plugins)
        body = (f"[workspace]\nplugins = [\n{plugin_list}\n]\n"
                if plugins else "[workspace]\nplugins = []\n")
        p = _agent_toml_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)

    def _fire_workspace_event(event_type: str, data: dict) -> None:
        """Best-effort emit on the plugin event bus so the messaging
        plugin's `workspace.added` subscriber (and friends) react to
        UI-driven workspace changes the same way they react to CLI
        ones."""
        try:
            registry = getattr(app.state, "plugin_registry", None)
            bus = getattr(registry, "event_bus", None) if registry else None
            if bus is not None:
                from relaydeck.plugin import Event
                bus.emit(Event(type=event_type, data=data, source_plugin="api"))
        except Exception:
            pass

    @app.get("/api/workspaces")
    async def list_workspaces():
        import asyncio
        from relaydeck import worktrees as wt

        registry = load_workspace_registry()

        def _build():
            batch = wt.batch_workspace_git_info(
                [(w.name, w.path) for w in registry],
                config_home=home,
            )
            return [
                {
                    "name": w.name,
                    "path": str(w.path),
                    "plugins": w.plugins,
                    "git": batch.get(w.name, wt._plain_git_info()),
                }
                for w in registry
            ]

        return await asyncio.to_thread(_build)

    @app.get("/api/workspace-plugins")
    async def list_workspace_plugin_catalog():
        """Canonical catalog of names that can appear in a workspace's
        `plugins = [...]` list. Anything not here has no per-workspace
        effect (always-on infrastructure / harnesses / providers).

        Used by the Workspaces tab to decide which checkboxes to show.
        """
        from relaydeck.plugin import get_registry
        from relaydeck.workspace_plugins import list_workspace_plugins
        registry = get_registry()
        return list_workspace_plugins(registry)

    @app.post("/api/workspaces")
    async def create_workspace(body: dict[str, Any]):
        """Register a new workspace. Mirrors `relaydeck workspace add` —
        writes the entry to config.toml and an agent.toml with the
        plugins list. Plugin events fire so `messaging` (or any other
        subscriber of `workspace.added`) reacts."""
        name = str(body.get("name") or "").strip()
        raw_path = str(body.get("path") or "").strip()
        plugins = _coerce_string_list(body.get("plugins"), "plugins")
        create_dir = bool(body.get("create_dir"))
        if not name or not raw_path:
            raise HTTPException(400, "name and path are required")
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            if not create_dir:
                raise HTTPException(400, f"path not found: {path}")
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(400, f"could not create directory: {exc}") from exc
        elif not path.is_dir():
            raise HTTPException(400, f"path is not a directory: {path}")

        data = _read_config_toml()
        workspaces = data.get("workspace", [])
        if any(w.get("name") == name for w in workspaces):
            raise HTTPException(409, f"workspace already registered: {name}")

        workspaces.append({"name": name, "path": str(path), "plugins": plugins})
        data["workspace"] = workspaces
        _write_config_toml(data)

        # State dir + agent.toml
        (home / "workspaces" / name).mkdir(parents=True, exist_ok=True)
        _write_agent_plugins(name, plugins)

        _fire_workspace_event("workspace.added", {"name": name, "path": str(path)})
        return {"name": name, "path": str(path), "plugins": plugins}

    @app.patch("/api/workspaces/{name}")
    async def update_workspace(name: str, body: dict[str, Any]):
        """Update a workspace's plugins list (the only thing meaningfully
        editable today). Updates both config.toml and the workspace's
        agent.toml so the source of truth stays consistent."""
        if "plugins" not in body:
            raise HTTPException(400, "plugins required")
        plugins = _coerce_string_list(body.get("plugins"), "plugins")

        data = _read_config_toml()
        workspaces = data.get("workspace", [])
        idx = next((i for i, w in enumerate(workspaces) if w.get("name") == name), -1)
        if idx < 0:
            raise HTTPException(404, f"workspace not found: {name}")
        workspaces[idx]["plugins"] = plugins
        data["workspace"] = workspaces
        _write_config_toml(data)

        _write_agent_plugins(name, plugins)

        _fire_workspace_event("workspace.updated",
                              {"name": name, "plugins": plugins})
        return {"name": name, "plugins": plugins}

    @app.delete("/api/workspaces/{name}")
    async def delete_workspace(name: str):
        """Remove a workspace from the registry. The on-disk workspace
        directory and its contents are NOT deleted — only the relaydeck
        registration. Drop the on-disk dir manually if you want a clean
        slate.

        Refuses if any agent is currently registered against this
        workspace; delete those first with `relaydeck agent rm` (or via UI).
        """
        agents_in_ws = [
            a for a in orch.list_agents()
            if (a.get("workspace") or "") == name
        ]
        if agents_in_ws:
            ids = ", ".join(a["id"] for a in agents_in_ws)
            raise HTTPException(
                409,
                f"refuses to remove workspace {name}: agents still registered ({ids})",
            )

        data = _read_config_toml()
        workspaces = data.get("workspace", [])
        if not any(w.get("name") == name for w in workspaces):
            raise HTTPException(404, f"workspace not found: {name}")
        data["workspace"] = [w for w in workspaces if w.get("name") != name]
        _write_config_toml(data)

        # Active-workspace cleanup so a stale pointer doesn't survive.
        try:
            from relaydeck.state import get_current_workspace, set_current_workspace
            if get_current_workspace() == name:
                set_current_workspace("")
        except Exception:
            pass

        _fire_workspace_event("workspace.removed", {"name": name})
        return {"name": name, "status": "removed"}

    # ── Worktrees (first-class git worktree → workspace) ─────────────
    #
    # A worktree is a registered workspace whose path is a linked git
    # worktree. These routes are the core, plugin-agnostic surface:
    # list/create/remove, with setup/teardown lifecycle hooks (a repo's
    # `.relaydeck/worktree.yaml`) so an agent lands in a provisioned env.
    # Create/remove emit `worktree.created` / `worktree.removed` so the
    # dashboard updates live and automations (loop agents with on_event)
    # can attach their own setup/shutdown work.

    @app.get("/api/worktrees")
    async def list_worktrees_api(repo: str | None = None):
        """Registered worktree workspaces with git status (branch/dirty/
        ahead-behind) + the agents on each. With `?repo=<path>`, instead
        list the raw `git worktree list` for that repo (incl. unregistered
        + the main checkout)."""
        from relaydeck import worktrees as wt
        if repo:
            try:
                rows = wt.list_worktrees(Path(repo).expanduser())
            except wt.WorktreeError as exc:
                raise HTTPException(400, str(exc))
            for r in rows:
                r["status"] = wt.worktree_status(Path(r.get("path", "")))
            return {"repo": repo, "worktrees": rows}
        agents_by_ws: dict[str, list[str]] = {}
        for a in orch.list_agents():
            agents_by_ws.setdefault(a.get("workspace") or "", []).append(a["id"])
        out = []
        for w in load_workspace_registry():
            wp = Path(w.path)
            if not wt.is_worktree(wp):
                continue
            out.append({
                "name": w.name, "path": str(w.path), "plugins": w.plugins,
                "status": wt.worktree_status(wp),
                "agents": agents_by_ws.get(w.name, []),
            })
        return out

    @app.post("/api/worktrees")
    async def create_worktree_api(body: dict[str, Any], request: Request):
        """Create a git worktree + register it as a workspace + run the
        setup hook. Body: {repo, branch, name?, base?, create_branch?,
        plugins?, setup?, run_setup?}."""
        from relaydeck import worktrees as wt
        repo = str((body or {}).get("repo") or "").strip()
        branch = str((body or {}).get("branch") or "").strip()
        if not repo or not branch:
            raise HTTPException(400, "repo and branch are required")
        try:
            result = wt.create_worktree_workspace(
                home, Path(repo), branch,
                name=(body.get("name") or None),
                base=(body.get("base") or None),
                create_branch=bool(body.get("create_branch", True)),
                plugins=body.get("plugins") or None,
                setup=body.get("setup"),
                run_setup=bool(body.get("run_setup", True)),
            )
        except wt.WorktreeError as exc:
            raise HTTPException(400, str(exc))
        except ValueError as exc:
            # e.g. duplicate workspace name — the worktree was rolled back
            # inside create_worktree_workspace, so this is a clean conflict.
            raise HTTPException(409, str(exc))
        audit.record(
            audit.actions.PLUGIN_SETTINGS_CHANGE, target=f"worktree:{result['name']}",
            payload={"repo": repo, "branch": branch},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        _fire_workspace_event("workspace.added",
                              {"name": result["name"], "path": result["path"]})
        _fire_workspace_event("worktree.created", {
            "name": result["name"], "path": result["path"],
            "branch": branch, "repo": result["repo"],
        })
        return result

    @app.delete("/api/worktrees/{name}")
    async def remove_worktree_api(
        name: str, request: Request,
        force: bool = False, run_teardown: bool = True, delete_dir: bool = True,
    ):
        """Tear down a worktree workspace: teardown hook → `git worktree
        remove` → unregister. Refuses if agents are still registered."""
        from relaydeck import worktrees as wt
        agents_in_ws = [a for a in orch.list_agents() if (a.get("workspace") or "") == name]
        if agents_in_ws:
            ids = ", ".join(a["id"] for a in agents_in_ws)
            raise HTTPException(409, f"refuses to remove worktree {name}: agents still registered ({ids})")
        result = wt.remove_worktree_workspace(
            home, name, force=force, run_teardown=run_teardown, delete_dir=delete_dir,
        )
        if result.get("error") == "no such workspace":
            raise HTTPException(404, f"no such worktree workspace: {name}")
        if result.get("error") == "not a worktree workspace":
            raise HTTPException(
                400, f"{name!r} is a regular workspace, not a worktree — "
                "use `relaydeck workspace rm` / the Workspaces remove action")
        audit.record(
            audit.actions.PLUGIN_SETTINGS_CHANGE, target=f"worktree:{name}",
            payload={"removed": result.get("removed")},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        _fire_workspace_event("worktree.removed", {"name": name})
        _fire_workspace_event("workspace.removed", {"name": name})
        return result

    @app.get("/api/state/active-workspace")
    async def get_active_workspace():
        from relaydeck.state import get_current_workspace
        return {"name": get_current_workspace()}

    @app.post("/api/state/active-workspace")
    async def set_active_workspace(body: dict[str, Any]):
        from relaydeck.state import set_current_workspace
        name = str(body.get("name") or "").strip()
        set_current_workspace(name)
        return {"name": name or None}

    # ── Filesystem browser ──────────────────────────────────────

    @app.get("/api/fs/browse")
    async def fs_browse(path: str = ""):
        """Lightweight directory browser used by the workspace-create
        modal. Returns the absolute resolved path + parent + a sorted
        list of child directories + validation flags the UI uses to
        render path-correctness chips (✓ git repo, ⚠ already a
        workspace, ⚠ read-only).

        Refuses non-directories and silently filters dotfiles + entries
        the daemon can't read. The daemon runs as the user, so this is
        bounded by the user's own permissions — same as a `ls`.
        """
        target = Path(path).expanduser() if path else Path.home()
        try:
            target = target.resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(404, f"path not found: {path}")
        if not target.is_dir():
            raise HTTPException(400, f"not a directory: {target}")

        entries: list[dict] = []
        try:
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if child.name.startswith("."):
                    continue
                try:
                    if child.is_dir():
                        entries.append({"name": child.name, "path": str(child)})
                except OSError:
                    continue
        except PermissionError as exc:
            raise HTTPException(403, f"permission denied: {exc}") from exc

        # Inline validation: cheap probes the frontend renders as chips so
        # the operator doesn't have to commit + read a 4xx to learn the
        # path's status. Each probe fails-safe to False.
        is_git_repo = False
        try:
            # Accept both regular repos (.git is a dir) and worktrees /
            # submodules (.git is a file). git itself does this check.
            git_marker = target / ".git"
            is_git_repo = git_marker.is_dir() or git_marker.is_file()
        except OSError:
            pass
        writable = os.access(str(target), os.W_OK)
        existing_workspace: str | None = None
        try:
            for w in load_workspace_registry(orch.config_home):
                try:
                    if Path(w.path).resolve(strict=False) == target:
                        existing_workspace = w.name
                        break
                except OSError:
                    continue
        except Exception:
            pass

        parent = str(target.parent) if target.parent != target else None
        return {
            "path": str(target),
            "parent": parent,
            "home": str(Path.home()),
            "entries": entries,
            "is_git_repo": is_git_repo,
            "writable": writable,
            "existing_workspace": existing_workspace,
        }

    # ── Model presets ────────────────────────────────────────────

    @app.get("/api/presets")
    async def list_presets():
        from relaydeck.db import get_preset_usage_map, open_db
        conn = open_db(orch.db_path)
        try:
            usage = get_preset_usage_map(conn)
        finally:
            conn.close()
        out = []
        for p in load_model_presets(orch.config_home):
            u = usage.get((p.model or "").lower(), {})
            out.append({
                "name": p.name, "provider": p.provider, "model": p.model,
                "tokens_24h": u.get("tokens_24h", 0),
                "requests_24h": u.get("requests_24h", 0),
                "spark": u.get("spark", []),
            })
        return out

    @app.post("/api/presets")
    async def create_preset(body: dict[str, Any]):
        import yaml
        name = (body.get("name") or "").strip()
        if not name or not body.get("provider") or not body.get("model"):
            raise HTTPException(400, "name, provider, model are required")
        presets_dir = orch.config_home / "presets"
        presets_dir.mkdir(parents=True, exist_ok=True)
        # A preset is purely name → (provider, model). Auth/endpoint live on
        # the provider, so legacy base_url/api_key_env/sampling keys are dropped.
        out = {k: body[k] for k in ("name", "provider", "model")}
        (presets_dir / f"{name}.yaml").write_text(yaml.safe_dump(out, sort_keys=False))
        return {"name": name, "status": "created"}

    @app.delete("/api/presets/{name}")
    async def delete_preset(name: str):
        p = orch.config_home / "presets" / f"{name}.yaml"
        if not p.exists():
            raise HTTPException(404, f"Preset {name} not found")
        p.unlink()
        return {"name": name, "status": "deleted"}

    @app.patch("/api/presets/{name}")
    async def update_preset(name: str, body: dict[str, Any]):
        """Update a preset's provider/model. Renaming is not supported here
        — delete + recreate is the model for renames so dependents (agents
        pointing at the preset) don't silently break."""
        import yaml
        current = next((x for x in load_model_presets(orch.config_home) if x.name == name), None)
        if current is None:
            raise HTTPException(404, f"Preset {name} not found")
        merged: dict[str, Any] = {
            "name": current.name,
            "provider": current.provider,
            "model": current.model,
        }
        for k in ("provider", "model"):
            if k in body:
                merged[k] = body[k]
        if not merged.get("provider") or not merged.get("model"):
            raise HTTPException(400, "provider and model are required")
        presets_dir = orch.config_home / "presets"
        presets_dir.mkdir(parents=True, exist_ok=True)
        (presets_dir / f"{name}.yaml").write_text(yaml.safe_dump(merged, sort_keys=False))
        return {"name": name, "status": "updated"}

    @app.get("/api/presets/{name}/check")
    async def check_preset(name: str):
        """Validate a preset against its provider's catalog.

        Returns: {ok, provider, model, suggestion?, provider_known}.
        `provider_known` is False when no plugin is registered for the
        preset's provider — clients can render that as a warning, not
        an error.
        """
        preset = next((p for p in load_model_presets(orch.config_home) if p.name == name), None)
        if preset is None:
            raise HTTPException(404, f"Preset {name} not found")
        from relaydeck.plugin import get_provider
        prov = get_provider(preset.provider)
        if prov is None:
            return {"ok": True, "provider": preset.provider, "model": preset.model,
                    "provider_known": False, "suggestion": None}
        ok, suggestion = prov.validate(preset.model)
        return {"ok": ok, "provider": preset.provider, "model": preset.model,
                "provider_known": True, "suggestion": suggestion}

    @app.get("/api/presets/{name}/stats")
    async def preset_stats(name: str):
        """Real per-preset telemetry for the detail screen, from
        `usage_records` (request/token series, 24h totals, used_by) +
        `model_invocations` (latency/success + traced recent calls).
        Everything is honest: empty series and null rates when nothing
        has been recorded — never fabricated."""
        import time as _time

        from relaydeck.db import get_model_stats, open_db
        from relaydeck.model_invocations import list_by_model, rollup_by_model
        preset = next((p for p in load_model_presets(orch.config_home) if p.name == name), None)
        if preset is None:
            raise HTTPException(404, f"Preset {name} not found")
        conn = open_db(orch.db_path)
        try:
            stats = get_model_stats(conn, preset.model, provider=preset.provider)
        finally:
            conn.close()
        day_ago = _time.time() - 86400.0
        latency = rollup_by_model(preset.model, since_ts=day_ago, db_path=orch.db_path)
        recent = [r.to_dict() for r in list_by_model(preset.model, limit=30, db_path=orch.db_path)]
        return {
            "name": preset.name,
            "provider": preset.provider,
            "model": preset.model,
            **stats,
            "latency": latency,
            "recent": recent,
        }

    @app.get("/api/models/resolve")
    async def resolve_model_spec(spec: str = ""):
        """Standardized model resolution for the shared model selector.

        Resolves any spec — a preset name, a built-in alias, an explicit
        `provider/model`, or a bare id — into the concrete
        `(provider, model)` it will run as, plus where it came from and a
        soft validation warning (never blocks; provider.validate fails
        open). Lets every model picker show the same resolved target."""
        from relaydeck.config import load_model_presets
        from relaydeck.plugin import get_provider
        from relaydeck.sdk import resolve_model

        spec = (spec or "").strip()
        if not spec:
            return {"spec": "", "provider": None, "model": None,
                    "source": "default", "preset": None,
                    "provider_known": False, "valid": True, "warning": None}

        preset = next((p for p in load_model_presets(orch.config_home) if p.name == spec), None)
        if spec.startswith("role:"):
            source = "role"
        elif preset is not None:
            source = "preset"
        elif "/" in spec:
            source = "provider/model"
        else:
            source = "bare"

        try:
            provider, model = resolve_model(spec, orch.config_home)
        except (ValueError, RuntimeError) as exc:
            # An unconfigured role (now any role until the operator sets it)
            # — surface it as invalid with the hint, don't 500 the picker.
            return {"spec": spec, "provider": None, "model": None,
                    "source": source, "preset": None,
                    "provider_known": False, "valid": False, "warning": str(exc)}
        prov = get_provider(provider)
        provider_known = prov is not None
        valid, suggestion = (prov.validate(model) if prov else (True, None))

        # models.dev metadata enrichment (fail-open): capabilities + price for
        # the picker, and a softer warning when the live catalog misses but
        # models.dev recognizes the model (i.e. it's likely real, the catalog
        # just hasn't surfaced it yet).
        md_caps: list[str] = []
        md_price = None
        md_known = False
        try:
            from relaydeck import models_dev

            if provider and model:
                md_known = models_dev.get_model_meta(
                    provider, model, orch.config_home, cache_only=True
                ) is not None
                if md_known:
                    md_caps = models_dev.model_capabilities(
                        provider, model, orch.config_home, cache_only=True
                    )
                    p = models_dev.get_price(
                        provider, model, orch.config_home, cache_only=True
                    )
                    if p is not None:
                        md_price = {"input": p[0], "output": p[1]}
        except Exception:
            pass

        warning = None
        if source == "role":
            warning = None  # resolved through a role; concrete target shown
        elif source == "bare":
            warning = (f"No provider in '{spec}' — defaulting to {provider}. "
                       "Prefer a preset or provider/model.")
        elif not provider_known:
            warning = f"Provider '{provider}' has no registered plugin."
        elif not valid:
            if md_known:
                # The live catalog hasn't surfaced it, but models.dev lists
                # it — soft, non-alarming note instead of "not in catalog".
                warning = (f"'{model}' isn't in the live {provider} catalog yet, "
                           "but models.dev recognizes it.")
            else:
                warning = f"'{model}' is not in the {provider} catalog"
                if suggestion:
                    warning += f" — did you mean {suggestion}?"
        return {"spec": spec, "provider": provider, "model": model,
                "source": source, "preset": preset.name if preset else None,
                "provider_known": provider_known, "valid": valid,
                "warning": warning,
                "capabilities": md_caps, "price": md_price,
                "models_dev_known": md_known}

    # ── Model roles (defaults-for-jobs) ──────────────────────────

    def _required_roles_unmet() -> dict[str, list[str]]:
        """Map role → [plugin names] that declare it required but where the
        role is unconfigured AND has no fallback (a real onboarding gap).
        Roles with a fallback are never "unmet" — they resolve fine."""
        from relaydeck.model_roles import effective_spec
        out: dict[str, list[str]] = {}
        registry = getattr(app.state, "plugin_registry", None)
        entries = registry.all() if registry else []
        for entry in entries:
            roles = getattr(getattr(entry, "manifest", None),
                            "required_model_roles", ()) or ()
            for role in roles:
                if effective_spec(role, orch.config_home) is None:
                    out.setdefault(role, []).append(entry.name)
        return out

    @app.get("/api/model-roles")
    async def list_model_roles():
        """Every semantic role + its effective model and where it resolved
        from (operator default | built-in fallback | unset), plus which
        roles enabled plugins need but no one has configured."""
        from relaydeck.model_roles import role_status
        rows = role_status(orch.config_home)
        unmet = _required_roles_unmet()
        for r in rows:
            r["required_by"] = unmet.get(r["name"], [])
        return {"roles": rows, "unmet": unmet}

    @app.put("/api/model-roles/{role}")
    async def set_model_role(role: str, body: dict[str, Any], request: Request):
        """Set the operator default model for a role. `spec` is a preset
        name | alias | provider/model. Validated (resolved + soft warning)
        but never blocked — provider catalogs are partial by design."""
        from relaydeck.model_roles import is_role, set_role_default
        from relaydeck.plugin import get_provider
        from relaydeck.sdk import resolve_model
        if not is_role(role):
            raise HTTPException(404, f"Unknown model role {role!r}")
        spec = str((body or {}).get("spec") or "").strip()
        if not spec:
            raise HTTPException(400, "spec is required")
        warning = None
        try:
            provider, model = resolve_model(spec, orch.config_home)
            prov = get_provider(provider)
            if prov is None:
                warning = f"Provider '{provider}' has no registered plugin."
            else:
                ok, suggestion = prov.validate(model)
                if not ok:
                    warning = f"'{model}' is not in the {provider} catalog"
                    if suggestion:
                        warning += f" — did you mean {suggestion}?"
        except (ValueError, RuntimeError) as exc:
            warning = str(exc)
        try:
            set_role_default(role, spec, orch.config_home)
        except ValueError as exc:
            # e.g. a self/role cycle — reject before persisting.
            raise HTTPException(400, str(exc))
        audit.record(
            audit.actions.PLUGIN_SETTINGS_CHANGE, target=f"model-role:{role}",
            payload={"spec": spec},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        _fire_workspace_event("model_roles.changed", {"role": role, "spec": spec})
        return {"role": role, "spec": spec, "warning": warning}

    @app.delete("/api/model-roles/{role}")
    async def unset_model_role(role: str, request: Request):
        """Clear a role's operator default (it reverts to its built-in
        fallback, or 'unset' for modality roles)."""
        from relaydeck.model_roles import is_role, unset_role_default
        if not is_role(role):
            raise HTTPException(404, f"Unknown model role {role!r}")
        existed = unset_role_default(role, orch.config_home)
        audit.record(
            audit.actions.PLUGIN_SETTINGS_CHANGE, target=f"model-role:{role}",
            payload={"cleared": True},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        _fire_workspace_event("model_roles.changed", {"role": role, "spec": None})
        return {"role": role, "cleared": existed}

    # ── Provider catalogs ────────────────────────────────────────

    @app.get("/api/providers")
    async def list_providers_api():
        """All registered provider plugins + their config state, for the
        Providers settings section. `key_env`/`has_key` drive the API-key
        field; `base_url`/`default_base_url` drive the endpoint override.
        New provider plugins appear here automatically."""
        from relaydeck.db import get_provider_usage_map, open_db
        from relaydeck.plugin import list_providers
        conn = open_db(orch.db_path)
        try:
            usage = get_provider_usage_map(conn)
        finally:
            conn.close()
        # Presets grouped by provider (names only) for "N presets use this".
        presets_by_provider: dict[str, list[str]] = {}
        for pr in load_model_presets(orch.config_home):
            presets_by_provider.setdefault((pr.provider or "").lower(), []).append(pr.name)
        out = []
        for p in list_providers():
            models = p.list_models()
            key_env = getattr(p, "key_env", "") or ""
            u = usage.get(p.provider_name.lower(), {})
            pnames = presets_by_provider.get(p.provider_name.lower(), [])
            # models.dev enrichment (fail-open): logo (served via our proxy,
            # never hotlinked) + env-key hints for the key field.
            logo = None
            env_hints: list[str] = []
            try:
                from relaydeck import models_dev
                if models_dev.get_provider_meta(p.provider_name, orch.config_home, cache_only=True):
                    logo = f"/api/providers/{p.provider_name}/logo"
                env_hints = models_dev.get_env_hints(
                    p.provider_name, orch.config_home, cache_only=True
                )
            except Exception:
                pass
            out.append({
                "name": p.provider_name,
                "version": p.version,
                "description": p.description,
                "model_count": len(models),
                "last_refresh_ts": p.last_refresh_ts(),
                "key_env": key_env,
                "needs_key": bool(key_env),
                "has_key": bool(getattr(p, "has_api_key", lambda: False)()),
                "base_url": p.resolved_base_url(orch.config_home) if hasattr(p, "resolved_base_url") else None,
                "default_base_url": getattr(p, "default_base_url", None),
                "custom": bool(getattr(p, "custom", False)),
                "api": getattr(p, "api", "native"),
                "presets": pnames,
                "preset_count": len(pnames),
                "tokens_24h": u.get("tokens_24h", 0),
                "cost_24h": u.get("cost_24h", 0.0),
                "requests_24h": u.get("requests_24h", 0),
                "logo_url": logo,
                "env_hints": env_hints,
            })
        return out

    @app.get("/api/providers/{name}/logo")
    async def provider_logo(name: str):
        """Proxy the provider's models.dev logo from the LOCAL cache —
        never hotlink. Keeps the dashboard offline-friendly and avoids
        coupling page render to models.dev availability. Served as an SVG
        image (so the dashboard renders it via <img src>, not inline — a
        third-party SVG can carry script, image context neutralizes it).
        Fail-open: 404 when there's no logo, so the dashboard falls back to
        a placeholder without a broken layout."""
        from fastapi.responses import Response
        from relaydeck import models_dev
        try:
            svg = await asyncio.to_thread(models_dev.fetch_logo, name, orch.config_home)
        except Exception:
            svg = None
        if not svg:
            raise HTTPException(404, "no logo")
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.post("/api/providers")
    async def create_custom_provider(body: dict[str, Any], request: Request):
        """Add a user-defined provider without writing a plugin. Body: name,
        base_url, optional api ('openai'|'anthropic'|'ollama', default
        openai), key_env, description. 'ollama' registers a native local
        endpoint (so you can run several Ollama hosts, e.g. ollama-rig);
        it needs no key. The API key itself is set separately via
        /api/vault/keys."""
        from relaydeck.plugin import get_provider
        from relaydeck.providers_extra import add_custom
        name = (body.get("name") or body.get("id") or "").strip().lower()
        base_url = (body.get("base_url") or "").strip()
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(400, "name is required (alphanumeric / - / _)")
        if not base_url:
            raise HTTPException(400, "base_url is required")
        if get_provider(name) is not None:
            raise HTTPException(409, f"a provider named {name!r} already exists")
        api = (body.get("api") or "openai").lower()
        if api not in ("openai", "anthropic", "ollama"):
            raise HTTPException(400, "api must be 'openai', 'anthropic', or 'ollama'")
        # Local (ollama) endpoints are keyless by default; remote endpoints
        # default to a per-provider env var name.
        default_key_env = "" if api == "ollama" else f"{name.upper()}_API_KEY"
        default_desc = (f"{name} (local Ollama endpoint)" if api == "ollama"
                        else f"{name} (custom {api}-compatible)")
        spec = {
            "name": name, "base_url": base_url, "api": api,
            "key_env": (body.get("key_env") or default_key_env).strip(),
            "description": (body.get("description") or default_desc).strip(),
        }
        add_custom(spec, config_home=orch.config_home)
        audit.record(
            audit.actions.PLUGIN_SETTINGS_CHANGE, target=f"provider:{name}",
            payload={"action": "create-custom", "api": api},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"name": name, "status": "created", "key_env": spec["key_env"]}

    @app.get("/api/providers/detect")
    async def detect_providers():
        """Probe known local ports (Ollama/vLLM/LM Studio) and report the
        reachable model servers — backs onboarding's 'we found N local
        models' and the Models-lens 'detected — add?' banner. Read-only."""
        from relaydeck.local_providers import detect_local_providers
        cands = detect_local_providers(orch.config_home)
        return {"candidates": [c.to_dict() for c in cands]}

    @app.post("/api/providers/detect")
    async def register_detected(body: dict[str, Any], request: Request):
        """Register a detected local endpoint as a provider (idempotent).
        Body: name, base_url, api. A name/endpoint that already exists is a
        no-op success, so onboarding's 'Add + use' is safe to retry."""
        from relaydeck.plugin import get_provider
        from relaydeck.providers_extra import add_custom
        name = (body.get("name") or "").strip().lower()
        base_url = (body.get("base_url") or "").strip()
        api = (body.get("api") or "ollama").lower()
        # Same name rule as POST /api/providers — this route also persists a
        # provider, so a name must round-trip cleanly through provider/model
        # specs and URLs.
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(400, "name is required (alphanumeric / - / _)")
        if not base_url:
            raise HTTPException(400, "base_url is required")
        if api not in ("openai", "anthropic", "ollama"):
            raise HTTPException(400, "api must be 'openai', 'anthropic', or 'ollama'")
        existing = get_provider(name)
        if existing is not None:
            return {"name": name, "status": "exists"}
        spec = {
            "name": name, "base_url": base_url, "api": api,
            "key_env": "" if api == "ollama" else f"{name.upper()}_API_KEY",
            "description": (f"{name} (local Ollama endpoint)" if api == "ollama"
                            else f"{name} (detected {api}-compatible)"),
        }
        add_custom(spec, config_home=orch.config_home)
        audit.record(
            audit.actions.PLUGIN_SETTINGS_CHANGE, target=f"provider:{name}",
            payload={"action": "register-detected", "api": api},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"name": name, "status": "created", "key_env": spec["key_env"]}

    @app.delete("/api/providers/{name}")
    async def delete_custom_provider(name: str, request: Request):
        """Remove a user-defined custom provider. Built-in / known
        providers can't be deleted (404)."""
        from relaydeck.plugin import get_provider
        from relaydeck.providers_extra import remove_custom
        p = get_provider(name)
        if p is None or not getattr(p, "custom", False):
            raise HTTPException(404, f"no custom provider named {name!r}")
        remove_custom(name, config_home=orch.config_home)
        audit.record(
            audit.actions.PLUGIN_SETTINGS_CHANGE, target=f"provider:{name}",
            payload={"action": "delete-custom"},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"name": name, "status": "deleted"}

    @app.put("/api/providers/{name}/config")
    async def set_provider_config(name: str, body: dict[str, Any], request: Request):
        """Set a provider's operator config. Today: `base_url` override
        (None/empty clears it). API keys are set via /api/vault/keys; this
        endpoint only touches non-secret config."""
        from relaydeck.plugin import get_provider
        from relaydeck.provider_config import set_base_url
        if get_provider(name) is None:
            raise HTTPException(404, f"Provider {name} not found")
        if "base_url" in body:
            set_base_url(name, (body.get("base_url") or "").strip() or None,
                         config_home=orch.config_home)
        audit.record(
            audit.actions.PLUGIN_SETTINGS_CHANGE, target=f"provider:{name}",
            payload={"fields": ["base_url"]},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        p = get_provider(name)
        return {"name": name, "base_url": p.resolved_base_url(orch.config_home),
                "has_key": bool(p.has_api_key())}

    @app.get("/api/providers/{name}/models")
    async def provider_models(name: str):
        """Full catalog for one provider."""
        from relaydeck.plugin import get_provider
        p = get_provider(name)
        if p is None:
            raise HTTPException(404, f"Provider {name} not found")
        return [m.__dict__ for m in p.list_models()]

    @app.post("/api/providers/{name}/refresh")
    async def provider_refresh(name: str):
        """Force re-fetch the catalog from upstream."""
        from relaydeck.plugin import get_provider
        p = get_provider(name)
        if p is None:
            raise HTTPException(404, f"Provider {name} not found")
        try:
            models = p.refresh()
        except Exception as exc:
            raise HTTPException(502, f"refresh failed: {exc}")
        return {"name": name, "model_count": len(models),
                "refreshed_at": p.last_refresh_ts()}

    # ── Tasks (plugin-owned work records) ────────────────────────

    @app.get("/api/tasks")
    async def list_tasks_api(
        workspace: str | None = None,
        plugin: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ):
        """Read-only task query for the dashboard. Tasks are written by
        plugins through `host.tasks`; this surfaces them for display
        across workspaces. Filterable by workspace / plugin / agent /
        status."""
        from relaydeck.tasks import list_tasks
        rows = list_tasks(
            plugin=plugin, workspace=workspace, agent_id=agent_id,
            status=status, limit=int(limit), db_path=orch.db_path,
        )
        return {"tasks": [t.to_dict() for t in rows]}

    @app.get("/api/tasks/{task_id}")
    async def get_task_api(task_id: str):
        from relaydeck.tasks import get_task
        task = get_task(task_id, db_path=orch.db_path)
        if task is None:
            raise HTTPException(404, f"no such task: {task_id}")
        return task.to_dict()

    # ── Automation run history ───────────────────────────────────

    @app.get("/api/automations")
    async def list_automations_api():
        """Configurable workers (loop agents) for the unified Workers
        lens, plus any history-only producers.

        Spec-driven: every loop agent appears here — even one that has
        never run — with its **triggers** (schedule = just another
        trigger: interval / cron / on_event) and its **attached action
        kinds** (model / code / script / gh / agent.message / bus.emit),
        merged with its run-history summary and live `agent_status`. Run
        records keyed to an id that's no longer a loop spec (a deleted
        worker, or a non-loop producer) still show as `history only`."""
        from relaydeck.automation_runs import list_automation_ids
        from relaydeck.config import load_agent_specs
        from relaydeck.db import open_db

        run_summary = {
            r["automation_id"]: r
            for r in list_automation_ids(db_path=orch.db_path)
        }
        specs = [s for s in load_agent_specs(orch.config_home) if s.type == "loop"]

        conn = open_db(orch.db_path)
        try:
            status_by_id = {
                row["id"]: row["status"]
                for row in conn.execute("SELECT id, status FROM agents").fetchall()
            }
        finally:
            conn.close()

        def _row(aid, *, name, atype, workspace, is_agent, trigger, actions, rs):
            status = status_by_id.get(aid)
            return {
                "automation_id": aid,
                "name": name,
                "automation_type": atype,
                "workspace": workspace,
                "is_agent": is_agent,
                "agent_status": status,
                "trigger": trigger,
                "action_kinds": actions,
                "runs": rs.get("runs", 0),
                "last_status": rs.get("last_status"),
                "last_started_at": rs.get("last_started_at"),
                "last_duration_ms": rs.get("last_duration_ms"),
                "last_error_count": rs.get("last_error_count", 0),
                "next_fire_at": _next_fire_at(trigger, rs.get("last_started_at"), status),
            }

        out = []
        seen = set()
        for s in specs:
            seen.add(s.id)
            out.append(_row(
                s.id, name=s.name, atype="loop", workspace=s.workspace,
                is_agent=True,
                trigger=_summarize_trigger(s.config.get("schedule")),
                actions=_summarize_action_kinds(s.config.get("actions")),
                rs=run_summary.get(s.id, {}),
            ))
        for aid, rs in run_summary.items():
            if aid in seen:
                continue
            out.append(_row(
                aid, name=aid, atype=rs.get("automation_type", ""),
                workspace=rs.get("workspace"), is_agent=aid in status_by_id,
                trigger=None, actions=[], rs=rs,
            ))
        return {"automations": out}

    @app.get("/api/automations/{automation_id}/runs")
    async def list_automation_runs_api(
        automation_id: str,
        status: str | None = None,
        limit: int = 50,
    ):
        from relaydeck.automation_runs import list_runs
        rows = list_runs(
            automation_id=automation_id, status=status,
            limit=int(limit), db_path=orch.db_path,
        )
        return {"runs": [r.to_dict() for r in rows]}

    @app.get("/api/automations/{automation_id}/invocations")
    async def list_worker_invocations_api(automation_id: str, limit: int = 50):
        """Per-call LLM invocation log for a worker — what it asked a
        model, when, latency, tokens (real when the provider reports
        them), ok/error. Powers the Workers lens 'LLM invocations' card."""
        from relaydeck.model_invocations import list_invocations, rollup
        return {
            "invocations": [
                i.to_dict() for i in list_invocations(
                    automation_id, limit=int(limit), db_path=orch.db_path)
            ],
            "rollup": rollup(automation_id, db_path=orch.db_path),
        }

    @app.post("/api/automations/validate")
    async def validate_worker_config(body: dict[str, Any]):
        """Validate a worker's trigger + actions before create/edit so the
        web form can give immediate feedback. Cron validity is
        server-side knowledge (croniter runs on the daemon), so this
        can't be done client-only. Returns {ok, errors:[...]}."""
        from relaydeck.automation import action_kinds

        errors: list[str] = []
        schedule = body.get("schedule")
        if not schedule:
            errors.append("A trigger (schedule) is required.")
        else:
            try:
                from relaydeck.automation import parse_schedule
                parse_schedule(str(schedule))
            except Exception as exc:
                errors.append(str(exc))
        actions = body.get("actions")
        if actions is not None:
            if not isinstance(actions, list):
                errors.append("actions must be a list.")
            else:
                known = set(action_kinds())
                for i, a in enumerate(actions):
                    if not isinstance(a, dict) or len(a) != 1:
                        errors.append(f"action {i + 1} must be a single-key mapping.")
                        continue
                    kind = next(iter(a.keys()))
                    if kind not in known:
                        errors.append(
                            f"action {i + 1}: unknown kind {kind!r} "
                            f"(known: {', '.join(sorted(known))})."
                        )
        return {"ok": not errors, "errors": errors}

    @app.post("/api/automations/{automation_id}/run")
    async def run_automation_now(automation_id: str, request: Request):
        """Run-now: fire one immediate tick on the live loop automation.
        409 if it isn't running (resume it first); 400 if the agent
        exists but isn't a loop automation."""
        try:
            dispatched = orch.trigger_loop_tick(automation_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not dispatched:
            raise HTTPException(
                409, f"automation {automation_id!r} is not running — resume it first"
            )
        audit.record(
            audit.actions.AGENT_START, target=automation_id,
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"id": automation_id, "status": "tick-dispatched"}

    @app.post("/api/automations/{automation_id}/pause")
    async def pause_automation(automation_id: str, request: Request):
        """Pause = stop the backing loop agent. Idempotent."""
        await asyncio.to_thread(orch.stop_agent, automation_id)
        audit.record(
            audit.actions.AGENT_STOP, target=automation_id,
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"id": automation_id, "status": "paused"}

    @app.post("/api/automations/{automation_id}/resume")
    async def resume_automation(automation_id: str, request: Request):
        """Resume = start the backing loop agent."""
        try:
            await asyncio.to_thread(orch.start_agent, automation_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        audit.record(
            audit.actions.AGENT_START, target=automation_id,
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {"id": automation_id, "status": "resumed"}

    # ── Daemon lifecycle (web-first: restart from the dashboard) ──

    # ── Danger zone: wipe messages + history ────────────────────────
    #
    # Operator-initiated wholesale deletes (vs the age-based prune the
    # db.maintenance worker runs). Surfaced in Settings → Danger Zone and
    # `relaydeck db wipe`, both gated behind a typed confirmation. Audited.

    @app.get("/api/maintenance/history")
    async def maintenance_history():
        """Per-scope row counts so the UI shows what a wipe deletes."""
        from relaydeck import maintenance
        return {
            "counts": maintenance.history_stats(orch.db_path),
            "labels": maintenance.scope_labels(),
        }

    @app.post("/api/maintenance/wipe")
    async def maintenance_wipe(body: dict[str, Any], request: Request):
        """Wipe the chosen scopes. Body: {scopes: ["messages","events",…]}.
        Validated against the fixed scope whitelist; audited."""
        from relaydeck import maintenance
        requested = list((body or {}).get("scopes") or [])
        scopes = [s for s in requested if s in maintenance.SCOPES]
        if not scopes:
            raise HTTPException(400, "no valid scopes (choose from "
                                + ", ".join(maintenance.SCOPES) + ")")
        deleted = maintenance.wipe(orch.db_path, scopes)
        audit.record(
            audit.actions.DATA_WIPE, target="maintenance",
            payload={"scopes": scopes, "deleted": deleted},
            identity=_audit_identity(request), source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        # Nudge every live view to refetch (the wiped data backs agents,
        # usage, automations, messages…).
        for ev in ("agent.updated", "usage.record", "workspace.updated"):
            _fire_workspace_event(ev, {"via": "maintenance.wipe"})
        return {"ok": True, "deleted": deleted}

    @app.get("/api/daemon/restart-info")
    async def daemon_restart_info():
        """What a restart will interrupt, so the UI can warn before
        acting. `managed` is False when the daemon isn't under `relaydeck
        daemon` supervision (foreground `relaydeck serve`, no PID file) — a
        web restart can't safely respawn it then, so the UI should tell
        the operator to restart from their terminal."""
        from relaydeck.daemon import read_pid
        running = [
            a for a in orch.list_agents()
            if a.get("status") in ("running", "starting")
        ]
        pid = read_pid(orch.config_home)
        return {
            "managed": pid is not None,
            "pid": pid,
            "running_agents": [a["id"] for a in running],
            "running_agent_count": len(running),
            "warning": (
                "Restarting interrupts ALL running agents, live terminals, "
                "and event streams. Agents with auto_start return "
                "automatically; others stay stopped until you start them."
            ),
        }

    @app.post("/api/daemon/restart")
    async def daemon_restart(request: Request):
        """Restart the daemon from the web. Spawns a DETACHED helper that
        — after this response flushes — gracefully stops the current
        daemon (SIGTERM, agents terminated cleanly) and starts a fresh
        one on the same bind host/port. The helper uses
        `start_new_session` so it survives the parent's death. Refuses
        when the daemon is unmanaged (no PID file), since we can't safely
        respawn a foreground `relaydeck serve`."""
        import os
        import subprocess
        import sys
        from urllib.parse import urlparse

        from relaydeck.daemon import log_file_path, read_pid
        from relaydeck.state import get_daemon_bind_host, get_daemon_url

        pid = read_pid(orch.config_home)
        if pid is None:
            raise HTTPException(
                409,
                "Daemon is not under `relaydeck daemon` supervision (no PID "
                "file) — restart it from your terminal instead.",
            )
        host = get_daemon_bind_host() or "127.0.0.1"
        port = 8765
        try:
            parsed = urlparse(get_daemon_url())
            if parsed.port:
                port = parsed.port
        except Exception:
            pass

        home_s = str(orch.config_home)
        # Detached helper: wait for our 200 to flush, then stop+start.
        helper = (
            "import time; from pathlib import Path; "
            "from relaydeck.daemon import stop_daemon, start_daemon; "
            "time.sleep(1.2); "
            f"stop_daemon(Path({home_s!r}), timeout=8.0); "
            f"start_daemon(Path({home_s!r}), host={host!r}, "
            f"port={int(port)}, wait_seconds=8.0)"
        )
        log_fd = os.open(
            str(log_file_path(orch.config_home)),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600,
        )
        try:
            subprocess.Popen(
                [sys.executable, "-c", helper],
                stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                start_new_session=True, close_fds=True,
            )
        finally:
            os.close(log_fd)

        audit.record(
            audit.actions.DAEMON_RESTART, target=f"pid:{pid}",
            payload={"host": host, "port": int(port)},
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {
            "status": "restarting", "host": host, "port": int(port),
            "note": (
                "Daemon is going down for a moment — reconnect in a few "
                "seconds."
            ),
        }

    # ── Version / self-update ────────────────────────────────────
    # Sync def so the (blocking, cached) GitHub fetch runs in FastAPI's
    # threadpool instead of stalling the event loop.
    @app.get("/api/version")
    def version_info(force: bool = False):
        """`{current, latest, update_available, repo, checked_at}`. Compares the
        installed version to the latest GitHub release tag (cached ~1h, fail-open:
        offline / no-releases → no update). Pass `force=true` to bypass cache."""
        from relaydeck import __version__ as cur
        from relaydeck.version_check import check_for_update
        try:
            return check_for_update(
                cur, cache_path=orch.config_home / "update-check.json", force=force,
            )
        except Exception:
            return {"current": cur, "latest": None, "update_available": False}

    @app.post("/api/update")
    async def self_update(request: Request):
        """Upgrade relaydeck in place (`uv tool upgrade`) then restart the
        daemon — the web counterpart of `relaydeck update`. Like
        /api/daemon/restart, this interrupts running agents/terminals; the UI
        confirms first. Refuses when the daemon is unmanaged (no PID file)."""
        import os
        import shlex
        import subprocess
        import sys
        from urllib.parse import urlparse

        from relaydeck.daemon import log_file_path, read_pid
        from relaydeck.state import get_daemon_bind_host, get_daemon_url

        pid = read_pid(orch.config_home)
        if pid is None:
            raise HTTPException(
                409,
                "Daemon is not under `relaydeck daemon` supervision — update "
                "from your terminal with `relaydeck update`.",
            )
        host = get_daemon_bind_host() or "127.0.0.1"
        port = 8765
        try:
            parsed = urlparse(get_daemon_url())
            if parsed.port:
                port = parsed.port
        except Exception:
            pass

        home_s = str(orch.config_home)
        upgrade_cmd = os.environ.get("RELAYDECK_UPDATE_CMD", "uv tool upgrade relaydeck")
        # Detached helper: wait for our 200 to flush, run the upgrade, then
        # stop+start so the new code is loaded.
        helper = (
            "import time, subprocess; from pathlib import Path; "
            "from relaydeck.daemon import stop_daemon, start_daemon; "
            "time.sleep(1.2); "
            f"subprocess.run({shlex.split(upgrade_cmd)!r}, check=False); "
            f"stop_daemon(Path({home_s!r}), timeout=8.0); "
            f"start_daemon(Path({home_s!r}), host={host!r}, "
            f"port={int(port)}, wait_seconds=12.0)"
        )
        log_fd = os.open(
            str(log_file_path(orch.config_home)),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600,
        )
        try:
            subprocess.Popen(
                [sys.executable, "-c", helper],
                stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                start_new_session=True, close_fds=True,
            )
        finally:
            os.close(log_fd)

        audit.record(
            audit.actions.DAEMON_RESTART, target=f"pid:{pid}",
            payload={"host": host, "port": int(port), "via": "update"},
            identity=_audit_identity(request),
            source_ip=_audit_source_ip(request),
            db_path=orch.db_path,
        )
        return {
            "status": "updating",
            "note": "Upgrading then restarting — reconnect in a few seconds.",
        }

    # ── Usage / metering ─────────────────────────────────────────

    @app.get("/api/usage")
    async def get_usage(agent_id: str | None = None, workspace: str | None = None):
        from relaydeck.db import get_usage_summary, open_db

        conn = open_db(orch.db_path)
        try:
            return get_usage_summary(conn, agent_id=agent_id, workspace=workspace)
        finally:
            conn.close()

    # ── Plugin introspection ───────────────────────────────────

    @app.get("/api/plugins")
    async def list_plugins():
        """All discovered plugins with metadata + settings count.
        Includes disabled-but-discovered plugins so the dashboard can
        list them and offer a one-click re-enable."""
        from relaydeck.plugin import get_registry
        from relaydeck.plugin_disabled import disabled_set
        from relaydeck.plugin_lock import load_lock
        from relaydeck.plugin_settings import normalize_schema
        registry = get_registry()
        disabled = disabled_set()
        locked = load_lock(orch.config_home)
        loaded_names = {e.name for e in registry.all()}
        out = []
        for e in registry.discovered_all():
            try:
                schema = normalize_schema(e.instance.get_settings_schema())
            except Exception:
                schema = []
            lock_entry = locked.get(e.name)
            out.append({
                "name": e.name, "category": e.category,
                "version": e.version, "source": e.source,
                "description": getattr(e.instance, "description", ""),
                "has_settings": bool(schema),
                "settings_count": len(schema),
                "enabled": e.name in loaded_names,
                "disabled_flag": e.name in disabled,
                # Whether the plugin opts into per-workspace enablement
                # (appears in agent.toml `plugins=[]`). Global plugins are
                # daemon-wide — the header popover uses this to show the
                # right "selected" state (workspace membership vs. global
                # enabled) instead of leaving global plugins unchecked.
                "workspace_scoped": bool(getattr(e.instance, "workspace_scoped", False)),
                "installed_via": lock_entry.installed_via if lock_entry else "",
                "locked_source": lock_entry.source if lock_entry else "",
                "user_installed": bool(lock_entry and lock_entry.scope == "user"),
            })
        return out

    @app.post("/api/plugins/install")
    async def install_plugin(body: dict[str, Any]):
        """Install a plugin into the daemon environment.

        Package and wheel installs mutate the Python environment that owns the
        daemon, so the newly approved plugin is available after restart.
        """
        from relaydeck.plugin_install import PluginInstallError, install_plugin_source

        source = str((body or {}).get("source") or "").strip()
        editable = bool((body or {}).get("editable", False))
        if not source:
            raise HTTPException(400, "source required")
        try:
            result = await asyncio.to_thread(
                install_plugin_source,
                source,
                orch.config_home,
                editable=editable,
            )
        except PluginInstallError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "plugins": result.names,
            "installed_via": result.installed_via,
            "source": result.source,
            "dest": result.dest,
            "restart_required": result.restart_required,
            "message": "Plugin installed. Restart the daemon to load it.",
        }

    @app.post("/api/plugins/{name}/disable")
    async def disable_plugin(name: str):
        """Persist the disabled flag and best-effort unload the running
        plugin. Returns whether the live unload succeeded — if a plugin
        contributes CLI/API surfaces those linger until daemon restart,
        and the UI shows that nudge."""
        from relaydeck.plugin import get_registry
        registry = get_registry()
        # 404 only when the name was never discovered. Already-disabled
        # but discovered plugins should still hit this endpoint cleanly.
        if not any(e.name == name for e in registry.discovered_all()):
            raise HTTPException(404, f"Plugin {name} not found")
        live, msg = registry.disable(name)
        return {"plugin": name, "enabled": False, "live": live, "message": msg}

    @app.post("/api/plugins/{name}/enable")
    async def enable_plugin(name: str):
        """Persist the enable flag and best-effort re-load if the
        plugin was discovered this process. If discovery didn't see it
        (newly added on disk after start), restart is required."""
        from relaydeck.plugin import get_registry
        registry = get_registry()
        if not any(e.name == name for e in registry.discovered_all()):
            raise HTTPException(404, f"Plugin {name} not found")
        live, msg = registry.enable(name)
        return {"plugin": name, "enabled": True, "live": live, "message": msg}

    @app.delete("/api/plugins/{name}")
    async def uninstall_plugin(name: str):
        """Remove a user-installed plugin and its lock entry.

        Running plugin code may remain in memory until daemon restart.
        """
        from relaydeck.plugin_install import uninstall_plugin as uninstall_plugin_from_disk

        removed, package = await asyncio.to_thread(
            uninstall_plugin_from_disk,
            name,
            orch.config_home,
        )
        if not removed:
            raise HTTPException(404, f"Plugin {name} not found in user installs")
        return {
            "plugin": name,
            "uninstalled": True,
            "package": package,
            "restart_required": True,
            "message": "Plugin uninstalled. Restart the daemon to fully unload it.",
        }

    @app.get("/api/plugins/{name}/settings")
    async def get_plugin_settings(name: str):
        """Return the plugin's schema + current values + source per key.

        `source` tells the UI whether each value came from an env var
        override, the on-disk yaml, or the schema default. The form
        renders an annotation like "· from env RELAYDECK_EMOTE_PRESET" so
        an operator can tell at a glance.
        """
        from relaydeck.plugin import get_registry
        from relaydeck.plugin_settings import normalize_schema, get_setting, value_source
        registry = get_registry()
        entry = next((e for e in registry.all() if e.name == name), None)
        if entry is None:
            raise HTTPException(404, f"Plugin {name} not found")
        schema = normalize_schema(entry.instance.get_settings_schema())
        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for field in schema:
            k = field["key"]
            values[k] = get_setting(name, k, field.get("default"))
            sources[k] = value_source(name, k)
        return {
            "plugin": name,
            "schema": schema,
            "values": values,
            "sources": sources,
        }

    @app.post("/api/plugins/{name}/settings")
    async def set_plugin_settings(name: str, body: dict[str, Any]):
        """Replace this plugin's stored settings. Body keys not in the
        schema are dropped silently. Workers read live via get_setting()
        so the change is visible on the next tick — no restart needed."""
        from relaydeck.plugin import get_registry
        from relaydeck.plugin_settings import normalize_schema, set_settings, validate_values
        registry = get_registry()
        entry = next((e for e in registry.all() if e.name == name), None)
        if entry is None:
            raise HTTPException(404, f"Plugin {name} not found")
        schema = normalize_schema(entry.instance.get_settings_schema())
        if not schema:
            raise HTTPException(400, f"Plugin {name} has no settings schema")
        validated = validate_values(schema, body or {})
        set_settings(name, validated)
        # Let the plugin react synchronously to its new settings (e.g.
        # re-materialize derived files, refresh caches). Default no-op.
        try:
            entry.instance.on_settings_changed(validated)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Plugin %s.on_settings_changed raised: %s", name, exc,
            )
        return {"plugin": name, "values": validated, "status": "saved"}

    # ── Workers ──────────────────────────────────────────────────

    @app.get("/api/workers")
    async def list_workers(plugin: str | None = None, agent: str | None = None):
        """Background workers registered by plugins (file watchers, log
        tailers, gateway listeners, …). Filter optionally by plugin or
        agent_id when the worker is agent-scoped."""
        from relaydeck.workers import get_worker_registry
        out = []
        for w in get_worker_registry().all():
            if plugin and w.plugin != plugin:
                continue
            if agent and w.agent_id != agent:
                continue
            out.append(w.snapshot())
        return out

    @app.get("/api/workers/{worker_id}")
    async def get_worker(worker_id: str):
        from relaydeck.workers import get_worker_registry
        w = get_worker_registry().get(worker_id)
        if w is None:
            raise HTTPException(404, f"Worker {worker_id} not found")
        return w.snapshot()

    @app.get("/api/workers/{worker_id}/logs")
    async def worker_logs(worker_id: str, tail: int = 200):
        from relaydeck.workers import get_worker_registry
        w = get_worker_registry().get(worker_id)
        if w is None:
            raise HTTPException(404, f"Worker {worker_id} not found")
        return w.recent_logs(tail=tail)

    @app.post("/api/workers/{worker_id}/retry")
    async def worker_retry(worker_id: str):
        """Re-arm a worker in crash_loop / errored state. Resets the
        restart-window counter and starts a fresh supervisor thread.
        Operator is responsible for having fixed the underlying issue
        before calling — the supervisor will hit the crash-loop guard
        again if the worker keeps failing."""
        from relaydeck.workers import retry_worker, get_worker_registry
        w = get_worker_registry().get(worker_id)
        if w is None:
            raise HTTPException(404, f"Worker {worker_id} not found")
        if not retry_worker(worker_id):
            raise HTTPException(
                409,
                f"Worker {worker_id} is not in a retry-able state "
                f"(current status: {w.status})",
            )
        return {"status": "retrying", "id": worker_id}

    @app.post("/api/workers/{worker_id}/stop")
    async def worker_stop(worker_id: str):
        """Stop a running worker. Idempotent — stopping an already
        stopped worker is a no-op success. Backed by Worker.stop()
        which signals the supervisor thread to exit cleanly."""
        from relaydeck.workers import get_worker_registry
        w = get_worker_registry().get(worker_id)
        if w is None:
            raise HTTPException(404, f"Worker {worker_id} not found")
        try:
            w.stop()
        except Exception as exc:
            raise HTTPException(500, f"stop failed: {exc}") from exc
        return {"status": "stopping", "id": worker_id}

    @app.post("/api/workers/{worker_id}/restart")
    async def worker_restart(worker_id: str):
        """Stop + retry. Used by the dashboard's Restart button.

        `retry_worker` only accepts workers in non-running states, so
        we wait for the stop event to actually transition the worker
        out of RUNNING before re-arming. Bounded wait so a stuck
        worker doesn't hang the request."""
        from relaydeck.workers import WorkerStatus, get_worker_registry, retry_worker
        w = get_worker_registry().get(worker_id)
        if w is None:
            raise HTTPException(404, f"Worker {worker_id} not found")
        try:
            w.stop()
        except Exception:
            pass
        # Wait up to ~3s for the supervisor thread to observe the stop
        # event and transition. Avoids the immediate-retry race that
        # would otherwise 409 a healthy stop+restart sequence.
        import time as _time
        deadline = _time.monotonic() + 3.0
        while _time.monotonic() < deadline:
            if w.status not in (WorkerStatus.RUNNING, WorkerStatus.IDLE):
                break
            _time.sleep(0.05)
        if not retry_worker(worker_id):
            raise HTTPException(409, f"could not restart (status: {w.status})")
        return {"status": "restarting", "id": worker_id}

    @app.get("/api/plugins/ui")
    async def plugin_ui_manifest():
        """Aggregated UI contributions from all CURRENTLY-LOADED plugins.

        The frozen manifest on `app.state.ui_manifest` is captured at
        daemon startup. Filtering by `get_registry().all()` here means
        a plugin disabled at runtime (Plugins tab → Disable globally,
        or `relaydeck plugin disable <name>`) has its tab/chip/tile drop
        out of the manifest immediately — the dashboard re-fetches
        this endpoint after a toggle and removes the corresponding
        DOM nodes.

        Each tab/chip/tile carries a `module` URL the dashboard
        lazy-imports.
        """
        from relaydeck.plugin import get_registry
        raw = getattr(app.state, "ui_manifest",
                      {"tabs": [], "header_chips": [], "agent_tiles": [], "widgets": []})
        loaded = {e.name for e in get_registry().all()}

        def keep(item: dict) -> bool:
            # core contributions (no `plugin` field) always pass through
            return not item.get("plugin") or item.get("plugin") in loaded

        return {
            "tabs": [t for t in raw.get("tabs", []) if keep(t)],
            "header_chips": [c for c in raw.get("header_chips", []) if keep(c)],
            "agent_tiles": [t for t in raw.get("agent_tiles", []) if keep(t)],
            # Home-dashboard widgets a plugin contributes via
            # `[plugin.ui] widgets`. Filtered like everything else so a
            # disabled plugin's widget drops out of the gallery.
            "widgets": [w for w in raw.get("widgets", []) if keep(w)],
        }

    # ── Activity feed ──────────────────────────────────────────

    @app.get("/api/activity")
    async def get_activity(since: int = 0, limit: int = 100):
        """Return recent events across all agents for the activity feed.

        Query params:
          since: event id to start after (0 = most recent)
          limit: max events to return (default 100)
        """
        from relaydeck.db import open_db
        conn = open_db(orch.db_path)
        try:
            rows = conn.execute(
                "SELECT id, type, payload, ts, agent_id FROM events "
                "WHERE id > ? ORDER BY id DESC LIMIT ?",
                (since, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
        finally:
            conn.close()

    # ── Plugin routes ────────────────────────────────────────────
    # Plugins register additional routes via register_api_routes()
    # which is called after app creation in serve().

    return app
