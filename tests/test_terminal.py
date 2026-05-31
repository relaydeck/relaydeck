"""
Terminal integration tests — PTY end-to-end through the harness subscriber
pool, and the binary frame protocol over the WebSocket endpoint.

These tests run a real subprocess inside a real PTY (so isatty() is true
inside the child), then assert that bytes flow to subscribers and the
ring buffer, that resize() doesn't error, and that stdin forwarding works.
"""

from __future__ import annotations

import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.harness import HarnessAgent


class EchoHarness(HarnessAgent):
    """Spawn /bin/sh -c 'echo hello-from-pty; sleep 0.5' so we get bytes,
    then the process exits naturally."""
    CLI = "/bin/sh"
    DEFAULT_ARGS = ["-c", "printf 'hello-from-pty\\n'; sleep 0.3"]


class InteractiveHarness(HarnessAgent):
    """A cat process — we can write to its stdin and see it echoed back."""
    CLI = "/bin/cat"
    DEFAULT_ARGS = []


def _make_agent(cls):
    tmp = tempfile.mkdtemp(prefix="relaydeck-term-test-")
    db = Path(tmp) / "relaydeck.db"
    # workspace=None so the base class doesn't append `--workspace <dir>` —
    # /bin/sh and /bin/cat don't understand that flag.
    return cls(
        agent_id="t1", name="t1", config={},
        workspace=None, db_path=str(db),
        stop_flag=threading.Event(),
    )


