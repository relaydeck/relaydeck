# Architecture — a reliable orchestration OS (delivered)

**Status:** ✅ DELIVERED. Started 2026-06-04 on
`feat/orchestration-command-center`; Phases 0–4 are all shipped, tested, and
committed. This is now a **record of the delivered architecture** — see
§5a for the (small, optional) future ideas that remain out of scope.
**Audience:** whoever maintains or extends the orchestration layer.

This is the architect's view of where relaydeck goes after the command-center
branch: from "a CLI + skill that *can* spawn a fleet" to **a reliable
multi-agent operating system** — add a skill, your harness spins up other
harness agents, and a command center (TUI + web) shows everything live with
messages and results flowing back, while a manager watches each agent's
context/usage/limits and acts before anything stalls.

The north star (memory `automation_vision`, `realtime_platform`,
`web_first_platform`): **customizable workers = trigger + model + code**, a
real-time dashboard by default, full CLI parity. This doc grounds that in the
current code and sequences the work by leverage.

---

## 1. The experience we're building toward

**As a developer**, from inside any harness (Claude Code, Codex, Cursor, …):

```
# one gesture from a directory to a live command center
relaydeck open .                 # find-or-register workspace, daemon up, TUI
# or drive it from your agent via the relaydeck skill:
relaydeck agent create reviewer --type codex   --workspace .
relaydeck agent create builder  --type claude-code --workspace .
relaydeck agent start reviewer builder
relaydeck broadcast "review PR #42; builder, fix what reviewer files"
```

…and then **see it all**: the TUI command center (`relaydeck view`) and the web
dashboard show the same live firehose — every agent's screen, messages flowing,
events streaming, and a usage panel that says *"reviewer is at 82% context,
builder hits the 5-hour cap in ~20 min."* The manager (you, or an
orchestrating agent) acts: compact, fresh session, switch model, hand off.

Three properties make this an *OS* rather than a script runner:

1. **Reliable** — results always make it back; a daemon restart or a slow
   viewer never silently drops work; crashes are visible within a tick.
2. **Cheap to watch** — N viewers and M agents don't melt the box; idle
   streams cost ~nothing.
3. **Observable + actionable** — every meaningful thing is an *event*
   (audit/visibility/hooks), and every event a human would react to has a
   one-command action behind it.

---

## 2. Current state (what already works)

Grounded in code, not aspiration:

- **Spawn + lifecycle is durable.** `agent create` writes a YAML spec + an
  `agents` DB row; `start_agent` runs the harness in a daemon thread; states
  `pending → running → stopped/errored` are persisted
  (`orchestrator.py:395-455`, `:554-719`, `db.py`).
- **Events persist + stream.** `BaseAgent.emit` / `Orchestrator.emit_event`
  write an `events` row *and* fan out on `_bus`; SSE serves `/api/events` (`*`)
  and `/api/agents/{id}/events`. Operators can now write to the stream too
  (`broadcast`, `events emit`) — the command-center branch closed that.
- **Messaging is durable + retried.** `agent_messages` rows with a
  `queued → delivered/failed` state machine; live PTY push with an optional
  echo-confirm, plus drain-on-startup so a restart redelivers
  (`messages.py`, `harness/base.py:1099-1161`).
- **Usage is metered per turn.** Most harnesses emit `usage.record`
  (pi, claude-code, codex, opencode, relaydeck-native) → `usage_records` table;
  the `usage-limits` plugin tracks rolling **5-hour session** and **weekly**
  windows per agent and emits `usage_limits.threshold` / `.exceeded`, with
  optional auto-pause (`plugins/metering/`, `plugins/usage_limits/`).
- **Command center exists.** `relaydeck view` has Terminal/Events/Messages/
  Tasks tabs + plugin-contributed TUI tabs + a CLI console; the web dashboard
  mirrors it over the same SSE bridge.

So the bones are good. The gaps are in **reliability under load**, **resource
cost of observation**, **result capture**, and **context/limit awareness**.

---

## 3. The reliability + resource model — findings

### 3.1 SSE was thread-per-subscriber (FIXED — Phase 0)

Every live event stream parked a thread in the **default ThreadPoolExecutor**
on a blocking `queue.Queue.get` (`run_in_executor(None, q.get, True, 1.0)`).
The pool is `min(32, cpu+4)` — **8 on a 4-core box**. The dashboard + `view`
TUI + every `events tail -f` + per-agent state/event/message streams each
**permanently occupy one pool thread**. Once the pool saturates,
control-plane `asyncio.to_thread(start_agent / stop_agent / runtime-stats)`
calls **queue behind the blocked viewers** — i.e. *watching the fleet starves
operating the fleet.* That's a hard ceiling at single-digit viewers, exactly
the load the command-center vision invites.

