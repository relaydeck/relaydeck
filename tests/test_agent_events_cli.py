"""
`relaydeck agent events <id>` (non-follow) — rendering of stored event
payloads.

Pinned because the non-follow path used to print just event names
(`#534 harness.exit`), which makes harness crashes invisible. The
follow path always rendered payloads; this test enforces parity.
"""

from __future__ import annotations

import sys
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.db import ensure_session, log_event, open_db
from relaydeck.transports.cli import main as cli


def _seed_events(tmp_path, monkeypatch, agent_id="alice"):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None

    db_path = cfg_home / "runtime" / "relaydeck.db"
    conn = open_db(str(db_path))
    try:
        ensure_session(conn, f"agent:{agent_id}")
        ids: list[int] = []
        ids.append(log_event(
            conn, f"agent:{agent_id}", "harness.spawn",
            {"command": ["pi"]}, agent_id=agent_id,
        ))
        ids.append(log_event(
            conn, f"agent:{agent_id}", "harness.assistant_message",
            {"text": "hello"}, agent_id=agent_id,
        ))
        ids.append(log_event(
            conn, f"agent:{agent_id}", "harness.exit",
            {"returncode": 101, "log_path": "/tmp/codex-tui.log"},
            agent_id=agent_id,
        ))
    finally:
        conn.close()
    return ids


def test_agent_events_renders_payload_inline(tmp_path, monkeypatch):
    _seed_events(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli, ["agent", "events", "alice"])
    assert result.exit_code == 0, result.output
    assert "harness.spawn" in result.output
    assert "harness.exit" in result.output
    assert "returncode" in result.output
    assert "/tmp/codex-tui.log" in result.output


def test_agent_events_filters_by_type_substring(tmp_path, monkeypatch):
    _seed_events(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        cli, ["agent", "events", "alice", "--type", "harness.exit"]
    )
    assert result.exit_code == 0, result.output
    assert "harness.exit" in result.output
    # spawn and assistant_message must NOT appear -- the filter
    # is a substring match, not a prefix.
    assert "harness.spawn" not in result.output
    assert "assistant_message" not in result.output


def test_agent_events_type_filter_is_substring_not_exact(tmp_path, monkeypatch):
    """--type harness. should match all three event types since
    they all start with 'harness.'. Pinned because exact-match
    would force users to know the full type name."""
    _seed_events(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        cli, ["agent", "events", "alice", "--type", "harness."]
    )
    assert result.exit_code == 0, result.output
    assert "harness.spawn" in result.output
    assert "harness.assistant_message" in result.output
    assert "harness.exit" in result.output


def test_agent_events_since_excludes_earlier_ids(tmp_path, monkeypatch):
    ids = _seed_events(tmp_path, monkeypatch)
    # since_id = the spawn event id -> drop spawn, keep the next two.
    result = CliRunner().invoke(
        cli, ["agent", "events", "alice", "--since", str(ids[0])]
    )
    assert result.exit_code == 0, result.output
    assert "harness.spawn" not in result.output
    assert "harness.assistant_message" in result.output
    assert "harness.exit" in result.output


def test_agent_events_since_and_type_combine(tmp_path, monkeypatch):
    ids = _seed_events(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli, [
        "agent", "events", "alice",
        "--since", str(ids[0]),
        "--type", "exit",
    ])
    assert result.exit_code == 0, result.output
    # Only harness.exit (id > ids[0], type contains 'exit').
    assert "harness.exit" in result.output
    assert "harness.spawn" not in result.output
    assert "assistant_message" not in result.output
