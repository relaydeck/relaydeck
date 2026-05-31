"""Shared helpers for the browser E2E suite (test_web_*_e2e.py).

A tiny loopback HTTP client for the isolated `live_daemon` (auth via the
loopback bootstrap token), plus the handful of dashboard-driving primitives
the tests reuse. Kept out of conftest.py because these are plain functions,
not fixtures.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── daemon HTTP client (loopback bootstrap token, no browser needed) ─────
_TOKEN_CACHE: dict[str, str] = {}


def token(base: str) -> str:
    if base not in _TOKEN_CACHE:
        with urllib.request.urlopen(f"{base}/api/auth/bootstrap", timeout=10) as r:
            _TOKEN_CACHE[base] = json.loads(r.read())["token"]
    return _TOKEN_CACHE[base]


def _req(base: str, path: str, *, method: str = "GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {token(base)}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(f"{base}{path}", data=data, method=method, headers=headers)


def get_json(base: str, path: str):
    with urllib.request.urlopen(_req(base, path), timeout=15) as r:
        return json.loads(r.read())


def get_text(base: str, path: str) -> str:
    # Tolerant by design — used for screen snapshots in poll loops, where a
    # transient 4xx ("not running yet") or a slow render (timeout) should just
    # mean "nothing to show this tick", not crash the test.
    try:
        with urllib.request.urlopen(_req(base, path), timeout=15) as r:
            return r.read().decode(errors="replace")
    except Exception:
        return ""


def post_json(base: str, path: str, body: dict):
    with urllib.request.urlopen(_req(base, path, method="POST", body=body), timeout=15) as r:
        return json.loads(r.read())


def patch_json(base: str, path: str, body: dict):
    with urllib.request.urlopen(_req(base, path, method="PATCH", body=body), timeout=15) as r:
        return json.loads(r.read())


def put_json(base: str, path: str, body: dict):
    with urllib.request.urlopen(_req(base, path, method="PUT", body=body), timeout=15) as r:
        return json.loads(r.read())


def config_home(daemon_home: Path) -> Path:
    return daemon_home / ".relaydeck"


def write_user_skill(home: Path, ws: str, name: str, *, description: str = "user skill") -> None:
    d = home / "workspaces" / ws / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUser skill body for {name}.\n"
    )


def write_runtime_skill(
    home: Path, ws: str, name: str, *, owner_plugin: str = "messaging",
) -> None:
    d = home / "workspaces" / ws / "runtime" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: reply to peers via the relaydeck CLI\n---\n\n"
        "Reply with `relaydeck reply <id> <body>`.\n"
    )
    (d / ".relaydeck-skill.json").write_text(
        json.dumps({"owner_plugin": owner_plugin, "managed_by": "skills"})
    )


def write_fleet_context(home: Path, ws: str, agent_id: str, marker: str) -> None:
    d = home / "workspaces" / ws / "runtime" / "fleet-context"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{agent_id}.md").write_text(f"# fleet context\n{marker}\n")


def seed_injection_fixtures(home: Path, ws: str, agent_id: str = "probe") -> dict[str, str]:
    """Standard probe skills + fleet context for injection e2e."""
    user = "user-deploy-probe"
    runtime = "relay-cli-probe"
    fc = "FLEET-CONTEXT-MARKER"
    write_user_skill(home, ws, user)
    write_runtime_skill(home, ws, runtime)
    write_fleet_context(home, ws, agent_id, fc)
    return {"user_skill": user, "runtime_skill": runtime, "fleet_marker": fc}


def patch_workspace_plugins(base: str, ws: str, plugins: list[str]) -> dict:
    return patch_json(base, f"/api/workspaces/{ws}", {"plugins": plugins})


def stop_agent(base: str, agent_id: str) -> None:
    post_json(base, f"/api/agents/{agent_id}/stop", {})


def wait_status(base: str, agent_id: str, want: str, timeout: float = 45.0) -> str | None:
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        try:
            status = get_json(base, f"/api/agents/{agent_id}").get("status")
        except Exception:
            status = None
        if status == want:
            return status
        time.sleep(0.75)
    return status


def click_subtab(page, label: str, *, content_selector: str | None = None) -> None:
    """Activate a detail-pane sub-tab and wait for it to become active."""
    page.click(f'.subtab:has-text("{label}")')
    page.wait_for_function(
        """(lbl) => [...document.querySelectorAll('.subtab')].some(
            t => t.classList.contains('on') && t.textContent.includes(lbl))""",
        arg=label,
        timeout=5000,
    )
    if content_selector:
        page.wait_for_selector(content_selector, timeout=8000)


def open_relaydeck_native_context_tab(page) -> None:
    """Open the relaydeck-native Context tile (injected prompt layers).

    Core ships a separate ``core:context`` heatmap tab with the same label —
    click the plugin one (``relaydeck-native`` source), including from +N overflow."""
    import re

    visible = page.locator("button.subtab").filter(has_text="Context")
    if visible.count() >= 2:
        visible.nth(1).click()
    else:
        overflow = page.locator("button.subtab").filter(has_text=re.compile(r"\+\d+"))
        if not overflow.count():
            raise AssertionError("relaydeck-native Context tab not found")
        overflow.first.click()
        page.wait_for_selector(".pop-body .srow", timeout=5000)
        page.locator(".pop-body .srow").filter(
            has=page.locator(".sub", has_text="relaydeck-native")
        ).filter(has=page.locator(".name", has_text="Context")).click()
    page.wait_for_function(
        """() => [...document.querySelectorAll('.subtab')].some(
            t => t.classList.contains('on') && t.textContent.includes('Context'))""",
        timeout=5000,
    )


# ── filesystem + API seeding (fast, no browser / no harness binary) ──────
def path_without_binaries(*names: str) -> str:
    """Return a PATH with directories containing any named binaries removed.

    Used to spawn a daemon that cannot see ``pi`` even when the test runner
    can — proves live ``shutil.which`` probes in the dashboard/API."""
    hide_dirs: set[str] = set()
    for name in names:
        for part in os.environ.get("PATH", "").split(os.pathsep):
            if not part:
                continue
            candidate = Path(part) / name
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    hide_dirs.add(str(Path(part).resolve()))
            except OSError:
                continue
    parts: list[str] = []
    seen: set[str] = set()
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        try:
            resolved = str(Path(part).resolve())
        except OSError:
            resolved = part
        if resolved in hide_dirs or resolved in seen:
            continue
        parts.append(part)
        seen.add(resolved)
    if parts and all(shutil.which(n, path=os.pathsep.join(parts)) is None for n in names):
        return os.pathsep.join(parts)
    # Fallback: minimal PATH so relaydeck still starts but harness CLIs are hidden.
    keep: list[str] = []
    relaydeck = shutil.which("relaydeck")
    if relaydeck:
        keep.append(str(Path(relaydeck).resolve().parent))
    for p in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if Path(p).is_dir():
            keep.append(p)
    return os.pathsep.join(keep)


def make_ws_dir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "README.md").write_text(f"# {name}\n")
    return d


def seed_workspace(base: str, name: str, path: Path, plugins: list[str] | None = None) -> str:
    """Register a workspace via the API (faster than the modal)."""
    post_json(base, "/api/workspaces", {"name": name, "path": str(path), "plugins": plugins or []})
    return name


def seed_agent(
    base: str,
    agent_id: str,
    agent_type: str,
    workspace: str,
    *,
    purpose: str = "",
    system_prompt: str = "",
    auto_start: bool = False,
) -> str:
    """Create an agent via the API (``auto_start=False`` by default)."""
    body: dict = {
        "id": agent_id,
        "type": agent_type,
        "workspace": workspace,
        "auto_start": auto_start,
    }
    if purpose:
        body["purpose"] = purpose
    if system_prompt:
        body["system_prompt"] = system_prompt
    post_json(base, "/api/agents", body)
    return agent_id


def start_agent(base: str, agent_id: str) -> None:
    """Start an agent's harness (needs the CLI on PATH)."""
    post_json(base, f"/api/agents/{agent_id}/start", {})


