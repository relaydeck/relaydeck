"""
`relaydeck status` and `relaydeck workspace info` — the agent's-eye-view and
per-workspace detail commands respectively.

Both flip between two display modes based on agent context, so the
tests pin both branches.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.db import open_db
from relaydeck.transports.cli import main as cli


def _seed(tmp_path, monkeypatch, workspaces=None, agents=None,
          active_ws=None, messages=None):
    """Stand up a fresh ~/.relaydeck under tmp_path with a workspace
    registry, an agents table, and (optionally) messages — enough for
    a CLI invocation to render its full output."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)

    if workspaces:
        body = ""
        for w in workspaces:
            ws_path = tmp_path / w["name"]
            ws_path.mkdir(parents=True, exist_ok=True)
            plugins = w.get("plugins", [])
            plugins_str = ", ".join(f'"{p}"' for p in plugins)
            body += (
                f'[[workspace]]\nname = "{w["name"]}"\n'
                f'path = "{ws_path}"\n'
                f'plugins = [{plugins_str}]\n\n'
            )
            ws_state = cfg_home / "workspaces" / w["name"]
            ws_state.mkdir(parents=True, exist_ok=True)
            (ws_state / "agent.toml").write_text(
                f'[workspace]\nplugins = [{plugins_str}]\n'
            )
        (cfg_home / "config.toml").write_text(body)

    db = str(cfg_home / "runtime" / "relaydeck.db")
    conn = open_db(db)
    try:
        for a in (agents or []):
            conn.execute(
                "INSERT INTO agents (id, type, name, status, workspace, "
                "auto_start, created_at) VALUES (?, ?, ?, ?, ?, 0, 0)",
                (a["id"], a.get("type", "pi"), a.get("name", a["id"]),
                 a.get("status", "stopped"), a.get("workspace")),
            )
        for m in (messages or []):
            conn.execute(
                "INSERT INTO agent_messages (id, from_id, to_id, body, "
                "ts, workspace) VALUES (?, ?, ?, ?, ?, ?)",
                (m["id"], m["from"], m["to"], m["body"], 0,
                 m.get("workspace", "")),
            )
        conn.commit()
    finally:
        conn.close()

    if active_ws:
        from relaydeck.state import set_current_workspace
        set_current_workspace(active_ws)

    # Force a fresh orchestrator with the tmp config home — `relaydeck`'s
    # commands instantiate one via get_orchestrator() each call.
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None


# ── relaydeck status ─────────────────────────────────────────────────────


