"""
Browser E2E: relaydeck-native (pi-backed operator harness).

Pins:
  - live pi detection via ``/api/harnesses`` + ``/api/plugins/relaydeck-native/status``
    (``shutil.which`` on every request — no daemon restart after installing pi)
  - dashboard UX when pi is missing (new-agent modal + agent detail banner)
  - relaydeck-native spawn reaches running + PTY when pi is on the daemon PATH

Run::

    uv sync --group e2e
    uv run playwright install chromium
    uv run pytest -m e2e tests/e2e/test_web_relaydeck_native_e2e.py
"""

from __future__ import annotations

import shutil
import time

import pytest

from ._webutil import (
    add_workspace,
    boot_page,
    get_json,
    get_text,
    make_ws_dir,
    open_relaydeck_native_context_tab,
    post_json,
    put_json,
    seed_agent,
    seed_workspace,
    set_input,
    sidebar_has,
    wait_running,
)

pytestmark = pytest.mark.e2e


def _relaydeck_entry(catalog: dict) -> dict:
    for e in catalog.get("harnesses") or []:
        if e.get("type") == "relaydeck":
            return e
    raise AssertionError(f"relaydeck harness missing from catalog: {catalog}")


def test_harness_catalog_live_pi_probe(live_daemon):
    """``cli_installed`` for relaydeck-native tracks the host PATH at request time."""
    host_pi = shutil.which("pi") is not None
    entry = _relaydeck_entry(get_json(live_daemon, "/api/harnesses"))
    assert entry.get("cli") == "pi"
    assert entry.get("cli_installed") is host_pi
    if not host_pi:
        assert entry.get("install_hint")


def test_native_status_endpoint_live_pi_probe(live_daemon):
    st = get_json(live_daemon, "/api/plugins/relaydeck-native/status")
    assert st.get("ok") is True
    assert st.get("pi_installed") == (shutil.which("pi") is not None)
    assert st.get("extension_present") is True


def test_new_agent_relaydeck_pi_warning_blocks_spawn(live_daemon_no_pi, browser, tmp_path):
    """With pi hidden from the daemon PATH, the modal warns and disables Spawn."""
    ws = make_ws_dir(tmp_path, "e2e-rd-nopi")
    ctx, page = boot_page(browser, live_daemon_no_pi, lens="workspaces")
    try:
        page.wait_for_selector(".side-list", timeout=5000)
        add_workspace(page, ws, "e2e-rd-nopi", recommended=False)
        sidebar_has(page, "e2e-rd-nopi")

        catalog = get_json(live_daemon_no_pi, "/api/harnesses")
        entry = _relaydeck_entry(catalog)
        assert entry.get("cli_installed") is False

        page.click('.hdr [data-act="new-agent"]')
        page.wait_for_selector('.na-card[data-type="relaydeck"]', timeout=5000)
        page.click('.na-card[data-type="relaydeck"]')
        page.wait_for_selector('[data-type-section] .na-warn', timeout=5000)
        warn = page.inner_text('[data-type-section] .na-warn')
        assert "pi" in warn.lower()

        set_input(page, '.na-modal input[placeholder="pr-reviewer"]', "rd-nopi")
        page.select_option(".na-modal select", value="e2e-rd-nopi")
        assert page.is_disabled('.na-modal [data-act="spawn"]')
    finally:
        ctx.close()


def test_agent_detail_pi_banner_when_pi_missing(live_daemon_no_pi, browser, tmp_path):
    ws = make_ws_dir(tmp_path, "e2e-rd-banner")
    seed_workspace(live_daemon_no_pi, "e2e-rd-banner", ws)
    seed_agent(live_daemon_no_pi, "rd-banner", "relaydeck", "e2e-rd-banner", purpose="operator")

    ctx, page = boot_page(browser, live_daemon_no_pi, lens="agents")
    try:
        page.goto(f"{live_daemon_no_pi}/?lens=agents&agent=rd-banner")
        page.wait_for_selector("[data-native-pi-banner]", timeout=10000)
        page.wait_for_function(
            """() => {
              const el = document.querySelector('[data-native-pi-banner]');
              return el && el.style.display !== 'none' && /pi/i.test(el.textContent);
            }""",
            timeout=8000,
        )
    finally:
        ctx.close()


