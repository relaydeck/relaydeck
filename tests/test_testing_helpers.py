"""
Tests for relaydeck.testing — MockBus, MockContext, make_plugin.

These helpers are the third-party plugin authoring contract. If
they break, every external plugin's test suite breaks. So treat
them as load-bearing public API and pin the behavior we promised
in the module docstring's worked example.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.plugin import (
    Event,
    EventSubscription,
    RelaydeckPlugin,
    PluginContext,
)
from relaydeck.testing import MockBus, MockContext, make_plugin


class _ToyPlugin(RelaydeckPlugin):
    name = "toy"
    category = "tool"

    def __init__(self) -> None:
        super().__init__()
        self.loaded_with: PluginContext | None = None
        self.seen: list[Event] = []

    def on_load(self, ctx: PluginContext) -> None:
        self.loaded_with = ctx

    def get_subscriptions(self) -> list[EventSubscription]:
        return [EventSubscription("agent.*", "_on_agent")]

    def _on_agent(self, event: Event) -> None:
        self.seen.append(event)


class TestMockBus:
    def test_emit_event_one_liner(self):
        bus = MockBus()
        bus.emit_event("agent.start", {"id": "demo"})
        assert len(bus.events) == 1
        assert bus.events[0].type == "agent.start"
        assert bus.events[0].data == {"id": "demo"}
        assert bus.events[0].source_plugin == "test"

    def test_assert_emitted_matches_type(self):
        bus = MockBus()
        bus.emit_event("agent.start")
        assert bus.assert_emitted("agent.start") is True
        assert bus.assert_emitted("agent.stop") is False

    def test_assert_emitted_subset_match(self):
        bus = MockBus()
        bus.emit_event("agent.start", {"id": "demo", "type": "pi"})
        assert bus.assert_emitted("agent.start", contains={"id": "demo"})
        assert bus.assert_emitted("agent.start", contains={"id": "demo", "type": "pi"})
        # Subset match — extra keys in event are fine
        # Wrong value fails
        assert not bus.assert_emitted("agent.start", contains={"id": "other"})
        # Missing key fails
        assert not bus.assert_emitted("agent.start", contains={"missing": 1})

    def test_subscribers_still_work(self):
        """The catch-all `events` mirror must not interfere with normal
        subscribe semantics."""
        bus = MockBus()
        received: list[Event] = []
        bus.subscribe("agent.*", received.append)
        bus.emit_event("agent.start")
        bus.emit_event("other")
        assert len(received) == 1
        assert received[0].type == "agent.start"


class TestMockContext:
    def test_creates_config_home_under_tmp(self, tmp_path):
        ctx = MockContext(tmp_path)
        assert ctx.config_home == tmp_path / ".relaydeck"
        assert ctx.config_home.exists()
        assert (ctx.config_home / "runtime").is_dir()

    def test_treats_relaydeck_path_as_config_home_directly(self, tmp_path):
        cfg = tmp_path / ".relaydeck"
        cfg.mkdir(parents=True)
        ctx = MockContext(cfg)
        assert ctx.config_home == cfg

    def test_provides_mock_bus_by_default(self, tmp_path):
        ctx = MockContext(tmp_path)
        assert isinstance(ctx.event_bus, MockBus)

    def test_accepts_explicit_bus(self, tmp_path):
        bus = MockBus()
        ctx = MockContext(tmp_path, bus=bus)
        assert ctx.event_bus is bus


class TestMakePlugin:
    def test_calls_on_load_with_ctx(self, tmp_path):
        p = make_plugin(_ToyPlugin, tmp_path=tmp_path)
        assert isinstance(p, _ToyPlugin)
        assert p.loaded_with is not None

    def test_wires_subscriptions_from_get_subscriptions(self, tmp_path):
        bus = MockBus()
        ctx = MockContext(tmp_path, bus=bus)
        p = make_plugin(_ToyPlugin, ctx=ctx)
        bus.emit_event("agent.start", {"id": "demo"})
        bus.emit_event("other.event", {})
        assert len(p.seen) == 1
        assert p.seen[0].data == {"id": "demo"}

    def test_wire_subscriptions_can_be_disabled(self, tmp_path):
        bus = MockBus()
        ctx = MockContext(tmp_path, bus=bus)
        p = make_plugin(_ToyPlugin, ctx=ctx, wire_subscriptions=False)
        bus.emit_event("agent.start", {})
        assert p.seen == []

    def test_creates_default_context_when_tmp_path_only(self):
        p = make_plugin(_ToyPlugin)
        assert p.loaded_with is not None

    def test_skips_missing_handlers_silently(self, tmp_path):
        """Subscription with handler name that doesn't exist on the
        plugin shouldn't crash — matches PluginRegistry behavior."""

        class _BadPlugin(RelaydeckPlugin):
            name = "bad"

            def get_subscriptions(self):
                return [EventSubscription("*", "_missing_handler")]

        # Should not raise.
        p = make_plugin(_BadPlugin, tmp_path=tmp_path)
        assert isinstance(p, _BadPlugin)
