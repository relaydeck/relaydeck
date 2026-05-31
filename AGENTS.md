# Notes for AI coding agents on this repo

relaydeck is a **micro-agent orchestrator & workflow engine** —
**web-primary**, plugin-extensible, with a fully scriptable CLI at parity.
One daemon per machine, web dashboard at `localhost:8765` (the primary operator
surface), a fleet of CLI-harness-wrapping agents per registered workspace,
durable agent-to-agent messaging, and a meta layer (`purpose`/`tags`) that lets
agents discover and delegate to each other.

**Tests:** `uv run pytest -q` (full suite green; `-m "not e2e"` in CI). Use
`uv run relaydeck …` in dev — a bare `relaydeck` may be a frozen tool copy that
ignores repo edits.

## Hard rules (don't violate without strong reason)

- **Everything is a plugin.** Harnesses, vault, metering, messaging, skills —
  all discovered at startup. No capability lives outside the plugin system.
  **All official plugins live in the root `plugins/` package**; `relaydeck/` is
  the engine + host contract. **The boundary is one-directional and enforced**:
  core NEVER statically imports `plugins`; plugins import only the public facades
  — `relaydeck.{sdk,harness,provider,vault,automation,testing}`. Strategy +
  governance: **Package separation** (below) and [CONTRIBUTING.md](CONTRIBUTING.md).
- **One primitive: the agent.** Every long-lived thing the daemon does is a
  `BaseAgent` subclass (`agents_base.py`). No parallel runtime surfaces.
- **YAML is the spec, SQLite mirrors it.** Agent defs live in
  `~/.relaydeck/agents/<id>.yaml` — source of truth for `id`/`name`/`type`/
  `workspace`/`config`/`auto_start`/`purpose`/`tags`/`system_prompt`/
  `inject_identity_preamble`. The DB mirrors a subset for fast queries. Never use
  the DB row as source of truth; `Orchestrator.start()` resyncs DB from YAML.
- **Workspace plugin source-of-truth is `agent.toml`, not `config.toml`.** The
  harness reads `workspaces/<ws>/agent.toml` at spawn; `load_workspace_registry()`
  prefers it, falling back to `config.toml` for legacy workspaces only.
- **The web dashboard is THE primary interface; the CLI is at parity, not
  ahead.** Anything doable from the CLI MUST be doable from the dashboard, both
  calling the same daemon HTTP API. **Never ship a web view that punts the
  mutation to the CLI** — no "use `relaydeck X`" empty-state where a form
  belongs. A new capability ships with its web affordance in the same change.
  The only legit CLI/TTY-only ops are inherently local-process (`daemon
  start/stop`, `serve`, `attach`, `view`, `workspace view`, `layout`).
  **Destructive/interrupting web actions must be responsible**: surface
  consequences + require confirmation (e.g. the daemon-restart confirm modal).
- **CLI ↔ daemon over HTTP, never via local orchestrator.** Live state (PTY
  injection, event subscribers, running instances) lives in the daemon. A CLI
  command that affects live state POSTs to the daemon, falling back to
  durable-enqueue when unreachable. Local-orchestrator calls are fine for DB
  reads + YAML edits, never for agent push.
- **Workspace plugins.** Workspaces declare enabled plugins in `agent.toml`
  (`plugins = ["messaging", "skills", ...]`); each harness checks the list and
  applies injections — no "modes". Only `workspace_scoped = True` plugins belong
  here, plus the harness-gate names in `workspace_plugins.py:HARNESS_GATES`.
  **Exception:** the GitHub poller starts when `workspaces/<ws>/github.yaml` has
  a valid `repo:` — not from `agent.toml`.
- **One source of truth for skills: `relaydeck/skills.py`.** Discovery, parsing,
  validation, hashing, materialization all live here; harnesses MUST use it so
  the Skills lens reports the *exact* set the model sees. Invalid skills (no
  `SKILL.md`, or frontmatter missing `name`/`description`) are skipped
  identically across harnesses. The **`skills` harness-gate** gates user-authored
  `workspaces/<ws>/skills/`; the bundled **`skills` plugin** is the inventory +
  management layer + the `[plugin.skills]` materializer.
- **Plugin-contributed skills go through `[plugin.skills]` + ownership sidecars
  — never hand-rolled.** A plugin declares `[plugin.skills]\n<name> =
  "SKILL.md"`; the `skills` plugin materializes into `runtime/skills/<name>/`
  with a `.relaydeck-skill.json` sidecar (`owner_plugin`, `source_hash`,
  `managed_by`) so plugins can't clobber each other and re-writes are
  hash-skipped. Optional duck-typed hooks: `skill_target_workspaces(all)` and
  `skill_content(name, source_text)`. Disabling `skills` stops re-sync but never
  strips materialized skills.
