"""
Pricing resolution for usage metering — static table + the live-catalog
fallback that lets openrouter models (e.g. deepseek-v4-flash, not in the
static table) get real cost from the provider's published per-model price.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.provider import ModelEntry
from plugins.metering.plugin import (
    _CATALOG_PRICE_CACHE,
    calculate_cost,
    get_price,
)


def test_static_table_price():
    assert get_price("openai", "gpt-4o") == (2.5, 10.0)


def test_unknown_provider_is_zero():
    _CATALOG_PRICE_CACHE.clear()
    assert get_price("nope-provider", "whatever") == (0.0, 0.0)


def test_catalog_fallback_prices_unlisted_model(monkeypatch):
    _CATALOG_PRICE_CACHE.clear()

    class _Prov:
        def list_models(self):
            return [ModelEntry(id="deepseek/deepseek-v4-flash", display_name="x",
                               prompt_price=0.5, completion_price=2.0)]

    monkeypatch.setattr("relaydeck.provider.get_provider",
                        lambda name: _Prov() if name == "openrouter" else None)
    assert get_price("openrouter", "deepseek/deepseek-v4-flash") == (0.5, 2.0)
    # 1M prompt + 1M completion → 0.5 + 2.0 = 2.5 USD
    assert abs(calculate_cost("openrouter", "deepseek/deepseek-v4-flash",
                              1_000_000, 1_000_000) - 2.5) < 1e-9


def test_catalog_lookup_is_cached(monkeypatch):
    _CATALOG_PRICE_CACHE.clear()
    n = {"calls": 0}

    class _Prov:
        def list_models(self):
            n["calls"] += 1
            return [ModelEntry(id="m", display_name="m",
                               prompt_price=1.0, completion_price=1.0)]

    monkeypatch.setattr("relaydeck.provider.get_provider", lambda name: _Prov())
    get_price("openrouter", "m")
    get_price("openrouter", "m")
    assert n["calls"] == 1  # second call hits the cache, no re-scan


def test_models_dev_fallback_when_catalog_misses(monkeypatch):
    """A model absent from the static table AND the live catalog gets its
    price from the models.dev metadata index (the new 4th-tier fallback)."""
    from plugins.metering.plugin import _MODELS_DEV_PRICE_CACHE
    _CATALOG_PRICE_CACHE.clear()
    _MODELS_DEV_PRICE_CACHE.clear()

    # No live catalog price.
    monkeypatch.setattr("relaydeck.provider.get_provider", lambda name: None)
    # models.dev knows it.
    def _md(p, m, *a, **k):
        return (0.27, 1.1) if (p, m) == ("openrouter", "deepseek/deepseek-v4") else None
    monkeypatch.setattr("relaydeck.models_dev.get_price", _md)
    assert get_price("openrouter", "deepseek/deepseek-v4") == (0.27, 1.1)


def test_static_table_beats_models_dev(monkeypatch):
    """The static exact-model table wins over models.dev."""
    from plugins.metering.plugin import _MODELS_DEV_PRICE_CACHE
    _CATALOG_PRICE_CACHE.clear()
    _MODELS_DEV_PRICE_CACHE.clear()
    monkeypatch.setattr("relaydeck.provider.get_provider", lambda name: None)
    monkeypatch.setattr("relaydeck.models_dev.get_price", lambda p, m, *a, **k: (999.0, 999.0))
    assert get_price("openai", "gpt-4o") == (2.5, 10.0)


def test_wildcard_beats_models_dev_for_local_provider(monkeypatch):
    """A provider's `*` wildcard (deliberate 'this provider is free', e.g.
    local ollama) must win over a models.dev hit so a local mirror never
    meters at cloud prices."""
    from plugins.metering.plugin import _MODELS_DEV_PRICE_CACHE
    _CATALOG_PRICE_CACHE.clear()
    _MODELS_DEV_PRICE_CACHE.clear()
    monkeypatch.setattr("relaydeck.provider.get_provider", lambda name: None)
    # Pretend models.dev has cloud prices for an ollama-named model.
    monkeypatch.setattr("relaydeck.models_dev.get_price", lambda p, m, *a, **k: (5.0, 20.0))
    assert get_price("ollama", "llama3") == (0.0, 0.0)


def test_models_dev_lookup_is_cached(monkeypatch):
    from plugins.metering.plugin import _MODELS_DEV_PRICE_CACHE
    _CATALOG_PRICE_CACHE.clear()
    _MODELS_DEV_PRICE_CACHE.clear()
    monkeypatch.setattr("relaydeck.provider.get_provider", lambda name: None)
    n = {"calls": 0}

    def _md(p, m, *a, **k):
        n["calls"] += 1
        return (1.0, 2.0)

    monkeypatch.setattr("relaydeck.models_dev.get_price", _md)
    get_price("openrouter", "x")
    get_price("openrouter", "x")
    assert n["calls"] == 1  # memoized in _MODELS_DEV_PRICE_CACHE
