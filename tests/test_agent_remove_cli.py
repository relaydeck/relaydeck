"""CLI agent deletion uses the daemon and supports safe batch cleanup."""

from __future__ import annotations

from click.testing import CliRunner

import relaydeck.transports.cli as cli_mod
from relaydeck.transports.cli import (
    _POST_DAEMON_ERROR,
    _POST_OK,
    _POST_TRANSPORT_FAILED,
)


def test_agent_rm_deletes_batch_through_daemon(monkeypatch):
    calls = []

    def fake(method, path, body=None, *, timeout=30.0):
        calls.append((method, path, body, timeout))
        return _POST_OK, {"status": "deleted"}

    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake)
    result = CliRunner().invoke(
        cli_mod.main, ["agent", "rm", "one", "two", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("DELETE", "/api/agents/one", None, 30.0),
        ("DELETE", "/api/agents/two", None, 30.0),
    ]
    assert "Agent one" in result.output
    assert "Agent two" in result.output


def test_agent_rm_reports_each_daemon_error_and_continues(monkeypatch):
    def fake(method, path, body=None, *, timeout=30.0):
        del method, body, timeout
        if path.endswith("/missing"):
            return _POST_DAEMON_ERROR, "HTTP 404: Agent missing not found"
        return _POST_OK, {"status": "deleted"}

    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake)
    result = CliRunner().invoke(
        cli_mod.main, ["agent", "rm", "missing", "present", "--yes"],
    )

    assert result.exit_code == 1
    assert "HTTP 404" in result.output
    assert "Agent present" in result.output


def test_agent_rm_fails_closed_when_daemon_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        cli_mod,
        "_json_to_daemon",
        lambda *args, **kwargs: (_POST_TRANSPORT_FAILED, "connection refused"),
    )

    result = CliRunner().invoke(
        cli_mod.main, ["agent", "rm", "worker", "--yes"],
    )

    assert result.exit_code == 1
    assert "nothing was deleted" in result.output
    assert "relaydeck daemon start" in result.output


def test_agent_rm_can_keep_history(monkeypatch):
    calls = []

    def fake(method, path, body=None, *, timeout=30.0):
        calls.append((method, path, body, timeout))
        return _POST_OK, {"status": "deleted", "purged_history": False}

    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake)
    result = CliRunner().invoke(
        cli_mod.main, ["agent", "rm", "worker", "--keep-history", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("DELETE", "/api/agents/worker?purge_history=false", None, 30.0),
    ]


def test_agent_rm_prompts_once_for_a_batch(monkeypatch):
    monkeypatch.setattr(
        cli_mod,
        "_json_to_daemon",
        lambda *args, **kwargs: (_POST_OK, {"status": "deleted"}),
    )

    result = CliRunner().invoke(
        cli_mod.main, ["agent", "rm", "one", "two"], input="n\n",
    )

    assert result.exit_code == 1
    assert result.output.count("Delete the selected agent(s) permanently?") == 1
    assert "Aborted" in result.output
