"""
Context-watch — emit `agent.context` window-fill so a manager can act early.

relaydeck already *computes* an agent's context fill on demand (the Context
tab: latest `prompt_tokens` vs the model's context window from models.dev).
But that's pull-only — nothing PUSHES a signal when an agent is about to run
out of room. A manager (human or orchestrating agent) wants to know *before*
the harness silently auto-compacts: auto-compaction rewrites the prefix, which
blows the KV cache (slow + costly next turn) and can drop earlier instructions.

This plugin closes that gap with zero harness changes. It feeds off the same
`usage.record` events every metered harness emits; on each one it computes the
fill percent and, when the agent crosses `warn` / `critical` (or recovers after
a compaction/fresh session), emits `agent.context` on the live stream. The
event carries a `recommend` so a manager policy (or a human watching the
dashboard / `view` Context tab) knows the move: compact, or start fresh.

Pure-core: `classify_fill` is a free function so the thresholds are unit-tested
without a daemon, and `relaydeck context status` shows the live picture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from relaydeck.sdk import (
    Event,
    Plugin,
    PluginContext,
    PluginEventBus,
    PluginHost,
)

logger = logging.getLogger(__name__)

PLUGIN_NAME = "context-watch"

_DEFAULTS: dict[str, Any] = {
    "warn_pct": 70.0,
    "critical_pct": 88.0,
    "default_context_window": 0,
}


@dataclass(frozen=True)
class Fill:
    """A point-in-time context-window reading for one agent."""

    used: int
    window: int
    pct: float
    state: str  # ok | warn | critical


def classify_fill(used: int, window: int, *, warn_pct: float,
                  critical_pct: float) -> Fill | None:
    """Pure: classify context fill, or None when the window is unknown (we
    never guess a limit). `used` is the latest turn's prompt tokens — the
    prompt sent each turn IS the live context, so it's a faithful proxy."""
    if not window or window <= 0:
        return None
    pct = round(100.0 * max(0, used) / window, 1)
    if pct >= critical_pct:
        state = "critical"
    elif pct >= warn_pct:
        state = "warn"
    else:
        state = "ok"
    return Fill(used=max(0, used), window=window, pct=pct, state=state)


def _recommend(state: str) -> str:
    if state == "critical":
        return ("compact now or start a fresh session — the harness is about to "
                "auto-compact (KV-cache loss + possible instruction drop)")
    if state == "warn":
        return "plan a compaction / handover soon; context is filling"
    return "ok"


