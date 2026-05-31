"""Antigravity harness (`agy`): command build, autonomy → permission flags,
model selection, positional-prompt context injection, and registration.

The flag surface is modeled on the documented/observed Antigravity 2.0 CLI
(flags not yet verified against an installed binary — see the agent module
docstring). These tests pin the *shape* relaydeck produces, not live `agy`
behavior, so a flag correction stays a one-line change with a failing test to
catch it.
"""

from __future__ import annotations

import tempfile
import threading
from contextlib import suppress
from pathlib import Path

from relaydeck.config import AgentSpec, register_workspace
from relaydeck.harness_options import HARNESS_CLI, build_harness_catalog
from plugins.harnesses.antigravity.agent import AntigravityAgent


def _agent(tmp_path, monkeypatch, config=None, *, purpose="agy probe"):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_CONFIG_HOME", raising=False)
    home = tmp_path / ".relaydeck"
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "proj"
    ws.mkdir(exist_ok=True)
    # Idempotent: some tests build two agents on the same tmp workspace.
    with suppress(ValueError):
        register_workspace(home, "proj", ws, ["messaging"])
    AgentSpec(
        id="probe", name="probe", type="antigravity", workspace="proj",
        purpose=purpose, inject_identity_preamble=True,
    ).save(home)
    return AntigravityAgent(
        agent_id="probe", name="probe", config=config or {}, workspace="proj",
        db_path=str(home / "runtime" / "relaydeck.db"), stop_flag=threading.Event(),
    )


# ── command shape ────────────────────────────────────────────────────


def test_auto_skips_permissions_and_injects_interactive_prompt(tmp_path, monkeypatch):
    cmd = _agent(
        tmp_path, monkeypatch, {"initial_prompt": "do the thing"},
    )._build_command()
    assert cmd[0] == "agy"
    # auto (default) is unattended → must auto-approve tools, else it blocks.
    assert "--dangerously-skip-permissions" in cmd
    # Prompt goes via --prompt-interactive (agy 1.0.3 has no positional prompt;
    # -p/--print is one-shot and would exit). The prompt is the flag's VALUE,
    # so it's the last token, immediately after --prompt-interactive.
    assert "--prompt-interactive" in cmd
    assert cmd[cmd.index("--prompt-interactive") + 1] == cmd[-1]
    assert cmd[-1].endswith("do the thing")
    assert "agent `probe`" in cmd[-1]
    # agy has NO --model flag — never pass one.
    assert "--model" not in cmd


def test_bypass_skips_permissions(tmp_path, monkeypatch):
    cmd = _agent(tmp_path, monkeypatch, {"autonomy": "bypass"})._build_command()
    assert "--dangerously-skip-permissions" in cmd


def test_locked_and_manual_do_not_skip_permissions(tmp_path, monkeypatch):
    # agy has no per-command allowlist, so locked can't selectively allow; it
    # (and manual) leave permission handling to the operator/CLI.
    for mode in ("locked", "manual"):
        cmd = _agent(tmp_path, monkeypatch, {"autonomy": mode})._build_command()
        assert "--dangerously-skip-permissions" not in cmd, mode


def test_never_passes_a_model_flag(tmp_path, monkeypatch):
    # agy 1.0.3 has no --model flag; the model is account-managed. Even a stray
    # config model must not produce a flag (there's nothing to pass it to).
    cmd = _agent(tmp_path, monkeypatch)._build_command()
    assert "--model" not in cmd
    cmd = _agent(
        tmp_path, monkeypatch, {"antigravity_model": "gemini-3-pro"},
    )._build_command()
    assert "--model" not in cmd
    assert "gemini-3-pro" not in cmd


def test_continue_flag_via_launch_option(tmp_path, monkeypatch):
    # The "Continue last conversation" launch option lands as config.args.
    cmd = _agent(tmp_path, monkeypatch, {"args": ["--continue"]})._build_command()
    assert "--continue" in cmd


def test_operator_skip_flag_not_double_added(tmp_path, monkeypatch):
    cmd = _agent(
        tmp_path, monkeypatch,
        {"autonomy": "auto", "args": "--dangerously-skip-permissions"},
    )._build_command()
    assert cmd.count("--dangerously-skip-permissions") == 1


def test_command_override_wins(tmp_path, monkeypatch):
    cmd = _agent(
        tmp_path, monkeypatch, {"command": "agy --version"},
    )._build_command()
    assert cmd == ["agy", "--version"]


