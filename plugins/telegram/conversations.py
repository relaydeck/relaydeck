"""Global conversation registry.

The Bot API can't enumerate the chats a bot is in — a bot only learns a
conversation exists when a message arrives from it. So "chat management at the
global level" is a registry **built from observed inbound traffic**: every DM /
group / supergroup / channel seen across ALL connections, with metadata and
last-activity. The dashboard lists it and routing selects from it (so an
operator picks a conversation instead of hand-typing a chat_id).

Persisted as JSON (mode 0600, atomic) under
`<config_home>/plugin-data/telegram/conversations.json` so it survives daemon
restarts. Keyed by (connection_id, chat_id) — the same chat seen via two bots
is two rows, because routing + delivery are per-connection.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Conversation:
    connection_id: str
    chat_id: int
    chat_type: str = ""          # private | group | supergroup | channel
    title: str = ""              # group/channel title, or a DM display name
    username: str = ""           # @username if public
    thread_ids: list[int] = field(default_factory=list)  # forum topics seen
    last_user: str = ""          # who last messaged (label)
    last_activity: float = 0.0   # unix seconds
    message_count: int = 0
    last_disposition: str = ""   # routed | unrouted | rejected_* (last seen)

    @property
    def key(self) -> str:
        return f"{self.connection_id}:{self.chat_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "title": self.title,
            "username": self.username,
            "thread_ids": list(self.thread_ids),
            "last_user": self.last_user,
            "last_activity": self.last_activity,
            "message_count": self.message_count,
            "last_disposition": self.last_disposition,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Conversation:
        return cls(
            connection_id=str(d.get("connection_id") or "default"),
            chat_id=int(d["chat_id"]),
            chat_type=str(d.get("chat_type") or ""),
            title=str(d.get("title") or ""),
            username=str(d.get("username") or ""),
            thread_ids=[int(t) for t in (d.get("thread_ids") or [])],
            last_user=str(d.get("last_user") or ""),
            last_activity=float(d.get("last_activity") or 0.0),
            message_count=int(d.get("message_count") or 0),
            last_disposition=str(d.get("last_disposition") or ""),
        )


class ConversationRegistry:
    """In-memory map (connection_id, chat_id) → Conversation, persisted to JSON.
    Cheap to write — one row per chat, not per message."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._convs: dict[str, Conversation] = {}

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        for row in (data.get("conversations") or []) if isinstance(data, dict) else []:
            try:
                c = Conversation.from_dict(row)
                self._convs[c.key] = c
            except (KeyError, ValueError, TypeError):
                continue

    def record(
        self,
        *,
        connection_id: str,
        chat_id: int,
        chat_type: str = "",
        title: str = "",
        username: str = "",
        thread_id: int | None = None,
        user: str = "",
        disposition: str = "",
    ) -> Conversation:
        """Upsert a conversation from one observed inbound message + persist."""
        key = f"{connection_id}:{chat_id}"
        c = self._convs.get(key)
        if c is None:
            c = Conversation(connection_id=connection_id, chat_id=chat_id)
            self._convs[key] = c
        if chat_type:
            c.chat_type = chat_type
        # Title: prefer a real chat title; for DMs fall back to the sender name.
        if title:
            c.title = title
        elif not c.title and user:
            c.title = user
        if username:
            c.username = username
        if thread_id is not None and thread_id not in c.thread_ids:
            c.thread_ids.append(thread_id)
        if user:
            c.last_user = user
        if disposition:
            c.last_disposition = disposition
        c.last_activity = time.time()
        c.message_count += 1
        self._save()
        return c

    def list(self) -> list[Conversation]:
        """Newest activity first."""
        return sorted(self._convs.values(), key=lambda c: c.last_activity, reverse=True)

    def get(self, connection_id: str, chat_id: int) -> Conversation | None:
        return self._convs.get(f"{connection_id}:{chat_id}")

    def delete(self, connection_id: str, chat_id: int) -> bool:
        """Forget one conversation (exact connection+chat key). Returns True
        if it existed. Persists only when something actually changed."""
        existed = self._convs.pop(f"{connection_id}:{chat_id}", None) is not None
        if existed:
            self._save()
        return existed

    def delete_chat(self, chat_id: int, *, connection_id: str | None = None) -> int:
        """Forget every row for `chat_id`, optionally scoped to one connection.
        The same chat seen via two bots is two rows, so this can remove >1.
        Returns the number removed."""
        victims = [
            k for k, c in self._convs.items()
            if c.chat_id == chat_id
            and (connection_id is None or c.connection_id == connection_id)
        ]
        for k in victims:
            del self._convs[k]
        if victims:
            self._save()
        return len(victims)

    def purge_all(self) -> int:
        """Forget every conversation. Returns the number removed. Chats
        reappear as inbound traffic is seen again — this only clears the
        accumulated registry, never the routes."""
        n = len(self._convs)
        if n:
            self._convs.clear()
            self._save()
        return n

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = {"conversations": [c.to_dict() for c in self.list()]}
        fd, tmp = tempfile.mkstemp(prefix=".conversations.", dir=str(self._path.parent))
        try:
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                Path(tmp).unlink(missing_ok=True)
            raise
