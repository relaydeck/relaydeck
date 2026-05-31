"""
Messaging primitive — core helpers, orchestrator send path, drain on
start, format resolution, state.py, skill materialization, and the
plugin's CLI fallback behavior.

The CLI's daemon-HTTP path is exercised by mocking `urlopen`. The
fallback path writes straight to SQLite, so a daemon-unreachable
scenario is asserted by stubbing the daemon URL to a closed port.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.db import open_db
from relaydeck.messages import (
    DEFAULT_FORMAT,
    Message,
    drain_pending,
    insert_message,
    list_inbox,
    list_one,
    list_workspace_inbox,
    mark_injected,
    render_format,
)

# ── Setup helpers ───────────────────────────────────────────────────


def _tmp_db(tmp_path) -> str:
    p = tmp_path / "relaydeck.db"
    # First open initializes the schema + migrations.
    open_db(p).close()
    return str(p)


def _seed_agent(tmp_path, monkeypatch, agent_id: str, workspace: str = "demo") -> str:
    """Make sure the daemon DB has a row for this agent so endpoints
    that check existence don't 404. Returns the db path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True, exist_ok=True)
    db_path = str(cfg_home / "runtime" / "relaydeck.db")
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO agents "
            "(id, type, name, status, workspace, auto_start, created_at) "
            "VALUES (?, 'pi', ?, 'stopped', ?, 0, 0)",
            (agent_id, agent_id, workspace),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


# ── Core helpers ────────────────────────────────────────────────────


def test_insert_and_list_one(tmp_path):
    db = _tmp_db(tmp_path)
    msg_id = insert_message("alice", "bob", "hello", workspace="ws", db_path=db)
    msg = list_one(msg_id, db_path=db)
    assert msg is not None
    assert msg.from_id == "alice"
    assert msg.to_id == "bob"
    assert msg.body == "hello"
    assert msg.workspace == "ws"
    assert msg.injected_at is None
    assert msg.rendered_body is None


def test_drain_pending_is_ordered_and_filters_delivered(tmp_path):
    db = _tmp_db(tmp_path)
    id_a = insert_message("u", "bob", "first", db_path=db)
    id_b = insert_message("u", "bob", "second", db_path=db)
    insert_message("u", "alice", "for-alice", db_path=db)  # ignored
    mark_injected(id_a, rendered_body="[from u] first", db_path=db)

    pending = drain_pending("bob", db_path=db)
    assert [m.id for m in pending] == [id_b]
    assert pending[0].rendered_body is None


def test_mark_injected_stores_rendered_body(tmp_path):
    db = _tmp_db(tmp_path)
    msg_id = insert_message("u", "bob", "hi", db_path=db)
    mark_injected(msg_id, rendered_body="[from u] hi", db_path=db)
    msg = list_one(msg_id, db_path=db)
    assert msg is not None and msg.injected_at is not None
    assert msg.rendered_body == "[from u] hi"


def test_list_inbox_newest_first_and_unread(tmp_path):
    db = _tmp_db(tmp_path)
    id_old = insert_message("u", "bob", "old", db_path=db)
    id_new = insert_message("u", "bob", "new", db_path=db)
    mark_injected(id_old, rendered_body="[u] old", db_path=db)

    all_msgs = list_inbox("bob", db_path=db)
    assert [m.id for m in all_msgs] == [id_new, id_old]

    only_unread = list_inbox("bob", unread=True, db_path=db)
    assert [m.id for m in only_unread] == [id_new]


def test_list_workspace_inbox(tmp_path):
    db = _tmp_db(tmp_path)
    insert_message("u", "alice", "for-alice", workspace="demo", db_path=db)
    insert_message("u", "bob", "for-bob", workspace="demo", db_path=db)
    insert_message("u", "carol", "elsewhere", workspace="other", db_path=db)

    rows = list_workspace_inbox("demo", db_path=db)
    assert {m.to_id for m in rows} == {"alice", "bob"}

    only_alice = list_workspace_inbox("demo", agent="alice", db_path=db)
    assert [m.to_id for m in only_alice] == ["alice"]


# ── Format rendering (resolution order) ─────────────────────────────


def _msg(from_id="alice", body="hello") -> Message:
    return Message(
        id="msg_x", from_id=from_id, to_id="bob", body=body,
        ts=1000.0, in_reply_to=None,
    )


def test_render_format_builtin_default():
    out = render_format(_msg())
    # The default's `[relay ...]` prefix is load-bearing — it's the
    # signal the receiving agent uses to recognize a peer message.
    # If you change the default, update the SKILL.md teaching to match.
    assert out == "[relay from=alice id=msg_x] hello"
    assert out.startswith("[relay "), \
        "default format must include the `[relay ` prefix marker"


def test_render_format_exposes_id_to_for_templates():
    """`{id}` and `{to}` should be available so a custom template can
    surface the message id (for replies) and the recipient id."""
    out = render_format(
        _msg(), format="msg {id} → {to}: {body}",
    )
    assert out == "msg msg_x → bob: hello"


def test_render_format_plugin_default():
    out = render_format(_msg(), plugin_default="<<{from}>> {body}")
    assert out == "<<alice>> hello"


def test_render_format_per_agent_overrides_plugin():
    out = render_format(
        _msg(),
        plugin_default="PLUGIN: {body}",
        agent_config={"message_format": "AGENT: {body}"},
    )
    assert out == "AGENT: hello"


def test_render_format_per_message_overrides_all():
    out = render_format(
        _msg(),
        format="EXPLICIT {body}",
        plugin_default="PLUGIN: {body}",
        agent_config={"message_format": "AGENT: {body}"},
    )
    assert out == "EXPLICIT hello"


def test_render_format_unknown_placeholder_is_kept_literal():
    # A typo'd template shouldn't crash — placeholder is left as-is.
    out = render_format(_msg(), format="{from}: {body} | {oops}")
    assert "{oops}" in out
    assert "alice" in out


# ── State.py resolution order ───────────────────────────────────────


def test_state_current_workspace_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.state import get_current_workspace, set_current_workspace
    set_current_workspace("from-file")
    monkeypatch.setenv("RELAYDECK_WORKSPACE", "from-env")
    assert get_current_workspace() == "from-env"
    monkeypatch.delenv("RELAYDECK_WORKSPACE")
    assert get_current_workspace() == "from-file"


