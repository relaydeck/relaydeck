"""
Usage-limits showcase plugin tests.

Two layers:
  1. **Core window math** — pure, no I/O. Most coverage lives here
     because the behavior we actually care about is "do windows roll
     correctly, does the warn threshold fire at the right %".
  2. **Plugin integration** — boot the plugin against a real
     PluginContext + orchestrator + DB, fire `usage.record` events,
     observe state queries / emitted threshold events / auto-pause.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from relaydeck.plugin import Event, PluginContext, PluginEventBus
from plugins.usage_limits.core import (
    WindowConfig,
    compute_window_state,
    fmt_eta,
    resolve_agent_budget,
)
from plugins.usage_limits.plugin import (
    UsageLimitsPlugin,
    _DEFAULTS,
    _legacy_on_load,
)


# ── Core window math ─────────────────────────────────────────────────


def test_empty_window_is_ok_when_budgeted():
    cfg = WindowConfig(name="session", duration_hours=5, budget_tokens=10_000)
    s = compute_window_state(config=cfg, records=[], now_ts=1000.0)
    assert s.used_tokens == 0
    assert s.pct_used == 0.0
    assert s.state == "ok"
    assert s.seconds_until_reset == 0  # nothing to wait on


def test_disabled_when_budget_is_zero():
    cfg = WindowConfig(name="session", duration_hours=5, budget_tokens=0)
    s = compute_window_state(
        config=cfg,
        records=[(1000.0, 50_000)],
        now_ts=1001.0,
    )
    assert s.state == "disabled"
    assert s.is_disabled() is True
    # used_tokens is still computed for visibility
    assert s.used_tokens == 50_000


def test_warn_threshold_fires_at_75_pct():
    cfg = WindowConfig(name="session", duration_hours=5, budget_tokens=10_000)
    s = compute_window_state(
        config=cfg,
        records=[(1000.0, 7500)],
        now_ts=1010.0,
        warn_threshold_pct=75.0,
    )
    assert s.state == "warn"
    assert s.pct_used == 75.0


def test_warn_threshold_configurable():
    cfg = WindowConfig(name="session", duration_hours=5, budget_tokens=10_000)
    s = compute_window_state(
        config=cfg,
        records=[(1000.0, 5000)],
        now_ts=1010.0,
        warn_threshold_pct=50.0,
    )
    assert s.state == "warn"


def test_exceeded_when_over_budget():
    cfg = WindowConfig(name="session", duration_hours=5, budget_tokens=10_000)
    s = compute_window_state(
        config=cfg,
        records=[(1000.0, 12_000)],
        now_ts=1010.0,
    )
    assert s.state == "exceeded"
    assert s.pct_used > 100.0


def test_old_records_fall_out_of_window():
    """An event from 6 hours ago must not count against a 5-hour window."""
    cfg = WindowConfig(name="session", duration_hours=5, budget_tokens=10_000)
    now = 100_000.0
    six_hours_ago = now - 6 * 3600
    one_hour_ago = now - 1 * 3600
    s = compute_window_state(
        config=cfg,
        records=[(six_hours_ago, 8000), (one_hour_ago, 100)],
        now_ts=now,
    )
    # Only the 1-hour-old record counts
    assert s.used_tokens == 100
    assert s.state == "ok"


def test_reset_at_is_oldest_in_window_plus_duration():
    cfg = WindowConfig(name="session", duration_hours=5, budget_tokens=10_000)
    now = 100_000.0
    one_hour_ago = now - 3600
    s = compute_window_state(
        config=cfg,
        records=[(one_hour_ago, 5000)],
        now_ts=now,
    )
    # Oldest record + 5 hours = reset time
    assert s.reset_at_ts == pytest.approx(one_hour_ago + 5 * 3600)
    # Resets in 4 hours
    assert s.seconds_until_reset == pytest.approx(4 * 3600)


# ── Per-agent overrides ──────────────────────────────────────────────


def test_override_absolute_replaces_base():
    assert resolve_agent_budget(
        base_budget=10_000, agent_overrides={"absolute": 50_000},
    ) == 50_000


def test_override_multiplier_scales_base():
    assert resolve_agent_budget(
        base_budget=10_000, agent_overrides={"multiplier": 2.5},
    ) == 25_000


def test_override_absolute_takes_precedence_over_multiplier():
    """If both are set, absolute wins — it's the more explicit form."""
    assert resolve_agent_budget(
        base_budget=10_000,
        agent_overrides={"absolute": 100, "multiplier": 99.0},
    ) == 100


