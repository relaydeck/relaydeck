"""Plugin + dashboard static assets must carry a revalidation header.

Regression guard for the bug where a plugin's static module (e.g. the
telegram lens `panel.js`) was served by a bare `StaticFiles` mount with NO
`Cache-Control`, so the browser kept serving a stale pre-fix module from its
heuristic cache — the telegram Plugin Settings card stayed clipped even after
a daemon restart + reload. Both the broad `/static` mount and every
`/static/plugins/<name>/` mount now share `_no_cache_staticfiles`, which tags
responses `no-cache, must-revalidate` (ETag 304 fast-path still applies).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from relaydeck.transports.cli import _no_cache_staticfiles


def _client_for(tmp_path, body: str = "export default class X {}\n"):
    (tmp_path / "panel.js").write_text(body)
    app = FastAPI()
    app.mount("/static/plugins/telegram",
              _no_cache_staticfiles(str(tmp_path)),
              name="plugin-telegram")
    return TestClient(app)


def test_plugin_static_sends_no_cache(tmp_path):
    client = _client_for(tmp_path)
    res = client.get("/static/plugins/telegram/panel.js")
    assert res.status_code == 200
    cc = res.headers.get("cache-control", "")
    assert "no-cache" in cc and "must-revalidate" in cc


def test_plugin_static_still_serves_body(tmp_path):
    # The revalidation header must not clobber the actual file content.
    client = _client_for(tmp_path, body="// telegram lens\n")
    res = client.get("/static/plugins/telegram/panel.js")
    assert res.text == "// telegram lens\n"
    # ETag is still present so the browser gets the 304 fast-path.
    assert res.headers.get("etag")
