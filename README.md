# relaydeck

**Your local fleet OS for CLI coding agents.**

One daemon on your machine. One dashboard. Every harness — run in parallel, watch live, message peer-to-peer, automate with plugins. No cloud account required.

![CI](https://github.com/relaydeck/relaydeck/actions/workflows/ci.yml/badge.svg)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Why relaydeck

| | |
|---|---|
| **Harness-native** | Wraps real CLI agents (`pi`, Claude Code, Codex, …) — not a replacement model runtime |
| **Local-first** | State in `~/.relaydeck` + SQLite; secrets stay in the vault on the daemon host |
| **Fleet-aware** | Agents discover peers via `purpose` / `tags`; durable messaging with late drain |
| **Observable** | Live PTY terminals, semantic status, usage metering, SSE — no polling dashboards |
| **CLI = API = UI** | Every operation works from the shell, HTTP API, and web dashboard |
| **Plugin-extensible** | Harnesses, providers, messaging, skills, automations — all plugins |

---

## Supported harnesses

relaydeck runs **unattended** harness children in PTYs. Each harness maps your agent config (model, autonomy, system prompt) to the vendor CLI.

| Harness | CLI | Agent type | Notes |
|---------|-----|------------|-------|
| **Pi** | `pi` | `pi` | Reference harness; append system prompt |
| **Claude Code** | `claude` | `claude-code` | Permission modes, hook integrations |
| **Codex CLI** | `codex` | `codex-cli` | Sandbox + approval translation |
| **Cursor CLI** | `cursor-agent` | `cursor-cli` | Subscription auth; per-agent config dir |
| **OpenCode** | `opencode` | `opencode-cli` | Config-file instructions injection |
| **Antigravity** | `agy` | `antigravity` | Google account auth; positional prompt; workspace-trust pre-seed |
| **relaydeck native** | `pi` | `relaydeck` | Fleet operator — customized pi with extension tools (messaging, agents, dashboard) |

Install at least one harness CLI on `$PATH`. **relaydeck-native** (`type: relaydeck`) also requires **`pi`** — same binary as the coding harness, but with relaydeck's operator prompt layers and a bundled pi extension for fleet tools:

```sh
npm install -g @mariozechner/pi-coding-agent
```

The dashboard and `relaydeck doctor` probe `pi` live on every request (`shutil.which`) — installing pi while the daemon is running is enough; no restart required. Model/provider keys are configured per harness or via relaydeck's provider plugins (relaydeck-native bridges vault keys into pi at spawn).

---

## Plugin system

Everything beyond the core runtime is a **`RelaydeckPlugin`**: discovered at startup, capability-gated, and removable without forking the engine.

| Group | Bundled examples | What it adds |
|----------|------------------|--------------|
| **Harness** | pi, claude-code, codex, cursor, opencode, antigravity, relaydeck-native | Agent types, PTY lifecycle, usage tailers |
| **Secrets & integrations** | vault, github, loop, external | Secrets, event pollers, automations, external-agent observation |
| **Messaging & I/O** | messaging, telegram, gateway, file_watcher | Inbox, webhooks, workspace file events |
| **Skills & appearance** | skills, theme | Skill materialization, appearance tokens |
| **Providers** | openrouter, openai, anthropic, ollama | Model catalogs + completion routing |
| **Usage** | metering, usage_limits | Token/cost tiles, spend guards |

> Each plugin declares its own `category` field in `plugin.toml` (one of `harness`/`tool`/`cognitive`/`infrastructure`). The groupings above are how this doc organizes them; the canonical category is the `plugin.toml` field.

Workspace-scoped plugins are opted in per project in `workspaces/<name>/agent.toml`. Daemon-wide plugins load globally.

```sh
relaydeck plugin list
relaydeck plugin install --editable ./my-plugin
relaydeck plugin disable telegram
```

---

## Quick start

**Requirements:** Python 3.12+ and at least one harness CLI on `$PATH`. Website: **[relaydeck.ai](https://relaydeck.ai)**.

```sh
# Recommended — installs uv if needed, then relaydeck as an isolated uv tool:
curl -fsSL https://relaydeck.ai/install.sh | sh
```

```sh
# Or with your own Python tooling:
uv tool install relaydeck       # or: pipx install relaydeck
```

```sh
# From source:
git clone https://github.com/relaydeck/relaydeck.git
cd relaydeck && uv tool install .
```

```sh
# Register this repo as a workspace with messaging enabled.
relaydeck init . --plugin messaging

# Start the daemon (dashboard at http://127.0.0.1:8765).
relaydeck daemon start

# Create and start two agents with distinct roles.
relaydeck agent create planner --type claude-code \
  --purpose "Plan changes; do not implement"
relaydeck agent create coder --type codex-cli \
  --purpose "Implement scoped patches from planner specs"
relaydeck agent start planner coder

# Send work; agents see peers via the auto identity preamble.
relaydeck workspace message --agent planner \
  "Plan: add --dry-run to relaydeck plugin uninstall. Keep it under 50 LOC."

# Observe
relaydeck view              # built-in TUI
open http://127.0.0.1:8765  # web dashboard (primary UI)
```

`relaydeck --help` documents every command. With the daemon running, OpenAPI lives at `http://127.0.0.1:8765/docs`.

---

## Core concepts

| Concept | Description |
|---------|-------------|
| **Daemon** | One per machine — PTYs, workers, FastAPI, SSE, WebSocket terminals |
| **Workspace** | Registered project directory; plugins listed in `agent.toml` |
| **Agent** | Named harness instance (`~/.relaydeck/agents/<id>.yaml` is source of truth) |
| **Message** | Durable row in SQLite; injected to PTY when live, drained on start |
| **Worktree workspace** | Parallel git checkout as a first-class workspace (branch per task) |
| **Plugin** | CLI + API + UI contributions declared in `plugin.toml` |

---

## Architecture

```
                    ┌───────────────────────────────────────┐
  CLI / scripts     │         relaydeck daemon              │     Web dashboard
  relaydeck …  ────▶│  orchestrator · plugins · event bus   │◀──── localhost:8765
       HTTP/SSE     │         │              │              │      (live SSE)
                    │    harness PTYs    SQLite state       │
                    │   pi · claude ·    agents · msgs      │
                    │   codex · cursor · usage · events     │
                    │   opencode · …                        │
                    └───────────────────────────────────────┘

  ~/.relaydeck/   agents/*.yaml   workspaces/*/agent.toml   vault.yaml   runtime/relaydeck.db
```

**Package layout:** one install ships `relaydeck/` (engine + host contract) and `plugins/` (every official plugin — infra + extensions). Core imports zero plugins; plugin authors import public facades only — `relaydeck.sdk`, `relaydeck.harness`, `relaydeck.provider`, `relaydeck.automation`, `relaydeck.vault`, `relaydeck.testing`. See [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Build your own plugin

Scaffold, develop, and verify without touching core:

```sh
relaydeck plugin new my-plugin              # harness | provider | skill pattern
relaydeck plugin dev ./my-plugin          # editable install
relaydeck plugin verify ./my-plugin       # manifest + skill validation
relaydeck plugin publish-check ./my-plugin
```

Minimal external plugin shape:

```
relaydeck-plugin-my-plugin/
  pyproject.toml          # [project.entry-points."relaydeck.plugins"]
  my_plugin/
    plugin.py             # PLUGIN = MyPlugin(...)
    plugin.toml           # name, category, capabilities, [plugin.ui]
    py.typed
  tests/
    test_plugin.py        # relaydeck.testing.MockHost / MockBus
```

Plugins can register **CLI commands**, **HTTP routes**, **dashboard lenses/tiles**, **workers**, **skills**, and **event subscriptions**. Capabilities declared in `plugin.toml` gate SDK access at runtime.

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full authoring workflow, git-install pinning, and hard rules (YAML source of truth, additive migrations, web/CLI parity).

---

## Development

```sh
git clone https://github.com/relaydeck/relaydeck.git && cd relaydeck
uv sync --group dev
uv run pytest -q -m "not e2e"    # fast CI-equivalent suite
uv run relaydeck plugin verify   # all bundled manifests
uv run ruff check relaydeck tests plugins
```

| Resource | |
|----------|---|
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Testing | [TESTING.md](TESTING.md) |
| Agent / architecture notes | [AGENTS.md](AGENTS.md) |
| Security | [SECURITY.md](SECURITY.md) |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |

---

## Remote control via Telegram

Route a Telegram chat to an agent and drive the fleet from your phone. Every message you send is delivered to the agent's PTY; the agent replies back through the bot. Slash-commands act on the routed agent **without** being forwarded into the harness — so they behave uniformly across every harness type:

| Command | Effect |
|---------|--------|
| `/new` (`/clear`, `/fresh`, `/reset`) | Start a fresh session (clean — drops the harness resume flag) |
| `/restart` | Restart the agent's PTY, keeping its history |
| `/screenshot` | Send a snapshot of the agent's live terminal |
| `/stop` · `/status` · `/help` | Stop, inspect, or list commands |

Typing `/` shows the command menu (pushed via the Bot API on startup).

```sh
relaydeck telegram setup                  # store the bot token in the vault
relaydeck telegram routes-add \           # map a chat → (workspace, agent)
  --chat <chat-id> --workspace <ws> --agent <agent>
```

---

## External agents (read-only)

relaydeck observes **Hermes Agent** and **OpenClaw** runtimes alongside the fleet it manages — health and risk posture only; no mutation or secret access.

```sh
relaydeck external detect ~/.hermes
relaydeck external add ~/.openclaw --probe
relaydeck external list
```

---

## Acknowledgements

relaydeck is **harness-native** — it wraps real CLI coding agents rather than
shipping its own model runtime. Huge thanks to the
**[pi](https://github.com/earendil-works/pi)** coding agent (relaydeck's
reference harness), and to Claude Code, Codex, Cursor, OpenCode, and Antigravity.
We were also inspired, in part, by ideas from across the open agent ecosystem —
including Nous Research's
[Hermes Agent](https://github.com/NousResearch/hermes-agent) and OpenClaw, which
relaydeck can observe read-only alongside the fleet it manages.

The dashboard builds on Lit, xterm.js, IBM Plex & JetBrains Mono, Heroicons, and
Simple Icons. Full attributions live in **[CREDITS.md](CREDITS.md)**. Product
names, logos, and trademarks belong to their respective owners.

## License

[MIT](LICENSE)
