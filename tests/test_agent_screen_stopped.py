"""
`relaydeck agent screen <id>` when the agent is stopped.

A stopped agent has no live PTY to render -- the daemon returns 409.
Instead of just printing "not running" and bailing, the CLI surfaces
the most recent harness.exit payload (rc + log_path) so operators get
the same answer they were after: "what was the last sign of life?"
"""

from __future__ import annotations

import sys
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.db import ensure_session, log_event, open_db
from relaydeck.transports.cli import main as cli


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None

    # Seed a harness.exit event with both rc and log_path.
    db_path = cfg_home / "runtime" / "relaydeck.db"
    conn = open_db(str(db_path))
    try:
        ensure_session(conn, "agent:alice")
        log_event(
            conn, "agent:alice", "harness.exit",
            {"returncode": 101, "log_path": "/tmp/x/codex-tui.log"},
            agent_id="alice",
        )
    finally:
        conn.close()


def test_screen_on_stopped_agent_prints_last_exit(tmp_path, monkeypatch):
    """Daemon returns 'not running' -> CLI exits 3 AND prints
    'last exit: rc=...' + 'log: <path>' so the operator can chase
    the crash without grepping events."""
    _seed(tmp_path, monkeypatch)

    # Stub _get_from_daemon to simulate the daemon's "not running" reply.
    import relaydeck.transports.cli as cli_mod
    monkeypatch.setattr(
        cli_mod, "_get_from_daemon",
        lambda *a, **kw: (cli_mod._POST_DAEMON_ERROR,
                          "Agent alice is not running -- no PTY"),
    )

    result = CliRunner().invoke(cli, ["agent", "screen", "alice"])
    assert result.exit_code == 3, result.output
    assert "not running" in result.output
    assert "last exit" in result.output
    assert "rc=101" in result.output
    assert "/tmp/x/codex-tui.log" in result.output


def test_screen_on_stopped_agent_with_no_events_doesnt_crash(tmp_path, monkeypatch):
    """If there are no harness.exit events on file (agent never
    spawned, or events purged), the fallback hint must degrade
    silently -- don't raise, don't print misleading data."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None

    import relaydeck.transports.cli as cli_mod
    monkeypatch.setattr(
        cli_mod, "_get_from_daemon",
        lambda *a, **kw: (cli_mod._POST_DAEMON_ERROR,
                          "Agent ghost is not running -- no PTY"),
    )

    result = CliRunner().invoke(cli, ["agent", "screen", "ghost"])
    assert result.exit_code == 3
    assert "not running" in result.output
    # No "last exit" line because there's nothing to show.
    assert "last exit" not in result.output
