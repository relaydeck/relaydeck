"""
Harness-type model policy — one resolver for PTY spawn vs relaydeck inference.

PTY harnesses fall into three buckets (see `harness_options.HARNESS_META`):

  native   — auth + model namespace owned by the harness CLI (codex, claude,
             cursor). relaydeck presets/roles/vault do NOT apply; optional
             harness-specific override keys only (`codex_model`, …).
  flex     — harness accepts `provider/model` but operator must configure the
             harness's own provider store (pi, opencode).
  relaydeck — relaydeck resolves presets/roles and bridges vault keys
             (relaydeck-native, loop/github `model` actions).

CLI research (2026-05):
  pi            `--model provider/id`  (pi auth in ~/.pi)
  claude        `--model sonnet|claude-sonnet-4-6`  (Anthropic sub / ~/.claude)
  codex         `-m gpt-5.3-codex`  (~/.codex ChatGPT or API; not relaydeck keys)
  cursor-agent  `--model sonnet-4|composer-2`  (Cursor Pro namespace)
  opencode      `-m provider/model`  (`opencode providers` auth)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck.harness_options import HARNESS_META, _PRIMARY_ORDER

logger = logging.getLogger(__name__)

# type/alias → canonical harness meta key
_TYPE_TO_META: dict[str, str] = {}
for _key, _meta in HARNESS_META.items():
    _TYPE_TO_META[_key] = _key
    for _alias in _meta.get("aliases", []):
        _TYPE_TO_META[_alias] = _key


def model_policy_for(agent_type: str) -> str:
    """Return `native`, `flex`, or `relaydeck` for an agent type string."""
    key = _TYPE_TO_META.get(agent_type, agent_type)
    meta = HARNESS_META.get(key, {})
    return str(meta.get("model_policy") or "flex")


def model_meta_for(agent_type: str) -> dict[str, Any]:
    key = _TYPE_TO_META.get(agent_type, agent_type)
    return dict(HARNESS_META.get(key, {}))


def _configured_model_ref(config: dict[str, Any]) -> str:
    return str(config.get("preset") or config.get("model") or "").strip()


@dataclass(frozen=True)
class ResolvedHarnessModel:
    spec: str
    provider: str
    model_id: str
    cli_model: str | None
    source: str  # empty | override | preset | provider/model | role


def _resolve_spec(spec: str, config_home: Path | None) -> tuple[str, str, str]:
    """Return (provider, model_id, source)."""
    if not spec:
        return "", "", "empty"
    if spec.startswith("role:"):
        from relaydeck.model_roles import resolve_role

        eff = resolve_role(spec[5:], config_home, require=False)
        if eff:
            return _resolve_spec(str(eff).strip(), config_home)
        return "", "", "role"
    from relaydeck.config import load_model_presets

    for preset in load_model_presets(config_home):
        if preset.name == spec:
            return str(preset.provider), str(preset.model), "preset"
    if "/" in spec:
        prov, mid = spec.split("/", 1)
        return prov, mid, "provider/model"
    return "", spec, "bare"


def resolve_harness_model(
    agent_type: str,
    config: dict[str, Any],
    *,
    config_home: Path | None = None,
) -> ResolvedHarnessModel:
    """Resolve configured model for an agent type + config dict."""
    meta = model_meta_for(agent_type)
    policy = meta.get("model_policy") or "flex"
    override_key = meta.get("model_config_key")
    spec = _configured_model_ref(config)

    if override_key:
        override = config.get(override_key)
        if override:
            ov = str(override).strip()
            return ResolvedHarnessModel(
                spec=ov, provider="", model_id=ov,
                cli_model=ov, source="override",
            )

    if not spec:
        return ResolvedHarnessModel(
            spec="", provider="", model_id="", cli_model=None, source="empty",
        )

    provider, model_id, source = _resolve_spec(spec, config_home)

    # An UNRESOLVED role (operator referenced role:fast but never assigned it)
    # must never propagate as a literal `--model role:fast` for ANY harness —
    # the CLI would reject the unknown id. Drop to None so the harness uses
    # its own default. (A *resolved* role keeps its concrete provider/model and
    # still hits each policy's normal handling, incl. the native guard below.)
    if source == "role" and not provider and not model_id:
        return ResolvedHarnessModel(
            spec=spec, provider="", model_id="", cli_model=None, source="role",
        )

    if policy == "native":
        cli = _native_cli_model(agent_type, provider, model_id, spec)
        return ResolvedHarnessModel(
            spec=spec, provider=provider, model_id=model_id,
            cli_model=cli, source=source,
        )

    if policy == "relaydeck":
        cli = _flex_cli_model(spec, provider, model_id)
        return ResolvedHarnessModel(
            spec=spec, provider=provider, model_id=model_id,
            cli_model=cli, source=source,
        )

    # flex — pi / opencode: pass provider/model when known
    cli = _flex_cli_model(spec, provider, model_id)
    return ResolvedHarnessModel(
        spec=spec, provider=provider, model_id=model_id,
        cli_model=cli, source=source,
    )


def _flex_cli_model(
    spec: str, provider: str, model_id: str,
) -> str | None:
    """Format `--model` for relaydeck/flex harnesses.

    The unresolved-`role:` case (operator hasn't set the role yet) is handled
    upstream in `resolve_harness_model`, which short-circuits to None before
    reaching here — so a literal `role:fast` can never become a `--model`
    value for pi/opencode."""
    if provider and model_id:
        return f"{provider}/{model_id}"
    if "/" in spec:
        return spec
    return spec or None


def _native_cli_model(
    agent_type: str, provider: str, model_id: str, spec: str,
) -> str | None:
    """Format `--model` for native harnesses (bare id / cursor namespace)."""
    key = _TYPE_TO_META.get(agent_type, agent_type)
    if key == "cursor-cli":
        # Only cursor_model override reaches here before preset; presets
        # are rejected at validation — never map relaydeck presets.
        return model_id or spec or None
    if key in ("codex-cli", "claude-code"):
        if model_id:
            return model_id
        if "/" in spec:
            return spec.split("/", 1)[1]
        return spec or None
    return model_id or spec or None


def validate_harness_model(
    agent_type: str,
    config: dict[str, Any],
    *,
    config_home: Path | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Spawn-time validation. Returns (ok, error_payload).

    For `native` harnesses, relaydeck presets pointing at non-native providers
    are rejected (e.g. deepseek preset on codex + ChatGPT auth). For
    `relaydeck`/`flex`, catalog validation runs when a named preset is used."""
    meta = model_meta_for(agent_type)
    policy = meta.get("model_policy") or "flex"
    native_providers: list[str] | None = meta.get("native_providers")
    override_key = meta.get("model_config_key")
    spec = _configured_model_ref(config)

    if override_key and config.get(override_key):
        return True, None

    if not spec:
        return True, None

    resolved = resolve_harness_model(agent_type, config, config_home=config_home)

    if policy == "native":
        if key := _TYPE_TO_META.get(agent_type, agent_type):
            if key == "cursor-cli" and spec and not config.get("cursor_model"):
                return False, {
                    "preset": spec,
                    "provider": resolved.provider,
                    "model": resolved.model_id,
                    "harness_type": agent_type,
                    "reason": "cursor_preset_ignored",
                    "message": (
                        "Cursor agents use config.cursor_model (Cursor namespace, "
                        "e.g. sonnet-4, composer-2) — relaydeck presets are not "
                        "injected. Set cursor_model or leave model empty for the "
                        "account default."
                    ),
                }
        if native_providers and resolved.provider:
            if resolved.provider not in native_providers:
                allowed = ", ".join(native_providers)
                return False, {
                    "preset": spec,
                    "provider": resolved.provider,
                    "model": resolved.model_id,
                    "harness_type": agent_type,
                    "reason": "native_provider_mismatch",
                    "message": (
                        f"{meta.get('label', agent_type)} uses its own account auth "
                        f"and only accepts {allowed} models via --model, not "
                        f"{resolved.provider}/{resolved.model_id}. "
                        f"Leave model empty for the harness default, set "
                        f"{override_key or 'a harness model override'}, or use pi/"
                        f"opencode/relaydeck-native for relaydeck provider presets."
                    ),
                }
        return True, None

    # flex / relaydeck — named preset catalog check (existing behavior)
    if policy == "flex":
        return True, None

    try:
        from relaydeck.config import load_model_presets
        from relaydeck.plugin import get_provider
    except Exception:
        return True, None

    preset = next(
        (p for p in load_model_presets(config_home) if p.name == spec), None,
    )
    if preset is None:
        return True, None

    provider = get_provider(preset.provider)
    if provider is None:
        return True, None

    ok, suggestion = provider.validate(preset.model)
    if ok:
        return True, None

    return False, {
        "preset": spec,
        "provider": preset.provider,
        "model": preset.model,
        "suggestion": suggestion,
        "harness_type": agent_type,
        "reason": "catalog_miss",
    }


def cli_model_for_agent(
    agent_type: str,
    config: dict[str, Any],
    *,
    config_home: Path | None = None,
) -> str | None:
    """Value to pass as the harness `--model` flag, or None to omit."""
    resolved = resolve_harness_model(agent_type, config, config_home=config_home)
    if model_policy_for(agent_type) == "native":
        meta = model_meta_for(agent_type)
        native_providers: list[str] | None = meta.get("native_providers")
        if native_providers and resolved.provider:
            if resolved.provider not in native_providers:
                return None
        if (
            _TYPE_TO_META.get(agent_type, agent_type) == "cursor-cli"
            and _configured_model_ref(config)
            and not config.get("cursor_model")
        ):
            return None
    return resolved.cli_model
