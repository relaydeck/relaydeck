"""
Autopilot — auto-answer benign native prompts.

A relaydeck agent runs UNATTENDED. The semantic-status engine already
*detects* when a harness is blocked on a prompt (it sets `awaiting-input`
by matching the rendered screen). But detection alone still leaves the
fleet stalled: "Do you trust the files in this folder? [y/N]", "press enter
to continue", an update notice — these sit forever with no human to press a
key, and orchestration grinds to a halt.

Autopilot closes that loop for the *safe* cases. On
`agent.status_changed → awaiting-input` it renders the agent's screen,
matches it against a small, conservative allowlist of prompt shapes, and —
if (and only if) a rule matches at the configured `mode` — sends the
benign answer straight to the PTY via the harness's sanctioned `send_input`
/ `send_message` (the same write the live terminal uses; it never touches
the read loop — the "terminal untouchable" contract).

Safety posture:
  - The matcher is a PURE function (`match_unblock_rule`) so the exact
    "what would fire" logic is unit-tested without a daemon, and
    `relaydeck autopilot test` lets an operator dry-run it.
  - Rules are tiered. `benign` (default) is only the always-safe prompts.
    `all-known` adds accept-the-default `[Y/n]` and update-deferral.
    Terms/license prompts are gated behind a *separate* explicit flag.
  - Anything NOT matched is left untouched and surfaced as
    `autopilot.held` — autopilot never guesses at an unknown prompt. The
    `hitl` plugin (loaded by default) escalates those to a human; the two
    compose — autopilot clears the benign noise, hitl raises the rest.
"""

from __future__ import annotations

import logging
import time
from collections import deque
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

PLUGIN_NAME = "autopilot"

_DEFAULTS: dict[str, Any] = {
    "mode": "benign",          # off | benign | all-known
    "auto_accept_terms": False,
    "cooldown_seconds": 20.0,
    "max_attempts": 3,
}

_TRUTHY = ("1", "true", "yes", "on")


# ── Rules (the curated allowlist) ──────────────────────────────────


@dataclass(frozen=True)
class UnblockRule:
    """A prompt shape autopilot knows how to clear.

    `pattern` is matched case-insensitively against the *tail* of the
    rendered screen (via the same matcher the awaiting-input detector
    uses, so detection and answering see the same region). `action` is the
    keystroke to send: `{"key": "<name>"}` or `{"data": "<text>", "enter":
    true}`. `tier` gates the rule by mode.
    """

    name: str
    pattern: str
    action: dict
    tier: str  # "benign" | "known" | "terms"
    why: str


# Conservative on purpose. Each pattern is anchored to a prompt shape so
# ordinary transcript prose can't trip it; each action is the lowest-risk
# response for that exact prompt.
_RULES: tuple[UnblockRule, ...] = (
    UnblockRule(
        name="press-enter",
        pattern=r"press (?:enter|return) to continue",
        action={"key": "enter"},
        tier="benign",
        why="A 'press enter to continue' gate just advances — always safe.",
    ),
    UnblockRule(
        name="trust-workspace",
        pattern=(
            r"(?:do you trust the (?:files|authors)"
            r"|trust this (?:folder|workspace|directory|project))"
        ),
        action={"data": "y", "enter": True},
        tier="benign",
        why=(
            "The operator already chose to run an agent in this workspace, "
            "so trusting it is implied — clears Claude Code / editor-style "
            "'do you trust the files in this folder?' prompts."
        ),
    ),
    # NOTE: there is deliberately no generic "accept the [Y/n] default" rule.
    # The shared tail-matcher is case-insensitive, so `[Y/n]` and `[y/N]` are
    # indistinguishable to it — and "blindly press the harness's default" is
    # exactly the kind of guess autopilot must refuse (the default could be
    # destructive). Unrecognized [y/n] prompts are HELD for a human.
    UnblockRule(
        name="defer-update",
        pattern=r"(?:update available|new version available|a new release of)",
        action={"data": "n", "enter": True},
        tier="known",
        why="Declines an in-session update — never swap the binary mid-run.",
    ),
    UnblockRule(
        name="accept-terms",
        pattern=r"(?:accept|agree to) (?:the )?(?:terms|license|agreement|eula)",
        action={"data": "y", "enter": True},
        tier="terms",
        why="Accepts a terms/license prompt (gated behind auto_accept_terms).",
    ),
)


