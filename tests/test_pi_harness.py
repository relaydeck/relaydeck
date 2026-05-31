"""
Pi harness adapter tests — specifically that the JSONL tailer fans one
`message` row out into the right semantic events on the plugin event
bus (usage.record + harness.assistant_message).

The PTY/command-construction surface is covered by tests/test_terminal.py.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.harnesses.pi.agent import PiAgent


def _make_agent(tmp_path: Path, monkeypatch, config: dict | None = None) -> PiAgent:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    ws_path = tmp_path / "repo"
    ws_path.mkdir(parents=True)
    cfg_home.mkdir(parents=True, exist_ok=True)
    (cfg_home / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{ws_path}"\n'
    )
    return PiAgent(
        agent_id="coder",
        name="coder",
        config=config or {},
        workspace="demo",
        db_path=str(tmp_path / "relaydeck.db"),
        stop_flag=threading.Event(),
    )


def test_pi_log_path_is_session_dir(tmp_path, monkeypatch):
    # Pi writes one JSONL per session; the session dir is the
    # debug surface (caller `ls`es to find the active one). Pinned
    # so harness.exit's log_path stays useful when a pi agent
    # crashes.
    agent = _make_agent(tmp_path, monkeypatch)
    assert agent.log_path() == agent._session_dir()


def test_pi_command_resolves_dashboard_preset_key(tmp_path, monkeypatch):
    cfg_home = tmp_path / ".relaydeck"
    presets = cfg_home / "presets"
    presets.mkdir(parents=True)
    (presets / "ollama-gemma.yaml").write_text(
        "name: ollama-gemma\nprovider: ollama\nmodel: gemma4:latest\n"
    )
    agent = _make_agent(tmp_path, monkeypatch, {"preset": "ollama-gemma"})

    cmd = agent._build_command()

    assert cmd[cmd.index("--model") + 1] == "ollama/gemma4:latest"


def test_pi_workspace_plugins_read_agent_toml_via_public_config_loader(
    tmp_path, monkeypatch
):
    agent = _make_agent(tmp_path, monkeypatch)
    agent_toml = tmp_path / ".relaydeck" / "workspaces" / "demo" / "agent.toml"
    agent_toml.parent.mkdir(parents=True, exist_ok=True)
    agent_toml.write_text('[workspace]\nplugins = ["skills", "messaging"]\n')

    assert agent._workspace_plugins() == ["skills", "messaging"]


def test_pi_tailer_emits_harness_assistant_message(tmp_path, monkeypatch):
    """A pi `message` row with assistant role + text content should fan
    out to `harness.assistant_message` on the plugin event bus, in
    addition to `usage.record` when a usage block is present."""
    agent = _make_agent(tmp_path, monkeypatch)
    sd = agent._session_dir()
    assert sd is not None
    sd.mkdir(parents=True)
    log = sd / "session.jsonl"

    bus_events: list[tuple[str, dict]] = []

    class _FakeBus:
        def emit(self, event):
            bus_events.append((event.type, event.data))

    class _FakeOrch:
        _plugin_event_bus = _FakeBus()

    import relaydeck.orchestrator as orch
    monkeypatch.setattr(orch, "get_orchestrator", lambda: _FakeOrch())
    agent.emit = lambda *_a, **_kw: 1  # type: ignore[assignment]

    log.write_text(json.dumps({
        "type": "message",
        "id": "abc",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Shipped — all green."}],
            "usage": {"input": 100, "output": 20,
                      "cost": {"total": 0.0012}},
            "model": "claude-sonnet-4",
            "provider": "anthropic",
        },
    }) + "\n")

    agent._scan_session_dir(sd)

    msgs = [d for t, d in bus_events if t == "harness.assistant_message"]
    usage = [d for t, d in bus_events if t == "usage.record"]
    assert msgs, f"expected harness.assistant_message, got {bus_events}"
    assert msgs[0]["agent_id"] == "coder"
    assert msgs[0]["harness"] == "pi"
    assert msgs[0]["text"] == "Shipped — all green."
    assert msgs[0]["session_id"] == "abc"
    assert usage and usage[0]["prompt"] == 100 and usage[0]["completion"] == 20


def test_pi_tailer_skips_non_text_blocks(tmp_path, monkeypatch):
    """Reasoning/tool_use blocks must not leak into the assistant text
    stream (otherwise emote would classify internal-monologue, not the
    user-visible turn)."""
    agent = _make_agent(tmp_path, monkeypatch)
    sd = agent._session_dir()
    assert sd is not None
    sd.mkdir(parents=True)
    log = sd / "session.jsonl"

    bus_events: list[tuple[str, dict]] = []

    class _FakeBus:
        def emit(self, event):
            bus_events.append((event.type, event.data))

    class _FakeOrch:
        _plugin_event_bus = _FakeBus()

    import relaydeck.orchestrator as orch
    monkeypatch.setattr(orch, "get_orchestrator", lambda: _FakeOrch())
    agent.emit = lambda *_a, **_kw: 1  # type: ignore[assignment]

    log.write_text(json.dumps({
        "type": "message",
        "id": "abc",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "text": "internal reasoning here"},
                {"type": "text", "text": "Visible answer."},
                {"type": "tool_use", "name": "edit_file"},
            ],
        },
    }) + "\n")

    agent._scan_session_dir(sd)

    msgs = [d for t, d in bus_events if t == "harness.assistant_message"]
    assert msgs and msgs[0]["text"] == "Visible answer."


def test_pi_tailer_silent_on_user_messages(tmp_path, monkeypatch):
    """User-role rows should not trigger any harness events — the bus
    would otherwise see twice the chatter (every user turn + every
    assistant turn)."""
    agent = _make_agent(tmp_path, monkeypatch)
    sd = agent._session_dir()
    assert sd is not None
    sd.mkdir(parents=True)
    log = sd / "session.jsonl"

    bus_events: list[tuple[str, dict]] = []

    class _FakeBus:
        def emit(self, event):
            bus_events.append((event.type, event.data))

    class _FakeOrch:
        _plugin_event_bus = _FakeBus()

    import relaydeck.orchestrator as orch
    monkeypatch.setattr(orch, "get_orchestrator", lambda: _FakeOrch())
    agent.emit = lambda *_a, **_kw: 1  # type: ignore[assignment]

    log.write_text(json.dumps({
        "type": "message",
        "id": "abc",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "fix the bug"}],
        },
    }) + "\n")

    agent._scan_session_dir(sd)
    assert bus_events == []
