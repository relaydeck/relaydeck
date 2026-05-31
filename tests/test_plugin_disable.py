"""
Plugin enable/disable: persistence + load-time skip + live unload.

The persistence module is straightforward (YAML list). The interesting
behavior is in the registry — `load_all` must skip disabled plugins
while still keeping them in `discovered_all()` so the UI can re-enable
them, and `disable(name)` must unsubscribe handlers from the event bus
so a disabled plugin really stops reacting.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relaydeck.plugin as plug
from relaydeck.plugin import (
    Event,
    EventSubscription,
    RelaydeckPlugin,
    PluginContext,
    PluginRegistry,
)
from relaydeck.plugin_disabled import disabled_set, is_disabled, set_disabled


# ── Persistence ─────────────────────────────────────────────────────


def test_disabled_set_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert disabled_set() == set()
    set_disabled("emote", True)
    assert is_disabled("emote") is True
    assert disabled_set() == {"emote"}
    set_disabled("metering", True)
    assert disabled_set() == {"emote", "metering"}
    set_disabled("emote", False)
    assert disabled_set() == {"metering"}


def test_disabled_set_handles_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Nothing written yet — file shouldn't exist, and we should get an
    # empty set without raising.
    assert not (tmp_path / ".relaydeck" / "plugins-disabled.yaml").exists()
    assert disabled_set() == set()


# ── Registry ────────────────────────────────────────────────────────


def _make_plugin(name: str, category: str = "tool") -> RelaydeckPlugin:
    """Spin up a synthetic RelaydeckPlugin instance that records load/unload
    calls and exposes an event subscription we can detect."""
    p = RelaydeckPlugin()
    p.name = name
    p.category = category
    p.version = "0.0.1"
    p.description = f"test plugin {name}"
    p.load_count = 0
    p.unload_count = 0
    p.events_seen = []

    def _on_load(ctx):
        p.load_count += 1

    def _on_unload():
        p.unload_count += 1

    def _on_test_event(event):
        p.events_seen.append(event.type)

    p.on_load = _on_load
    p.on_unload = _on_unload
    p._on_test_event = _on_test_event
    p.get_subscriptions = lambda: [EventSubscription("test.*", "_on_test_event")]
    return p


def _registry_with_two_plugins(tmp_path) -> tuple[PluginRegistry, RelaydeckPlugin, RelaydeckPlugin]:
    """Bypass disk discovery — inject two synthetic plugins straight
    into the registry's discovered map so we can test load/disable
    without standing up a real filesystem plugin tree."""
    reg = PluginRegistry(config_home=tmp_path / ".relaydeck")
    p1 = _make_plugin("alpha")
    p2 = _make_plugin("beta")
    for inst in (p1, p2):
        entry = plug.PluginEntry(
            name=inst.name, category=inst.category, version=inst.version,
            instance=inst, source="test", path=tmp_path,
        )
        reg._discovered[entry.name] = entry
    return reg, p1, p2


def _load(reg: PluginRegistry, tmp_path: Path) -> None:
    """Run load_all but with discovery short-circuited to the synthetic
    entries we injected via _discovered. We monkey-patch discover() so
    the test never touches the real plugin tree."""
    reg.discover = lambda: list(reg._discovered.values())  # type: ignore[method-assign]
    ctx = PluginContext(config_home=reg.config_home)
    reg.load_all(ctx)


def test_load_all_skips_disabled_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    set_disabled("beta", True)
    reg, p1, p2 = _registry_with_two_plugins(tmp_path)
    _load(reg, tmp_path)
    assert p1.load_count == 1, "alpha should have loaded"
    assert p2.load_count == 0, "beta is disabled — should not have loaded"
    # Both should still be in discovered_all so the UI can re-enable beta.
    names = {e.name for e in reg.discovered_all()}
    assert names == {"alpha", "beta"}
    # ... but only alpha is in the live registry.
    assert {e.name for e in reg.all()} == {"alpha"}


def test_disable_live_unloads_and_unsubscribes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    reg, p1, p2 = _registry_with_two_plugins(tmp_path)
    _load(reg, tmp_path)
    assert p2.load_count == 1 and p2.unload_count == 0

    # Send a test event — beta's handler should run.
    reg.event_bus.emit(Event(type="test.ping", data={}))
    assert "test.ping" in p2.events_seen
    p2.events_seen.clear()

    # Disable beta — on_unload runs, bus subscription drops.
    live, msg = reg.disable("beta")
    assert live is True, msg
    assert p2.unload_count == 1
    assert is_disabled("beta") is True
    assert {e.name for e in reg.all()} == {"alpha"}

    # After disable, beta's handler must NOT receive new events.
    reg.event_bus.emit(Event(type="test.ping2", data={}))
    assert p2.events_seen == [], "disabled plugin's handler should be unwired"


def test_enable_reloads_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    set_disabled("beta", True)
    reg, p1, p2 = _registry_with_two_plugins(tmp_path)
    _load(reg, tmp_path)
    assert p2.load_count == 0

    live, msg = reg.enable("beta")
    assert live is True, msg
    assert p2.load_count == 1
    assert {e.name for e in reg.all()} == {"alpha", "beta"}
    assert is_disabled("beta") is False

    # And the re-subscribed handler should receive events again.
    reg.event_bus.emit(Event(type="test.after", data={}))
    assert "test.after" in p2.events_seen


def test_enable_rolls_back_failed_on_load(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    set_disabled("beta", True)
    reg, p1, p2 = _registry_with_two_plugins(tmp_path)
    _load(reg, tmp_path)

    def _boom(ctx):
        p2.load_count += 1
        raise RuntimeError("boom")

    p2.on_load = _boom
    live, msg = reg.enable("beta")

    assert live is False
    assert "on_load failed" in msg
    assert p2.load_count == 1
    assert p2.unload_count == 1
    assert reg.get("beta") is None
    assert {e.name for e in reg.all()} == {"alpha"}
    assert is_disabled("beta") is False


def test_disable_persists_for_unknown_plugin(tmp_path, monkeypatch):
    """If a plugin isn't currently loaded (e.g. disabled at startup),
    disable() should still save the flag so the choice survives across
    a future re-discovery."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    reg = PluginRegistry(config_home=tmp_path / ".relaydeck")
    live, _msg = reg.disable("ghost-plugin")
    assert live is False  # never loaded → no live unload happened
    assert is_disabled("ghost-plugin") is True
