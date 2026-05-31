"""
Messaging plugin — user-facing surface for the agent-to-agent inbox.

Architecture: data layer lives in core (`relaydeck/messages.py` +
`agent_messages` table in `relaydeck/db.py`). The orchestrator owns
`send_message_to` (the actual send + push). This plugin owns the
**surface**:

  - CLI: `relaydeck workspace message`, `relaydeck workspace inbox`,
    `relaydeck message show <id>`, `relaydeck agent send` (alias).
  - SKILL.md materialization so agents discover the CLI on startup.
  - Plugin settings for default format + skill content + toggle.

CLI commands always reach the **running daemon over HTTP** — never the
in-process orchestrator. The CLI runs in a separate process with empty
`_instances`; only the daemon can push into a live PTY. If the daemon
is unreachable, the CLI falls back to direct-SQLite enqueue and warns
the operator. Messages drain into the receiving PTY on next start.

CLI registration uses `extend_cli` to add commands onto the *existing*
`workspace` group (the declarative `host.cli.command` API only supports a
plugin's own subgroup or top-level).

`declared_capabilities`:
  - `events.subscribe` — workspace.added / .updated / .removed
  - `cli.register`     — `relaydeck message show <id>` (top-level command)

`top_level_cli = true` is kept because `relaydeck message show` lives at
top level (sibling of `relaydeck workspace`), not under a `messaging`
subgroup. `top_level_api` isn't set — messaging exposes no HTTP
routes of its own (the data plane lives on
`/api/workspaces/{ws}/messages`, owned by the daemon's core API).
The UI tab is declarative in `plugin.toml`.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from relaydeck.sdk import Plugin, PluginContext, PluginEventBus, PluginHost

logger = logging.getLogger(__name__)

PLUGIN_NAME = "messaging"


# ── Built-in defaults ───────────────────────────────────────────────


DEFAULT_FORMAT = "[relay from={from} id={id}] {body}"
SKILL_NAME = "relaydeck-cli"  # the dir name under workspaces/<ws>/runtime/skills/

_BUILTIN_SKILL_PATH = Path(__file__).resolve().parent / "SKILL.md"


def _builtin_skill() -> str:
    try:
        return _BUILTIN_SKILL_PATH.read_text()
    except OSError:
        # Fallback content if the file got removed somehow.
        return (
            "# relaydeck CLI\n\n"
            "Use `relaydeck workspace message \"<body>\"` to broadcast.\n"
            "Add `--agent <id>` to target one peer.\n"
            "Use `relaydeck workspace inbox` to read your inbox.\n"
        )


# ── Live settings access ────────────────────────────────────────────


def _setting(key: str, default: Any) -> Any:
    """Read live so a settings change in the dashboard takes effect on
    the next call — no plugin reload needed."""
    from relaydeck.plugin_settings import get_setting
    return get_setting(PLUGIN_NAME, key, default=default)


def _default_format() -> str:
    v = _setting("default_format", DEFAULT_FORMAT)
    return str(v) if v else DEFAULT_FORMAT


def _skill_content() -> str:
    v = _setting("skill_content", None)
    if isinstance(v, str) and v.strip():
        return v
    return _builtin_skill()


def _bool_setting(key: str, default: bool) -> bool:
    v = _setting(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


def _inject_skill_enabled() -> bool:
    return _bool_setting("inject_skill", True)


def _nudge_enabled() -> bool:
    return _bool_setting("nudge_unanswered", True)


# ── Skill provider (consumed by the bundled `skills` plugin) ─────────
#
# Materialization itself is owned by the generic `[plugin.skills]`
# consumer in `plugins/skills/`. Messaging just declares
# `relaydeck-cli = "SKILL.md"` in its plugin.toml and exposes two provider
# hooks the skills manager calls: which workspaces should receive the
# skill, and (optionally) the content to write.


def _workspace_has_messaging(config_home: Path, workspace: str) -> bool:
    """Check the workspace's agent.toml for `messaging` in plugins=[].

    The skill should only ship to workspaces that opted into messaging
    — otherwise enabling the plugin globally would silently shove a
    skill into every workspace's agent prompt, even ones that don't
    use peer messaging at all.
    """
    toml_path = config_home / "workspaces" / workspace / "agent.toml"
    if not toml_path.exists():
        return False
    try:
        from relaydeck.config import load_config_file
        data = load_config_file(toml_path)
        plugins = data.get("workspace", {}).get("plugins", [])
        return "messaging" in plugins
    except Exception:
        return False


# ── SDK Plugin ──────────────────────────────────────────────────────


class MessagingPlugin(Plugin):
    """Workspace-scoped peer messaging surface.

    Workspaces declare `messaging` in their agent.toml plugins list to
    opt in. When opted in:
      - the relaydeck-cli SKILL.md is materialized into the workspace so the
        agent's harness picks it up on next start,
      - the workspace's agents see the chat-block format `[relay from=…]`
        when they receive a message,
      - the CLI commands work against this workspace.

    The plugin's CLI surface is unusual: it extends the existing
    `workspace` group (for `message` and `inbox`), replaces the
    `relaydeck agent send` stub, and adds a top-level `message` group for
    `relaydeck message show <id>`. The declarative `host.cli.command` API
    can't reach into existing groups, so we use the `extend_cli`
    escape hatch.
    """

    # Per-(agent, msg) nudge ledger so we remind about an unanswered peer
    # message at most once (the agent going idle again right after the nudge
    # must not re-fire). The entry persists for as long as the obligation is
    # still owed, then is dropped once the message has been answered — so the
    # "once per message" guarantee is permanent for a given obligation, while
    # memory stays bounded by the *live* owed-set rather than growing one row
    # per message for the daemon's lifetime (see _prune_resolved). Plus a
    # light per-agent cooldown so a backlog can't burst.
    _NUDGE_COOLDOWN_S = 30.0
    # The per-agent cooldown ledger (`_last_nudge_at`) is purely transient
    # bookkeeping; entries older than this are dropped to bound its size. Far
    # longer than the cooldown itself, so pruning never affects cooldown
    # correctness. (The per-message ledger is NOT aged out — ageing it would
    # re-arm nudges for a still-unanswered message, so it is pruned by
    # resolved-status instead.)
    _COOLDOWN_LEDGER_TTL_S = 6 * 3600.0

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self._config_home = host.config_home
        self._nudged: dict[tuple[str, str], float] = {}
        self._last_nudge_at: dict[str, float] = {}
        # Reply-owed safety net: when an agent ends its turn (→ idle) still
        # holding an unanswered peer message, inject ONE gentle PTY reminder
        # to run `relaydeck reply`. Transcripts are invisible to senders, so
        # a forgotten reply is otherwise silently lost. See _on_status_changed.
        try:
            host.events.subscribe("agent.status_changed", self._on_status_changed)
        except Exception:
            logger.debug("messaging: could not subscribe to agent.status_changed")
        # Skill materialization is owned by the bundled `skills` plugin
        # (the generic `[plugin.skills]` consumer). It picks up this
        # plugin's `relaydeck-cli` skill via the provider hooks below and
        # reacts to workspace lifecycle on its own.

    def on_unload(self) -> None:
        # Don't strip materialized skills here — the skills plugin removes
        # them on `system.plugin.unloaded` for this plugin. Leaving cleanup
        # to the single owner avoids double-handling.
        pass

    def on_settings_changed(self, new_values: dict[str, Any]) -> None:
        """`inject_skill` / `skill_content` change what gets materialized;
        signal the skills plugin to re-sync. `default_format` takes effect
        on the next send (read live)."""
        del new_values
        try:
            self.host.events.emit("plugin.skills.changed", {"plugin": PLUGIN_NAME})
        except Exception:
            pass

    # ── Reply-owed nudge (idle-edge safety net) ─────────────────────

    def _on_status_changed(self, event: Any) -> None:
        """On working→idle, remind the agent of any unanswered peer message.

        Non-invasive by construction: fires only on the idle edge, only when
        the toggle is on, only for genuine peer messages (initiating messages
        from a registered peer — see list_unanswered_peer_messages), at most
        once per message, with a short per-agent cooldown. If the agent
        ignores the nudge, we do NOT re-nudge that message again (the ledger
        entry survives until the obligation is answered) — the standing
        obligation surfaces to the operator via `inbox --awaiting-reply`."""
        try:
            if not _nudge_enabled():
                return
            data = getattr(event, "data", None) or {}
            if data.get("to") != "idle":
                return
            agent_id = str(data.get("agent_id") or data.get("id") or "")
            if not agent_id:
                return
            self._nudge_unanswered(agent_id)
        except Exception as exc:  # never let a handler break the bus
            logger.debug("messaging: nudge handler error: %s", exc)

    def _prune_cooldown_ledger(self, now: float) -> None:
        """Bound the transient per-agent cooldown ledger (memory guard).

        Drops `_last_nudge_at` entries older than ``_COOLDOWN_LEDGER_TTL_S``.
        Safe because the cooldown window is only ``_NUDGE_COOLDOWN_S`` (30s),
        far shorter than the TTL, so an aged-out entry's cooldown has long
        since elapsed. The per-message ledger (`_nudged`) is deliberately NOT
        aged out here — see _prune_resolved."""
        if not self._last_nudge_at:
            return
        ttl = self._COOLDOWN_LEDGER_TTL_S
        self._last_nudge_at = {
            k: t for k, t in self._last_nudge_at.items() if (now - t) < ttl
        }

    def _prune_resolved(self, agent_id: str, owed_ids: set[str]) -> None:
        """Drop `_nudged` entries for `agent_id` whose obligation is resolved.

        A message leaves the owed-set once it has been answered (a reply
        threads off it) — at which point its ledger row is dead weight and is
        removed. A *still-owed* message keeps its row, so it is never
        re-nudged. This bounds `_nudged` to the live owed-set across the fleet
        (not one row per message ever sent) WITHOUT ever re-arming a nudge for
        an unanswered message — the bug a wall-clock TTL would have introduced."""
        self._nudged = {
            k: t for k, t in self._nudged.items()
            if k[0] != agent_id or k[1] in owed_ids
        }

    def _nudge_unanswered(self, agent_id: str) -> None:
        now = time.time()
        self._prune_cooldown_ledger(now)
        last = self._last_nudge_at.get(agent_id, 0.0)
        if (now - last) < self._NUDGE_COOLDOWN_S:
            return

        orch = getattr(self.host, "_orchestrator", None)
        if orch is None:
            return
        instance = None
        try:
            instance = orch.get_running_instance(agent_id)
        except Exception:
            instance = None
        if instance is None or not hasattr(instance, "send_message"):
            return  # only nudge a live PTY

        try:
            from relaydeck.messages import list_unanswered_peer_messages
            db_path = getattr(orch, "db_path", None)
            pending = list_unanswered_peer_messages(agent_id, limit=10, db_path=db_path)
        except Exception as exc:
            logger.debug("messaging: pending-reply lookup failed for %s: %s", agent_id, exc)
            return

        # Garbage-collect ledger rows for obligations this agent has since
        # answered, keeping memory bounded to the live owed-set (see
        # _prune_resolved). Still-owed messages keep their row, so a message
        # already nudged is never re-nudged.
        self._prune_resolved(agent_id, {m.id for m in pending})

        # Pick the oldest obligation we haven't nudged yet.
        target = next((m for m in pending if (agent_id, m.id) not in self._nudged), None)
        if target is None:
            return

        n_more = sum(1 for m in pending if (agent_id, m.id) not in self._nudged) - 1
        extra = f" (+{n_more} more unanswered)" if n_more > 0 else ""
        reminder = (
            f"[relaydeck] You ended your turn without replying to {target.from_id} "
            f"(msg {target.id}). Senders read the inbox, not your transcript — "
            f'run: relaydeck reply {target.id} "<your reply>"{extra}'
        )
        try:
            instance.send_message(reminder)
            self._nudged[(agent_id, target.id)] = now
            self._last_nudge_at[agent_id] = now
            logger.info("messaging: nudged %s about unanswered %s", agent_id, target.id)
        except Exception as exc:
            logger.debug("messaging: failed to inject nudge to %s: %s", agent_id, exc)

    # ── Skill provider hooks (called by the skills manager) ─────────

    def skill_target_workspaces(self, all_workspaces: list[str]) -> list[str]:
        """Ship the relaydeck-cli skill to workspaces that opted into
        messaging — unless the operator turned `inject_skill` off."""
        if not _inject_skill_enabled():
            return []
        return [w for w in all_workspaces if _workspace_has_messaging(self._config_home, w)]

    def skill_content(self, skill_name: str, source_text: str) -> str:
        """Honor the `skill_content` setting override; otherwise the
        built-in SKILL.md (source_text is the manifest file)."""
        del skill_name, source_text
        return _skill_content()

    def get_settings_schema(self) -> list[dict]:
        """Settings schema returned to the dashboard form. Kept in
        code (not the manifest) because the textarea default carries
        the runtime contents of SKILL.md, which has multi-line content
        the TOML default doesn't accept cleanly."""
        return [
            {
                "key": "default_format",
                "type": "textarea",
                "label": "Default message format template",
                "description": (
                    "Template applied when a message is pushed to a "
                    "receiving agent's PTY. Placeholders: {from}, {to}, "
                    "{body}, {id}, {in_reply_to}, {ts}. The default's "
                    "`[relay ...]` prefix makes peer messages unambiguous "
                    "to the receiving agent's model — the relaydeck-cli skill "
                    "teaches the pattern and how to reply "
                    "(`--in-reply-to {id}`). Per-agent override: set "
                    "message_format in the agent's spec. Per-message "
                    "override: pass format= via API/CLI."
                ),
                "default": DEFAULT_FORMAT,
            },
            {
                "key": "inject_skill",
                "type": "bool",
                "label": "Inject relaydeck-cli SKILL.md into workspaces",
                "description": (
                    "When on, every registered workspace gets a "
                    "runtime/skills/relaydeck-cli/SKILL.md that teaches "
                    "agents the messaging CLI. Pi/codex harnesses read "
                    "both the user skills dir and the runtime skills "
                    "dir."
                ),
                "default": True,
            },
            {
                "key": "nudge_unanswered",
                "type": "bool",
                "label": "Remind agents about unanswered peer messages",
                "description": (
                    "When on, an agent that ends its turn (goes idle) while "
                    "still holding a peer message it never replied to gets one "
                    "gentle terminal reminder to run `relaydeck reply`. Catches "
                    "the common failure where a model answers in its transcript "
                    "(invisible to the sender) but forgets the durable reply. "
                    "Fires at most once per message, with a short cooldown."
                ),
                "default": True,
            },
            {
                "key": "skill_content",
                "type": "textarea",
                "label": "Skill content",
                "description": (
                    "The SKILL.md body written into "
                    "runtime/skills/relaydeck-cli/. Leave blank to use the "
                    "built-in template. Changes are re-materialized to "
                    "every workspace immediately on save."
                ),
                "default": _builtin_skill(),
            },
            {
                "key": "confirm_delivery",
                "type": "bool",
                "label": "Confirm delivery via PTY echo",
                "description": (
                    "When on, a message is only marked 'delivered' after "
                    "its id is observed echoing back in the receiving "
                    "agent's terminal — proof the input widget accepted "
                    "it, not just that the bytes were written. Unconfirmed "
                    "messages stay queued and the harness retries them on "
                    "its next live-drain. Maximises assurance at the cost "
                    "of an occasional duplicate send if an echo is missed. "
                    "Per-agent override: set confirm_delivery in the "
                    "agent's config. Default off."
                ),
                "default": False,
            },
        ]

    # Escape hatch: SDK declarative host.cli can't attach to existing
    # top-level groups (`workspace`, `agent`), so messaging hooks
    # directly into the root click group instead.
    def extend_cli(self, root) -> None:
        _register_cli(root)


