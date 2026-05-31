"""
Browser E2E: settings overlay, plugin enable/disable, workspace plugin
toggles, and per-harness new-agent onboarding.

Run headed with delay so an operator can follow along::

    RELAYDECK_E2E_HEADED=1 RELAYDECK_E2E_SLOWMO=600 RELAYDECK_E2E_PAUSE_MS=900 \\
      uv run pytest -m e2e tests/e2e/test_web_settings_onboarding_e2e.py -v

Headless (CI / batch)::

    uv run pytest -m e2e tests/e2e/test_web_settings_onboarding_e2e.py -q
"""

from __future__ import annotations

import shutil

import pytest

from ._webutil import (
    boot_page,
    errors,
    get_json,
    make_ws_dir,
    open_settings,
    patch_workspace_plugins,
    seed_workspace,
    set_input,
    viewer_pause,
)

pytestmark = pytest.mark.e2e

NATIVE_HARNESSES = (
    "pi",
    "claude-code",
    "codex-cli",
    "cursor-cli",
    "opencode-cli",
    "relaydeck",
)

_CLI = {
    "pi": "pi",
    "claude-code": "claude",
    "codex-cli": "codex",
    "cursor-cli": "cursor-agent",
    "opencode-cli": "opencode",
    "relaydeck": "pi",
}


def _boot_collecting(browser, base, lens="agents"):
    ctx = browser.new_context(viewport={"width": 1360, "height": 920})
    page = ctx.new_page()
    msgs = []
    page.on("console", lambda m: msgs.append(m))
    page.goto(f"{base}/?lens={lens}")
    page.wait_for_selector(".rail", timeout=15000)
    return ctx, page, msgs


def test_settings_all_sections_render(live_daemon, browser):
    """Every settings nav section must render without console errors."""
    ctx, page, msgs = _boot_collecting(browser, live_daemon)
    try:
        open_settings(page, "general")
        viewer_pause(page)
        page.wait_for_selector(".theme-card", timeout=8000)
        viewer_pause(page)

        page.click('.app-subtabs .seg-i[data-v="customize"]')
        page.wait_for_selector(".ap-pane, [data-tokens]", timeout=8000)
        viewer_pause(page)

        for sec in ("shortcuts", "plugins", "integrations", "auth", "vault", "about", "credits", "danger"):
            page.click(f'.settings-nav [data-s="{sec}"]')
            page.wait_for_timeout(300)
            viewer_pause(page)
            content = page.text_content(".settings-content") or ""
            assert content.strip() and "loading" not in content.lower()[:40], (
                f"settings section {sec!r} empty or stuck loading"
            )

        bad = errors(msgs)
        assert not bad, "console errors in settings walk:\n" + "\n".join(bad)
    finally:
        ctx.close()


def test_settings_shortcuts_toggle_roundtrip(live_daemon, browser):
    ctx, page = _boot_collecting(browser, live_daemon)[:2]
    try:
        open_settings(page, "shortcuts")
        viewer_pause(page)
        toggles = page.locator(".sc-tog:not(.locked)")
        assert toggles.count() >= 1, "expected at least one non-locked shortcut toggle"
        first = toggles.first
        was_on = "on" in (first.get_attribute("class") or "")
        first.click()
        viewer_pause(page)
        now_on = "on" in (first.get_attribute("class") or "")
        assert now_on != was_on, "shortcut toggle did not flip"
        first.click()
        viewer_pause(page)
        assert ("on" in (first.get_attribute("class") or "")) == was_on
    finally:
        ctx.close()


def test_settings_theme_switch_ink_and_back(live_daemon, browser):
    ctx, page = _boot_collecting(browser, live_daemon)[:2]
    try:
        open_settings(page, "general")
        page.wait_for_selector('.theme-card[data-theme="base"]', timeout=8000)
        viewer_pause(page)

        page.click('.theme-card[data-theme="ink"]')
        page.wait_for_function(
            """() => {
                const bg = getComputedStyle(document.documentElement)
                    .getPropertyValue('--bg-0').trim().toLowerCase();
                return bg && bg !== '#f2efe6';
            }""",
            timeout=8000,
        )
        ink_bg = page.evaluate(
            "() => getComputedStyle(document.documentElement).getPropertyValue('--bg-0').trim()"
        )
        viewer_pause(page)

        page.click('.theme-card[data-theme="base"]')
        page.wait_for_function(
            "() => getComputedStyle(document.documentElement).getPropertyValue('--bg-0').trim().toUpperCase() === '#F2EFE6'",
            timeout=8000,
        )
        viewer_pause(page)
        assert ink_bg.lower() != "#f2efe6", "ink theme should differ from base paper bg"
    finally:
        ctx.close()


