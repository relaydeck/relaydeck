"""Map relaydeck inbox ids → Telegram delivery metadata.

When the plugin routes an inbound Telegram update to an agent via
`send_message_to`, the agent sees `[relay from=telegram:<chat_id> id=msg_…]`.
Replying in-thread on Telegram needs the original Bot API `message_id`, not
the relay id — this sidecar stores that link at route time.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 500


@dataclass(frozen=True)
class InboundMeta:
    chat_id: int
    telegram_message_id: int
    thread_id: int | None = None
    connection_id: str | None = None
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "telegram_message_id": self.telegram_message_id,
            "thread_id": self.thread_id,
            "connection_id": self.connection_id,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> InboundMeta:
        return cls(
            chat_id=int(d["chat_id"]),
            telegram_message_id=int(d["telegram_message_id"]),
            thread_id=int(d["thread_id"]) if d.get("thread_id") is not None else None,
            connection_id=(str(d["connection_id"]).strip() or None)
            if d.get("connection_id") else None,
            ts=float(d.get("ts") or 0.0),
        )


class InboundMap:
    """relay msg_id → InboundMeta, persisted for daemon restarts."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._rows: dict[str, InboundMeta] = {}

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        rows = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(rows, dict):
            return
        for msg_id, raw in rows.items():
            if not isinstance(raw, dict):
                continue
            with contextlib.suppress(KeyError, TypeError, ValueError):
                self._rows[str(msg_id)] = InboundMeta.from_dict(raw)

    def register(self, relay_msg_id: str, ctx: dict[str, Any]) -> None:
        """Record metadata for one routed inbound update."""
        try:
            meta = InboundMeta(
                chat_id=int(ctx["chat_id"]),
                telegram_message_id=int(ctx["message_id"]),
                thread_id=int(ctx["thread_id"]) if ctx.get("thread_id") is not None else None,
                connection_id=(str(ctx.get("connection_id") or "").strip() or None),
                ts=time.time(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # Silently dropped registrations break in-reply-to threading on the
            # agent's reply path. Log enough to diagnose a future ctx shape
            # change without spamming on every inbound.
            logger.debug(
                "telegram: inbound_map.register skipped for %s: %s",
                relay_msg_id, exc,
            )
            return
        self._rows[str(relay_msg_id)] = meta
        if len(self._rows) > _MAX_ENTRIES:
            oldest = sorted(
                self._rows.items(),
                key=lambda item: item[1].ts,
            )[: len(self._rows) - _MAX_ENTRIES]
            for key, _ in oldest:
                self._rows.pop(key, None)
        self._save()

    def lookup(self, relay_msg_id: str) -> InboundMeta | None:
        return self._rows.get(str(relay_msg_id))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = {"entries": {k: v.to_dict() for k, v in self._rows.items()}}
        fd, tmp = tempfile.mkstemp(prefix=".inbound-map.", dir=str(self._path.parent))
        try:
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self._path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
