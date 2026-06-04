# relaydeck CLI usage guide

relaydeck is **CLI-first**: the shell is the platform, and the two UIs (the
`relaydeck view` TUI and the web `dashboard` plugin) are co-equal peers over the
same daemon API. This guide is an example-driven tour — workspaces, skills,
plugins, and driving a fleet — entirely from the CLI. `rdk` is an alias for
`relaydeck`; `relaydeck <cmd> --help` is always authoritative.

> Dev note: use `uv run relaydeck …` against a source checkout — a bare
> `relaydeck` may be a frozen tool copy that ignores repo edits.

---

## 1. From a directory to a live command center

One gesture finds-or-registers the workspace that owns a directory, ensures the
daemon, and opens the command center:

```sh
relaydeck open .              # register if needed → daemon up → TUI
relaydeck open . --web        # …open the web dashboard instead
relaydeck open . --no-view    # …just ensure workspace + daemon (scripts/CI)
relaydeck open ~/code/api     # a specific repo
```

Under the hood that's three first-class commands you can also run yourself:

```sh
relaydeck init . --plugin messaging --plugin skills   # register a workspace
relaydeck daemon start                                # background the daemon
relaydeck view                                        # open the TUI
```

---

## 2. Workspaces

A **workspace** is a registered directory (usually a repo) that agents work in.
Resolution is git-style: a `cd` into a registered path picks it; the deepest
match wins.

```sh
relaydeck workspace add . --name api --plugin messaging --plugin skills
relaydeck workspace list                 # all workspaces + health
relaydeck workspace info api             # plugins + agents in one workspace
relaydeck workspace plugins api --add github     # toggle plugins per workspace
relaydeck workspace set api              # durable default for this shell
relaydeck workspace rm api               # unregister (leaves the directory)
```

Per-workspace plugins live in `~/.relaydeck/workspaces/<name>/agent.toml` — that's
the source of truth the harness reads at spawn (see the skill-injection note in §3).

Git worktrees are first-class workspaces (a branch per task):

```sh
relaydeck worktree create api --branch feat/x --name api-feat-x
relaydeck worktree list
```

---

## 3. Skills — using and injecting

Skills are SKILL.md bundles agents can load. relaydeck both **manages** them and
**injects** them into the agents you spawn.

```sh
relaydeck skills list                    # discovered skills across workspaces + codex
relaydeck skills show <name>             # one skill's metadata + which agents see it
relaydeck skills doctor                  # health across all sources
```

**Arm your own agent with the `relaydeck` skill** (teaches any SKILL.md harness
to install + drive relaydeck — the publishable, npm-able skill):

```sh
relaydeck skills install                 # → ~/.claude/skills/relaydeck
relaydeck skills install --target both   # Claude + Codex skill roots
```

**Import external skills** into a workspace (so the agents there get them):

```sh
relaydeck skills add <source>            # discover + import from any supported source
relaydeck skills import-git <git-url>    # clone a Git/GitHub skill + link it
relaydeck skills link ./my-skill --workspace api   # symlink/copy a local skill
relaydeck skills unlink <name> --workspace api
```

**How injection works (the contract).** When a workspace enables `skills` /
`messaging`, each agent spawned there gets the right skills materialized into its
own skill dir, per harness:

