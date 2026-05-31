"""
Telegram reserved control-commands (/new, /clear, /stop, /status, /help) and
the HITL "operator home" channel (subscribes to hitl.escalation → admin DM).

These act ON the routed agent instead of being forwarded as a chat message,
so an operator can start a fresh harness session or stop an agent from
Telegram. Driven with a lightweight fake host so we exercise the dispatch
logic without PTB / a live bot.
"""

from __future__ import annotations

from types import SimpleNamespace

from relaydeck.harness import HarnessAgent
from plugins.telegram.plugin import TelegramPlugin
from plugins.telegram.routes import Route


class _Orch:
    def __init__(self):
        self.reset_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.restart_calls: list[str] = []
        self._inst = SimpleNamespace(get_pty_buffer=lambda: b"hello\nworld\n")

    def reset_agent_session(self, aid):
        self.reset_calls.append(aid)
        return "restart"

    def restart_agent(self, aid):
        self.restart_calls.append(aid)

    def stop_agent(self, aid):
        self.stop_calls.append(aid)
        return True

    def get_running_instance(self, aid):
        return self._inst

    def list_agents(self):
        return [{"id": "rev", "workspace": "demo",
                 "status": "running", "semantic_status": "working"}]


def _plugin(settings=None):
    """A TelegramPlugin with a minimal fake host + stubbed outbound, enough
    to drive command dispatch / the HITL handler in isolation."""
    orch = _Orch()
    events: list[tuple] = []
    host = SimpleNamespace(
        _orchestrator=orch,
        settings=SimpleNamespace(get=lambda k, d=None: (settings or {}).get(k, d)),
        events=SimpleNamespace(emit=lambda t, d=None: events.append((t, d))),
    )
    p = TelegramPlugin()
    p.host = host
    sent: list[tuple] = []
    p.send_reply = lambda chat_id, text, **kw: sent.append((chat_id, text, kw)) or {"ok": True}  # type: ignore
    p._react = lambda *a, **k: None  # type: ignore
    p._record_activity = lambda *a, **k: None  # type: ignore
    return p, orch, sent, events


def _ctx():
    return {"chat_id": 1, "thread_id": None, "connection_id": None,
            "reactions_enabled": False}


# ── reserved commands ────────────────────────────────────────────────


def test_new_command_resets_routed_agent():
    p, orch, sent, _ = _plugin()
    p._dispatch_command_action("new", [Route(workspace="demo", chat_id=1, agent="rev")], _ctx())
    assert orch.reset_calls == ["rev"]
    assert any("New session created" in t for _, t, _ in sent)


def test_clear_fresh_reset_are_aliases_for_new():
    for cmd in ("clear", "fresh", "reset"):
        p, orch, sent, _ = _plugin()
        p._dispatch_command_action(cmd, [Route(workspace="demo", chat_id=1, agent="rev")], _ctx())
        assert orch.reset_calls == ["rev"], cmd
        assert any("New session created" in t for _, t, _ in sent), cmd


def test_restart_command_restarts_routed_agent_keeping_history():
    p, orch, sent, _ = _plugin()
    p._dispatch_command_action("restart", [Route(workspace="demo", chat_id=1, agent="rev")], _ctx())
    assert orch.restart_calls == ["rev"]
    assert orch.reset_calls == []  # restart keeps history; not a fresh session
    assert any("Restarted" in t for _, t, _ in sent)


def test_screenshot_command_sends_terminal_snapshot():
    p, _orch, sent, _ = _plugin()
    p._dispatch_command_action("screenshot", [Route(workspace="demo", chat_id=1, agent="rev")], _ctx())
    assert len(sent) == 1
    _chat_id, text, kw = sent[0]
    assert kw.get("parse_mode") == "HTML"
    assert "<pre>" in text and "hello" in text and "rev" in text


def test_stop_command_stops_routed_agent():
    p, orch, sent, _ = _plugin()
    p._dispatch_command_action("stop", [Route(workspace="demo", chat_id=1, agent="rev")], _ctx())
    assert orch.stop_calls == ["rev"]
    assert any("stopped" in t for _, t, _ in sent)


