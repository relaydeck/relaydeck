"""
Per-agent `system_prompt` + auto identity preamble.

Pins:
  - spec round-trips both new fields
  - `_compose_system_prompt` produces the identity preamble from purpose
    and peer list, appends the user's system_prompt, and yields ""
    when both are off/empty (so the harness skips the flag entirely)
  - pi adds `--append-system-prompt <composed>` to its command
  - codex passes composed text via `developer_instructions` config
  - claude-code adds `--append-system-prompt`
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import yaml
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.config import AgentSpec
from relaydeck.db import open_db
from relaydeck.transports.cli import main as cli

# ── Spec persistence ────────────────────────────────────────────────


def test_spec_defaults_for_new_fields():
    s = AgentSpec(id="x", name="x", type="pi")
    assert s.system_prompt == ""
    assert s.inject_identity_preamble is True


def test_spec_yaml_roundtrip_system_prompt(tmp_path):
    s = AgentSpec(
        id="rev", name="Reviewer", type="claude-code",
        system_prompt="Always check for SQL injection.",
        inject_identity_preamble=False,
    )
    path = s.save(tmp_path)
    loaded = AgentSpec.from_yaml(path)
    assert loaded.system_prompt == "Always check for SQL injection."
    assert loaded.inject_identity_preamble is False


def test_spec_loads_legacy_yaml_without_prompt_fields(tmp_path):
    """A pre-existing spec must continue to load with sensible defaults."""
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.dump({"id": "legacy", "name": "Legacy", "type": "pi"}))
    loaded = AgentSpec.from_yaml(path)
    assert loaded.system_prompt == ""
    assert loaded.inject_identity_preamble is True


# ── Composition ─────────────────────────────────────────────────────


def _make_agent(tmp_path, monkeypatch, agent_id="me", workspace="demo", **kwargs):
    """Build a minimal Harness-style agent against a tmp config home.
    The spec is materialized so HarnessAgent._load_local_spec finds it."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)

    spec = AgentSpec(id=agent_id, name=agent_id, type="pi", workspace=workspace, **kwargs)
    spec.save(cfg_home)

    from relaydeck.harness import HarnessAgent

    class _Bare(HarnessAgent):
        CLI = "true"

    return _Bare(
        agent_id=agent_id, name=agent_id, config={}, workspace=workspace,
        db_path=str(cfg_home / "runtime" / "relaydeck.db"),
        stop_flag=threading.Event(),
    )


def _seed_peer(tmp_path, peer_id, workspace, *, purpose="", type="pi"):
    """Drop a peer row into the DB so _list_peers picks it up."""
    db_path = str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db")
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO agents (id, type, name, status, workspace, "
            "auto_start, created_at, purpose) VALUES "
            "(?, ?, ?, 'stopped', ?, 0, 0, ?)",
            (peer_id, type, peer_id, workspace, purpose or None),
        )
        conn.commit()
    finally:
        conn.close()


def test_compose_empty_when_no_preamble_and_no_prompt(tmp_path, monkeypatch):
    """Both off → empty string → harness adds no --append-system-prompt."""
    a = _make_agent(tmp_path, monkeypatch,
                    inject_identity_preamble=False, system_prompt="")
    assert a._compose_system_prompt() == ""


def test_compose_includes_identity_preamble(tmp_path, monkeypatch):
    a = _make_agent(tmp_path, monkeypatch, agent_id="me",
                    purpose="Designs APIs",
                    inject_identity_preamble=True)
    text = a._compose_system_prompt()
    assert "You are agent `me`" in text
    assert "workspace `demo`" in text
    assert "Your purpose: Designs APIs" in text


def test_compose_includes_peers_with_purposes(tmp_path, monkeypatch):
    a = _make_agent(tmp_path, monkeypatch, agent_id="me", purpose="Coordinator")
    _seed_peer(tmp_path, "reviewer", "demo",
               purpose="Reviews PRs for security", type="claude-code")
    _seed_peer(tmp_path, "coder", "demo",
               purpose="Implements features per spec", type="pi")
    # Self should not appear in the peer list.
    _seed_peer(tmp_path, "me", "demo", purpose="Coordinator")

    text = a._compose_system_prompt()
    assert "reviewer" in text and "claude-code" in text
    assert "Reviews PRs for security" in text
    assert "coder" in text
    # Self exclusion — purpose appears in the header line but not in
    # the peer list (we'd see "me (pi)" twice if the filter was broken).
    assert text.count("`me`") == 1
    assert text.count("Coordinator") == 1


