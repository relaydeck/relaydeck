"""
Tests for the observability layer:

  - Prometheus registry: counter/gauge math, label-equivalence,
    exposition format correctness.
  - Built-in series wired at message + bus + worker + PTY touch points.
  - /metrics endpoint serves Prometheus text and stays public.
  - SDK host.metrics surface enforces metrics.register capability,
    auto-prefixes series names, exposes inc/set facades.
  - JSON logging formatter emits one valid JSON object per record.
"""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from relaydeck.metrics import (
    _REGISTRY,
    configure_json_logging,
    init_builtin_series,
    record_bus_event,
    record_message_state,
    record_usage_event,
    registry,
    set_agents_gauge,
)


@pytest.fixture
def clean_registry():
    """Start every test with an empty registry so we can assert exact
    counter values."""
    _REGISTRY.clear()
    yield _REGISTRY
    _REGISTRY.clear()


# ── Counter / gauge math ────────────────────────────────────────────


def test_counter_increments_per_label_set(clean_registry):
    c = clean_registry.counter("test_total", "test counter")
    c.add(1.0, {"state": "queued"})
    c.add(2.0, {"state": "queued"})
    c.add(5.0, {"state": "failed"})
    snap = dict(c.snapshot())
    assert snap[(("state", "queued"),)] == 3.0
    assert snap[(("state", "failed"),)] == 5.0


def test_label_order_independence(clean_registry):
    """`{a=1, b=2}` and `{b=2, a=1}` must hit the same series."""
    c = clean_registry.counter("t")
    c.add(1.0, {"a": "1", "b": "2"})
    c.add(1.0, {"b": "2", "a": "1"})
    snap = dict(c.snapshot())
    assert len(snap) == 1
    assert next(iter(snap.values())) == 2.0


def test_gauge_set_replaces_value(clean_registry):
    g = clean_registry.gauge("test_gauge")
    g.set(5.0, {"status": "running"})
    g.set(2.0, {"status": "running"})
    g.set(3.0, {"status": "stopped"})
    snap = dict(g.snapshot())
    assert snap[(("status", "running"),)] == 2.0
    assert snap[(("status", "stopped"),)] == 3.0


def test_counter_and_gauge_same_name_rejected(clean_registry):
    clean_registry.counter("dup")
    with pytest.raises(ValueError, match="already registered"):
        clean_registry.gauge("dup")


# ── Exposition format ───────────────────────────────────────────────


def test_prometheus_render_includes_help_and_type(clean_registry):
    c = clean_registry.counter("foo_total", "Total foos.")
    c.add(7, {"status": "ok"})
    text = clean_registry.render_prometheus()
    assert "# HELP foo_total Total foos." in text
    assert "# TYPE foo_total counter" in text
    assert 'foo_total{status="ok"} 7' in text


def test_prometheus_render_quotes_label_values(clean_registry):
    c = clean_registry.counter("q_total")
    c.add(1, {"label": 'with "quote" and \\backslash'})
    text = clean_registry.render_prometheus()
    # Backslash + quote get escaped per Prometheus spec.
    assert '\\"quote\\"' in text
    assert "\\\\backslash" in text


def test_prometheus_render_orders_lines_stably(clean_registry):
    """Two consecutive renders of an unchanged registry must produce
    byte-identical output — important for diff-based testing."""
    c = clean_registry.counter("a")
    c.add(1, {"x": "1"})
    c.add(1, {"x": "2"})
    assert clean_registry.render_prometheus() == clean_registry.render_prometheus()


def test_renders_trailing_newline(clean_registry):
    """Prometheus format requires a final newline so the last line
    isn't dropped by line-buffered readers."""
    clean_registry.counter("x").add(1)
    text = clean_registry.render_prometheus()
    assert text.endswith("\n")


# ── Built-in series wiring ──────────────────────────────────────────


def test_init_builtin_series_registers_documented_metrics(clean_registry):
    init_builtin_series()
    text = clean_registry.render_prometheus()
    for series_name in (
        "relaydeck_messages_total",
        "relaydeck_agents",
        "relaydeck_usage_tokens_total",
        "relaydeck_bus_events_total",
        "relaydeck_worker_restarts_total",
    ):
        assert f"# HELP {series_name}" in text


