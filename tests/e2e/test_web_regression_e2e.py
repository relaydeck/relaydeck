"""
Browser E2E: full-site regression net for the dashboard, driven in headless
Chromium (Playwright) against a REAL daemon on an isolated $HOME.

These pin the contracts that broke (and were fixed) while building out the
Studio dashboard, plus baseline "every surface renders" health. They need
NO harness binary or model auth -- agents are seeded as ``pending`` via the
API, which still renders the detail pane / tabs / terminal empty-state. So
this file exercises the whole UI cheaply and deterministically.

Run: ``uv sync --group e2e && uv run playwright install chromium`` then
``uv run pytest -m e2e tests/e2e/test_web_regression_e2e.py``. Deselected
from the default suite (``addopts = -m "not e2e"``).
"""

from __future__ import annotations

import pytest

from ._webutil import (
    boot_page,
    errors,
    make_ws_dir,
    put_json,
    seed_agent,
    seed_workspace,
    set_input,
)

pytestmark = pytest.mark.e2e


def _boot_collecting(browser, base, lens="agents"):
    """Like boot_page but attaches a console listener BEFORE navigation so we
    capture every console error the page emits."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    msgs = []
    page.on("console", lambda m: msgs.append(m))
    page.goto(f"{base}/?lens={lens}")
    page.wait_for_selector(".rail", timeout=15000)
    return ctx, page, msgs


# ── baseline health: every lens renders, no console errors ───────────────
def test_every_lens_renders_without_console_errors(live_daemon, browser, tmp_path):
    """Visit every rail lens; each must activate and emit no console errors.
    A messaging workspace is seeded so the Messages lens is present."""
    seed_workspace(live_daemon, "rws", make_ws_dir(tmp_path, "rws"), ["messaging", "skills"])
    ctx, page, msgs = _boot_collecting(browser, live_daemon, "agents")
    try:
        titles = page.eval_on_selector_all(
            ".rail-btn", "els => els.map(e => e.title).filter(Boolean)"
        )
        # The Settings rail slot opens an overlay, not a lens — skip it here
        # (covered by test_settings_overlay_sections).
        lens_titles = [t for t in titles if not t.startswith("Settings")]
        assert "Messages" in lens_titles, f"Messages lens missing: {lens_titles}"

        for t in lens_titles:
            page.click(f'.rail-btn[title="{t}"]')
            page.wait_for_timeout(700)  # let plugin lenses load their panel
            active = page.eval_on_selector(
                f'.rail-btn[title="{t}"]', "e => e.classList.contains('on')"
            )
            assert active, f"rail did not activate for {t!r}"

        bad = errors(msgs)
        assert not bad, f"console errors while visiting {lens_titles}:\n" + "\n".join(bad)
    finally:
        ctx.close()


def test_github_workspace_sidebar_opens_visual_rule_builder(live_daemon, browser, tmp_path):
    ws = seed_workspace(live_daemon, "gh-e2e", make_ws_dir(tmp_path, "gh-e2e"))
    resp = put_json(
        live_daemon,
        "/api/plugins/github/config",
        {
            "workspace": ws,
            "structured": {
                "repo": "octocat/Hello-World",
                "poll_interval_s": 60,
                "rules": [
                    {
                        "name": "pr-opened",
                        "when": {"event": "PullRequestEvent", "action": "opened"},
                        "do": [
                            {
                                "agent.message": {
                                    "to": "reviewer",
                                    "body": "Review PR #{{ pull_request.number }}",
                                }
                            }
                        ],
                    }
                ],
            },
        },
    )
    assert resp.get("ok") is True, resp

    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    msgs = []
    page.on("console", lambda m: msgs.append(m))
    try:
        page.goto(f"{live_daemon}/?lens=github")
        page.wait_for_selector(".gh-wrap", timeout=15000)
        page.wait_for_selector('.pl-nav-item:has-text("gh-e2e")', timeout=10000)
        page.click('.pl-nav-item:has-text("gh-e2e")')
        page.wait_for_selector(".gh-workspace-detail", timeout=10000)
        page.wait_for_selector('.gh-editor input[placeholder="owner/repo"]', timeout=10000)
        page.wait_for_selector(".gh-flow", timeout=10000)

        active = page.eval_on_selector(
            '.pl-nav-item:has-text("gh-e2e")',
            "e => e.classList.contains('active')",
        )
        assert active
        assert "PullRequestEvent" in page.inner_text(".gh-flow")
        page.wait_for_selector(".gh-vars", timeout=10000)
        assert "pull_request.number" in page.inner_text(".gh-vars")
        page.click(".gh-act textarea")
        page.click('.gh-var:has-text("pull_request.title")')
        body = page.eval_on_selector(".gh-act textarea", "e => e.value")
        assert "{{ pull_request.title }}" in body
        action_types = page.eval_on_selector_all(
            ".gh-act .ah select option",
            "els => els.map(e => e.value)",
        )
        assert {"agent.message", "model", "code", "bus.emit"}.issubset(set(action_types))
        bad = errors(msgs)
        assert not bad, "console errors on GitHub workspace rule builder:\n" + "\n".join(bad)
    finally:
        ctx.close()


# ── rail: icons must be unique (no two lenses share a glyph) ─────────────
def test_rail_icons_unique(live_daemon, browser):
    ctx, page = boot_page(browser, live_daemon)
    try:
        paths = page.eval_on_selector_all(
            ".rail-btn",
            """els => els.map(b => {
                const p = b.querySelector('svg path');
                return (p && p.getAttribute('d')) || (b.querySelector('svg')||{}).innerHTML || '';
            })""",
        )
        dupes = sorted({p for p in paths if paths.count(p) > 1})
        assert not dupes, f"duplicate rail icon paths: {dupes}"
    finally:
        ctx.close()


# ── sidebar search: magnifier sits INSIDE the input (not above it) ───────
@pytest.mark.parametrize("lens", ["agents", "models", "workers", "workspaces"])
def test_sidebar_search_icon_inside_input(live_daemon, browser, lens):
    ctx, page = boot_page(browser, live_daemon, lens=lens)
    try:
        page.wait_for_selector(".side-search input", timeout=8000)
        geom = page.eval_on_selector(
            ".side-search",
            """ss => {
                const svg = ss.querySelector('svg'), inp = ss.querySelector('input');
                const s = svg.getBoundingClientRect(), i = inp.getBoundingClientRect();
                return {
                    inside_x: s.left >= i.left - 1 && s.right <= i.right + 1,
                    centered_y: Math.abs((s.top+s.bottom)/2 - (i.top+i.bottom)/2) < 4,
                };
            }""",
        )
        assert geom["inside_x"] and geom["centered_y"], (
            f"{lens}: search icon not inside input ({geom})"
        )
    finally:
        ctx.close()


# ── command palette: clicking a result navigates (the mouseenter bug) ────
def test_command_palette_click_navigates(live_daemon, browser, tmp_path):
    seed_workspace(live_daemon, "cws", make_ws_dir(tmp_path, "cws"))
    seed_agent(live_daemon, "navtarget", "pi", "cws")
    ctx, page = boot_page(browser, live_daemon)
    try:
        page.click(".hdr-search")
        page.wait_for_selector(".cmdk-input", timeout=5000)
        set_input(page, ".cmdk-input", "navtarget")
        page.wait_for_selector('.cmdk-item:has-text("navtarget")', timeout=5000)
        item = page.locator('.cmdk-item:has-text("navtarget")').first
        item.hover()      # re-rendering on hover used to detach the node mid-click
        item.click()
        page.wait_for_function(
            "() => new URL(location.href).searchParams.get('agent') === 'navtarget'",
            timeout=8000,
        )
    finally:
        ctx.close()


# ── back to the fleet/home dashboard from an agent detail ────────────────
def test_go_home_from_agent_detail(live_daemon, browser, tmp_path):
    seed_workspace(live_daemon, "hws", make_ws_dir(tmp_path, "hws"))
    seed_agent(live_daemon, "hometest", "pi", "hws")
    ctx, page = boot_page(browser, live_daemon)
    try:
        # Deep-link straight into the agent detail.
        page.goto(f"{live_daemon}/?workspace=hws&agent=hometest")
        page.wait_for_selector(".dhdr-back", timeout=10000)

        # (1) the "fleet" breadcrumb clears the selection → home dashboard.
        page.click(".dhdr-back")
        page.wait_for_function(
            "() => !new URL(location.href).searchParams.get('agent')", timeout=8000
        )
        page.wait_for_selector(".home", timeout=8000)

        # (2) re-selecting then clicking the Agents rail also returns home.
        page.click(".side-list .srow")
        page.wait_for_function(
            "() => new URL(location.href).searchParams.get('agent') === 'hometest'",
            timeout=8000,
        )
        page.click('.rail-btn[title="Agents"]')
        page.wait_for_function(
            "() => !new URL(location.href).searchParams.get('agent')", timeout=8000
        )
    finally:
        ctx.close()


# ── deep-link nits: shorthand lens id resolves; section doesn't leak ─────
def test_lens_shorthand_deeplink_resolves(live_daemon, browser):
    # ?lens=skills must map to the namespaced skills:skills lens.
    ctx, page = boot_page(browser, live_daemon, lens="skills")
    try:
        page.wait_for_function(
            """() => { const b = document.querySelector('.rail-btn.on');
                       return b && b.title === 'Skills'; }""",
            timeout=8000,
        )
    finally:
        ctx.close()


def test_section_param_does_not_leak_across_lenses(live_daemon, browser):
    ctx, page = boot_page(browser, live_daemon)
    try:
        # Telegram has sidebar sections → sets ?section=. Switch to External
        # (no sections) and the stale section param must be dropped.
        page.click('.rail-btn[title="Telegram"]')
        page.wait_for_function(
            "() => /section=/.test(location.search)", timeout=8000
        )
        page.click('.rail-btn[title="External"]')
        page.wait_for_function(
            "() => location.search.includes('external') && !/section=/.test(location.search)",
            timeout=8000,
        )
    finally:
        ctx.close()


# ── settings overlay: opens + sections switch without errors ─────────────
def test_settings_overlay_sections(live_daemon, browser):
    ctx, page, msgs = _boot_collecting(browser, live_daemon, "agents")
    try:
        page.click('.hdr [data-act="settings"]')
        page.wait_for_selector(".settings-overlay", timeout=5000)
        for sec in ["general", "plugins", "integrations", "vault", "about", "danger"]:
            page.click(f'.settings-nav [data-s="{sec}"]')
            page.wait_for_timeout(300)
        bad = errors(msgs)
        assert not bad, "console errors in settings:\n" + "\n".join(bad)
    finally:
        ctx.close()


def test_theme_card_check_does_not_overlap_badge(live_daemon, browser):
    ctx, page = boot_page(browser, live_daemon)
    try:
        page.click('.hdr [data-act="settings"]')
        page.wait_for_selector(".settings-overlay", timeout=5000)
        page.click('.settings-nav [data-s="general"]')
        page.wait_for_selector(".theme-card.on", timeout=5000)
        overlap = page.eval_on_selector(
            ".theme-card.on",
            """card => {
                const ck = card.querySelector('.ck'), bi = card.querySelector('.bi');
                if (!ck || !bi) return false;
                const c = ck.getBoundingClientRect(), b = bi.getBoundingClientRect();
                return c.left < b.right && c.right > b.left;   // horizontal overlap
            }""",
        )
        assert overlap is False, "selected theme-card check overlaps the built-in badge"
    finally:
        ctx.close()


# ── new-agent modal: lists every shipped harness ─────────────────────────
def test_new_agent_modal_lists_harnesses(live_daemon, browser, tmp_path):
    # A workspace must exist or the modal shows the "No workspaces yet" empty
    # state instead of the harness cards (new_agent.js renderNoWorkspacesEmptyState).
    seed_workspace(live_daemon, "naws", make_ws_dir(tmp_path, "naws"))
    ctx, page = boot_page(browser, live_daemon)
    try:
        page.click('.hdr [data-act="new-agent"]')
        page.wait_for_selector(".na-card", timeout=5000)  # cards render async
        types = page.eval_on_selector_all(
            ".na-card", "els => els.map(c => c.getAttribute('data-type'))"
        )
        for want in ("pi", "claude-code", "codex-cli", "opencode-cli",
                     "cursor-cli", "antigravity", "relaydeck"):
            assert want in types, f"harness card {want!r} missing from {types}"
    finally:
        ctx.close()


# ── home dashboard renders its widget grid ───────────────────────────────
def test_home_dashboard_renders(live_daemon, browser):
    ctx, page = boot_page(browser, live_daemon)
    try:
        # No agent selected → the Agents detail pane is the home dashboard.
        page.wait_for_selector(".home", timeout=8000)
        n_widgets = page.eval_on_selector_all(".home .wgt", "els => els.length")
        assert n_widgets >= 1, "home dashboard rendered no widgets"
        # The greeting headline is present (warm time-of-day line).
        greeting = page.text_content(".home .greeting") or ""
        assert greeting.strip(), "home greeting is empty"
    finally:
        ctx.close()


def test_home_feed_shows_empty_state(live_daemon, browser):
    """Live activity must not render a blank box when no events have arrived."""
    ctx, page = boot_page(browser, live_daemon)
    try:
        page.wait_for_selector(".home", timeout=8000)
        feed_text = page.evaluate("""() => {
            const titles = [...document.querySelectorAll('.home .wgt-head .ttl')];
            const idx = titles.findIndex(t => t.textContent.trim() === 'Live activity');
            if (idx < 0) return '';
            const bodies = document.querySelectorAll('.home .wgt-body');
            return bodies[idx]?.textContent?.trim() || '';
        }""")
        assert "No events yet" in feed_text, f"feed empty state missing: {feed_text!r}"
    finally:
        ctx.close()


def test_home_gallery_closes_on_escape(live_daemon, browser):
    ctx, page = boot_page(browser, live_daemon)
    try:
        page.wait_for_selector(".home", timeout=8000)
        page.click('.home-hdr [data-act="add"]')
        page.wait_for_selector(".gallery", timeout=5000)
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('.gallery')", timeout=3000)
    finally:
        ctx.close()


def test_home_gallery_matches_theme_surface(live_daemon, browser):
    """Widget gallery must use --bg-1 like the widget cards, not a hardcoded dark panel."""
    ctx, page = boot_page(browser, live_daemon)
    try:
        page.wait_for_selector(".home", timeout=8000)
        page.click('.home-hdr [data-act="add"]')
        page.wait_for_selector(".gallery", timeout=5000)
        match = page.evaluate("""() => {
            const wgt = getComputedStyle(document.querySelector('.wgt'));
            const gal = getComputedStyle(document.querySelector('.gallery'));
            return wgt.backgroundColor === gal.backgroundColor;
        }""")
        assert match, "gallery background must match widget surface (--bg-1)"
    finally:
        ctx.close()


# ── workspaces lens: plugin-card category label never clipped ────────────
def test_workspace_plugin_card_category_not_clipped(live_daemon, browser, tmp_path):
    seed_workspace(live_daemon, "pcws", make_ws_dir(tmp_path, "pcws"))
    ctx, page = boot_page(browser, live_daemon, lens="workspaces")
    try:
        page.click('.side-list .srow:has-text("pcws")')
        page.wait_for_selector(".ws-plug-card", timeout=8000)
        clipped = page.eval_on_selector_all(
            ".ws-plug-card",
            """cards => cards.filter(c => {
                const cat = c.querySelector('.wpc-cat');
                return cat && cat.scrollWidth > cat.clientWidth + 1;
            }).map(c => c.querySelector('.wpc-name')?.textContent)""",
        )
        assert not clipped, f"plugin-card category clipped on: {clipped}"
    finally:
        ctx.close()


# ── models lens: sub-tabs auto-select first item; search filters ─────────
def test_models_subtabs_autoselect_and_search(live_daemon, browser):
    # Seed two presets so the Presets list + search have something to act on.
    from ._webutil import post_json
    post_json(live_daemon, "/api/presets", {"name": "alpha", "provider": "oai", "model": "gpt-x"})
    post_json(live_daemon, "/api/presets", {"name": "beta", "provider": "oai", "model": "gpt-y"})

    ctx, page = boot_page(browser, live_daemon, lens="models")
    try:
        page.wait_for_selector(".side-list", timeout=8000)

        # Switching to Providers auto-selects a provider (no bare "No provider").
        page.click('.mdl-tab:has-text("Providers")')
        page.wait_for_timeout(400)
        prov_text = page.eval_on_selector(".detail-host", "el => el.textContent") or ""
        assert "No provider" not in prov_text, "Providers tab showed a bare empty state"

        # Switching to Defaults auto-selects a role.
        page.click('.mdl-tab:has-text("Defaults")')
        page.wait_for_timeout(400)
        def_text = page.eval_on_selector(".detail-host", "el => el.textContent") or ""
        assert "No role" not in def_text, "Defaults tab showed a bare empty state"

        # Back to Presets: search filters the list (rows are .mdl-row).
        page.click('.mdl-tab:has-text("Presets")')
        page.wait_for_selector('.side-list .mdl-row:has-text("alpha")', timeout=5000)
        set_input(page, ".side-search input", "beta")
        page.wait_for_function(
            """() => {
                const rows = [...document.querySelectorAll('.side-list .mdl-row')]
                    .map(r => r.textContent);
                return rows.some(t => /beta/.test(t)) && !rows.some(t => /alpha/.test(t));
            }""",
            timeout=5000,
        )
    finally:
        ctx.close()