def test_compose_appends_user_system_prompt(tmp_path, monkeypatch):
    a = _make_agent(tmp_path, monkeypatch, agent_id="me", purpose="X",
                    system_prompt="Always speak in haiku.")
    text = a._compose_system_prompt()
    # Identity first, then a blank line, then the user prompt
    assert text.endswith("Always speak in haiku.")
    assert "You are agent `me`" in text


def test_compose_user_prompt_only_when_identity_off(tmp_path, monkeypatch):
    a = _make_agent(tmp_path, monkeypatch,
                    inject_identity_preamble=False,
                    system_prompt="A blank-slate agent.")
    text = a._compose_system_prompt()
    assert text == "A blank-slate agent."


# ── Per-harness wiring ──────────────────────────────────────────────


def _seed_full_workspace(tmp_path, monkeypatch, *, agent_id, agent_type="pi",
                         purpose="", system_prompt="", identity=True):
    """Set up workspace + spec + DB row so a real harness subclass can
    build its command end-to-end."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    ws_path = tmp_path / "ws"
    ws_path.mkdir(parents=True, exist_ok=True)
    cfg_home.mkdir(parents=True, exist_ok=True)
    (cfg_home / "runtime").mkdir(exist_ok=True)
    (cfg_home / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{ws_path}"\n'
    )
    (cfg_home / "workspaces" / "demo").mkdir(parents=True, exist_ok=True)
    (cfg_home / "workspaces" / "demo" / "agent.toml").write_text(
        '[workspace]\nplugins = []\n'
    )
    spec = AgentSpec(
        id=agent_id, name=agent_id, type=agent_type, workspace="demo",
        purpose=purpose, system_prompt=system_prompt,
        inject_identity_preamble=identity,
    )
    spec.save(cfg_home)
    return cfg_home


def test_pi_command_includes_append_system_prompt(tmp_path, monkeypatch):
    _seed_full_workspace(tmp_path, monkeypatch, agent_id="me",
                         purpose="Designs APIs",
                         system_prompt="Use snake_case.")
    from plugins.harnesses.pi.agent import PiAgent
    a = PiAgent(
        agent_id="me", name="me", config={}, workspace="demo",
        db_path=str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db"),
        stop_flag=threading.Event(),
    )
    cmd = a._build_command()
    assert "--append-system-prompt" in cmd
    idx = cmd.index("--append-system-prompt")
    assert "Designs APIs" in cmd[idx + 1]
    assert "Use snake_case." in cmd[idx + 1]


def test_pi_omits_flag_when_both_disabled(tmp_path, monkeypatch):
    _seed_full_workspace(tmp_path, monkeypatch, agent_id="me",
                         purpose="", system_prompt="", identity=False)
    from plugins.harnesses.pi.agent import PiAgent
    a = PiAgent(
        agent_id="me", name="me", config={}, workspace="demo",
        db_path=str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db"),
        stop_flag=threading.Event(),
    )
    cmd = a._build_command()
    assert "--append-system-prompt" not in cmd


def test_claude_code_command_includes_append_system_prompt(tmp_path, monkeypatch):
    """Same shape for claude-code — its harness subclass adds the
    --append-system-prompt flag (claude CLI supports it natively)."""
    _seed_full_workspace(tmp_path, monkeypatch, agent_id="me",
                         agent_type="claude-code",
                         purpose="Reviews PRs",
                         system_prompt="Always be terse.")
    from plugins.harnesses.claude_code.plugin import ClaudeCodeAgent
    a = ClaudeCodeAgent(
        agent_id="me", name="me", config={}, workspace="demo",
        db_path=str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db"),
        stop_flag=threading.Event(),
    )
    cmd = a._build_command()
    assert "--append-system-prompt" in cmd
    idx = cmd.index("--append-system-prompt")
    body = cmd[idx + 1]
    assert "You are agent `me`" in body
    assert "Reviews PRs" in body
    assert "Always be terse." in body
    # Regression guard: the base harness used to unconditionally
    # inject `--workspace <name>`, which `claude` rejects with
    # "error: unknown option '--workspace'". No shipped harness CLI
    # accepts that flag.
    assert "--workspace" not in cmd


def test_codex_developer_instructions_includes_composed(tmp_path, monkeypatch):
    """Codex gets composed relaydeck context through `developer_instructions`
    (an additive developer message), NOT a positional prompt (would auto-
    submit) and NOT `model_instructions_file` (a dead config key current
    Codex silently ignores)."""
    _seed_full_workspace(tmp_path, monkeypatch, agent_id="me",
                         agent_type="codex-cli",
                         purpose="Specialist reviewer",
                         system_prompt="Use rust idioms.")
    from plugins.harnesses.codex.agent import CodexAgent
    a = CodexAgent(
        agent_id="me", name="me", config={}, workspace="demo",
        db_path=str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db"),
        stop_flag=threading.Event(),
    )
    cmd = a._build_command()
    joined = " ".join(cmd)
    assert "developer_instructions=" in joined
    assert "model_instructions_file=" not in joined
    instr = next((cmd[i + 1].split("=", 1)[1] for i, p in enumerate(cmd)
                  if p == "--config" and cmd[i + 1].startswith("developer_instructions=")), "")
    assert "You are agent `me`" in instr
    assert "Specialist reviewer" in instr
    assert "Use rust idioms." in instr
    assert a._initial_prompt() is None


def test_opencode_instructions_file_includes_composed(tmp_path, monkeypatch):
    """OpenCode gets composed relaydeck context through its generated
    opencode.json instructions file, not through an auto-submitted
    positional prompt."""
    _seed_full_workspace(tmp_path, monkeypatch, agent_id="me",
                         agent_type="opencode-cli",
                         purpose="Owns frontend polish",
                         system_prompt="Prefer small diffs.")
    from plugins.harnesses.opencode.agent import OpenCodeAgent
    a = OpenCodeAgent(
        agent_id="me", name="me", config={}, workspace="demo",
        db_path=str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db"),
        stop_flag=threading.Event(),
    )

    env = a._build_env()

    assert env["OPENCODE_CONFIG"] == str(a._relaydeck_config_file())
    config = yaml.safe_load(a._relaydeck_config_file().read_text())
    assert str(a._relaydeck_instructions_file()) in config["instructions"]
    body = a._relaydeck_instructions_file().read_text()
    assert "You are agent `me`" in body
    assert "Owns frontend polish" in body
    assert "Prefer small diffs." in body
    assert "--prompt" not in a._build_command()


# ── CLI smoke ───────────────────────────────────────────────────────


def test_cli_create_with_system_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None

    runner = CliRunner()
    result = runner.invoke(cli, [
        "agent", "create", "rev", "--type", "claude-code",
        "--purpose", "Reviewer",
        "--system-prompt", "Be terse.",
        "--no-identity",
    ])
    assert result.exit_code == 0, result.output

    loaded = AgentSpec.from_yaml(cfg_home / "agents" / "rev.yaml")
    assert loaded.system_prompt == "Be terse."
    assert loaded.inject_identity_preamble is False


def test_cli_edit_system_prompt_from_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None

    runner = CliRunner()
    runner.invoke(cli, ["agent", "create", "a", "--type", "pi"])

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("# Custom prompt\n\nbe nice to peers\n")

    result = runner.invoke(cli, [
        "agent", "edit", "a",
        "--system-prompt-file", str(prompt_file),
    ])
    assert result.exit_code == 0, result.output

    loaded = AgentSpec.from_yaml(cfg_home / "agents" / "a.yaml")
    assert "be nice to peers" in loaded.system_prompt


def test_cli_edit_no_args_prints_current(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None

    runner = CliRunner()
    runner.invoke(cli, [
        "agent", "create", "a", "--type", "pi",
        "--purpose", "Helps with x",
        "--system-prompt", "Be helpful and direct.",
    ])
    result = runner.invoke(cli, ["agent", "edit", "a"])
    assert result.exit_code == 0
    assert "Helps with x" in result.output
    assert "system_prompt" in result.output
    # System prompt body is shown
    assert "Be helpful" in result.output


def test_cli_edit_toggles_identity_preamble(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None

    runner = CliRunner()
    runner.invoke(cli, ["agent", "create", "a", "--type", "pi"])
    runner.invoke(cli, ["agent", "edit", "a", "--no-identity"])

    loaded = AgentSpec.from_yaml(cfg_home / "agents" / "a.yaml")
    assert loaded.inject_identity_preamble is False