def test_no_override_returns_base():
    assert resolve_agent_budget(base_budget=10_000, agent_overrides=None) == 10_000
    assert resolve_agent_budget(base_budget=10_000, agent_overrides={}) == 10_000


def test_negative_override_ignored():
    """Defensive: a malformed override doesn't allow negative budgets."""
    assert resolve_agent_budget(
        base_budget=10_000, agent_overrides={"absolute": -5},
    ) == 10_000


# ── ETA formatting ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "secs,expected",
    [
        (0, "0s"),
        (45, "45s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (3700, "1h 1m"),
        (86_400, "1d"),
        (86_400 + 3700, "1d 1h"),
    ],
)
def test_fmt_eta(secs, expected):
    assert fmt_eta(secs) == expected


# ── Plugin integration ──────────────────────────────────────────────


@pytest.fixture
def plugin_ctx(tmp_path, monkeypatch):
    """Plugin context backed by a real DB and a real event bus, with
    a mocked orchestrator that satisfies the plugin's `host.agents`
    needs without spinning up real harnesses."""
    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    runtime = cfg_home / "runtime"
    runtime.mkdir()

    # Mock orchestrator: list_agents returns one agent; get_agent / _load_spec
    # used by the per-agent override resolver. The MagicMock keeps the
    # surface tiny — we don't need a real orchestrator for these tests.
    orch = MagicMock()
    orch.list_agents.return_value = [
        {"id": "alice", "workspace": "demo", "status": "running"},
    ]
    orch.get_agent.return_value = {"id": "alice", "workspace": "demo"}

    # `_load_spec(...)` returns an object with a `.config` dict so per-
    # agent overrides can be exercised. Default: empty config.
    spec_obj = MagicMock()
    spec_obj.config = {}
    orch._load_spec.return_value = spec_obj

    orch.stop_agent.return_value = True

    bus = PluginEventBus()
    ctx = PluginContext(
        config_home=cfg_home,
        workspace_path=None,
        event_bus=bus,
        orchestrator=orch,
    )
    return ctx, orch, bus, str(runtime / "relaydeck.db")


def _emit_usage(bus: PluginEventBus, *, agent_id: str, tokens: int, ts: float | None = None):
    """Fire a `usage.record` event with the shape harnesses actually emit."""
    bus.emit(Event(
        type="usage.record",
        data={
            "agent_id": agent_id,
            "model": "test-model",
            "provider": "test-provider",
            "prompt": tokens,
            "completion": 0,
            "cost_usd": 0.0,
        },
        source_plugin="test",
    ))


def _seed_usage(db_path: str, agent_id: str, total_tokens: int, ts: float | None = None):
    """Write a usage_records row directly. The plugin reads this table
    via the metering plugin's prior writes; in unit tests we bypass
    the metering subscriber and seed directly."""
    from relaydeck.db import open_db, record_usage

    conn = open_db(db_path)
    try:
        record_usage(
            conn,
            agent_id=agent_id,
            session_id=f"session:{agent_id}",
            model="test-model",
            provider="test-provider",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=total_tokens,
        )
        if ts is not None:
            # Override the wall-clock ts the helper set (it uses now()).
            conn.execute(
                "UPDATE usage_records SET ts = ? "
                "WHERE id = (SELECT id FROM usage_records ORDER BY id DESC LIMIT 1)",
                (ts,),
            )
            conn.commit()
    finally:
        conn.close()


def test_plugin_boots_and_subscribes(plugin_ctx):
    ctx, _, bus, _ = plugin_ctx
    _legacy_on_load(ctx)
    # bus._subscriptions is list[tuple[pattern, handler]] — look for ours.
    patterns = [p for p, _ in bus._subscriptions]
    assert "usage.record" in patterns


def test_state_for_returns_both_windows(plugin_ctx):
    ctx, _, _, db_path = plugin_ctx
    plugin = _legacy_on_load(ctx)
    state = plugin.state_for("alice")
    assert set(state.keys()) == {"session", "weekly"}


def test_state_picks_up_seeded_usage(plugin_ctx):
    ctx, _, _, db_path = plugin_ctx
    PLUGIN = _legacy_on_load(ctx)  # noqa: F811 — see _legacy_on_load docstring
    # Sanity: the plugin must be bound to OUR ctx's db, not a leftover
    # one from an earlier test's load_all.
    assert PLUGIN.db_path == db_path, (
        f"plugin db_path drift — expected {db_path}, got {PLUGIN.db_path}"
    )

    now = time.time()
    _seed_usage(db_path, "alice", 5000, ts=now - 600)  # 10 minutes ago
    state = PLUGIN.state_for("alice")
    assert state["session"].used_tokens == 5000
    assert state["weekly"].used_tokens == 5000


