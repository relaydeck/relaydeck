"""
Tests for per-call LLM invocation logging (relaydeck/model_invocations.py),
the `model` action recording path, and the invocations API.

Pin:
 - record_invocation / list_invocations / rollup round-trip
 - real token counts when the provider reports them; tokens_known=False
   (never fabricated) when it doesn't
 - timed_complete records an invocation (ok AND error paths) and returns
   text / re-raises
 - a loop worker's `model` action records an invocation
 - GET /api/automations/{id}/invocations returns rows + rollup
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck import model_invocations as mi
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


class TestDataLayer:
    def test_record_list_rollup(self, db_path):
        mi.record_invocation("w1", model="llama3.2", provider="ollama",
                             latency_ms=120, prompt_chars=50, completion_chars=10,
                             prompt_tokens=12, completion_tokens=4, total_tokens=16,
                             tokens_known=True, ok=True, source="loop", db_path=db_path)
        mi.record_invocation("w1", model="llama3.2", provider="ollama",
                             latency_ms=80, ok=False, error="boom", db_path=db_path)
        rows = mi.list_invocations("w1", db_path=db_path)
        assert len(rows) == 2
        assert rows[0].ts >= rows[1].ts  # newest first
        roll = mi.rollup("w1", db_path=db_path)
        assert roll["count"] == 2
        assert roll["errors"] == 1
        assert roll["total_tokens"] == 16
        assert roll["tokens_known_count"] == 1
        assert roll["avg_latency_ms"] == 100  # (120+80)/2

    def test_list_and_rollup_by_model(self, db_path):
        # Two agents, same model (mixed case) + a different model.
        mi.record_invocation("w1", model="Gemma3:1b", provider="ollama",
                             latency_ms=100, ok=True, db_path=db_path)
        mi.record_invocation("w2", model="gemma3:1b", provider="ollama",
                             latency_ms=300, ok=False, error="x", db_path=db_path)
        mi.record_invocation("w1", model="llama3.2", provider="ollama",
                             latency_ms=50, ok=True, db_path=db_path)
        rows = mi.list_by_model("gemma3:1b", db_path=db_path)
        assert len(rows) == 2  # case-insensitive, both agents
        roll = mi.rollup_by_model("gemma3:1b", db_path=db_path)
        assert roll["count"] == 2
        assert roll["errors"] == 1
        assert roll["success_rate"] == 0.5
        assert roll["avg_latency_ms"] == 200

    def test_rollup_by_model_empty_is_none_rate(self, db_path):
        roll = mi.rollup_by_model("never-called", db_path=db_path)
        assert roll["count"] == 0
        assert roll["success_rate"] is None

    def test_prune(self, db_path):
        mi.record_invocation("w1", model="m", db_path=db_path)
        conn = open_db(db_path)
        try:
            import time as _t
            conn.execute("UPDATE model_invocations SET ts = ?",
                         (_t.time() - 60 * 86400,))
            conn.commit()
        finally:
            conn.close()
        assert mi.prune_invocations(older_than_days=30, db_path=db_path) == 1
        assert mi.list_invocations("w1", db_path=db_path) == []


class TestTimedComplete:
    def test_records_real_tokens(self, db_path, monkeypatch):
        import relaydeck.sdk as sdk

        def fake_ex(prompt, *, model="local-fast", max_tokens=256, **kw):
            return "the answer", {
                "provider": "ollama", "model": "llama3.2",
                "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
                "tokens_known": True,
            }
        monkeypatch.setattr(sdk, "complete_with_model_ex", fake_ex)

        out = mi.timed_complete("w1", "hello", model="local-fast",
                                source="loop", db_path=db_path)
        assert out == "the answer"
        rows = mi.list_invocations("w1", db_path=db_path)
        assert len(rows) == 1
        assert rows[0].tokens_known is True
        assert rows[0].total_tokens == 10
        assert rows[0].provider == "ollama"
        assert rows[0].ok is True
        # The actual prompt + response are captured for the "what did it
        # ask / get" view.
        assert rows[0].prompt == "hello"
        assert rows[0].response == "the answer"

    def test_records_unknown_tokens_no_fabrication(self, db_path, monkeypatch):
        import relaydeck.sdk as sdk

        def fake_ex(prompt, *, model="local-fast", max_tokens=256, **kw):
            return "text", {"provider": "openrouter", "model": "x",
                            "prompt_tokens": 0, "completion_tokens": 0,
                            "total_tokens": 0, "tokens_known": False}
        monkeypatch.setattr(sdk, "complete_with_model_ex", fake_ex)
        mi.timed_complete("w1", "hi", db_path=db_path)
        r = mi.list_invocations("w1", db_path=db_path)[0]
        assert r.tokens_known is False
        assert r.total_tokens == 0

    def test_records_error_and_reraises(self, db_path, monkeypatch):
        import relaydeck.sdk as sdk

        def boom(prompt, *, model="local-fast", max_tokens=256, **kw):
            raise RuntimeError("provider down")
        monkeypatch.setattr(sdk, "complete_with_model_ex", boom)
        with pytest.raises(RuntimeError, match="provider down"):
            mi.timed_complete("w1", "hi", db_path=db_path)
        r = mi.list_invocations("w1", db_path=db_path)[0]
        assert r.ok is False
        assert "provider down" in (r.error or "")


class TestLoopModelActionRecords:
    def test_loop_model_action_records_invocation(self, db_path, monkeypatch):
        import relaydeck.sdk as sdk

        def fake_ex(prompt, *, model="local-fast", max_tokens=256, **kw):
            return "ok", {"provider": "ollama", "model": "llama3.2",
                          "prompt_tokens": 5, "completion_tokens": 2,
                          "total_tokens": 7, "tokens_known": True}
        monkeypatch.setattr(sdk, "complete_with_model_ex", fake_ex)

        bus = PluginEventBus()
        agent = LoopAgent(
            agent_id="w1", name="w1",
            config={"schedule": "interval:1h",
                    "actions": [{"model": {"prompt": "react", "include_event": True}}],
                    "_event_bus": bus},
            workspace=None, db_path=db_path, stop_flag=threading.Event(),
        )
        agent._tick({"trigger": "interval"})
        rows = mi.list_invocations("w1", db_path=db_path)
        assert len(rows) == 1
        assert rows[0].source == "loop"
        assert rows[0].total_tokens == 7


class TestInvocationsAPI:
    def _client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from pathlib import Path as _P
        monkeypatch.setattr(_P, "home", lambda: tmp_path)
        cfg_home = tmp_path / ".relaydeck"
        (cfg_home / "runtime").mkdir(parents=True, exist_ok=True)
        import relaydeck.orchestrator as orch_mod
        orch_mod._orchestrator = None
        from relaydeck.transports.api import create_app
        return TestClient(create_app(cfg_home)), str(cfg_home / "runtime" / "relaydeck.db")

    def test_invocations_endpoint(self, tmp_path, monkeypatch):
        client, db = self._client(tmp_path, monkeypatch)
        mi.record_invocation("w1", model="llama3.2", provider="ollama",
                             latency_ms=90, prompt_tokens=3, completion_tokens=2,
                             total_tokens=5, tokens_known=True,
                             prompt="say hi", response="hi there!", db_path=db)
        res = client.get("/api/automations/w1/invocations")
        assert res.status_code == 200
        body = res.json()
        assert len(body["invocations"]) == 1
        inv = body["invocations"][0]
        assert inv["model"] == "llama3.2"
        assert inv["prompt"] == "say hi"
        assert inv["response"] == "hi there!"
        assert body["rollup"]["count"] == 1
        assert body["rollup"]["total_tokens"] == 5


class TestNextFire:
    def test_interval_next_fire(self):
        from relaydeck.transports.api import _next_fire_at
        nf = _next_fire_at({"kind": "interval", "value": 300.0, "raw": "interval:5m"},
                           1000.0, "running")
        assert nf == 1300.0

    def test_cron_next_fire(self):
        from relaydeck.transports.api import _next_fire_at
        nf = _next_fire_at({"kind": "cron", "value": "* * * * *", "raw": "cron:* * * * *"},
                           None, "running")
        assert nf is not None and nf > 0

    def test_paused_has_no_next_fire(self):
        from relaydeck.transports.api import _next_fire_at
        assert _next_fire_at({"kind": "interval", "value": 60, "raw": "interval:60s"},
                             1000.0, "stopped") is None

    def test_on_event_has_no_next_fire(self):
        from relaydeck.transports.api import _next_fire_at
        assert _next_fire_at({"kind": "on_event", "value": "x", "raw": "on_event:x"},
                             1000.0, "running") is None
