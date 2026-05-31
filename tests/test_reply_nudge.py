"""Reply-owed detection — list_unanswered_peer_messages() + the messaging
plugin's idle-edge nudge guards.

Background: a peer message carries `[relay from=… id=…]` and "owes a durable
reply" because the sender reads the inbox, not the recipient's transcript. The
query underpins both the idle nudge and the operator `inbox --awaiting-reply`
view.
"""

from __future__ import annotations

import pytest

from relaydeck.db import open_db
from relaydeck.messages import (
    insert_message,
    list_unanswered_peer_messages,
    mark_injected,
)


@pytest.fixture
def db_path(tmp_path) -> str:
    p = tmp_path / "relaydeck.db"
    conn = open_db(str(p))
    conn.close()
    return str(p)


def _looks_like_agent(x: str) -> bool:
    """Agent ids never contain a colon and are never the literal 'user'.

    Channel addresses (`telegram:…`), plugins (`plugin:…`) and the operator
    (`user`) are deliberately NOT registerable peers."""
    return x != "user" and ":" not in x


def _register_agent(db_path: str, agent_id: str) -> None:
    """Register a minimal agent row so the sender counts as a real fleet peer.

    list_unanswered_peer_messages only raises an obligation when the SENDER
    exists in the agents table (i.e. has an inbox of its own to reply into).
    Tests that expect a sender to owe a reply must register it first."""
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, type, name, status, created_at) "
            "VALUES (?, 'claude-code', ?, 'running', 0.0)",
            (agent_id, agent_id),
        )
        conn.commit()
    finally:
        conn.close()


def _deliver(from_id: str, to_id: str, body: str, db_path: str, *,
             in_reply_to: str | None = None, workspace: str | None = None,
             broadcast_id: str | None = None, register: bool = True) -> str:
    """Insert + mark delivered (injected) — only delivered peer messages owe a reply.

    By default registers agent-shaped sender/recipient ids in the agents
    table, because the query gates obligations on the sender being a real
    registered peer. Pass register=False to model a channel sender or a
    removed agent. `broadcast_id` models a fleet-broadcast fan-out row."""
    if register:
        for aid in (from_id, to_id):
            if _looks_like_agent(aid):
                _register_agent(db_path, aid)
    mid = insert_message(from_id, to_id, body, in_reply_to=in_reply_to,
                         workspace=workspace, broadcast_id=broadcast_id,
                         db_path=db_path)
    mark_injected(mid, body, db_path=db_path)
    return mid


# ── the query ───────────────────────────────────────────────────────


def test_delivered_peer_message_owes_a_reply(db_path):
    mid = _deliver("alice", "bob", "review PR 12 please", db_path)
    pending = list_unanswered_peer_messages("bob", db_path=db_path)
    assert [m.id for m in pending] == [mid]


def test_reply_clears_the_obligation(db_path):
    mid = _deliver("alice", "bob", "review PR 12", db_path)
    # bob replies → a message FROM bob with in_reply_to = mid
    _deliver("bob", "alice", "SIGN OFF", db_path, in_reply_to=mid)
    assert list_unanswered_peer_messages("bob", db_path=db_path) == []


def test_user_and_plugin_senders_do_not_owe(db_path):
    """Operator broadcasts and plugin/system messages aren't peer tasks."""
    _deliver("user", "bob", "fyi", db_path)
    _deliver("plugin:messaging", "bob", "[relaydeck] reminder", db_path)
    assert list_unanswered_peer_messages("bob", db_path=db_path) == []


def test_self_sent_excluded(db_path):
    _deliver("bob", "bob", "note to self", db_path)
    assert list_unanswered_peer_messages("bob", db_path=db_path) == []


def test_undelivered_message_does_not_owe_yet(db_path):
    """A queued-but-not-injected message hasn't reached the agent — no nudge."""
    _register_agent(db_path, "alice")  # a real peer…
    insert_message("alice", "bob", "queued only", db_path=db_path)  # …but not injected
    assert list_unanswered_peer_messages("bob", db_path=db_path) == []


def test_oldest_first_ordering(db_path):
    first = _deliver("alice", "bob", "one", db_path)
    second = _deliver("carol", "bob", "two", db_path)
    pending = list_unanswered_peer_messages("bob", db_path=db_path)
    assert [m.id for m in pending] == [first, second]


