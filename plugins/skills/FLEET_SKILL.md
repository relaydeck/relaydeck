---
name: relaydeck-fleet
description: You're a relaydeck-managed agent — the `relaydeck` CLI is the API for everything fleet-related. Read this whenever a task involves another agent or workspace — listing peers, finding one by purpose/tag, spawning/creating/starting/stopping/removing an agent, registering a workspace, or sending a fresh message to a peer. Skip filesystem exploration; the CLI already exposes all of it.
metadata:
  short-description: Orient + admin commands for a relaydeck-managed agent
---

# You're inside relaydeck

The `relaydeck` CLI is the API. Don't grep YAML in `~/.relaydeck/`,
don't read `agent.toml`, don't reverse-engineer the registry — every
fleet operation has a subcommand. `relaydeck --help` lists them;
`relaydeck <cmd> --help` shows flags.

Your stable identity is in env vars — no `whoami` shell trips needed:

- `$RELAYDECK_AGENT_ID` — your agent id (matches `~/.relaydeck/agents/<id>.yaml`)
- `$RELAYDECK_WORKSPACE` — your workspace name
- `$RELAYDECK_WORKSPACE_PATH` — the workspace's filesystem path

## Look around

```sh
relaydeck status                              # one-line: who you are, peers, unread inbox
relaydeck agent list                          # peers in your workspace + status + purpose
relaydeck agent find --tag <tag>              # discover by tag
relaydeck agent find --purpose <substring>    # discover by purpose (case-insensitive regex)
relaydeck workspace list                      # every registered workspace + health
relaydeck workspace info [name]               # workspace detail (plugins, agents)
```

## Spawn / kill peers

```sh
# Create THEN start — `--auto-start` only flips persistent auto_start (next boot);
# to bring a new agent up *now*, run `start` after `create`.
relaydeck agent create <id> --type <type> --purpose "<one-line role>" [--auto-start]
relaydeck agent start <id> [<id>…]
relaydeck agent stop <id>
relaydeck agent rm <id>                       # prompts; deletes the spec
relaydeck workspace add <path> --name <name> [--plugin messaging --plugin skills]
```

Harness types: `pi`, `claude-code`, `codex-cli`, `cursor-cli`, `opencode-cli`,
`relaydeck` (native).

## Talk to peers

```sh
# Reply to an inbox line that starts `[relay from=X id=msg_YYY]`:
relaydeck reply <msg-id> '<your reply>'

# Open a *new* thread with a peer you picked from `agent list`:
relaydeck agent send <peer-id> '<body>'
```

**Quote message bodies with single quotes** — the body is a shell arg, so
backticks / `$(…)` / `$VAR` inside double quotes get expanded and the peer
receives a mangled message. Use single quotes (or `$'…'` for literal `\n`)
unless you specifically want interpolation.

Full reply contract + threading + "don't poll your inbox" → **relaydeck-cli** skill.
