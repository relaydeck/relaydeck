"""
Action dispatcher for relaydeck automations.

This is the one action schema shared by every trigger in relaydeck — the
github poller's rules AND loop-agent automations both dispatch through
here, so an operator who wrote a github rule already knows how to write a
loop config. It lives in core (not a plugin) because it is part of the
host contract: the loop agent, the github plugin, and the HTTP automation
routes all depend on it, and none of them should reach into another
plugin to get it.

Each matched rule produces a list of action dicts. Every dict has
exactly one top-level key naming the action and a value carrying its
parameters. We dispatch on that key:

    {"agent.message": {"to": "reviewer", "body": "Review #42"}}
    {"script":        {"path": "scripts/notify.py"}}
    {"gh":            {"args": ["pr", "comment", "42", "--body", "ack"]}}
    {"bus.emit":      {"type": "custom-event", "data": {...}}}

The dispatcher is intentionally synchronous: actions run on the
worker tick. A misbehaving action shouldn't take the worker down,
so every dispatch is wrapped in try/except + worker.log("…", "warn")
in the poller. The dispatcher itself raises ActionError on shape
problems so the worker can decide what to log.

`subprocess.run` calls use a fixed timeout (default 60s) — a wedged
`gh` call must not lock the worker forever. The timeout is
overridable per-action via `timeout: <seconds>`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """Raised when an action dict is structurally invalid. The poller
    catches this and emits a worker.log so misconfigured rules show
    up in the workers dashboard instead of crashing the loop."""


@dataclass
class ActionContext:
    """Everything an action needs to do its job, threaded through one
    container so the dispatch signatures stay readable.

    `send_message`: bound callable that sends to a peer agent. The
    plugin wires this from `host.agents.send_message` so the gate
    check + workspace filter live in the SDK, not in this file.

    `emit_event`: `host.events.emit`-equivalent.

    `gh_binary`: resolved path or "gh"; lets the operator override
    via the plugin setting on machines where gh isn't on PATH.

    `workspace_path`: scripts run with this as cwd so a rule like
    `path: scripts/notify.py` resolves against the workspace root,
    not the daemon's cwd.

    `model_complete`: bound `complete(prompt, *, model, max_tokens) ->
    str` for the `model` action — lets an automation run a (local) model
    on each trigger and route the result. None when no model gateway is
    wired (the action then raises ActionError rather than silently
    no-op'ing).
    """

    send_message: Callable[..., Any] | None
    emit_event: Callable[[str, dict[str, Any]], None] | None
    gh_binary: str
    workspace_path: Path | None
    event: dict[str, Any]
    model_complete: Callable[..., str] | None = None


_DEFAULT_TIMEOUT_S = 60.0


def dispatch(action: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    """Run a single action dict against the context. Returns a
    summary dict so the poller can log "what just happened" with
    structured detail."""
    if not isinstance(action, dict) or len(action) != 1:
        raise ActionError(
            f"action must be a single-key mapping, got: {action!r}"
        )
    [(kind, params)] = action.items()
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ActionError(
            f"action {kind!r} params must be a mapping, got {type(params).__name__}"
        )
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise ActionError(f"unknown action {kind!r}")
    return handler(params, ctx)


# ── agent.message ──────────────────────────────────────────────────


def _handle_agent_message(params: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    to = params.get("to")
    body = params.get("body")
    if not isinstance(to, str) or not to:
        raise ActionError("agent.message requires `to: <agent-id>`")
    if not isinstance(body, str) or not body:
        raise ActionError("agent.message requires `body: <text>`")
    if ctx.send_message is None:
        raise ActionError("agent.message unavailable: orchestrator not wired")
    in_reply_to = params.get("in_reply_to")
    result = ctx.send_message(
        to=to,
        body=body,
        from_="github",
        in_reply_to=in_reply_to if isinstance(in_reply_to, str) else None,
    )
    # SendResult is dataclass-like; pull a couple of fields for the log.
    return {
        "kind": "agent.message",
        "to": to,
        "body_len": len(body),
        "msg_id": getattr(result, "ids", (None,))[0],
    }


# ── script ─────────────────────────────────────────────────────────


def _handle_script(params: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    raw_path = params.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ActionError("script requires `path`")
    path = Path(raw_path)
    if not path.is_absolute() and ctx.workspace_path is not None:
        path = ctx.workspace_path / raw_path
    if not path.exists():
        raise ActionError(f"script not found: {path}")

    timeout = float(params.get("timeout", _DEFAULT_TIMEOUT_S))
    extra_env = params.get("env", {})
    if not isinstance(extra_env, dict):
        raise ActionError("script `env` must be a mapping if provided")
    env = os.environ.copy()
    for k, v in extra_env.items():
        env[str(k)] = "" if v is None else str(v)

    cwd = ctx.workspace_path or Path.cwd()
    cmd = _resolve_script_cmd(path)
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(ctx.event).encode("utf-8"),
            cwd=str(cwd),
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "kind": "script",
            "path": str(path),
            "status": "timeout",
            "timeout_s": timeout,
        }
    return {
        "kind": "script",
        "path": str(path),
        "returncode": proc.returncode,
        "stdout_bytes": len(proc.stdout),
        "stderr_bytes": len(proc.stderr),
    }


def _resolve_script_cmd(path: Path) -> list[str]:
    """Choose how to invoke the script. Honors shebang lines so
    operators can write either executable scripts (`chmod +x`) or
    Python files with a shebang. Falls back to `python` for `.py`
    files without exec bit."""
    if os.access(path, os.X_OK):
        return [str(path)]
    if path.suffix == ".py":
        py = shutil.which("python3") or shutil.which("python") or "python"
        return [py, str(path)]
    # Fallback: run via /bin/sh so shell scripts without exec bit still
    # do something sensible.
    return ["/bin/sh", str(path)]


# ── gh ─────────────────────────────────────────────────────────────


def _handle_gh(params: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    args = params.get("args")
    if not isinstance(args, list) or not args:
        raise ActionError("gh requires `args: [<gh subcommand>, ...]`")
    rendered_args = [str(a) for a in args]
    timeout = float(params.get("timeout", _DEFAULT_TIMEOUT_S))
    cwd = ctx.workspace_path or Path.cwd()
    try:
        proc = subprocess.run(
            [ctx.gh_binary, *rendered_args],
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ActionError(f"gh binary not found: {ctx.gh_binary}") from exc
    except subprocess.TimeoutExpired:
        return {
            "kind": "gh",
            "args": rendered_args,
            "status": "timeout",
            "timeout_s": timeout,
        }
    return {
        "kind": "gh",
        "args": rendered_args,
        "returncode": proc.returncode,
        "stdout_bytes": len(proc.stdout),
        "stderr_bytes": len(proc.stderr),
    }


# ── bus.emit ───────────────────────────────────────────────────────


def _handle_bus_emit(params: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    type_ = params.get("type")
    if not isinstance(type_, str) or not type_:
        raise ActionError("bus.emit requires `type: <event-name>`")
    data = params.get("data", {})
    if not isinstance(data, dict):
        raise ActionError("bus.emit `data` must be a mapping if provided")
    if ctx.emit_event is None:
        raise ActionError("bus.emit unavailable: event bus not wired")
    ctx.emit_event(type_, dict(data))
    return {"kind": "bus.emit", "type": type_, "data_keys": list(data.keys())}


# ── code (inline) ──────────────────────────────────────────────────


def _code_cmd(lang: str) -> list[str] | None:
    """Resolve `lang` to an interpreter invocation that takes the source
    as the final argument (`<interp> -c <body>`). Returns None for an
    unsupported language so the handler can raise a clean ActionError."""
    if lang in ("python", "py", "python3"):
        py = shutil.which("python3") or shutil.which("python") or "python3"
        return [py, "-c"]
    if lang == "sh":
        return ["/bin/sh", "-c"]
    if lang == "bash":
        return [shutil.which("bash") or "/bin/bash", "-c"]
    return None


def _handle_code(params: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    """Run an INLINE code block on the trigger.

    ```yaml
    - code:
        lang: python            # python (default) | sh | bash
        body: |
          import json, sys
          ev = json.load(sys.stdin)
          print("urgent" if "down" in ev.get("issue", {}).get("title", "") else "ok")
        timeout: 30
        env: {LEVEL: high}
        emit: loop.code.result  # optional: emit stdout as this event type
    ```

    Same trust + isolation model as `script` (operator-authored, run as a
    subprocess — never `exec()` in the daemon), but the source lives
    inline in the automation so devs don't need a separate file. The
    trigger event JSON is piped on stdin; with `emit` set, captured
    stdout is emitted back onto the bus so a code block can react AND
    feed the result forward.
    """
    body = params.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ActionError("code requires `body: <source>`")
    lang = str(params.get("lang", "python")).lower()
    base = _code_cmd(lang)
    if base is None:
        raise ActionError("code `lang` must be one of python|sh|bash; got " + repr(lang))

    timeout = float(params.get("timeout", _DEFAULT_TIMEOUT_S))
    extra_env = params.get("env", {})
    if not isinstance(extra_env, dict):
        raise ActionError("code `env` must be a mapping if provided")
    env = os.environ.copy()
    for k, v in extra_env.items():
        env[str(k)] = "" if v is None else str(v)
    cwd = ctx.workspace_path or Path.cwd()

    try:
        proc = subprocess.run(
            [*base, body],
            input=json.dumps(ctx.event).encode("utf-8"),
            cwd=str(cwd),
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"kind": "code", "lang": lang, "status": "timeout", "timeout_s": timeout}

    emitted = None
    emit_type = params.get("emit")
    if isinstance(emit_type, str) and emit_type:
        if ctx.emit_event is None:
            raise ActionError("code `emit` set but event bus not wired")
        ctx.emit_event(emit_type, {
            "stdout": proc.stdout.decode("utf-8", "replace"),
            "returncode": proc.returncode,
            "trigger": ctx.event,
        })
        emitted = emit_type

    return {
        "kind": "code",
        "lang": lang,
        "returncode": proc.returncode,
        "stdout_bytes": len(proc.stdout),
        "stderr_bytes": len(proc.stderr),
        "emitted": emitted,
    }


# ── model ──────────────────────────────────────────────────────────


def _append_workspace_files(prompt: str, params: dict[str, Any],
                            ctx: ActionContext) -> str:
    """Append workspace file contents to a model prompt when requested.

    `read_files` (or legacy alias `include_files`) accepts a string or
    list of paths relative to `ctx.workspace_path`. Paths must stay
    inside the workspace root."""
    raw = params.get("read_files") or params.get("include_files")
    if not raw or ctx.workspace_path is None:
        return prompt
    if isinstance(raw, str):
        paths = [raw]
    elif isinstance(raw, list):
        paths = [str(p) for p in raw]
    else:
        raise ActionError("read_files must be a string or list of paths")
    ws = ctx.workspace_path.resolve()
    blocks: list[str] = []
    for rel in paths:
        rel = rel.strip().lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise ActionError(f"read_files: invalid path {rel!r}")
        path = (ws / rel).resolve()
        try:
            path.relative_to(ws)
        except ValueError as exc:
            raise ActionError(
                f"read_files: path escapes workspace: {rel!r}"
            ) from exc
        if not path.is_file():
            logger.warning("read_files: skipping missing %s", rel)
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ActionError(f"read_files: cannot read {rel!r}: {exc}") from exc
        blocks.append(f"## {rel}\n{text.rstrip()}\n")
    if not blocks:
        return prompt
    return prompt + "\n\n--- workspace files ---\n\n" + "\n".join(blocks)


def _handle_model(params: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    """Run a model on the trigger and route the result.

    ```yaml
    - model:
        prompt: "Is this issue urgent? Answer yes or no."
        model: role:fast         # role | preset | provider/model | alias
        max_tokens: 64
        include_event: true      # append the trigger JSON to the prompt
        read_files:               # optional — append workspace file contents
          - inbox/today.md
          - home/brief-context.md
        emit: loop.model.result  # emit the text as this event type
        to: triager              # and/or send the text to this agent
    ```

    This is the "load a model and let it rip" primitive: a cheap local
    model reacting to anything the automation is triggered by. At least
    one sink (`emit` or `to`) is usual but not required — the returned
    summary always carries the text length so a bare call still records.
    """
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ActionError("model requires `prompt: <text>`")
    if ctx.model_complete is None:
        raise ActionError("model unavailable: model gateway not wired")
    model = str(params.get("model", "role:fast"))
    try:
        max_tokens = int(params.get("max_tokens", 256))
    except (TypeError, ValueError) as exc:
        raise ActionError("model `max_tokens` must be an integer") from exc

    full_prompt = _append_workspace_files(prompt, params, ctx)
    if params.get("include_event"):
        try:
            full_prompt = (
                full_prompt + "\n\nEvent:\n"
                + json.dumps(ctx.event, default=str, indent=2)
            )
        except Exception:
            pass

    try:
        text = ctx.model_complete(full_prompt, model=model, max_tokens=max_tokens)
    except ActionError:
        raise
    except Exception as exc:
        raise ActionError(
            f"model completion failed: {type(exc).__name__}: {exc}"
        ) from exc
    text = text or ""

    emitted = None
    emit_type = params.get("emit")
    if isinstance(emit_type, str) and emit_type:
        if ctx.emit_event is None:
            raise ActionError("model `emit` set but event bus not wired")
        ctx.emit_event(emit_type, {"text": text, "model": model, "trigger": ctx.event})
        emitted = emit_type

    sent_to = None
    to = params.get("to")
    if isinstance(to, str) and to:
        if not text.strip():
            raise ActionError("model returned empty text; nothing sent to agent")
        if ctx.send_message is None:
            raise ActionError("model `to` set but messaging not wired")
        sender = params.get("from")
        ctx.send_message(
            to=to, body=text,
            from_=sender if isinstance(sender, str) and sender else "model",
        )
        sent_to = to

    return {
        "kind": "model",
        "model": model,
        "prompt_len": len(full_prompt),
        "text_len": len(text),
        "emitted": emitted,
        "sent_to": sent_to,
    }


_HANDLERS: dict[str, Callable[[dict[str, Any], ActionContext], dict[str, Any]]] = {
    "agent.message": _handle_agent_message,
    "script": _handle_script,
    "gh": _handle_gh,
    "bus.emit": _handle_bus_emit,
    "model": _handle_model,
    "code": _handle_code,
}