def test_scoped_to_recipient(db_path):
    _deliver("alice", "bob", "for bob", db_path)
    assert list_unanswered_peer_messages("carol", db_path=db_path) == []


def test_workspace_scope(db_path):
    """The operator view scopes by workspace column, across recipients."""
    a = _deliver("alice", "bob", "x", db_path, workspace="ws1")
    _deliver("carol", "dave", "y", db_path, workspace="ws2")

    ws1 = list_unanswered_peer_messages(workspace="ws1", db_path=db_path)
    assert [m.id for m in ws1] == [a]
    # to_id + workspace compose (AND).
    none = list_unanswered_peer_messages(to_id="dave", workspace="ws1", db_path=db_path)
    assert none == []


# ── ack-loop + channel-sender guards (regression) ───────────────────


def test_reply_is_not_itself_an_obligation(db_path):
    """A message that is itself a reply (in_reply_to set) never owes a
    counter-reply. Without this an ack would owe an ack forever."""
    task = _deliver("alice", "bob", "review PR 12", db_path)
    # bob replies to alice: delivered, from a registered peer, no child —
    # yet it must NOT make alice owe a reply back.
    _deliver("bob", "alice", "SIGN OFF", db_path, in_reply_to=task)
    assert list_unanswered_peer_messages("alice", db_path=db_path) == []


def test_ack_loop_cannot_form(db_path):
    """A full round trip leaves NEITHER side owing, so the ack-of-ack loop
    (the runaway that spammed the fleet) can never start."""
    task = _deliver("alice", "bob", "please review", db_path)
    ack = _deliver("bob", "alice", "done", db_path, in_reply_to=task)
    # bob answered the task → bob owes nothing.
    assert list_unanswered_peer_messages("bob", db_path=db_path) == []
    # alice received an ack (a reply) → alice owes nothing back.
    assert list_unanswered_peer_messages("alice", db_path=db_path) == []
    # …and even if alice acked the ack anyway, that ack owes nothing either.
    _deliver("alice", "bob", "thanks", db_path, in_reply_to=ack)
    assert list_unanswered_peer_messages("bob", db_path=db_path) == []


def test_channel_sender_never_owes(db_path):
    """A Telegram (or any channel) sender has no agent inbox: its reply
    routes back through the bot and never threads a row here. It must never
    be flagged as owed — the bug that re-nudged an already-replied chat."""
    _deliver("telegram:8283066035", "architect", "hello", db_path)
    assert list_unanswered_peer_messages("architect", db_path=db_path) == []


def test_broadcast_fanout_owes_no_per_recipient_reply(db_path):
    """A fleet broadcast is one announcement, not N separate 1:1 asks.

    Regression: a `workspace message` fans out to one row per recipient.
    Without the broadcast marker, every recipient was flagged "owes a reply"
    — so a single fleet message nudged all 8 agents to reply, and the acks
    polluted the threads. Broadcast rows share a `broadcast_id` and owe nothing."""
    bid = "bc_round7"
    for who in ("nova", "flash", "echo"):
        _deliver("atlas", who, "round 7 is live: thread-safe LRU cache", db_path,
                 broadcast_id=bid)
    for who in ("nova", "flash", "echo"):
        assert list_unanswered_peer_messages(who, db_path=db_path) == [], \
            f"{who} should not owe a reply to a fleet broadcast"


def test_direct_message_still_owes_after_broadcast_fix(db_path):
    """The broadcast carve-out must NOT weaken the 1:1 case: a targeted
    message (broadcast_id NULL) still owes a reply."""
    _deliver("atlas", "nova", "nova — can you take this one?", db_path)
    pending = list_unanswered_peer_messages("nova", db_path=db_path)
    assert len(pending) == 1
    assert pending[0].from_id == "atlas"


def test_unregistered_sender_does_not_owe(db_path):
    """A sender absent from the agents table (e.g. a since-removed agent)
    has no inbox to reply into; replying would never clear the obligation,
    so it must not be raised at all."""
    _deliver("ghost", "bob", "I was deleted after sending", db_path,
             register=False)
    assert list_unanswered_peer_messages("bob", db_path=db_path) == []


