"""
Browser + API E2E: harness integration contracts — skills/plugins injection,
spawn composition, vendor integrations registry, SDK-registered harness types,
and the start/stop/restart lifecycle.

Fills gaps left by ``test_web_harness_e2e.py`` (PTY mount only) and the unit-
level ``test_harness_skill_injection_matrix.py`` (in-process, no daemon).

Most tests need NO model auth — agents stay ``pending`` or we assert on the
composition API / Identity tile / built command. Lifecycle + delivery tests
skip per harness when the CLI isn't on PATH.

Runs under ``-m e2e``. Headless by default; set ``RELAYDECK_E2E_HEADED=1`` or
``RELAYDECK_E2E_SLOWMO=<ms>`` to watch in a real browser (CI uses
``RELAYDECK_E2E_HEADLESS=1``).

    uv sync --group e2e
    uv run playwright install chromium
    uv run pytest -m e2e tests/e2e/test_harness_integration_e2e.py -s
"""

from __future__ import annotations

import importlib
import shutil
import threading
import time
from pathlib import Path

import pytest

from relaydeck.testing import harness_delivery_blob

from ._webutil import (
    boot_page,
    click_subtab,
    config_home,
    get_json,
    get_text,
    make_ws_dir,
    patch_workspace_plugins,
    post_json,
    seed_agent,
    seed_injection_fixtures,
    seed_workspace,
    set_input,
    start_agent,
    stop_agent,
    wait_running,
    wait_status,
    write_fleet_context,
)

pytestmark = pytest.mark.e2e

PURPOSE = "GATE-PRS-SENTINEL"
WS = "inj-ws"

# (harness type, CLI binary, module, class name)
HARNESSES = [
    ("pi", "pi", "plugins.harnesses.pi.agent", "PiAgent"),
    ("claude-code", "claude", "plugins.harnesses.claude_code.agent", "ClaudeCodeAgent"),
    ("codex-cli", "codex", "plugins.harnesses.codex.agent", "CodexAgent"),
    ("opencode-cli", "opencode", "plugins.harnesses.opencode.agent", "OpenCodeAgent"),
    ("cursor-cli", "cursor-agent", "plugins.harnesses.cursor.agent", "CursorAgent"),
]

_SHIPPED_NATIVE = ("pi", "claude-code", "codex-cli", "opencode-cli", "cursor-cli")