**Shipped this branch:** an async-native fanout on `EventBus`
(`subscribe_async` → `asyncio.Queue`, delivered cross-thread with
`loop.call_soon_threadsafe`, drop-oldest bound). `/api/events` and
`/api/agents/{id}/events` now `await asyncio.wait_for(sub.queue.get(), 15)` —
**an idle stream costs zero threads.** The legacy thread-queue API stays for
in-process consumers; the PTY WebSocket is deliberately untouched (terminal
contract). Tests: `tests/test_event_bus_async.py`.

**Still on the executor (Phase 1):** the two *plugin-bus*-backed streams —
`/api/agents/{id}/state/stream` and `/api/workspaces/{ws}/messages/stream`
(`api.py`, pyee handler + local queue + `run_in_executor`). Convert them the
same way: have the pyee handler `call_soon_threadsafe` into an `asyncio.Queue`.

### 3.2 Reliability gaps (ranked)

| # | Gap | Evidence | Risk |
|---|-----|----------|------|
| R1 | **No result primitive.** "Collect results" = `workspace inbox` (peer messages) or scrollback. PTY buffer is a 256KB ring, never persisted; events capped at 100/agent. If an agent crashes, its output is gone. | `orchestrator.py:1173` (limit=100); skill `recipes.md` | Work silently lost |
| R2 | **Reconnect misses events.** Bus is in-memory; a viewer that drops and reconnects after a daemon blip sees nothing emitted while away — DB has it, but no automatic `since_id` backfill on the live stream. | `api.py` stream subscribe-after-history | Audit/visibility holes |
| R3 | **Crash visibility is a poll.** Zombie reconcile runs on an interval; for up to one tick `agent list` can call a dead agent "running," and a message sent in that window queues against a corpse. | `orchestrator.py` sweeper | Operator acts on stale truth |
| R4 | **Failed messages are silent.** After N delivery attempts a message goes `failed` with no event/alert; operator must query for it. | `messages.py` | Dropped coordination |
| R5 | **Shutdown can SIGKILL agents.** Per-thread 10s join vs 3s graceful daemon shutdown — many agents can't stop cleanly. | `orchestrator.py` join; `cli.py` timeout | Corrupted in-flight work |
| R6 | **No context-fullness signal.** Nothing tracks tokens-used vs model-max; no "approaching compaction" event. | (absent) | Manager can't act early — see §4 |

---

## 4. The missing pillar: context / usage / limit awareness (manager-actionable)

The dev's ask: *be hyper-aware of each coding agent's context usage, context
limits, and provider 5-hour / weekly caps — emit them so the manager can act
(new session, compact, custom command), with KV-cache kept warm and no
correctness problems. Everything emitted for audit/visibility/hooks.*

**What we have** (don't rebuild): per-turn `usage.record`; the `usage-limits`
plugin's 5h + weekly rolling windows and `usage_limits.threshold/.exceeded`
events with auto-pause; `reset_agent_session` (in-place if the harness supports
it, else fresh restart); `restart_agent(session=fresh|resume)`.

**What's missing** (the gap to close):

1. **Context-window fullness.** No tokens-used-vs-max per agent, no
   `context.pressure` event. This is the single most useful new signal — it's
   what tells a manager to compact *before* the harness auto-compacts (which
   blows the KV cache and can drop instructions). Source it from each harness:
   claude-code/codex/opencode surface usage and model max; compute
   `pct = used / context_max` and emit `agent.context` (`pct`, `used`, `max`,
   `source`) on a change threshold. Harnesses that don't expose it (cursor —
   opaque store.db; antigravity) report `source="unknown"` honestly rather than
   guessing.
2. **Provider-level caps, not just per-agent.** `usage-limits` budgets are
   per-agent token windows; a provider's *account-wide* 5h/weekly cap is
   shared across all agents on that key. Add a provider-scoped roll-up
   (`provider.usage` / `provider.limit` events) so the manager sees "Anthropic
   account hits the weekly cap in ~2h across 4 agents," not just per-agent.
   Where the harness exposes rate-limit headers/notices, parse them; else
   estimate from summed `usage_records`.
3. **Compaction as a first-class, KV-safe action.** Today the only lever is
   reset/restart (throws away the session). Add `agent compact` that asks the
   harness to summarize-and-trim *in place* where supported (claude-code
   `/compact`, codex equivalents), keeping the prefix stable so the KV cache
   stays warm; fall back to a fresh session with a carried-over handover
   summary where not. Surface it as the one-tap action behind a
   `context.pressure` / `usage_limits.threshold` event.
4. **A `manager` policy layer (composition, like autopilot/hitl).** A plugin
   that subscribes to `agent.context`, `usage_limits.*`, `provider.*` and runs
   a declarative policy: *at 85% context → compact; at session-cap warn →
   switch this agent to a cheaper model or pause; at weekly-cap → stop new
   spawns.* Every decision emits `manager.action` (audit) and is overridable.
   This is the "trigger + model + code" worker vision applied to the fleet's
   own health.