def test_threshold_event_emitted_on_warn(plugin_ctx, monkeypatch):
    ctx, _, bus, db_path = plugin_ctx
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_SESSION_TOKEN_BUDGET", "10000")
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_WARN_THRESHOLD_PCT", "50.0")
    _legacy_on_load(ctx)

    seen: list[Event] = []
    bus.subscribe("usage_limits.threshold", lambda e: seen.append(e))

    now = time.time()
    _seed_usage(db_path, "alice", 6000, ts=now - 60)  # 60% of 10k
    _emit_usage(bus, agent_id="alice", tokens=1)

    # At least one threshold event for the session window. (Weekly is
    # disabled by default budget=0, so it stays silent.)
    threshold_events = [
        e for e in seen
        if e.data.get("window") == "session" and e.data.get("state") == "warn"
    ]
    assert threshold_events, f"expected warn event, got: {[e.data for e in seen]}"


def test_exceeded_event_emitted_on_overflow(plugin_ctx, monkeypatch):
    ctx, _, bus, db_path = plugin_ctx
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_SESSION_TOKEN_BUDGET", "1000")
    _legacy_on_load(ctx)

    seen: list[Event] = []
    bus.subscribe("usage_limits.exceeded", lambda e: seen.append(e))

    now = time.time()
    _seed_usage(db_path, "alice", 2000, ts=now - 60)
    _emit_usage(bus, agent_id="alice", tokens=1)

    assert any(e.data.get("state") == "exceeded" for e in seen)


def test_pause_at_limit_stops_agent(plugin_ctx, monkeypatch):
    ctx, orch, bus, db_path = plugin_ctx
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_SESSION_TOKEN_BUDGET", "1000")
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_PAUSE_AT_LIMIT", "true")
    _legacy_on_load(ctx)

    now = time.time()
    _seed_usage(db_path, "alice", 2000, ts=now - 60)
    _emit_usage(bus, agent_id="alice", tokens=1)

    orch.stop_agent.assert_called_with("alice")


def test_pause_off_does_not_stop_agent(plugin_ctx, monkeypatch):
    ctx, orch, bus, db_path = plugin_ctx
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_SESSION_TOKEN_BUDGET", "1000")
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_PAUSE_AT_LIMIT", "false")
    _legacy_on_load(ctx)

    now = time.time()
    _seed_usage(db_path, "alice", 2000, ts=now - 60)
    _emit_usage(bus, agent_id="alice", tokens=1)

    orch.stop_agent.assert_not_called()


def test_per_agent_override_increases_budget(plugin_ctx, monkeypatch):
    """An agent with `config.usage_limits.session.absolute = 50_000`
    should NOT be considered exceeded at 10_000 tokens even though the
    plugin-wide budget is 1_000."""
    ctx, orch, bus, db_path = plugin_ctx
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_SESSION_TOKEN_BUDGET", "1000")
    plugin = _legacy_on_load(ctx)

    # Override the spec the plugin reads
    spec = MagicMock()
    spec.config = {"usage_limits": {"session": {"absolute": 50_000}}}
    orch._load_spec.return_value = spec

    now = time.time()
    _seed_usage(db_path, "alice", 10_000, ts=now - 60)
    state = plugin.state_for("alice")
    assert state["session"].state == "ok"
    assert state["session"].budget_tokens == 50_000


def test_threshold_not_re_emitted_on_repeat(plugin_ctx, monkeypatch):
    """Once a window is `warn`, the plugin should not re-emit
    `usage_limits.threshold` on every subsequent usage event — that
    would be noisy for downstream subscribers."""
    ctx, _, bus, db_path = plugin_ctx
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_SESSION_TOKEN_BUDGET", "10000")
    _legacy_on_load(ctx)

    seen: list[Event] = []
    bus.subscribe("usage_limits.threshold", lambda e: seen.append(e))

    now = time.time()
    _seed_usage(db_path, "alice", 8000, ts=now - 60)
    _emit_usage(bus, agent_id="alice", tokens=1)
    first_count = len(seen)
    _emit_usage(bus, agent_id="alice", tokens=1)
    _emit_usage(bus, agent_id="alice", tokens=1)
    assert len(seen) == first_count  # no duplicates


