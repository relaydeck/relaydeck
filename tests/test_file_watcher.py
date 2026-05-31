"""
Tests for the file_watcher plugin.

  - Capability gates enforce the declared set.
  - workspace.added → watcher started; workspace.removed / shutdown → stopped.
  - FileWatcher emits workspace.file.{created,changed,deleted} events.
  - Ignore + match patterns work end-to-end.
  - Manifest declares the right capabilities.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from relaydeck.plugin import (
    Event,
    EventType,
    PluginContext,
    PluginEventBus,
)
from plugins.file_watcher.plugin import (
    FileWatcher,
    FileWatcherPlugin,
    PLUGIN,
    _legacy_on_load,
)
from relaydeck.sdk import CapabilityNotDeclared, PluginHost


# ── Manifest sanity ─────────────────────────────────────────────────


def test_manifest_declares_required_capabilities():
    from relaydeck.plugin_manifest import find_manifest

    pkg = Path(__file__).resolve().parent.parent / "plugins" / "file_watcher"
    m = find_manifest(pkg)
    assert m is not None
    caps = set(m.declared_capabilities)
    assert "events.subscribe" in caps
    assert "events.emit" in caps
    assert "workers.spawn" in caps


# ── FileWatcher (snapshot diff) ─────────────────────────────────────


class _StubWorker:
    """Minimal worker stand-in — captures logs, no thread."""

    def __init__(self):
        self.logs: list[str] = []

    def log(self, line: str, level: str = "info") -> None:
        self.logs.append(f"[{level}] {line}")

    def stop(self) -> None:
        pass


class _StubSpawner:
    """host.workers replacement that doesn't actually start a thread —
    captures the registration and exposes `tick()` so tests drive
    the worker deterministically."""

    def __init__(self):
        self.spawned = []
        self._target = None
        self._stub = _StubWorker()

    def spawn(self, *, name, fn, interval=None, config=None, description="", **_):
        self._target = fn
        self.spawned.append({"name": name, "interval": interval, "config": config,
                             "description": description})
        return self._stub

    def tick(self):
        if self._target is None:
            raise RuntimeError("spawn never called")
        self._target(self._stub)


@pytest.fixture
def stub_env(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    bus = PluginEventBus()
    spawner = _StubSpawner()
    watcher = FileWatcher(
        root=root,
        bus=bus,
        worker_spawner=spawner,
        # fnmatch's `*` matches across directory separators, so "*.py"
        # actually catches top-level + nested. The legacy defaults
        # include "*.md" specifically for this reason — and "**/*.x"
        # in fnmatch only matches paths that contain a slash.
        watch_patterns=["*.py", "*.md"],
        ignore_patterns=[".git/**", "ignored.md"],
        debounce_ms=50,
    )
    return root, bus, spawner, watcher


def test_first_tick_primes_without_emitting(stub_env):
    root, bus, spawner, watcher = stub_env
    (root / "a.py").write_text("first")
    received: list[Event] = []
    bus.subscribe("workspace.file.*", lambda e: received.append(e))

    watcher.start()
    spawner.tick()  # primes
    assert received == []  # priming tick must not emit


def test_created_event_on_new_file(stub_env):
    root, bus, spawner, watcher = stub_env
    received: list[Event] = []
    bus.subscribe("workspace.file.*", lambda e: received.append(e))

    watcher.start()
    spawner.tick()  # prime
    (root / "new.py").write_text("hello")
    spawner.tick()  # diff
    assert any(e.type == EventType.WORKSPACE_FILE_CREATED for e in received)
    payload = next(e for e in received if e.type == EventType.WORKSPACE_FILE_CREATED)
    assert payload.data["relative_path"] == "new.py"
    assert payload.data["root"] == str(root)


def test_changed_event_on_modify(stub_env):
    root, bus, spawner, watcher = stub_env
    (root / "x.py").write_text("v1")
    received: list[Event] = []
    bus.subscribe("workspace.file.*", lambda e: received.append(e))

    watcher.start()
    spawner.tick()  # prime
    # Force mtime change deterministically — write_text may set mtime
    # to current second, identical to the prior write.
    import os
    new_mtime = time.time() + 2.0
    os.utime(root / "x.py", (new_mtime, new_mtime))
    (root / "x.py").write_text("v2")
    os.utime(root / "x.py", (new_mtime, new_mtime))
    spawner.tick()
    assert any(e.type == EventType.WORKSPACE_FILE_CHANGED for e in received)


def test_deleted_event_on_remove(stub_env):
    root, bus, spawner, watcher = stub_env
    p = root / "gone.py"
    p.write_text("bye")
    received: list[Event] = []
    bus.subscribe("workspace.file.*", lambda e: received.append(e))

    watcher.start()
    spawner.tick()  # prime
    p.unlink()
    spawner.tick()
    assert any(e.type == EventType.WORKSPACE_FILE_DELETED for e in received)


def test_ignored_patterns_excluded(stub_env):
    root, bus, spawner, watcher = stub_env
    received: list[Event] = []
    bus.subscribe("workspace.file.*", lambda e: received.append(e))

    watcher.start()
    spawner.tick()
    (root / "ignored.md").write_text("nope")
    (root / "kept.md").write_text("yes")
    spawner.tick()
    paths = [e.data["relative_path"] for e in received]
    assert "kept.md" in paths
    assert "ignored.md" not in paths


def test_non_watch_pattern_excluded(stub_env):
    """A file outside the watch_patterns set should never produce an event."""
    root, bus, spawner, watcher = stub_env
    received: list[Event] = []
    bus.subscribe("workspace.file.*", lambda e: received.append(e))

    watcher.start()
    spawner.tick()
    (root / "binary.dat").write_text("blob")  # not matched by *.py / *.md
    spawner.tick()
    paths = [e.data["relative_path"] for e in received]
    assert "binary.dat" not in paths


# ── Plugin lifecycle via SDK ────────────────────────────────────────


@pytest.fixture
def ctx(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    return PluginContext(
        config_home=cfg,
        workspace_path=None,
        event_bus=PluginEventBus(),
        orchestrator=None,
    )


def test_capability_gate_refuses_undeclared_emit(tmp_path):
    """If the plugin's manifest forgets events.emit, the capability
    gate must refuse."""
    host = PluginHost(
        name="file-watcher",
        config_home=tmp_path,
        declared_capabilities=["events.subscribe", "workers.spawn"],  # NO emit
        event_bus=PluginEventBus(),
    )
    with pytest.raises(CapabilityNotDeclared):
        host.events.emit("workspace.file.created", {})


def test_legacy_on_load_subscribes(ctx):
    bus = ctx.event_bus
    _legacy_on_load(ctx)
    patterns = [p for p, _ in bus._subscriptions]
    assert "workspace.added" in patterns
    assert "workspace.removed" in patterns
    assert "system.shutdown" in patterns


def test_workspace_added_starts_watcher(ctx, tmp_path):
    """A workspace.added event should land in a new FileWatcher
    entry and the plugin's worker should be spawned."""
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _legacy_on_load(ctx)
    ctx.event_bus.emit(Event(
        type="workspace.added",
        data={"name": "demo", "path": str(ws_root)},
        source_plugin="test",
    ))
    # PLUGIN is the singleton from the module; check it has a watcher.
    assert "demo" in PLUGIN._watchers
    # Cleanup so subsequent tests don't see leftover state.
    PLUGIN.on_unload()


