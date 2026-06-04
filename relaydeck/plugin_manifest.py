"""Manifest parsing for the v1 plugin platform.

The manifest is intentionally read before plugin code is imported. That
lets the registry decide whether a plugin is loadable, which Host major
it targets, and which capabilities it declares without executing
third-party Python.
"""

from __future__ import annotations

import hashlib
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ManifestError(ValueError):
    """Raised when a plugin.toml is present but invalid."""


@dataclass(frozen=True)
class SettingsField:
    key: str
    type: str = "text"
    default: Any = None
    description: str = ""
    options: list[Any] = field(default_factory=list)
    multiline: bool = False

    def to_legacy_schema(self) -> dict[str, Any]:
        field_type = "textarea" if self.multiline and self.type == "text" else self.type
        return {
            "key": self.key,
            "type": field_type,
            "label": self.key.replace("_", " ").title(),
            "description": self.description,
            "default": self.default,
            "options": list(self.options),
        }


@dataclass(frozen=True)
class UIContribution:
    """A single dashboard contribution from a plugin.

    Used by both `[plugin.ui] tabs = [...]`, `header_chips = [...]`, and
    `agent_tiles = [...]`. The dashboard interprets the same fields
    slightly differently per slot:

    - **tabs**: rendered as top-level lenses in the activity rail when
      `default_state = "lens"`. Plain `tabs` entries (no default_state)
      are legacy: they get ignored under the new IA. Authors who want a
      first-class rail icon must opt into "lens".
    - **header_chips**: rendered as inline chips next to the workspace
      pill.
    - **agent_tiles** (recommended: the new `tiles` alias in plugin.toml):
      rendered inside the Agents-lens detail body via the tile system
      (tab / pop / hidden). The user — not the plugin author — decides
      which slot via the Panels manager; `default_state` is the seed.

    Fields beyond the original three (id/title/module) live here for the
    tile system:
      - `default_state`: 'tab' | 'pop' | 'hidden' | 'lens' (default 'hidden')
      - `description`: rendered under the name in the Panels manager
      - `protected`: if true, the user cannot hide the tile (core only)
      - `source`: rendering hint; defaults to the plugin name
    """

    id: str
    title: str = ""
    module: str = ""
    icon: str = ""
    order: int = 100
    applies_to: list[str] = field(default_factory=list)
    default_state: str = ""
    description: str = ""
    protected: bool = False
    source: str = ""
    # Home-dashboard widget sizing (grid cells). Only meaningful for
    # `[plugin.ui] widgets`; 0 means "let the dashboard pick a default".
    def_w: int = 0
    def_h: int = 0
    min_w: int = 0
    min_h: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "title": self.title,
            "module": self.module,
            "order": self.order,
        }
        if self.icon:
            out["icon"] = self.icon
        if self.applies_to:
            out["applies_to"] = list(self.applies_to)
        if self.default_state:
            out["default_state"] = self.default_state
        if self.description:
            out["description"] = self.description
        if self.protected:
            out["protected"] = True
        if self.source:
            out["source"] = self.source
        for k, v in (("def_w", self.def_w), ("def_h", self.def_h),
                     ("min_w", self.min_w), ("min_h", self.min_h)):
            if v:
                out[k] = v
        return out


@dataclass(frozen=True)
class TuiTab:
    """A `relaydeck view` (terminal TUI) tab contributed by a plugin —
    the terminal-side mirror of `[plugin.ui] tabs` for the web dashboard.

    The `view` client renders the tab by GETting `endpoint` (mounted under
    `/api/plugins/<plugin>/`), expecting JSON `{"title"?: str, "lines":
    [str, ...]}` (or plain text). No plugin widget code runs in the client —
    it stays a thin HTTP consumer, consistent with the CLI/daemon split.
    """

    id: str
    title: str = ""
    endpoint: str = "tui"
    order: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "endpoint": self.endpoint,
            "order": self.order,
        }


