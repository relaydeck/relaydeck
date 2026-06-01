"""POST /api/update — managed-daemon gate, reinstall default, operator HOME."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True, exist_ok=True)
    import relaydeck.orchestrator as orch_mod

    orch_mod._orchestrator = None
    from relaydeck.transports.api import create_app

    return TestClient(create_app(cfg_home)), cfg_home


def test_update_refuses_when_daemon_unmanaged(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    res = client.post("/api/update")
    assert res.status_code == 409


def test_update_starts_helper_with_reinstall_and_operator_home(tmp_path, monkeypatch):
    client, cfg_home = _client(tmp_path, monkeypatch)
    (cfg_home / "daemon.pid").write_text(str(os.getpid()))

    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setenv(
        "RELAYDECK_UPDATE_CMD",
        "echo relaydeck-update-test",
    )
    monkeypatch.setattr("relaydeck.transports.api.subprocess.Popen", fake_popen)
    monkeypatch.setattr("relaydeck.transports.api.os.open", lambda *a, **k: 3)

    from relaydeck.transports import api as api_mod

    monkeypatch.setattr(api_mod, "_operator_login_home", lambda: "/operator-home")

    res = client.post("/api/update")
    assert res.status_code == 200
    assert res.json()["status"] == "updating"

    helper = captured["cmd"][-1]
    assert "echo" in helper and "relaydeck-update-test" in helper
    assert "'HOME': '/operator-home'" in helper
    assert "check=False" in helper


def test_update_default_cmd_is_reinstall(tmp_path, monkeypatch):
    client, cfg_home = _client(tmp_path, monkeypatch)
    (cfg_home / "daemon.pid").write_text(str(os.getpid()))
    monkeypatch.delenv("RELAYDECK_UPDATE_CMD", raising=False)

    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["helper"] = cmd[-1]
        return MagicMock()

    monkeypatch.setattr("relaydeck.transports.api.subprocess.Popen", fake_popen)
    monkeypatch.setattr("relaydeck.transports.api.os.open", lambda *a, **k: 3)

    res = client.post("/api/update")
    assert res.status_code == 200
    assert "install" in captured["helper"] and "reinstall" in captured["helper"]
