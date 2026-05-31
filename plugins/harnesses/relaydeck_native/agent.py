"""
RelaydeckNativeAgent — operator agent (`type: relaydeck`) backed by pi.

Uses pi's agent loop (read/bash/edit/write + custom fleet extension) instead
of a hand-rolled completion + <<tool>> parser. Operator context (contract,
soul, workspace peers, skills, policy) is injected via `--append-system-prompt`;
fleet actions (message, manage, dashboard) are pi extension tools.

Chat surfaces:
  - PTY: interactive `pi` in the Terminal tab (same as type:pi)
  - HTTP / `relaydeck chat -m`: one-shot `pi -p --mode json --continue`
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from plugins.harnesses.pi.agent import PiAgent

from . import pi_engine
from . import prompt as prompt_mod

logger = logging.getLogger(__name__)


def _emit_stream(agent_id: str, reasoning: str, content: str) -> None:
    if not reasoning and not content:
        return
    try:
        from relaydeck.orchestrator import _bus
        _bus.publish(agent_id, "relaydeck.native.token", {
            "agent_id": agent_id,
            "reasoning": (reasoning or "")[:2000],
            "content": (content or "")[:2000],
        }, 0)
    except Exception:
        pass


def _emit_step(agent_id: str, text: str, *, kind: str = "tool") -> None:
    text = (text or "").strip()
    if not text:
        return
    cap = 1500 if kind == "reasoning" else 300
    try:
        from relaydeck.orchestrator import _bus
        _bus.publish(agent_id, "relaydeck.native.step",
                     {"agent_id": agent_id, "text": text[:cap], "kind": kind}, 0)
    except Exception:
        pass


def _emit_usage(agent_id: str, model: str, usage: dict[str, Any]) -> None:
    payload = {
        "agent_id": agent_id, "model": model,
        "provider": usage.get("provider") or "unknown",
        "prompt": int(usage.get("prompt_tokens") or 0),
        "completion": int(usage.get("completion_tokens") or 0),
        "cost_usd": usage.get("cost_usd"),
        "harness": "relaydeck-native",
    }
    try:
        from relaydeck.plugin import Event, get_registry
        bus = getattr(get_registry(), "_event_bus", None)
        if bus is not None:
            bus.emit(Event(type="usage.record", data=payload, source_plugin="relaydeck-native"))
    except Exception:
        pass
    try:
        from relaydeck.orchestrator import _bus
        _bus.publish(agent_id, "usage.record", payload, 0)
    except Exception:
        pass


def generate_reply(
    config_home: Path,
    db_path: str,
    agent_id: str,
    user_text: str,
    *,
    from_id: str = "user",
    complete: Callable[..., str] | None = None,
    ui_state: str | None = None,
    run_turn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Produce one assistant turn via pi. `complete` is kept only for tests
    that stub the old gateway path — production always uses pi."""
    if complete is not None:
        return _generate_reply_legacy(
            config_home, db_path, agent_id, user_text,
            from_id=from_id, complete=complete, ui_state=ui_state,
        )
    runner = run_turn or pi_engine.run_turn

    def _on_event(ev: dict[str, Any]) -> None:
        if ev.get("type") == "message_update":
            ame = ev.get("assistantMessageEvent") or {}
            if ame.get("type") == "text_delta":
                _emit_stream(agent_id, "", str(ame.get("delta") or ""))
        elif ev.get("type") == "tool_execution_end":
            _emit_step(agent_id, str(ev.get("toolName") or "tool"))

    try:
        return runner(
            config_home, db_path, agent_id, user_text,
            ui_state=ui_state, on_event=_on_event,
        )
    except RuntimeError as exc:
        logger.warning("relaydeck-native %s pi turn failed: %s", agent_id, exc)
        err = str(exc)
        return {
            "ok": False,
            "reply": f"(pi error: {err})",
            "error": err,
            "model": "",
            "layers": [],
            "tools": [],
            **pi_engine.pi_runtime_status(),
        }


