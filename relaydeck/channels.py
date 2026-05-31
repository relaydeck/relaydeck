"""
Provider-agnostic messaging channel registry.

This is the swappable/mixable seam for *outbound* interactive messaging.
A "channel" is a provider type (`telegram`, `discord`, `slack`,
`whatsapp`, `sms`, `web`, …). A "connection" is one configured instance
of a channel — a specific bot token / account / number — so a single
channel can run many connections at once (multiple Telegram bots,
multiple Discord apps). A "target" identifies a destination inside a
connection (a Telegram chat id, a Discord channel id, a phone number).

Core code never imports a concrete provider. It speaks only the
`MessagingProvider` contract and the `Address` value type, so adding
Discord/Slack/WhatsApp later is "drop in a plugin that registers a
provider" — no change here, in `prompts.py`, or in the orchestrator.

The interactive-prompt feature (see `relaydeck/prompts.py`) builds on
this: it asks each target's provider `capabilities()` whether it can
render native buttons. If yes, the provider renders an inline keyboard
(Telegram), action buttons (Slack/Discord), etc. If not, the core
degrades to a numbered-list **text fallback** and parses the human's
typed reply back into a choice (`render_choices_as_text` /
`match_text_to_choice`). Either way the agent-facing API is identical —
that's what makes interactive approvals "standard" across platforms
instead of a Telegram-only trick.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from relaydeck.prompts import Choice, Prompt

logger = logging.getLogger(__name__)


# ── Address ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Address:
    """Where a message goes, decomposed into the three swap axes.

    Wire form is a colon-delimited string so it round-trips through CLI
    flags, JSON, and callback payloads:

        ``<channel>[:<connection>[:<target>[:<thread>]]]``

    Examples::

        telegram:ops:-1001234567890        # a bot named "ops", a chat
        telegram:ops:-1001234567890:42     # …a forum topic in that chat
        telegram::123456789                # default connection, a DM
        discord:main:#approvals
        whatsapp:twilio:+15551230000
        web                                # the dashboard itself

    `connection` empty means "the channel's default/only connection" —
    the provider resolves it (mirrors how the telegram plugin synthesizes
    a default connection from a single token).
    """

    channel: str
    connection: str = ""
    target: str = ""
    thread: str = ""

    @classmethod
    def parse(cls, raw: str) -> Address:
        """Parse a `channel:connection:target:thread` string.

        Lenient on missing trailing parts; raises on an empty channel.
        Targets that themselves contain colons (rare, but e.g. a URL)
        are NOT supported in the compact form — pass them via the
        structured API instead. We split into at most four parts so a
        negative Telegram chat id (`-100…`, no colons) stays intact.
        """
        parts = (raw or "").split(":", 3)
        channel = parts[0].strip()
        if not channel:
            raise ValueError(f"address missing channel: {raw!r}")
        return cls(
            channel=channel,
            connection=(parts[1].strip() if len(parts) > 1 else ""),
            target=(parts[2].strip() if len(parts) > 2 else ""),
            thread=(parts[3].strip() if len(parts) > 3 else ""),
        )

    def to_str(self) -> str:
        bits = [self.channel, self.connection, self.target, self.thread]
        # Trim empty trailing components so `web` stays `web`, not `web:::`.
        while len(bits) > 1 and not bits[-1]:
            bits.pop()
        return ":".join(bits)

    def to_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "connection": self.connection,
            "target": self.target,
            "thread": self.thread,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Address:
        return cls(
            channel=str(data.get("channel") or ""),
            connection=str(data.get("connection") or ""),
            target=str(data.get("target") or ""),
            thread=str(data.get("thread") or ""),
        )


# ── Capabilities ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChannelCapabilities:
    """What a provider can do, so core can negotiate gracefully.

    `interactive_buttons` is the pivotal one: True ⇒ render a native
    tap-able keyboard and resolve via a structured callback; False ⇒
    core renders a numbered-list text fallback and matches the typed
    reply. `editable` ⇒ the provider can edit a delivered message in
    place (to show "✅ Approved by …" and retract the buttons once a
    decision lands). `max_buttons` / `max_per_row` let core lay out or
    chunk large choice sets.
    """

    interactive_buttons: bool = False
    editable: bool = False
    rich_text: bool = False
    max_buttons: int = 0  # 0 = unlimited / not advertised
    max_per_row: int = 1


# ── Delivery result ──────────────────────────────────────────────────


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of delivering a prompt/text to one address.

    `ref` is the provider's own message handle (Telegram message_id,
    Slack ts, …) so a later edit/close can target it. `mode` records how
    the prompt was actually rendered — `"buttons"` (native) or `"text"`
    (numbered-list fallback) — which the responder path needs to know
    when interpreting an inbound reply.
    """

    ok: bool
    ref: str = ""
    mode: str = "text"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "ref": self.ref, "mode": self.mode, "error": self.error}


# ── Provider contract ────────────────────────────────────────────────