- **Vault values never leave the daemon.** `/api/vault/keys` returns names only.
  `${vault:NAME}` substitution happens at agent start; substituted config never
  round-trips to disk.
- **Migrations are additive.** `relaydeck/db.py:_migrate` runs on every
  `open_db`. New columns/tables only, guarded by try/except. Never rename, drop,
  or alter. Indexes on new columns live in `_migrate`, not `SCHEMA`.
- **Streaming is SSE + WebSocket.** SSE for events, WebSocket for harness PTY
  output. Never persist PTY bytes as events.
- **The dashboard is real-time by default — no per-view polling.** **Server**:
  one SSE feed (`_bus`) backs `/api/events`; `BaseAgent.log_event` publishes
  usage/harness/loop events, and `Orchestrator.set_event_bus` bridges
  `PluginEventBus` → `_bus` for the other domains — new mutations must emit a bus
  event. **Client**: views call `live.subscribe(path, cb)` (the `LiveStore` in
  `data.js`), not `getJSON`/`setInterval`; mutations call `live.invalidate(prefix)`.
  - **Terminal-safety: never re-render a detail pane from a live callback** —
    that remounts tiles incl. the PTY terminal. Patch in place, re-render the
    sidebar only, or guard a full `host.render()` to the active lens. The
    terminal tile (WebSocket PTY) and the identity tile (inline edit form) own
    their own fetch by design; the events tile streams via SSE.
- **The dashboard + every plugin UI are build-less, light-DOM Lit on the shared
  `@relaydeck/ui` kit.** Vendored Lit (no bundler — `scripts/vendor_lit.sh`); the
  daemon importmaps the bare specifiers `lit` and `@relaydeck/ui`
  (`transports/api.py`). Lenses extend `RelayLens` (`sidebar()`/`detail()`
  templates, `this.use('/api/…')` for live data), tiles use `RelayElement` +
  `defineTile`, plugins keep the framework-neutral `mount(container, api, ctx)`
  boundary — and all share one themed vocabulary (buttons, `<rd-toggle>`,
  `<rd-settings-form>`, modals, the CSS design tokens). `app.js` stays the
  imperative controller. Author against `web/static/uikit/README.md`; **never
  migrate `tiles/terminal.js`** (xterm), and keep terminal/picker/drag lifecycle
  imperative against static anchor nodes the reactive layer never re-renders.
