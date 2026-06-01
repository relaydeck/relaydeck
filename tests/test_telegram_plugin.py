"""
Telegram plugin tests.

These cover the parts that don't require python-telegram-bot:

  - Route table parsing (telegram.yaml ↔ RouteTable)
  - Route precedence (chat_id+thread_id+command > chat_id+thread_id > …)
  - Allowlist gates (private chats → user allowlist, groups → group allowlist)
  - Atomic 0600 writes
  - Slash-command parser
  - Mention-detection helper

PTB-dependent paths (worker lifecycle, polling, getMe, webhook dispatch)
are integration-tested separately. `python-telegram-bot` ships with
relaydeck as a core dependency, so it's present on a normal install. The
plugin still fails closed if the import is somehow broken — the worker
stays absent and the status snapshot explains how to fix it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from plugins.telegram.routes import (
    Route,
    RouteTable,
    load_table,
    save_table,
)
from plugins.telegram.worker import (
    message_mentions_bot,
    parse_command,
)

# ── Route precedence ────────────────────────────────────────────────


def _table(*routes: Route) -> RouteTable:
    return RouteTable(routes=list(routes))


def test_chat_id_only_route_matches_any_thread_or_command():
    t = _table(Route(workspace="demo", chat_id=123))
    assert t.match_inbound(123) == [t.routes[0]]
    assert t.match_inbound(123, thread_id=7) == [t.routes[0]]
    assert t.match_inbound(123, command="triage") == [t.routes[0]]
    assert t.match_inbound(999) == []


def test_any_chat_wildcard_route_matches_inbound():
    # chat_id None = wildcard: matches any chat (a catch-all default route).
    wild = Route(workspace="demo")
    t = _table(wild)
    assert t.match_inbound(123) == [wild]
    assert t.match_inbound(999, thread_id=7) == [wild]
    assert t.match_inbound(-100123, command="triage") == [wild]


def test_explicit_chat_route_beats_wildcard():
    wild = Route(workspace="ws", agent="catchall")          # any chat
    specific = Route(workspace="ws", chat_id=123, agent="vip")
    t = _table(wild, specific)
    # Both match chat 123, but the explicit chat_id (specificity 1) outranks
    # the wildcard (0), so the plugin fires only the specific route.
    matches = t.match_inbound(123)
    assert matches[0] is specific
    # A different chat hits only the wildcard.
    assert t.match_inbound(555) == [wild]


def test_command_route_more_specific_than_chat_only():
    chat_only = Route(workspace="demo", chat_id=123, agent="generic")
    cmd_route = Route(workspace="demo", chat_id=123, command="triage", agent="triager")
    t = _table(chat_only, cmd_route)
    matches = t.match_inbound(123, command="triage")
    # Higher specificity first; both still in the list so caller can
    # decide. The plugin only fires the top.
    assert matches[0] is cmd_route
    assert matches[1] is chat_only


def test_thread_route_more_specific_than_chat_only():
    chat_only = Route(workspace="demo", chat_id=123)
    thread = Route(workspace="demo", chat_id=123, thread_id=42, agent="indexer")
    t = _table(chat_only, thread)
    matches = t.match_inbound(123, thread_id=42)
    assert matches[0] is thread


def test_thread_plus_command_beats_thread_alone():
    thread = Route(workspace="demo", chat_id=123, thread_id=42)
    thread_cmd = Route(workspace="demo", chat_id=123, thread_id=42, command="ack")
    t = _table(thread, thread_cmd)
    matches = t.match_inbound(123, thread_id=42, command="ack")
    assert matches[0] is thread_cmd


def test_multiple_chat_only_routes_broadcast():
    a = Route(workspace="ws1", chat_id=123, agent="a")
    b = Route(workspace="ws2", chat_id=123, agent="b")
    t = _table(a, b)
    matches = t.match_inbound(123)
    assert {r.agent for r in matches} == {"a", "b"}


def test_thread_route_does_not_match_when_thread_differs():
    r = Route(workspace="demo", chat_id=123, thread_id=42)
    t = _table(r)
    assert t.match_inbound(123, thread_id=99) == []
    # Threaded route doesn't match an un-threaded message either.
    assert t.match_inbound(123, thread_id=None) == []


def test_command_route_only_fires_for_that_command():
    cmd = Route(workspace="demo", chat_id=123, command="triage")
    other_cmd = Route(workspace="demo", chat_id=123, command="ack")
    t = _table(cmd, other_cmd)
    assert t.match_inbound(123, command="triage") == [cmd]
    assert t.match_inbound(123, command="ack") == [other_cmd]
    assert t.match_inbound(123, command="unknown") == []


# ── Allowlists ──────────────────────────────────────────────────────


def test_user_allowlist_required_for_dms():
    t = RouteTable(allowed_users={111})
    assert t.is_user_allowed(111) is True
    assert t.is_user_allowed(222) is False


def test_group_allowlist_required_for_groups():
    t = RouteTable(allowed_groups={-100123456789})
    assert t.is_chat_allowed(-100123456789) is True
    assert t.is_chat_allowed(-100987654321) is False


def test_empty_allowlist_is_fail_closed():
    """An operator who forgot to populate allowlists shouldn't have
    their bot accepting messages from the entire internet. We refuse
    everyone until at least one entry exists."""
    t = RouteTable()
    assert t.is_user_allowed(1) is False
    assert t.is_chat_allowed(-1) is False


# ── Inbound vs outbound filtering ───────────────────────────────────


def test_direction_filters_inbound_outbound():
    in_only = Route(workspace="demo", chat_id=1, direction="in")
    out_only = Route(workspace="demo", chat_id=2, agent="bot", direction="out")
    both = Route(workspace="demo", chat_id=3, direction="in+out")
    t = _table(in_only, out_only, both)
    inbound = t.inbound_routes
    outbound = t.outbound_routes
    assert in_only in inbound and both in inbound and out_only not in inbound
    assert out_only in outbound and both in outbound and in_only not in outbound


def test_outbound_match_filters_by_workspace_and_agent():
    a = Route(workspace="ws1", chat_id=1, agent="alice", direction="out")
    b = Route(workspace="ws1", chat_id=2, agent="bob", direction="out")
    c = Route(workspace="ws2", chat_id=3, agent="alice", direction="out")
    t = _table(a, b, c)
    assert t.match_outbound(workspace="ws1", agent="alice") == [a]
    assert t.match_outbound(workspace="ws1", agent="bob") == [b]
    assert t.match_outbound(workspace="ws2", agent="alice") == [c]


# ── Persistence: atomic + 0600 ──────────────────────────────────────


def test_load_missing_file_returns_empty(tmp_path):
    assert load_table(tmp_path).routes == []


def test_save_then_load_roundtrip(tmp_path):
    t = RouteTable(
        allowed_users={111, 222},
        allowed_groups={-100123456789},
        routes=[
            Route(workspace="demo", chat_id=111, agent="review"),
            Route(workspace="relaydeck", chat_id=-100123456789, command="triage",
                  agent="triager", direction="in"),
        ],
    )
    save_table(tmp_path, t)
    again = load_table(tmp_path)
    assert again.allowed_users == {111, 222}
    assert again.allowed_groups == {-100123456789}
    assert [r.chat_id for r in again.routes] == [111, -100123456789]
    assert again.routes[1].command == "triage"
    assert again.routes[1].direction == "in"


def test_save_writes_0600(tmp_path):
    """Regression: the route file may contain chat IDs of private
    groups + bootstrap user IDs; keep it 0600 to match the auth-token
    and preferences contracts. Force a permissive umask so a missing
    fchmod would show up immediately."""
    prev = os.umask(0)
    try:
        save_table(tmp_path, RouteTable(allowed_users={1}))
    finally:
        os.umask(prev)
    p = tmp_path / "telegram.yaml"
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600, f"telegram.yaml wrote at {oct(mode)}, expected 0o600"


def test_invalid_direction_falls_back_to_in_out(tmp_path):
    p = tmp_path / "telegram.yaml"
    p.write_text("""
