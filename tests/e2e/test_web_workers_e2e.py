"""
Browser + API E2E: Workers lens, configurable loop workers, and system workers.

Covers:
  - default daemon workers (db.maintenance, skills.scan)
  - trigger permutations (interval / cron / on_event) via validate + live ticks
  - action kinds (code, bus.emit, script, gh, agent.message) via validate
  - Workers lens UI: create, detail, pause/resume/run-now, system worker detail
  - plugin disable/re-enable does not duplicate daemon-wide workers
  - on_event wiring fires when a matching bus event occurs (workspace.added)

Run headed (visible browser)::

    RELAYDECK_E2E_HEADED=1 uv run pytest -m e2e tests/e2e/test_web_workers_e2e.py -v
"""

from __future__ import annotations

import time

import pytest

from ._webutil import (
    boot_page,
    errors,
    get_json,
    make_ws_dir,
    post_json,
    seed_workspace,
    set_input,
)

pytestmark = pytest.mark.e2e

# ── Validation matrix (all trigger × action permutations) ────────────

TRIGGER_CASES = [
    ("interval:5s", True),
    ("interval:2m", True),
    ("cron:0 9 * * 1-5", True),
    ("cron:* * * * *", True),
    ("on_event:agent.start", True),
    ("on_event:workspace.added", True),
    ("on_event:agent.*", True),
    ("interval:garbage", False),
    ("on_event:", False),
    ("daily:1", False),
]

ACTION_CASES = [
    {"code": {"lang": "python", "body": "print('ok')", "emit": "worker.test"}},
    {"bus.emit": {"type": "worker.e2e.ping", "data": {"n": 1}}},
    {"script": {"path": "scripts/noop.py"}},
    {"gh": {"args": ["api", "user"]}},
    {"agent.message": {"to": "peer", "body": "hello"}},
    {"model": {"prompt": "say hi", "max_tokens": 32}},
]


@pytest.mark.parametrize("schedule,expect_ok", TRIGGER_CASES)
def test_automation_validate_triggers(live_daemon, schedule, expect_ok):
    body = {"schedule": schedule, "actions": [{"bus.emit": {"type": "x", "data": {}}}]}
    out = post_json(live_daemon, "/api/automations/validate", body)
    assert out["ok"] is expect_ok


@pytest.mark.parametrize("action", ACTION_CASES)
def test_automation_validate_action_kinds(live_daemon, action):
    out = post_json(
        live_daemon,
        "/api/automations/validate",
        {"schedule": "interval:30s", "actions": [action]},
    )
    assert out["ok"] is True, out.get("errors")


def _workers_named(base: str, name: str) -> list[dict]:
    return [w for w in get_json(base, "/api/workers") if w.get("name") == name]


def _wait_runs(base: str, automation_id: str, min_runs: int = 1, timeout: float = 12.0) -> int:
    deadline = time.time() + timeout
    count = 0
    while time.time() < deadline:
        runs = get_json(base, f"/api/automations/{automation_id}/runs?limit=5").get("runs") or []
        count = len(runs)
        if count >= min_runs:
            return count
        time.sleep(0.4)
    return count


def _wait_loop_running(base: str, worker_id: str, timeout: float = 10.0) -> str:
    """Poll until a loop automation's backing agent reaches running."""
    deadline = time.time() + timeout
    status = "pending"
    while time.time() < deadline:
        autos = get_json(base, "/api/automations")["automations"]
        row = next((a for a in autos if a["automation_id"] == worker_id), None)
        if row:
            status = row.get("agent_status") or status
            if status == "running":
                return status
        time.sleep(0.2)
    return status


def _create_loop_worker(
    base: str,
    worker_id: str,
    *,
    schedule: str,
    actions: list[dict],
    workspace: str | None = None,
    auto_start: bool = True,
) -> None:
    body = {
        "id": worker_id,
        "name": worker_id,
        "type": "loop",
        "workspace": workspace,
        "auto_start": auto_start,
        "config": {"schedule": schedule, "actions": actions},
    }
    post_json(base, "/api/agents", body)
    if auto_start:
        post_json(base, f"/api/agents/{worker_id}/start", {})
        assert _wait_loop_running(base, worker_id) == "running"


# ── System workers ───────────────────────────────────────────────────


def test_default_system_workers_present(live_daemon):
    workers = get_json(live_daemon, "/api/workers")
    names = {w.get("name") for w in workers}
    assert "db.maintenance" in names
    maint = next(w for w in workers if w.get("name") == "db.maintenance")
    assert maint.get("plugin") == "relaydeck"
    assert maint.get("status") in ("running", "idle")
    assert maint.get("description")


