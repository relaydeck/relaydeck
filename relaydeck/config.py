"""
Configuration layer for relaydeck.

Handles:
  - Workspace registry at ~/.relaydeck/config.toml
  - Per-workspace agent.toml (mode, settings)
  - Per-agent YAML specs at ~/.relaydeck/agents/<id>.yaml
  - Vault at ~/.relaydeck/vault.yaml (mode 0600)

YAML is always the spec; SQLite mirrors runtime state only.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ── Agent id validation ──────────────────────────────────────────────
#
# One rule, enforced at the create chokepoint (Orchestrator.create_agent),
# so CLI, API, and web all reject the same bad names. Machine-generated
# ids (worktree/external spawn) coerce via `sanitize_agent_id` instead of
# erroring. (Sibling convention: skills aliases validate; workspace/theme/
# layout names sanitize.)
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
AGENT_ID_MAXLEN = 64
AGENT_ID_RULE = (
    "lowercase letters, numbers, and dashes — start with a letter, "
    "no spaces or underscores, no leading/trailing/double dash "
    "(e.g. pr-reviewer)"
)


def validate_agent_id(agent_id: str) -> str:
    """Validate a user-chosen agent id; return it stripped. Raises
    ValueError with a human message (CLI prints it, API maps to 400) so
    the rule is identical everywhere. Use `sanitize_agent_id` to coerce a
    generated name rather than reject it."""
    aid = (agent_id or "").strip()
    if not aid:
        raise ValueError("agent id is required")
    if len(aid) > AGENT_ID_MAXLEN:
        raise ValueError(f"agent id too long (max {AGENT_ID_MAXLEN} characters)")
    if not AGENT_ID_RE.match(aid):
        raise ValueError(f"invalid agent id {aid!r}: use {AGENT_ID_RULE}")
    return aid


def sanitize_agent_id(raw: str) -> str:
    """Coerce an arbitrary string into a valid agent id (lowercase,
    non-`[a-z0-9]` runs → `-`, letter-prefixed if needed, ≤64). For
    machine-generated ids so they always satisfy `validate_agent_id`."""
    s = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    if s and not s[0].isalpha():
        s = "a-" + s
    return (s[:AGENT_ID_MAXLEN].strip("-")) or "agent"


def _coerce_string_list(value: Any) -> list[str]:
    """Tolerant loader for YAML/TOML fields that are logically string lists."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _secure_perms(path: Path) -> None:
    """Ensure file is readable only by owner (vault, etc.)."""
    if path.exists():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ── Workspace config ─────────────────────────────────────────────────


@dataclass
class WorkspaceConfig:
    """Per-workspace configuration from agent.toml.

    No more modes. Workspaces declare which plugins they enable:

        [workspace]
        name = "my-project"
        plugins = ["messaging", "skills"]

    Each harness agent checks enabled plugins and applies their
    injections (extensions, prompt addenda, fleet context, etc.).
    """

    name: str
    path: Path
    plugins: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, name: str, repo_path: Path) -> "WorkspaceConfig":
        """Load workspace config from centralized state dir."""
        config_home = Path.home() / ".relaydeck"
        toml_path = config_home / "workspaces" / name / "agent.toml"
        plugins: list[str] = []
        if toml_path.exists():
            data = _load_yaml_or_toml(toml_path)
            plugins = data.get("workspace", {}).get("plugins", [])
        return cls(name=name, path=repo_path, plugins=plugins)

    def has_plugin(self, name: str) -> bool:
        """Check if a plugin is enabled for this workspace."""
        return name in self.plugins


# ── Agent spec ───────────────────────────────────────────────────────


