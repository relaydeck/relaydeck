"""
Plugin-contributed TUI tabs — the terminal-side mirror of [plugin.ui].

A plugin declares `[plugin.tui] tabs` + a data endpoint; the daemon
aggregates them onto /api/plugins/ui; `relaydeck view` discovers them and
renders the endpoint's content in a shared pane. No plugin widget code runs
in the client. Covers: the manifest parse, autopilot's demo tab content, and
the view client (headless pilot) — including that selecting a plugin tab
never unmounts the terminal.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relaydeck.plugin_manifest as m
import relaydeck.transports.view as view
from relaydeck.plugin import PluginContext, PluginEventBus


# ── manifest ───────────────────────────────────────────────────────


def test_plugin_tui_tabs_parse(tmp_path):
    p = tmp_path / "plugin.toml"
    p.write_text(
        '[plugin]\nname = "demo"\nversion = "0.1.0"\n'
        '[plugin.tui]\n'
        'tabs = [ { id = "panel", title = "Demo", endpoint = "tui", order = 50 } ]\n'
    )
    man = m.load_manifest(p)
    assert len(man.tui_tabs) == 1
    t = man.tui_tabs[0]
    assert (t.id, t.title, t.endpoint, t.order) == ("panel", "Demo", "tui", 50)
    assert man.ui_manifest()["tui"] == [
        {"id": "panel", "title": "Demo", "endpoint": "tui", "order": 50}
    ]


def test_plugin_tui_tab_requires_id(tmp_path):
    p = tmp_path / "plugin.toml"
    p.write_text(
        '[plugin]\nname = "demo"\nversion = "0.1.0"\n'
        '[plugin.tui]\ntabs = [ { title = "no id" } ]\n'
    )
    with pytest.raises(m.ManifestError):
        m.load_manifest(p)


def test_autopilot_ships_a_tui_tab():
    man = m.load_manifest(Path("plugins/autopilot/plugin.toml"))
    ids = {t.id for t in man.tui_tabs}
    assert "autopilot" in ids
    assert any(t.endpoint == "tui" for t in man.tui_tabs)


# ── autopilot _tui_lines (the demo content) ────────────────────────


def test_autopilot_tui_lines_show_rules_and_recent(tmp_path):
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    ctx = PluginContext(config_home=tmp_path, event_bus=PluginEventBus(),
                        orchestrator=MagicMock())
    from plugins.autopilot.plugin import _legacy_on_load
    pl = _legacy_on_load(ctx)
    pl._recent.append("unblocked alice · press-enter")
    text = "\n".join(pl._tui_lines())
    assert "mode:" in text
    assert "press-enter" in text          # a rule
    assert "unblocked alice" in text      # the recent action


# ── view client (headless pilot) ───────────────────────────────────


class _Agents:
    def list(self):
        return []


class _Host:
    daemon_url = "http://127.0.0.1:0"

    def __init__(self):
        self.agents = _Agents()
        self.requested: list[str] = []

    def _request(self, path, *a, **k):
        self.requested.append(path)
        if path == "/api/plugins/ui":
            return {
                "tabs": [], "header_chips": [], "agent_tiles": [], "widgets": [],
                "tui": [{
                    "id": "autopilot:autopilot", "title": "Autopilot",
                    "endpoint": "/api/plugins/autopilot/tui", "plugin": "autopilot",
                }],
            }
        if path == "/api/plugins/autopilot/tui":
            return {"title": "Autopilot", "lines": ["mode: benign", "rules: ..."]}
        return []


def _noop_sse(host, path, on_event, stop):
    return


async def test_view_discovers_plugin_tab_fetches_it_and_keeps_pty_mounted(monkeypatch):
    monkeypatch.setattr(view, "_sse_worker", _noop_sse)
    t = view._import_textual()
    host = _Host()
    app = view._build_app(t, host, initial_workspace=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Discovered from /api/plugins/ui and appended after the 4 fixed tabs.
        assert len(app._plugin_tabs) == 1
        tabs = app._tabs()
        assert len(tabs) == 5
        assert tabs[-1] == ("plugin:0", "Autopilot")

        pty_before = app.query_one("#pty")
        await app._set_tab("plugin:0")
        await pilot.pause()
        assert app._active_tab == "plugin:0"
        assert app.query_one("#plugin").display          # plugin pane visible
        assert not app.query_one("#pty").display          # terminal hidden
        assert app.query_one("#pty") is pty_before        # NOT remounted
        # The tab's endpoint was fetched to render content.
        assert "/api/plugins/autopilot/tui" in host.requested
