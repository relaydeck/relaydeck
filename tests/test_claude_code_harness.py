"""
Claude Code harness — native skills via `--plugin-dir`.

Claude Code 2.x has a native Agent Skills system, so the harness materializes
every workspace `runtime/skills/<dir>/SKILL.md` (+ gated user skills) into a
session-scoped Claude Code plugin dir and passes `--plugin-dir`. The model
then loads them natively (shows in `/skills`, namespaced `/relaydeck:<name>`,
progressive disclosure) instead of every body being inlined into
`--append-system-prompt`.

This file pins:

  - skills are materialized into a `--plugin-dir` plugin tree
    (`.claude-plugin/plugin.json` + `skills/<name>/SKILL.md`)
  - the identity preamble + spec.system_prompt still go via
    `--append-system-prompt` (skills do NOT)
  - workspaces with no skills emit no `--plugin-dir`
  - invalid / empty SKILL.md files are not materialized
  - RELAYDECK_CONFIG_HOME is honored (tests don't need to monkeypatch Path.home)
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from plugins.harnesses.claude_code.agent import ClaudeCodeAgent


@pytest.fixture(autouse=True)
def _isolate_config_home_env():
    """`_agent` sets RELAYDECK_CONFIG_HOME directly on os.environ; pop it after
    each test so it never leaks into sibling modules that monkeypatch
    Path.home() instead (claude resolves its config home env-first)."""
    yield
    os.environ.pop("RELAYDECK_CONFIG_HOME", None)


def _agent(
    tmp_path: Path,
    *,
    skills: dict[str, str] | None = None,
    raw_skills: dict[str, str] | None = None,
) -> ClaudeCodeAgent:
    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    # Pretend pi-style spec for the agent so _compose_system_prompt
    # finds the YAML and inject_identity_preamble works.
    (cfg_home / "agents").mkdir(parents=True)
    (cfg_home / "agents" / "claudie.yaml").write_text(
        "id: claudie\n"
        "name: claudie\n"
        "type: claude-code\n"
        "workspace: demo\n"
        "purpose: Test agent for skill inlining\n"
        "tags: []\n"
        "system_prompt: ''\n"
        "inject_identity_preamble: true\n"
        "auto_start: false\n"
        "config: {}\n",
    )
    runtime_skills = cfg_home / "workspaces" / "demo" / "runtime" / "skills"
    runtime_skills.mkdir(parents=True)
    # A real skill carries `name`/`description` frontmatter — wrap bare
    # test bodies so they validate (and therefore inject). `raw_skills`
    # is written verbatim so tests can exercise invalid SKILL.md files.
    for name, body in (skills or {}).items():
        (runtime_skills / name).mkdir(parents=True)
        content = body
        if body and not body.lstrip().startswith("---"):
            content = f"---\nname: {name}\ndescription: test skill {name}\n---\n\n{body}"
        (runtime_skills / name / "SKILL.md").write_text(content)
    for name, body in (raw_skills or {}).items():
        (runtime_skills / name).mkdir(parents=True)
        (runtime_skills / name / "SKILL.md").write_text(body)

    os.environ["RELAYDECK_CONFIG_HOME"] = str(cfg_home)
    return ClaudeCodeAgent(
        agent_id="claudie", name="claudie", config={},
        workspace="demo",
        db_path=str(tmp_path / "relaydeck.db"),
        stop_flag=threading.Event(),
    )


def _append_value(cmd: list[str]) -> str | None:
    """Return the text passed to `--append-system-prompt`."""
    for i, part in enumerate(cmd):
        if part == "--append-system-prompt" and i + 1 < len(cmd):
            return cmd[i + 1]
    return None


def _plugin_dir(cmd: list[str]) -> Path | None:
    """Return the path passed to `--plugin-dir`."""
    for i, part in enumerate(cmd):
        if part == "--plugin-dir" and i + 1 < len(cmd):
            return Path(cmd[i + 1])
    return None


def test_claude_code_materializes_plugin_skills(tmp_path):
    import json
    agent = _agent(tmp_path, skills={
        "relaydeck-cli": "## Messaging\nUse `relaydeck workspace message …`",
        "relaydeck-telegram": "## Telegram\nReply via `relaydeck telegram reply <chat_id> …`",
    })
    cmd = agent._build_command()
    pdir = _plugin_dir(cmd)
    assert pdir is not None, "claude-code must pass --plugin-dir when skills exist"
    # Native plugin manifest.
    manifest = pdir / ".claude-plugin" / "plugin.json"
    assert manifest.exists()
    assert json.loads(manifest.read_text())["name"] == "relaydeck"
    # Each skill materialized as skills/<name>/SKILL.md with its body.
    for name, marker in [("relaydeck-cli", "relaydeck workspace message"),
                         ("relaydeck-telegram", "relaydeck telegram reply")]:
        sm = pdir / "skills" / name / "SKILL.md"
        assert sm.exists(), f"{name} not materialized into the plugin dir"
        assert marker in sm.read_text()
    # Skills are NOT dumped into the system prompt anymore.
    appended = _append_value(cmd) or ""
    assert "relaydeck workspace message" not in appended


def test_claude_code_no_skills_no_plugin_dir(tmp_path):
    agent = _agent(tmp_path, skills={})
    cmd = agent._build_command()
    appended = _append_value(cmd) or ""
    # Identity preamble still lands; no --plugin-dir without skills.
    assert "You are agent `claudie`" in appended
    assert _plugin_dir(cmd) is None


def test_claude_code_identity_in_prompt_skills_in_plugin_dir(tmp_path):
    """Identity preamble + system_prompt go via --append-system-prompt; skills
    go via the native --plugin-dir. Separate channels."""
    agent = _agent(tmp_path, skills={"a": "skill body A"})
    cmd = agent._build_command()
    appended = _append_value(cmd) or ""
    assert "You are agent `claudie`" in appended
    assert _plugin_dir(cmd) is not None


def test_claude_code_empty_skill_md_skipped(tmp_path):
    agent = _agent(
        tmp_path,
        skills={"real": "real body"},
        raw_skills={"empty": ""},
    )
    pdir = _plugin_dir(agent._build_command())
    assert pdir is not None
    assert (pdir / "skills" / "real" / "SKILL.md").exists()
    # Empty SKILL.md is invalid → not materialized.
    assert not (pdir / "skills" / "empty").exists()


def test_claude_code_invalid_frontmatter_skipped(tmp_path):
    """A SKILL.md with a body but no valid frontmatter (missing
    name/description) is invalid and must NOT be materialized — same rule
    pi/codex apply via relaydeck.skills, so the Skills lens and the model
    agree on what's injectable."""
    agent = _agent(
        tmp_path,
        skills={"good": "good body"},
        raw_skills={"bad": "no frontmatter here, just prose"},
    )
    pdir = _plugin_dir(agent._build_command())
    assert pdir is not None
    assert (pdir / "skills" / "good" / "SKILL.md").exists()
    assert not (pdir / "skills" / "bad").exists()


