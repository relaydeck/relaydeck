---
name: relaydeck-orchestrate
description: >
  Spin up and run a TEAM of CLI coding agents in parallel — orchestrate,
  delegate to, supervise, and coordinate multiple background agents
  (Claude Code, Codex, Cursor, opencode, Gemini, pi) from one place. Use
  this whenever a task is bigger than one agent: "run N agents in
  parallel", "spin up a fleet/team of agents", "delegate subtasks to
  sub-agents", "manage background coding agents", "have agents review each
  other", or "orchestrate a swarm". It installs and drives `relaydeck` —
  the open-source orchestrator — and finds an existing install if there is
  one. Read it BEFORE hand-rolling tmux panes or backgrounded `&` processes.
metadata:
  short-description: Install + drive relaydeck to orchestrate a fleet of CLI agents
---

# Orchestrate a fleet of agents with relaydeck

You are one agent. Some jobs want many — a reviewer, an implementer, and a
tester working at once; a fan-out across ten services; a long migration you
supervise rather than type. `relaydeck` is the orchestrator that makes that
a first-class, observable thing instead of a pile of backgrounded shells:
it spawns each agent in its own PTY, tracks what every one is *actually
doing*, routes messages between them, and gives you (and a human) a live
dashboard. Your job with this skill is to **drive relaydeck from the
outside**: install it if needed, start it, spawn workers, watch them,
coordinate them, unblock them, and tear them down.

