"""
Tests for the LoopAgent type.

Pin:
 - schedule parser accepts interval + on_event, rejects cron + garbage
 - interval mode ticks on schedule and dispatches each configured action
 - on_event mode subscribes to the bus and ticks per matching event
 - action errors don't crash the run loop; failing actions are emitted
 - consecutive errors → status flips to errored
 - terminate stops the loop and unsubscribes
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.db import _close_all_pools, open_db
from relaydeck.plugin import Event, PluginEventBus
from plugins.loop.agent import LoopAgent, parse_schedule


@pytest.fixture
def db_path(tmp_path):
    _close_all_pools()
    p = tmp_path / "relaydeck.db"
    conn = open_db(str(p))
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    yield str(p)
    _close_all_pools()


def _events_for(db_path: str, agent_id: str, event_type: str | None = None) -> list[dict]:
    conn = open_db(db_path)
    try:
        if event_type:
            rows = conn.execute(
                "SELECT type, payload FROM events WHERE agent_id = ? AND type = ? "
                "ORDER BY id ASC",
                (agent_id, event_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT type, payload FROM events WHERE agent_id = ? ORDER BY id ASC",
                (agent_id,),
            ).fetchall()
        return [{"type": r[0], "payload": r[1]} for r in rows]
    finally:
        conn.close()


# ── Schedule parser ─────────────────────────────────────────────────


class TestParseSchedule:
    def test_interval_seconds(self):
        assert parse_schedule("interval:30s") == ("interval", 30.0)

    def test_interval_minutes(self):
        assert parse_schedule("interval:5m") == ("interval", 300.0)

    def test_interval_hours(self):
        assert parse_schedule("interval:2h") == ("interval", 7200.0)

    def test_interval_tolerates_whitespace(self):
        assert parse_schedule("interval: 10 s") == ("interval", 10.0)

    def test_on_event(self):
        assert parse_schedule("on_event:agent.message") == ("on_event", "agent.message")

    def test_on_event_glob(self):
        assert parse_schedule("on_event:agent.*") == ("on_event", "agent.*")

    def test_cron_accepted(self):
        # croniter is a core dependency, so a valid expression always
        # parses to a cron schedule.
        assert parse_schedule("cron:0 9 * * 1-5") == ("cron", "0 9 * * 1-5")

    def test_cron_every_minute(self):
        assert parse_schedule("cron:* * * * *") == ("cron", "* * * * *")

    def test_cron_invalid_expr_rejected(self):
        with pytest.raises(ValueError, match="invalid cron expression"):
            parse_schedule("cron:not a cron")

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="unknown schedule kind"):
            parse_schedule("daily:1")

    def test_missing_colon_rejected(self):
        with pytest.raises(ValueError):
            parse_schedule("interval30s")

    def test_garbage_interval_rejected(self):
        with pytest.raises(ValueError, match="interval must be"):
            parse_schedule("interval:thirty")

    def test_empty_on_event_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            parse_schedule("on_event:")


# ── Helpers ─────────────────────────────────────────────────────────


def _make_agent(
    db_path: str,
    *,
    schedule: str = "interval:1s",
    actions: list[dict] | None = None,
    bus: PluginEventBus | None = None,
    workspace_path: Path | None = None,
) -> tuple[LoopAgent, threading.Event]:
    stop = threading.Event()
    cfg = {
        "schedule": schedule,
        "actions": actions or [],
        "workspace_path": str(workspace_path) if workspace_path else None,
    }
    if bus is not None:
        cfg["_event_bus"] = bus
    agent = LoopAgent(
        agent_id="loopy",
        name="loopy",
        config=cfg,
        workspace=None,
        db_path=db_path,
        stop_flag=stop,
    )
    return agent, stop


# ── Tick dispatch ───────────────────────────────────────────────────


class TestTickDispatch:
    def test_interval_tick_dispatches_bus_emit(self, db_path):
        bus = PluginEventBus()
        captured: list[Event] = []
        bus.subscribe("heartbeat.*", captured.append)
        agent, _stop = _make_agent(
            db_path,
            actions=[{"bus.emit": {"type": "heartbeat.tick", "data": {"n": 1}}}],
            bus=bus,
        )
        agent._tick({"trigger": "interval"})
        assert len(captured) == 1
        assert captured[0].type == "heartbeat.tick"
        assert captured[0].data == {"n": 1}

    def test_tick_emits_loop_action_event(self, db_path):
        bus = PluginEventBus()
        agent, _ = _make_agent(
            db_path,
            actions=[{"bus.emit": {"type": "x", "data": {}}}],
            bus=bus,
        )
        agent._tick({"trigger": "interval"})
        actions = _events_for(db_path, "loopy", "loop.action")
        assert len(actions) == 1

    def test_action_error_does_not_crash_tick(self, db_path):
        # `agent.message` requires the orchestrator; not wiring it
        # should raise ActionError, caught and emitted as
        # loop.action_error. The second action still runs.
        bus = PluginEventBus()
        captured: list[Event] = []
        bus.subscribe("survives.*", captured.append)
        agent, _ = _make_agent(
            db_path,
            actions=[
                {"agent.message": {"to": "ghost", "body": "fail"}},
                {"bus.emit": {"type": "survives.fail", "data": {}}},
            ],
            bus=bus,
        )
        agent._tick({"trigger": "interval"})
        # Second action fired
        assert any(e.type == "survives.fail" for e in captured)
        # First action recorded as error
        errors = _events_for(db_path, "loopy", "loop.action_error")
        assert len(errors) == 1

    def test_consecutive_errors_increment_then_reset_on_success(self, db_path):
        bus = PluginEventBus()
        agent, _ = _make_agent(
            db_path,
            actions=[{"bus.emit": {"type": "x", "data": {}}}],  # always succeeds
            bus=bus,
        )
        # Force a failure once via a malformed action injection.
        bad = [{"bus.emit": {}}]  # missing required `type`
        agent._actions = bad
        agent._tick({"trigger": "test"})
        assert agent._consecutive_errors == 1
        # Restore good action; counter should reset.
        agent._actions = [{"bus.emit": {"type": "ok", "data": {}}}]
        agent._tick({"trigger": "test"})
        assert agent._consecutive_errors == 0


# ── Interval run loop ────────────────────────────────────────────────


class TestIntervalRunLoop:
    def test_run_interval_ticks_until_stopped(self, db_path):
        bus = PluginEventBus()
        agent, stop = _make_agent(
            db_path,
            schedule="interval:1s",
            actions=[{"bus.emit": {"type": "tick", "data": {}}}],
            bus=bus,
        )
        # Run in a thread; let it tick a couple of times then stop.
        t = threading.Thread(target=agent.run)
        t.start()
        time.sleep(0.2)  # first tick is immediate
        stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive()
        ticks = _events_for(db_path, "loopy", "loop.tick")
        assert len(ticks) >= 1

    def test_run_emits_lifecycle_events(self, db_path):
        bus = PluginEventBus()
        agent, stop = _make_agent(
            db_path,
            schedule="interval:1h",   # only the immediate tick fires
            actions=[{"bus.emit": {"type": "tick", "data": {}}}],
            bus=bus,
        )
        t = threading.Thread(target=agent.run)
        t.start()
        time.sleep(0.1)
        stop.set()
        t.join(timeout=2.0)
        starts = _events_for(db_path, "loopy", "loop.start")
        stops = _events_for(db_path, "loopy", "loop.stop")
        assert len(starts) == 1
        assert len(stops) == 1


# ── Cron run loop ────────────────────────────────────────────────────


class TestCronRunLoop:
    def test_cron_fires_tick_and_records_run(self, db_path, monkeypatch):
        # croniter's finest granularity is 1 minute, too slow for a unit
        # test, so swap in a fake that schedules the next fire ~20ms out.
        import croniter as cron_mod

        class _Fast:
            def __init__(self, expr, base):
                self._base = base

            @staticmethod
            def is_valid(expr):
                return True

            def get_next(self, ret_type=float):
                return time.time() + 0.02

        monkeypatch.setattr(cron_mod, "croniter", _Fast)

        bus = PluginEventBus()
        seen: list[Event] = []
        bus.subscribe("cron.fired", seen.append)
        agent, stop = _make_agent(
            db_path,
            schedule="cron:* * * * *",  # validated at parse time (real croniter)
            actions=[{"bus.emit": {"type": "cron.fired", "data": {}}}],
            bus=bus,
        )
        t = threading.Thread(target=agent._run_cron, args=("* * * * *",))
        t.start()
        for _ in range(200):
            if seen:
                break
            time.sleep(0.01)
        stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert len(seen) >= 1
        # The cron tick was recorded as an automation run.
        from relaydeck import automation_runs as runs_mod
        runs = runs_mod.list_runs(automation_id="loopy", db_path=db_path)
        assert any(r.trigger_type == "cron" for r in runs)

    def test_cron_waits_does_not_fire_immediately(self, db_path, monkeypatch):
        # Cron must WAIT for the next slot, never tick at t=0 like interval.
        import croniter as cron_mod

        class _Far:
            def __init__(self, expr, base):
                pass

            @staticmethod
            def is_valid(expr):
                return True

            def get_next(self, ret_type=float):
                return time.time() + 3600.0  # an hour out

        monkeypatch.setattr(cron_mod, "croniter", _Far)
        bus = PluginEventBus()
        agent, stop = _make_agent(
            db_path,
            schedule="cron:0 * * * *",
            actions=[{"bus.emit": {"type": "x", "data": {}}}],
            bus=bus,
        )
        t = threading.Thread(target=agent._run_cron, args=("0 * * * *",))
        t.start()
        time.sleep(0.1)
        stop.set()
        t.join(timeout=2.0)
        ticks = _events_for(db_path, "loopy", "loop.tick")
        assert ticks == []  # nothing fired in the first 100ms


# ── On-event mode ────────────────────────────────────────────────────


class TestOnEventMode:
    def test_on_event_dispatches_on_bus_event(self, db_path):
        bus = PluginEventBus()
        captured: list[Event] = []
        bus.subscribe("loop.replied", captured.append)
        agent, stop = _make_agent(
            db_path,
            schedule="on_event:agent.error",
            actions=[{"bus.emit": {"type": "loop.replied", "data": {"src": "loop"}}}],
            bus=bus,
        )
        t = threading.Thread(target=agent.run)
        t.start()
        time.sleep(0.05)
        # Emit a matching event; the loop's handler should fire.
        bus.emit(Event(type="agent.error", data={"id": "x"}))
        time.sleep(0.05)
        # Non-matching event should be ignored.
        bus.emit(Event(type="other.thing", data={}))
        time.sleep(0.05)
        stop.set()
        t.join(timeout=2.0)
        replies = [e for e in captured if e.type == "loop.replied"]
        assert len(replies) == 1
        assert replies[0].data == {"src": "loop"}

    def test_terminate_unsubscribes(self, db_path):
        bus = PluginEventBus()
        agent, stop = _make_agent(
            db_path,
            schedule="on_event:demo",
            actions=[{"bus.emit": {"type": "x", "data": {}}}],
            bus=bus,
        )
        t = threading.Thread(target=agent.run)
        t.start()
        time.sleep(0.05)
        agent.terminate()
        t.join(timeout=2.0)
        # After terminate, an emit shouldn't reach our agent.
        before = len(_events_for(db_path, "loopy", "loop.tick"))
        bus.emit(Event(type="demo", data={}))
        time.sleep(0.05)
        after = len(_events_for(db_path, "loopy", "loop.tick"))
        assert before == after, "tick fired after terminate — unsubscribe leaked"


# ── Init validation ──────────────────────────────────────────────────


class TestInitValidation:
    def test_bad_schedule_raises_at_construction(self, db_path):
        with pytest.raises(ValueError):
            LoopAgent(
                agent_id="x", name="x",
                config={"schedule": "garbage", "actions": []},
                workspace=None, db_path=db_path, stop_flag=threading.Event(),
            )

    def test_actions_must_be_list(self, db_path):
        with pytest.raises(ValueError, match="must be a list"):
            LoopAgent(
                agent_id="x", name="x",
                config={"schedule": "interval:1s", "actions": "not a list"},
                workspace=None, db_path=db_path, stop_flag=threading.Event(),
            )


# ── Issue 4: workspace resolution must use the daemon's config home ───


class TestConfigHomeResolution:
    def test_resolve_workspace_path_uses_config_home_from_db_path(self, tmp_path):
        """_resolve_workspace_path must look the workspace up in the SAME
        config home the daemon runs under (derived from db_path), not the
        default ~/.relaydeck. Otherwise a non-default root /
        RELAYDECK_CONFIG_HOME can't resolve the workspace and script/gh/code
        actions run from the daemon cwd instead of the workspace."""
        _close_all_pools()
        config_home = tmp_path / "altroot" / ".relaydeck"
        (config_home / "runtime").mkdir(parents=True)
        db = str(config_home / "runtime" / "relaydeck.db")
        open_db(db).close()
        from relaydeck.config import register_workspace
        ws_src = tmp_path / "myproj-src"
        ws_src.mkdir()
        register_workspace(config_home, "myproj", ws_src, [])

        agent = LoopAgent(
            agent_id="x", name="x",
            config={"schedule": "interval:1s", "actions": []},
            workspace="myproj", db_path=db, stop_flag=threading.Event(),
        )
        assert agent._workspace_path is not None
        assert agent._workspace_path.resolve() == ws_src.resolve()
        _close_all_pools()


# ── Issue 3: on_event automations must be durable across restart ──────


class TestDurableOnEvent:
    def test_on_event_replays_missed_event_across_restart(self, db_path):
        """An event that arrives while an on_event worker is stopped must
        be replayed when it comes back (durable bus), so the trigger is not
        silently lost. Emit BEFORE the agent subscribes, then start it and
        assert the missed event drove a tick."""
        bus = PluginEventBus(db_path=db_path)
        # Trigger fires while the worker is "down" — persisted to bus_events.
        bus.emit(Event(type="trig.fire", data={"n": 1}))

        agent, stop = _make_agent(
            db_path,
            schedule="on_event:trig.*",
            actions=[{"bus.emit": {"type": "did.tick", "data": {}}}],
            bus=bus,
        )
        t = threading.Thread(target=agent.run, daemon=True)
        t.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if _events_for(db_path, "loopy", "loop.action"):
                    break
                time.sleep(0.05)
        finally:
            stop.set()
            t.join(timeout=2.0)

        actions = _events_for(db_path, "loopy", "loop.action")
        assert len(actions) >= 1, "missed on_event trigger must be replayed on restart"
