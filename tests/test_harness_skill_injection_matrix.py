"""Cross-harness injection matrix — the contract that makes messaging + CLI
+ skills actually work for the model.

It is *paramount* that when a workspace enables `skills` / `messaging`, the
relevant content reaches EVERY harness through the mechanism that CLI really
reads. If it silently doesn't, the agent never learns the reply contract and
peer messages drop into its own terminal, invisible to the sender.

Two axes, asserted per harness:

  1. **Runtime (plugin-contributed) skill** — e.g. messaging's relaydeck-cli
     reply contract, materialized into `runtime/skills/`. Injected ALWAYS,
     independent of the `skills` gate (a skills-disabled workspace must still
     get messaging). This is the "else messaging won't work" guarantee.
  2. **User-authored workspace skill** (`workspaces/<ws>/skills/`) — gated by
     `skills` in `agent.toml`: delivered when enabled, NOT when disabled.

Plus the identity preamble (which carries the "use the relaydeck CLI to
message peers" hint) must reach every harness.

Delivery shapes differ on purpose (pi `--skill <dir>`; claude-code via a native
`--plugin-dir` skills tree; codex inlines the bodies; opencode lists SKILL.md
paths in its config) so the assertions look for the skill *name* in the
resolved, model-visible blob.
"""

from __future__ import annotations

import importlib
import json
import threading
from pathlib import Path

import pytest

from relaydeck.testing import harness_delivery_blob

PURPOSE = "GATE-PRS-SENTINEL"        # distinctive identity-preamble marker
USER_SKILL = "user-authored-probe"   # workspaces/<ws>/skills/<name> (gated)
MSG_SKILL = "relay-cli-probe"        # runtime/skills/<name> (always)

# (label, module, class) for the four harnesses with a system-prompt analog.
HARNESSES = [
    ("pi", "plugins.harnesses.pi.agent", "PiAgent"),
    ("claude-code", "plugins.harnesses.claude_code.agent", "ClaudeCodeAgent"),
    ("codex", "plugins.harnesses.codex.agent", "CodexAgent"),
    ("opencode", "plugins.harnesses.opencode.agent", "OpenCodeAgent"),
    ("cursor", "plugins.harnesses.cursor.agent", "CursorAgent"),
]


def _setup(tmp_path, monkeypatch, agent_type: str, ws_plugins: list[str]) -> Path:
    """Isolated config home: a registered workspace whose `agent.toml` carries
    `ws_plugins`, one user-authored skill, one runtime (plugin) skill, and an
    agent spec with a purpose."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # claude-code resolves its config home via RELAYDECK_CONFIG_HOME-first
    # (pi/codex/opencode use Path.home()). The daemon sets neither, but a
    # sibling test can leak it — clear it so every harness resolves to this
    # isolated home identically.
    monkeypatch.delenv("RELAYDECK_CONFIG_HOME", raising=False)
    home = tmp_path / ".relaydeck"
    (home / "runtime").mkdir(parents=True)

    from relaydeck.config import AgentSpec, register_workspace
    ws_dir = tmp_path / "proj"
    ws_dir.mkdir()
    # register_workspace writes both config.toml AND agent.toml's plugin list,
    # which is what each harness reads to decide its gated injections.
    register_workspace(home, "proj", ws_dir, ws_plugins)

    user = home / "workspaces" / "proj" / "skills" / USER_SKILL
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text(
        f"---\nname: {USER_SKILL}\ndescription: a user-authored workspace skill\n---\n\n"
        "Do the user-authored thing.\n"
    )

    runtime = home / "workspaces" / "proj" / "runtime" / "skills" / MSG_SKILL
    runtime.mkdir(parents=True)
    (runtime / "SKILL.md").write_text(
        f"---\nname: {MSG_SKILL}\ndescription: reply to peers via the relaydeck CLI\n---\n\n"
        "Reply with `relaydeck reply <id> <body>`.\n"
    )
    (runtime / ".relaydeck-skill.json").write_text(
        json.dumps({"owner_plugin": "messaging", "managed_by": "skills"})
    )

    AgentSpec(
        id="probe", name="probe", type=agent_type, workspace="proj",
        purpose=PURPOSE, inject_identity_preamble=True,
    ).save(home)
    return home


def _agent(module: str, cls_name: str, tmp_path):
    cls = getattr(importlib.import_module(module), cls_name)
    return cls(
        agent_id="probe", name="probe", config={}, workspace="proj",
        db_path=str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db"),
        stop_flag=threading.Event(),
    )


@pytest.mark.parametrize("label,module,cls", HARNESSES)
def test_skills_and_messaging_injected_when_enabled(tmp_path, monkeypatch, label, module, cls):
    """`skills` + `messaging` enabled → preamble, the messaging (runtime)
    skill, AND the user-authored skill all reach the harness."""
    _setup(tmp_path, monkeypatch, label, ["skills", "messaging"])
    blob = harness_delivery_blob(_agent(module, cls, tmp_path))
    assert PURPOSE in blob, f"{label}: identity preamble (purpose) not delivered"
    assert MSG_SKILL in blob, (
        f"{label}: messaging skill not delivered -- peer replies would silently drop"
    )
    assert USER_SKILL in blob, (
        f"{label}: user-authored skill not delivered though `skills` is enabled"
    )


@pytest.mark.parametrize("label,module,cls", HARNESSES)
def test_messaging_injected_even_without_skills_gate(tmp_path, monkeypatch, label, module, cls):
    """`skills` gate OFF (only messaging) → the messaging runtime skill is
    STILL delivered (ungated, so a skills-disabled workspace keeps working),
    while the user-authored skill is correctly withheld."""
    _setup(tmp_path, monkeypatch, label, ["messaging"])
    blob = harness_delivery_blob(_agent(module, cls, tmp_path))
    assert MSG_SKILL in blob, (
        f"{label}: messaging skill not delivered without the `skills` gate"
    )
    assert USER_SKILL not in blob, (
        f"{label}: user-authored skill leaked despite the `skills` gate being OFF"
    )
