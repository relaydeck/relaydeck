"""models.dev cached metadata index — fetch/cache discipline, ID mapping,
pricing, capabilities, env hints, logos. No external network: the fetch seam
is monkeypatched and the fixture is hand-authored to the schema (not copied
from models.dev, to avoid redistributing their data)."""

from __future__ import annotations

import json
import time

import pytest

from relaydeck import models_dev

# A schema-faithful slice authored for tests. Mirrors the real api.json shape:
# provider → {id, env: [...], name, doc, models: {model_id: {cost, limit,
# modalities, reasoning, tool_call, ...}}}. Prices here are invented.
FIXTURE = {
    "anthropic": {
        "id": "anthropic",
        "env": ["ANTHROPIC_API_KEY"],
        "name": "Anthropic",
        "doc": "https://docs.anthropic.com",
        "models": {
            "claude-sonnet-4-6": {
                "id": "claude-sonnet-4-6",
                "name": "Claude Sonnet 4.6",
                "cost": {"input": 3.0, "output": 15.0},
                "limit": {"context": 200000, "output": 64000},
                "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
                "reasoning": True,
                "tool_call": True,
            },
        },
    },
    "openrouter": {
        "id": "openrouter",
        "env": ["OPENROUTER_API_KEY"],
        "name": "OpenRouter",
        "doc": "https://openrouter.ai/docs",
        "models": {
            # Nested provider/model key — the hard ID-mapping case.
            "deepseek/deepseek-v4-flash": {
                "id": "deepseek/deepseek-v4-flash",
                "name": "DeepSeek V4 Flash",
                "cost": {"input": 0.27, "output": 1.1},
                "modalities": {"input": ["text"], "output": ["text"]},
                "tool_call": True,
            },
            "free/model": {  # a free model: cost present but zero
                "id": "free/model",
                "cost": {"input": 0, "output": 0},
            },
            "no/price": {  # a model with NO cost block at all
                "id": "no/price",
                "modalities": {"input": ["text", "audio"], "output": ["text"]},
            },
        },
    },
    # models.dev calls Google's provider "google"; relaydeck calls it "gemini".
    "google": {
        "id": "google",
        "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "name": "Google",
        "models": {
            "gemini-2.5-pro": {
                "id": "gemini-2.5-pro",
                "cost": {"input": 1.25, "output": 10.0},
                "modalities": {"input": ["text", "image"], "output": ["text"]},
            },
        },
    },
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Each test gets a fresh cache home + cleared in-memory memo. These
    tests fully control the network seam (monkeypatched `_fetch_raw` /
    `urlopen`), so re-enable fetching here — the suite-wide conftest hard-
    disables it for every other test file."""
    models_dev.set_fetch_disabled(False)
    models_dev.clear_cache()
    yield
    models_dev.clear_cache()
    models_dev.set_fetch_disabled(True)


@pytest.fixture
def fed(monkeypatch):
    """Monkeypatch the network seam to return the fixture; count fetches."""
    calls = {"n": 0}

    def _fake():
        calls["n"] += 1
        return FIXTURE

    monkeypatch.setattr(models_dev, "_fetch_raw", _fake)
    return calls


# ── Fetch + cache discipline ───────────────────────────────────────────

def test_fetch_then_disk_cache(fed, tmp_path):
    idx = models_dev.load_index(tmp_path)
    assert "anthropic" in idx
    assert fed["n"] == 1
    # Cache file written with {ts, data} shape.
    cache = tmp_path / "cache" / "models-dev.json"
    assert cache.exists()
    blob = json.loads(cache.read_text())
    assert "ts" in blob and blob["data"]["anthropic"]["id"] == "anthropic"


def test_memo_avoids_refetch(fed, tmp_path):
    models_dev.load_index(tmp_path)
    models_dev.load_index(tmp_path)
    assert fed["n"] == 1  # second call served from in-memory memo


def test_stale_disk_cache_used_when_fetch_fails(monkeypatch, tmp_path):
    # Pre-seed an EXPIRED cache, then make the fetch blow up.
    cache = tmp_path / "cache" / "models-dev.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"ts": time.time() - 999999, "data": FIXTURE}))

    def _boom():
        raise RuntimeError("models.dev is 500ing")

    monkeypatch.setattr(models_dev, "_fetch_raw", _boom)
    models_dev.clear_cache()
    idx = models_dev.load_index(tmp_path)
    # Stale-but-present beats empty: metering keeps yesterday's prices.
    assert idx["anthropic"]["id"] == "anthropic"


def test_empty_when_no_cache_and_fetch_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(models_dev, "_fetch_raw", lambda: (_ for _ in ()).throw(OSError("down")))
    models_dev.clear_cache()
    assert models_dev.load_index(tmp_path) == {}


# ── cache_only (the non-blocking request path) ─────────────────────────

def test_cache_only_cold_returns_empty_without_sync_fetch(monkeypatch, tmp_path):
    """The request path (cache_only=True) must never fetch synchronously.
    A cold cache returns {} immediately and kicks a background refresh."""
    fetched = {"n": 0}
    spawned = {"n": 0}

    def _fetch():
        fetched["n"] += 1
        return FIXTURE

    monkeypatch.setattr(models_dev, "_fetch_raw", _fetch)
    monkeypatch.setattr(models_dev, "_spawn_background_refresh",
                        lambda ch: spawned.__setitem__("n", spawned["n"] + 1))

    out = models_dev.load_index(tmp_path, cache_only=True)
    assert out == {}
    assert fetched["n"] == 0   # no synchronous fetch on the request path
    assert spawned["n"] == 1   # background refresh was kicked instead


def test_cache_only_serves_stale_without_sync_fetch(monkeypatch, tmp_path):
    """A stale (>24h) cache under cache_only serves its prices immediately
    and kicks a background refresh — it does NOT block on a fetch."""
    cache = tmp_path / "cache" / "models-dev.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"ts": time.time() - 999999, "data": FIXTURE}))
    models_dev.clear_cache()

    fetched = {"n": 0}
    spawned = {"n": 0}
    monkeypatch.setattr(models_dev, "_fetch_raw",
                        lambda: (fetched.__setitem__("n", fetched["n"] + 1), FIXTURE)[1])
    monkeypatch.setattr(models_dev, "_spawn_background_refresh",
                        lambda ch: spawned.__setitem__("n", spawned["n"] + 1))

    out = models_dev.load_index(tmp_path, cache_only=True)
    assert out["anthropic"]["id"] == "anthropic"  # stale served, not empty
    assert fetched["n"] == 0                       # no synchronous fetch
    assert spawned["n"] == 1                       # background refresh kicked


def test_metering_uses_cache_only(monkeypatch, tmp_path):
    """Regression guard: metering's models.dev lookup must pass cache_only=True
    so cost recording never blocks on a stale-cache network fetch."""
    from plugins.metering.plugin import _MODELS_DEV_PRICE_CACHE, _models_dev_price
    _MODELS_DEV_PRICE_CACHE.clear()
    seen = {}

    def _spy(provider, model, config_home=None, *, cache_only=False):
        seen["cache_only"] = cache_only
        return (1.0, 2.0)

    monkeypatch.setattr(models_dev, "get_price", _spy)
    assert _models_dev_price("openrouter", "x") == (1.0, 2.0)
    assert seen["cache_only"] is True


# ── Pricing + ID mapping ───────────────────────────────────────────────

def test_price_simple(fed, tmp_path):
    assert models_dev.get_price("anthropic", "claude-sonnet-4-6", tmp_path) == (3.0, 15.0)


def test_price_nested_openrouter_key(fed, tmp_path):
    # The ID-mapping case: openrouter + "deepseek/deepseek-v4-flash".
    assert models_dev.get_price("openrouter", "deepseek/deepseek-v4-flash", tmp_path) == (0.27, 1.1)


def test_price_unit_is_per_million(fed, tmp_path):
    # Guard against a 1e3-vs-1e6 unit slip: input 3.0/1M, output 15.0/1M.
    inp, out = models_dev.get_price("anthropic", "claude-sonnet-4-6", tmp_path)
    assert inp == 3.0 and out == 15.0


def test_price_zero_cost_model_returns_zero_not_none(fed, tmp_path):
    assert models_dev.get_price("openrouter", "free/model", tmp_path) == (0.0, 0.0)


def test_price_none_when_no_cost_block(fed, tmp_path):
    assert models_dev.get_price("openrouter", "no/price", tmp_path) is None


def test_price_none_for_unknown_provider(fed, tmp_path):
    assert models_dev.get_price("my-local-vllm", "whatever", tmp_path) is None


def test_price_none_for_unknown_model(fed, tmp_path):
    assert models_dev.get_price("anthropic", "claude-does-not-exist", tmp_path) is None


# ── Provider alias map ─────────────────────────────────────────────────

def test_gemini_aliases_to_google(fed, tmp_path):
    assert models_dev.resolve_provider_key("gemini") == "google"
    assert models_dev.get_price("gemini", "gemini-2.5-pro", tmp_path) == (1.25, 10.0)


def test_unmapped_provider_passes_through(fed, tmp_path):
    assert models_dev.resolve_provider_key("openrouter") == "openrouter"


# ── Capabilities + env hints ───────────────────────────────────────────

def test_capabilities_tools_reasoning_vision_pdf(fed, tmp_path):
    caps = models_dev.model_capabilities("anthropic", "claude-sonnet-4-6", tmp_path)
    assert set(caps) == {"tools", "reasoning", "vision", "pdf"}


def test_capabilities_audio(fed, tmp_path):
    caps = models_dev.model_capabilities("openrouter", "no/price", tmp_path)
    assert "audio" in caps and "vision" not in caps


def test_capabilities_empty_for_unknown(fed, tmp_path):
    assert models_dev.model_capabilities("nope", "nope", tmp_path) == []


def test_env_hints(fed, tmp_path):
    assert models_dev.get_env_hints("anthropic", tmp_path) == ["ANTHROPIC_API_KEY"]
    assert models_dev.get_env_hints("gemini", tmp_path) == ["GEMINI_API_KEY", "GOOGLE_API_KEY"]


def test_env_hints_empty_for_unknown(fed, tmp_path):
    assert models_dev.get_env_hints("my-local-vllm", tmp_path) == []


# ── Logos ──────────────────────────────────────────────────────────────

def test_logo_url_uses_resolved_provider_key():
    assert models_dev.logo_url("gemini") == "https://models.dev/logos/google.svg"
    assert models_dev.logo_url("anthropic") == "https://models.dev/logos/anthropic.svg"


def test_fetch_logo_caches_and_falls_open(monkeypatch, tmp_path):
    svg = b"<svg>logo</svg>"

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return svg

    calls = {"n": 0}

    def _urlopen(req, timeout=None):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(models_dev.urllib.request, "urlopen", _urlopen)
    out = models_dev.fetch_logo("anthropic", tmp_path)
    assert out == svg
    assert calls["n"] == 1
    # Second call served from disk cache (no re-fetch).
    out2 = models_dev.fetch_logo("anthropic", tmp_path)
    assert out2 == svg
    assert calls["n"] == 1


def test_fetch_logo_returns_none_on_failure(monkeypatch, tmp_path):
    def _boom(req, timeout=None):
        raise OSError("offline")

    monkeypatch.setattr(models_dev.urllib.request, "urlopen", _boom)
    assert models_dev.fetch_logo("anthropic", tmp_path) is None
