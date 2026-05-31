"""Telegram conversation purge + orphaned-route cleanup.

Covers the gap "conversations aren't deletable / no purge": the
ConversationRegistry delete/purge methods, and the agent/workspace-delete
hooks that prune routes which pointed at a now-gone target.
"""

from __future__ import annotations

from plugins.telegram.conversations import ConversationRegistry
from plugins.telegram.routes import Route, RouteTable, load_table, save_table


# ── registry deletion ───────────────────────────────────────────────


def _reg(tmp_path):
    return ConversationRegistry(tmp_path / "conversations.json")


def test_delete_one_and_persist(tmp_path):
    reg = _reg(tmp_path)
    reg.record(connection_id="default", chat_id=111)
    reg.record(connection_id="default", chat_id=222)

    assert reg.delete("default", 111) is True
    assert reg.delete("default", 111) is False           # already gone
    assert {c.chat_id for c in reg.list()} == {222}

    reloaded = _reg(tmp_path)
    reloaded.load()
    assert {c.chat_id for c in reloaded.list()} == {222}  # change hit disk


def test_delete_chat_across_connections(tmp_path):
    reg = _reg(tmp_path)
    reg.record(connection_id="bot1", chat_id=-100123)     # same group, two bots
    reg.record(connection_id="bot2", chat_id=-100123)
    reg.record(connection_id="bot1", chat_id=999)

    assert reg.delete_chat(-100123) == 2
    assert {c.chat_id for c in reg.list()} == {999}


def test_delete_chat_scoped_to_one_connection(tmp_path):
    reg = _reg(tmp_path)
    reg.record(connection_id="bot1", chat_id=-100123)
    reg.record(connection_id="bot2", chat_id=-100123)

    assert reg.delete_chat(-100123, connection_id="bot1") == 1
    assert {(c.connection_id, c.chat_id) for c in reg.list()} == {("bot2", -100123)}


def test_purge_all_is_idempotent(tmp_path):
    reg = _reg(tmp_path)
    reg.record(connection_id="default", chat_id=1)
    reg.record(connection_id="default", chat_id=2)

    assert reg.purge_all() == 2
    assert reg.list() == []
    assert reg.purge_all() == 0


# ── orphaned-route cleanup on agent/workspace deletion ──────────────


def _plugin(tmp_path, routes):
    from plugins.telegram.plugin import TelegramPlugin

    home = tmp_path
    save_table(home, RouteTable(routes=routes))

    class _Events:
        def subscribe(self, *a, **k):
            return lambda: None

        def emit(self, *a, **k):
            pass

    class _Host:
        config_home = home
        events = _Events()
        settings = None  # _open_access guards None defensively

    p = TelegramPlugin()
    p.host = _Host()
    p.table = load_table(home)
    p.restart_worker = lambda: {"ok": True}  # don't spawn a real worker in tests
    return p, home


def _event(**data):
    class E:
        pass
    e = E()
    e.data = data
    return e


def test_agent_deleted_prunes_only_its_routes(tmp_path):
    routes = [
        Route(workspace="demo", chat_id=111, agent="reviewer"),   # pruned
        Route(workspace="demo", chat_id=222, agent="keeper"),     # kept (other agent)
        Route(workspace="other", chat_id=333, agent="reviewer"),  # kept (other ws)
    ]
    p, home = _plugin(tmp_path, routes)

    p._on_agent_deleted(_event(agent_id="reviewer", workspace="demo"))

    after = load_table(home).routes
    assert {(r.workspace, r.agent) for r in after} == {
        ("demo", "keeper"), ("other", "reviewer"),
    }


def test_agent_deleted_without_workspace_matches_by_agent(tmp_path):
    """If the event carries no workspace, fall back to agent-name match."""
    routes = [
        Route(workspace="demo", chat_id=1, agent="gone"),
        Route(workspace="x", chat_id=2, agent="stays"),
    ]
    p, home = _plugin(tmp_path, routes)

    p._on_agent_deleted(_event(agent_id="gone"))

    after = load_table(home).routes
    assert [r.agent for r in after] == ["stays"]


def test_workspace_removed_prunes_all_its_routes(tmp_path):
    routes = [
        Route(workspace="demo", chat_id=1, agent="a"),
        Route(workspace="demo", chat_id=2, agent="b"),
        Route(workspace="keep", chat_id=3, agent="c"),
    ]
    p, home = _plugin(tmp_path, routes)

    p._on_workspace_removed(_event(workspace="demo"))

    after = load_table(home).routes
    assert [r.workspace for r in after] == ["keep"]


def test_no_prune_when_nothing_matches(tmp_path):
    routes = [Route(workspace="demo", chat_id=1, agent="a")]
    p, home = _plugin(tmp_path, routes)

    p._on_agent_deleted(_event(agent_id="ghost", workspace="demo"))

    assert len(load_table(home).routes) == 1