def test_global_plugin_disable_enable_messaging(live_daemon, browser, tmp_path):
    """Daemon-wide disable drops the Messages rail slot; re-enable restores it."""
    seed_workspace(live_daemon, "msgws", make_ws_dir(tmp_path, "msgws"), ["messaging"])

    ctx, page = _boot_collecting(browser, live_daemon)[:2]
    try:
        titles_before = page.eval_on_selector_all(
            ".rail-btn", "els => els.map(e => e.title).filter(Boolean)"
        )
        assert "Messages" in titles_before
        viewer_pause(page)

        open_settings(page, "plugins")
        page.wait_for_selector('.set-toggle[data-name="messaging"]', timeout=8000)
        viewer_pause(page)
        page.click('.set-toggle[data-name="messaging"]')
        page.wait_for_function(
            """() => {
                const t = document.querySelector('.set-toggle[data-name="messaging"]');
                return t && !t.classList.contains('on');
            }""",
            timeout=8000,
        )
        viewer_pause(page)
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # Live UI manifest should drop Messages from the rail.
        page.wait_for_function(
            """() => ![...document.querySelectorAll('.rail-btn')]
                .some(b => b.title === 'Messages')""",
            timeout=10000,
        )
        viewer_pause(page)

        plugins = get_json(live_daemon, "/api/plugins")
        messaging = next(p for p in plugins if p["name"] == "messaging")
        assert messaging.get("enabled") is False

        open_settings(page, "plugins")
        page.click('.set-toggle[data-name="messaging"]')
        page.wait_for_function(
            """() => {
                const t = document.querySelector('.set-toggle[data-name="messaging"]');
                return t && t.classList.contains('on');
            }""",
            timeout=8000,
        )
        viewer_pause(page)
        page.keyboard.press("Escape")

        page.wait_for_function(
            """() => [...document.querySelectorAll('.rail-btn')]
                .some(b => b.title === 'Messages')""",
            timeout=10000,
        )
    finally:
        # Always re-enable so later tests / the operator daemon aren't left broken.
        try:
            from ._webutil import post_json
            post_json(live_daemon, "/api/plugins/messaging/enable", {})
        except Exception:
            pass
        ctx.close()


def test_workspace_plugin_toggle_messaging(live_daemon, browser, tmp_path):
    """Workspaces lens plugin cards toggle messaging for one workspace."""
    seed_workspace(live_daemon, "togws", make_ws_dir(tmp_path, "togws"), ["messaging", "skills"])

    ctx, page = boot_page(browser, live_daemon, lens="workspaces")
    try:
        page.click('.side-list .srow:has-text("togws")')
        page.wait_for_selector(".ws-plug-card", timeout=8000)
        viewer_pause(page)

        card = page.locator('.ws-plug-card:has(.wpc-name:text-is("messaging"))')
        toggle = card.locator("[data-pl-toggle]")
        assert "on" in (toggle.get_attribute("class") or "")
        toggle.click()
        viewer_pause(page)

        page.wait_for_function(
            """() => {
                const cards = [...document.querySelectorAll('.ws-plug-card')];
                const m = cards.find(c => c.querySelector('.wpc-name')?.textContent === 'messaging');
                return m && !m.querySelector('[data-pl-toggle]')?.classList.contains('on');
            }""",
            timeout=8000,
        )

        ws = next(w for w in get_json(live_daemon, "/api/workspaces") if w["name"] == "togws")
        assert "messaging" not in (ws.get("plugins") or [])

        toggle.click()
        viewer_pause(page)
        ws = next(w for w in get_json(live_daemon, "/api/workspaces") if w["name"] == "togws")
        assert "messaging" in (ws.get("plugins") or [])
    finally:
        ctx.close()