**Audit/visibility/hooks (cross-cutting).** All of the above are *events* on
the same bus the dashboard, `view`, and `events tail -f` already consume, and
any plugin can `host.events.subscribe("agent.context")` /
`subscribe("manager.*")` to hook custom behavior. Add a thin **event taxonomy
doc + a stable `since_id` replay contract** (see R2) so an external auditor can
reconstruct the full timeline. No new transport — just disciplined emission.

---

## 5. Roadmap (sequenced by leverage)

**Phase 0 — shipped on this branch**
- [x] Async event-bus fanout → kill thread-per-subscriber on `/api/events` +
      `/api/agents/{id}/events`. (`orchestrator.py`, `api.py`,
      `tests/test_event_bus_async.py`)
- [x] `relaydeck open [path]` — context-aware on-ramp: find-or-register the
      owning workspace, ensure the daemon, open TUI / `--web` / `--no-view`.
      (`cli.py`, `tests/test_open_command.py`)

**Phase 1 — finish the resource fix + close the loudest reliability gaps (DONE)**
- [x] Convert the plugin-bus SSE streams (`state/stream`, `messages/stream`)
      off `run_in_executor` (§3.1) — `_plugin_bus_sse` bridge.
- [x] **R2 reconnect-safe replay:** `?since_id=` on `/api/agents/{id}/events` —
      subscribe before the history read (no gap), dedupe the overlap (no dup).
- [x] **R4:** `agent.message_failed` emitted when delivery exhausts retries.
- [x] **R5:** shared-deadline concurrent join at shutdown (no N×serial SIGKILL).

**Phase 2 — result capture (R1) (DONE)**
- [x] `agent_results` table (migration 19, latest-wins per agent+key) +
      `Orchestrator.put_result/get_results` (emits `agent.result`) +
      `POST/GET /api/agents/{id}/result` + `relaydeck agent result put/get`.
      The skill's "collect results" is now a durable guarantee, not scrollback.
- [x] Opt-in transcript persistence (`RELAYDECK_TRANSCRIPT_BYTES`) snapshots a
      crashed agent's last screen on exit → `agent transcript` / endpoint.

**Phase 3 — context/usage/limit awareness (§4) (DONE)**
- [x] `agent.context` fullness event + a Context tab — the `context-watch`
      plugin computes fill (latest prompt tokens vs the model's context window
      from models.dev) off `usage.record` and emits warn/critical/recovery.
- [x] `manager` policy plugin — `agent.context` (critical), `usage_limits.exceeded`,
      and `agent.message_failed` → auditable `manager.action`; recommend by
      default, opt-in compact / fresh-session / pause. Composes with
      usage-limits/autopilot/hitl.
- [x] Provider-account-wide usage roll-up — `usage_limits.provider_threshold/_exceeded`
      summed across all agents on a provider/key (the shared 5h/weekly cap).
- [x] `agent compact` — KV-safe in-place compaction (claude-code `/compact`;
      harnesses without one report "unsupported"), wired as the manager's
      `compact` action; `Orchestrator.compact_agent` + endpoint + CLI.
- [x] `agent.message_failed` routed through the plugin bus so the manager reacts
      (and it still bridges to SSE).

**Phase 4 — OS feel (DONE)**
- [x] autopilot `held`/`unblocked`, `usage_limits.*`, `manager.*`, and
      `agent.context` all bridged onto the SSE feed — every policy/health
      decision is live + auditable on the dashboard, `view`, and `events tail`.
- [x] One-tap escalation — `agent escalate` emits `hitl.escalation` so the
      configured channels ping a human after a hold or a context-critical alert.

---

## 5a. Status: roadmap delivered

Every phase above is shipped, tested, and committed on
`feat/orchestration-command-center`. This document is now a record of the
delivered architecture, not an open work-list.

**Genuinely future / optional** (not blocking, not started — each needs a
capability that doesn't exist yet, so they're deliberately out of scope):
- Manager `switch-model` / `gate-spawns` actions — need runtime model-change
  and spawn-interception hooks.
- A dedicated, styled "fleet health" card in the web dashboard (the events are
  already in the feed; this is pure web-UI polish).
- Carrying a handover summary across a `compact`/fresh-session fallback.

---

## 6. Principles to hold

- **Observation must be cheap.** Anything a viewer does at rest must cost no
  thread and no poll. Push, don't spin. (The async fanout is the template.)
- **Every reaction is an event first.** If a manager would act on it, emit it —
  then the dashboard, hooks, and audit get it for free.
- **The terminal/PTY path is untouchable** without explicit per-change
  approval (memory `feedback_terminal_untouchable`). Reliability work routes
  *around* the PTY read loop, never through it.
- **Compose, don't centralize.** autopilot + hitl + manager are separate
  policy plugins on one bus, not one monolith — each independently
  testable and overridable.
- **Honest zeros.** A harness that can't report usage/context says so
  (`source="unknown"`), never a fabricated number.