def test_state_daemon_url_default(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_DAEMON_URL", raising=False)
    from relaydeck.state import DEFAULT_DAEMON_URL, get_daemon_url, set_daemon_url
    assert get_daemon_url() == DEFAULT_DAEMON_URL
    set_daemon_url("http://127.0.0.1:9999")
    assert get_daemon_url() == "http://127.0.0.1:9999"
    monkeypatch.setenv("RELAYDECK_DAEMON_URL", "http://override:8000")
    assert get_daemon_url() == "http://override:8000"


# ── Orchestrator.send_message_to ────────────────────────────────────


def _make_orch(tmp_path, monkeypatch):
    """Build a minimal Orchestrator with a stubbed plugin bus."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.orchestrator import Orchestrator
    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True, exist_ok=True)
    (cfg_home / "runtime").mkdir(exist_ok=True)
    orch = Orchestrator(cfg_home)
    captured: list = []

    class _FakeBus:
        def emit(self, event):
            captured.append(event)

    orch._plugin_event_bus = _FakeBus()
    return orch, captured


def test_send_message_to_persists_when_target_stopped(tmp_path, monkeypatch):
    _seed_agent(tmp_path, monkeypatch, "bob", workspace="demo")
    orch, captured = _make_orch(tmp_path, monkeypatch)
    msg_id, injected = orch.send_message_to("bob", "hello", from_id="user")
    assert injected is False
    assert msg_id.startswith("msg_")

    msg = list_one(msg_id, db_path=orch.db_path)
    assert msg is not None and msg.injected_at is None

    # Plugin bus saw the event with injected=false
    assert any(e.type == "agent.message" and e.data["injected"] is False
               for e in captured)


def test_send_message_to_pushes_when_target_running(tmp_path, monkeypatch):
    _seed_agent(tmp_path, monkeypatch, "bob", workspace="demo")
    orch, _ = _make_orch(tmp_path, monkeypatch)

    pushed: list[str] = []

    class _FakeInstance:
        def send_message(self, text: str) -> bool:
            pushed.append(text)
            return True

    orch._instances["bob"] = _FakeInstance()  # type: ignore[assignment]

    msg_id, injected = orch.send_message_to("bob", "hello", from_id="alice")
    assert injected is True
    expected = f"[relay from=alice id={msg_id}] hello"
    assert pushed == [expected]
    msg = list_one(msg_id, db_path=orch.db_path)
    assert msg is not None and msg.injected_at is not None
    assert msg.rendered_body == expected


def test_send_message_to_does_not_mark_injected_on_write_failure(tmp_path, monkeypatch):
    """Critical regression guard — PTY write failure must not
    silently mark the message delivered."""
    _seed_agent(tmp_path, monkeypatch, "bob", workspace="demo")
    orch, _ = _make_orch(tmp_path, monkeypatch)

    class _FailingInstance:
        def send_message(self, text: str) -> bool:
            return False

    orch._instances["bob"] = _FailingInstance()  # type: ignore[assignment]

    msg_id, injected = orch.send_message_to("bob", "hi", from_id="user")
    assert injected is False
    msg = list_one(msg_id, db_path=orch.db_path)
    assert msg is not None
    assert msg.injected_at is None
    assert msg.rendered_body is None


def test_send_message_to_unknown_agent_raises(tmp_path, monkeypatch):
    orch, _ = _make_orch(tmp_path, monkeypatch)
    try:
        orch.send_message_to("ghost", "hi", from_id="user")
    except ValueError as exc:
        assert "ghost" in str(exc)
    else:
        raise AssertionError("expected ValueError")


# ── Skill materialization (plugin) ──────────────────────────────────


def _setup_workspace_registry(
    tmp_path,
    workspaces: list[str],
    *,
    enable_messaging: bool = True,
) -> Path:
    """Write a config.toml so load_workspace_registry returns the
    workspaces the test wants. Each workspace also gets an agent.toml
    listing `messaging` in its plugins by default — the skill plugin
    only ships its SKILL.md to opted-in workspaces. Pass
    enable_messaging=False to simulate a workspace that didn't opt in."""
    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True, exist_ok=True)
    body = ""
    for w in workspaces:
        ws_path = tmp_path / w
        ws_path.mkdir(parents=True, exist_ok=True)
        body += f'[[workspace]]\nname = "{w}"\npath = "{ws_path}"\n\n'

        # Per-workspace agent.toml — required for the messaging plugin
        # to consider this workspace opted-in.
        ws_home = cfg_home / "workspaces" / w
        ws_home.mkdir(parents=True, exist_ok=True)
        plugins_str = '["messaging"]' if enable_messaging else "[]"
        (ws_home / "agent.toml").write_text(
            f"[workspace]\nplugins = {plugins_str}\n"
        )
    (cfg_home / "config.toml").write_text(body)
    return cfg_home


def _messaging_entry(cfg_home):
    """Build a skills-manager registry entry pointing at the REAL
    messaging plugin (its plugin dir + manifest skill + live instance).
    Materialization is now owned by the `skills` plugin's manager, so the
    messaging tests assert behavior *through* that path."""
    from types import SimpleNamespace

    from plugins.messaging import plugin as msg
    from relaydeck.testing import MockHost

    # The provider hooks read self._config_home — set it via on_load.
    msg.PLUGIN.on_load(MockHost(name="messaging", config_home=cfg_home))
    plugin_dir = Path(msg.__file__).parent
    return SimpleNamespace(
        name=msg.PLUGIN_NAME,
        instance=msg.PLUGIN,
        manifest=SimpleNamespace(skills={"relaydeck-cli": "SKILL.md"}),
        path=plugin_dir,
    ), SimpleNamespace(all=lambda: [SimpleNamespace(
        name=msg.PLUGIN_NAME, instance=msg.PLUGIN,
        manifest=SimpleNamespace(skills={"relaydeck-cli": "SKILL.md"}),
        path=plugin_dir)])