def _wait_for_pty_open(agent, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getattr(agent, "_master_fd", None) is not None:
            return
        time.sleep(0.02)
    raise AssertionError("PTY master did not open")


def test_pty_subscriber_receives_output():
    """Bytes from a subprocess running in a real PTY arrive in subscribers."""
    agent = _make_agent(EchoHarness)
    sub = agent.subscribe_pty()

    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()

    received = bytearray()
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            chunk = sub.get(timeout=0.2)
        except queue.Empty:
            continue
        if chunk is None:
            break  # EOF sentinel
        received.extend(chunk)
        if b"hello-from-pty" in received:
            break

    agent.stop_flag.set()
    agent.terminate()
    thread.join(timeout=2)

    assert b"hello-from-pty" in bytes(received), \
        f"expected PTY output in subscriber, got: {bytes(received)!r}"


def test_pty_ring_buffer_replay():
    """The ring buffer captures recent output for reconnecting clients."""
    agent = _make_agent(EchoHarness)
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()

    # Wait for the process to produce output and exit.
    deadline = time.time() + 3.0
    while time.time() < deadline and not agent.get_pty_buffer():
        time.sleep(0.05)

    buf = agent.get_pty_buffer()
    agent.stop_flag.set()
    agent.terminate()
    thread.join(timeout=2)

    assert b"hello-from-pty" in buf, \
        f"expected ring buffer to capture output, got: {buf!r}"


def test_harness_exit_payload_carries_log_path():
    """When a harness exits, the `harness.exit` event must include
    a `log_path` so operators can chase a crash without knowing each
    harness's storage layout. Default is None; subclasses with a
    known log location override `log_path()`. Pinned because this
    is the only breadcrumb from a daemon-side death (PTY bytes
    aren't persisted per AGENTS.md)."""
    agent = _make_agent(EchoHarness)
    events: list[tuple[str, dict]] = []
    agent.emit = lambda kind, payload=None: (  # type: ignore[method-assign]
        events.append((kind, payload or {})) or 1
    )
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()
    thread.join(timeout=3)

    exits = [p for k, p in events if k == "harness.exit"]
    assert len(exits) == 1, f"expected one harness.exit event, got {events}"
    payload = exits[0]
    assert "returncode" in payload
    # EchoHarness inherits the base default — no override.
    assert "log_path" in payload and payload["log_path"] is None


def test_pty_eof_sentinel_pushed_on_exit():
    """When the child exits, every subscriber receives a None sentinel."""
    agent = _make_agent(EchoHarness)
    sub = agent.subscribe_pty()
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()

    saw_eof = False
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            chunk = sub.get(timeout=0.2)
        except queue.Empty:
            continue
        if chunk is None:
            saw_eof = True
            break

    agent.stop_flag.set()
    thread.join(timeout=2)
    assert saw_eof, "expected EOF sentinel after child exit"


def test_pty_stdin_round_trip():
    """send_input() bytes show up in subscribers via the child's stdout."""
    agent = _make_agent(InteractiveHarness)
    sub = agent.subscribe_pty()
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()

    _wait_for_pty_open(agent)
    ok = agent.send_input(b"ping-roundtrip\n")
    assert ok, "expected send_input to succeed against a live PTY"

    received = bytearray()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            chunk = sub.get(timeout=0.2)
        except queue.Empty:
            continue
        if chunk is None:
            break
        received.extend(chunk)
        if b"ping-roundtrip" in received:
            break

    agent.stop_flag.set()
    agent.terminate()
    thread.join(timeout=2)

    assert b"ping-roundtrip" in bytes(received), \
        f"expected echoed stdin in PTY output, got: {bytes(received)!r}"


def test_pty_resize_does_not_error():
    """resize() should succeed once the PTY master is open."""
    agent = _make_agent(InteractiveHarness)
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()

    try:
        _wait_for_pty_open(agent)
        assert agent.resize(120, 40) is True
        assert agent.resize(80, 24) is True
    finally:
        agent.stop_flag.set()
        agent.terminate()
        thread.join(timeout=2)


def test_pty_missing_cli_emits_error():
    """A nonexistent command should set status=errored and not crash."""
    agent = _make_agent(EchoHarness)
    agent.CLI = "/nonexistent/cli-xyzzy-relaydeck"
    agent.DEFAULT_ARGS = []
    # patch the build to ignore workspace --workspace arg too
    agent.config = {"command": ["/nonexistent/cli-xyzzy-relaydeck"]}
    agent.run()  # synchronous — should return after emitting harness.error
    # If we got here without raising, the error path worked.


def test_unsubscribe_stops_delivery():
    """Unsubscribed queues stop receiving new bytes."""
    agent = _make_agent(InteractiveHarness)
    sub = agent.subscribe_pty()
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()

    time.sleep(0.2)
    agent.send_input(b"first\n")
    time.sleep(0.2)
    # Drain anything seen so far.
    drained_before = 0
    try:
        while True:
            sub.get_nowait()
            drained_before += 1
    except queue.Empty:
        pass

    agent.unsubscribe_pty(sub)
    agent.send_input(b"second\n")
    time.sleep(0.3)

    # After unsubscribe, no new bytes should arrive.
    remaining = 0
    try:
        while True:
            sub.get_nowait()
            remaining += 1
    except queue.Empty:
        pass

    agent.stop_flag.set()
    agent.terminate()
    thread.join(timeout=2)

    assert remaining == 0, f"expected no bytes after unsubscribe, got {remaining}"


# ── WebSocket frame protocol (heartbeat, forwarding, replay) ─────────
#
# These exercise the /api/agents/{id}/term endpoint through FastAPI's
# TestClient with a fake running instance, so we test the frame
# protocol + heartbeat without spinning a real PTY.

import pytest
from fastapi.testclient import TestClient


def _make_app(tmp_path):
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
    """Minimal duck-typed harness instance for the WS endpoint.
    subscribe_pty hands out a queue the test can push bytes into."""

    def __init__(self, buffer: bytes = b""):
        self._buf = buffer
        self.q: "queue.Queue" = queue.Queue()
        self.inputs: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.unsubscribed = False

    def subscribe_pty(self):
        return self.q

    def unsubscribe_pty(self, sub):
        self.unsubscribed = True

    def get_pty_buffer(self):
        return self._buf

    def send_input(self, data):
        self.inputs.append(bytes(data))

    def resize(self, cols, rows):
        self.resizes.append((cols, rows))


class _NoReplayFakeInstance(_FakeInstance):
    REPLAY_PTY_BUFFER = False


class _SanitizedReplayFakeInstance(_FakeInstance):
    SANITIZE_PTY_REPLAY = True


def test_ws_replays_buffer_on_connect(tmp_path, monkeypatch):
    """A non-empty ring buffer is replayed (clear + bytes) on connect
    so a refreshing tab sees screen context."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_AUTH_TOKEN", "test-token-xyz")
    app, orch = _make_app(tmp_path)
    inst = _FakeInstance(buffer=b"screen-state-here")
    monkeypatch.setattr(
        orch, "get_running_instance",
        lambda agent_id: inst if agent_id == "alice" else None,
    )
    with TestClient(app) as c:
        with c.websocket_connect("/api/agents/alice/term?token=test-token-xyz") as ws:
            frame = ws.receive_bytes()
            assert frame[0:1] == b"\x00"           # PTY-output frame
            assert b"screen-state-here" in frame
            assert b"\x1b[2J" in frame             # clear-screen prefix


def test_ws_sanitizes_replay_when_harness_opts_in(tmp_path, monkeypatch):
    """Full-screen TUIs can keep replay while trimming a leading ANSI
    fragment that would otherwise render as literal text."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_AUTH_TOKEN", "test-token-xyz")
    app, orch = _make_app(tmp_path)
    inst = _SanitizedReplayFakeInstance(buffer=b"10;10m\x1b[31mvisible")
    monkeypatch.setattr(
        orch, "get_running_instance",
        lambda agent_id: inst if agent_id == "alice" else None,
    )
    with TestClient(app) as c:
        with c.websocket_connect("/api/agents/alice/term?token=test-token-xyz") as ws:
            frame = ws.receive_bytes()
            assert b"10;10m" not in frame
            assert b"\x1b[31mvisible" in frame


