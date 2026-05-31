"""models.dev — cached model metadata + pricing index.

Fetches ``https://models.dev/api.json`` once per day into
``~/.relaydeck/cache/models-dev.json``, mirroring ``ProviderPlugin``'s cache
discipline (TTL + stale-fallback on fetch failure). It is a **metadata**
source only: per-model pricing (USD per 1M tokens), capabilities
(``tool_call``/``reasoning``/modalities), limits, and provider env-key hints +
logos. It never ships model *picks* or defaults — the operator still chooses
presets/roles (see AGENTS.md "Model selection").

Fail-open everywhere: a network error or a models.dev 500 must never take the
daemon down. We serve a stale cache if we have one, else report "unknown"
(``None`` / empty), and every consumer treats that as "I don't know" rather
than an error.

ID mapping: relaydeck splits a spec like ``openrouter/deepseek/deepseek-v4``
into ``provider="openrouter"`` + ``model_id="deepseek/deepseek-v4"``; that maps
to ``index["openrouter"]["models"]["deepseek/deepseek-v4"]``. A small alias map
bridges relaydeck provider names to models.dev provider ids where they differ
(e.g. relaydeck ``gemini`` → models.dev ``google``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_API_URL = "https://models.dev/api.json"
_LOGO_URL = "https://models.dev/logos/{provider}.svg"
_TTL_S = 24 * 3600
_FETCH_TIMEOUT_S = 20

# relaydeck provider name → models.dev provider id (only where they differ).
# Unmapped names pass through unchanged; an unknown id simply misses and the
# caller falls open.
PROVIDER_ALIASES: dict[str, str] = {
    "gemini": "google",
}

# ── In-memory memo (avoid re-reading the ~2MB blob on every get_price) ──
_lock = threading.Lock()
_mem_index: dict[str, Any] | None = None
_mem_at: float = 0.0
_mem_key: str | None = None

# Network kill-switch. The test suite flips this on (conftest) so no path can
# hit models.dev; production leaves it off. Monkeypatching `_fetch_raw`
# bypasses the guard, which is how the models.dev unit tests inject a fixture.
_fetch_disabled = False

# Stale-while-revalidate bookkeeping: at most one in-flight background refresh
# per process, throttled so a persistent outage doesn't hammer the network.
_refresh_in_flight = False
_last_refresh_attempt = 0.0
_REFRESH_THROTTLE_S = 60.0


def set_fetch_disabled(disabled: bool) -> None:
    """Test hook: hard-disable all live fetches (api.json + logos)."""
    global _fetch_disabled
    _fetch_disabled = disabled


def _cache_dir(config_home: Path | None = None) -> Path:
    home = config_home or (Path.home() / ".relaydeck")
    return home / "cache"


def _cache_path(config_home: Path | None = None) -> Path:
    return _cache_dir(config_home) / "models-dev.json"


def _fetch_raw() -> dict[str, Any]:
    """GET the live api.json. Raises on any failure (caller falls open)."""
    if _fetch_disabled:
        raise RuntimeError("models.dev fetch disabled")
    req = urllib.request.Request(_API_URL, headers={"User-Agent": "relaydeck"})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("models.dev api.json is not an object")
    return data


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({"ts": time.time(), "data": data})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(blob, encoding="utf-8")
    tmp.replace(path)


def _read_disk(cache_path: Path) -> tuple[dict[str, Any], float] | None:
    """Return (data, ts) from disk cache regardless of age, or None."""
    if not cache_path.exists():
        return None
    try:
        blob = json.loads(cache_path.read_text(encoding="utf-8"))
        return (blob.get("data") or {}), float(blob.get("ts", 0))
    except Exception:
        return None


def _do_fetch(config_home: Path | None) -> dict[str, Any]:
    """Fetch + cache (used synchronously and from the background refresh)."""
    cache_path = _cache_path(config_home)
    data = _fetch_raw()
    _write_cache(cache_path, data)
    _memoize(str(cache_path), data, time.time())
    return data


def _spawn_background_refresh(config_home: Path | None) -> None:
    """Stale-while-revalidate: refresh the cache off the request path so no
    async endpoint or cost-recording call ever blocks on models.dev. At most
    one in flight; throttled; a no-op when fetches are disabled (tests)."""
    global _refresh_in_flight, _last_refresh_attempt
    if _fetch_disabled:
        return
    now = time.time()
    with _lock:
        if _refresh_in_flight or (now - _last_refresh_attempt) < _REFRESH_THROTTLE_S:
            return
        _refresh_in_flight = True
        _last_refresh_attempt = now

    def _run() -> None:
        global _refresh_in_flight
        try:
            _do_fetch(config_home)
        except Exception as exc:
            logger.debug("models.dev background refresh failed: %s", exc)
        finally:
            with _lock:
                _refresh_in_flight = False

    threading.Thread(target=_run, name="models-dev-refresh", daemon=True).start()


def load_index(
    config_home: Path | None = None, *, force: bool = False, cache_only: bool = False,
) -> dict[str, Any]:
    """Return the provider-keyed models.dev index, cached + fail-open.

    ``cache_only=True`` (the request path: API endpoints + cost recording)
    never blocks on the network — it returns the freshest cache it has
    (memo → fresh disk → stale disk → empty) and kicks a background refresh
    when the cache is cold or stale. ``cache_only=False`` (the background
    warmer + tests) may fetch synchronously. Never raises."""
    global _mem_index, _mem_at, _mem_key
    cache_path = _cache_path(config_home)
    key = str(cache_path)
    now = time.time()

    with _lock:
        if (
            not force
            and _mem_index is not None
            and _mem_key == key
            and (now - _mem_at) < _TTL_S
        ):
            return _mem_index

    disk = _read_disk(cache_path) if not force else None
    if disk is not None:
        data, ts = disk
        fresh = (now - ts) < _TTL_S
        if fresh:
            _memoize(key, data, ts)
            return data
        if cache_only:
            # Serve stale immediately; refresh in the background.
            _memoize(key, data, ts)
            _spawn_background_refresh(config_home)
            return data

    if cache_only:
        # Cold cache: return empty now, warm in the background.
        _spawn_background_refresh(config_home)
        return {}

    # Synchronous fetch with stale-cache fallback on failure.
    try:
        return _do_fetch(config_home)
    except Exception as exc:
        logger.debug("models.dev fetch failed: %s", exc)
        if disk is not None:
            data, ts = disk
            _memoize(key, data, ts)
            return data
        return {}


def _memoize(key: str, data: dict[str, Any], ts: float) -> None:
    global _mem_index, _mem_at, _mem_key
    with _lock:
        _mem_index = data
        _mem_at = ts
        _mem_key = key


def warm(config_home: Path | None = None) -> None:
    """Populate the cache synchronously (daemon startup warmer). Fail-open."""
    with contextlib.suppress(Exception):
        load_index(config_home, force=True)


def clear_cache() -> None:
    """Drop the in-memory memo (tests + a forced refresh use this)."""
    global _mem_index, _mem_at, _mem_key, _refresh_in_flight, _last_refresh_attempt
    with _lock:
        _mem_index = None
        _mem_at = 0.0
        _mem_key = None
        _refresh_in_flight = False
        _last_refresh_attempt = 0.0


def resolve_provider_key(provider: str) -> str:
    """relaydeck provider name → models.dev provider id."""
    return PROVIDER_ALIASES.get(provider, provider)


def get_provider_meta(
    provider: str, config_home: Path | None = None, *, cache_only: bool = False
) -> dict[str, Any] | None:
    """Provider-level metadata (``env``, ``name``, ``doc``) or None."""
    idx = load_index(config_home, cache_only=cache_only)
    meta = idx.get(resolve_provider_key(provider))
    return meta if isinstance(meta, dict) else None


def get_env_hints(
    provider: str, config_home: Path | None = None, *, cache_only: bool = False
) -> list[str]:
    """The vault/env key names models.dev associates with the provider."""
    meta = get_provider_meta(provider, config_home, cache_only=cache_only)
    if not meta:
        return []
    env = meta.get("env")
    if isinstance(env, list):
        return [str(e) for e in env]
    if isinstance(env, str):
        return [env]
    return []


def get_model_meta(
    provider: str, model: str, config_home: Path | None = None, *, cache_only: bool = False
) -> dict[str, Any] | None:
    """The model object for ``(provider, model)`` or None when unknown."""
    meta = get_provider_meta(provider, config_home, cache_only=cache_only)
    if not meta:
        return None
    models = meta.get("models")
    if not isinstance(models, dict):
        return None
    m = models.get(model)
    return m if isinstance(m, dict) else None


def get_price(
    provider: str, model: str, config_home: Path | None = None, *, cache_only: bool = False
) -> tuple[float, float] | None:
    """(input, output) USD per 1M tokens, or None when unknown.

    Same unit as the metering static table — models.dev ``cost.input`` /
    ``cost.output`` are already per-1M-token USD."""
    m = get_model_meta(provider, model, config_home, cache_only=cache_only)
    if not m:
        return None
    cost = m.get("cost")
    if not isinstance(cost, dict):
        return None
    inp = cost.get("input")
    out = cost.get("output")
    if inp is None and out is None:
        return None
    return (float(inp or 0.0), float(out or 0.0))


def model_capabilities(
    provider: str, model: str, config_home: Path | None = None, *, cache_only: bool = False
) -> list[str]:
    """Normalized capability tags derived from the model object.

    Returns a subset of ``["tools", "reasoning", "vision", "audio", "pdf"]``
    — the picker renders these as badges. Empty when unknown."""
    m = get_model_meta(provider, model, config_home, cache_only=cache_only)
    if not m:
        return []
    caps: list[str] = []
    if m.get("tool_call"):
        caps.append("tools")
    if m.get("reasoning"):
        caps.append("reasoning")
    modalities = m.get("modalities") or {}
    inputs = modalities.get("input") if isinstance(modalities, dict) else None
    if isinstance(inputs, list):
        if "image" in inputs:
            caps.append("vision")
        if "audio" in inputs:
            caps.append("audio")
        if "pdf" in inputs:
            caps.append("pdf")
    return caps


def logo_url(provider: str) -> str:
    """The upstream logo URL (models.dev serves a default SVG when unknown)."""
    return _LOGO_URL.format(provider=resolve_provider_key(provider))


def _logo_cache_path(provider: str, config_home: Path | None = None) -> Path:
    safe = resolve_provider_key(provider).replace("/", "_").replace("..", "_")
    return _cache_dir(config_home) / "models-dev" / "logos" / f"{safe}.svg"


def fetch_logo(provider: str, config_home: Path | None = None) -> bytes | None:
    """Return the provider's logo SVG bytes, cached locally + fail-open.

    Mirrors the api.json cache discipline: fresh disk cache → live fetch →
    stale cache → None. Callers (the daemon logo proxy) treat None as "no
    logo" and let the dashboard fall back to a placeholder — never block
    render on this."""
    cache_path = _logo_cache_path(provider, config_home)
    now = time.time()

    if cache_path.exists():
        try:
            age = now - cache_path.stat().st_mtime
            if age < _TTL_S:
                return cache_path.read_bytes()
        except Exception:
            pass

    if _fetch_disabled:
        # Tests / offline: serve a stale cached logo if present, else None.
        if cache_path.exists():
            try:
                return cache_path.read_bytes()
            except Exception:
                pass
        return None

    try:
        req = urllib.request.Request(
            logo_url(provider), headers={"User-Agent": "relaydeck"}
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as r:
            body = r.read()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(cache_path)
        return body
    except Exception as exc:
        logger.debug("models.dev logo fetch failed for %s: %s", provider, exc)
        if cache_path.exists():
            try:
                return cache_path.read_bytes()
            except Exception:
                pass
        return None
