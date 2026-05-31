"""
Browser E2E: Telegram lens — real Bot API wiring (no mocks).

Uses a live ``relaydeck serve`` daemon + real Telegram:
  - getMe on token save (connections / setup)
  - long-polling getUpdates when you DM the bot from Telegram
  - sendMessage on ``POST /api/plugins/telegram/reply`` (outbound)

Operator env (never committed):
  RELAYDECK_E2E_TELEGRAM_TOKEN       — bot token from @BotFather
  RELAYDECK_E2E_TELEGRAM_USER_ID     — your numeric Telegram user id (default 8283066035)

Watch headed (recommended — uses your running daemon on :8765):

    uv sync --group e2e
    uv run playwright install chromium
    RELAYDECK_E2E_LIVE_DAEMON=1 \\
      RELAYDECK_E2E_TELEGRAM_TOKEN='…' \\
      RELAYDECK_E2E_TELEGRAM_USER_ID=8283066035 \\
      RELAYDECK_E2E_HEADED=1 RELAYDECK_E2E_SLOWMO=400 \\
      uv run pytest -m e2e \\
      tests/e2e/test_web_telegram_e2e.py::test_telegram_full_live_wiring -v -s

``RELAYDECK_E2E_LIVE_DAEMON=1`` is required when your normal daemon already
polls the bot — Telegram allows only one getUpdates consumer per token.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from ._webutil import (
    boot_page,
    errors,
    get_json,
    make_ws_dir,
    post_json,
    put_json,
    seed_agent,
    seed_workspace,
    set_input,
)

pytestmark = pytest.mark.e2e

CONN_ID = "e2e-bot"
WS_NAME = "tg-e2e"
AGENT_ID = "tg-inbox"


def _use_live_daemon() -> bool:
    return os.environ.get("RELAYDECK_E2E_LIVE_DAEMON", "").lower() in ("1", "true", "yes")


def _watch_pause(page, ms: int = 900) -> None:
    if os.environ.get("RELAYDECK_E2E_HEADED", "").lower() in ("1", "true", "yes"):
        page.wait_for_timeout(ms)
    elif int(os.environ.get("RELAYDECK_E2E_SLOWMO", "0") or "0") > 0:
        page.wait_for_timeout(max(400, ms // 2))


def _prompt(msg: str) -> None:
    print(f"\n>>> {msg}\n", flush=True)
    sys.stdout.flush()


def _require_telegram_token() -> str:
    value = os.environ.get("RELAYDECK_E2E_TELEGRAM_TOKEN", "").strip()
    if not value:
        pytest.skip("RELAYDECK_E2E_TELEGRAM_TOKEN not set")
    return value


def _telegram_user_id() -> int:
    raw = os.environ.get("RELAYDECK_E2E_TELEGRAM_USER_ID", "8283066035").strip()
    try:
        return int(raw)
    except ValueError:
        pytest.skip(f"RELAYDECK_E2E_TELEGRAM_USER_ID invalid: {raw!r}")


def _require_ptb() -> None:
    # PTB ships with relaydeck core, so this should never skip; it only
    # guards against a broken/partial install in a live e2e environment.
    try:
        import telegram  # noqa: F401
    except ImportError:
        pytest.skip("python-telegram-bot failed to import — broken relaydeck install?")


def _activity_rows(base: str) -> list[dict]:
    return get_json(base, "/api/plugins/telegram/activity").get("activity") or []


def _wait_for_activity(
    base: str,
    *,
    disposition: str | None = None,
    preview_contains: str | None = None,
    user_id: int | None = None,
    after_ts: float | None = None,
    timeout_s: float | None = None,
) -> dict:
    if timeout_s is None:
        headed = os.environ.get("RELAYDECK_E2E_HEADED", "").lower() in ("1", "true", "yes")
        timeout_s = 180.0 if headed else 120.0
    deadline = time.time() + timeout_s
    started = time.time()
    last_heartbeat = started
    while time.time() < deadline:
        for row in _activity_rows(base):
            ts = float(row.get("ts") or 0)
            if after_ts is not None and ts <= after_ts:
                continue
            if disposition and row.get("disposition") != disposition:
                continue
            if user_id is not None and int(row.get("user_id") or 0) != user_id:
                continue
            preview = str(row.get("preview") or "")
            if preview_contains and preview_contains not in preview:
                continue
            return row
        now = time.time()
        if now - last_heartbeat >= 10.0:
            elapsed = int(now - started)
            rows = _activity_rows(base)
            n = len(rows)
            hint = ""
            if rows and after_ts is not None:
                fresh = [r for r in rows if float(r.get("ts") or 0) > after_ts]
                if fresh:
                    r0 = fresh[0]
                    hint = (
                        f" — newest since step: {r0.get('preview')!r} "
                        f"({r0.get('disposition')})"
                    )
            print(
                f"    … waiting for Telegram DM ({elapsed}s, {n} row(s) in feed{hint})",
                flush=True,
            )
            last_heartbeat = now
        time.sleep(2.0)
    bits = [f"disposition={disposition!r}"]
    if preview_contains:
        bits.append(f"preview~={preview_contains!r}")
    recent = _activity_rows(base)[:5]
    pytest.fail(
        f"no matching Telegram activity within {timeout_s:.0f}s ({', '.join(bits)}). "
        f"Recent activity: {recent!r}"
    )


def _wait_worker_ready(
    base: str, conn_id: str | None = None, timeout_s: float = 30.0,
) -> tuple[str, str]:
    """Return (bot_username, conn_id) once the worker is polling."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = get_json(base, "/api/plugins/telegram/status")
        conns = status.get("connections") or []
        if conn_id:
            pool = [c for c in conns if c.get("id") == conn_id]
        else:
            pool = [c for c in conns if c.get("has_token")]
        for match in pool:
            bot = match.get("bot") or {}
            if bot.get("ready") and bot.get("bot_username"):
                return bot["bot_username"], match["id"]
            err = bot.get("last_error") or match.get("stub_reason") or ""
            if "conflict" in err.lower() or "getupdates" in err.lower():
                pytest.fail(
                    f"Telegram poller conflict on {match['id']!r}: {err}\n"
                    "Use RELAYDECK_E2E_LIVE_DAEMON=1 or stop the other daemon."
                )
        time.sleep(1.0)
    pytest.fail(f"no ready Telegram connection within {timeout_s:.0f}s")