def test_status_user_view_no_agent_context(tmp_path, monkeypatch):
    """Without RELAYDECK_AGENT_ID, status shows the user-mode view:
    daemon line + active workspace + agents in active + plugin counts."""
    _seed(tmp_path, monkeypatch,
          workspaces=[{"name": "demo", "plugins": ["messaging"]}],
          agents=[{"id": "alice", "workspace": "demo", "status": "running"}],
          active_ws="demo")
    monkeypatch.delenv("RELAYDECK_AGENT_ID", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "demo" in result.output
    # Active workspace marker
    assert "active workspace" in result.output
    # Sees the running agent
    assert "alice" in result.output


def test_status_agent_view_uses_RELAYDECK_AGENT_ID(tmp_path, monkeypatch):
    """With RELAYDECK_AGENT_ID set, status flips to agent-mode: identity
    header + inbox count + peers. The user-mode "active workspace"
    line should NOT appear (different output shape)."""
    _seed(tmp_path, monkeypatch,
          workspaces=[{"name": "demo", "plugins": []}],
          agents=[
              {"id": "alice", "workspace": "demo", "status": "running"},
              {"id": "bob", "workspace": "demo", "status": "stopped"},
          ])
    monkeypatch.setenv("RELAYDECK_AGENT_ID", "alice")

    runner = CliRunner()
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    assert "demo" in result.output  # workspace shown in header
    # Peer is visible
    assert "bob" in result.output
    # Inbox line present
    assert "inbox" in result.output.lower()


def test_status_agent_view_shows_unread_inbox(tmp_path, monkeypatch):
    """Unread messages should surface with the sender list."""
    _seed(tmp_path, monkeypatch,
          workspaces=[{"name": "demo", "plugins": []}],
          agents=[
              {"id": "alice", "workspace": "demo"},
              {"id": "bob", "workspace": "demo"},
          ],
          messages=[
              {"id": "msg_a", "from": "bob", "to": "alice",
               "body": "hi alice", "workspace": "demo"},
          ])
    monkeypatch.setenv("RELAYDECK_AGENT_ID", "alice")

    runner = CliRunner()
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "1 unread" in result.output
    assert "bob" in result.output  # sender listed


def test_status_agent_404_when_RELAYDECK_AGENT_ID_stale(tmp_path, monkeypatch):
    """Stale env var (agent was deleted) should fail loudly, not show
    a confusing partial view."""
    _seed(tmp_path, monkeypatch,
          workspaces=[{"name": "demo", "plugins": []}])
    monkeypatch.setenv("RELAYDECK_AGENT_ID", "ghost")

    runner = CliRunner()
    result = runner.invoke(cli, ["status"])
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_status_explicit_agent_flag_overrides_env(tmp_path, monkeypatch):
    """--agent flag wins over RELAYDECK_AGENT_ID env so an operator can
    inspect any agent's view from outside the harness."""
    _seed(tmp_path, monkeypatch,
          workspaces=[{"name": "demo", "plugins": []}],
          agents=[
              {"id": "alice", "workspace": "demo"},
              {"id": "bob", "workspace": "demo"},
          ])
    monkeypatch.setenv("RELAYDECK_AGENT_ID", "alice")

    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--agent", "bob"])
    assert result.exit_code == 0, result.output
    # Header should be for bob, not alice
    lines = result.output.splitlines()
    header_line = next((line for line in lines if "in workspace" in line), "")
    assert "bob" in header_line


# ── relaydeck workspace info ─────────────────────────────────────────────


def test_workspace_info_defaults_to_active(tmp_path, monkeypatch):
    """Without an argument, falls back to the active workspace."""
    _seed(tmp_path, monkeypatch,
          workspaces=[{"name": "demo", "plugins": ["messaging"]}],
          agents=[{"id": "alice", "workspace": "demo", "status": "running"}],
          active_ws="demo")
    runner = CliRunner()
    result = runner.invoke(cli, ["workspace", "info"])
    assert result.exit_code == 0, result.output
    assert "demo" in result.output
    assert "messaging" in result.output
    assert "alice" in result.output
    # Active marker
    assert "active" in result.output.lower()


def test_workspace_info_explicit_name(tmp_path, monkeypatch):
    """With NAME, shows that workspace specifically."""
    _seed(tmp_path, monkeypatch,
          workspaces=[
              {"name": "demo", "plugins": []},
              {"name": "other", "plugins": ["recipes"]},
          ],
          agents=[{"id": "alice", "workspace": "other", "status": "stopped"}],
          active_ws="demo")
    runner = CliRunner()
    result = runner.invoke(cli, ["workspace", "info", "other"])
    assert result.exit_code == 0, result.output
    assert "other" in result.output
    assert "recipes" in result.output
    assert "alice" in result.output


def test_workspace_info_unknown_name_errors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch,
          workspaces=[{"name": "demo", "plugins": []}])
    runner = CliRunner()
    result = runner.invoke(cli, ["workspace", "info", "ghost"])
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_workspace_plugins_cli_patches_daemon_when_reachable(tmp_path, monkeypatch):
    """Regression for P3: `relaydeck workspace plugins` must PATCH the
    running daemon (so workspace.updated fires and messaging
    re-materializes the skill, etc.) instead of writing config.toml
    directly. Mocks urlopen to capture the request the CLI makes —
    verifies method+url+body. The fallback path is covered by
    test_workspace_plugins_cli_falls_back_when_daemon_unreachable."""
    from unittest.mock import patch

    _seed(tmp_path, monkeypatch, workspaces=[{"name": "demo", "plugins": []}])
    monkeypatch.setattr("relaydeck.state.get_daemon_url",
                        lambda: "http://127.0.0.1:8765")

    captured: dict = {}

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, _n=0): return b""

    def _fake_urlopen(req, timeout=5, context=None):
        del context  # TLS context — irrelevant for mocked HTTP
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp()

    runner = CliRunner()
    with patch("urllib.request.urlopen", _fake_urlopen):
        result = runner.invoke(cli, [
            "workspace", "plugins", "demo", "--add", "messaging",
        ])
    assert result.exit_code == 0, result.output
    assert captured.get("method") == "PATCH"
    assert captured["url"].endswith("/api/workspaces/demo")
    assert captured["body"]["plugins"] == ["messaging"]


def test_workspace_plugins_cli_falls_back_when_daemon_unreachable(tmp_path, monkeypatch):
    """When the daemon socket is closed, the CLI falls back to direct
    config.toml + agent.toml writes AND warns the user that listeners
    won't react until restart. Without this fallback, mutations would
    silently fail on a stopped daemon."""
    _seed(tmp_path, monkeypatch, workspaces=[{"name": "demo", "plugins": []}])
    # Point daemon URL at a port nothing is listening on.
    monkeypatch.setattr("relaydeck.state.get_daemon_url",
                        lambda: "http://127.0.0.1:1")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "workspace", "plugins", "demo", "--add", "messaging",
    ])
    assert result.exit_code == 0, result.output
    # User is warned that side effects won't fire
    assert "daemon unreachable" in result.output.lower()
    assert "until the daemon restarts" in result.output.lower() \
        or "subscribers" in result.output.lower()

    # And the files DID get written so the next daemon start picks them up.
    agent_toml = tmp_path / ".relaydeck" / "workspaces" / "demo" / "agent.toml"
    assert "messaging" in agent_toml.read_text()


def test_workspace_info_no_active_workspace_errors(tmp_path, monkeypatch):
    """If no name given and no active workspace, the command should
    refuse rather than guess."""
    _seed(tmp_path, monkeypatch, workspaces=[])  # empty registry
    monkeypatch.delenv("RELAYDECK_WORKSPACE", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["workspace", "info"])
    assert result.exit_code != 0
    assert "No active workspace" in result.output or "no active" in result.output.lower()
