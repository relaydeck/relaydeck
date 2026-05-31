"""
Messaging reliability — the pre-launch hardening pass.

Pins the cross-harness delivery guarantees so peer messaging works the
same way regardless of which CLI a workspace's agents wrap:

  L1 submit mechanics — body newline-normalization + tunable split-write
     (the fix for "the message sits in the input box, never sent").
  L2 honest delivery  — readiness flag + opt-in echo-confirm (only mark
     delivered once the harness echoes the message id back).
  L3 reply ergonomics — `relaydeck reply <id> <body>` infers recipient +
     thread so an agent can't misroute or forget --in-reply-to.
  L4 cleanup          — prune_agent_messages drops terminal rows by age
     but never a still-queued (undelivered) message.

Real SQLite + real Click runner, no I/O mocks at the data boundary —
the same discipline as the rest of the suite.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from relaydeck.db import (
    DEFAULT_MESSAGE_RETENTION_DAYS,
    open_db,
    prune_agent_messages,
)
from relaydeck.messages import insert_message, list_one
from relaydeck.harness.base import HarnessAgent, _flatten_for_match
from plugins.harnesses.claude_code.agent import ClaudeCodeAgent
from plugins.harnesses.codex.agent import CodexAgent
from plugins.harnesses.opencode.agent import OpenCodeAgent
from plugins.harnesses.pi.agent import PiAgent


class _BaseHarness(HarnessAgent):
    """Concrete stand-in for exercising base HarnessAgent behaviour
    without spawning a real child."""

    CLI = "true"


def _mk(cls: type[HarnessAgent] = _BaseHarness, *, agent_id="a",
        db_path=":memory:", **config):
    return cls(
        agent_id=agent_id, name=agent_id, config=config,
        workspace="demo", db_path=db_path, stop_flag=threading.Event(),
    )


def _capture_writes(agent) -> list[bytes]:
    writes: list[bytes] = []
    agent.send_input = lambda data: (writes.append(data), True)[1]  # type: ignore[assignment]
    return writes


def _tmp_db(tmp_path) -> str:
    p = tmp_path / "relaydeck.db"
    open_db(p).close()
    return str(p)


# ── L1: body normalization ──────────────────────────────────────────


def test_normalize_collapses_newlines():
    a = _mk()
    assert a._normalize_for_submit("a\nb\r\nc\rd") == "a b c d"


def test_normalize_strips_and_preserves_single_line():
    a = _mk()
    assert a._normalize_for_submit("  hello world  ") == "hello world"
    assert a._normalize_for_submit("single line") == "single line"


def test_normalize_can_be_disabled_per_class():
    class _NoCollapse(_BaseHarness):
        SUBMIT_COLLAPSE_NEWLINES = False

    a = _mk(_NoCollapse)
    # Only the outer strip applies; embedded newlines survive.
    assert a._normalize_for_submit("a\nb") == "a\nb"


# ── L1: submit mechanics (single vs split write) ────────────────────


def test_base_send_message_single_write():
    a = _mk()
    writes = _capture_writes(a)
    assert a.send_message("hello") is True
    assert writes == [b"hello\r"]


def test_base_send_message_normalizes_multiline_body():
    a = _mk()
    writes = _capture_writes(a)
    a.send_message("line one\nline two")
    assert writes == [b"line one line two\r"]


def test_pi_uses_single_write(tmp_path):
    a = _mk(PiAgent, submit_delay_s=0)
    writes = _capture_writes(a)
    a.send_message("hey")
    assert writes == [b"hey\r"], "pi takes input immediately — no split needed"


def test_tui_harnesses_split_body_and_enter(tmp_path):
    """Claude Code (Ink) and the other full-screen TUIs (codex, opencode)
    split the body and the CR into two writes so a paste-debouncing input
    widget can't swallow the Enter."""
    for cls in (ClaudeCodeAgent, CodexAgent, OpenCodeAgent):
        a = _mk(cls, submit_delay_s=0)
        writes = _capture_writes(a)
        ok = a.send_message("[relay from=pi id=msg_abc] hi")
        assert ok is True
        assert writes == [b"[relay from=pi id=msg_abc] hi", b"\r"], (
            f"{cls.__name__} must split into (body, CR); got {writes!r}"
        )


