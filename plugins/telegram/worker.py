"""
PTB worker that bridges Telegram updates into relaydeck's orchestrator.

One worker per daemon. PTB is async; relaydeck workers are threads. We
spawn an asyncio event loop inside the worker thread and run
`application.run_polling()` to completion, exiting when the worker's
stop event fires.

The worker also serves outbound: agents send replies via the CLI,
which calls a small helper here (`send_text`) that schedules a
`send_message` coroutine on the worker's running loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class PTBNotAvailable(RuntimeError):  # noqa: N818 — "Error" suffix would shadow the helper name
    """Raised when python-telegram-bot isn't installed.

    The plugin fails closed (no worker spawned, status reports the
    miss) rather than crashing the daemon. Operators see a clear
    hint via `relaydeck telegram status`.
    """


def require_ptb() -> Any:
    """Lazy-import PTB and surface a useful error when the extra
    isn't installed. Returns the `telegram` module on success."""
    try:
        import telegram  # noqa: F401 — surface ImportError clearly
        import telegram.ext  # noqa: F401
    except ImportError as exc:
        raise PTBNotAvailable(
            "python-telegram-bot is not installed. "
            "Install with `pip install 'relaydeck-plugins[telegram]'` for split installs, "
            "or `pip install 'relaydeck[telegram]'` for the monorepo distribution."
        ) from exc
    return telegram


# ── helpers ─────────────────────────────────────────────────────────


_MENTION_RE = re.compile(r"^\s*@(\w+)\b")
_COMMAND_RE = re.compile(r"^\s*/([A-Za-z][A-Za-z0-9_]{0,31})(?:@(\w+))?(?:\s+(.*))?$", re.DOTALL)


def parse_command(text: str) -> tuple[str | None, str | None, str]:
    """Decompose a Telegram message into `(command, bot_mention, body)`.

    Returns `(None, None, original_text)` when the message isn't a
    slash-command. `bot_mention` is non-None only when the message
    used `/cmd@botname` form — the worker uses it to filter messages
    intended for other bots in the same group.
    """
    if not text:
        return (None, None, "")
    m = _COMMAND_RE.match(text)
    if not m:
        return (None, None, text)
    return (m.group(1).lower(), m.group(2), (m.group(3) or "").strip())


def message_mentions_bot(text: str, bot_username: str) -> bool:
    """True iff `text` starts with `@<bot_username>`. We compare
    case-insensitively because Telegram usernames are case-folded by
    the server. This is the "require mention" gate in groups."""
    if not text or not bot_username:
        return False
    m = _MENTION_RE.match(text)
    if not m:
        return False
    return m.group(1).lower() == bot_username.lower()


# ── the worker target ───────────────────────────────────────────────