def test_skills_scan_worker_running(live_daemon):
    scan = _workers_named(live_daemon, "skills.scan")
    assert len(scan) == 1
    assert scan[0].get("status") in ("running", "idle")
    assert scan[0].get("restart_policy") == "restart"


def test_plugin_disable_reenable_no_duplicate_workers(live_daemon):
    before = len(_workers_named(live_daemon, "skills.scan"))
    assert before == 1
    post_json(live_daemon, "/api/plugins/skills/disable", {})
    disabled = _workers_named(live_daemon, "skills.scan")
    assert disabled == []
    post_json(live_daemon, "/api/plugins/skills/enable", {})
    time.sleep(0.5)
    after = _workers_named(live_daemon, "skills.scan")
    assert len(after) == 1
    assert after[0].get("status") in ("running", "idle")


# ── Configurable workers (API) ───────────────────────────────────────


def test_interval_worker_ticks_and_records_runs(live_daemon, tmp_path):
    ws = make_ws_dir(tmp_path, "wk-interval")
    seed_workspace(live_daemon, "wk-interval", ws)
    wid = "e2e-interval"
    _create_loop_worker(
        live_daemon,
        wid,
        workspace="wk-interval",
        schedule="interval:1s",
        actions=[{"bus.emit": {"type": "worker.e2e.interval", "data": {"ok": True}}}],
    )
    auto = next(a for a in get_json(live_daemon, "/api/automations")["automations"]
                if a["automation_id"] == wid)
    assert auto["trigger"]["kind"] == "interval"
    assert auto["agent_status"] == "running"
    assert _wait_runs(live_daemon, wid, min_runs=1) >= 1
    post_json(live_daemon, f"/api/automations/{wid}/run", {})
    assert _wait_runs(live_daemon, wid, min_runs=2) >= 2


def test_on_event_worker_fires_on_workspace_added(live_daemon, tmp_path):
    wid = "e2e-on-event"
    _create_loop_worker(
        live_daemon,
        wid,
        schedule="on_event:workspace.added",
        actions=[{"bus.emit": {"type": "worker.e2e.on_event", "data": {"fired": True}}}],
        auto_start=True,
    )
    # Event must arrive AFTER the loop agent is subscribed.
    ws = make_ws_dir(tmp_path, "wk-event")
    seed_workspace(live_daemon, "wk-event", ws)
    assert _wait_runs(live_daemon, wid, min_runs=1, timeout=10.0) >= 1


def test_code_action_run_now(live_daemon, tmp_path):
    ws = make_ws_dir(tmp_path, "wk-code")
    seed_workspace(live_daemon, "wk-code", ws)
    wid = "e2e-code"
    _create_loop_worker(
        live_daemon,
        wid,
        workspace="wk-code",
        schedule="interval:1h",
        actions=[{
            "code": {
                "lang": "python",
                "body": "import json; print(json.dumps({'e2e': True}))",
            },
        }],
    )
    post_json(live_daemon, f"/api/automations/{wid}/run", {})
    deadline = time.time() + 10.0
    status = "running"
    while time.time() < deadline:
        runs = get_json(live_daemon, f"/api/automations/{wid}/runs?limit=1").get("runs") or []
        if runs:
            status = runs[0].get("status") or "running"
            if status in ("succeeded", "partial", "failed"):
                break
        time.sleep(0.3)
    assert status in ("succeeded", "partial"), f"run ended as {status!r}"


def test_worker_pause_resume(live_daemon, tmp_path):
    ws = make_ws_dir(tmp_path, "wk-pause")
    seed_workspace(live_daemon, "wk-pause", ws)
    wid = "e2e-pause"
    _create_loop_worker(
        live_daemon,
        wid,
        workspace="wk-pause",
        schedule="interval:1s",
        actions=[{"bus.emit": {"type": "worker.e2e.pause", "data": {}}}],
    )
    post_json(live_daemon, f"/api/automations/{wid}/pause", {})
    time.sleep(0.3)
    auto = next(a for a in get_json(live_daemon, "/api/automations")["automations"]
                if a["automation_id"] == wid)
    assert auto["agent_status"] != "running"
    post_json(live_daemon, f"/api/automations/{wid}/resume", {})
    time.sleep(0.5)
    auto = next(a for a in get_json(live_daemon, "/api/automations")["automations"]
                if a["automation_id"] == wid)
    assert auto["agent_status"] == "running"


# ── Workers lens (browser) ───────────────────────────────────────────