def test_claude_code_send_message_splits_body_and_enter(tmp_path, monkeypatch):
    """Claude Code's Ink TUI treats `body + \\r` arriving in one PTY
    read as a paste and swallows the Enter. We must split the write
    so the trailing CR arrives in a separate read() and registers
    as a submit keystroke. Pin both halves of the write contract."""
    agent = _agent(tmp_path, skills={})
    writes: list[bytes] = []
    monkeypatch.setattr(agent, "send_input", lambda data: (writes.append(data), True)[1])
    monkeypatch.setattr(agent, "SUBMIT_DELAY_S", 0)  # don't slow the test

    ok = agent.send_message("[relay from=pi id=msg_abc] Hello world")
    assert ok is True
    assert len(writes) == 2, (
        f"expected two PTY writes (body, then CR); got {len(writes)}: {writes!r}"
    )
    assert writes[0] == b"[relay from=pi id=msg_abc] Hello world"
    assert writes[1] == b"\r"


# ── External-model routing (OpenRouter / deepseek-anthropic / proxy) ──────

def _routed_agent(tmp_path: Path, config: dict) -> ClaudeCodeAgent:
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "agents").mkdir(parents=True, exist_ok=True)
    (cfg_home / "agents" / "router.yaml").write_text(
        "id: router\nname: router\ntype: claude-code\nworkspace: demo\nconfig: {}\n"
    )
    (cfg_home / "workspaces" / "demo").mkdir(parents=True, exist_ok=True)
    os.environ["RELAYDECK_CONFIG_HOME"] = str(cfg_home)
    return ClaudeCodeAgent(
        agent_id="router", name="router", config=config, workspace="demo",
        db_path=str(tmp_path / "relaydeck.db"), stop_flag=threading.Event(),
    )


