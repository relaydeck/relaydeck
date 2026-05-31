from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.harnesses.codex.agent import CodexAgent


def _config_value(cmd: list[str], key: str) -> str | None:
    for i, part in enumerate(cmd):
        if part != "--config" or i + 1 >= len(cmd):
            continue
        item = cmd[i + 1]
        if item.startswith(key + "="):
            return item.split("=", 1)[1]
    return None


def test_codex_plugin_loads_and_registers_agent_types(tmp_path):
    import relaydeck.plugin as plug
    from relaydeck.orchestrator import known_agent_types
    from relaydeck.plugin import PluginContext, get_registry

    plug._registry = None
    reg = get_registry(tmp_path / ".relaydeck")
    reg.load_all(PluginContext(config_home=tmp_path / ".relaydeck"))

    assert reg.get("codex-harness") is not None
    assert "codex" in known_agent_types()
    assert "codex-cli" in known_agent_types()


def _make_agent(tmp_path: Path, monkeypatch, config: dict | None = None) -> CodexAgent:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    ws_path = tmp_path / "repo"
    ws_path.mkdir(parents=True)
    cfg_home.mkdir(parents=True, exist_ok=True)
    (cfg_home / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{ws_path}"\nplugins = ["skills"]\n'
    )
    ws_home = cfg_home / "workspaces" / "demo"
    ws_home.mkdir(parents=True)
    (ws_home / "agent.toml").write_text(
        '[workspace]\n'
        'name = "demo"\n'
        'plugins = ["fleet-context", "skills", "forbidden-tools"]\n'
    )
    return CodexAgent(
        agent_id="coder",
        name="coder",
        config=config or {},
        workspace="demo",
        db_path=str(tmp_path / "relaydeck.db"),
        stop_flag=threading.Event(),
    )


def test_codex_command_uses_codex_native_flags(tmp_path, monkeypatch):
    cfg_home = tmp_path / ".relaydeck"
    presets = cfg_home / "presets"
    presets.mkdir(parents=True)
    (presets / "codex.yaml").write_text(
        "name: codex\nprovider: openai\nmodel: gpt-5.3-codex\n"
    )
    agent = _make_agent(
        tmp_path,
        monkeypatch,
        {
            "model": "codex",
            "sandbox": "workspace-write",
            "ask_for_approval": "on-request",
            "add_dirs": [str(tmp_path / "extra")],
            "initial_prompt": "Start here.",
            "forbidden_tools": ["rm", "curl"],
            "codex_config": {"model_reasoning_effort": '"medium"'},
        },
    )

    ws_home = cfg_home / "workspaces" / "demo"
    (ws_home / "runtime" / "fleet-context").mkdir(parents=True)
    (ws_home / "runtime" / "fleet-context" / "coder.md").write_text("Fleet context.")
    (ws_home / "skills" / "review").mkdir(parents=True)
    (ws_home / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: review skill\n---\n\nSkill body."
    )

    cmd = agent._build_command()

    assert cmd[:3] == ["codex", "--model", "gpt-5.3-codex"]
    assert "--workspace" not in cmd
    assert "--session-dir" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("--ask-for-approval") + 1] == "on-request"
    assert "--cd" in cmd
    assert str(tmp_path / "repo") in cmd
    assert "--add-dir" in cmd
    assert "--config" in cmd
    assert cmd[-1] == "Start here."
    # Codex has no model_instructions_file (dead key); instructions are
    # delivered as a developer message via `developer_instructions`.
    assert _config_value(cmd, "model_instructions_file") is None
    instr = _config_value(cmd, "developer_instructions")
    assert instr is not None
    assert "Fleet context." in instr
    assert "SKILL.md" in instr
    assert "rm, curl" in instr


def test_codex_command_resolves_dashboard_preset_key(tmp_path, monkeypatch):
    cfg_home = tmp_path / ".relaydeck"
    presets = cfg_home / "presets"
    presets.mkdir(parents=True)
    (presets / "codex.yaml").write_text(
        "name: codex\nprovider: openai\nmodel: gpt-5.3-codex\n"
    )
    agent = _make_agent(tmp_path, monkeypatch, {"preset": "codex"})

    cmd = agent._build_command()

    assert cmd[cmd.index("--model") + 1] == "gpt-5.3-codex"


