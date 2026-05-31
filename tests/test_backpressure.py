"""
Tests for backpressure surfacing:

  - Harness PTY ring + subscriber queues report drops via pty_stats()
    and emit harness.pty_overflow events.
  - PluginEventBus times every handler call, flags slow subscribers,
    and emits bus.slow_subscriber notifications.

We exercise the bus directly (no daemon needed) and the PTY paths
through a minimal HarnessAgent stub that bypasses subprocess spawn.
"""

from __future__ import annotations

import queue
import time

import pytest

from relaydeck.plugin import Event, PluginEventBus
from relaydeck.harness import HarnessAgent


# ── Bus slow-subscriber detection ────────────────────────────────────


def test_bus_records_per_handler_stats():
    bus = PluginEventBus()
    seen: list[Event] = []

    def handler(e):
        seen.append(e)

    bus.subscribe("ping", handler)
    for i in range(3):
        bus.emit(Event(type="ping", data={"i": i}, source_plugin="test"))

    stats = bus.subscriber_stats()
    assert len(stats) == 1
    row = stats[0]
    assert row["count"] == 3
    assert row["errors"] == 0
    assert row["pattern"] == "ping"
    assert "handler" in row


def test_bus_flags_slow_subscriber(monkeypatch):
    """A handler whose mean exceeds SLOW_HANDLER_MS after SLOW_MIN_SAMPLES
    must trigger a `bus.slow_subscriber` event."""
    bus = PluginEventBus()
    # Tighten thresholds so we don't have to actually sleep 100ms × 5.
    monkeypatch.setattr(PluginEventBus, "SLOW_HANDLER_MS", 5.0)
    monkeypatch.setattr(PluginEventBus, "SLOW_MIN_SAMPLES", 3)

    def slow(e):
        time.sleep(0.010)  # 10ms — over the 5ms threshold

    bus.subscribe("ping", slow)
    notifications: list[Event] = []
    bus.subscribe("bus.slow_subscriber", lambda e: notifications.append(e))

    for i in range(5):
        bus.emit(Event(type="ping", data={"i": i}, source_plugin="test"))

    assert notifications, "expected at least one bus.slow_subscriber event"
    payload = notifications[0].data
    assert payload["mean_ms"] >= 5.0
    assert payload["pattern"] == "ping"


def test_bus_does_not_flag_fast_subscriber():
    bus = PluginEventBus()

    def fast(e):
        pass

    bus.subscribe("ping", fast)
    notifications: list[Event] = []
    bus.subscribe("bus.slow_subscriber", lambda e: notifications.append(e))

    for i in range(50):
        bus.emit(Event(type="ping", data={"i": i}, source_plugin="test"))

    assert notifications == []


def test_bus_slow_emit_is_rate_limited(monkeypatch):
    """Once a slow subscriber is flagged, repeated slow calls within
    SLOW_EVENT_INTERVAL_S must not re-emit — otherwise the bus
    drowns itself in slow-subscriber notifications."""
    bus = PluginEventBus()
    monkeypatch.setattr(PluginEventBus, "SLOW_HANDLER_MS", 5.0)
    monkeypatch.setattr(PluginEventBus, "SLOW_MIN_SAMPLES", 2)
    # Long interval so we never re-emit during the test.
    monkeypatch.setattr(PluginEventBus, "SLOW_EVENT_INTERVAL_S", 999.0)

    def slow(e):
        time.sleep(0.010)

    bus.subscribe("ping", slow)
    seen: list[Event] = []
    bus.subscribe("bus.slow_subscriber", lambda e: seen.append(e))

    for _ in range(20):
        bus.emit(Event(type="ping", data={}, source_plugin="test"))

    assert len(seen) == 1, f"expected exactly one slow-subscriber event, got {len(seen)}"


def test_bus_records_handler_errors():
    bus = PluginEventBus()

    def explode(e):
        raise RuntimeError("kaboom")

    bus.subscribe("ping", explode)
    bus.emit(Event(type="ping", data={}, source_plugin="test"))
    bus.emit(Event(type="ping", data={}, source_plugin="test"))

    stats = bus.subscriber_stats()
    assert stats[0]["errors"] == 2


