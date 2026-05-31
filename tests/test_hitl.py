"""
Human-in-the-loop escalation plugin.

Boots the plugin against a real PluginEventBus + a mocked orchestrator, fires
the trigger events harnesses/orchestrator actually emit, and asserts the
`hitl.escalation` fan-out + optional auto-stop behavior. Channels are
decoupled (subscribers of `hitl.escalation`), so a test channel just
subscribes to the bus.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from relaydeck.plugin import Event, PluginContext, PluginEventBus
from plugins.hitl.plugin import _DEFAULTS, _legacy_on_load


@pytest.fixture
def ctx(tmp_path):
    cfg_home = tmp_path / "cfg"
    (cfg_home / "runtime").mkdir(parents=True)
    orch = MagicMock()
    orch.get_agent.return_value = {"id": "alice", "workspace": "demo"}
    orch.stop_agent.return_value = True
    orch.set_semantic_status.return_value = True
    bus = PluginEventBus()
    return PluginContext(config_home=cfg_home, workspace_path=None,
                         event_bus=bus, orchestrator=orch), orch, bus


def _capture(bus):
    seen: list[Event] = []
    bus.subscribe("hitl.escalation", lambda e: seen.append(e))
    return seen


def _status_changed(bus, agent_id="alice", to="awaiting-input"):
    bus.emit(Event(type="agent.status_changed",
                   data={"agent_id": agent_id, "from": "working", "to": to,
                         "source": "hook"},
                   source_plugin="orchestrator"))


def _error(bus, agent_id="alice", error="boom"):
    bus.emit(Event(type="agent.error",
                   data={"agent_id": agent_id, "type": "pi", "error": error},
                   source_plugin="orchestrator"))


def test_defaults_match_manifest():
    assert _DEFAULTS["enabled"] is True
    assert _DEFAULTS["escalate_on"] == "both"
    assert _DEFAULTS["on_escalation"] == "notify"


def test_awaiting_input_escalates(ctx):
    c, _orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus)
    _status_changed(bus)
    assert len(seen) == 1
    assert seen[0].data["kind"] == "awaiting-input"
    assert seen[0].data["agent_id"] == "alice"


def test_non_awaiting_status_does_not_escalate(ctx):
    c, _orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus)
    _status_changed(bus, to="working")
    assert seen == []


def test_error_escalates(ctx):
    c, _orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus)
    _error(bus, error="stack overflow")
    assert len(seen) == 1
    assert seen[0].data["kind"] == "error"
    assert "stack overflow" in seen[0].data["message"]


def test_escalate_on_errors_skips_awaiting_input(ctx, monkeypatch):
    monkeypatch.setenv("RELAYDECK_HITL_ESCALATE_ON", "errors")
    c, _orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus)
    _status_changed(bus)          # awaiting-input — should be ignored
    assert seen == []
    _error(bus)                   # error — should escalate
    assert len(seen) == 1


def test_disabled_suppresses_all(ctx, monkeypatch):
    monkeypatch.setenv("RELAYDECK_HITL_ENABLED", "false")
    c, _orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus)
    _status_changed(bus)
    _error(bus)
    assert seen == []


def test_cooldown_suppresses_repeat(ctx):
    c, _orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus)
    _status_changed(bus)
    _status_changed(bus)  # within default 300s cooldown for (alice, awaiting-input)
    assert len(seen) == 1


def test_notify_and_stop_stops_the_agent(ctx, monkeypatch):
    monkeypatch.setenv("RELAYDECK_HITL_ON_ESCALATION", "notify_and_stop")
    c, orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus)
    _error(bus)
    orch.stop_agent.assert_called_with("alice")
    assert seen[0].data["stopped"] is True


def test_notify_only_does_not_stop(ctx):
    c, orch, bus = ctx
    _legacy_on_load(c)
    _error(bus)
    orch.stop_agent.assert_not_called()


def test_manual_escalate_sets_awaiting_input(ctx):
    c, orch, bus = ctx
    plugin = _legacy_on_load(c)
    seen = _capture(bus)
    res = plugin.escalate("alice", kind="manual", message="need a human")
    assert res["escalated"] is True
    assert len(seen) == 1 and seen[0].data["message"] == "need a human"
    # Manual path flips the agent to awaiting-input so the UI shows it paused.
    orch.set_semantic_status.assert_called_with("alice", "awaiting-input", source="hitl")


# ── reentrancy + emit-ordering regressions ────────────


class _ReentrantOrch:
    """Mimics the REAL orchestrator: set_semantic_status emits
    agent.status_changed synchronously on the same bus (orchestrator.py).
    The MagicMock in `ctx` can't exercise this reentrant path."""

    def __init__(self, bus):
        self._bus = bus
        self.stopped: list[str] = []

    def set_semantic_status(self, agent_id, status, *, source="hook"):
        self._bus.emit(Event(
            type="agent.status_changed",
            data={"agent_id": agent_id, "from": "working", "to": status,
                  "source": source},
            source_plugin="orchestrator"))
        return True

    def stop_agent(self, agent_id):
        self.stopped.append(agent_id)
        return True