def test_codex_local_provider_selects_builtin(tmp_path, monkeypatch):
    # Routing codex at a LOCAL OSS server uses codex's built-in providers via
    # `--config model_provider=...`; the local model id rides --model. (codex
    # 0.134 dropped custom chat-completions providers — requires the OpenAI
    # Responses API — so the built-in local route is the only working
    # non-OpenAI path; verified live against ollama llama3.2.)
    agent = _make_agent(tmp_path, monkeypatch, {
        "codex_model": "llama3.2:latest",
        "codex_local_provider": "ollama",
    })
    cmd = agent._build_command()
    assert _config_value(cmd, "model_provider") == '"ollama"'
    assert cmd[cmd.index("--model") + 1] == "llama3.2:latest"


def test_codex_no_local_provider_omits_model_provider(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, {"codex_model": "gpt-5.3-codex"})
    assert _config_value(agent._build_command(), "model_provider") is None


def test_codex_local_provider_whitelisted(tmp_path, monkeypatch):
    # Only the two built-in OSS ids may reach the -c flag — an arbitrary value
    # must never be injected.
    agent = _make_agent(tmp_path, monkeypatch, {
        "codex_model": "x", "codex_local_provider": "evil; rm -rf /"})
    assert _config_value(agent._build_command(), "model_provider") is None


def test_codex_instructions_file_folds_user_configured_file(tmp_path, monkeypatch):
    user_file = tmp_path / "user-codex-instructions.md"
    user_file.write_text("User-provided Codex instructions.")
    agent = _make_agent(
        tmp_path,
        monkeypatch,
        {"codex_config": {"model_instructions_file": json.dumps(str(user_file))}},
    )

    cmd = agent._build_command()

    # The user's configured file is folded into the developer_instructions
    # text; the dead model_instructions_file key is never forwarded.
    instr = _config_value(cmd, "developer_instructions")
    assert instr is not None
    assert "User-provided Codex instructions." in instr
    assert _config_value(cmd, "model_instructions_file") is None


def test_codex_cli_agent_spec_builds_without_workspace_flag(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)

    cmd = agent._build_command()

    assert cmd[0] == "codex"
    assert "--workspace" not in cmd
    assert "--cd" in cmd
    assert cmd[cmd.index("--cd") + 1] == str(tmp_path / "repo")


def test_codex_env_sets_per_agent_codex_home(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    env = agent._build_env()

    expected = (
        tmp_path
        / ".relaydeck"
        / "workspaces"
        / "demo"
        / "runtime"
        / "codex-homes"
        / "coder"
    )
    assert env["CODEX_HOME"] == str(expected)
    assert expected.exists()
    assert env["RELAYDECK_AGENT_ID"] == "coder"
    assert env["RELAYDECK_WORKSPACE"] == "demo"


def test_codex_log_path_points_at_tui_log(tmp_path, monkeypatch):
    # Pinned because when codex panics (we hit one in a 0.130 TUI
    # wrapping bug), the tui log is the only place the panic
    # message lives. harness.exit carries this path so operators
    # don't have to know codex's storage layout.
    agent = _make_agent(tmp_path, monkeypatch)
    expected = Path(agent._codex_home()) / "log" / "codex-tui.log"
    assert agent.log_path() == expected


def test_codex_usage_tailer_emits_token_count_records(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    session_dir = agent._session_dir() / "2026" / "05" / "14"
    session_dir.mkdir(parents=True)
    log = session_dir / "rollout-test.jsonl"

    captured: list[tuple[str, dict]] = []

    def _capture(event_type, payload=None):
        captured.append((event_type, payload or {}))
        return 1

    agent.emit = _capture  # type: ignore[method-assign]
    rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": "sess-1",
                "model_provider": "openai",
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "model": "gpt-5.3-codex",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 123,
                        "cached_input_tokens": 100,
                        "output_tokens": 45,
                        "reasoning_output_tokens": 10,
                        "total_tokens": 168,
                    }
                },
            },
        },
    ]
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))

    agent._scan_session_dir(agent._session_dir())

    event_type, payload = captured[-1]
    assert event_type == "usage.record"
    assert payload["agent_id"] == "coder"
    assert payload["model"] == "gpt-5.3-codex"
    assert payload["provider"] == "openai"
    assert payload["prompt"] == 123
    assert payload["completion"] == 45
    assert payload["session_id"] == "sess-1"
    assert payload["harness"] == "codex"


