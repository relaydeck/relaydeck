# relaydeck-orchestrate — a publishable agent skill

A self-contained **agent skill** that teaches *any* capable CLI agent
(Claude Code, and any harness that reads `SKILL.md`) to install and drive
[relaydeck](https://relaydeck.ai) — turning a single agent into the
**conductor of a fleet**. Drop it into a skill directory and your agent can
spin up, supervise, coordinate, unblock, and tear down a team of background
coding agents.

This bundle is intentionally portable and stands alone — it is **not** a
relaydeck-internal skill (those, like `relaydeck-fleet`, are injected into
agents that already run *inside* relaydeck). This one is for the agent
*outside*, looking in.

## What's in here

```
relaydeck-orchestrate/
├── SKILL.md                     # the skill itself (frontmatter + body)
├── README.md                    # this file (install + publish + positioning)
├── scripts/
│   └── relaydeck-bootstrap.sh   # idempotent detect → install → daemon-up
└── reference/
    ├── commands.md              # orchestration command cheat-sheet
    ├── edge-cases.md            # detect/prevent/answer/escalate for stuck prompts
    └── recipes.md               # fan-out, pipeline, supervisor, quorum, cross-repo
```

## Install (for an end user)

**Claude Code** — with relaydeck installed, use the built-in installer:

```sh
relaydeck skills install-orchestrator
```

Or copy the folder into a skills directory manually:

```sh
# Personal (all projects):
cp -r relaydeck-orchestrate ~/.claude/skills/

# Or project-scoped:
cp -r relaydeck-orchestrate .claude/skills/
```

The agent discovers it by `name` + `description`; when a task smells like
"run several agents", it reads `SKILL.md` and follows it — installing
relaydeck on first use via `scripts/relaydeck-bootstrap.sh`.

**Any other harness** that supports SKILL.md-style skills: place the folder
in that tool's skills path. The skill only depends on a shell and `relaydeck`
(which it installs itself).

> `SKILL.md` references `$CLAUDE_SKILL_DIR` for the script path. If your
> harness exposes the skill's directory under a different variable,
> substitute it (or just run the absolute path to `scripts/relaydeck-bootstrap.sh`).

## Self-awareness built in (inside vs outside)

The skill's first step checks `RELAYDECK_AGENT_ID`:

- **unset** → external orchestrator: install + bootstrap + drive a fleet.
- **set** → already a managed agent: do *not* bootstrap a second fleet;
  coordinate as a peer. `RELAYDECK_ORCHESTRATION_DEPTH` (injected by
  relaydeck on every spawn) lets a nested agent see how deep it is and
  refuse to start a runaway fleet-of-fleets.

The bootstrap script enforces the same guard and exits early (code 2) if run
from inside relaydeck. So the skill is safe to publish broadly: it behaves
correctly whether it lands in a plain terminal or inside a relaydeck worker.

## Why this is the orchestration wedge

The CLI-agent world is exploding, but everyone hits the same wall the moment
one agent isn't enough: **how do I run, see, and coordinate many?** Today
people hand-roll tmux panes, `&` background jobs, and bespoke scripts —
fragile, invisible, and impossible to supervise. relaydeck already solved
the hard parts (per-agent PTYs, cross-harness status, a message bus, a live
dashboard, prompt auto-answering). This skill is the **zero-to-fleet on-ramp**
that makes all of it reachable from inside the agent the user already trusts.

The flywheel:

1. **Distribution rides the host.** The skill installs into the agent the
   user *already* runs (Claude Code, etc.). No new app to adopt — the
   orchestrator shows up where the user already is.
2. **One folder, instant capability.** Copy a directory → the agent can now
   command a fleet. The "wow" is one prompt away ("spin up three reviewers
   in parallel").
3. **Harness-agnostic by design.** It orchestrates *any* harness
   (Claude Code, Codex, Cursor, opencode, Gemini, pi). It doesn't compete
   with the agent that loaded it — it multiplies it. That neutrality is why
   it spreads instead of threatening incumbents.
4. **The dashboard is the share moment.** The first time a human watches
   three agents work in parallel on one screen, they screenshot it. That's
   the viral artifact.
5. **Safe-by-default earns trust.** Inside/outside guards, look-before-you-
   unblock, conservative auto-answer, escalate-the-rest. Orchestration that
   doesn't surprise you is orchestration people leave on.

### Go-to-market checklist (for the maintainers)

- [ ] Publish `relaydeck` to PyPI so `uv tool install relaydeck` works
      cold (the bootstrap script's happy path).
- [ ] List this skill in the public skill marketplaces / registries the
      target harnesses index, with the demo GIF (three agents, one
      dashboard) as the hero.
- [ ] Ship a 60-second "zero to fleet" screencast: copy folder → "review
      this repo with three agents" → dashboard lights up.
- [x] Ship the bundle *inside* the `relaydeck` package plus a
      `relaydeck skills install-orchestrator` command that copies it into
      `~/.claude/skills` or `~/.codex/skills` — so an existing relaydeck user
      can arm their local agent with one command.
- [ ] Keep `SKILL.md` lean and the `reference/` deep — agents load the
      former always, the latter on demand.

## License

MIT, same as relaydeck.
