"""
Bundled Telegram gateway.

Single PTB worker per daemon. Inbound Telegram messages route through
`~/.relaydeck/telegram.yaml` to workspace agents; outbound replies
go via the `relaydeck telegram reply` CLI or by subscribing the bot to
agent.message events.

## How it fits

  - On `on_load`, the plugin reads telegram.yaml, resolves the bot
    token via vault, and spawns one `telegram.bot` worker that runs
    PTB's polling loop in its own asyncio thread.
  - Inbound messages are filtered (allowlists, mention requirement,
    privacy mode) and matched against the route table. Matches call
    `orchestrator.send_message_to(agent, body, from_id="telegram:…")`
    — same path the messaging plugin uses.
  - Outbound: the `relaydeck telegram reply` CLI is the supported surface
    for now. A future enhancement subscribes to `agent.message` and
    forwards messages whose `to` starts with `telegram:`.
  - `relaydeck telegram status / routes / setup / reply / poll-once / auth`
    cover the operator UX. The route YAML is editable by hand;
    `routes add/rm` round-trips it atomically.

## Config

`~/.relaydeck/telegram.yaml`:

    allowed_users: [12345678]
    allowed_groups: [-100123456789]
    routes:
      - chat_id: 12345678
        workspace: demo
      - chat_id: -100123456789
        workspace: relaydeck
        agent: review

`~/.relaydeck/vault.yaml`:

    TELEGRAM_BOT_TOKEN: <token from BotFather>
"""

from __future__ import annotations

import collections
import contextlib
import functools
import logging
import re
import time
from typing import Any

from relaydeck.channels import Address, ChannelCapabilities, DeliveryResult
from relaydeck.sdk import Plugin, PluginContext, PluginEventBus, PluginHost

from .conversations import ConversationRegistry
from .routes import Connection, RouteTable, load_table, save_table
from .worker import PTBNotAvailable, TelegramWorker, require_ptb

# The `relaydeck-telegram` skill (declared in plugin.toml `[plugin.skills]`,
# named so it doesn't collide with messaging's `relaydeck-cli`) is
# materialized by the bundled `skills` plugin — see `skill_target_workspaces`.

PLUGIN_NAME = "telegram"
logger = logging.getLogger(__name__)


def _is_conn_error(resp: object) -> bool:
    return isinstance(resp, str) and (
        "URLError" in resp or "ConnectionRefused" in resp or "OSError" in resp
        or "timed out" in resp or "Connection refused" in resp
    )


def _post_daemon(path: str, payload: dict) -> tuple[bool, dict | str]:
    """POST to the running daemon (where the bot workers live). CLI commands
    that drive live bots (reply) must go through it; falls back to in-process
    when the daemon is unreachable. Mirrors github's `_post_daemon`."""
    import json
    import urllib.error
    import urllib.request

    from relaydeck.auth import read_token
    from relaydeck.state import get_daemon_ca, get_daemon_url

    base = get_daemon_url().rstrip("/")
    ctx = None
    if base.startswith("https://"):
        import ssl
        ca = get_daemon_ca()
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    headers = {"Content-Type": "application/json"}
    tok = read_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            raw = r.read()
            return True, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _connection_webhook_url(base: str, conn_id: str) -> str:
    """Per-connection webhook URL. Each bot registers its OWN URL with
    `?connection=<id>` so Telegram's POSTs carry the connection and the
    ingress handler dispatches to the right worker. Without this, every bot
    would register the same URL and updates would all hit the first worker."""
    if not base:
        return ""
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}connection={conn_id}"


def _slug_conn_id(value: str) -> str:
    """Normalize a connection id to a safe, stable handle: lowercased,
    `[a-z0-9_-]`, collapsed. Keeps ids out of YAML-bool territory (`on`/`off`)
    and filename/path trouble."""
    import re
    s = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()).strip("-")
    return s


def _normalize_parse_mode(fmt: str | None) -> str | None:
    """Map a human `--format`/`format` value to a Telegram `parse_mode`.
    `html` → "HTML", `markdown`/`md` → "MarkdownV2", everything else → None
    (plain text). HTML is the recommended mode (forgiving escape rules)."""
    f = (fmt or "").strip().lower()
    if f in ("html", "h"):
        return "HTML"
    if f in ("markdown", "md", "markdownv2", "mdv2"):
        return "MarkdownV2"
    return None


_HTML_TAG_RE = re.compile(
    r"<\s*/?\s*(b|i|u|s|code|pre|a|blockquote|tg-spoiler)\b",
    re.IGNORECASE,
)


def _infer_parse_mode(body: str, fmt: str | None) -> str | None:
    """Resolve Telegram parse_mode from an explicit format or body heuristics."""
    if fmt is not None and str(fmt).strip():
        return _normalize_parse_mode(fmt)
    if _HTML_TAG_RE.search(body):
        return "HTML"
    return None


def _bool_setting(host: Any, key: str, default: bool) -> bool:
    """Read a boolean plugin setting without turning explicit False into True.

    Settings can come from TOML/YAML, plugin defaults, or env vars; env values
    arrive as strings, so normalize common false strings here.
    """
    try:
        value = host.settings.get(key)
    except Exception:
        return default
    if value is None:
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("", "0", "false", "no", "off"):
            return False
        if v in ("1", "true", "yes", "on"):
            return True
    return bool(value)


def _getme(token: str, timeout: float = 8.0) -> tuple[bool, str | None, int | None, str | None]:
    """Direct Bot API getMe — verifies a token without starting a poller.
    Safe in any process (getMe is not getUpdates, so no Conflict). Returns
    (ok, username, bot_id, error)."""
    import json
    import urllib.error
    import urllib.request
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return False, None, None, f"HTTP {exc.code}: {body[:200]}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, None, None, f"{type(exc).__name__}: {exc}"
    if not data.get("ok"):
        return False, None, None, str(data.get("description") or "getMe failed")
    res = data.get("result") or {}
    return True, res.get("username"), res.get("id"), None


def _running_as_daemon() -> bool:
    """True only in the `relaydeck serve` (daemon) process.

    Plugins are loaded — and `on_load` runs — in every `relaydeck` CLI
    invocation (see `relaydeck/__init__.py:main`), but the Telegram getUpdates
    poller may run in only one process at a time. We key off the invoked
    subcommand being `serve` (also true for the `serve` subprocess that
    `relaydeck daemon start` spawns). As a backstop, also accept when this
    process owns the daemon PID file — covers edge cases where argv is
    rewritten but we're still the long-lived serve process."""
    import os
    import sys

    argv = sys.argv
    if len(argv) > 1 and argv[1] == "serve":
        return True
    try:
        from pathlib import Path

        from relaydeck.daemon import read_pid

        pid = read_pid(Path.home() / ".relaydeck")
        if pid is not None and pid == os.getpid():
            return True
    except Exception:
        pass
    return False


# Reserved slash-commands that ACT ON the routed agent instead of being
# forwarded to it as a chat message. Everything else (e.g. /triage) keeps
# routing as a normal message per the route table.
_RESERVED_COMMANDS = {
    "new", "clear", "fresh", "reset",  # → fresh session (drop --continue)
    "restart",                          # → restart PTY, keep history
    "screenshot",                       # → live terminal snapshot
    "stop", "status", "help",
}

# Aliases that all mean "start a fresh session".
_NEW_SESSION_ALIASES = {"new", "clear", "fresh", "reset"}

_HELP_TEXT = (
    "Control commands (act on the agent routed to this chat):\n"
    "/new (or /clear, /fresh, /reset) — start a fresh session\n"
    "/restart — restart the agent, keeping its history\n"
    "/screenshot — snapshot the agent's live terminal\n"
    "/stop — stop the agent\n"
    "/status — show the agent's state\n"
    "/help — this message\n"
    "Any other text is delivered to the agent."
)

# (command, description) pairs pushed to Telegram via setMyCommands on worker
# startup so typing `/` shows an autocomplete menu. A curated subset of the
# reserved commands — the extra fresh-session aliases still work when typed,
# they're just omitted here to keep the menu uncluttered.
_BOT_COMMAND_MENU: list[tuple[str, str]] = [
    ("new", "Start a fresh session (clears the agent's history)"),
    ("clear", "Start a fresh session (alias of /new)"),
    ("restart", "Restart the agent, keeping its history"),
    ("screenshot", "Snapshot the agent's live terminal"),
    ("stop", "Stop the agent"),
    ("status", "Show the agent's state"),
    ("help", "List these control commands"),
]