def send_message(base: str, workspace: str, from_id: str, to: str | None, body: str) -> dict:
    """Send a peer message via the daemon (the same endpoint the dashboard +
    `relaydeck workspace message` use). `to=None` broadcasts to the workspace.
    Returns {ids, injected, pending, recipients:[{agent_id,status,injected}]}."""
    payload = {"from": from_id, "body": body}
    if to is not None:
        payload["agent"] = to
    return post_json(base, f"/api/workspaces/{workspace}/messages", payload)


# ── dashboard driving primitives ─────────────────────────────────────────
def viewer_pause(page, ms: int | None = None) -> None:
    """Extra dwell time between steps when ``RELAYDECK_E2E_PAUSE_MS`` is set —
    use with ``RELAYDECK_E2E_HEADED=1`` + ``RELAYDECK_E2E_SLOWMO`` so an
    operator can follow the flow in a real browser."""
    delay = ms if ms is not None else int(os.environ.get("RELAYDECK_E2E_PAUSE_MS", "0") or "0")
    if delay > 0:
        page.wait_for_timeout(delay)


def open_settings(page, section: str = "general") -> None:
    page.click('.hdr [data-act="settings"]')
    page.wait_for_selector(".settings-overlay", timeout=8000)
    if section != "general":
        page.click(f'.settings-nav [data-s="{section}"]')
        page.wait_for_timeout(200)


