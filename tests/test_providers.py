"""
Tests for the provider catalog plugin layer.

Covers:
- ProviderPlugin base: disk cache hit/miss, fuzzy-suggest validation.
- Bundled providers load and register their static catalogs.
- Ollama plugin handles an unreachable daemon gracefully.
- Pre-spawn validation in HarnessAgent emits `preset.invalid` and
  refuses to start when the preset names a missing model.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.plugin import PluginContext, get_registry
from relaydeck.provider import ModelEntry, ProviderPlugin, get_provider, list_providers
from relaydeck.sdk import get_provider as sdk_get_provider
from relaydeck.sdk import list_providers as sdk_list_providers

# ── ProviderPlugin base behavior ────────────────────────────────────


class _FakeProvider(ProviderPlugin):
    name = "fake"
    provider_name = "fake"

    def __init__(self, catalog: list[ModelEntry]):
        super().__init__()
        self._fake_catalog = catalog
        self.fetch_calls = 0

    def fetch_catalog(self) -> list[ModelEntry]:
        self.fetch_calls += 1
        return list(self._fake_catalog)


def test_provider_lookup_exports_match_sdk_facade():
    assert get_provider is sdk_get_provider
    assert list_providers is sdk_list_providers


def test_provider_list_models_fetches_once_then_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = _FakeProvider([ModelEntry(id="m1"), ModelEntry(id="m2")])
    models = p.list_models()
    assert {m.id for m in models} == {"m1", "m2"}
    assert p.fetch_calls == 1

    # Second call serves from in-memory cache.
    p.list_models()
    assert p.fetch_calls == 1


def test_provider_refresh_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = _FakeProvider([ModelEntry(id="m1")])
    p.refresh()
    cache = tmp_path / ".relaydeck" / "cache" / "providers" / "fake.json"
    assert cache.exists(), "expected disk cache to be written"
    assert "m1" in cache.read_text()

    # A fresh instance should pick up the cached blob without calling fetch.
    p2 = _FakeProvider([ModelEntry(id="m999")])  # different upstream
    models = p2.list_models()
    assert {m.id for m in models} == {"m1"}, "fresh instance should read from disk cache"
    assert p2.fetch_calls == 0


def test_provider_validate_exact_match():
    p = _FakeProvider([ModelEntry(id="anthropic/claude-3-5-haiku"),
                       ModelEntry(id="openai/gpt-4o")])
    ok, sug = p.validate("anthropic/claude-3-5-haiku")
    assert ok and sug is None


def test_provider_validate_fuzzy_suggests_closest():
    p = _FakeProvider([
        ModelEntry(id="anthropic/claude-3-5-haiku"),
        ModelEntry(id="anthropic/claude-3-5-sonnet"),
        ModelEntry(id="openai/gpt-4o"),
    ])
    # User's bug: typed "claude-haiku-3.5" — wrong order, wrong separators.
    ok, sug = p.validate("claude-haiku-3.5")
    assert ok is False
    assert sug is not None and "haiku" in sug


def test_provider_validate_unprefixed_id_accepted():
    p = _FakeProvider([ModelEntry(id="anthropic/claude-3-5-haiku")])
    # If user wrote bare "claude-3-5-haiku", we treat the matching
    # suffix as a hit (the provider plugin already knows the namespace).
    ok, _ = p.validate("claude-3-5-haiku")
    assert ok is True


def test_provider_empty_catalog_fails_open(tmp_path, monkeypatch):
    # Isolate the disk cache from earlier test runs — otherwise a stale
    # ~/.relaydeck/cache/providers/fake.json would poison this case.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = _FakeProvider([])
    ok, _ = p.validate("anything-here")
    assert ok is True


# ── Bundled providers load + ship static catalogs ───────────────────


def _load_bundled_providers(tmp_path):
    """Force-load relaydeck's bundled providers into the registry against an
    isolated config_home so we don't write to the real ~/.config."""
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    # Reset singleton so each test gets a fresh registry.
    import relaydeck.plugin as plug
    plug._registry = None
    reg = get_registry(cfg)
    reg.load_all(PluginContext(config_home=cfg))
    return reg


def test_bundled_anthropic_provider_ships_static_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _load_bundled_providers(tmp_path)
    p = get_provider("anthropic")
    assert p is not None
    models = p.list_models()
    ids = {m.id for m in models}
    assert "claude-3-5-haiku" in ids
    assert "claude-opus-4-7" in ids


def test_bundled_openai_provider_ships_static_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _load_bundled_providers(tmp_path)
    p = get_provider("openai")
    assert p is not None
    ids = {m.id for m in p.list_models()}
    assert "gpt-4o" in ids and "gpt-4o-mini" in ids


