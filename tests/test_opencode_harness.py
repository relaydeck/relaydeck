from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.harnesses.opencode.agent import OpenCodeAgent


def _make_agent(tmp_path: Path, monkeypatch, config: dict | None = None) -> OpenCodeAgent:
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
    return OpenCodeAgent(
        agent_id="coder",
        name="coder",
        config=config or {},
        workspace="demo",
        db_path=str(tmp_path / "relaydeck.db"),
        stop_flag=threading.Event(),
    )


def test_opencode_plugin_loads_and_registers_agent_types(tmp_path):
    import relaydeck.plugin as plug
    from relaydeck.orchestrator import known_agent_types
    from relaydeck.plugin import PluginContext, get_registry

    plug._registry = None
    reg = get_registry(tmp_path / ".relaydeck")
    reg.load_all(PluginContext(config_home=tmp_path / ".relaydeck"))

    assert reg.get("opencode-harness") is not None
    assert "opencode" in known_agent_types()
    assert "opencode-cli" in known_agent_types()


def test_opencode_command_uses_native_tui_flags(tmp_path, monkeypatch):
    cfg_home = tmp_path / ".relaydeck"
    presets = cfg_home / "presets"
    presets.mkdir(parents=True)
    (presets / "sonnet.yaml").write_text(
        "name: sonnet\nprovider: anthropic\nmodel: claude-sonnet-4-5\n"
    )
    agent = _make_agent(
        tmp_path,
        monkeypatch,
        {
            "model": "sonnet",
            "opencode_agent": "build",
            "initial_prompt": "Start here.",
            "log_level": "DEBUG",
            "forbidden_tools": ["rm", "curl"],
        },
    )

    cmd = agent._build_command()

    assert cmd[:3] == ["opencode", "--log-level", "DEBUG"]
    assert cmd[cmd.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    assert cmd[cmd.index("--agent") + 1] == "build"
    assert cmd[cmd.index("--prompt") + 1] == "Start here."
    assert cmd[-1] == str(tmp_path / "repo")
    assert "--workspace" not in cmd
    assert "--session-dir" not in cmd


def test_opencode_command_resolves_dashboard_preset_key(tmp_path, monkeypatch):
    cfg_home = tmp_path / ".relaydeck"
    presets = cfg_home / "presets"
    presets.mkdir(parents=True)
    (presets / "ollama-gemma.yaml").write_text(
        "name: ollama-gemma\nprovider: ollama\nmodel: gemma4:latest\n"
    )
    agent = _make_agent(tmp_path, monkeypatch, {"preset": "ollama-gemma"})

    cmd = agent._build_command()

    assert cmd[cmd.index("--model") + 1] == "ollama/gemma4:latest"


def test_opencode_config_declares_ollama_preset_model(tmp_path, monkeypatch):
    cfg_home = tmp_path / ".relaydeck"
    presets = cfg_home / "presets"
    presets.mkdir(parents=True)
    (presets / "ollama-gemma.yaml").write_text(
        "name: ollama-gemma\nprovider: ollama\nmodel: gemma4:latest\n"
    )
    agent = _make_agent(tmp_path, monkeypatch, {"preset": "ollama-gemma"})

    env = agent._build_env()

    data = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
    ollama = data["provider"]["ollama"]
    assert ollama["npm"] == "@ai-sdk/openai-compatible"
    assert ollama["options"]["baseURL"] == "http://127.0.0.1:11434/v1"
    assert ollama["models"]["gemma4:latest"]["name"] == "gemma4:latest"


def test_opencode_config_preserves_user_ollama_provider_values(tmp_path, monkeypatch):
    cfg_home = tmp_path / ".relaydeck"
    presets = cfg_home / "presets"
    presets.mkdir(parents=True)
    (presets / "ollama-gemma.yaml").write_text(
        "name: ollama-gemma\nprovider: ollama\nmodel: gemma4:latest\n"
    )
    agent = _make_agent(
        tmp_path,
        monkeypatch,
        {
            "preset": "ollama-gemma",
            "opencode_config": {
                "provider": {
                    "ollama": {
                        "name": "Local",
                        "options": {"baseURL": "http://localhost:11434/v1"},
                        "models": {"qwen": {"name": "qwen"}},
                    }
                }
            },
        },
    )

    env = agent._build_env()

    data = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
    ollama = data["provider"]["ollama"]
    assert ollama["name"] == "Local"
    assert ollama["options"]["baseURL"] == "http://localhost:11434/v1"
    assert "qwen" in ollama["models"]
    assert "gemma4:latest" in ollama["models"]


def test_opencode_env_points_to_generated_config(tmp_path, monkeypatch):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    agent = _make_agent(tmp_path, monkeypatch, {"forbidden_tools": ["rm"]})
    env = agent._build_env()

    cfg = agent._relaydeck_config_file()
    instructions = agent._relaydeck_instructions_file()
    data = json.loads(cfg.read_text())

    assert env["OPENCODE_CONFIG"] == str(cfg)
    assert "OPENCODE_CONFIG_CONTENT" not in env
    assert str(instructions) in data["instructions"]
    assert "Avoid these tools" in instructions.read_text()
    assert env["RELAYDECK_AGENT_ID"] == "coder"
    assert env["RELAYDECK_WORKSPACE"] == "demo"
    assert env["COLORTERM"] == "truecolor"
    assert env["FORCE_COLOR"] == "3"
    assert env["TERM_PROGRAM"] == "xterm.js"


def test_opencode_harness_sanitizes_raw_reconnect_replay(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)

    assert agent.REPLAY_PTY_BUFFER is True
    assert agent.SANITIZE_PTY_REPLAY is True


def test_opencode_log_path_points_at_global_log_dir(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)

    assert agent.log_path() == tmp_path / ".local" / "share" / "opencode" / "log"


def test_opencode_instructions_include_workspace_contributions(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    cfg_home = tmp_path / ".relaydeck"
    ws_home = cfg_home / "workspaces" / "demo"
    (ws_home / "runtime" / "fleet-context").mkdir(parents=True)
    (ws_home / "runtime" / "fleet-context" / "coder.md").write_text("Fleet context.")
    (ws_home / "skills" / "review").mkdir(parents=True)
    (ws_home / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: review skill\n---\n\nSkill body."
    )
    (ws_home / "runtime" / "skills" / "relaydeck-cli").mkdir(parents=True)
    (ws_home / "runtime" / "skills" / "relaydeck-cli" / "SKILL.md").write_text(
        "---\nname: relaydeck-cli\ndescription: relaydeck cli skill\n---\n\nrelaydeck skill."
    )

    agent._build_env()

    instructions = Path(json.loads(agent._relaydeck_config_file().read_text())["instructions"][-1])
    body = instructions.read_text()
    assert "Fleet context." in body
    assert "skills/review/SKILL.md" in body
    assert "runtime/skills/relaydeck-cli/SKILL.md" in body


def test_opencode_skips_invalid_skills(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    cfg_home = tmp_path / ".relaydeck"
    ws_home = cfg_home / "workspaces" / "demo"
    (ws_home / "skills" / "valid").mkdir(parents=True)
    (ws_home / "skills" / "valid" / "SKILL.md").write_text(
        "---\nname: valid\ndescription: valid skill\n---\n\nSkill body."
    )
    (ws_home / "skills" / "invalid").mkdir(parents=True)
    (ws_home / "skills" / "invalid" / "SKILL.md").write_text("missing frontmatter")
    (ws_home / "runtime" / "skills" / "runtime-valid").mkdir(parents=True)
    (ws_home / "runtime" / "skills" / "runtime-valid" / "SKILL.md").write_text(
        "---\nname: runtime-valid\ndescription: runtime skill\n---\n\nRuntime body."
    )
    (ws_home / "runtime" / "skills" / "runtime-invalid").mkdir(parents=True)
    (ws_home / "runtime" / "skills" / "runtime-invalid" / "SKILL.md").write_text(
        "missing frontmatter"
    )

    agent._build_env()

    body = agent._relaydeck_instructions_file().read_text()
    assert "skills/valid/SKILL.md" in body
    assert "skills/invalid/SKILL.md" not in body
    assert "runtime/skills/runtime-valid/SKILL.md" in body
    assert "runtime/skills/runtime-invalid/SKILL.md" not in body


def test_opencode_generated_config_merges_user_config_file(tmp_path, monkeypatch):
    user_cfg = tmp_path / "user-opencode.json"
    existing = tmp_path / "existing.md"
    existing.write_text("Existing instructions.")
    user_cfg.write_text(
        json.dumps({
            "autoupdate": False,
            "instructions": [str(existing)],
            "permission": {"edit": "ask"},
        })
    )
    agent = _make_agent(
        tmp_path,
        monkeypatch,
        {
            "opencode_config_file": str(user_cfg),
            "opencode_config": {
                "model": "anthropic/claude-sonnet-4-5",
                "instructions": [str(tmp_path / "override.md")],
            },
        },
    )

    agent._build_env()

    data = json.loads(agent._relaydeck_config_file().read_text())
    assert data["autoupdate"] is False
    assert data["permission"] == {"edit": "ask"}
    assert data["model"] == "anthropic/claude-sonnet-4-5"
    assert str(existing) not in data["instructions"]
    assert str(tmp_path / "override.md") in data["instructions"]
    assert str(agent._relaydeck_instructions_file()) in data["instructions"]


def test_opencode_command_override_wins(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, {"command": "opencode models"})

    assert agent._build_command() == ["opencode", "models"]


def test_opencode_appends_config_args(tmp_path, monkeypatch):
    """`config.args` (list or string) is appended last — parity with
    pi/codex/base so the new-agent modal's "extra flags" field works for
    opencode too."""
    agent = _make_agent(tmp_path, monkeypatch, {"args": ["--print-logs"]})
    assert agent._build_command()[-1] == "--print-logs"

    # string form is shlex-split
    agent.config["args"] = "--log-level debug"
    assert agent._build_command()[-2:] == ["--log-level", "debug"]


def test_colorterm_overrides_inherited_empty(tmp_path, monkeypatch):
    """An inherited COLORTERM="" (present-but-empty) must still become
    truecolor — setdefault wouldn't override it, `or` does."""
    monkeypatch.setenv("COLORTERM", "")
    monkeypatch.setenv("FORCE_COLOR", "")
    agent = _make_agent(tmp_path, monkeypatch)
    env = agent._build_env()
    assert env["COLORTERM"] == "truecolor"
    assert env["FORCE_COLOR"] == "3"


def test_force_color_zero_is_preserved(tmp_path, monkeypatch):
    """An explicit FORCE_COLOR=0 must not be overridden."""
    monkeypatch.setenv("FORCE_COLOR", "0")
    agent = _make_agent(tmp_path, monkeypatch)
    env = agent._build_env()
    assert env["FORCE_COLOR"] == "0"
