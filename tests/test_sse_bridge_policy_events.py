"""
Policy / fleet-health events reach the live SSE stream.

The orchestrator bridges selected plugin-bus event families onto the SSE bus
(`_bus`) so the dashboard, `view`, and `events tail -f` see them without
polling. autopilot / usage-limits / manager decisions used to be invisible
there (they only lived on the plugin bus); this pins that they're now bridged,
so a manager's actions and an autopilot hold are auditable on the live feed.
"""

from __future__ import annotations

from pathlib import Path

import relaydeck.orchestrator as _orch_mod
from relaydeck.plugin import Event, PluginEventBus
from relaydeck.orchestrator import get_orchestrator


def _orch(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)
    orch.set_event_bus(PluginEventBus())
    return orch


def _drain(q):
    out = []
    try:
        while True:
            out.append(q.get_nowait())
    except Exception:
        pass
    return out


def test_policy_events_bridge_to_sse(tmp_path, monkeypatch):
    orch = _orch(tmp_path, monkeypatch)
    plugin_bus = orch._plugin_event_bus
    q = orch.subscribe_events("*")
    try:
        plugin_bus.emit(Event(type="manager.action",
                              data={"agent_id": "alice", "action": "pause"},
                              source_plugin="manager"))
        plugin_bus.emit(Event(type="autopilot.held",
                              data={"agent_id": "bob", "reason": "no_rule"},
                              source_plugin="autopilot"))
        plugin_bus.emit(Event(type="usage_limits.exceeded",
                              data={"agent_id": "carol", "window": "session"},
                              source_plugin="usage-limits"))
        plugin_bus.emit(Event(type="agent.context",
                              data={"agent_id": "dave", "state": "critical"},
                              source_plugin="context-watch"))
    finally:
        events = _drain(q)
        orch.unsubscribe_events("*", q)

    types = {e["type"] for e in events}
    assert {"manager.action", "autopilot.held",
            "usage_limits.exceeded", "agent.context"} <= types
    # The agent_id is carried through so the feed can attribute each event.
    by_type = {e["type"]: e for e in events}
    assert by_type["manager.action"]["agent_id"] == "alice"
    assert by_type["agent.context"]["agent_id"] == "dave"
