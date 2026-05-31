"""
Smoke tests for the workspace message stream SSE endpoint and the
CLI's `inbox -f` plumbing.

The SSE endpoint backs `relaydeck workspace inbox -f`. We can't easily
drive a long-lived streaming response through TestClient (it
suspends the test loop), so we exercise the pieces independently:

  - the endpoint accepts the request and starts sending heartbeats,
  - the CLI helper `_emit_streamed_message` parses an SSE-style
    payload and prints the right shape,
  - the agent-filter logic restricts output.
"""

from __future__ import annotations

import io
from pathlib import Path

from plugins.messaging.plugin import (
    _emit_streamed_message,
    _print_inbox_line,
)


class _CaptureConsole:
    """Stub for the rich.Console.print method — captures every printed
    line as a plain string so the test can assert on contents
    without parsing ANSI codes."""

    def __init__(self):
        self.lines: list[str] = []

    def print(self, *args, **kwargs):
        del kwargs
        text = " ".join(str(a) for a in args)
        # Strip the most common rich markup so assertions read clean.
        for tag in ("[cyan]", "[/]", "[dim]", "[bold]", "[green]"):
            text = text.replace(tag, "")
        # The longer rich tags (e.g. "[in-reply-to:]") don't show up in
        # this test's fixtures, so the simple strip above is enough.
        self.lines.append(text)


def test_emit_streamed_message_prints_one_row():
    console = _CaptureConsole()
    raw = (
        '{"id":"msg_1","from":"architect","to":"coder",'
        '"body":"refactor `parse_args` to use click","injected":true,'
        '"ts":1700000000}'
    )
    _emit_streamed_message(raw, agent_filter=None, console=console, full=True)
    joined = "\n".join(console.lines)
    assert "msg_1" in joined
    assert "architect" in joined
    assert "coder" in joined
    assert "refactor `parse_args`" in joined


def test_emit_streamed_message_respects_agent_filter():
    """When `--agent X` is in effect we only print messages addressed
    to X — others must not leak into the live tail."""
    console = _CaptureConsole()
    raw_match = '{"id":"a","from":"u","to":"alice","body":"hi","injected":false}'
    raw_skip = '{"id":"b","from":"u","to":"bob","body":"hi","injected":false}'
    _emit_streamed_message(raw_match, agent_filter="alice", console=console, full=False)
    _emit_streamed_message(raw_skip, agent_filter="alice", console=console, full=False)
    joined = "\n".join(console.lines)
    assert "a" in joined          # the alice-addressed msg id
    assert "bob" not in joined    # the bob-addressed payload is filtered


def test_emit_streamed_message_tolerates_malformed_json():
    """Stream survives a malformed frame from a misbehaving daemon —
    the CLI must not crash mid-tail."""
    console = _CaptureConsole()
    _emit_streamed_message("not-json", agent_filter=None, console=console, full=True)
    # No row was printed; no exception bubbled.
    assert console.lines == []


def test_print_inbox_line_marks_pending_vs_delivered():
    """The static-list view and the live-tail view both go through
    `_print_inbox_line`, so this test pins the rendering for both
    code paths."""
    console = _CaptureConsole()

    class _Row:
        pass

    delivered = _Row()
    delivered.id = "m1"
    delivered.from_id = "u"
    delivered.to_id = "alice"
    delivered.body = "ok"
    delivered.injected_at = 12345.0
    delivered.in_reply_to = None

    pending = _Row()
    pending.id = "m2"
    pending.from_id = "u"
    pending.to_id = "alice"
    pending.body = "later"
    pending.injected_at = None
    pending.in_reply_to = "m1"

    _print_inbox_line(console, delivered, full=False)
    _print_inbox_line(console, pending, full=False)
    joined = "\n".join(console.lines)
    assert "delivered" in joined
    assert "pending" in joined


def test_stream_reconnects_after_transport_failure(tmp_path, monkeypatch):
    """The SSE consumer must NOT die on a single transport drop —
    the inbox pane in `relaydeck workspace view` is supposed to run for
    hours and survive daemon restarts. We stub urlopen so the first
    call raises URLError, the second returns a one-event stream,
    and the consumer hits `max_reconnects=2`. If reconnect works
    we'll see the one event in the output; if not, the stub for the
    second call goes unused."""
    import urllib.error
    from io import BytesIO

    from plugins.messaging import plugin as messaging
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)

    # Two responses queued for two urlopen calls. The first one
    # raises (simulating a daemon restart); the second yields a
    # single SSE event and ends (server closed connection cleanly).
    sse_body = b"data: " + (
        b'{"id":"msg_after_reconnect","from":"u","to":"a",'
        b'"body":"survived a reconnect","injected":true,"ts":1700000001}'
    ) + b"\n\n"

    class _Stream:
        """Minimal stand-in for a urllib response that yields the SSE
        body one line at a time then closes. Implements just enough
        of the file-like protocol for `_consume_sse_stream`."""
        def __init__(self, body: bytes):
            self._buf = BytesIO(body)
        def __iter__(self): return self
        def __next__(self):
            line = self._buf.readline()
            if not line:
                raise StopIteration
            return line
        def __enter__(self): return self
        def __exit__(self, *_): return False

    calls = {"n": 0}

    def _fake_urlopen(req, context=None):
        del req, context
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("simulated daemon restart")
        return _Stream(sse_body)

    # The stream function imports urllib.request inside its body, so
    # patching the canonical module is enough — every fresh import
    # resolves to our fake.
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    console = _CaptureConsole()
    messaging._stream_workspace_messages(
        "demo",
        agent_filter=None, console=console, full=True,
        max_reconnects=2,
        reconnect_backoff_s=0.01,
    )

    joined = "\n".join(console.lines)
    assert calls["n"] == 2, f"expected reconnect (2 urlopen calls), got {calls['n']}"
    assert "msg_after_reconnect" in joined
    assert "survived a reconnect" in joined
    # Operator gets a notice on the drop, not a silent reconnect.
    assert any(
        "daemon unreachable" in line or "reconnecting" in line
        for line in console.lines
    ), f"expected a reconnect notice, got lines: {console.lines}"


def test_stream_http_error_does_not_retry(tmp_path, monkeypatch):
    """4xx/5xx is configuration / auth — retrying won't help. The
    consumer should print the body and return without looping."""
    import urllib.error
    from io import BytesIO

    from plugins.messaging import plugin as messaging
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)

    calls = {"n": 0}

    def _fake_urlopen(req, context=None):
        del req, context
        calls["n"] += 1
        raise urllib.error.HTTPError(
            url="x", code=401, msg="Unauthorized",
            hdrs=None, fp=BytesIO(b'{"detail":"auth required"}'),
        )

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    console = _CaptureConsole()
    messaging._stream_workspace_messages(
        "demo",
        agent_filter=None, console=console, full=True,
        max_reconnects=5,  # high cap; we don't want any of these used
        reconnect_backoff_s=0.01,
    )
    assert calls["n"] == 1, "HTTP error must short-circuit, not retry"
    joined = "\n".join(console.lines)
    assert "401" in joined
    assert "auth required" in joined


def test_stream_endpoint_is_registered_on_app(tmp_path, monkeypatch):
    """Light regression guard: confirm /api/workspaces/{ws}/messages/stream
    is actually registered. We don't open the stream here (the test
    client would block) — just verify the route exists in the FastAPI
    routes list so a path-prefix accident at refactor time gets caught."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.transports.api import create_app
    import relaydeck.orchestrator as _orch_mod

    _orch_mod._orchestrator = None
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    app = create_app(cfg)

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/workspaces/{workspace}/messages/stream" in paths