def test_subscriber_stats_sorted_by_latency_desc(monkeypatch):
    bus = PluginEventBus()
    monkeypatch.setattr(PluginEventBus, "SLOW_HANDLER_MS", 9999.0)  # never slow

    def fast(e):
        pass

    def medium(e):
        time.sleep(0.003)

    bus.subscribe("ping", fast)
    bus.subscribe("ping", medium)
    for _ in range(5):
        bus.emit(Event(type="ping", data={}, source_plugin="test"))

    rows = bus.subscriber_stats()
    assert len(rows) == 2
    assert rows[0]["mean_ms"] >= rows[1]["mean_ms"]


# ── PTY backpressure surfacing ───────────────────────────────────────


@pytest.fixture
def fake_harness(tmp_path):
    """A HarnessAgent instance built without actually spawning a
    subprocess — we only need the _broadcast machinery."""
    h = HarnessAgent.__new__(HarnessAgent)
    h.agent_id = "test-agent"
    h.config = {}
    h.db_path = str(tmp_path / "relaydeck.db")
    h._proc = None
    h._master_fd = None
    import threading
    h._sub_lock = threading.Lock()
    h._subscribers = []
    h._pty_buffer = bytearray()
    h._overflow_bytes = 0
    h._subscriber_drops = 0
    h._last_overflow_event_ts = 0.0
    h._seen_output = False
    h._plugin_event_bus = None
    return h


def test_pty_buffer_overflow_counted(fake_harness):
    """Once the buffer exceeds BUFFER_BYTES, overflow_bytes must
    reflect the dropped count. Pre-fix the bytes were dropped silently."""
    # Send enough data to overflow the ring buffer twice over.
    cap = fake_harness.BUFFER_BYTES
    big = b"x" * (cap + 4096)
    fake_harness._broadcast(big)
    stats = fake_harness.pty_stats()
    assert stats["overflow_bytes"] == 4096
    assert stats["buffer_bytes"] == cap


def test_pty_subscriber_drops_counted(fake_harness):
    """If a subscriber queue is full, the broadcast drops with a
    counter increment so operators can see the lossy subscriber."""
    # Tiny queue so one chunk fills it.
    q: queue.Queue = queue.Queue(maxsize=1)
    fake_harness._subscribers.append(q)
    fake_harness._broadcast(b"first")    # fills the queue
    fake_harness._broadcast(b"second")   # full → dropped
    fake_harness._broadcast(b"third")    # full → dropped
    stats = fake_harness.pty_stats()
    assert stats["subscriber_drops"] == 2


def test_pty_overflow_emits_bus_event(fake_harness):
    """When the ring overflows, the harness should fire a
    `harness.pty_overflow` event so observability sees it."""
    bus = PluginEventBus()
    fake_harness._plugin_event_bus = bus
    seen: list[Event] = []
    bus.subscribe("harness.pty_overflow", lambda e: seen.append(e))

    cap = fake_harness.BUFFER_BYTES
    fake_harness._broadcast(b"x" * (cap + 8192))
    assert seen, "expected harness.pty_overflow event on ring overflow"
    payload = seen[0].data
    assert payload["agent_id"] == "test-agent"
    assert payload["overflow_bytes"] >= 8192


def test_pty_overflow_event_is_rate_limited(fake_harness):
    """A runaway producer must not flood the bus with overflow events
    — the harness emits one per 30s window per agent."""
    bus = PluginEventBus()
    fake_harness._plugin_event_bus = bus
    seen: list[Event] = []
    bus.subscribe("harness.pty_overflow", lambda e: seen.append(e))

    cap = fake_harness.BUFFER_BYTES
    for _ in range(20):
        fake_harness._broadcast(b"x" * (cap + 1024))
    # Should still be 1 because of the 30s rate-limit.
    assert len(seen) == 1


def test_pty_stats_shape(fake_harness):
    s = fake_harness.pty_stats()
    assert set(s.keys()) == {
        "buffer_bytes", "buffer_cap", "overflow_bytes",
        "subscriber_drops", "subscribers",
    }
