"""Tests for the redesigned dashboard's structural contract.

These tests pin behavior the dashboard relies on, not pixel-level
rendering (which lives in the browser). Specifically:

- index.html boots the ES-module app and references the right URLs
- /static/ serves the new file layout (app.js, lenses/, tiles/, …)
- plugin manifests can declare tile-system fields (default_state,
  description, protected, source)
- the "tiles" alias works alongside "agent_tiles"
- plugins promoted to lenses via default_state="lens" survive the
  parser
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relaydeck.plugin_manifest import ManifestError, load_manifest
from relaydeck.transports.api import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    monkeypatch.setenv("RELAYDECK_CONFIG_HOME", str(cfg_home))
    app = create_app(cfg_home)
    return TestClient(app)


# ── index.html boot contract ────────────────────────────────────────


def test_index_loads_es_module_app(client):
    """The redesign drops the inline-script monolith for a single
    <script type=module> entrypoint at /static/app.js. The dashboard
    endpoint also stamps every same-origin module URL with ?v=<pid>
    for cache-busting, so we match on the stamped form."""
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    import re
    assert re.search(r'<script type="module" src="/static/app\.js\?v=\d+"></script>', body)
    assert "/static/styles.css" in body
    assert "/static/panels.css" in body
    # The importmap is what gets dynamic imports (./lenses/agents.js)
    # to resolve to ?v=… URLs too — without it dynamic imports would
    # bypass the stamp.
    assert '<script type="importmap">' in body
    assert '"/static/lenses/agents.js":' in body


# ── Plugin manifest: tile system fields ─────────────────────────────


def _write_manifest(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "plugin.toml"
    p.write_text(body)
    return p


def test_manifest_accepts_tiles_alias(tmp_path):
    """`tiles = [...]` should be accepted as an alias for `agent_tiles`."""
    p = _write_manifest(tmp_path, """
[plugin]
name = "x"
version = "0.1.0"

[[plugin.ui.tiles]]
id = "inbox"
title = "Inbox"
icon = "inbox"
module = "tile.js"
default_state = "pop"
description = "Peer messages."
""")
    m = load_manifest(p)
    assert len(m.ui_agent_tiles) == 1
    t = m.ui_agent_tiles[0]
    assert t.id == "inbox"
    assert t.default_state == "pop"
    assert t.description == "Peer messages."


def test_manifest_rejects_both_tiles_and_agent_tiles(tmp_path):
    p = _write_manifest(tmp_path, """
[plugin]
name = "x"
version = "0.1.0"

[plugin.ui]
tiles = [ { id = "a", module = "a.js" } ]
agent_tiles = [ { id = "b", module = "b.js" } ]
""")
    with pytest.raises(ManifestError):
        load_manifest(p)


def test_manifest_default_state_validates(tmp_path):
    p = _write_manifest(tmp_path, """
[plugin]
name = "x"
version = "0.1.0"

[plugin.ui]
tiles = [ { id = "a", module = "a.js", default_state = "tab" } ]
""")
    m = load_manifest(p)
    assert m.ui_agent_tiles[0].default_state == "tab"

    bad = _write_manifest(tmp_path, """
[plugin]
name = "x"
version = "0.1.0"

[plugin.ui]
tiles = [ { id = "a", module = "a.js", default_state = "elsewhere" } ]
""")
    with pytest.raises(ManifestError):
        load_manifest(bad)


def test_manifest_tab_with_lens_default_state(tmp_path):
    """Plugins promoting their tab to a top-level rail lens use
    default_state="lens". The parser must accept it and pass it through."""
    p = _write_manifest(tmp_path, """
[plugin]
name = "github"
version = "0.1.0"

[[plugin.ui.tabs]]
id = "github"
title = "GitHub"
icon = "git"
module = "panel.js"
default_state = "lens"
""")
    m = load_manifest(p)
    assert m.ui_tabs[0].default_state == "lens"
    d = m.ui_tabs[0].to_dict()
    assert d["default_state"] == "lens"


def test_manifest_ui_serialization_preserves_extended_fields(tmp_path):
    p = _write_manifest(tmp_path, """
[plugin]
name = "x"
version = "0.1.0"

[[plugin.ui.tiles]]
id = "core-thing"
title = "Thing"
icon = "agent"
module = "t.js"
default_state = "tab"
description = "what it is"
protected = true
source = "core"
""")
    m = load_manifest(p)
    d = m.ui_agent_tiles[0].to_dict()
    assert d["default_state"] == "tab"
    assert d["description"] == "what it is"
    assert d["protected"] is True
    assert d["source"] == "core"

