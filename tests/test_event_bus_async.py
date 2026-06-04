"""
Async-native event fanout on the EventBus.

Every live event SSE stream (`/api/events`, `/api/agents/{id}/events`) used to
park a thread in the default ThreadPoolExecutor on a blocking
`queue.Queue.get` (`run_in_executor(None, q.get, True, 1.0)`). With the pool
sized `min(32, cpu+4)`, a handful of long-lived viewers (dashboard + `view`
TUI + per-agent streams) saturate it — and then control-plane
`asyncio.to_thread(start_agent/stop_agent)` calls queue *behind* them.

`subscribe_async` removes the thread: an idle stream is just an awaiting
`asyncio.Queue.get`, costing no pool thread. `publish` (called from agent
threads) hands events across to the loop with `call_soon_threadsafe`. These
tests pin the cross-thread delivery, the `"*"` broadcast, and the
drop-oldest bound.
"""

from __future__ import annotations

import asyncio
import threading

from relaydeck.orchestrator import EventBus


def test_subscribe_async_receives_cross_thread_publish():
    """A publish from a *different* thread (the agent thread model) reaches an
    asyncio subscriber bound to the running loop."""

    async def scenario():
        bus = EventBus()
        sub = bus.subscribe_async("alice")

        # Publish from a non-loop thread, exactly like an agent runner does.
        t = threading.Thread(
            target=bus.publish, args=("alice", "note.left", {"text": "hi"}, 1)
        )
        t.start()
        t.join()

        event = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
        assert event["type"] == "note.left"
        assert event["agent_id"] == "alice"
        assert event["payload"] == {"text": "hi"}
        assert event["id"] == 1
        bus.unsubscribe_async("alice", sub)

    asyncio.run(scenario())


def test_async_star_subscriber_sees_every_agent():
    """The broadcast ("*") async subscriber — the dashboard / `events tail -f`
    path — receives events for any agent_id."""

    async def scenario():
        bus = EventBus()
        star = bus.subscribe_async("*")
        bus.publish("a", "x", {}, 1)
        bus.publish("b", "y", {}, 2)
        e1 = await asyncio.wait_for(star.queue.get(), timeout=2.0)
        e2 = await asyncio.wait_for(star.queue.get(), timeout=2.0)
        assert {e1["agent_id"], e2["agent_id"]} == {"a", "b"}

    asyncio.run(scenario())


def test_async_subscriber_drops_oldest_on_overflow():
    """A stalled SSE client must not grow memory without bound: a full queue
    drops its oldest event to admit the newest (newest wins, like a tail)."""

    async def scenario():
        bus = EventBus()
        sub = bus.subscribe_async("a", maxsize=2)
        for i in range(5):
            bus.publish("a", "tick", {"i": i}, i)
        # Let the cross-thread call_soon callbacks run (same loop here, so a
        # yield is enough to flush the scheduled deliveries).
        await asyncio.sleep(0)
        drained = []
        while not sub.queue.empty():
            drained.append(sub.queue.get_nowait())
        # Bounded to maxsize, and it kept the most-recent events.
        assert len(drained) == 2
        assert [e["payload"]["i"] for e in drained] == [3, 4]

    asyncio.run(scenario())


def test_thread_and_async_subscribers_both_served():
    """Legacy thread-queue consumers and async consumers coexist under one
    publish — so converting the SSE endpoints didn't strand other callers."""

    async def scenario():
        bus = EventBus()
        tq = bus.subscribe("a")
        asub = bus.subscribe_async("a")
        bus.publish("a", "z", {"k": 1}, 9)
        # Thread queue is filled synchronously inside publish.
        assert tq.get_nowait()["type"] == "z"
        # Async queue is filled via the loop.
        e = await asyncio.wait_for(asub.queue.get(), timeout=2.0)
        assert e["type"] == "z"

    asyncio.run(scenario())


def test_subscribe_events_async_via_orchestrator(tmp_path, monkeypatch):
    """The orchestrator surface (`subscribe_events_async`) backs the SSE
    endpoints; emit_event delivers to it end-to-end."""
    from pathlib import Path

    import relaydeck.orchestrator as _orch_mod
    from relaydeck.orchestrator import get_orchestrator

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)

    async def scenario():
        sub = orch.subscribe_events_async("*")
        # emit_event persists + publishes from this (loop) thread.
        orch.emit_event("operator", "deploy.started", {"service": "api"})
        e = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
        assert e["type"] == "deploy.started"
        assert e["payload"] == {"service": "api"}
        orch.unsubscribe_events_async("*", sub)

    asyncio.run(scenario())
