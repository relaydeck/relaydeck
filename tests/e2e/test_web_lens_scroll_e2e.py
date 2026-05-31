"""Every lens/detail surface must scroll when content overflows.

Regression guard for the `.dbody { overflow:hidden }` trap (Models/Workspaces)
and plugin wraps using `height:100%` instead of `flex:1; min-height:0`.
"""

from __future__ import annotations

import pytest

from ._webutil import boot_page, post_json


def _scroll_audit(page, selector: str) -> dict:
    return page.evaluate(
        """(sel) => {
          const el = document.querySelector(sel);
          if (!el) return { found: false, selector: sel };
          const oy = getComputedStyle(el).overflowY;
          const before = el.scrollTop;
          el.scrollTop = el.scrollHeight;
          return {
            found: true,
            selector: sel,
            overflowY: oy,
            scrollable: oy === 'auto' || oy === 'scroll',
            scrollHeight: el.scrollHeight,
            clientHeight: el.clientHeight,
            canScroll: el.scrollHeight > el.clientHeight + 2,
            scrollMoved: el.scrollTop > before,
            scrollTop: el.scrollTop,
          };
        }""",
        selector,
    )


@pytest.mark.e2e
def test_lens_scroll_containers(live_daemon, browser):
    post_json(live_daemon, "/api/presets", {"name": "scroll-a", "provider": "oai", "model": "m1"})
    post_json(live_daemon, "/api/presets", {"name": "scroll-b", "provider": "oai", "model": "m2"})

    checks: list[tuple[str, str, dict]] = []

    # Models — `.lens-body` (NOT `.dbody`)
    ctx, page = boot_page(browser, live_daemon, lens="models")
    try:
        page.wait_for_selector(".lens-body", timeout=10000)
        checks.append(("models", ".lens-body", _scroll_audit(page, ".lens-body")))
        page.click('.mdl-tab[data-tab="providers"]')
        page.wait_for_timeout(400)
        checks.append(("models-providers", ".lens-body", _scroll_audit(page, ".lens-body")))
    finally:
        ctx.close()

    # Workspaces
    ctx, page = boot_page(browser, live_daemon, lens="workspaces")
    try:
        page.wait_for_selector(".side-list .srow, .pane-empty", timeout=10000)
        if page.query_selector(".lens-body"):
            checks.append(("workspaces", ".lens-body", _scroll_audit(page, ".lens-body")))
    finally:
        ctx.close()

    # Workers
    ctx, page = boot_page(browser, live_daemon, lens="workers")
    try:
        page.wait_for_selector(".cwk-body, .pane-empty", timeout=10000)
        if page.query_selector(".cwk-body"):
            checks.append(("workers", ".cwk-body", _scroll_audit(page, ".cwk-body")))
    finally:
        ctx.close()

    # Agents home
    ctx, page = boot_page(browser, live_daemon, lens="agents")
    try:
        page.wait_for_selector(".home-scroll", timeout=10000)
        checks.append(("agents-home", ".home-scroll", _scroll_audit(page, ".home-scroll")))
    finally:
        ctx.close()

    # Plugin lenses (when rail slot present)
    for lens, sel in [
        ("telegram", ".tg-body"),
        ("github", ".gh-body"),
        ("skills", ".sk-body"),
        ("external", ".ea-body"),
    ]:
        ctx, page = boot_page(browser, live_daemon, lens=lens)
        try:
            page.wait_for_timeout(800)
            if page.query_selector(sel):
                checks.append((lens, sel, _scroll_audit(page, sel)))
        finally:
            ctx.close()

    # Messages (only when messaging rail exists)
    ctx, page = boot_page(browser, live_daemon, lens="agents")
    try:
        if page.query_selector('.rail-btn[title="Messages"]'):
            page.click('.rail-btn[title="Messages"]')
            page.wait_for_timeout(600)
            if page.query_selector(".msg-list"):
                checks.append(("messages", ".msg-list", _scroll_audit(page, ".msg-list")))
    finally:
        ctx.close()

    failures = []
    for name, sel, r in checks:
        if not r.get("found"):
            failures.append(f"{name}: missing {sel}")
            continue
        if not r.get("scrollable"):
            failures.append(f"{name}: {sel} overflow-y={r.get('overflowY')!r} (expected auto/scroll)")

    assert not failures, "Scroll container audit failed:\n" + "\n".join(failures)
