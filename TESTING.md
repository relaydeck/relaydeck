# Testing relaydeck

How the test suite is organized, how to run each layer, and the conventions to
follow when adding tests. The short version:

```sh
uv run pytest                       # the fast suite (e2e excluded)
uv run pytest -m e2e tests/e2e/     # opt-in end-to-end (browser + real harnesses)
scripts/test-install.sh             # clean-install verification in Docker
```

Everything ships with tests. Real I/O at the boundaries (real SQLite, a real
FastAPI `TestClient`, the Click runner, real subprocesses for harness tests) —
**no mocks at I/O boundaries**. See `tests/README.md` for what's worth pinning vs.
what's incidental.

---

## 1. The layers

| Layer | Where | Needs | Default run? |
|---|---|---|---|
| **Unit / integration** | `tests/*.py` (100 files) | nothing (tmp SQLite + `TestClient` + Click runner) | ✅ yes |
| **In-process e2e smokes** | `tests/e2e/test_pi_smoke.py`, `test_relaydeck_native_smoke.py` | a harness binary on PATH + `OPENROUTER_API_KEY` | ❌ `-m e2e` |
| **Browser e2e** | `tests/e2e/test_web_*.py` | `playwright` + chromium; some need harness CLIs | ❌ `-m e2e` |
| **Docker install** | `tests/install/` + `scripts/*install*.sh` | Docker | ❌ manual / CI |

The default `uv run pytest` runs **only the first layer** — `pyproject.toml` sets
`addopts = -m "not e2e"`, so a plain run stays fast and hermetic (no network, no
browser, no harness binaries). A CLI `-m e2e` overrides that to opt back in.

Current counts: **1525** default tests pass; **24** are e2e (deselected by default).

---

## 2. Running tests

### The fast suite (what you run constantly)
```sh
uv run pytest               # all non-e2e
uv run pytest -q            # quiet
uv run pytest -x            # stop at first failure
uv run pytest tests/test_messaging.py            # one file
uv run pytest -k "skill and injection"           # by name
uv run pytest -m "e2e or not e2e"                # absolutely everything (slow)
```

### End-to-end (`-m e2e`, opt-in)
One-time setup for the browser tests:
```sh
uv sync --group e2e                 # installs playwright (kept out of `dev`)
uv run playwright install chromium
```
Then:
```sh
uv run pytest -m e2e tests/e2e/                          # all e2e
uv run pytest -m e2e tests/e2e/test_web_regression_e2e.py   # just the UI net (no harness binary)
uv run pytest -m e2e "tests/e2e/test_web_harness_e2e.py::test_spawn_harness_renders_and_runs[pi]"
```

### Watch the browser e2e run (headed)
The suite uses its **own** `browser` fixture, so the pytest-playwright `--headed`
flag does **not** apply. Use env vars:
```sh
RELAYDECK_E2E_HEADED=1 RELAYDECK_E2E_SLOWMO=220 \
  uv run pytest -m e2e tests/e2e/test_web_regression_e2e.py
```
- `RELAYDECK_E2E_HEADED=1` — open a real Chrome window (one per test).
- `RELAYDECK_E2E_SLOWMO=<ms>` — pause before each action so it's followable.
- `PWDEBUG=1` — Playwright Inspector (headed + step-through), no fixture change.

Run **one test / one `[param]` at a time** when watching — headed mode opens a
fresh window per test.

> **Footgun:** spawning `codex` / `claude-code` under the test's isolated `$HOME`
> (no auth) triggers their **browser OAuth login** ("asking for openai password").
> Harness orphans used to survive teardown and keep retrying — the `live_daemon`
> fixture now reaps them, and the messaging test is gated (below). When running
> the harness-spawn tests headed, prefer `-k "pi or opencode"` (no browser login)
> unless you've authed codex/claude on the machine.

### Docker install verification
```sh
scripts/test-install.sh             # clean `uv tool install` (python:3.13-slim) + smoke
docker build -f tests/install/Dockerfile.ubuntu -t relaydeck-ubuntu . && docker run --rm relaydeck-ubuntu
```

---

## 3. The e2e suite in detail (`tests/e2e/`)

