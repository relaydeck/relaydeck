"""
`relaydeck open [path]` — the context-aware on-ramp.

One gesture: find-or-register the workspace that owns a directory, ensure the
daemon is up, open a viewer. These pin the find-vs-register decision (strict
path ownership, not the durable-default fallback), the daemon-up step, and the
three viewer modes (TUI default / --web / --no-view).
"""

from __future__ import annotations

import sys
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relaydeck.daemon as daemon_mod
import relaydeck.state as state_mod
import relaydeck.transports.cli as cli_mod
import relaydeck.transports.view as view_mod
from relaydeck.transports.cli import main


def _isolate(tmp_path, monkeypatch, *, daemon_running=True):
    """Point config home at a tmp root and stub the daemon lifecycle so the
    test never touches a real daemon or browser."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(state_mod, "get_daemon_url", lambda: "http://127.0.0.1:8765")
    monkeypatch.setattr(state_mod, "get_daemon_bind_host", lambda: "127.0.0.1")

    started: dict = {}

    def fake_status(home, host="127.0.0.1", port=8765):
        return {"running": daemon_running, "state": "managed" if daemon_running else "down"}

    def fake_start(home, host="127.0.0.1", port=8765, wait_seconds=5.0):
        started["called"] = True
        return {"pid": 4242, "healthy": True}

    monkeypatch.setattr(daemon_mod, "daemon_status", fake_status)
    monkeypatch.setattr(daemon_mod, "start_daemon", fake_start)
    return started


def test_open_registers_unowned_dir(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    proj = tmp_path / "code" / "api"
    proj.mkdir(parents=True)

    res = CliRunner().invoke(main, ["open", str(proj), "--no-view"])
    assert res.exit_code == 0, res.output
    assert "registered" in res.output

    # It landed in the registry with the default plugins.
    from relaydeck.config import load_workspace_registry
    reg = load_workspace_registry(tmp_path / ".relaydeck")
    entry = next(w for w in reg if w.path == proj.resolve())
    assert entry.name == "api"
    assert set(entry.plugins) == {"messaging", "skills"}


def test_open_attaches_to_owning_workspace_without_reregister(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    proj = tmp_path / "repo"
    proj.mkdir()
    # Pre-register it, then open a SUBDIRECTORY — should resolve to the owner.
    cli_mod._workspace_add_impl(str(proj), "repo", ["messaging"])
    sub = proj / "src"
    sub.mkdir()

    res = CliRunner().invoke(main, ["open", str(sub), "--no-view"])
    assert res.exit_code == 0, res.output
    assert "owns" in res.output
    assert "registered" not in res.output  # did NOT re-register

    from relaydeck.config import load_workspace_registry
    reg = load_workspace_registry(tmp_path / ".relaydeck")
    assert len(reg) == 1


def test_open_no_register_fails_on_unowned(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    proj = tmp_path / "loose"
    proj.mkdir()
    res = CliRunner().invoke(main, ["open", str(proj), "--no-register", "--no-view"])
    assert res.exit_code == 1
    assert "no workspace owns" in res.output.lower()


def test_open_starts_daemon_when_down(tmp_path, monkeypatch):
    started = _isolate(tmp_path, monkeypatch, daemon_running=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    res = CliRunner().invoke(main, ["open", str(proj), "--no-view"])
    assert res.exit_code == 0, res.output
    assert started.get("called") is True
    assert "daemon ready" in res.output


def test_open_web_launches_browser(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    opened: dict = {}
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url))

    res = CliRunner().invoke(main, ["open", str(proj), "--web"])
    assert res.exit_code == 0, res.output
    assert opened["url"] == "http://127.0.0.1:8765"


def test_open_default_launches_tui(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    seen: dict = {}

    def fake_view(workspace=None):
        seen["ws"] = workspace
        return 0

    monkeypatch.setattr(view_mod, "run_view", fake_view)

    res = CliRunner().invoke(main, ["open", str(proj)])
    assert res.exit_code == 0, res.output
    assert seen["ws"] == "proj"