allowed_users: [1]
routes:
  - workspace: ws
    chat_id: 9
    direction: weird
""")
    t = load_table(tmp_path)
    assert t.routes[0].direction == "in+out"


def test_bool_chat_id_is_not_accepted(tmp_path):
    """A historical pytfall: PyYAML happily decodes `chat_id: true`
    into the integer 1 because bool is a subclass of int. Reject."""
    p = tmp_path / "telegram.yaml"
    p.write_text("""
routes:
  - workspace: ws
    chat_id: true
""")
    t = load_table(tmp_path)
    assert t.routes[0].chat_id is None


# ── Command + mention parsing ───────────────────────────────────────


def test_parse_command_extracts_command_and_body():
    cmd, who, body = parse_command("/triage urgent\nplease look")
    assert cmd == "triage"
    assert who is None
    assert body == "urgent\nplease look"


def test_parse_command_handles_mention_form():
    cmd, who, body = parse_command("/ack@otherbot got it")
    assert cmd == "ack"
    assert who == "otherbot"
    assert body == "got it"


def test_parse_command_returns_none_for_plain_text():
    cmd, who, body = parse_command("hi there")
    assert cmd is None
    assert who is None
    assert body == "hi there"


def test_mention_helper_is_case_insensitive():
    assert message_mentions_bot("@RelaydeckBot please review", "relaydeckbot") is True
    assert message_mentions_bot("hey @otherbot", "relaydeckbot") is False
    assert message_mentions_bot("", "relaydeckbot") is False
    assert message_mentions_bot("@relaydeckbot", "") is False


# ── Plugin manifest sanity ──────────────────────────────────────────


def test_plugin_manifest_parses_with_tile_alias():
    from relaydeck.plugin_manifest import load_manifest

    p = (Path(__file__).resolve().parent.parent
         / "plugins" / "telegram" / "plugin.toml")
    m = load_manifest(p)
    assert m.name == "telegram"
    assert m.workspace_scoped is False
    assert m.top_level_cli is True
    assert "vault.read" in m.declared_capabilities
    assert "vault.write" in m.declared_capabilities
    assert "vault.delete" in m.declared_capabilities
    assert "TELEGRAM_BOT_TOKEN" in m.needs_vault
    assert "TELEGRAM_BOT_TOKEN_*" in m.needs_vault
    # Must load AFTER vault: telegram reads its token from the vault at
    # on_load, so the registry has to topo-sort vault first (else telegram
    # stubs "token unset" on startup until a manual restart).
    assert "vault" in m.dependencies
    # Tab promoted to a rail lens
    assert any(t.id == "telegram" and t.default_state == "lens" for t in m.ui_tabs)
    # Tile contribution survives the `tiles` alias
    assert any(t.id == "chats" for t in m.ui_agent_tiles)


def test_skill_md_present_for_materialization():
    p = Path(__file__).resolve().parent.parent / "plugins" / "telegram" / "SKILL.md"
    assert p.is_file()
    body = p.read_text()
    assert "relaydeck telegram reply" in body
    assert "[relay from=telegram:" in body
    # Must be a valid skill (frontmatter with name + description) or the
    # harnesses + Skills lens will skip it.
    assert body.startswith("---\n")
    from relaydeck import skills as _skills
    valid, errors, _w = _skills.validate_skill_dir(p.parent)
    assert valid, errors


def test_telegram_skill_target_workspaces_hook(tmp_path, monkeypatch):
    """The provider hook the skills manager calls returns exactly the
    workspaces a route currently points at."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram.plugin import TelegramPlugin
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    plugin = TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)
    plugin.table = RouteTable(routes=[Route(workspace="routed", chat_id=1)])
    assert plugin.skill_target_workspaces(["routed", "other"]) == ["routed"]