@pytest.mark.skipif(shutil.which("pi") is None, reason="pi not on PATH")
def test_spawn_relaydeck_native_renders_and_runs(live_daemon, browser, tmp_path):
    """relaydeck-native is a pi PTY harness — same platform contract as type:pi."""
    ws_name = "e2e-relaydeck"
    agent_id = "a-relaydeck"
    ws = make_ws_dir(tmp_path, ws_name)

    ctx, page = boot_page(browser, live_daemon, lens="workspaces")
    try:
        page.wait_for_selector(".side-list", timeout=5000)
        add_workspace(page, ws, ws_name, recommended=False)
        sidebar_has(page, ws_name)

        page.click('.hdr [data-act="new-agent"]')
        page.wait_for_selector('.na-card[data-type="relaydeck"]', timeout=5000)
        page.click('.na-card[data-type="relaydeck"]')
        set_input(page, '.na-modal input[placeholder="pr-reviewer"]', agent_id)
        page.select_option(".na-modal select", value=ws_name)
        assert not page.is_disabled('.na-modal [data-act="spawn"]')
        page.click('.na-modal button:has-text("Spawn agent")')

        page.wait_for_selector(".xterm, .term-host", timeout=20000)
        status = wait_running(live_daemon, agent_id, timeout=45.0)
        assert status == "running", f"relaydeck-native did not reach running (status={status!r})"

        page.wait_for_function(
            "() => { const t = window.__relaydeckTerm;"
            " return !!(t && t.ws && t.ws.readyState === 1); }",
            timeout=15000,
        )

        screen = ""
        for _ in range(12):
            screen = get_text(live_daemon, f"/api/agents/{agent_id}/screen?cols=120&rows=40")
            if "relaydeck-native" in screen.lower():
                break
            time.sleep(0.75)
        assert "relaydeck-native" in screen.lower(), (
            f"expected relaydeck-native startup chrome in PTY, got: {screen[:400]!r}"
        )
        assert "[skills]" not in screen.lower(), "pi default skill dump should be suppressed"
        assert "pi v0." not in screen.lower(), "pi default logo header should be suppressed"
        print(f"[relaydeck] custom startup verified; PTY sample: {screen[:200]!r}")
    finally:
        ctx.close()


def test_dashboard_get_api_includes_widget_grid(live_daemon, tmp_path):
    """POST /api/dashboard/command op=get must return the saved Home grid."""
    ws = make_ws_dir(tmp_path, "e2e-dash-grid")
    ws_name = seed_workspace(live_daemon, "e2e-dash-grid", ws)
    layout = [
        {"key": "clock", "x": 9, "y": 0, "w": 3, "h": 2},
        {"key": "fleet", "x": 0, "y": 0, "w": 6, "h": 3},
    ]
    put_json(live_daemon, f"/api/appearance?workspace={ws_name}", {"dashboard": layout})

    got = post_json(live_daemon, "/api/dashboard/command", {
        "op": "get", "workspace": ws_name,
    })
    ap = got.get("appearance") or {}
    assert ap.get("dashboard") == layout
    assert "themes" in got and "daylight" in got["themes"]

    default = post_json(live_daemon, "/api/dashboard/command", {"op": "get"})
    assert (default.get("appearance") or {}).get("theme")


def test_native_context_tile_shows_dashboard_layout(live_daemon, browser, tmp_path):
    """Context tile + native context API expose the saved widget grid to operators."""
    ws = make_ws_dir(tmp_path, "e2e-ctx-grid")
    ws_name = seed_workspace(live_daemon, "e2e-ctx-grid", ws)
    layout = [{"key": "clock", "x": 9, "y": 0, "w": 3, "h": 2}]
    put_json(live_daemon, f"/api/appearance?workspace={ws_name}", {"dashboard": layout})
    agent_id = "rd-ctx-grid"
    post_json(live_daemon, "/api/agents", {
        "id": agent_id,
        "type": "relaydeck",
        "workspace": ws_name,
        "config": {"tools": ["read", "relaydeck", "dashboard"]},
        "purpose": "layout probe",
    })

    ctx_api = get_json(
        live_daemon, f"/api/plugins/relaydeck-native/{agent_id}/context")
    caps = next(ly for ly in ctx_api.get("layers") or [] if ly.get("id") == "capabilities")
    assert "clock @ (9,0) 3x2" in caps.get("body", ""), caps.get("body", "")[:300]

    ctx, page = boot_page(browser, live_daemon, lens="agents")
    try:
        page.goto(
            f"{live_daemon}/?lens=agents&agent={agent_id}&workspace={ws_name}",
            wait_until="domcontentloaded",
        )
        page.wait_for_selector(".subtabs", timeout=10000)
        page.wait_for_selector(".dhdr", timeout=10000)
        open_relaydeck_native_context_tab(page)
        page.wait_for_selector(".card-title:has-text('Injected context')", timeout=15000)
        page.wait_for_function(
            """() => {
              const sum = [...document.querySelectorAll('details summary')]
                .find(s => s.textContent.includes('Capabilities'));
              const pre = sum?.closest('details')?.querySelector('pre');
              return pre?.textContent?.includes('clock @ (9,0) 3x2');
            }""",
            timeout=15000,
        )
        pre = page.locator('details:has(summary:has-text("Capabilities")) pre').first
        assert "clock @ (9,0) 3x2" in pre.inner_text()
    finally:
        ctx.close()