@dataclass
class AgentSpec:
    """An agent's definition loaded from YAML (source of truth).

    Fields match the YAML spec format. The DB `agents` table mirrors
    status only — never treat a DB row as the definition source.
    """

    id: str
    name: str
    type: str  # registered agent type name
    composition: str | None = None  # composition name (harness primitive)
    model_override: str | None = None  # model/provider preset name
    config: dict[str, Any] = field(default_factory=dict)
    auto_start: bool = False
    workspace: str | None = None
    # Cross-orchestration meta — how this agent introduces itself to
    # peers. `purpose` is the one-line "what I'm for" string an agent
    # sees in `relaydeck agent list` / `relaydeck workspace info` / `relaydeck status`
    # before deciding to delegate. `tags` lets agents find peers by
    # category ("reviewer", "security", "local-only") via
    # `relaydeck agent find --tag X`.
    #
    # Modeled on the SKILL.md frontmatter pattern — same shape, scoped
    # to the agent instead of a skill. An empty purpose just means the
    # agent hasn't introduced itself yet; peers see "—" and have to
    # ask explicitly.
    purpose: str = ""
    tags: list[str] = field(default_factory=list)
    # Free-form text appended to the agent's system prompt at spawn.
    # Goes through each harness's native append-mechanism:
    #   pi          → `--append-system-prompt <text>`
    #   claude-code → `--append-system-prompt <text>`
    #   codex       → generated `model_instructions_file`
    # Auto-on (no plugin gate) — the harness skips injection entirely
    # if both this and the auto-identity preamble are empty/disabled.
    system_prompt: str = ""
    # When True, an auto-generated identity preamble is prepended to
    # the system prompt. The preamble names the agent, states its
    # purpose, and lists peers in the same workspace with their
    # purposes — turning the meta layer into model-visible context.
    # Set False on a per-agent basis to opt out (e.g. for a "blank
    # slate" agent that should not know about peers).
    inject_identity_preamble: bool = True

    @classmethod
    def from_yaml(cls, path: Path) -> "AgentSpec":
        data = _load_yaml_or_toml(path)
        return cls(
            id=data.get("id", path.stem),
            name=data.get("name", path.stem),
            type=data.get("type", "harness"),
            composition=data.get("composition"),
            model_override=data.get("model"),
            config=data.get("config", {}),
            auto_start=data.get("auto_start", False),
            workspace=data.get("workspace"),
            purpose=data.get("purpose", ""),
            tags=_coerce_string_list(data.get("tags")),
            system_prompt=data.get("system_prompt", ""),
            inject_identity_preamble=bool(
                data.get("inject_identity_preamble", True)
            ),
        )

    def to_yaml(self) -> str:
        return yaml.dump({
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "composition": self.composition,
            "model": self.model_override,
            "config": self.config,
            "auto_start": self.auto_start,
            "workspace": self.workspace,
            "purpose": self.purpose,
            "tags": self.tags,
            "system_prompt": self.system_prompt,
            "inject_identity_preamble": self.inject_identity_preamble,
        }, default_flow_style=False, sort_keys=False)

    def save(self, config_home: Path) -> Path:
        agents_dir = config_home / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        path = agents_dir / f"{self.id}.yaml"
        path.write_text(self.to_yaml())
        return path


# ── Model/provider presets ───────────────────────────────────────────


@dataclass
class ModelPreset:
    """A named `provider/model` pairing — a friendly handle for a model.

    Presets live at ~/.relaydeck/presets/<name>.yaml and can be
    referenced by agents as a model_override. A preset is purely a name →
    `(provider, model)` mapping; auth (API key) and endpoint (base URL)
    belong to the *provider*, not the preset. The package ships **no**
    presets — every one is operator-created, picking a real model from a
    connected provider (no assumed defaults, no auto-pulled models).
    """

    name: str
    provider: str  # "openrouter", "anthropic", "openai", "ollama", etc.
    model: str      # e.g. "claude-sonnet-4-20250514"

    @classmethod
    def from_yaml(cls, path: Path) -> "ModelPreset":
        data = _load_yaml_or_toml(path)
        # Tolerant load: legacy presets may carry base_url/api_key_env/
        # max_tokens/temperature/extra keys (now owned by the provider or
        # dropped) — read only the fields that still matter, ignore the rest.
        return cls(
            name=data.get("name", path.stem),
            provider=data["provider"],
            model=data["model"],
        )


# ── Helpers ──────────────────────────────────────────────────────────


def load_config_file(path: Path) -> dict[str, Any]:
    """Load a YAML or TOML file into a dict."""
    raw = path.read_text()
    if path.suffix in (".toml",):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        return tomllib.loads(raw)
    return yaml.safe_load(raw) or {}


def _load_yaml_or_toml(path: Path) -> dict[str, Any]:
    """Backward-compatible private alias for older in-tree callers."""
    return load_config_file(path)


def _read_agent_toml_plugins(
    name: str, config_home: Path | None = None
) -> list[str] | None:
    """Return the workspace's plugin list from its agent.toml, or None
    if the file is missing/unreadable. agent.toml is the source of
    truth because it's what the harness reads at spawn — config.toml's
    plugins field is a convenience cache kept in sync by writers."""
    base = config_home or (Path.home() / ".relaydeck")
    p = base / "workspaces" / name / "agent.toml"
    if not p.exists():
        return None
    try:
        data = _load_yaml_or_toml(p)
        v = data.get("workspace", {}).get("plugins", [])
        return _coerce_string_list(v) if isinstance(v, (list, str)) else None
    except Exception:
        return None


def load_workspace_registry(
    config_home: Path | None = None,
) -> list[WorkspaceConfig]:
    """Load all registered workspaces from `<config_home>/config.toml`
    (defaults to ~/.relaydeck). Pass `config_home` for embedded/test
    roots so reads stay consistent with writes (`register_workspace` /
    `host.workspaces`).

    Per-workspace plugins come from `agent.toml` when present (that's
    what the harness reads at spawn, so it's the authoritative source).
    config.toml's `plugins` field is a fallback for legacy workspaces
    that don't have an agent.toml yet.
    """
    base = config_home or (Path.home() / ".relaydeck")
    config_path = base / "config.toml"
    if not config_path.exists():
        return []
    data = _load_yaml_or_toml(config_path)
    workspaces = []
    for block in data.get("workspace", []):
        name = block["name"]
        agent_plugins = _read_agent_toml_plugins(name, config_home=base)
        plugins = agent_plugins if agent_plugins is not None else block.get("plugins", [])
        workspaces.append(WorkspaceConfig(
            name=name,
            path=Path(block["path"]).expanduser().resolve(),
            plugins=plugins,
        ))
    return workspaces