def test_telegram_skill_materialized_via_manager(tmp_path, monkeypatch):
    """End-to-end: the generic skills manager materializes the
    relaydeck-telegram skill into a routed workspace using the real plugin."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from types import SimpleNamespace

    from relaydeck.config import register_workspace
    from plugins.skills import manager
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    register_workspace(cfg_home, "routed", tmp_path / "routed", [])
    register_workspace(cfg_home, "other", tmp_path / "other", [])

    plugin = tg.TelegramPlugin()
    plugin.on_load(MockHost(name="telegram", config_home=cfg_home))
    plugin.table = RouteTable(routes=[Route(workspace="routed", chat_id=1)])

    plugin_dir = Path(tg.__file__).parent
    reg = SimpleNamespace(all=lambda: [SimpleNamespace(
        name="telegram", instance=plugin,
        manifest=SimpleNamespace(skills={"relaydeck-telegram": "SKILL.md"}),
        path=plugin_dir)])
    manager.sync_all(cfg_home, registry=reg)

    skill_rel = Path("runtime") / "skills" / "relaydeck-telegram" / "SKILL.md"
    routed = cfg_home / "workspaces" / "routed" / skill_rel
    other = cfg_home / "workspaces" / "other" / skill_rel
    assert routed.exists()
    assert not other.exists()


# ── PTB-not-available smoke ─────────────────────────────────────────


def test_worker_require_ptb_raises_useful_error(monkeypatch):
    """PTB is a core dependency now, so an import failure means a broken
    install. We still want a clear error (not a bare ImportError) that
    points at reinstalling — never at the old optional extras. The CLI /
    status surface relies on this string."""
    import builtins

    from plugins.telegram import worker as worker_mod
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("telegram"):
            raise ImportError("simulated: telegram not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(worker_mod.PTBNotAvailable) as excinfo:
        worker_mod.require_ptb()
    message = str(excinfo.value)
    assert "python-telegram-bot" in message
    assert "reinstall relaydeck" in message
    assert "relaydeck update" in message
    # No longer advertise the optional extras — telegram ships in core.
    assert "relaydeck[telegram]" not in message
    assert "relaydeck-plugins[telegram]" not in message


def test_dashboard_has_no_telegram_extra_install_hints():
    """Telegram ships in core now, so the panel must not advertise the old
    optional-extra install commands (they named three different packages and
    confused operators)."""
    panel = Path("plugins/telegram/static/panel.js").read_text()

    assert "relaydeck[telegram]" not in panel
    assert "relaydeck-plugins[telegram]" not in panel
    assert "--extra telegram" not in panel


# ── Web monitoring: activity feed + health + per-route mention ───────


def test_record_activity_buffer_shape_and_dispositions():
    from plugins.telegram.plugin import TelegramPlugin
    p = TelegramPlugin()
    base = {"chat_id": -100, "chat_type": "group", "user_id": 7,
            "user_name": "sam", "thread_id": None, "command": None, "body": "hi"}
    p._record_activity(base, "rejected_user")
    p._record_activity({**base, "body": "go"}, "routed", target=["bob", "ann"])
    acts = list(p._activity)
    assert [a["disposition"] for a in acts] == ["rejected_user", "routed"]
    last = acts[-1]
    assert last["target"] == ["bob", "ann"]
    assert last["user_id"] == 7 and last["chat_id"] == -100
    assert last["preview"] == "go" and "ts" in last


def test_record_activity_buffer_is_bounded():
    from plugins.telegram.plugin import TelegramPlugin
    p = TelegramPlugin()
    for i in range(500):
        p._record_activity({"chat_id": i, "user_id": i, "body": "x"}, "unrouted")
    assert len(p._activity) == 200          # deque maxlen caps it
    assert list(p._activity)[-1]["chat_id"] == 499


def test_record_activity_truncates_long_preview():
    from plugins.telegram.plugin import TelegramPlugin
    p = TelegramPlugin()
    p._record_activity({"chat_id": 1, "user_id": 1, "body": "z" * 500}, "routed")
    prev = list(p._activity)[-1]["preview"]
    assert len(prev) <= 121 and prev.endswith("…")


def test_require_mention_roundtrips(tmp_path):
    """A per-route require_mention override survives save/load (the web
    form + API PUT can now set it; previously YAML-only)."""
    t = RouteTable(routes=[
        Route(workspace="w", chat_id=-100, require_mention=True),
        Route(workspace="w", chat_id=-200, require_mention=False),
        Route(workspace="w", chat_id=-300),   # inherit (None)
    ])
    save_table(tmp_path, t)
    again = load_table(tmp_path)
    by_chat = {r.chat_id: r.require_mention for r in again.routes}
    assert by_chat[-100] is True
    assert by_chat[-200] is False
    assert by_chat[-300] is None


def test_parse_table_accepts_require_mention():
    """The API PUT /routes path (body → _parse_table) honors require_mention."""
    from plugins.telegram.routes import _parse_table
    table = _parse_table({
        "allowed_users": [1],
        "routes": [{"workspace": "w", "chat_id": -100, "require_mention": True}],
    })
    assert table.routes[0].require_mention is True


def _telegram_ctx(
    *,
    chat_id: int = -100,
    body: str = "hello",
    mention_ok: bool = False,
    mentions_bot: bool = False,
) -> dict:
    return {
        "chat_id": chat_id,
        "chat_type": "supergroup",
        "chat_title": "Ops",
        "chat_username": "",
        "thread_id": None,
        "user_id": 123,
        "user_name": "alice",
        "message_id": 7,
        "is_private": False,
        "mention_ok": mention_ok,
        "mentions_bot": mentions_bot,
        "command": None,
        "body": body,
        "reactions_enabled": False,
        "bot": None,
        "raw_message": None,
    }


def _loaded_telegram_plugin(tmp_path):
    from plugins.telegram.plugin import TelegramPlugin
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    plugin = TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    host.add_agent(id="agent-a", workspace="w", status="running")
    plugin.on_load(host)
    plugin._react = lambda *a, **kw: None
    plugin._maybe_start_typing = lambda *a, **kw: None
    return plugin, host


def test_route_can_relax_global_group_mention_requirement(tmp_path):
    plugin, host = _loaded_telegram_plugin(tmp_path)
    plugin.table = RouteTable(
        allowed_groups={-100},
        routes=[Route(workspace="w", chat_id=-100, agent="agent-a", require_mention=False)],
    )

    plugin._handle_inbound(_telegram_ctx(mention_ok=False, mentions_bot=False))

    assert host.memory_orchestrator.sent
    assert host.memory_orchestrator.sent[0]["to"] == "agent-a"


def test_route_can_require_group_mention_when_global_setting_is_relaxed(tmp_path):
    plugin, host = _loaded_telegram_plugin(tmp_path)
    plugin.table = RouteTable(
        allowed_groups={-100},
        routes=[Route(workspace="w", chat_id=-100, agent="agent-a", require_mention=True)],
    )

    plugin._handle_inbound(_telegram_ctx(mention_ok=True, mentions_bot=False))
    assert host.memory_orchestrator.sent == []

    plugin._handle_inbound(_telegram_ctx(mention_ok=True, mentions_bot=True))
    assert len(host.memory_orchestrator.sent) == 1
    assert host.memory_orchestrator.sent[0]["to"] == "agent-a"


def test_auto_allow_route_chats_ignores_outbound_only_routes():
    from plugins.telegram.plugin import TelegramPlugin

    plugin = TelegramPlugin()
    plugin.table = RouteTable(routes=[
        Route(workspace="w", chat_id=123, direction="out"),
        Route(workspace="w", chat_id=-100, direction="in"),
        Route(workspace="w", chat_id=-200, direction="in+out"),
    ])

    assert plugin._auto_allow_route_chats() is True
    assert 123 not in plugin.table.allowed_users
    assert plugin.table.allowed_groups == {-100, -200}


def test_status_snapshot_does_not_mark_disabled_connection_as_worker_error(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as telegram_plugin
    from plugins.telegram.plugin import TelegramPlugin
    from plugins.telegram.routes import Connection, RouteTable, save_table
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    monkeypatch.setattr(vault_plugin, "_vault", None)
    vault_plugin.set_secret("TG_DISABLED", "111:aaa")
    save_table(cfg_home, RouteTable(
        connections=[
            Connection(id="disabled", token_vault_key="TG_DISABLED", enabled=False),
        ],
    ))
    monkeypatch.setattr(telegram_plugin, "_running_as_daemon", lambda: True)

    plugin = TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)

    snap = plugin.status_snapshot()
    conn = snap["connections"][0]
    assert conn["enabled"] is False
    assert conn["stub_reason"] is None
    assert snap["errored"] is False


def test_worker_info_exposes_health_fields():
    """info() carries last_update_ts + last_error for the dashboard health
    line (constructible without PTB)."""
    from plugins.telegram.worker import TelegramWorker
    w = TelegramWorker(
        bot_token="x", on_text=lambda **k: None,
        require_mention_in_groups=True, reactions=True, poll_timeout_s=30.0,
    )
    info = w.info()
    assert "last_update_ts" in info and "last_error" in info
    assert info["last_update_ts"] is None and info["last_error"] is None


def test_status_snapshot_marks_worker_error_not_starting(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram.plugin import TelegramPlugin
    from plugins.telegram.routes import Connection, RouteTable, save_table
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    monkeypatch.setattr(vault_plugin, "_vault", None)
    vault_plugin.set_secret("TG_ERR", "111:aaa")
    save_table(cfg_home, RouteTable(
        connections=[Connection(id="errbot", token_vault_key="TG_ERR")],
    ))

    class ErrorWorker:
        def info(self):
            return {
                "ready": False,
                "bot_username": "",
                "bot_id": 0,
                "last_update_ts": None,
                "last_error": "getMe failed",
            }

    plugin = TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)
    plugin.workers["errbot"] = ErrorWorker()

    snap = plugin.status_snapshot()
    assert snap["errored"] is True
    assert snap["ready"] is False
    assert snap["connections"][0]["bot"]["last_error"] == "getMe failed"


def test_setup_endpoint_writes_token_to_vault(tmp_path, monkeypatch):
    """Regression: POST /api/plugins/telegram/setup must not 500.

    It imports `plugins.vault.plugin.set_secret` to persist the bot
    token — that symbol previously did not exist, so the endpoint (and the
    `relaydeck telegram setup` CLI + the webhook-secret endpoint) raised
    ImportError → 500. Drives the collected route handler directly; no PTB.
    """
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram.plugin import TelegramPlugin
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    # Force the vault to lazy-init under the test's HOME (not a leaked global).
    monkeypatch.setattr(vault_plugin, "_vault", None)

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    plugin = TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)

    handler = next(r["handler"] for r in host.api.routes if r["path"] == "/setup")
    result = asyncio.run(handler({"token": "123456:ABC", "allowed_users": [42]}))

    assert isinstance(result, dict)  # no exception / no 500
    assert vault_plugin.get_secret("TELEGRAM_BOT_TOKEN") == "123456:ABC"
    assert 42 in plugin.table.allowed_users


def test_setup_endpoint_user_id_optional_and_autonames_default(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    monkeypatch.setattr(vault_plugin, "_vault", None)
    monkeypatch.setattr(tg, "_getme",
                        lambda token, timeout=8.0: (True, "deckbot", 42, None))

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)

    handler = next(r["handler"] for r in host.api.routes if r["path"] == "/setup")
    result = asyncio.run(handler({"token": "123456:ABC"}))

    assert result["bot_username"] == "deckbot"
    assert plugin.table.allowed_users == set()
    assert [c.id for c in plugin.table.connections] == ["default"]
    assert plugin.table.connections[0].name == "deckbot"


def test_setup_endpoint_rejects_missing_token(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from fastapi import HTTPException

    from plugins.telegram.plugin import TelegramPlugin
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    plugin = TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)

    handler = next(r["handler"] for r in host.api.routes if r["path"] == "/setup")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(handler({"token": "   "}))
    assert exc.value.status_code == 400


# ── open_access (bypass allowlist) ──────────────────────────────────


def test_open_access_off_is_fail_closed():
    t = RouteTable()  # empty allowlists, open_access defaults False
    assert t.is_user_allowed(42) is False
    assert t.is_chat_allowed(-100123) is False


def test_open_access_on_allows_anyone():
    t = RouteTable(open_access=True)
    assert t.is_user_allowed(42) is True
    assert t.is_chat_allowed(-100123) is True


def test_open_access_setting_flows_to_table(tmp_path, monkeypatch):
    """The plugin reads the `open_access` setting and applies it to the live
    route table (on load + on /config PUT) so the gate honors it."""
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram.plugin import TelegramPlugin
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    plugin = TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)
    # default: off → fail-closed
    assert plugin.table.open_access is False
    assert plugin.table.is_user_allowed(999) is False

    # flip it via the /config PUT endpoint (mirrors the dashboard toggle)
    put = next(r["handler"] for r in host.api.routes
              if r["path"] == "/config" and "PUT" in r["methods"])
    asyncio.run(put({"open_access": True}))
    assert plugin.table.open_access is True
    assert plugin.table.is_user_allowed(999) is True

    # and the GET reflects it
    get = next(r["handler"] for r in host.api.routes
              if r["path"] == "/config" and "GET" in r["methods"])
    cfg = asyncio.run(get())
    assert cfg["open_access"] is True


def test_bool_settings_preserve_explicit_false(tmp_path, monkeypatch):
    """False is a meaningful Telegram setting value; it must not be folded
    through `value or True` back to the default."""
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram.plugin import TelegramPlugin, _bool_setting
    from relaydeck.plugin_settings import set_settings
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    set_settings("telegram", {
        "require_mention_in_groups": False,
        "reactions": False,
        "typing_indicator": False,
    })
    plugin = TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)

    get = next(r["handler"] for r in host.api.routes
              if r["path"] == "/config" and "GET" in r["methods"])
    cfg = asyncio.run(get())
    assert cfg["require_mention_in_groups"] is False
    assert cfg["reactions"] is False
    assert _bool_setting(host, "typing_indicator", True) is False


# ── daemon-only worker guard (getUpdates Conflict prevention) ───────


def test_running_as_daemon_keys_off_serve(monkeypatch):
    import sys

    from plugins.telegram.plugin import _running_as_daemon

    monkeypatch.setattr(sys, "argv", ["relaydeck", "serve", "--port", "8765"])
    assert _running_as_daemon() is True
    monkeypatch.setattr(sys, "argv", ["relaydeck", "chat", "agent-a"])
    assert _running_as_daemon() is False


def test_running_as_daemon_accepts_pid_file_owner(tmp_path, monkeypatch):
    import os
    import sys

    from plugins.telegram.plugin import _running_as_daemon

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["relaydeck", "chat", "agent-a"])
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    (cfg / "daemon.pid").write_text(str(os.getpid()))
    assert _running_as_daemon() is True


def test_worker_not_started_outside_serve_process(tmp_path, monkeypatch):
    """on_load runs in every CLI process; with a token present but argv not
    `serve`, the bot poller must NOT start (else two getUpdates pollers
    collide → Telegram 'Conflict')."""
    import sys

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["relaydeck", "chat", "agent-a"])
    from plugins.telegram.plugin import TelegramPlugin
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    # A token IS present, so the only thing keeping the worker down is the guard.
    monkeypatch.setattr(vault_plugin, "_vault", None)
    vault_plugin.set_secret("TELEGRAM_BOT_TOKEN", "123456:ABC")

    plugin = TelegramPlugin()
    plugin.on_load(MockHost(name="telegram", config_home=cfg_home))
    assert not plugin.workers  # not the daemon → no pollers


def test_worker_not_started_twice_on_repeated_on_load(tmp_path, monkeypatch):
    """Plugin reload in the daemon process must not spawn duplicate pollers."""
    import sys

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["relaydeck", "serve"])
    from plugins.telegram import plugin as tg_plugin
    from plugins.telegram.plugin import TelegramPlugin
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    class _StubWorker:
        def __init__(self, **kwargs):
            pass

        def run(self):
            return None

    monkeypatch.setattr(tg_plugin, "TelegramWorker", _StubWorker)
    monkeypatch.setattr(tg_plugin, "require_ptb", lambda: object())

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    monkeypatch.setattr(vault_plugin, "_vault", None)
    vault_plugin.set_secret("TELEGRAM_BOT_TOKEN", "123456:ABC")

    spawn_calls = {"n": 0}
    host = MockHost(name="telegram", config_home=cfg_home)

    def fake_spawn(*args, **kwargs):
        spawn_calls["n"] += 1
        return object()

    host.workers.spawn = fake_spawn

    plugin = TelegramPlugin()
    plugin.on_load(host)
    assert spawn_calls["n"] == 1
    assert plugin.workers
    plugin.on_load(host)
    assert spawn_calls["n"] == 1


# ── connections (multi-bot) ─────────────────────────────────────────


def test_connections_parse_and_match_scoping():
    from plugins.telegram.routes import Connection, _parse_table

    data = {
        "connections": [
            {"id": "main", "name": "Main", "token_vault_key": "TG_MAIN", "enabled": True},
            {"id": "ops", "token_vault_key": "TG_OPS", "enabled": False},
        ],
        "routes": [
            {"connection": "ops", "chat_id": 5, "workspace": "ws", "agent": "a"},
            {"chat_id": 5, "workspace": "ws", "agent": "any"},  # connection=None → any bot
        ],
    }
    t = _parse_table(data)
    assert t.connections == [
        Connection(id="main", name="Main", token_vault_key="TG_MAIN", enabled=True),
        Connection(id="ops", name="", token_vault_key="TG_OPS", enabled=False),
    ]
    # From the "main" connection: only the connection-agnostic route matches.
    m_main = t.match_inbound(5, connection="main")
    assert [r.agent for r in m_main] == ["any"]
    # From "ops": both the ops-scoped route and the agnostic one match.
    m_ops = {r.agent for r in t.match_inbound(5, connection="ops")}
    assert m_ops == {"a", "any"}


def test_connections_roundtrip_and_backcompat(tmp_path):
    from plugins.telegram.routes import (
        Connection,
        Route,
        RouteTable,
        load_table,
        save_table,
    )

    # With connections → persisted + reloaded.
    t = RouteTable(
        connections=[Connection(id="main", token_vault_key="TG_MAIN")],
        routes=[Route(workspace="ws", chat_id=1, agent="a", connection="main")],
    )
    save_table(tmp_path, t)
    again = load_table(tmp_path)
    assert again.connections == [Connection(id="main", token_vault_key="TG_MAIN")]
    assert again.routes[0].connection == "main"

    # Legacy table (no connections) → empty list, and the key is NOT written.
    save_table(tmp_path, RouteTable(routes=[Route(workspace="ws", chat_id=2)]))
    assert load_table(tmp_path).connections == []
    assert "connections:" not in (tmp_path / "telegram.yaml").read_text()


def test_multi_connection_spawns_one_worker_each(tmp_path, monkeypatch):
    """In the daemon, each enabled connection gets its own worker (its own
    token/poller) — keyed by connection id. No real PTB: TelegramWorker and
    workers.spawn are faked."""
    import sys

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["relaydeck", "serve"])  # daemon
    from plugins.telegram import plugin as tg
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    monkeypatch.setattr(vault_plugin, "_vault", None)
    vault_plugin.set_secret("TG_MAIN", "111:aaa")
    vault_plugin.set_secret("TG_OPS", "222:bbb")
    (cfg_home / "telegram.yaml").write_text(
        "connections:\n"
        "  - id: main\n    token_vault_key: TG_MAIN\n"
        "  - id: ops\n    token_vault_key: TG_OPS\n"
        "  - id: spare\n    token_vault_key: TG_OFF\n    enabled: false\n"
        "routes: []\n"
    )

    class FakeWorker:
        def __init__(self, **kw):
            self.kw = kw
            self._loop = None
            self._app = None

        def run(self, *a, **k):
            return None

        def info(self):
            return {"bot_username": "", "bot_id": 0}

    monkeypatch.setattr(tg, "TelegramWorker", FakeWorker)
    monkeypatch.setattr(tg, "require_ptb", lambda: object())
    host = MockHost(name="telegram", config_home=cfg_home)
    monkeypatch.setattr(host.workers, "spawn", lambda *a, **k: object())

    plugin = tg.TelegramPlugin()
    plugin.on_load(host)

    # `spare` is disabled; `main` + `ops` get workers.
    assert set(plugin.workers) == {"main", "ops"}
    snap = plugin.status_snapshot()
    assert {c["id"] for c in snap["connections"]} == {"main", "ops", "spare"}
    # Each worker was bound to its connection id via the on_text partial.
    assert plugin.workers["main"].kw["on_text"].keywords == {"connection_id": "main"}


def test_missing_ptb_reports_stub_before_worker_spawn(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["relaydeck", "serve"])
    from plugins.telegram import plugin as tg
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    monkeypatch.setattr(vault_plugin, "_vault", None)
    vault_plugin.set_secret("TELEGRAM_BOT_TOKEN", "111:aaa")
    monkeypatch.setattr(
        tg,
        "require_ptb",
        lambda: (_ for _ in ()).throw(tg.PTBNotAvailable("python-telegram-bot missing")),
    )
    host = MockHost(name="telegram", config_home=cfg_home)
    spawned = []
    monkeypatch.setattr(host.workers, "spawn", lambda *a, **k: spawned.append(a))

    plugin = tg.TelegramPlugin()
    plugin.on_load(host)

    snap = plugin.status_snapshot()
    assert spawned == []
    assert snap["stub_reason"] == "python-telegram-bot missing"
    assert snap["connections"][0]["stub_reason"] == "python-telegram-bot missing"


def _tg_route(host, path, method):
    return next(r["handler"] for r in host.api.routes
                if r["path"] == path and method in r["methods"])


def test_connections_add_endpoint_autonames_and_emits(tmp_path, monkeypatch):
    """POST /connections with no name defaults the display name to the bot's
    @username (so the lens never shows the bare id), and emits
    `telegram.connection.added` so the dashboard updates without a reload."""
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    monkeypatch.setattr(vault_plugin, "_vault", None)
    monkeypatch.setattr(tg, "_getme",
                        lambda token, timeout=8.0: (True, "opsbot", 7, None))

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)

    seen: list = []
    host.events.subscribe("telegram.*", seen.append)

    res = asyncio.run(_tg_route(host, "/connections", "POST")(
        {"id": "ops", "token": "222:bbb"}))

    assert res["ok"] is True and res["name"] == "opsbot"
    conn = next(c for c in plugin.table.connections if c.id == "ops")
    assert conn.name == "opsbot"
    assert any(e.type == "telegram.connection.added"
               and e.data.get("connection") == "ops" for e in seen)


def test_connection_rename_endpoint_materializes_default(tmp_path, monkeypatch):
    """PATCH /connections/<id> renames; for the implicit default it
    materializes an explicit connection so the name persists. Emits
    `telegram.connection.changed`; no token or restart required."""
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    monkeypatch.setattr(vault_plugin, "_vault", None)
    vault_plugin.set_secret("TELEGRAM_BOT_TOKEN", "111:aaa")

    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)
    assert plugin.table.connections == []  # only the synthesized default

    seen: list = []
    host.events.subscribe("telegram.*", seen.append)

    res = asyncio.run(_tg_route(host, "/connections/{conn_id}", "PATCH")(
        "default", {"name": "TestBot"}))

    assert res == {"ok": True, "id": "default", "name": "TestBot"}
    conn = next(c for c in plugin.table.connections if c.id == "default")
    assert conn.name == "TestBot"
    assert conn.token_vault_key == "TELEGRAM_BOT_TOKEN"
    assert any(e.type == "telegram.connection.changed"
               and e.data.get("name") == "TestBot" for e in seen)


def test_connection_remove_emits_event(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from plugins.telegram.routes import Connection
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)
    plugin.table.connections = [Connection(id="ops", token_vault_key="TG_OPS")]
    plugin.persist_table()

    seen: list = []
    host.events.subscribe("telegram.*", seen.append)

    res = asyncio.run(_tg_route(host, "/connections/{conn_id}", "DELETE")("ops"))
    assert res["ok"] is True
    assert any(e.type == "telegram.connection.removed"
               and e.data.get("connection") == "ops" for e in seen)


def test_worker_status_callback_emits_connection_changed(tmp_path, monkeypatch):
    """`_on_worker_status` bridges a poller's ready/error transition onto the
    bus so the lens flips 'starting…' → 'connected' live."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg_home)
    plugin.on_load(host)

    seen: list = []
    host.events.subscribe("telegram.*", seen.append)
    plugin._on_worker_status("ready", {"bot_username": "opsbot"},
                             connection_id="ops")

    e = next(e for e in seen if e.type == "telegram.connection.changed")
    assert e.data["connection"] == "ops"
    assert e.data["state"] == "ready"
    assert e.data["bot_username"] == "opsbot"


