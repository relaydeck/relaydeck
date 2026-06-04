# Handover — orchestration command center

**Branch:** `feat/orchestration-command-center`
**Date:** 2026-06-03
**Scope:** Review the CLI foundation; close the real gaps; turn `relaydeck
view` into a command center; ship a publishable skill that lets an external
agent install + drive relaydeck to orchestrate a fleet — with safe handling
of the native prompts that stall an unattended fleet.

---

## TL;DR

The CLI foundation was already mature and complete. Three real gaps existed,
all now closed, plus the command-center TUI rework and the publishable skill:

| # | Deliverable | Key files |
|---|---|---|
| 1 | **Event broadcast/emit + global tail** — write side of the event stream | `orchestrator.py`, `transports/api.py`, `transports/cli.py` |
| 2 | **`agent unblock`** — answer stuck native prompts headlessly | `transports/api.py`, `transports/cli.py` |
| 3 | **`autopilot` plugin** — auto-answer *benign* prompts, hold the rest | `plugins/autopilot/` |
| 4 | **`RELAYDECK_ORCHESTRATION_DEPTH`** — nesting marker for inside/outside | `harness/base.py` |
| 5 | **`relaydeck-orchestrate` skill** — publishable outside-in on-ramp | `skills/relaydeck-orchestrate/` |
| 6 | **`relaydeck view` command center** — tabbed panes + CLI console | `transports/view.py` |

---

## 1. Event broadcast / emit / tail

**Why:** the event stream was read-only from the CLI — `agent events` could
*view*, `/api/events` (SSE) existed, but nothing could *publish* an
operator/announcement event, and there was no global tail.

**What:**
- `Orchestrator.emit_event(agent_id, type, payload)` — the operator twin of
  `BaseAgent.emit`: one `events` row + `_bus.publish` to per-agent **and**
  `"*"` subscribers (the dashboard + `view` TUI watch `"*"`).
- `POST /api/events/emit` — daemon write endpoint.
- CLI:
  - `relaydeck events emit <type> [--data k=v] [-m msg] [--agent id]`
  - `relaydeck broadcast '<msg>' [--type T] [--data k=v]` (friendly wrapper)
  - `relaydeck events tail -f [--type substr] [--agent id]` (consumes the
    daemon `/api/events` SSE — the robust cross-process path)
- `--data k=v` values are JSON-coerced when possible (`n=3`→int), else string.

**Distinction:** broadcast/emit = an **ambient event** on the stream (nobody's
inbox touched). `workspace message` / `agent send` = **inbox delivery** into
an agent's session. Documented in the skill's `reference/commands.md`.

**Tests:** `tests/test_events_broadcast.py` (14) — orchestrator persist+publish,
endpoint happy/validation, CLI payload assembly, daemon-unreachable.

**Known follow-up:** `relaydeck agent events --follow` subscribes to the
*local-process* `_bus`; against a running daemon (separate process) it sees
nothing. `events tail` uses the daemon SSE (correct). Aligning `agent events
-f` onto the SSE path is a worthwhile follow-up (out of scope here).

## 2. `agent unblock` + input endpoint

**Why:** the semantic engine *detects* `awaiting-input` (read-only by the
"terminal untouchable" contract) but nothing *answered* it — an unattended
fleet stalls on "trust this folder? [y/N]", "press enter to continue", an
update notice.

**What:**
- `POST /api/agents/{id}/input` — the **headless twin** of the term
  WebSocket's stdin frame. Reuses the harness's sanctioned `send_input` /
  `send_message`; it **never touches the PTY read loop**. Body:
  `{"data": "...", "enter": bool}` or `{"key": "<name>"}` (enter, esc, ctrl-c,
  tab, arrows, y, n, space, backspace). 404 unknown agent / 409 not running.
- CLI `relaydeck agent unblock <id>`:
  - `--answer y` (text + Enter) · `--enter` · `--key esc`
  - **no flag = show the screen and send nothing** (so a dangerous default is
    never accepted by accident). `--show/--no-show` controls the screen peek.

**Tests:** `tests/test_agent_unblock.py` (13) — endpoint send paths, named
keys, 400/404/409, CLI body assembly + safe no-action default.

## 3. `autopilot` plugin (auto-answer + escalation, by composition)