PLUGIN = MessagingPlugin()


# ── Module-level shims for tests + legacy callers ───────────────────
#
# Older tests import these names directly. They forward into the
# `MessagingPlugin` instance, keeping a stable module-level API while
# the SDK-style class owns the actual logic.


def _on_load(ctx: PluginContext) -> None:
    _legacy_on_load(ctx)


def _on_unload() -> None:
    PLUGIN.on_unload()


def _on_settings_changed(new_values: dict[str, Any]) -> None:
    PLUGIN.on_settings_changed(new_values)


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compat shim. The real loader path is the SDK adapter; this
    helper exists for the small number of tests that build a
    PluginContext by hand."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "events.subscribe",
            "events.emit",
            "cli.register",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
        top_level_cli=True,
    )
    PLUGIN.on_load(host)


# ── CLI: workspace-scoped messaging ─────────────────────────────────


def _resolve_workspace(explicit: str | None) -> str | None:
    from relaydeck.state import get_current_workspace
    if explicit:
        return explicit
    return get_current_workspace()


def _resolve_from(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("RELAYDECK_AGENT_ID")
    if env and env.strip():
        return env.strip()
    return "user"


def _is_registered_agent(agent_id: str, db_path: str) -> bool:
    """True if `agent_id` names a real registered agent (agents table).

    Reply targets that aren't registered have no inbox to reply into —
    e.g. a synthetic automation sender (`github` poller, `model` action)
    or an agent that has since been removed. Fails open: if the lookup
    itself errors, return True so a legitimate reply is never blocked."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM agents WHERE id=? LIMIT 1", (agent_id,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return True


def _parse_telegram_chat_id(from_id: str) -> int | None:
    """Extract chat_id from a Telegram sender id (`telegram:<chat_id>`)."""
    if not from_id.startswith("telegram:"):
        return None
    raw = from_id.split(":", 1)[1].strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _telegram_thread_from_body(body: str) -> int | None:
    """Forum topic id from the attribution prefix (`… topic=8 …:`)."""
    import re

    head = body.split(":", 1)[0] if ":" in body else body
    m = re.search(r"\btopic=(\d+)\b", head)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _telegram_format_hint(body: str, explicit: str | None) -> str | None:
    """Pass-through explicit format or infer html when body carries Telegram tags."""
    if explicit and str(explicit).strip():
        return str(explicit).strip().lower()
    import re
    if re.search(
        r"<\s*/?\s*(b|i|u|s|code|pre|a|blockquote|tg-spoiler)\b",
        body,
        re.IGNORECASE,
    ):
        return "html"
    return None


def _reply_via_telegram(
    chat_id: int, body: str, *, thread_id: int | None = None,
    in_reply_to: str | None = None, fmt: str | None = None,
) -> tuple[bool, dict | str]:
    """POST to the daemon's telegram reply endpoint."""
    import json
    import urllib.error
    import urllib.request

    from relaydeck.auth import read_token
    from relaydeck.state import get_daemon_ca, get_daemon_url

    payload: dict[str, Any] = {"chat_id": chat_id, "body": body}
    if thread_id is not None:
        payload["thread_id"] = thread_id
    if in_reply_to:
        payload["in_reply_to"] = in_reply_to
    hinted = _telegram_format_hint(body, fmt)
    if hinted:
        payload["format"] = hinted

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
        base + "/api/plugins/telegram/reply",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            raw = r.read()
            return True, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _send_via_daemon(workspace: str, body_payload: dict) -> tuple[bool, dict | str]:
    """POST to the daemon's messages endpoint. Returns (ok, response_or_error)."""
    import json
    import urllib.error
    import urllib.request

    from relaydeck.auth import read_token
    from relaydeck.state import get_daemon_url

    url = get_daemon_url().rstrip("/") + f"/api/workspaces/{workspace}/messages"
    data = json.dumps(body_payload).encode()
    headers = {"Content-Type": "application/json"}
    tok = read_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(
        url, data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            payload = json.loads(r.read())
        return True, payload
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, f"HTTP {exc.code}: {body}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _wait_for_delivery(
    msg_ids: list[str],
    *,
    timeout: float,
    poll_interval: float = 0.25,
    console=None,
) -> set[str]:
    """Poll the local DB until each `msg_id` is marked `injected_at`
    (i.e. successfully written to the agent's PTY by the harness's
    live-drain), or `timeout` seconds elapse.

    Returns the set of msg_ids that flipped to delivered before the
    timeout. The caller decides what to print for the rest. Polling
    via the local SQLite file is cheap (~ms) and avoids a per-message
    HTTP roundtrip; the harness writes `injected_at` from the same
    daemon process, so the read sees it as soon as it's committed.
    """
    import time as _time
    from relaydeck.messages import list_one

    db_path = str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db")
    deadline = _time.monotonic() + max(0.0, timeout)
    delivered: set[str] = set()
    remaining = set(msg_ids)

    if console is not None:
        console.print(
            f"  [dim]waiting up to {timeout:g}s for live-drain "
            f"({len(remaining)} message{'s' if len(remaining)!=1 else ''})…[/]"
        )

    while remaining and _time.monotonic() < deadline:
        for mid in list(remaining):
            row = list_one(mid, db_path=db_path)
            if row is not None and row.injected_at is not None:
                delivered.add(mid)
                remaining.discard(mid)
        if remaining:
            _time.sleep(poll_interval)
    return delivered


def _fallback_enqueue(
    workspace: str,
    body: str,
    *,
    from_id: str,
    agent: str | None,
    in_reply_to: str | None,
    include_self: bool,
) -> list[str]:
    """Daemon unreachable → write straight to SQLite so messages aren't
    lost. The next time the target agent starts, the drain block in
    HarnessAgent.run will push them to the PTY."""
    from relaydeck.config import load_workspace_registry
    from relaydeck.messages import enqueue_workspace_messages

    registry = {w.name: w for w in load_workspace_registry()}
    if workspace not in registry:
        return []

    return enqueue_workspace_messages(
        workspace, body,
        from_id=from_id,
        agent=agent,
        in_reply_to=in_reply_to,
        include_self=include_self,
    )


def _print_inbox_line(console, m, *, full: bool) -> None:
    """Render one inbox row consistently across the static-list view
    and the live-tail view. Truncates to 80 chars by default, prints
    full body when `--full`."""
    state = "delivered" if m.injected_at else "pending"
    body = m.body if full else m.body[:80]
    reply = f"  [dim]in-reply-to:[/] {m.in_reply_to}" if m.in_reply_to else ""
    console.print(
        f"[cyan]{m.id}[/]  [dim]from[/] [bold]{m.from_id}[/] "
        f"[dim]→[/] [bold]{m.to_id}[/]  [green]{state}[/]{reply}"
    )
    console.print(body)


def _stream_workspace_messages(
    workspace: str,
    *,
    agent_filter: str | None,
    console,
    full: bool,
    max_reconnects: int | None = None,
    reconnect_backoff_s: float = 1.0,
) -> None:
    """SSE consumer for `relaydeck workspace inbox -f`.

    Connects to `/api/workspaces/{ws}/messages/stream` and prints
    each `agent.message` event. We use urllib over httpx to keep
    the dependency surface flat — SSE is just `text/event-stream`
    framed as `data: <json>\\n\\n`, the parser fits in 20 lines.

    Resilient by design. Three classes of problem are handled
    explicitly so the inbox pane in `relaydeck workspace view` survives
    routine daemon restarts and network blips:

      - **HTTP-level rejection (401/404/etc.)**: print the body and
        stop. Retrying won't help — the operator has to fix the
        auth or the URL.
      - **Connect failure / dropped stream**: print a dim notice
        and reconnect after `reconnect_backoff_s` (doubling, capped
        at 30s). Stops only on Ctrl-C or after `max_reconnects`
        if that's set (the CLI passes None → retry forever).
      - **Bad SSE frame**: ignore the single frame, keep tailing.

    Honors the CA pin (state.yaml `daemon_ca`) so the dev TLS path
    works.
    """
    import ssl
    import time as _time
    import urllib.error
    import urllib.request

    from relaydeck.auth import read_token
    from relaydeck.state import get_daemon_ca, get_daemon_url

    daemon_url = get_daemon_url().rstrip("/")
    url = f"{daemon_url}/api/workspaces/{workspace}/messages/stream"

    def _build_request():
        headers = {"Accept": "text/event-stream"}
        tok = read_token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        return urllib.request.Request(url, headers=headers)

    ctx: ssl.SSLContext | None = None
    if daemon_url.startswith("https://"):
        ca = get_daemon_ca()
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()

    backoff = max(0.25, float(reconnect_backoff_s))
    attempt = 0
    while True:
        attempt += 1
        try:
            # No timeout — SSE is long-lived. urlopen will block on
            # iteration until either a frame arrives or the connection
            # drops, at which point the for-loop ends and we fall
            # through to the reconnect logic.
            resp = urllib.request.urlopen(_build_request(), context=ctx)
        except urllib.error.HTTPError as exc:
            # 4xx/5xx is configuration / auth — don't retry. The
            # operator needs to fix something out-of-band.
            body = exc.read().decode("utf-8", "replace")
            console.print(f"[red]✗[/] HTTP {exc.code}: {body}")
            return
        except (urllib.error.URLError, OSError) as exc:
            console.print(
                f"[yellow]·[/] daemon unreachable ({exc}); "
                f"reconnecting in {backoff:.1f}s — Ctrl-C to stop"
            )
            if max_reconnects is not None and attempt > max_reconnects:
                return
            try:
                _time.sleep(backoff)
            except KeyboardInterrupt:
                return
            backoff = min(backoff * 2, 30.0)
            continue

        # Connected. Reset the backoff so the *next* drop starts
        # cheap; only sustained failure climbs to 30s.
        backoff = max(0.25, float(reconnect_backoff_s))

        with resp:
            try:
                _consume_sse_stream(
                    resp, agent_filter=agent_filter,
                    console=console, full=full,
                )
            except (urllib.error.URLError, OSError, ConnectionError) as exc:
                console.print(
                    f"[yellow]·[/] stream dropped ({exc}); reconnecting…"
                )

        # Stream ended (server closed, connection dropped, or our
        # consumer returned). If max_reconnects is None we loop;
        # otherwise we honor the cap so tests can stop after one.
        if max_reconnects is not None and attempt >= max_reconnects:
            return
        # A clean server-close (daemon shutdown) is the most common
        # cause here. Brief pause so we don't tight-loop if the
        # daemon is gone for good.
        try:
            _time.sleep(backoff)
        except KeyboardInterrupt:
            return


def _consume_sse_stream(
    resp,
    *,
    agent_filter: str | None,
    console,
    full: bool,
) -> None:
    """Drain one open SSE response. Returns when the response
    closes, raises on transport errors so the outer reconnect loop
    can decide what to do."""
    buf = ""
    for line_bytes in resp:
        line = line_bytes.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            # End of one SSE event. Parse accumulated data.
            if buf:
                _emit_streamed_message(
                    buf, agent_filter=agent_filter,
                    console=console, full=full,
                )
            buf = ""
            continue
        if line.startswith(":"):
            # Comment / heartbeat; ignore.
            continue
        if line.startswith("data:"):
            # SSE allows multi-line data blocks; concatenate with
            # newline. In practice the server sends single-line
            # JSON, but we honor the protocol.
            if buf:
                buf += "\n"
            buf += line[5:].lstrip()


def _emit_streamed_message(
    raw: str,
    *,
    agent_filter: str | None,
    console,
    full: bool,
) -> None:
    """Parse one SSE `data:` payload (JSON) and print one inbox row.
    `agent_filter`, if set, restricts to messages whose `to` matches —
    same semantics as `--agent` in the static view.

    Prints a leading blank line so back-to-back messages in a busy
    inbox stay visually separated (the static-view code does the
    same via a `console.print()` between rows in `--full` mode)."""
    import json
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return
    if agent_filter and data.get("to") != agent_filter:
        return
    # Make a tiny ad-hoc row with the same attribute names as
    # the MessageRow dataclass so _print_inbox_line works without
    # a branch. SimpleNamespace would do but a dynamic class makes
    # the intent explicit at the call site.
    class _RowShim:
        pass
    row = _RowShim()
    row.id = data.get("id", "?")
    row.from_id = data.get("from", "?")
    row.to_id = data.get("to", "?")
    row.body = str(data.get("body") or "")
    row.injected_at = float(data.get("ts", 0)) if data.get("injected") else None
    row.in_reply_to = data.get("in_reply_to")
    console.print()  # divider between live-tail entries
    _print_inbox_line(console, row, full=full)


def _register_cli(cli) -> None:
    """Attach messaging commands to the root relaydeck CLI. Called from
    `MessagingPlugin.extend_cli` and exported as a module-level
    function so tests can wire commands without standing up the full
    plugin lifecycle."""
    import click
    from rich.console import Console
    from rich.table import Table

    console = Console()

    ws_group = cli.commands.get("workspace") if hasattr(cli, "commands") else None
    if ws_group is None:
        return  # No workspace group present — happens in some test setups.

    @ws_group.command("message")
    @click.argument("body")
    @click.option("--agent", "agent_id", default=None,
                  help="Target one agent in the workspace (default: broadcast)")
    @click.option("--from", "from_id", default=None,
                  help="Sender ID (defaults to RELAYDECK_AGENT_ID env, else 'user')")
    @click.option("--workspace", "-w", "ws_override", default=None,
                  help="Override active workspace")
    @click.option("--in-reply-to", "in_reply_to", default=None,
                  help="Mark this message as a reply to a prior message id")
    @click.option("--format", "format_override", default=None,
                  help="Per-message format template override")
    @click.option("--include-self/--exclude-self", default=False,
                  help="Include the sender when broadcasting (default: exclude)")
    @click.option("--wait", "wait_seconds", default=5.0, type=float,
                  help="Seconds to wait for delivery confirmation (0 = fire-and-forget)")
    def workspace_message(
        body: str, agent_id: str | None, from_id: str | None,
        ws_override: str | None, in_reply_to: str | None,
        format_override: str | None, include_self: bool,
        wait_seconds: float,
    ):
        """Send a message to the workspace fleet (or one agent in it)."""
        workspace = _resolve_workspace(ws_override)
        if not workspace:
            console.print(
                "[red]No active workspace.[/] Set one with "
                "[bold]relaydeck workspace set <name>[/], pass --workspace, "
                "or set RELAYDECK_WORKSPACE."
            )
            raise SystemExit(2)

        from_resolved = _resolve_from(from_id)

        payload = {
            "body": body,
            "from": from_resolved,
            "include_self": include_self,
        }
        if agent_id:
            payload["agent"] = agent_id
        if in_reply_to:
            payload["in_reply_to"] = in_reply_to
        if format_override:
            payload["format"] = format_override

        ok, resp = _send_via_daemon(workspace, payload)
        if ok and isinstance(resp, dict):
            injected = resp.get("injected", []) or []
            pending = resp.get("pending", []) or []
            recipients = resp.get("recipients", []) or []
            if injected:
                console.print(f"[green]✓[/] injected to: {', '.join(injected)}")
            if pending:
                stopped = [r for r in recipients
                           if not r.get("injected")
                           and r.get("status") in ("stopped", "errored", "unknown")]
                running_pending = [r for r in recipients
                                   if not r.get("injected")
                                   and r.get("status") == "running"]
                if stopped:
                    ids = ", ".join(r["msg_id"] for r in stopped)
                    agents_text = ", ".join(
                        f"{r['agent_id']}=[red]{r['status']}[/]" for r in stopped
                    )
                    console.print(
                        f"[yellow]·[/] persisted ({agents_text}): {ids}"
                    )
                    console.print(
                        "  [dim]Start the agent with "
                        "[bold]relaydeck agent start <id>[/] to deliver this "
                        "message on the next start-time drain.[/]"
                    )
                if running_pending and wait_seconds > 0:
                    pending_ids = [r["msg_id"] for r in running_pending]
                    delivered = _wait_for_delivery(
                        pending_ids, timeout=wait_seconds, console=console,
                    )
                    confirmed = [mid for mid in pending_ids if mid in delivered]
                    still_waiting = [mid for mid in pending_ids if mid not in delivered]
                    if confirmed:
                        console.print(
                            f"[green]✓[/] delivered after live-drain: "
                            f"{', '.join(confirmed)}"
                        )
                    if still_waiting:
                        console.print(
                            f"[yellow]·[/] still pending after {wait_seconds:g}s: "
                            f"{', '.join(still_waiting)}"
                        )
                        console.print(
                            "  [dim]Agent is marked running but didn't pick up "
                            "the message — check [bold]relaydeck agent list[/] and "
                            "consider [bold]relaydeck agent restart <id>[/].[/]"
                        )
                elif running_pending:
                    ids = ", ".join(r["msg_id"] for r in running_pending)
                    console.print(
                        f"[yellow]·[/] persisted (target running, not yet on PTY): {ids}"
                    )
                    console.print(
                        "  [dim]The harness will live-drain within ~2s. "
                        "Use [bold]--wait N[/] to block until delivery.[/]"
                    )
            if not injected and not pending:
                console.print("[yellow]No recipients.[/]")
            return

        console.print(f"[yellow]Warning:[/] daemon unreachable ({resp}). "
                      "Enqueuing to inbox; messages will drain on next agent start.")
        msg_ids = _fallback_enqueue(
            workspace, body,
            from_id=from_resolved, agent=agent_id,
            in_reply_to=in_reply_to, include_self=include_self,
        )
        if msg_ids:
            console.print(f"[green]✓[/] enqueued: {', '.join(msg_ids)}")
        else:
            console.print("[red]✗[/] no recipients found.")
            raise SystemExit(1)

    @ws_group.command("inbox")
    @click.option("--agent", "agent_id", default=None,
                  help="Filter to one agent's inbox")
    @click.option("--workspace", "ws_override", default=None)
    @click.option("--unread", is_flag=True, default=False,
                  help="Only undelivered messages")
    @click.option("--limit", default=20, type=int)
    @click.option("--full", is_flag=True, default=False,
                  help="Print full bodies (no truncation, one per block) "
                       "instead of the summary table.")
    @click.option("-f", "--follow", "follow", is_flag=True, default=False,
                  help="Stream new messages as they arrive (SSE-backed). "
                       "Prints the current inbox first, then tails. Ctrl-C to stop.")
    @click.option("--failed", "failed_only", is_flag=True, default=False,
                  help="Show only messages that exhausted delivery retries "
                       "(terminal 'failed' state) so you can spot stuck peers.")
    @click.option("--awaiting-reply", "awaiting_reply", is_flag=True, default=False,
                  help="Show thread-OPENING peer messages that still owe a "
                       "durable reply (delivered, from a registered agent, no "
                       "`relaydeck reply` threaded off them yet) — spot a peer "
                       "that answered in its transcript but never sent the "
                       "reply. Only initiating messages are tracked: a reply "
                       "raised inside an existing thread is not listed here "
                       "(use plain `inbox` to inspect open threads). "
                       "Narrow with --agent.")
    def workspace_inbox_cli(
        agent_id: str | None, ws_override: str | None,
        unread: bool, limit: int, full: bool, follow: bool,
        failed_only: bool, awaiting_reply: bool,
    ):
        """List messages addressed to agents in the active workspace.

        Default: a summary table with bodies truncated to ~80 chars.
        Pass --full to print each message in a multi-line block (no
        truncation); use this when an agent needs to read what a peer
        actually said. For a single message, prefer
        `relaydeck message show <id>`.

        `-f` / `--follow` switches to live-tail mode. The current
        inbox prints first as context, then new messages stream in
        as the daemon delivers them. Works through the SSE endpoint
        `/api/workspaces/{ws}/messages/stream`, so the latency is
        the same as the dashboard — typically <100ms from PTY
        delivery to your terminal.
        """
        workspace = _resolve_workspace(ws_override)
        if not workspace:
            console.print("[red]No active workspace.[/]")
            raise SystemExit(2)

        db_path = str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db")

        if failed_only:
            # Terminal failures: messages that exhausted MAX_DELIVERY_ATTEMPTS
            # and will not be retried. Surfacing them is how an operator
            # notices a stuck recipient (wrong id, perpetually-down agent).
            from relaydeck.messages import list_failed
            fails = list_failed(workspace, limit=limit, db_path=db_path)
            if agent_id:
                fails = [m for m in fails if m.to_id == agent_id]
            if not fails:
                console.print("[green]No failed messages.[/]")
                return
            console.print(f"[bold]Failed · {workspace}[/]  ({len(fails)})")
            for m in fails:
                console.print()
                console.print(
                    f"[red]{m.id}[/] [dim]from[/] {m.from_id} [dim]→[/] {m.to_id}"
                    f"  [dim]attempts[/]={m.attempts}"
                )
                if m.last_error:
                    console.print(f"  [dim]{m.last_error}[/]")
                console.print(m.body if full else m.body[:80])
            return

        if awaiting_reply:
            # Reply-owed: delivered peer messages with no `relaydeck reply`
            # threaded off them. The flip side of the agent-side idle nudge —
            # lets an operator catch a peer that answered in its transcript
            # but never sent the durable reply.
            from relaydeck.messages import list_unanswered_peer_messages
            owed = list_unanswered_peer_messages(
                to_id=agent_id, workspace=workspace, limit=limit, db_path=db_path,
            )
            if not owed:
                console.print("[green]No replies owed.[/]")
                return
            console.print(f"[bold]Awaiting reply · {workspace}[/]  ({len(owed)})")
            for m in owed:
                age = max(0, int(time.time() - float(m.ts)))
                console.print()
                console.print(
                    f"[yellow]{m.id}[/] [dim]{m.to_id} owes[/] {m.from_id}"
                    f"  [dim]{age // 60}m ago[/]"
                )
                console.print(
                    f"  [dim]reply:[/] relaydeck reply {m.id} \"<reply>\" "
                    f"[dim]--from {m.to_id}[/]"
                )
                console.print(m.body if full else m.body[:80])
            return

        from relaydeck.messages import list_workspace_inbox
        msgs = list_workspace_inbox(
            workspace, agent=agent_id, unread=unread,
            limit=limit, db_path=db_path,
        )
        if follow:
            # Print the existing inbox first so the operator has
            # context, then transition into live tail. Empty inbox
            # is fine — we still start the stream.
            for m in msgs:
                _print_inbox_line(console, m, full=full)
            console.print(
                f"[dim]· tailing {workspace}"
                f"{(' · ' + agent_id) if agent_id else ''} "
                f"(Ctrl-C to stop) ·[/]"
            )
            try:
                _stream_workspace_messages(
                    workspace, agent_filter=agent_id,
                    console=console, full=full,
                )
            except KeyboardInterrupt:
                console.print("[dim]· detached[/]")
            return

        if not msgs:
            console.print("[dim]Inbox empty.[/]")
            return

        if full:
            scope = f"{workspace}" + (f" · {agent_id}" if agent_id else "")
            console.print(f"[bold]Inbox · {scope}[/]  ({len(msgs)} messages)")
            for m in msgs:
                console.print()
                _print_inbox_line(console, m, full=True)
            return

        table = Table(title=f"Inbox · {workspace}" + (f" · {agent_id}" if agent_id else ""))
        table.add_column("id", style="cyan")
        table.add_column("from")
        table.add_column("to")
        table.add_column("state", style="green")
        table.add_column("body")
        for m in msgs:
            state = "delivered" if m.injected_at else "pending"
            table.add_row(m.id, m.from_id, m.to_id, state, m.body[:80])
        console.print(table)
        console.print(
            "[dim]Bodies truncated to 80 chars. Run with --full to see "
            "everything, or `relaydeck message show <id>` for one.[/]"
        )

    # `relaydeck message show <id>` — top-level lookup for one message's
    # full body. Lives at top level (not under `workspace`) because
    # `relaydeck workspace message <body>` is already a command and click
    # doesn't allow a group + command at the same name.
    if "message" not in cli.commands:
        @click.group("message", invoke_without_command=False)
        def message_group():
            """Read individual messages (use `workspace inbox` for listing)."""

        @message_group.command("show")
        @click.argument("msg_id")
        def message_show(msg_id: str):
            """Print one message's full body, headers, and thread info."""
            from relaydeck.messages import list_one
            db_path = str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db")
            msg = list_one(msg_id, db_path=db_path)
            if msg is None:
                console.print(f"[red]No such message:[/] {msg_id}")
                raise SystemExit(1)
            state = "delivered" if msg.injected_at else "pending"
            reply = f"\n[dim]in-reply-to:[/] {msg.in_reply_to}" if msg.in_reply_to else ""
            console.print(
                f"[cyan]{msg.id}[/]\n"
                f"[dim]from:[/] [bold]{msg.from_id}[/]\n"
                f"[dim]to:[/]   [bold]{msg.to_id}[/]\n"
                f"[dim]workspace:[/] {msg.workspace or '—'}\n"
                f"[dim]state:[/] [green]{state}[/]{reply}\n"
                f"[dim]ts:[/] {msg.ts}\n"
            )
            console.print(msg.body)

        cli.add_command(message_group)

    # `relaydeck reply <msg_id> <body>` — the can't-get-it-wrong reply path.
    # The full form (`relaydeck workspace message <body> --agent <sender>
    # --in-reply-to <msg-id>`) is easy to fumble: agents forget --agent
    # (and broadcast), or forget --in-reply-to (and break the thread).
    # `relaydeck reply` derives both from the message id itself, so a reply is
    # always routed to the original sender and threaded.
    if "reply" not in cli.commands:
        @cli.command("reply")
        @click.argument("msg_id")
        @click.argument("body")
        @click.option("--from", "from_id", default=None,
                      help="Sender ID (defaults to RELAYDECK_AGENT_ID env, else 'user')")
        @click.option("--format", "fmt", default=None,
                      type=click.Choice(["plain", "html", "markdown"]),
                      help="For Telegram senders: rich text mode (html recommended). "
                           "Auto-detected when body contains <b>, <i>, etc.")
        def reply_cmd(msg_id: str, body: str, from_id: str | None, fmt: str | None):
            """Reply to a peer message by id (infers recipient + thread).

            Looks up MSG_ID, sends BODY back to whoever sent it, with
            --in-reply-to set automatically. Equivalent to
            `relaydeck workspace message BODY --agent <sender> --in-reply-to MSG_ID`
            but you can't forget the routing or the thread link.
            """
            from relaydeck.messages import list_one
            db_path = str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db")
            msg = list_one(msg_id, db_path=db_path)
            if msg is None:
                console.print(f"[red]No such message:[/] {msg_id}")
                raise SystemExit(1)

            target = msg.from_id
            chat_id = _parse_telegram_chat_id(target)
            if chat_id is not None:
                thread_id = _telegram_thread_from_body(msg.body)
                ok, resp = _reply_via_telegram(
                    chat_id, body, thread_id=thread_id,
                    in_reply_to=msg_id, fmt=fmt,
                )
                if ok and isinstance(resp, dict) and resp.get("ok"):
                    note = (
                        " [yellow](markup invalid → sent plain)[/]"
                        if resp.get("downgraded") else ""
                    )
                    thread_note = (
                        f" thread={thread_id}" if thread_id is not None else ""
                    )
                    console.print(
                        f"[green]✓[/] sent to telegram chat {chat_id}{thread_note} "
                        f"(message_id={resp.get('message_id')}){note}"
                    )
                    return
                err = resp.get("error") if isinstance(resp, dict) else str(resp)
                console.print(f"[red]✗[/] telegram reply failed: {err}")
                raise SystemExit(1)

            if (target == "user" or target.startswith("plugin:")
                    or not _is_registered_agent(target, db_path)):
                console.print(
                    f"[yellow]·[/] {msg_id} was sent by [bold]{target}[/], "
                    "which is not a peer agent you can reply to — it may be an "
                    "automation (e.g. the github poller) or an agent that no "
                    "longer exists. Respond in your normal output instead: do "
                    "the action the message asked for."
                )
                raise SystemExit(2)

            workspace = msg.workspace
            if not workspace:
                console.print(
                    "[red]Message has no workspace context.[/] Use "
                    "[bold]relaydeck workspace message[/] with --workspace and "
                    "--agent instead."
                )
                raise SystemExit(2)

            from_resolved = _resolve_from(from_id)
            payload = {
                "body": body, "agent": target,
                "from": from_resolved, "in_reply_to": msg_id,
            }
            ok, resp = _send_via_daemon(workspace, payload)
            if ok and isinstance(resp, dict):
                if resp.get("injected"):
                    console.print(
                        f"[green]✓[/] replied to {target}: {resp['injected'][0]}"
                    )
                elif resp.get("pending"):
                    console.print(
                        f"[yellow]·[/] reply persisted (target not on PTY yet): "
                        f"{resp['pending'][0]}"
                    )
                else:
                    console.print(f"[yellow]·[/] reply queued for {target}.")
                return

            # A string `resp` starting with "HTTP " means the daemon WAS
            # reached and rejected the request (e.g. 404 unknown agent) —
            # that's not the same as "unreachable", and enqueuing would
            # just fail again. Only fall back to durable enqueue on a real
            # connection error.
            if str(resp).startswith("HTTP "):
                console.print(
                    f"[red]✗[/] daemon rejected the reply ({resp}). "
                    f"{target!r} is not a reachable agent inbox."
                )
                raise SystemExit(1)

            console.print(
                f"[yellow]Warning:[/] daemon unreachable ({resp}). "
                "Enqueuing reply; it drains on the target's next start."
            )
            msg_ids = _fallback_enqueue(
                workspace, body, from_id=from_resolved,
                agent=target, in_reply_to=msg_id, include_self=False,
            )
            if msg_ids:
                console.print(f"[green]✓[/] enqueued reply: {msg_ids[0]}")
            else:
                console.print("[red]✗[/] could not enqueue (recipient not found).")
                raise SystemExit(1)

    # `relaydeck agent send <to> <body>` — backwards-compat alias for muscle memory.
    agent_group = cli.commands.get("agent") if hasattr(cli, "commands") else None
    if agent_group is not None:
        existing_send = agent_group.commands.pop("send", None)  # drop the stub
        del existing_send  # silence linter

        @agent_group.command("send")
        @click.argument("to_agent")
        @click.argument("body")
        @click.option("--from", "from_id", default=None)
        @click.option("--workspace", "ws_override", default=None)
        def agent_send(to_agent: str, body: str, from_id: str | None,
                       ws_override: str | None):
            """Send a message to a specific agent (alias for `relaydeck workspace message --agent`)."""
            workspace = _resolve_workspace(ws_override)
            if not workspace:
                from relaydeck.db import open_db
                db_path = str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db")
                conn = open_db(db_path)
                try:
                    row = conn.execute(
                        "SELECT workspace FROM agents WHERE id = ?", (to_agent,),
                    ).fetchone()
                finally:
                    conn.close()
                if not row or not row["workspace"]:
                    console.print(
                        "[red]No workspace context.[/] "
                        "Set one with [bold]relaydeck workspace set <name>[/]."
                    )
                    raise SystemExit(2)
                workspace = row["workspace"]

            from_resolved = _resolve_from(from_id)
            payload = {"body": body, "agent": to_agent, "from": from_resolved}
            ok, resp = _send_via_daemon(workspace, payload)
            if ok and isinstance(resp, dict):
                if resp.get("injected"):
                    console.print(f"[green]✓[/] injected to {to_agent}: {resp['injected'][0]}")
                elif resp.get("pending"):
                    console.print(
                        f"[yellow]·[/] persisted (not yet delivered to PTY): "
                        f"{resp['pending'][0]}"
                    )
                return

            # A string `resp` starting with "HTTP " means the daemon WAS
            # reached and rejected the request (e.g. 404 unknown agent) — not
            # the same as "unreachable", and enqueuing would just fail again
            # (or silently queue a message to a peer that doesn't exist). Only
            # fall back to durable enqueue on a real connection error. Mirrors
            # the `reply` command's guard.
            if str(resp).startswith("HTTP "):
                console.print(
                    f"[red]✗[/] daemon rejected the message ({resp}). "
                    f"{to_agent!r} is not a reachable agent inbox."
                )
                raise SystemExit(1)

            console.print(f"[yellow]Warning:[/] daemon unreachable ({resp}). Enqueuing.")
            msg_ids = _fallback_enqueue(
                workspace, body, from_id=from_resolved,
                agent=to_agent, in_reply_to=None, include_self=False,
            )
            if msg_ids:
                console.print(f"[green]✓[/] enqueued: {msg_ids[0]}")