def test_orchestrator_bridges_telegram_events_to_sse(tmp_path):
    """`telegram.*` plugin-bus events are forwarded onto the SSE `_bus` so the
    Telegram lens updates in real time (the regression behind 'I need to
    reload the page')."""
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True)

    import relaydeck.orchestrator as _orch_mod
    from relaydeck.orchestrator import _bus, get_orchestrator
    from relaydeck.plugin import Event, PluginEventBus

    _orch_mod._orchestrator = None
    orch = get_orchestrator(cfg)

    q = _bus.subscribe("*")
    try:
        bus = PluginEventBus()
        orch.set_event_bus(bus)
        bus.emit(Event(type="telegram.connection.changed",
                       data={"connection": "ops", "state": "ready"},
                       source_plugin="telegram"))
        ev = q.get(timeout=2.0)
    finally:
        _bus.unsubscribe("*", q)

    assert ev["type"] == "telegram.connection.changed"
    assert ev["payload"]["connection"] == "ops"


def test_no_synthesized_default_when_token_unset(tmp_path, monkeypatch):
    """With no explicit connections AND no default token there is no bot, so
    `effective_connections` is empty — no phantom 'default · token missing'
    row. Once a token exists the legacy single-bot default reappears."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    monkeypatch.setattr(vault_plugin, "_vault", None)  # vault empty → no token

    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)

    assert plugin.effective_connections() == []
    assert plugin.status_snapshot()["connections"] == []

    vault_plugin.set_secret("TELEGRAM_BOT_TOKEN", "111:aaa")
    assert [c.id for c in plugin.effective_connections()] == ["default"]


def test_normalize_parse_mode():
    from plugins.telegram.plugin import _infer_parse_mode, _normalize_parse_mode
    assert _normalize_parse_mode("html") == "HTML"
    assert _normalize_parse_mode("HTML") == "HTML"
    assert _normalize_parse_mode("markdown") == "MarkdownV2"
    assert _normalize_parse_mode("md") == "MarkdownV2"
    assert _normalize_parse_mode("plain") is None
    assert _normalize_parse_mode("") is None
    assert _normalize_parse_mode(None) is None
    assert _infer_parse_mode("Hello <b>joker0</b>", None) == "HTML"
    assert _infer_parse_mode("plain text", None) is None
    assert _infer_parse_mode("plain", "html") == "HTML"


def test_typing_starts_for_live_inbound(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)

    class FakeWorker:
        def send_typing(self, chat_id, *, thread_id=None, timeout=8.0):
            return True

    plugin.workers = {"default": FakeWorker()}  # type: ignore[dict-item]
    ctx = {"connection_id": "default", "chat_id": 8283066035, "thread_id": None}
    plugin._maybe_start_typing(ctx)
    assert plugin._typing.active_keys() == ["default:8283066035:0"]


def test_typing_disabled_by_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)
    monkeypatch.setattr(plugin, "_typing_enabled", lambda: False)
    plugin.workers = {"default": object()}  # type: ignore[dict-item]
    plugin._maybe_start_typing(
        {"connection_id": "default", "chat_id": 1, "thread_id": None},
    )
    assert plugin._typing.active_keys() == []


def test_send_reply_stops_typing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    plugin.on_load(MockHost(name="telegram", config_home=cfg))
    plugin._typing.start(
        lambda: True,
        connection_id="default",
        chat_id=42,
    )
    assert plugin._typing.active_keys()

    class FakeWorker:
        def send_text(
            self, chat_id, body, *, thread_id=None,
            reply_to_message_id=None, parse_mode=None,
        ):
            return {"ok": True, "message_id": 1}

    plugin.workers = {"default": FakeWorker()}  # type: ignore[dict-item]
    res = plugin.send_reply(42, "done")
    assert res["ok"]
    assert plugin._typing.active_keys() == []


def test_inbound_map_register_and_lookup(tmp_path):
    from plugins.telegram.inbound_map import InboundMap

    path = tmp_path / "inbound-map.json"
    m = InboundMap(path)
    ctx = {
        "chat_id": 8283066035,
        "message_id": 17,
        "thread_id": 8,
        "connection_id": "default",
    }
    m.register("msg_abc123", ctx)
    meta = m.lookup("msg_abc123")
    assert meta is not None
    assert meta.chat_id == 8283066035
    assert meta.telegram_message_id == 17
    assert meta.thread_id == 8
    assert meta.connection_id == "default"

    m2 = InboundMap(path)
    m2.load()
    meta2 = m2.lookup("msg_abc123")
    assert meta2 is not None
    assert meta2.telegram_message_id == 17


def test_send_reply_resolves_in_reply_to(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)
    assert plugin._inbound_map is not None
    plugin._inbound_map.register("msg_in", {
        "chat_id": 8283066035,
        "message_id": 55,
        "thread_id": None,
        "connection_id": "default",
    })

    captured: dict = {}

    class FakeWorker:
        def send_text(
            self, chat_id, body, *, thread_id=None,
            reply_to_message_id=None, parse_mode=None,
        ):
            captured.update(
                chat_id=chat_id, body=body, thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
            )
            return {"ok": True, "message_id": 99}

    plugin.workers = {"default": FakeWorker()}  # type: ignore[dict-item]
    res = plugin.send_reply(
        8283066035, "hi", in_reply_to="msg_in",
    )
    assert res["ok"]
    assert captured["reply_to_message_id"] == 55


def test_reply_endpoint_in_reply_to(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)
    assert plugin._inbound_map is not None
    plugin._inbound_map.register("msg_in", {
        "chat_id": 42,
        "message_id": 9,
        "thread_id": 3,
        "connection_id": "default",
    })

    captured: dict = {}

    def fake_send_reply(
        chat_id, body, *, thread_id=None, connection_id=None,
        parse_mode=None, in_reply_to=None,
    ):
        captured.update(
            chat_id=chat_id, in_reply_to=in_reply_to, thread_id=thread_id,
        )
        return {"ok": True, "message_id": 1}

    plugin.send_reply = fake_send_reply  # type: ignore[method-assign]
    res = asyncio.run(_tg_route(host, "/reply", "POST")({
        "chat_id": 42, "body": "ok", "in_reply_to": "msg_in",
    }))
    assert res["ok"]
    assert captured["in_reply_to"] == "msg_in"


def test_reply_endpoint_maps_format_to_parse_mode(tmp_path, monkeypatch):
    """POST /reply with `format: html` reaches send_reply as parse_mode=HTML
    (so the agent's `<b>…</b>` actually renders instead of showing literally)."""
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)

    captured: dict = {}

    def fake_send_reply(chat_id, body, *, thread_id=None, connection_id=None,
                        parse_mode=None, in_reply_to=None):
        captured.update(parse_mode=parse_mode, body=body)
        return {"ok": True, "message_id": 1}

    plugin.send_reply = fake_send_reply  # type: ignore[method-assign]
    res = asyncio.run(_tg_route(host, "/reply", "POST")(
        {"chat_id": 42, "body": "<b>hi</b>", "format": "html"}))
    assert res["ok"] and captured["parse_mode"] == "HTML"