# ── plugin guards (dedup / cooldown / idle-only) ────────────────────


class _Instance:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, text: str) -> bool:
        self.sent.append(text)
        return True


class _Orch:
    def __init__(self, db_path: str, instance):
        self.db_path = db_path
        self._instance = instance

    def get_running_instance(self, agent_id: str):
        return self._instance


class _Host:
    def __init__(self, orch):
        self._orchestrator = orch
        self.config_home = None

        class _Events:
            def subscribe(self, *a, **k):
                pass

            def emit(self, *a, **k):
                pass
        self.events = _Events()


def _plugin(db_path, instance):
    from plugins.messaging.plugin import MessagingPlugin
    p = MessagingPlugin()
    p.on_load(_Host(_Orch(db_path, instance)))
    return p


def _idle(agent_id: str):
    class E:
        data = {"agent_id": agent_id, "to": "idle"}
    return E()


def test_nudge_fires_once_then_dedups(db_path):
    _deliver("alice", "bob", "review please", db_path)
    inst = _Instance()
    p = _plugin(db_path, inst)

    p._on_status_changed(_idle("bob"))
    assert len(inst.sent) == 1
    assert "relaydeck reply" in inst.sent[0]

    # Going idle again (e.g. right after the nudge) must NOT re-nudge.
    p._last_nudge_at["bob"] = 0.0  # bypass the time cooldown to prove the per-msg dedup
    p._on_status_changed(_idle("bob"))
    assert len(inst.sent) == 1


def test_no_nudge_on_non_idle(db_path):
    _deliver("alice", "bob", "review", db_path)
    inst = _Instance()
    p = _plugin(db_path, inst)

    class E:
        data = {"agent_id": "bob", "to": "working"}
    p._on_status_changed(E())
    assert inst.sent == []


def test_no_nudge_when_nothing_owed(db_path):
    inst = _Instance()
    p = _plugin(db_path, inst)
    p._on_status_changed(_idle("bob"))
    assert inst.sent == []


# ── ledger memory-bound: resolved-status prune, NOT a wall-clock TTL ──


def test_still_owed_message_is_never_re_nudged(db_path):
    """A still-unanswered obligation is nudged exactly once and never again,
    no matter how many idle cycles pass. Regression: an earlier design aged
    the ledger out on a 6h TTL, which re-armed the nudge for a genuinely
    stuck (or no-reply-needed) message forever."""
    _deliver("alice", "bob", "do X", db_path)
    inst = _Instance()
    p = _plugin(db_path, inst)

    p._on_status_changed(_idle("bob"))
    assert len(inst.sent) == 1

    # Many idle cycles with the cooldown always cleared: still only one nudge,
    # because the obligation is still owed so its ledger row is retained.
    for _ in range(5):
        p._last_nudge_at.pop("bob", None)
        p._on_status_changed(_idle("bob"))
    assert len(inst.sent) == 1


def test_answered_obligation_is_pruned_from_ledger(db_path):
    """Once a message is answered it leaves the owed-set and its ledger row
    is dropped — so memory is bounded by live obligations, not by every
    message ever nudged."""
    mid = _deliver("alice", "bob", "review", db_path)
    inst = _Instance()
    p = _plugin(db_path, inst)

    p._on_status_changed(_idle("bob"))
    assert ("bob", mid) in p._nudged

    # bob replies (threads off mid) → obligation cleared. The next idle GCs
    # the now-dead ledger row.
    _deliver("bob", "alice", "done", db_path, in_reply_to=mid)
    p._last_nudge_at.pop("bob", None)
    p._on_status_changed(_idle("bob"))
    assert ("bob", mid) not in p._nudged
    assert len(inst.sent) == 1  # nothing new owed → no extra nudge


def test_cooldown_ledger_is_ttl_bounded(db_path):
    """The transient per-agent cooldown ledger is the only one aged by time;
    stale entries are dropped so it can't grow without bound."""
    inst = _Instance()
    p = _plugin(db_path, inst)
    p._last_nudge_at["ghost"] = 0.0  # ancient relative to the TTL below
    p._prune_cooldown_ledger(p._COOLDOWN_LEDGER_TTL_S + 1.0)
    assert "ghost" not in p._last_nudge_at