def test_split_first_write_failure_returns_false():
    a = _mk(submit_split=True, submit_delay_s=0)
    a.send_input = lambda data: False  # type: ignore[assignment]
    assert a.send_message("x") is False


def test_config_can_force_split_on_base():
    a = _mk(submit_split=True, submit_delay_s=0)
    writes = _capture_writes(a)
    a.send_message("hi")
    assert writes == [b"hi", b"\r"]


def test_config_can_force_split_off():
    # claude-code defaults to split; a per-agent override turns it off.
    a = _mk(ClaudeCodeAgent, submit_split=False)
    writes = _capture_writes(a)
    a.send_message("hi")
    assert writes == [b"hi\r"]


# ── L2: readiness flag ──────────────────────────────────────────────


def test_seen_output_flag_set_on_first_bytes():
    a = _mk()
    assert a._seen_output is False
    a._broadcast(b"")          # empty read — not readiness
    assert a._seen_output is False
    a._broadcast(b"banner\r\n")
    assert a._seen_output is True


# ── L2: echo confirmation ───────────────────────────────────────────


def test_flatten_for_match_strips_ansi_and_whitespace():
    raw = "\x1b[1mmsg_ab\x1b[0m\n  c123"
    assert _flatten_for_match(raw) == "msg_abc123"


def test_confirm_echo_true_when_marker_present():
    a = _mk()
    a.get_pty_buffer = lambda: b"... [relay from=x id=msg_abc123] hi ..."  # type: ignore[assignment]
    assert a._confirm_echo("msg_abc123", timeout=0.5) is True


def test_confirm_echo_survives_ansi_and_wrapping():
    a = _mk()
    # The id is split across a colour reset and a wrapped line.
    a.get_pty_buffer = lambda: b"\x1b[2mmsg_ab\x1b[0m\nc123\x1b[0m"  # type: ignore[assignment]
    assert a._confirm_echo("msg_abc123", timeout=0.5) is True


def test_confirm_echo_false_when_absent():
    a = _mk()
    a.get_pty_buffer = lambda: b"unrelated terminal noise"  # type: ignore[assignment]
    assert a._confirm_echo("msg_zzz999", timeout=0.2) is False


def test_confirm_echo_empty_marker_is_trivially_true():
    a = _mk()
    a.get_pty_buffer = lambda: b""  # type: ignore[assignment]
    assert a._confirm_echo("", timeout=0.1) is True


def test_confirm_delivery_enabled_config_override():
    assert _mk(confirm_delivery=True)._confirm_delivery_enabled() is True
    assert _mk(confirm_delivery=False)._confirm_delivery_enabled() is False


def test_confirm_delivery_reads_plugin_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck").mkdir(parents=True)
    from relaydeck.plugin_settings import set_settings

    assert _mk()._confirm_delivery_enabled() is False
    set_settings("messaging", {"confirm_delivery": True})
    assert _mk()._confirm_delivery_enabled() is True


# ── L2: drain + confirm integration ─────────────────────────────────


def test_drain_is_always_optimistic(tmp_path):
    """The read-loop drain marks delivered on a successful write — it
    never blocks on echo-confirm (that would deadlock the very loop that
    fills the buffer the echo would appear in)."""
    db = _tmp_db(tmp_path)
    mid = insert_message("boss", "a", "do X", workspace="demo", db_path=db)
    a = _mk(agent_id="a", db_path=db)  # confirm off (default)
    _capture_writes(a)
    a.get_pty_buffer = lambda: b""  # type: ignore[assignment]
    assert a._drain_pending_messages() == 1
    assert list_one(mid, db_path=db).injected_at is not None


def test_drain_never_calls_confirm_echo_even_when_enabled(tmp_path):
    """Regression guard for the read-loop deadlock: even with confirm
    turned on, the drain must NOT call _confirm_echo. The drain runs on
    the PTY read-loop thread; blocking there would freeze the reader that
    refills the ring buffer, so the echo could never arrive."""
    db = _tmp_db(tmp_path)
    mid = insert_message("boss", "a", "do X", workspace="demo", db_path=db)
    a = _mk(agent_id="a", db_path=db, confirm_delivery=True)
    _capture_writes(a)
    called: list = []
    a._confirm_echo = lambda *args, **kw: called.append(args) or True  # type: ignore[assignment]
    a.get_pty_buffer = lambda: b""  # type: ignore[assignment]

    n = a._drain_pending_messages()
    assert n == 1, "drain must deliver optimistically"
    assert called == [], "the read-loop drain must never call echo-confirm"
    assert list_one(mid, db_path=db).injected_at is not None


