"""
Tests for provider configuration (the Providers settings section): the
base-URL override store, vault/env key resolution on ProviderPlugin, and
the /api/providers config endpoints.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck import provider_config as pc
from relaydeck.provider import ModelEntry, ProviderPlugin


class _P(ProviderPlugin):
    name = "tprov"
    provider_name = "tprov"
    key_env = "TPROV_API_KEY"
    default_base_url = "https://default.example/v1"

    def fetch_catalog(self):
        return [ModelEntry(id="m1", display_name="m1")]


def test_base_url_override_roundtrip(tmp_path):
    assert pc.get_base_url("tprov", config_home=tmp_path) is None
    pc.set_base_url("tprov", "https://proxy.example/v1", config_home=tmp_path)
    assert pc.get_base_url("tprov", config_home=tmp_path) == "https://proxy.example/v1"
    pc.set_base_url("tprov", None, config_home=tmp_path)
    assert pc.get_base_url("tprov", config_home=tmp_path) is None


def test_custom_provider_store(tmp_path):
    pc.upsert_custom({"id": "myco", "base_url": "https://x/v1"}, config_home=tmp_path)
    assert any(c["id"] == "myco" for c in pc.list_custom(config_home=tmp_path))
    assert pc.remove_custom("myco", config_home=tmp_path) is True
    assert pc.list_custom(config_home=tmp_path) == []


def test_resolved_api_key_prefers_env_when_no_vault(monkeypatch):
    p = _P()
    monkeypatch.delenv("TPROV_API_KEY", raising=False)
    assert p.resolved_api_key() in (None, "")  # nothing set
    monkeypatch.setenv("TPROV_API_KEY", "sk-env")
    assert p.resolved_api_key() == "sk-env"
    assert p.has_api_key() is True


def test_resolved_base_url_uses_override(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = _P()
    assert p.resolved_base_url() == "https://default.example/v1"
    pc.set_base_url("tprov", "https://override/v1")  # default config_home = ~/.relaydeck
    assert p.resolved_base_url() == "https://override/v1"


# ── HTTP ───────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from relaydeck.plugin import PluginContext, get_registry
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True, exist_ok=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    # Reset the extra-provider registry (module globals persist in-process).
    import relaydeck.providers_extra as px
    px._inited = False
    px._extra.clear()
    # Load bundled plugins (incl. providers) into the registry so
    # /api/providers is populated, mirroring `relaydeck serve`.
    get_registry(cfg_home).load_all(PluginContext(config_home=cfg_home))
    from relaydeck.transports.api import create_app
    return TestClient(create_app(cfg_home))


def test_providers_endpoint_has_config_fields(client):
    rows = client.get("/api/providers").json()
    assert rows, "bundled providers should be registered"
    by = {r["name"]: r for r in rows}
    assert "ollama" in by
    o = by["ollama"]
    assert "has_key" in o and "base_url" in o and "model_count" in o
    # openai needs a key
    if "openai" in by:
        assert by["openai"]["needs_key"] is True
        assert by["openai"]["key_env"] == "OPENAI_API_KEY"


def test_put_provider_base_url(client):
    r = client.put("/api/providers/ollama/config", json={"base_url": "http://box:11434"})
    assert r.status_code == 200
    assert r.json()["base_url"] == "http://box:11434"
    # reflected in the list
    rows = {x["name"]: x for x in client.get("/api/providers").json()}
    assert rows["ollama"]["base_url"] == "http://box:11434"


def test_put_unknown_provider_404(client):
    assert client.put("/api/providers/nope/config", json={"base_url": "x"}).status_code == 404


# ── Models lens: enriched providers/presets + preset stats ────────────


def _record_usage(client, model, provider, **kw):
    """Insert a usage_records row into the TestClient's daemon DB."""
    from relaydeck.db import open_db, record_usage
    from relaydeck.orchestrator import get_orchestrator
    db = get_orchestrator().db_path
    conn = open_db(db)
    try:
        record_usage(conn, "agentX", "sess", model, provider, **kw)
    finally:
        conn.close()


def test_providers_enriched_with_usage_and_presets(client):
    client.post("/api/presets", json={"name": "p-ollama", "provider": "ollama", "model": "gemma:4b"})
    _record_usage(client, "gemma:4b", "ollama", total_tokens=120, request_count=3, cost_usd=0.0)
    by = {r["name"]: r for r in client.get("/api/providers").json()}
    assert by["ollama"]["preset_count"] == 1
    assert "p-ollama" in by["ollama"]["presets"]
    assert by["ollama"]["tokens_24h"] == 120
    assert by["ollama"]["requests_24h"] == 3


def test_presets_carry_usage(client):
    client.post("/api/presets", json={"name": "p2", "provider": "ollama", "model": "llama3.2"})
    _record_usage(client, "llama3.2", "ollama", total_tokens=80, request_count=2)
    rows = {p["name"]: p for p in client.get("/api/presets").json()}
    assert rows["p2"]["tokens_24h"] == 80
    assert rows["p2"]["requests_24h"] == 2
    assert len(rows["p2"]["spark"]) == 24