def _agent_toml_plugins_body(plugins: list[str]) -> str:
    if plugins:
        plugin_list = "\n".join(f'  "{pl}",' for pl in plugins)
        return f"[workspace]\nplugins = [\n{plugin_list}\n]\n"
    return "[workspace]\nplugins = []\n"


def register_workspace(
    config_home: Path,
    name: str,
    path: Path,
    plugins: list[str] | None = None,
) -> dict[str, Any]:
    """Register a workspace: append it to config.toml and write its
    `agent.toml` plugins list. Reusable core of `relaydeck workspace add` /
    `POST /api/workspaces` / `host.workspaces.create`, so all three write
    the registry identically. Raises ValueError if the name is taken.

    Does NOT validate that `path` exists — programmatic callers (e.g. a
    plugin that just created a git worktree) own that check; the registry
    write is pure.
    """
    import tomllib

    import tomli_w

    plugins = list(plugins or [])
    cfg = config_home / "config.toml"
    data: dict[str, Any] = {}
    if cfg.exists():
        try:
            data = tomllib.loads(cfg.read_text())
        except Exception:
            data = {}
    workspaces = data.get("workspace", [])
    if any(w.get("name") == name for w in workspaces):
        raise ValueError(f"workspace already registered: {name}")
    resolved = str(Path(path).expanduser().resolve())
    workspaces.append({"name": name, "path": resolved, "plugins": plugins})
    data["workspace"] = workspaces
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(tomli_w.dumps(data))

    ws_state = config_home / "workspaces" / name
    ws_state.mkdir(parents=True, exist_ok=True)
    (ws_state / "agent.toml").write_text(_agent_toml_plugins_body(plugins))
    return {"name": name, "path": resolved, "plugins": plugins}


def set_workspace_plugins(
    config_home: Path,
    name: str,
    plugins: list[str],
) -> dict[str, Any]:
    """Update a registered workspace's plugin list in both config.toml and
    its agent.toml (agent.toml is the spawn-time source of truth). Raises
    ValueError if the workspace isn't registered."""
    import tomllib

    import tomli_w

    plugins = [str(p) for p in plugins]
    cfg = config_home / "config.toml"
    data: dict[str, Any] = {}
    if cfg.exists():
        try:
            data = tomllib.loads(cfg.read_text())
        except Exception:
            data = {}
    workspaces = data.get("workspace", [])
    idx = next((i for i, w in enumerate(workspaces) if w.get("name") == name), -1)
    if idx < 0:
        raise ValueError(f"workspace not found: {name}")
    workspaces[idx]["plugins"] = plugins
    data["workspace"] = workspaces
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(tomli_w.dumps(data))

    ws_state = config_home / "workspaces" / name
    ws_state.mkdir(parents=True, exist_ok=True)
    (ws_state / "agent.toml").write_text(_agent_toml_plugins_body(plugins))
    return {"name": name, "plugins": plugins}


def unregister_workspace(config_home: Path, name: str) -> bool:
    """Remove a workspace from config.toml. Leaves the on-disk directory
    and `agent.toml` in place (registration-only removal, matching
    `relaydeck workspace rm` semantics). Returns True if an entry was removed.
    Used for spawn rollback — undoing a registration we just made."""
    import tomllib

    import tomli_w

    cfg = config_home / "config.toml"
    if not cfg.exists():
        return False
    try:
        data = tomllib.loads(cfg.read_text())
    except Exception:
        return False
    workspaces = data.get("workspace", [])
    kept = [w for w in workspaces if w.get("name") != name]
    if len(kept) == len(workspaces):
        return False
    data["workspace"] = kept
    cfg.write_text(tomli_w.dumps(data))
    return True


def load_agent_specs(config_home: Path | None = None) -> list[AgentSpec]:
    """Load all agent specs from ~/.relaydeck/agents/*.yaml."""
    agents_dir = (config_home or Path.home() / ".relaydeck") / "agents"
    if not agents_dir.exists():
        return []
    specs = []
    for yaml_path in sorted(agents_dir.glob("*.yaml")):
        try:
            specs.append(AgentSpec.from_yaml(yaml_path))
        except Exception:
            pass
    return specs


def load_model_presets(config_home: Path | None = None) -> list[ModelPreset]:
    """Load the operator's model presets from ``presets/*.yaml``.

    The package ships none — an empty list until the user creates one."""
    presets_dir = (config_home or Path.home() / ".relaydeck") / "presets"
    if not presets_dir.exists():
        return []
    presets: list[ModelPreset] = []
    for yaml_path in sorted(presets_dir.glob("*.yaml")):
        try:
            presets.append(ModelPreset.from_yaml(yaml_path))
        except Exception:
            pass
    return presets