def test_send_reply_forwards_parse_mode_to_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)

    captured: dict = {}

    class FakeWorker:
        def send_text(
            self, chat_id, body, *, thread_id=None,
            reply_to_message_id=None, parse_mode=None,
        ):
            captured.update(parse_mode=parse_mode)
            return {"ok": True, "message_id": 7}

    plugin.workers = {"default": FakeWorker()}  # type: ignore[dict-item]
    res = plugin.send_reply(42, "<b>x</b>", parse_mode="HTML")
    assert res["ok"] and captured["parse_mode"] == "HTML"


def test_worker_send_text_downgrades_to_plain_on_parse_error(tmp_path):
    """A malformed-markup send (parse_mode set) must not be lost: send_text
    retries once as plain text and flags `downgraded`."""
    import asyncio
    import threading

    from plugins.telegram.worker import TelegramWorker

    # Run a real loop in a background thread (send_text submits onto it).
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    calls: list = []

    class FakeBot:
        async def send_message(self, **kw):
            calls.append(kw.get("parse_mode"))
            if kw.get("parse_mode"):              # first attempt (HTML) fails
                raise RuntimeError("can't parse entities")
            return type("M", (), {"message_id": 99})()  # plain retry succeeds

    w = TelegramWorker(bot_token="x", on_text=lambda *a, **k: None,
                       require_mention_in_groups=False, reactions=False,
                       poll_timeout_s=1.0)
    w._loop = loop
    w._app = type("App", (), {"bot": FakeBot()})()
    try:
        res = w.send_text(42, "<b>bad", parse_mode="HTML")
    finally:
        loop.call_soon_threadsafe(loop.stop)

    assert res["ok"] and res.get("downgraded") is True
    assert res["message_id"] == 99
    assert calls == ["HTML", None]   # tried HTML, then plain