@pytest.mark.parametrize("htype", NATIVE_HARNESSES)
def test_harness_onboarding_modal_sections(live_daemon, browser, tmp_path, htype):
    """Each native harness exposes type-specific options in the new-agent wizard."""
    ws_name = f"onb-{htype.replace('-', '')[:8]}"
    seed_workspace(live_daemon, ws_name, make_ws_dir(tmp_path, ws_name), ["messaging", "skills"])

    ctx, page, msgs = _boot_collecting(browser, live_daemon, lens="agents")
    try:
        page.click('.hdr [data-act="new-agent"]')
        page.wait_for_selector(f'.na-card[data-type="{htype}"]', timeout=8000)
        viewer_pause(page)
        page.click(f'.na-card[data-type="{htype}"]')
        # Two-step wizard: clicking a harness card advances to its curated config
        # pane. Pin Guided so the deep per-harness options below stay in view
        # (Quick mode collapses them behind "Customize options").
        page.wait_for_selector('[data-pane="config"]:not([hidden])', timeout=5000)
        page.click('.na-pace-i[data-pace="guided"]')

        agent_id = f"a-{htype.replace('-', '')[:10]}"
        set_input(page, '.na-modal input[placeholder="pr-reviewer"]', agent_id)
        page.select_option(".na-modal select", value=ws_name)
        page.fill('.na-modal textarea[data-f="purpose"]', f"E2E probe for {htype}")
        viewer_pause(page)

        if htype == "relaydeck":
            page.wait_for_selector("[data-type-section] textarea", timeout=5000)
            section = page.text_content("[data-type-section]") or ""
            assert "Tool access" in section
            if shutil.which("pi") is None:
                assert "pi" in section.lower()
        elif htype in ("pi", "claude-code", "codex-cli", "cursor-cli", "opencode-cli"):
            # Harness-specific launch options panel (autonomy etc.)
            section = page.text_content("[data-type-section]") or ""
            assert section.strip(), f"{htype}: expected launch options section"

        page.wait_for_selector("[data-preamble]", timeout=8000)
        page.wait_for_function(
            "() => { const p = document.querySelector('[data-preamble]');"
            " return p && p.textContent && !/composing/i.test(p.textContent); }",
            timeout=8000,
        )
        viewer_pause(page)

        cli = _CLI.get(htype)
        spawn_disabled = page.is_disabled('.na-modal [data-act="spawn"]')
        if cli and shutil.which(cli) is None:
            assert spawn_disabled, f"{htype}: spawn must stay disabled when {cli} missing"
        else:
            assert not spawn_disabled, f"{htype}: spawn should enable when CLI present"

        bad = errors(msgs)
        assert not bad, f"console errors in harness modal ({htype}):\n" + "\n".join(bad)

        page.keyboard.press("Escape")
        viewer_pause(page)
    finally:
        ctx.close()


@pytest.mark.parametrize("htype", NATIVE_HARNESSES)
def test_harness_onboarding_spawn_reaches_running(live_daemon, browser, tmp_path, htype):
    """Full onboarding: modal spawn → running + terminal WS (when CLI on PATH)."""
    cli = _CLI.get(htype)
    if not cli or shutil.which(cli) is None:
        pytest.skip(f"{cli} not on PATH")

    ws_name = f"sp-{htype.replace('-', '')[:8]}"
    agent_id = f"s-{htype.replace('-', '')[:10]}"
    seed_workspace(live_daemon, ws_name, make_ws_dir(tmp_path, ws_name), ["messaging"])

    ctx, page = boot_page(browser, live_daemon, lens="agents")
    try:
        page.click('.hdr [data-act="new-agent"]')
        page.wait_for_selector(f'.na-card[data-type="{htype}"]', timeout=8000)
        viewer_pause(page)
        page.click(f'.na-card[data-type="{htype}"]')
        set_input(page, '.na-modal input[placeholder="pr-reviewer"]', agent_id)
        page.select_option(".na-modal select", value=ws_name)
        page.fill('.na-modal textarea[data-f="purpose"]', "spawn e2e")
        viewer_pause(page)
        page.click('.na-modal button:has-text("Spawn agent")')

        page.wait_for_selector(".xterm, .term-host", timeout=25000)
        viewer_pause(page)
        from ._webutil import wait_running
        status = wait_running(live_daemon, agent_id, timeout=45.0, label=htype)
        assert status == "running", f"{htype} spawn did not reach running ({status!r})"
        page.wait_for_function(
            "() => { const t = window.__relaydeckTerm;"
            " return !!(t && t.ws && t.ws.readyState === 1); }",
            timeout=15000,
        )
        viewer_pause(page)
    finally:
        ctx.close()
