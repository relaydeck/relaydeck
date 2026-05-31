"""
Browser E2E: a fresh workspace + the six shipped PTY harnesses, driven
through the REAL dashboard in Chromium (Playwright) against a REAL daemon.

The "first test" an operator asks for after a clean install: every harness
must INITIALIZE, RENDER, and be USABLE. Concretely, per harness:

  - the new-agent modal CREATES the agent AND auto-starts it ("Spawn" =
    create + start -- regression: the modal used to leave it ``pending``),
  - the agent reaches ``running``,
  - the terminal tile MOUNTS (xterm) in the detail pane,
  - the PTY WebSocket connects, so operator keystrokes would reach the child.

Plus the workspace-init regression: a workspace added through the modal
appears in the Workspaces lens sidebar LIVE, no reload.

Runs only under ``-m e2e``. Needs the harness CLIs on PATH (skips per
harness otherwise) and the browser deps:

    uv sync --group e2e
    uv run playwright install chromium
    uv run pytest -m e2e tests/e2e/test_web_harness_e2e.py

``$HOME`` is isolated by the ``live_daemon`` fixture, so NO model auth is
required (harnesses render their startup TUI before any LLM call) and the
operator's ~/.relaydeck is never touched.
"""

from __future__ import annotations

import shutil
import time

import pytest

from ._webutil import (
    add_workspace,
    boot_page,
    get_text,
    make_ws_dir,
    set_input,
    sidebar_has,
    wait_running,
)

pytestmark = pytest.mark.e2e

# relaydeck harness `type` → the CLI binary that must be on PATH.
_CLI = {
    "pi": "pi",
    "claude-code": "claude",
    "codex-cli": "codex",
    "cursor-cli": "cursor-agent",
    "opencode-cli": "opencode",
    "relaydeck": "pi",
}


def test_fresh_workspace_appears_live(live_daemon, browser, tmp_path):
    """A workspace registered through the modal shows in the Workspaces lens
    sidebar immediately (no reload) and is registered server-side with the
    Recommended plugin set."""
    ws = make_ws_dir(tmp_path, "e2e-ws-live")
    ctx, page = boot_page(browser, live_daemon, lens="workspaces")
    try:
        page.wait_for_selector(".side-list", timeout=5000)
        add_workspace(page, ws, "e2e-ws-live", recommended=True)

        sidebar_has(page, "e2e-ws-live")  # live, no reload

        from ._webutil import get_json
        wss = get_json(live_daemon, "/api/workspaces")
        match = next((w for w in wss if w["name"] == "e2e-ws-live"), None)
        assert match is not None, f"workspace not registered server-side: {wss}"
        assert "messaging" in (match.get("plugins") or []), match
    finally:
        ctx.close()


def test_native_model_field_optin_picker(live_daemon, browser, tmp_path):
    """Native-harness model field defaults to a free-text override; an opt-in
    toggle reveals the standard picker, which writes a RESOLVED BARE id (the
    provider prefix stripped) back into the same field.

    Locks the picker-vs-text decision: native account namespaces (claude/codex/
    cursor) don't take relaydeck presets, so the text input stays the source of
    truth and the picker is an optional assist. No harness CLI needed — the
    modal renders the field without spawning."""
    ws_name = "e2e-native-picker"
    ws = make_ws_dir(tmp_path, ws_name)
    ctx, page = boot_page(browser, live_daemon, lens="workspaces")
    try:
        page.wait_for_selector(".side-list", timeout=5000)
        add_workspace(page, ws, ws_name, recommended=False)
        sidebar_has(page, ws_name)

        page.click('.hdr [data-act="new-agent"]')
        page.wait_for_selector('.na-card[data-type="claude-code"]', timeout=5000)
        page.click('.na-card[data-type="claude-code"]')

        # Default: type-in field present; picker hidden but offered.
        page.wait_for_selector('[data-f="harness_model"]', timeout=5000)
        assert page.is_visible('[data-pick-toggle]'), "opt-in picker toggle missing"
        assert page.eval_on_selector(
            "[data-native-picker]", "el => el.style.display === 'none'"
        ), "native picker should be hidden by default"

        # Opt in -> the standard picker mounts (custom provider/model field).
        page.click('[data-pick-toggle]')
        page.wait_for_selector('[data-native-picker] [data-ms-custom]', state="visible", timeout=5000)

        # Picking a provider/model writes the BARE id into the same field —
        # `deepseek/deepseek-chat` becomes `deepseek-chat` (what the harness wants).
        set_input(page, '[data-native-picker] [data-ms-custom]', "deepseek/deepseek-chat")
        page.wait_for_function(
            "() => document.querySelector('[data-f=\"harness_model\"]').value === 'deepseek-chat'",
            timeout=8000,
        )
    finally:
        ctx.close()


@pytest.mark.parametrize("htype", list(_CLI))
def test_spawn_harness_renders_and_runs(live_daemon, browser, tmp_path, htype):
    """Spawn a harness through the new-agent modal and assert it auto-starts,
    reaches running, mounts the terminal, and connects the PTY WebSocket."""
    cli = _CLI[htype]
    if shutil.which(cli) is None:
        pytest.skip(f"{cli} not on PATH -- install it to exercise the {htype} harness")

    ws_name = f"e2e-{htype}"
    agent_id = f"a-{htype}"  # valid id: lowercase + dashes, starts with a letter
    ws = make_ws_dir(tmp_path, ws_name)

    ctx, page = boot_page(browser, live_daemon, lens="workspaces")
    try:
        page.wait_for_selector(".side-list", timeout=5000)
        add_workspace(page, ws, ws_name, recommended=False)
        sidebar_has(page, ws_name)

        # Open the new-agent modal, pick the harness, name it, spawn.
        page.click('.hdr [data-act="new-agent"]')
        page.wait_for_selector(f'.na-card[data-type="{htype}"]', timeout=5000)
        page.click(f'.na-card[data-type="{htype}"]')
        set_input(page, '.na-modal input[placeholder="pr-reviewer"]', agent_id)
        page.select_option(".na-modal select", value=ws_name)  # first select = workspace
        page.click('.na-modal button:has-text("Spawn agent")')

        # The modal navigates to the agent detail; the terminal tile mounts.
        page.wait_for_selector(".xterm, .term-host", timeout=20000)

        # INITIALIZE: reaches running -- proof "Spawn" created AND started it.
        status = wait_running(live_daemon, agent_id, timeout=45.0)
        assert status == "running", (
            f"{htype} agent {agent_id!r} did not reach running (status={status!r})"
        )

        # RENDER + USABLE: terminal mounted + PTY WebSocket connected. This is
        # the platform contract relaydeck owns for EVERY harness; each CLI's
        # own content depends on its config/auth (isolated $HOME omits it --
        # pi/claude/codex still paint a banner, opencode stays quiet without a
        # provider; a live model round-trip is the dedicated smoke tests).
        page.wait_for_function(
            "() => { const t = window.__relaydeckTerm;"
            " return !!(t && t.ws && t.ws.readyState === 1); }",
            timeout=15000,
        )

        # Bonus signal (not asserted): how much the CLI painted.
        screen = ""
        for _ in range(8):
            screen = get_text(live_daemon, f"/api/agents/{agent_id}/screen?cols=120&rows=40")
            if screen.strip():
                break
            time.sleep(0.5)
        print(f"[{htype}] running, ws-connected; PTY painted {len(screen.strip())} chars")
    finally:
        ctx.close()
