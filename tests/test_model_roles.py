"""
Model roles — the "default model per job" platform primitive.

Covers the registry (relaydeck/model_roles.py), the resolver integration
(`role:<name>` in relaydeck.sdk.resolve_model), the HTTP surface
(/api/model-roles + the role source on /api/models/resolve), and the
plugin manifest's `required_model_roles` declaration.

Real SQLite + real FastAPI TestClient, isolated tmp config home.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck import model_roles as mr
from relaydeck.sdk import resolve_model


# ── Registry ─────────────────────────────────────────────────────────


def test_builtin_roles_present():
    names = {r.name for r in mr.builtin_roles()}
    assert {"fast", "frontier", "classifier", "embedding", "vision",
            "voice", "image"} <= names
    assert mr.is_role("classifier")
    assert not mr.is_role("nope")


def test_no_role_ships_a_fallback():
    # The package ships zero picks: every role needs operator config, none
    # falls back to an assumed local/frontier model.
    for r in mr.builtin_roles():
        assert r.fallback == "", f"{r.name} must ship no fallback"


def test_set_get_unset_roundtrip(tmp_path):
    assert mr.configured_spec("classifier", tmp_path) is None
    mr.set_role_default("classifier", "anthropic/claude-haiku-4-5", tmp_path)
    assert mr.configured_spec("classifier", tmp_path) == "anthropic/claude-haiku-4-5"
    # Persisted to model-defaults.yaml under the given home.
    assert (tmp_path / "model-defaults.yaml").exists()
    assert mr.unset_role_default("classifier", tmp_path) is True
    assert mr.configured_spec("classifier", tmp_path) is None
    assert mr.unset_role_default("classifier", tmp_path) is False


def test_set_rejects_unknown_role_and_empty_spec(tmp_path):
    with pytest.raises(ValueError):
        mr.set_role_default("bogus", "x", tmp_path)
    with pytest.raises(ValueError):
        mr.set_role_default("classifier", "  ", tmp_path)


def test_load_ignores_unknown_roles_and_blank(tmp_path):
    (tmp_path / "model-defaults.yaml").write_text(
        "roles:\n  classifier: anthropic/x\n  bogus: y\n  voice: ''\n"
    )
    loaded = mr.load_role_defaults(tmp_path)
    assert loaded == {"classifier": "anthropic/x"}


def test_effective_vs_configured(tmp_path):
    # With no fallbacks, an unset role is None either way.
    assert mr.configured_spec("fast", tmp_path) is None
    assert mr.effective_spec("fast", tmp_path) is None
    assert mr.effective_spec("voice", tmp_path) is None
    # Operator default is what makes it resolve.
    mr.set_role_default("fast", "ollama/qwen2.5", tmp_path)
    assert mr.effective_spec("fast", tmp_path) == "ollama/qwen2.5"


def test_role_status_sources(tmp_path):
    mr.set_role_default("classifier", "anthropic/x", tmp_path)
    by = {r["name"]: r for r in mr.role_status(tmp_path)}
    assert by["classifier"]["source"] == "default"
    # No fallbacks → every unset role is "unset" (an onboarding gap).
    assert by["fast"]["source"] == "unset"
    assert by["voice"]["source"] == "unset"
    assert by["voice"]["has_fallback"] is False


def test_resolve_role_require(tmp_path):
    # Every unset role → require raises with an actionable hint (no fallback).
    with pytest.raises(ValueError) as e:
        mr.resolve_role("fast", tmp_path, require=True)
    assert "relaydeck defaults set fast" in str(e.value)
    with pytest.raises(ValueError):
        mr.resolve_role("voice", tmp_path, require=True)
    # Unknown role.
    with pytest.raises(ValueError):
        mr.resolve_role("nope", tmp_path, require=True)
    # Once configured, it resolves.
    mr.set_role_default("fast", "ollama/qwen2.5", tmp_path)
    assert mr.resolve_role("fast", tmp_path) == "ollama/qwen2.5"


# ── Resolver integration (role: prefix) ──────────────────────────────


def test_resolve_model_role_unset_raises(tmp_path):
    # No assumed fallback — an unset role can't resolve until configured.
    for role in ("role:fast", "role:embedding", "role:vision", "role:image"):
        with pytest.raises(ValueError):
            resolve_model(role, tmp_path)


def test_resolve_model_role_to_provider_model(tmp_path):
    mr.set_role_default("voice", "gemini/gemini-2.0-flash", tmp_path)
    assert resolve_model("role:voice", tmp_path) == ("gemini", "gemini-2.0-flash")


def test_resolve_model_role_once_configured(tmp_path):
    mr.set_role_default("fast", "ollama/qwen2.5", tmp_path)
    assert resolve_model("role:fast", tmp_path) == ("ollama", "qwen2.5")


def test_resolve_model_role_through_preset(tmp_path):
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "haiku.yaml").write_text(
        "name: haiku\nprovider: anthropic\nmodel: claude-haiku-4-5\n"
    )
    mr.set_role_default("classifier", "haiku", tmp_path)
    assert resolve_model("role:classifier", tmp_path) == ("anthropic", "claude-haiku-4-5")


def test_resolve_model_role_cycle_guarded(tmp_path):
    # A hand-edited file that points a role at itself must not recurse
    # forever — the depth guard converts it to an error.
    (tmp_path / "model-defaults.yaml").write_text(
        "roles:\n  fast: role:fast\n"
    )
    with pytest.raises(RuntimeError):
        resolve_model("role:fast", tmp_path)


# ── config_home threading on the completion path (finding 1) ─────────


def _stub_provider(monkeypatch, name="faux"):
    """Make get_provider(name) return a stub whose complete() echoes the
    resolved model id, so a test can assert which model was resolved."""
    import relaydeck.plugin as plug
    seen = []

    class _Stub:
        def complete(self, prompt, *, model, max_tokens=256, **kw):
            return f"ran:{model}"

    def fake_get(n):
        seen.append(n)
        return _Stub() if n == name else None

    monkeypatch.setattr(plug, "get_provider", fake_get)
    return seen


def test_complete_with_model_threads_config_home(tmp_path, monkeypatch):
    # role:fast configured ONLY in tmp_path. If config_home isn't threaded,
    # resolve hits the default home where fast is unset → it can't resolve
    # to "faux", so this asserts the home is threaded through.
    mr.set_role_default("fast", "faux/special-model", tmp_path)
    seen = _stub_provider(monkeypatch)
    from relaydeck.sdk import complete_with_model
    out = complete_with_model("hi", model="role:fast", config_home=tmp_path)
    assert out == "ran:special-model"
    assert seen == ["faux"]


def test_timed_complete_threads_config_home(tmp_path, monkeypatch):
    mr.set_role_default("fast", "faux/m2", tmp_path)
    _stub_provider(monkeypatch)
    from relaydeck.model_invocations import timed_complete
    out = timed_complete("agent", "hi", model="role:fast", config_home=tmp_path)
    assert out == "ran:m2"


def test_gateway_host_complete_uses_its_config_home(tmp_path, monkeypatch):
    mr.set_role_default("classifier", "faux/cls", tmp_path)
    _stub_provider(monkeypatch)
    from relaydeck.sdk import ModelGatewayHost, _CapabilityGate
    host = ModelGatewayHost(_CapabilityGate({"models.complete"}), tmp_path)
    assert host.complete("hi", model="role:classifier") == "ran:cls"


# ── cycle rejection at the setter (finding 2) ────────────────────────


def test_set_rejects_self_cycle_without_persisting(tmp_path):
    with pytest.raises(ValueError):
        mr.set_role_default("fast", "role:fast", tmp_path)
    assert mr.configured_spec("fast", tmp_path) is None


def test_set_rejects_longer_cycle_without_persisting(tmp_path):
    # fast → role:frontier is fine (frontier is unset → the chain just ends).
    mr.set_role_default("fast", "role:frontier", tmp_path)
    # But pointing frontier back at fast closes the loop → rejected.
    with pytest.raises(ValueError):
        mr.set_role_default("frontier", "role:fast", tmp_path)
    assert mr.configured_spec("frontier", tmp_path) is None
    # The earlier (valid) default is untouched.
    assert mr.configured_spec("fast", tmp_path) == "role:frontier"


# ── HTTP surface ─────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    from relaydeck.transports.api import create_app
    return TestClient(create_app(cfg_home)), cfg_home


def test_api_list_roles(client):
    c, _ = client
    r = c.get("/api/model-roles")
    assert r.status_code == 200
    body = r.json()
    names = {row["name"] for row in body["roles"]}
    assert "classifier" in names and "voice" in names
    assert body["unmet"] == {}


def test_api_set_and_clear_role(client):
    c, cfg_home = client
    r = c.put("/api/model-roles/voice", json={"spec": "gemini/gemini-2.0-flash"})
    assert r.status_code == 200
    assert r.json()["spec"] == "gemini/gemini-2.0-flash"
    assert mr.configured_spec("voice", cfg_home) == "gemini/gemini-2.0-flash"
    # Cleared.
    r = c.delete("/api/model-roles/voice")
    assert r.status_code == 200 and r.json()["cleared"] is True
    assert mr.configured_spec("voice", cfg_home) is None


def test_api_set_unknown_role_404(client):
    c, _ = client
    assert c.put("/api/model-roles/bogus", json={"spec": "x"}).status_code == 404


def test_api_set_role_cycle_rejected_400(client):
    c, cfg_home = client
    r = c.put("/api/model-roles/fast", json={"spec": "role:fast"})
    assert r.status_code == 400
    assert mr.configured_spec("fast", cfg_home) is None


def test_api_resolve_endpoint_role_source(client):
    c, cfg_home = client
    mr.set_role_default("classifier", "anthropic/claude-haiku-4-5", cfg_home)
    r = c.get("/api/models/resolve", params={"spec": "role:classifier"})
    body = r.json()
    assert body["source"] == "role"
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-haiku-4-5"


def test_api_resolve_unset_modality_role_is_invalid_not_500(client):
    c, _ = client
    r = c.get("/api/models/resolve", params={"spec": "role:voice"})
    assert r.status_code == 200
    assert r.json()["valid"] is False


# ── Plugin manifest declaration ──────────────────────────────────────


def test_manifest_parses_required_model_roles(tmp_path):
    from relaydeck.plugin_manifest import load_manifest
    p = tmp_path / "plugin.toml"
    p.write_text(
        '[plugin]\nname = "voicegw"\nversion = "0.1.0"\n'
        'required_model_roles = ["voice", "transcribe"]\n'
    )
    m = load_manifest(p)
    assert m.required_model_roles == ("voice", "transcribe")


def test_manifest_required_roles_default_empty(tmp_path):
    from relaydeck.plugin_manifest import load_manifest
    p = tmp_path / "plugin.toml"
    p.write_text('[plugin]\nname = "x"\nversion = "0.1.0"\n')
    assert load_manifest(p).required_model_roles == ()