- **`\r` not `\n` to submit to TUI children.** Raw-mode terminals treat `\n` as a
  literal line-feed and `\r` as Enter. Base `HarnessAgent.send_message` is the
  single submit chokepoint, tunable per-agent: newline-normalize (collapse
  embedded CR/LF so a multi-line message can't partial-submit), split-write
  (write body, sleep, then write `\r` separately so a paste-debouncing TUI like
  Ink can't swallow Enter), else append `\r`. A True return means bytes reached
  the PTY, **not** that the widget accepted them.
- **Messaging delivery is best-effort-with-confirmation.** PTY injection can't be
  TCP-reliable, so relaydeck layers defenses: a **readiness gate** (a harness
  that hasn't emitted output isn't input-ready; push skips the live write for a
  cold instance and leaves it queued; live-drain retries); **honest delivery**
  (opt-in `messaging.confirm_delivery` marks `delivered` only after the id echoes
  back in the PTY — runs only on the push path, never the drain, which would
  deadlock); **`relaydeck reply <msg_id> <body>`** infers recipient +
  `--in-reply-to`; and **cleanup** (prune `delivered`/`failed` rows past
  retention; a `queued` message is never pruned by age).
- **Soft cap: keep files under ~600 LOC.**

## Where things live

Two top-level packages: **`relaydeck/`** (the engine + host contract — imports
ZERO plugins) and **`plugins/`** (every relaydeck-managed plugin: the infra ones
`vault`/`github`/`loop`/`harnesses`/`external_agents` plus the extensions
messaging, telegram, skills, theme, metering, gateway, file_watcher,
usage_limits, hitl, dashboard, providers). Packaging + governance: see
**Package separation** below and [CONTRIBUTING.md](CONTRIBUTING.md).

| Concern | Module |
|---|---|
| Plugin system + `RelaydeckPlugin` + bus + trust ladder | `relaydeck/plugin.py` |
| Public facades (plugins import these, never internal modules) | `relaydeck/{sdk,harness,provider,vault,automation,testing}.py` |
| Automation action dispatcher + `parse_schedule` | `relaydeck/automation/` |
| Harness PTY base + `compose_identity_preamble` | `relaydeck/harness/base.py` |
| Secret backend protocol + `get/set/delete_secret` | `relaydeck/vault.py` |
| Recommended bundles / curated registry | `relaydeck/bundles.py`, `relaydeck/plugin_registry.py` |
| Agent base class | `relaydeck/agents_base.py` |
| Orchestrator + `send_message_to` + `update_agent_meta` | `relaydeck/orchestrator.py` |
| Config + `AgentSpec` + workspace registry | `relaydeck/config.py` |
| DB + schema + migrations + `agent_messages` | `relaydeck/db.py` |
| Message envelope + `render_format` | `relaydeck/messages.py` |
| Plugin-owned task records (PR/issue/CI) | `relaydeck/tasks.py` |
| Automation run history / per-call LLM log | `relaydeck/automation_runs.py`, `relaydeck/model_invocations.py` |
| Skills: discover/validate/hash + materialize (+ SQLite mirror) | `relaydeck/skills.py`, `relaydeck/skills_cache.py` |
| Git worktrees (+ setup/teardown hooks) | `relaydeck/worktrees.py` |
| Danger-zone data wipe | `relaydeck/maintenance.py` |
| Persistent state (active ws, daemon URL) | `relaydeck/state.py` |
| Plugin disabled-list / settings / catalog+gates | `relaydeck/plugin_disabled.py`, `plugin_settings.py`, `workspace_plugins.py` |
| Workers (background threads + logs) | `relaydeck/workers.py` |
| CLI / HTTP API+SSE+WS | `relaydeck/transports/{cli,api}.py` |
| Provider config + known/custom providers + model roles | `relaydeck/provider_config.py`, `providers_extra.py`, `model_roles.py` |
| models.dev metadata index | `relaydeck/models_dev.py` |
| Dashboard shell + lens router + SSE | `relaydeck/web/static/app.js` |
| Home dashboard / header, rail, status bar | `relaydeck/web/static/home.js`, `shell.js` |
| Lenses (Agents/Messages/Models/Workers/Workspaces/Appearance) | `relaydeck/web/static/lenses/*.js` |
| Tile system + built-in tiles | `relaydeck/web/static/tile_system.js`, `tiles/*.js` |
| Overlays (cmdk, drawer, new-agent, settings) | `relaydeck/web/static/overlays.js` |
| Auth/SSE/prefs client + **`LiveStore`** | `relaydeck/web/static/data.js` |
| Primitives (esc, h, iconSVG, sparkline) | `relaydeck/web/static/primitives.js` |
| **UI kit** (Lit bases `RelayElement`/`RelayLens`, controllers, components, `<rd-toggle>`/`<rd-settings-form>`, token contract) + authoring guide | `relaydeck/web/static/uikit/` (`README.md`) |
| Vendored Lit bundle (build-less) | `relaydeck/web/static/vendor/lit-all.min.js` (`scripts/vendor_lit.sh`) |
| CSS tokens + shell / theme runtime | `relaydeck/web/static/styles.css`, `panels.css`, `theme.js` |
| Dashboard prefs + appearance resolution | `relaydeck/preferences.py` |
| Harness plugins | `plugins/harnesses/<name>/` |
| Harness catalog + live CLI probes | `relaydeck/harness_options.py` |
| Messaging / GitHub / Telegram / external-agents plugins | `plugins/{messaging,github,telegram,external_agents}/` |
| Skills / Theme / Vault / Metering plugins | `plugins/{skills,theme,vault,metering}/` |
| Built-in TUI / workspace viewers + PTY snapshot (pyte) | `relaydeck/transports/{view,viewers}.py`, `relaydeck/screen.py` |
| Workspace-health roll-up | `relaydeck/workspace_health.py` |
| Vendor hook integrations | `relaydeck/integrations/` |
| Saved viewer layouts / theme engine | `relaydeck/layouts.py`, `relaydeck/themes.py` |
| Daemon lifecycle helpers | `relaydeck/daemon.py` |
| Public SDK (plugins + RemoteHost + AgentInfo) | `relaydeck/sdk.py` |
| One-liner installer | `scripts/install.sh` |

## Package separation (one repo, one wheel, split-ready)

**Single repo, single distribution.** The `relaydeck` package ships both
`relaydeck/` (engine) and `plugins/` (all official plugins) — `pip install
relaydeck` gives the whole thing.

- **`relaydeck/`** — engine + host contract: daemon, CLI, SDK, plugin host,
  runtime, dashboard assets, and the public facades. Imports zero plugins.
- **`plugins/`** — every relaydeck-managed plugin, loaded via
  `_scan_package("plugins")`. Split-ready into a future `relaydeck-plugins` wheel.
- **Trust ladder:** bundled > curated > local > untrusted. `plugins/bundle.toml`
  pins recommended bundles; `plugins/registry.yaml` is the curated registry.
- **Authoring:** scaffold `relaydeck plugin new <name> --pattern
  harness|provider|skill`; dev `relaydeck plugin dev|install --editable`;
  validate `relaydeck plugin verify`; release `relaydeck plugin publish-check`.
  Loaders: `_scan_package` (official), `_scan_entry_points` (external).

Full strategy + the future-split plan: see **Package separation** above.

## Config layout

```
~/.relaydeck/
  config.toml              workspace registry
  state.yaml               active workspace + daemon URL + daemon_bind_host
  plugins-disabled.yaml    globally-disabled plugin names
  plugin-settings.yaml     per-plugin settings store
  vault.yaml               secrets (mode 0600)
  agents/<id>.yaml         agent specs (source of truth, incl. purpose/tags/system_prompt)
  presets/<name>.yaml      model/provider presets
  layouts/<name>.yaml      saved viewer layouts (mode 0600)
  themes/<name>.yaml       user themes (mode 0600)
  auth-token               daemon auth token (mode 0600)
  integrations/<harness>/  vendor-hook installer artifacts
  workspaces/<name>/
    agent.toml             workspace plugins list (spawn-time source of truth)
    AGENTS.md              workspace context
    github.yaml            github plugin: repo + rules
    skills/                workspace user-authored skills
    runtime/               sessions.db, per-harness homes, fleet-context,
                           materialized plugin skills, github cursor
  runtime/relaydeck.db     main SQLite database
```

## The shipped harnesses

| Harness | CLI | Plugin | System-prompt path |
|---|---|---|---|
| pi | `pi` | `harnesses/pi/` | `--append-system-prompt <text>` |
| claude-code | `claude` | `harnesses/claude_code/` | `--append-system-prompt` (subclass) |
| codex-cli | `codex` | `harnesses/codex/` | Prepended to positional initial prompt |
| cursor-cli | `cursor-agent` | `harnesses/cursor/` | Positional initial prompt (per-agent `CURSOR_CONFIG_DIR`) |
| opencode-cli | `opencode` | `harnesses/opencode/` | `OPENCODE_CONFIG` with `instructions` |
| antigravity | `agy` | `harnesses/antigravity/` | `--prompt-interactive <prompt>` (no `--model`; trust pre-seed) |
| relaydeck-native | `pi` | `harnesses/relaydeck_native/` | Layered operator prompt via `--append-system-prompt` + bundled `pi_extension.ts` |

- **relaydeck-native** (`type: relaydeck`) is the fleet **operator** harness, not
  a separate model runtime. It subclasses `PiAgent`, spawns customized `pi` in a
  PTY, and runs HTTP turns via `pi -p --mode json --continue` with a session-dir
  file lock so the dashboard chat widget and the Terminal tab share one pi
  session. A bundled pi extension exposes fleet tools that shell out to the
  `relaydeck` CLI. `HARNESS_CLI["relaydeck"] = "pi"` — the catalog, status
  endpoint, and `doctor` probe `shutil.which("pi")` on every call (no restart
  after installing pi); surfaces show `install_hint` when pi is missing.
- **cursor-cli** authenticates via the operator's own Cursor account (no provider
  keys); model is Cursor's namespace (`config.cursor_model`). Flat-rate
  subscription → no per-call token metering (honest 0 / $0.00). Per-agent
  isolation is a relocated `CURSOR_CONFIG_DIR`.
- **antigravity** (Google's `agy`) authenticates via the operator's Google
  account (keyring; no provider keys → account-default model, switched with
  `/model`, no `--model` flag). Because the CLI blocks on an interactive
  workspace-trust prompt, the harness **pre-seeds** the cwd into the antigravity
  settings `trustedWorkspaces` at spawn (skipped for `manual` autonomy).

### Autonomy posture (every harness runs UNATTENDED)

A relaydeck agent has no human at its harness TUI to answer an approval prompt —
an unlisted tool that would prompt blocks forever. Each harness translates one
cross-harness knob, `config.autonomy` (default `"auto"`), into native permission
flags via `HarnessAgent._autonomy_mode()`:

- `"auto"` — run safe work autonomously, guard dangerous ops (best-effort per
  harness): claude `--permission-mode auto`; codex `--sandbox workspace-write
  --ask-for-approval never` + `~/.relaydeck` writable + network on; opencode a
  `permission` block; cursor un-sandboxed unrestricted (no safe/dangerous
  classifier); pi nothing (no approval prompts — the reference).
- `"bypass"` — skip all checks. `"locked"` — fail-safe; only the allowlist runs.
  `"manual"` — inject nothing; the operator drives the harness's own flags.

**The `relaydeck` CLI is ALWAYS auto-allowed** (per-harness allow rules / keeping
`~/.relaydeck` writable) so peer messaging / `relaydeck reply` never stalls and
can persist outside any workspace sandbox. Operator-pinned native flags take
precedence.

## Agent meta layer

Each agent has YAML-persisted, model-visible identity (`id`/`type`/`workspace`/
`purpose`/`tags`/`system_prompt`/`inject_identity_preamble`):

- `purpose` + `tags` mirror into the DB for fast `relaydeck agent find` / peer
  lookups (YAML is source of truth; resynced on every boot). `system_prompt` is
  YAML-only (read at spawn, no DB mirror).
- The auto-generated **identity preamble** at spawn time names the agent's id,
  workspace, purpose, and peer agents (id/type/purpose) with the
  `relaydeck workspace message "<body>" --agent <peer-id>` collaboration hint.
  Built by `HarnessAgent._build_identity_preamble` reading the DB live, so a new
  peer is visible on the next agent start without restart.

## Semantic status (observable agent state)

Orthogonal to process-level `status`, every agent has a **semantic status**
describing what the model is *doing*: `working`, `awaiting-input` (paused for
human approval), `complete-unread`, `idle`.

- **DB**: `agents.semantic_status` + `_at` + `_source`. Set via
  `Orchestrator.set_semantic_status(...)` — emits `agent.status_changed` only if
  changed. Endpoint `POST /api/agents/{id}/state`; read-transition
  `POST /api/agents/{id}/viewed` (and `relaydeck agent viewed`) clears
  `complete-unread` → `idle` when the operator focuses the agent.
- **Sources**: `hook` (vendor-side, deterministic — Claude Code), `engine`
  (daemon screen sampler in `relaydeck/semantic_engine.py` — every PTY harness),
  `hitl` / `manual` / `viewer` (operator paths). The engine defers to a fresh
  `hook`/`hitl`/`manual` signal and reclaims after ~45s of silence. Integration
  catalog entries for hookless harnesses are `kind="classifier"` for back-compat
  but report the always-on engine (not a plugin).
- **Workspace roll-up**: `workspace_health.py:roll_up()` via precedence
  `errored > awaiting-input > working > complete-unread > idle > stopped > empty`.

## Subsystems (pointers)

Each of these is a focused subsystem; read the listed module for detail. They
share two recurring shapes: the **automation action dispatcher** (below) and the
**web-primary, CLI-at-parity** surface contract.

- **Messaging + Telegram (`relaydeck workspace message` / `relaydeck telegram …`)**
  — the durable agent-to-agent inbox (SQLite rows, PTY injection when live, late
  drain on start) plus the Telegram gateway that bridges a human chat to an
  agent. Reply contract: answer a `[relay from=… id=…]` line with `relaydeck
  reply <id>` (infers recipient + threads; routes back through the bot for a
  Telegram sender). Safety net: `list_unanswered_peer_messages` flags a turn that
  ended still owing a reply — but ONLY for an *initiating* message
  (`in_reply_to IS NULL`) from a *registered* fleet agent, so acks and channel
  senders never loop; it drives the idle nudge and `inbox --awaiting-reply`.
  Telegram control commands (`/new` fresh-session, `/restart` keep-history,
  `/screenshot`) act through the orchestrator's one-shot session intent
  (`start_agent(session="fresh"|"resume")`, via `harness_options.apply_session_mode`)
  — no PTY/spawn code touched. Plugins: `plugins/{messaging,telegram}/`.
- **Vendor integrations (`relaydeck integration …`)** — pluggable telemetry for
  the semantic-status axis. `kind="hook"` installs a script in the vendor's hook
  system (claude); `kind="classifier"` catalog entries document harnesses whose
  status comes from the built-in screen engine (no per-harness install).
  Registry: `relaydeck/integrations/`.
- **GitHub plugin (`relaydeck github …`)** — workspace-scoped poller of `gh api
  repos/<repo>/events`, routed through per-workspace `github.yaml` rules. Opt-in
  via the yaml (`repo:` + optional `poll_interval_s`/`rules`), not `agent.toml`.
  Auth is whatever `gh auth status` reports — never ship/read tokens. Companions:
  `enrich.py` (targeted `gh api` for CI/review) and `spawn.py:spawn_worktree_agent`
  (issue→PR workflow: worktree → workspace → agent → task). Webhook ingress lives
  in the `gateway` plugin, not here.
- **`relaydeck view`** — single-window TUI: workspaces sidebar + focused-agent
  PTY pane + message tail. Live PTY via WebSocket; pyte resolves escapes; key
  forwarding is byte-sensitive (`\r` vs `\n`, `\x7f` vs `\x08`, CSI vs SS3).
- **Saved layouts (`relaydeck layout …`)** — named bundle of `workspace view`
  flags; `layouts/<name>.yaml`, atomic temp+rename, path-traversal guard.
- **Git worktrees** — a separate working tree on its own branch, registered as a
  normal workspace, so a fleet works several branches in parallel. **Core, not a
  plugin** (`relaydeck/worktrees.py`): pure git. A repo declares
  `<repo>/.relaydeck/worktree.yaml` (`setup:`/`teardown:`/`env:`) run as a
  subprocess (same trust as automation `script`/`code`, never in-daemon `exec`);
  a failed hook is reported, not fatal. Create/remove emit
  `worktree.created`/`.removed`.
- **Theme engine + appearance** (`relaydeck/themes.py`) — `THEME_TOKENS` is the
  token contract (~50 `--*` design tokens). A theme is a named bundle of token
  overrides that may `extends:` another, flattened and applied as inline CSS vars
  on `<html>`. Builtins ship in-package (`base` default + accent/dark presets); a
  user file shadows a builtin. **Appearance** binds theme + density + glow +
  widget layout per-workspace with a global default (`preferences.py`).
- **External agents (read-only)** — a second agent *primitive*, plugin-owned and
  NOT a `BaseAgent`: a runtime relaydeck does **not** run (Hermes Agent /
  OpenClaw). v1 detects + reports (filesystem-first detection, independent
  read-only probes → health + risk labels); it never onboards, mutates, starts,
  or reads secrets. Store: one JSON file per agent under `plugin-data/external/`.
  Plugin: `plugins/external_agents/`.

## Plugin event system

Typed event bus (`PluginEventBus`); events are immutable, dotted type names.

| Category | Examples | Emitted by |
|---|---|---|
| `agent.*` | `start`, `stop`, `error`, `message`, `status_changed` | Orchestrator |
| `workspace.*` | `added`, `updated`, `removed`, `file.changed` | API + file-watcher |
| `worktree.*` | `created`, `removed` | API (worktree routes) |
| `external_agent.*` | `added`, `removed`, `health` | external-agents plugin |
| `system.*` | `startup`, `shutdown`, `plugin.loaded`, `plugin.unloaded` | PluginRegistry |
| `gateway.*` | `webhook`, `message`, `teleport` | gateway plugin |
| `harness.*` | `assistant_message`, `spawn`, `exit` | harnesses |
| `usage.record` | per-LLM-call token + cost record | harness JSONL tailers |

Subscribe via `get_subscriptions()` returning
`[EventSubscription("agent.message", "_on_message"), ...]`.

### Plugin lifecycle hooks

```python
class MyPlugin(RelaydeckPlugin):
    name = "my-plugin"
    category = "tool"            # harness | tool | cognitive | infrastructure
    workspace_scoped = False     # True if it appears in agent.toml plugins
    def on_load(self, ctx): ...
    def on_unload(self): ...                       # disabled or shutdown
    def on_settings_changed(self, values): ...     # POST /api/plugins/<name>/settings
    def get_subscriptions(self): return [...]
    def get_settings_schema(self): return [...]    # Settings overlay form
    def register_cli(self, cli): ...               # `relaydeck <plugin> ...`
    def register_api_routes(self, app): ...        # FastAPI routes
    def register_ui(self): return {"tabs":[...], "header_chips":[...], "agent_tiles":[...]}
```

## Adding a plugin

- **Harness**: `plugins/harnesses/<name>/plugin.py` — an SDK `Plugin` whose
  `on_load(host)` subclasses `HarnessAgent` (from `relaydeck.harness`), sets
  `CLI`, and registers via `host.harnesses.register(...)` (NOT a direct
  `register_agent_type()`). System-prompt injection: override `_build_command`
  (raw CLIs) or `_initial_prompt` (positional CLIs like codex) to include
  `_compose_system_prompt()`. See `pi/agent.py` + `codex/agent.py`.
- **Infrastructure**: `plugins/<category>/<name>/plugin.py` — an SDK `Plugin`
  whose `on_load(host)` wires routes/CLI/UI via `host.{api,cli,...}`. If
  workspace-scoped: set `workspace_scoped = True` and gate on
  `_workspace_has_<name>` checks. See `messaging/plugin.py`.

## Model roles & selection

- **Roles** (`model_roles.py`) are a layer above presets: a named semantic slot
  (`fast`/`frontier`/`classifier`/`embedding`/`vision`/`voice`/`image`). The
  package ships role *definitions*, NEVER the *picks* and **NO fallbacks** —
  every role resolves only once the operator assigns a model (onboarding or
  `relaydeck defaults set`). Unset → `None`; `resolve_role(require=True)` raises
  an actionable hint. Consumers call `host.models.for_role(...)` or
  `complete(prompt, model="role:fast")`.
- **One picker, one resolver.** `model_select.js` (`mountModelSelect`) is THE
  model picker everywhere; output is a single spec string (preset name |
  `provider/model` | `role:<name>`). `sdk.py:resolve_model` is resolution truth
  (`role:` → preset → `provider/model` → bare→openrouter); `GET
  /api/models/resolve` returns the resolved spec + source + validity. Validation
  never blocks — a bare id warns rather than mis-routing. **No shipped presets or
  aliases** — presets are 100% operator-created; don't auto-pull or assume any
  model.
- **Providers** (Models lens → Providers): each `ProviderPlugin` is configurable
  — API key → vault, base-URL override → `provider_config.py`. Known + custom
  providers via `providers_extra.py` (generic OpenAI/Anthropic-compat + native
  `OllamaProvider`). Local detection (`local_providers.py`) probes standard ports
  fail-open and never registers or pulls.
- **models.dev metadata index** (`models_dev.py`) — a cached, **metadata-only**
  provider-keyed index (pricing/capabilities/limits/logos), NOT a source of
  picks. Fail-open + stale-cache fallback; the request path is never blocking
  (`cache_only=True` + background refresh; a startup `warm()` thread). Metering
  inserts it as a pricing ladder tier (static → live catalog → provider `*`
  wildcard → models.dev → zero).

## Dashboard IA

- **No top tabs.** A 44px vertical **activity rail** switches core lenses:
  Agents · Messages · Models · Workers · Workspaces · Appearance (`Messages` only
  appears when `messaging` is enabled somewhere). The **Workers** lens is
  unified: **Configurable workers** (user loop agents — triggers + optional
  attached actions, fully managed from the web via `worker_form.js`) and **System
  workers** (github poller, telegram, skills-rescan, db maintenance). A plugin
  tab earns a rail slot only with `default_state = "lens"`; default is a tile.
- **Header**: brand · workspace pill · `plugins · N` popover · `⌘K` search ·
  `+ New Agent` · 🔔 · ⚙ · LIVE.
- **Settings → Danger Zone**: red section to wipe transient tables (messages /
  events / usage / invocations / runs) — per-scope checklist + live row counts +
  type-`wipe`-to-confirm. Config/agents/vault/audit/durable-bus never wipeable.
  Reference for a responsible destructive web action.
- **Agents detail = tile system.** Plugins contribute tiles via `[plugin.ui]
  tiles`; each is `tab` or `hidden`. The **Panels manager** toggles tab/hidden,
  reorders, sets visible-tab count. Per-user prefs in `/api/preferences`
  (`tile_states`/`tile_order`/`tile_max_tabs`). Tile lifecycle: ES-module class
  with `mount(container, api, ctx)` + `unmount()`.
- **Popovers must be `document.body`-mounted + anchored** — capture the trigger
  rect before any re-render that detaches it; exactly one outside-click listener
  at a time (`_installPopOutsideClick`).
- **Per-agent stats are real, not faked** (`GET /api/agents/{id}/stats` +
  usage-rollup). Zero usage renders honest 0 / `$0.00`. The **stat strip** is a
  fixed CORE catalog (`STAT_CELLS`), user-reorderable but NOT plugin-extensible —
  plugin metrics belong in tiles.
- **Cache busting**: `/` served `no-store`; `/static/*.{js,css}` + importmap
  stamped `?v=<pid>` (changes every `daemon start`). "Edits not showing up" →
  restart daemon + reload.
- **URL deep-links**: `?workspace=`, `?lens=`, `?agent=`, `?section=` —
  bookmarkable, synced via `replaceState`.
- **Visual system (Studio)**: `:root` CSS tokens in `styles.css`. Warm paper
  canvas (`--bg-0:#F2EFE6`), single terracotta accent (`--acc:#B7410E`), no glow.
  IBM Plex Sans (body) + IBM Plex Mono (labels/code). Default theme `base`;
  `themes.py THEME_TOKENS` defaults must mirror `:root`. Density/glow via
  `<html data-density>`/`data-glow`.
- **Plugin lens sidebar nav (standardized).** A lens module's default class may
  define `sections()` returning nav entries (flat or grouped); `app.js:PluginLens`
  renders a consistent sidebar nav, drives the active section (deep-linked via
  `?section=`), and hands the lens state through `mount`'s `ctx`. Reference: the
  telegram lens. A lens with no `sections()` keeps the whole UI in the detail pane.
- **Home dashboard** (`home.js`) is the no-agent-selected overview — a 12-col
  customizable widget grid, rendered inside the Agents detail pane. Widget
  sources: core, personal (browser-local), plugin (gated on the source plugin).

## Recurring patterns to follow

- **New agent meta field**: decide if it needs query speed (mirror via
  `upsert_agent` + boot resync) or only spawn-time read (YAML-only).
- **Plugin-scoped writes need lifecycle hooks.** If a plugin materializes files,
  wire ALL three workspace events (`added`/`updated`/`removed`) plus
  `on_settings_changed` + `on_unload`.
- **Two bus surfaces, two emit shapes.** Raw `PluginEventBus.emit` takes a built
  `Event`; the SDK `host.events` takes `emit(type, data)` and builds it (prefer
  `(type, data)`).
- **SDK host surface is capability-gated. Plugins go through `host.*`, never
  private orchestrator internals or `relaydeck` shell-outs** (`relaydeck/sdk.py`).
  Each call requires the matching capability in `plugin.toml
  declared_capabilities`. Key surfaces: `host.agents` (`list`/`get`/`find`/
  `send_message`/`create`/`start`/`stop` — `create` is YAML+DB but does not
  start), `host.events` (`subscribe(..., durable=True)` for at-least-once
  replay), `host.workers` (`spawn(..., restart_policy=)`), `host.tasks`
  (plugin-owned PR/issue/CI records, namespaced + workspace-scoped),
  `host.workspaces` (`create`/`update`/`list`).
- **RemoteHost SDK parity.** External automation using `RemoteHost` manages
  providers, plugin settings/toggles, and plugin lifecycle through the same
  daemon API as the dashboard.
- **Live unload requires unsubscribe + worker stop + on_unload** (see
  `PluginRegistry.disable`); DOM reconciled via `window.reloadPluginTabs()`.

### Automation action dispatcher (shared by github + loop)

One schema across triggers (`relaydeck/automation/`). Action types:

| Action | Params | Effect |
|---|---|---|
| `agent.message` | `to`, `body`, opt `in_reply_to` | `send_message_to` |
| `script` | `path`, opt `env`/`timeout` | subprocess; event JSON on stdin; cwd=workspace |
| `gh` | `args`, opt `timeout` | run any `gh` subcommand |
| `bus.emit` | `type`, `data` | emit a plugin-bus event |
| `model` | `prompt`, opt `model`/`max_tokens`/`include_event`/`emit`/`to` | run a model; route via `emit`/`to` |
| `code` | `body`, opt `lang`/`env`/`timeout`/`emit` | run an INLINE code block as subprocess (never `exec()`) |

**Template syntax**: `{{ dotted.path }}` resolved against `payload` then
top-level event keys. No eval, no Jinja. Missing paths render as the literal
placeholder + log a warning.

## Discipline

- **Ship features with tests** (real SQLite + FastAPI TestClient + Click runner;
  no mocks at I/O boundaries). **No new files without a reason** — prefer
  editing. **Rare comments** — only when the *why* is non-obvious. **No
  domain-specific defaults in the package.** **`relaydeck` is the only
  entrypoint** — no `relaydeck-*` sub-commands.
- **Verify dashboard/UI changes in a real browser via the Playwright MCP**;
  `docs/playwright.md` is the verification cheat sheet — keep it updated.
- **Plugin disable must remove tab/chip/tile too.** `/api/plugins/ui` filters the
  manifest by loaded plugins; the dashboard reconciles DOM.

## CLI

`relaydeck --help` documents every command, and `http://127.0.0.1:8765/docs` has
the OpenAPI. The command groups: `daemon`/`serve`, `status`/`doctor`,
`agent`, `workspace`, `reply`, `attach`/`view`/`chat`, `preset`/`defaults`/
`vault`/`usage`, `plugin`/`integration`, `github`, `external`, `worktree`,
`skills`, `theme`/`layout`, `workers`/`automation`, `db` (danger zone). The CLI
is the scriptable path and the reference for *what* an op does; the dashboard is
at parity for everything except inherently-local-process ops.

## Chat-block format (peer messages)

When A sends to B, B's PTY receives one line: `[relay from=A id=msg_xxx]
<body>`. The `[relay ...]` prefix is a stable marker the messaging SKILL.md
teaches the receiver to recognize. Reply with `relaydeck reply msg_xxx
"<reply>"` — it infers the recipient and threads the reply (the can't-misroute
path; expands to `workspace message --agent A --in-reply-to msg_xxx`). An agent
that ends its turn still owing a reply gets one gentle nudge, and an operator
can audit with `inbox --awaiting-reply` (see the Messaging subsystem above).
Format is configurable per-message (`format=`), per-agent
(`config.message_format`), plugin setting (`messaging.default_format`), or
default. Placeholders: `{from}`, `{to}`, `{body}`, `{id}`, `{in_reply_to}`,
`{ts}`.
