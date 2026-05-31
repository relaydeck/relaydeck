"""
Route table for the telegram plugin.

`~/.relaydeck/telegram.yaml`:

    allowed_users: [12345678, 87654321]
    allowed_groups: [-100123456789]
    routes:
      - chat_id: 12345678
        workspace: demo
        direction: in
      - chat_id: -100123456789
        workspace: relaydeck
        agent: review
        require_mention: true
      - chat_id: -100987654321
        thread_id: 42
        workspace: my-workspace
        agent: indexer
      - chat_id: -100123456789
        command: triage
        workspace: demo
        agent: triager
      - workspace: demo
        agent: site-builder
        chat_id: 12345678
        direction: out

Routes are matched in precedence order:

  chat_id + thread_id + command  >  chat_id + thread_id  >
  chat_id + command              >  chat_id

If multiple chat_id-only routes match, ALL of them fire (broadcast
semantics — useful when several agents should see the same chat).

Operations on this file are atomic + 0600 — same contract as
preferences / auth-token / github.yaml.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Direction semantics:
#   in     — only forward Telegram → agent
#   out    — only forward agent → Telegram
#   in+out — both directions (default)
ALLOWED_DIRECTIONS = frozenset({"in", "out", "in+out"})


@dataclass(frozen=True)
class Connection:
    """One Telegram bot the gateway runs. A single Bot API token is one
    `getUpdates` consumer, so multiple bots = multiple Connections, each with
    its own token (resolved from `token_vault_key`) and its own poller.

    Connections are the GLOBAL layer: chats/groups/channels are discovered per
    connection and routed to workspaces/agents on top. `id` is a stable
    operator-chosen handle ("main", "ops"); `token_vault_key` names the vault
    entry holding that bot's token (the value never lands in this file)."""

    id: str
    name: str = ""
    token_vault_key: str = "TELEGRAM_BOT_TOKEN"
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "token_vault_key": self.token_vault_key,
            "enabled": self.enabled,
        }
        if self.name:
            out["name"] = self.name
        return out

    @property
    def label(self) -> str:
        return self.name or self.id


@dataclass(frozen=True)
class Route:
    """One row in the route table.

    A route matches an inbound message when every populated selector
    matches. `chat_id` None is a wildcard that matches any chat (a
    catch-all default route); a populated `chat_id` matches only that
    chat. `command` matches a leading `/cmd` (optionally `/cmd@botname`)
    in the message text. For outbound (`direction in {"out", "in+out"}`),
    the route declares "send messages from `agent` in `workspace` to
    `chat_id`" — outbound needs a concrete `chat_id` to target.
    """

    workspace: str
    chat_id: int | None = None
    thread_id: int | None = None
    command: str | None = None
    agent: str | None = None        # specific agent, else broadcast to workspace
    direction: str = "in+out"
    require_mention: bool | None = None  # None = inherit from plugin setting
    connection: str | None = None   # which Connection (bot); None = any connection

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"workspace": self.workspace, "direction": self.direction}
        if self.connection:
            out["connection"] = self.connection
        if self.chat_id is not None:
            out["chat_id"] = self.chat_id
        if self.thread_id is not None:
            out["thread_id"] = self.thread_id
        if self.command:
            out["command"] = self.command
        if self.agent:
            out["agent"] = self.agent
        if self.require_mention is not None:
            out["require_mention"] = self.require_mention
        return out

    def specificity(self) -> int:
        """Higher = more specific. Used for precedence when picking
        the matching route. chat_id+thread_id+command beats
        chat_id+thread_id, which beats chat_id+command, which beats
        chat_id-only."""
        score = 0
        if self.chat_id is not None:
            score += 1
        if self.thread_id is not None:
            score += 2
        if self.command:
            score += 4
        return score


@dataclass
class RouteTable:
    """Parsed telegram.yaml. `inbound_routes` are routes the worker
    consults when handling a new Telegram update; `outbound_routes`
    are consulted by the agent→Telegram forwarder."""

    connections: list[Connection] = field(default_factory=list)
    allowed_users: set[int] = field(default_factory=set)
    allowed_groups: set[int] = field(default_factory=set)
    routes: list[Route] = field(default_factory=list)
    # Operator opt-in: when True, the user/group allowlists are bypassed and
    # the bot responds to ANYONE. Default False keeps the fail-closed posture.
    # Set from the `open_access` plugin setting, not from telegram.yaml.
    open_access: bool = False

    @property
    def inbound_routes(self) -> list[Route]:
        return [r for r in self.routes if r.direction in ("in", "in+out")]

    @property
    def outbound_routes(self) -> list[Route]:
        return [r for r in self.routes if r.direction in ("out", "in+out")]

    def is_user_allowed(self, user_id: int) -> bool:
        """An empty allowlist is fail-closed: nobody can DM the bot
        unless they're explicitly listed. We refuse to silently accept
        every Telegram user; that'd be a great way to wake up to your
        agents being used by strangers.

        The one escape hatch is the explicit `open_access` setting — when an
        operator turns it on, every user is allowed (used for demos / fully
        public bots). It defaults off so the safe posture is the default."""
        if self.open_access:
            return True
        return user_id in self.allowed_users

    def is_chat_allowed(self, chat_id: int) -> bool:
        """Group/channel chats: must be explicitly allowed. Private
        chats (chat_id == user_id for Telegram DMs, positive integer)
        are handled by `is_user_allowed` instead. `open_access` bypasses
        this too."""
        if self.open_access:
            return True
        return chat_id in self.allowed_groups

    def match_inbound(
        self,
        chat_id: int,
        *,
        connection: str | None = None,
        thread_id: int | None = None,
        command: str | None = None,
    ) -> list[Route]:
        """Return every route matching this update, sorted by
        specificity descending. The worker fires the most-specific
        route; for routes that tie, ALL fire (broadcast). The caller
        decides which behavior to apply.

        A route with `chat_id is None` is a **wildcard** — it matches any
        chat (the "— any chat —" rows the dashboard offers, a catch-all
        default route). It has the lowest specificity, so an explicit
        chat_id route always wins over it for that chat."""
        out: list[Route] = []
        for r in self.inbound_routes:
            # A connection-scoped route only matches its connection; a route
            # with connection=None is a wildcard across all bots.
            if r.connection is not None and r.connection != connection:
                continue
            # chat_id None = wildcard (any chat); otherwise must match exactly.
            if r.chat_id is not None and r.chat_id != chat_id:
                continue
            if r.thread_id is not None and r.thread_id != thread_id:
                continue
            if r.command and r.command != (command or ""):
                continue
            # A command-routed message must actually carry that command.
            if not r.command and command:
                # commandless route + a /command message: still matches
                # (chat-level catch-all), unless a more-specific
                # command route exists for the same chat. We let
                # specificity sort that out.
                pass
            out.append(r)
        out.sort(key=lambda r: -r.specificity())
        return out

    def match_outbound(self, *, workspace: str, agent: str) -> list[Route]:
        """Return every outbound route sending from this (workspace,
        agent) pair. Sorted by chat_id for determinism."""
        out = [
            r for r in self.outbound_routes
            if r.workspace == workspace and (r.agent or "") == agent
        ]
        out.sort(key=lambda r: (r.chat_id or 0, r.thread_id or 0))
        return out