def test_codex_tailer_emits_harness_assistant_message(tmp_path, monkeypatch):
    """Codex JSONL carries assistant text in two shapes (`response_item`
    message + `event_msg/agent_message`). Both should fan out to
    `harness.assistant_message` on the plugin event bus so emote (and
    any other subscriber) gets text without tailing files itself."""
    agent = _make_agent(tmp_path, monkeypatch)
    session_dir = agent._session_dir() / "2026" / "05" / "14"
    session_dir.mkdir(parents=True)
    log = session_dir / "rollout-test.jsonl"

    bus_events: list[tuple[str, dict]] = []

    class _FakeBus:
        def emit(self, event):
            bus_events.append((event.type, event.data))

    class _FakeOrch:
        _plugin_event_bus = _FakeBus()

    import relaydeck.orchestrator as orch
    monkeypatch.setattr(orch, "get_orchestrator", lambda: _FakeOrch())
    agent.emit = lambda *_a, **_kw: 1  # type: ignore[assignment]

    rows = [
        {
            "type": "session_meta",
            "payload": {"id": "sess-9", "model_provider": "openai"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Shipped — all green."}],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Following up on tests."},
        },
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))

    agent._scan_session_dir(agent._session_dir())

    msgs = [data for typ, data in bus_events if typ == "harness.assistant_message"]
    assert len(msgs) == 2, f"expected two assistant messages, got {msgs}"
    assert msgs[0]["text"] == "Shipped — all green."
    assert msgs[1]["text"] == "Following up on tests."
    for m in msgs:
        assert m["agent_id"] == "coder"
        assert m["harness"] == "codex"
        assert m["session_id"] == "sess-9"


def test_codex_prepare_replaces_dangling_symlink(tmp_path, monkeypatch):
    """A codex-home migrated between hosts (eg macOS -> Linux) leaves
    dangling auth.json / config.toml symlinks pointing at the previous
    operating-system's home path. _prepare_codex_home must detect
    and replace those, otherwise codex's sign-in flow fails with
    ENOENT on auth.json read."""
    agent = _make_agent(tmp_path, monkeypatch)

    # Set up host ~/.codex/ with real files (the new operator did
    # `codex login` from a shell on this host).
    user_codex = tmp_path / ".codex"
    user_codex.mkdir()
    (user_codex / "auth.json").write_text('{"token":"new"}')
    (user_codex / "config.toml").write_text('model = "gpt-5"\n')

    # Pre-create the agent codex-home with dangling symlinks pointing
    # at a path that does not exist on this host (the macOS layout).
    home = agent._codex_home()
    home.mkdir(parents=True, exist_ok=True)
    bogus = tmp_path / "Users" / "devuser" / ".codex"
    bogus_auth = bogus / "auth.json"
    (home / "auth.json").symlink_to(bogus_auth)
    (home / "config.toml").symlink_to(bogus / "config.toml")
    assert not (home / "auth.json").exists()  # dangling

    agent._prepare_codex_home()

    # Symlinks should now resolve and point at the host ~/.codex.
    assert (home / "auth.json").is_symlink()
    assert (home / "auth.json").resolve() == (user_codex / "auth.json").resolve()
    assert (home / "auth.json").read_text() == '{"token":"new"}'
    assert (home / "config.toml").read_text() == 'model = "gpt-5"\n'


def test_codex_prepare_preserves_real_auth_file(tmp_path, monkeypatch):
    """If the agent has already written its own auth.json (eg by
    completing the device-auth flow inside relaydeck), we must NOT overwrite
    it with a symlink to the host's ~/.codex/auth.json — that would
    blow away credentials the agent just earned."""
    agent = _make_agent(tmp_path, monkeypatch)
    user_codex = tmp_path / ".codex"
    user_codex.mkdir()
    (user_codex / "auth.json").write_text('{"token":"host"}')

    home = agent._codex_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text('{"token":"agent-owned"}')

    agent._prepare_codex_home()

    assert not (home / "auth.json").is_symlink()
    assert (home / "auth.json").read_text() == '{"token":"agent-owned"}'


def test_codex_continue_builds_clean_resume(tmp_path, monkeypatch):
    """Codex has no --continue flag; continue is the `resume --last`
    subcommand, which does NOT accept --model/--cd/--sandbox. The harness
    must build a clean `codex resume --last` (no incompatible flags)."""
    agent = _make_agent(tmp_path, monkeypatch, {"resume_last": True})
    assert agent._build_command() == ["codex", "resume", "--last"]


def test_codex_continue_ignores_model_and_sandbox(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, {
        "resume_last": True, "preset": "anything", "sandbox": "workspace-write",
    })
    cmd = agent._build_command()
    assert cmd == ["codex", "resume", "--last"]
    assert "--model" not in cmd and "--sandbox" not in cmd


def test_codex_continue_appends_args(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, {"resume_last": True, "args": ["-c", "model=o3"]})
    assert agent._build_command() == ["codex", "resume", "--last", "-c", "model=o3"]