> One mental model: **you are the conductor, relaydeck is the podium, the
> workers are the orchestra.** You never play an instrument (never do a
> worker's task yourself); you cue, listen, and keep time.

## 0. First: where am I? (inside vs outside)

Before anything, check whether you are *already* a relaydeck-managed agent.
This decides everything.

```sh
echo "agent=${RELAYDECK_AGENT_ID:-<none>} depth=${RELAYDECK_ORCHESTRATION_DEPTH:-0}"
```

- **`RELAYDECK_AGENT_ID` is empty** → you are an **external orchestrator**.
  relaydeck (if installed) is a tool you call. Continue to §1.
- **`RELAYDECK_AGENT_ID` is set** → you are **already inside relaydeck** — a
  managed worker, not the conductor. **Do NOT** install relaydeck, start a
  daemon, or bootstrap a second fleet. Two `relaydeck` skills are already
  available to you (`relaydeck-fleet` for admin, `relaydeck-cli` for
  messaging) — use those. You *may* spawn peers when a task genuinely needs
  them (`relaydeck agent create … && relaydeck agent start …`), but they are
  **siblings in the same daemon** — visible to the same operator, not hidden
  helpers of yours. If `RELAYDECK_ORCHESTRATION_DEPTH ≥ 2` you are deeply
  nested: do not spawn more agents unless explicitly told to; coordinate
  instead. **Never spawn a copy of yourself that would re-run this
  bootstrap** — that is how a fleet-of-fleets runs away.

Everything below is the **external orchestrator** path.

## 1. Find or install relaydeck

```sh
# The bootstrap script (shipped with this skill) is idempotent: it detects
# an existing install, installs relaydeck if missing, and starts the daemon.
bash "$CLAUDE_SKILL_DIR/scripts/relaydeck-bootstrap.sh"   # path of THIS skill dir
```

Prefer the script. If you'd rather do it by hand:

```sh
command -v relaydeck && relaydeck --version    # already installed?
# If not, in order of preference:
uv tool install relaydeck            # isolated, recommended
pipx install relaydeck               # also isolated
pip install --user relaydeck         # fallback
```

`relaydeck` and `rdk` are the same CLI. Everything is a subcommand;
`relaydeck --help` and `relaydeck <cmd> --help` are authoritative.

## 2. Start the daemon (once)

The daemon owns the agents, the message bus, and the web dashboard. It
keeps running after you exit.

```sh
relaydeck daemon status || relaydeck daemon start
relaydeck status               # prints who you are, peers, and the dashboard URL
```

Open the dashboard URL in a browser — that's the human's live window into
everything you're about to do.

## 3. A workspace, then workers

A **workspace** is a directory the agents work in (usually a repo). Register
it, then create + start agents in it.

```sh
relaydeck workspace add . --name proj --plugin messaging --plugin skills

# Create THEN start — create writes the spec; start brings it up NOW.
relaydeck agent create reviewer  --type claude-code --workspace proj \
    --purpose "Reviews diffs for correctness and security"
relaydeck agent create implementer --type codex-cli --workspace proj \
    --purpose "Implements the agreed change"
relaydeck agent start reviewer implementer
```

Harness types: `claude-code`, `codex-cli`, `cursor-cli`, `opencode-cli`,
`gemini`, `pi`, `relaydeck` (native). Pick per worker.

**Unattended posture.** A spawned agent has no human at its keyboard, so a
harness approval prompt would hang it. Spawn with autonomy so safe work
runs without prompting (see §6 + `reference/edge-cases.md`):

```sh
relaydeck agent create builder --type codex-cli --workspace proj \
    --purpose "build + test" --autonomy auto      # auto (default) | bypass | locked
```

## 4. See what they're doing (they're not "invisible" — relaydeck is the lens)

Workers run **inside the daemon**, not as children of your shell. `ps` won't
show them as yours and you can't see their terminals directly — that's
expected. relaydeck IS the visibility layer. Use it instead of hunting for
processes:

```sh
relaydeck agent list                 # every worker + live status
relaydeck agent screen reviewer      # render any worker's screen right now
relaydeck events tail -f             # the whole fleet's live event firehose
relaydeck workspace inbox -f         # messages passing between agents, live
```

Status words you'll see (the cross-harness semantic status):
`working` · `awaiting-input` (blocked on a prompt — see §6) ·
`complete-unread` (finished, result waiting) · `idle`.

## 5. Coordinate

```sh
# Assign / talk to one worker (pushed straight into its session):
relaydeck agent send implementer 'Apply the change reviewer approved in #2.'

# Broadcast a task or status to the whole workspace fleet (into inboxes):
relaydeck workspace message 'Freeze: prep for release cut.' 

# Announce an AMBIENT event on the stream (dashboard + events tail see it),
# WITHOUT pushing into anyone's inbox — good for orchestration milestones:
relaydeck broadcast 'phase 1 complete, starting verification' \
    --data phase=1 --data ok=true

# Block until a worker actually finishes — the synchronization primitive:
relaydeck agent wait reviewer --status complete-unread --timeout 600
```

Quote message bodies with **single quotes** — they're shell args; backticks
and `$(…)` in double quotes get expanded before relaydeck sees them.

## 6. Unblock the things that break orchestration

The #1 way an unattended fleet stalls: a **native prompt** with no human to
answer it — "Do you trust the files in this folder? [y/N]", "accept the
terms", "press enter to continue", "update available". relaydeck gives you
three layers (full playbook in `reference/edge-cases.md`):

1. **Prevent** — spawn with `--autonomy auto` (default) or `bypass` so the
   harness runs safe work without prompting.
2. **Auto-answer** — enable the `autopilot` plugin; it auto-clears the
   *benign* prompts (trust-folder, press-enter, declines mid-run updates)
   and HOLDS anything it doesn't recognize for a human. Check it:
   ```sh
   relaydeck plugin set autopilot mode benign     # off | benign | all-known
   relaydeck autopilot rules                       # what it will auto-answer
   ```
3. **Bypass by hand / from your orchestration loop** — when `agent list`
   shows `awaiting-input`, look, then answer:
   ```sh
   relaydeck agent screen builder          # SEE what it's stuck on first
   relaydeck agent unblock builder --answer y     # type y + Enter
   relaydeck agent unblock builder --enter        # just press Enter
   relaydeck agent unblock builder --key esc      # dismiss
   ```
   `unblock` with no flag only *shows* the screen — so a dangerous default
   is never accepted by accident. Answer explicitly.

Watch for `autopilot.held` on `relaydeck events tail` — that's autopilot
telling you a worker needs a human decision it won't make for you.

## 7. Tear down

```sh
relaydeck agent stop reviewer implementer
relaydeck agent rm reviewer implementer        # delete the specs
relaydeck workspace rm proj                     # unregister (leaves the dir)
```

Always clean up agents you spawned for a one-off job. Leaving a fleet
running is the orchestration equivalent of a leaked process.

## The dream run (end to end)

"Review this repo with three specialists in parallel, then summarize."

```sh
bash "$CLAUDE_SKILL_DIR/scripts/relaydeck-bootstrap.sh"
relaydeck workspace add . --name audit --plugin messaging --plugin skills
for role in security:"security review" perf:"performance review" \
            tests:"test-coverage review"; do
  id=${role%%:*}; purpose=${role#*:}
  relaydeck agent create "$id" --type claude-code --workspace audit \
      --purpose "$purpose" --autonomy auto
done
relaydeck agent start security perf tests
relaydeck workspace message 'Audit the repo for your specialty. Reply with findings.' 
# Supervise: unblock anything that stalls, wait for all to finish.
for id in security perf tests; do
  relaydeck agent wait "$id" --status complete-unread --timeout 900 \
    || relaydeck agent screen "$id"     # if it didn't finish, look at why
done
relaydeck workspace inbox --full         # collect the findings
relaydeck agent rm security perf tests   # clean up
```

## Guardrails (read these)

- **Never do a worker's task yourself.** You orchestrate; they execute. If
  you find yourself writing the code, you've stopped conducting.
- **Cap concurrency.** Spawn what the job needs, not a swarm. Each agent is
  a real process burning tokens.
- **Workers are visible, not secret.** Anything you spawn shows up in
  `agent list` and on the dashboard to the human running relaydeck. Don't
  design around hiding them — design around the human being able to watch.
- **Respect the inside/outside split (§0).** If you're already a managed
  agent, don't bootstrap a fleet; coordinate as a peer.
- **Clean up.** Stop and remove one-off agents when done.

Deeper material lives next to this file: `reference/commands.md` (full
command cheat-sheet), `reference/edge-cases.md` (the unblock/autonomy
playbook), `reference/recipes.md` (orchestration patterns).