def test_preset_stats_endpoint(client):
    client.post("/api/presets", json={"name": "p3", "provider": "ollama", "model": "qwen3"})
    _record_usage(client, "qwen3", "ollama", total_tokens=40, request_count=1)
    s = client.get("/api/presets/p3/stats").json()
    assert len(s["requests_series"]) == 60
    assert s["tokens_24h"] == 40
    assert s["latency"]["count"] == 0          # nothing traced → honest
    assert s["latency"]["success_rate"] is None
    assert s["recent"] == []
    assert any(u["agent_id"] == "agentX" for u in s["used_by"])


def test_preset_stats_404(client):
    assert client.get("/api/presets/nope/stats").status_code == 404


# ── known + custom providers (extra registry) ─────────────────────────


def test_known_providers_registered(client):
    rows = {r["name"]: r for r in client.get("/api/providers").json()}
    # A few verified OpenAI-compatible backends should be present.
    for name in ("groq", "vllm", "together", "deepseek", "gemini"):
        assert name in rows, f"{name} should be a known provider"
    assert rows["groq"]["base_url"] == "https://api.groq.com/openai/v1"
    assert rows["groq"]["key_env"] == "GROQ_API_KEY"
    assert rows["groq"]["api"] == "openai"
    assert rows["vllm"]["base_url"] == "http://localhost:8000/v1"


def test_known_provider_resolves(client):
    # resolve a `groq/<model>` spec to the groq provider.
    r = client.get("/api/models/resolve?spec=groq/llama-3.3-70b").json()
    assert r["provider"] == "groq"
    assert r["model"] == "llama-3.3-70b"


def test_create_and_delete_custom_provider(client):
    r = client.post("/api/providers", json={
        "name": "mybox", "base_url": "http://box:9000/v1", "api": "openai",
    })
    assert r.status_code == 200, r.text
    rows = {x["name"]: x for x in client.get("/api/providers").json()}
    assert "mybox" in rows
    assert rows["mybox"]["custom"] is True
    assert rows["mybox"]["base_url"] == "http://box:9000/v1"
    assert rows["mybox"]["key_env"] == "MYBOX_API_KEY"  # defaulted
    # delete it
    assert client.delete("/api/providers/mybox").status_code == 200
    rows2 = {x["name"]: x for x in client.get("/api/providers").json()}
    assert "mybox" not in rows2


def test_cannot_delete_known_provider(client):
    # groq is a known (non-custom) provider — DELETE refuses.
    assert client.delete("/api/providers/groq").status_code == 404


def test_create_custom_rejects_duplicate(client):
    assert client.post("/api/providers", json={"name": "groq", "base_url": "http://x/v1"}).status_code == 409


def test_create_custom_requires_base_url(client):
    assert client.post("/api/providers", json={"name": "nourl"}).status_code == 400


# ── native multi-endpoint local providers (Ollama) ────────────────────


def test_register_second_native_ollama_endpoint(client):
    # The bundled `ollama` (localhost) plus an operator-added remote.
    r = client.post("/api/providers", json={
        "name": "ollama-rig", "base_url": "http://192.168.1.50:11434",
        "api": "ollama",
    })
    assert r.status_code == 200, r.text
    # No API key required for a local endpoint.
    assert r.json()["key_env"] == ""
    rows = {x["name"]: x for x in client.get("/api/providers").json()}
    assert "ollama" in rows and "ollama-rig" in rows  # both, side by side
    rig = rows["ollama-rig"]
    assert rig["custom"] is True
    assert rig["needs_key"] is False
    assert rig["base_url"] == "http://192.168.1.50:11434"


def test_native_ollama_endpoint_is_real_ollama_provider(client, monkeypatch):
    # The remote must be a native OllamaProvider (full fidelity), not the
    # generic OpenAI shim — and not hijacked by the global OLLAMA_HOST env.
    monkeypatch.setenv("OLLAMA_HOST", "http://should-not-be-used:11434")
    client.post("/api/providers", json={
        "name": "ollama-rig", "base_url": "http://192.168.1.50:11434",
        "api": "ollama",
    })
    from relaydeck.plugin import get_provider
    from plugins.providers.ollama.plugin import OllamaProvider
    prov = get_provider("ollama-rig")
    assert isinstance(prov, OllamaProvider)
    assert prov.base_url == "http://192.168.1.50:11434"  # not OLLAMA_HOST


def test_native_ollama_endpoint_resolves(client):
    client.post("/api/providers", json={
        "name": "ollama-rig", "base_url": "http://192.168.1.50:11434",
        "api": "ollama",
    })
    r = client.get("/api/models/resolve?spec=ollama-rig/llama3.3").json()
    assert r["provider"] == "ollama-rig"
    assert r["model"] == "llama3.3"
    assert r["provider_known"] is True
