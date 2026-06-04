"""
`relaydeck agent unblock` + `POST /api/agents/{id}/input`.

The headless write path that answers a harness blocked on a native prompt
("trust this folder? [y/N]", "press enter to continue", an update notice).
It reuses the harness's sanctioned `send_input` / `send_message` — the same
write the live term WebSocket uses — so an unattended fleet can keep moving
without a human attaching a terminal. The semantic engine already *detects*
awaiting-input; this is the *answer* side.
"""

from __future__ import annotations

import sys
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relaydeck.transports.cli as cli_mod
from relaydeck.transports.cli import _POST_OK, _POST_TRANSPORT_FAILED


def _make_app(tmp_path: Path):
    from relaydeck.transports.api import create_app
    from relaydeck.orchestrator import get_orchestrator
    import relaydeck.orchestrator as _orch_mod

    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)
    app = create_app(home)
    app.state.orchestrator = orch
    return app, orch


class _FakeInstance:
    """A stand-in harness instance exposing the PTY write surface the
    input endpoint duck-types against."""

    def __init__(self):
        self.inputs: list[bytes] = []
        self.messages: list[str] = []

    def send_input(self, data: bytes) -> bool:
        self.inputs.append(data)
        return True

    def send_message(self, text: str) -> bool:
        self.messages.append(text)
        return True


# ── POST /api/agents/{id}/input ────────────────────────────────────


def test_input_endpoint_text_with_enter_uses_send_message(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    fake = _FakeInstance()
    monkeypatch.setattr(orch, "get_running_instance",
                        lambda aid: fake if aid == "alice" else None)
    with TestClient(app) as c:
        r = c.post("/api/agents/alice/input", json={"data": "yes", "enter": True})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    # enter=True routes through send_message (harness submit semantics).
    assert fake.messages == ["yes"]
    assert fake.inputs == []


def test_input_endpoint_text_without_enter_uses_send_input(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    fake = _FakeInstance()
    monkeypatch.setattr(orch, "get_running_instance", lambda aid: fake)
    with TestClient(app) as c:
        r = c.post("/api/agents/alice/input", json={"data": "abc"})
    assert r.status_code == 200, r.text
    assert fake.inputs == [b"abc"]
    assert fake.messages == []


def test_input_endpoint_named_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    fake = _FakeInstance()
    monkeypatch.setattr(orch, "get_running_instance", lambda aid: fake)
    with TestClient(app) as c:
        assert c.post("/api/agents/a/input", json={"key": "enter"}).status_code == 200
        assert c.post("/api/agents/a/input", json={"key": "esc"}).status_code == 200
        assert c.post("/api/agents/a/input", json={"key": "ctrl-c"}).status_code == 200
        assert c.post("/api/agents/a/input", json={"key": "up"}).status_code == 200
    assert fake.inputs == [b"\r", b"\x1b", b"\x03", b"\x1b[A"]


def test_input_endpoint_unknown_key_400(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    monkeypatch.setattr(orch, "get_running_instance", lambda aid: _FakeInstance())
    with TestClient(app) as c:
        r = c.post("/api/agents/a/input", json={"key": "f13"})
    assert r.status_code == 400
    assert "unknown key" in r.json()["detail"].lower()


def test_input_endpoint_409_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    monkeypatch.setattr(orch, "get_running_instance", lambda aid: None)
    monkeypatch.setattr(orch, "get_agent", lambda aid: {"id": aid})  # known but stopped
    with TestClient(app) as c:
        r = c.post("/api/agents/bob/input", json={"key": "enter"})
    assert r.status_code == 409
    assert "not running" in r.json()["detail"].lower()


def test_input_endpoint_404_unknown_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    monkeypatch.setattr(orch, "get_running_instance", lambda aid: None)
    monkeypatch.setattr(orch, "get_agent", lambda aid: None)
    with TestClient(app) as c:
        r = c.post("/api/agents/ghost/input", json={"key": "enter"})
    assert r.status_code == 404


def test_input_endpoint_requires_an_action(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    monkeypatch.setattr(orch, "get_running_instance", lambda aid: _FakeInstance())
    with TestClient(app) as c:
        r = c.post("/api/agents/a/input", json={})
    assert r.status_code == 400


# ── CLI: agent unblock ─────────────────────────────────────────────


def _patch_cli(monkeypatch, *, screen="prompt: accept terms? [y/N]"):
    captured: dict = {}

    def fake_get(path, *a, **k):
        return _POST_OK, screen

    def fake_json(method, path, body=None, *, timeout=30.0):
        captured["path"] = path
        captured["body"] = body
        return _POST_OK, {"ok": True, "agent_id": "alice", "sent": body}

    monkeypatch.setattr(cli_mod, "_get_from_daemon", fake_get)
    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake_json)
    return captured


def test_unblock_answer_sends_data_and_enter(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = _patch_cli(monkeypatch)
    res = CliRunner().invoke(
        cli_mod.main, ["agent", "unblock", "alice", "--answer", "y", "--no-show"]
    )
    assert res.exit_code == 0, res.output
    assert captured["path"] == "/api/agents/alice/input"
    assert captured["body"] == {"data": "y", "enter": True}


def test_unblock_enter_sends_enter_key(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = _patch_cli(monkeypatch)
    res = CliRunner().invoke(
        cli_mod.main, ["agent", "unblock", "alice", "--enter", "--no-show"]
    )
    assert res.exit_code == 0, res.output
    assert captured["body"] == {"key": "enter"}


def test_unblock_key_passthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = _patch_cli(monkeypatch)
    res = CliRunner().invoke(
        cli_mod.main, ["agent", "unblock", "alice", "--key", "esc", "--no-show"]
    )
    assert res.exit_code == 0, res.output
    assert captured["body"] == {"key": "esc"}


def test_unblock_no_action_shows_screen_and_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = _patch_cli(monkeypatch)
    res = CliRunner().invoke(cli_mod.main, ["agent", "unblock", "alice"])
    assert res.exit_code == 0, res.output
    assert "accept terms" in res.output  # the screen tail was shown
    assert "No action sent" in res.output
    assert captured == {}  # no input POST happened


def test_unblock_rejects_multiple_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _patch_cli(monkeypatch)
    res = CliRunner().invoke(
        cli_mod.main,
        ["agent", "unblock", "alice", "--answer", "y", "--enter", "--no-show"],
    )
    assert res.exit_code == 2
    assert "at most one" in res.output.lower()


def test_unblock_reports_daemon_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_json(method, path, body=None, *, timeout=30.0):
        return _POST_TRANSPORT_FAILED, "URLError: refused"

    monkeypatch.setattr(cli_mod, "_get_from_daemon", lambda *a, **k: (_POST_OK, ""))
    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake_json)
    res = CliRunner().invoke(
        cli_mod.main, ["agent", "unblock", "alice", "--enter", "--no-show"]
    )
    assert res.exit_code == 1
    assert "unreachable" in res.output.lower()