def _activity_baseline_ts(base: str) -> float:
    """Only match activity newer than what's already in the feed."""
    rows = _activity_rows(base)
    if not rows:
        return time.time()
    return max(float(r.get("ts") or 0) for r in rows)


def _put_routes(base: str, *, allowed_users: list[int], routes: list[dict]) -> None:
    put_json(
        base,
        "/api/plugins/telegram/routes",
        {"allowed_users": allowed_users, "allowed_groups": [], "routes": routes},
    )


def _routes_allowed_users(base: str) -> list[int]:
    return get_json(base, "/api/plugins/telegram/routes").get("allowed_users") or []


def _boot_telegram(browser, base):
    ctx, page = boot_page(browser, base, lens="telegram")
    page.wait_for_selector(".tg-wrap", timeout=15000)
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('section') === 'connections'",
        timeout=8000,
    )
    return ctx, page


def _goto_section(page, label: str) -> None:
    page.click(f'.pl-nav-item:has-text("{label}")')
    _watch_pause(page, 500)


def _allow_user_via_activity_button(page, base: str, user_id: int, *, preview: str) -> None:
    """One-click onboarding: Activity row → '+ allow user' (PUT /routes)."""
    row = page.locator(".tg-act").filter(has_text=preview)
    row.locator('[data-act="allow-user"]').click()
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if user_id in _routes_allowed_users(base):
            return
        time.sleep(0.5)
    pytest.fail(f"allow-user UI did not persist user_id={user_id}")


def _allow_user_via_allowlists_form(page, base: str, user_id: int) -> None:
    """Allowlists section → type user_id → '+' button."""
    _goto_section(page, "Allowlists")
    section = page.locator(".tg-section").filter(has_text="Allowed users")
    set_input(section.locator('input[data-add]'), str(user_id))
    section.locator('button[data-act="add"]').click()
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if user_id in _routes_allowed_users(base):
            return
        time.sleep(0.5)
    pytest.fail(f"allowlists form did not persist user_id={user_id}")