def _generate_reply_legacy(
    config_home: Path,
    db_path: str,
    agent_id: str,
    user_text: str,
    *,
    from_id: str,
    complete: Callable[..., str],
    ui_state: str | None,
) -> dict[str, Any]:
    """Test-only shim: old gateway + <<tool>> loop (stub via ``complete=`` in tests)."""
    from . import tools as tools_mod

    config, workspace = _load_spec(config_home, agent_id)
    enabled = pi_engine.resolved_tools(config)
    ws_path = pi_engine._workspace_path(config_home, workspace)
    history = _load_history(db_path, agent_id, from_id, since=_session_since(config_home, agent_id))
    system_prompt, layers = prompt_mod.build_session(
        agent_id=agent_id, workspace=workspace, config=config,
        config_home=config_home, db_path=db_path, history=history, tools=enabled,
    )
    model = str(config.get("preset") or config.get("model") or "role:fast")
    convo = system_prompt
    if ui_state:
        convo += f"\n\n# Live UI state\n{ui_state}"
    convo += f"\n\n# Operator message\n{user_text}"
    final = ""
    tool_log: list[dict[str, Any]] = []
    empty_retry = True
    try:
        max_iters = max(1, int(config.get("max_tool_iters") or tools_mod.MAX_TOOL_ITERS))
    except (TypeError, ValueError):
        max_iters = tools_mod.MAX_TOOL_ITERS
    for _ in range(max_iters):
        prompt = convo + "\n\n# Your reply (emit a tool block if you need one, else answer)\n"
        try:
            raw = complete(agent_id, prompt, model=model, db_path=db_path) or ""
        except Exception as exc:
            final = f"(model error: {type(exc).__name__}: {exc})"
            break
        calls = tools_mod.parse_calls(raw)
        if not calls:
            stripped = tools_mod.strip_tool_blocks(raw)
            if not stripped and empty_retry and not tool_log:
                empty_retry = False
                convo += "\n\n# (your last reply was blank — answer the operator now)"
                continue
            final = stripped
            break
        obs = tools_mod.run_calls(
            calls, enabled=enabled, workspace_path=ws_path, agent_id=agent_id,
            workspace=workspace,
        )
        tool_log.append({"calls": [c["name"] for c in calls], "observations": obs})
        _emit_step(agent_id, obs)
        convo += f"\n\n# Assistant\n{raw}\n\n# Tool results\n{obs}"
    else:
        final = final or "(stopped after the tool-iteration limit)"
    if not final:
        final = "Done." if tool_log else "(the model returned an empty reply)"
    _persist_turn(db_path, from_id, agent_id, user_text, workspace)
    _persist_turn(db_path, agent_id, from_id, final, workspace)
    return {"reply": final, "model": model, "layers": layers, "tools": tool_log}


def chat_endpoint(config_home: Path, db_path: str, body: dict[str, Any]) -> dict[str, Any]:
    agent_id = str((body or {}).get("agent_id") or "").strip()
    text = str((body or {}).get("text") or "").strip()
    if not agent_id or not text:
        return {"ok": False, "error": "agent_id and text are required"}
    from relaydeck.config import AgentSpec
    spec_path = config_home / "agents" / f"{agent_id}.yaml"
    if not spec_path.exists():
        return {"ok": False, "error": f"no such agent: {agent_id}"}
    if AgentSpec.from_yaml(spec_path).type != "relaydeck":
        return {"ok": False, "error": f"{agent_id} is not a relaydeck-native agent"}
    ui_state = _render_ui_state(body or {})
    out = generate_reply(config_home, db_path, agent_id, text, ui_state=ui_state)
    if out.get("ok") is False or str(out.get("reply", "")).startswith("(pi error:"):
        return {
            "ok": False,
            "error": out.get("error") or out.get("reply") or "chat failed",
            **{k: out[k] for k in out if k not in ("ok",)},
        }
    return {"ok": True, **out}


def _render_ui_state(body: dict[str, Any]) -> str | None:
    layout = body.get("dashboard_layout")
    if isinstance(layout, list) and layout:
        lines = []
        for w in layout[:40]:
            if isinstance(w, dict) and w.get("key"):
                lines.append(
                    f"- {w['key']} @ ({w.get('x', '?')},{w.get('y', '?')}) "
                    f"{w.get('w', '?')}x{w.get('h', '?')}"
                )
        if lines:
            return (
                "Dashboard grid — 12 columns wide; each line is "
                "`widget @ (col x, row y) WxH cells`:\n" + "\n".join(lines)
            )
    widgets = body.get("dashboard_widgets")
    if isinstance(widgets, list) and widgets:
        return "Current dashboard widgets: " + ", ".join(str(w) for w in widgets[:40])
    return None