@pytest.fixture
def isolated_home_env(daemon_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Pin harness agent construction to the same home the live daemon uses."""
    monkeypatch.setattr(Path, "home", lambda: daemon_home)
    monkeypatch.setenv("HOME", str(daemon_home))
    monkeypatch.setenv("RELAYDECK_CONFIG_HOME", str(config_home(daemon_home)))
    return daemon_home


def _home(daemon_home: Path) -> Path:
    return config_home(daemon_home)


def _labels(base: str, agent_id: str) -> list[str]:
    comp = get_json(base, f"/api/agents/{agent_id}/prompt-composition")
    return [c["label"] for c in comp.get("components", [])]


def _make_harness_agent(module: str, cls_name: str, home: Path, ws: str, agent_type: str):
    cls = getattr(importlib.import_module(module), cls_name)
    return cls(
        agent_id="probe", name="probe", config={}, workspace=ws,
        db_path=str(home / "runtime" / "relaydeck.db"),
        stop_flag=threading.Event(),
    )


def _seed_injection_workspace(base: str, daemon_home: Path, tmp_path: Path) -> dict[str, str]:
    ws_path = make_ws_dir(tmp_path, WS)
    seed_workspace(base, WS, ws_path, ["messaging", "skills", "fleet-context"])
    probes = seed_injection_fixtures(_home(daemon_home), WS, "probe")
    return probes


# ── harness catalog + integrations (daemon boot contracts) ───────────────


def test_harness_catalog_lists_shipped_native_types(live_daemon):
    """Every shipped native harness appears in /api/harnesses with SDK metadata."""
    harnesses = get_json(live_daemon, "/api/harnesses").get("harnesses") or []
    types = {h["type"] for h in harnesses if h.get("kind") == "native"}
    for want in _SHIPPED_NATIVE:
        assert want in types, f"{want!r} missing from harness catalog: {sorted(types)}"


def test_integrations_registry_lists_hook_and_classifier_sources(live_daemon):
    """Vendor integrations: claude hook + classifier bridges for other harnesses."""
    rows = get_json(live_daemon, "/api/integrations")
    by_name = {r["name"]: r for r in rows}
    assert by_name["claude"]["kind"] == "hook"
    for name in ("pi", "codex", "opencode", "cursor"):
        assert name in by_name, f"missing integration for {name!r}: {sorted(by_name)}"
        assert by_name[name]["kind"] == "classifier"


# ── prompt composition: plugin gates mirror harness injection ────────────


def test_composition_gates_skills_messaging_and_fleet_context(
    live_daemon, daemon_home, tmp_path,
):
    """Composition API reflects the same gates harnesses honor at spawn."""
    probes = _seed_injection_workspace(live_daemon, daemon_home, tmp_path)
    seed_agent(live_daemon, "probe", "pi", WS, purpose=PURPOSE)

    labels = _labels(live_daemon, "probe")
    assert any("identity preamble" in lab for lab in labels)
    assert any(probes["runtime_skill"] in lab for lab in labels), "messaging skill must be ungated"
    assert any(probes["user_skill"] in lab for lab in labels), "user skill with skills gate ON"
    assert any("fleet context" in lab.lower() for lab in labels)

    # skills gate OFF → user skill drops, runtime skill stays
    patch_workspace_plugins(live_daemon, WS, ["messaging"])
    labels = _labels(live_daemon, "probe")
    assert any(probes["runtime_skill"] in lab for lab in labels)
    assert not any(probes["user_skill"] in lab for lab in labels)

    # fleet-context gate OFF (file on disk) → fleet block drops
    patch_workspace_plugins(live_daemon, WS, ["messaging", "skills"])
    write_fleet_context(_home(daemon_home), WS, "probe", probes["fleet_marker"])
    labels = _labels(live_daemon, "probe")
    assert not any("fleet context" in lab.lower() for lab in labels)

    # fleet-context gate ON + file present → fleet block injects
    patch_workspace_plugins(live_daemon, WS, ["messaging", "skills", "fleet-context"])
    labels = _labels(live_daemon, "probe")
    assert any("fleet context" in lab.lower() for lab in labels)


def test_preview_prompt_reports_workspace_injections(live_daemon, daemon_home, tmp_path):
    """New-agent preview (read-only) lists active plugins + preamble markers."""
    _seed_injection_workspace(live_daemon, daemon_home, tmp_path)
    body = post_json(live_daemon, "/api/agents/preview-prompt", {
        "agent_id": "reviewer", "workspace": WS, "purpose": PURPOSE,
        "system_prompt": "Prefer rejection.",
    })
    assert PURPOSE in body.get("preamble", "")
    assert "Prefer rejection" in body.get("system_prompt", "")
    inj = {i["plugin"]: i for i in body.get("injections") or []}
    assert "skills" in inj
    assert "messaging" in inj


# ── browser: Identity tile + new-agent modal ─────────────────────────────


def test_identity_tile_shows_spawn_composition(live_daemon, daemon_home, browser, tmp_path):
    """Identity tab renders real composition rows from prompt-composition API."""
    probes = _seed_injection_workspace(live_daemon, daemon_home, tmp_path)
    seed_agent(live_daemon, "probe", "pi", WS, purpose=PURPOSE,
               system_prompt="Always check SQL injection.")

    ctx, page = boot_page(browser, live_daemon, lens="agents")
    try:
        page.goto(f"{live_daemon}/?workspace={WS}&agent=probe")
        page.wait_for_selector(".subtab", timeout=10000)
        click_subtab(page, "Identity", content_selector=".idn-comp")
        text = page.text_content(".idn-comp") or ""
        assert probes["runtime_skill"] in text
        assert probes["user_skill"] in text
        assert "Always check SQL injection" in (page.text_content(".idn-sp") or "")
    finally:
        ctx.close()


def test_new_agent_modal_preview_shows_preamble_and_plugins(
    live_daemon, daemon_home, browser, tmp_path,
):
    """Modal section 05 live-previews preamble; section 04 lists workspace plugins."""
    _seed_injection_workspace(live_daemon, daemon_home, tmp_path)

    ctx, page = boot_page(browser, live_daemon, lens="agents")
    try:
        page.click('.hdr [data-act="new-agent"]')
        page.wait_for_selector('.na-card[data-type="pi"]', timeout=8000)
        page.click('.na-card[data-type="pi"]')
        set_input(page, '.na-modal input[placeholder="pr-reviewer"]', "reviewer")
        page.select_option(".na-modal select", value=WS)
        page.fill('.na-modal textarea[data-f="purpose"]', PURPOSE)
        page.fill('.na-modal textarea[data-f="system_prompt"]', "Gate risky merges.")

        page.wait_for_function(
            """() => {
                const pre = document.querySelector('[data-preamble]');
                return pre && pre.textContent.includes('GATE-PRS-SENTINEL');
            }""",
            timeout=8000,
        )
        preview = page.text_content("[data-preview]") or ""
        assert "skills" in preview.lower() or "messaging" in preview.lower()
        # Plugin section 04 shows workspace-scoped plugins as checkboxes
        assert page.locator('.na-pl input[data-plugin="skills"]').count() >= 1
        assert page.locator('.na-pl input[data-plugin="messaging"]').count() >= 1
    finally:
        ctx.close()


def test_identity_plugin_toggle_updates_composition_live(
    live_daemon, daemon_home, browser, tmp_path,
):
    """Toggling the skills gate in Identity tile drops user skills from composition."""
    probes = _seed_injection_workspace(live_daemon, daemon_home, tmp_path)
    seed_agent(live_daemon, "probe", "pi", WS, purpose=PURPOSE)

    ctx, page = boot_page(browser, live_daemon, lens="agents")
    try:
        page.goto(f"{live_daemon}/?workspace={WS}&agent=probe")
        click_subtab(page, "Identity", content_selector=".idn-toggle")
        toggle = page.locator('.idn-toggle[data-toggle="skills"]')
        assert "on" in (toggle.get_attribute("class") or ""), "skills gate must start enabled"
        toggle.click()
        page.wait_for_function(
            """(skill) => {
                const rows = [...document.querySelectorAll('.idn-cmp-label')];
                return !rows.some(r => r.textContent.includes(skill));
            }""",
            arg=probes["user_skill"],
            timeout=10000,
        )
        labels = _labels(live_daemon, "probe")
        assert not any(probes["user_skill"] in lab for lab in labels)
        assert any(probes["runtime_skill"] in lab for lab in labels)
    finally:
        ctx.close()


# ── delivery-level: harness _build_command / env (daemon home) ───────────


@pytest.mark.parametrize("htype,cli,module,cls", HARNESSES)
def test_harness_delivers_purpose_and_skills_at_spawn(
    live_daemon, daemon_home, isolated_home_env, tmp_path, htype, cli, module, cls,
):
    """Each harness's spawn path delivers preamble + runtime + user skills."""
    if shutil.which(cli) is None:
        pytest.skip(f"{cli} not on PATH")

    ws_name = f"del-{htype}"
    home = _home(daemon_home)
    ws_path = make_ws_dir(tmp_path, ws_name)
    seed_workspace(live_daemon, ws_name, ws_path, ["messaging", "skills"])
    probes = seed_injection_fixtures(home, ws_name)
    seed_agent(live_daemon, "probe", htype, ws_name, purpose=PURPOSE)

    agent = _make_harness_agent(module, cls, home, ws_name, htype)
    blob = harness_delivery_blob(agent)
    assert PURPOSE in blob, f"{htype}: identity preamble (purpose) not in delivery blob"
    assert probes["runtime_skill"] in blob, (
        f"{htype}: messaging runtime skill missing — peer replies would silently drop"
    )
    assert probes["user_skill"] in blob, f"{htype}: user skill missing with skills gate ON"


@pytest.mark.parametrize("htype,cli,module,cls", HARNESSES)
def test_messaging_skill_delivered_without_skills_gate(
    live_daemon, daemon_home, isolated_home_env, tmp_path, htype, cli, module, cls,
):
    """Runtime messaging skill is ungated; user skill is withheld when skills OFF."""
    if shutil.which(cli) is None:
        pytest.skip(f"{cli} not on PATH")

    ws_name = f"ungated-{htype}"
    home = _home(daemon_home)
    ws_path = make_ws_dir(tmp_path, ws_name)
    seed_workspace(live_daemon, ws_name, ws_path, ["messaging"])
    probes = seed_injection_fixtures(home, ws_name)
    seed_agent(live_daemon, "probe", htype, ws_name, purpose=PURPOSE)

    blob = harness_delivery_blob(_make_harness_agent(module, cls, home, ws_name, htype))
    assert probes["runtime_skill"] in blob
    assert probes["user_skill"] not in blob


# ── lifecycle: start → running → stop → restart ──────────────────────────


@pytest.mark.parametrize("htype,cli", [(h[0], h[1]) for h in HARNESSES])
def test_harness_lifecycle_start_stop_restart(live_daemon, browser, tmp_path, htype, cli):
    """Full lifecycle via API + dashboard terminal remount after restart."""
    if shutil.which(cli) is None:
        pytest.skip(f"{cli} not on PATH")

    ws_name = f"lc-{htype}"
    agent_id = f"lc-{htype.replace('-cli', '')}"
    seed_workspace(live_daemon, ws_name, make_ws_dir(tmp_path, ws_name), ["messaging"])
    seed_agent(live_daemon, agent_id, htype, ws_name, purpose="lifecycle probe")

    start_agent(live_daemon, agent_id)
    assert wait_running(live_daemon, agent_id, timeout=45) == "running"

    ctx, page = boot_page(browser, live_daemon, lens="agents")
    try:
        page.goto(f"{live_daemon}/?workspace={ws_name}&agent={agent_id}")
        page.wait_for_selector(".xterm, .term-host", timeout=20000)
        page.wait_for_function(
            "() => { const t = window.__relaydeckTerm;"
            " return !!(t && t.ws && t.ws.readyState === 1); }",
            timeout=15000,
        )

        stop_agent(live_daemon, agent_id)
        assert wait_status(live_daemon, agent_id, "stopped", timeout=30) == "stopped"

        start_agent(live_daemon, agent_id)
        assert wait_running(live_daemon, agent_id, timeout=45) == "running"
        page.wait_for_function(
            "() => { const t = window.__relaydeckTerm;"
            " return !!(t && t.ws && t.ws.readyState === 1); }",
            timeout=20000,
        )
        # PTY repaints after restart (not just a stale WS reconnect).
        screen = ""
        deadline = time.time() + 15.0
        while time.time() < deadline:
            screen = get_text(live_daemon, f"/api/agents/{agent_id}/screen?cols=120&rows=40")
            if screen.strip():
                break
            time.sleep(0.5)
        assert screen.strip(), f"{htype}: PTY did not repaint after restart"
    finally:
        ctx.close()


# ── invalid skills never reach composition or delivery ───────────────────


def test_invalid_skill_skipped_in_composition_and_delivery(
    live_daemon, daemon_home, isolated_home_env, tmp_path,
):
    """Malformed SKILL.md (no frontmatter) is skipped identically everywhere."""
    ws_name = "invalid-skill-ws"
    home = _home(daemon_home)
    ws_path = make_ws_dir(tmp_path, ws_name)
    seed_workspace(live_daemon, ws_name, ws_path, ["skills"])
    bad = home / "workspaces" / ws_name / "skills" / "bad-skill"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("no frontmatter here")
    seed_agent(live_daemon, "probe", "pi", ws_name)

    labels = _labels(live_daemon, "probe")
    assert not any("bad-skill" in lab for lab in labels)

    if shutil.which("pi"):
        blob = harness_delivery_blob(
            _make_harness_agent(
                "plugins.harnesses.pi.agent", "PiAgent", home, ws_name, "pi",
            )
        )
        assert "bad-skill" not in blob