class TelegramWorker:
    """Owns the PTB Application, its asyncio loop, and the inbound
    dispatch glue. Methods are called from two threads:

      * `run(worker)` — invoked by relaydeck.workers in a dedicated thread.
        Sets up the loop, runs PTB to completion, tears down.
      * `send_text(...)` — invoked from any thread (CLI, agent reply
        forwarder). Submits a coroutine onto the running loop via
        `asyncio.run_coroutine_threadsafe`.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        on_text: Callable[..., None],
        on_callback: Callable[..., None] | None = None,
        require_mention_in_groups: bool = True,
        reactions: bool,
        poll_timeout_s: float,
        mode: str = "polling",
        webhook_url: str = "",
        webhook_secret: str = "",
        on_status: Callable[[str, dict], None] | None = None,
        bot_commands: list[tuple[str, str]] | None = None,
    ) -> None:
        self._bot_token = bot_token
        # (command, description) pairs pushed to Telegram via setMyCommands on
        # startup so typing `/` in the chat shows an autocomplete menu. None or
        # empty = leave the bot's command list untouched.
        self._bot_commands = list(bot_commands or [])
        self._on_text = on_text
        # Optional inbound callback-query hook: fires when a human taps an
        # inline-keyboard button (interactive prompts). ctx mirrors the
        # text path plus `callback_data` + `callback_query_id`.
        self._on_callback = on_callback
        # Lifecycle hook: called with (state, info) on each transition
        # (ready/error). Lets plugin.py emit `telegram.connection.changed` so
        # the dashboard flips "starting…" → "connected" without a reload.
        self._on_status = on_status
        self._require_mention = require_mention_in_groups
        self._reactions = reactions
        self._poll_timeout_s = poll_timeout_s
        self._mode = mode if mode in ("polling", "webhook") else "polling"
        self._webhook_url = webhook_url
        self._webhook_secret = webhook_secret
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app: Any = None
        self._bot_username: str = ""
        self._bot_id: int = 0
        self._ready = threading.Event()
        self._stopped = threading.Event()
        # Liveness for the dashboard health line: when we last received an
        # update from Telegram, and the last error string (if any).
        self._last_update_ts: float = 0.0
        self._last_error: str = ""

    # --- worker target (driven by relaydeck.workers) -------------------

    def run(self, worker: Any) -> None:
        require_ptb()  # raises PTBNotAvailable if the extra isn't installed
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            MessageHandler,
            filters,
        )

        async def _async_main() -> None:
            self._loop = asyncio.get_running_loop()
            app: Any = (
                Application.builder()
                .token(self._bot_token)
                .build()
            )
            self._app = app

            # Token + webhook self-check. If a webhook is set on the
            # account, getUpdates won't deliver — refuse to silently
            # start a doomed poller.
            bot = app.bot
            try:
                me = await bot.get_me()
            except Exception as exc:
                logger.error("telegram: getMe failed (%s) — token likely invalid", exc)
                self._last_error = f"getMe failed: {exc}"
                self._stopped.set()
                self._emit_status("error", reason=self._last_error)
                return
            self._bot_username = me.username or ""
            self._bot_id = me.id

            # Push the slash-command menu so Telegram offers autocomplete when
            # a user types `/`. Best-effort — a failure here must not stop the
            # poller (the commands still work without the menu hint).
            if self._bot_commands:
                from telegram import BotCommand
                try:
                    await bot.set_my_commands(
                        [BotCommand(cmd, desc) for cmd, desc in self._bot_commands]
                    )
                except Exception as exc:
                    logger.warning("telegram: set_my_commands failed (%s)", exc)

            # Mode-specific bring-up. Polling refuses to start if a
            # webhook is set (Telegram returns 409 Conflict otherwise);
            # webhook mode installs/replaces the webhook so we own it.
            try:
                info = await bot.get_webhook_info()
            except Exception as exc:
                logger.warning("telegram: getWebhookInfo failed (%s)", exc)
                info = None
            if self._mode == "polling":
                if info is not None and info.url:
                    logger.error(
                        "telegram: a webhook is set for @%s (%s) — refusing to poll. "
                        "Either clear it (mode=polling) or switch settings.mode to "
                        "'webhook' to keep using it.",
                        self._bot_username, info.url,
                    )
                    self._last_error = "a webhook is set — refusing to poll"
                    self._stopped.set()
                    self._emit_status("error", reason=self._last_error)
                    return
            else:  # webhook
                if not self._webhook_url:
                    logger.error(
                        "telegram: mode=webhook but webhook_url is empty — "
                        "set it in plugin settings.",
                    )
                    self._last_error = "mode=webhook but webhook_url is empty"
                    self._stopped.set()
                    self._emit_status("error", reason=self._last_error)
                    return
                try:
                    await bot.set_webhook(
                        url=self._webhook_url,
                        secret_token=self._webhook_secret or None,
                        allowed_updates=["message", "edited_message", "callback_query"],
                    )
                    logger.info(
                        "telegram: webhook registered: %s", self._webhook_url,
                    )
                except Exception as exc:
                    logger.error("telegram: set_webhook failed (%s)", exc)
                    self._last_error = f"set_webhook failed: {exc}"
                    self._stopped.set()
                    self._emit_status("error", reason=self._last_error)
                    return

            # ── Handlers ────────────────────────────────────────────
            # Plain text + group text (privacy-mode-permitting): one
            # handler. Commands: another (PTB's CommandHandler matches
            # `/cmd` strictly, so we register without a list of
            # commands — we filter inside the handler).
            app.add_handler(MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._on_text_update,
            ))
            app.add_handler(MessageHandler(
                filters.COMMAND,
                self._on_command_update,
            ))
            # Inline-keyboard button taps (interactive prompts). Registered
            # unconditionally — callback_query is already in allowed_updates
            # for both polling and webhook modes; the handler no-ops when no
            # on_callback hook is wired.
            app.add_handler(CallbackQueryHandler(self._on_callback_update))

            logger.info(
                "telegram: %s mode for @%s (id=%s)",
                self._mode, self._bot_username, self._bot_id,
            )
            self._ready.set()
            self._emit_status(
                "ready", bot_username=self._bot_username, bot_id=self._bot_id,
            )

            # In both modes the Application owns the dispatcher we use
            # to process Updates. Polling additionally starts the
            # `updater` (long-poll loop); webhook mode skips it and
            # relies on plugin.py's /webhook route to call
            # `app.process_update(...)`.
            await app.initialize()
            await app.start()
            if self._mode == "polling":
                await app.updater.start_polling(
                    timeout=int(self._poll_timeout_s),
                    drop_pending_updates=False,
                    allowed_updates=["message", "edited_message", "callback_query"],
                )

            while not worker.should_stop():
                await asyncio.sleep(0.5)

            logger.info("telegram: stopping (%s mode)", self._mode)
            try:
                if self._mode == "polling":
                    await app.updater.stop()
                elif self._mode == "webhook":
                    # Be polite: drop our webhook when shutting down so
                    # Telegram doesn't keep retrying against a dead URL.
                    try:
                        await app.bot.delete_webhook()
                    except Exception as exc:
                        logger.debug("telegram: delete_webhook on shutdown: %s", exc)
                await app.stop()
                await app.shutdown()
            except Exception as exc:
                logger.warning("telegram: shutdown error %s", exc)
            self._stopped.set()

        try:
            asyncio.run(_async_main())
        except Exception as exc:
            self._last_error = f"worker crashed: {exc}"
            self._emit_status("error", reason=self._last_error)
            logger.exception("telegram: worker crashed")
            self._stopped.set()
            raise

    # --- callable from any thread (outbound) ----------------------

    def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        """Send a text message. Submits onto the worker's asyncio
        loop from whatever thread called us. Returns
        `{ok, message_id?, error?}`.

        `parse_mode` enables Telegram rich text ("HTML" / "MarkdownV2").
        Malformed markup makes Telegram reject the WHOLE message, so on a
        parse-mode failure we retry once as plain text — the human always
        gets the content, just unformatted (result carries `downgraded`).

        `reply_markup` (an `InlineKeyboardMarkup`) attaches tap-able
        buttons — the interactive-prompt path passes one here. It's kept
        on the plain-text retry so a markup failure doesn't silently drop
        the buttons."""
        if self._loop is None or self._app is None:
            return {"ok": False, "error": "telegram worker not ready"}

        async def _send(pm: str | None) -> Any:
            return await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True,
                parse_mode=pm,
                reply_markup=reply_markup,
            )

        def _run(pm: str | None) -> Any:
            return asyncio.run_coroutine_threadsafe(_send(pm), self._loop).result(timeout=timeout)

        try:
            msg = _run(parse_mode)
        except Exception as exc:
            if parse_mode:
                # Bad HTML/markdown → don't lose the message; resend plain.
                try:
                    msg = _run(None)
                    return {"ok": True, "message_id": getattr(msg, "message_id", None),
                            "downgraded": True, "format_error": str(exc)}
                except Exception as exc2:
                    return {"ok": False, "error": str(exc2)}
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "message_id": getattr(msg, "message_id", None)}

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        """Edit a previously-sent message in place. Used to retract an
        interactive prompt's buttons and stamp the outcome once a human
        answers (pass `reply_markup=None` to drop the keyboard). Idempotent
        from Telegram's side; a 'message is not modified' error is treated
        as success."""
        if self._loop is None or self._app is None:
            return {"ok": False, "error": "telegram worker not ready"}

        async def _edit() -> Any:
            return await self._app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )

        try:
            asyncio.run_coroutine_threadsafe(_edit(), self._loop).result(timeout=timeout)
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return {"ok": True, "noop": True}
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        """Acknowledge a button tap so the client's loading spinner clears.
        Optional `text` shows a transient toast (e.g. '✓ Approved')."""
        if self._loop is None or self._app is None:
            return {"ok": False, "error": "telegram worker not ready"}

        async def _answer() -> Any:
            return await self._app.bot.answer_callback_query(
                callback_query_id=callback_query_id,
                text=text,
                show_alert=show_alert,
            )

        try:
            asyncio.run_coroutine_threadsafe(_answer(), self._loop).result(timeout=timeout)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def send_typing(
        self,
        chat_id: int,
        *,
        thread_id: int | None = None,
        timeout: float = 8.0,
    ) -> bool:
        """Best-effort sendChatAction(typing). Returns False when the worker
        is gone or Telegram rejects the action — callers should stop refreshing."""
        if self._loop is None or self._app is None:
            return False

        async def _go() -> None:
            await self._app.bot.send_chat_action(
                chat_id=chat_id,
                action="typing",
                message_thread_id=thread_id,
            )

        try:
            asyncio.run_coroutine_threadsafe(_go(), self._loop).result(timeout=timeout)
            return True
        except Exception:
            return False

    # --- async handlers (run inside PTB's loop) -------------------

    @property
    def bot_username(self) -> str:
        return self._bot_username

    async def _on_text_update(self, update: Any, context: Any) -> None:
        msg = update.effective_message
        if msg is None:
            return
        await self._dispatch(update, msg, command=None, body=msg.text or "")

    async def _on_command_update(self, update: Any, context: Any) -> None:
        msg = update.effective_message
        if msg is None:
            return
        command, bot_mention, body = parse_command(msg.text or "")
        # `/cmd@otherbot` — silently ignore in groups (the other bot
        # is the intended recipient).
        if bot_mention and bot_mention.lower() != self._bot_username.lower():
            return
        await self._dispatch(update, msg, command=command, body=body)

    async def _on_callback_update(self, update: Any, context: Any) -> None:
        """Handle an inline-keyboard button tap.

        Builds a ctx (chat/user + the button's `callback_data` and the
        `callback_query_id` the plugin must answer to clear the client's
        loading spinner) and hands off to the plugin's on_callback hook on
        a worker thread, so the blocking prompt-resolve call doesn't stall
        the asyncio loop.
        """
        cq = update.callback_query
        if cq is None or self._on_callback is None:
            # Still answer so the user's client doesn't spin forever.
            if cq is not None:
                try:
                    await cq.answer()
                except Exception:
                    pass
            return
        self._last_update_ts = time.time()
        chat = update.effective_chat
        user = update.effective_user
        msg = cq.message
        ctx = {
            "chat_id": getattr(chat, "id", None),
            "chat_type": getattr(chat, "type", "") or "",
            "thread_id": getattr(msg, "message_thread_id", None) if msg else None,
            "user_id": getattr(user, "id", None),
            "user_name": (
                getattr(user, "username", None)
                or getattr(user, "full_name", None)
                or str(getattr(user, "id", ""))
            ),
            "message_id": getattr(msg, "message_id", None) if msg else None,
            "callback_data": cq.data or "",
            "callback_query_id": cq.id,
            "bot": self._app.bot,
            "raw_query": cq,
        }
        await asyncio.to_thread(self._on_callback, ctx)

    async def _dispatch(
        self,
        update: Any,
        msg: Any,
        *,
        command: str | None,
        body: str,
    ) -> None:
        self._last_update_ts = time.time()  # liveness for the health line
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return

        # Mention requirement (groups only). The caller-side router
        # may override this per-route; here we apply the global
        # default. In DMs (chat.type == 'private') we accept any text.
        is_private = chat.type == "private"
        mention_ok = True
        if not is_private and self._require_mention and command is None:
            mention_ok = message_mentions_bot(body, self._bot_username) or (
                msg.reply_to_message is not None
                and getattr(msg.reply_to_message.from_user, "id", None) == self._bot_id
            )

        ctx = {
            "chat_id": chat.id,
            "chat_type": chat.type,
            "chat_title": getattr(chat, "title", None) or "",
            "chat_username": getattr(chat, "username", None) or "",
            "thread_id": getattr(msg, "message_thread_id", None),
            "user_id": user.id,
            "user_name": user.username or user.full_name or str(user.id),
            "message_id": msg.message_id,
            "is_private": is_private,
            "mention_ok": mention_ok,
            "command": command,
            "body": _strip_bot_mention(body, self._bot_username) if not command else body,
            "reactions_enabled": self._reactions,
            "bot": self._app.bot,
            "raw_message": msg,
        }

        # The plugin-side callback decides what to do (look up the
        # route, send to agents, react). Run it in a worker thread so
        # blocking orchestrator calls don't stall the asyncio loop.
        await asyncio.to_thread(self._on_text, ctx)

    # --- introspection --------------------------------------------

    def ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout=timeout)

    def _emit_status(self, state: str, **info: Any) -> None:
        """Notify the plugin of a lifecycle transition (best-effort; a failing
        callback must never take down the poller)."""
        cb = self._on_status
        if cb is None:
            return
        try:
            cb(state, info)
        except Exception:
            logger.debug("telegram: on_status callback failed", exc_info=True)

    def info(self) -> dict[str, Any]:
        return {
            "bot_username": self._bot_username,
            "bot_id": self._bot_id,
            "ready": self._ready.is_set(),
            "stopped": self._stopped.is_set(),
            "last_update_ts": self._last_update_ts or None,
            "last_error": self._last_error or None,
        }


def _strip_bot_mention(text: str, bot_username: str) -> str:
    """Remove a leading `@bot_username` from a message so the routed
    body doesn't carry the addressing prefix. Mirrors what an agent
    expects to see in a DM.
    """
    if not bot_username:
        return text
    pattern = re.compile(rf"^\s*@{re.escape(bot_username)}\b\s*", re.IGNORECASE)
    return pattern.sub("", text)
