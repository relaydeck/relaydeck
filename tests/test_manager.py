"""
Manager plugin — policy layer that turns fleet-health signals into auditable
actions.

Two layers, mirroring autopilot/context-watch:
  1. `decide` — the PURE policy (trigger + policy → action), incl. the rule
     that agent.context only acts on `critical`.
  2. the handler — booted against a PluginEventBus + mocked orchestrator; the
     plugin-bus signals (agent.context / usage_limits.exceeded) produce a
     `manager.action` event; `recommend` (default) is advisory (executed
     false), while opted-in actions (fresh-session / pause) call through.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from relaydeck.plugin import Event, PluginContext, PluginEventBus
from plugins.manager.plugin import _DEFAULTS, _legacy_on_load, decide


# ── Pure policy ────────────────────────────────────────────────────


def test_decide_defaults_are_recommend():
    p = dict(_DEFAULTS)
    assert decide("agent.context", p, context_state="critical") == "recommend"
    assert decide("usage_limits.exceeded", p) == "recommend"


def test_decide_context_only_on_critical():
    p = {"on_context_critical": "pause"}
    assert decide("agent.context", p, context_state="warn") == "off"
    assert decide("agent.context", p, context_state="critical") == "pause"


def test_decide_unknown_trigger_is_off():
    assert decide("something.else", dict(_DEFAULTS)) == "off"


def test_decide_honours_policy():
    p = {"on_context_critical": "fresh-session", "on_usage_exceeded": "pause"}
    assert decide("agent.context", p, context_state="critical") == "fresh-session"
    assert decide("usage_limits.exceeded", p) == "pause"


# ── Handler ────────────────────────────────────────────────────────


@pytest.fixture
def ctx(tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "runtime").mkdir(parents=True)
    orch = MagicMock()
    orch.get_agent.return_value = {"id": "alice", "workspace": "demo"}
    orch.stop_agent.return_value = True
    orch.reset_agent_session.return_value = "in-place"
    bus = PluginEventBus()
    return PluginContext(config_home=cfg, event_bus=bus, orchestrator=orch), orch, bus


def _capture(bus, etype: str):
    seen: list[Event] = []
    bus.subscribe(etype, lambda e: seen.append(e))
    return seen


def _context_event(bus, state="critical", agent_id="alice"):
    bus.emit(Event(
        type="agent.context",
        data={"agent_id": agent_id, "state": state, "pct": 92.0,
              "workspace": "demo"},
        source_plugin="context-watch",
    ))


def test_recommend_is_advisory(ctx):
    c, orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus, "manager.action")
    _context_event(bus, state="critical")
    assert len(seen) == 1
    d = seen[0].data
    assert d["trigger"] == "agent.context"
    assert d["action"] == "recommend"
    assert d["executed"] is False
    orch.reset_agent_session.assert_not_called()
    orch.stop_agent.assert_not_called()


def test_context_warn_does_not_act(ctx):
    c, _orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus, "manager.action")
    _context_event(bus, state="warn")
    assert seen == []


def test_fresh_session_executes(ctx, monkeypatch):
    c, orch, bus = ctx
    # Opt into execution.
    import plugins.manager.plugin as mod
    monkeypatch.setattr(
        mod.ManagerPlugin, "_policy",
        lambda self: {"on_context_critical": "fresh-session",
                      "on_usage_exceeded": "recommend"},
    )
    _legacy_on_load(c)
    seen = _capture(bus, "manager.action")
    _context_event(bus, state="critical")
    assert len(seen) == 1
    assert seen[0].data["action"] == "fresh-session"
    assert seen[0].data["executed"] is True
    orch.reset_agent_session.assert_called_once_with("alice")


def test_message_failed_is_recommend(ctx):
    c, orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus, "manager.action")
    bus.emit(Event(
        type="agent.message_failed",
        data={"agent_id": "alice", "message_id": "m1", "workspace": "demo"},
        source_plugin="orchestrator",
    ))
    assert len(seen) == 1
    assert seen[0].data["trigger"] == "agent.message_failed"
    assert seen[0].data["action"] == "recommend"
    assert seen[0].data["executed"] is False
    # Never auto-acts on a failed message — the agent is usually down.
    orch.stop_agent.assert_not_called()
    orch.reset_agent_session.assert_not_called()


def test_usage_exceeded_pause_executes(ctx, monkeypatch):
    c, orch, bus = ctx
    import plugins.manager.plugin as mod
    monkeypatch.setattr(
        mod.ManagerPlugin, "_policy",
        lambda self: {"on_context_critical": "recommend",
                      "on_usage_exceeded": "pause"},
    )
    _legacy_on_load(c)
    seen = _capture(bus, "manager.action")
    bus.emit(Event(
        type="usage_limits.exceeded",
        data={"agent_id": "alice", "window": "session", "workspace": "demo"},
        source_plugin="usage-limits",
    ))
    assert len(seen) == 1
    assert seen[0].data["action"] == "pause"
    assert seen[0].data["executed"] is True
    orch.stop_agent.assert_called_once_with("alice")
