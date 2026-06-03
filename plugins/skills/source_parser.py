"""Parse flexible skill import sources (URLs, commands, local paths)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_GITHUB_TREE_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?"
    r"(?:/tree/(?P<ref>[^/]+)(?:/(?P<path>.*))?)?/?$"
)
_GITHUB_BLOB_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?"
    r"/blob/(?P<ref>[^/]+)(?:/(?P<path>.*))?/?$"
)
_NPX_SKILLS_RE = re.compile(
    r"^\s*npx\s+(?:--yes\s+|--no-install\s+|--package=\S+\s+)*skills\s+add\s+"
    r"(?P<url>\S+)(?:\s+(?P<rest>.*))?$",
    re.IGNORECASE,
)
_NPM_INSTALL_RE = re.compile(
    r"^\s*npm\s+install\s+(?:-(?:g|global)\s+|--global\s+)+(?P<pkg>@?[^\s]+)\s*$",
    re.IGNORECASE,
)
_SKILL_FLAG_RE = re.compile(r"(?:--skill|-s)\s+(?P<name>[^\s]+)", re.IGNORECASE)
_URLISH_RE = re.compile(r"^https?://|^git@|^ssh://|\.git(?:/|$|\?)", re.IGNORECASE)


@dataclass
class ParsedSource:
    kind: str  # git | npm | local
    raw: str
    repo_url: str | None = None
    source_ref: str | None = None
    source_subpath: str | None = None
    npm_package: str | None = None
    skill_filter: str | None = None
    local_path: str | None = None
    display: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "raw": self.raw,
            "repo_url": self.repo_url,
            "source_ref": self.source_ref,
            "source_subpath": self.source_subpath,
            "npm_package": self.npm_package,
            "skill_filter": self.skill_filter,
            "local_path": self.local_path,
            "display": self.display,
        }


def _strip_repo_suffix(name: str) -> str:
    return name[:-4] if name.endswith(".git") else name


def _github_parts(path: str) -> tuple[str, str, str | None, str | None] | None:
    for pattern in (_GITHUB_TREE_RE, _GITHUB_BLOB_RE):
        match = pattern.match(path)
        if match:
            owner = match.group("owner")
            repo = _strip_repo_suffix(match.group("repo"))
            return owner, repo, match.group("ref"), match.group("path")
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] and parts[1]:
        owner, repo = parts[0], _strip_repo_suffix(parts[1])
        if repo and owner:
            return owner, repo, None, None
    return None


def normalize_github_subpath(subpath: str | None) -> str | None:
    """Turn …/SKILL.md blob paths into the skill directory subpath."""
    if not subpath:
        return None
    parts = subpath.replace("\\", "/").strip("/")
    if parts.lower().endswith("/skill.md"):
        parts = parts[: -len("/skill.md")]
    elif parts.lower() == "skill.md":
        parts = ""
    return parts or None


def parse_github_url(url: str) -> dict[str, str | None]:
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    if host not in ("github.com", "www.github.com"):
        return {
            "repo_url": url.strip(),
            "source_ref": None,
            "source_subpath": None,
        }
    gh = _github_parts(parsed.path or "")
    if not gh:
        return {"repo_url": url.strip(), "source_ref": None, "source_subpath": None}
    owner, repo, ref, subpath = gh
    return {
        "repo_url": f"https://github.com/{owner}/{repo}.git",
        "source_ref": ref,
        "source_subpath": normalize_github_subpath(subpath),
    }


def parse(source_text: str) -> ParsedSource:
    raw = (source_text or "").strip()
    if not raw:
        raise ValueError("paste a GitHub URL, npm package, npx command, or local path")

    npx = _NPX_SKILLS_RE.match(raw)
    if npx:
        url = npx.group("url").strip().strip("'\"")
        rest = npx.group("rest") or ""
        skill_filter = None
        m = _SKILL_FLAG_RE.search(rest)
        if m:
            skill_filter = m.group("name").strip().strip("'\"")
        git = parse_github_url(url)
        return ParsedSource(
            kind="git",
            raw=raw,
            repo_url=str(git["repo_url"]),
            source_ref=git.get("source_ref"),
            source_subpath=git.get("source_subpath"),
            skill_filter=skill_filter,
            display=f"GitHub · {git['repo_url']}" + (f" · skill {skill_filter}" if skill_filter else ""),
        )

    npm = _NPM_INSTALL_RE.match(raw)
    if npm:
        pkg = npm.group("pkg").strip().strip("'\"")
        return ParsedSource(
            kind="npm",
            raw=raw,
            npm_package=pkg,
            display=f"npm · {pkg}",
        )

    expanded = Path(raw).expanduser()
    if not _URLISH_RE.search(raw) and (expanded.exists() or raw.startswith(("/", "~", "."))):
        path = str(expanded.resolve()) if expanded.exists() else str(expanded)
        return ParsedSource(
            kind="local",
            raw=raw,
            local_path=path,
            display=f"local · {path}",
        )

    git = parse_github_url(raw)
    return ParsedSource(
        kind="git",
        raw=raw,
        repo_url=str(git["repo_url"]),
        source_ref=git.get("source_ref"),
        source_subpath=git.get("source_subpath"),
        display=f"GitHub · {git['repo_url']}",
    )


def skill_matches_filter(scan: dict[str, Any], needle: str) -> bool:
    if not needle:
        return True
    n = needle.strip().lower()
    if not n:
        return True
    hay = [
        str(scan.get("name") or ""),
        str(scan.get("display_name") or ""),
        Path(str(scan.get("path") or "")).name,
        str(scan.get("relative_subpath") or scan.get("subpath") or ""),
    ]
    for item in hay:
        val = item.lower()
        if val == n or n in val or val in n:
            return True
        if val.replace("_", "-") == n.replace("_", "-"):
            return True
    return False
