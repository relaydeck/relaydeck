"""External skill import and managed-source refresh helpers."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from relaydeck import skills_cache

from . import analysis, source_parser

_GITHUB_TREE_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?"
    r"(?:/tree/(?P<ref>[^/]+)(?:/(?P<path>.*))?)?/?$"
)
_MAX_DISCOVER_DEPTH = 10


def imports_root(config_home: Path) -> Path:
    return config_home / "plugin-data" / "skills" / "imports"


def safe_source_slug(label: str) -> str:
    parsed = urlparse(label)
    raw = (parsed.netloc + parsed.path).strip("/") if parsed.netloc else label
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")[:64] or "skill"
    digest = hashlib.sha256(label.encode()).hexdigest()[:10]
    return f"{slug}-{digest}"


def alias_from_meta(meta: dict[str, Any], fallback: str) -> str:
    raw = str(meta.get("name") or fallback or "skill").strip().lower()
    alias = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-._")
    return alias[:64] or "skill"


def parse_git_source(
    source_url: str,
    *,
    source_ref: str | None = None,
    source_subpath: str | None = None,
) -> dict[str, str | None]:
    git = source_parser.parse_github_url(source_url)
    return {
        "repo_url": git["repo_url"],
        "source_ref": source_ref or git.get("source_ref"),
        "source_subpath": source_subpath or git.get("source_subpath"),
    }


def fetch_git_repo(
    config_home: Path,
    repo_url: str,
    *,
    source_ref: str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    parsed = parse_git_source(repo_url, source_ref=source_ref)
    cache_key = f"{parsed['repo_url']}@{parsed.get('source_ref') or 'default'}"
    dest = imports_root(config_home) / safe_source_slug(cache_key)
    if refresh and dest.exists():
        shutil.rmtree(dest)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth", "1"]
        if parsed.get("source_ref"):
            cmd.extend(["--branch", str(parsed["source_ref"])])
        cmd.extend([str(parsed["repo_url"]), str(dest)])
        _run_git(cmd, "git clone failed")
    return {
        "repo_path": str(dest),
        "repo_url": parsed["repo_url"],
        "source_ref": parsed.get("source_ref"),
    }


def _remote_refs(repo_url: str) -> set[str]:
    """Branch + tag names on the remote (no clone). Empty set on any failure."""
    out = subprocess.run(
        ["git", "ls-remote", "--heads", "--tags", repo_url],
        capture_output=True, text=True, timeout=30,
    )
    names: set[str] = set()
    for line in out.stdout.splitlines():
        _sha, _tab, refname = line.partition("\t")
        refname = refname.strip()
        if refname.endswith("^{}"):  # peeled tag — same name, skip the dup
            continue
        for prefix in ("refs/heads/", "refs/tags/"):
            if refname.startswith(prefix):
                names.add(refname[len(prefix):])
    return names


def _disambiguate_ref(
    repo_url: str, ref: str | None, subpath: str | None,
) -> tuple[str | None, str | None]:
    """GitHub `/tree/<ref>/<path>` URLs are ambiguous when the branch name
    contains slashes — `/tree/feature/foo/skills/bar` parses to
    ref=`feature`, subpath=`foo/skills/bar`, which clones the wrong branch.
    Resolve the real ref against the remote (longest ref that is a path-prefix
    of `<ref>/<subpath>` wins) and re-split the remainder as the subpath. A
    no-op when there's no tail to disambiguate or the remote can't be reached."""
    if not ref or not subpath:
        return ref, subpath
    tail = f"{ref}/{subpath}".strip("/")
    try:
        refs = _remote_refs(repo_url)
    except Exception:
        return ref, subpath
    best = None
    for r in refs:
        if (r == tail or tail.startswith(r + "/")) and (best is None or len(r) > len(best)):
            best = r
    if best is None or best == ref:
        return ref, subpath
    rest = tail[len(best):].strip("/")
    return best, (rest or None)


def fetch_git_skill(
    config_home: Path,
    source_url: str,
    *,
    source_ref: str | None = None,
    source_subpath: str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    parsed = parse_git_source(
        source_url, source_ref=source_ref, source_subpath=source_subpath
    )
    ref, subpath = _disambiguate_ref(
        str(parsed["repo_url"]),
        parsed.get("source_ref"),
        parsed.get("source_subpath"),
    )
    parsed["source_ref"], parsed["source_subpath"] = ref, subpath
    repo = fetch_git_repo(
        config_home,
        str(parsed["repo_url"]),
        source_ref=parsed.get("source_ref"),
        refresh=refresh,
    )
    skill_path = (Path(repo["repo_path"]) / str(parsed.get("source_subpath") or "")).resolve()
    if not (skill_path / "SKILL.md").is_file():
        discovered = discover_skills_in_tree(
            skill_path if skill_path.is_dir() else Path(repo["repo_path"])
        )
        if len(discovered) == 1:
            skill_path = Path(discovered[0]["path"])
        elif discovered:
            names = ", ".join(d["name"] for d in discovered[:8])
            extra = f" (+{len(discovered) - 8} more)" if len(discovered) > 8 else ""
            raise ValueError(
                f"{skill_path} has no SKILL.md — found {len(discovered)} skills nearby: "
                f"{names}{extra}. Pick a skill directory or filter with --skill."
            )
        else:
            raise ValueError(f"{skill_path} has no SKILL.md")
    return {
        "path": str(skill_path),
        "repo_path": repo["repo_path"],
        "source_url": source_url,
        "source_ref": parsed.get("source_ref"),
        "source_subpath": parsed.get("source_subpath") or _relative_subpath(
            Path(repo["repo_path"]), skill_path
        ),
    }


def discover_skills_in_tree(root: Path, *, max_depth: int = _MAX_DISCOVER_DEPTH) -> list[dict[str, Any]]:
    root = root.expanduser().resolve()
    if not root.exists():
        raise ValueError(f"path not found: {root}")
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")

    if (root / "SKILL.md").is_file():
        meta = read_skill_metadata(root)
        meta["relative_subpath"] = "."
        meta["subpath"] = "."
        return [meta]

    found: list[dict[str, Any]] = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        try:
            rel = skill_dir.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) > max_depth:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        meta = read_skill_metadata(skill_dir)
        rel_s = str(rel).replace("\\", "/")
        meta["relative_subpath"] = rel_s
        meta["subpath"] = rel_s
        found.append(meta)
    found.sort(key=lambda s: (str(s.get("name") or ""), str(s.get("relative_subpath") or "")))
    return found


def resolve_import_source(
    config_home: Path,
    source_text: str,
    *,
    refresh: bool = False,
    skill_filter: str | None = None,
) -> dict[str, Any]:
    parsed = source_parser.parse(source_text)
    needle = skill_filter or parsed.skill_filter

    if parsed.kind == "local":
        root = Path(str(parsed.local_path)).expanduser().resolve()
        skills = discover_skills_in_tree(root)
        if needle:
            skills = [s for s in skills if source_parser.skill_matches_filter(s, needle)]
        if not skills:
            raise ValueError("no skills with SKILL.md found at that path")
        return {
            "parsed": parsed.to_dict(),
            "source": {
                "kind": "local",
                "local_path": str(root),
                "display": parsed.display,
            },
            "skills": skills,
            "skill_filter": needle,
        }

    if parsed.kind == "npm":
        pkg = fetch_npm_package(config_home, str(parsed.npm_package), refresh=refresh)
        skills = discover_skills_in_tree(Path(pkg["path"]))
        if needle:
            skills = [s for s in skills if source_parser.skill_matches_filter(s, needle)]
        if not skills:
            raise ValueError(f"no SKILL.md found in npm package {parsed.npm_package!r}")
        return {
            "parsed": parsed.to_dict(),
            "source": {
                "kind": "npm",
                "npm_package": parsed.npm_package,
                "path": pkg["path"],
                "display": parsed.display,
            },
            "skills": skills,
            "skill_filter": needle,
        }

    repo = fetch_git_repo(
        config_home,
        str(parsed.repo_url),
        source_ref=parsed.source_ref,
        refresh=refresh,
    )
    subpath = source_parser.normalize_github_subpath(parsed.source_subpath)
    root = Path(repo["repo_path"])
    if subpath:
        root = (root / subpath).resolve()
    if (root / "SKILL.md").is_file():
        meta = read_skill_metadata(root)
        rel = subpath or "."
        meta["relative_subpath"] = rel
        meta["subpath"] = rel
        skills = [meta]
    elif root.is_dir():
        skills = discover_skills_in_tree(root)
    else:
        raise ValueError(f"subpath not found in repo: {subpath or parsed.source_subpath}")
    if needle:
        skills = [s for s in skills if source_parser.skill_matches_filter(s, needle)]
    if not skills:
        raise ValueError("no skills with SKILL.md found at that source")
    return {
        "parsed": parsed.to_dict(),
        "source": {
            "kind": "git",
            "repo_path": repo["repo_path"],
            "repo_url": parsed.repo_url,
            "source_ref": repo.get("source_ref"),
            "source_subpath": subpath or parsed.source_subpath,
            "display": parsed.display,
        },
        "skills": skills,
        "skill_filter": needle,
    }


def fetch_npm_package(
    config_home: Path,
    package_name: str,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    dest = imports_root(config_home) / safe_source_slug(f"npm:{package_name}")
    if refresh and dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    pack_dir = dest / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["npm", "pack", package_name, "--pack-destination", str(pack_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise ValueError("npm is not installed — install Node.js/npm to import npm skills") from exc
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or str(exc)).strip()
        raise ValueError(f"npm pack failed: {msg}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("npm pack timed out") from exc

    tarball_name = (proc.stdout or "").strip().splitlines()[-1].strip()
    tarball = pack_dir / tarball_name
    if not tarball.is_file():
        matches = list(pack_dir.glob("*.tgz"))
        tarball = matches[0] if matches else None
    if tarball is None or not tarball.is_file():
        raise ValueError(f"npm pack did not produce a tarball for {package_name!r}")

    extract = dest / "extract"
    if extract.exists():
        shutil.rmtree(extract)
    extract.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(extract, filter="data")
    package_root = extract / "package"
    if not package_root.is_dir():
        children = [p for p in extract.iterdir() if p.is_dir()]
        package_root = children[0] if len(children) == 1 else extract
    return {"path": str(package_root.resolve()), "npm_package": package_name}


def read_skill_metadata(path: str | Path) -> dict[str, Any]:
    skill_path = Path(path).expanduser().resolve()
    meta = analysis.skill_metadata(skill_path)
    return {
        **meta,
        "path": str(skill_path),
        "has_skill_md": (skill_path / "SKILL.md").is_file(),
    }


def import_git_skill(
    config_home: Path,
    db_path: str | Path,
    *,
    workspace: str,
    source_url: str,
    alias: str | None = None,
    mode: str = "symlink",
    source_ref: str | None = None,
    source_subpath: str | None = None,
    refresh: bool = False,
    require_valid: bool = True,
) -> dict[str, Any]:
    if source_subpath is not None or source_ref is not None:
        fetched = fetch_git_skill(
            config_home,
            source_url,
            source_ref=source_ref,
            source_subpath=source_subpath,
            refresh=refresh,
        )
        meta = read_skill_metadata(fetched["path"])
        if require_valid and not meta["valid"]:
            raise ValueError("invalid skill: " + "; ".join(meta["errors"]))
        from . import manager

        resolved_alias = alias or alias_from_meta(meta, Path(fetched["path"]).name)
        link = manager.link_skill(
            config_home,
            db_path,
            workspace,
            fetched["path"],
            resolved_alias,
            mode,
            target_id=meta["content_hash"],
            source_url=fetched.get("source_url"),
            source_ref=fetched.get("source_ref"),
            source_subpath=fetched.get("source_subpath"),
            link_status="imported",
        )
        return {"link": link, "meta": meta, "source": fetched}

    resolved = resolve_import_source(config_home, source_url, refresh=refresh)
    skills = resolved["skills"]
    if len(skills) != 1:
        raise ValueError(
            f"source resolves to {len(skills)} skills — use batch import or pick a skill path"
        )
    batch = import_resolved_skills(
        config_home,
        db_path,
        workspace=workspace,
        resolved=resolved,
        selections=[{"subpath": skills[0].get("relative_subpath") or ".", "alias": alias}],
        mode=mode,
        require_valid=require_valid,
    )
    item = batch["imports"][0]
    return {"link": item["link"], "meta": item["meta"], "source": resolved["source"]}


def import_resolved_skills(
    config_home: Path,
    db_path: str | Path,
    *,
    resolved: dict[str, Any],
    selections: list[dict[str, Any]],
    workspace: str | None = None,
    deploy_to: str | list[str] | None = "all",
    mode: str = "symlink",
    require_valid: bool = True,
) -> dict[str, Any]:
    from . import manager

    by_subpath = {
        str(s.get("relative_subpath") or s.get("subpath") or "."): s
        for s in resolved.get("skills") or []
    }
    source = resolved.get("source") or {}
    parsed = resolved.get("parsed") or {}
    imports: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    if isinstance(deploy_to, list):
        deploy_workspaces = [w for w in deploy_to if w]
    elif deploy_to == "none":
        deploy_workspaces = []
    elif workspace:
        deploy_workspaces = [workspace]
    else:
        deploy_workspaces = manager._all_workspaces(config_home)

    for sel in selections:
        subpath = str(sel.get("subpath") or sel.get("relative_subpath") or ".").strip() or "."
        meta = by_subpath.get(subpath)
        if meta is None:
            errors.append({"subpath": subpath, "error": "skill not found in resolved source"})
            continue
        if require_valid and not meta.get("valid"):
            errors.append({
                "subpath": subpath,
                "error": "invalid: " + "; ".join(meta.get("errors") or []),
            })
            continue
        alias = str(sel.get("alias") or alias_from_meta(meta, Path(str(meta["path"])).name))
        try:
            catalog = manager.register_catalog_skill(
                config_home,
                db_path,
                str(meta["path"]),
                alias,
                target_id=meta.get("content_hash"),
                source_url=_provenance_url(parsed, source),
                source_ref=source.get("source_ref"),
                source_subpath=_provenance_subpath(source, subpath),
            )
            deployments: list[dict[str, Any]] = []
            for ws in deploy_workspaces:
                try:
                    deployments.append(
                        manager.deploy_catalog_skill(
                            config_home, db_path, alias, ws, mode=mode,
                        )
                    )
                except Exception as exc:
                    errors.append({
                        "subpath": subpath,
                        "workspace": ws,
                        "alias": alias,
                        "error": str(exc),
                    })
            imports.append({
                "catalog": catalog,
                "deployments": deployments,
                "meta": meta,
                "subpath": subpath,
                "alias": alias,
                "link": deployments[0] if len(deployments) == 1 else catalog,
            })
        except Exception as exc:
            errors.append({"subpath": subpath, "alias": alias, "error": str(exc)})

    if not imports and errors:
        raise ValueError(errors[0]["error"])
    return {
        "imports": imports,
        "errors": errors,
        "source": source,
        "deployed_workspaces": deploy_workspaces,
    }


def refresh_managed_imports(
    config_home: Path,
    db_path: str | Path,
    *,
    min_interval_s: float = 3600.0,
    force: bool = False,
) -> dict[str, Any]:
    """Check Relaydeck-managed imports for local drift or upstream commits."""
    now = time.time()
    checked = 0
    changed = 0
    update_available = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    for link in skills_cache.list_skill_links(db_path):
        if not link.get("source_url"):
            continue
        last = float(link.get("last_checked_at") or 0)
        if not force and last and now - last < min_interval_s:
            skipped += 1
            continue
        try:
            meta_path = _link_metadata_path(config_home, link)
            meta = read_skill_metadata(meta_path)
            checked += 1
            current_hash = str(meta.get("content_hash") or "")
            status = str(link.get("review_status") or "imported")
            summary = str(link.get("review_summary") or "")
            if current_hash and link.get("target_id") and current_hash != link["target_id"]:
                changed += 1
                status = "changed-locally"
                summary = "Managed skill content changed on disk."
            upstream = _upstream_status(link)
            if upstream == "available":
                update_available += 1
                if status != "changed-locally":
                    status = "update-available"
                    summary = "Upstream Git source has a newer commit available."
            skills_cache.update_skill_link_state(
                db_path,
                link["workspace"],
                link["alias"],
                target_id=current_hash or link.get("target_id"),
                review_status=status,
                review_summary=summary,
                last_checked_at=now,
            )
        except Exception as exc:
            errors.append({
                "workspace": str(link.get("workspace") or ""),
                "alias": str(link.get("alias") or ""),
                "error": str(exc),
            })
    return {
        "checked": checked,
        "skipped": skipped,
        "changed": changed,
        "update_available": update_available,
        "errors": errors,
    }


def _link_metadata_path(config_home: Path, link: dict[str, Any]) -> str:
    mode = str(link.get("mode") or "symlink")
    if mode == "copy":
        ws = str(link.get("workspace") or "")
        alias = str(link.get("alias") or "")
        if ws and alias:
            deployed = (config_home / "workspaces" / ws / "skills" / alias).resolve()
            if (deployed / "SKILL.md").is_file():
                return str(deployed)
    return str(link.get("target_path") or "")


def _provenance_url(parsed: dict[str, Any], source: dict[str, Any]) -> str:
    if source.get("kind") == "npm":
        return f"npm:{source.get('npm_package')}"
    if parsed.get("raw"):
        return str(parsed["raw"])
    return str(source.get("repo_url") or "")


def _provenance_subpath(source: dict[str, Any], skill_subpath: str) -> str | None:
    base = source.get("source_subpath")
    if skill_subpath in (".", ""):
        return base
    if base:
        return f"{base}/{skill_subpath}".strip("/")
    return skill_subpath


def _relative_subpath(repo_root: Path, skill_path: Path) -> str:
    try:
        rel = skill_path.relative_to(repo_root)
        return str(rel).replace("\\", "/") or "."
    except ValueError:
        return "."


def _upstream_status(link: dict[str, Any]) -> str:
    repo = _repo_root(Path(link["target_path"]))
    if repo is None:
        return "unknown"
    local = _git_stdout(["git", "-C", str(repo), "rev-parse", "HEAD"])
    parsed = parse_git_source(
        str(link.get("source_url") or ""),
        source_ref=link.get("source_ref"),
        source_subpath=link.get("source_subpath"),
    )
    ref = str(parsed.get("source_ref") or "HEAD")
    remote = _git_stdout(["git", "ls-remote", str(parsed["repo_url"]), ref])
    remote_sha = remote.split()[0] if remote.strip() else ""
    if remote_sha and local and remote_sha != local:
        return "available"
    return "current"


def _repo_root(path: Path) -> Path | None:
    cur = path.resolve()
    if cur.is_file():
        cur = cur.parent
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _run_git(cmd: list[str], label: str) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=90)
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or str(exc)).strip()
        raise ValueError(f"{label}: {msg}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{label}: timed out") from exc


def _git_stdout(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip()