# ── conversation registry (global, auto-discovered) ─────────────────


def test_conversation_registry_record_persist_reload(tmp_path):
    from plugins.telegram.conversations import ConversationRegistry

    path = tmp_path / "conversations.json"
    reg = ConversationRegistry(path)
    reg.load()
    reg.record(connection_id="main", chat_id=123, chat_type="private",
               user="alice", disposition="routed")
    reg.record(connection_id="main", chat_id=123, chat_type="private",
               user="alice", disposition="unrouted")          # same chat → upsert
    reg.record(connection_id="ops", chat_id=-100, chat_type="supergroup",
               title="Eng", thread_id=7, disposition="routed")  # different connection

    lst = reg.list()
    assert len(lst) == 2
    main = reg.get("main", 123)
    assert main.message_count == 2
    assert main.title == "alice"            # DM title falls back to the sender
    ops = reg.get("ops", -100)
    assert ops.title == "Eng" and ops.thread_ids == [7]

    # Persisted to disk and reloads.
    reg2 = ConversationRegistry(path)
    reg2.load()
    assert reg2.get("main", 123).message_count == 2
    assert {c.key for c in reg2.list()} == {"main:123", "ops:-100"}


def test_record_activity_feeds_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram.plugin import TelegramPlugin
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = TelegramPlugin()
    plugin.on_load(MockHost(name="telegram", config_home=cfg))
    plugin._record_activity(
        {"connection_id": "main", "chat_id": 42, "chat_type": "private",
         "user_name": "bob", "chat_title": "", "chat_username": "", "body": "hi"},
        "unrouted",
    )
    convs = plugin.conversations.list()
    assert len(convs) == 1
    assert convs[0].chat_id == 42 and convs[0].connection_id == "main"


