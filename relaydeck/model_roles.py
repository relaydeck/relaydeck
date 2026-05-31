"""
Model roles — the platform's first-class "default model for a job" layer.

A *role* is a named semantic slot ("the model I use for classification",
"the model I use for voice", "the cheap-fast text model") that maps to a
concrete model spec (a preset name, an alias, or `provider/model`). It is
resolved through the one resolver (`relaydeck.sdk.resolve_model`), fail-open,
exactly like presets — roles add a layer of *indirection by purpose*
above presets' *indirection by name*.

Why this exists: before roles, "what model do I use for X?" was answered
in three disagreeing places — hardcoded aliases in `resolve_model`, a
private `preset` setting on the emote classifier, and string literals in
the automation `model` action. Roles collapse that into one operator-owned
registry that plugins and platform features consume from.

## The flavor-free contract (assume nothing)

The package ships role *definitions* (the slots + their required
capability), NOT the *picks* and **no fallbacks**. Which provider/model
fills a role is operator config in
`~/.relaydeck/model-defaults.yaml` — like `presets/` and `vault.yaml`,
never baked in. Concretely:

  - **Every role** (text + modality) resolves *only* once the operator
    assigns it a model during onboarding (or `relaydeck defaults set`).
    There is no fall-through to a local model or any assumed default — an
    unset role resolves to `None`, and `resolve_role(..., require=True)`
    raises an actionable error pointing at onboarding. Nothing assumes
    ollama is installed or pulls any model.
  - Consumers that need a role should declare it via `required_model_roles`
    so onboarding/`doctor` surface the gap, and stay dormant (gating on
    `configured_spec`) rather than failing at call time.

With no fallbacks, `configured_spec` and `effective_spec` are equal today;
both are kept so a future per-role default (if ever added) has a seam.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Resolution-source tags surfaced to the dashboard + CLI.
SOURCE_DEFAULT = "default"    # operator set it in model-defaults.yaml
SOURCE_FALLBACK = "fallback"  # using the role's built-in fallback
SOURCE_UNSET = "unset"        # no default and no fallback (needs config)

# Capability tags — used by the picker to filter + warn (never block;
# provider catalogs rarely tag modality, so this is advisory).
CAP_TEXT = "text"
CAP_VISION = "vision"
CAP_EMBEDDING = "embedding"
CAP_AUDIO = "audio"
CAP_IMAGE = "image"


@dataclass(frozen=True)
class Role:
    """A built-in semantic model slot.

    `fallback` is a model spec used when the operator hasn't set a
    default — empty string means "no fallback; this role needs config".
    """

    name: str
    capability: str
    description: str
    fallback: str = ""


# The shipped role catalog. The package ships role *definitions* (slot +
# capability + description) but **no picks and no fallbacks** — every role
# resolves only once the operator assigns it a model during onboarding (or
# `relaydeck defaults set`). Nothing assumes a local model or a specific
# provider; an unset role is an explicit onboarding gap, not a silent
# fall-through to ollama.
BUILTIN_ROLES: tuple[Role, ...] = (
    Role("fast", CAP_TEXT,
         "Small, cheap, fast text — routing, short summaries, the default "
         "for automation `model` actions."),
    Role("frontier", CAP_TEXT,
         "Most capable reasoning model, for hard one-off tasks."),
    Role("classifier", CAP_TEXT,
         "Tiny text classification (mood / intent / routing labels)."),
    Role("embedding", CAP_EMBEDDING,
         "Vector embeddings for search / memory / dedup."),
    Role("vision", CAP_VISION,
         "Image understanding — screenshots, diagrams, photos."),
    Role("voice", CAP_AUDIO,
         "Speech in/out — transcription, TTS, live voice gateways."),
    Role("image", CAP_IMAGE,
         "Image generation."),
)

_BY_NAME: dict[str, Role] = {r.name: r for r in BUILTIN_ROLES}


def builtin_roles() -> tuple[Role, ...]:
    return BUILTIN_ROLES


def is_role(name: str) -> bool:
    return name in _BY_NAME


def get_role(name: str) -> Role | None:
    return _BY_NAME.get(name)


# ── On-disk store (~/.relaydeck/model-defaults.yaml) ───────────────


def _home(config_home: Path | None) -> Path:
    if config_home is not None:
        return config_home
    override = os.environ.get("RELAYDECK_CONFIG_HOME")
    return Path(override) if override else Path.home() / ".relaydeck"


def _defaults_path(config_home: Path | None) -> Path:
    return _home(config_home) / "model-defaults.yaml"


def load_role_defaults(config_home: Path | None = None) -> dict[str, str]:
    """Operator role→spec map. Missing file / malformed → empty (fail-open).

    Only known role names with non-empty string specs are returned, so a
    stale or hand-edited entry can't inject a bogus role."""
    path = _defaults_path(config_home)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
    roles = data.get("roles") if isinstance(data, dict) else None
    if not isinstance(roles, dict):
        return {}
    out: dict[str, str] = {}
    for name, spec in roles.items():
        if name in _BY_NAME and isinstance(spec, str) and spec.strip():
            out[name] = spec.strip()
    return out