class ContextWatchPlugin(Plugin):
    """Turn per-turn usage into an `agent.context` fullness signal."""

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        # agent_id → {"state","pct","used","window","model","provider"}
        self._latest: dict[str, dict[str, Any]] = {}
        host.events.subscribe("usage.record", self._on_usage)
        self._register_cli(host)
        self._register_api(host)

    # ── Settings ─────────────────────────────────────────────────────

    def _settings(self) -> dict[str, Any]:
        vals = dict(_DEFAULTS)
        try:
            vals.update(self.host.settings.all())
        except Exception:
            pass
        return {
            "warn_pct": float(vals.get("warn_pct") or 0) or 70.0,
            "critical_pct": float(vals.get("critical_pct") or 0) or 88.0,
            "default_context_window": int(vals.get("default_context_window") or 0),
        }

    # ── Context window lookup ────────────────────────────────────────

    def _context_window(self, provider: str, model: str, fallback: int) -> int:
        """The model's context-window size (tokens) from models.dev's cached
        catalogue; `fallback` (settings) for uncatalogued models, else 0."""
        try:
            from relaydeck import models_dev
            ch = getattr(self.host, "config_home", None)
            meta = models_dev.get_model_meta(provider, model, ch, cache_only=True)
            if meta:
                lim = meta.get("limit") or {}
                ctx = lim.get("context")
                if ctx:
                    return int(ctx)
        except Exception:
            pass
        return int(fallback or 0)

    # ── Event handler ────────────────────────────────────────────────

    def _on_usage(self, event: Event) -> None:
        data = event.data or {}
        agent_id = str(data.get("agent_id") or "")
        if not agent_id or agent_id == "unknown":
            return
        used = int(data.get("prompt", data.get("prompt_tokens", 0)) or 0)
        model = str(data.get("model") or "")
        provider = str(data.get("provider") or "")

        s = self._settings()
        window = self._context_window(provider, model, s["default_context_window"])
        fill = classify_fill(used, window, warn_pct=s["warn_pct"],
                             critical_pct=s["critical_pct"])
        if fill is None:
            return  # uncatalogued model, no fallback — don't guess

        prev = self._latest.get(agent_id)
        self._latest[agent_id] = {
            "state": fill.state, "pct": fill.pct, "used": fill.used,
            "window": fill.window, "model": model, "provider": provider,
        }
        # Emit on a state change only — escalation (ok→warn→critical) AND
        # recovery (critical→ok after a compaction / fresh session), so a
        # manager sees both "act now" and "cleared". The effective baseline is
        # "ok", so a fresh agent sitting at ok stays silent (no noise).
        prev_state = prev["state"] if prev else "ok"
        if prev_state == fill.state:
            return
        self._emit("agent.context", {
            "agent_id": agent_id,
            "workspace": self._agent_workspace(agent_id),
            "model": model,
            "provider": provider,
            "used_tokens": fill.used,
            "context_window": fill.window,
            "pct": fill.pct,
            "state": fill.state,
            "recommend": _recommend(fill.state),
        })
        logger.info("context-watch: %s %s %.1f%% (%d/%d) — %s",
                    agent_id, fill.state, fill.pct, fill.used, fill.window,
                    _recommend(fill.state))

    def _agent_workspace(self, agent_id: str) -> str | None:
        agents = getattr(self.host, "agents", None)
        if agents is None:
            return None
        try:
            info = agents.get(agent_id)
            return getattr(info, "workspace", None) if info else None
        except Exception:
            return None

    def _emit(self, event_type: str, payload: dict) -> None:
        try:
            self.host.events.emit(event_type, payload)
        except Exception as exc:
            logger.debug("context-watch: emit %s failed: %s", event_type, exc)

    # ── API ──────────────────────────────────────────────────────────

    def _state_rows(self) -> list[dict[str, Any]]:
        rows = []
        for aid, st in sorted(self._latest.items()):
            rows.append({"agent_id": aid, **st})
        return rows

    def _tui_lines(self) -> list[str]:
        s = self._settings()
        lines = [
            f"warn ≥ {s['warn_pct']:g}%    critical ≥ {s['critical_pct']:g}%    "
            f"tracking {len(self._latest)} agent(s)",
            "",
            "context fill (latest turn's prompt tokens vs model window):",
        ]
        if not self._latest:
            lines.append("  (no usage yet — metered harnesses feed this)")
            return lines
        glyph = {"ok": "·", "warn": "▲", "critical": "■"}
        for aid, st in sorted(self._latest.items()):
            lines.append(
                f"  {glyph.get(st['state'], '·')} {aid}  {st['pct']:.1f}%  "
                f"({st['used']}/{st['window']})  {st['state']}  {st.get('model', '')}"
            )
        return lines

    def _register_api(self, host: PluginHost) -> None:
        @host.api.route("/state", ["GET"])
        async def state():
            return {"agents": self._state_rows(), **self._settings()}

        @host.api.route("/tui", ["GET"])
        async def tui():
            return {"title": "Context", "lines": self._tui_lines()}

    # ── CLI ──────────────────────────────────────────────────────────

    def _register_cli(self, host: PluginHost) -> None:
        from rich.console import Console
        from rich.table import Table

        console = Console()

        @host.cli.command("status")
        def status_cmd():
            """Show each agent's context-window fill + warn/critical state."""
            ok, data = _call_daemon("/api/plugins/context-watch/state", method="GET")
            if not ok:
                console.print(f"[red]context-watch status failed:[/] {data}")
                return
            rows = data.get("agents") or []
            if not rows:
                console.print("[dim]no context readings yet "
                              "(metered harnesses feed this).[/]")
                return
            table = Table(title="context fill")
            table.add_column("agent"); table.add_column("fill")
            table.add_column("used/window"); table.add_column("state")
            table.add_column("model")
            color = {"ok": "green", "warn": "yellow", "critical": "red"}
            for r in rows:
                st = r.get("state", "ok")
                table.add_row(
                    r.get("agent_id", ""),
                    f"{r.get('pct', 0):.1f}%",
                    f"{r.get('used', 0)}/{r.get('window', 0)}",
                    f"[{color.get(st, '')}]{st}[/]",
                    r.get("model", ""),
                )
            console.print(table)


def _call_daemon(path: str, *, payload: dict | None = None,
                 method: str = "POST") -> tuple[bool, Any]:
    """CLI → local daemon over HTTP with the operator's bearer token."""
    import json
    import urllib.error
    import urllib.request

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


PLUGIN = ContextWatchPlugin()


def _legacy_on_load(ctx: PluginContext) -> "ContextWatchPlugin":
    """Boot against a PluginContext (tests + legacy loader); mirrors autopilot."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "events.subscribe", "events.emit",
            "api.register", "cli.register", "agents.list",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
        settings_defaults=_DEFAULTS,
    )
    PLUGIN.on_load(host)
    return PLUGIN