def test_no_double_count_under_normal_load_order(plugin_ctx, monkeypatch):
    """Regression for the second-round find: under the normal
    dependency-ordered load (metering subscribes first, writes the
    usage_records row, then usage-limits subscribes and reads it),
    usage-limits used to ALSO fold the in-flight event into its own
    rollup — double-counting. The fix removes the fold and relies on
    the metering dependency for ordering.

    To model "metering already wrote the row," seed the usage_records
    table *first* (mirroring what metering's subscriber will have
    done by the time usage-limits's subscriber runs), then emit the
    event. The reported used_tokens must equal the seeded value, not
    twice it.
    """
    ctx, _, bus, db_path = plugin_ctx
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_SESSION_TOKEN_BUDGET", "1000")
    plugin = _legacy_on_load(ctx)

    seen: list[Event] = []
    bus.subscribe("usage_limits.threshold", lambda e: seen.append(e))
    bus.subscribe("usage_limits.exceeded", lambda e: seen.append(e))

    # "metering already wrote the row" — this is what the dependency
    # ordering guarantees in production.
    _seed_usage(db_path, "alice", 600)

    bus.emit(Event(
        type="usage.record",
        data={
            "agent_id": "alice",
            "model": "m", "provider": "p",
            "prompt": 400, "completion": 200,  # 600 total — same event
        },
        source_plugin="test",
    ))

    # 600 ≤ 1000 budget → must NOT trigger exceeded. If the fold
    # regressed, used would be 1200 and exceeded would fire.
    for e in seen:
        assert e.data.get("state") != "exceeded", (
            f"double-counted: emitted exceeded with used_tokens="
            f"{e.data.get('used_tokens')} but only 600 should be counted"
        )

    # And the persisted view matches the same 600.
    state = plugin.state_for("alice")
    assert state["session"].used_tokens == 600


def test_state_does_not_double_count_event_with_existing_record(plugin_ctx, monkeypatch):
    """Tighter shape of the same regression at the math layer:
    state_for should be a pure read of usage_records and never fold
    in any 'in-flight' event payload. Calling state_for from a CLI
    after metering has written must not inflate the rollup."""
    ctx, _, _, db_path = plugin_ctx
    monkeypatch.setenv("RELAYDECK_USAGE_LIMITS_SESSION_TOKEN_BUDGET", "1000")
    plugin = _legacy_on_load(ctx)

    _seed_usage(db_path, "alice", 600)
    state = plugin.state_for("alice")
    assert state["session"].used_tokens == 600
    assert state["session"].state == "ok"


def test_host_agents_stop_returns_bool_from_real_orchestrator(tmp_path, monkeypatch):
    """Regression for the find: `host.agents.stop()` wrapped
    `orchestrator.stop_agent()` in `bool(...)`, but `stop_agent`
    returned None, so plugins always saw False — useless for
    deciding whether the stop actually did anything.

    Now `stop_agent` returns True when an instance was terminated and
    False when there was nothing live to stop. SDK plugins can branch
    on the answer.
    """
    from relaydeck.orchestrator import Orchestrator
    from relaydeck.sdk import PluginHost
    from relaydeck.plugin import PluginEventBus

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("RELAYDECK_CONFIG_HOME", str(cfg))
    orch = Orchestrator(cfg)
    host = PluginHost(
        name="stop-test",
        config_home=cfg,
        declared_capabilities=["agents.stop", "agents.list"],
        event_bus=PluginEventBus(),
        orchestrator=orch,
    )
    # No agent named "ghost" → nothing to stop. Old behavior would
    # have returned False after going through bool(None); the new
    # contract returns False *meaningfully* and the DB reconcile
    # still ran.
    assert host.agents.stop("ghost") is False


def test_usage_limits_declares_metering_dependency():
    """The manifest must declare metering as a dependency so the
    plugin loader orders subscriber wiring deterministically. Pairs
    with the in-flight-fold above as belt-and-suspenders."""
    from relaydeck.plugin_manifest import find_manifest
    pkg = Path(__file__).resolve().parent.parent / "plugins" / "usage_limits"
    m = find_manifest(pkg)
    assert m is not None
    assert "metering" in m.dependencies


def test_disabled_window_emits_nothing(plugin_ctx):
    """Budget 0 = disabled. Never emit threshold/exceeded events for it."""
    ctx, _, bus, db_path = plugin_ctx
    _legacy_on_load(ctx)

    seen: list[Event] = []
    bus.subscribe("usage_limits.threshold", lambda e: seen.append(e))
    bus.subscribe("usage_limits.exceeded", lambda e: seen.append(e))

    _seed_usage(db_path, "alice", 1_000_000)
    _emit_usage(bus, agent_id="alice", tokens=1)

    assert seen == []


# ── UI registration ──────────────────────────────────────────────────


