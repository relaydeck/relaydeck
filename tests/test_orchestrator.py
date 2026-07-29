"""
Tests for the orchestrator: agent lifecycle, CRUD, event bus.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.agents_base import BaseAgent
from relaydeck.orchestrator import (
    EventBus,
    Orchestrator,
    register_agent_type,
    get_agent_type,
    known_agent_types,
    get_orchestrator,
)


# ── Test Agent ───────────────────────────────────────────────────────


class SimpleTestAgent(BaseAgent):
    """A minimal agent that runs briefly and exits."""

    run_count = 0
    # Signals the runner thread has actually entered its loop body — tests
    # wait on this instead of guessing a sleep duration, which used to flake
    # under CI scheduling pressure (run_count stayed 0 when the daemon
    # thread didn't get CPU within the wall-clock window).
    iterated = threading.Event()

    def run(self) -> None:
        self.emit("test.start", {})
        while not self.stop_flag.is_set():
            SimpleTestAgent.run_count += 1
            SimpleTestAgent.iterated.set()
            if self.sleep_unless_stopped(0.1):
                break
        self.emit("test.stop", {})


class ErrorTestAgent(BaseAgent):
    """An agent that crashes on run."""

    def run(self) -> None:
        raise RuntimeError("simulated crash")


class SelfErroredTestAgent(BaseAgent):
    """An agent whose run() records 'errored' and RETURNS cleanly — the
    missing-harness-CLI path. The runner must preserve that status (it used to
    clobber it with 'stopped', losing the reason)."""

    def run(self) -> None:
        self.update_status("errored", "command not found: nonesuch")
        return


# ── Event Bus Tests ──────────────────────────────────────────────────


class TestEventBus:
    def test_subscribe_and_publish(self):
        bus = EventBus()
        q = bus.subscribe("agent-1")

        bus.publish("agent-1", "test.event", {"key": "val"}, 42)

        event = q.get(timeout=1.0)
        assert event["type"] == "test.event"
        assert event["agent_id"] == "agent-1"
        assert event["payload"] == {"key": "val"}
        assert event["id"] == 42

    def test_broadcast_subscriber(self):
        bus = EventBus()
        q = bus.subscribe("*")

        bus.publish("agent-1", "test.event", {}, 1)
        bus.publish("agent-2", "other.event", {}, 2)

        e1 = q.get(timeout=1.0)
        e2 = q.get(timeout=1.0)
        assert {e1["agent_id"], e2["agent_id"]} == {"agent-1", "agent-2"}

    def test_unsubscribe(self):
        bus = EventBus()
        q = bus.subscribe("agent-1")
        bus.unsubscribe("agent-1", q)

        bus.publish("agent-1", "test", {}, 1)
        assert q.empty()

    def test_only_routes_to_matching_agent(self):
        bus = EventBus()
        q1 = bus.subscribe("agent-1")
        q2 = bus.subscribe("agent-2")

        bus.publish("agent-1", "test", {}, 1)
        assert not q1.empty()
        assert q2.empty()


# ── Orchestrator Tests ───────────────────────────────────────────────


class TestOrchestrator:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        """Register test agent types before each test."""
        register_agent_type("simple", SimpleTestAgent)
        register_agent_type("error", ErrorTestAgent)
        register_agent_type("self-errored", SelfErroredTestAgent)
        self.config_home = tmp_path / "config"
        self.config_home.mkdir(parents=True)
        yield
        # Cleanup
        SimpleTestAgent.run_count = 0

    def test_known_types(self):
        types = known_agent_types()
        assert "simple" in types
        assert "error" in types

    def test_get_agent_type(self):
        assert get_agent_type("simple") is SimpleTestAgent
        assert get_agent_type("nonexistent") is None

    def test_create_agent(self, tmp_path):
        orch = Orchestrator(config_home=self.config_home)
        agent_id = orch.create_agent("test-agent", "simple", "Test Agent")
        assert agent_id == "test-agent"

        # Check YAML was written
        spec_path = self.config_home / "agents" / "test-agent.yaml"
        assert spec_path.exists()

        # Check DB was updated
        agent = orch.get_agent("test-agent")
        assert agent is not None
        assert agent["status"] == "pending"

    def test_multi_agent_workspace_auto_enables_messaging(self, tmp_path):
        """When a workspace gains a 2nd agent, messaging turns on so the
        peers can actually reply to each other (regression: the lost
        bob→alice reply)."""
        from relaydeck.config import load_workspace_registry, register_workspace
        register_workspace(self.config_home, "ops", tmp_path / "ops", [])
        orch = Orchestrator(config_home=self.config_home)
        # First (solo) agent: no peers → messaging stays off.
        orch.create_agent("alice", "simple", "Alice", workspace="ops")
        ws = next(w for w in load_workspace_registry(self.config_home) if w.name == "ops")
        assert "messaging" not in (ws.plugins or [])
        # Second agent into the same workspace → now multi-agent → enabled.
        orch.create_agent("bob", "simple", "Bob", workspace="ops")
        ws = next(w for w in load_workspace_registry(self.config_home) if w.name == "ops")
        assert "messaging" in ws.plugins
        # agent.toml carries it too (what the harness reads at spawn).
        toml = (self.config_home / "workspaces" / "ops" / "agent.toml").read_text()
        assert "messaging" in toml

    def test_solo_agent_workspace_leaves_messaging_off(self, tmp_path):
        from relaydeck.config import load_workspace_registry, register_workspace
        register_workspace(self.config_home, "lab", tmp_path / "lab", [])
        orch = Orchestrator(config_home=self.config_home)
        orch.create_agent("solo", "simple", "Solo", workspace="lab")
        ws = next(w for w in load_workspace_registry(self.config_home) if w.name == "lab")
        assert "messaging" not in (ws.plugins or [])

    def test_create_preserves_existing_workspace_plugins(self, tmp_path):
        """Auto-enabling messaging appends — it doesn't clobber plugins the
        workspace already had."""
        from relaydeck.config import load_workspace_registry, register_workspace
        register_workspace(self.config_home, "wsp", tmp_path / "wsp", ["recipes"])
        orch = Orchestrator(config_home=self.config_home)
        orch.create_agent("a", "simple", "A", workspace="wsp")
        orch.create_agent("b", "simple", "B", workspace="wsp")
        ws = next(w for w in load_workspace_registry(self.config_home) if w.name == "wsp")
        assert "recipes" in ws.plugins and "messaging" in ws.plugins

    def test_list_agents(self, tmp_path):
        orch = Orchestrator(config_home=self.config_home)
        orch.create_agent("a", "simple", "A")
        orch.create_agent("b", "simple", "B")

        agents = orch.list_agents()
        assert len(agents) == 2

    def test_start_stop_agent(self, tmp_path):
        orch = Orchestrator(config_home=self.config_home)
        orch.create_agent("test", "simple", "Test")

        orch.start_agent("test")
        time.sleep(0.3)  # Let it run a bit

        agent = orch.get_agent("test")
        assert agent["status"] in ("running", "stopped")

        orch.stop_agent("test")
        time.sleep(0.3)

    def test_start_unknown_agent(self, tmp_path):
        orch = Orchestrator(config_home=self.config_home)
        with pytest.raises(ValueError, match="not found"):
            orch.start_agent("nonexistent")

    def test_start_unknown_type(self, tmp_path):
        orch = Orchestrator(config_home=self.config_home)
        # Create a spec with an unregistered type
        spec_path = self.config_home / "agents" / "unknown.yaml"
        import yaml
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(yaml.dump({"id": "unknown", "type": "no-such-type", "name": "Unknown"}))

        with pytest.raises(ValueError, match="Unknown agent type"):
            orch.start_agent("unknown")

    def test_delete_agent(self, tmp_path):
        orch = Orchestrator(config_home=self.config_home)
        orch.create_agent("to-delete", "simple", "Delete Me")

        assert orch.get_agent("to-delete") is not None
        orch.delete_agent("to-delete")
        assert orch.get_agent("to-delete") is None
        assert not (self.config_home / "agents" / "to-delete.yaml").exists()

    def _seed_agent_artifacts(self, orch, agent_id, workspace):
        """Create the per-agent runtime files + DB history a real agent
        would leave behind, so we can assert thorough delete clears them."""
        from relaydeck.db import log_event, open_db, put_result, record_usage
        rt = self.config_home / "workspaces" / workspace / "runtime"
        prompts = self.config_home / "workspaces" / workspace / "prompts"
        (rt / "pi-sessions" / agent_id).mkdir(parents=True, exist_ok=True)
        (rt / "pi-sessions" / agent_id / "s.jsonl").write_text("{}")
        (rt / "codex-homes" / agent_id).mkdir(parents=True, exist_ok=True)
        (rt / "opencode-homes" / agent_id).mkdir(parents=True, exist_ok=True)
        (rt / "fleet-context").mkdir(parents=True, exist_ok=True)
        (rt / "fleet-context" / f"{agent_id}.md").write_text("ctx")
        prompts.mkdir(parents=True, exist_ok=True)
        (prompts / f"{agent_id}.md").write_text("addendum")
        conn = open_db(orch.db_path)
        try:
            from relaydeck.db import ensure_session
            ensure_session(conn, "sess")
            log_event(conn, "sess", "harness.spawn", {}, agent_id=agent_id)
            record_usage(conn, agent_id, "sess", "m", "p", total_tokens=10)
            put_result(conn, agent_id, "durable result", key="review")
        finally:
            conn.close()

    def _history_counts(self, orch, agent_id):
        from relaydeck.db import open_db
        conn = open_db(orch.db_path)
        try:
            ev = conn.execute(
                "SELECT COUNT(*) FROM events WHERE agent_id=?", (agent_id,),
            ).fetchone()[0]
            us = conn.execute(
                "SELECT COUNT(*) FROM usage_records WHERE agent_id=?", (agent_id,),
            ).fetchone()[0]
            results = conn.execute(
                "SELECT COUNT(*) FROM agent_results WHERE agent_id=?", (agent_id,),
            ).fetchone()[0]
            return ev, us, results
        finally:
            conn.close()

    def test_delete_purges_files_and_history(self, tmp_path):
        orch = Orchestrator(config_home=self.config_home)
        orch.create_agent("gone", "simple", "Gone", workspace="ws")
        self._seed_agent_artifacts(orch, "gone", "ws")
        assert self._history_counts(orch, "gone") != (0, 0, 0)

        orch.delete_agent("gone")  # purge_history defaults True

        rt = self.config_home / "workspaces" / "ws" / "runtime"
        assert not (rt / "pi-sessions" / "gone").exists()
        assert not (rt / "codex-homes" / "gone").exists()
        assert not (rt / "opencode-homes" / "gone").exists()
        assert not (rt / "fleet-context" / "gone.md").exists()
        assert not (self.config_home / "workspaces" / "ws" / "prompts" / "gone.md").exists()
        assert self._history_counts(orch, "gone") == (0, 0, 0)

    def test_delete_keep_history(self, tmp_path):
        orch = Orchestrator(config_home=self.config_home)
        orch.create_agent("keep", "simple", "Keep", workspace="ws")
        self._seed_agent_artifacts(orch, "keep", "ws")

        orch.delete_agent("keep", purge_history=False)

        # Files always go; history is preserved for audit.
        sessions = self.config_home / "workspaces" / "ws" / "runtime" / "pi-sessions"
        assert not (sessions / "keep").exists()
        assert self._history_counts(orch, "keep") != (0, 0, 0)

    def test_delete_files_path_traversal_guard(self, tmp_path):
        orch = Orchestrator(config_home=self.config_home)
        # A malformed id must never escape the runtime dir.
        sentinel = self.config_home / "sentinel.txt"
        sentinel.write_text("keep me")
        orch._purge_agent_files("../../sentinel", "ws")
        assert sentinel.exists()

    def test_error_agent(self, tmp_path):
        """An agent whose `run()` raises must surface the error to the
        caller AND end with status='errored' in the DB. Previously
        start_agent was fire-and-forget; with the start-verification
        fix it raises RuntimeError when it detects the thread died
        with status='errored' inside the verification window — so
        the CLI can print the real reason instead of returning a
        misleading "✓ started" for a child that died immediately."""
        orch = Orchestrator(config_home=self.config_home)
        orch.create_agent("bad", "error", "Bad Agent")
        with pytest.raises(RuntimeError, match="failed to start"):
            orch.start_agent("bad")
        time.sleep(0.3)

        agent = orch.get_agent("bad")
        assert agent["status"] == "errored"

    def test_errored_status_survives_clean_return(self, tmp_path):
        """An agent whose run() sets 'errored' then RETURNS (the missing-harness
        path, e.g. `command not found: pi`) must STAY errored with its reason —
        the runner used to overwrite it with 'stopped' after run() returned."""
        orch = Orchestrator(config_home=self.config_home)
        orch.create_agent("noharness", "self-errored", "No Harness")
        try:
            orch.start_agent("noharness")
        except RuntimeError:
            pass  # start-verification may surface the error synchronously — fine
        time.sleep(0.4)  # let the run thread fully exit (where the clobber happened)

        agent = orch.get_agent("noharness")
        assert agent["status"] == "errored", agent["status"]
        assert "command not found" in (agent.get("last_error") or "")


# ── BaseAgent Tests ──────────────────────────────────────────────────


class TestBaseAgent:
    def test_emit(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        stop_flag = threading.Event()
        agent = BaseAgent(
            agent_id="test", name="Test", config={},
            workspace=None, db_path=db_path, stop_flag=stop_flag,
        )
        ev_id = agent.emit("test.event", {"key": "val"})
        assert ev_id > 0

    def test_sleep_unless_stopped(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        stop_flag = threading.Event()
        agent = BaseAgent(
            agent_id="test", name="Test", config={},
            workspace=None, db_path=db_path, stop_flag=stop_flag,
        )

        # Should sleep and return False (not stopped)
        result = agent.sleep_unless_stopped(0.1)
        assert result is False

    def test_sleep_unless_stopped_during_stop(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        stop_flag = threading.Event()
        agent = BaseAgent(
            agent_id="test", name="Test", config={},
            workspace=None, db_path=db_path, stop_flag=stop_flag,
        )

        # Set stop flag after short delay in another thread
        def set_stop():
            time.sleep(0.05)
            stop_flag.set()

        t = threading.Thread(target=set_stop, daemon=True)
        t.start()
        result = agent.sleep_unless_stopped(5.0)  # Long timeout
        assert result is True


class TestSimpleTestAgent:
    def test_runs_and_stops(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        stop_flag = threading.Event()
        SimpleTestAgent.run_count = 0
        SimpleTestAgent.iterated.clear()
        agent = SimpleTestAgent(
            agent_id="test", name="Test", config={},
            workspace=None, db_path=db_path, stop_flag=stop_flag,
        )

        def runner():
            agent.run()

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        # Wait on the runner's actual progress, not a wall-clock sleep — under
        # CI scheduling pressure the daemon thread sometimes didn't get CPU
        # within the prior 0.3 s window, leaving run_count at 0.
        assert SimpleTestAgent.iterated.wait(timeout=5.0), (
            "runner thread never entered loop body within 5s"
        )
        stop_flag.set()
        t.join(timeout=2)
        assert SimpleTestAgent.run_count > 0
