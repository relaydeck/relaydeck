"""End-to-end: every harness must DELIVER the agent's identity (purpose)
and plugin-contributed skills through the mechanism that CLI actually reads.

This asserts on the built command / env / generated config — not just the
text builder. That distinction matters: the codex `model_instructions_file`
regression produced a perfectly correct instructions *string* that codex
silently ignored (dead config key), so only a delivery-level test catches
it.

Claude Code is intentionally skipped (per request). It inlines the
preamble + skill bodies into `--append-system-prompt`; its composition is
covered in test_claude_code_harness.py.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

PURPOSE = "GATE-PRS-SENTINEL"      # distinctive marker we can grep for
SKILL = "relay-cli"                 # a plugin-contributed (runtime) skill


def _setup(tmp_path, monkeypatch, agent_type: str):
    """Temp config home with: a registered workspace, one materialized
    plugin skill, and an agent spec carrying a purpose."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".relaydeck"
    (home / "runtime").mkdir(parents=True)

    from relaydeck.config import AgentSpec, register_workspace
    ws_dir = tmp_path / "proj"
    ws_dir.mkdir()
    register_workspace(home, "proj", ws_dir, [])

    # A plugin-contributed skill lives under runtime/skills/<name>/ with a
    # valid SKILL.md (frontmatter name+description) + ownership sidecar —
    # exactly what the `skills` plugin materializes.
    skill = home / "workspaces" / "proj" / "runtime" / "skills" / SKILL
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {SKILL}\ndescription: message peers via the relaydeck CLI\n---\n\n"
        "Reply to peers with `relaydeck reply <id> <body>`.\n"
    )
    (skill / ".relaydeck-skill.json").write_text(
        json.dumps({"owner_plugin": "messaging", "managed_by": "skills"})
    )

    AgentSpec(
        id="probe", name="probe", type=agent_type, workspace="proj",
        purpose=PURPOSE, inject_identity_preamble=True,
    ).save(home)
    return home


def _agent(cls, tmp_path):
    return cls(
        agent_id="probe", name="probe", config={}, workspace="proj",
        db_path=str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db"),
        stop_flag=threading.Event(),
    )


def _config_value(cmd: list[str], key: str) -> str | None:
    for i, part in enumerate(cmd):
        if part == "--config" and i + 1 < len(cmd) and cmd[i + 1].startswith(key + "="):
            return cmd[i + 1].split("=", 1)[1]
    return None


# ── pi: --append-system-prompt (purpose) + --skill <dir> (plugin skill) ──


def test_pi_delivers_purpose_and_plugin_skill(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "pi")
    from plugins.harnesses.pi.agent import PiAgent
    cmd = _agent(PiAgent, tmp_path)._build_command()
    joined = " ".join(cmd)
    assert "--append-system-prompt" in cmd
    assert PURPOSE in joined                 # identity preamble carries the purpose
    assert "--skill" in cmd
    assert SKILL in joined                   # plugin skill injected as a --skill dir


# ── codex: developer_instructions (NOT the dead model_instructions_file) ──


def test_codex_delivers_purpose_and_plugin_skill(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "codex")
    from plugins.harnesses.codex.agent import CodexAgent
    cmd = _agent(CodexAgent, tmp_path)._build_command()
    instr = _config_value(cmd, "developer_instructions")
    assert instr is not None, "codex must deliver instructions via developer_instructions"
    assert PURPOSE in instr
    assert SKILL in instr
    # the dead key must never be used (current codex silently ignores it)
    assert _config_value(cmd, "model_instructions_file") is None


# ── opencode: OPENCODE_CONFIG → instructions file list ──────────────────


def test_opencode_delivers_purpose_and_plugin_skill(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "opencode")
    from plugins.harnesses.opencode.agent import OpenCodeAgent
    env = _agent(OpenCodeAgent, tmp_path)._build_env()
    assert "OPENCODE_CONFIG" in env
    cfg = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
    instr_files = cfg.get("instructions") or []
    assert instr_files, "opencode config must reference an instructions file"
    text = "\n".join(
        Path(p).read_text() for p in instr_files if Path(p).exists()
    )
    assert PURPOSE in text
    assert SKILL in text