def test_openrouter_modality_string_becomes_capability_tokens():
    from plugins.providers.openrouter.plugin import _capabilities_from_modality

    assert _capabilities_from_modality("text->image") == ["text", "image"]
    assert _capabilities_from_modality("text+vision") == ["text", "vision"]
    assert _capabilities_from_modality(["text", "audio"]) == ["text", "audio"]


def test_ollama_provider_unreachable_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _load_bundled_providers(tmp_path)
    p = get_provider("ollama")
    assert p is not None
    # Hit a port that's almost certainly closed.
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")
    p.refresh()
    # Refresh failed; falls back to empty (no cache yet either).
    # The important contract: this does NOT raise.
    assert isinstance(p.list_models(), list)


def test_list_providers_returns_all_bundled(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _load_bundled_providers(tmp_path)
    names = {p.provider_name for p in list_providers()}
    assert {"openrouter", "anthropic", "openai", "ollama"} <= names


# ── Pre-spawn validation in HarnessAgent ────────────────────────────


def _write_preset(cfg_home: Path, name: str, provider: str, model: str) -> None:
    import yaml
    presets = cfg_home / "presets"
    presets.mkdir(parents=True, exist_ok=True)
    (presets / f"{name}.yaml").write_text(yaml.safe_dump({
        "name": name, "provider": provider, "model": model,
    }))


def test_harness_pre_spawn_validation_aborts_on_unknown_model(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _load_bundled_providers(tmp_path)
    cfg = tmp_path / ".relaydeck"
    _write_preset(cfg, "bad", provider="anthropic", model="claude-haiku-3.5")

    from relaydeck.harness import HarnessAgent

    class _Echo(HarnessAgent):
        CLI = "/bin/echo"
        HARNESS_TYPE = "relaydeck"

    db = tmp_path / "relaydeck.db"
    agent = _Echo(
        agent_id="t1", name="t1",
        config={"model": "bad"},
        workspace=None, db_path=str(db),
        stop_flag=threading.Event(),
    )

    # Capture emit() so we can assert preset.invalid was published.
    captured: list[tuple[str, dict]] = []
    orig_emit = agent.emit
    def _capture(t, p=None):
        captured.append((t, p or {}))
        return orig_emit(t, p)
    agent.emit = _capture  # type: ignore

    agent.run()  # synchronous — should abort before subprocess.Popen

    types = [t for t, _ in captured]
    assert "preset.invalid" in types, f"expected preset.invalid, got: {types}"
    payload = next(p for t, p in captured if t == "preset.invalid")
    assert payload["preset"] == "bad"
    assert payload["provider"] == "anthropic"
    assert payload["model"] == "claude-haiku-3.5"
    # And we should have offered a suggestion (claude-3-5-haiku is in catalog).
    assert payload.get("suggestion") is not None


def test_harness_validation_passes_for_known_model(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _load_bundled_providers(tmp_path)
    cfg = tmp_path / ".relaydeck"
    _write_preset(cfg, "good", provider="anthropic", model="claude-3-5-haiku")

    from relaydeck.harness import HarnessAgent

    class _Echo(HarnessAgent):
        CLI = "/bin/echo"
        HARNESS_TYPE = "relaydeck"
        DEFAULT_ARGS = ["validated"]

    db = tmp_path / "relaydeck.db"
    agent = _Echo(
        agent_id="t2", name="t2",
        config={"model": "good"},
        workspace=None, db_path=str(db),
        stop_flag=threading.Event(),
    )
    ok, payload = agent._validate_preset()
    assert ok is True
    assert payload is None


# ── OpenAI-compat reasoning fallback ────────────────────────────────


def test_openai_message_parts_uses_reasoning_when_content_empty():
    from types import SimpleNamespace

    from relaydeck.providers_extra import _openai_message_parts

    msg = SimpleNamespace(content="", reasoning="the answer")
    text, reasoning = _openai_message_parts(msg)
    assert text == "the answer"
    assert reasoning == "the answer"


def test_openai_message_parts_reads_reasoning_content_from_model_extra():
    from types import SimpleNamespace

    from relaydeck.providers_extra import _openai_message_parts

    msg = SimpleNamespace(content=None, reasoning=None,
                          model_extra={"reasoning_content": "deepseek says hi"})
    text, reasoning = _openai_message_parts(msg)
    assert text == "deepseek says hi"
    assert reasoning == "deepseek says hi"


def test_openai_message_parts_prefers_content_when_both_present():
    from types import SimpleNamespace

    from relaydeck.providers_extra import _openai_message_parts

    msg = SimpleNamespace(content="final", reasoning="thinking")
    text, reasoning = _openai_message_parts(msg)
    assert text == "final"
    assert reasoning == "thinking"
