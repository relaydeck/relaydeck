"""
Plugin-contributed Home dashboard widgets.

A plugin declares `[plugin.ui] widgets = [...]` and the dashboard's Home
view surfaces them in the Add-widget gallery (mounting any `module` by
dynamic import). This pins the manifest parse → ui_manifest flow so the
contribution actually reaches `/api/plugins/ui`.
"""

from __future__ import annotations

from pathlib import Path

from relaydeck.plugin_manifest import load_manifest


def _manifest(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "plugin.toml"
    p.write_text(body)
    return p


def test_manifest_parses_widgets(tmp_path):
    m = load_manifest(_manifest(tmp_path, '''
[plugin]
name = "spotify"
version = "0.1"

[plugin.ui]
widgets = [
  { id = "nowplaying", title = "Now playing", icon = "music", module = "widget.js", description = "On your speakers.", def_w = 5, def_h = 2, min_w = 4, min_h = 2 },
]
'''))
    assert len(m.ui_widgets) == 1
    w = m.ui_widgets[0]
    assert w.id == "nowplaying"
    assert w.title == "Now playing"
    assert w.def_w == 5 and w.def_h == 2
    assert w.min_w == 4 and w.min_h == 2

    ui = m.ui_manifest()
    assert "widgets" in ui
    assert ui["widgets"][0]["id"] == "nowplaying"
    assert ui["widgets"][0]["def_w"] == 5
    assert ui["widgets"][0]["module"] == "widget.js"


def test_manifest_widgets_default_empty(tmp_path):
    m = load_manifest(_manifest(tmp_path, '[plugin]\nname = "x"\nversion = "0.1"\n'))
    assert m.ui_widgets == ()
    assert m.ui_manifest()["widgets"] == []


def test_widget_sizes_optional(tmp_path):
    """Sizing is optional — the dashboard picks a default when omitted."""
    m = load_manifest(_manifest(tmp_path, '''
[plugin]
name = "p"
version = "0.1"
[plugin.ui]
widgets = [ { id = "w", title = "W" } ]
'''))
    d = m.ui_manifest()["widgets"][0]
    assert "def_w" not in d and "def_h" not in d