# ── on-disk load/save ───────────────────────────────────────────────


def _table_path(config_home: Path) -> Path:
    return config_home / "telegram.yaml"


def load_table(config_home: Path) -> RouteTable:
    """Read telegram.yaml. Missing or empty → empty table (fail-safe
    rather than crash the daemon during bring-up)."""
    p = _table_path(config_home)
    if not p.exists():
        return RouteTable()
    try:
        raw = p.read_text()
    except OSError:
        return RouteTable()
    if not raw.strip():
        return RouteTable()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        return RouteTable()
    return _parse_table(data)


def _parse_connections(data: dict[str, Any]) -> list[Connection]:
    out: list[Connection] = []
    seen: set[str] = set()
    for raw in (data.get("connections") or []):
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("id") or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(Connection(
            id=cid,
            name=str(raw.get("name") or "").strip(),
            token_vault_key=str(raw.get("token_vault_key") or "TELEGRAM_BOT_TOKEN").strip(),
            enabled=bool(raw.get("enabled", True)),
        ))
    return out


def _parse_table(data: dict[str, Any]) -> RouteTable:
    connections = _parse_connections(data)
    allowed_users = {int(v) for v in (data.get("allowed_users") or []) if _is_intish(v)}
    allowed_groups = {int(v) for v in (data.get("allowed_groups") or []) if _is_intish(v)}
    routes: list[Route] = []
    for raw in (data.get("routes") or []):
        if not isinstance(raw, dict):
            continue
        ws = raw.get("workspace")
        if not isinstance(ws, str) or not ws.strip():
            continue
        direction = str(raw.get("direction") or "in+out").strip()
        if direction not in ALLOWED_DIRECTIONS:
            direction = "in+out"
        require_mention = raw.get("require_mention")
        if require_mention is not None and not isinstance(require_mention, bool):
            require_mention = None
        cmd_raw = raw.get("command")
        command = (
            str(cmd_raw).lstrip("/").strip() or None
            if cmd_raw else None
        )
        agent_raw = raw.get("agent")
        agent = (str(agent_raw).strip() or None) if agent_raw else None
        conn_raw = raw.get("connection")
        connection = (str(conn_raw).strip() or None) if conn_raw else None
        routes.append(Route(
            workspace=ws.strip(),
            chat_id=int(raw["chat_id"]) if _is_intish(raw.get("chat_id")) else None,
            thread_id=int(raw["thread_id"]) if _is_intish(raw.get("thread_id")) else None,
            command=command,
            agent=agent,
            direction=direction,
            require_mention=require_mention,
            connection=connection,
        ))
    return RouteTable(
        connections=connections,
        allowed_users=allowed_users,
        allowed_groups=allowed_groups,
        routes=routes,
    )


def save_table(config_home: Path, table: RouteTable) -> None:
    """Atomically write telegram.yaml at 0600. Same pattern as
    `relaydeck/preferences.py:write_preferences` — mkstemp on the same dir,
    fchmod the FD before any bytes land, then os.replace.

    `routes` is written in insertion order so operators editing the
    file see the same row positions next time `relaydeck telegram routes
    list` prints. We don't sort keys inside each row either, for the
    same reason."""
    config_home.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {}
    # Only write `connections:` when explicitly configured — a single-bot
    # setup leaves it out and the plugin synthesizes a default connection
    # from the bot_token_vault_key setting (backward compatible).
    if table.connections:
        body["connections"] = [c.to_dict() for c in table.connections]
    body["allowed_users"] = sorted(table.allowed_users)
    body["allowed_groups"] = sorted(table.allowed_groups)
    body["routes"] = [r.to_dict() for r in table.routes]
    p = _table_path(config_home)
    fd, tmp_name = tempfile.mkstemp(prefix=".telegram.", dir=str(config_home))
    tmp = Path(tmp_name)
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
        try:
            os.write(fd, yaml.safe_dump(body, sort_keys=False).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _is_intish(v: Any) -> bool:
    if isinstance(v, bool):
        # bools are ints in Python — exclude explicitly so True doesn't
        # become chat_id=1
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, str):
        try:
            int(v)
            return True
        except ValueError:
            return False
    return False