def context_endpoint(config_home: Path, db_path: str, agent_id: str) -> dict[str, Any]:
    config, workspace = _load_spec(config_home, agent_id)
    history = _load_history(db_path, agent_id, "user", since=_session_since(config_home, agent_id))
    composed, layers = prompt_mod.build_session(
        agent_id=agent_id, workspace=workspace, config=config,
        config_home=config_home, db_path=db_path, history=history,
    )
    return {
        "ok": True, "layers": layers, "composed": composed,
        "model": str(config.get("preset") or config.get("model") or "role:fast"),
        **pi_engine.pi_runtime_status(),
    }


def _load_spec(config_home: Path, agent_id: str) -> tuple[dict[str, Any], str | None]:
    return pi_engine._load_spec(config_home, agent_id)


def _load_history(
    db_path: str, agent_id: str, peer: str, *, since: float = 0.0
) -> list[dict[str, str]]:
    try:
        from relaydeck.db import open_db
        conn = open_db(db_path)
        try:
            rows = conn.execute(
                "SELECT from_id, body FROM agent_messages "
                "WHERE ((from_id=? AND to_id=?) OR (from_id=? AND to_id=?)) AND ts > ? "
                "ORDER BY ts DESC LIMIT ?",
                (agent_id, peer, peer, agent_id, since, prompt_mod._MAX_HISTORY),
            ).fetchall()
        finally:
            conn.close()
        rows = list(reversed(rows))
        return [
            {"role": "assistant" if r[0] == agent_id else "user", "text": r[1]}
            for r in rows
        ]
    except Exception:
        return []


def _session_marker_path(config_home: Path, agent_id: str) -> Path:
    from relaydeck.skills import validate_path_component
    safe = validate_path_component(agent_id, kind="agent id")
    return config_home / "runtime" / "chat-sessions" / f"{safe}.json"


def _session_since(config_home: Path, agent_id: str) -> float:
    try:
        import json
        p = _session_marker_path(config_home, agent_id)
        if not p.exists():
            return 0.0
        return float(json.loads(p.read_text()).get("since") or 0.0)
    except Exception:
        return 0.0


def reset_session(config_home: Path, agent_id: str) -> float:
    import json
    import time
    since = time.time()
    p = _session_marker_path(config_home, agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"since": since}))
    config, workspace = _load_spec(config_home, agent_id)
    pi_engine.clear_pi_session(config_home, workspace, agent_id)
    return since


def _persist_turn(db_path: str, from_id: str, to_id: str, body: str, workspace: str | None) -> None:
    pi_engine._persist_turn(db_path, from_id, to_id, body, workspace)


class RelaydeckNativeAgent(PiAgent):
    """Operator agent — pi under the hood with relaydeck fleet customization."""

    CLI = "pi"
    HARNESS_TYPE = "relaydeck"
    _pi_spawn: tuple[list[str], dict[str, str], Path | None] | None = None

    def run(self) -> None:
        if not pi_engine.pi_available():
            msg = pi_engine.pi_missing_message()
            logger.warning("Agent %s: %s", self.agent_id, msg)
            self.emit("harness.error", {"error": msg})
            self.update_status("errored", msg)
            return
        super().run()

    def _session_dir(self) -> Path | None:
        if not (self.workspace and self.agent_id):
            return None
        return pi_engine.session_dir(self._config_home, self.workspace, self.agent_id)

    def _pi_spawn_bundle(self) -> tuple[list[str], dict[str, str], Path | None]:
        if self._pi_spawn is None:
            self._pi_spawn = pi_engine.build_pi_argv(
                self._config_home, self.agent_id,
                config=self.config, workspace=self.workspace,
                interactive=True,
            )
        return self._pi_spawn

    def _build_command(self) -> list[str]:
        argv, _env, _cwd = self._pi_spawn_bundle()
        return argv

    def _build_env(self) -> dict[str, str]:
        _argv, pi_env, _cwd = self._pi_spawn_bundle()
        env = super()._build_env()
        env.update(pi_env)
        return env

    def _emit_usage(self, rec: dict, msg: dict) -> None:
        usage = msg.get("usage") or {}
        if not usage:
            return
        cost = (usage.get("cost") or {}).get("total")
        payload = {
            "agent_id": self.agent_id,
            "model": str(msg.get("model") or "unknown"),
            "provider": str(msg.get("provider") or "unknown"),
            "prompt": int(usage.get("input", 0) or 0),
            "completion": int(usage.get("output", 0) or 0),
            "cost_usd": float(cost) if cost is not None else None,
            "harness": "relaydeck-native",
        }
        from contextlib import suppress
        with suppress(Exception):
            self.emit("usage.record", payload)
        self._emit_bus("usage.record", payload)