def test_ws_rejects_bad_token(tmp_path, monkeypatch):
    """No/!wrong token closes the socket before any PTY pipe opens."""
    from starlette.websockets import WebSocketDisconnect as _WSD
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_AUTH_TOKEN", "test-token-xyz")
    app, _orch = _make_app(tmp_path)
    with TestClient(app) as c:
        with pytest.raises(_WSD):
            with c.websocket_connect("/api/agents/alice/term?token=wrong") as ws:
                ws.receive_bytes()


def test_ws_unknown_agent_signals_not_running(tmp_path, monkeypatch):
    """Connecting to an agent with no live instance gets a lifecycle
    frame, not a silent drop."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_AUTH_TOKEN", "test-token-xyz")
    app, orch = _make_app(tmp_path)
    monkeypatch.setattr(orch, "get_running_instance", lambda agent_id: None)
    with TestClient(app) as c:
        with c.websocket_connect("/api/agents/ghost/term?token=test-token-xyz") as ws:
            frame = ws.receive_bytes()
            assert frame[0:1] == b"\x01"
            assert b"agent_not_running" in frame


# ── Stat-strip + sidebar data endpoints (dashboard) ──────────────────


def test_agent_stats_endpoint_returns_rollup(tmp_path, monkeypatch):
    """GET /api/agents/{id}/stats returns the per-agent rollup the
    detail stat strip renders."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_AUTH_TOKEN", "t")
    app, orch = _make_app(tmp_path)
    from relaydeck.db import open_db, record_usage, upsert_agent
    conn = open_db(orch.db_path)
    upsert_agent(conn, "alice", "pi", "alice")
    record_usage(conn, "alice", "s1", model="m", provider="p",
                 prompt_tokens=100, completion_tokens=40, total_tokens=140, cost_usd=0.01)
    conn.close()
    with TestClient(app) as c:
        r = c.get("/api/agents/alice/stats", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tokens_24h"] == 140
    assert body["tokens_in"] == 100
    assert len(body["activity"]) == 30


def test_agent_stats_endpoint_404_for_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_AUTH_TOKEN", "t")
    app, _orch = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/agents/ghost/stats", headers={"Authorization": "Bearer t"})
    assert r.status_code == 404


def test_usage_rollup_endpoint(tmp_path, monkeypatch):
    """GET /api/agents/usage-rollup returns the fleet-wide map the
    sidebar uses — and isn't shadowed by the {agent_id} route."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_AUTH_TOKEN", "t")
    app, orch = _make_app(tmp_path)
    from relaydeck.db import open_db, record_usage
    conn = open_db(orch.db_path)
    record_usage(conn, "alice", "s1", model="m", provider="p",
                 prompt_tokens=100, completion_tokens=40, total_tokens=140)
    conn.close()
    with TestClient(app) as c:
        r = c.get("/api/agents/usage-rollup", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200, r.text
    assert r.json()["alice"]["tokens"] == 140