| File | What it covers | Needs |
|---|---|---|
| `conftest.py` | `live_daemon` (real `relaydeck serve` on an ephemeral port + isolated `$HOME`), `browser` (headless/headed Chromium), plus the in-process `isolated_home` / `pi_binary` / `openrouter_key` / `e2e_model` fixtures | playwright (browser tests) |
| `_webutil.py` | shared helpers: loopback HTTP client (bootstrap token), API seeding (`seed_workspace`/`seed_agent`/`start_agent`/`send_message`), dashboard drivers (`boot_page`/`set_input`/`add_workspace`), `errors()` console filter | — |
| `test_web_regression_e2e.py` | **16 UI regressions** — every lens renders w/o console errors, rail icons unique, search icon inside input, ⌘K click navigates, back-to-home, deep-link/section handling, settings sections, theme-card, new-agent modal, home widgets, workspace plugin-card, models sub-tabs. **No harness binary** (agents seeded `pending` via API). | browser |
| `test_web_harness_e2e.py` | workspace-init renders live; each of **pi / claude-code / codex-cli / opencode-cli** spawns, auto-starts, mounts the terminal, connects the PTY WebSocket | browser + harness CLIs (skips per-harness) |
| `test_cross_harness_messaging_e2e.py` | a sender messages one recipient per harness type; asserts the relay reaches each PTY (`injected`). **Opt-in** (below). | browser + harness CLIs |
| `test_pi_smoke.py` | in-process: spawn real `pi` → OpenRouter, send one prompt, assert an assistant message on the bus | `pi` + `OPENROUTER_API_KEY` |
| `test_relaydeck_native_smoke.py` | in-process: run a model through relaydeck's own gateway, assert a reply + persistence | `OPENROUTER_API_KEY` |

**Opt-in / safety gates** (env vars):
- `RELAYDECK_E2E_MESSAGING=1` — required to run `test_cross_harness_messaging_e2e` at all (it spawns real harnesses).
- `RELAYDECK_E2E_LOGIN_OK=1` — include the browser-OAuth harnesses (claude, codex) in the messaging test; off by default → pi + opencode only.
- `RELAYDECK_E2E_HEADED` / `RELAYDECK_E2E_SLOWMO` — headed viewing.
- `RELAYDECK_UPDATE_CMD` — override the self-update command (so `POST /api/update` / `relaydeck update` is testable without a real `uv tool install --reinstall relaydeck`).

**What e2e asserts vs. what it doesn't:** the browser tests assert the *platform*
contract relaydeck owns — workspace init, spawn → running, terminal mounts, PTY
WS connects, messaging *delivery* (`injected`). They do **not** assert a harness's
own content under the isolated `$HOME` (no model auth → opencode renders blank, a
relay submits-but-errors). A live model round-trip is the in-process smokes' job.

---

## 4. Docker install tests (`tests/install/`)

| File | Purpose |
|---|---|
| `Dockerfile` | clean `uv tool install /src` on `python:3.13-slim` |
| `Dockerfile.ubuntu` | runs the real `scripts/install.sh` on `ubuntu:24.04` (the "fresh server" case) |
| `scripts/install-smoke.sh` | boot daemon → `/healthz` + auth + `/api/agents` + `/api/version` + dashboard + `relaydeck doctor` |
| `scripts/install-behavior.sh` | the **no-harness** probe: confirms graceful degradation when no harness CLI is installed (doctor warns, `/api/harnesses` reports `cli_installed:false`, a start errors with "command not found", daemon stays healthy) |
| `scripts/test-install.sh` | local convenience: build + run the smoke |

These catch packaging / entry-point / first-boot breakage the unit suite can't.
`.dockerignore` keeps the build context lean.

---

## 5. Fixtures you should know

**`tests/conftest.py`** (suite-wide, autouse):
- Pins `RELAYDECK_AUTH_TOKEN` to a deterministic test token + monkeypatches
  `TestClient.__init__` to attach it — so tests do `TestClient(app)` without
  401s. Tests that verify the *no-token* path build an httpx client directly.
- `_flush_db_pools_after_test` closes the daemon-lifetime connection pool after
  each test so tmp DBs don't pin fds across the run.

**`tests/e2e/conftest.py`**:
- `live_daemon(tmp_path)` — spawns `relaydeck serve` on a free port with `HOME`
  set to a tmp dir (no pollution of `~/.relaydeck`), waits for `/healthz`, yields
  the base URL. Teardown stops agents gracefully then `pkill`s anything still
  referencing the test home (harnesses spawn in their own session and don't die
  with the daemon).