def test_initial_prompt_lists_plugin_skills(tmp_path, monkeypatch):
    # A messaging workspace materializes the relay-cli skill into runtime/skills;
    # the harness must reference it in the initial prompt (path, not body).
    agent = _agent(tmp_path, monkeypatch)
    runtime_skills = (
        tmp_path / ".relaydeck" / "workspaces" / "proj" / "runtime" / "skills"
        / "relaydeck-cli"
    )
    runtime_skills.mkdir(parents=True, exist_ok=True)
    (runtime_skills / "SKILL.md").write_text(
        "---\nname: relaydeck-cli\ndescription: peer messaging\n---\n\nbody"
    )
    prompt = agent._initial_prompt() or ""
    assert "SKILL.md" in prompt
    assert "relaydeck-cli" in prompt


# ── workspace-trust pre-seed (unattended blocker) ──────────────────────


def _trusted(agy_home):
    import json
    return json.loads((agy_home / "settings.json").read_text())["trustedWorkspaces"]


def test_workspace_trust_pre_seeded(tmp_path, monkeypatch):
    # agy blocks on "Do you trust the contents of this project?" for an
    # unattended agent; the harness must pre-seed the workspace into agy's
    # trustedWorkspaces so it boots straight into work.
    agy_home = tmp_path / "agy-home"
    monkeypatch.setenv("ANTIGRAVITY_CLI_HOME", str(agy_home))
    agent = _agent(tmp_path, monkeypatch)
    agent._ensure_workspace_trusted()
    proj = str((tmp_path / "proj").resolve())
    assert proj in _trusted(agy_home)


def test_workspace_trust_skipped_for_manual(tmp_path, monkeypatch):
    # manual autonomy = operator drives; don't touch agy's trust store.
    agy_home = tmp_path / "agy-home-manual"
    monkeypatch.setenv("ANTIGRAVITY_CLI_HOME", str(agy_home))
    agent = _agent(tmp_path, monkeypatch, {"autonomy": "manual"})
    agent._ensure_workspace_trusted()
    assert not (agy_home / "settings.json").exists()


def test_trust_pre_seed_preserves_existing_settings(tmp_path, monkeypatch):
    import json
    agy_home = tmp_path / "agy-home-merge"
    agy_home.mkdir()
    (agy_home / "settings.json").write_text(
        json.dumps({"trustedWorkspaces": ["/some/other"], "keybind": "x"}))
    monkeypatch.setenv("ANTIGRAVITY_CLI_HOME", str(agy_home))
    agent = _agent(tmp_path, monkeypatch)
    agent._ensure_workspace_trusted()
    data = json.loads((agy_home / "settings.json").read_text())
    assert "/some/other" in data["trustedWorkspaces"]  # not clobbered
    assert data["keybind"] == "x"                       # other keys preserved
    assert any("proj" in p for p in data["trustedWorkspaces"])  # ours added


def test_build_env_pre_seeds_trust(tmp_path, monkeypatch):
    # _build_env is the spawn-time hook that pre-seeds trust.
    agy_home = tmp_path / "agy-home-env"
    monkeypatch.setenv("ANTIGRAVITY_CLI_HOME", str(agy_home))
    _agent(tmp_path, monkeypatch)._build_env()
    proj = str((tmp_path / "proj").resolve())
    assert proj in _trusted(agy_home)


# ── catalog + registration ─────────────────────────────────────────────


def test_catalog_lists_antigravity_with_install_hint():
    cat = build_harness_catalog(tempfile.mkdtemp())
    entry = next((c for c in cat if c["type"] == "antigravity"), None)
    assert entry is not None
    assert entry["cli"] == "agy"
    assert HARNESS_CLI["antigravity"] == "agy"
    # No model picker for agy — it has no CLI model flag, so no model_config_key.
    assert not entry.get("model_config_key")
    # Binary isn't installed in CI → an actionable install hint is surfaced.
    if not entry["cli_installed"]:
        assert "agy" in entry["install_hint"]


def test_plugin_registers_both_type_names():
    from plugins.harnesses.antigravity.plugin import PLUGIN

    registered: dict[str, type] = {}

    class _FakeHarnesses:
        def register(self, name, cls):
            registered[name] = cls

    class _FakeHost:
        harnesses = _FakeHarnesses()

    PLUGIN.on_load(_FakeHost())
    assert registered["antigravity"] is AntigravityAgent
    assert registered["agy"] is AntigravityAgent