# ── L2: echo confirm on the orchestrator push path (the sound place) ─


def _orch(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.orchestrator import Orchestrator
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True, exist_ok=True)
    orch = Orchestrator(cfg)

    class _Bus:
        def __init__(self):
            self.events: list = []

        def emit(self, e):
            self.events.append(e)

    orch._plugin_event_bus = _Bus()
    return orch


def _seed_running(orch, agent_id, workspace="demo"):
    conn = open_db(orch.db_path)
    conn.execute(
        "INSERT OR REPLACE INTO agents "
        "(id, type, name, status, workspace, auto_start, created_at) "
        "VALUES (?, 'pi', ?, 'running', ?, 0, 0)",
        (agent_id, agent_id, workspace),
    )
    conn.commit()
    conn.close()


class _ConfirmInstance:
    """Stand-in for a running harness exposing the confirm hooks the
    orchestrator probes via getattr."""

    def __init__(self, echo_ok: bool):
        self.echo_ok = echo_ok
        self.sent: list[str] = []

    def send_message(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def _confirm_delivery_enabled(self) -> bool:
        return True

    def _confirm_echo(self, marker, **kw) -> bool:
        return self.echo_ok


def test_push_marks_injected_when_echo_confirmed(tmp_path, monkeypatch):
    orch = _orch(tmp_path, monkeypatch)
    _seed_running(orch, "bob")
    inst = _ConfirmInstance(echo_ok=True)
    orch._instances["bob"] = inst  # type: ignore[assignment]

    mid, injected = orch.send_message_to("bob", "hi", from_id="alice")
    assert injected is True
    assert inst.sent, "message must still be written to the PTY"
    assert list_one(mid, db_path=orch.db_path).injected_at is not None


def test_push_leaves_queued_when_echo_unconfirmed(tmp_path, monkeypatch):
    orch = _orch(tmp_path, monkeypatch)
    _seed_running(orch, "bob")
    inst = _ConfirmInstance(echo_ok=False)
    orch._instances["bob"] = inst  # type: ignore[assignment]

    mid, injected = orch.send_message_to("bob", "hi", from_id="alice")
    assert injected is False, "unconfirmed echo must not mark delivered"
    assert inst.sent, "best-effort: the bytes are still written"
    m = list_one(mid, db_path=orch.db_path)
    assert m.injected_at is None
    assert m.attempts >= 1, "a delivery failure should be recorded"
    assert m.delivery_state == "queued", "still retryable, not terminal"


def test_push_defers_when_agent_not_yet_ready(tmp_path, monkeypatch):
    """A running instance that hasn't emitted PTY output yet has a TUI
    that isn't input-ready (startup race). The push must NOT write into
    that cold widget — it leaves the message queued for the readiness-
    gated drain."""
    orch = _orch(tmp_path, monkeypatch)
    _seed_running(orch, "bob")

    class _Cold:
        def __init__(self):
            self._seen_output = False
            self.sent: list[str] = []

        def send_message(self, text: str) -> bool:
            self.sent.append(text)
            return True

    inst = _Cold()
    orch._instances["bob"] = inst  # type: ignore[assignment]
    mid, injected = orch.send_message_to("bob", "hi", from_id="alice")
    assert injected is False, "must not push into a not-yet-ready TUI"
    assert inst.sent == [], "no PTY write while the agent is cold"
    m = list_one(mid, db_path=orch.db_path)
    assert m.injected_at is None
    assert m.delivery_state == "queued", "stays queued for the drain"


def test_push_optimistic_when_confirm_disabled(tmp_path, monkeypatch):
    orch = _orch(tmp_path, monkeypatch)
    _seed_running(orch, "bob")

    class _Plain:
        def __init__(self):
            self.sent: list[str] = []

        def send_message(self, text: str) -> bool:
            self.sent.append(text)
            return True

    orch._instances["bob"] = _Plain()  # type: ignore[assignment]
    mid, injected = orch.send_message_to("bob", "hi", from_id="alice")
    assert injected is True
    assert list_one(mid, db_path=orch.db_path).injected_at is not None


# ── L3: relaydeck reply ──────────────────────────────────────────────────


def _reply_cli():
    import click

    @click.group()
    def root():
        pass

    @root.group()
    def workspace():
        pass

    @root.group()
    def agent():
        pass

    from plugins.messaging.plugin import _register_cli
    _register_cli(root)
    return root


def _home_db(tmp_path, monkeypatch) -> str:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runtime = tmp_path / ".relaydeck" / "runtime"
    runtime.mkdir(parents=True)
    db = str(runtime / "relaydeck.db")
    open_db(db).close()
    return db


def test_reply_infers_recipient_and_thread(tmp_path, monkeypatch):
    db = _home_db(tmp_path, monkeypatch)
    monkeypatch.setenv("RELAYDECK_AGENT_ID", "alice")
    # Force the daemon-unreachable durable-enqueue path so the test is
    # deterministic whether or not a real daemon is listening — we assert
    # the inference (recipient + thread) on the persisted message.
    monkeypatch.setenv("RELAYDECK_DAEMON_URL", "http://127.0.0.1:1")

    # Register the workspace + seed the sender as an agent so the fallback
    # enqueue resolves a recipient.
    cfg = tmp_path / ".relaydeck"
    ws_path = tmp_path / "demo"
    ws_path.mkdir()
    (cfg / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{ws_path}"\n'
    )
    conn = open_db(db)
    conn.execute(
        "INSERT INTO agents "
        "(id, type, name, status, workspace, auto_start, created_at) "
        "VALUES ('boss','pi','boss','stopped','demo',0,0)"
    )
    conn.commit()
    conn.close()

    mid = insert_message("boss", "alice", "do X", workspace="demo", db_path=db)

    from click.testing import CliRunner
    res = CliRunner().invoke(_reply_cli(), ["reply", mid, "done, PR #1"])
    assert res.exit_code == 0, res.output

    from relaydeck.messages import list_inbox
    boss_inbox = list_inbox("boss", db_path=db)
    assert boss_inbox, "reply must enqueue a message back to the sender"
    reply = boss_inbox[0]
    assert reply.from_id == "alice"
    assert reply.to_id == "boss", "reply must route back to the original sender"
    assert reply.in_reply_to == mid, "reply must thread to the original id"
    assert reply.body == "done, PR #1"


def test_reply_refuses_non_agent_sender(tmp_path, monkeypatch):
    db = _home_db(tmp_path, monkeypatch)
    mid = insert_message("user", "alice", "hello", workspace="demo", db_path=db)

    from click.testing import CliRunner
    res = CliRunner().invoke(_reply_cli(), ["reply", mid, "hi"])
    assert res.exit_code == 2
    assert "not a peer agent" in res.output


def test_reply_unknown_id(tmp_path, monkeypatch):
    _home_db(tmp_path, monkeypatch)
    from click.testing import CliRunner
    res = CliRunner().invoke(_reply_cli(), ["reply", "msg_nope", "hi"])
    assert res.exit_code == 1
    assert "No such message" in res.output


def test_reply_refuses_unregistered_sender(tmp_path, monkeypatch):
    # A synthetic automation sender (e.g. the github poller's `github`,
    # or the `model` action) is not a registered agent — there's no inbox
    # to reply into, so `relaydeck reply` must refuse cleanly rather than
    # attempt a doomed send that 404s and gets mislabeled "unreachable".
    db = _home_db(tmp_path, monkeypatch)
    mid = insert_message(
        "github", "design-reviewer", "[pipeline] Issue #8 ...",
        workspace="demo", db_path=db,
    )
    from click.testing import CliRunner
    res = CliRunner().invoke(_reply_cli(), ["reply", mid, "approved"])
    assert res.exit_code == 2
    assert "not a peer agent" in res.output
    assert "github" in res.output


def test_reply_delegates_to_telegram(tmp_path, monkeypatch):
    db = _home_db(tmp_path, monkeypatch)
    mid = insert_message(
        "telegram:8283066035", "joker0",
        "@sre_on_call in private: Hello",
        workspace="demo", db_path=db,
    )
    calls: list[dict] = []

    def _fake_telegram(chat_id, body, *, thread_id=None, in_reply_to=None, fmt=None):
        calls.append({
            "chat_id": chat_id, "body": body, "thread_id": thread_id,
            "in_reply_to": in_reply_to,
        })
        return True, {"ok": True, "message_id": 42}

    monkeypatch.setattr(
        "plugins.messaging.plugin._reply_via_telegram",
        _fake_telegram,
    )

    from click.testing import CliRunner
    res = CliRunner().invoke(_reply_cli(), ["reply", mid, "Hello back!"])
    assert res.exit_code == 0, res.output
    assert "sent to telegram chat 8283066035" in res.output
    assert calls == [{
        "chat_id": 8283066035,
        "body": "Hello back!",
        "thread_id": None,
        "in_reply_to": mid,
    }]


def test_reply_delegates_to_telegram_forum_topic(tmp_path, monkeypatch):
    db = _home_db(tmp_path, monkeypatch)
    mid = insert_message(
        "telegram:-100123456789", "inbox",
        "@alice in supergroup topic=8 /triage: please review",
        workspace="demo", db_path=db,
    )
    calls: list[dict] = []

    def _fake_telegram(chat_id, body, *, thread_id=None, in_reply_to=None, fmt=None):
        calls.append({
            "chat_id": chat_id, "body": body, "thread_id": thread_id,
            "in_reply_to": in_reply_to,
        })
        return True, {"ok": True, "message_id": 99}

    monkeypatch.setattr(
        "plugins.messaging.plugin._reply_via_telegram",
        _fake_telegram,
    )

    from click.testing import CliRunner
    res = CliRunner().invoke(_reply_cli(), ["reply", mid, "on it"])
    assert res.exit_code == 0, res.output
    assert calls == [{
        "chat_id": -100123456789,
        "body": "on it",
        "thread_id": 8,
        "in_reply_to": mid,
    }]


def test_telegram_format_hint_auto_html():
    from plugins.messaging.plugin import _telegram_format_hint

    assert _telegram_format_hint("Hello <b>joker0</b>", None) == "html"
    assert _telegram_format_hint("plain text", None) is None
    assert _telegram_format_hint("plain", "html") == "html"


def test_parse_telegram_chat_id_helpers():
    from plugins.messaging.plugin import (
        _parse_telegram_chat_id,
        _telegram_thread_from_body,
    )

    assert _parse_telegram_chat_id("telegram:8283066035") == 8283066035
    assert _parse_telegram_chat_id("telegram:-100123456789") == -100123456789
    assert _parse_telegram_chat_id("github") is None
    assert _telegram_thread_from_body(
        "@alice in supergroup topic=8 /triage: please review",
    ) == 8
    assert _telegram_thread_from_body("@sre_on_call in private: Hello") is None


# ── L4: prune ───────────────────────────────────────────────────────


def _raw_insert(db, msg_id, state, ts, *, to_id="a"):
    conn = open_db(db)
    try:
        conn.execute(
            "INSERT INTO agent_messages "
            "(id, from_id, to_id, body, ts, workspace, delivery_state) "
            "VALUES (?, 'u', ?, 'body', ?, 'demo', ?)",
            (msg_id, to_id, ts, state),
        )
        conn.commit()
    finally:
        conn.close()


def test_prune_drops_old_terminal_keeps_queued(tmp_path):
    db = _tmp_db(tmp_path)
    now = time.time()
    old = now - 30 * 86400
    _raw_insert(db, "m_del_old", "delivered", old)
    _raw_insert(db, "m_fail_old", "failed", old)
    _raw_insert(db, "m_queued_old", "queued", old)
    _raw_insert(db, "m_del_new", "delivered", now)

    deleted = prune_agent_messages(db, retention_days=14)
    assert deleted == 2
    assert list_one("m_del_old", db_path=db) is None
    assert list_one("m_fail_old", db_path=db) is None
    # Undelivered work is never pruned by age.
    assert list_one("m_queued_old", db_path=db) is not None
    # Recent terminal rows are kept.
    assert list_one("m_del_new", db_path=db) is not None


def test_prune_disabled_with_nonpositive_retention(tmp_path):
    db = _tmp_db(tmp_path)
    _raw_insert(db, "m_old", "delivered", time.time() - 999 * 86400)
    assert prune_agent_messages(db, retention_days=0) == 0
    assert list_one("m_old", db_path=db) is not None


def test_default_retention_is_sane():
    assert DEFAULT_MESSAGE_RETENTION_DAYS > 0
