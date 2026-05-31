"""
Human-in-the-loop (HITL) escalation.

A relaydeck agent runs UNATTENDED — there's no human at its harness TUI to
answer "can I delete prod?" or to notice it errored out. This plugin closes
that loop: when an agent needs a human, it reaches the operator through
pluggable notification "homes," and (optionally) stops the agent until a
human responds.

Three triggers:
  1. `agent.status_changed → awaiting-input` — a vendor hook (Claude Code
     today) reported the agent is paused for approval.
  2. `agent.error` — the agent process errored.
  3. `relaydeck hitl ask "<question>"` — the UNIVERSAL, harness-agnostic path:
     any agent (via the always-allowed relaydeck CLI) can explicitly request
     a human, even harnesses with no vendor hook. It marks the agent
     awaiting-input and escalates.

Pluggable homes (the "where does it reach me" question) are decoupled via the
`hitl.escalation` bus event rather than a hard-coded channel list: a channel
plugin SUBSCRIBES to `hitl.escalation` and delivers to its own destination —
telegram → an admin DM, a future email plugin → an inbox. The dashboard bell
is always-on (the event is bridged onto the SSE feed). Adding a new home is
"write a plugin that subscribes," no change here.

Setting `on_escalation = notify_and_stop` also stops the agent process after
notifying, so a risky agent doesn't keep running while it waits for a human.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from relaydeck.sdk import (
    Event,
    Plugin,
    PluginContext,
    PluginEventBus,
    PluginHost,
)

logger = logging.getLogger(__name__)

PLUGIN_NAME = "hitl"

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "escalate_on": "both",       # both | awaiting-input | errors
    "on_escalation": "notify",   # notify | notify_and_stop
    "cooldown_seconds": 300.0,
}

_TRUTHY = ("1", "true", "yes", "on")


class HitlPlugin(Plugin):
    """Detect HITL triggers, emit `hitl.escalation`, optionally stop the agent."""

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        # (agent_id, kind) → last escalation ts, for cooldown suppression.
        self._last_escalation: dict[tuple[str, str], float] = {}

        host.events.subscribe("agent.status_changed", self._on_status_changed)
        host.events.subscribe("agent.error", self._on_error)
        self._register_cli(host)
        self._register_api(host)

    # ── Settings ────────────────────────────────────────────────────

    def _settings(self) -> dict[str, Any]:
        """Live settings, coerced — re-read each event so a change via
        the dashboard / `relaydeck plugin settings hitl set …` takes effect
        without a daemon reload."""
        vals = dict(_DEFAULTS)
        try:
            vals.update(self.host.settings.all())
        except Exception:
            pass
        return {
            "enabled": str(vals.get("enabled", True)).lower() in (*_TRUTHY, "true"),
            "escalate_on": str(vals.get("escalate_on") or "both").lower(),
            "on_escalation": str(vals.get("on_escalation") or "notify").lower(),
            "cooldown_seconds": float(vals.get("cooldown_seconds") or 0),
        }

    # ── Event handlers ──────────────────────────────────────────────

    def _on_status_changed(self, event: Event) -> None:
        data = event.data or {}
        # Ignore status changes WE caused. escalate(set_status=True) flips the
        # agent to awaiting-input via set_semantic_status(source="hitl"), which
        # the real orchestrator emits `agent.status_changed` for SYNCHRONOUSLY.
        # Without this guard a manual `relaydeck hitl ask` would re-enter here
        # and fire a second, generic "awaiting-input" escalation on top of the
        # intended one — the operator gets two notifications for one ask.
        if data.get("source") == "hitl":
            return
        # orchestrator emits {agent_id, from, to, source}; `to` is the new
        # semantic status. Fall back to other shapes defensively.
        status = data.get("to") or data.get("semantic_status") or data.get("status")
        if status != "awaiting-input":
            return
        s = self._settings()
        if s["escalate_on"] not in ("both", "awaiting-input"):
            return
        agent_id = str(data.get("agent_id") or data.get("id") or "")
        if agent_id:
            self.escalate(
                agent_id, kind="awaiting-input",
                message="Agent is paused waiting for human approval.",
                set_status=False,  # already awaiting-input
            )

    def _on_error(self, event: Event) -> None:
        data = event.data or {}
        s = self._settings()
        if s["escalate_on"] not in ("both", "errors"):
            return
        agent_id = str(data.get("id") or data.get("agent_id") or "")
        if agent_id:
            err = str(data.get("error") or data.get("last_error") or "agent error")
            self.escalate(agent_id, kind="error", message=err, set_status=False)

    # ── Core ────────────────────────────────────────────────────────

    def escalate(
        self,
        agent_id: str,
        *,
        kind: str,
        message: str = "",
        set_status: bool = True,
    ) -> dict[str, Any]:
        """Raise a HITL escalation for `agent_id`.

        Emits `hitl.escalation` (the channel hook — subscribers deliver it),
        optionally flips the agent to awaiting-input, and optionally stops the
        agent. Returns a result dict (suppressed/emitted/stopped) for the API.
        """
        s = self._settings()
        if not s["enabled"]:
            return {"escalated": False, "reason": "disabled"}

        now = time.time()
        cooldown = s["cooldown_seconds"]
        key = (agent_id, kind)
        last = self._last_escalation.get(key, 0.0)
        if cooldown > 0 and (now - last) < cooldown:
            return {"escalated": False, "reason": "cooldown"}
        self._last_escalation[key] = now

        workspace = self._agent_workspace(agent_id)
        if set_status:
            self._set_awaiting_input(agent_id)

        # Resolve `stopped` BEFORE emitting. Subscribers (SSE bridge, telegram)
        # run synchronously during emit(), and the bus persists the payload to
        # `bus_events` at emit() time — so a payload mutated after emit() never
        # reaches channels or durable history. notify_and_stop must therefore
        # stop the agent first and publish the final flag.
        stopped = False
        if s["on_escalation"] == "notify_and_stop":
            stopped = self._stop_agent(agent_id)

        payload = {
            "agent_id": agent_id,
            "workspace": workspace,
            "kind": kind,
            "message": message,
            "ts": now,
            "stopped": stopped,
            # How a human responds — handy for channels to render verbatim.
            "respond_hint": (
                f"relaydeck workspace message \"<reply>\" --agent {agent_id}"
            ),
        }
        try:
            self.host.events.emit("hitl.escalation", payload)
        except Exception as exc:
            logger.warning("hitl: failed to emit escalation for %s: %s", agent_id, exc)

        logger.info("hitl: escalated %s (%s) stop=%s", agent_id, kind, stopped)
        return {"escalated": True, **payload}

    def _stop_agent(self, agent_id: str) -> bool:
        agents = self.host.agents
        if agents is None:
            return False
        try:
            return bool(agents.stop(agent_id))
        except Exception as exc:
            logger.warning("hitl: stop(%s) raised: %s", agent_id, exc)
            return False

    def _set_awaiting_input(self, agent_id: str) -> None:
        try:
            orch = self.host._orchestrator  # type: ignore[attr-defined]
        except AttributeError:
            return
        setter = getattr(orch, "set_semantic_status", None)
        if not callable(setter):
            return
        try:
            setter(agent_id, "awaiting-input", source="hitl")
        except Exception as exc:
            logger.debug("hitl: set_semantic_status(%s) failed: %s", agent_id, exc)

    def _agent_workspace(self, agent_id: str) -> str | None:
        agents = self.host.agents
        if agents is None:
            return None
        try:
            info = agents.get(agent_id)
            return getattr(info, "workspace", None) if info else None
        except Exception:
            return None

    def _available_channels(self) -> list[str]:
        """Notification homes for the status view: the always-on web bell, plus
        any loaded channel plugin that is ACTUALLY wired to deliver. We report
        `telegram` only when it's loaded AND has an admin chat id configured —
        its `hitl.escalation` handler is a no-op without one, so listing it
        unconditionally gave false confidence ("channels: web, telegram" with
        nothing actually reachable)."""
        channels = ["web"]
        try:
            from relaydeck.plugin import get_registry
            loaded = {e.name for e in get_registry().all()}
        except Exception:
            loaded = set()
        if "telegram" in loaded and self._telegram_hitl_configured():
            channels.append("telegram")
        return channels

    @staticmethod
    def _telegram_hitl_configured() -> bool:
        """True when telegram has `hitl_admin_chat_id` set (env or yaml) — the
        exact condition under which its escalation handler delivers a DM. Reads
        the channel's own setting so the status reflects reality, not just
        "the plugin is loaded"."""
        try:
            from relaydeck import plugin_settings
            chat = plugin_settings.get_setting("telegram", "hitl_admin_chat_id", "")
            return bool(str(chat or "").strip())
        except Exception:
            return False

    # ── API (CLI → daemon over HTTP) ────────────────────────────────

    def _register_api(self, host: PluginHost) -> None:
        @host.api.route("/status", ["GET"])
        async def status():
            s = self._settings()
            return {**s, "channels": self._available_channels()}

        @host.api.route("/escalate", ["POST"])
        async def escalate_route(body: dict | None = None):
            body = body or {}
            agent_id = str(body.get("agent_id") or "").strip()
            if not agent_id:
                return {"escalated": False, "reason": "agent_id required"}
            return self.escalate(
                agent_id, kind=str(body.get("kind") or "manual"),
                message=str(body.get("message") or "Agent requested human input."),
                set_status=True,
            )

    # ── CLI ─────────────────────────────────────────────────────────

    def _register_cli(self, host: PluginHost) -> None:
        import click
        from rich.console import Console

        console = Console()

        @host.cli.command("status")
        def status_cmd():
            """Show HITL config + available notification channels."""
            ok, data = _call_daemon("/api/plugins/hitl/status", method="GET")
            if not ok:
                console.print(f"[red]hitl status failed:[/] {data}")
                return
            console.print(
                f"enabled={data.get('enabled')}  "
                f"escalate_on={data.get('escalate_on')}  "
                f"on_escalation={data.get('on_escalation')}  "
                f"cooldown={data.get('cooldown_seconds')}s"
            )
            console.print("channels: " + ", ".join(data.get("channels") or ["web"]))

        @host.cli.command("ask")
        @click.argument("message", nargs=-1, required=True)
        @click.option("--agent", "-a", "agent_id",
                      help="Agent id (default: $RELAYDECK_AGENT_ID — set inside a harness).")
        def ask_cmd(message: tuple[str, ...], agent_id: str | None):
            """Request a human. Marks this agent awaiting-input and escalates
            to the operator. Run by an agent that hits a decision it shouldn't
            make alone — works for every harness via the relaydeck CLI."""
            aid = agent_id or os.environ.get("RELAYDECK_AGENT_ID")
            if not aid:
                console.print("[red]no agent id[/] (pass --agent or set RELAYDECK_AGENT_ID)")
                raise SystemExit(1)
            body = " ".join(message).strip()
            ok, data = _call_daemon(
                "/api/plugins/hitl/escalate",
                payload={"agent_id": aid, "message": body, "kind": "manual"},
            )
            if not ok:
                console.print(f"[red]escalation failed:[/] {data}")
                raise SystemExit(1)
            if data.get("escalated"):
                console.print(f"[green]escalated[/] — operator notified for {aid}")
            else:
                console.print(f"[yellow]not escalated[/] ({data.get('reason')})")

        @host.cli.command("test")
        @click.option("--agent", "-a", "agent_id", required=True,
                      help="Agent id to fire a test escalation for.")
        def test_cmd(agent_id: str):
            """Fire a test escalation to verify notification channels work."""
            ok, data = _call_daemon(
                "/api/plugins/hitl/escalate",
                payload={"agent_id": agent_id,
                         "message": "HITL test escalation.", "kind": "test"},
            )
            console.print(data if ok else f"[red]{data}[/]")


def _call_daemon(path: str, *, payload: dict | None = None,
                 method: str = "POST") -> tuple[bool, Any]:
    """POST/GET to the local daemon with the operator's bearer token —
    the CLI↔daemon HTTP path (live escalation state lives in the daemon)."""
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


PLUGIN = HitlPlugin()


def _legacy_on_load(ctx: PluginContext) -> "HitlPlugin":
    """Boot the plugin against a PluginContext (tests + the legacy loader
    path). Returns the instance it operated on — see the matching note in
    the usage-limits plugin re: the spec_from_file_location aliasing trap."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "events.subscribe",
            "events.emit",
            "api.register",
            "cli.register",
            "agents.list",
            "agents.stop",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
        settings_defaults=_DEFAULTS,
    )
    PLUGIN.on_load(host)
    return PLUGIN
