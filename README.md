> [!WARNING]
> **Early beta release.** relaydeck is brand new and moving fast, so expect rough edges, breaking changes, and the occasional bug. Please [open an issue](https://github.com/relaydeck/relaydeck/issues) if something breaks, and contributions are always welcome.

<div align="center">

<h1>relaydeck</h1>

<h2>Orchestrate agents. Plug the world in.</h2>

<p>
<b>One local daemon, one dashboard, your entire fleet.</b>
<br>Run CLI coding agents in parallel: live terminals, durable peer-to-peer messaging,
<br>your choice of model providers, and a plugin stack you extend per workspace.
<br>No cloud account. No telemetry.
</p>

<p>
  <a href="https://pypi.org/project/relaydeck/"><img alt="PyPI" src="https://img.shields.io/pypi/v/relaydeck?style=flat-square&logo=pypi&logoColor=white&color=B7410E"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12+-B7410E?style=flat-square&logo=python&logoColor=white">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-141210?style=flat-square"></a>
  <a href="https://relaydeck.ai"><img alt="relaydeck.ai" src="https://img.shields.io/badge/relaydeck.ai-141210?style=flat-square&logo=safari&logoColor=white"></a>
</p>

<br>

<sub><b>HARNESSES</b></sub>

<p>
  <img alt="Claude Code" src="https://img.shields.io/badge/Claude_Code-141210?style=flat-square&logo=claude&logoColor=white">
  <img alt="Codex" src="https://img.shields.io/badge/Codex-141210?style=flat-square&logo=data:image%2Fsvg%2Bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0id2hpdGUiPjxwYXRoIGQ9Ik0yMi4yODE5IDkuODIxMWE1Ljk4NDcgNS45ODQ3IDAgMCAwLS41MTU3LTQuOTEwOCA2LjA0NjIgNi4wNDYyIDAgMCAwLTYuNTA5OC0yLjlBNi4wNjUxIDYuMDY1MSAwIDAgMCA0Ljk4MDcgNC4xODE4YTUuOTg0NyA1Ljk4NDcgMCAwIDAtMy45OTc3IDIuOSA2LjA0NjIgNi4wNDYyIDAgMCAwIC43NDI3IDcuMDk2NiA1Ljk4IDUuOTggMCAwIDAgLjUxMSA0LjkxMDcgNi4wNTEgNi4wNTEgMCAwIDAgNi41MTQ2IDIuOTAwMUE1Ljk4NDcgNS45ODQ3IDAgMCAwIDEzLjI1OTkgMjRhNi4wNTU3IDYuMDU1NyAwIDAgMCA1Ljc3MTgtNC4yMDU4IDUuOTg5NCA1Ljk4OTQgMCAwIDAgMy45OTc3LTIuOTAwMSA2LjA1NTcgNi4wNTU3IDAgMCAwLS43NDc1LTcuMDcyOXptLTkuMDIyIDEyLjYwODFhNC40NzU1IDQuNDc1NSAwIDAgMS0yLjg3NjQtMS4wNDA4bC4xNDE5LS4wODA0IDQuNzc4My0yLjc1ODJhLjc5NDguNzk0OCAwIDAgMCAuMzkyNy0uNjgxM3YtNi43MzY5bDIuMDIgMS4xNjg2YS4wNzEuMDcxIDAgMCAxIC4wMzguMDUydjUuNTgyNmE0LjUwNCA0LjUwNCAwIDAgMS00LjQ5NDUgNC40OTQ0em0tOS42NjA3LTQuMTI1NGE0LjQ3MDggNC40NzA4IDAgMCAxLS41MzQ2LTMuMDEzN2wuMTQyLjA4NTIgNC43ODMgMi43NTgyYS43NzEyLjc3MTIgMCAwIDAgLjc4MDYgMGw1Ljg0MjgtMy4zNjg1djIuMzMyNGEuMDgwNC4wODA0IDAgMCAxLS4wMzMyLjA2MTVMOS43NCAxOS45NTAyYTQuNDk5MiA0LjQ5OTIgMCAwIDEtNi4xNDA4LTEuNjQ2NHpNMi4zNDA4IDcuODk1NmE0LjQ4NSA0LjQ4NSAwIDAgMSAyLjM2NTUtMS45NzI4VjExLjZhLjc2NjQuNzY2NCAwIDAgMCAuMzg3OS42NzY1bDUuODE0NCAzLjM1NDMtMi4wMjAxIDEuMTY4NWEuMDc1Ny4wNzU3IDAgMCAxLS4wNzEgMGwtNC44MzAzLTIuNzg2NUE0LjUwNCA0LjUwNCAwIDAgMSAyLjM0MDggNy44NzJ6bTE2LjU5NjMgMy44NTU4TDEzLjEwMzggOC4zNjQgMTUuMTE5MiA3LjJhLjA3NTcuMDc1NyAwIDAgMSAuMDcxIDBsNC44MzAzIDIuNzkxM2E0LjQ5NDQgNC40OTQ0IDAgMCAxLS42NzY1IDguMTA0MnYtNS42NzcyYS43OS43OSAwIDAgMC0uNDA3LS42Njd6bTIuMDEwNy0zLjAyMzFsLS4xNDItLjA4NTItNC43NzM1LTIuNzgxOGEuNzc1OS43NzU5IDAgMCAwLS43ODU0IDBMOS40MDkgOS4yMjk3VjYuODk3NGEuMDY2Mi4wNjYyIDAgMCAxIC4wMjg0LS4wNjE1bDQuODMwMy0yLjc4NjZhNC40OTkyIDQuNDk5MiAwIDAgMSA2LjY4MDIgNC42NnpNOC4zMDY1IDEyLjg2M2wtMi4wMi0xLjE2MzhhLjA4MDQuMDgwNCAwIDAgMS0uMDM4LS4wNTY3VjYuMDc0MmE0LjQ5OTIgNC40OTkyIDAgMCAxIDcuMzc1Ny0zLjQ1MzdsLS4xNDIuMDgwNUw4LjcwNCA1LjQ1OWEuNzk0OC43OTQ4IDAgMCAwLS4zOTI3LjY4MTN6bTEuMDk3Ni0yLjM2NTRsMi42MDItMS40OTk4IDIuNjA2OSAxLjQ5OTh2Mi45OTk0bC0yLjU5NzQgMS40OTk3LTIuNjA2Ny0xLjQ5OTdaIi8%2BPC9zdmc%2B&logoColor=white">
  <img alt="Cursor" src="https://img.shields.io/badge/Cursor-141210?style=flat-square&logo=cursor&logoColor=white">
  <img alt="Antigravity" src="https://img.shields.io/badge/Antigravity-141210?style=flat-square&logo=googlegemini&logoColor=white">
  <img alt="OpenCode" src="https://img.shields.io/badge/OpenCode-141210?style=flat-square">
  <img alt="pi" src="https://img.shields.io/badge/pi-141210?style=flat-square">
  <img alt="relaydeck native" src="https://img.shields.io/badge/relaydeck_native-B7410E?style=flat-square">
</p>

<sub><b>PROVIDERS</b></sub>

<p>
  <img alt="Anthropic" src="https://img.shields.io/badge/Anthropic-141210?style=flat-square&logo=anthropic&logoColor=white">
  <img alt="OpenAI" src="https://img.shields.io/badge/OpenAI-141210?style=flat-square&logo=data:image%2Fsvg%2Bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0id2hpdGUiPjxwYXRoIGQ9Ik0yMi4yODE5IDkuODIxMWE1Ljk4NDcgNS45ODQ3IDAgMCAwLS41MTU3LTQuOTEwOCA2LjA0NjIgNi4wNDYyIDAgMCAwLTYuNTA5OC0yLjlBNi4wNjUxIDYuMDY1MSAwIDAgMCA0Ljk4MDcgNC4xODE4YTUuOTg0NyA1Ljk4NDcgMCAwIDAtMy45OTc3IDIuOSA2LjA0NjIgNi4wNDYyIDAgMCAwIC43NDI3IDcuMDk2NiA1Ljk4IDUuOTggMCAwIDAgLjUxMSA0LjkxMDcgNi4wNTEgNi4wNTEgMCAwIDAgNi41MTQ2IDIuOTAwMUE1Ljk4NDcgNS45ODQ3IDAgMCAwIDEzLjI1OTkgMjRhNi4wNTU3IDYuMDU1NyAwIDAgMCA1Ljc3MTgtNC4yMDU4IDUuOTg5NCA1Ljk4OTQgMCAwIDAgMy45OTc3LTIuOTAwMSA2LjA1NTcgNi4wNTU3IDAgMCAwLS43NDc1LTcuMDcyOXptLTkuMDIyIDEyLjYwODFhNC40NzU1IDQuNDc1NSAwIDAgMS0yLjg3NjQtMS4wNDA4bC4xNDE5LS4wODA0IDQuNzc4My0yLjc1ODJhLjc5NDguNzk0OCAwIDAgMCAuMzkyNy0uNjgxM3YtNi43MzY5bDIuMDIgMS4xNjg2YS4wNzEuMDcxIDAgMCAxIC4wMzguMDUydjUuNTgyNmE0LjUwNCA0LjUwNCAwIDAgMS00LjQ5NDUgNC40OTQ0em0tOS42NjA3LTQuMTI1NGE0LjQ3MDggNC40NzA4IDAgMCAxLS41MzQ2LTMuMDEzN2wuMTQyLjA4NTIgNC43ODMgMi43NTgyYS43NzEyLjc3MTIgMCAwIDAgLjc4MDYgMGw1Ljg0MjgtMy4zNjg1djIuMzMyNGEuMDgwNC4wODA0IDAgMCAxLS4wMzMyLjA2MTVMOS43NCAxOS45NTAyYTQuNDk5MiA0LjQ5OTIgMCAwIDEtNi4xNDA4LTEuNjQ2NHpNMi4zNDA4IDcuODk1NmE0LjQ4NSA0LjQ4NSAwIDAgMSAyLjM2NTUtMS45NzI4VjExLjZhLjc2NjQuNzY2NCAwIDAgMCAuMzg3OS42NzY1bDUuODE0NCAzLjM1NDMtMi4wMjAxIDEuMTY4NWEuMDc1Ny4wNzU3IDAgMCAxLS4wNzEgMGwtNC44MzAzLTIuNzg2NUE0LjUwNCA0LjUwNCAwIDAgMSAyLjM0MDggNy44NzJ6bTE2LjU5NjMgMy44NTU4TDEzLjEwMzggOC4zNjQgMTUuMTE5MiA3LjJhLjA3NTcuMDc1NyAwIDAgMSAuMDcxIDBsNC44MzAzIDIuNzkxM2E0LjQ5NDQgNC40OTQ0IDAgMCAxLS42NzY1IDguMTA0MnYtNS42NzcyYS43OS43OSAwIDAgMC0uNDA3LS42Njd6bTIuMDEwNy0zLjAyMzFsLS4xNDItLjA4NTItNC43NzM1LTIuNzgxOGEuNzc1OS43NzU5IDAgMCAwLS43ODU0IDBMOS40MDkgOS4yMjk3VjYuODk3NGEuMDY2Mi4wNjYyIDAgMCAxIC4wMjg0LS4wNjE1bDQuODMwMy0yLjc4NjZhNC40OTkyIDQuNDk5MiAwIDAgMSA2LjY4MDIgNC42NnpNOC4zMDY1IDEyLjg2M2wtMi4wMi0xLjE2MzhhLjA4MDQuMDgwNCAwIDAgMS0uMDM4LS4wNTY3VjYuMDc0MmE0LjQ5OTIgNC40OTkyIDAgMCAxIDcuMzc1Ny0zLjQ1MzdsLS4xNDIuMDgwNUw4LjcwNCA1LjQ1OWEuNzk0OC43OTQ4IDAgMCAwLS4zOTI3LjY4MTN6bTEuMDk3Ni0yLjM2NTRsMi42MDItMS40OTk4IDIuNjA2OSAxLjQ5OTh2Mi45OTk0bC0yLjU5NzQgMS40OTk3LTIuNjA2Ny0xLjQ5OTdaIi8%2BPC9zdmc%2B&logoColor=white">
  <img alt="Google Gemini" src="https://img.shields.io/badge/Gemini-141210?style=flat-square&logo=googlegemini&logoColor=white">
  <img alt="DeepSeek" src="https://img.shields.io/badge/DeepSeek-141210?style=flat-square&logo=deepseek&logoColor=white">
  <img alt="Mistral" src="https://img.shields.io/badge/Mistral-141210?style=flat-square&logo=mistralai&logoColor=white">
  <img alt="Ollama" src="https://img.shields.io/badge/Ollama-141210?style=flat-square&logo=ollama&logoColor=white">
  <img alt="OpenRouter" src="https://img.shields.io/badge/OpenRouter-141210?style=flat-square&logo=openrouter&logoColor=white">
  <img alt="Meta Llama" src="https://img.shields.io/badge/Llama-141210?style=flat-square&logo=meta&logoColor=white">
</p>

</div>

```sh
curl -fsSL https://relaydeck.ai/install.sh | sh
```

or with pip:

```sh
pip install relaydeck
```

then start the daemon (dashboard at `http://127.0.0.1:8765`):

```sh
relaydeck daemon start
```

---

## What it is

relaydeck is a **local-first fleet OS for CLI coding agents**. It wraps the real
vendor CLIs you already use (Claude Code, Codex, Cursor, OpenCode, pi), runs them
**unattended in PTYs**, and gives you one place to watch, message, and automate
them. Everything runs on your machine; state lives in `~/.relaydeck` and a local
SQLite db, and secrets stay in a vault on the daemon host.

|  |  |
|---|---|
| **Harness-native** | Drives real CLI agents, not a replacement model runtime |
| **Local-first** | State in `~/.relaydeck` + SQLite; secrets in the on-host vault |
| **Fleet-aware** | Agents discover peers by `purpose`/`tags`; durable peer messaging with late drain |
| **Observable** | Live PTY terminals, semantic status, usage metering, SSE; no polling |
| **CLI-first, full parity** | The CLI is the platform — every operation works from the shell; the HTTP API and both UIs are peers over the same daemon |
| **Web is a UI plugin** | The dashboard ships as the platform-level `dashboard` plugin; the built-in `relaydeck view` TUI needs no browser |
| **Plugin-extensible** | Harnesses, providers, messaging, skills, automations, *and the web UI itself*: all plugins |

---

## Works with every harness

Each harness maps your agent config (model, autonomy, system prompt) onto the
vendor CLI. Install at least one on your `$PATH`.

| Harness | CLI | Agent type | Notes |
|---------|-----|------------|-------|
| **Claude Code** | `claude` | `claude-code` | Permission modes, hook integrations |
| **Codex** | `codex` | `codex-cli` | Sandbox + approval translation |
| **Cursor** | `cursor-agent` | `cursor-cli` | Subscription auth; per-agent config dir |
| **OpenCode** | `opencode` | `opencode-cli` | Config-file instruction injection |
| **Antigravity** | `agy` | `antigravity` | Google-account auth; workspace-trust pre-seed |
| **pi** | `pi` | `pi` | Reference harness; system-prompt append |
| **relaydeck native** | `pi` | `relaydeck` | Fleet operator: pi plus extension tools (messaging, agents, dashboard) |

---

## Bring any provider

Bundled provider plugins for **Anthropic, OpenAI, OpenRouter, and Ollama**, plus
any **OpenAI-compatible** endpoint (Groq, Together, Fireworks, Cerebras, DeepSeek,
xAI, Mistral, vLLM, and more). Keys live in the vault, never in agent configs.

---

## Three first-class surfaces — CLI, TUI, web

relaydeck is **CLI-first**: the shell is the platform, and everything runs
against one local daemon. The two UIs are peers over the same HTTP/SSE API, not
a privileged layer — pick whichever fits.

| Surface | Command | When |
|---------|---------|------|
| **CLI** | `relaydeck …` (`rdk` alias) | The platform. Scripts, CI, agents, automation — full parity, nothing UI-only |
| **TUI command center** | `relaydeck view` | One terminal, no browser: workspaces sidebar, focused-agent PTY, Events/Messages/Tasks tabs + plugin tabs + a CLI console |
| **Web dashboard** | `open http://127.0.0.1:8765` | A rich browser UI — and itself the platform-level **`dashboard` plugin** (disable or replace it like any other) |

```
relaydeck view              # built-in TUI command center (no deps)
```

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

The web dashboard is a fleet home with live usage and worker pulse, plus a
per-agent lens (live PTY terminal, usage/cost tiles, and panels for identity,
context, events, config, inbox, compose). Both UIs read the same SSE stream the
CLI's `events tail -f` does — no polling, no privileged path.

---

## Quickstart

Requirements: Python 3.12+ and at least one harness CLI on your `$PATH`.

```sh
# One gesture: find-or-register this dir as a workspace, ensure the daemon,
# open the command center (TUI; --web opens the dashboard, --no-view for scripts).
relaydeck open .
```

…or do it step by step:

```sh
# Register this project as a workspace with messaging enabled.
relaydeck init . --plugin messaging

# Start the daemon (dashboard at http://127.0.0.1:8765).
relaydeck daemon start

# Create two agents with distinct roles.
relaydeck agent create planner --type claude-code \
  --purpose "Plan changes; do not implement"
relaydeck agent create coder --type codex-cli \
  --purpose "Implement scoped patches from planner specs"
relaydeck agent start planner coder

# Send work; agents discover peers via the identity preamble.
relaydeck workspace message --agent planner \
  "Plan: add --dry-run to relaydeck plugin uninstall. Keep it under 50 LOC."

# Observe
relaydeck view              # built-in TUI
open http://127.0.0.1:8765  # web dashboard (primary UI)
```

`relaydeck --help` documents every command. With the daemon running, the OpenAPI
docs live at `http://127.0.0.1:8765/docs`.

### Drive it from your agent — the `relaydeck` skill

relaydeck ships a publishable **agent skill** (named `relaydeck`) that teaches
any SKILL.md-capable harness (Claude Code, …) to install and drive the fleet.
Arm your local agent with one command, then just ask it to "review this repo
with three agents in parallel":

```sh
relaydeck skills install            # → ~/.claude/skills/relaydeck
relaydeck skills install --target both   # Claude + Codex skill roots
```

The skill is self-aware (it won't bootstrap a fleet-of-fleets from inside one)
and finds an existing relaydeck install. See **[skills/relaydeck/](skills/relaydeck/)**
and the **[CLI usage guide](docs/USAGE.md)**.

---

## Extend with plugins

Everything beyond the core runtime is a **`RelaydeckPlugin`**: discovered at
startup, capability-gated, and removable without forking the engine. A 30-plugin
bundle ships in the box.

| Plugin | What it adds |
|--------|--------------|
| **vault** | Secrets vault with a key-name-only API and CLI management |
| **github** | Poll `gh` for issues/PRs and route them through rules to agents |
| **telegram** | Drive an agent from a chat; auto-discover chats into a registry |
| **messaging** | Durable agent-to-agent inbox plus dashboard surfaces |
| **prompts** | Agents ask questions with tap-able choices, not blocking stdin |
| **hitl** | Human-in-the-loop escalation over pluggable channels |
| **metering** · **usage_limits** | Token/cost tiles; rolling session and weekly quotas with auto-pause |
| **skills** · **theme** · **dashboard** | Agent-authored skills, themes, and dashboard layouts |
| **file_watcher** · **gateway** · **loop** | Workspace file events, webhooks, scheduled and event-driven agents |

```sh
relaydeck plugin list
relaydeck plugin new my-plugin          # scaffold: harness | provider | skill
relaydeck plugin dev ./my-plugin        # editable install
relaydeck plugin verify ./my-plugin     # manifest + skill validation
```

Plugins register **CLI commands, HTTP routes, dashboard lenses/tiles, workers,
skills, and event subscriptions**; capabilities declared in `plugin.toml` gate SDK
access at runtime. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the authoring guide.

---

## Core concepts

| Concept | Description |
|---------|-------------|
| **Daemon** | One per machine: PTYs, workers, FastAPI, SSE, WebSocket terminals |
| **Workspace** | Registered project directory; plugins listed in `agent.toml` |
| **Agent** | Named harness instance (`~/.relaydeck/agents/<id>.yaml` is source of truth) |
| **Message** | Durable row in SQLite; injected to the PTY when live, drained on start |
| **Worktree workspace** | Parallel git checkout as a first-class workspace (branch per task) |
| **Plugin** | CLI + API + UI contributions declared in `plugin.toml` |

```
                    +---------------------------------------+
  CLI / scripts     |          relaydeck daemon             |     Web dashboard
  relaydeck ... --->|  orchestrator . plugins . event bus   |<---- localhost:8765
       HTTP/SSE     |         |              |              |      (live SSE)
                    |    harness PTYs    SQLite state       |
                    |   claude . codex . agents . msgs      |
                    |   cursor . pi  .   usage . events     |
                    +---------------------------------------+

  ~/.relaydeck/   agents/*.yaml   workspaces/*/agent.toml   vault.yaml   runtime/relaydeck.db
```

One install ships `relaydeck/` (engine + host contract) and `plugins/` (every
official plugin). Core imports zero plugins; plugin authors import public facades
only: `relaydeck.sdk`, `relaydeck.harness`, `relaydeck.provider`, and so on.

---

## More

<details>
<summary><b>Remote control via Telegram</b></summary>

<br>

Route a Telegram chat to an agent and drive the fleet from your phone. Slash
commands act on the routed agent without being forwarded into the harness, so
they behave uniformly across every harness type:

| Command | Effect |
|---------|--------|
| `/new` (`/clear`, `/fresh`, `/reset`) | Start a fresh session (drops the harness resume flag) |
| `/restart` | Restart the agent's PTY, keeping its history |
| `/screenshot` | Send a snapshot of the agent's live terminal |
| `/stop` · `/status` · `/help` | Stop, inspect, or list commands |

```sh
relaydeck telegram setup                  # store the bot token in the vault
relaydeck telegram routes-add \           # map a chat to (workspace, agent)
  --chat <chat-id> --workspace <ws> --agent <agent>
```

</details>

<details>
<summary><b>Observe external agent runtimes</b></summary>

<br>

relaydeck observes **Hermes Agent** and **OpenClaw** runtimes alongside the fleet
it manages: health and risk posture only, with no mutation or secret access.

```sh
relaydeck external detect ~/.hermes
relaydeck external add ~/.openclaw --probe
relaydeck external list
```

</details>

<details>
<summary><b>Development</b></summary>

<br>

```sh
git clone https://github.com/relaydeck/relaydeck.git && cd relaydeck
uv sync --group dev
uv run pytest -q -m "not e2e"     # fast CI-equivalent suite
uv run relaydeck plugin verify    # all bundled manifests
uv run ruff check relaydeck tests plugins
```

| Resource | |
|----------|---|
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Testing | [TESTING.md](TESTING.md) |
| Architecture notes | [AGENTS.md](AGENTS.md) |
| Security | [SECURITY.md](SECURITY.md) |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |

</details>

---

## Acknowledgements

relaydeck is **harness-native**: it wraps real CLI coding agents rather than
shipping its own model runtime. Huge thanks to the
**[pi](https://github.com/earendil-works/pi)** coding agent (relaydeck's reference
harness), and to Claude Code, Codex, Cursor, OpenCode, and Antigravity. We were
also inspired by ideas from across the open agent ecosystem, including Nous
Research's [Hermes Agent](https://github.com/NousResearch/hermes-agent) and
OpenClaw, which relaydeck can observe read-only.

The dashboard builds on Lit, xterm.js, IBM Plex & JetBrains Mono, Heroicons, and
Simple Icons. Full attributions live in **[CREDITS.md](CREDITS.md)**. Product
names, logos, and trademarks belong to their respective owners.

## License

[MIT](LICENSE)
