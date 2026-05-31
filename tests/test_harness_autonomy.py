"""Cross-harness autonomy posture.

A relaydeck agent runs UNATTENDED — there's no human at the harness TUI to
answer an approval prompt. So every harness must, by default, run its work
without stalling AND always be able to run the `relaydeck` CLI (peer messaging /
replies / status), which writes ~/.relaydeck outside any workspace sandbox.

`config.autonomy` (default "auto") is the cross-harness knob:
  - "auto"   run safe work autonomously, guard dangerous ops (best-effort
             per harness: claude classifier, codex sandbox, opencode allow)
  - "bypass" skip all approval/sandbox checks
  - "locked" fail-safe: only the relaydeck allowlist runs, rest denied
  - "manual" inject nothing — operator drives the harness's own flags

pi is the reference: it has no approval prompts at all, so nothing is injected
(its only gating is the tool-name allowlist, operator-driven).
"""

from __future__ import annotations

import importlib
import json
import threading
from pathlib import Path


def _make_agent(tmp_path, monkeypatch, agent_type, module, cls, config=None):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_CONFIG_HOME", raising=False)
    home = tmp_path / ".relaydeck"
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    from relaydeck.config import AgentSpec, register_workspace

    ws_dir = tmp_path / "proj"
    ws_dir.mkdir(exist_ok=True)
    register_workspace(home, "proj", ws_dir, ["messaging"])
    AgentSpec(
        id="probe", name="probe", type=agent_type, workspace="proj",
        purpose="autonomy probe", inject_identity_preamble=True,
    ).save(home)
    cls_ = getattr(importlib.import_module(module), cls)
    return cls_(
        agent_id="probe", name="probe", config=config or {}, workspace="proj",
        db_path=str(home / "runtime" / "relaydeck.db"), stop_flag=threading.Event(),
    )


