"""
Tests for the standardized model resolver endpoint (/api/models/resolve)
that backs the shared model_select component.

Pin the source classification + resolution: preset name, built-in alias,
explicit provider/model, and a bare id (which warns about the openrouter
default). Validation is fail-open — it never blocks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True, exist_ok=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    from relaydeck.transports.api import create_app
    return TestClient(create_app(cfg_home)), cfg_home


def _resolve(client, spec):
    return client.get(f"/api/models/resolve?spec={spec}").json()


def test_empty_is_default(client):
    c, _ = client
    r = _resolve(c, "")
    assert r["source"] == "default"
    assert r["provider"] is None


def test_no_assumed_aliases(client):
    # The package ships zero model picks: the former local-fast/frontier
    # aliases are NOT special anymore — a bare name is just a bare id.
    c, _ = client
    r = _resolve(c, "local-fast")
    assert r["source"] == "bare"
    assert r["provider"] == "openrouter"  # bare → openrouter (with a warning)
    assert r["warning"]


def test_preset_list_ships_empty(client):
    # No built-in presets — empty until the operator creates one.
    c, _ = client
    assert c.get("/api/presets").json() == []


def test_unconfigured_role_is_invalid_not_500(client):
    # With no fallbacks, an unset role doesn't resolve — the picker shows it
    # as invalid (needs onboarding), never a 500.
    c, _ = client
    r = _resolve(c, "role:fast")
    assert r["valid"] is False
    assert r["source"] == "role"
    assert "configured" in (r["warning"] or "").lower()


def test_provider_model(client):
    c, _ = client
    r = _resolve(c, "anthropic/some-model")
    assert r["source"] == "provider/model"
    assert r["provider"] == "anthropic"
    assert r["model"] == "some-model"


def test_bare_warns_about_openrouter_default(client):
    c, _ = client
    r = _resolve(c, "gemma3:1b")
    assert r["source"] == "bare"
    assert r["provider"] == "openrouter"
    assert r["warning"] and "default" in r["warning"].lower()


def test_preset(client):
    c, cfg_home = client
    presets = cfg_home / "presets"
    presets.mkdir(parents=True, exist_ok=True)
    (presets / "fast.yaml").write_text(
        "name: fast\nprovider: ollama\nmodel: gemma3:1b\n"
    )
    r = _resolve(c, "fast")
    assert r["source"] == "preset"
    assert r["preset"] == "fast"
    assert r["provider"] == "ollama"
    assert r["model"] == "gemma3:1b"


def test_resolve_enriches_from_models_dev(client, monkeypatch):
    """When models.dev knows the model, resolve surfaces capabilities + price
    and softens the 'not in live catalog' warning."""
    c, _ = client
    monkeypatch.setattr("relaydeck.models_dev.get_model_meta",
                        lambda p, m, *a, **k: {"id": m, "cost": {"input": 3.0, "output": 15.0}})
    monkeypatch.setattr("relaydeck.models_dev.model_capabilities",
                        lambda p, m, *a, **k: ["tools", "reasoning", "vision"])
    monkeypatch.setattr("relaydeck.models_dev.get_price",
                        lambda p, m, *a, **k: (3.0, 15.0))
    r = _resolve(c, "anthropic/claude-sonnet-4-6")
    assert r["models_dev_known"] is True
    assert r["capabilities"] == ["tools", "reasoning", "vision"]
    assert r["price"] == {"input": 3.0, "output": 15.0}


def test_resolve_no_models_dev_is_unchanged(client):
    """models.dev disabled (suite-wide) → enrichment fields are empty, the
    endpoint still resolves normally with no network."""
    c, _ = client
    r = _resolve(c, "anthropic/claude-sonnet-4-6")
    assert r["models_dev_known"] is False
    assert r["capabilities"] == []
    assert r["price"] is None


def test_provider_logo_proxy_404_when_no_logo(client, monkeypatch):
    """The logo proxy serves from the LOCAL cache only; no cached logo → 404
    (the dashboard falls back to a placeholder, no broken layout)."""
    c, _ = client
    monkeypatch.setattr("relaydeck.models_dev.fetch_logo", lambda name, ch=None: None)
    r = c.get("/api/providers/anthropic/logo")
    assert r.status_code == 404


def test_provider_logo_proxy_serves_svg(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr("relaydeck.models_dev.fetch_logo",
                        lambda name, ch=None: b"<svg>x</svg>")
    r = c.get("/api/providers/anthropic/logo")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.content == b"<svg>x</svg>"
