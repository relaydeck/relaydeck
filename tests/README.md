# Test Suite Foundation

This suite should protect relaydeck's operating contracts, not freeze incidental implementation details.

Keep tests that cover:

- Agent/workspace source of truth: YAML specs, `agent.toml`, workspace registry reads/writes.
- CLI-to-daemon boundaries: live-state commands go through daemon HTTP and handle daemon errors separately from transport failures.
- Messaging foundations: durable enqueue/drain, readiness gates, reply routing, delivery states, inbox streams, and handover-style workflows.
- Plugin platform contracts: capability gates, plugin load/unload, workspace-scoped hosts, settings, workers, UI/API/CLI registration.
- Workers and durability: supervision, bus durability, DB maintenance, pruning, WAL/pool behavior.
- Harness contracts: command construction, environment isolation, submit semantics (`\r`), skill/runtime injection, usage/event parsing.
- Dev workflows: creating workspaces, starting/stopping agents, worktrees, GitHub issue/PR spawn, CLI cwd scoping.
- Security/ops foundations: auth, TLS CA pinning, vault secrecy, audit logs, install-script syntax.
- E2E smoke tests where subprocesses or harnesses are the contract.

Avoid tests that only pin:

- Exact CSS/HTML/static file inventories unless the route contract depends on them.
- Full generated shell/terminal scripts when a semantic assertion is enough.
- Plugin-specific dashboard defaults that are already covered by manifest parser tests.
- Duplicated one-case-per-status tests when a parametrized contract is clearer.
- Incidental ordering unless it is explicitly user-visible behavior.

Prefer adding a focused regression test next to the layer that owns the behavior. If a bug crosses layers, keep one integration test for the workflow and unit tests for the lower-level edge cases.

## E2E tests (`-m e2e`, opt-in)

`tests/e2e/` holds tests that spawn real subprocesses. They are **deselected by
default** (`addopts = -m "not e2e"`) so a plain `uv run pytest` stays fast and
hermetic; run them explicitly with `uv run pytest -m e2e` (a CLI `-m` overrides
the default). Each skips cleanly when its prerequisites are missing, so the
opt-in run is still green on a laptop without every tool.

- **Harness smoke** (`test_pi_smoke.py`, `test_relaydeck_native_smoke.py`):
  in-process, spawn a real harness / run a real model. Need **`pi`** on PATH +
  `OPENROUTER_API_KEY` (relaydeck-native runs pi, not an in-process gateway).
- **Web E2E** (`test_web_harness_e2e.py`, `test_web_relaydeck_native_e2e.py`,
  `test_web_regression_e2e.py`): drive the REAL dashboard in headless Chromium
  (Playwright) against a REAL daemon on an isolated `$HOME`. Setup:

  ```sh
  uv sync --group e2e
  uv run playwright install chromium
  uv run pytest -m e2e tests/e2e/      # all browser + harness e2e
  ```

  The `live_daemon` fixture (in `conftest.py`) starts `relaydeck serve` on an
  ephemeral port with an isolated `$HOME` — no pollution of `~/.relaydeck`, and
  the dashboard auto-auths over loopback. Shared helpers live in `_webutil.py`
  (loopback HTTP client + API seeding + dashboard-driving primitives).

  - `test_harness_integration_e2e.py` — skills/plugins/fleet-context injection
    (composition API + Identity tile + new-agent preview + per-harness delivery
    matrix + start/stop/restart lifecycle + integrations registry). Headless by
    default; `RELAYDECK_E2E_HEADED=1` or `RELAYDECK_E2E_SLOWMO>0` to watch.
  - `test_web_harness_e2e.py` — workspace init renders live; each of
    pi/claude-code/codex-cli/opencode-cli/cursor-cli/**relaydeck** spawns,
    auto-starts to `running`, mounts the terminal, connects the PTY WebSocket.
    Needs the harness CLIs on PATH (skips per harness; relaydeck-native needs
    `pi`).
  - `test_web_relaydeck_native_e2e.py` — live pi probes on
    `/api/harnesses` + `/api/plugins/relaydeck-native/status`; new-agent modal
    + agent-detail banner when pi is hidden from the daemon PATH
    (`live_daemon_no_pi` — no restart required after installing pi); spawn +
    PTY contract when pi is present.
  - `test_web_regression_e2e.py` — full-site UI net (NO harness binary needed;
    agents seeded as `pending` via the API): every lens renders without console
    errors, rail icons unique, sidebar search icon inside the input, command
    palette click navigates, back-to-home from agent detail, deep-link / section
    param handling, settings sections, new-agent modal, home widgets, workspace
    plugin-card layout, models sub-tab auto-select + search.

  These assert the *platform* contract relaydeck owns, not each CLI's own
  content (config/auth-dependent; the harness smoke tests cover a live model
  round-trip).
