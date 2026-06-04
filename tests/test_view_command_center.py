"""
`relaydeck view` command-center: tabbed Terminal/Events/Messages/Tasks +
a CLI console line.

Driven headlessly via Textual's pilot (no TTY needed). The load-bearing
assertion is the safety contract for the "terminal untouchable" rule: tabs
toggle pane *visibility* only — the #pty widget is NEVER unmounted/remounted
when you switch away from and back to the Terminal tab, and on_resize is
suppressed while it's hidden so the harness never gets a bogus geometry.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relaydeck.transports.view as view


class _FakeAgents:
    def list(self):
        return []


class _FakeHost:
    daemon_url = "http://127.0.0.1:0"

    def __init__(self):
        self.agents = _FakeAgents()

    def _request(self, path, *a, **k):
        return []


def _noop_sse(host, path, on_event, stop):
    # Don't touch the network during the test; the event driver just idles.
    return


def _make_app(monkeypatch):
    monkeypatch.setattr(view, "_sse_worker", _noop_sse)
    t = view._import_textual()
    return view._build_app(t, _FakeHost(), initial_workspace=None)


async def test_all_panes_mount_and_terminal_is_default(monkeypatch):
    app = _make_app(monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        for sel in ("#pty", "#events", "#msgs", "#tasks", "#console", "#tabbar"):
            assert app.query_one(sel) is not None
        assert app._active_tab == "terminal"
        assert app.query_one("#pty").display          # visible
        assert not app.query_one("#events").display   # hidden
        assert not app.query_one("#msgs").display
        assert not app.query_one("#tasks").display


async def test_switching_tabs_never_unmounts_the_terminal(monkeypatch):
    app = _make_app(monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        pty_before = app.query_one("#pty")

        await app._set_tab("events")
        await pilot.pause()
        assert app._active_tab == "events"
        assert app.query_one("#events").display
        assert not app.query_one("#pty").display
        # CRUCIAL: same widget object, still in the DOM — not remounted.
        assert app.query_one("#pty") is pty_before

        await app._set_tab("tasks")
        await pilot.pause()
        assert app.query_one("#tasks").display
        assert app.query_one("#pty") is pty_before

        await app._set_tab("terminal")
        await pilot.pause()
        assert app.query_one("#pty").display
        assert app.query_one("#pty") is pty_before


async def test_event_feed_survives_bracket_laden_payload(monkeypatch):
    app = _make_app(monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        # A payload with '[' (a relay marker, a [y/N]) must not corrupt the
        # RichLog markup — _append_event_line escapes dynamic content.
        app._append_event_line(
            "agent.message",
            {"agent_id": "a", "payload": {"body": "[relay from=x] do it? [y/N]"}},
        )
        await pilot.pause()  # would raise on a markup parse error


async def test_resize_is_suppressed_off_terminal_tab(monkeypatch):
    app = _make_app(monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        import asyncio
        q: asyncio.Queue = asyncio.Queue()
        app._pty_send_queue = q

        await app._set_tab("events")          # terminal hidden
        await pilot.pause()
        # The active-tab guard returns before any geometry is computed, so a
        # hidden terminal is never shrunk to a bogus size.
        await app.on_resize(object())
        assert q.qsize() == 0                  # nothing forwarded while hidden


async def test_console_mode_runs_cli_and_shows_events_tab(monkeypatch):
    app = _make_app(monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()

        class _Done:
            stdout = "agent  status\nalice  idle\n"
            stderr = ""
            returncode = 0

        import subprocess
        captured = {}

        def fake_run(args, **kw):
            captured["args"] = args
            return _Done()

        monkeypatch.setattr(subprocess, "run", fake_run)

        app._begin_console()
        assert app._console_mode is True
        app._console_buffer = "agent list"
        await app._run_console()
        await pilot.pause()
        assert app._console_mode is False          # cleared after run
        assert captured["args"] == ["relaydeck", "agent", "list"]
        assert app._active_tab == "events"          # output shown on Events tab
