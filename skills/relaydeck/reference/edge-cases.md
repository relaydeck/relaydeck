# Edge cases: when human-in-the-loop prompts break orchestration

The single biggest threat to an unattended fleet is a **native prompt with
no human to answer it**. A worker hits "Do you trust the files in this
folder? [y/N]" or "press enter to continue" and silently sits there forever
while you think it's working. relaydeck gives you a detect → prevent →
answer → escalate stack. Use it deliberately.

## The lifecycle of a stuck agent

1. The harness prints a prompt and blocks on stdin.
2. relaydeck's **semantic-status engine** reads the rendered screen ~1×/s
   and flips the agent to `awaiting-input` (visible in `agent list`, on the
   dashboard, and as an `agent.status_changed` event). *Detection is
   automatic and works for every harness — it never types for you.*
3. From there, one of three things answers it: autonomy flags (set at
   spawn), the `autopilot` plugin (automatic), or you (`agent unblock`).

## Layer 1 — Prevent at spawn (autonomy)

Set the agent's autonomy so the harness runs safe work without prompting:

```sh
relaydeck agent create w --type codex-cli --workspace proj --autonomy auto
```

- `auto` (default) — run safe work autonomously, still guard genuinely
  dangerous ops. Best default for unattended runs.
- `bypass` — skip all approval/sandbox checks. Fastest, no guardrails; use
  only in a sandbox/throwaway workspace you trust.
- `locked` — fail-safe: only an allowlist runs, everything else is denied.
- `manual` — inject nothing; you set the harness's own flags via `--args`.

Autonomy covers the harness's *own* approval prompts (tool/permission/
sandbox). It does **not** cover first-run OS-ish prompts like trust-this-
folder, license acceptance, or "update available" — those are Layers 2/3.

## Layer 2 — Auto-answer the benign ones (autopilot)

The `autopilot` plugin watches for `awaiting-input` and auto-answers a
**small, curated, conservative** set of prompts, then HOLDS anything it
doesn't recognize for a human (it never guesses).

```sh
relaydeck plugin set autopilot mode benign        # default
relaydeck autopilot rules                          # exactly what it will answer
relaydeck autopilot test "Do you trust the files in this folder? [y/N]"
```

Modes:
- `off` — detection only; you/HITL handle everything.
- `benign` (default) — only always-safe prompts: "press enter to continue"
  (→ Enter) and trust-this-workspace (→ y, because you already chose to run
  an agent here).
- `all-known` — also declines in-session update prompts (never swap the
  binary mid-run) and, **only if** `auto_accept_terms=true`, accepts
  terms/license prompts.

Autopilot deliberately has **no "just press the default" rule** — the
screen matcher is case-insensitive, so it can't tell `[Y/n]` (default yes)
from `[y/N]` (default no), and blindly accepting an unknown default could be
destructive. Unrecognized prompts are emitted as `autopilot.held` and left
for a human.

Watch holds live:
```sh
relaydeck events tail -f --type autopilot     # unblocked + held, as they happen
```

## Layer 3 — Answer by hand / from your loop (agent unblock)

When `agent list` shows `awaiting-input` and autopilot held it (or is off),
you decide. **Always look first**, then answer:

```sh
relaydeck agent screen w                 # see exactly what it's asking
relaydeck agent unblock w --answer y      # type "y" + Enter
relaydeck agent unblock w --enter         # just Enter (e.g. "press enter")
relaydeck agent unblock w --key esc       # dismiss / cancel
relaydeck agent unblock w                 # NO action — only prints the screen
```

The no-flag form is the safe default: it shows the prompt and sends nothing,
so you never accept a dangerous default by reflex. This is the manual
"orchestration bypasses the prompt" path — fully scriptable inside your own
supervise loop:

```sh
# In a supervisor loop: clear benign stalls, surface the rest.
while :; do
  for a in $(relaydeck agent list -A --quiet); do
    if relaydeck agent list -A | grep -q "$a.*awaiting-input"; then
      relaydeck agent screen "$a"        # decide based on what you see
      # … apply unblock OR escalate to the human …
    fi
  done
  sleep 5
done
```

## Layer 4 — Escalate to a human (hitl)

For prompts that genuinely need a person (a real approve/deny, a destructive
op), the `hitl` plugin escalates `awaiting-input` to the operator through
whatever channels are configured (the dashboard bell always; Telegram/etc.
if wired) and can optionally stop the agent until answered. Autopilot and
hitl compose: autopilot clears the benign noise, hitl raises the rest. An
agent can also explicitly ask: `relaydeck hitl ask "<question>"`, or raise a
button prompt with `relaydeck prompts ask … --wait`.

## Decision table

| Prompt | Who answers | Result |
| --- | --- | --- |
| Harness tool/permission/sandbox approval | autonomy (`auto`/`bypass`) | runs without prompting |
| "press enter to continue" | autopilot `benign` | Enter |
| "trust the files in this folder?" | autopilot `benign` | y |
| "update available?" | autopilot `all-known` | declined (n) |
| "accept the terms?" | autopilot `all-known` + `auto_accept_terms` | y |
| Arbitrary `[Y/n]` default | **held** | you / hitl decide |
| "Delete production? (type DELETE)" | **held → hitl** | a human, always |

## Golden rules

- **Detection is free and automatic; answering is a deliberate choice.**
- **Look before you unblock.** `agent screen` first, every time.
- **Never auto-accept what you haven't vetted** — keep `auto_accept_terms`
  off unless you know what your harnesses prompt for.
- **`bypass` only in throwaway/sandboxed workspaces.**