def _val_after(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


# ── Claude Code ───────────────────────────────────────────────────

_CLAUDE = ("claude-code", "plugins.harnesses.claude_code.agent", "ClaudeCodeAgent")


def test_claude_auto_default(tmp_path, monkeypatch):
    cmd = _make_agent(tmp_path, monkeypatch, *_CLAUDE)._build_command()
    assert _val_after(cmd, "--permission-mode") == "auto"
    allow = _val_after(cmd, "--allowedTools")
    assert allow and "Bash(relaydeck *)" in allow


def test_claude_bypass(tmp_path, monkeypatch):
    cmd = _make_agent(tmp_path, monkeypatch, *_CLAUDE, config={"autonomy": "bypass"})._build_command()
    assert _val_after(cmd, "--permission-mode") == "bypassPermissions"


def test_claude_locked(tmp_path, monkeypatch):
    cmd = _make_agent(tmp_path, monkeypatch, *_CLAUDE, config={"autonomy": "locked"})._build_command()
    assert _val_after(cmd, "--permission-mode") == "dontAsk"
    # The relaydeck CLI is still allowlisted so messaging survives the fail-safe deny.
    assert "Bash(relaydeck *)" in (_val_after(cmd, "--allowedTools") or "")


def test_claude_manual_injects_nothing(tmp_path, monkeypatch):
    cmd = _make_agent(tmp_path, monkeypatch, *_CLAUDE, config={"autonomy": "manual"})._build_command()
    assert "--permission-mode" not in cmd
    assert "--allowedTools" not in cmd


def test_claude_operator_pinned_mode_wins(tmp_path, monkeypatch):
    cmd = _make_agent(
        tmp_path, monkeypatch, *_CLAUDE,
        config={"args": ["--permission-mode", "plan"]},
    )._build_command()
    assert cmd.count("--permission-mode") == 1
    assert _val_after(cmd, "--permission-mode") == "plan"


# ── Codex ─────────────────────────────────────────────────────────

_CODEX = ("codex-cli", "plugins.harnesses.codex.agent", "CodexAgent")


def test_codex_auto_default_is_sandboxed_no_prompt(tmp_path, monkeypatch):
    cmd = _make_agent(tmp_path, monkeypatch, *_CODEX)._build_command()
    assert _val_after(cmd, "--sandbox") == "workspace-write"
    assert _val_after(cmd, "--ask-for-approval") == "never"
    # ~/.relaydeck must stay writable so `relaydeck reply` can persist.
    assert any("sandbox_workspace_write.writable_roots" in a and ".relaydeck" in a for a in cmd)
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


def test_codex_bypass(tmp_path, monkeypatch):
    cmd = _make_agent(tmp_path, monkeypatch, *_CODEX, config={"autonomy": "bypass"})._build_command()
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--sandbox" not in cmd


def test_codex_operator_pinned_sandbox_wins(tmp_path, monkeypatch):
    cmd = _make_agent(
        tmp_path, monkeypatch, *_CODEX, config={"sandbox": "read-only"},
    )._build_command()
    assert _val_after(cmd, "--sandbox") == "read-only"
    # We didn't auto-add approval / writable-roots over the operator's choice.
    assert "--ask-for-approval" not in cmd
    assert not any("writable_roots" in a for a in cmd)


def test_codex_manual_injects_nothing(tmp_path, monkeypatch):
    cmd = _make_agent(tmp_path, monkeypatch, *_CODEX, config={"autonomy": "manual"})._build_command()
    assert "--sandbox" not in cmd
    assert "--ask-for-approval" not in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


# ── OpenCode ──────────────────────────────────────────────────────

_OPENCODE = ("opencode-cli", "plugins.harnesses.opencode.agent", "OpenCodeAgent")


def _opencode_config(agent) -> dict:
    path = agent._write_opencode_config()
    assert path is not None, "opencode config should be written"
    return json.loads(Path(path).read_text())


def test_opencode_auto_allows_relaydeck(tmp_path, monkeypatch):
    data = _opencode_config(_make_agent(tmp_path, monkeypatch, *_OPENCODE))
    perm = data["permission"]
    assert perm["bash"]["relaydeck *"] == "allow"
    assert perm["edit"] == "allow"


def test_opencode_locked_denies_rest(tmp_path, monkeypatch):
    data = _opencode_config(
        _make_agent(tmp_path, monkeypatch, *_OPENCODE, config={"autonomy": "locked"})
    )
    perm = data["permission"]
    assert perm["bash"]["relaydeck *"] == "allow"
    assert perm["bash"]["*"] == "deny"
    assert perm["edit"] == "deny"


def test_opencode_manual_injects_no_permission(tmp_path, monkeypatch):
    data = _opencode_config(
        _make_agent(tmp_path, monkeypatch, *_OPENCODE, config={"autonomy": "manual"})
    )
    assert "permission" not in data


def test_opencode_operator_permission_wins(tmp_path, monkeypatch):
    custom = {"bash": "ask"}
    data = _opencode_config(
        _make_agent(
            tmp_path, monkeypatch, *_OPENCODE,
            config={"opencode_config": {"permission": custom}},
        )
    )
    assert data["permission"] == custom


# ── Cursor (config-file approval posture) ─────────────────────────

_CURSOR = ("cursor-cli", "plugins.harnesses.cursor.agent", "CursorAgent")


def _cursor_config(agent) -> dict:
    path = agent._write_cursor_config()
    assert path is not None, "cursor config should be written"
    return json.loads(Path(path).read_text())


def test_cursor_auto_allows_relaydeck_unsandboxed(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, *_CURSOR)
    data = _cursor_config(agent)
    assert data["approvalMode"] == "unrestricted"
    # Un-sandboxed so `relaydeck reply` can persist to ~/.relaydeck (Cursor has
    # no trustable filesystem writable-root escape).
    assert data["sandbox"]["mode"] == "disabled"
    assert "Shell(relaydeck)" in data["permissions"]["allow"]
    cmd = agent._build_command()
    assert _val_after(cmd, "--sandbox") == "disabled"
    assert "--force" not in cmd


def test_cursor_bypass_forces(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, *_CURSOR, config={"autonomy": "bypass"})
    assert _cursor_config(agent)["approvalMode"] == "unrestricted"
    assert "--force" in agent._build_command()


def test_cursor_locked_allowlists_relaydeck(tmp_path, monkeypatch):
    data = _cursor_config(
        _make_agent(tmp_path, monkeypatch, *_CURSOR, config={"autonomy": "locked"})
    )
    assert data["approvalMode"] == "allowlist"
    # The relaydeck CLI is still allowlisted so messaging survives the fail-safe.
    assert "Shell(relaydeck)" in data["permissions"]["allow"]


def test_cursor_manual_injects_nothing(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, *_CURSOR, config={"autonomy": "manual"})
    data = _cursor_config(agent)
    assert "approvalMode" not in data
    assert "permissions" not in data
    cmd = agent._build_command()
    assert "--sandbox" not in cmd
    assert "--force" not in cmd


def test_cursor_operator_pinned_sandbox_wins(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, *_CURSOR, config={"sandbox": "enabled"})
    data = _cursor_config(agent)
    assert data["sandbox"]["mode"] == "enabled"
    # relaydeck CLI stays allowed even when the operator pins the sandbox.
    assert "Shell(relaydeck)" in data["permissions"]["allow"]
    assert _val_after(agent._build_command(), "--sandbox") == "enabled"


# ── Pi (reference: nothing injected) ──────────────────────────────

_PI = ("pi", "plugins.harnesses.pi.agent", "PiAgent")


def test_pi_injects_no_approval_flags(tmp_path, monkeypatch):
    """Pi has no per-command approval prompts, so relaydeck injects nothing —
    pi runs the relaydeck CLI via its built-in bash tool out of the box."""
    cmd = _make_agent(tmp_path, monkeypatch, *_PI)._build_command()
    for flag in ("--permission-mode", "--allowedTools", "--sandbox",
                 "--ask-for-approval", "--dangerously-bypass-approvals-and-sandbox"):
        assert flag not in cmd, f"pi should not get {flag}"
