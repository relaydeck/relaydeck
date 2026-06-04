"""
Context-watch — emit `agent.context` fullness so a manager acts before the
harness auto-compacts.

Two layers, mirroring the autopilot tests:
  1. `classify_fill` — the PURE threshold decision (no daemon).
  2. the handler — booted against a PluginEventBus + mocked orchestrator; a
     `usage.record` event computes fill against the model's context window and
     emits `agent.context` on a state change (escalation AND recovery), never
     for an uncatalogued model.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from relaydeck.plugin import Event, PluginContext, PluginEventBus
from plugins.context_watch.plugin import _DEFAULTS, _legacy_on_load, classify_fill


# ── Pure classifier ────────────────────────────────────────────────


def test_classify_thresholds():
    assert classify_fill(10, 100, warn_pct=70, critical_pct=88).state == "ok"
    assert classify_fill(72, 100, warn_pct=70, critical_pct=88).state == "warn"
    assert classify_fill(90, 100, warn_pct=70, critical_pct=88).state == "critical"


def test_classify_pct_and_clamp():
    f = classify_fill(50, 200, warn_pct=70, critical_pct=88)
    assert f.pct == 25.0 and f.used == 50 and f.window == 200
    # negative used clamps to 0.
    assert classify_fill(-5, 100, warn_pct=70, critical_pct=88).used == 0


def test_classify_unknown_window_is_none():
    assert classify_fill(100, 0, warn_pct=70, critical_pct=88) is None
    assert classify_fill(100, -1, warn_pct=70, critical_pct=88) is None


def test_defaults_match_manifest():
    assert _DEFAULTS["warn_pct"] == 70.0
    assert _DEFAULTS["critical_pct"] == 88.0


# ── Handler ────────────────────────────────────────────────────────


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    (cfg / "runtime").mkdir(parents=True)
    orch = MagicMock()
    orch.get_agent.return_value = {"id": "alice", "workspace": "demo"}
    bus = PluginEventBus()
    # Pin a known context window so the test is deterministic (no models.dev).
    import plugins.context_watch.plugin as mod
    monkeypatch.setattr(
        mod.ContextWatchPlugin, "_context_window",
        lambda self, provider, model, fallback: 1000,
    )
    return PluginContext(config_home=cfg, event_bus=bus, orchestrator=orch), orch, bus


def _capture(bus, etype: str):
    seen: list[Event] = []
    bus.subscribe(etype, lambda e: seen.append(e))
    return seen


def _usage(bus, prompt, agent_id="alice", model="claude-opus-4-8", provider="anthropic"):
    bus.emit(Event(
        type="usage.record",
        data={"agent_id": agent_id, "model": model, "provider": provider,
              "prompt": prompt, "completion": 10},
        source_plugin="metering",
    ))


def test_emits_on_crossing_warn(ctx):
    c, _orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus, "agent.context")
    _usage(bus, prompt=750)  # 75% of 1000 → warn
    assert len(seen) == 1
    d = seen[0].data
    assert d["state"] == "warn"
    assert d["pct"] == 75.0
    assert d["used_tokens"] == 750
    assert d["context_window"] == 1000
    assert d["agent_id"] == "alice"
    assert "compact" in d["recommend"] or "compaction" in d["recommend"]


def test_no_duplicate_emit_while_state_unchanged(ctx):
    c, _orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus, "agent.context")
    _usage(bus, prompt=720)   # warn
    _usage(bus, prompt=730)   # still warn — no new emit
    assert len(seen) == 1


def test_emits_on_escalation_and_recovery(ctx):
    c, _orch, bus = ctx
    _legacy_on_load(c)
    seen = _capture(bus, "agent.context")
    _usage(bus, prompt=300)   # ok (first reading, state changes from default ok? no)
    _usage(bus, prompt=750)   # warn  → emit
    _usage(bus, prompt=950)   # critical → emit
    _usage(bus, prompt=100)   # ok after a compaction/fresh session → emit
    states = [e.data["state"] for e in seen]
    assert states == ["warn", "critical", "ok"]


def test_uncatalogued_model_no_emit(ctx, monkeypatch):
    c, _orch, bus = ctx
    # Override the window lookup back to "unknown" (0) for this test.
    import plugins.context_watch.plugin as mod
    monkeypatch.setattr(
        mod.ContextWatchPlugin, "_context_window",
        lambda self, provider, model, fallback: 0,
    )
    _legacy_on_load(c)
    seen = _capture(bus, "agent.context")
    _usage(bus, prompt=999999, model="some-unknown-model")
    assert seen == []
