"""Regression tests for bugs found in the Lit-migration review pass.

These lock in fixes that the broader render/mount smoke tests don't exercise —
caret preservation in a controlled input, and the imperative detail pane
catching up after its live collections load. Both reproduce the original bug if
their fix is reverted.

Run: uv run pytest -m e2e tests/e2e/test_web_lit_regressions_e2e.py
"""
from __future__ import annotations

import pytest

from ._webutil import boot_page, make_ws_dir, open_settings, post_json, seed_workspace

pytestmark = pytest.mark.e2e


def test_addworkspace_name_input_preserves_caret_midstring(live_daemon, browser, tmp_path):
    """The add-workspace NAME input must not reset the caret to the end on each
    keystroke. It bound `.value=${state.name}` and ran a full paint() per
    keystroke; without lit's live() directive the property re-commit yanked the
    caret to the end, so mid-string typing scrambled the value.
    """
    seed_workspace(live_daemon, "seed-ws", make_ws_dir(tmp_path, "seed-ws"))
    ctx, page = boot_page(browser, live_daemon, lens="workspaces")
    try:
        page.click('.side [data-act="new"]')
        page.wait_for_selector('input[placeholder="workspace-name"]', timeout=5000)
        # Wait for the async directory listing to settle — it rewrites the path
        # input (and derives the name) once, which would otherwise clobber us.
        page.wait_for_function(
            "() => { const el = document.querySelector('[data-current]');"
            " return el && !/loading/i.test(el.textContent); }",
            timeout=5000,
        )
        name = page.locator('input[placeholder="workspace-name"]')
        name.fill("")            # clear + mark the field user-touched
        page.keyboard.type("abc")
        page.keyboard.press("Home")
        page.keyboard.type("XY")
        # Fixed: caret stays after each insert → "XYabc".
        # Bug: caret jumps to end after the first insert → "XabcY".
        assert name.input_value() == "XYabc"
    finally:
        ctx.close()


def test_newagent_relaydeck_soul_prefilled_with_default(live_daemon, browser, tmp_path):
    """The relaydeck-native wizard must pre-fill the persona/soul textarea with a
    sensible default (saved as config.soul on spawn unless cleared). The
    migration initialized relaydeckSoul to '' and never seeded it, so native
    agents spawned with no persona. Reproduces if the SUGGESTED_SOUL seed is
    removed (the textarea is empty).
    """
    seed_workspace(live_daemon, "soul-ws", make_ws_dir(tmp_path, "soul-ws"))
    ctx, page = boot_page(browser, live_daemon, lens="agents")
    try:
        page.click('.side [data-act="new"]')
        page.wait_for_selector('.na-card[data-type="relaydeck"]', timeout=8000)
        page.click('.na-card[data-type="relaydeck"]')
        soul = page.locator('[data-f="relaydeck_soul"]')
        soul.wait_for(timeout=6000)
        assert "operator for this workspace" in soul.input_value()
    finally:
        ctx.close()


def test_workers_sidebar_refreshes_promptly_after_delete(live_daemon, browser, tmp_path):
    """Deleting a worker must drop it from the (reactive, cached) sidebar well
    before the 12s heartbeat. The sidebar reads use('/api/automations'), but
    agent.deleted only invalidates /api/agents — without an explicit
    live.invalidate('/api/automations') the row lingers ~12s. Reproduces the
    bug if that invalidate is removed (the row survives past the 5s window).
    """
    seed_workspace(live_daemon, "wk-refresh", make_ws_dir(tmp_path, "wk-refresh"))
    post_json(live_daemon, "/api/agents", {
        "id": "e2e-refresh", "name": "e2e-refresh", "type": "loop",
        "workspace": "wk-refresh", "auto_start": False,
        "config": {"schedule": "interval:30s",
                   "actions": [{"code": {"lang": "python", "body": "pass"}}]},
    })
    ctx, page = boot_page(browser, live_daemon, lens="workers")
    page.on("dialog", lambda d: d.accept())   # native confirm() on delete
    try:
        page.wait_for_selector(".wk-side-row.cfg", timeout=10000)   # worker in sidebar
        page.click('[data-act="delete"]')
        # Sidebar must drop the row in well under the 12s heartbeat.
        page.wait_for_function(
            "() => document.querySelectorAll('.wk-side-row.cfg').length === 0",
            timeout=5000)
    finally:
        ctx.close()