def test_record_message_state_bumps_counter(clean_registry):
    init_builtin_series()
    record_message_state("queued")
    record_message_state("queued")
    record_message_state("delivered")
    text = clean_registry.render_prometheus()
    assert 'relaydeck_messages_total{state="queued"} 2' in text
    assert 'relaydeck_messages_total{state="delivered"} 1' in text


def test_record_usage_event_splits_prompt_completion(clean_registry):
    init_builtin_series()
    record_usage_event("anthropic", "claude-sonnet-4", prompt=1000, completion=500)
    text = clean_registry.render_prometheus()
    assert 'kind="prompt"' in text
    assert 'kind="completion"' in text
    # Prompt is exactly 1000
    assert '1000' in text and '500' in text


def test_set_agents_gauge_clears_stale_statuses(clean_registry):
    init_builtin_series()
    set_agents_gauge({"running": 3, "stopped": 1})
    set_agents_gauge({"running": 0, "stopped": 2})  # one less running
    text = clean_registry.render_prometheus()
    # Running must surface 0 explicitly, not vanish
    assert 'relaydeck_agents{status="running"} 0' in text
    assert 'relaydeck_agents{status="stopped"} 2' in text


# ── /metrics endpoint ───────────────────────────────────────────────


def test_metrics_endpoint_is_public(tmp_path, monkeypatch, clean_registry):
    """The /metrics endpoint must serve unauthenticated — scrapers
    typically don't manage bearer tokens, and the metric values
    aren't sensitive."""
    from fastapi.testclient import TestClient
    from relaydeck.transports.api import create_app

    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    monkeypatch.setenv("RELAYDECK_CONFIG_HOME", str(cfg_home))
    init_builtin_series()
    record_message_state("queued")

    app = create_app(cfg_home)
    client = TestClient(app)
    # Strip the auto-bearer to confirm public access
    r = client.get("/metrics", headers={"Authorization": ""})
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "relaydeck_messages_total" in r.text


def test_metrics_endpoint_renders_live_agent_gauge(tmp_path, monkeypatch, clean_registry):
    """The endpoint should refresh the agents gauge on each scrape
    from orchestrator state — otherwise gauges would always reflect
    the last write rather than current truth."""
    from fastapi.testclient import TestClient
    from relaydeck.transports.api import create_app

    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    monkeypatch.setenv("RELAYDECK_CONFIG_HOME", str(cfg_home))
    init_builtin_series()

    app = create_app(cfg_home)
    client = TestClient(app)
    r = client.get("/metrics", headers={"Authorization": ""})
    # `relaydeck_agents` series exists (no agents → no labels).
    assert "relaydeck_agents" in r.text


# ── SDK host.metrics surface ────────────────────────────────────────


def test_metrics_register_capability_required(tmp_path, clean_registry):
    """Without `metrics.register` in declared_capabilities, the call
    raises — the capability gate is the trust boundary."""
    from relaydeck.plugin import PluginEventBus
    from relaydeck.sdk import PluginHost, CapabilityNotDeclared

    host = PluginHost(
        name="test-plugin",
        config_home=tmp_path,
        declared_capabilities=["events.subscribe"],  # NO metrics.register
        event_bus=PluginEventBus(),
    )
    with pytest.raises(CapabilityNotDeclared):
        host.metrics.counter("foo_total")


def test_metrics_series_name_is_prefixed_with_plugin(tmp_path, clean_registry):
    from relaydeck.plugin import PluginEventBus
    from relaydeck.sdk import PluginHost

    host = PluginHost(
        name="usage-limits",
        config_home=tmp_path,
        declared_capabilities=["metrics.register"],
        event_bus=PluginEventBus(),
    )
    c = host.metrics.counter("threshold_total")
    c.inc(1.0, {"window": "session"})
    text = clean_registry.render_prometheus()
    # Plugin name is normalized (dashes → underscores) and prefixed
    assert "usage_limits_threshold_total" in text