**Why:** automate the *benign* unblocks so a fleet doesn't stall, without
guessing at risky prompts.

**What:** a builtin infrastructure plugin (`plugins/autopilot/`). On
`agent.status_changed → awaiting-input` it renders the agent's screen and
matches a **pure, tiered allowlist** (`match_unblock_rule`):
- `benign` (default): `press enter to continue` → Enter; trust-this-workspace
  → y.
- `all-known`: also declines in-session updates (→ n); and, gated by
  `auto_accept_terms` (default off), accepts terms/license.
- Anything unmatched → emits `autopilot.held` (visible), **never guessed**.

**Safety:** there is deliberately **no "accept the default [Y/n]" rule** — the
shared screen matcher is case-insensitive, so it can't tell `[Y/n]` from
`[y/N]`, and a wrong default could be destructive. It **composes with `hitl`**:
autopilot clears benign noise; hitl escalates the rest to a human.
Sends input via the in-process orchestrator (`host._orchestrator.get_running_instance`),
the same path hitl uses for `set_semantic_status`.

**CLI/API:** `relaydeck autopilot status|rules|test "<screen>"`,
`/api/plugins/autopilot/{status,rules}`. Settings: `mode`, `auto_accept_terms`,
`cooldown_seconds`, `max_attempts`.

**Tests:** `tests/test_autopilot.py` (14) — the pure matcher (every tier +
gating + safety), and the booted handler (auto-answer, mode-off, hold-unknown,
episode reset, cooldown). *A test caught a real bug: case-insensitive matching
made an early `[Y/n]` rule hijack update/terms prompts → fixed by removing the
rule (the run now correctly defers updates).*

## 4. `RELAYDECK_ORCHESTRATION_DEPTH` spawn marker

**Why:** `RELAYDECK_AGENT_ID` says "you're managed"; the skill also needs
"how deep" so a worker that *also* carries the orchestrate skill won't
bootstrap a runaway fleet-of-fleets.

**What:** `harness/base.py:_build_env` sets `RELAYDECK_ORCHESTRATION_DEPTH =
parent + 1` (default 1). In the usual single-daemon topology every agent is
depth 1; a relaydeck daemon run *inside* a managed agent makes its agents
depth 2+. **Tests:** `tests/test_orchestration_depth.py` (3).

## 5. Publishable `relaydeck-orchestrate` skill

**Why:** the headline. The existing `relaydeck-fleet`/`relaydeck-cli` skills
assume you're *already inside* relaydeck. Nothing taught an **external** agent
to install + drive relaydeck and orchestrate a fleet.