def save_role_defaults(mapping: dict[str, str], config_home: Path | None = None) -> None:
    """Atomically persist the role→spec map (temp + rename)."""
    path = _defaults_path(config_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in mapping.items() if k in _BY_NAME and str(v).strip()}
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump({"roles": clean}, sort_keys=True))
    tmp.replace(path)


def _cycles(start: str, mapping: dict[str, str]) -> bool:
    """True if walking `start`'s role-reference chain under `mapping`
    revisits a role (a `role:a → role:b → role:a` loop). Non-role specs
    and built-in fallbacks terminate the walk, so only `role:`-valued
    defaults can cycle."""
    seen: set[str] = set()
    role: str | None = start
    while role is not None:
        if role in seen:
            return True
        seen.add(role)
        spec = mapping.get(role)
        if spec is None:
            r = _BY_NAME.get(role)
            spec = r.fallback if r else None
        if not spec or not spec.startswith("role:"):
            return False
        nxt = spec[5:]
        role = nxt if nxt in _BY_NAME else None
    return False


def set_role_default(role: str, spec: str, config_home: Path | None = None) -> None:
    """Set the operator default for `role`. Raises ValueError on an
    unknown role, empty spec, or a spec that would create a role cycle
    (validated against the *candidate* mapping, so the bad state is never
    persisted — the resolver's depth guard is a backstop, not the gate)."""
    if role not in _BY_NAME:
        raise ValueError(
            f"unknown model role {role!r} — one of {sorted(_BY_NAME)}"
        )
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("spec must be a non-empty string")
    spec = spec.strip()
    candidate = load_role_defaults(config_home)
    candidate[role] = spec
    if _cycles(role, candidate):
        raise ValueError(
            f"setting role {role!r} to {spec!r} would create a role "
            "reference cycle — point it at a preset, alias, or provider/model"
        )
    save_role_defaults(candidate, config_home)


def unset_role_default(role: str, config_home: Path | None = None) -> bool:
    """Clear the operator default for `role`. Returns True if it existed."""
    mapping = load_role_defaults(config_home)
    if role not in mapping:
        return False
    del mapping[role]
    save_role_defaults(mapping, config_home)
    return True


# ── Resolution ───────────────────────────────────────────────────────


def configured_spec(role: str, config_home: Path | None = None) -> str | None:
    """The operator-set spec for `role`, or None. Excludes the built-in
    fallback — consumers that should stay dormant until explicitly
    configured (e.g. emote) gate on this."""
    return load_role_defaults(config_home).get(role)


def effective_spec(role: str, config_home: Path | None = None) -> str | None:
    """The spec a `role:<name>` reference resolves to: operator default,
    else the role's built-in fallback, else None (needs config)."""
    spec = configured_spec(role, config_home)
    if spec:
        return spec
    r = _BY_NAME.get(role)
    if r and r.fallback:
        return r.fallback
    return None


def resolve_role(
    role: str, config_home: Path | None = None, *, require: bool = False
) -> str | None:
    """Resolve a role to its effective model spec.

    With `require=True`, an unknown role or an unconfigured role with no
    fallback raises ValueError with an actionable hint instead of
    returning None — used on the completion path so a misconfigured
    `role:voice` fails loudly, not silently."""
    if role not in _BY_NAME:
        if require:
            raise ValueError(
                f"unknown model role {role!r} — one of {sorted(_BY_NAME)}"
            )
        return None
    spec = effective_spec(role, config_home)
    if spec is None and require:
        raise ValueError(
            f"model role {role!r} is not configured — set it with "
            f"`relaydeck defaults set {role} <preset|provider/model>`"
        )
    return spec


def role_status(config_home: Path | None = None) -> list[dict[str, Any]]:
    """Per-role view for the dashboard / `relaydeck defaults list`: configured
    spec, effective spec, and where it came from."""
    defaults = load_role_defaults(config_home)
    out: list[dict[str, Any]] = []
    for r in BUILTIN_ROLES:
        cfg = defaults.get(r.name)
        eff = cfg or (r.fallback or None)
        source = SOURCE_DEFAULT if cfg else (
            SOURCE_FALLBACK if r.fallback else SOURCE_UNSET
        )
        out.append({
            "name": r.name,
            "capability": r.capability,
            "description": r.description,
            "configured": cfg,
            "fallback": r.fallback or None,
            "effective": eff,
            "source": source,
            "has_fallback": bool(r.fallback),
        })
    return out
