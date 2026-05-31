"""
Plugin settings — typed, persisted, source-tracked.

Each plugin declares a schema via `RelaydeckPlugin.get_settings_schema()`:

    [{
        "key": "preset",
        "type": "preset_ref",          # see _ALLOWED_TYPES below
        "label": "Sentiment preset",
        "description": "Pick a preset to use as the mood classifier.",
        "default": None,
    }]

Values live in `~/.relaydeck/plugin-settings.yaml` keyed by plugin
name. Plugins read at runtime with `get_setting(plugin, key, default)`
so config changes take effect without a daemon restart — workers pick
up new values on their next tick.

We also track *where each value came from* (`source`): yaml-overridden,
env-overridden, or the schema default. The dashboard and CLI surface
this so the user can see at a glance "this setting is currently coming
from `RELAYDECK_EMOTE_PRESET`, not the yaml."
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Types a settings schema field may declare. The dashboard chooses the
# right control per type; the API validates POSTs against the type.
_ALLOWED_TYPES = {"text", "textarea", "number", "bool", "enum", "preset_ref", "secret_ref"}

# Author-friendly aliases → canonical control types. Manifests and legacy
# schemas in the wild use JSON-schema-ish spellings ("string", "boolean",
# "integer"). Without this map they parse fine but get silently dropped from
# the dashboard (gap G1) — several bundled plugins (github, telegram, skills,
# usage_limits) tripped this. Normalize to the canonical type instead of
# discarding the field.
_TYPE_ALIASES = {
    "string": "text",
    "str": "text",
    "boolean": "bool",
    "integer": "number",
    "int": "number",
    "float": "number",
}


def canonical_type(ftype: Any) -> Any:
    """Map an author-supplied setting type onto a canonical control type.

    Canonical types pass through unchanged; common non-canonical spellings
    are aliased; anything else is returned as-is so the allow-list check can
    still reject it.
    """
    return _TYPE_ALIASES.get(ftype, ftype)


# ── On-disk store ────────────────────────────────────────────────────


_STORE_LOCK = threading.RLock()


def _store_path() -> Path:
    return Path.home() / ".relaydeck" / "plugin-settings.yaml"


def _load_all() -> dict[str, dict[str, Any]]:
    p = _store_path()
    if not p.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict):
            return {}
        # Normalise: each top-level value should be a dict.
        return {k: (v if isinstance(v, dict) else {}) for k, v in data.items()}
    except Exception as exc:
        logger.warning("plugin-settings: load failed: %s", exc)
        return {}


def _save_all(data: dict[str, dict[str, Any]]) -> None:
    import yaml
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=True, default_flow_style=False))
    try:
        p.chmod(0o600)  # may contain provider preferences; keep tidy
    except OSError:
        pass


# ── Public API ───────────────────────────────────────────────────────


def get_setting(plugin: str, key: str, default: Any = None) -> Any:
    """Read a single setting. Resolution order:

      1. Environment variable `RELAYDECK_<PLUGIN>_<KEY>` (uppercased, hyphens→underscores)
      2. The plugin-settings.yaml value
      3. `default`

    Env wins so an operator can pin a value without editing the yaml.
    """
    env_key = _env_key(plugin, key)
    env_val = os.environ.get(env_key)
    if env_val is not None and env_val != "":
        return env_val
    with _STORE_LOCK:
        return _load_all().get(plugin, {}).get(key, default)


def get_all(plugin: str) -> dict[str, Any]:
    """Return the *effective* settings dict for a plugin (env wins over yaml)."""
    with _STORE_LOCK:
        yaml_vals = _load_all().get(plugin, {})
    merged = dict(yaml_vals)
    for k in yaml_vals:
        ek = _env_key(plugin, k)
        if os.environ.get(ek) not in (None, ""):
            merged[k] = os.environ[ek]
    return merged


def set_settings(plugin: str, values: dict[str, Any]) -> None:
    """Replace the yaml-stored settings dict for a plugin. Caller is
    responsible for having validated against the schema."""
    with _STORE_LOCK:
        data = _load_all()
        data[plugin] = {k: v for k, v in values.items() if v is not None and v != ""}
        if not data[plugin]:
            data.pop(plugin, None)  # drop empty entries — keeps the yaml tidy
        _save_all(data)


def value_source(plugin: str, key: str) -> str:
    """Where does `get_setting(plugin,key)` actually come from? Used by
    the dashboard to label fields with `· from env RELAYDECK_EMOTE_PRESET`."""
    if os.environ.get(_env_key(plugin, key)) not in (None, ""):
        return "env"
    with _STORE_LOCK:
        if _load_all().get(plugin, {}).get(key) is not None:
            return "yaml"
    return "default"


def _env_key(plugin: str, key: str) -> str:
    p = re.sub(r"[^A-Z0-9_]", "_", plugin.upper().replace("-", "_"))
    k = re.sub(r"[^A-Z0-9_]", "_", key.upper().replace("-", "_"))
    return f"RELAYDECK_{p}_{k}"


# ── Schema helpers ───────────────────────────────────────────────────


def normalize_schema(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Apply defaults + drop fields with unknown types. Plugins can return
    a sloppy list — we don't want one bad field to break the API."""
    out: list[dict[str, Any]] = []
    for raw_field in raw or []:
        if not isinstance(raw_field, dict):
            continue
        key = raw_field.get("key")
        ftype = canonical_type(raw_field.get("type", "text"))
        if not key or not isinstance(key, str):
            continue
        if ftype not in _ALLOWED_TYPES:
            continue
        out.append({
            "key": key,
            "type": ftype,
            "label": raw_field.get("label", key),
            "description": raw_field.get("description", ""),
            "default": raw_field.get("default"),
            "options": list(raw_field.get("options") or []),
            "secret": bool(raw_field.get("secret")),
        })
    return out


def validate_values(schema: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, Any]:
    """Coerce + validate. Unknown keys are dropped. Type-check is
    permissive (we coerce text → number where possible) so the form
    layer can stay simple."""
    by_key = {f["key"]: f for f in schema}
    out: dict[str, Any] = {}
    for k, v in (values or {}).items():
        spec = by_key.get(k)
        if not spec:
            continue
        t = canonical_type(spec["type"])
        if v in (None, ""):
            continue
        if t == "number":
            try: out[k] = float(v) if "." in str(v) else int(v)
            except (TypeError, ValueError): pass
        elif t == "bool":
            out[k] = bool(v) if isinstance(v, bool) else str(v).lower() in ("1","true","yes","on")
        elif t == "enum":
            if v in spec.get("options", []):
                out[k] = v
        else:  # text, textarea, preset_ref, secret_ref — opaque strings
            out[k] = str(v)
    return out


import re  # used by _env_key; placed at bottom to keep the doc section tidy