def test_workers_detail_autoselects_after_collections_load(live_daemon, browser, tmp_path):
    """The workers DETAIL pane must auto-render the first worker once its live
    collections load. The detail is imperative; when /api/automations resolved
    after the detail first mounted (the reactive sidebar path repaints only the
    sidebar), it used to stay on the 'No workers' empty state until a click.
    """
    seed_workspace(live_daemon, "wk-auto", make_ws_dir(tmp_path, "wk-auto"))
    post_json(live_daemon, "/api/agents", {
        "id": "e2e-autosel", "name": "e2e-autosel", "type": "loop",
        "workspace": "wk-auto", "auto_start": False,
        "config": {"schedule": "interval:30s",
                   "actions": [{"code": {"lang": "python", "body": "pass"}}]},
    })
    ctx, page = boot_page(browser, live_daemon, lens="workers")
    try:
        page.wait_for_selector(".wk-side-section", timeout=10000)
        # The configurable-worker detail header must appear WITHOUT a click —
        # i.e. the post-load auto-select rebuilt the imperative detail pane.
        page.wait_for_selector(".cwk-hdr", timeout=6000)
    finally:
        ctx.close()


def test_appearance_token_editor_survives_themes_sidebar_refresh(live_daemon, browser, tmp_path):
    """Editing a theme token must not be clobbered when /api/themes refreshes.
    The themes live subscription repaints the sidebar only (requestSidebarUpdate);
    without that split, the 12s heartbeat requestUpdate() repainted the detail
    mid-edit and yanked the token-editor caret.
    """
    seed_workspace(live_daemon, "ap-ws", make_ws_dir(tmp_path, "ap-ws"))
    ctx, page = boot_page(browser, live_daemon, lens="agents")
    try:
        open_settings(page, "general")
        page.click('.app-subtabs .seg-i[data-v="customize"]')
        page.wait_for_selector(".ap-pane", timeout=10000)
        page.wait_for_selector(".ap-tok-input", timeout=8000)
        tok = page.locator(".ap-tok-input").first
        tok.fill("")
        page.keyboard.type("abc")
        page.keyboard.press("Home")
        page.keyboard.type("XY")
        # Simulate the /api/themes heartbeat / themes.changed invalidation.
        page.evaluate("""async () => {
          const { live } = await import('@relaydeck/ui');
          live.invalidate('/api/themes');
        }""")
        page.wait_for_timeout(300)
        assert tok.input_value() == "XYabc"
    finally:
        ctx.close()


def test_models_provider_baseurl_preserves_caret_on_detail_refresh(live_daemon, browser, tmp_path):
    """The provider base-URL field must keep mid-string edits when the detail
    repaints (e.g. a /api/presets live push calls requestUpdate()). Uses
    liveDirective + a draft map; without them the bound .value re-commit yanks
    the caret to the end.
    """
    seed_workspace(live_daemon, "mdl-ws", make_ws_dir(tmp_path, "mdl-ws"))
    ctx, page = boot_page(browser, live_daemon, lens="models")
    try:
        page.wait_for_selector(".mdl-tab", timeout=10000)
        page.click('.mdl-tab[data-tab="providers"]')
        page.wait_for_selector(".side-list .mdl-row", timeout=8000)
        page.locator(".side-list .mdl-row").first.click()
        page.wait_for_selector("[data-baseurl]", timeout=8000)
        url = page.locator("[data-baseurl]")
        url.fill("")
        page.keyboard.type("abc")
        page.keyboard.press("Home")
        page.keyboard.type("XY")
        # Presets heartbeat repaints the detail; providers heartbeat is sidebar-only.
        page.evaluate("""async () => {
          const { live } = await import('@relaydeck/ui');
          live.invalidate('/api/presets');
        }""")
        page.wait_for_timeout(300)
        assert url.input_value() == "XYabc"
    finally:
        ctx.close()
