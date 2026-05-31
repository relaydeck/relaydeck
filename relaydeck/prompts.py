"""
Interactive prompts: structured human-in-the-loop approvals.

Instead of an agent printing "reply YES to deploy" and blocking on a
free-text answer, it raises a *prompt* — a question plus a set of tap-able
*choices* (``[Approve] [Reject] [Hold]``). The prompt is fanned out to one
or more messaging *addresses* (see `relaydeck/channels.py`); whichever human
taps a button first resolves it, and the structured decision is delivered
back to the asking agent as a `[relay …]` line (the same resume primitive
peer messages use) — or returned synchronously to a blocking
``relaydeck prompt ask --wait`` call.

Design goals this module encodes:

* **Provider-agnostic.** Nothing here imports Telegram. A prompt names
  *addresses* (`telegram:ops:123`, `discord:main:#ops`, `web`), and the
  channel registry resolves each to a provider. Capability negotiation
  decides native buttons vs. a numbered-list text fallback, so the agent
  API is identical on every platform.
* **Mixable / multi-token.** A single prompt may target several
  connections of one channel (two Telegram bots) *and* several channels
  at once. First valid response wins; the rest are retracted.
* **Durable + race-safe.** Prompts live in SQLite (`interactive_prompts`).
  Resolution is a single conditional UPDATE, so a double-tap or two humans
  racing can never both "win".

The data layer (store functions) and the orchestration layer
(`PromptService`) live here; the user-facing surface — CLI, HTTP, the web
button provider — lives in `plugins/prompts/`. Telegram/Discord/etc. plug
in as channel providers without touching this file.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relaydeck.db import open_db

logger = logging.getLogger(__name__)


# Prompt lifecycle states.
STATE_OPEN = "open"          # awaiting a response
STATE_ANSWERED = "answered"  # a human picked a choice
STATE_EXPIRED = "expired"    # passed expires_ts with no answer
STATE_CANCELED = "canceled"  # withdrawn by the asker / superseded

VALID_STYLES = ("default", "primary", "danger", "success")


# ── Model ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Choice:
    """One tap-able option on a prompt.

    `id` is the stable machine token the agent keys its logic on
    (`"approve"`); `label` is what the human sees (`"Approve & deploy"`);
    `style` is an advisory hint providers map to their own affordances
    (Slack button color, a leading emoji) — Telegram inline buttons are
    visually uniform, so style is cosmetic there. `value` is optional
    free-form data echoed back with the answer.
    """

    id: str
    label: str = ""
    style: str = "default"
    value: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not str(self.id).strip():
            raise ValueError("choice id must be non-empty")
        if self.style not in VALID_STYLES:
            # Don't hard-fail on an unknown style — just normalize so a
            # provider's style→color map always has a key it understands.
            object.__setattr__(self, "style", "default")
        if not self.label:
            object.__setattr__(self, "label", self.id)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "style": self.style, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Choice:
        return cls(
            id=str(data.get("id") or ""),
            label=str(data.get("label") or ""),
            style=str(data.get("style") or "default"),
            value=data.get("value"),
        )

    @classmethod
    def parse(cls, spec: str) -> Choice:
        """Parse a CLI-friendly ``id[:label[:style]]`` spec.

        ``approve`` → id=approve, label=Approve. ``approve:Approve &
        ship:primary`` → fully specified. Splits at most twice so a label
        may contain spaces (but not a colon — use the structured API for
        that). Capitalizes a bare id into a default label.
        """
        parts = (spec or "").split(":", 2)
        cid = parts[0].strip()
        if len(parts) > 1 and parts[1].strip():
            label = parts[1].strip()
        else:
            label = cid.replace("_", " ").capitalize()
        style = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "default"
        return cls(id=cid, label=label, style=style)


@dataclass
class Prompt:
    """One row of `interactive_prompts`."""

    id: str
    agent_id: str            # who asked / who to resume ("" if user/plugin)
    body: str
    choices: list[Choice]
    created_ts: float
    workspace: str | None = None
    expires_ts: float | None = None
    state: str = STATE_OPEN
    multi: bool = False      # reserved: allow multiple selections
    # Where the answer goes back to. "relay:<agent_id>" injects a
    # [relay …] line into the agent's PTY (async TUI agents). "" means
    # the asker polls the store (a blocking `--wait` CLI / the dashboard).
    resume_target: str = ""
    # Resolution.
    answer_choice: str | None = None
    answer_value: str | None = None
    answered_by: str | None = None
    answered_ts: float | None = None
    # Fan-out bookkeeping: one dict per address we delivered to,
    # carrying the provider message ref so we can retract on resolve.
    deliveries: list[dict[str, Any]] = field(default_factory=list)

    def choice_by_id(self, choice_id: str) -> Choice | None:
        for c in self.choices:
            if c.id == choice_id:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "body": self.body,
            "choices": [c.to_dict() for c in self.choices],
            "created_ts": self.created_ts,
            "workspace": self.workspace,
            "expires_ts": self.expires_ts,
            "state": self.state,
            "multi": self.multi,
            "resume_target": self.resume_target,
            "answer_choice": self.answer_choice,
            "answer_value": self.answer_value,
            "answered_by": self.answered_by,
            "answered_ts": self.answered_ts,
            "deliveries": list(self.deliveries),
        }


def _default_db_path() -> str:
    return str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db")


def _short_id() -> str:
    """`prm_` + 12 hex chars. Short enough to fit a Telegram callback
    payload (64-byte cap) and paste on a CLI."""
    return "prm_" + uuid.uuid4().hex[:12]


def _row_to_prompt(row: sqlite3.Row) -> Prompt:
    try:
        choices = [Choice.from_dict(c) for c in json.loads(row["choices"] or "[]")]
    except Exception:
        choices = []
    try:
        deliveries = json.loads(row["deliveries"] or "[]")
    except Exception:
        deliveries = []
    return Prompt(
        id=row["id"],
        agent_id=row["agent_id"] or "",
        body=row["body"] or "",
        choices=choices,
        created_ts=row["created_ts"],
        workspace=row["workspace"],
        expires_ts=row["expires_ts"],
        state=row["state"] or STATE_OPEN,
        multi=bool(row["multi"]),
        resume_target=row["resume_target"] or "",
        answer_choice=row["answer_choice"],
        answer_value=row["answer_value"],
        answered_by=row["answered_by"],
        answered_ts=row["answered_ts"],
        deliveries=deliveries if isinstance(deliveries, list) else [],
    )


# ── Store ────────────────────────────────────────────────────────────


def insert_prompt(
    agent_id: str,
    body: str,
    choices: list[Choice],
    *,
    workspace: str | None = None,
    expires_ts: float | None = None,
    multi: bool = False,
    resume_target: str = "",
    db_path: str | None = None,
) -> Prompt:
    """Create an OPEN prompt. Returns the persisted `Prompt`."""
    if not choices:
        raise ValueError("a prompt needs at least one choice")
    seen: set[str] = set()
    for c in choices:
        if c.id in seen:
            raise ValueError(f"duplicate choice id: {c.id!r}")
        seen.add(c.id)

    pid = _short_id()
    now = time.time()
    conn = open_db(db_path or _default_db_path())
    try:
        conn.execute(
            """INSERT INTO interactive_prompts
               (id, agent_id, body, choices, created_ts, workspace,
                expires_ts, state, multi, resume_target, deliveries)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid, agent_id, body,
                json.dumps([c.to_dict() for c in choices]),
                now, workspace, expires_ts, STATE_OPEN,
                1 if multi else 0, resume_target, "[]",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return Prompt(
        id=pid, agent_id=agent_id, body=body, choices=list(choices),
        created_ts=now, workspace=workspace, expires_ts=expires_ts,
        state=STATE_OPEN, multi=multi, resume_target=resume_target,
    )