def test_connections_api_add_and_remove(tmp_path, monkeypatch):
    """POST /connections verifies + stores a bot token, adds the connection
    (materializing the legacy default so it isn't dropped), and persists.
    DELETE removes it. _getme is mocked (no network)."""
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from plugins.telegram.routes import load_table
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    monkeypatch.setattr(tg, "_getme", lambda token, timeout=8.0: (True, "ops_bot", 999, None))
    monkeypatch.setattr(vault_plugin, "_vault", None)
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    # An existing single-bot default (token set) — adding a 2nd bot must
    # materialize it so it isn't silently dropped once `connections:` exists.
    vault_plugin.set_secret("TELEGRAM_BOT_TOKEN", "111:aaa")
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)

    add = next(r["handler"] for r in host.api.routes
              if r["path"] == "/connections" and "POST" in r["methods"])
    res = asyncio.run(add({"id": "ops", "token": "222:bbb", "name": "Ops"}))
    assert res["ok"] and res["bot_username"] == "ops_bot"
    assert vault_plugin.get_secret("TELEGRAM_BOT_TOKEN_OPS") == "222:bbb"
    conn_ids = {c.id for c in load_table(cfg).connections}
    assert {"default", "ops"} <= conn_ids   # default materialized, ops added

    # bad token → rejected, nothing persisted
    monkeypatch.setattr(tg, "_getme", lambda token, timeout=8.0: (False, None, None, "401"))
    bad = asyncio.run(add({"id": "nope", "token": "x"}))
    assert bad["ok"] is False
    assert "nope" not in {c.id for c in load_table(cfg).connections}

    rm = next(r["handler"] for r in host.api.routes
             if r["path"] == "/connections/{conn_id}" and "DELETE" in r["methods"])
    res2 = asyncio.run(rm("ops"))
    assert res2["ok"]
    assert "ops" not in {c.id for c in load_table(cfg).connections}


def test_routes_put_preserves_connections(tmp_path, monkeypatch):
    """A dashboard route save sends allowlists+routes but no connections —
    it must NOT wipe the configured bots."""
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from plugins.telegram.routes import (
        Connection,
        RouteTable,
        load_table,
        save_table,
    )
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    save_table(cfg, RouteTable(connections=[Connection(id="ops", token_vault_key="TG_OPS")]))
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)

    put = next(r["handler"] for r in host.api.routes
             if r["path"] == "/routes" and "PUT" in r["methods"])
    asyncio.run(put({"allowed_users": [1], "allowed_groups": [],
                     "routes": [{"workspace": "ws", "chat_id": 5}]}))
    again = load_table(cfg)
    assert [c.id for c in again.connections] == ["ops"]   # preserved
    assert len(again.routes) == 1
    assert 5 in again.allowed_users


def test_routes_put_auto_allowlists_private_and_group_chats(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from plugins.telegram.routes import load_table
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)

    put = next(r["handler"] for r in host.api.routes
             if r["path"] == "/routes" and "PUT" in r["methods"])
    asyncio.run(put({
        "allowed_users": [],
        "allowed_groups": [],
        "routes": [
            {"workspace": "ws", "chat_id": 123},
            {"workspace": "ws", "chat_id": -100456},
            {"workspace": "ws"},
        ],
    }))

    again = load_table(cfg)
    assert again.allowed_users == {123}
    assert again.allowed_groups == {-100456}
    assert len(again.routes) == 3


def test_on_load_auto_allowlists_existing_concrete_routes(tmp_path):
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    save_table(cfg, RouteTable(routes=[
        Route(workspace="ws", chat_id=123),
        Route(workspace="ws", chat_id=-100456),
        Route(workspace="ws"),
    ]))

    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)

    again = load_table(cfg)
    assert again.allowed_users == {123}
    assert again.allowed_groups == {-100456}


# ── multi-bot review fixes ──────────────────────────────────────────


def test_connection_webhook_url_is_per_connection():
    from plugins.telegram.plugin import _connection_webhook_url
    assert _connection_webhook_url("https://h/api/plugins/telegram/webhook", "ops") == \
        "https://h/api/plugins/telegram/webhook?connection=ops"
    assert _connection_webhook_url("https://h/wh?x=1", "main") == "https://h/wh?x=1&connection=main"
    assert _connection_webhook_url("", "main") == ""


