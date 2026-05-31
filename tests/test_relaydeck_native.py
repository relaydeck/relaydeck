"""
Tests for the relaydeck-native harness (`type: relaydeck`).

Pin:
 - plugin manifest parses; Chat/Context tiles are agent-type-gated (applies_to=["relaydeck"])
 - the legacy on_load shim registers the `relaydeck` agent type
 - the layered prompt builder includes the protected contract + editable
   soul/policy, reflects context toggles, sees peer agents + skills
 - generate_reply completes a turn, persists both turns, returns the model
 - a <<relaydeck:message>> action in the reply dispatches + is replaced with a note
 - a completion error is caught (never crashes the caller)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.db import _close_all_pools, open_db
from plugins.harnesses.relaydeck_native import agent as native_agent
from plugins.harnesses.relaydeck_native import prompt as native_prompt


@pytest.fixture
def home(tmp_path):
    _close_all_pools()
    ch = tmp_path / ".relaydeck"
    (ch / "runtime").mkdir(parents=True)
    (ch / "agents").mkdir(parents=True)
    db = str(ch / "runtime" / "relaydeck.db")
    open_db(db).close()
    yield ch, db
    _close_all_pools()


def _write_spec(ch: Path, agent_id: str, *, workspace=None, config=None) -> None:
    import yaml
    (ch / "agents" / f"{agent_id}.yaml").write_text(yaml.safe_dump({
        "id": agent_id, "name": agent_id, "type": "relaydeck",
        "workspace": workspace, "config": config or {},
    }))


def _add_agent_row(db: str, agent_id: str, workspace: str, status="running", purpose=""):
    conn = open_db(db)
    try:
        conn.execute(
            "INSERT INTO agents (id, type, name, status, workspace, auto_start, created_at, purpose) "
            "VALUES (?,?,?,?,?,0,0,?)",
            (agent_id, "pi", agent_id, status, workspace, purpose),
        )
        conn.commit()
    finally:
        conn.close()


# ── manifest + registration ──────────────────────────────────────────


def test_native_agent_preflight_errors_without_pi(home, monkeypatch):
    import threading
    from plugins.harnesses.relaydeck_native.agent import RelaydeckNativeAgent

    monkeypatch.setattr("plugins.harnesses.relaydeck_native.pi_engine.pi_available", lambda: False)
    calls = {"run": 0}
    errors: list[str] = []
    monkeypatch.setattr(
        "plugins.harnesses.pi.agent.PiAgent.run",
        lambda self: calls.__setitem__("run", calls["run"] + 1),
    )
    monkeypatch.setattr(
        RelaydeckNativeAgent, "emit",
        lambda self, et, data=None: errors.append(et) if et == "harness.error" else None,
    )
    monkeypatch.setattr(RelaydeckNativeAgent, "update_status", lambda *a, **k: None)
    a = RelaydeckNativeAgent(
        agent_id="sup", name="sup", config={}, workspace="w",
        db_path=str(home[1]), stop_flag=threading.Event(),
    )
    a._config_home = home[0]
    a.run()
    assert calls["run"] == 0
    assert "harness.error" in errors


def test_context_endpoint_includes_pi_status(home):
    ch, db = home
    from plugins.harnesses.relaydeck_native import agent as native_agent
    _write_spec(ch, "sup")
    out = native_agent.context_endpoint(ch, db, "sup")
    assert "pi_installed" in out


def test_manifest_parses_and_context_tile_is_type_gated():
    from relaydeck.plugin_manifest import find_manifest
    pkg = Path(native_agent.__file__).resolve().parent
    m = find_manifest(pkg)
    assert m.name == "relaydeck-native"
    assert not m.workspace_scoped
    tiles = {t.id: t for t in m.ui_agent_tiles}
    # The chat session renders in the core Terminal tab (PTY harness); the
    # plugin only adds the Context tile, gated to `relaydeck` agents.
    assert "relaydeck-native:context" in tiles
    assert tiles["relaydeck-native:context"].applies_to == ["relaydeck"]


def test_agent_is_a_pi_pty_harness():
    # Native agents run customized pi in the Terminal tab — same harness model
    # as type:pi, with operator prompt + fleet extension injected.
    import threading
    from plugins.harnesses.pi.agent import PiAgent
    assert issubclass(native_agent.RelaydeckNativeAgent, PiAgent)
    a = native_agent.RelaydeckNativeAgent(
        agent_id="sup", name="sup", config={"preset": "local-fast", "tools": ["read"]},
        workspace="w", db_path="/tmp/x.db", stop_flag=threading.Event(),
    )
    cmd = a._build_command()
    assert cmd[0] == "pi"
    assert "--session-dir" in cmd
    assert any("native-sessions" in str(x) for x in cmd)
    assert any("pi_extension.ts" in str(x) for x in cmd)
    assert a._validate_preset() == (True, None)


def test_legacy_on_load_registers_relaydeck_type(home):
    ch, _db = home
    from relaydeck.orchestrator import known_agent_types, register_agent_type  # noqa: F401
    from relaydeck.plugin import PluginContext
    from plugins.harnesses.relaydeck_native.plugin import _legacy_on_load
    _legacy_on_load(PluginContext(config_home=ch))
    assert "relaydeck" in known_agent_types()


# ── prompt builder ───────────────────────────────────────────────────


def test_prompt_layers_contract_soul_policy(home):
    ch, db = home
    composed, layers = native_prompt.build_session(
        agent_id="sup", workspace="w", config={
            "soul": "You are terse.", "policy": "Ask before deleting.",
        }, config_home=ch, db_path=db, history=[],
    )
    by_id = {ly["id"]: ly for ly in layers}
    assert by_id["contract"]["editable"] is False
    assert "safety contract" in by_id["contract"]["body"].lower()
    assert by_id["soul"]["editable"] is True and "terse" in by_id["soul"]["body"]
    assert by_id["policy"]["editable"] is True
    # Contract precedence reasserted at the end (anti prompt-injection).
    assert "takes precedence" in composed


def test_prompt_workspace_context_lists_peers_and_respects_toggle(home):
    ch, db = home
    _add_agent_row(db, "bob", "w", status="running", purpose="does things")
    _add_agent_row(db, "sup", "w")  # self — must be excluded
    composed, layers = native_prompt.build_session(
        agent_id="sup", workspace="w", config={"context": {"events": False}},
        config_home=ch, db_path=db, history=[],
    )
    wctx = next(ly for ly in layers if ly["id"] == "workspace")["body"]
    assert "bob" in wctx and "does things" in wctx
    assert "sup" not in wctx.split("Peer agents:")[1]  # self excluded from peers
    assert "Recent events" not in wctx  # toggle off


def test_prompt_injects_workspace_skills(home):
    ch, db = home
    skill = ch / "workspaces" / "w" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy\ndescription: ship it\n---\nbody")
    _composed, layers = native_prompt.build_session(
        agent_id="sup", workspace="w", config={}, config_home=ch, db_path=db, history=[],
    )
    sk = next((ly for ly in layers if ly["id"] == "skills"), None)
    assert sk and "deploy" in sk["body"] and "ship it" in sk["body"]


# ── generate_reply ───────────────────────────────────────────────────


def test_generate_reply_persists_turns_and_uses_preset(home):
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={
        "preset": "local-fast", "soul": "Be helpful.",
    })
    seen = {}

    def stub(agent_id, model_prompt, *, model, db_path):
        seen["prompt"] = model_prompt
        seen["model"] = model
        return "Hello operator."

    out = native_agent.generate_reply(ch, db, "sup", "hi there", complete=stub)
    assert out["reply"] == "Hello operator."
    assert out["model"] == "local-fast"
    # Soul + contract + the user message all reached the model.
    assert "Be helpful." in seen["prompt"]
    assert "safety contract" in seen["prompt"].lower()
    assert "hi there" in seen["prompt"]

    # Both turns persisted as agent_messages.
    conn = open_db(db)
    try:
        rows = conn.execute(
            "SELECT from_id, to_id, body FROM agent_messages ORDER BY ts"
        ).fetchall()
    finally:
        conn.close()
    assert ("user", "sup", "hi there") in [(r[0], r[1], r[2]) for r in rows]
    assert ("sup", "user", "Hello operator.") in [(r[0], r[1], r[2]) for r in rows]


def test_tool_loop_messages_peer(home, monkeypatch):
    # `relaydeck` tool enabled → the model emits a message tool call; the loop
    # runs it, feeds the result back, then the model gives a final answer.
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["relaydeck"]})
    sent = []

    class _Orch:
        def send_message_to(self, to, body, *, from_id=None, in_reply_to=None):
            sent.append((to, body, from_id))
            return ("msg_x", True)

    monkeypatch.setattr("relaydeck.orchestrator.get_orchestrator", lambda *a, **k: _Orch())
    replies = iter([
        "Checking. <<tool name=message to=bob>>what's your status?<<end>>",
        "Pinged bob for status.",
    ])
    out = native_agent.generate_reply(ch, db, "sup", "ping bob",
                                      complete=lambda *a, **k: next(replies))
    assert sent == [("bob", "what's your status?", "sup")]
    assert out["reply"] == "Pinged bob for status."
    assert out["tools"][0]["calls"] == ["message"]


def test_tool_loop_read_is_workspace_scoped(home, tmp_path):
    ch, db = home
    from relaydeck.config import register_workspace
    ws = tmp_path / "wsdir"
    ws.mkdir()
    register_workspace(ch, "w", ws, [])
    (ws / "hello.txt").write_text("hi there")
    _write_spec(ch, "sup", workspace="w", config={"tools": ["read"]})
    replies = iter(["<<tool name=read path=hello.txt>><<end>>", "It says hi."])
    out = native_agent.generate_reply(ch, db, "sup", "read hello.txt",
                                      complete=lambda *a, **k: next(replies))
    assert "hi there" in out["tools"][0]["observations"]
    assert out["reply"] == "It says hi."


def test_disabled_tool_is_refused_with_feedback(home):
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["read"]})  # no bash
    replies = iter(["<<tool name=bash>>ls<<end>>", "Can't — no bash access."])
    out = native_agent.generate_reply(ch, db, "sup", "run ls",
                                      complete=lambda *a, **k: next(replies))
    assert "refused" in out["tools"][0]["observations"]
    assert out["reply"] == "Can't — no bash access."


def test_dashboard_tool_persists_scalar_op(home, monkeypatch):
    # A scalar op (theme/accent/density/glow) persists server-side + emits
    # appearance.changed so any open dashboard repaints — NOT dashboard.command
    # (which is reserved for live widget/layout ops).
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["dashboard"]})
    emitted = []

    class _Bus:
        def emit(self, ev):
            emitted.append(ev)

    class _Reg:
        _event_bus = _Bus()

    monkeypatch.setattr("relaydeck.plugin.get_registry", lambda *a, **k: _Reg())
    monkeypatch.setattr("relaydeck.orchestrator.get_orchestrator",
                        lambda *a, **k: type("_O", (), {"config_home": ch})())
    replies = iter([
        "<<tool name=dashboard op=accent value=violet>><<end>>",
        "Done — accent is violet now.",
    ])
    out = native_agent.generate_reply(ch, db, "sup", "make it violet",
                                      complete=lambda *a, **k: next(replies))
    assert emitted and emitted[0].type == "appearance.changed"
    from relaydeck.preferences import resolve_appearance
    assert resolve_appearance(ch, "w")["theme"] == "violet"  # accent -> theme
    assert out["reply"] == "Done — accent is violet now."


def test_dashboard_move_widget_emits_coords(home, monkeypatch):
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["dashboard"]})
    emitted = []

    class _Reg:
        _event_bus = type("_B", (), {"emit": staticmethod(lambda ev: emitted.append(ev))})()

    monkeypatch.setattr("relaydeck.plugin.get_registry", lambda *a, **k: _Reg())
    replies = iter([
        "<<tool name=dashboard op=move_widget value=clock x=9 y=0>><<end>>",
        "moved clock to the top-right",
    ])
    native_agent.generate_reply(ch, db, "sup", "move clock top-right",
                                complete=lambda *a, **k: next(replies))
    assert emitted[0].data == {"op": "move_widget", "value": "clock", "x": 9, "y": 0}


def test_empty_completion_is_retried_once(home):
    # A blank first completion is nudged + retried rather than surfaced.
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={})
    replies = iter(["   ", "here is my real answer"])
    out = native_agent.generate_reply(ch, db, "sup", "hi",
                                      complete=lambda *a, **k: next(replies))
    assert out["reply"] == "here is my real answer"


def test_tool_run_emits_live_step(home, monkeypatch):
    # Each tool run pushes a relaydeck.native.step over the SSE bus so the chat
    # widget can show 'things happening' mid-turn.
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["dashboard"]})
    import relaydeck.orchestrator as orch_mod
    steps = []
    monkeypatch.setattr(orch_mod._bus, "publish",
                        lambda *a, **k: steps.append(a[1] if len(a) > 1 else None))

    class _Reg:
        _event_bus = type("_B", (), {"emit": staticmethod(lambda ev: None)})()

    monkeypatch.setattr("relaydeck.plugin.get_registry", lambda *a, **k: _Reg())
    replies = iter(["<<tool name=dashboard op=tidy>><<end>>", "tidied"])
    native_agent.generate_reply(ch, db, "sup", "tidy",
                                complete=lambda *a, **k: next(replies))
    assert "relaydeck.native.step" in steps


def test_tokens_known_false_when_provider_says_so(monkeypatch):
    # A provider reporting tokens_known=False (e.g. openrouter with no usage
    # block) must NOT be recorded as a confident zero.
    from relaydeck import sdk

    class _P:
        def complete_ex(self, prompt, *, model, max_tokens, **k):
            return {"text": "hi", "prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": 0, "tokens_known": False}

    monkeypatch.setattr("relaydeck.plugin.get_provider", lambda n: _P())
    _t, usage = sdk.complete_with_model_ex("hi", model="openrouter/x")
    assert usage["tokens_known"] is False


def test_tokens_known_defaults_true_for_reporting_providers(monkeypatch):
    from relaydeck import sdk

    class _P:
        def complete_ex(self, prompt, *, model, max_tokens, **k):
            return {"text": "hi", "prompt_tokens": 5, "completion_tokens": 2,
                    "total_tokens": 7}  # no tokens_known key -> defaults True

    monkeypatch.setattr("relaydeck.plugin.get_provider", lambda n: _P())
    _t, usage = sdk.complete_with_model_ex("hi", model="ollama/x")
    assert usage["tokens_known"] is True and usage["prompt_tokens"] == 5


def test_complete_with_model_ex_falls_back_to_reasoning_text(monkeypatch):
    from relaydeck import sdk

    class _P:
        def complete_ex(self, prompt, *, model, max_tokens, **k):
            return {"text": "", "reasoning": "brief body",
                    "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}

    monkeypatch.setattr("relaydeck.plugin.get_provider", lambda n: _P())
    text, usage = sdk.complete_with_model_ex("hi", model="deepseek/x")
    assert text == "brief body"
    assert usage["reasoning"] == "brief body"


def test_render_ui_state_is_spatial():
    s = native_agent._render_ui_state(
        {"dashboard_layout": [{"key": "fleet", "x": 0, "y": 0, "w": 8, "h": 3}]})
    assert "fleet @ (0,0) 8x3" in s and "12 columns" in s


def test_render_ui_state_falls_back_to_keys():
    s = native_agent._render_ui_state({"dashboard_widgets": ["clock", "fleet"]})
    assert "clock" in s and "fleet" in s


def test_dashboard_tool_refused_without_capability(home):
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["read"]})  # no dashboard
    replies = iter(["<<tool name=dashboard op=accent value=violet>><<end>>", "can't"])
    out = native_agent.generate_reply(ch, db, "sup", "x",
                                      complete=lambda *a, **k: next(replies))
    assert "refused" in out["tools"][0]["observations"]


def test_dashboard_tool_rejects_unknown_op(home, monkeypatch):
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["dashboard"]})

    class _Reg:
        _event_bus = type("_B", (), {"emit": staticmethod(lambda ev: None)})()

    monkeypatch.setattr("relaydeck.plugin.get_registry", lambda *a, **k: _Reg())
    replies = iter(["<<tool name=dashboard op=explode>><<end>>", "nope"])
    out = native_agent.generate_reply(ch, db, "sup", "x",
                                      complete=lambda *a, **k: next(replies))
    assert "error" in out["tools"][0]["observations"]


def test_ui_state_injected_into_prompt(home):
    # The browser's dashboard widget list reaches the model so it can be
    # dashboard-aware (answer "what's on the dashboard").
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={})
    seen = {}

    def stub(agent_id, model_prompt, *, model, db_path):
        seen["p"] = model_prompt
        return "you have a clock and a fleet widget"

    native_agent.generate_reply(ch, db, "sup", "what widgets?", complete=stub,
                                ui_state="Current dashboard widgets: clock, fleet")
    assert "Current dashboard widgets: clock, fleet" in seen["p"]


def test_empty_reply_after_tools_says_done(home, monkeypatch):
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["dashboard"]})

    class _Reg:
        _event_bus = type("_B", (), {"emit": staticmethod(lambda ev: None)})()

    monkeypatch.setattr("relaydeck.plugin.get_registry", lambda *a, **k: _Reg())
    replies = iter(["<<tool name=dashboard op=tidy>><<end>>", "   "])  # tool then blank
    out = native_agent.generate_reply(ch, db, "sup", "tidy up",
                                      complete=lambda *a, **k: next(replies))
    assert out["reply"] == "Done."


def test_chat_endpoint_passes_dashboard_widgets(home, monkeypatch):
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={})
    captured = {}

    def fake_reply(*a, ui_state=None, **k):
        captured["ui_state"] = ui_state
        return {"reply": "ok", "model": "m", "tools": []}

    monkeypatch.setattr(native_agent, "generate_reply", fake_reply)
    native_agent.chat_endpoint(ch, db, {
        "agent_id": "sup", "text": "what widgets?",
        "dashboard_widgets": ["clock", "fleet"],
    })
    assert "clock" in (captured.get("ui_state") or "")


def test_manifest_has_chat_widget():
    from relaydeck.plugin_manifest import find_manifest
    m = find_manifest(Path(native_agent.__file__).resolve().parent)
    assert "relaydeck-native:chat" in {w.id for w in m.ui_widgets}


def test_tool_safe_path_blocks_traversal():
    from pathlib import Path as _P
    from plugins.harnesses.relaydeck_native import tools as T
    with pytest.raises(Exception):
        T._safe_path(_P("/tmp/ws"), "../../etc/passwd")
    with pytest.raises(Exception):
        T._safe_path(_P("/tmp/ws"), "/etc/passwd")


class _FakeOrch:
    """Minimal orchestrator stub for manage-tool tests. `agents` maps
    id -> workspace."""
    def __init__(self, agents):
        self._agents = agents
        self.acted = []

    def list_agents(self):
        return [{"id": i, "status": "running", "workspace": w} for i, w in self._agents.items()]

    def get_agent(self, i):
        return {"id": i, "workspace": self._agents[i]} if i in self._agents else None

    def start_agent(self, a):
        self.acted.append(("start", a))

    def stop_agent(self, a):
        self.acted.append(("stop", a)); return True


def test_manage_tool_stops_same_workspace_agent(home, monkeypatch):
    # `manage` → can stop a peer IN THE SAME workspace.
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["manage"]})
    orch = _FakeOrch({"alice": "w", "sup": "w"})
    monkeypatch.setattr("relaydeck.orchestrator.get_orchestrator", lambda *a, **k: orch)
    replies = iter(["<<tool name=stop agent=alice>><<end>>", "Stopped alice."])
    out = native_agent.generate_reply(ch, db, "sup", "stop alice",
                                      complete=lambda *a, **k: next(replies))
    assert ("stop", "alice") in orch.acted
    assert out["reply"] == "Stopped alice."


def test_manage_tool_refuses_cross_workspace(home, monkeypatch):
    # A native agent in workspace `w` must NOT stop an agent in `other`.
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["manage"]})
    orch = _FakeOrch({"sup": "w", "victim": "other"})
    monkeypatch.setattr("relaydeck.orchestrator.get_orchestrator", lambda *a, **k: orch)
    replies = iter(["<<tool name=stop agent=victim>><<end>>", "couldn't"])
    out = native_agent.generate_reply(ch, db, "sup", "stop victim",
                                      complete=lambda *a, **k: next(replies))
    assert orch.acted == []  # never touched the cross-workspace agent
    assert "another workspace" in out["tools"][0]["observations"]


def test_manage_agents_listing_is_workspace_scoped(home, monkeypatch):
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["manage"]})
    orch = _FakeOrch({"sup": "w", "alice": "w", "elsewhere": "other"})
    monkeypatch.setattr("relaydeck.orchestrator.get_orchestrator", lambda *a, **k: orch)
    replies = iter(["<<tool name=agents>><<end>>", "listed"])
    out = native_agent.generate_reply(ch, db, "sup", "list agents",
                                      complete=lambda *a, **k: next(replies))
    obs = out["tools"][0]["observations"]
    assert "alice" in obs and "elsewhere" not in obs


def test_manage_tool_refused_without_capability(home):
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["read"]})  # no manage
    replies = iter(["<<tool name=stop agent=alice>><<end>>", "Can't — no manage access."])
    out = native_agent.generate_reply(ch, db, "sup", "stop alice",
                                      complete=lambda *a, **k: next(replies))
    assert "refused" in out["tools"][0]["observations"]


def test_generate_reply_catches_model_error(home):
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={})

    def boom(agent_id, model_prompt, *, model, db_path):
        raise RuntimeError("provider down")

    out = native_agent.generate_reply(ch, db, "sup", "hi", complete=boom)
    assert "model error" in out["reply"] and "provider down" in out["reply"]


def test_tool_loop_caps_iterations(home):
    # A model that emits a tool block forever stops at MAX_TOOL_ITERS
    # instead of looping unbounded.
    from plugins.harnesses.relaydeck_native import tools as T
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["read"]})
    n = {"calls": 0}

    def loopy(*a, **k):
        n["calls"] += 1
        return "<<tool name=read path=x.txt>><<end>>"  # never a final answer

    out = native_agent.generate_reply(ch, db, "sup", "go", complete=loopy)
    assert n["calls"] == T.MAX_TOOL_ITERS
    assert out["reply"]  # still returns something (the cap message)


# ── tools unit ───────────────────────────────────────────────────────


def test_parse_calls_handles_attrs_and_body():
    from plugins.harnesses.relaydeck_native import tools as T
    calls = T.parse_calls(
        "before <<tool name=read path=a/b.py>><<end>> mid "
        "<<tool name=bash>>\nls -la\n<<end>> after"
    )
    assert [c["name"] for c in calls] == ["read", "bash"]
    assert calls[0]["attrs"]["path"] == "a/b.py"
    assert calls[1]["body"] == "ls -la"


def test_strip_tool_blocks_leaves_prose():
    from plugins.harnesses.relaydeck_native import tools as T
    assert T.strip_tool_blocks("ok <<tool name=read path=x>><<end>> done") == "ok  done"


def test_capabilities_operator_surface_with_bash(home):
    """A bash-capable native agent is told the relaydeck CLI is its full
    read+write control surface (god mode) — not 'no other powers'."""
    ch, db = home
    composed, layers = native_prompt.build_session(
        agent_id="sup", workspace="w", config={"tools": ["bash", "dashboard"]},
        config_home=ch, db_path=db, history=[],
    )
    caps = next(ly for ly in layers if ly["id"] == "capabilities")["body"]
    assert "Operator control surface" in caps
    assert "relaydeck theme set" in caps and "relaydeck agent" in caps
    assert "NEVER read their values" in caps          # secrets boundary kept
    assert "no other powers" not in caps              # the cage is gone


def test_capabilities_no_operator_surface_without_bash(home):
    """An agent WITHOUT bash stays scoped to its explicit tools."""
    ch, db = home
    _composed, layers = native_prompt.build_session(
        agent_id="sup", workspace="w", config={"tools": ["dashboard"]},
        config_home=ch, db_path=db, history=[],
    )
    caps = next(ly for ly in layers if ly["id"] == "capabilities")["body"]
    assert "Operator control surface" not in caps
    assert "relaydeck_dashboard" in caps
    assert "daylight" in caps and "not 'light'" in caps
    assert "fleet @ (0,0)" in caps
    assert "name=dashboard" not in caps


def test_dashboard_get_reads_appearance(home, monkeypatch):
    """op=get returns the resolved appearance so the agent can answer
    'what theme are we using?' instead of guessing."""
    from plugins.harnesses.relaydeck_native import tools as T
    from relaydeck.preferences import set_appearance
    ch, db = home
    set_appearance(ch, {"theme": "gruvbox-dark", "density": "compact"})
    monkeypatch.setattr("relaydeck.orchestrator.get_orchestrator",
                        lambda *a, **k: type("_O", (), {"config_home": ch})())
    out = T.run_calls(
        [{"name": "dashboard", "attrs": {"op": "get"}, "body": ""}],
        enabled={"dashboard"}, workspace_path=None, agent_id="sup", workspace="w",
    )
    assert "theme=gruvbox-dark" in out and "density=compact" in out
    assert "daylight" in out and "light UI" in out
    assert "fleet @ (0,0)" in out


def test_dashboard_theme_validates(home, monkeypatch):
    from plugins.harnesses.relaydeck_native import tools as T
    ch, db = home
    emitted = []
    monkeypatch.setattr("relaydeck.orchestrator.get_orchestrator",
                        lambda *a, **k: type("_O", (), {"config_home": ch})())
    monkeypatch.setattr("relaydeck.plugin.get_registry",
                        lambda *a, **k: type("_R", (), {
                            "_event_bus": type("_B", (), {
                                "emit": staticmethod(lambda ev: emitted.append(ev))})()})())
    # unknown theme → refused with the available list
    bad = T.run_calls([{"name": "dashboard", "attrs": {"op": "theme", "value": "nope"}, "body": ""}],
                      enabled={"dashboard"}, workspace_path=None, agent_id="sup", workspace="w")
    assert "unknown theme" in bad and not emitted
    # a builtin theme → persists + emits appearance.changed (not dashboard.command)
    ok = T.run_calls([{"name": "dashboard", "attrs": {"op": "theme", "value": "amber"}, "body": ""}],
                     enabled={"dashboard"}, workspace_path=None, agent_id="sup", workspace="w")
    assert emitted and emitted[0].type == "appearance.changed"
    assert "theme=amber" in ok
    from relaydeck.preferences import resolve_appearance
    assert resolve_appearance(ch, "w")["theme"] == "amber"


def test_chat_continues_session_by_default(home):
    """History persists + is loaded each turn — the chat continues by
    default (no fresh session unless asked)."""
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={})
    native_agent.generate_reply(ch, db, "sup", "remember: my name is Sam",
                                complete=lambda *a, **k: "Noted.")
    # Next turn's prompt includes the prior conversation.
    captured = {}
    def _cap(agent_id, prompt, **k):
        captured["p"] = prompt
        return "ok"
    native_agent.generate_reply(ch, db, "sup", "what's my name?", complete=_cap)
    assert "my name is Sam" in captured["p"]


def test_new_session_forgets_prior_turns(home):
    """reset_session sets a boundary so earlier turns drop out of context,
    without destroying the durable record."""
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={})
    native_agent.generate_reply(ch, db, "sup", "secret: blue42",
                                complete=lambda *a, **k: "ok")
    native_agent.reset_session(ch, "sup")
    captured = {}
    def _cap(agent_id, prompt, **k):
        captured["p"] = prompt
        return "ok"
    native_agent.generate_reply(ch, db, "sup", "anything?", complete=_cap)
    assert "blue42" not in captured["p"]            # forgotten from context
    # …but the durable rows are still there.
    rows = native_agent._load_history(db, "sup", "user")  # no `since` → all
    assert any("blue42" in r["text"] for r in rows)


def test_max_tool_iters_config_override(home, monkeypatch):
    """config.max_tool_iters overrides the default cap."""
    ch, db = home
    _write_spec(ch, "sup", workspace="w", config={"tools": ["bash"], "max_tool_iters": 2})
    calls = {"n": 0}
    def _loop(agent_id, prompt, **k):
        calls["n"] += 1
        return "<<tool name=bash>>\necho hi\n<<end>>"   # always emits a tool → loops
    out = native_agent.generate_reply(ch, db, "sup", "go", complete=_loop)
    # Capped at 2 iterations (not the default 25).
    assert calls["n"] == 2
    assert "tool-iteration limit" in out["reply"]


def test_bash_and_write_tools_execute(tmp_path):
    from plugins.harnesses.relaydeck_native import tools as T
    ws = tmp_path / "ws"
    ws.mkdir()
    # write
    out = T.run_calls(
        [{"name": "write", "attrs": {"path": "note.txt"}, "body": "hi"}],
        enabled={"write"}, workspace_path=ws, agent_id="sup",
    )
    assert "wrote" in out and (ws / "note.txt").read_text() == "hi"
    # bash
    out = T.run_calls(
        [{"name": "bash", "attrs": {}, "body": "echo marker123"}],
        enabled={"bash"}, workspace_path=ws, agent_id="sup",
    )
    assert "marker123" in out and "exit 0" in out


def test_emit_usage_publishes_record(monkeypatch):
    # _default_complete should emit a usage.record so metering writes
    # usage_records (model chip + tokens/cost). Verify the emit shape.
    emitted = []

    class _Bus:
        def emit(self, ev):
            emitted.append(ev)

    class _Reg:
        _event_bus = _Bus()

    monkeypatch.setattr("relaydeck.plugin.get_registry", lambda *a, **k: _Reg())
    native_agent._emit_usage("sup", "gemma", {
        "provider": "ollama", "prompt_tokens": 10, "completion_tokens": 5,
    })
    assert emitted, "no usage.record emitted"
    ev = emitted[0]
    assert ev.type == "usage.record"
    assert ev.data["agent_id"] == "sup" and ev.data["model"] == "gemma"
    assert ev.data["prompt"] == 10 and ev.data["completion"] == 5