def _ensure_workspace(base: str, tmp_path: Path) -> None:
    wss = get_json(base, "/api/workspaces")
    if any(w.get("name") == WS_NAME for w in wss):
        return
    if _use_live_daemon():
        path = os.environ.get(
            "RELAYDECK_E2E_WORKSPACE_PATH",
            str(Path.home() / ".relaydeck" / "workspaces" / WS_NAME),
        )
        Path(path).mkdir(parents=True, exist_ok=True)
        (Path(path) / "README.md").write_text(f"# {WS_NAME}\n")
        post_json(base, "/api/workspaces", {
            "name": WS_NAME, "path": path, "plugins": ["messaging"],
        })
    else:
        seed_workspace(base, WS_NAME, make_ws_dir(tmp_path, WS_NAME), ["messaging"])


def _ensure_agent(base: str) -> None:
    agents = get_json(base, "/api/agents")
    if any(a.get("id") == AGENT_ID for a in agents):
        return
    seed_agent(base, AGENT_ID, "pi", WS_NAME, purpose="Telegram inbox")


def _ensure_bot(page, base: str, token: str) -> tuple[str, str]:
    """Return (bot_username, connection_id); register only when needed."""
    status = get_json(base, "/api/plugins/telegram/status")
    for c in status.get("connections") or []:
        if not c.get("has_token"):
            continue
        bot = c.get("bot") or {}
        if bot.get("ready") and bot.get("bot_username"):
            return bot["bot_username"], c["id"]
    if _use_live_daemon():
        post_json(base, "/api/plugins/telegram/setup", {"token": token})
        return _wait_worker_ready(base)
    _add_bot_via_ui(page, base, token, CONN_ID)
    return _wait_worker_ready(base, conn_id=CONN_ID)


def _add_bot_via_ui(page, base: str, token: str, conn_id: str = CONN_ID) -> str:
    page.click('[data-act="add-conn"]')
    page.wait_for_selector('.tg-row-form [data-f="id"]', timeout=5000)
    set_input(page, '.tg-row-form [data-f="id"]', conn_id)
    set_input(page, '.tg-row-form [data-f="name"]', "E2E Playwright Bot")
    set_input(page, '.tg-row-form [data-f="token"]', token)
    page.click('.tg-row-form [data-act="save"]')
    page.wait_for_function(
        """() => [...document.querySelectorAll('.tg-route')]
                    .some(r => r.textContent.includes('token set'))""",
        timeout=20000,
    )

    deadline = time.time() + 30.0
    bot_username = None
    while time.time() < deadline:
        status = get_json(base, "/api/plugins/telegram/status")
        match = next(
            (c for c in (status.get("connections") or []) if c.get("id") == conn_id),
            None,
        )
        assert match is not None, f"connection {conn_id!r} missing"
        assert match.get("has_token") is True
        bot = match.get("bot") or {}
        if bot.get("ready") and bot.get("bot_username"):
            bot_username = bot["bot_username"]
            break
        time.sleep(1.0)
    assert bot_username, f"bot never reached ready state: {match}"
    return bot_username