def test_no_duplicate_ui_entries_when_loaded_via_adapter(tmp_path):
    """Regression: the plugin used to register the same tab + tile
    both in plugin.toml AND imperatively via host.ui.*. The host
    adapter concatenates manifest-declared and runtime entries, so a
    user would see two `usage-limits` tabs and two tiles. After the
    fix only the manifest path emits UI entries.
    """
    import sys

    # Re-execute the plugin module under a fresh sys.modules entry so
    # we exercise the same import path PluginRegistry._scan_directory
    # uses, not the cached test-time PLUGIN.
    import importlib.util
    from pathlib import Path as _Path

    pkg = _Path(__file__).resolve().parent.parent / "plugins" / "usage_limits"
    spec = importlib.util.spec_from_file_location(
        "plugins.usage_limits.plugin_for_test_ui",
        str(pkg / "plugin.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    from relaydeck.plugin import HostPluginAdapter, PluginContext, PluginEventBus
    from relaydeck.plugin_manifest import find_manifest

    manifest = find_manifest(pkg)
    adapter = HostPluginAdapter(module.PLUGIN, manifest, pkg)
    adapter.on_load(PluginContext(
        config_home=tmp_path,
        workspace_path=None,
        event_bus=PluginEventBus(),
    ))
    ui = adapter.register_ui()
    tab_ids = [t["id"] for t in ui.get("tabs", [])]
    tile_ids = [t["id"] for t in ui.get("agent_tiles", [])]
    assert len(tab_ids) == len(set(tab_ids)), f"duplicate tab ids: {tab_ids}"
    assert len(tile_ids) == len(set(tile_ids)), f"duplicate tile ids: {tile_ids}"
    # And the one we expect is present
    assert any(tid.endswith(":usage-limits") for tid in tab_ids)
    assert any(tid.endswith(":usage-limits-tile") for tid in tile_ids)


# ── Manifest sanity ──────────────────────────────────────────────────


def test_plugin_manifest_present_and_valid():
    """The plugin must have a discoverable plugin.toml with the right
    declared capabilities so `relaydeck plugin verify` succeeds."""
    from relaydeck.plugin_manifest import find_manifest

    pkg = Path(__file__).resolve().parent.parent / "plugins" / "usage_limits"
    m = find_manifest(pkg)
    assert m is not None
    assert m.name == "usage-limits"
    caps = set(m.declared_capabilities)
    assert "agents.stop" in caps
    assert "events.subscribe" in caps
    assert "events.emit" in caps


# ── Provider-account-wide roll-up ──────────────────────────────────


def test_provider_rollup_sums_across_agents(plugin_ctx, monkeypatch):
    """The account-wide window sums usage across EVERY agent on the
    provider, not just one — the shared 5h/weekly cap."""
    ctx, _, _, db_path = plugin_ctx
    plugin = _legacy_on_load(ctx)
    _seed_usage(db_path, "alice", 600)
    _seed_usage(db_path, "bob", 700)  # same provider "test-provider"

    base = plugin._settings()
    monkeypatch.setattr(
        plugin, "_settings",
        lambda: {**base, "provider_session_token_budget": 1000},
    )
    pstate = plugin.provider_state_for("test-provider")
    assert pstate["session"].used_tokens == 1300
    assert pstate["session"].state == "exceeded"


def test_provider_exceeded_event_emitted(plugin_ctx, monkeypatch):
    ctx, _, bus, db_path = plugin_ctx
    plugin = _legacy_on_load(ctx)
    base = plugin._settings()
    monkeypatch.setattr(
        plugin, "_settings",
        lambda: {**base, "provider_session_token_budget": 500},
    )
    seen = []
    bus.subscribe("usage_limits.provider_exceeded", lambda e: seen.append(e))

    _seed_usage(db_path, "alice", 600)        # already over the 500 cap
    _emit_usage(bus, agent_id="alice", tokens=600)  # fires the handler

    assert len(seen) == 1
    assert seen[0].data["provider"] == "test-provider"
    assert seen[0].data["window"] == "session"
    assert seen[0].data["state"] == "exceeded"


def test_no_provider_event_without_budget(plugin_ctx):
    """Default (no provider budget) emits nothing provider-scoped — the
    feature is opt-in, no behaviour change for existing deployments."""
    ctx, _, bus, db_path = plugin_ctx
    _legacy_on_load(ctx)
    seen = []
    bus.subscribe("usage_limits.provider_threshold", lambda e: seen.append(e))
    bus.subscribe("usage_limits.provider_exceeded", lambda e: seen.append(e))
    _seed_usage(db_path, "alice", 999999)
    _emit_usage(bus, agent_id="alice", tokens=999999)
    assert seen == []
