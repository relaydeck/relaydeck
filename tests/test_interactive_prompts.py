"""
Interactive prompts: structured human-in-the-loop approvals.

Covers the provider-agnostic core (`relaydeck/channels.py` +
`relaydeck/prompts.py`), the `prompts` plugin's web channel + host
surface, and the contract every messaging provider plugs into. The
Telegram-specific provider is exercised separately by the plugin's own
tests; here we use a fake provider to prove the abstraction never needs
to know which platform it's talking to.
"""

from __future__ import annotations

import time
import types

import pytest

from relaydeck import channels as C
from relaydeck import prompts as P
from relaydeck.db import open_db

# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    p = str(tmp_path / "relaydeck.db")
    open_db(p).close()  # trigger migration
    return p


@pytest.fixture(autouse=True)
def _clean_channel_registry():
    """Snapshot + restore the process-wide channel registry so tests
    that register providers don't leak into each other."""
    saved = dict(C._REGISTRY)
    try:
        yield
    finally:
        C._REGISTRY.clear()
        C._REGISTRY.update(saved)


@pytest.fixture(autouse=True)
def _reset_service():
    P.reset_service()
    yield
    P.reset_service()


class FakeProvider:
    """A minimal MessagingProvider for testing the abstraction."""

    def __init__(self, channel: str, interactive: bool = True):
        self.channel = channel
        self._interactive = interactive
        self.delivered: list[tuple[str, str]] = []
        self.texts: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str, str | None]] = []

    def capabilities(self):
        return C.ChannelCapabilities(interactive_buttons=self._interactive, editable=True)

    def connections(self):
        return [{"id": "x", "name": "x", "ready": True}]

    def deliver_prompt(self, address, prompt):
        self.delivered.append((address.to_str(), prompt.id))
        return C.DeliveryResult(ok=True, ref=f"ref-{prompt.id}", mode="buttons")

    def deliver_text(self, address, text, *, in_reply_to=None):
        self.texts.append((address.to_str(), text))
        return C.DeliveryResult(ok=True, ref="t1", mode="text")

    def close_prompt(self, address, ref, prompt):
        self.closed.append((address.to_str(), ref, prompt.answer_choice))


# ── Address ──────────────────────────────────────────────────────────


def test_address_parse_full():
    a = C.Address.parse("telegram:ops:-1001234567890:42")
    assert (a.channel, a.connection, a.target, a.thread) == (
        "telegram", "ops", "-1001234567890", "42",
    )
    assert a.to_str() == "telegram:ops:-1001234567890:42"


def test_address_parse_trims_trailing_empties():
    assert C.Address.parse("web").to_str() == "web"
    assert C.Address.parse("telegram::123").to_str() == "telegram::123"


def test_address_negative_chat_id_not_split():
    # A bare chat id with no connection: channel + empty conn + target.
    a = C.Address.parse("telegram::-100999")
    assert a.target == "-100999"


def test_address_empty_channel_raises():
    with pytest.raises(ValueError):
        C.Address.parse("")


def test_address_dict_roundtrip():
    a = C.Address.parse("discord:main:#ops")
    assert C.Address.from_dict(a.to_dict()) == a


# ── Choice ───────────────────────────────────────────────────────────


def test_choice_parse_variants():
    assert P.Choice.parse("approve").to_dict() == {
        "id": "approve", "label": "Approve", "style": "default", "value": None,
    }
    c = P.Choice.parse("approve:Ship it:primary")
    assert c.label == "Ship it" and c.style == "primary"
    # underscores become spaces in the auto-label
    assert P.Choice.parse("hold_for_review").label == "Hold for review"


def test_choice_unknown_style_normalized():
    assert P.Choice("x", style="rainbow").style == "default"


def test_choice_empty_id_raises():
    with pytest.raises(ValueError):
        P.Choice("")


# ── Store ────────────────────────────────────────────────────────────