def get_prompt(prompt_id: str, db_path: str | None = None) -> Prompt | None:
    conn = open_db(db_path or _default_db_path())
    try:
        row = conn.execute(
            "SELECT * FROM interactive_prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_prompt(row) if row else None


def list_prompts(
    *,
    workspace: str | None = None,
    agent_id: str | None = None,
    state: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> list[Prompt]:
    clauses: list[str] = []
    params: list[Any] = []
    if workspace is not None:
        clauses.append("workspace = ?")
        params.append(workspace)
    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    conn = open_db(db_path or _default_db_path())
    try:
        rows = conn.execute(
            f"SELECT * FROM interactive_prompts{where} "
            "ORDER BY created_ts DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_prompt(r) for r in rows]


def record_delivery(
    prompt_id: str, delivery: dict[str, Any], db_path: str | None = None
) -> None:
    """Append one delivery record (channel/connection/target/ref/mode)
    to the prompt's fan-out list. Read-modify-write under the connection;
    fan-out happens serially from the dispatch path so there's no
    concurrent writer to race."""
    conn = open_db(db_path or _default_db_path())
    try:
        row = conn.execute(
            "SELECT deliveries FROM interactive_prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
        if row is None:
            return
        try:
            current = json.loads(row["deliveries"] or "[]")
        except Exception:
            current = []
        current.append(delivery)
        conn.execute(
            "UPDATE interactive_prompts SET deliveries = ? WHERE id = ?",
            (json.dumps(current), prompt_id),
        )
        conn.commit()
    finally:
        conn.close()


def resolve_prompt(
    prompt_id: str,
    choice_id: str,
    *,
    answered_by: str,
    value: str | None = None,
    db_path: str | None = None,
) -> Prompt | None:
    """Atomically move a prompt OPEN → ANSWERED, recording the choice.

    Returns the updated `Prompt` **only if this call won** the race
    (the prompt was still open and the choice is valid). Returns None if
    the prompt was already resolved/expired/canceled, doesn't exist, or
    the choice id isn't one of its options. The single conditional
    UPDATE (`WHERE state='open'`) is the concurrency guarantee — two
    simultaneous taps can't both come back non-None.
    """
    now = time.time()
    conn = open_db(db_path or _default_db_path())
    try:
        row = conn.execute(
            "SELECT * FROM interactive_prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
        if row is None:
            return None
        prompt = _row_to_prompt(row)
        if prompt.choice_by_id(choice_id) is None:
            return None
        cur = conn.execute(
            """UPDATE interactive_prompts
               SET state = ?, answer_choice = ?, answer_value = ?,
                   answered_by = ?, answered_ts = ?
               WHERE id = ? AND state = ?""",
            (
                STATE_ANSWERED, choice_id, value,
                answered_by, now, prompt_id, STATE_OPEN,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None  # lost the race / not open
    finally:
        conn.close()
    prompt.state = STATE_ANSWERED
    prompt.answer_choice = choice_id
    prompt.answer_value = value
    prompt.answered_by = answered_by
    prompt.answered_ts = now
    return prompt


def cancel_prompt(
    prompt_id: str, *, reason: str = "", db_path: str | None = None
) -> Prompt | None:
    """Withdraw an open prompt (asker no longer needs the answer)."""
    conn = open_db(db_path or _default_db_path())
    try:
        cur = conn.execute(
            "UPDATE interactive_prompts SET state = ?, answer_value = ? "
            "WHERE id = ? AND state = ?",
            (STATE_CANCELED, reason or None, prompt_id, STATE_OPEN),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_prompt(prompt_id, db_path=db_path)


def expire_due(now: float | None = None, db_path: str | None = None) -> list[Prompt]:
    """Mark every open, past-deadline prompt EXPIRED and return them
    (so the caller can resume the asking agents with a timeout signal)."""
    ts = now if now is not None else time.time()
    conn = open_db(db_path or _default_db_path())
    try:
        rows = conn.execute(
            "SELECT * FROM interactive_prompts "
            "WHERE state = ? AND expires_ts IS NOT NULL AND expires_ts <= ?",
            (STATE_OPEN, ts),
        ).fetchall()
        prompts = [_row_to_prompt(r) for r in rows]
        if prompts:
            conn.executemany(
                "UPDATE interactive_prompts SET state = ? WHERE id = ? AND state = ?",
                [(STATE_EXPIRED, p.id, STATE_OPEN) for p in prompts],
            )
            conn.commit()
    finally:
        conn.close()
    for p in prompts:
        p.state = STATE_EXPIRED
    return prompts


# ── Service ──────────────────────────────────────────────────────────


# resume_fn(to_id, body, from_id=...) -> (msg_id, injected); matches
# Orchestrator.send_message_to. event_emit(type, data) fans the
# lifecycle onto the plugin bus (for the dashboard SSE feed + audit).
ResumeFn = Callable[..., tuple[str, bool]]
EmitFn = Callable[[str, dict[str, Any]], None]


class PromptService:
    """Owns the prompt lifecycle: dispatch → resolve → resume.

    Decoupled from the orchestrator and the providers by two injected
    callables (`resume_fn`, `event_emit`) and the global channel
    registry, so it's trivially testable with fakes and never imports a
    concrete channel.
    """

    def __init__(
        self,
        *,
        db_path: str,
        resume_fn: ResumeFn | None = None,
        event_emit: EmitFn | None = None,
    ) -> None:
        self.db_path = db_path
        self._resume_fn = resume_fn
        self._emit = event_emit

    # -- creation / fan-out --------------------------------------------

    def ask(
        self,
        body: str,
        choices: list[Choice],
        *,
        addresses: list[str] | None = None,
        agent_id: str = "",
        workspace: str | None = None,
        expires_in: float | None = None,
        multi: bool = False,
        notify_agent: bool = True,
    ) -> Prompt:
        """Create a prompt and deliver it to every address.

        `addresses` is a list of ``channel:connection:target`` strings.
        Empty/None defaults to ``["web"]`` so a prompt is always at least
        answerable from the dashboard even with no messaging integration
        configured. `notify_agent=True` (the default) makes resolution
        inject a `[relay …]` line back to `agent_id`; pass False for a
        blocking ``--wait`` caller that reads the answer from the store.
        """
        expires_ts = (time.time() + expires_in) if expires_in else None
        resume_target = f"relay:{agent_id}" if (notify_agent and agent_id) else ""
        prompt = insert_prompt(
            agent_id, body, choices,
            workspace=workspace, expires_ts=expires_ts, multi=multi,
            resume_target=resume_target, db_path=self.db_path,
        )
        self._emit_event("prompt.created", prompt)

        targets = addresses or ["web"]
        from relaydeck import channels as ch

        for raw in targets:
            try:
                addr = ch.Address.parse(raw)
            except ValueError as exc:
                logger.warning("prompt %s: bad address %r: %s", prompt.id, raw, exc)
                record_delivery(prompt.id, {
                    "address": raw, "ok": False, "error": f"bad address: {exc}",
                }, db_path=self.db_path)
                continue
            self._deliver_one(prompt, addr)

        return get_prompt(prompt.id, db_path=self.db_path) or prompt

    def _deliver_one(self, prompt: Prompt, addr: Any) -> None:
        from relaydeck import channels as ch

        provider = ch.get_channel(addr.channel)
        if provider is None:
            logger.info(
                "prompt %s: no provider for channel %r — delivery skipped",
                prompt.id, addr.channel,
            )
            record_delivery(prompt.id, {
                **addr.to_dict(), "ok": False,
                "error": f"no provider registered for channel {addr.channel!r}",
            }, db_path=self.db_path)
            return

        caps = ch.ChannelCapabilities()
        with contextlib.suppress(Exception):
            caps = provider.capabilities()

        try:
            if caps.interactive_buttons:
                result = provider.deliver_prompt(addr, prompt)
            else:
                # Text fallback: numbered list; the human's typed reply
                # is matched back to a choice by the provider's inbound
                # path (which calls match_text_to_choice + respond()).
                text = ch.render_choices_as_text(prompt)
                result = provider.deliver_text(addr, text)
                if result.ok:
                    result = ch.DeliveryResult(
                        ok=True, ref=result.ref, mode="text", error=None,
                    )
        except Exception as exc:
            logger.warning(
                "prompt %s: delivery to %s failed: %s", prompt.id, addr.to_str(), exc
            )
            result = ch.DeliveryResult(ok=False, error=str(exc))

        record_delivery(prompt.id, {
            **addr.to_dict(), **result.to_dict(),
        }, db_path=self.db_path)

    # -- resolution ----------------------------------------------------

    def respond(
        self,
        prompt_id: str,
        choice_id: str,
        *,
        answered_by: str,
        value: str | None = None,
    ) -> Prompt | None:
        """Resolve a prompt to a choice. Returns the resolved `Prompt` if
        this call won the race, else None (already answered/expired). On a
        win: resume the asking agent and retract the other deliveries."""
        prompt = resolve_prompt(
            prompt_id, choice_id,
            answered_by=answered_by, value=value, db_path=self.db_path,
        )
        if prompt is None:
            return None
        self._finalize(prompt)
        self._emit_event("prompt.answered", prompt)
        return prompt

    def respond_text(
        self, prompt_id: str, reply: str, *, answered_by: str
    ) -> Prompt | None:
        """Resolve via a free-text reply (text-fallback channels). Maps
        `reply` to a choice and delegates to `respond`. Returns None if
        the reply didn't match any choice (caller should re-prompt)."""
        from relaydeck import channels as ch

        prompt = get_prompt(prompt_id, db_path=self.db_path)
        if prompt is None or prompt.state != STATE_OPEN:
            return None
        choice = ch.match_text_to_choice(prompt, reply)
        if choice is None:
            return None
        return self.respond(prompt_id, choice.id, answered_by=answered_by, value=reply)

    def _finalize(self, prompt: Prompt) -> None:
        """Deliver the decision to the asker and retract open deliveries."""
        choice = prompt.choice_by_id(prompt.answer_choice or "")
        label = choice.label if choice else (prompt.answer_choice or "?")

        # Resume the asking agent via the relay primitive (same path peer
        # messages use), unless this prompt was created for a blocking
        # caller (resume_target empty → the --wait CLI returns the answer).
        if prompt.resume_target.startswith("relay:") and self._resume_fn:
            agent_id = prompt.resume_target.split(":", 1)[1]
            if agent_id:
                body = (
                    f"✅ Prompt {prompt.id} answered: {label} "
                    f"(choice={prompt.answer_choice}) by {prompt.answered_by}"
                )
                try:
                    self._resume_fn(agent_id, body, from_id="prompt")
                except Exception as exc:
                    logger.warning(
                        "prompt %s: resume of %s failed: %s", prompt.id, agent_id, exc
                    )

        self._retract_deliveries(prompt)

    def _retract_deliveries(self, prompt: Prompt) -> None:
        from relaydeck import channels as ch

        for d in prompt.deliveries:
            if not d.get("ok"):
                continue
            channel = d.get("channel")
            if not channel:
                continue
            provider = ch.get_channel(channel)
            close = getattr(provider, "close_prompt", None)
            if provider is None or not callable(close):
                continue
            try:
                addr = ch.Address.from_dict(d)
                close(addr, str(d.get("ref") or ""), prompt)
            except Exception as exc:
                logger.debug(
                    "prompt %s: close on %s failed: %s",
                    prompt.id, d.get("channel"), exc,
                )

    # -- expiry --------------------------------------------------------

    def cancel(self, prompt_id: str, *, reason: str = "") -> Prompt | None:
        """Withdraw an open prompt, retract deliveries, and emit canceled."""
        prompt = cancel_prompt(prompt_id, reason=reason, db_path=self.db_path)
        if prompt is None:
            return None
        self._retract_deliveries(prompt)
        self._emit_event("prompt.canceled", prompt)
        return prompt

    def sweep_expired(self) -> list[Prompt]:
        """Expire overdue prompts and notify their askers. Safe to call
        on a timer; idempotent (a prompt only transitions once)."""
        expired = expire_due(db_path=self.db_path)
        for prompt in expired:
            if prompt.resume_target.startswith("relay:") and self._resume_fn:
                agent_id = prompt.resume_target.split(":", 1)[1]
                if agent_id:
                    with contextlib.suppress(Exception):
                        self._resume_fn(
                            agent_id,
                            f"⏰ Prompt {prompt.id} expired with no response.",
                            from_id="prompt",
                        )
            self._retract_deliveries(prompt)
            self._emit_event("prompt.expired", prompt)
        return expired

    # -- helpers -------------------------------------------------------

    def _emit_event(self, event_type: str, prompt: Prompt) -> None:
        if self._emit is None:
            return
        try:
            self._emit(event_type, prompt.to_dict())
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("prompt event emit failed (%s): %s", event_type, exc)


# Process-wide service singleton. The daemon wires `resume_fn` +
# `event_emit` the first time a plugin host touches `host.prompts`; tests
# build their own `PromptService` directly with fakes.
_SERVICE: PromptService | None = None


def get_service(
    *,
    db_path: str | None = None,
    resume_fn: ResumeFn | None = None,
    event_emit: EmitFn | None = None,
) -> PromptService:
    """Return the shared `PromptService`, constructing it on first use.

    Later calls may supply `resume_fn`/`event_emit` to fill in wiring
    that wasn't available at first construction (e.g. the orchestrator
    binds its `send_message_to` once it exists) without rebuilding the
    service or losing the db path.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = PromptService(
            db_path=db_path or _default_db_path(),
            resume_fn=resume_fn,
            event_emit=event_emit,
        )
    else:
        if resume_fn is not None:
            _SERVICE._resume_fn = resume_fn
        if event_emit is not None:
            _SERVICE._emit = event_emit
        if db_path:
            _SERVICE.db_path = db_path
    return _SERVICE


def reset_service() -> None:
    """Test hook: drop the singleton so the next `get_service` rebuilds."""
    global _SERVICE
    _SERVICE = None