def _tiers_for(mode: str, allow_terms: bool) -> set[str]:
    if mode == "benign":
        return {"benign"}
    if mode == "all-known":
        tiers = {"benign", "known"}
        if allow_terms:
            tiers.add("terms")
        return tiers
    return set()  # "off" or unknown → nothing


def match_unblock_rule(
    screen: str, *, mode: str = "benign", allow_terms: bool = False
) -> UnblockRule | None:
    """Pure: return the first rule that fires for `screen` at `mode`, or
    None. The single source of truth for autopilot's decision — exercised
    directly by tests and `relaydeck autopilot test`."""
    from relaydeck.semantic_engine import screen_matches_any

    tiers = _tiers_for(mode, allow_terms)
    if not tiers or not screen:
        return None
    for rule in _RULES:
        if rule.tier in tiers and screen_matches_any(screen, (rule.pattern,)):
            return rule
    return None


# Named single-key sequences autopilot can send (subset of the term-input
# endpoint's map — the only keys our rules use).
_KEY_SEQUENCES: dict[str, bytes] = {
    "enter": b"\r", "return": b"\r", "esc": b"\x1b",
    "y": b"y", "n": b"n", "space": b" ", "tab": b"\t",
}


def _apply_action(instance: Any, action: dict) -> bool:
    """Send a rule's action to a running harness instance via its
    sanctioned write path. Returns the harness's write result."""
    key = action.get("key")
    if key is not None:
        seq = _KEY_SEQUENCES.get(str(key).lower())
        if seq is None:
            return False
        return bool(instance.send_input(seq))
    data = str(action.get("data", ""))
    if action.get("enter"):
        return bool(instance.send_message(data))
    return bool(instance.send_input(data.encode()))


# ── Plugin ─────────────────────────────────────────────────────────


