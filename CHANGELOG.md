# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]





## [0.1.4] - 2026-06-03
## [0.1.3] - 2026-06-02
## [0.1.2] - 2026-06-01
## [0.1.1] - 2026-05-31
### Changed

- **python-telegram-bot** and **croniter** are now core dependencies — the
  Telegram gateway and `schedule: cron:...` workers work on a plain
  `pip install relaydeck` / `uv tool install relaydeck` with no extra step.
  The `[telegram]` and `[cron]` extras remain as no-op aliases for backward
  compatibility.
- **`scripts/install.sh`** next-steps now recommend `relaydeck daemon start`
  (background, survives terminal close) over `relaydeck serve` (foreground).

## [0.1.0] - 2026-05-31
### Added

- **Telegram control commands** — drive the routed agent from a chat:
  `/new` (`/clear`, `/fresh`, `/reset`) starts a fresh session, `/restart`
  restarts the PTY keeping history, `/screenshot` sends a snapshot of the live
  terminal (pyte render, the web-lens engine). Typing `/` shows the command
  menu (pushed via the Bot API `setMyCommands` on startup)
- One-shot **session intent** on `orchestrator.start_agent` (`fresh` / `resume`):
  strips or injects the harness's resume flag per respawn, derived from the
  per-type launch-option catalog (handles arg flags like `--continue` and config
  keys like codex `resume_last`) — no PTY/spawn code is touched.
  `reset_agent_session` now respawns clean; new `restart_agent` resumes
- Idle **reply-owed nudge** + operator view `relaydeck workspace inbox
  --awaiting-reply`: reminds an agent that ends its turn still owing a peer a
  durable reply, and lets an operator audit who owes whom
- Recommended plugin bundles (`plugins/bundle.toml`): `relaydeck plugin bundle`
  lists/inspects coverage and `relaydeck doctor` flags a missing default-bundle
  plugin
- Curated community plugin registry (`plugins/registry.yaml`): `relaydeck plugin
  search` surfaces recommended/pinned entries first and `relaydeck plugin install
  <name>` resolves a curated name to its pinned spec; new `curated` trust tier
  (bundled > curated > local > untrusted)
- **Release engineering for the public/PyPI launch**: a contribution gate
  (issue/PR auto-close + `lgtm`/`lgtmi` approval, adapted from pi), GitHub issue
  & PR templates, `CREDITS.md`, and a **Credits & Licenses** tab in the dashboard
  settings
- PyPI publish pipeline — `release.yml` (Trusted Publishing, triggered by a
  published GitHub Release so PyPI and the Release tag stay in lockstep),
  `version-bump.yml` + `scripts/bump_version.py`, and `docs/RELEASE.md`
  (release flow + GitHub hardening guide)

### Fixed

- Reply-owed detection no longer fires forever on channel senders (a Telegram
  reply routes back through the bot and never threads a row, so the message
  stayed "unanswered") and no longer loops on ack-of-ack exchanges — only an
  *initiating* message from a *registered fleet agent* owes a reply. The nudge
  ledger is pruned by resolved status, bounding memory to the live owed-set

### Changed

- Messaging skills: agents are told not to acknowledge acknowledgements, that
  the reply IS the tool call (don't re-echo the body as terminal prose), and to
  post milestone progress to a human gateway (Telegram) during long tasks

- **All official plugins consolidated into a single root `plugins/` package**
  (the 5 infra plugins extracted from core + the bundled extensions);
  `relaydeck/plugins/` and `relaydeck_plugins/` are gone. Core now imports zero
  plugins — the boundary is enforced by `tests/test_plugin_boundary.py`. New core
  facades: `relaydeck.automation` (action dispatcher + `parse_schedule`),
  `relaydeck.harness` (the harness PTY base), and a `SecretBackend` registry in
  `relaydeck.vault`. Single `relaydeck` wheel still ships everything
- Workspace git metadata is cached per path with a 30s TTL (above the
  12s dashboard heartbeat) to cut subprocess fan-out on `/api/workspaces`
  refreshes; cache key is case-folded on case-insensitive filesystems
  (APFS, NTFS); worktree create/remove explicitly busts the cache
- `workspace_git_info` reads from the batch result instead of recomputing the same path
- `enqueue_workspace_messages` accepts YAML-only agent specs not yet mirrored in SQLite
- Installer and docs now default to the published PyPI package
  (`uv tool install relaydeck`); `scripts/install.sh` re-pins the install source
  on re-run (so a git install can migrate to PyPI), and the primary domain is
  **relaydeck.ai** (`relaydeck.ai/install.sh`)
- CI, e2e, and docker workflows run on pull requests (the pre-merge gate) rather
  than on every push to `main`; CI also builds + `twine check`s the wheel/sdist

## [0.1.0] - 2026-05-26

### Added

- Local-first daemon with web dashboard, CLI, and HTTP API at parity
- Harness fleet for `pi`, `claude-code`, `codex-cli`, `cursor-cli`, and `opencode-cli`
- Durable agent-to-agent messaging with PTY injection and late drain
- Plugin system for harnesses, providers, messaging, skills, metering, and more
- Workspace-scoped configuration via `agent.toml` and YAML agent specs
- Semantic status, usage metering, GitHub event routing, and worktree support
- Read-only observation of external agents (Hermes, OpenClaw)

[0.1.0]: https://github.com/relaydeck/relaydeck/releases/tag/v0.1.0