class TelegramPlugin(Plugin):
    description = (
        "Telegram gateway: route chats, groups, topics, and "
        "slash-commands to workspace agents."
    )

    def __init__(self) -> None:
        self.host: PluginHost | None = None
        self.table: RouteTable = RouteTable()
        # One bot per connection id. Multiple bots = multiple tokens =
        # multiple pollers (one getUpdates each, no Conflict).
        self.workers: dict[str, TelegramWorker] = {}
        self._worker_handles: dict[str, Any] = {}
        self._conn_stub: dict[str, str] = {}   # connection id → stub reason
        self._unsub_callbacks: list[Any] = []
        self._stub_reason: str | None = None   # aggregate (no worker came up)
        # Recent inbound activity (newest last). Powers the dashboard
        # feed: who messaged the bot + what happened (routed → which
        # agent / unrouted / rejected). Doubles as the onboarding aid —
        # a rejected/unrouted row carries the user/chat id, so the lens
        # offers one-click "allow + route". In-memory only (monitoring,
        # not an audit log); survives nothing but a daemon restart.
        self._activity: collections.deque = collections.deque(maxlen=200)
        # Global, persisted registry of every chat/group/channel seen across
        # all connections (Bot API can't enumerate them — we build it from
        # observed traffic). Set in on_load.
        self.conversations: ConversationRegistry | None = None
        self._inbound_map: InboundMap | None = None
        from plugins.telegram.typing_sessions import TypingController
        self._typing = TypingController()

    # ── activity feed (monitoring + onboarding) ─────────────────────

    def _record_activity(self, ctx: dict[str, Any], disposition: str,
                         target: list[str] | None = None) -> None:
        """Append one inbound update + its disposition to the ring buffer.
        `disposition` ∈ routed | unrouted | rejected_user | rejected_group
        | rejected_mention. Also upserts the global conversation registry."""
        if self.conversations is not None:
            with contextlib.suppress(Exception):
                self.conversations.record(
                    connection_id=str(ctx.get("connection_id") or "default"),
                    chat_id=int(ctx["chat_id"]),
                    chat_type=str(ctx.get("chat_type") or ""),
                    title=str(ctx.get("chat_title") or ""),
                    username=str(ctx.get("chat_username") or ""),
                    thread_id=ctx.get("thread_id"),
                    user=str(ctx.get("user_name") or ""),
                    disposition=disposition,
                )
        try:
            body = str(ctx.get("body") or "")
            self._activity.append({
                "ts": time.time(),
                "connection_id": ctx.get("connection_id"),
                "chat_id": ctx.get("chat_id"),
                "chat_type": ctx.get("chat_type"),
                "user_id": ctx.get("user_id"),
                "username": ctx.get("user_name"),
                "thread_id": ctx.get("thread_id"),
                "command": ctx.get("command"),
                "preview": (body[:120] + "…") if len(body) > 120 else body,
                "disposition": disposition,
                "target": list(target or []),
            })
        except Exception:
            logger.debug("telegram: activity record failed", exc_info=True)

    # ── lifecycle ───────────────────────────────────────────────────

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self.table = load_table(host.config_home)
        self.table.open_access = self._open_access()
        if self._auto_allow_route_chats():
            self.persist_table()
        plugin_data = host.config_home / "plugin-data" / "telegram"
        self.conversations = ConversationRegistry(plugin_data / "conversations.json")
        self.conversations.load()
        from plugins.telegram.inbound_map import InboundMap
        self._inbound_map = InboundMap(plugin_data / "inbound-map.json")
        self._inbound_map.load()
        self._register_cli()
        self._register_api()
        # Register Telegram as a messaging channel so interactive prompts
        # ([Approve]/[Reject]) can be fanned out to chats as inline
        # keyboards. Core never imports telegram — it speaks the
        # MessagingProvider contract through this registration.
        with contextlib.suppress(Exception):
            host.channels.register(TelegramChannelProvider(self))
        # HITL "operator home": deliver human-in-the-loop escalations to the
        # configured admin chat. The hitl plugin emits `hitl.escalation`; any
        # plugin can be a channel by subscribing. No-op until an admin chat id
        # is set, so this is safe to wire unconditionally.
        if host.events is not None:
            with contextlib.suppress(Exception):
                self._unsub_callbacks.append(
                    host.events.subscribe("hitl.escalation", self._on_hitl_escalation)
                )
            # Orphan cleanup: when an agent (or workspace) is deleted, prune the
            # routes that targeted it so telegram.yaml doesn't keep forwarding
            # to a dead agent. (workspace.removed is a declared topic; the
            # subscription is dormant until/unless it's emitted.)
            with contextlib.suppress(Exception):
                self._unsub_callbacks.append(
                    host.events.subscribe("agent.deleted", self._on_agent_deleted)
                )
            with contextlib.suppress(Exception):
                self._unsub_callbacks.append(
                    host.events.subscribe("workspace.removed", self._on_workspace_removed)
                )
        # Skill materialization is owned by the bundled `skills` plugin,
        # which discovers this plugin's `relaydeck-telegram` skill via
        # `[plugin.skills]` and our `skill_target_workspaces` hook.
        # PTB ships with relaydeck, so bot start normally just works. We
        # still fail closed if the import is somehow broken (partial
        # install) — the CLI/API surface stays usable so operators can
        # manage the route table and see the reason in status.
        self._maybe_start_worker()

    def on_unload(self) -> None:
        import contextlib as _ctxlib

        for unsub in self._unsub_callbacks:
            # Subscriber teardown is best-effort — a stale handler ref
            # shouldn't block plugin shutdown.
            with _ctxlib.suppress(Exception):
                unsub()
        self._unsub_callbacks.clear()
        if self._typing is not None:
            self._typing.stop_all()
        if self.host:
            self.host.workers.teardown()
        self.workers.clear()
        self._worker_handles.clear()
        self._conn_stub.clear()
        self._stub_reason = None

    def _bot_token_key_setting(self) -> str:
        if self.host is None:
            return "TELEGRAM_BOT_TOKEN"
        try:
            return str(self.host.settings.get("bot_token_vault_key") or "TELEGRAM_BOT_TOKEN")
        except Exception:
            return "TELEGRAM_BOT_TOKEN"

    def effective_connections(self) -> list[Connection]:
        """Configured connections, or a synthesized default for legacy
        single-bot setups (no `connections:` in telegram.yaml). The default
        reads the token from the `bot_token_vault_key` setting, so existing
        installs keep working with zero config.

        The default is surfaced ONLY when its token actually exists. With no
        token AND no explicit connections there is no bot — synthesizing a
        phantom "default" row (perpetually "token missing") just confuses;
        the empty state + setup card guide adding one instead."""
        if self.table.connections:
            return self.table.connections
        default = Connection(
            id="default", name="", token_vault_key=self._bot_token_key_setting(), enabled=True,
        )
        return [default] if self._resolve_token(default) else []

    def _resolve_token(self, conn: Connection) -> str | None:
        """Read a connection's bot token from the vault."""
        if self.host is None:
            return None
        try:
            return self.host.vault.get(conn.token_vault_key)
        except Exception:
            return None

    def _store_secret(self, key: str, value: str) -> None:
        if self.host is None:
            raise RuntimeError("telegram plugin host is not loaded")
        self.host.vault.set(key, value)

    def _delete_secret(self, key: str) -> bool:
        if self.host is None:
            return False
        return self.host.vault.delete(key)

    def _maybe_start_worker(self) -> None:
        if self.host is None:
            return
        # The Telegram getUpdates poller must run in EXACTLY ONE process — the
        # daemon (`relaydeck serve`). `on_load` (which calls this) also runs in
        # every transient CLI invocation, so without this guard a long-lived
        # CLI (`relaydeck chat`/`view`/`attach`) starts a SECOND poller per bot
        # and Telegram returns "Conflict: terminated by other getUpdates
        # request". The daemon's setup/config/restart endpoints re-enter here
        # in the serve process, so they still (re)start workers normally.
        if not _running_as_daemon():
            return
        # Idempotent on_load: plugin reload without unload must not spawn
        # a second getUpdates poller for the same connection.
        if self.workers:
            return
        host = self.host
        require_mention = _bool_setting(host, "require_mention_in_groups", True)
        reactions = _bool_setting(host, "reactions", True)
        try:
            poll_timeout_s = float(host.settings.get("poll_timeout_s") or 30.0)
        except Exception:
            poll_timeout_s = 30.0
        mode = str(host.settings.get("mode") or "polling")
        webhook_url = str(host.settings.get("webhook_url") or "")
        webhook_secret = self._webhook_secret()

        self._conn_stub.clear()
        for conn in self.effective_connections():
            if not conn.enabled:
                continue
            token = self._resolve_token(conn)
            if not token:
                self._conn_stub[conn.id] = (
                    f"vault key {conn.token_vault_key!r} is unset — run "
                    "`relaydeck telegram setup` to add the bot token."
                )
                logger.info("telegram[%s]: %s", conn.id, self._conn_stub[conn.id])
                continue
            try:
                require_ptb()
            except PTBNotAvailable as exc:
                self._conn_stub[conn.id] = str(exc)
                logger.info("telegram[%s]: %s", conn.id, exc)
                continue
            worker = TelegramWorker(
                bot_token=token,
                # Slash-command autocomplete menu (setMyCommands on startup).
                bot_commands=_BOT_COMMAND_MENU,
                # Bind the connection id so inbound messages know which bot
                # they arrived on (routing, reply, reactions, registry).
                on_text=functools.partial(self._handle_inbound, connection_id=conn.id),
                # Inline-keyboard button taps for interactive prompts.
                on_callback=functools.partial(self._handle_callback, connection_id=conn.id),
                require_mention_in_groups=require_mention,
                reactions=reactions,
                poll_timeout_s=poll_timeout_s,
                mode=mode,
                # Per-connection webhook URL so Telegram's POSTs identify the
                # bot (`?connection=<id>`) and ingress routes to this worker.
                webhook_url=_connection_webhook_url(webhook_url, conn.id),
                webhook_secret=webhook_secret,
                # Lifecycle → SSE: lets the dashboard flip "starting…" →
                # "connected" (or surface an error) live, no reload needed.
                on_status=functools.partial(
                    self._on_worker_status, connection_id=conn.id,
                ),
            )
            try:
                # interval=0 → the worker drives its own cadence (the PTB
                # asyncio loop runs continuously inside run()).
                self._worker_handles[conn.id] = host.workers.spawn(
                    f"bot:{conn.id}",
                    worker.run,
                    interval=0,
                    config={"mode": mode, "connection": conn.id},
                    description=(
                        f"Telegram bot poller for connection {conn.id!r} "
                        "(python-telegram-bot); routes inbound chats to agents."
                    ),
                )
            except PTBNotAvailable as exc:
                self._conn_stub[conn.id] = str(exc)
                logger.info("telegram[%s]: %s", conn.id, exc)
                continue
            self.workers[conn.id] = worker

        # Aggregate top-level stub reason (backward-compat): only when NO
        # worker came up at all.
        self._stub_reason = (
            next(iter(self._conn_stub.values()), None) if not self.workers else None
        )

    def _on_worker_status(
        self, state: str, info: dict[str, Any] | None = None, *,
        connection_id: str = "default",
    ) -> None:
        """A bot poller changed lifecycle state (ready/error). Forward it onto
        the plugin event bus as `telegram.connection.changed`; the orchestrator
        bridges `telegram.*` to the SSE feed, so the lens updates live."""
        if self.host is None:
            return
        with contextlib.suppress(Exception):
            self.host.events.emit("telegram.connection.changed", {
                "connection": connection_id,
                "state": state,
                **(info or {}),
            })

    # ── inbound dispatch ────────────────────────────────────────────

    def _handle_inbound(self, ctx: dict[str, Any], *, connection_id: str = "default") -> None:
        """Called from a worker thread (off the asyncio loop). Heavy
        lifting (orchestrator send_message_to) is allowed here.

        `connection_id` identifies which bot received the update (bound per
        worker), so routing/reply/reactions/registry are connection-aware."""
        host = self.host
        if host is None or host.events is None:
            return
        ctx["connection_id"] = connection_id

        # 1. Allowlists. Private chats use the user allowlist; groups
        #    use the group allowlist (plus the bot still respects
        #    privacy mode at the Telegram side).
        user_id = int(ctx["user_id"])
        chat_id = int(ctx["chat_id"])
        is_private = bool(ctx["is_private"])
        if is_private:
            if not self.table.is_user_allowed(user_id):
                logger.info(
                    "telegram: drop DM from un-allowlisted user_id=%s", user_id,
                )
                host.events.emit("telegram.update.rejected", {
                    "reason": "user not allowlisted",
                    "chat_id": chat_id, "user_id": user_id,
                })
                self._record_activity(ctx, "rejected_user")
                return
        else:
            if not self.table.is_chat_allowed(chat_id):
                logger.info(
                    "telegram: drop group message from un-allowlisted chat_id=%s",
                    chat_id,
                )
                host.events.emit("telegram.update.rejected", {
                    "reason": "chat not allowlisted",
                    "chat_id": chat_id, "user_id": user_id,
                })
                self._record_activity(ctx, "rejected_group")
                return

        # 2. Route lookup. Mention gating happens after matching so
        #    route-level `require_mention: false` can relax the global
        #    group default for a specific chat/topic/command.
        command = ctx["command"]
        thread_id = ctx["thread_id"]
        matches = self.table.match_inbound(
            chat_id, connection=connection_id, thread_id=thread_id, command=command,
        )
        if not matches:
            if not is_private and command is None and not ctx["mention_ok"]:
                logger.debug(
                    "telegram: group message without bot mention — ignored (chat_id=%s)",
                    chat_id,
                )
                self._record_activity(ctx, "rejected_mention")
                return
            host.events.emit("telegram.message.unrouted", {
                "chat_id": chat_id, "thread_id": thread_id,
                "command": command,
                "user_id": user_id,
                "user_name": ctx["user_name"],
            })
            self._record_activity(ctx, "unrouted")
            self._react(ctx, "❓")
            return

        # 4. Specificity: the highest-specificity routes fire; ties at
        #    chat_id-only level broadcast to all matching agents.
        top = matches[0].specificity()
        winners = [r for r in matches if r.specificity() == top]

        def _mention_allowed(route: Any) -> bool:
            if is_private or command is not None:
                return True
            if route.require_mention is False:
                return True
            if route.require_mention is True:
                return bool(ctx.get("mentions_bot"))
            return bool(ctx.get("mention_ok"))

        winners = [r for r in winners if _mention_allowed(r)]
        if not winners:
            host.events.emit("telegram.message.unrouted", {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "command": command,
                "reason": "matched routes but all gated by require_mention",
            })
            self._record_activity(ctx, "rejected_mention")
            return

        body = (ctx["body"] or "").strip()
        if command and not body:
            body = f"/{command}"

        # Reserved control commands act ON the routed agent (new session /
        # stop / status) instead of being forwarded as a chat message — this
        # is how an operator starts a fresh harness session from Telegram.
        # Other commands fall through to normal message routing below.
        if command in _RESERVED_COMMANDS:
            self._dispatch_command_action(command, winners, ctx)
            return

        sent_to: list[str] = []
        injected_live = False
        for route in winners:
            # Header that the agent sees alongside the body. Format
            # mirrors the messaging plugin's "[relay from=… id=…]"
            # — the receiving agent's SKILL.md teaches reply via
            # `relaydeck telegram reply <chat_id> "…"`.
            sender = f"telegram:{chat_id}"
            attribution = (
                f"@{ctx['user_name']} in {ctx['chat_type']}"
                + (f" topic={thread_id}" if thread_id else "")
                + (f" /{command}" if command else "")
            )
            outgoing = f"{attribution}: {body}" if body else attribution

            agents, injected = self._send_to_route(route, outgoing, sender, ctx)
            sent_to.extend(agents)
            injected_live = injected_live or injected

        if sent_to:
            host.events.emit("telegram.message.routed", {
                "chat_id": chat_id, "thread_id": thread_id,
                "command": command,
                "agents": sent_to,
                "user_id": user_id,
            })
            self._record_activity(ctx, "routed", target=sent_to)
            self._react(ctx, "👀")
            if injected_live:
                self._maybe_start_typing(ctx)
        else:
            host.events.emit("telegram.message.unrouted", {
                "chat_id": chat_id, "thread_id": thread_id,
                "command": command,
                "reason": "matched routes but delivery failed",
            })
            self._record_activity(ctx, "unrouted")

    def _send_to_route(
        self, route: Any, body: str, sender: str, ctx: dict[str, Any],
    ) -> tuple[list[str], bool]:
        """Deliver to one route. If `route.agent` is set, send to that
        agent. Otherwise broadcast to every agent in the workspace."""
        host = self.host
        if host is None or host._orchestrator is None:
            return [], False
        orch = host._orchestrator
        sent: list[str] = []
        injected_live = False
        if route.agent:
            try:
                msg_id, injected = orch.send_message_to(route.agent, body, from_id=sender)
                if self._inbound_map is not None:
                    self._inbound_map.register(msg_id, ctx)
                if injected:
                    injected_live = True
                sent.append(route.agent)
            except Exception as exc:
                logger.warning(
                    "telegram: send_message_to %s failed: %s", route.agent, exc,
                )
        else:
            from relaydeck.messages import new_broadcast_id
            broadcast_id = new_broadcast_id()
            for a in orch.list_agents():
                if (a.get("workspace") or "") != route.workspace:
                    continue
                try:
                    msg_id, injected = orch.send_message_to(
                        a["id"], body, from_id=sender, broadcast_id=broadcast_id,
                    )
                    if self._inbound_map is not None:
                        self._inbound_map.register(msg_id, ctx)
                    if injected:
                        injected_live = True
                    sent.append(a["id"])
                except Exception as exc:
                    logger.warning(
                        "telegram: broadcast send to %s failed: %s", a["id"], exc,
                    )
        return sent, injected_live

    # ── reserved control commands ───────────────────────────────────

    def _route_target_agents(self, winners: list[Any]) -> list[str]:
        """Resolve the agent id(s) the winning routes point at — the targets
        a control command acts on. Dedupes, preserves order."""
        host = self.host
        if host is None or host._orchestrator is None:
            return []
        orch = host._orchestrator
        targets: list[str] = []
        for route in winners:
            if route.agent:
                targets.append(route.agent)
            else:
                for a in orch.list_agents():
                    if (a.get("workspace") or "") == route.workspace:
                        targets.append(a["id"])
        return list(dict.fromkeys(targets))

    def _dispatch_command_action(
        self, command: str, winners: list[Any], ctx: dict[str, Any],
    ) -> None:
        """Run a reserved control command against the routed agent(s) and
        reply to the chat with the outcome. Forwarding is bypassed for these."""
        host = self.host
        chat_id = int(ctx["chat_id"])
        thread_id = ctx["thread_id"]
        conn = ctx.get("connection_id")

        if command == "help":
            self._reply_safe(chat_id, _HELP_TEXT, thread_id, conn)
            self._react(ctx, "✅")
            return

        targets = self._route_target_agents(winners)
        if not targets:
            self._reply_safe(chat_id, "No agent is routed to this chat.", thread_id, conn)
            self._react(ctx, "❓")
            return

        # /screenshot renders the live terminal and sends it as its own
        # monospace block per agent — it doesn't fit the one-line summary.
        if command == "screenshot":
            self._send_screenshots(targets, ctx)
            self._emit_command_event(command, targets, ctx)
            return

        orch = host._orchestrator if host else None
        errors: list[str] = []
        status_lines: list[str] = []
        for aid in targets:
            try:
                if command in _NEW_SESSION_ALIASES:
                    if orch:
                        orch.reset_agent_session(aid)
                elif command == "restart":
                    if orch:
                        orch.restart_agent(aid)
                elif command == "stop":
                    ok = bool(orch.stop_agent(aid)) if orch else False
                    status_lines.append(f"{aid}: {'stopped' if ok else 'was not running'}")
                elif command == "status":
                    status_lines.append(f"{aid}: {self._agent_status_line(aid)}")
            except Exception as exc:
                errors.append(f"{aid}: error — {exc}")

        # Compose the reply. Session/restart get the clean emoji confirmation;
        # stop/status report per-agent state.
        names = ", ".join(targets)
        if command in _NEW_SESSION_ALIASES:
            reply = "🆕 New session created." if len(targets) == 1 else f"🆕 New session created — {names}."
        elif command == "restart":
            reply = "🔄 Restarted." if len(targets) == 1 else f"🔄 Restarted — {names}."
        else:
            reply = "\n".join(status_lines) or "(no action)"
        if errors:
            reply = (reply + "\n" + "\n".join(errors)).strip()

        self._reply_safe(chat_id, reply, thread_id, conn)
        self._react(ctx, "✅")
        self._emit_command_event(command, targets, ctx)

    def _emit_command_event(self, command: str, targets: list[str], ctx: dict[str, Any]) -> None:
        host = self.host
        if host and host.events is not None:
            host.events.emit("telegram.command.dispatched", {
                "chat_id": int(ctx["chat_id"]), "command": command, "agents": targets,
            })
        self._record_activity(ctx, "command", target=targets)

    def _send_screenshots(self, targets: list[str], ctx: dict[str, Any]) -> None:
        """Render each routed agent's live terminal (pyte snapshot, the same
        engine as the web terminal lens) and send it as an HTML <pre> block."""
        import contextlib
        chat_id = int(ctx["chat_id"])
        thread_id = ctx["thread_id"]
        conn = ctx.get("connection_id")
        host = self.host
        orch = host._orchestrator if host else None
        for aid in targets:
            inst = None
            try:
                inst = orch.get_running_instance(aid) if orch else None
            except Exception:
                inst = None
            if inst is None or not hasattr(inst, "get_pty_buffer"):
                self._reply_safe(
                    chat_id, f"{aid}: not running — no terminal to snapshot.",
                    thread_id, conn,
                )
                continue
            try:
                from relaydeck.screen import render
                snap = render(inst.get_pty_buffer())
            except Exception as exc:
                self._reply_safe(chat_id, f"{aid}: screenshot failed — {exc}", thread_id, conn)
                continue
            with contextlib.suppress(Exception):
                self.send_reply(
                    chat_id, self._format_screenshot(aid, snap),
                    thread_id=thread_id, connection_id=conn, parse_mode="HTML",
                )
        self._react(ctx, "✅")

    @staticmethod
    def _format_screenshot(agent_id: str, snap: str) -> str:
        """Wrap a terminal snapshot for Telegram: HTML-escaped <pre>, tail-
        truncated to stay under Telegram's ~4096-char message cap.

        Truncation happens AFTER escaping: a snapshot dense with <, > or &
        (common in TUIs/code) expands under html.escape (& → &amp;), so a
        raw-length cap could still overflow the rendered message and Telegram
        would reject the whole thing. Slicing the *escaped* tail also can't
        leave a dangling entity — keeping the rightmost chars never strips the
        trailing ';' off a complete entity, and a fragment cut on the left
        (e.g. 'mp;') has no '&' so it renders as harmless text."""
        import html as _html
        # Escaped-body budget; the header + <pre></pre> tags fit the rest well
        # under Telegram's 4096 cap.
        max_body = 3500
        esc = _html.escape(snap or "(empty screen)")
        if len(esc) > max_body:
            esc = "…\n" + esc[-max_body:]
        return f"📸 <b>{_html.escape(agent_id)}</b>\n<pre>{esc}</pre>"

    def _agent_status_line(self, agent_id: str) -> str:
        host = self.host
        if host is None or host._orchestrator is None:
            return "unknown"
        for a in host._orchestrator.list_agents():
            if a.get("id") == agent_id:
                proc = a.get("status") or "?"
                sem = a.get("semantic_status") or "—"
                return f"{proc} / {sem}"
        return "not found"

    def _reply_safe(self, chat_id: int, text: str, thread_id: Any, conn: Any) -> None:
        with contextlib.suppress(Exception):
            self.send_reply(
                chat_id, text, thread_id=thread_id, connection_id=conn,
                parse_mode="plain",
            )

    # ── HITL channel (operator home) ─────────────────────────────────

    def _on_hitl_escalation(self, event: Any) -> None:
        """Deliver a human-in-the-loop escalation to the configured admin
        chat. No-op when no admin chat is set — telegram simply isn't acting
        as a HITL home then."""
        host = self.host
        if host is None:
            return
        try:
            chat_raw = str(host.settings.get("hitl_admin_chat_id") or "").strip()
        except Exception:
            chat_raw = ""
        if not chat_raw:
            return
        try:
            chat_id = int(chat_raw)
        except ValueError:
            logger.warning("telegram: hitl_admin_chat_id not an int: %r", chat_raw)
            return
        try:
            conn = (str(host.settings.get("hitl_admin_connection") or "").strip() or None)
        except Exception:
            conn = None
        data = getattr(event, "data", None) or {}
        agent_id = data.get("agent_id") or "?"
        ws = data.get("workspace") or "?"
        kind = data.get("kind") or "escalation"
        message = data.get("message") or ""
        hint = data.get("respond_hint") or ""
        stopped = " — agent STOPPED" if data.get("stopped") else ""
        text = (
            f"🛎 HITL ({kind}){stopped} — agent {agent_id} in workspace {ws}\n"
            f"{message}"
            + (f"\n\nRespond: {hint}" if hint else "")
        )
        # Deliver directly (NOT via _reply_safe, which intentionally stays quiet
        # for ordinary chat replies). HITL is a "reach me" path: a dropped
        # escalation must not be silent. Log loudly on failure so an operator
        # relying on Telegram learns it isn't wired (worker down, bad token,
        # ambiguous connection). The always-on web bell still fires regardless.
        try:
            res = self.send_reply(chat_id, text, connection_id=conn, parse_mode="plain")
        except Exception as exc:  # noqa: BLE001 — never let a channel failure escape the bus
            logger.warning(
                "telegram: HITL escalation for agent %s could not be delivered to "
                "chat %s: %s", agent_id, chat_id, exc,
            )
            return
        if not (res or {}).get("ok"):
            logger.warning(
                "telegram: HITL escalation for agent %s NOT delivered to chat %s: %s "
                "(check the bot connection — token/worker/`hitl_admin_connection`)",
                agent_id, chat_id, (res or {}).get("error") or "unknown error",
            )

    def _react(self, ctx: dict[str, Any], emoji: str) -> None:
        """Set a single emoji reaction on the inbound message. Best-
        effort; failures (e.g. older clients, restricted chats) are
        swallowed — this is decoration, not delivery."""
        if not ctx.get("reactions_enabled"):
            return
        worker = self.workers.get(ctx.get("connection_id") or "default")
        if worker is None or worker._loop is None:
            return
        import asyncio as _asyncio
        bot = ctx["bot"]
        chat_id = ctx["chat_id"]
        msg_id = ctx["message_id"]

        import contextlib as _ctxlib

        async def _set() -> None:
            # Reactions are best-effort: not every client / chat type
            # supports them, and a failure shouldn't kill the inbound
            # path.
            with _ctxlib.suppress(Exception):
                await bot.set_message_reaction(
                    chat_id=chat_id,
                    message_id=msg_id,
                    reaction=emoji,
                )

        _asyncio.run_coroutine_threadsafe(_set(), worker._loop)

    def _typing_enabled(self) -> bool:
        if self.host is None:
            return True
        return _bool_setting(self.host, "typing_indicator", True)

    def _maybe_start_typing(self, ctx: dict[str, Any]) -> None:
        """Show Telegram 'typing…' only when a live agent PTY received the message."""
        if not self._typing_enabled():
            return
        connection_id = str(ctx.get("connection_id") or "default")
        chat_id = int(ctx["chat_id"])
        thread_id = ctx.get("thread_id")
        tid = int(thread_id) if thread_id is not None else None
        if connection_id not in self.workers:
            return

        def _send() -> bool:
            worker = self.workers.get(connection_id)
            if worker is None:
                return False
            return worker.send_typing(chat_id, thread_id=tid)

        self._typing.start(
            _send,
            connection_id=connection_id,
            chat_id=chat_id,
            thread_id=tid,
        )

    def _stop_typing(
        self, connection_id: str | None, chat_id: int, thread_id: int | None,
    ) -> None:
        cid = (connection_id or "default").strip() or "default"
        self._typing.stop(cid, chat_id, thread_id)

    # ── public surface for the CLI/API ──────────────────────────────

    def status_snapshot(self) -> dict[str, Any]:
        conns: list[dict[str, Any]] = []
        explicit_ids = {c.id for c in self.table.connections}
        for conn in self.effective_connections():
            w = self.workers.get(conn.id)
            bot = w.info() if w else None
            has_token = bool(self._resolve_token(conn))
            stub_reason = self._conn_stub.get(conn.id)
            if (
                conn.enabled
                and bot is None
                and has_token
                and not stub_reason
                and _running_as_daemon()
            ):
                stub_reason = "worker not running — restart the bot or check the daemon log"
            resolved_name = conn.name
            if not resolved_name and bot and bot.get("bot_username"):
                resolved_name = f"@{bot['bot_username']}"
            conns.append({
                "id": conn.id,
                "name": resolved_name or conn.id,
                "configured_name": conn.name,
                "enabled": conn.enabled,
                "token_vault_key": conn.token_vault_key,
                # `has_token` lets the UI show "set/missing" + label the editor
                # "Rotate"/"Add" without ever exposing the value.
                "has_token": has_token,
                # `explicit` = configured in telegram.yaml (removable). A
                # synthesized legacy default is implicit — removing it is a
                # no-op (it re-synthesizes), so the UI hides its remove button.
                "explicit": conn.id in explicit_ids,
                "bot": bot,
                "stub_reason": stub_reason,
            })
        any_ready = any((c.get("bot") or {}).get("ready") for c in conns)
        any_error = any((c.get("bot") or {}).get("last_error") or c.get("stub_reason") for c in conns)
        info: dict[str, Any] = {
            "plugin_loaded": self.host is not None,
            # Top-level `bot` mirrors the first connection so existing
            # single-bot dashboard/CLI readers keep working.
            "bot": conns[0]["bot"] if conns else None,
            "stub_reason": self._stub_reason,
            "mode": "polling",
            "connections": conns,
            "routes": len(self.table.routes),
            "allowed_users": len(self.table.allowed_users),
            "allowed_groups": len(self.table.allowed_groups),
            "ready": any_ready,
            "errored": any_error and not any_ready,
        }
        if self.host:
            info["mode"] = str(self.host.settings.get("mode") or "polling")
        return info

    def _open_access(self) -> bool:
        """Whether the user/group allowlists are bypassed (bot is public).
        Off by default — the safe fail-closed posture."""
        if self.host is None:
            return False
        return _bool_setting(self.host, "open_access", False)

    # ── Lifecycle cleanup (orphaned routes) ─────────────────────────

    def _on_agent_deleted(self, event: Any) -> None:
        """An agent was deleted — drop routes that forwarded to it so the
        gateway stops delivering to a dead recipient. Conversations (global
        discovered chats) are left alone; they're not agent-owned and have a
        manual purge."""
        data = getattr(event, "data", None) or {}
        agent_id = str(data.get("agent_id") or data.get("id") or "")
        if not agent_id:
            return
        workspace = str(data.get("workspace") or "") or None
        self._prune_routes(
            lambda r: (r.agent or "") == agent_id
            and (workspace is None or r.workspace == workspace),
            reason=f"agent {agent_id!r} deleted",
        )

    def _on_workspace_removed(self, event: Any) -> None:
        """A workspace was removed — drop all its routes."""
        data = getattr(event, "data", None) or {}
        ws = str(data.get("workspace") or data.get("name") or "")
        if not ws:
            return
        self._prune_routes(lambda r: r.workspace == ws,
                           reason=f"workspace {ws!r} removed")

    def _prune_routes(self, predicate: Any, *, reason: str) -> int:
        """Remove every route matching `predicate`, persist, and bounce the
        worker if anything changed. Best-effort — never raises into the bus."""
        try:
            self.reload_table()
            before = len(self.table.routes)
            self.table.routes = [r for r in self.table.routes if not predicate(r)]
            removed = before - len(self.table.routes)
            if removed:
                self.persist_table()
                logger.info("telegram: pruned %d orphaned route(s) — %s", removed, reason)
                with contextlib.suppress(Exception):
                    self.restart_worker()
            return removed
        except Exception as exc:
            logger.debug("telegram: route prune failed (%s): %s", reason, exc)
            return 0

    def reload_table(self) -> None:
        if self.host:
            self.table = load_table(self.host.config_home)
            self.table.open_access = self._open_access()
            if self._auto_allow_route_chats():
                self.persist_table()

    def persist_table(self) -> None:
        if self.host:
            save_table(self.host.config_home, self.table)
            # The routed-workspace set may have changed → ask the skills
            # plugin to re-sync the relaydeck-telegram skill.
            with contextlib.suppress(Exception):
                self.host.events.emit("plugin.skills.changed", {"plugin": PLUGIN_NAME})

    def _auto_allow_route_chats(self) -> bool:
        """Keep routing ergonomic and fail-closed at the same time.

        Adding a concrete private-chat route implies that user should be able
        to DM the bot; adding a concrete group/channel route implies the chat
        should be accepted. Wildcard routes stay allowlist-neutral.
        """
        before_users = set(self.table.allowed_users)
        before_groups = set(self.table.allowed_groups)
        for route in self.table.routes:
            if route.direction not in ("in", "in+out"):
                continue
            if route.chat_id is None:
                continue
            if route.chat_id < 0:
                self.table.allowed_groups.add(route.chat_id)
            else:
                self.table.allowed_users.add(route.chat_id)
        return (
            self.table.allowed_users != before_users
            or self.table.allowed_groups != before_groups
        )

    # ── Skill provider (consumed by the bundled `skills` plugin) ────
    #
    # The `relaydeck-telegram` skill teaches agents the
    # `[relay from=telegram:<chat_id>]` header and the
    # `relaydeck telegram reply` CLI. Materialization is owned by the generic
    # `[plugin.skills]` consumer in `plugins/skills/`; this plugin
    # only declares the skill (plugin.toml) and resolves its *dynamic*
    # target set — the workspaces a route currently points at. When the
    # route table changes (`persist_table`) we emit `plugin.skills.changed`
    # so the skills manager re-syncs.

    def skill_target_workspaces(self, all_workspaces: list[str]) -> list[str]:
        routed = {r.workspace for r in self.table.inbound_routes if r.workspace}
        return [w for w in all_workspaces if w in routed]

    def _reply_worker(
        self, connection_id: str | None, chat_id: int | None = None,
    ) -> TelegramWorker | None:
        """Pick the bot to send an outbound message through. Explicit
        connection wins; else if there's exactly one bot use it; else infer
        from the conversation registry (which connection has seen this chat).
        Returns None only when it's genuinely ambiguous."""
        if connection_id and connection_id in self.workers:
            return self.workers[connection_id]
        if len(self.workers) == 1:
            return next(iter(self.workers.values()))
        if chat_id is not None and self.conversations is not None:
            owners = {
                c.connection_id for c in self.conversations.list()
                if c.chat_id == chat_id and c.connection_id in self.workers
            }
            if len(owners) == 1:
                return self.workers[next(iter(owners))]
        return None

    def send_reply(
        self,
        chat_id: int,
        body: str,
        *,
        thread_id: int | None = None,
        connection_id: str | None = None,
        parse_mode: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        reply_to_message_id: int | None = None
        if in_reply_to and self._inbound_map is not None:
            meta = self._inbound_map.lookup(in_reply_to)
            if meta is not None:
                reply_to_message_id = meta.telegram_message_id
                if thread_id is None:
                    thread_id = meta.thread_id
                if connection_id is None:
                    connection_id = meta.connection_id
        worker = self._reply_worker(connection_id, chat_id=chat_id)
        stop_conn = connection_id
        if worker is not None and stop_conn is None:
            for cid, w in self.workers.items():
                if w is worker:
                    stop_conn = cid
                    break
        if parse_mode is None:
            parse_mode = _infer_parse_mode(body, None)
        try:
            if connection_id and connection_id not in self.workers:
                return {"ok": False, "error": (
                    f"Telegram connection {connection_id!r} is not running"
                )}
            if worker is None:
                if self.workers:
                    return {"ok": False, "error": (
                        "ambiguous Telegram connection — pass `connection` to choose "
                        f"one of: {', '.join(sorted(self.workers))}"
                    )}
                return {"ok": False, "error": self._stub_reason or "worker not ready"}
            return worker.send_text(
                chat_id, body, thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                parse_mode=parse_mode,
            )
        finally:
            self._stop_typing(stop_conn, chat_id, thread_id)

    # ── interactive prompts (inline-keyboard callbacks) ─────────────

    def _handle_callback(self, ctx: dict[str, Any], *, connection_id: str = "default") -> None:
        """Resolve an interactive prompt when a human taps an inline button.

        Button callback_data is ``rd:<prompt_id>:<choice_index>`` (compact
        enough for Telegram's 64-byte cap). We map the index back to a
        choice, call ``host.prompts.respond`` (first tap wins the race),
        and ack the query so the client's spinner clears. The winning
        ``respond`` retracts the buttons via the provider's ``close_prompt``
        — so we don't edit the message here, only toast the result.
        """
        host = self.host
        worker = self.workers.get(connection_id)
        cq_id = ctx.get("callback_query_id")

        def ack(text: str | None = None) -> None:
            if worker is not None and cq_id:
                with contextlib.suppress(Exception):
                    worker.answer_callback_query(cq_id, text=text)

        data = str(ctx.get("callback_data") or "")
        if host is None or not data.startswith("rd:"):
            ack()  # not ours (or no host) — still clear the spinner
            return
        try:
            user_id = int(ctx.get("user_id") or 0)
            chat_id = int(ctx.get("chat_id") or 0)
            is_private = (ctx.get("chat_type") or "") == "private"
        except (TypeError, ValueError):
            ack("Invalid request")
            return
        if is_private:
            if not self.table.is_user_allowed(user_id):
                logger.info(
                    "telegram: drop prompt callback from un-allowlisted user_id=%s",
                    user_id,
                )
                ack("Not authorized")
                return
        elif chat_id and not self.table.is_chat_allowed(chat_id):
            logger.info(
                "telegram: drop prompt callback from un-allowlisted chat_id=%s",
                chat_id,
            )
            ack("Not authorized")
            return
        try:
            _, pid, idx_raw = data.split(":", 2)
            idx = int(idx_raw)
        except (ValueError, IndexError):
            ack("Invalid button")
            return
        prompt = host.prompts.get(pid)
        if prompt is None:
            ack("This prompt is no longer available.")
            return
        if idx < 0 or idx >= len(prompt.choices):
            ack("Invalid choice")
            return
        choice = prompt.choices[idx]
        answered_by = f"telegram:{ctx.get('chat_id')} @{ctx.get('user_name') or ctx.get('user_id')}"
        try:
            resolved = host.prompts.respond(pid, choice.id, answered_by=answered_by)
        except Exception as exc:
            logger.warning("telegram: prompt respond failed: %s", exc)
            ack("Couldn't record that — try again.")
            return
        if resolved is not None:
            ack(f"✓ {choice.label}")
        else:
            cur = host.prompts.get(pid)
            state = cur.state if cur else "closed"
            ack("Already answered" if state == "answered" else f"Closed ({state})")

    # ── worker lifecycle (used by setup + settings changes) ─────────

    def restart_worker(self) -> dict[str, Any]:
        """Tear down the current PTB worker (if any) and try to start
        a fresh one. Called from the setup/settings/restart endpoints
        so operators don't have to bounce the daemon to pick up a new
        token or mode change.
        """
        if self.host is None:
            return {"ok": False, "error": "plugin not loaded"}
        self._typing.stop_all()
        # Teardown — workers.teardown() stops every worker this plugin
        # owns and waits for them to join (5s default).
        try:
            self.host.workers.teardown()
        except Exception as exc:
            logger.warning("telegram: teardown error during restart: %s", exc)
        self.workers.clear()
        self._worker_handles.clear()
        self._conn_stub.clear()
        self._stub_reason = None
        self._maybe_start_worker()
        snap = self.status_snapshot()
        ready = any((c.get("bot") or {}).get("ready") for c in snap.get("connections", []))
        error = next(
            (
                (c.get("bot") or {}).get("last_error") or c.get("stub_reason")
                for c in snap.get("connections", [])
                if (c.get("bot") or {}).get("last_error") or c.get("stub_reason")
            ),
            None,
        )
        return {
            "ok": ready,
            "status": snap,
            "error": None if ready else (error or snap.get("stub_reason") or "worker not ready"),
        }

    def auth_check(self) -> dict[str, Any]:
        """Synchronous `getMe` against the current bot token. Used by
        the dashboard's "Verify token" button. Returns the same shape
        as the CLI `relaydeck telegram auth` command.
        """
        if not self.workers:
            return {"ok": False, "error": self._stub_reason or "worker not ready"}
        # Report each connection's getMe result (the worker's `_ready` event
        # flips once getMe succeeded inside its loop). Top-level fields mirror
        # the first connection for the existing single-bot "Verify token" button.
        per_conn: list[dict[str, Any]] = []
        for cid, worker in self.workers.items():
            worker.ready(timeout=5.0)
            info = worker.info()
            per_conn.append({
                "connection": cid,
                "ok": bool(info.get("bot_username")),
                "bot_username": info.get("bot_username"),
                "bot_id": info.get("bot_id"),
            })
        first = per_conn[0]
        return {
            "ok": first["ok"],
            "bot_username": first.get("bot_username"),
            "bot_id": first.get("bot_id"),
            "connections": per_conn,
            "error": None if first["ok"] else "bot not ready (check daemon log)",
        }

    # ── CLI registration ────────────────────────────────────────────

    def _register_cli(self) -> None:
        host = self.host
        if host is None:
            return
        import click

        # status -------------------------------------------------------
        @host.cli.command("status", help="Bot info, mode, route count, and stub reason.")
        def _status():
            from rich.console import Console
            from rich.table import Table

            console = Console()
            s = self.status_snapshot()
            if s.get("stub_reason"):
                console.print(f"[yellow]●[/] telegram: [dim]{s['stub_reason']}[/]")
            elif s.get("bot"):
                bot = s["bot"]
                if bot.get("ready"):
                    state = "[green]ready[/]"
                elif bot.get("last_error"):
                    state = f"[red]error[/] [dim]{bot.get('last_error')}[/]"
                else:
                    state = "[yellow]starting…[/]"
                console.print(
                    f"[green]●[/] telegram: @{bot.get('bot_username') or '?'} "
                    f"({s['mode']}) — {state}"
                )
            else:
                console.print("[dim]telegram: worker not running[/]")
            t = Table.grid(padding=(0, 2))
            t.add_row("Connections", str(len(s.get("connections", []))))
            t.add_row("Routes", str(s["routes"]))
            t.add_row("Allowed users", str(s["allowed_users"]))
            t.add_row("Allowed groups", str(s["allowed_groups"]))
            console.print(t)
            for c in s.get("connections", []):
                bot = c.get("bot") or {}
                err = bot.get("last_error") or c.get("stub_reason")
                mark = "[green]●[/]" if bot.get("ready") else ("[red]●[/]" if err else "[yellow]○[/]")
                uname = bot.get("bot_username") or err or "—"
                console.print(f"  {mark} {c['id']}: {uname}")

        @host.cli.command("conversations", help="Discovered chats/groups/channels (global).")
        def _conversations():
            from rich.console import Console
            from rich.table import Table

            console = Console()
            convs = self.conversations.list() if self.conversations else []
            if not convs:
                console.print(
                    "[dim]No conversations seen yet. Message a bot — chats are "
                    "discovered from inbound traffic (the Bot API can't list them).[/]"
                )
                return
            t = Table(title="Telegram conversations")
            t.add_column("connection", style="cyan")
            t.add_column("chat_id")
            t.add_column("type")
            t.add_column("title")
            t.add_column("msgs", justify="right")
            t.add_column("last")
            for c in convs:
                t.add_row(
                    c.connection_id, str(c.chat_id), c.chat_type,
                    (c.title or c.last_user or "")[:32], str(c.message_count),
                    c.last_disposition or "",
                )
            console.print(t)

        @host.cli.command(
            "conversations-rm",
            help="Forget discovered chat(s) from the registry. Pass --chat <id> "
                 "(use --chat=-100... for groups) or --all.",
        )
        @click.option("--chat", "chat", default=None,
                      help="chat_id to forget (negative group ids: --chat=-100123).")
        @click.option("--connection", "connection", default=None,
                      help="Only forget this chat under one bot connection.")
        @click.option("--all", "purge_all", is_flag=True, default=False,
                      help="Forget ALL conversations (the registry, not routes).")
        def _conversations_rm(chat: str | None, connection: str | None, purge_all: bool):
            from rich.console import Console

            console = Console()
            if not self.conversations:
                console.print("[dim]No conversation registry.[/]")
                return
            if purge_all:
                n = self.conversations.purge_all()
                console.print(f"[green]✓[/] forgot {n} conversation{'' if n == 1 else 's'}")
                return
            if chat is None:
                console.print("[red]✗[/] pass --chat <id> or --all")
                return
            try:
                chat_id = int(str(chat).strip())
            except ValueError:
                console.print(f"[red]✗[/] --chat must be an integer (got {chat!r})")
                return
            n = self.conversations.delete_chat(chat_id, connection_id=connection)
            if n:
                console.print(f"[green]✓[/] forgot {n} row{'' if n == 1 else 's'} for chat {chat_id}")
            else:
                console.print(f"[dim]no conversation for chat {chat_id}[/]")

        # connections (multi-bot) -------------------------------------
        @host.cli.command("connections", help="List configured bots (connections).")
        def _connections():
            from rich.console import Console
            from rich.table import Table

            console = Console()
            t = Table(title="Telegram connections")
            t.add_column("id", style="cyan")
            t.add_column("name")
            t.add_column("token_vault_key")
            t.add_column("enabled")
            t.add_column("token")
            for conn in self.effective_connections():
                has = "✓" if self._resolve_token(conn) else "[red]missing[/]"
                t.add_row(
                    conn.id, conn.name or "", conn.token_vault_key,
                    "yes" if conn.enabled else "no", has,
                )
            console.print(t)

        @host.cli.command("connections-add", help="Register a bot (writes token to vault).")
        @click.argument("conn_id")
        @click.option("--token", required=True, help="Bot API token (verified via getMe).")
        @click.option("--name", default="", help="Display name.")
        @click.option("--token-key", "token_key", default=None,
                      help="Vault key for the token (default TELEGRAM_BOT_TOKEN_<ID>).")
        def _connections_add(conn_id: str, token: str, name: str, token_key: str | None):
            from rich.console import Console

            console = Console()
            cid = _slug_conn_id(conn_id)
            if not cid:
                console.print("[red]✗[/] invalid id (use a-z0-9_-)")
                raise SystemExit(1)
            ok, username, _bid, err = _getme(token)
            if not ok:
                console.print(f"[red]✗[/] token rejected: {err}")
                raise SystemExit(1)
            if not name and username:
                name = username
            key = token_key or f"TELEGRAM_BOT_TOKEN_{cid.upper()}"
            self._store_secret(key, token)
            self.reload_table()
            conns = list(self.table.connections) or list(self.effective_connections())
            conns = [c for c in conns if c.id != cid] + [
                Connection(id=cid, name=name, token_vault_key=key, enabled=True)
            ]
            self.table.connections = conns
            self.persist_table()
            console.print(
                f"[green]✓[/] connection [bold]{cid}[/] → @{username}. "
                "[dim]Restart the daemon (relaydeck daemon stop && start) to apply.[/]"
            )

        @host.cli.command("connections-rm", help="Remove a bot connection.")
        @click.argument("conn_id")
        def _connections_rm(conn_id: str):
            from rich.console import Console

            console = Console()
            cid = _slug_conn_id(conn_id)
            self.reload_table()
            conns = list(self.table.connections)
            remaining = [c for c in conns if c.id != cid]
            if len(remaining) != len(conns):
                # Explicit: drop the YAML row; the token stays in the vault.
                self.table.connections = remaining
                self.persist_table()
                console.print(
                    f"[green]✓[/] removed connection [bold]{cid}[/]. "
                    "[dim]Restart the daemon to apply.[/]"
                )
                return
            # Implicit default: clear its vault token (the only thing keeping
            # it alive), mirroring the dashboard DELETE.
            implicit = next(
                (c for c in self.effective_connections() if c.id == cid), None,
            )
            if implicit is None:
                console.print(f"[red]✗[/] unknown connection {cid!r}")
                raise SystemExit(1)
            self._delete_secret(implicit.token_vault_key)
            console.print(
                f"[green]✓[/] cleared token for the default bot "
                f"([dim]{implicit.token_vault_key}[/]); it stops polling. "
                "[dim]Restart the daemon to apply.[/]"
            )

        @host.cli.command("connections-rename", help="Rename a bot connection.")
        @click.argument("conn_id")
        @click.argument("name")
        def _connections_rename(conn_id: str, name: str):
            from rich.console import Console

            console = Console()
            cid = _slug_conn_id(conn_id)
            self.reload_table()
            existing = next(
                (c for c in self.effective_connections() if c.id == cid), None,
            )
            if existing is None:
                console.print(f"[red]✗[/] unknown connection {cid!r}")
                raise SystemExit(1)
            conn = Connection(
                id=cid, name=name.strip(),
                token_vault_key=existing.token_vault_key,
                enabled=existing.enabled,
            )
            self.table.connections = [
                c for c in self.table.connections if c.id != cid
            ] + [conn]
            self.persist_table()
            console.print(
                f"[green]✓[/] renamed [bold]{cid}[/] → {name.strip()!r}. "
                "[dim]Restart the daemon to apply.[/]"
            )

        # auth ---------------------------------------------------------
        @host.cli.command("auth", help="Verify each bot token via getMe.")
        def _auth():
            from rich.console import Console

            console = Console()
            conns = self.effective_connections()
            if not conns:
                console.print("[red]✗[/] no Telegram connections configured")
                raise SystemExit(1)
            any_ok = False
            for conn in conns:
                token = self._resolve_token(conn)
                if not token:
                    console.print(
                        f"[red]✗[/] {conn.label}: vault key "
                        f"{conn.token_vault_key!r} is unset",
                    )
                    continue
                ok, username, bot_id, err = _getme(token)
                if ok:
                    any_ok = True
                    console.print(f"[green]✓[/] {conn.label}: @{username} (id={bot_id})")
                else:
                    console.print(f"[red]✗[/] {conn.label}: {err}")
            if not any_ok:
                raise SystemExit(1)

        # setup --------------------------------------------------------
        @host.cli.command(
            "setup",
            help="Interactive: paste bot token, save to vault, allowlist bootstrap users.",
        )
        @click.option("--token", default=None, help="Bot API token (skip the prompt).")
        @click.option(
            "--user", "user_ids", multiple=True, type=int,
            help="Authorize a user ID to DM the bot. Repeat for multiple.",
        )
        def _setup(token: str | None, user_ids: tuple[int, ...]):
            from rich.console import Console

            console = Console()
            if not token:
                console.print(
                    "[bold]Create a bot:[/] open Telegram → message [cyan]@BotFather[/] "
                    "→ /newbot → follow prompts to get an API token.",
                )
                token = click.prompt("Paste the token", hide_input=True).strip()
            if not token:
                console.print("[red]✗[/] no token provided")
                return
            try:
                self._store_secret("TELEGRAM_BOT_TOKEN", token)
            except Exception as exc:
                console.print(f"[red]✗[/] failed to write vault: {exc}")
                return
            # Update the route table with the bootstrap user allowlist.
            self.reload_table()
            users = set(self.table.allowed_users)
            users.update(user_ids)
            self.table.allowed_users = users
            self.persist_table()
            console.print(
                f"[green]✓[/] saved TELEGRAM_BOT_TOKEN; "
                f"{len(user_ids)} user(s) allowlisted. "
                "Restart the daemon (relaydeck daemon stop && relaydeck daemon start) "
                "to pick up the new token.",
            )

        # routes group ------------------------------------------------
        @host.cli.command("routes-list", help="List configured routes.")
        def _routes_list():
            from rich.console import Console
            from rich.table import Table

            console = Console()
            self.reload_table()
            if not self.table.routes:
                console.print(
                    "[dim]No routes. Edit ~/.relaydeck/telegram.yaml or use "
                    "`relaydeck telegram routes-add`.[/]",
                )
                return
            t = Table(show_header=True, header_style="bold")
            t.add_column("#")
            t.add_column("Chat / topic")
            t.add_column("Match")
            t.add_column("Workspace")
            t.add_column("Agent")
            t.add_column("Dir")
            for i, r in enumerate(self.table.routes):
                chat = str(r.chat_id) if r.chat_id is not None else "-"
                if r.thread_id is not None:
                    chat += f"#{r.thread_id}"
                match = f"/{r.command}" if r.command else "*"
                t.add_row(
                    str(i), chat, match, r.workspace, r.agent or "[dim]*[/]", r.direction,
                )
            console.print(t)

        @host.cli.command("routes-add", help="Add a route to telegram.yaml.")
        @click.option("--chat", "chat_id", type=int, required=True)
        @click.option("--thread", "thread_id", type=int, default=None)
        @click.option("--command", "command", default=None,
                      help="Match a leading /<command> (without the slash).")
        @click.option("--workspace", required=True)
        @click.option("--agent", default=None,
                      help="Target agent id; omit to broadcast to every agent in the workspace.")
        @click.option("--direction", default="in+out",
                      type=click.Choice(["in", "out", "in+out"]))
        @click.option("--connection", default=None,
                      help="Scope to one bot (connection id); omit for any bot.")
        def _routes_add(
            chat_id: int, thread_id: int | None, command: str | None,
            workspace: str, agent: str | None, direction: str,
            connection: str | None,
        ):
            from rich.console import Console

            from .routes import Route

            console = Console()
            self.reload_table()
            r = Route(
                workspace=workspace,
                chat_id=chat_id,
                thread_id=thread_id,
                command=command,
                agent=agent,
                direction=direction,
                connection=connection,
            )
            self.table.routes.append(r)
            self._auto_allow_route_chats()
            self.persist_table()
            console.print(f"[green]✓[/] added route #{len(self.table.routes)-1}: {r}")

        @host.cli.command("routes-rm", help="Remove route by index.")
        @click.argument("index", type=int)
        def _routes_rm(index: int):
            from rich.console import Console

            console = Console()
            self.reload_table()
            if not 0 <= index < len(self.table.routes):
                console.print(f"[red]✗[/] no route at index {index}")
                return
            removed = self.table.routes.pop(index)
            self.persist_table()
            console.print(f"[green]✓[/] removed {removed}")

        @host.cli.command("routes-test", help="Dry-run: which agents would receive this chat?")
        @click.argument("chat_id", type=int)
        @click.option("--thread", "thread_id", type=int, default=None)
        @click.option("--command", default=None)
        @click.option("--connection", default=None,
                      help="Test as if the message arrived on this bot (connection id).")
        def _routes_test(chat_id: int, thread_id: int | None, command: str | None,
                         connection: str | None):
            from rich.console import Console

            console = Console()
            self.reload_table()
            matches = self.table.match_inbound(
                chat_id, connection=connection, thread_id=thread_id, command=command,
            )
            if not matches:
                console.print("[dim]No matching route.[/]")
                return
            top = matches[0].specificity()
            console.print(f"[bold]{len(matches)} match(es); top specificity: {top}[/]")
            for r in matches:
                marker = "[green]→[/]" if r.specificity() == top else "[dim]·[/]"
                console.print(
                    f"  {marker} workspace={r.workspace} agent={r.agent or '*'} "
                    f"command={r.command or '*'} thread={r.thread_id or '*'} "
                    f"specificity={r.specificity()}"
                )

        # reply --------------------------------------------------------
        @host.cli.command("reply", help="Send a message to a Telegram chat.")
        @click.argument("chat_id", type=int)
        @click.argument("body", nargs=-1, required=True)
        @click.option("--thread", "thread_id", type=int, default=None)
        @click.option("--connection", default=None,
                      help="Which bot to send from (connection id); inferred if omitted.")
        @click.option("--format", "fmt", default=None,
                      type=click.Choice(["plain", "html", "markdown"]),
                      help="Rich text mode: html (recommended) or markdown. "
                           "Default plain. Malformed markup falls back to plain.")
        @click.option("--in-reply-to", "in_reply_to", default=None,
                      help="Relay msg id (msg_…) to quote as a Telegram reply.")
        def _reply(chat_id: int, body: tuple[str, ...], thread_id: int | None,
                   connection: str | None, fmt: str | None, in_reply_to: str | None):
            from rich.console import Console

            console = Console()
            text = " ".join(body).strip()
            if not text:
                console.print("[red]✗[/] empty body")
                return
            # The bot workers live in the daemon, so send via the daemon's
            # /reply endpoint (falls back to in-process for daemon contexts).
            payload = {
                "chat_id": chat_id, "body": text, "thread_id": thread_id,
                "connection": connection, "format": fmt,
                "in_reply_to": in_reply_to,
            }
            ok, resp = _post_daemon("/api/plugins/telegram/reply", payload)
            result = resp if (ok and isinstance(resp, dict)) else (
                self.send_reply(
                    chat_id, text, thread_id=thread_id,
                    connection_id=connection,
                    parse_mode=_infer_parse_mode(text, fmt),
                    in_reply_to=in_reply_to,
                )
                if _is_conn_error(resp) else {"ok": False, "error": str(resp)}
            )
            if result.get("ok"):
                note = " [yellow](markup invalid → sent plain)[/]" if result.get("downgraded") else ""
                console.print(f"[green]✓[/] sent (message_id={result.get('message_id')}){note}")
            else:
                console.print(f"[red]✗[/] {result.get('error')}")

        # allow --------------------------------------------------------
        @host.cli.command("allow-user", help="Allow a user ID to DM the bot.")
        @click.argument("user_id", type=int)
        def _allow_user(user_id: int):
            from rich.console import Console

            console = Console()
            self.reload_table()
            self.table.allowed_users.add(user_id)
            self.persist_table()
            console.print(f"[green]✓[/] allowed user_id={user_id}")

        @host.cli.command("allow-group", help="Allow a group/channel chat_id.")
        @click.argument("chat_id", type=int)
        def _allow_group(chat_id: int):
            from rich.console import Console

            console = Console()
            self.reload_table()
            self.table.allowed_groups.add(chat_id)
            self.persist_table()
            console.print(f"[green]✓[/] allowed chat_id={chat_id}")

    # ── HTTP API (used by the dashboard lens) ───────────────────────

    def _register_api(self) -> None:
        host = self.host
        if host is None:
            return

        @host.api.route("/status", methods=["GET"])
        async def status():
            return self.status_snapshot()

        @host.api.route("/activity", methods=["GET"])
        async def activity(limit: int = 60):
            """Recent inbound updates + disposition (newest first). Powers
            the dashboard feed + onboarding (rejected/unrouted rows carry
            the user/chat id for one-click allow)."""
            rows = list(self._activity)[-max(1, min(200, limit)):]
            return {"activity": list(reversed(rows))}

        @host.api.route("/conversations", methods=["GET"])
        async def conversations():
            """The global registry of chats/groups/channels seen across all
            connections (newest activity first). Routing selects from this."""
            convs = self.conversations.list() if self.conversations else []
            return {"conversations": [c.to_dict() for c in convs]}

        @host.api.route("/conversations", methods=["DELETE"])
        async def conversations_purge():
            """Forget ALL discovered conversations. Routes are untouched; chats
            reappear in the registry as inbound traffic is seen again."""
            n = self.conversations.purge_all() if self.conversations else 0
            return {"ok": True, "removed": n}

        @host.api.route("/conversations/{connection_id}/{chat_id}", methods=["DELETE"])
        async def conversations_rm_one(connection_id: str, chat_id: str):
            """Forget one conversation by connection + chat_id (chat_id may be
            negative for groups; the str path segment handles the leading '-')."""
            try:
                cid = int(chat_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": "chat_id must be an integer"}
            removed = (
                self.conversations.delete(connection_id, cid)
                if self.conversations else False
            )
            return {"ok": True, "removed": 1 if removed else 0}

        @host.api.route("/connections", methods=["GET"])
        async def connections_get():
            """All bots (connections) + per-connection bot status."""
            return {"connections": self.status_snapshot().get("connections", [])}

        @host.api.route("/connections", methods=["POST"])
        async def connections_add(body: dict[str, Any]):
            """Register a bot (connection): store its token in the vault and
            add it to telegram.yaml, then restart workers. Body: id, token,
            optional name + token_vault_key."""
            cid = _slug_conn_id(str((body or {}).get("id") or ""))
            token = str((body or {}).get("token") or "").strip()
            if not cid:
                return {"ok": False, "error": "id is required (a-z0-9_-)"}
            if not token:
                return {"ok": False, "error": "token is required"}
            name = str((body or {}).get("name") or "").strip()
            key = (str((body or {}).get("token_vault_key") or "").strip()
                   or f"TELEGRAM_BOT_TOKEN_{cid.upper()}")
            # Verify the token before persisting anything.
            ok, username, _bot_id, err = _getme(token)
            if not ok:
                return {"ok": False, "error": f"token rejected: {err}"}
            # Default the display name to the bot's @username so the lens shows
            # the real bot identity instead of the bare connection id.
            if not name and username:
                name = username
            self._store_secret(key, token)
            conn = Connection(id=cid, name=name, token_vault_key=key, enabled=True)
            # Materialize the implicit default first so adding a 2nd bot never
            # silently drops the original single-bot connection.
            conns = list(self.table.connections) or list(self.effective_connections())
            conns = [c for c in conns if c.id != cid] + [conn]
            self.table.connections = conns
            self.persist_table()
            with contextlib.suppress(Exception):
                self.host.events.emit("telegram.connection.added", {
                    "connection": cid, "name": name, "bot_username": username,
                })
            restart = self.restart_worker()
            return {"ok": True, "id": cid, "bot_username": username,
                    "name": name, "restart": restart}

        @host.api.route("/connections/{conn_id}", methods=["DELETE"])
        async def connections_rm(conn_id: str):
            cid = _slug_conn_id(conn_id)
            conns = list(self.table.connections)
            remaining = [c for c in conns if c.id != cid]
            if len(remaining) != len(conns):
                # Explicit connection: drop the YAML row. Its token stays in
                # the vault so the bot can be re-added later.
                self.table.connections = remaining
                self.persist_table()
                token_cleared = False
            else:
                # The synthesized legacy default is implicit (no YAML row) — the
                # only thing keeping it alive is its vault token, so "delete"
                # clears that token. It then reverts to an unconfigured stub.
                implicit = next(
                    (c for c in self.effective_connections() if c.id == cid),
                    None,
                )
                if implicit is None:
                    return {"ok": False, "error": f"unknown connection {cid!r}"}
                with contextlib.suppress(Exception):
                    self._delete_secret(implicit.token_vault_key)
                token_cleared = True
            with contextlib.suppress(Exception):
                self.host.events.emit("telegram.connection.removed", {
                    "connection": cid, "token_cleared": token_cleared,
                })
            restart = self.restart_worker()
            return {"ok": True, "id": cid, "token_cleared": token_cleared,
                    "restart": restart}

        @host.api.route("/connections/{conn_id}", methods=["PATCH"])
        async def connections_rename(conn_id: str, body: dict[str, Any]):
            """Rename a connection (cosmetic — no token change, no restart).
            Works on the implicit default too: it materializes an explicit
            connection so the name persists. Body: name."""
            cid = _slug_conn_id(conn_id)
            new_name = str((body or {}).get("name") or "").strip()
            existing = next(
                (c for c in self.effective_connections() if c.id == cid), None,
            )
            if existing is None:
                return {"ok": False, "error": f"unknown connection {cid!r}"}
            conn = Connection(
                id=cid, name=new_name,
                token_vault_key=existing.token_vault_key,
                enabled=existing.enabled,
            )
            self.table.connections = [
                c for c in self.table.connections if c.id != cid
            ] + [conn]
            self.persist_table()
            with contextlib.suppress(Exception):
                self.host.events.emit("telegram.connection.changed", {
                    "connection": cid, "name": new_name, "state": "renamed",
                })
            return {"ok": True, "id": cid, "name": new_name}

        @host.api.route("/routes", methods=["GET"])
        async def routes_get():
            self.reload_table()
            return {
                "allowed_users": sorted(self.table.allowed_users),
                "allowed_groups": sorted(self.table.allowed_groups),
                "routes": [r.to_dict() for r in self.table.routes],
            }

        @host.api.route("/routes", methods=["PUT"])
        async def routes_put(body: dict[str, Any]):
            from .routes import _parse_connections, _parse_table

            table = _parse_table(body)
            # The routes editor sends allowlists + routes but NOT connections;
            # preserve the existing connection set so saving a route can't wipe
            # the configured bots. (If the body DOES carry connections, honor
            # them — a future connections editor uses the same endpoint.)
            if "connections" in (body or {}):
                table.connections = _parse_connections(body)
            else:
                self.reload_table()  # pick up latest on-disk connections
                table.connections = self.table.connections
            table.open_access = self._open_access()
            self.table = table
            self._auto_allow_route_chats()
            self.persist_table()
            return {"ok": True, "routes": len(table.routes)}

        @host.api.route("/routes/test", methods=["POST"])
        async def routes_test(body: dict[str, Any]):
            chat_id = int(body.get("chat_id") or 0)
            thread_id = body.get("thread_id")
            thread_id = int(thread_id) if thread_id is not None else None
            command = body.get("command") or None
            connection = (str(body.get("connection") or "").strip() or None)
            matches = self.table.match_inbound(
                chat_id, connection=connection, thread_id=thread_id, command=command,
            )
            top = matches[0].specificity() if matches else 0
            return {
                "matches": [
                    {
                        **r.to_dict(),
                        "specificity": r.specificity(),
                        "winner": r.specificity() == top,
                    }
                    for r in matches
                ],
            }

        @host.api.route("/reply", methods=["POST"])
        async def reply(body: dict[str, Any]):
            chat_id = int(body.get("chat_id") or 0)
            text = str(body.get("body") or "")
            thread_id = body.get("thread_id")
            thread_id = int(thread_id) if thread_id is not None else None
            connection = (str(body.get("connection") or "").strip() or None)
            in_reply_to = (str(body.get("in_reply_to") or "").strip() or None)
            fmt = body.get("format")
            # `format` (html/markdown/plain) or a raw `parse_mode` both accepted.
            parse_mode = (str(body.get("parse_mode") or "").strip() or None)
            if parse_mode is None:
                parse_mode = _infer_parse_mode(text, fmt if fmt is not None else None)
            if not chat_id or not text:
                return {"ok": False, "error": "chat_id and body required"}
            return self.send_reply(
                chat_id, text, thread_id=thread_id, connection_id=connection,
                parse_mode=parse_mode, in_reply_to=in_reply_to,
            )

        # ── CLI parity endpoints ────────────────────────────────────
        #
        # Each of these mirrors a `relaydeck telegram ...` subcommand so
        # operators can drive the plugin entirely from the dashboard.

        @host.api.route("/setup", methods=["POST"])
        async def setup_endpoint(body: dict[str, Any]):
            """Mirror of `relaydeck telegram setup`. Writes the bot token
            to the vault under the configured `bot_token_vault_key`
            (default TELEGRAM_BOT_TOKEN), optionally seeds the user
            allowlist, and restarts the worker so the new token takes
            effect immediately.
            """
            from fastapi import HTTPException

            token = str(body.get("token") or "").strip()
            if not token:
                raise HTTPException(400, "token is required")
            users = body.get("allowed_users") or []
            key = str(host.settings.get("bot_token_vault_key") or "TELEGRAM_BOT_TOKEN")
            ok, username, _bot_id, _err = _getme(token)
            try:
                self._store_secret(key, token)
            except Exception as exc:
                raise HTTPException(500, f"vault write failed: {exc}") from exc
            # Merge bootstrap users into the allowlist.
            self.reload_table()
            for u in users:
                if isinstance(u, (int, str)):
                    try:
                        self.table.allowed_users.add(int(u))
                    except (TypeError, ValueError):
                        continue
            if ok and not self.table.connections and username:
                self.table.connections = [
                    Connection(
                        id="default",
                        name=username,
                        token_vault_key=key,
                        enabled=True,
                    )
                ]
            self.persist_table()
            result = self.restart_worker()
            return {
                "ok": result.get("ok", False),
                "allowed_users": sorted(self.table.allowed_users),
                "bot_username": username if ok else None,
                "status": result.get("status"),
                "error": result.get("error"),
            }

        @host.api.route("/webhook-secret", methods=["POST"])
        async def webhook_secret_endpoint(body: dict[str, Any]):
            """Set (or rotate) the webhook secret in the vault — so the
            operator never has to drop to `relaydeck vault set`. Restarts
            the worker so webhook validation picks it up."""
            from fastapi import HTTPException

            secret = str(body.get("secret") or "").strip()
            if not secret:
                raise HTTPException(400, "secret is required")
            key = str(host.settings.get("webhook_secret_vault_key")
                      or "TELEGRAM_WEBHOOK_SECRET")
            try:
                self._store_secret(key, secret)
            except Exception as exc:
                raise HTTPException(500, f"vault write failed: {exc}") from exc
            result = self.restart_worker()
            return {"ok": result.get("ok", False), "status": result.get("status")}

        @host.api.route("/auth", methods=["POST"])
        async def auth_endpoint():
            """Mirror of `relaydeck telegram auth`. Returns the bot
            identity if the token is valid + the worker is up."""
            return self.auth_check()

        @host.api.route("/restart", methods=["POST"])
        async def restart_endpoint():
            """Tear down + respawn the PTB worker. Useful after the
            operator changes settings (mode, webhook URL) from the
            dashboard — they don't have to bounce the daemon."""
            return self.restart_worker()

        # The route name is `/config` (not `/settings`) deliberately:
        # `/api/plugins/{name}/settings` is owned by the core registry
        # and serves the declarative schema + value sources. Our
        # endpoint returns *resolved values* in the dashboard's
        # preferred shape, plus extras like `has_bot_token`. Picking
        # a different path avoids the collision.

        @host.api.route("/config", methods=["GET"])
        async def settings_get():
            """Read resolved values + extras (`has_bot_token`,
            `has_webhook_secret`). The dashboard renders a form
            against this; the values persist via /config PUT."""
            sget = host.settings.get
            return {
                "mode": sget("mode") or "polling",
                "webhook_url": sget("webhook_url") or "",
                "require_mention_in_groups": _bool_setting(host, "require_mention_in_groups", True),
                "reactions": _bool_setting(host, "reactions", True),
                "open_access": _bool_setting(host, "open_access", False),
                "poll_timeout_s": float(sget("poll_timeout_s") or 30.0),
                "bot_token_vault_key": (
                    sget("bot_token_vault_key") or "TELEGRAM_BOT_TOKEN"
                ),
                "webhook_secret_vault_key": (
                    sget("webhook_secret_vault_key") or "TELEGRAM_WEBHOOK_SECRET"
                ),
                "has_bot_token": any(
                    self._resolve_token(c) for c in self.effective_connections()
                ),
                "has_webhook_secret": bool(self._webhook_secret()),
            }

        @host.api.route("/config", methods=["PUT"])
        async def settings_put(body: dict[str, Any]):
            """Update plugin settings + restart the worker so the
            change takes effect. The dashboard hits this when the
            operator flips mode / edits webhook_url / toggles
            reactions or mention requirement. (`/config`, not
            `/settings`, to avoid the core schema-endpoint collision.)
            """
            from relaydeck.plugin_settings import set_settings

            # Whitelist of settings the API is allowed to write. We
            # deliberately exclude the vault keys (setup endpoint
            # handles those) so a CSRF-y mistake can't redirect a
            # plugin to read from a different vault key.
            allowed = {
                "mode", "webhook_url", "require_mention_in_groups",
                "reactions", "open_access", "poll_timeout_s",
            }
            changes = {k: v for k, v in (body or {}).items() if k in allowed}
            if changes:
                set_settings("telegram", changes)
                # open_access gates inbound on the live table — apply it now so
                # it takes effect without waiting for the next table reload.
                if "open_access" in changes:
                    self.table.open_access = self._open_access()
                # Restart so the worker picks up the new mode / webhook URL.
                result = self.restart_worker()
            else:
                result = {"ok": True, "status": self.status_snapshot()}
            return {"ok": True, "changes": changes, "restart": result}

        # ── Webhook ingress ─────────────────────────────────────────
        #
        # Used when settings.mode == "webhook". Telegram POSTs each
        # update to this URL and includes the
        # `X-Telegram-Bot-Api-Secret-Token` header we set during
        # `setWebhook`; we validate it before letting any handler run.
        # The payload shape is the standard Update object; we hand it
        # to PTB so the existing handlers fire.

        @host.api.route("/webhook", methods=["POST"])
        async def webhook(request):  # type: ignore[no-untyped-def]
            from fastapi import HTTPException

            mode = str(host.settings.get("mode") or "polling")
            if mode != "webhook":
                raise HTTPException(404, "webhook mode is not enabled")
            # Secret check
            expected = self._webhook_secret()
            if expected:
                presented = request.headers.get("x-telegram-bot-api-secret-token", "")
                if presented != expected:
                    raise HTTPException(401, "invalid webhook secret")
            # Deserialize via PTB so handlers see the same Update
            # objects polling produces.
            try:
                import telegram  # type: ignore[import-not-found]
                from telegram.ext import Application  # noqa: F401
            except ImportError as exc:
                raise HTTPException(500, f"PTB not installed: {exc}") from exc
            # Webhook ingress targets a single bot. Use the connection named
            # in `?connection=` (so multi-bot webhook setups POST to distinct
            # URLs), else the only/first worker.
            conn_q = request.query_params.get("connection")
            worker = self.workers.get(conn_q) if conn_q else next(iter(self.workers.values()), None)
            if worker is None or worker._app is None:
                raise HTTPException(503, "telegram worker not ready")
            import json as _json
            body = await request.body()
            try:
                data = _json.loads(body)
            except Exception as exc:
                raise HTTPException(400, f"invalid JSON: {exc}") from exc
            update = telegram.Update.de_json(data, worker._app.bot)
            # Schedule onto PTB's running loop so handlers run inside
            # its dispatcher and any awaits resolve normally.
            import asyncio as _asyncio
            fut = _asyncio.run_coroutine_threadsafe(
                worker._app.process_update(update),
                worker._loop,
            )
            try:
                fut.result(timeout=5.0)
            except Exception as exc:
                raise HTTPException(500, f"dispatch failed: {exc}") from exc
            return {"ok": True}

    # ── webhook helpers ─────────────────────────────────────────────

    def _webhook_secret(self) -> str:
        if not self.host:
            return ""
        key = str(self.host.settings.get("webhook_secret_vault_key") or "TELEGRAM_WEBHOOK_SECRET")
        try:
            return self.host.vault.get(key)
        except (KeyError, Exception):
            return ""


def _build_prompt_keyboard(prompt: Any, per_row: int = 2) -> Any:
    """Build an InlineKeyboardMarkup from a prompt's choices.

    callback_data is ``rd:<prompt_id>:<index>`` — well under Telegram's
    64-byte cap (prompt ids are ``prm_`` + 12 hex). Imports PTB lazily;
    only ever called once a worker exists, so the extra is installed.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list[Any]] = []
    row: list[Any] = []
    for i, choice in enumerate(prompt.choices):
        row.append(InlineKeyboardButton(
            choice.label or choice.id, callback_data=f"rd:{prompt.id}:{i}",
        ))
        if len(row) >= max(1, per_row):
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


class TelegramChannelProvider:
    """Adapts the Telegram plugin to the `MessagingProvider` contract so
    core can fan interactive prompts out to Telegram chats as inline
    keyboards — without core ever importing telegram. Holds a back-ref to
    the plugin for worker resolution; all state lives on the plugin.
    """

    channel = "telegram"

    def __init__(self, plugin: TelegramPlugin) -> None:
        self._plugin = plugin

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            interactive_buttons=True, editable=True, rich_text=True, max_per_row=2,
        )

    def connections(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            for c in self._plugin.effective_connections():
                out.append({
                    "id": c.id, "name": c.name or c.id,
                    "ready": c.id in self._plugin.workers,
                })
        except Exception:
            pass
        return out

    def _resolve(self, address: Address) -> tuple[Any, int | None, int | None]:
        """(worker, chat_id, thread_id) for an address; worker None if
        the target isn't a numeric chat id or no bot can serve it."""
        try:
            chat_id = int(address.target)
        except (TypeError, ValueError):
            return None, None, None
        thread_id: int | None = None
        if address.thread:
            try:
                thread_id = int(address.thread)
            except ValueError:
                thread_id = None
        worker = self._plugin._reply_worker(address.connection or None, chat_id=chat_id)
        return worker, chat_id, thread_id

    def deliver_prompt(self, address: Address, prompt: Any) -> DeliveryResult:
        worker, chat_id, thread_id = self._resolve(address)
        if worker is None or chat_id is None:
            return DeliveryResult(ok=False, error=f"no telegram bot for {address.to_str()}")
        markup = _build_prompt_keyboard(prompt)
        res = worker.send_text(
            chat_id, prompt.body, thread_id=thread_id, reply_markup=markup,
        )
        return DeliveryResult(
            ok=bool(res.get("ok")), ref=str(res.get("message_id") or ""),
            mode="buttons", error=res.get("error"),
        )

    def deliver_text(
        self, address: Address, text: str, *, in_reply_to: str | None = None
    ) -> DeliveryResult:
        worker, chat_id, thread_id = self._resolve(address)
        if worker is None or chat_id is None:
            return DeliveryResult(ok=False, error=f"no telegram bot for {address.to_str()}")
        res = worker.send_text(chat_id, text, thread_id=thread_id)
        return DeliveryResult(
            ok=bool(res.get("ok")), ref=str(res.get("message_id") or ""),
            mode="text", error=res.get("error"),
        )

    def close_prompt(self, address: Address, ref: str, prompt: Any) -> None:
        """Retract the buttons and stamp the outcome on the original
        message once the prompt resolves."""
        worker, chat_id, _ = self._resolve(address)
        if worker is None or chat_id is None or not ref:
            return
        if prompt.state == "answered":
            choice = prompt.choice_by_id(prompt.answer_choice or "")
            label = choice.label if choice else (prompt.answer_choice or "?")
            outcome = f"✅ {label}"
            if prompt.answered_by:
                outcome += f" — {prompt.answered_by}"
        elif prompt.state == "expired":
            outcome = "⏰ Expired (no response)."
        else:
            outcome = f"({prompt.state})"
        with contextlib.suppress(ValueError, TypeError):
            worker.edit_message_text(
                chat_id, int(ref), f"{prompt.body}\n\n{outcome}", reply_markup=None,
            )


PLUGIN = TelegramPlugin()


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compat shim mirroring `gateway._legacy_on_load`. The real load
    path is the SDK adapter; this is here so tests can drive the
    plugin directly without booting the full registry."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "events.subscribe", "events.emit",
            "workers.spawn", "agents.list", "agents.send",
            "api.register", "cli.register",
            "channels.register", "prompts.read", "prompts.write",
            "vault.read", "vault.write", "vault.delete",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
        top_level_cli=True,
        vault_keys=["TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN_*", "TELEGRAM_WEBHOOK_SECRET"],
    )
    PLUGIN.on_load(host)