class AutopilotPlugin(Plugin):
    """React to awaiting-input, auto-answer benign prompts, surface the rest."""

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        # agent_id → {"attempts": int, "last_ts": float, "held": bool}
        self._state: dict[str, dict[str, Any]] = {}
        # Recent actions, newest-last — surfaced in the `relaydeck view`
        # Autopilot tab and the dashboard.
        self._recent: deque[str] = deque(maxlen=20)
        host.events.subscribe("agent.status_changed", self._on_status_changed)
        self._register_cli(host)
        self._register_api(host)

    # ── Settings ────────────────────────────────────────────────────

    def _settings(self) -> dict[str, Any]:
        vals = dict(_DEFAULTS)
        try:
            vals.update(self.host.settings.all())
        except Exception:
            pass
        return {
            "mode": str(vals.get("mode") or "benign").lower(),
            "auto_accept_terms": str(vals.get("auto_accept_terms", False)).lower()
            in _TRUTHY,
            "cooldown_seconds": float(vals.get("cooldown_seconds") or 0),
            "max_attempts": int(vals.get("max_attempts") or 0),
        }

    # ── Event handler ────────────────────────────────────────────────

    def _on_status_changed(self, event: Event) -> None:
        data = event.data or {}
        # Don't react to our own (or hitl's) writes; the engine re-emits
        # the real transition as the agent proceeds anyway.
        if data.get("source") in ("autopilot",):
            return
        status = data.get("to") or data.get("semantic_status") or data.get("status")
        agent_id = str(data.get("agent_id") or data.get("id") or "")
        if not agent_id:
            return

        if status != "awaiting-input":
            # Episode over — reset so a future stall is handled fresh.
            self._state.pop(agent_id, None)
            return

        s = self._settings()
        if s["mode"] == "off":
            return

        now = time.time()
        st = self._state.setdefault(agent_id, {"attempts": 0, "last_ts": 0.0, "held": False})
        if s["cooldown_seconds"] > 0 and (now - st["last_ts"]) < s["cooldown_seconds"]:
            return
        if s["max_attempts"] > 0 and st["attempts"] >= s["max_attempts"]:
            self._hold(agent_id, "max_attempts", st, now)
            return

        screen = self._render_screen(agent_id)
        if not screen:
            return
        rule = match_unblock_rule(
            screen, mode=s["mode"], allow_terms=s["auto_accept_terms"]
        )
        if rule is None:
            self._hold(agent_id, "no_matching_rule", st, now, screen=screen)
            return

        instance = self._running_instance(agent_id)
        if instance is None:
            return
        ok = False
        try:
            ok = _apply_action(instance, rule.action)
        except Exception as exc:
            logger.debug("autopilot: send failed for %s: %s", agent_id, exc)
        st["attempts"] += 1
        st["last_ts"] = now
        st["held"] = False
        self._emit("autopilot.unblocked", {
            "agent_id": agent_id,
            "workspace": self._agent_workspace(agent_id),
            "rule": rule.name,
            "action": rule.action,
            "ok": ok,
            "attempt": st["attempts"],
            "ts": now,
        })
        self._recent.append(
            f"unblocked {agent_id} · {rule.name} → {rule.action} (ok={ok})"
        )
        logger.info(
            "autopilot: unblocked %s via rule %s (ok=%s, attempt=%d)",
            agent_id, rule.name, ok, st["attempts"],
        )

    def _hold(self, agent_id: str, reason: str, st: dict, now: float,
              *, screen: str | None = None) -> None:
        """Surface an awaiting-input we won't auto-answer (once per episode)
        so it's visible the agent needs a human. hitl escalates from here."""
        if st.get("held"):
            return
        st["held"] = True
        st["last_ts"] = now
        tail = ""
        if screen:
            tail = "\n".join(screen.rstrip().splitlines()[-6:])
        self._emit("autopilot.held", {
            "agent_id": agent_id,
            "workspace": self._agent_workspace(agent_id),
            "reason": reason,
            "screen_tail": tail,
            "ts": now,
        })
        self._recent.append(f"HELD {agent_id} · {reason} — left for hitl/human")
        logger.info("autopilot: holding %s (%s) — left for hitl/human", agent_id, reason)

    # ── Orchestrator access (in-process, like hitl) ──────────────────

    def _orch(self) -> Any:
        return getattr(self.host, "_orchestrator", None)

    def _running_instance(self, agent_id: str) -> Any:
        orch = self._orch()
        getter = getattr(orch, "get_running_instance", None)
        if not callable(getter):
            return None
        try:
            inst = getter(agent_id)
        except Exception:
            return None
        return inst if (inst is not None and hasattr(inst, "send_input")) else None

    def _render_screen(self, agent_id: str) -> str:
        inst = self._running_instance(agent_id)
        if inst is None:
            return ""
        try:
            from relaydeck.screen import render
            buf = bytes(inst.get_pty_buffer() or b"")
            return render(buf, cols=120, rows=40) if buf else ""
        except Exception:
            return ""

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
            logger.debug("autopilot: emit %s failed: %s", event_type, exc)

    # ── API ──────────────────────────────────────────────────────────

    def _tui_lines(self) -> list[str]:
        """Plain-text content for the `relaydeck view` Autopilot tab
        (served at /api/plugins/autopilot/tui). Shows the live posture, which
        rules are active, and recent auto-answer / hold actions."""
        s = self._settings()
        lines = [
            f"mode: {s['mode']}    auto_accept_terms: {s['auto_accept_terms']}",
            f"cooldown: {s['cooldown_seconds']:g}s    "
            f"max_attempts: {s['max_attempts']}    "
            f"active episodes: {len(self._state)}",
            "",
            "rules (active at this mode marked [on]):",
        ]
        tiers = _tiers_for(s["mode"], s["auto_accept_terms"])
        for r in _RULES:
            mark = "on " if r.tier in tiers else "off"
            ans = (f"key:{r.action['key']}" if "key" in r.action
                   else f"{r.action.get('data', '')!r}+Enter")
            lines.append(f"  [{mark}] {r.name} ({r.tier}) -> {ans}")
        lines += ["", "recent actions (newest last):"]
        lines += [f"  {x}" for x in self._recent] or ["  (none yet)"]
        return lines

    def _register_api(self, host: PluginHost) -> None:
        @host.api.route("/status", ["GET"])
        async def status():
            s = self._settings()
            return {**s, "active_episodes": len(self._state)}

        @host.api.route("/rules", ["GET"])
        async def rules():
            return {
                "rules": [
                    {"name": r.name, "tier": r.tier, "pattern": r.pattern,
                     "action": r.action, "why": r.why}
                    for r in _RULES
                ]
            }

        @host.api.route("/tui", ["GET"])
        async def tui():
            return {"title": "Autopilot", "lines": self._tui_lines()}

    # ── CLI ──────────────────────────────────────────────────────────

    def _register_cli(self, host: PluginHost) -> None:
        import click
        from rich.console import Console
        from rich.table import Table

        console = Console()

        @host.cli.command("status")
        def status_cmd():
            """Show autopilot mode + how many agents are mid-prompt."""
            ok, data = _call_daemon("/api/plugins/autopilot/status", method="GET")
            if not ok:
                console.print(f"[red]autopilot status failed:[/] {data}")
                return
            console.print(
                f"mode=[bold]{data.get('mode')}[/]  "
                f"auto_accept_terms={data.get('auto_accept_terms')}  "
                f"cooldown={data.get('cooldown_seconds')}s  "
                f"max_attempts={data.get('max_attempts')}  "
                f"active={data.get('active_episodes')}"
            )

        @host.cli.command("rules")
        def rules_cmd():
            """List the prompt-shape allowlist autopilot can auto-answer."""
            table = Table(title="autopilot rules")
            table.add_column("rule"); table.add_column("tier")
            table.add_column("answer"); table.add_column("why")
            for r in _RULES:
                ans = (f"key:{r.action['key']}" if "key" in r.action
                       else f"{r.action.get('data', '')!r}+Enter")
                table.add_row(r.name, r.tier, ans, r.why)
            console.print(table)

        @host.cli.command("test")
        @click.argument("screen_text", nargs=-1, required=True)
        @click.option("--mode", default="benign",
                      type=click.Choice(["off", "benign", "all-known"]),
                      help="Mode to evaluate against (default: benign).")
        @click.option("--terms", is_flag=True, default=False,
                      help="Treat auto_accept_terms as on for this dry-run.")
        def test_cmd(screen_text: tuple[str, ...], mode: str, terms: bool):
            """Dry-run the matcher against SCREEN_TEXT (no agent touched).

            \b
            relaydeck autopilot test "Do you trust the files in this folder? [y/N]"
            relaydeck autopilot test "Update available! [Y/n]" --mode all-known
            """
            text = " ".join(screen_text)
            rule = match_unblock_rule(text, mode=mode, allow_terms=terms)
            if rule is None:
                console.print(
                    f"[yellow]no rule fires[/] at mode={mode} — autopilot would "
                    "HOLD this (left for a human / hitl)."
                )
                return
            ans = (f"key:{rule.action['key']}" if "key" in rule.action
                   else f"{rule.action.get('data', '')!r} + Enter")
            console.print(
                f"[green]would fire[/] [bold]{rule.name}[/] ({rule.tier}) "
                f"→ send {ans}\n  [dim]{rule.why}[/]"
            )


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


PLUGIN = AutopilotPlugin()


def _legacy_on_load(ctx: PluginContext) -> "AutopilotPlugin":
    """Boot against a PluginContext (tests + legacy loader). Mirrors the
    hitl plugin's pattern."""
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