def test_send_reply_infers_connection_from_registry(tmp_path, monkeypatch):
    """With multiple bots and no explicit connection, send_reply infers the
    bot from the conversation registry (which bot has seen the chat)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    plugin.on_load(MockHost(name="telegram", config_home=cfg))

    sent = {}

    class FakeWorker:
        def __init__(self, cid):
            self.cid = cid

        def send_text(
            self, chat_id, body, thread_id=None, parse_mode=None,
            reply_to_message_id=None,
        ):
            sent["cid"] = self.cid
            return {"ok": True, "message_id": 1}

    plugin.workers = {"main": FakeWorker("main"), "ops": FakeWorker("ops")}
    # chat 555 was seen on "ops"
    plugin.conversations.record(connection_id="ops", chat_id=555, chat_type="private")

    res = plugin.send_reply(555, "hi")          # no connection given
    assert res["ok"] and sent["cid"] == "ops"   # inferred ops
    # an unknown chat with 2 bots stays ambiguous
    res2 = plugin.send_reply(999, "hi")
    assert res2["ok"] is False and "ambiguous" in res2["error"]


def test_send_reply_explicit_connection_must_be_running(tmp_path, monkeypatch):
    """An explicit --connection/send selector must not silently fall back to a
    different bot when that connection is configured but not running."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    plugin.on_load(MockHost(name="telegram", config_home=cfg))

    class FakeWorker:
        def send_text(
            self, chat_id, body, thread_id=None, parse_mode=None,
            reply_to_message_id=None,
        ):
            raise AssertionError("must not send from fallback worker")

    plugin.workers = {"main": FakeWorker()}
    res = plugin.send_reply(555, "hi", connection_id="ops")
    assert res["ok"] is False
    assert "ops" in res["error"] and "not running" in res["error"]


def test_delete_implicit_default_clears_vault_token(tmp_path, monkeypatch):
    """Deleting the implicit default has no YAML row to drop, so it clears the
    bot's vault token instead (mirroring the dashboard's delete button) and
    emits `telegram.connection.removed` with `token_cleared`."""
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    monkeypatch.setattr(vault_plugin, "_vault", None)
    vault_plugin.set_secret("TELEGRAM_BOT_TOKEN", "111:aaa")

    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)   # no explicit connections → synthesized default

    seen: list = []
    host.events.subscribe("telegram.*", seen.append)

    rm = _tg_route(host, "/connections/{conn_id}", "DELETE")
    res = asyncio.run(rm("default"))

    assert res["ok"] is True and res["token_cleared"] is True
    assert vault_plugin.get_secret("TELEGRAM_BOT_TOKEN") is None
    assert any(e.type == "telegram.connection.removed"
               and e.data.get("token_cleared") is True for e in seen)


def test_delete_unknown_connection_is_rejected(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    host = MockHost(name="telegram", config_home=cfg)
    plugin.on_load(host)
    rm = _tg_route(host, "/connections/{conn_id}", "DELETE")
    res = asyncio.run(rm("nope"))
    assert res["ok"] is False and "unknown connection" in res["error"]


def test_activity_record_carries_connection_id(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plugin = tg.TelegramPlugin()
    plugin.on_load(MockHost(name="telegram", config_home=cfg))
    plugin._record_activity(
        {"connection_id": "ops", "chat_id": 5, "chat_type": "private",
         "user_name": "x", "chat_title": "", "chat_username": "", "body": "hi"},
        "unrouted",
    )
    assert plugin._activity[-1]["connection_id"] == "ops"


def test_status_reports_per_connection_token_flags(tmp_path, monkeypatch):
    """status connections expose has_token + explicit so the lens can show
    token status and gate edit/remove."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.telegram import plugin as tg
    from plugins.vault import plugin as vault_plugin
    from relaydeck.testing import MockHost

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    monkeypatch.setattr(vault_plugin, "_vault", None)
    vault_plugin.set_secret("TELEGRAM_BOT_TOKEN", "1:a")   # default has a token
    plugin = tg.TelegramPlugin()
    plugin.on_load(MockHost(name="telegram", config_home=cfg))
    d = next(c for c in plugin.status_snapshot()["connections"] if c["id"] == "default")
    assert d["has_token"] is True
    assert d["explicit"] is False   # synthesized default → not removable


def test_panel_unique_chats_preserves_topics():
    """The send picker should keep separate rows for forum topics in the same
    chat instead of collapsing by connection+chat only."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    # The send-picker dedup logic lives in the dependency-free conversations.js
    # (panel.js is now a light-DOM Lit module importing '@relaydeck/ui', so it
    # can't load in bare node); panel._uniqueChats() delegates to it.
    script = """
      import { uniqueChats } from './plugins/telegram/static/conversations.js';
      const convs = [{ connection_id: 'ops', chat_id: -100, title: 'Eng', thread_ids: [7, 9] }];
      const routes = { routes: [
        { connection: 'ops', chat_id: -100, thread_id: 11, workspace: 'ws' },
        { connection: 'main', chat_id: -100, thread_id: 7, workspace: 'ws' },
      ] };
      console.log(JSON.stringify(uniqueChats(convs, routes)));
    """
    out = subprocess.check_output(
        [node, "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parent.parent,
        text=True,
    )
    rows = json.loads(out)
    keys = {
        (r["connection"], r["chat_id"], r.get("thread_id"))
        for r in rows
    }
    assert ("ops", -100, None) in keys
    assert ("ops", -100, 7) in keys
    assert ("ops", -100, 9) in keys
    assert ("ops", -100, 11) in keys
    assert ("main", -100, 7) in keys


def test_route_chat_id_resolves_from_picked_row_not_hidden_field():
    """Regression: picking an existing chat in the Add-route form must save that
    chat's chat_id, NOT chat_id=null (a wildcard route).

    The manual chat_id <input> is hidden unless the picker is on "Custom", so
    when a real chat is picked the draft's chatId is '' — reading it gave
    parseInt('')=NaN → chat_id=null, silently wildcarding the route. The fix
    makes the picked catalog row authoritative (resolveRouteChatId), so the
    chosen chat's id is persisted. This reproduces the original bug if reverted.
    """
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    script = """
      import { resolveRouteChatId } from './plugins/telegram/static/conversations.js';
      const row = { connection: 'ops', chat_id: -1001234, thread_ids: [] };
      const out = {
        // real chat picked, manual field empty (the bug case) → row is authoritative
        picked:   resolveRouteChatId({ chatPick: 'ops|-1001234', chatId: '' }, row),
        // wildcard
        any:      resolveRouteChatId({ chatPick: '__any__', chatId: '' }, null),
        // custom id typed
        custom:   resolveRouteChatId({ chatPick: '__custom__', chatId: '-100777' }, null),
        // custom selected but nothing typed → caller must reject
        missing:  resolveRouteChatId({ chatPick: '__custom__', chatId: '' }, null),
        // real chat picked but its row is gone AND no manual id → reject, don't wildcard
        pickedMissing: resolveRouteChatId({ chatPick: 'ops|-1001234', chatId: '' }, null),
      };
      console.log(JSON.stringify(out));
    """
    out = json.loads(subprocess.check_output(
        [node, "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parent.parent,
        text=True,
    ))
    assert out["picked"] == {"chat_id": -1001234}          # NOT null — the bug
    assert out["any"] == {"chat_id": None}
    assert out["custom"] == {"chat_id": -100777}
    assert out["missing"].get("missing") is True
    assert out["pickedMissing"].get("missing") is True