**What:** a **standalone, publishable** skill bundle at
`skills/relaydeck-orchestrate/` (drop into `~/.claude/skills/` or any
SKILL.md harness):
- `SKILL.md` — §0 inside-vs-outside detection (`RELAYDECK_AGENT_ID`); install
  + daemon-up; workspace + spawn; **observe** (the workers aren't "invisible"
  — relaydeck is the lens: `agent list/screen`, `events tail`, `inbox`,
  dashboard); coordinate (`send`/`broadcast`/`wait`); **unblock** edge cases;
  teardown; guardrails (never do a worker's task, cap concurrency, clean up).
- `scripts/relaydeck-bootstrap.sh` — idempotent detect→install→daemon-up;
  **refuses (exit 2) if run inside relaydeck**.
- `reference/commands.md`, `reference/edge-cases.md` (the detect→prevent→
  answer→escalate playbook), `reference/recipes.md` (fan-out, pipeline,
  supervisor, quorum, cross-repo).
- `README.md` — install + the market/viral plan + GTM checklist.

Validated by relaydeck's own parser: `validate_skill_dir → (True, [], [])`.

**Follow-up (noted in README):** package the bundle inside the `relaydeck`
wheel + a `relaydeck skill install-orchestrator` command that copies it into
`~/.claude/skills` — one-command arming for existing users. (Repo root
`skills/` is the canonical source; pyproject ships `relaydeck` + `plugins`,
not `skills/`, so the bundle is currently copy-to-install.)

## 6. `relaydeck view` command center

**Why:** the goal's command-center vision; the tabbar literally said "+ tabs
from plugins later: terminal · messages · events · tasks".

**What:** tabbed content in `#main` — **Terminal / Events / Messages / Tasks**
+ **plugin-contributed tabs** (`Ctrl+B 1-9`) + a **CLI console** line
(`Ctrl+B C`) that runs `relaydeck` subcommands and shows output in the Events
feed. Events tab is fed by the SSE stream the TUI already consumes (dynamic
content is markup-escaped so a payload with `[...]` can't corrupt RichLog).
Tasks tab = the agent roll-up.

**Plugins extend the TUI too (`[plugin.tui]`).** A plugin declares
`[plugin.tui] tabs = [{ id, title, endpoint }]` + a data endpoint; the daemon
aggregates them onto `/api/plugins/ui` (symmetric with `[plugin.ui]` for the
web). `view` discovers them and renders the endpoint's `{lines:[...]}` in one
shared `#plugin` pane — **no plugin widget code runs in the client** (thin
HTTP consumer, consistent with the CLI/daemon split). `autopilot` ships the
example tab (its mode, active rules, and recent auto-answer/hold actions via
`GET /api/plugins/autopilot/tui`). Manifest plumbing:
`plugin_manifest.TuiTab` → `cli.py` serve aggregate → `/api/plugins/ui` →
`view._fetch_plugin_tui_tabs`.

**Terminal-untouchable contract held:**
- Tabs **toggle visibility only** — `#pty` is never unmounted/remounted; its
  `_stream_pty`/`_render_screen`/WebSocket/`key_to_bytes` are unchanged.
- `on_resize` early-returns when the Terminal tab isn't active, so a hidden
  (size-0) terminal never gets a bogus geometry; switching back re-forwards
  the real size (the same path a window resize uses).
- New input modes (console) go through the existing Ctrl+B prefix, mirroring
  compose mode — no competing Input widget.

**Tests:** `tests/test_view_command_center.py` (5, headless Textual pilot) —
all panes mount, **switching tabs never unmounts the terminal** (same widget
object), resize suppressed off-terminal, event feed survives bracket payloads,
console runs a CLI command and shows the Events tab.

---

## Testing

Targeted verification across **every subsystem touched** is green
(~430 tests): events 14, unblock 13, autopilot 14, depth 3, view 5, plugin-load
(incl. autopilot) + CLI 115, harness/PTY/messaging/worktrees 217, plus hitl +
semantic. Run them with:

```sh
uv run pytest tests/test_events_broadcast.py tests/test_agent_unblock.py \
  tests/test_autopilot.py tests/test_orchestration_depth.py \
  tests/test_view_command_center.py -q
```

> **Full-suite note.** `uv run pytest -m "not e2e and not docker"` **passes**
> (pytest exit 0, ~2089 tests). One **pre-existing, unrelated** test is
> slow/occasionally-blocking: `tests/test_self_update_api.py::test_update_default_cmd_is_reinstall`
> blocks on a `concurrent.futures` `result()` in the self-update path
> (subprocess/network — `faulthandler` pinpointed it at `test_self_update_api.py:81`).
> A first run stalled there for >20 min (transient network/subprocess wait);
> a second run completed normally. Recommend installing `pytest-timeout` and
> giving that test a hard timeout + a mocked update channel. **None of the
> changes here are implicated** — plugin-load, CLI, harness, PTY, messaging,
> view, autopilot, events, unblock, and depth suites are all green.

## Conventions honored

- **No `CHANGELOG.md` hand-edits** — it's automated (`scripts/bump_version.py`
  / `version-bump` workflow). This handover adds that as a hard rule in
  `AGENTS.md` + a callout in `docs/RELEASE.md`.
- **Commit identity** is repo-local `relaydeck <dev@relaydeck.ai>`; no Claude
  attribution in commits. The work is committed as logical per-feature commits
  on `feat/orchestration-command-center`.

## Where to pick up next

1. Give `test_self_update_api.py::test_update_default_cmd_is_reinstall` a hard
   timeout + a mocked update channel (and install `pytest-timeout`) — it can
   block for minutes on a real `future.result()`.
2. Package the orchestrate skill in the wheel + `relaydeck skill install-orchestrator`.
3. Align `relaydeck agent events --follow` onto the daemon SSE path.
4. Wire the autopilot `held`/`unblocked` events into the dashboard event feed
   prominently (they already ride the plugin bus → SSE bridge like hitl).
5. Consider an `autopilot.held` → one-tap `prompts`/`hitl` escalation shortcut.
