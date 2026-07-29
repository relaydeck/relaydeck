"""
Manager — turn fleet-health signals into auditable actions.

autopilot, hitl, usage-limits, and context-watch each SURFACE a condition (a
stuck prompt, a usage cap, a full context). The manager decides what to DO
about it, fleet-wide, from one declarative policy — and records every decision
as a `manager.action` event so there's an audit trail of why an agent got
reset / paused / flagged.

Safe by default: every policy starts at `recommend`, which only emits advice
(executed=false). An operator opts into real actions (`fresh-session`,
`pause`) per trigger. That keeps the manager composable — it never fights
usage-limits' own auto-pause or hitl's escalation unless you tell it to.

Triggers (events it consumes):
  - `agent.context`         (state=critical)  → context-watch
  - `usage_limits.exceeded`                   → usage-limits
  - `agent.message_failed`                    → core messaging

`decide()` is a pure function (policy + trigger → action), unit-tested without
a daemon.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from contextlib import suppress
from typing import Any

from relaydeck.sdk import (
    Event,
    Plugin,
    PluginContext,
    PluginEventBus,
    PluginHost,
)

logger = logging.getLogger(__name__)

PLUGIN_NAME = "manager"

_DEFAULTS: dict[str, Any] = {
    "on_context_critical": "recommend",
    "on_usage_exceeded": "recommend",
    "on_message_failed": "recommend",
}

# Which policy setting governs each trigger. All three ride the PLUGIN bus
# (context-watch, usage-limits, and core messaging emit there); the manager
# subscribes there too, so the wiring stays on one bus and each decision also
# bridges to the SSE feed.
_TRIGGERS = {
    "agent.context": "on_context_critical",
    "usage_limits.exceeded": "on_usage_exceeded",
    "agent.message_failed": "on_message_failed",
}


def decide(trigger: str, policy: dict[str, str], *, context_state: str | None = None) -> str:
    """Pure: the action for a trigger under the current policy. Returns one of
    off | recommend | fresh-session | pause. `agent.context` only acts on the
    critical state — a warn is informational, not actionable."""
    setting = _TRIGGERS.get(trigger)
    if setting is None:
        return "off"
    if trigger == "agent.context" and context_state != "critical":
        return "off"
    return policy.get(setting, "recommend")


def _advice(trigger: str, action: str) -> str:
    if action == "compact":
        return "compacted context in place (KV-safe)"
    if action == "fresh-session":
        return "reset to a clean session"
    if action == "pause":
        return "paused for a human"
    # recommend
    if trigger == "agent.context":
        return "context critical — compact or start a fresh session"
    if trigger == "usage_limits.exceeded":
        return "usage cap hit — pause, slow down, or switch to a cheaper model"
    if trigger == "agent.message_failed":
        return "message never delivered — check the agent is running, then resend"
    return "review"


class ManagerPlugin(Plugin):
    """Policy: signal → action, every decision an auditable manager.action."""

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self._recent: deque[dict[str, Any]] = deque(maxlen=50)
        for trigger in _TRIGGERS:
            host.events.subscribe(trigger, self._make_handler(trigger))
        self._register_cli(host)
        self._register_api(host)

    # ── Settings ─────────────────────────────────────────────────────

    def _policy(self) -> dict[str, str]:
        vals = dict(_DEFAULTS)
        with suppress(Exception):
            vals.update(self.host.settings.all())
        return {k: str(vals.get(k) or _DEFAULTS[k]).lower() for k in _DEFAULTS}

    # ── Handlers ─────────────────────────────────────────────────────

    def _make_handler(self, trigger: str):
        def _handler(event: Event) -> None:
            self._on_trigger(trigger, event)
        return _handler

    def _on_trigger(self, trigger: str, event: Event) -> None:
        data = event.data or {}
        agent_id = str(data.get("agent_id") or "")
        if not agent_id:
            return
        policy = self._policy()
        action = decide(trigger, policy, context_state=data.get("state"))
        if action == "off":
            return

        executed = False
        detail = _advice(trigger, action)
        if action == "compact":
            executed = self._compact(agent_id)
            if not executed:
                # Harness can't compact in place — degrade to advice, don't
                # silently do nothing.
                detail = "no in-place compaction — start a fresh session"
        elif action == "fresh-session":
            executed = self._reset_session(agent_id)
        elif action == "pause":
            executed = self._pause(agent_id)

        record = {
            "agent_id": agent_id,
            "workspace": data.get("workspace") or self._agent_workspace(agent_id),
            "trigger": trigger,
            "action": action,
            "executed": executed,
            "detail": detail,
            "ts": time.time(),
        }
        self._recent.append(record)
        self._emit("manager.action", record)
        logger.info("manager: %s on %s → %s (executed=%s) — %s",
                    trigger, agent_id, action, executed, detail)

    # ── Actions (in-process orchestrator, like hitl) ─────────────────

    def _orch(self) -> Any:
        return getattr(self.host, "_orchestrator", None)

    def _reset_session(self, agent_id: str) -> bool:
        orch = self._orch()
        fn = getattr(orch, "reset_agent_session", None)
        if not callable(fn):
            return False
        try:
            fn(agent_id)
            return True
        except Exception as exc:
            logger.debug("manager: reset_session failed for %s: %s", agent_id, exc)
            return False

    def _pause(self, agent_id: str) -> bool:
        orch = self._orch()
        fn = getattr(orch, "stop_agent", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(agent_id))
        except Exception as exc:
            logger.debug("manager: pause failed for %s: %s", agent_id, exc)
            return False

    def _compact(self, agent_id: str) -> bool:
        orch = self._orch()
        fn = getattr(orch, "compact_agent", None)
        if not callable(fn):
            return False
        try:
            return bool((fn(agent_id) or {}).get("ok"))
        except Exception as exc:
            logger.debug("manager: compact failed for %s: %s", agent_id, exc)
            return False

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
            logger.debug("manager: emit %s failed: %s", event_type, exc)

    # ── API ──────────────────────────────────────────────────────────

    def _tui_lines(self) -> list[str]:
        p = self._policy()
        lines = [
            f"context-critical: {p['on_context_critical']}    "
            f"usage-exceeded: {p['on_usage_exceeded']}    "
            f"message-failed: {p['on_message_failed']}",
            "",
            "recent actions (newest last):",
        ]
        if not self._recent:
            lines.append("  (none yet)")
            return lines
        glyph = {"recommend": "·", "fresh-session": "↻", "pause": "⏸"}
        for r in self._recent:
            lines.append(
                f"  {glyph.get(r['action'], '·')} {r['agent_id']}  "
                f"{r['trigger']} → {r['action']}"
                f"{' [done]' if r['executed'] else ''}  {r['detail']}"
            )
        return lines

    def _register_api(self, host: PluginHost) -> None:
        @host.api.route("/policy", ["GET"])
        async def policy():
            return self._policy()

        @host.api.route("/actions", ["GET"])
        async def actions():
            return {"actions": list(self._recent)}

        @host.api.route("/tui", ["GET"])
        async def tui():
            return {"title": "Manager", "lines": self._tui_lines()}

    # ── CLI ──────────────────────────────────────────────────────────

    def _register_cli(self, host: PluginHost) -> None:
        from rich.console import Console
        from rich.table import Table

        console = Console()

        @host.cli.command("status")
        def status_cmd():
            """Show the manager policy + recent fleet-health actions."""
            ok, data = _call_daemon("/api/plugins/manager/policy", method="GET")
            if not ok:
                console.print(f"[red]manager status failed:[/] {data}")
                return
            console.print(
                "policy: "
                f"context-critical=[bold]{data.get('on_context_critical')}[/]  "
                f"usage-exceeded=[bold]{data.get('on_usage_exceeded')}[/]  "
                f"message-failed=[bold]{data.get('on_message_failed')}[/]"
            )
            ok2, adata = _call_daemon("/api/plugins/manager/actions", method="GET")
            actions = (adata or {}).get("actions") or [] if ok2 else []
            if not actions:
                console.print("[dim]no actions yet.[/]")
                return
            table = Table(title="recent manager actions")
            table.add_column("agent")
            table.add_column("trigger")
            table.add_column("action")
            table.add_column("done")
            table.add_column("detail")
            for r in actions[-20:]:
                table.add_row(
                    r.get("agent_id", ""), r.get("trigger", ""),
                    r.get("action", ""), "✓" if r.get("executed") else "",
                    r.get("detail", ""),
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


PLUGIN = ManagerPlugin()


def _legacy_on_load(ctx: PluginContext) -> ManagerPlugin:
    """Boot against a PluginContext (tests + legacy loader); mirrors autopilot."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "events.subscribe", "events.emit",
            "api.register", "cli.register", "agents.list", "agents.stop",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
        settings_defaults=_DEFAULTS,
    )
    PLUGIN.on_load(host)
    return PLUGIN