def test_workers_lens_shows_system_and_configurable(live_daemon, browser, tmp_path):
    ws = make_ws_dir(tmp_path, "wk-ui")
    seed_workspace(live_daemon, "wk-ui", ws)
    _create_loop_worker(
        live_daemon,
        "e2e-ui-worker",
        workspace="wk-ui",
        schedule="interval:30s",
        actions=[{"code": {"lang": "python", "body": "pass"}}],
    )
    msgs: list = []
    ctx, page = boot_page(browser, live_daemon, lens="workers")
    page.on("console", lambda m: msgs.append(m))
    try:
        page.wait_for_selector(".wk-side-section", timeout=10000)
        sections = page.locator(".wk-side-section").all_text_contents()
        assert any("Configurable" in s for s in sections)
        assert any("System" in s for s in sections)
        page.wait_for_selector('.wk-side-row.cfg:has-text("e2e-ui-worker")', timeout=8000)
        page.click('.wk-side-row.cfg:has-text("e2e-ui-worker")')
        page.wait_for_selector(".cwk-hdr", timeout=8000)
        page.wait_for_selector(".cwk-pipeline", timeout=8000)
        assert "code" in page.inner_text(".cwk-pipeline").lower()
        page.click('.wk-side-row.sys:has-text("db.maintenance")')
        page.wait_for_selector(".cwk-hdr", timeout=8000)
        assert "db.maintenance" in page.inner_text(".cwk-hdr")
        assert not errors(msgs)
    finally:
        ctx.close()


def test_workers_lens_create_via_form(live_daemon, browser, tmp_path):
    ws = make_ws_dir(tmp_path, "wk-form")
    seed_workspace(live_daemon, "wk-form", ws)
    wid = "e2e-form-worker"
    msgs: list = []
    ctx, page = boot_page(browser, live_daemon, lens="workers")
    page.on("console", lambda m: msgs.append(m))
    try:
        page.wait_for_selector('[data-new]', timeout=8000)
        page.click('[data-new]')
        page.wait_for_selector(".ewm", timeout=8000)
        set_input(page, '.ewm input[data-f="id"]', wid)
        set_input(page, '.ewm input[data-f="name"]', "E2E Form Worker")
        page.select_option('.ewm select[data-f="workspace"]', "wk-form")
        page.select_option('.ewm select[data-f="trig_kind"]', "interval")
        set_input(page, '.ewm input[data-f="trig_value"]', "10s")
        # Default action is model (needs a configured role) — use bus.emit instead.
        page.select_option('.ewm-act-card select[data-k="type"]', "bus.emit")
        page.wait_for_selector('.ewm-act-card input[data-k="eventName"]', timeout=5000)
        page.locator('.ewm-act-card input[data-k="eventName"]').fill("worker.e2e.form")
        page.click('.ewm [data-act="save"]')
        page.wait_for_selector(".ewm", state="detached", timeout=15000)
        # API is source of truth; sidebar may lag one live refresh beat.
        deadline = time.time() + 8.0
        auto = None
        while time.time() < deadline:
            autos = get_json(live_daemon, "/api/automations")["automations"]
            auto = next((a for a in autos if a["automation_id"] == wid), None)
            if auto:
                break
            time.sleep(0.3)
        assert auto is not None, "worker was not created via the form"
        assert auto["workspace"] == "wk-form"
        assert auto["trigger"]["kind"] == "interval"
        assert auto["agent_status"] == "running"
        page.goto(f"{live_daemon}/?lens=workers&workspace=wk-form")
        page.wait_for_selector(".rail", timeout=10000)
        page.wait_for_selector('.wk-side-row.cfg:has-text("E2E Form Worker")', timeout=10000)
        assert not errors(msgs)
    finally:
        ctx.close()


def test_workers_lens_run_now_button(live_daemon, browser, tmp_path):
    ws = make_ws_dir(tmp_path, "wk-run")
    seed_workspace(live_daemon, "wk-run", ws)
    wid = "e2e-run-btn"
    _create_loop_worker(
        live_daemon,
        wid,
        workspace="wk-run",
        schedule="interval:1h",
        actions=[{"bus.emit": {"type": "worker.e2e.runbtn", "data": {}}}],
    )
    ctx, page = boot_page(browser, live_daemon, lens="workers")
    try:
        page.goto(f"{live_daemon}/?lens=workers")
        page.wait_for_selector(f'.wk-side-row.cfg:has-text("{wid}")', timeout=10000)
        page.click(f'.wk-side-row.cfg:has-text("{wid}")')
        page.wait_for_selector('[data-act="run"]', timeout=8000)
        page.click('[data-act="run"]')
        assert _wait_runs(live_daemon, wid, min_runs=1, timeout=12.0) >= 1
    finally:
        ctx.close()