def _boot_reentrant(tmp_path):
    cfg_home = tmp_path / "cfg"
    (cfg_home / "runtime").mkdir(parents=True)
    bus = PluginEventBus()
    orch = _ReentrantOrch(bus)
    ctx = PluginContext(config_home=cfg_home, workspace_path=None,
                        event_bus=bus, orchestrator=orch)
    return _legacy_on_load(ctx), bus, orch


def test_manual_ask_emits_exactly_one_escalation_under_reentrant_bus(tmp_path):
    """A manual ask flips the agent to awaiting-input; the orchestrator emits
    agent.status_changed(source=hitl) synchronously. The plugin must NOT
    re-enter and double-notify — exactly one hitl.escalation, the manual one."""
    plugin, bus, _orch = _boot_reentrant(tmp_path)

    seen: list[dict] = []
    # Copy the payload AT dispatch — never rely on post-emit mutation.
    bus.subscribe("hitl.escalation", lambda e: seen.append(dict(e.data)))

    res = plugin.escalate("alice", kind="manual", message="approve deploy?")
    assert res["escalated"] is True
    assert len(seen) == 1, [s.get("kind") for s in seen]
    assert seen[0]["kind"] == "manual"
    assert seen[0]["message"] == "approve deploy?"


def test_stopped_flag_present_in_payload_at_dispatch(tmp_path, monkeypatch):
    """notify_and_stop must stop the agent BEFORE emitting, so synchronous
    subscribers + durable bus_events see `stopped: true` — not just the API
    return value (which mutates the dict post-emit)."""
    monkeypatch.setenv("RELAYDECK_HITL_ON_ESCALATION", "notify_and_stop")
    plugin, bus, orch = _boot_reentrant(tmp_path)

    seen: list[dict] = []
    bus.subscribe("hitl.escalation", lambda e: seen.append(dict(e.data)))

    plugin.escalate("alice", kind="error", message="boom", set_status=False)
    assert orch.stopped == ["alice"]
    assert len(seen) == 1
    assert seen[0]["stopped"] is True  # present at dispatch, not post-mutation


def test_status_lists_telegram_only_when_configured(ctx, monkeypatch):
    """`hitl status` must report telegram as a channel ONLY when telegram is
    loaded AND has an admin chat id set — listing it while unconfigured was a
    false 'you're covered' signal (its escalation handler no-ops without one)."""
    from types import SimpleNamespace
    import relaydeck.plugin as rdp
    import relaydeck.plugin_settings as ps

    c, _orch, _bus = ctx
    plugin = _legacy_on_load(c)
    monkeypatch.setattr(
        rdp, "get_registry",
        lambda: SimpleNamespace(all=lambda: [SimpleNamespace(name="telegram")]),
    )
    # Loaded but NOT configured → telegram omitted (only the always-on web bell).
    monkeypatch.setattr(ps, "get_setting", lambda plugin, key, default=None: "")
    assert plugin._available_channels() == ["web"]
    # Configured (admin chat set) → telegram listed.
    monkeypatch.setattr(
        ps, "get_setting",
        lambda plugin, key, default=None: "555" if key == "hitl_admin_chat_id" else default,
    )
    assert plugin._available_channels() == ["web", "telegram"]
