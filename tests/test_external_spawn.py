"""Phase 3: spawn a linked external runtime's chat TUI as a relaydeck agent.

Two layers:
  * `harness.py` — the `ExternalChatAgent` command builder (pure; no PTY).
  * `plugin._spawn_agent` — create+start a harness agent in a workspace,
    exercised against `MockHost` (in-memory orchestrator), so no real
    process and no real Hermes/OpenClaw needed on CI.
"""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.external_agents import store
from plugins.external_agents.harness import (
    HARNESS_TYPES,
    KIND_TO_TYPE,
    HermesChatAgent,
    OpenClawChatAgent,
)
from plugins.external_agents.models import ExternalAgent
from relaydeck.testing import MockHost

plugin_mod = importlib.import_module("plugins.external_agents.plugin")


def _agent(cls, **config):
    return cls(
        agent_id="x", name="x", config=config, workspace="proj",
        db_path=":memory:", stop_flag=threading.Event(),
    )


# ── harness command building ────────────────────────────────────────


def test_hermes_command_is_interactive_chat():
    assert _agent(HermesChatAgent)._build_command() == ["hermes", "chat"]


def test_openclaw_command_is_bare_tui():
    assert _agent(OpenClawChatAgent)._build_command() == ["openclaw"]


def test_config_args_are_appended():
    cmd = _agent(HermesChatAgent, args=["-m", "anthropic/claude"])._build_command()
    assert cmd == ["hermes", "chat", "-m", "anthropic/claude"]
    # string form too
    assert _agent(HermesChatAgent, args="--worktree")._build_command() == [
        "hermes", "chat", "--worktree"]


def test_config_command_is_full_override():
    assert _agent(HermesChatAgent, command="hermes chat --yolo")._build_command() == [
        "hermes", "chat", "--yolo"]
    assert _agent(HermesChatAgent, command=["hermes", "--continue"])._build_command() == [
        "hermes", "--continue"]


def test_no_model_or_system_prompt_injection():
    """External runtimes own their model + prompting — relaydeck must NOT
    inject --model / --append-system-prompt (would break the CLI)."""
    cmd = _agent(HermesChatAgent, preset="some-preset", model="anthropic/claude",
                 system_prompt="hi")._build_command()
    assert "--model" not in cmd
    assert "--append-system-prompt" not in cmd
    assert cmd == ["hermes", "chat"]


def test_kind_to_type_and_registry():
    assert KIND_TO_TYPE == {"hermes": "hermes", "openclaw": "openclaw"}
    assert HARNESS_TYPES["hermes"] is HermesChatAgent
    assert HARNESS_TYPES["openclaw"] is OpenClawChatAgent


# ── spawn into a workspace (via MockHost) ───────────────────────────


def _plugin_with_ext(kind="hermes", ext_id=None, workspace="proj"):
    host = MockHost(name="external")
    p = plugin_mod.ExternalAgentsPlugin()
    p.host = host
    p.config_home = host.config_home
    ext_id = ext_id or kind
    store.save_agent(
        host.config_home,
        ExternalAgent(id=ext_id, kind=kind, name=ext_id, root="/tmp/runtime"),
    )
    # Spawning requires a real workspace — register one so the happy-path
    # tests exercise the actual (non-lenient) validation.
    if workspace:
        from relaydeck.config import register_workspace
        wsdir = host.config_home / f"ws-{workspace}"
        wsdir.mkdir(parents=True, exist_ok=True)
        register_workspace(host.config_home, workspace, wsdir)
    return p, host


def test_spawn_creates_and_starts_harness_agent():
    p, host = _plugin_with_ext("hermes")
    out = p._spawn_agent("hermes", "proj")
    assert out == {
        "ok": True, "agent_id": "hermes", "type": "hermes",
        "workspace": "proj", "kind": "hermes", "started": True,
    }
    row = host.memory_orchestrator.get_agent("hermes")
    assert row["type"] == "hermes"
    assert row["workspace"] == "proj"
    assert row["status"] == "running"  # started
    assert "external" in row["tags"] and "hermes" in row["tags"]


def test_spawn_no_start_creates_but_does_not_run():
    p, host = _plugin_with_ext("hermes")
    out = p._spawn_agent("hermes", "proj", start=False)
    assert out["started"] is False
    assert host.memory_orchestrator.get_agent("hermes")["status"] == "stopped"


def test_spawn_openclaw_maps_to_openclaw_type():
    p, host = _plugin_with_ext("openclaw")
    out = p._spawn_agent("openclaw", "proj")
    assert out["type"] == "openclaw"
    assert host.memory_orchestrator.get_agent("openclaw")["type"] == "openclaw"


def test_spawn_dedupes_agent_id():
    p, host = _plugin_with_ext("hermes")
    host.add_agent(id="hermes", type="pi", status="running")  # id taken
    out = p._spawn_agent("hermes", "proj")
    assert out["agent_id"] == "hermes-proj"  # falls back to <kind>-<ws>


def test_spawn_custom_agent_id():
    p, host = _plugin_with_ext("hermes")
    out = p._spawn_agent("hermes", "proj", agent_id="my-hermes")
    assert out["agent_id"] == "my-hermes"


def test_spawn_unknown_external_raises():
    p, _ = _plugin_with_ext("hermes")
    with pytest.raises(ValueError, match="no external agent"):
        p._spawn_agent("ghost", "proj")


def test_spawn_requires_workspace():
    p, _ = _plugin_with_ext("hermes")
    with pytest.raises(ValueError, match="workspace is required"):
        p._spawn_agent("hermes", "")


def test_spawn_rejects_unknown_workspace():
    # Even with a registry present, an unregistered name is rejected — and on
    # an EMPTY registry too (no lenient pass): use a plugin with no workspace.
    p, host = _plugin_with_ext("hermes", workspace=None)
    with pytest.raises(ValueError, match="unknown workspace"):
        p._spawn_agent("hermes", "proj")  # empty registry → still rejected
    from relaydeck.config import register_workspace
    wsdir = host.config_home / "ws-real"
    wsdir.mkdir(parents=True, exist_ok=True)
    register_workspace(host.config_home, "real", wsdir)
    with pytest.raises(ValueError, match="unknown workspace"):
        p._spawn_agent("hermes", "nope")
    assert p._spawn_agent("hermes", "real")["workspace"] == "real"


def test_spawn_runtime_unavailable_returns_clean_error():
    """If the host has no orchestrator-backed agent registry (host.agents is
    None — e.g. daemon not fully booted), spawn raises a clean RuntimeError
    (API → 503), not an AttributeError."""
    p, host = _plugin_with_ext("hermes")
    host.agents = None
    with pytest.raises(RuntimeError, match="runtime unavailable"):
        p._spawn_agent("hermes", "proj")
