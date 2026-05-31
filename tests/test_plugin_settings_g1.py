"""Regression tests for G1 — settings `type` drift.

Non-canonical setting types (`string`/`boolean`/`integer`) used to parse
fine through the manifest but then get *silently dropped* by
`normalize_schema` (its `_ALLOWED_TYPES` allow-list), making those settings
invisible/uneditable in the dashboard. Several bundled plugins tripped this
(telegram, github, skills, usage_limits).

The fix is the alias map + `canonical_type()` in `relaydeck/plugin_settings.py`,
applied in both `normalize_schema` and `validate_values`. These tests pin it:
genuine typos must still be rejected; aliased types must survive and coerce.

(Originally covered by the now-removed plugin lab; re-added as a focused
regression.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck.plugin_manifest import load_manifest
from relaydeck.plugin_settings import (
    canonical_type,
    normalize_schema,
    validate_values,
)

_PLUGINS = Path(__file__).resolve().parent.parent / "plugins"


# ── canonical_type ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("string", "text"),
        ("str", "text"),
        ("boolean", "bool"),
        ("integer", "number"),
        ("int", "number"),
        ("float", "number"),
    ],
)
def test_aliases_map_to_canonical(alias, canonical):
    assert canonical_type(alias) == canonical


@pytest.mark.parametrize("canon", ["text", "textarea", "number", "bool", "enum", "preset_ref", "secret_ref"])
def test_canonical_types_pass_through_unchanged(canon):
    assert canonical_type(canon) == canon


def test_unknown_type_returned_as_is_for_allowlist_to_reject():
    # canonical_type doesn't validate — it leaves unknowns alone so the
    # _ALLOWED_TYPES check in normalize_schema can still drop them.
    assert canonical_type("widget") == "widget"


# ── normalize_schema: aliased types survive, typos still dropped ──────


def test_normalize_keeps_aliased_types():
    raw = [
        {"key": "a", "type": "string"},
        {"key": "b", "type": "boolean"},
        {"key": "c", "type": "integer"},
    ]
    out = {f["key"]: f["type"] for f in normalize_schema(raw)}
    assert out == {"a": "text", "b": "bool", "c": "number"}


def test_normalize_still_drops_genuine_typos():
    raw = [
        {"key": "good", "type": "string"},
        {"key": "bad", "type": "strnig"},  # typo — must be dropped
    ]
    keys = {f["key"] for f in normalize_schema(raw)}
    assert keys == {"good"}


# ── validate_values: coercion goes through the canonical type ─────────


def test_validate_coerces_aliased_integer_and_boolean():
    schema = normalize_schema([
        {"key": "count", "type": "integer"},
        {"key": "flag", "type": "boolean"},
    ])
    out = validate_values(schema, {"count": "5", "flag": "true"})
    assert out == {"count": 5, "flag": True}


# ── No settings dropped for the bundled plugins G1 affected ───────────


@pytest.mark.parametrize("plugin", ["telegram", "github", "skills", "usage_limits"])
def test_bundled_plugin_settings_not_dropped(plugin):
    # The four plugins G1 originally affected (non-canonical types). Named
    # explicitly so the regression stays documented even if the manifests move.
    manifest = load_manifest(_PLUGINS / plugin / "plugin.toml")
    raw = manifest.settings_schema()
    assert raw, f"{plugin} should declare [plugin.settings]"
    dropped = _dropped_keys(raw)
    assert not dropped, f"{plugin}: settings dropped by normalize_schema: {sorted(dropped)}"


def _bundled_manifests_with_settings():
    for toml in sorted(_PLUGINS.glob("**/plugin.toml")):
        try:
            manifest = load_manifest(toml)
        except Exception:
            continue
        if manifest.settings_schema():
            yield pytest.param(toml.parent.name, manifest, id=toml.parent.name)


def _dropped_keys(raw):
    kept = {f["key"] for f in normalize_schema(raw)}
    return {f["key"] for f in raw} - kept


@pytest.mark.parametrize("name,manifest", list(_bundled_manifests_with_settings()))
def test_no_bundled_manifest_drops_settings(name, manifest):
    # Forward-coverage: every bundled plugin that
    # declares settings — not just the four G1 touched — must survive
    # normalize_schema, so a future plugin using a non-canonical type is caught.
    dropped = _dropped_keys(manifest.settings_schema())
    assert not dropped, f"{name}: settings dropped by normalize_schema: {sorted(dropped)}"