def test_migration_creates_table(db_path):
    conn = open_db(db_path)
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 14
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='interactive_prompts'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_insert_and_get(db_path):
    pr = P.insert_prompt(
        "agent_a", "Deploy?",
        [P.Choice.parse("yes:Yes"), P.Choice.parse("no:No")],
        workspace="ws1", db_path=db_path,
    )
    got = P.get_prompt(pr.id, db_path=db_path)
    assert got is not None
    assert got.body == "Deploy?" and got.state == P.STATE_OPEN
    assert [c.id for c in got.choices] == ["yes", "no"]
    assert got.workspace == "ws1"


def test_insert_rejects_duplicate_choice(db_path):
    with pytest.raises(ValueError):
        P.insert_prompt("a", "q", [P.Choice("x"), P.Choice("x")], db_path=db_path)


def test_resolve_is_atomic_first_wins(db_path):
    pr = P.insert_prompt("a", "q", [P.Choice("yes"), P.Choice("no")], db_path=db_path)
    first = P.resolve_prompt(pr.id, "yes", answered_by="alice", db_path=db_path)
    second = P.resolve_prompt(pr.id, "no", answered_by="bob", db_path=db_path)
    assert first is not None and first.answer_choice == "yes"
    assert first.state == P.STATE_ANSWERED and first.answered_by == "alice"
    assert second is None  # lost the race


def test_resolve_rejects_unknown_choice(db_path):
    pr = P.insert_prompt("a", "q", [P.Choice("yes")], db_path=db_path)
    assert P.resolve_prompt(pr.id, "maybe", answered_by="x", db_path=db_path) is None
    # prompt stays open
    assert P.get_prompt(pr.id, db_path=db_path).state == P.STATE_OPEN


def test_cancel(db_path):
    pr = P.insert_prompt("a", "q", [P.Choice("ok")], db_path=db_path)
    assert P.cancel_prompt(pr.id, reason="superseded", db_path=db_path) is not None
    assert P.get_prompt(pr.id, db_path=db_path).state == P.STATE_CANCELED
    # can't resolve a canceled prompt
    assert P.resolve_prompt(pr.id, "ok", answered_by="x", db_path=db_path) is None


def test_service_cancel_retracts_and_emits(db_path):
    tg = FakeProvider("telegram")
    C.register_channel(tg)
    events = []
    svc = _service(db_path, [], events)
    pr = svc.ask("q", [P.Choice("ok")], addresses=["telegram:ops:-100"], agent_id="x")
    canceled = svc.cancel(pr.id, reason="no longer needed")
    assert canceled is not None and canceled.state == P.STATE_CANCELED
    assert tg.closed  # deliveries retracted
    assert ("prompt.canceled", pr.id) in events


def test_expire_due(db_path):
    fresh = P.insert_prompt("a", "fresh", [P.Choice("ok")],
                            expires_ts=time.time() + 999, db_path=db_path)
    stale = P.insert_prompt("a", "stale", [P.Choice("ok")],
                            expires_ts=time.time() - 1, db_path=db_path)
    expired = P.expire_due(db_path=db_path)
    ids = {p.id for p in expired}
    assert stale.id in ids and fresh.id not in ids
    assert P.get_prompt(stale.id, db_path=db_path).state == P.STATE_EXPIRED
    assert P.get_prompt(fresh.id, db_path=db_path).state == P.STATE_OPEN


def test_list_filters(db_path):
    P.insert_prompt("a1", "q1", [P.Choice("ok")], workspace="ws1", db_path=db_path)
    P.insert_prompt("a2", "q2", [P.Choice("ok")], workspace="ws2", db_path=db_path)
    assert len(P.list_prompts(workspace="ws1", db_path=db_path)) == 1
    assert len(P.list_prompts(agent_id="a2", db_path=db_path)) == 1
    assert len(P.list_prompts(state="open", db_path=db_path)) == 2


# ── Channel registry + text fallback ────────────────────────────────


def test_registry_register_get_list():
    fp = FakeProvider("telegram")
    C.register_channel(fp)
    assert C.get_channel("telegram") is fp
    assert "telegram" in C.list_channels()
    C.unregister_channel("telegram", fp)
    assert C.get_channel("telegram") is None


