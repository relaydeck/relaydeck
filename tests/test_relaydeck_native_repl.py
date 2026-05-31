"""
Full coverage for the relaydeck-native REPL surface:

  - `_chat_request`  — HTTP client (ok / daemon-rejected / unreachable)
  - `_chat_repl`     — the interactive terminal loop (reply, /exit, empty
                       lines, EOF, error display, tool-usage line)
  - `relaydeck chat`      — the Click command (one-shot + error exit)
  - `chat_endpoint`  — request validation + type-guard + dispatch
  - `context_endpoint` — the injected-layers view (no model call)

These pin the user-facing native chat without needing a live daemon or a
real model: the HTTP layer is mocked, and the model is stubbed via the
`generate_reply` seam.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relaydeck.transports.cli as cli
from relaydeck.db import _close_all_pools, open_db
from plugins.harnesses.relaydeck_native import agent as native_agent


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path):
    _close_all_pools()
    ch = tmp_path / ".relaydeck"
    (ch / "runtime").mkdir(parents=True)
    (ch / "agents").mkdir(parents=True)
    db = str(ch / "runtime" / "relaydeck.db")
    open_db(db).close()
    yield ch, db
    _close_all_pools()


def _write_spec(ch: Path, agent_id: str, *, type="relaydeck", config=None):
    import yaml
    (ch / "agents" / f"{agent_id}.yaml").write_text(yaml.safe_dump({
        "id": agent_id, "name": agent_id, "type": type,
        "workspace": "w", "config": config or {},
    }))


@pytest.fixture
def capture_console(monkeypatch):
    """Swap cli.console for one writing to a buffer so we can assert on
    REPL output without a real terminal."""
    from rich.console import Console
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, force_terminal=False, width=100))
    return buf


# ── _chat_request (HTTP client) ──────────────────────────────────────


def _fake_resp(payload: dict):
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(payload).encode()
    return _R()


def test_chat_request_ok(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _fake_resp({"ok": True, "reply": "yo"}))
    r = cli._chat_request("sup", "hi")
    assert r["ok"] is True and r["reply"] == "yo"


def test_chat_request_daemon_rejected(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 404, "nope", {}, io.BytesIO(b'{"detail":"x"}'))
    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = cli._chat_request("sup", "hi")
    assert r["ok"] is False and "404" in r["error"]


def test_chat_request_unreachable(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = cli._chat_request("sup", "hi")
    assert r["ok"] is False and "unreachable" in r["error"]


# ── _chat_repl (interactive loop) ────────────────────────────────────


def _feed(monkeypatch, lines):
    it = iter(lines)
    def _input(*a):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    monkeypatch.setattr("builtins.input", _input)


def test_repl_shows_reply(monkeypatch, capture_console):
    _feed(monkeypatch, ["hello", "/exit"])
    monkeypatch.setattr(cli, "_chat_request",
                        lambda a, t: {"ok": True, "reply": "hi back", "tools": []})
    cli._chat_repl("sup")
    out = capture_console.getvalue()
    assert "hi back" in out


def test_repl_exit_command_stops(monkeypatch, capture_console):
    calls = []
    _feed(monkeypatch, ["/exit", "should-not-run"])
    monkeypatch.setattr(cli, "_chat_request", lambda a, t: calls.append(t) or {"ok": True, "reply": "x"})
    cli._chat_repl("sup")
    assert calls == []  # exited before any request


def test_repl_skips_empty_input(monkeypatch, capture_console):
    calls = []
    _feed(monkeypatch, ["", "   ", "real", "/q"])
    monkeypatch.setattr(cli, "_chat_request", lambda a, t: calls.append(t) or {"ok": True, "reply": "ok"})
    cli._chat_repl("sup")
    assert calls == ["real"]  # blanks skipped


def test_repl_eof_exits(monkeypatch, capture_console):
    _feed(monkeypatch, [])  # immediate EOFError
    monkeypatch.setattr(cli, "_chat_request", lambda a, t: {"ok": True, "reply": "x"})
    cli._chat_repl("sup")  # must return cleanly, not raise


def test_repl_new_resets_session(monkeypatch, capture_console):
    """`/new` calls the reset endpoint and does NOT send a chat turn."""
    sent, resets = [], []
    _feed(monkeypatch, ["/new", "/exit"])
    monkeypatch.setattr(cli, "_chat_request", lambda a, t: sent.append(t) or {"ok": True, "reply": "x"})
    monkeypatch.setattr(cli, "_chat_new_request", lambda a: resets.append(a) or (True, ""))
    cli._chat_repl("sup")
    assert resets == ["sup"] and sent == []
    assert "new session" in capture_console.getvalue()


def test_repl_banner_says_continuing(monkeypatch, capture_console):
    _feed(monkeypatch, ["/exit"])
    cli._chat_repl("sup")
    out = capture_console.getvalue()
    assert "continuing session" in out and "/new" in out


def test_repl_shows_error(monkeypatch, capture_console):
    _feed(monkeypatch, ["hi", "/exit"])
    monkeypatch.setattr(cli, "_chat_request", lambda a, t: {"ok": False, "error": "boom"})
    cli._chat_repl("sup")
    assert "boom" in capture_console.getvalue()


def test_repl_shows_tool_usage(monkeypatch, capture_console):
    _feed(monkeypatch, ["do it", "/exit"])
    monkeypatch.setattr(cli, "_chat_request",
                        lambda a, t: {"ok": True, "reply": "done", "tools": [{"calls": ["bash"]}]})
    cli._chat_repl("sup")
    out = capture_console.getvalue()
    assert "done" in out and "bash" in out


# ── relaydeck chat command ────────────────────────────────────────────────


def test_cli_chat_one_shot(monkeypatch):
    from click.testing import CliRunner
    monkeypatch.setattr(cli, "_chat_request",
                        lambda a, t: {"ok": True, "reply": "pong", "tools": []})
    res = CliRunner().invoke(cli.main, ["chat", "sup", "-m", "ping"])
    assert res.exit_code == 0 and "pong" in res.output


def test_cli_chat_one_shot_error(monkeypatch):
    from click.testing import CliRunner
    monkeypatch.setattr(cli, "_chat_request", lambda a, t: {"ok": False, "error": "nope"})
    res = CliRunner().invoke(cli.main, ["chat", "x", "-m", "hi"])
    assert res.exit_code == 1 and "nope" in res.output


# ── chat_endpoint (request contract) ─────────────────────────────────


def test_endpoint_requires_fields(home):
    ch, db = home
    assert native_agent.chat_endpoint(ch, db, {})["ok"] is False
    assert native_agent.chat_endpoint(ch, db, {"agent_id": "sup"})["ok"] is False


def test_endpoint_unknown_agent(home):
    ch, db = home
    r = native_agent.chat_endpoint(ch, db, {"agent_id": "ghost", "text": "hi"})
    assert r["ok"] is False and "no such agent" in r["error"]


def test_endpoint_rejects_non_relaydeck_agent(home):
    ch, db = home
    _write_spec(ch, "bob", type="pi")
    r = native_agent.chat_endpoint(ch, db, {"agent_id": "bob", "text": "hi"})
    assert r["ok"] is False and "not a relaydeck-native agent" in r["error"]


def test_endpoint_success(home, monkeypatch):
    ch, db = home
    _write_spec(ch, "sup", type="relaydeck", config={"preset": "local-fast"})
    monkeypatch.setattr(native_agent, "generate_reply",
                        lambda *a, **k: {"reply": "hello", "model": "local-fast", "tools": []})
    r = native_agent.chat_endpoint(ch, db, {"agent_id": "sup", "text": "hi"})
    assert r["ok"] is True and r["reply"] == "hello"


def test_context_endpoint_returns_layers(home):
    ch, db = home
    _write_spec(ch, "sup", type="relaydeck", config={"soul": "be terse", "tools": ["read"]})
    r = native_agent.context_endpoint(ch, db, "sup")
    assert r["ok"] is True
    ids = [ly["id"] for ly in r["layers"]]
    assert "contract" in ids and "soul" in ids and "capabilities" in ids
    caps = next(ly for ly in r["layers"] if ly["id"] == "capabilities")
    assert "- read — read files in the workspace" in caps["body"]
