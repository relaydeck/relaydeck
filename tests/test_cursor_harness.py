"""Cursor harness (cursor-agent): command build, per-agent CURSOR_CONFIG_DIR
isolation, model selection, and positional-prompt context injection.

Autonomy posture (approvalMode / sandbox / relaydeck-always-allowed) is pinned
cross-harness in test_harness_autonomy.py; this file covers the Cursor-specific
command shape + isolation.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from relaydeck.config import AgentSpec, register_workspace
from plugins.harnesses.cursor.agent import CursorAgent


def _agent(tmp_path, monkeypatch, config=None, *, purpose="cursor probe"):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_CONFIG_HOME", raising=False)
    home = tmp_path / ".relaydeck"
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "proj"
    ws.mkdir(exist_ok=True)
    register_workspace(home, "proj", ws, ["messaging"])
    AgentSpec(
        id="probe", name="probe", type="cursor-cli", workspace="proj",
        purpose=purpose, inject_identity_preamble=True,
    ).save(home)
    return CursorAgent(
        agent_id="probe", name="probe", config=config or {}, workspace="proj",
        db_path=str(home / "runtime" / "relaydeck.db"), stop_flag=threading.Event(),
    )


def test_command_has_model_workspace_and_positional_prompt(tmp_path, monkeypatch):
    cmd = _agent(
        tmp_path, monkeypatch,
        {"cursor_model": "sonnet-4", "initial_prompt": "do the thing"},
    )._build_command()
    assert cmd[0] == "cursor-agent"
    assert cmd[cmd.index("--model") + 1] == "sonnet-4"
    assert "--workspace" in cmd
    # The positional prompt is LAST and carries identity preamble + user prompt.
    assert cmd[-1].endswith("do the thing")
    assert "agent `probe`" in cmd[-1]


def test_no_model_flag_without_cursor_model(tmp_path, monkeypatch):
    # Cursor uses its own model namespace; without an explicit cursor_model we
    # leave --model off and let the account's selected model stand.
    cmd = _agent(tmp_path, monkeypatch)._build_command()
    assert "--model" not in cmd


def test_config_home_is_isolated_and_exported(tmp_path, monkeypatch):
    env = _agent(tmp_path, monkeypatch)._build_env()
    home = env["CURSOR_CONFIG_DIR"]
    assert home.endswith("cursor-homes/probe")
    # Both config + data dirs are isolated per-agent.
    assert env["CURSOR_DATA_DIR"] == home
    # The merged cli-config.json is materialized before spawn.
    assert (Path(home) / "cli-config.json").exists()


def test_workspace_is_pre_trusted_to_avoid_the_trust_prompt(tmp_path, monkeypatch):
    # Cursor blocks on a "Workspace Trust Required" prompt on first run in a
    # directory — fatal unattended. The harness pre-seeds the trust file under
    # the isolated data dir so the agent boots straight into work.
    agent = _agent(tmp_path, monkeypatch)
    agent._build_env()  # writes the trust file
    home = Path(agent._cursor_home())
    cwd = str((tmp_path / "proj").resolve())
    expected = home / "projects" / agent._munge_path(cwd) / ".workspace-trusted"
    assert expected.exists(), "workspace trust file was not pre-seeded"
    data = json.loads(expected.read_text())
    assert data["workspacePath"] == cwd


def test_manual_autonomy_does_not_pre_trust(tmp_path, monkeypatch):
    # manual = operator drives; relaydeck injects nothing, trust included.
    agent = _agent(tmp_path, monkeypatch, {"autonomy": "manual"})
    assert agent._ensure_workspace_trusted() is None


def test_extra_args_precede_positional_prompt(tmp_path, monkeypatch):
    # Cursor's prompt is variadic ([prompt...]); launch-option flags must land
    # BEFORE it or they'd be swallowed as prompt words.
    cmd = _agent(
        tmp_path, monkeypatch, {"args": ["--plan"], "initial_prompt": "go"},
    )._build_command()
    assert cmd.index("--plan") < len(cmd) - 1
    assert cmd[-1].endswith("go")


def test_command_override_replaces_everything(tmp_path, monkeypatch):
    cmd = _agent(
        tmp_path, monkeypatch, {"command": "cursor-agent --version"},
    )._build_command()
    assert cmd == ["cursor-agent", "--version"]


def test_user_cursor_config_is_preserved_and_merged(tmp_path, monkeypatch):
    # An operator's ~/.cursor model/privacy settings carry into the isolated
    # home; relaydeck only layers on autonomy + the relaydeck allowlist.
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "cli-config.json").write_text(
        json.dumps({"model": {"modelId": "composer-2.5"}, "permissions": {"allow": ["Shell(ls)"]}})
    )
    agent = _agent(tmp_path, monkeypatch)
    data = json.loads(Path(agent._write_cursor_config()).read_text())
    assert data["model"]["modelId"] == "composer-2.5"          # preserved
    assert "Shell(ls)" in data["permissions"]["allow"]          # preserved
    assert "Shell(relaydeck)" in data["permissions"]["allow"]   # added
