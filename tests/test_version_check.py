"""Update-check: version compare, GitHub fetch seam, caching, fail-open."""

from __future__ import annotations

import time

from relaydeck import version_check as vc


def test_version_compare():
    assert vc.is_newer("0.2.0", "0.1.0")
    assert vc.is_newer("1.0.0", "0.9.9")
    assert vc.is_newer("v0.2.0", "0.1.0")        # leading v tolerated
    assert vc.is_newer("0.1.10", "0.1.2")        # numeric, not lexical
    assert not vc.is_newer("0.1.0", "0.1.0")     # equal
    assert not vc.is_newer("0.1.0", "0.2.0")     # older
    assert not vc.is_newer(None, "0.1.0")        # no release → not newer
    assert not vc.is_newer("0.1.0rc1", "0.1.0")  # pre-release truncates, == base


def test_fetch_latest_tag_parses_and_normalizes():
    assert vc.fetch_latest_tag(_fetch=lambda url: {"tag_name": "v1.4.0"}) == "1.4.0"
    assert vc.fetch_latest_tag(_fetch=lambda url: {"name": "2.0.0"}) == "2.0.0"


def test_fetch_latest_tag_fails_open():
    def boom(url):
        raise RuntimeError("offline / 404 no releases")
    assert vc.fetch_latest_tag(_fetch=boom) is None
    # Malformed payloads don't throw.
    assert vc.fetch_latest_tag(_fetch=lambda url: []) is None
    assert vc.fetch_latest_tag(_fetch=lambda url: {}) is None


def test_check_for_update_flags_and_caches(tmp_path):
    cache = tmp_path / "update-check.json"
    calls = {"n": 0}

    def fetch(url):
        calls["n"] += 1
        return {"tag_name": "0.5.0"}

    out = vc.check_for_update("0.1.0", cache_path=cache, _fetch=fetch)
    assert out["update_available"] is True
    assert out["latest"] == "0.5.0" and out["current"] == "0.1.0"
    assert calls["n"] == 1 and cache.is_file()

    # Second call within TTL is served from cache — no new fetch.
    out2 = vc.check_for_update("0.1.0", cache_path=cache, _fetch=fetch)
    assert out2["update_available"] is True and calls["n"] == 1

    # force=True re-fetches.
    vc.check_for_update("0.1.0", cache_path=cache, force=True, _fetch=fetch)
    assert calls["n"] == 2


def test_check_for_update_no_update_when_current_is_latest(tmp_path):
    out = vc.check_for_update(
        "1.0.0", cache_path=tmp_path / "c.json", _fetch=lambda url: {"tag_name": "1.0.0"}
    )
    assert out["update_available"] is False


def test_check_for_update_offline_uses_stale_cache(tmp_path):
    cache = tmp_path / "c.json"
    # Prime the cache with a known-newer release, but make it look stale.
    cache.write_text('{"latest": "0.9.0", "checked_at": 1}')

    def boom(url):
        raise RuntimeError("offline")

    out = vc.check_for_update("0.1.0", cache_path=cache, _fetch=boom)
    # Fetch failed, but we keep the last-known latest rather than dropping it.
    assert out["latest"] == "0.9.0" and out["update_available"] is True


def test_check_for_update_fail_open_no_cache(tmp_path):
    def boom(url):
        raise RuntimeError("offline, no cache")
    out = vc.check_for_update("0.1.0", cache_path=tmp_path / "missing.json", _fetch=boom)
    assert out["latest"] is None and out["update_available"] is False


def test_ttl_expiry_refetches(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(f'{{"latest": "0.1.0", "checked_at": {time.time() - 10}}}')
    hit = {"n": 0}

    def fetch(url):
        hit["n"] += 1
        return {"tag_name": "0.3.0"}

    # ttl=1s and the cache is 10s old → must refetch.
    out = vc.check_for_update("0.1.0", cache_path=cache, ttl=1, _fetch=fetch)
    assert hit["n"] == 1 and out["latest"] == "0.3.0"