def test_messaging_skill_materializes_for_opted_in_workspace(tmp_path, monkeypatch):
    """Through the generic skills manager, messaging's relaydeck-cli skill is
    materialized into a workspace that opted into messaging."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = _setup_workspace_registry(tmp_path, ["demo"])
    from plugins.skills import manager
    _entry, reg = _messaging_entry(cfg_home)

    manager.sync_all(cfg_home, registry=reg)

    skill = cfg_home / "workspaces" / "demo" / "runtime" / "skills" / "relaydeck-cli" / "SKILL.md"
    assert skill.exists()
    body = skill.read_text()
    assert "relaydeck workspace message" in body
    assert body.startswith("---\n"), "SKILL.md must start with YAML frontmatter fence"
    head = body.split("---", 2)[1]
    assert "name:" in head and "description:" in head


def test_messaging_skill_skipped_when_workspace_did_not_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = _setup_workspace_registry(tmp_path, ["demo"], enable_messaging=False)
    from plugins.skills import manager
    _entry, reg = _messaging_entry(cfg_home)

    manager.sync_all(cfg_home, registry=reg)

    skill = cfg_home / "workspaces" / "demo" / "runtime" / "skills" / "relaydeck-cli" / "SKILL.md"
    assert not skill.exists()


def test_messaging_skill_only_for_opted_in_workspaces(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True, exist_ok=True)
    for w in ("yes", "no"):
        (tmp_path / w).mkdir()
        (cfg_home / "workspaces" / w).mkdir(parents=True)
    (cfg_home / "config.toml").write_text(
        f'[[workspace]]\nname = "yes"\npath = "{tmp_path / "yes"}"\n\n'
        f'[[workspace]]\nname = "no"\npath = "{tmp_path / "no"}"\n'
    )
    (cfg_home / "workspaces" / "yes" / "agent.toml").write_text(
        '[workspace]\nplugins = ["messaging"]\n'
    )
    (cfg_home / "workspaces" / "no" / "agent.toml").write_text('[workspace]\nplugins = []\n')
    from plugins.skills import manager
    _entry, reg = _messaging_entry(cfg_home)

    manager.sync_all(cfg_home, registry=reg)

    yes_skill = cfg_home / "workspaces" / "yes" / "runtime" / "skills" / "relaydeck-cli" / "SKILL.md"
    no_skill = cfg_home / "workspaces" / "no" / "runtime" / "skills" / "relaydeck-cli" / "SKILL.md"
    assert yes_skill.exists()
    assert not no_skill.exists()


def test_messaging_skill_content_override(tmp_path, monkeypatch):
    """The `skill_content` setting override flows through the provider
    hook into the materialized SKILL.md."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = _setup_workspace_registry(tmp_path, ["demo"])
    from relaydeck.plugin_settings import set_settings
    from plugins.skills import manager

    new_body = "# Custom\n\nbe nice to peers\n"
    set_settings("messaging", {"skill_content": new_body, "inject_skill": True,
                               "default_format": DEFAULT_FORMAT})
    _entry, reg = _messaging_entry(cfg_home)
    manager.sync_all(cfg_home, registry=reg)
    skill_path = cfg_home / "workspaces" / "demo" / "runtime" / "skills" / "relaydeck-cli" / "SKILL.md"
    assert skill_path.read_text() == new_body


def test_messaging_inject_skill_false_targets_nothing(tmp_path, monkeypatch):
    """`inject_skill=false` makes the provider hook return no targets, so
    the manager materializes nothing (and removes any prior skill)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = _setup_workspace_registry(tmp_path, ["demo"])
    from relaydeck.plugin_settings import set_settings
    from plugins.skills import manager

    _entry, reg = _messaging_entry(cfg_home)
    manager.sync_all(cfg_home, registry=reg)
    skill_path = cfg_home / "workspaces" / "demo" / "runtime" / "skills" / "relaydeck-cli" / "SKILL.md"
    assert skill_path.exists()

    set_settings("messaging", {"inject_skill": False, "default_format": DEFAULT_FORMAT,
                               "skill_content": ""})
    manager.sync_all(cfg_home, registry=reg)
    assert not skill_path.exists()


def test_messaging_provider_hooks_directly(tmp_path, monkeypatch):
    """Unit-level pin on the provider hooks the skills manager calls."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = _setup_workspace_registry(tmp_path, ["demo"])
    from plugins.messaging import plugin as msg
    from relaydeck.testing import MockHost

    msg.PLUGIN.on_load(MockHost(name="messaging", config_home=cfg_home))
    assert msg.PLUGIN.skill_target_workspaces(["demo", "other"]) == ["demo"]
    content = msg.PLUGIN.skill_content("relaydeck-cli", "ignored source")
    assert "relaydeck workspace message" in content


# ── Pi/codex harness picks up runtime/skills ────────────────────────