def test_registry_unregister_only_own_instance():
    a = FakeProvider("web")
    b = FakeProvider("web")
    C.register_channel(a)
    C.unregister_channel("web", b)
    assert C.get_channel("web") is a
    C.unregister_channel("web", a)
    assert C.get_channel("web") is None


def test_registry_rejects_empty_channel():
    class Bad:
        channel = ""
    with pytest.raises(ValueError):
        C.register_channel(Bad())


def test_render_choices_as_text(db_path):
    pr = P.insert_prompt("a", "Restart?", [P.Choice("yes"), P.Choice("no")], db_path=db_path)
    txt = C.render_choices_as_text(pr)
    assert "Restart?" in txt and "1. yes" in txt and "2. no" in txt


@pytest.mark.parametrize("reply,expected", [
    ("1", "yes"), ("2", "no"), ("yes", "yes"), ("  NO ", "no"),
    ("approve, do it", None),  # not a choice for this prompt
    ("3", None), ("", None),
])
def test_match_text_to_choice(db_path, reply, expected):
    pr = P.insert_prompt("a", "q", [P.Choice("yes"), P.Choice("no")], db_path=db_path)
    got = C.match_text_to_choice(pr, reply)
    assert (got.id if got else None) == expected


# ── PromptService (provider-agnostic orchestration) ─────────────────


def _service(db_path, resumes, events):
    def resume(to, body, from_id="user"):
        resumes.append((to, body, from_id))
        return ("m", True)

    return P.PromptService(
        db_path=db_path,
        resume_fn=resume,
        event_emit=lambda t, data: events.append((t, data["id"])),
    )


def test_service_fans_out_to_multiple_channels(db_path):
    tg = FakeProvider("telegram", interactive=True)
    web = FakeProvider("web", interactive=True)
    C.register_channel(tg)
    C.register_channel(web)
    svc = _service(db_path, [], [])
    pr = svc.ask(
        "Ship v2?", [P.Choice("approve"), P.Choice("reject")],
        addresses=["telegram:ops:-100", "web"], agent_id="dep",
    )
    assert tg.delivered and web.delivered
    chans = {d["channel"] for d in pr.deliveries}
    assert chans == {"telegram", "web"}
    assert all(d["ok"] for d in pr.deliveries)


def test_service_resolve_resumes_agent_and_retracts(db_path):
    tg = FakeProvider("telegram")
    C.register_channel(tg)
    resumes, events = [], []
    svc = _service(db_path, resumes, events)
    pr = svc.ask("Deploy?", [P.Choice.parse("approve:Approve")],
                 addresses=["telegram:ops:-100"], agent_id="dep")
    res = svc.respond(pr.id, "approve", answered_by="telegram @al")
    assert res is not None and res.answer_choice == "approve"
    # agent resumed with a structured, parseable decision
    assert resumes and resumes[0][0] == "dep"
    assert "choice=approve" in resumes[0][1] and resumes[0][2] == "prompt"
    # other deliveries retracted
    assert tg.closed and tg.closed[0][2] == "approve"
    # lifecycle events
    assert ("prompt.created", pr.id) in events
    assert ("prompt.answered", pr.id) in events


def test_service_double_resolve_loses(db_path):
    C.register_channel(FakeProvider("web"))
    svc = _service(db_path, [], [])
    pr = svc.ask("q", [P.Choice("a"), P.Choice("b")], addresses=["web"], agent_id="x")
    assert svc.respond(pr.id, "a", answered_by="1") is not None
    assert svc.respond(pr.id, "b", answered_by="2") is None


def test_service_no_resume_when_notify_disabled(db_path):
    C.register_channel(FakeProvider("web"))
    resumes = []
    svc = _service(db_path, resumes, [])
    pr = svc.ask("q", [P.Choice("a")], addresses=["web"], agent_id="x", notify_agent=False)
    svc.respond(pr.id, "a", answered_by="cli")
    assert resumes == []  # blocking caller reads the answer from the store instead