def boot_page(browser, base: str, *, lens: str = "agents"):
    """Open a fresh authed dashboard page on a lens; wait for the shell."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(f"{base}/?lens={lens}")
    page.wait_for_selector(".rail", timeout=15000)
    return ctx, page


def set_input(page, selector: str, value: str) -> None:
    """Set a controlled <input> the way a user would (native value setter +
    an 'input' event), so the framework's listeners fire."""
    page.eval_on_selector(
        selector,
        """(el, v) => {
            const set = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            set.call(el, v);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        value,
    )


def add_workspace(page, ws_path: Path, name: str, *, recommended: bool = False) -> None:
    """Drive the Add-workspace modal: directory + name, optional Recommended
    preset, submit. (The Workspaces lens must be active.)"""
    page.click('.side [data-act="new"]')
    page.wait_for_selector('input[placeholder="/abs/path/on/daemon/host"]', timeout=5000)
    # The modal rewrites the path input once its async directory listing lands;
    # wait for that or it clobbers our value (name then derives to $HOME).
    page.wait_for_function(
        "() => { const el = document.querySelector('[data-current]');"
        " return el && !/loading/i.test(el.textContent); }",
        timeout=5000,
    )
    set_input(page, 'input[placeholder="/abs/path/on/daemon/host"]', str(ws_path))
    set_input(page, 'input[placeholder="workspace-name"]', name)
    if recommended:
        # Plugins now live behind the "Advanced" disclosure (collapsed by
        # default; the Recommended preset is auto-applied on open). Expand it to
        # reach the plugin controls, then (re)assert the Recommended preset.
        page.click('[data-disc-toggle="adv"]')
        page.wait_for_selector(".addws-plugins .addws-plugin", timeout=5000)
        page.click('button[data-preset="recommended"]')
    page.click('.addws-modal [data-act="confirm"]')


def sidebar_has(page, name: str) -> None:
    page.wait_for_function(
        """(n) => [...document.querySelectorAll('.side-list .srow .name')]
                    .some(e => e.textContent.trim() === n)""",
        arg=name,
        timeout=8000,
    )


def errors(messages) -> list[str]:
    """Filter Playwright console messages to genuine errors, dropping the
    benign favicon 404 noise (documented in docs/playwright.md). Headless-shell
    doesn't request the favicon; full Chrome (headed) does, and logs a GENERIC
    'Failed to load resource: ... 404' whose text has no 'favicon' substring --
    so match on the message's location URL, not just its text."""
    out = []
    for m in messages:
        if m.type != "error":
            continue
        text = m.text or ""
        url = ""
        try:
            url = (m.location or {}).get("url", "") or ""
        except Exception:
            url = ""
        if "favicon" in (text + " " + url).lower():
            continue
        out.append(f"{text}  [{url}]" if url else text)
    return out


def wait_running(base: str, agent_id: str, timeout: float = 45.0, *, label: str | None = None) -> str | None:
    """Poll until the agent reaches running (resilient to transient slowness)."""
    deadline = time.time() + timeout
    status = None
    last_heartbeat = time.time()
    while time.time() < deadline:
        try:
            status = get_json(base, f"/api/agents/{agent_id}").get("status")
        except Exception:
            status = None
        if status == "running":
            return status
        now = time.time()
        if label and now - last_heartbeat >= 10.0:
            print(f"[wait_running] {label}: status={status!r} ({int(deadline - now)}s left)")
            last_heartbeat = now
        time.sleep(1.0)
    return status