def test_workspace_removed_stops_watcher(ctx, tmp_path):
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _legacy_on_load(ctx)
    ctx.event_bus.emit(Event(
        type="workspace.added",
        data={"name": "demo2", "path": str(ws_root)},
        source_plugin="test",
    ))
    assert "demo2" in PLUGIN._watchers

    ctx.event_bus.emit(Event(
        type="workspace.removed",
        data={"name": "demo2"},
        source_plugin="test",
    ))
    assert "demo2" not in PLUGIN._watchers


def test_workspace_added_idempotent(ctx, tmp_path):
    """Re-emitting workspace.added for the same name must not double-spawn."""
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _legacy_on_load(ctx)
    for _ in range(3):
        ctx.event_bus.emit(Event(
            type="workspace.added",
            data={"name": "dup", "path": str(ws_root)},
            source_plugin="test",
        ))
    assert "dup" in PLUGIN._watchers
    # Only one watcher object.
    assert len([w for k, w in PLUGIN._watchers.items() if k == "dup"]) == 1
    PLUGIN.on_unload()


def test_system_shutdown_clears_all(ctx, tmp_path):
    ws1 = tmp_path / "ws1"; ws1.mkdir()
    ws2 = tmp_path / "ws2"; ws2.mkdir()
    _legacy_on_load(ctx)
    for name, path in [("a", ws1), ("b", ws2)]:
        ctx.event_bus.emit(Event(
            type="workspace.added",
            data={"name": name, "path": str(path)},
            source_plugin="test",
        ))
    ctx.event_bus.emit(Event(
        type="system.shutdown",
        data={},
        source_plugin="test",
    ))
    assert PLUGIN._watchers == {}