- `browser` — Chromium; honors `RELAYDECK_E2E_HEADED` / `RELAYDECK_E2E_SLOWMO`;
  skips cleanly if playwright / its browser is absent.
- `isolated_home`, `pi_binary`, `openrouter_key`, `e2e_model` — for the in-process
  smokes; each skips when its prerequisite is missing.

---

## 6. CI (`.github/workflows/`)

| Workflow | Job | Runs |
|---|---|---|
| `ci.yml` | **test** | `uv sync --frozen` → `uv run pytest -m "not e2e"` → `relaydeck plugin verify` → a daemon smoke (boot, 401-without-token, 200-with-token) |
| `ci.yml` | **lint** | `ruff check relaydeck tests --exit-zero` (soft — known style debt; not yet a gate) |
| `e2e.yml` | **pi-smoke** | installs `pi` via npm, runs `pytest -m e2e` against OpenRouter (`OPENROUTER_API_KEY` secret; skips on forks). push to main / PR / manual |
| `install.yml` | **clean install + smoke** | `docker build tests/install/Dockerfile` + `docker run` the smoke. push / PR |

Ruff is **soft in CI** (`--exit-zero`) today — the package carries pre-existing
style debt unrelated to current work; keep *new* files clean (run
`uv run ruff check <your-files>`).

---

## 7. Test map by concern

- **Harnesses**: `test_pi_harness`, `test_claude_code_harness`, `test_codex_harness`,
  `test_opencode_harness`, `test_harness_options`, `test_harness_plugin_registration`,
  `test_harness_pty`, `test_harness_pty_resize`, `test_harness_sdk`,
  `test_harness_injection_e2e`, `test_harness_skill_injection_matrix` (cross-harness
  skill/messaging injection contract), `test_system_prompt`.
- **Orchestrator / agents**: `test_orchestrator` (incl. the missing-CLI →
  `errored` regression), `test_semantic_status`, `test_agent_*`, `test_worker_*`.
- **Messaging**: `test_messaging`, `test_messaging_reliability` (`\r` submit,
  readiness gate, delivery state), `test_messaging_durability`, `test_inbox_stream`.
- **Plugins**: `test_plugin`, `test_plugin_platform`, `test_plugin_disable`,
  `test_plugin_trust_level`, `test_skills(_plugin)`, `test_telegram_plugin`,
  `test_github_plugin`, `test_external_agents_*`, `test_integrations` (vendor hooks).
- **Models / providers**: `test_model_roles`, `test_model_resolve`,
  `test_provider_config`, `test_providers`, `test_local_providers`,
  `test_model_invocations`, `test_metering_pricing`, `test_usage_limits`.
- **API / web**: `test_workspace_api`, `test_auth(_tokens)`, `test_tls`,
  `test_dashboard_redesign`, `test_static_cache_headers`, `test_home_widgets_manifest`,
  `test_version_check` (update check).
- **Persistence / ops**: `test_db(_pool/_maintenance)`, `test_bus_durability`,
  `test_audit`, `test_observability`, `test_vault_encryption`, `test_maintenance`,
  `test_backpressure`, `test_tasks`, `test_layouts`, `test_themes`.
- **CLI / install**: `test_status_cli`, `test_workers_cli`, `test_cli_cwd_scoping`,
  `test_install_script`, `test_daemon_lifecycle`, `test_attach`, `test_view(ers)`.

---

## 8. Conventions for new tests

- **Ship features with tests.** A new capability lands with its test in the same change.
- **No mocks at I/O boundaries** — use real SQLite, a real `TestClient`, the Click
  runner, real subprocesses. Inject seams (`_fetch`, `_which`, `_tcp`) for network.
- **Pin contracts, not incidentals** (see `tests/README.md`): assert behavior the
  operator depends on, not exact CSS/HTML or generated-script text.
- **Web/UI changes are verified in a real browser** via the Playwright MCP — see
  `docs/playwright.md` for selectors, gotchas, and the headed cheatsheet.
- **e2e tests skip cleanly** when their prerequisite (binary / key / browser) is
  missing, and never trigger an interactive login unattended.
- **Keep new files ruff-clean** even though CI's ruff is soft.
- **`<600 LOC` per file** soft cap.
