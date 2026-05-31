"""
Tests for automation run history (relaydeck/automation_runs.py + the loop
agent's run recording + the `relaydeck automation` CLI).

Pin:
 - data layer: start_run opens a `running` row; finish_run settles it
   with a terminal status, finished_at, and computed duration_ms
 - list_runs filters by automation/status and orders newest-first
 - list_automation_ids aggregates one row per automation with last-run
 - prune_runs drops finished rows by age but never `running` ones
 - the loop agent records ONE run per tick: succeeded / partial / failed
   depending on action outcomes, with action_count + error_count
 - run recording is best-effort: a broken db_path can't crash a tick
 - `relaydeck automation list` / `runs` render recorded history
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck import automation_runs as runs_mod
from relaydeck.db import _close_all_pools, open_db
from relaydeck.plugin import PluginEventBus
from plugins.loop.agent import LoopAgent


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


# ── Data layer ───────────────────────────────────────────────────────


class TestDataLayer:
    def test_start_run_opens_running_row(self, db_path):
        run = runs_mod.start_run(
            "loopy", automation_type="loop", workspace="api",
            trigger_type="interval", db_path=db_path,
        )
        assert run.status == runs_mod.STATUS_RUNNING
        assert run.id.startswith("run_")
        assert run.finished_at is None
        fetched = runs_mod.get_run(run.id, db_path=db_path)
        assert fetched is not None
        assert fetched.automation_id == "loopy"
        assert fetched.workspace == "api"

    def test_finish_run_settles_terminal(self, db_path):
        run = runs_mod.start_run("loopy", trigger_type="interval", db_path=db_path)
        time.sleep(0.01)
        done = runs_mod.finish_run(
            run.id, status=runs_mod.STATUS_SUCCEEDED,
            action_count=3, error_count=0, db_path=db_path,
        )
        assert done is not None
        assert done.status == runs_mod.STATUS_SUCCEEDED
        assert done.finished_at is not None
        assert done.duration_ms is not None and done.duration_ms >= 0
        assert done.action_count == 3
        assert done.error_count == 0

    def test_finish_unknown_run_returns_none(self, db_path):
        assert runs_mod.finish_run("run_nope", status="succeeded", db_path=db_path) is None

    def test_list_runs_newest_first_and_filtered(self, db_path):
        for i in range(3):
            r = runs_mod.start_run("a", trigger_type="interval", db_path=db_path)
            runs_mod.finish_run(
                r.id,
                status=runs_mod.STATUS_SUCCEEDED if i < 2 else runs_mod.STATUS_FAILED,
                db_path=db_path,
            )
            time.sleep(0.005)
        runs_mod.start_run("b", trigger_type="interval", db_path=db_path)

        all_a = runs_mod.list_runs(automation_id="a", db_path=db_path)
        assert len(all_a) == 3
        # newest first
        assert all_a[0].started_at >= all_a[-1].started_at
        failed = runs_mod.list_runs(
            automation_id="a", status=runs_mod.STATUS_FAILED, db_path=db_path,
        )
        assert len(failed) == 1
        only_b = runs_mod.list_runs(automation_id="b", db_path=db_path)
        assert len(only_b) == 1

    def test_list_automation_ids_aggregates(self, db_path):
        r1 = runs_mod.start_run("a", automation_type="loop", db_path=db_path)
        runs_mod.finish_run(r1.id, status=runs_mod.STATUS_SUCCEEDED, db_path=db_path)
        time.sleep(0.005)
        r2 = runs_mod.start_run("a", automation_type="loop", db_path=db_path)
        runs_mod.finish_run(r2.id, status=runs_mod.STATUS_FAILED,
                            error_count=2, db_path=db_path)
        runs_mod.start_run("b", automation_type="loop", db_path=db_path)

        rows = {r["automation_id"]: r for r in runs_mod.list_automation_ids(db_path=db_path)}
        assert rows["a"]["runs"] == 2
        # last run for "a" was the failed one
        assert rows["a"]["last_status"] == runs_mod.STATUS_FAILED
        assert rows["a"]["last_error_count"] == 2
        assert rows["b"]["runs"] == 1
        assert rows["b"]["last_status"] == runs_mod.STATUS_RUNNING

    def test_list_automation_ids_uses_latest_run_metadata(self, db_path):
        old = runs_mod.start_run(
            "a", automation_type="zzz-old", workspace="zzz-old", db_path=db_path,
        )
        runs_mod.finish_run(old.id, status=runs_mod.STATUS_SUCCEEDED, db_path=db_path)
        time.sleep(0.005)
        runs_mod.start_run(
            "a", automation_type="aaa-new", workspace="aaa-new", db_path=db_path,
        )

        row = {
            r["automation_id"]: r
            for r in runs_mod.list_automation_ids(db_path=db_path)
        }["a"]

        assert row["automation_type"] == "aaa-new"
        assert row["workspace"] == "aaa-new"

    def test_prune_drops_old_finished_keeps_running(self, db_path):
        old = runs_mod.start_run("a", db_path=db_path)
        runs_mod.finish_run(old.id, status=runs_mod.STATUS_SUCCEEDED, db_path=db_path)
        # Backdate the finished row well past the cutoff.
        conn = open_db(db_path)
        try:
            ancient = time.time() - 60 * 86400
            conn.execute(
                "UPDATE automation_runs SET started_at = ?, finished_at = ? WHERE id = ?",
                (ancient, ancient, old.id),
            )
            conn.commit()
        finally:
            conn.close()
        # A still-running (never finished) old row.
        stuck = runs_mod.start_run("a", db_path=db_path)
        conn = open_db(db_path)
        try:
            conn.execute(
                "UPDATE automation_runs SET started_at = ? WHERE id = ?",
                (time.time() - 60 * 86400, stuck.id),
            )
            conn.commit()
        finally:
            conn.close()

        deleted = runs_mod.prune_runs(older_than_days=30, db_path=db_path)
        assert deleted == 1
        assert runs_mod.get_run(old.id, db_path=db_path) is None
        # the running row survives age-based prune
        assert runs_mod.get_run(stuck.id, db_path=db_path) is not None


# ── Loop agent integration ───────────────────────────────────────────


def _make_agent(db_path, *, actions, bus, workspace=None):
    stop = threading.Event()
    cfg = {"schedule": "interval:1s", "actions": actions, "_event_bus": bus}
    return LoopAgent(
        agent_id="loopy", name="loopy", config=cfg,
        workspace=workspace, db_path=db_path, stop_flag=stop,
    )


class TestLoopRecordsRuns:
    def test_successful_tick_records_succeeded_run(self, db_path):
        bus = PluginEventBus()
        agent = _make_agent(
            db_path,
            actions=[{"bus.emit": {"type": "x", "data": {}}}],
            bus=bus, workspace="api",
        )
        agent._tick({"trigger": "interval"})
        runs = runs_mod.list_runs(automation_id="loopy", db_path=db_path)
        assert len(runs) == 1
        r = runs[0]
        assert r.status == runs_mod.STATUS_SUCCEEDED
        assert r.automation_type == "loop"
        assert r.workspace == "api"
        assert r.trigger_type == "interval"
        assert r.action_count == 1
        assert r.error_count == 0
        assert r.finished_at is not None

    def test_partial_failure_records_partial(self, db_path):
        bus = PluginEventBus()
        agent = _make_agent(
            db_path,
            actions=[
                {"agent.message": {"to": "ghost", "body": "fail"}},  # raises
                {"bus.emit": {"type": "ok", "data": {}}},            # succeeds
            ],
            bus=bus,
        )
        agent._tick({"trigger": "interval"})
        r = runs_mod.list_runs(automation_id="loopy", db_path=db_path)[0]
        assert r.status == runs_mod.STATUS_PARTIAL
        assert r.action_count == 2
        assert r.error_count == 1

    def test_all_fail_records_failed(self, db_path):
        bus = PluginEventBus()
        agent = _make_agent(
            db_path,
            actions=[{"bus.emit": {}}],  # missing required `type` → fails
            bus=bus,
        )
        agent._tick({"trigger": "interval"})
        r = runs_mod.list_runs(automation_id="loopy", db_path=db_path)[0]
        assert r.status == runs_mod.STATUS_FAILED
        assert r.error_count == 1

    def test_on_event_trigger_id_recorded(self, db_path):
        bus = PluginEventBus()
        agent = _make_agent(
            db_path,
            actions=[{"bus.emit": {"type": "x", "data": {}}}],
            bus=bus,
        )
        agent._tick({"trigger": "on_event", "event_type": "agent.error"})
        r = runs_mod.list_runs(automation_id="loopy", db_path=db_path)[0]
        assert r.trigger_type == "on_event"
        assert r.trigger_event_id == "agent.error"

    def test_run_recording_is_best_effort(self, db_path, monkeypatch):
        # If run recording itself blows up, the tick must still dispatch
        # actions and not raise — history is best-effort, never load-
        # bearing for the automation.
        def _boom(*a, **k):
            raise RuntimeError("recording is down")
        monkeypatch.setattr(runs_mod, "start_run", _boom)
        monkeypatch.setattr(runs_mod, "finish_run", _boom)

        bus = PluginEventBus()
        captured = []
        bus.subscribe("survive.*", captured.append)
        agent = _make_agent(
            db_path,
            actions=[{"bus.emit": {"type": "survive.tick", "data": {}}}],
            bus=bus,
        )
        agent._tick({"trigger": "interval"})  # must not raise
        assert any(e.type == "survive.tick" for e in captured)
        # No run row was written, but the loop kept working.
        assert runs_mod.list_runs(automation_id="loopy", db_path=db_path) == []


class TestLoopModelAction:
    def test_loop_model_action_calls_gateway_and_emits(self, db_path, monkeypatch):
        # The "load a model and let it rip" path: a loop tick with a
        # `model` action runs the model gateway and routes the result.
        import relaydeck.sdk as sdk

        captured = {}

        def fake_complete_ex(prompt, *, model="local-fast", max_tokens=256, **kw):
            captured["prompt"] = prompt
            captured["model"] = model
            return "needs attention", {
                "provider": "ollama", "model": "llama3.2",
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "tokens_known": False,
            }

        # The model action records invocations via timed_complete →
        # complete_with_model_ex, so patch that.
        monkeypatch.setattr(sdk, "complete_with_model_ex", fake_complete_ex)

        bus = PluginEventBus()
        got: list = []
        bus.subscribe("loop.model.result", got.append)
        agent = _make_agent(
            db_path,
            actions=[{"model": {"prompt": "react", "include_event": True,
                                "emit": "loop.model.result"}}],
            bus=bus,
        )
        agent._tick({"trigger": "on_event", "event_type": "agent.error"})

        # The `model` action defaults to the `fast` role (which falls back
        # to local-fast until an operator sets a default).
        assert captured["model"] == "role:fast"
        assert "Event:" in captured["prompt"]
        assert len(got) == 1
        assert got[0].data["text"] == "needs attention"
        # And the run is recorded as a clean success.
        r = runs_mod.list_runs(automation_id="loopy", db_path=db_path)[0]
        assert r.status == runs_mod.STATUS_SUCCEEDED
        assert r.action_count == 1


# ── CLI ──────────────────────────────────────────────────────────────


class TestAutomationCLI:
    def _seed(self, db_path):
        r1 = runs_mod.start_run("alpha", automation_type="loop",
                                workspace="api", trigger_type="interval",
                                db_path=db_path)
        runs_mod.finish_run(r1.id, status=runs_mod.STATUS_SUCCEEDED,
                            action_count=2, db_path=db_path)
        r2 = runs_mod.start_run("beta", automation_type="loop",
                                trigger_type="on_event", db_path=db_path)
        runs_mod.finish_run(r2.id, status=runs_mod.STATUS_FAILED,
                            action_count=1, error_count=1, db_path=db_path)

    def _runner(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from relaydeck.transports import cli as cli_mod
        (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(cli_mod, "_get_config_home", lambda: tmp_path)
        return CliRunner(), cli_mod

    def test_list_shows_automations(self, tmp_path, monkeypatch):
        db = str(tmp_path / "runtime" / "relaydeck.db")
        (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
        self._seed(db)
        runner, cli_mod = self._runner(tmp_path, monkeypatch)
        res = runner.invoke(cli_mod.main, ["automation", "list"])
        assert res.exit_code == 0, res.output
        assert "alpha" in res.output
        assert "beta" in res.output

    def test_list_empty(self, tmp_path, monkeypatch):
        runner, cli_mod = self._runner(tmp_path, monkeypatch)
        res = runner.invoke(cli_mod.main, ["automation", "list"])
        assert res.exit_code == 0
        assert "No automation runs" in res.output

    def test_runs_shows_history(self, tmp_path, monkeypatch):
        db = str(tmp_path / "runtime" / "relaydeck.db")
        (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
        self._seed(db)
        runner, cli_mod = self._runner(tmp_path, monkeypatch)
        res = runner.invoke(cli_mod.main, ["automation", "runs", "beta"])
        assert res.exit_code == 0, res.output
        assert "on_event" in res.output
        assert "failed" in res.output

    def test_prune_removes_old(self, tmp_path, monkeypatch):
        db = str(tmp_path / "runtime" / "relaydeck.db")
        (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
        r = runs_mod.start_run("alpha", db_path=db)
        runs_mod.finish_run(r.id, status=runs_mod.STATUS_SUCCEEDED, db_path=db)
        conn = open_db(db)
        try:
            ancient = time.time() - 60 * 86400
            conn.execute(
                "UPDATE automation_runs SET started_at=?, finished_at=? WHERE id=?",
                (ancient, ancient, r.id),
            )
            conn.commit()
        finally:
            conn.close()
        runner, cli_mod = self._runner(tmp_path, monkeypatch)
        res = runner.invoke(cli_mod.main, ["automation", "prune", "--older-than", "30d", "-y"])
        assert res.exit_code == 0, res.output
        assert "Pruned 1" in res.output


# ── Controls (run-now / pause / resume) ───────────────────────────────


class TestAutomationControls:
    def test_trigger_now_records_manual_run(self, db_path):
        bus = PluginEventBus()
        agent = _make_agent(
            db_path, actions=[{"bus.emit": {"type": "x", "data": {}}}], bus=bus,
        )
        agent.trigger_now()
        runs = runs_mod.list_runs(automation_id="loopy", db_path=db_path)
        assert len(runs) == 1
        assert runs[0].trigger_type == "manual"
        assert runs[0].status == runs_mod.STATUS_SUCCEEDED

    def test_trigger_loop_tick_false_when_not_running(self, tmp_path):
        import relaydeck.orchestrator as orch_mod
        orch_mod._orchestrator = None
        from relaydeck.orchestrator import Orchestrator
        orch = Orchestrator(tmp_path / "cfg")
        assert orch.trigger_loop_tick("nope") is False

    def test_trigger_loop_tick_raises_for_non_loop(self, tmp_path):
        import relaydeck.orchestrator as orch_mod
        orch_mod._orchestrator = None
        from relaydeck.orchestrator import Orchestrator
        orch = Orchestrator(tmp_path / "cfg")

        class _Dummy:  # no trigger_now → not a loop automation
            pass

        orch._instances["x"] = _Dummy()
        with pytest.raises(ValueError, match="not a loop automation"):
            orch.trigger_loop_tick("x")

    def test_trigger_loop_tick_dispatches_on_live_instance(self, db_path, tmp_path):
        import relaydeck.orchestrator as orch_mod
        orch_mod._orchestrator = None
        from relaydeck.orchestrator import Orchestrator
        bus = PluginEventBus()
        agent = _make_agent(
            db_path, actions=[{"bus.emit": {"type": "x", "data": {}}}], bus=bus,
        )
        orch = Orchestrator(tmp_path / "cfg")
        orch._instances["loopy"] = agent
        assert orch.trigger_loop_tick("loopy") is True
        # The tick runs in a daemon thread; wait for the run to land.
        for _ in range(200):
            if runs_mod.list_runs(automation_id="loopy", db_path=db_path):
                break
            time.sleep(0.01)
        runs = runs_mod.list_runs(automation_id="loopy", db_path=db_path)
        assert any(r.trigger_type == "manual" for r in runs)


# ── HTTP API ──────────────────────────────────────────────────────────


class TestAutomationHTTP:
    def _client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from pathlib import Path as _P
        monkeypatch.setattr(_P, "home", lambda: tmp_path)
        cfg_home = tmp_path / ".relaydeck"
        (cfg_home / "runtime").mkdir(parents=True, exist_ok=True)
        import relaydeck.orchestrator as orch_mod
        orch_mod._orchestrator = None
        from relaydeck.transports.api import create_app
        client = TestClient(create_app(cfg_home))
        db = str(cfg_home / "runtime" / "relaydeck.db")
        return client, db

    def test_list_endpoint_history_only_for_orphan_runs(self, tmp_path, monkeypatch):
        client, db = self._client(tmp_path, monkeypatch)
        r = runs_mod.start_run("ghost", automation_type="loop", db_path=db)
        runs_mod.finish_run(r.id, status=runs_mod.STATUS_SUCCEEDED, db_path=db)
        res = client.get("/api/automations")
        assert res.status_code == 200
        rows = {a["automation_id"]: a for a in res.json()["automations"]}
        # A run with no backing loop spec / agent row → history only.
        assert "ghost" in rows
        assert rows["ghost"]["is_agent"] is False
        assert rows["ghost"]["agent_status"] is None

    def test_list_endpoint_is_spec_driven(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch)
        # A configured loop worker that has NEVER run still appears, with
        # its trigger summary + attached action kinds.
        agents_dir = tmp_path / ".relaydeck" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "myworker.yaml").write_text(
            "id: myworker\n"
            "name: My Worker\n"
            "type: loop\n"
            "workspace: null\n"
            "config:\n"
            "  schedule: interval:30s\n"
            "  actions:\n"
            "    - model:\n"
            "        prompt: hi\n"
            "    - bus.emit:\n"
            "        type: x\n"
        )
        res = client.get("/api/automations")
        assert res.status_code == 200
        rows = {a["automation_id"]: a for a in res.json()["automations"]}
        assert "myworker" in rows
        w = rows["myworker"]
        assert w["name"] == "My Worker"
        assert w["runs"] == 0  # never run, still listed
        assert w["trigger"]["kind"] == "interval"
        assert w["action_kinds"] == ["model", "bus.emit"]

    def test_list_endpoint_cron_trigger(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch)
        agents_dir = tmp_path / ".relaydeck" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "nightly.yaml").write_text(
            "id: nightly\ntype: loop\nworkspace: null\n"
            "config:\n  schedule: cron:0 9 * * 1-5\n  actions: []\n"
        )
        res = client.get("/api/automations")
        rows = {a["automation_id"]: a for a in res.json()["automations"]}
        assert rows["nightly"]["trigger"]["kind"] == "cron"

    def test_runs_endpoint(self, tmp_path, monkeypatch):
        client, db = self._client(tmp_path, monkeypatch)
        r = runs_mod.start_run("a", trigger_type="cron", db_path=db)
        runs_mod.finish_run(r.id, status=runs_mod.STATUS_FAILED, error_count=1, db_path=db)
        res = client.get("/api/automations/a/runs")
        assert res.status_code == 200
        runs = res.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["trigger_type"] == "cron"
        assert runs[0]["status"] == "failed"

    def test_run_now_409_when_not_running(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch)
        res = client.post("/api/automations/nobody/run")
        assert res.status_code == 409

    def test_resume_unknown_agent_400(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch)
        res = client.post("/api/automations/nobody/resume")
        assert res.status_code == 400

    def test_pause_is_idempotent(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch)
        res = client.post("/api/automations/nobody/pause")
        assert res.status_code == 200
        assert res.json()["status"] == "paused"

    # ── validate ──────────────────────────────────────────────────────

    def test_validate_ok(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch)
        res = client.post("/api/automations/validate", json={
            "schedule": "interval:30s",
            "actions": [{"model": {"prompt": "hi"}}, {"bus.emit": {"type": "x"}}],
        })
        assert res.status_code == 200
        assert res.json()["ok"] is True

    def test_validate_bad_schedule(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch)
        res = client.post("/api/automations/validate", json={"schedule": "interval:nope"})
        body = res.json()
        assert body["ok"] is False
        assert body["errors"]

    def test_validate_unknown_action(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch)
        res = client.post("/api/automations/validate", json={
            "schedule": "interval:5m",
            "actions": [{"frobnicate": {}}],
        })
        body = res.json()
        assert body["ok"] is False
        assert any("frobnicate" in e for e in body["errors"])

    # ── daemon restart (responsible) ──────────────────────────────────

    def test_restart_info_unmanaged_in_tests(self, tmp_path, monkeypatch):
        # The test cfg home has no daemon.pid → not managed; the UI uses
        # this to tell the operator to restart from the terminal instead.
        client, _ = self._client(tmp_path, monkeypatch)
        res = client.get("/api/daemon/restart-info")
        assert res.status_code == 200
        body = res.json()
        assert body["managed"] is False
        assert "running_agent_count" in body
        assert body["warning"]

    def test_restart_refuses_when_unmanaged(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch)
        res = client.post("/api/daemon/restart")
        assert res.status_code == 409
