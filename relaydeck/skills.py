"""
relaydeck/skills.py — single source of truth for skill discovery, validation,
parsing, and plugin-contributed materialization.

A *skill* is a directory containing a required `SKILL.md` (YAML
frontmatter with `name` + `description`, then a markdown body) plus
optional `scripts/`, `references/`, `assets/`, and `agents/openai.yaml`.

This module is pure (it imports only `relaydeck.config` for the workspace
registry; no daemon/plugin runtime). Harnesses call the discovery helpers
so the dashboard Skills lens reports the *exact same set* the model
actually sees. The bundled `skills` plugin layers an inventory cache, a
lens, CLI, API, and a rescan worker on top.

Materialization: plugins declare skills in `[plugin.skills]`; this module
writes them into `workspaces/<ws>/runtime/skills/<name>/` alongside a
`.relaydeck-skill.json` ownership sidecar so one plugin can't clobber another
plugin's skill, and idempotent re-writes are hash-skipped. The materialize
primitives are pure; the `skills` plugin's manager orchestrates them with
per-plugin provider hooks (content override + target-workspace resolution).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

logger = logging.getLogger("relaydeck.skills")

# A safe single path component: letters/digits/dot/underscore/dash, no
# separators. Used to gate every untrusted value (workspace name, skill
# alias, plugin-declared skill name) that flows into a filesystem path,
# so HTTP input or a malformed plugin manifest can't escape the intended
# directory (e.g. `../../etc`, `a/b`, an absolute path).
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_path_component(value: str, *, kind: str = "name") -> str:
    """Return `value` unchanged if it is a single safe path component,
    else raise ValueError. Rejects empty, `.`/`..`, separators, and
    anything outside `[A-Za-z0-9._-]`."""
    if (not isinstance(value, str) or value in (".", "..")
            or not _PATH_COMPONENT_RE.match(value)):
        raise ValueError(
            f"invalid {kind} {value!r}: must be a single path component "
            "(letters/digits/._- only, no separators)"
        )
    return value

# Source classifications. The first two are relaydeck-injectable (an agent's
# harness wires them into its prompt); the codex-* roots are read-only
# inventory — relaydeck never writes into `~/.codex/skills`.
SOURCE_WORKSPACE = "workspace"          # workspaces/<ws>/skills/<name>
SOURCE_RUNTIME_PLUGIN = "runtime-plugin"  # workspaces/<ws>/runtime/skills/<name>
SOURCE_CODEX_USER = "codex-user"        # ~/.codex/skills/<name>
SOURCE_CODEX_SYSTEM = "codex-system"    # ~/.codex/skills/.system/<name>
SOURCE_CLAUDE_USER = "claude-user"      # ~/.claude/skills/<name>
SOURCE_CLAUDE_PROJECT = "claude-project"  # <workspace repo>/.claude/skills/<name>

INJECTABLE_SOURCES = frozenset({SOURCE_WORKSPACE, SOURCE_RUNTIME_PLUGIN})

SIDECAR_NAME = ".relaydeck-skill.json"
MANAGED_BY = "relaydeck-skills"

# A SKILL.md larger than this is flagged (warning, not error): the body
# is loaded into every agent prompt in the workspace, so a runaway file
# bloats context for the whole fleet.
_LARGE_SKILL_BYTES = 64 * 1024


# ── config home ──────────────────────────────────────────────────────


def default_config_home() -> Path:
    """relaydeck-wide convention: `$RELAYDECK_CONFIG_HOME` override, else
    `~/.relaydeck`. Mirrors what the harnesses resolve so discovery
    here matches injection there."""
    override = os.environ.get("RELAYDECK_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".relaydeck"


def codex_skills_root() -> Path:
    """`$CODEX_HOME/skills` or `~/.codex/skills`."""
    home = os.environ.get("CODEX_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / "skills"


def claude_skills_root() -> Path:
    """`$CLAUDE_CONFIG_DIR/skills` or `~/.claude/skills` — Claude Code's
    user-level skills. Read-only inventory; claude-code loads these
    natively, relaydeck never writes here."""
    home = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(home).expanduser() if home else Path.home() / ".claude"
    return base / "skills"


# ── data model ───────────────────────────────────────────────────────


@dataclass
class SkillRef:
    """One discovered skill. `id` is a stable hash of the canonical
    realpath + source scope, so the same skill keeps its id across
    rescans (and a symlink and its target get *different* ids because
    the visible path differs)."""

    id: str
    name: str
    description: str
    source_type: str
    path: str                       # visible skill directory
    realpath: str                   # resolved skill directory
    valid: bool
    display_name: str = ""
    owner_plugin: str | None = None
    workspace: str | None = None
    symlink_target: str | None = None
    content_hash: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)
    size: int = 0
    mtime: float = 0.0

    @property
    def injectable(self) -> bool:
        return self.source_type in INJECTABLE_SOURCES

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "source_type": self.source_type,
            "owner_plugin": self.owner_plugin,
            "workspace": self.workspace,
            "path": self.path,
            "realpath": self.realpath,
            "symlink_target": self.symlink_target,
            "content_hash": self.content_hash,
            "valid": self.valid,
            "injectable": self.injectable,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "frontmatter": self.frontmatter,
            "size": self.size,
            "mtime": self.mtime,
        }
        return d


# ── parsing ──────────────────────────────────────────────────────────


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Split a SKILL.md into (frontmatter dict, body). Frontmatter is a
    leading `---`-delimited YAML block. Returns ({}, text) when there is
    no frontmatter or it doesn't parse."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            block = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1:])
            try:
                fm = yaml.safe_load(block) or {}
            except yaml.YAMLError:
                return {}, text
            if not isinstance(fm, dict):
                return {}, text
            return fm, body
    return {}, text


def read_skill_metadata(skill_dir: Path) -> dict[str, Any]:
    """Frontmatter merged with `agents/openai.yaml` (display name, short
    description, icons, dependencies, invocation policy). Frontmatter
    wins for `name`/`description`; openai.yaml supplies `display_name`."""
    md = skill_dir / "SKILL.md"
    meta: dict[str, Any] = {}
    if md.is_file():
        try:
            fm, _ = parse_skill_md(md.read_text())
            meta.update(fm)
        except OSError:
            pass
    openai_yaml = skill_dir / "agents" / "openai.yaml"
    if openai_yaml.is_file():
        try:
            data = yaml.safe_load(openai_yaml.read_text()) or {}
            if isinstance(data, dict):
                meta.setdefault("display_name", data.get("name") or data.get("display_name"))
                meta["openai"] = data
        except (OSError, yaml.YAMLError):
            pass
    return meta


# ── hashing ──────────────────────────────────────────────────────────


def content_hash(skill_dir: Path) -> str:
    """Stable content hash over SKILL.md + agents/openai.yaml. Excludes
    the ownership sidecar so re-stamping it never changes the hash."""
    h = hashlib.sha256()
    for rel in ("SKILL.md", "agents/openai.yaml"):
        p = skill_dir / rel
        if p.is_file():
            try:
                h.update(rel.encode())
                h.update(b"\0")
                h.update(p.read_bytes())
            except OSError:
                continue
    return h.hexdigest()[:16]


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _skill_id(realpath: str, source_type: str, workspace: str | None) -> str:
    raw = f"{source_type}\0{workspace or ''}\0{realpath}"
    return "sk_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── validation ───────────────────────────────────────────────────────


def validate_skill_dir(skill_dir: Path) -> tuple[bool, list[str], list[str]]:
    """Validate a skill directory. Returns (valid, errors, warnings).

    Injection-blocking errors: SKILL.md missing/unreadable, frontmatter
    missing `name` or `description`. Everything else is a warning so the
    skill still shows up (and stays injectable) but the operator is told.
    """
    errors: list[str] = []
    warnings: list[str] = []

    real = skill_dir
    try:
        real = skill_dir.resolve(strict=False)
    except (OSError, RuntimeError):
        # RuntimeError == symlink loop on some platforms.
        errors.append("path does not resolve (possible symlink loop)")
        return False, errors, warnings

    md = skill_dir / "SKILL.md"
    if not md.is_file():
        errors.append("missing SKILL.md")
        return False, errors, warnings

    try:
        text = md.read_text()
    except OSError as exc:
        errors.append(f"SKILL.md unreadable: {exc}")
        return False, errors, warnings

    fm, _ = parse_skill_md(text)
    name = str(fm.get("name", "")).strip()
    desc = str(fm.get("description", "")).strip()
    if not name:
        errors.append("frontmatter missing required `name`")
    if not desc:
        errors.append("frontmatter missing required `description`")

    # Soft signals.
    if name and name != skill_dir.name:
        warnings.append(
            f"frontmatter name `{name}` differs from directory `{skill_dir.name}`"
        )
    try:
        size = md.stat().st_size
        if size > _LARGE_SKILL_BYTES:
            warnings.append(f"SKILL.md is large ({size // 1024} KB)")
    except OSError:
        pass
    if skill_dir.is_symlink():
        warnings.append(f"skill is a symlink → {real}")
    if fm.get("allowed-tools") or fm.get("allowed_tools"):
        warnings.append("declares allowed-tools (review tool grants)")

    return (not errors), errors, warnings


# ── ref construction + discovery ─────────────────────────────────────


def _make_ref(
    skill_dir: Path,
    source_type: str,
    *,
    workspace: str | None = None,
    owner_plugin: str | None = None,
) -> SkillRef:
    valid, errors, warnings = validate_skill_dir(skill_dir)
    fm = read_skill_metadata(skill_dir) if valid or (skill_dir / "SKILL.md").is_file() else {}
    real = skill_dir
    try:
        real = skill_dir.resolve(strict=False)
    except (OSError, RuntimeError):
        pass
    sym_target = None
    if skill_dir.is_symlink():
        try:
            sym_target = os.readlink(skill_dir)
        except OSError:
            sym_target = str(real)

    # Adopt owner_plugin from the sidecar when not explicitly given.
    if owner_plugin is None:
        sc = read_sidecar(skill_dir)
        if sc:
            owner_plugin = sc.get("owner_plugin")

    md = skill_dir / "SKILL.md"
    size = 0
    mtime = 0.0
    try:
        st = md.stat()
        size = st.st_size
        mtime = st.st_mtime
    except OSError:
        pass

    return SkillRef(
        id=_skill_id(str(real), source_type, workspace),
        name=str(fm.get("name") or skill_dir.name),
        display_name=str(fm.get("display_name") or fm.get("name") or skill_dir.name),
        description=str(fm.get("description") or ""),
        source_type=source_type,
        path=str(skill_dir),
        realpath=str(real),
        owner_plugin=owner_plugin,
        workspace=workspace,
        symlink_target=sym_target,
        content_hash=content_hash(skill_dir),
        valid=valid,
        errors=errors,
        warnings=warnings,
        frontmatter=fm,
        size=size,
        mtime=mtime,
    )


def _iter_skill_dirs(root: Path) -> Iterable[Path]:
    """Yield immediate child directories of `root` that contain a
    SKILL.md (sorted, hidden dirs skipped)."""
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            yield entry


def discover_workspace_skills(
    config_home: Path,
    workspace: str,
    *,
    include_user: bool = True,
    include_runtime: bool = True,
) -> list[SkillRef]:
    """Discover the skills a given workspace exposes — user-authored
    (`skills/`) and plugin-materialized (`runtime/skills/`). This is the
    function the harnesses call so injection and the lens agree."""
    ws_root = config_home / "workspaces" / workspace
    out: list[SkillRef] = []
    if include_user:
        for d in _iter_skill_dirs(ws_root / "skills"):
            out.append(_make_ref(d, SOURCE_WORKSPACE, workspace=workspace))
    if include_runtime:
        for d in _iter_skill_dirs(ws_root / "runtime" / "skills"):
            out.append(_make_ref(d, SOURCE_RUNTIME_PLUGIN, workspace=workspace))
    return out


def discover_codex_skills() -> list[SkillRef]:
    """Read-only inventory of `~/.codex/skills` + `.system`. relaydeck never
    writes here; these are shown so the operator sees the full picture."""
    root = codex_skills_root()
    out: list[SkillRef] = []
    for d in _iter_skill_dirs(root):
        out.append(_make_ref(d, SOURCE_CODEX_USER))
    for d in _iter_skill_dirs(root / ".system"):
        out.append(_make_ref(d, SOURCE_CODEX_SYSTEM))
    return out


def discover_claude_skills(
    workspaces: Iterable[tuple[str, Path]] | None = None,
) -> list[SkillRef]:
    """Read-only inventory of Claude Code's skills: user-level
    `~/.claude/skills` plus each registered workspace's project-level
    `<repo>/.claude/skills`. claude-code loads these itself, so relaydeck
    shows them (so the operator sees what a claude-code agent has) but
    never injects or writes them."""
    out: list[SkillRef] = []
    for d in _iter_skill_dirs(claude_skills_root()):
        out.append(_make_ref(d, SOURCE_CLAUDE_USER))
    for name, path in (workspaces or []):
        proj = Path(path) / ".claude" / "skills"
        for d in _iter_skill_dirs(proj):
            out.append(_make_ref(d, SOURCE_CLAUDE_PROJECT, workspace=name))
    return out


def discover_all_skills(
    config_home: Path, *, include_codex: bool = True, include_claude: bool = True
) -> list[SkillRef]:
    """Fleet-wide inventory across every registered workspace, plus the
    read-only codex + claude-code roots."""
    from relaydeck.config import load_workspace_registry

    out: list[SkillRef] = []
    ws_paths: list[tuple[str, Path]] = []
    try:
        for ws in load_workspace_registry(config_home):
            out.extend(discover_workspace_skills(config_home, ws.name))
            ws_paths.append((ws.name, ws.path))
    except Exception as exc:
        logger.debug("skills: workspace discovery failed: %s", exc)
    if include_codex:
        out.extend(discover_codex_skills())
    if include_claude:
        out.extend(discover_claude_skills(ws_paths))
    return out


# ── injection helpers (used by harnesses) ────────────────────────────


def injection_dirs(
    config_home: Path,
    workspace: str,
    *,
    valid_only: bool = True,
) -> tuple[list[Path], list[Path]]:
    """Return (user_skill_dirs, runtime_skill_dirs) for a workspace, as
    `Path`s, ready for `--skill` flags / prompt path lists / inlining.
    Invalid skills are skipped by default so a malformed SKILL.md never
    reaches the model."""
    user: list[Path] = []
    runtime: list[Path] = []
    for ref in discover_workspace_skills(config_home, workspace):
        if valid_only and not ref.valid:
            continue
        if ref.source_type == SOURCE_WORKSPACE:
            user.append(Path(ref.path))
        elif ref.source_type == SOURCE_RUNTIME_PLUGIN:
            runtime.append(Path(ref.path))
    return user, runtime


# ── materialization (plugin-contributed skills) ──────────────────────


def _sidecar_path(skill_dir: Path) -> Path:
    return skill_dir / SIDECAR_NAME


def read_sidecar(skill_dir: Path) -> dict[str, Any] | None:
    p = _sidecar_path(skill_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _runtime_skill_dir(config_home: Path, workspace: str, skill_name: str) -> Path:
    return config_home / "workspaces" / workspace / "runtime" / "skills" / skill_name


def materialize_skill(
    config_home: Path,
    workspace: str,
    owner_plugin: str,
    skill_name: str,
    content: str,
    *,
    source_path: str | None = None,
) -> str:
    """Write `runtime/skills/<skill_name>/SKILL.md` + ownership sidecar
    for a plugin-contributed skill.

    Collision-safe and idempotent. Returns one of:
      - "written"   — the SKILL.md was (re)written
      - "unchanged" — same owner + same source hash already on disk
      - "conflict"  — a *different* plugin owns this dir; left untouched
      - "invalid"   — skill_name/workspace is not a safe path component;
                      skipped (a malformed/malicious `[plugin.skills]`
                      name like `../x` can't escape the runtime dir)
    """
    try:
        validate_path_component(workspace, kind="workspace")
        validate_path_component(skill_name, kind="skill name")
    except ValueError as exc:
        logger.warning("skills: %s skipped — %s", owner_plugin, exc)
        return "invalid"
    d = _runtime_skill_dir(config_home, workspace, skill_name)
    new_hash = _text_hash(content)
    existing = read_sidecar(d)
    if existing and existing.get("owner_plugin") not in (None, owner_plugin):
        logger.warning(
            "skills: %s/%s owned by %s; %s refused to overwrite",
            workspace, skill_name, existing.get("owner_plugin"), owner_plugin,
        )
        return "conflict"
    md = d / "SKILL.md"
    if existing and existing.get("source_hash") == new_hash and md.is_file():
        return "unchanged"

    d.mkdir(parents=True, exist_ok=True)
    md.write_text(content)
    sidecar = {
        "owner_plugin": owner_plugin,
        "skill_name": skill_name,
        "source_path": source_path,
        "source_hash": new_hash,
        "managed_by": MANAGED_BY,
        "written_at": time.time(),
    }
    _sidecar_path(d).write_text(json.dumps(sidecar, indent=2))
    return "written"


def remove_skill(
    config_home: Path, workspace: str, owner_plugin: str, skill_name: str
) -> bool:
    """Remove a relaydeck-managed skill (and its dir if it empties). Only
    removes when the sidecar marks `owner_plugin` as the owner — never
    touches user-authored or another plugin's skill. Returns True if a
    skill was removed."""
    d = _runtime_skill_dir(config_home, workspace, skill_name)
    if not d.exists():
        return False
    sc = read_sidecar(d)
    if sc is None or sc.get("owner_plugin") not in (None, owner_plugin):
        return False
    removed = False
    for child in (d / "SKILL.md", _sidecar_path(d)):
        try:
            if child.exists():
                child.unlink()
                removed = True
        except OSError as exc:
            logger.debug("skills: remove %s failed: %s", child, exc)
    try:
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass
    return removed


def materialized_skills(config_home: Path, workspace: str) -> list[dict[str, Any]]:
    """List relaydeck-managed skills in a workspace's runtime/skills tree,
    reading each ownership sidecar. Used by sync to find orphans."""
    root = config_home / "workspaces" / workspace / "runtime" / "skills"
    out: list[dict[str, Any]] = []
    for d in _iter_skill_dirs(root):
        sc = read_sidecar(d)
        if sc and sc.get("managed_by") == MANAGED_BY:
            out.append({"skill_name": d.name, **sc})
    return out


def sync_plugin_skills(
    config_home: Path,
    owner_plugin: str,
    skills: dict[str, str],
    target_workspaces: Iterable[str],
    all_workspaces: Iterable[str],
) -> dict[str, Any]:
    """Reconcile one plugin's skills across workspaces.

    `skills` maps skill_name → content. For every workspace in
    `target_workspaces` each skill is materialized; in every other
    workspace (from `all_workspaces`) any relaydeck-managed skill owned by
    this plugin that's no longer wanted is removed. Idempotent — safe to
    call on every lifecycle event.

    Returns a small report: {written, unchanged, conflict, removed}.
    """
    targets = set(target_workspaces)
    report = {"written": 0, "unchanged": 0, "conflict": 0, "removed": 0}
    wanted_names = set(skills)

    for ws in targets:
        for name, content in skills.items():
            res = materialize_skill(config_home, ws, owner_plugin, name, content)
            if res in report:
                report[res] += 1

    for ws in set(all_workspaces):
        for entry in materialized_skills(config_home, ws):
            if entry.get("owner_plugin") != owner_plugin:
                continue
            name = entry.get("skill_name")
            # Remove if the workspace is no longer a target, or the skill
            # name was retired from the plugin's manifest.
            if ws not in targets or name not in wanted_names:
                if remove_skill(config_home, ws, owner_plugin, str(name)):
                    report["removed"] += 1
    return report


def remove_all_plugin_skills(
    config_home: Path, owner_plugin: str, all_workspaces: Iterable[str]
) -> int:
    """Remove every relaydeck-managed skill owned by `owner_plugin` across the
    given workspaces. Used when a plugin is uninstalled (not merely
    disabled). Returns the count removed."""
    count = 0
    for ws in set(all_workspaces):
        for entry in materialized_skills(config_home, ws):
            if entry.get("owner_plugin") == owner_plugin:
                if remove_skill(config_home, ws, owner_plugin, str(entry["skill_name"])):
                    count += 1
    return count
