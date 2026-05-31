from __future__ import annotations

import sys
from pathlib import Path

from relaydeck.web_runtime import web_static_dir


def test_web_static_dir_defaults_to_in_tree_dashboard(monkeypatch):
    monkeypatch.delenv("RELAYDECK_WEB_STATIC_DIR", raising=False)
    monkeypatch.delenv("RELAYDECK_WEB_PACKAGE", raising=False)

    resolved = web_static_dir()

    assert resolved.name == "static"
    assert (resolved / "index.html").exists()


def test_web_static_dir_honors_dev_override(tmp_path, monkeypatch):
    static = tmp_path / "web-static"
    static.mkdir()
    monkeypatch.setenv("RELAYDECK_WEB_STATIC_DIR", str(static))

    assert web_static_dir() == static


def test_web_static_dir_can_resolve_external_package(tmp_path, monkeypatch):
    pkg = tmp_path / "relaydeck_web"
    static = pkg / "static"
    static.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("RELAYDECK_WEB_STATIC_DIR", raising=False)
    monkeypatch.setenv("RELAYDECK_WEB_PACKAGE", "relaydeck_web")
    sys.modules.pop("relaydeck_web", None)

    assert web_static_dir() == Path(str(static))


def test_web_static_dir_prefers_default_split_web_package(tmp_path, monkeypatch):
    pkg = tmp_path / "relaydeck_web"
    static = pkg / "static"
    static.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("RELAYDECK_WEB_STATIC_DIR", raising=False)
    monkeypatch.delenv("RELAYDECK_WEB_PACKAGE", raising=False)
    sys.modules.pop("relaydeck_web", None)

    assert web_static_dir() == Path(str(static))
