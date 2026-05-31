"""
Per-call LLM invocation log — the granular trace of every model call a
worker's `model` action makes (and any future model caller).

This is what gives the Workers lens real visibility into "what did this
worker ask a model, when, how long did it take, how many tokens." It is
deliberately distinct from `usage_records` (the metering rollup fed by
harness JSONL tailers): a row here is ONE invocation, with latency, char
counts, ok/error, and the resolved provider/model — keyed by the calling
agent/worker id.

Token counts are REAL when the provider reports them (ollama does, via
`complete_ex`); otherwise they're 0 with `tokens_known=False`. We never
fabricate token counts — an unknown is recorded as unknown.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck.db import open_db


@dataclass
class ModelInvocation:
    id: int
    agent_id: str
    ts: float
    model: str
    provider: str
    latency_ms: int
    prompt_chars: int
    completion_chars: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    tokens_known: bool
    ok: bool
    error: str | None
    source: str | None
    prompt: str | None = None
    response: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "agent_id": self.agent_id, "ts": self.ts,
            "model": self.model, "provider": self.provider,
            "latency_ms": self.latency_ms, "prompt_chars": self.prompt_chars,
            "completion_chars": self.completion_chars,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tokens_known": self.tokens_known, "ok": self.ok,
            "error": self.error, "source": self.source,
            "prompt": self.prompt, "response": self.response,
        }


# Cap stored prompt/response text — enough to inspect, bounded so the
# invocation log can't bloat the DB on long generations.
EXCERPT_LIMIT = 8000


def _excerpt(s: str | None) -> str | None:
    if not s:
        return s
    return s if len(s) <= EXCERPT_LIMIT else (s[:EXCERPT_LIMIT] + "\n…[truncated]")


def _default_db_path() -> str:
    return str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db")


def _row(r: sqlite3.Row) -> ModelInvocation:
    return ModelInvocation(
        id=int(r["id"]), agent_id=r["agent_id"], ts=float(r["ts"] or 0.0),
        model=r["model"] or "", provider=r["provider"] or "",
        latency_ms=int(r["latency_ms"] or 0),
        prompt_chars=int(r["prompt_chars"] or 0),
        completion_chars=int(r["completion_chars"] or 0),
        prompt_tokens=int(r["prompt_tokens"] or 0),
        completion_tokens=int(r["completion_tokens"] or 0),
        total_tokens=int(r["total_tokens"] or 0),
        tokens_known=bool(r["tokens_known"]),
        ok=bool(r["ok"]), error=r["error"], source=r["source"],
        prompt=(r["prompt"] if "prompt" in r.keys() else None),
        response=(r["response"] if "response" in r.keys() else None),
    )


def record_invocation(
    agent_id: str,
    *,
    model: str = "",
    provider: str = "",
    latency_ms: int = 0,
    prompt_chars: int = 0,
    completion_chars: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    tokens_known: bool = False,
    ok: bool = True,
    error: str | None = None,
    source: str | None = None,
    prompt: str | None = None,
    response: str | None = None,
    db_path: str | None = None,
) -> None:
    """Append one invocation. Best-effort by convention — callers wrap in
    try/except so metering never breaks the worker. `prompt`/`response`
    are stored truncated (see EXCERPT_LIMIT) for the 'what did it ask /
    get' view — bounded so the log doesn't bloat."""
    conn = open_db(db_path or _default_db_path())
    try:
        conn.execute(
            """INSERT INTO model_invocations
               (agent_id, ts, model, provider, latency_ms, prompt_chars,
                completion_chars, prompt_tokens, completion_tokens,
                total_tokens, tokens_known, ok, error, source, prompt, response)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, time.time(), model, provider, int(latency_ms),
             int(prompt_chars), int(completion_chars), int(prompt_tokens),
             int(completion_tokens), int(total_tokens), 1 if tokens_known else 0,
             1 if ok else 0, error, source,
             _excerpt(prompt), _excerpt(response)),
        )
        conn.commit()
    finally:
        conn.close()


