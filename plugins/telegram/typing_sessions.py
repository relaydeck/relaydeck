"""Telegram typing indicator sessions (sendChatAction → typing).

Telegram clears the typing bubble after ~5s, so live agent work refreshes
every few seconds until a reply is sent, the worker disconnects, sending
typing fails, or a max duration is reached.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)


def session_key(connection_id: str, chat_id: int, thread_id: int | None) -> str:
    return f"{connection_id}:{chat_id}:{thread_id or 0}"


class TypingController:
    """Periodic typing refreshes keyed by (connection, chat, thread)."""

    def __init__(self, *, refresh_s: float = 4.0, max_s: float = 120.0) -> None:
        self._refresh_s = refresh_s
        self._max_s = max_s
        self._lock = threading.Lock()
        self._sessions: dict[str, _TypingSession] = {}

    def start(
        self,
        send_fn: Callable[[], bool],
        *,
        connection_id: str,
        chat_id: int,
        thread_id: int | None = None,
    ) -> None:
        key = session_key(connection_id, chat_id, thread_id)
        with self._lock:
            self._stop_locked(key)
            sess = _TypingSession(
                send_fn=send_fn,
                refresh_s=self._refresh_s,
                max_s=self._max_s,
            )
            # Identity-checked drop: a back-to-back start() pops the old
            # session and inserts a new one under the SAME key. The old
            # thread's `finally` then fires `on_done` after cancel — if we
            # popped by key alone, we'd evict the new session and lose the
            # ability to stop it (typing keeps refreshing until the 120 s cap).
            sess.bind_on_done(lambda k=key, s=sess: self._drop(k, s))
            self._sessions[key] = sess
            sess.start()

    def stop(
        self, connection_id: str, chat_id: int, thread_id: int | None = None,
    ) -> None:
        with self._lock:
            self._stop_locked(session_key(connection_id, chat_id, thread_id))

    def stop_connection(self, connection_id: str) -> None:
        prefix = f"{connection_id}:"
        with self._lock:
            for key in list(self._sessions):
                if key.startswith(prefix):
                    self._stop_locked(key)

    def stop_all(self) -> None:
        with self._lock:
            for key in list(self._sessions):
                self._stop_locked(key)

    def active_keys(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    def _stop_locked(self, key: str) -> None:
        sess = self._sessions.pop(key, None)
        if sess is not None:
            sess.cancel()

    def _drop(self, key: str, sess: _TypingSession) -> None:
        with self._lock:
            if self._sessions.get(key) is sess:
                self._sessions.pop(key, None)


class _TypingSession:
    def __init__(
        self,
        *,
        send_fn: Callable[[], bool],
        refresh_s: float,
        max_s: float,
    ) -> None:
        self._send_fn = send_fn
        self._refresh_s = refresh_s
        self._max_s = max_s
        self._on_done: Callable[[], Any] = lambda: None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def bind_on_done(self, cb: Callable[[], Any]) -> None:
        self._on_done = cb

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="tg-typing", daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        started = time.monotonic()
        try:
            while not self._cancel.is_set():
                if not self._send_fn():
                    break
                if time.monotonic() - started >= self._max_s:
                    break
                if self._cancel.wait(self._refresh_s):
                    break
        except Exception:
            logger.debug("telegram: typing session ended with error", exc_info=True)
        finally:
            with suppress(Exception):
                self._on_done()
