"""Live dashboard control: shared validator + endpoint + CLI + skill.

The native `dashboard` tool, the `relaydeck dashboard` CLI, and
`POST /api/dashboard/command` all route through
`relaydeck.dashboard_commands` so they can't drift. These pin the validator,
the HTTP contract, the CLI wiring, and that the dashboard plugin ships the
`relaydeck-dashboard` skill.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck import dashboard_commands as dash
from relaydeck.transports.api import create_app

# ── shared validator ────────────────────────────────────────────────


def test_build_command_widget_ops():
    assert dash.build_dashboard_command("add_widget", "fleet") == {
        "op": "add_widget", "value": "fleet"}
    assert dash.build_dashboard_command("move_widget", "usage", x=8, y=0) == {
        "op": "move_widget", "value": "usage", "x": 8, "y": 0}
    assert dash.build_dashboard_command("resize_widget", "agents", w=6, h=4) == {
        "op": "resize_widget", "value": "agents", "w": 6, "h": 4}
    assert dash.build_dashboard_command("tidy") == {"op": "tidy", "value": None}


def test_build_command_rejects_unknown_op():
    with pytest.raises(dash.DashboardCommandError):
        dash.build_dashboard_command("explode")


def test_build_command_rejects_get_as_write():
    # `get` is a read, not a command to build.
    with pytest.raises(dash.DashboardCommandError):
        dash.build_dashboard_command("get")


def test_build_command_requires_value():
    with pytest.raises(dash.DashboardCommandError):
        dash.build_dashboard_command("theme")


def test_build_command_validates_enums():
    with pytest.raises(dash.DashboardCommandError):
        dash.build_dashboard_command("add_widget", "nope")
    with pytest.raises(dash.DashboardCommandError):
        dash.build_dashboard_command("density", "huge")
    with pytest.raises(dash.DashboardCommandError):
        dash.build_dashboard_command("glow", "maybe")


def test_build_command_validates_theme_against_known():
    assert dash.build_dashboard_command(
        "theme", "base", known_themes=["base", "ink"])["value"] == "base"
    with pytest.raises(dash.DashboardCommandError):
        dash.build_dashboard_command("theme", "nope", known_themes=["base", "ink"])


def test_build_command_move_needs_ints():
    with pytest.raises(dash.DashboardCommandError):
        dash.build_dashboard_command("move_widget", "fleet", x=None, y=2)


def test_theme_catalog_hint_lists_light_and_dark(tmp_path):
    hint = dash.theme_catalog_hint(config_home=tmp_path)
    assert "base" in hint and "daylight" in hint
    assert "light UI" in hint and "not 'light'" in hint
    assert "ink" in hint


def test_format_widget_layout_uses_default_when_unsaved():
    text = dash.format_widget_layout(None, scope="global")
    assert "fleet @ (0,0) 8x3" in text
    assert "package default" in text


def test_format_widget_layout_shows_saved():
    layout = [{"key": "clock", "x": 9, "y": 0, "w": 3, "h": 2}]
    text = dash.format_widget_layout(layout, scope="workspace")
    assert "clock @ (9,0) 3x2" in text
    assert "saved" in text


# ── endpoint ────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    return TestClient(create_app(cfg))


def test_endpoint_get_returns_appearance(client):
    r = client.post("/api/dashboard/command", json={"op": "get"})
    assert r.status_code == 200
    body = r.json()
    assert "appearance" in body
    assert "themes" in body
    assert "daylight" in body["themes"]


def test_endpoint_widget_op_broadcasts_not_persisted(client):
    r = client.post("/api/dashboard/command", json={"op": "add_widget", "value": "fleet"})
    assert r.status_code == 200
    assert r.json()["command"] == {"op": "add_widget", "value": "fleet"}
    assert r.json()["persisted"] is False  # live grid op, browser applies


def test_theme_persists_headless_and_get_reflects_it(client):
    """The fix qn-dev surfaced: a scalar op must persist server-side (no browser
    needed) and `get` must reflect it — not stay on the previous value."""
    r = client.post("/api/dashboard/command", json={"op": "theme", "value": "ink"})
    assert r.status_code == 200 and r.json()["persisted"] is True
    got = client.post("/api/dashboard/command", json={"op": "get"})
    assert got.json()["appearance"]["theme"] == "ink"


def test_density_glow_persist_and_get_reflects(client):
    client.post("/api/dashboard/command", json={"op": "density", "value": "compact"})
    client.post("/api/dashboard/command", json={"op": "glow", "value": "off"})
    ap = client.post("/api/dashboard/command", json={"op": "get"}).json()["appearance"]
    assert ap["density"] == "compact"
    assert ap["glow"] == "off"


def test_endpoint_rejects_bad_op(client):
    r = client.post("/api/dashboard/command", json={"op": "explode"})
    assert r.status_code == 400


def test_endpoint_rejects_unknown_theme(client):
    # Builtins ship in-package; an unknown theme is rejected.
    r = client.post("/api/dashboard/command", json={"op": "theme", "value": "no-such-theme"})
    assert r.status_code == 400


# ── CLI ─────────────────────────────────────────────────────────────


def test_cli_dashboard_posts_command(monkeypatch):
    from relaydeck.transports import cli
    calls = []

    def fake(method, path, body=None, **kw):
        calls.append((method, path, body))
        return cli._POST_OK, {"ok": True, "command": body}

    monkeypatch.setattr(cli, "_json_to_daemon", fake)
    r = CliRunner().invoke(cli.main, ["dashboard", "theme", "ink"])
    assert r.exit_code == 0, r.output
    assert calls[-1] == ("POST", "/api/dashboard/command", {"op": "theme", "value": "ink"})

    r = CliRunner().invoke(cli.main, ["dashboard", "move", "usage", "8", "0"])
    assert r.exit_code == 0, r.output
    assert calls[-1] == ("POST", "/api/dashboard/command",
                         {"op": "move_widget", "value": "usage", "x": 8, "y": 0})


# ── plugin ships the skill ──────────────────────────────────────────


def test_dashboard_skill_validates():
    from relaydeck import skills
    plug = Path(__file__).resolve().parent.parent / "plugins" / "dashboard"
    ok, errors, _ = skills.validate_skill_dir(plug)
    assert ok, errors
    fm, _body = skills.parse_skill_md((plug / "SKILL.md").read_text())
    assert fm["name"] == "relaydeck-dashboard"
    assert fm.get("description")


# ── SSE bridge (the second half of the fix) ─────────────────────────


def test_appearance_changed_is_bridged_to_sse(tmp_path, monkeypatch):
    """Scalar dashboard ops emit `appearance.changed`; it must be bridged from
    the plugin bus to the SSE feed so connected dashboards repaint live. (The
    first e2e exposed this: get reflected the theme but the browser didn't.)"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.plugin import Event, PluginEventBus

    orch = get_orchestrator(cfg)
    forwarded = []
    monkeypatch.setattr(orch_mod._bus, "publish",
                        lambda *a, **k: forwarded.append(a[1] if len(a) > 1 else None))
    bus = PluginEventBus()
    orch.set_event_bus(bus)
    bus.emit(Event(type="appearance.changed", data={"workspace": None, "keys": ["theme"]}))
    assert "appearance.changed" in forwarded