VALID_TRUST_LEVELS = frozenset({"bundled", "local", "signed", "untrusted"})


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    description: str = ""
    author: str = ""
    license: str = ""
    homepage: str = ""
    category: str = "tool"
    host_api_version: int = 1
    workspace_scoped: bool = False
    declared_capabilities: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    needs_vault: tuple[str, ...] = ()
    # Semantic model roles this plugin consumes (e.g. "classifier",
    # "voice"). Advisory metadata: `relaydeck doctor` + the dashboard surface
    # roles a plugin needs but the operator hasn't configured (and that
    # have no built-in fallback). It does NOT gate loading.
    required_model_roles: tuple[str, ...] = ()
    top_level_cli: bool = False
    top_level_api: bool = False
    # Plugin author's declared trust level. Empty means "let the
    # registry derive it from install source" (bundled vs local).
    # Refusing untrusted plugins at load time lives in the registry,
    # not here -- the manifest just states intent.
    trust_level: str = ""
    settings: tuple[SettingsField, ...] = ()
    ui_tabs: tuple[UIContribution, ...] = ()
    ui_header_chips: tuple[UIContribution, ...] = ()
    ui_agent_tiles: tuple[UIContribution, ...] = ()
    ui_widgets: tuple[UIContribution, ...] = ()
    tui_tabs: tuple[TuiTab, ...] = ()
    skills: dict[str, str] = field(default_factory=dict)
    path: Path | None = None
    manifest_hash: str = ""

    @property
    def capabilities(self) -> set[str]:
        return set(self.declared_capabilities)

    def settings_schema(self) -> list[dict[str, Any]]:
        return [field.to_legacy_schema() for field in self.settings]

    def ui_manifest(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "tabs": [item.to_dict() for item in self.ui_tabs],
            "header_chips": [item.to_dict() for item in self.ui_header_chips],
            "agent_tiles": [item.to_dict() for item in self.ui_agent_tiles],
            "widgets": [item.to_dict() for item in self.ui_widgets],
            "tui": [item.to_dict() for item in self.tui_tabs],
        }


def load_manifest(path: Path) -> PluginManifest:
    """Load and validate a plugin.toml."""
    raw_text = path.read_text()
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path}: invalid TOML: {exc}") from exc

    plugin = data.get("plugin")
    if not isinstance(plugin, dict):
        raise ManifestError(f"{path}: missing [plugin] table")

    name = _required_str(plugin, "name", path)
    version = _required_str(plugin, "version", path)
    category = str(plugin.get("category") or "tool")
    host_api_version = int(plugin.get("host_api_version") or 1)
    if host_api_version < 1:
        raise ManifestError(f"{path}: host_api_version must be >= 1")

    settings = tuple(_parse_settings(plugin.get("settings") or {}))
    ui = plugin.get("ui") or {}
    if not isinstance(ui, dict):
        raise ManifestError(f"{path}: [plugin.ui] must be a table")
    # `tiles` is the recommended alias for `agent_tiles` in plugin.toml —
    # reads more naturally now that the agent-detail tile system is the
    # primary slot. Both names accepted; the legacy key still works.
    if ui.get("tiles") is not None and ui.get("agent_tiles") is not None:
        raise ManifestError(f"{path}: [plugin.ui] use either 'tiles' or 'agent_tiles', not both")
    if ui.get("tiles") is not None and ui.get("agent_tiles") is None:
        ui = {**ui, "agent_tiles": ui.get("tiles")}

    trust_level = str(plugin.get("trust_level") or "").strip()
    if trust_level and trust_level not in VALID_TRUST_LEVELS:
        raise ManifestError(
            f"{path}: plugin.trust_level must be one of "
            f"{sorted(VALID_TRUST_LEVELS)}, got {trust_level!r}"
        )

    return PluginManifest(
        name=name,
        version=version,
        description=str(plugin.get("description") or ""),
        author=str(plugin.get("author") or ""),
        license=str(plugin.get("license") or ""),
        homepage=str(plugin.get("homepage") or ""),
        category=category,
        host_api_version=host_api_version,
        workspace_scoped=bool(plugin.get("workspace_scoped") or False),
        declared_capabilities=tuple(_string_list(plugin.get("declared_capabilities"))),
        dependencies=tuple(_string_list(plugin.get("dependencies"))),
        needs_vault=tuple(_string_list(plugin.get("needs_vault"))),
        required_model_roles=tuple(_string_list(plugin.get("required_model_roles"))),
        top_level_cli=bool(plugin.get("top_level_cli") or False),
        top_level_api=bool(plugin.get("top_level_api") or False),
        trust_level=trust_level,
        settings=settings,
        ui_tabs=tuple(_parse_ui_list(ui.get("tabs"))),
        ui_header_chips=tuple(_parse_ui_list(ui.get("header_chips"))),
        ui_agent_tiles=tuple(_parse_ui_list(ui.get("agent_tiles"))),
        ui_widgets=tuple(_parse_ui_list(ui.get("widgets"))),
        tui_tabs=tuple(_parse_tui_list((plugin.get("tui") or {}).get("tabs"))),
        skills=_parse_skills(plugin.get("skills") or {}),
        path=path,
        manifest_hash="sha256:" + hashlib.sha256(raw_text.encode()).hexdigest(),
    )