def timed_complete(
    agent_id: str,
    prompt: str,
    *,
    model: str = "role:fast",
    max_tokens: int = 256,
    source: str | None = None,
    db_path: str | None = None,
    config_home: "Path | None" = None,
    **kwargs: Any,
) -> str:
    """Run a model completion AND record the invocation. The single
    chokepoint the `model` action uses so every worker model call shows
    up in the Workers lens. Times the call, captures real tokens when the
    provider reports them, records ok/error, and returns the text
    (re-raising any error after recording it). Recording failures are
    swallowed — metering must never break the worker."""
    from relaydeck.sdk import complete_with_model_ex

    t0 = time.time()
    text = ""
    usage: dict[str, Any] = {}
    ok = True
    err: str | None = None
    try:
        text, usage = complete_with_model_ex(
            prompt, model=model, max_tokens=max_tokens,
            config_home=config_home, **kwargs
        )
        return text
    except Exception as exc:
        ok = False
        err = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            record_invocation(
                agent_id,
                model=usage.get("model") or model,
                provider=usage.get("provider") or "",
                latency_ms=int((time.time() - t0) * 1000),
                prompt_chars=len(prompt),
                completion_chars=len(text),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                tokens_known=bool(usage.get("tokens_known")),
                ok=ok, error=err, source=source,
                prompt=prompt, response=text,
                db_path=db_path,
            )
        except Exception:
            pass


def list_invocations(
    agent_id: str, *, limit: int = 50, db_path: str | None = None,
) -> list[ModelInvocation]:
    conn = open_db(db_path or _default_db_path())
    try:
        rows = conn.execute(
            "SELECT * FROM model_invocations WHERE agent_id = ? "
            "ORDER BY ts DESC LIMIT ?",
            (agent_id, int(limit)),
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def list_by_model(
    model: str, *, limit: int = 30, db_path: str | None = None,
) -> list[ModelInvocation]:
    """Recent invocations for a given model id (case-insensitive),
    newest first — for the preset-detail 'recent calls' stream. Only
    `model`-action calls are traced here (harness usage isn't), so this
    is honestly empty for presets used only by chat agents."""
    conn = open_db(db_path or _default_db_path())
    try:
        rows = conn.execute(
            "SELECT * FROM model_invocations WHERE LOWER(model) = ? "
            "ORDER BY ts DESC LIMIT ?",
            ((model or "").lower(), int(limit)),
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def rollup_by_model(
    model: str, *, since_ts: float | None = None, db_path: str | None = None,
) -> dict[str, Any]:
    """Latency + success rollup for one model id (case-insensitive).
    `count` is 0 when nothing has been traced — the client renders '—'
    rather than a fake success rate."""
    conn = open_db(db_path or _default_db_path())
    try:
        q = ("SELECT COUNT(*) AS n, "
             "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS errors, "
             "AVG(latency_ms) AS avg_latency, MAX(ts) AS last_ts "
             "FROM model_invocations WHERE LOWER(model) = ?")
        params: list[Any] = [(model or "").lower()]
        if since_ts is not None:
            q += " AND ts >= ?"
            params.append(since_ts)
        r = conn.execute(q, params).fetchone()
        n = int(r["n"] or 0)
        errors = int(r["errors"] or 0)
        return {
            "count": n,
            "errors": errors,
            "success_rate": ((n - errors) / n) if n else None,
            "avg_latency_ms": int(r["avg_latency"]) if r["avg_latency"] is not None else 0,
            "last_ts": float(r["last_ts"]) if r["last_ts"] is not None else None,
        }
    finally:
        conn.close()


def rollup(agent_id: str, *, db_path: str | None = None) -> dict[str, Any]:
    """Totals for an agent's invocations: count, errors, token sum (only
    counts rows where tokens are known), avg latency, last ts."""
    conn = open_db(db_path or _default_db_path())
    try:
        r = conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS errors,
                      SUM(total_tokens) AS tokens,
                      SUM(CASE WHEN tokens_known=1 THEN 1 ELSE 0 END) AS known,
                      AVG(latency_ms) AS avg_latency,
                      MAX(ts) AS last_ts
               FROM model_invocations WHERE agent_id = ?""",
            (agent_id,),
        ).fetchone()
        n = int(r["n"] or 0)
        return {
            "count": n,
            "errors": int(r["errors"] or 0),
            "total_tokens": int(r["tokens"] or 0),
            "tokens_known_count": int(r["known"] or 0),
            "avg_latency_ms": int(r["avg_latency"]) if r["avg_latency"] is not None else 0,
            "last_ts": float(r["last_ts"]) if r["last_ts"] is not None else None,
        }
    finally:
        conn.close()


def prune_invocations(
    *, older_than_days: float = 30.0, db_path: str | None = None,
) -> int:
    cutoff = time.time() - (older_than_days * 86400.0)
    conn = open_db(db_path or _default_db_path())
    try:
        cur = conn.execute(
            "DELETE FROM model_invocations WHERE ts < ?", (cutoff,)
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()
