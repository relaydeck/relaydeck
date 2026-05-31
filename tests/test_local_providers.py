"""
Tests for local-model detection (relaydeck/local_providers.py) and its
HTTP surface. Detection is side-effect-free and fail-open; the TCP + HTTP
seams are injectable so these tests never touch a real port.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.local_providers import detect_local_providers

_OLLAMA_TAGS = {"models": [{"name": "llama3.2"}, {"name": "qwen2.5"}]}
_OPENAI_MODELS = {"data": [{"id": "facebook/opt-125m"}]}


def _tcp_only(*open_ports):
    open_ports = set(open_ports)
    return lambda host, port, timeout: port in open_ports


def test_detect_ollama_when_reachable():
    def fetch(url, timeout):
        return _OLLAMA_TAGS if "/api/tags" in url else None
    cands = detect_local_providers(
        _tcp=_tcp_only(11434), _fetch=fetch, configured_keys=set())
    assert len(cands) == 1
    c = cands[0]
    assert c.kind == "ollama"
    assert c.api == "ollama"
    assert c.base_url == "http://127.0.0.1:11434"  # native: no /v1
    assert c.model_count == 2
    assert "llama3.2" in c.models
    assert c.already_configured is False


def test_vllm_uses_openai_shape_and_v1_base():
    def fetch(url, timeout):
        return _OPENAI_MODELS if "/v1/models" in url else None
    cands = detect_local_providers(
        _tcp=_tcp_only(8000), _fetch=fetch, configured_keys=set())
    assert len(cands) == 1
    c = cands[0]
    assert c.kind == "vllm"
    assert c.api == "openai"
    assert c.base_url == "http://127.0.0.1:8000/v1"  # openai-compat needs /v1


def test_port_open_but_not_the_expected_server_is_skipped():
    # TCP connects but the catalog endpoint returns nothing → no false +.
    cands = detect_local_providers(
        _tcp=_tcp_only(11434), _fetch=lambda url, timeout: None,
        configured_keys=set())
    assert cands == []


def test_nothing_listening_returns_empty():
    cands = detect_local_providers(
        _tcp=lambda *a: False, _fetch=lambda *a: None, configured_keys=set())
    assert cands == []


def test_already_configured_flag_set_from_endpoint_keys():
    def fetch(url, timeout):
        return _OLLAMA_TAGS if "/api/tags" in url else None
    cands = detect_local_providers(
        _tcp=_tcp_only(11434), _fetch=fetch,
        configured_keys={"127.0.0.1:11434"})
    assert cands[0].already_configured is True


# ── HTTP surface ──────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from relaydeck.plugin import PluginContext, get_registry
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True, exist_ok=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    import relaydeck.providers_extra as px
    px._inited = False
    px._extra.clear()
    get_registry(cfg_home).load_all(PluginContext(config_home=cfg_home))
    from relaydeck.transports.api import create_app
    return TestClient(create_app(cfg_home))


def test_detect_endpoint_returns_candidates(client, monkeypatch):
    import relaydeck.local_providers as lp

    def fake_detect(config_home=None, **kw):
        return [lp.LocalCandidate(
            kind="ollama", label="Ollama", suggested_name="ollama",
            base_url="http://127.0.0.1:11434", api="ollama",
            model_count=3, models=["a", "b", "c"], already_configured=False)]
    monkeypatch.setattr(lp, "detect_local_providers", fake_detect)
    r = client.get("/api/providers/detect").json()
    assert len(r["candidates"]) == 1
    assert r["candidates"][0]["kind"] == "ollama"
    assert r["candidates"][0]["model_count"] == 3


def test_register_detected_remote_then_idempotent(client):
    body = {"name": "ollama-rig", "base_url": "http://10.0.0.4:11434", "api": "ollama"}
    r1 = client.post("/api/providers/detect", json=body)
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "created"
    assert r1.json()["key_env"] == ""
    # Now it exists → a second call is a no-op success (safe to retry).
    r2 = client.post("/api/providers/detect", json=body)
    assert r2.json()["status"] == "exists"
    rows = {x["name"]: x for x in client.get("/api/providers").json()}
    assert rows["ollama-rig"]["base_url"] == "http://10.0.0.4:11434"


def test_register_detected_existing_name_is_noop(client):
    # The bundled `ollama` provider already exists → exists, not 409.
    r = client.post("/api/providers/detect", json={
        "name": "ollama", "base_url": "http://127.0.0.1:11434", "api": "ollama"})
    assert r.json()["status"] == "exists"


def test_register_detected_rejects_invalid_name(client):
    # Same name rule as POST /api/providers — this route persists a provider,
    # so a bad name (won't round-trip through provider/model specs) is rejected.
    for bad in ("bad name", "Bad/Name", "x@y", "a.b"):
        r = client.post("/api/providers/detect", json={
            "name": bad, "base_url": "http://10.0.0.9:11434", "api": "ollama"})
        assert r.status_code == 400, f"{bad!r} should be rejected"
    # A clean name still works.
    ok = client.post("/api/providers/detect", json={
        "name": "ollama-rig", "base_url": "http://10.0.0.9:11434", "api": "ollama"})
    assert ok.status_code == 200 and ok.json()["status"] == "created"