def test_missing_workspace_path_is_noop(ctx, tmp_path):
    """A workspace.added with a path that doesn't exist must not
    raise — it just gets skipped."""
    _legacy_on_load(ctx)
    ctx.event_bus.emit(Event(
        type="workspace.added",
        data={"name": "ghost", "path": str(tmp_path / "does-not-exist")},
        source_plugin="test",
    ))
    assert "ghost" not in PLUGIN._watchers


# ── EventBusHost adapter: pre-built Event must not nest ──────────────
#
# Regression for a silent production-only failure: the file_watcher is
# wired in production with `host.events` (the SDK EventBusHost), but the
# tests above inject a *raw* PluginEventBus. EventBusHost.emit takes
# (event_type, data) and builds the Event itself, so a pre-built Event
# passed in got nested as the *type* — which broke both bus_events
# persistence and `workspace.file.*` subscriber matching, with no error.
# The raw-bus fixtures couldn't catch it; these exercise the real path.


def test_event_bus_host_forwards_prebuilt_event():
    from relaydeck.sdk import EventBusHost, _CapabilityGate

    raw = PluginEventBus()
    got: list[Event] = []
    raw.subscribe("workspace.file.*", lambda e: got.append(e))
    host_bus = EventBusHost("file-watcher", raw, _CapabilityGate(["events.emit"]))

    # The raw-bus idiom: a fully-built Event. Must forward as-is.
    host_bus.emit(Event(type="workspace.file.changed",
                        data={"path": "/x"}, source_plugin="file-watcher"))
    assert len(got) == 1
    assert isinstance(got[0].type, str), "event type must stay a string, not a nested Event"
    assert got[0].type == "workspace.file.changed"
    assert got[0].data == {"path": "/x"}

    # The documented (event_type, data) signature still works + stamps source.
    got.clear()
    host_bus.emit("workspace.file.created", {"path": "/y"})
    assert got and got[0].type == "workspace.file.created"
    assert got[0].source_plugin == "file-watcher"


def test_file_watcher_emits_through_event_bus_host(tmp_path):
    """Production wiring: FileWatcher built with an EventBusHost (host.events),
    not a raw bus. Pin that file events arrive with a string type + full
    data through that adapter — the path where the nesting bug lived."""
    from relaydeck.sdk import EventBusHost, _CapabilityGate

    root = tmp_path / "ws"
    root.mkdir()
    raw = PluginEventBus()
    received: list[Event] = []
    raw.subscribe("workspace.file.*", lambda e: received.append(e))
    host_bus = EventBusHost("file-watcher", raw, _CapabilityGate(["events.emit"]))

    spawner = _StubSpawner()
    watcher = FileWatcher(
        root=root, bus=host_bus, worker_spawner=spawner,
        watch_patterns=["*.py", "*.md"], ignore_patterns=[], debounce_ms=50,
    )
    watcher.start()
    spawner.tick()  # prime
    (root / "new.py").write_text("hi")
    spawner.tick()  # diff → emit

    assert received, "no event delivered through EventBusHost (nesting regression?)"
    ev = received[0]
    assert isinstance(ev.type, str) and ev.type.startswith("workspace.file."), (
        f"event type must be a string, got {type(ev.type).__name__}: {ev.type!r}"
    )
    assert ev.data["relative_path"] == "new.py"
    assert ev.data["root"] == str(root)
