"""
End-to-end smoke test: spawn a real `pi` harness against OpenRouter,
send one prompt, verify the assistant-message event fires.

If this passes, the whole stack works:
  - HarnessAgent spawns pi in a real PTY
  - Message injection (`\\r` not `\\n`) submits to the harness child
  - pi's session JSONL gets tailed
  - emote plugin's classifier fires on `harness.assistant_message`
  - the bus delivers events to subscribers in-process

If this fails, the unit suite probably already passes -- this is the
integration canary that says "yes, the parts compose."

Runs only in the dedicated e2e workflow (`pytest -m e2e`) because it
needs a real LLM endpoint and a real harness binary.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def test_pi_round_trip(
    pi_binary: str,
    openrouter_key: str,
    e2e_model: str,
    isolated_home: Path,
) -> None:
    """Spawn pi, send one short prompt, wait for an assistant message
    on the bus within 90 seconds."""
    import relaydeck.plugin as plug
    from relaydeck.config import AgentSpec
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.plugin import PluginContext, get_registry

    # Per-agent session directory pi writes its JSONL into, and which
    # PiAgent's tailer Worker reads from. Creating it up front so pi
    # doesn't race against the tailer registration on first turn.
    workspace = "smoke-ws"
    agent_id = "smoke-pi"
    session_dir = (
        isolated_home / "workspaces" / workspace
        / "runtime" / "pi-sessions" / agent_id
    )
    session_dir.mkdir(parents=True)

    # Load plugins so the emote classifier-source + pi harness register
    # and the orchestrator picks up a fresh bus.
    plug._registry = None
    reg = get_registry(isolated_home)
    ctx = PluginContext(config_home=isolated_home)
    reg.load_all(ctx)
    bus = ctx.event_bus
    assert bus is not None, "plugin bus didn't initialise"

    # Wire the bus into the orchestrator so harness emits land where
    # we'll subscribe. In production this is `relaydeck serve` boot code.
    orch = get_orchestrator(isolated_home)
    orch.set_event_bus(bus)

    # Subscribe BEFORE starting the agent so we don't race the first
    # assistant turn. Also collect EVERY event for diagnostic dump on
    # failure -- a free-tier model going slow looks identical to a
    # broken stack from the outside, and we'd rather know which.
    received: list[dict] = []
    all_events: list[tuple[str, dict]] = []
    flag = threading.Event()

    def on_msg(event):
        data = event.data or {}
        if data.get("agent_id") == agent_id:
            received.append(data)
            flag.set()

    def on_any(event):
        all_events.append((event.type, event.data or {}))

    bus.subscribe("harness.assistant_message", on_msg)
    bus.subscribe("*", on_any)

    # Persist the agent spec. PiAgent honors `config.command` as a full
    # command override, so we pass --provider/--model directly and keep
    # --session-dir pointed at the same path the tailer reads from.
    spec = AgentSpec(
        id=agent_id,
        name=agent_id,
        type="pi",
        workspace=workspace,
        purpose="E2E smoke test agent",
        config={
            "command": [
                pi_binary,
                "--provider", "openrouter",
                "--model", e2e_model,
                "--mode", "text",
                "--session-dir", str(session_dir),
            ],
        },
    )
    spec.save(isolated_home)

    # YAML is source-of-truth, but the orchestrator's send_message_to /
    # get_agent / list paths read from the DB mirror. `Orchestrator.start()`
    # is what 'relaydeck serve' calls on daemon boot to do the sync; mirror
    # that in-process so the spec we just wrote is visible.
    orch.start()
    orch.start_agent(agent_id)

    try:
        # Wait until pi's TUI has rendered something to the PTY before
        # injecting input. A flat sleep races: a cold GH runner can take
        # 4-6s before pi's input field is wired up to consume `\r`, and
        # a too-early write lands in nothing. Poll the ring buffer for
        # ANY bytes (pi prints its banner + model line within ~1s of a
        # working spawn) with a generous ceiling.
        instance = orch.get_running_instance(agent_id)
        assert instance is not None, "agent has no running instance"
        ready_deadline = time.time() + 15.0
        while time.time() < ready_deadline:
            if len(getattr(instance, "_pty_buffer", b"")) > 256:
                break
            time.sleep(0.25)
        else:
            pytest.fail("pi PTY produced no output in 15s -- agent likely didn't spawn")
        # One extra second so pi's TUI finishes painting the input field.
        time.sleep(1.0)

        # Send a tiny prompt. The body itself doesn't matter; we only
        # need pi to emit ANY assistant turn so the bus subscriber
        # fires.
        # `format="{body}"` strips the chat-block envelope (`[relay from=...]`)
        # that the messaging plugin would normally wrap around peer
        # messages. A test prompt to a model isn't a peer message; we
        # don't want the model trying to interpret the envelope.
        orch.send_message_to(
            agent_id,
            body="Say the single word 'pong' and nothing else.",
            from_id="e2e",
            format="{body}",
        )

        # Wait up to 120s for any assistant message. Poll the session
        # JSONL while we wait so we can fast-fail on pi API errors
        # (the upstream model returning 404 / 401 shows up as an
        # assistant message with empty content) instead of paying the
        # full ceiling for a deterministic failure.
        deadline = time.time() + 120.0
        while time.time() < deadline:
            if flag.wait(timeout=2.0):
                break
            err = _detect_pi_api_error(session_dir)
            if err:
                pytest.fail(
                    f"pi reported an upstream API error -- bailing early:\n"
                    f"  {err}\n\n"
                    + _dump_diagnostics(orch, agent_id, session_dir, all_events)
                )
        else:
            pytest.fail(_dump_diagnostics(
                orch, agent_id, session_dir, all_events,
            ))
        assert received, "flag set but no payload captured"
        assert received[0].get("harness") == "pi"
        assert received[0].get("text"), "assistant message had no text"
    finally:
        orch.stop_agent(agent_id)


def _detect_pi_api_error(session_dir: Path) -> str | None:
    """If pi wrote an assistant message with empty content, the model
    call failed upstream (404, 401, rate limit, etc.). Returns a short
    one-liner; None when no error is visible yet."""
    import json as _json
    for jsonl in session_dir.rglob("*.jsonl"):
        try:
            text = jsonl.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            try:
                rec = _json.loads(line)
            except (ValueError, _json.JSONDecodeError):
                continue
            if rec.get("type") != "message":
                continue
            msg = rec.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, list) and len(content) == 0:
                return (
                    f"empty assistant content in {jsonl.name} "
                    f"(provider={msg.get('provider')!r}, "
                    f"model={msg.get('model')!r})"
                )
    return None


def _dump_diagnostics(orch, agent_id, session_dir, all_events) -> str:
    """Return a multi-line failure description: agent status, recent
    events, PTY ring buffer tail, session JSONL listing + first lines
    of each. Lets the workflow log tell us what actually happened
    instead of forcing a local repro."""
    lines = ["E2E smoke timed out waiting for harness.assistant_message."]

    info = orch.get_agent(agent_id) or {}
    lines.append(f"  agent.status         = {info.get('status')!r}")
    lines.append(f"  agent.semantic_status= {info.get('semantic_status')!r}")
    lines.append(f"  agent.last_error     = {info.get('last_error')!r}")

    lines.append(f"  events seen ({len(all_events)}):")
    for ev_type, ev_data in all_events[-25:]:
        lines.append(f"    - {ev_type}: {ev_data!r}"[:200])

    instance = orch.get_running_instance(agent_id)
    if instance is not None and hasattr(instance, "_pty_buffer"):
        tail = bytes(instance._pty_buffer)[-2000:]
        lines.append("  PTY ring buffer tail (last ~2KB):")
        lines.append("    " + tail.decode("utf-8", errors="replace").replace("\n", "\n    "))
    else:
        lines.append("  PTY ring buffer: <no running instance>")

    if session_dir.exists():
        jsonls = sorted(session_dir.rglob("*.jsonl"))
        lines.append(f"  session jsonls ({len(jsonls)}):")
        for path in jsonls:
            lines.append(f"    - {path}: {path.stat().st_size} bytes")
            try:
                head = path.read_text().splitlines()[:5]
                for line in head:
                    lines.append(f"        {line[:200]}")
            except OSError as exc:
                lines.append(f"        (read failed: {exc})")
    else:
        lines.append(f"  session_dir does not exist: {session_dir}")

    return "\n".join(lines)