@runtime_checkable
class MessagingProvider(Protocol):
    """The contract a channel plugin implements and registers via
    ``host.channels.register(provider)``.

    Only `channel` and `capabilities()` are mandatory. `deliver_prompt`
    is required for any provider that wants to carry interactive prompts;
    providers that can't (a pure outbound SMS gateway) can rely on the
    core text fallback by advertising `interactive_buttons=False` and
    implementing `deliver_text`. `close_prompt` is optional — providers
    that can edit in place implement it to retract buttons on resolution.
    """

    channel: str

    def capabilities(self) -> ChannelCapabilities: ...

    def connections(self) -> list[dict[str, Any]]:
        """List configured connections: ``[{"id", "name", "ready"}]``."""
        ...

    def deliver_prompt(self, address: Address, prompt: Prompt) -> DeliveryResult:
        """Render `prompt` at `address` with native buttons when able."""
        ...

    def deliver_text(
        self, address: Address, text: str, *, in_reply_to: str | None = None
    ) -> DeliveryResult:
        """Send a plain text message (used by the text fallback path)."""
        ...

    def close_prompt(self, address: Address, ref: str, prompt: Prompt) -> None:
        """Best-effort: edit the delivered message to show the outcome
        and retract the buttons. Optional; no-op if unsupported."""
        ...


# ── Registry ─────────────────────────────────────────────────────────
#
# Process-wide, like the harness/viewer registries. Populated at plugin
# load time by the SDK adapter (which binds `host.channels.registrations`
# here) and consulted by `prompts.py` at dispatch time. A plain dict
# under a lock — registration happens on the load thread, lookups happen
# on HTTP/worker threads.

_REGISTRY: dict[str, MessagingProvider] = {}
_LOCK = threading.RLock()


def register_channel(provider: MessagingProvider) -> None:
    """Register (or replace) the provider for `provider.channel`.

    Last-registered wins, mirroring the viewer registry — lets an
    operator override a bundled provider with a custom one. The `web`
    channel is registered by the bundled prompts plugin; external
    plugins register telegram/discord/slack/etc.
    """
    name = getattr(provider, "channel", "") or ""
    if not name:
        raise ValueError("messaging provider must set a non-empty `channel`")
    with _LOCK:
        _REGISTRY[name] = provider
    logger.debug("registered messaging channel provider: %s", name)


def unregister_channel(name: str, provider: MessagingProvider | None = None) -> None:
    """Remove `name` from the registry. When `provider` is given, only
    remove it if the slot still points at that instance (so reloading a
    plugin can't clobber an override that replaced it)."""
    with _LOCK:
        current = _REGISTRY.get(name)
        if current is None:
            return
        if provider is not None and current is not provider:
            return
        _REGISTRY.pop(name, None)


def get_channel(name: str) -> MessagingProvider | None:
    with _LOCK:
        return _REGISTRY.get(name)


def list_channels() -> list[str]:
    with _LOCK:
        return sorted(_REGISTRY)


def channel_capabilities(name: str) -> ChannelCapabilities | None:
    provider = get_channel(name)
    if provider is None:
        return None
    try:
        return provider.capabilities()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("capabilities() failed for channel %s: %s", name, exc)
        return None


# ── Text fallback helpers ────────────────────────────────────────────
#
# Used by both the core dispatch path (for providers that can't render
# native buttons) and any provider that wants a ready-made textual form.


def render_choices_as_text(prompt: Prompt, *, header: str = "") -> str:
    """Render a prompt as a numbered list a human can answer by typing.

    Example output::

        Deploy v2.3 to production?

        Reply with a number or word:
          1. Approve
          2. Reject
          3. Hold

    The numbers AND the choice labels/ids are both accepted by
    `match_text_to_choice`, so "1", "approve", or "Approve" all resolve.
    """
    lines: list[str] = []
    if header:
        lines.append(header)
    lines.append(prompt.body)
    lines.append("")
    lines.append("Reply with a number or word:")
    for idx, choice in enumerate(prompt.choices, start=1):
        lines.append(f"  {idx}. {choice.label}")
    return "\n".join(lines)


_PUNCT = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    return _PUNCT.sub("", (text or "").strip().lower())


def match_text_to_choice(prompt: Prompt, reply: str) -> Choice | None:
    """Map a free-text human reply to one of `prompt.choices`.

    Accepts, in order: the 1-based position ("1"), the exact choice id
    ("approve"), the exact label ("Approve"), and finally a normalized
    label/id match ("APPROVE!", " approve "). Returns None when nothing
    matches confidently — the caller then re-prompts rather than guessing.
    """
    raw = (reply or "").strip()
    if not raw:
        return None

    # 1-based numeric position.
    if raw.isdigit():
        pos = int(raw)
        if 1 <= pos <= len(prompt.choices):
            return prompt.choices[pos - 1]
        return None

    norm = _normalize(raw)
    if not norm:
        return None
    for choice in prompt.choices:
        if _normalize(choice.id) == norm or _normalize(choice.label) == norm:
            return choice
    # Loose: reply starts with a choice token (e.g. "approve, ship it").
    first = _normalize(raw.split()[0]) if raw.split() else ""
    if first:
        for choice in prompt.choices:
            if _normalize(choice.id) == first or _normalize(choice.label) == first:
                return choice
    return None
