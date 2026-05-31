"""
Regression tests for the `relaydeck workers …` CLI commands.

These used to hardcode `http://127.0.0.1:8777` with no auth and no
TLS context — the wrong port, the wrong host pattern, and a
guaranteed `Connection refused` against any real daemon running on
8765 with auth enabled. Fixed by routing every call through the
shared `_get_from_daemon` helper.

Tests verify:
  - `_workers_via_api` honors `state.yaml.daemon_url` (no hardcode).
  - The request carries the Bearer auth header.
  - A 401 from the daemon surfaces a clean message, not a trace.
  - Empty 200 list ("no workers") doesn't crash the parser.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest


def _stub_urlopen(monkeypatch, *, status_code: int = 200, body: bytes = b"[]"):
    """Replace urllib.request.urlopen with a stub that captures the
    Request object and returns a fixed response.  Returns the
    capture dict so tests can assert on URL + headers."""
    import urllib.error
    import urllib.request

    captured: dict = {}

    class _FakeResp:
        def __init__(self, payload: bytes):
            self._payload = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *_): return self._payload

    def _fake(req, timeout=None, context=None):
        del timeout, context
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["method"] = req.get_method()
        if status_code >= 400:
            raise urllib.error.HTTPError(
                url=req.full_url, code=status_code, msg="error",
                hdrs=None, fp=io.BytesIO(body),
            )
        return _FakeResp(body)

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    return captured


def test_workers_via_api_uses_state_yaml_daemon_url(tmp_path, monkeypatch):
    """The fix: `_workers_via_api` reads `state.yaml.daemon_url`
    instead of hardcoding port 8777. With state pointing at a
    non-default port, the CLI must reach for *that* URL."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck").mkdir(parents=True)

    from relaydeck.state import set_daemon_url
    set_daemon_url("http://127.0.0.1:9999")

    captured = _stub_urlopen(monkeypatch, body=b"[]")
    from relaydeck.transports.cli import _workers_via_api
    result = _workers_via_api()
    assert result == []
    assert captured["url"] == "http://127.0.0.1:9999/api/workers"
    # And the request carries the Bearer header — pre-fix, this was
    # missing entirely.
    auth = captured["headers"].get("Authorization", "")
    assert auth.lower().startswith("bearer "), captured["headers"]


def test_workers_via_api_surfaces_daemon_error(tmp_path, monkeypatch, capsys):
    """A 4xx/5xx from the daemon is printed as `Daemon refused: …`,
    not as a Python traceback. Returns None so the caller exits
    gracefully."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck").mkdir(parents=True)

    _stub_urlopen(
        monkeypatch, status_code=401,
        body=b'{"detail":"auth required"}',
    )
    from relaydeck.transports.cli import _workers_via_api
    assert _workers_via_api() is None
    out = capsys.readouterr().out
    assert "Daemon refused" in out
    assert "401" in out


def test_workers_via_api_transport_error_explains_next_step(tmp_path, monkeypatch, capsys):
    """The original bug: `Connection refused` on the wrong port
    just printed the error. Now we also tell the operator what to
    do next (`relaydeck serve` or set RELAYDECK_DAEMON_URL)."""
    import urllib.error
    import urllib.request

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck").mkdir(parents=True)

    def _fail(*_a, **_kw):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", _fail)

    from relaydeck.transports.cli import _workers_via_api
    assert _workers_via_api() is None
    out = capsys.readouterr().out
    assert "Couldn't reach daemon" in out
    assert "relaydeck serve" in out
    assert "RELAYDECK_DAEMON_URL" in out


def test_get_from_daemon_returns_parsed_json(tmp_path, monkeypatch):
    """The shared helper underneath workers list, workers logs,
    and any future read endpoint. Pins the parse + envelope shape
    so call sites can rely on it."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck").mkdir(parents=True)

    _stub_urlopen(monkeypatch, body=b'{"ok":true,"items":[1,2,3]}')
    from relaydeck.transports.cli import _POST_OK, _get_from_daemon
    outcome, payload = _get_from_daemon("/api/anything")
    assert outcome == _POST_OK
    assert payload == {"ok": True, "items": [1, 2, 3]}
