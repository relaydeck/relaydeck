# relaydeck skills

This is the catalog of every first-party **agent skill** relaydeck ships — the
single, visible place to see what each one teaches an agent and how it reaches
one. A *skill* is a `SKILL.md` bundle (YAML frontmatter + Markdown body, plus
optional `scripts/` and `reference/`) that an agent reads to learn a
capability.

Two kinds, by **how they reach an agent**:

- The standalone **`relaydeck` driver skill** lives here under
  `skills/relaydeck/`. It's packaged with relaydeck and installed into an
  agent's own skill root (e.g. `~/.claude/skills`) by
  `relaydeck skills install`. It is for an agent *outside* a fleet, looking in.
- **Plugin-materialized skills** ship *with the plugin that owns them* (in
  `plugins/<plugin>/SKILL.md`) and are injected into a workspace's
  `runtime/skills/` only when that plugin is enabled — so a skill and its
  backing capability always travel together. They're listed here for
  discoverability; the file physically lives with its plugin.

> Design note: plugin-owned skills stay co-located with their plugin on
> purpose (self-contained, independently publishable). This folder is the
> catalog + home of the standalone driver skill; it is **not** a dumping
> ground that breaks plugin packaging. The generic in-fleet skills
> (`relaydeck-fleet`, `relaydeck-plugin-dev`) live with the `skills` plugin
> that materializes them.

## The catalog

| Skill | Teaches | Lives in | Reaches an agent via |
| --- | --- | --- | --- |
| **relaydeck** | Install + drive a fleet from *outside* (the operator playbook) | `skills/relaydeck/` | `relaydeck skills install` → `~/.claude` / `~/.codex` skill root; or copy the folder |
| **relaydeck-fleet** | Orient + admin commands for an agent *inside* a fleet (look around, find peers, spawn/stop) | `plugins/skills/relaydeck-fleet/` | `skills` plugin → workspace `runtime/skills/` |
| **relaydeck-plugin-dev** | Author, test, and ship a relaydeck plugin (private / community / core) | `plugins/skills/relaydeck-plugin-dev/` | `skills` plugin (toggle: `inject_plugin_authoring_skill`) |
| **relaydeck-cli** | Peer messaging contract — reply to `[relay from=…]`, hand off, report status | `plugins/messaging/SKILL.md` | `messaging` plugin |
| **relaydeck-telegram** | Receive + reply through the Telegram gateway | `plugins/telegram/SKILL.md` | `telegram` plugin |
| **relaydeck-prompts** | Ask a human a tap-able Approve/Reject question | `plugins/prompts/SKILL.md` | `prompts` plugin |
| **relaydeck-dashboard** | Reshape the live web dashboard (widgets, theme, density) | `plugins/dashboard/SKILL.md` | `dashboard` plugin |
| **relaydeck-theme** | Author / recolor dashboard themes | `plugins/theme/SKILL.md` | `theme` plugin |

## How they fit together

```
                 ┌─────────────────────────────────────────────┐
   outside  ───▶ │  relaydeck  (install + drive a fleet)        │
   the fleet     └───────────────┬─────────────────────────────┘
                                 │ spawns agents that get…
                                 ▼
   inside    ┌──────────────────────────────────────────────────────┐
   the fleet │ relaydeck-fleet  — orient / admin / spawn peers       │
             │ relaydeck-cli    — reply to peers (the messaging core) │
             │ relaydeck-telegram — reply to a human over Telegram    │
             │ relaydeck-prompts  — ask a human with buttons          │
             └──────────────────────────────────────────────────────┘

   operator  ┌──────────────────────────────────────────────────────┐
   surfaces  │ relaydeck-dashboard — restyle the dashboard           │
             │ relaydeck-theme     — author themes                   │
             │ relaydeck-plugin-dev — extend relaydeck itself        │
             └──────────────────────────────────────────────────────┘
```

The `relaydeck` skill's own `reference/` goes deeper on the operator side:
`commands.md` (verified cheat-sheet), `permissions.md`, `monitoring.md`,
`extending.md` (skills + plugins), `recipes.md`.

## Inspecting + managing skills at runtime

```sh
relaydeck skills list            # every discovered skill across sources + state
relaydeck skills show <name>     # one skill's metadata + which agents see it
relaydeck skills doctor          # health across all sources
relaydeck skills install         # install the bundled `relaydeck` skill locally
```

See `relaydeck/reference/extending.md` for importing your own skills into a
workspace and the full injection contract (runtime vs user vs native skills).

## Authoring guidelines

- Keep `SKILL.md` **lean** — the frontmatter (`name` + `description`) is always
  in an agent's context; the body is read on demand. Push depth into
  `reference/`.
- The `description` is the discovery hook: write it as "read this when …" so an
  agent knows *when* to reach for the skill.
- **Verify every command** against the real CLI (`relaydeck <cmd> --help`).
  Hallucinated flags are the #1 cause of agent tool-call failures — the whole
  point of a skill is to lower that rate, not raise it.
- A skill validates via `relaydeck.skills.validate_skill_dir(path)` (required
  `SKILL.md`, parseable frontmatter, `name` + `description`).

All skills here are MIT-licensed, same as relaydeck.