- **Runtime (plugin-contributed) skills** — e.g. messaging's reply contract —
  are injected **always** (otherwise messaging wouldn't work).
- **User skills** are **gated** by the agent's `skills` list.
- Coverage is pinned by `tests/test_harness_skill_injection_matrix.py`, so the
  behaviour is identical across Claude Code, Codex, Cursor, opencode, pi.

You don't call an "inject" command — registering a workspace with `skills`/
`messaging` and creating agents in it is the gesture; relaydeck does the rest.

---

## 4. Agents — create, run, manage

```sh
# create writes the spec; start brings it up NOW.
relaydeck agent create reviewer --type claude-code --workspace api \
    --purpose "Review diffs for correctness + security" --autonomy auto
relaydeck agent create builder  --type codex-cli   --workspace api \
    --purpose "Implement the approved change"
relaydeck agent start reviewer builder

relaydeck agent list                     # every agent + live semantic status
relaydeck agent screen reviewer          # render any agent's screen right now
relaydeck agent stop|restart|rm reviewer
```

Harness `--type`: `claude-code`, `codex-cli`, `cursor-cli`, `opencode-cli`,
`gemini`, `pi`, `relaydeck` (native). `--autonomy`: `auto` (default) | `bypass`
| `locked` — a spawned agent has no human at its keyboard, so autonomy lets safe
work run without prompting.

**Durable results + crash recovery** (results survive an agent crash):

```sh
relaydeck agent wait reviewer --status complete-unread --timeout 600
relaydeck agent result get reviewer      # the durable hand-back
relaydeck agent transcript reviewer      # last screen of an exited agent
                                         #   (opt-in: RELAYDECK_TRANSCRIPT_BYTES>0)
```

**Keep agents healthy:**

```sh
relaydeck context status                 # each agent's context-window fill
relaydeck agent compact reviewer         # KV-safe in-place compaction when filling
relaydeck agent unblock builder --answer y   # answer a native prompt (or --enter/--key)
relaydeck agent escalate builder -m "needs a human call"   # ping your channels (HITL)
```

---

## 5. Coordinate the fleet

```sh
relaydeck agent send builder 'Apply the change reviewer approved.'   # into one session
relaydeck workspace message 'Freeze for the release cut.'            # into every inbox
relaydeck broadcast 'phase 1 done' --data phase=1 --data ok=true     # ambient event, no inbox
relaydeck agent wait reviewer --status complete-unread --timeout 600 # the sync primitive
```

Quote message bodies with **single quotes** — they're shell args.

---

## 6. Observe — the command center

The `relaydeck view` TUI is one terminal, no browser: a workspaces sidebar, the
focused agent's live PTY, and tabbed panes — **Terminal / Events / Messages /
Tasks**, plus plugin-contributed tabs and a CLI console (`Ctrl+B C`).

```
┌ relaydeck ───────────────────────────── ws: api · agent: reviewer ─┐
│ workspaces        │ 1 Terminal  2 Events  3 Messages  4 Tasks  ·C  │
│ ▸ api    3 agents │ reviewer  $ pytest -q ......... [ working ]    │
│   web    1 agent  │ 41 passed in 2.10s                             │
│ agents            │ › builder is applying the diff reviewer filed  │
│  ● reviewer  work │ ────────────────────────────────────────────  │
│  ▲ builder   82%  │ events ▸ agent.context builder 82% (warn)      │
│  ⏸ tester    idle │         ▸ manager.action builder → compact     │
│ ^B 1-4 tabs · ^B C console · ^B M message · ^B D detach            │
└────────────────────────────────────────────────────────────────────┘
```

From plain CLI, the same firehose:

```sh
relaydeck events tail -f                 # the whole fleet's live event stream
relaydeck workspace inbox -f             # messages between agents, live
relaydeck agent events reviewer -f       # one agent's event log
relaydeck manager status                 # fleet-health policy + recent actions
relaydeck usage-limits status            # rolling session/weekly quota state
relaydeck autopilot status               # native-prompt auto-answer posture
```

The web dashboard (`open http://127.0.0.1:8765`) shows the same data — it's the
platform-level **`dashboard` plugin**, a peer of the CLI/TUI, not a privileged
layer. Disable it like any plugin and the CLI + TUI lose nothing.

---

## 7. Create a plugin

Everything beyond the core runtime is a plugin (harnesses, providers, messaging,
skills, the web UI itself). Scaffold, develop, and verify entirely from the CLI:

```sh
relaydeck plugin list                    # installed plugins
relaydeck plugin new my-tool             # publishable package relaydeck-plugin-my-tool/
relaydeck plugin new my-tool --pattern ui          # web UI contribution
relaydeck plugin new my-tool --pattern harness     # wrap a new CLI agent
relaydeck plugin new my-tool --local     # PRIVATE plain-dir plugin in ~/.relaydeck/plugins/
relaydeck plugin new my-tool --workspace api       # …scoped to one workspace

relaydeck plugin dev ./relaydeck-plugin-my-tool    # editable install
relaydeck plugin verify ./relaydeck-plugin-my-tool # manifest + skill validation
relaydeck plugin lint ./relaydeck-plugin-my-tool
relaydeck plugin test ./relaydeck-plugin-my-tool
```

`--pattern` is one of `reactor | workflow | harness | provider | ui | cli |
skill`. A plugin declares its capabilities in `plugin.toml` (CLI commands, HTTP
routes, web tabs/tiles via `[plugin.ui]`, **TUI tabs via `[plugin.tui]`**,
typed settings, event subscriptions); those gate SDK access at runtime. See
[CONTRIBUTING.md](../CONTRIBUTING.md) for the authoring guide and
[AGENTS.md](../AGENTS.md) for the architecture.

A plugin can contribute a tab to **both** UIs symmetrically: `[plugin.ui]` for
the web dashboard, `[plugin.tui]` for `relaydeck view` (the `view` client GETs
the tab's endpoint for `{lines:[...]}` content). The bundled `autopilot`,
`context-watch`, and `manager` plugins each ship one.

---

## 8. Tear down

```sh
relaydeck agent stop reviewer builder
relaydeck agent rm reviewer builder       # delete the specs
relaydeck workspace rm api                # unregister (leaves the directory)
```

Always clean up agents spawned for a one-off job — a leaked fleet is the
orchestration equivalent of a leaked process.