def find_manifest(plugin_dir: Path) -> PluginManifest | None:
    path = plugin_dir / "plugin.toml"
    if not path.exists():
        return None
    return load_manifest(path)


def _required_str(table: dict[str, Any], key: str, path: Path) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{path}: plugin.{key} is required")
    return value.strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ManifestError("expected a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ManifestError("expected a list of strings")
        out.append(item)
    return out


def _parse_settings(raw: Any) -> list[SettingsField]:
    if not isinstance(raw, dict):
        raise ManifestError("[plugin.settings] must be a table")
    fields: list[SettingsField] = []
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            raise ManifestError(f"setting {key}: expected inline table")
        ftype = str(spec.get("type") or "text")
        # Load-time warning (G1 follow-up): a setting whose type is neither
        # canonical nor a known alias is silently dropped from the dashboard
        # later by normalize_schema. Surface it now so the author gets a
        # signal instead of an invisible setting. Non-fatal — the field still
        # parses; the drop happens (or doesn't) downstream.
        from relaydeck.plugin_settings import _ALLOWED_TYPES, canonical_type
        if canonical_type(ftype) not in _ALLOWED_TYPES:
            logger.warning(
                "plugin setting %r: unknown type %r (expected one of %s) — "
                "it will be dropped from the dashboard",
                key, ftype, ", ".join(sorted(_ALLOWED_TYPES)),
            )
        fields.append(SettingsField(
            key=str(key),
            type=ftype,
            default=spec.get("default"),
            description=str(spec.get("description") or ""),
            options=list(spec.get("options") or []),
            multiline=bool(spec.get("multiline") or False),
        ))
    return fields


def _parse_tui_list(raw: Any) -> list[TuiTab]:
    """Parse `[plugin.tui] tabs = [...]`. Each entry: id (required), title,
    endpoint (default "tui"), order."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError("[plugin.tui] tabs must be a list")
    out: list[TuiTab] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ManifestError("[plugin.tui] tab entries must be tables")
        ident = str(item.get("id") or "").strip()
        if not ident:
            raise ManifestError("[plugin.tui] tab id is required")
        if ident in seen:
            raise ManifestError(f"duplicate [plugin.tui] tab id: {ident}")
        seen.add(ident)
        out.append(TuiTab(
            id=ident,
            title=str(item.get("title") or ident),
            endpoint=str(item.get("endpoint") or "tui").strip("/") or "tui",
            order=int(item.get("order") or 100),
        ))
    return out


def _parse_ui_list(raw: Any) -> list[UIContribution]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError("UI contribution must be a list")
    out: list[UIContribution] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ManifestError("UI contribution entries must be tables")
        ident = str(item.get("id") or "").strip()
        if not ident:
            raise ManifestError("UI contribution id is required")
        if ident in seen:
            raise ManifestError(f"duplicate UI contribution id: {ident}")
        seen.add(ident)
        default_state = str(item.get("default_state") or item.get("default") or "").strip()
        if default_state and default_state not in {"tab", "pop", "hidden", "lens"}:
            raise ManifestError(
                f"UI contribution {ident!r}: default_state must be one of "
                f"tab|pop|hidden|lens, got {default_state!r}"
            )
        out.append(UIContribution(
            id=ident,
            title=str(item.get("title") or ident),
            module=str(item.get("module") or ""),
            icon=str(item.get("icon") or ""),
            order=int(item.get("order") or 100),
            applies_to=[str(v) for v in (item.get("applies_to") or [])],
            default_state=default_state,
            description=str(item.get("description") or item.get("desc") or ""),
            protected=bool(item.get("protected") or False),
            source=str(item.get("source") or "").strip(),
            def_w=int(item.get("def_w") or item.get("w") or 0),
            def_h=int(item.get("def_h") or item.get("h") or 0),
            min_w=int(item.get("min_w") or 0),
            min_h=int(item.get("min_h") or 0),
        ))
    return out


def _parse_skills(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError("[plugin.skills] must be a table")
    return {str(k): str(v) for k, v in raw.items()}