def test_claude_no_routing_keeps_anthropic_default(tmp_path):
    env = _routed_agent(tmp_path, {})._build_env()
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_claude_external_routing_injects_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "relaydeck.vault.get_secret",
        lambda k: "sk-or-xyz" if k == "OPENROUTER_API_KEY" else None,
    )
    env = _routed_agent(tmp_path, {
        "claude_base_url": "https://openrouter.ai/api/v1",
        "claude_key_env": "OPENROUTER_API_KEY",
        "claude_model": "deepseek/deepseek-chat",
    })._build_env()
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or-xyz"   # Bearer (proxies/OpenRouter)
    # ANTHROPIC_API_KEY must NOT be set: both-set trips claude's auth-conflict
    # warning AND the interactive "custom API key — use it? [No]" gate that an
    # unattended agent can't answer (verified live, 2026-05-28).
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANTHROPIC_MODEL"] == "deepseek/deepseek-chat"


def test_claude_infers_key_env_from_host(tmp_path, monkeypatch):
    # Blank claude_key_env → infer DEEPSEEK_API_KEY from api.deepseek.com.
    monkeypatch.setattr(
        "relaydeck.vault.get_secret",
        lambda k: "ds-key" if k == "DEEPSEEK_API_KEY" else None,
    )
    env = _routed_agent(tmp_path, {
        "claude_base_url": "https://api.deepseek.com/anthropic",
        "claude_model": "deepseek-chat",
    })._build_env()
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ds-key"
    assert "ANTHROPIC_API_KEY" not in env


def test_claude_routing_drops_inherited_anthropic_api_key(tmp_path, monkeypatch):
    # The daemon env may carry the operator's own ANTHROPIC_API_KEY (for
    # non-routed agents). When routing externally we must drop it: otherwise
    # claude sees both our Bearer token AND the inherited key → auth-conflict
    # warning + the unattended-hostile custom-key approval gate.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setattr(
        "relaydeck.vault.get_secret",
        lambda k: "ds-key" if k == "DEEPSEEK_API_KEY" else None,
    )
    env = _routed_agent(tmp_path, {
        "claude_base_url": "https://api.deepseek.com/anthropic",
        "claude_model": "deepseek-chat",
    })._build_env()
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ds-key"
    assert "ANTHROPIC_API_KEY" not in env


def test_claude_routing_proxy_without_token_still_sets_base(tmp_path, monkeypatch):
    # Proxy method: "any string works" — a missing token is non-fatal; the
    # local proxy injects the real key. Base URL must still be set.
    monkeypatch.setattr("relaydeck.vault.get_secret", lambda k: None)
    env = _routed_agent(tmp_path, {"claude_base_url": "http://localhost:3010"})._build_env()
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:3010"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_claude_external_model_passes_validation_and_flag(tmp_path):
    # The external id rides claude_model (override key) → validation passes
    # the native-provider guard, and --model gets the raw provider/model.
    from relaydeck.harness_model import cli_model_for_agent, validate_harness_model
    cfg = {
        "claude_model": "deepseek/deepseek-chat",
        "claude_base_url": "https://openrouter.ai/api/v1",
    }
    ok, err = validate_harness_model("claude-code", cfg)
    assert ok is True and err is None
    assert cli_model_for_agent("claude-code", cfg) == "deepseek/deepseek-chat"


def test_claude_tailer_emits_harness_assistant_message(tmp_path, monkeypatch):
    """A claude transcript assistant row should fan out to
    `harness.assistant_message` on the plugin bus (same signal pi/codex
    emit), in addition to usage.record — so classifier/HITL subscribers
    work across harnesses, not just pi/codex."""
    agent = _agent(tmp_path)

    bus_events: list[tuple[str, dict]] = []

    class _FakeBus:
        def emit(self, event):
            bus_events.append((event.type, event.data))

    class _FakeOrch:
        _plugin_event_bus = _FakeBus()

    import relaydeck.orchestrator as orch
    monkeypatch.setattr(orch, "get_orchestrator", lambda: _FakeOrch())
    agent.emit = lambda *_a, **_kw: 1  # type: ignore[assignment]

    line = json.dumps({
        "type": "assistant",
        "sessionId": "sess-1",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4",
            "content": [
                {"type": "thinking", "thinking": "hidden reasoning"},
                {"type": "text", "text": "Done — tests pass."},
            ],
            "usage": {"input_tokens": 80, "output_tokens": 12},
        },
    }).encode()

    agent._handle_transcript_line(line)

    msgs = [d for t, d in bus_events if t == "harness.assistant_message"]
    assert msgs, f"expected harness.assistant_message, got {bus_events}"
    assert msgs[0]["harness"] == "claude-code"
    assert msgs[0]["text"] == "Done — tests pass."  # thinking block skipped
    assert msgs[0]["agent_id"] == "claudie"
    assert msgs[0]["session_id"] == "sess-1"