def test_service_text_fallback_and_reply_match(db_path):
    sms = FakeProvider("sms", interactive=False)
    C.register_channel(sms)
    svc = _service(db_path, [], [])
    pr = svc.ask("Restart?", [P.Choice("yes"), P.Choice("no")],
                 addresses=["sms:twilio:+1555"], agent_id="ops")
    # text fallback rendered as a numbered list
    assert sms.texts and "1. yes" in sms.texts[0][1]
    # human texts "1" back
    res = svc.respond_text(pr.id, "1", answered_by="sms:+1555")
    assert res is not None and res.answer_choice == "yes"


def test_service_text_reply_no_match_returns_none(db_path):
    C.register_channel(FakeProvider("sms", interactive=False))
    svc = _service(db_path, [], [])
    pr = svc.ask("q", [P.Choice("a"), P.Choice("b")], addresses=["sms:t:+1"], agent_id="x")
    assert svc.respond_text(pr.id, "banana", answered_by="x") is None
    assert P.get_prompt(pr.id, db_path=db_path).state == P.STATE_OPEN


def test_service_unknown_channel_records_failure(db_path):
    svc = _service(db_path, [], [])
    pr = svc.ask("q", [P.Choice("ok")], addresses=["discord:main:#x"], agent_id="x")
    failed = [d for d in pr.deliveries if d.get("channel") == "discord"]
    assert failed and not failed[0]["ok"] and "no provider" in failed[0]["error"]


def test_service_defaults_to_web(db_path):
    web = FakeProvider("web")
    C.register_channel(web)
    svc = _service(db_path, [], [])
    pr = svc.ask("q", [P.Choice("ok")], agent_id="x")  # no addresses
    assert web.delivered and pr.deliveries[0]["channel"] == "web"


def test_service_sweep_expired_resumes_agent(db_path):
    C.register_channel(FakeProvider("web"))
    resumes = []
    svc = _service(db_path, resumes, [])
    pr = svc.ask("q", [P.Choice("ok")], addresses=["web"], agent_id="dep", expires_in=-1)
    swept = svc.sweep_expired()
    assert any(p.id == pr.id for p in swept)
    assert resumes and "expired" in resumes[0][1].lower()


# ── prompts plugin: web provider + host surface ─────────────────────


def test_web_provider_and_host_surface(tmp_path):
    import plugins.prompts.plugin as PP
    from relaydeck.testing import MockHost

    host = MockHost(
        name="prompts",
        config_home=tmp_path,
        declared_capabilities={
            "channels.register", "prompts.read", "prompts.write",
            "cli.register", "api.register", "agents.list",
            "events.subscribe", "events.emit",
        },
    )
    # migrate the host's db
    open_db(str(tmp_path / "runtime" / "relaydeck.db")).close()

    plugin = PP.PromptsPlugin()
    plugin.on_load(host)
    # web provider registered + interactive
    C.register_channel(plugin._web)  # also bind into the global registry
    assert plugin._web.channel == "web"
    assert plugin._web.capabilities().interactive_buttons is True

    # raise a prompt through the gated host surface
    pr = host.prompts.ask(
        "Approve deploy?",
        [P.Choice.parse("approve:Approve:primary"), P.Choice.parse("reject:Reject:danger")],
        addresses=["web"], agent_id="agent_q", notify_agent=False,
    )
    assert pr.state == P.STATE_OPEN
    assert host.prompts.get(pr.id).body == "Approve deploy?"
    assert len(host.prompts.list(state="open")) == 1

    # respond through the host (as the dashboard button POST would)
    resolved = host.prompts.respond(pr.id, "approve", answered_by="web")
    assert resolved is not None and resolved.answer_choice == "approve"
    assert host.prompts.get(pr.id).state == P.STATE_ANSWERED
    # second response loses
    assert host.prompts.respond(pr.id, "reject", answered_by="web") is None