def test_status_command_replies_state_without_mutation():
    p, orch, sent, _ = _plugin()
    p._dispatch_command_action("status", [Route(workspace="demo", chat_id=1, agent="rev")], _ctx())
    assert orch.reset_calls == [] and orch.stop_calls == []
    assert any("running" in t for _, t, _ in sent)


def test_help_command_needs_no_agent():
    p, orch, sent, _ = _plugin()
    p._dispatch_command_action("help", [], _ctx())
    assert orch.reset_calls == [] and orch.stop_calls == []
    assert any("/new" in t for _, t, _ in sent)


def test_command_with_no_route_reports_no_agent():
    p, _orch, sent, _ = _plugin()
    p._dispatch_command_action("new", [], _ctx())
    assert any("No agent" in t for _, t, _ in sent)


def test_broadcast_route_targets_all_workspace_agents():
    p, orch, _sent, _ = _plugin()
    # route with no agent → broadcast to the workspace (list_agents → rev).
    p._dispatch_command_action("stop", [Route(workspace="demo", chat_id=1)], _ctx())
    assert orch.stop_calls == ["rev"]


# ── HITL channel ──────────────────────────────────────────────────────


def test_hitl_escalation_dms_admin_chat_when_configured():
    p, _orch, sent, _ = _plugin({"hitl_admin_chat_id": "555"})
    p._on_hitl_escalation(SimpleNamespace(data={
        "agent_id": "rev", "workspace": "demo", "kind": "awaiting-input",
        "message": "need approval", "respond_hint": "relaydeck reply ...",
    }))
    assert len(sent) == 1
    chat_id, text, _kw = sent[0]
    assert chat_id == 555
    assert "rev" in text and "need approval" in text


def test_hitl_escalation_noop_without_admin_chat():
    p, _orch, sent, _ = _plugin({})  # no hitl_admin_chat_id
    p._on_hitl_escalation(SimpleNamespace(data={"agent_id": "rev"}))
    assert sent == []


# ── cross-harness session-reset extension point ───────────────────────


def test_base_harness_reset_session_defaults_false():
    # The base contract: no in-place reset → orchestrator restarts for a
    # fresh session. A harness with a native reset overrides this.
    assert HarnessAgent.reset_session(object.__new__(HarnessAgent)) is False


def test_hitl_escalation_logs_when_delivery_fails(caplog):
    """A failed Telegram delivery (worker down / bad token / ambiguous
    connection) must be LOGGED, not silently dropped — HITL is a reach-me path.
    (The always-on web bell still fires independently of this.)"""
    import logging

    p, _orch, _sent, _ = _plugin({"hitl_admin_chat_id": "555"})
    # Simulate the channel being un-deliverable.
    p.send_reply = lambda chat_id, text, **kw: {"ok": False, "error": "worker not ready"}  # type: ignore
    with caplog.at_level(logging.WARNING):
        p._on_hitl_escalation(SimpleNamespace(data={"agent_id": "rev", "message": "x"}))
    assert any("NOT delivered" in r.getMessage() for r in caplog.records)


# ── /screenshot formatting: HTML-escape + post-escape size cap ────────


def test_format_screenshot_escapes_special_chars():
    out = TelegramPlugin._format_screenshot("rev", "a<b>&c")
    assert "<pre>" in out and out.endswith("</pre>")
    assert "a&lt;b&gt;&amp;c" in out  # escaped inside the <pre> block


def test_format_screenshot_caps_after_escape_without_breaking_entities():
    import re
    # Dense special chars: each expands under html.escape, so a raw-length cap
    # could overflow. Truncating the ESCAPED tail must keep us under 4096 and
    # never leave a broken/partial entity.
    big = "x&<>" * 3000
    out = TelegramPlugin._format_screenshot("rev", big)
    assert len(out) < 4096
    assert out.endswith("</pre>") and "…" in out
    body = out.split("<pre>", 1)[1].rsplit("</pre>", 1)[0]
    # No bare '&' that doesn't open a known entity.
    assert not re.search(r"&(?!amp;|lt;|gt;|quot;|#x27;)", body)
