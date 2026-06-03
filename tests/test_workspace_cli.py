from pathlib import Path


def test_remove_workspace_via_daemon_url_encodes_name(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from relaydeck.state import set_daemon_url
    from relaydeck.transports import cli

    set_daemon_url("http://127.0.0.1:8765")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *_args):
            return b""

    def fake_urlopen(req, timeout=5, context=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["context"] = context
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ok, msg = cli._remove_workspace_via_daemon("demo workspace#1")

    assert (ok, msg) == (True, "ok")
    assert captured["url"].endswith("/api/workspaces/demo%20workspace%231")