# ── expiry sweeper (prompts plugin worker) ──────────────────────────


def _prompts_host(tmp_path):
    from relaydeck.testing import MockHost

    host = MockHost(
        name="prompts",
        config_home=tmp_path,
        declared_capabilities={
            "channels.register", "prompts.read", "prompts.write",
            "cli.register", "api.register", "agents.list", "workers.spawn",
            "events.subscribe", "events.emit",
        },
    )
    open_db(str(tmp_path / "runtime" / "relaydeck.db")).close()
    return host


def test_sweeper_spawned_when_interval_positive(tmp_path, monkeypatch):
    import plugins.prompts.plugin as PP

    host = _prompts_host(tmp_path)
    plugin = PP.PromptsPlugin()
    monkeypatch.setattr(plugin, "_sweep_interval", lambda: 30.0)
    spawned = []

    def fake_spawn(name, fn, **kw):
        spawned.append((name, fn, kw))
        return types.SimpleNamespace(id="w1", name=name)

    monkeypatch.setattr(host.workers, "spawn", fake_spawn)
    plugin.on_load(host)
    assert spawned and spawned[0][0] == "expiry-sweeper"
    assert spawned[0][2].get("interval") == 30.0
    assert plugin._sweeper is not None


def test_sweeper_disabled_when_interval_zero(tmp_path, monkeypatch):
    import plugins.prompts.plugin as PP

    host = _prompts_host(tmp_path)
    plugin = PP.PromptsPlugin()
    monkeypatch.setattr(plugin, "_sweep_interval", lambda: 0.0)
    spawned = []
    monkeypatch.setattr(host.workers, "spawn", lambda *a, **k: spawned.append(1))
    plugin.on_load(host)
    assert spawned == []
    assert plugin._sweeper is None


def test_sweep_tick_expires_overdue(tmp_path):
    import plugins.prompts.plugin as PP

    host = _prompts_host(tmp_path)
    plugin = PP.PromptsPlugin()
    plugin.on_load(host)  # interval defaults to 0 in MockHost → no real thread
    C.register_channel(plugin._web)

    pr = host.prompts.ask(
        "Restart worker?", [P.Choice("ok")],
        addresses=["web"], agent_id="agent_x", expires_in=-1,
    )
    assert host.prompts.get(pr.id).state == P.STATE_OPEN
    plugin._sweep_tick()  # the worker entry point
    assert host.prompts.get(pr.id).state == P.STATE_EXPIRED


@pytest.mark.parametrize("setting,expected", [
    ("0", 0.0),       # disabled
    ("0.1", 5.0),     # floored — can't hammer SQLite
    ("3", 5.0),       # floored
    ("60", 60.0),     # honored as-is
    ("nonsense", 0.0),  # unparseable → disabled
])
def test_sweep_interval_floor(tmp_path, monkeypatch, setting, expected):
    import plugins.prompts.plugin as PP
    from relaydeck.plugin_settings import _env_key

    host = _prompts_host(tmp_path)
    plugin = PP.PromptsPlugin()
    plugin.host = host
    monkeypatch.setenv(_env_key("prompts", "sweep_interval_seconds"), setting)
    assert plugin._sweep_interval() == expected


def test_sweeper_stops_on_teardown(tmp_path, monkeypatch):
    import plugins.prompts.plugin as PP
    from relaydeck.workers import WorkerStatus, get_worker_registry

    host = _prompts_host(tmp_path)
    plugin = PP.PromptsPlugin()
    # Big interval: the worker ticks once on start, then idles on an
    # interruptible wait — so teardown returns promptly, no ticking races.
    monkeypatch.setattr(plugin, "_sweep_interval", lambda: 3600.0)
    plugin.on_load(host)
    assert plugin._sweeper is not None
    worker = get_worker_registry().get(plugin._sweeper.id)
    assert worker is not None
    host.workers.teardown()  # what host.teardown() calls on plugin unload
    assert worker.status == WorkerStatus.STOPPED