def test_metrics_counter_rejects_negative_increment(tmp_path, clean_registry):
    """Counters are monotonic — `inc(-1)` is a bug, surface it loud."""
    from relaydeck.plugin import PluginEventBus
    from relaydeck.sdk import PluginHost

    host = PluginHost(
        name="p",
        config_home=tmp_path,
        declared_capabilities=["metrics.register"],
        event_bus=PluginEventBus(),
    )
    c = host.metrics.counter("c")
    with pytest.raises(ValueError, match="non-negative"):
        c.inc(-1.0)


def test_metrics_gauge_inc_and_set(tmp_path, clean_registry):
    from relaydeck.plugin import PluginEventBus
    from relaydeck.sdk import PluginHost

    host = PluginHost(
        name="p",
        config_home=tmp_path,
        declared_capabilities=["metrics.register"],
        event_bus=PluginEventBus(),
    )
    g = host.metrics.gauge("g")
    g.set(5.0)
    g.inc(2.0)
    snap = dict(clean_registry.gauge("p_g").snapshot())
    assert snap[()] == 7.0


# ── Bus + PTY integration ───────────────────────────────────────────


def test_bus_emit_increments_bus_events_total(clean_registry):
    from relaydeck.plugin import Event, PluginEventBus
    init_builtin_series()
    bus = PluginEventBus()
    bus.emit(Event(type="agent.start", data={}, source_plugin="test"))
    bus.emit(Event(type="agent.start", data={}, source_plugin="test"))
    bus.emit(Event(type="agent.stop", data={}, source_plugin="test"))
    text = clean_registry.render_prometheus()
    assert 'relaydeck_bus_events_total{type="agent.start"} 2' in text
    assert 'relaydeck_bus_events_total{type="agent.stop"} 1' in text


def test_message_insert_increments_queued_counter(clean_registry, tmp_path):
    """The hot-path: messages.insert_message should bump the queued
    counter. End-to-end check that the wiring lands."""
    init_builtin_series()
    from relaydeck.messages import insert_message
    db = str(tmp_path / "m.db")
    insert_message("user", "alice", "hi", db_path=db)
    insert_message("user", "bob", "hi", db_path=db)
    text = clean_registry.render_prometheus()
    assert 'relaydeck_messages_total{state="queued"} 2' in text


# ── JSON logging ────────────────────────────────────────────────────


def test_json_logger_emits_valid_json_per_record(clean_registry):
    """RELAYDECK_LOG_FORMAT=json: every record is one JSON object per line
    with ts, level, logger, msg at minimum."""
    buf = StringIO()
    configure_json_logging(stream=buf)
    # Pin the root level so pytest's logging fixtures don't filter
    # us out — production uses INFO by default but the test env can
    # be set to WARNING by pytest.ini.
    logging.getLogger().setLevel(logging.INFO)
    logger = logging.getLogger("relaydeck.test_obs")
    logger.setLevel(logging.INFO)
    logger.info("hello %s", "world", extra={"agent_id": "alice"})

    lines = [ln for ln in buf.getvalue().strip().splitlines() if ln]
    assert lines, f"expected at least one JSON line, got: {buf.getvalue()!r}"
    payload = json.loads(lines[-1])
    assert payload["level"] == "INFO"
    assert payload["logger"] == "relaydeck.test_obs"
    assert payload["msg"] == "hello world"
    assert payload["agent_id"] == "alice"
    assert "ts" in payload


def test_json_logger_handles_unserializable_extra(clean_registry):
    """A non-JSON-able `extra` field shouldn't crash the formatter —
    fall back to repr()."""
    buf = StringIO()
    configure_json_logging(stream=buf)
    logging.getLogger().setLevel(logging.INFO)

    class Weird:
        def __repr__(self):
            return "<Weird>"

    logger = logging.getLogger("relaydeck.test_obs2")
    logger.setLevel(logging.WARNING)
    logger.warning("x", extra={"obj": Weird()})
    lines = [ln for ln in buf.getvalue().strip().splitlines() if ln]
    assert lines
    payload = json.loads(lines[-1])
    assert payload["obj"] == "<Weird>"