def test_telegram_lens_renders_without_console_errors(live_daemon, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    msgs = []
    page.on("console", lambda m: msgs.append(m))
    page.goto(f"{live_daemon}/?lens=telegram&section=connections")
    page.wait_for_selector(".rail", timeout=15000)
    page.wait_for_selector(".tg-wrap", timeout=15000)
    try:
        active = page.eval_on_selector(
            '.rail-btn[title="Telegram"]', "e => e.classList.contains('on')"
        )
        assert active
        page.wait_for_selector('[data-act="add-conn"]', timeout=8000)
        bad = errors(msgs)
        assert not bad, "console errors on Telegram lens:\n" + "\n".join(bad)
    finally:
        ctx.close()


def test_telegram_sidebar_sections_switch(live_daemon, browser):
    ctx, page = _boot_telegram(browser, live_daemon)
    try:
        for label in ("Routes", "Settings"):
            _goto_section(page, label)
            page.wait_for_function(
                """(lbl) => {
                    const on = document.querySelector('.pl-nav-item.active');
                    return on && on.textContent.includes(lbl);
                }""",
                arg=label,
                timeout=5000,
            )
    finally:
        ctx.close()


def test_telegram_add_bot_via_dashboard(live_daemon, browser, tmp_path):
    _require_ptb()
    token = _require_telegram_token()
    ctx, page = _boot_telegram(browser, live_daemon)
    try:
        _watch_pause(page)
        bot_username = _add_bot_via_ui(page, live_daemon, token)
        page.click('[data-act="verify"]')
        _watch_pause(page, 1200)
        page.wait_for_function(
            """(uname) => document.querySelector('.tg-head h1')?.textContent.includes('@' + uname)""",
            arg=bot_username,
            timeout=10000,
        )
    finally:
        ctx.close()


def test_telegram_full_live_wiring(telegram_e2e_base, browser, tmp_path):
    """End-to-end real Telegram: allowlist, routes, inbound DMs, outbound send."""
    _require_ptb()
    token = _require_telegram_token()
    user_id = _telegram_user_id()
    dm_chat_id = user_id  # private DM chat_id == user_id
    base = telegram_e2e_base

    _ensure_workspace(base, tmp_path)
    _ensure_agent(base)

    ctx, page = _boot_telegram(browser, base)
    try:
        _watch_pause(page)
        bot_username, conn_id = _ensure_bot(page, base, token)
        put_json(base, "/api/plugins/telegram/config", {"open_access": False})
        _put_routes(base, allowed_users=[], routes=[])

        _prompt(
            f"Starting Telegram e2e on @{bot_username}.\n"
            f"Keep Telegram open — you'll send a few DMs when each STEP appears."
        )

        # ── 1. Fail-closed allowlist (any message while blocked) ─────
        _goto_section(page, "Activity")
        baseline_ts = _activity_baseline_ts(base)
        _prompt(
            f"STEP 1/5 — DM @{bot_username} anything (e.g. e2e-blocked).\n"
            f"You're NOT allowlisted → Activity shows 'user blocked'."
        )
        blocked = _wait_for_activity(
            base,
            disposition="rejected_user",
            user_id=user_id,
            after_ts=baseline_ts,
            timeout_s=120.0,
        )
        if int(blocked["user_id"]) != user_id:
            pytest.fail(f"unexpected user_id on blocked row: {blocked}")
        page.wait_for_selector('.tg-act .badge.err:has-text("user blocked")', timeout=15000)
        _watch_pause(page)

        # ── 2. Whitelist via Activity '+ allow user' ─────────────────
        _prompt(
            f"STEP 2/5 — Watch the dashboard: '+ allow user' whitelists {user_id}…"
        )
        preview = str(blocked.get("preview") or "")
        if preview:
            _allow_user_via_activity_button(page, base, user_id, preview=preview)
        else:
            _allow_user_via_allowlists_form(page, base, user_id)
        _goto_section(page, "Allowlists")
        page.wait_for_function(
            """(uid) => {
                const chips = [...document.querySelectorAll('.tg-allow .chip')];
                return chips.some(c => c.textContent.includes(String(uid)));
            }""",
            arg=user_id,
            timeout=10000,
        )
        assert user_id in _routes_allowed_users(base)
        _watch_pause(page)

        # ── 3. Unrouted inbound (now allowlisted) ────────────────────
        _goto_section(page, "Activity")
        t1 = _activity_baseline_ts(base)
        _prompt(f"STEP 3/5 — DM @{bot_username} any text (allowlisted, no route → 'no route').")
        unrouted = _wait_for_activity(
            base,
            disposition="unrouted",
            user_id=user_id,
            after_ts=t1,
            timeout_s=120.0,
        )
        assert unrouted["chat_id"] == dm_chat_id

        # Conversations registry (global, per connection).
        convs = get_json(base, "/api/plugins/telegram/conversations").get(
            "conversations"
        ) or []
        assert any(c.get("chat_id") == dm_chat_id for c in convs), convs
        _goto_section(page, "Conversations")
        page.wait_for_function(
            """(cid) => document.body.textContent.includes(String(cid))""",
            arg=dm_chat_id,
            timeout=10000,
        )

        # ── 4. Route table + test-route API ──────────────────────────
        route = {
            "chat_id": dm_chat_id,
            "workspace": WS_NAME,
            "connection": conn_id,
            "direction": "in",
        }
        _put_routes(base, allowed_users=[user_id], routes=[route])
        test = post_json(
            base,
            "/api/plugins/telegram/routes/test",
            {"chat_id": dm_chat_id, "connection": conn_id},
        )
        matches = test.get("matches") or []
        assert matches and matches[0].get("winner"), matches

        _goto_section(page, "Routes")
        page.wait_for_selector(".tg-route", timeout=8000)
        t2 = _activity_baseline_ts(base)
        _prompt(f"STEP 4/5 — DM @{bot_username} any text (routed → workspace {WS_NAME}).")
        routed = _wait_for_activity(
            base,
            disposition="routed",
            user_id=user_id,
            after_ts=t2,
            timeout_s=120.0,
        )
        assert AGENT_ID in (routed.get("target") or []), routed

        # ── 5. Slash-command route precedence ────────────────────────
        cmd_route = {
            "chat_id": dm_chat_id,
            "workspace": WS_NAME,
            "agent": AGENT_ID,
            "command": "ping",
            "connection": conn_id,
            "direction": "in",
        }
        _put_routes(
            base, allowed_users=[user_id], routes=[route, cmd_route],
        )
        cmd_test = post_json(
            base,
            "/api/plugins/telegram/routes/test",
            {"chat_id": dm_chat_id, "connection": conn_id, "command": "ping"},
        )
        cmd_matches = cmd_test.get("matches") or []
        assert any(m.get("command") == "ping" and m.get("winner") for m in cmd_matches)

        t3 = _activity_baseline_ts(base)
        _prompt(f"STEP 5/5 — DM @{bot_username}: /ping (command route).")
        cmd_row = _wait_for_activity(
            base,
            disposition="routed",
            user_id=user_id,
            after_ts=t3,
            timeout_s=120.0,
        )
        assert cmd_row.get("command") == "ping"

        # ── 5. Outbound — real sendMessage via daemon API ────────────
        outbound = f"e2e-outbound-{int(time.time())}"
        send_res = post_json(
            base,
            "/api/plugins/telegram/reply",
            {"chat_id": dm_chat_id, "body": outbound, "connection": conn_id},
        )
        assert send_res.get("ok") is True, send_res
        assert send_res.get("message_id"), send_res
        _prompt(f"Check Telegram — bot should have sent: {outbound!r}")

        # ── 6. Dashboard Send section (same API, UI path) ────────────
        _goto_section(page, "Send")
        page.wait_for_selector('.tg-send [data-f="chat_id"]', timeout=8000)
        set_input(page, '.tg-send [data-f="chat_id"]', str(dm_chat_id))
        ui_out = f"e2e-ui-send-{int(time.time())}"
        set_input(page, '.tg-send [data-f="body"]', ui_out)
        _watch_pause(page, 800)
        page.click('.tg-send .footer button')
        page.wait_for_function(
            """() => {
                const s = document.querySelector('.tg-send .status.ok');
                return s && /sent/i.test(s.textContent);
            }""",
            timeout=15000,
        )
        _prompt(f"Check Telegram — UI send should have delivered: {ui_out!r}")

        # ── 7. Verify token + settings round-trip ────────────────────
        auth = post_json(base, "/api/plugins/telegram/auth", {})
        assert auth.get("ok") is True or auth.get("connections"), auth

        cfg = put_json(
            base,
            "/api/plugins/telegram/config",
            {"reactions": True, "require_mention_in_groups": True},
        )
        assert cfg.get("ok") is True
        got = get_json(base, "/api/plugins/telegram/config")
        assert got.get("require_mention_in_groups") is True

        _goto_section(page, "Settings")
        page.wait_for_selector(".tg-settings", timeout=8000)
        _watch_pause(page, 2500)
    finally:
        ctx.close()