def test_pi_inject_plugin_skills_returns_runtime_skills(tmp_path, monkeypatch):
    """`_inject_plugin_skills` scans `runtime/skills/` independently of
    the `skills` workspace plugin gate. This is what makes the
    messaging skill discoverable when only `messaging` is enabled in
    the workspace (not `skills`)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    ws_home = cfg_home / "workspaces" / "demo"
    (ws_home / "skills" / "user-skill").mkdir(parents=True)
    (ws_home / "skills" / "user-skill" / "SKILL.md").write_text(
        "---\nname: user-skill\ndescription: a user skill\n---\n\nuser"
    )
    (ws_home / "runtime" / "skills" / "relaydeck-cli").mkdir(parents=True)
    (ws_home / "runtime" / "skills" / "relaydeck-cli" / "SKILL.md").write_text(
        "---\nname: relaydeck-cli\ndescription: plugin skill\n---\n\nplugin"
    )

    from plugins.harnesses.pi.agent import PiAgent
    agent = PiAgent(
        agent_id="x", name="x", config={}, workspace="demo",
        db_path=str(tmp_path / "relaydeck.db"),
        stop_flag=threading.Event(),
    )

    # User-authored skills go through the (gated) _inject_skills path.
    user_args = agent._inject_skills()
    assert user_args is not None
    assert "user-skill" in " ".join(user_args)
    assert "relaydeck-cli" not in " ".join(user_args), \
        "_inject_skills must NOT scan runtime/skills/ (that's the plugin-skills path)"

    # Plugin-contributed skills go through the always-on path.
    plugin_args = agent._inject_plugin_skills()
    assert plugin_args is not None
    assert "relaydeck-cli" in " ".join(plugin_args)
    assert "user-skill" not in " ".join(plugin_args), \
        "_inject_plugin_skills must NOT scan skills/ (that's the user path)"


def test_codex_inject_plugin_skills_returns_runtime_skills(tmp_path, monkeypatch):
    """Same shape for codex — its _inject_plugin_skills lists
    runtime/skills/* paths and the model-instructions builder appends them
    unconditionally."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    ws_home = cfg_home / "workspaces" / "demo"
    ws_home.mkdir(parents=True)
    (cfg_home / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{tmp_path / "demo"}"\n'
    )
    (ws_home / "agent.toml").write_text('[workspace]\nplugins = []\n')
    (ws_home / "runtime" / "skills" / "relaydeck-cli").mkdir(parents=True)
    (ws_home / "runtime" / "skills" / "relaydeck-cli" / "SKILL.md").write_text(
        "---\nname: relaydeck-cli\ndescription: plugin skill\n---\n\nplugin"
    )

    from plugins.harnesses.codex.agent import CodexAgent
    agent = CodexAgent(
        agent_id="x", name="x", config={}, workspace="demo",
        db_path=str(tmp_path / "relaydeck.db"),
        stop_flag=threading.Event(),
    )

    plugin_text = agent._inject_plugin_skills()
    assert plugin_text is not None and "relaydeck-cli" in plugin_text

    # And the model instructions should pick it up even though the
    # workspace plugins list is empty (no "skills" enabled).
    instructions = agent._model_instructions_text()
    assert instructions is not None and "relaydeck-cli" in instructions


# ── HarnessAgent.send_message returns bool ──────────────────────────


def test_drain_pending_messages_method_uses_self_db_path(tmp_path, monkeypatch):
    """Regression for P3: HarnessAgent._drain_pending_messages() must
    use `self.db_path`, not the default `~/.relaydeck/runtime/relaydeck.db`.

    The earlier version of this test duplicated the drain logic
    inline, so a regression that hardcoded the default path inside
    the production method would still pass. This version calls the
    actual method on a real HarnessAgent instance — if the production
    code regresses to `drain_pending(self.agent_id)` (no db_path),
    the queued message in the custom DB stays NULL and this test
    fails on the `injected_at is not None` assertion.
    """
    # Set Path.home() to a different directory than the custom config
    # home — so a buggy fallback to the default path would read/write
    # a DIFFERENT sqlite file and the test would catch it.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home" / ".relaydeck" / "runtime").mkdir(parents=True)

    custom_cfg = tmp_path / "custom-config"
    (custom_cfg / "runtime").mkdir(parents=True)
    custom_db = str(custom_cfg / "runtime" / "relaydeck.db")

    # Queue a message in the CUSTOM db only.
    from relaydeck.messages import insert_message, list_inbox, list_one
    msg_id = insert_message(
        "user", "alice", "queued for alice",
        workspace="demo", db_path=custom_db,
    )

    from relaydeck.harness import HarnessAgent

    pushed: list[str] = []

    class _Recording(HarnessAgent):
        CLI = "true"
        def send_message(self, message: str) -> bool:
            pushed.append(message)
            return True

    a = _Recording(
        agent_id="alice", name="alice", config={}, workspace="demo",
        db_path=custom_db,  # ← NOT the default Path.home() path
        stop_flag=threading.Event(),
    )

    # Call the EXACT method HarnessAgent.run uses. A buggy
    # implementation that reads from a default path would never see
    # the queued message and `injected` would be 0.
    injected_count = a._drain_pending_messages()
    assert injected_count == 1, \
        f"drain method should have injected 1 message, got {injected_count}"

    # Verify what got pushed AND that the right DB was updated.
    assert pushed, "drain should have pushed the queued message"
    assert "queued for alice" in pushed[0]
    after = list_one(msg_id, db_path=custom_db)
    assert after is not None and after.injected_at is not None, \
        "drain method must mark the message injected in the custom db"

    # And the default DB stays empty — proves the drain method didn't
    # leak to the wrong path.
    default_db = str(tmp_path / "home" / ".relaydeck" / "runtime" / "relaydeck.db")
    if Path(default_db).exists():
        assert list_inbox("alice", db_path=default_db) == [], \
            "default db should be untouched — drain leaked across paths"


def test_drain_pending_messages_leaves_failed_writes_pending(tmp_path, monkeypatch):
    """A PTY write failure must NOT mark the message injected — the
    next agent start should retry. Pins the no-silent-loss guarantee
    in `_drain_pending_messages`."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    custom_cfg = tmp_path / "cfg"
    (custom_cfg / "runtime").mkdir(parents=True)
    custom_db = str(custom_cfg / "runtime" / "relaydeck.db")

    from relaydeck.messages import insert_message, list_one
    msg_id = insert_message("user", "alice", "won't go through",
                            workspace="demo", db_path=custom_db)

    from relaydeck.harness import HarnessAgent

    class _Failing(HarnessAgent):
        CLI = "true"
        def send_message(self, message: str) -> bool:
            return False  # PTY write fails

    a = _Failing(
        agent_id="alice", name="alice", config={}, workspace="demo",
        db_path=custom_db,
        stop_flag=threading.Event(),
    )

    injected_count = a._drain_pending_messages()
    assert injected_count == 0
    after = list_one(msg_id, db_path=custom_db)
    assert after is not None
    assert after.injected_at is None, \
        "failed PTY write must leave injected_at NULL for retry on next start"


def test_harness_send_message_terminates_with_carriage_return(tmp_path):
    """Regression for the "message appears in pi's input box but doesn't
    submit" bug. TUIs (pi, claude-code, codex) run in raw mode where
    `\\n` is a literal line-feed glyph and `\\r` is the Enter key.
    `send_message` must terminate with `\\r` (0x0D), not `\\n` (0x0A).
    """
    captured: list[bytes] = []

    from relaydeck.harness import HarnessAgent

    class _Recorder(HarnessAgent):
        CLI = "true"
        def send_input(self, data: bytes) -> bool:
            captured.append(data)
            return True

    a = _Recorder(
        agent_id="x", name="x", config={}, workspace=None,
        db_path=str(tmp_path / "relaydeck.db"),
        stop_flag=threading.Event(),
    )
    a.send_message("hello peer")

    assert captured, "send_input should have been called"
    payload = captured[0]
    assert payload.endswith(b"\r"), (
        f"send_message must end with \\r (TUIs treat \\r as Enter, "
        f"\\n as literal newline). Got: {payload!r}"
    )
    # And it shouldn't accidentally double up with a stray \n.
    assert not payload.endswith(b"\n"), (
        f"trailing \\n leaves stray newline in raw-mode TUIs. "
        f"Got: {payload!r}"
    )


def test_harness_send_message_returns_false_when_no_pty(tmp_path):
    """send_message must return bool so the drain code can detect
    failed writes."""
    from relaydeck.harness import HarnessAgent

    class _Bare(HarnessAgent):
        CLI = "true"

    a = _Bare(
        agent_id="x", name="x", config={}, workspace=None,
        db_path=str(tmp_path / "relaydeck.db"),
        stop_flag=threading.Event(),
    )
    # No PTY → send_message reaches send_input which returns False.
    assert a.send_message("hello") is False


# ── CLI fallback when daemon unreachable ────────────────────────────


def test_cli_fallback_enqueues_when_daemon_unreachable(tmp_path, monkeypatch):
    """_fallback_enqueue writes straight to SQLite when daemon's down.
    The drain on next agent start picks them up."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = _seed_agent(tmp_path, monkeypatch, "bob", workspace="demo")
    # Also seed alice so broadcast has multiple targets
    conn = open_db(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO agents (id, type, name, status, workspace, "
            "auto_start, created_at) VALUES ('alice', 'pi', 'alice', 'stopped', 'demo', 0, 0)"
        )
        conn.commit()
    finally:
        conn.close()

    _setup_workspace_registry(tmp_path, ["demo"])

    from plugins.messaging.plugin import _fallback_enqueue
    ids = _fallback_enqueue(
        "demo", "hello fleet",
        from_id="user", agent=None, in_reply_to=None, include_self=True,
    )
    assert len(ids) == 2  # alice + bob

    # Sender-exclusion when from is an agent
    ids2 = _fallback_enqueue(
        "demo", "from alice",
        from_id="alice", agent=None, in_reply_to=None, include_self=False,
    )
    # Only bob should be a recipient
    assert len(ids2) == 1


def test_send_via_daemon_returns_payload_on_success(tmp_path, monkeypatch):
    """_send_via_daemon happy path — mock urlopen and assert the
    request shape + parsed response."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.messaging import plugin as messaging
    from relaydeck.state import set_daemon_url
    set_daemon_url("http://127.0.0.1:8765")

    class _FakeResp:
        def __init__(self, body):
            self._body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._body

    captured_request = {}

    def _fake_urlopen(req, timeout=5, context=None):
        del context
        captured_request["url"] = req.full_url
        captured_request["data"] = json.loads(req.data.decode())
        return _FakeResp(b'{"ids":["msg_1"],"injected":["msg_1"],"pending":[]}')

    with patch("urllib.request.urlopen", _fake_urlopen):
        ok, resp = messaging._send_via_daemon(
            "demo", {"body": "hi", "agent": "bob", "from": "user"},
        )
    assert ok is True
    assert resp == {"ids": ["msg_1"], "injected": ["msg_1"], "pending": []}
    assert captured_request["url"].endswith("/api/workspaces/demo/messages")
    assert captured_request["data"]["body"] == "hi"


def test_send_via_daemon_fails_when_unreachable(tmp_path, monkeypatch):
    """When the daemon socket is closed, urlopen raises URLError —
    _send_via_daemon should return (False, descriptive_error)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.messaging import plugin as messaging
    from relaydeck.state import set_daemon_url
    # Point at a port nothing is listening on.
    set_daemon_url("http://127.0.0.1:1")
    ok, resp = messaging._send_via_daemon(
        "demo", {"body": "hi", "from": "user"},
    )
    assert ok is False
    assert isinstance(resp, str) and ("Error" in resp or "refused" in resp.lower()
                                       or "connection" in resp.lower())


def test_enqueue_workspace_messages_accepts_yaml_only_agent(tmp_path, monkeypatch):
    """Fallback enqueue must work before the agent row exists in SQLite."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "agents").mkdir(parents=True)
    (cfg / "runtime").mkdir(parents=True)
    ws_path = tmp_path / "repo"
    ws_path.mkdir()
    (cfg / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{ws_path}"\nplugins = []\n'
    )
    (cfg / "agents" / "reviewer.yaml").write_text(
        "id: reviewer\nname: reviewer\ntype: pi\nworkspace: demo\n"
    )
    db = str(cfg / "runtime" / "relaydeck.db")

    from relaydeck.messages import enqueue_workspace_messages, list_inbox

    ids = enqueue_workspace_messages(
        "demo", "hello", agent="reviewer", db_path=db,
    )
    assert len(ids) == 1
    inbox = list_inbox("reviewer", db_path=db)
    assert len(inbox) == 1
    assert inbox[0].body == "hello"


def test_broadcast_path_marks_broadcast_id_and_owes_no_reply(tmp_path, monkeypatch):
    """End-to-end: a fleet broadcast (no `agent=`) stamps every fan-out row
    with a shared broadcast_id, and no recipient owes a reply. A targeted
    `agent=` message leaves broadcast_id NULL and still owes a reply.

    Regression for the reply-storm where one `workspace message` nudged
    every agent to reply, polluting the threads."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "agents").mkdir(parents=True)
    (cfg / "runtime").mkdir(parents=True)
    ws_path = tmp_path / "repo"; ws_path.mkdir()
    (cfg / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{ws_path}"\nplugins = []\n'
    )
    db = str(cfg / "runtime" / "relaydeck.db")

    from relaydeck.db import open_db
    from relaydeck.messages import (
        enqueue_workspace_messages,
        list_unanswered_peer_messages,
        mark_injected,
    )

    conn = open_db(db)
    for aid in ("atlas", "nova", "flash"):
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, type, name, status, created_at, workspace) "
            "VALUES (?, 'pi', ?, 'running', 0.0, 'demo')",
            (aid, aid),
        )
    conn.commit(); conn.close()

    # Broadcast: atlas → fleet (no agent=). Fans out to nova + flash.
    bc_ids = enqueue_workspace_messages("demo", "round 7 is live", from_id="atlas", db_path=db)
    assert len(bc_ids) == 2
    # broadcast_id is set + shared across the fan-out (read straight from DB)
    conn = open_db(db)
    bvals = [r[0] for r in conn.execute(
        "SELECT broadcast_id FROM agent_messages WHERE id IN (?,?)", bc_ids).fetchall()]
    conn.close()
    assert all(v and v.startswith("bc_") for v in bvals)
    assert len(set(bvals)) == 1, "fan-out rows must share one broadcast_id"

    for mid in bc_ids:
        mark_injected(mid, "round 7 is live", db_path=db)
    for who in ("nova", "flash"):
        assert list_unanswered_peer_messages(who, db_path=db) == []

    # Direct: atlas → nova (agent=nova). Owes a reply.
    direct = enqueue_workspace_messages("demo", "nova, take this", from_id="atlas",
                                        agent="nova", db_path=db)
    assert len(direct) == 1
    conn = open_db(db)
    dbid = conn.execute("SELECT broadcast_id FROM agent_messages WHERE id=?", direct).fetchone()[0]
    conn.close()
    assert dbid is None, "a targeted message must not be marked a broadcast"
    mark_injected(direct[0], "nova, take this", db_path=db)
    assert len(list_unanswered_peer_messages("nova", db_path=db)) == 1


def test_api_broadcast_stamps_shared_broadcast_id(tmp_path, monkeypatch):
    """Live daemon path: POST /api/workspaces/{ws}/messages (no agent)
    must stamp every fan-out row with one shared broadcast_id."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "agents").mkdir(parents=True)
    (cfg / "runtime").mkdir(parents=True)
    ws_path = tmp_path / "repo"; ws_path.mkdir()
    (cfg / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{ws_path}"\nplugins = []\n'
    )
    db = str(cfg / "runtime" / "relaydeck.db")

    from fastapi.testclient import TestClient

    import relaydeck.orchestrator as _orch_mod
    from relaydeck.db import open_db
    from relaydeck.messages import list_unanswered_peer_messages, mark_injected
    from relaydeck.transports.api import create_app

    conn = open_db(db)
    for aid in ("atlas", "nova", "flash"):
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, type, name, status, created_at, workspace) "
            "VALUES (?, 'pi', ?, 'stopped', 0.0, 'demo')",
            (aid, aid),
        )
    conn.commit(); conn.close()

    # The orchestrator is a module-level singleton; an earlier test may have
    # cached one pointing at a different config_home. Reset it and pin the app
    # to this tmp config_home so list_agents() reads the db we just seeded.
    _orch_mod._orchestrator = None
    client = TestClient(create_app(cfg))
    r = client.post(
        "/api/workspaces/demo/messages",
        json={"body": "fleet ping", "from": "atlas"},
    )
    assert r.status_code == 200, r.text
    ids = r.json()["ids"]
    assert len(ids) == 2  # nova + flash (atlas excluded as sender)

    conn = open_db(db)
    bvals = [row[0] for row in conn.execute(
        f"SELECT broadcast_id FROM agent_messages WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()]
    conn.close()
    assert all(v and v.startswith("bc_") for v in bvals)
    assert len(set(bvals)) == 1

    for mid in ids:
        mark_injected(mid, "fleet ping", db_path=db)
    for who in ("nova", "flash"):
        assert list_unanswered_peer_messages(who, db_path=db) == []


# ── Live re-drain (Bug fix: pending message stuck after warmup race) ──


def test_drain_pending_messages_picks_up_message_inserted_after_first_call(tmp_path, monkeypatch):
    """Regression for the "send during warmup race → stays pending
    forever" bug from the test0/test1 transcript.

    The harness's read loop now re-runs `_drain_pending_messages` on
    every idle tick (every `_LIVE_DRAIN_INTERVAL`). So a message
    inserted between two drain calls must be picked up by the second.
    This test simulates that: drain once (empty), insert, drain again,
    assert injection.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    custom = tmp_path / "cfg"
    (custom / "runtime").mkdir(parents=True)
    db = str(custom / "runtime" / "relaydeck.db")

    from relaydeck.messages import insert_message, list_one
    from relaydeck.harness import HarnessAgent

    pushed: list[str] = []

    class _Recording(HarnessAgent):
        CLI = "true"
        def send_message(self, message: str) -> bool:
            pushed.append(message)
            return True

    a = _Recording(
        agent_id="alice", name="alice", config={}, workspace="demo",
        db_path=db, stop_flag=threading.Event(),
    )

    # First drain — nothing pending, agent just started.
    assert a._drain_pending_messages() == 0
    assert pushed == []

    # While the agent is happily running, a peer sends a message.
    msg_id = insert_message(
        "test0", "alice", "checking in — how are you doing?",
        workspace="demo", db_path=db,
    )

    # Read loop's next idle tick fires another drain. The pending
    # message must be injected this time (the bug was: it stayed
    # NULL until restart because drain only ran once).
    assert a._drain_pending_messages() == 1
    assert pushed and "checking in" in pushed[0]
    assert list_one(msg_id, db_path=db).injected_at is not None


def test_harness_has_live_drain_interval_constant():
    """Pins the contract that the read-loop periodic drain exists with
    a configurable cadence. If this field disappears, the silent-loss
    regression returns."""
    from relaydeck.harness import HarnessAgent
    assert hasattr(HarnessAgent, "_LIVE_DRAIN_INTERVAL")
    assert isinstance(HarnessAgent._LIVE_DRAIN_INTERVAL, (int, float))
    assert HarnessAgent._LIVE_DRAIN_INTERVAL > 0


# ── CLI: inbox --full and `relaydeck message show <id>` ────────────────────


def _seed_messaging_cli(tmp_path, monkeypatch):
    """Stand up the minimum environment for the messaging CLI commands:
    a workspace, an active-workspace marker, a queued message, AND
    the messaging plugin's CLI handlers attached to the main group.

    The plugin loader (run by `relaydeck serve`) is what wires plugin CLI
    handlers in production. Tests bypass that loader by calling
    `_register_cli(main)` directly here — the same function the loader
    would invoke."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    ws_path = tmp_path / "demo"
    ws_path.mkdir(parents=True)
    (cfg_home / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{ws_path}"\n'
        f'plugins = ["messaging"]\n'
    )
    (cfg_home / "workspaces" / "demo").mkdir(parents=True)
    (cfg_home / "workspaces" / "demo" / "agent.toml").write_text(
        '[workspace]\nplugins = ["messaging"]\n'
    )
    from relaydeck.state import set_current_workspace
    set_current_workspace("demo")
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None

    # Wire the messaging plugin's CLI subcommands onto the main group
    # so `workspace inbox` / `message show` exist for the runner.
    from relaydeck.transports.cli import main as _main
    from plugins.messaging.plugin import _register_cli
    # _register_cli is idempotent on workspace/agent subgroup attachment
    # but adding `message` twice would raise; check before re-registering.
    if "message" not in _main.commands:
        _register_cli(_main)


def test_workspace_inbox_full_prints_untruncated_body(tmp_path, monkeypatch):
    """The default table truncates bodies to 80 chars (table.add_row
    passes `m.body[:80]`). `--full` must print the entire body so an
    agent can read what its peer actually said without shelling
    into SQLite."""
    _seed_messaging_cli(tmp_path, monkeypatch)
    long_body = (
        "This is a long reply that would be truncated by the summary "
        "table view. It contains way more than 80 characters so the "
        "default view would silently cut off the rest, forcing agents "
        "to query SQLite to read it."
    )
    assert len(long_body) > 80
    from relaydeck.messages import insert_message
    db = str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db")
    insert_message("peer", "alice", long_body, workspace="demo", db_path=db)

    from click.testing import CliRunner
    from relaydeck.transports.cli import main as cli
    runner = CliRunner()
    result = runner.invoke(cli, ["workspace", "inbox", "--full"])
    assert result.exit_code == 0, result.output
    # Full body should appear verbatim — particularly the tail past
    # the 80-char truncation point.
    assert "forcing agents to query SQLite" in result.output, result.output


def test_message_show_prints_full_body_and_metadata(tmp_path, monkeypatch):
    """`relaydeck message show <id>` must print: id, from, to, workspace,
    state, ts, and the full untruncated body. This is the explicit
    "read one message" command the inbox table points at."""
    _seed_messaging_cli(tmp_path, monkeypatch)
    body = "A reply with enough text that the table view would chop it."
    from relaydeck.messages import insert_message
    db = str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db")
    msg_id = insert_message("peer", "alice", body, workspace="demo",
                            in_reply_to="msg_prev_xyz", db_path=db)

    from click.testing import CliRunner
    from relaydeck.transports.cli import main as cli
    runner = CliRunner()
    result = runner.invoke(cli, ["message", "show", msg_id])
    assert result.exit_code == 0, result.output
    assert msg_id in result.output
    assert "peer" in result.output
    assert "alice" in result.output
    assert "demo" in result.output
    assert "msg_prev_xyz" in result.output  # in-reply-to header
    assert body in result.output


def test_message_show_404_for_unknown_id(tmp_path, monkeypatch):
    _seed_messaging_cli(tmp_path, monkeypatch)
    from click.testing import CliRunner
    from relaydeck.transports.cli import main as cli
    runner = CliRunner()
    result = runner.invoke(cli, ["message", "show", "msg_does_not_exist"])
    assert result.exit_code != 0
    assert "No such message" in result.output


# ── Zombie reconcile + start/stop hardening ──────────────────────────


def test_send_message_to_reconciles_zombie_running_state(tmp_path, monkeypatch):
    """If the DB says status=running but the orchestrator has no live
    instance, send_message_to should snap the row back to 'stopped'
    (and the message goes pending without trying to inject into a
    non-existent PTY). Without this, the CLI lies forever — it says
    "running" and silently swallows every message.
    """
    from relaydeck.orchestrator import Orchestrator
    from relaydeck.db import open_db, upsert_agent
    from relaydeck.messages import list_one

    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    (cfg_home / "agents").mkdir(parents=True)
    (cfg_home / "agents" / "ghost.yaml").write_text(
        'id: ghost\ntype: pi\nname: Ghost\nworkspace: demo\n'
    )

    orch = Orchestrator(config_home=cfg_home)
    # Plant a zombie: DB says running, but `_instances` is empty.
    conn = open_db(orch.db_path)
    try:
        upsert_agent(conn, "ghost", "pi", "Ghost", workspace="demo")
        conn.execute("UPDATE agents SET status='running' WHERE id='ghost'")
        conn.commit()
    finally:
        conn.close()
    assert orch.get_agent("ghost")["status"] == "running"
    assert orch.get_running_instance("ghost") is None

    msg_id, injected = orch.send_message_to("ghost", "hello?", from_id="user")
    assert injected is False, "no live instance — must not claim injection"

    # The DB row should now reflect the actual state: not running.
    assert orch.get_agent("ghost")["status"] == "stopped"
    # And the message should be queued for next start (no injected_at).
    m = list_one(msg_id, db_path=orch.db_path)
    assert m is not None and m.injected_at is None


def test_stop_agent_forces_db_to_stopped_when_no_instance(tmp_path, monkeypatch):
    """Calling stop_agent on a row that says 'running' but has no live
    instance must still set the DB to 'stopped'. Without this fix,
    `relaydeck agent stop` was a silent no-op for zombies — leaving the
    UI lying and `send_message_to` silently persisting forever."""
    from relaydeck.orchestrator import Orchestrator
    from relaydeck.db import open_db, upsert_agent

    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    (cfg_home / "agents").mkdir(parents=True)
    orch = Orchestrator(config_home=cfg_home)
    conn = open_db(orch.db_path)
    try:
        upsert_agent(conn, "zombie", "pi", "Zombie", workspace="demo")
        conn.execute("UPDATE agents SET status='running' WHERE id='zombie'")
        conn.commit()
    finally:
        conn.close()

    orch.stop_agent("zombie")
    assert orch.get_agent("zombie")["status"] == "stopped"


def test_reconcile_zombies_snaps_running_rows_with_no_live_thread(tmp_path, monkeypatch):
    """The periodic sweeper (and `reconcile_zombies()`) must snap any
    DB row claiming `running`/`starting` to `stopped` when this
    daemon's `_running` doesn't actually have the thread. Without
    this, `relaydeck agent list` keeps lying between sends — the original
    user-visible bug from the transcript ("test1 shows green dot and
    says running but logs say agent is not running").
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.orchestrator import Orchestrator
    from relaydeck.db import open_db, upsert_agent

    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    (cfg_home / "agents").mkdir(parents=True)

    orch = Orchestrator(config_home=cfg_home)
    # The sweeper / reconciler only acts when we're the daemon —
    # CLI-side orchestrators have empty `_instances` and would
    # otherwise mark every agent stopped on every list. Flip the flag
    # to simulate the daemon process without running the full start().
    orch._is_daemon = True

    conn = open_db(orch.db_path)
    try:
        upsert_agent(conn, "phantom", "pi", "Phantom", workspace="demo")
        conn.execute("UPDATE agents SET status='running' WHERE id='phantom'")
        conn.commit()
    finally:
        conn.close()
    assert orch.get_agent("phantom")["status"] == "running"

    reconciled = orch.reconcile_zombies()
    assert "phantom" in reconciled, (
        "reconcile_zombies() must reconcile rows whose threads aren't alive"
    )
    assert orch.get_agent("phantom")["status"] == "stopped"


def test_reconcile_zombies_is_noop_outside_daemon_process(tmp_path, monkeypatch):
    """An orchestrator instance created by a CLI command has empty
    `_instances`. If `reconcile_zombies` ran there it would wrongly
    mark every agent as stopped on every `relaydeck agent list`. The
    `_is_daemon` guard prevents that — CLI processes get raw DB
    reads, the daemon's sweeper is the only writer."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.orchestrator import Orchestrator
    from relaydeck.db import open_db, upsert_agent

    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)

    orch = Orchestrator(config_home=cfg_home)
    # No `start()` → `_is_daemon` stays False (CLI process scenario).
    assert orch._is_daemon is False

    conn = open_db(orch.db_path)
    try:
        upsert_agent(conn, "real", "pi", "Real", workspace="demo")
        conn.execute("UPDATE agents SET status='running' WHERE id='real'")
        conn.commit()
    finally:
        conn.close()

    reconciled = orch.reconcile_zombies()
    assert reconciled == [], "CLI-side orchestrator must not reconcile"
    # Row is untouched — the daemon is the source of truth, not the CLI.
    assert orch.get_agent("real")["status"] == "running"


def test_daemon_api_returns_409_when_start_verification_raises(tmp_path, monkeypatch):
    """`Orchestrator.start_agent` raises `RuntimeError` when the child
    fails the verify window. The POST /api/agents/<id>/start endpoint
    must catch that and return a 4xx with the real reason — otherwise
    FastAPI returns 500 and the CLI sees an opaque error.

    Pins the API boundary so a regression that goes back to "only
    catch ValueError" surfaces here, not in a live session.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from fastapi.testclient import TestClient
    from relaydeck.transports.api import create_app
    from relaydeck.orchestrator import register_agent_type
    from relaydeck.agents_base import BaseAgent

    class _Crashy(BaseAgent):
        def run(self):
            raise RuntimeError("synthetic boot failure")

    register_agent_type("crashy-api", _Crashy)

    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    (cfg_home / "agents").mkdir(parents=True)
    (cfg_home / "agents" / "boom.yaml").write_text(
        'id: boom\ntype: crashy-api\nname: Boom\nworkspace: demo\n'
    )

    app = create_app()
    client = TestClient(app)
    r = client.post("/api/agents/boom/start")
    # Must be a 4xx (not 500) AND the body must carry the real reason
    # the CLI needs to print.
    assert r.status_code == 409, (
        f"start verification failure should map to a 4xx; got "
        f"{r.status_code}: {r.text}"
    )
    body = r.json()
    detail = body.get("detail", "")
    assert "failed to start" in detail or "exited" in detail, (
        f"daemon must include the real start-failure reason; got: {detail!r}"
    )


def test_post_to_daemon_distinguishes_transport_vs_daemon_error(tmp_path, monkeypatch):
    """The CLI helper `_post_to_daemon` must return different outcomes
    for "couldn't connect" vs "daemon answered with 4xx/5xx".
    Conflating them caused round-3's CLI to fall back to local spawn
    on a 409 — re-introducing the very path the round-3 fix removed.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.transports import cli as cli_mod
    from relaydeck.state import set_daemon_url

    # Point at a port nothing is listening on → URLError → transport_failed
    set_daemon_url("http://127.0.0.1:1")
    outcome, _ = cli_mod._post_to_daemon("/api/agents/anything/start")
    assert outcome == cli_mod._POST_TRANSPORT_FAILED, (
        "Connection refused must map to _POST_TRANSPORT_FAILED so the "
        "CLI falls back to local spawn with a warning."
    )

    # Now simulate a reachable daemon that returns HTTPError. We monkey-
    # patch urlopen so we don't have to bring up a full FastAPI app for
    # this assertion — what we're pinning is the helper's classifier,
    # not the daemon's response. The companion test
    # `test_daemon_api_returns_409_when_start_verification_raises`
    # covers the end-to-end shape.
    import urllib.error
    import urllib.request as _ur
    import io as _io

    def _fake_urlopen(req, timeout=None, context=None):
        del context
        raise urllib.error.HTTPError(
            url=req.full_url, code=409, msg="Conflict",
            hdrs=None, fp=_io.BytesIO(b'{"detail":"Agent boom failed to start: synthetic"}'),
        )

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)
    outcome, payload = cli_mod._post_to_daemon("/api/agents/boom/start")
    assert outcome == cli_mod._POST_DAEMON_ERROR
    assert "409" in str(payload) and "synthetic" in str(payload), (
        "Daemon error must include both the HTTP code and the detail "
        f"so the CLI can print the real reason. Got: {payload!r}"
    )


def test_start_agent_raises_on_immediate_child_failure(tmp_path, monkeypatch):
    """`start_agent` now waits up to START_VERIFY_TIMEOUT for the
    agent to confirm a healthy PTY. If the runner thread crashes
    immediately (e.g. `run()` raises before Popen can succeed), the
    caller gets a RuntimeError — not a misleading "✓ started" for
    a child that died before the CLI even returned. Pins the new
    contract together with `test_error_agent` in test_orchestrator.py.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.orchestrator import Orchestrator, register_agent_type
    from relaydeck.agents_base import BaseAgent
    import threading as _th

    class _Crashy(BaseAgent):
        def run(self):
            raise RuntimeError("synthetic boot failure")

    register_agent_type("crashy", _Crashy)

    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    (cfg_home / "agents").mkdir(parents=True)
    (cfg_home / "agents" / "boom.yaml").write_text(
        'id: boom\ntype: crashy\nname: Boom\nworkspace: demo\n'
    )

    orch = Orchestrator(config_home=cfg_home)
    try:
        orch.start_agent("boom")
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "failed to start" in str(exc) or "exited" in str(exc)
    assert raised, "start_agent must raise when the child dies during verification"


def test_start_agent_reconciles_zombie_before_spec_or_type_resolution(tmp_path, monkeypatch):
    """Pre-start reconcile must run BEFORE spec load + type resolution
    so that even a corrupt spec or removed harness plugin still cleans
    up the lying "running" row. Otherwise users with a misconfigured
    agent type can never recover the row without manual SQL.

    This test plants a zombie row whose YAML names a nonexistent agent
    type. `start_agent` should:
      1. Reconcile the row to 'stopped' (zombie cleanup), then
      2. Raise ValueError on the unknown type.

    Both observable: the exception propagates, AND the DB row ends
    at 'stopped'.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.orchestrator import Orchestrator
    from relaydeck.db import open_db, upsert_agent

    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    (cfg_home / "agents").mkdir(parents=True)
    (cfg_home / "agents" / "frozen.yaml").write_text(
        'id: frozen\ntype: nonexistent-type\nname: Frozen\nworkspace: demo\n'
    )

    orch = Orchestrator(config_home=cfg_home)
    conn = open_db(orch.db_path)
    try:
        upsert_agent(conn, "frozen", "pi", "Frozen", workspace="demo")
        conn.execute("UPDATE agents SET status='running' WHERE id='frozen'")
        conn.commit()
    finally:
        conn.close()
    assert orch.get_agent("frozen")["status"] == "running"

    # The start fails because the type is unknown — but the reconcile
    # MUST have run first.
    raised = None
    try:
        orch.start_agent("frozen")
    except ValueError as exc:
        raised = str(exc)
    assert raised is not None and "Unknown agent type" in raised

    # Critical assertion the reviewer flagged: the DB row is now
    # 'stopped', proving the reconcile ran before the type check
    # raised. The original test stopped at the ValueError and never
    # verified this; a regression that moved the reconcile back
    # below the type check would silently re-introduce the lying
    # status row.
    assert orch.get_agent("frozen")["status"] == "stopped", (
        "Pre-start reconcile did not run — the DB row is still "
        "'running' after a failed start_agent call. The reconcile "
        "must precede spec load + type resolution so it always "
        "fires, regardless of downstream spawn failures."
    )


def test_periodic_live_drain_runs_independent_of_pty_activity(tmp_path):
    """Pin the contract that the PERIODIC live-drain (the one gated by
    `_LIVE_DRAIN_INTERVAL`, not the once-at-startup drain) is reached
    on every read-loop iteration regardless of whether `select()`
    returned ready.

    The earlier version of this test used `src.find('_drain_pending_messages()')`
    which matched the FIRST occurrence (the startup drain at the top
    of `run()`). That startup drain was already above `select` — a
    regression that moved ONLY the periodic drain back inside
    `if not ready:` would still pass that check.

    To pin the periodic drain specifically, locate the
    `_LIVE_DRAIN_INTERVAL` reference, find the drain call that
    follows it, and assert THAT call is above `select.select(`. This
    fails if anyone re-gates the periodic drain on PTY-idle.
    """
    import inspect
    from relaydeck.harness import HarnessAgent

    src = inspect.getsource(HarnessAgent.run)

    # Find the time-gate condition that fronts the periodic drain.
    idx_interval = src.find("_LIVE_DRAIN_INTERVAL")
    assert idx_interval != -1, (
        "The periodic live-drain trigger (`_LIVE_DRAIN_INTERVAL`) is "
        "gone from `run()` — the periodic-drain contract is broken."
    )

    # The drain call that BELONGS to the periodic path is the first
    # `_drain_pending_messages()` AFTER the `_LIVE_DRAIN_INTERVAL`
    # reference. There may be a separate startup-drain call before
    # the loop; we don't care about that one here.
    idx_periodic_drain = src.find("_drain_pending_messages()", idx_interval)
    assert idx_periodic_drain != -1, (
        "Couldn't find a `_drain_pending_messages()` call inside the "
        "`_LIVE_DRAIN_INTERVAL`-gated block."
    )

    idx_select = src.find("select.select(")
    idx_if_not_ready = src.find("if not ready:")
    assert idx_select != -1 and idx_if_not_ready != -1

    # The periodic drain must come BEFORE the select call (and thus
    # before `if not ready:`) — otherwise busy PTYs (pi/claude-code/
    # codex are TUIs that emit constantly) starve the drain.
    assert idx_periodic_drain < idx_select, (
        "Periodic live-drain is at or after `select.select(...)` — TUI "
        "harnesses (pi/claude-code) emit bytes constantly, so select() "
        "rarely returns idle and the drain won't fire. Move the "
        "interval-gated drain ABOVE the select call."
    )
    assert idx_periodic_drain < idx_if_not_ready, (
        "Periodic live-drain is back inside the `if not ready:` branch — "
        "this is the exact regression the original transcript exposed. "
        "Run-loop gating must be time-based, not PTY-idle-based."
    )
