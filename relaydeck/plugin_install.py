"""Shared plugin install helpers for CLI and daemon API surfaces."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class PluginInstallError(RuntimeError):
    """Raised when a plugin install source cannot be handled."""


@dataclass(frozen=True)
class PluginInstallResult:
    names: list[str]
    installed_via: str
    source: str
    dest: str | None = None
    restart_required: bool = True


def find_plugin_manifest_path(root: Path) -> Path:
    if root.is_file():
        return root
    direct = root / "plugin.toml"
    if direct.exists():
        return direct
    matches = sorted(root.rglob("plugin.toml"))
    if not matches:
        raise PluginInstallError(f"No plugin.toml found in {str(root)!r}")
    return matches[0]


def install_plugin_source(
    src: str,
    config_home: Path,
    *,
    editable: bool = False,
    install_python_package: Callable[[str], None] | None = None,
    install_editable_python_package: Callable[[str], None] | None = None,
    package_plugin_manifests: Callable[[str], list] | None = None,
) -> PluginInstallResult:
    """Install a local, git, wheel, or package plugin and approve its lock entry."""

    import importlib
    import shutil
    import subprocess
    import tempfile

    from relaydeck.plugin_lock import entry_from_manifest, load_lock, save_lock
    from relaydeck.plugin_manifest import load_manifest

    install_python_package = install_python_package or _install_python_package
    install_editable_python_package = (
        install_editable_python_package or _install_editable_python_package
    )
    package_plugin_manifests = package_plugin_manifests or _package_plugin_manifests

    tmp: tempfile.TemporaryDirectory | None = None
    installed_via = "local"
    source = Path(src)
    source_label = src
    git_url = ""
    git_ref = ""
    git_commit = ""
    if editable and not source.exists():
        raise PluginInstallError("--editable requires a local plugin directory.")
    if editable and source.exists() and source.is_dir() and _has_plugin_entry_points(source):
        source_resolved = str(source.resolve())
        _install_and_approve_package(
            source_resolved,
            config_home,
            install_editable_python_package,
            package_plugin_manifests,
            installed_via="editable-package",
        )
        importlib.invalidate_caches()
        manifests = package_plugin_manifests(source_resolved)
        return PluginInstallResult(
            names=[m.name for m in manifests],
            installed_via="editable-package",
            source=source_resolved,
        )
    if source.exists() and source.is_file() and source.suffix == ".whl":
        _install_and_approve_package(
            src, config_home, install_python_package, package_plugin_manifests
        )
        importlib.invalidate_caches()
        manifests = package_plugin_manifests(src)
        return PluginInstallResult(
            names=[m.name for m in manifests],
            installed_via="package",
            source=src,
        )
    if not source.exists():
        if _is_git_plugin_source(src):
            tmp = tempfile.TemporaryDirectory(prefix="relaydeck-plugin-install-")
            git_url, git_ref = _parse_git_plugin_source(src)
            if not git_ref:
                raise PluginInstallError(
                    "Git plugin installs must pin a tag, branch, or commit: "
                    "use git+https://.../plugin.git@<tag-or-commit>."
                )
            clone_cmd = ["git", "clone"]
            clone_cmd.extend([git_url, tmp.name])
            try:
                subprocess.run(clone_cmd, check=True)
                source = Path(tmp.name)
                installed_via = "git"
                if git_ref:
                    subprocess.run(["git", "-C", str(source), "checkout", git_ref], check=True)
                git_commit = _git_rev_parse(source)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise PluginInstallError(str(exc)) from exc
        else:
            if not _is_package_plugin_source(src):
                raise PluginInstallError(
                    "Unsupported plugin source. Use a local directory, git URL, "
                    "wheel file, or relaydeck-plugin-* package name."
                )
            _install_and_approve_package(
                src, config_home, install_python_package, package_plugin_manifests
            )
            importlib.invalidate_caches()
            manifests = package_plugin_manifests(src)
            return PluginInstallResult(
                names=[m.name for m in manifests],
                installed_via="package",
                source=src,
            )
    elif editable:
        installed_via = "editable"

    manifest_path = find_plugin_manifest_path(source)
    source = manifest_path.parent
    manifest = load_manifest(manifest_path)
    dest = config_home / "plugins" / manifest.name
    if dest.exists():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if editable:
        dest.symlink_to(source.resolve(), target_is_directory=True)
    else:
        shutil.copytree(source, dest, symlinks=True)
    if tmp is not None:
        tmp.cleanup()
    entries = load_lock(config_home)
    entries[manifest.name] = entry_from_manifest(
        manifest,
        source=source_label if installed_via == "git" else str(source.resolve()),
        scope="user",
        installed_via=installed_via,
        git_url=git_url,
        git_ref=git_ref,
        git_commit=git_commit,
    )
    save_lock(config_home, entries)
    return PluginInstallResult(
        names=[manifest.name],
        installed_via=installed_via,
        source=source_label if installed_via == "git" else str(source.resolve()),
        dest=str(dest),
    )


def approve_package_plugin(
    src: str,
    config_home: Path,
    *,
    package_plugin_manifests: Callable[[str], list] | None = None,
    installed_via: str = "package",
) -> list:
    from relaydeck.plugin_lock import entry_from_manifest, load_lock, save_lock

    package_plugin_manifests = package_plugin_manifests or _package_plugin_manifests
    manifests = package_plugin_manifests(src)
    if not manifests:
        raise PluginInstallError(
            f"No relaydeck.plugins entry point with plugin.toml found in {src!r}"
        )
    entries = load_lock(config_home)
    for manifest in manifests:
        entries[manifest.name] = entry_from_manifest(
            manifest,
            source=src,
            scope="user",
            installed_via=installed_via,
        )
    save_lock(config_home, entries)
    return manifests


def uninstall_plugin(
    name: str,
    config_home: Path,
    *,
    uninstall_python_package: Callable[[str], None] | None = None,
) -> tuple[bool, str | None]:
    import shutil

    from relaydeck.plugin_lock import load_lock, save_lock

    uninstall_python_package = uninstall_python_package or _uninstall_python_package
    dest = config_home / "plugins" / name
    had_dest = dest.exists()
    if had_dest:
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    entries = load_lock(config_home)
    entry = entries.pop(name, None)
    uninstalled_package: str | None = None
    if entry and entry.installed_via in ("package", "editable-package"):
        still_used = any(
            other.installed_via in ("package", "editable-package")
            and other.source == entry.source
            for other in entries.values()
        )
        if not still_used:
            uninstall_python_package(entry.source)
            uninstalled_package = entry.source
    save_lock(config_home, entries)
    return entry is not None or had_dest, uninstalled_package


def _install_and_approve_package(
    src: str,
    config_home: Path,
    install_python_package: Callable[[str], None],
    package_plugin_manifests: Callable[[str], list],
    *,
    installed_via: str = "package",
) -> None:
    import subprocess

    try:
        install_python_package(src)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PluginInstallError(str(exc)) from exc
    approve_package_plugin(
        src,
        config_home,
        package_plugin_manifests=package_plugin_manifests,
        installed_via=installed_via,
    )


def _has_plugin_entry_points(root: Path) -> bool:
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.exists():
        return False
    try:
        import tomllib

        pyproject = tomllib.loads(pyproject_path.read_text())
    except Exception:
        return False
    return bool(
        (pyproject.get("project") or {})
        .get("entry-points", {})
        .get("relaydeck.plugins")
    )


def _is_git_plugin_source(src: str) -> bool:
    if src.startswith(("git+", "ssh://", "git@")) or src.endswith(".git"):
        return True
    if src.startswith("https://"):
        return src.endswith(".git") or src.startswith("https://github.com/")
    return False


def _is_package_plugin_source(src: str) -> bool:
    if src.endswith(".whl"):
        return True
    name = _package_project_name(src)
    return name.startswith("relaydeck-plugin-")


def _package_project_name(src: str) -> str:
    import re

    value = src.strip()
    path = Path(value)
    if path.exists() and path.is_dir():
        try:
            import tomllib

            project = tomllib.loads((path / "pyproject.toml").read_text()).get("project") or {}
            name = str(project.get("name") or "").strip()
            if name:
                return name.replace("_", "-").lower()
        except Exception:
            pass
    if value.endswith(".whl"):
        return Path(value).name.split("-", 1)[0].replace("_", "-").lower()
    value = value.split("[", 1)[0]
    value = re.split(r"[<>=!~]", value, maxsplit=1)[0]
    return value.strip().replace("_", "-").lower()


def _install_python_package(src: str) -> None:
    import shutil
    import subprocess
    import sys

    uv = shutil.which("uv")
    if uv:
        project = _package_project_name(src)
        cmd = [uv, "pip", "install", "--python", sys.executable]
        if project:
            cmd.extend(["--reinstall-package", project])
        cmd.append(src)
        subprocess.run(cmd, check=True)
        return
    subprocess.run([sys.executable, "-m", "pip", "install", src], check=True)


def _install_editable_python_package(src: str) -> None:
    import shutil
    import subprocess
    import sys

    uv = shutil.which("uv")
    if uv:
        subprocess.run(
            [uv, "pip", "install", "--python", sys.executable, "-e", src],
            check=True,
        )
        return
    subprocess.run([sys.executable, "-m", "pip", "install", "-e", src], check=True)


def _uninstall_python_package(src: str) -> None:
    import shutil
    import subprocess
    import sys

    project = _package_project_name(src)
    if not project:
        return
    uv = shutil.which("uv")
    if uv:
        subprocess.run(
            [uv, "pip", "uninstall", "--python", sys.executable, "-y", project],
            check=True,
        )
        return
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", project], check=True)


def _package_plugin_manifests(src: str):
    import importlib.metadata
    import importlib.util

    from relaydeck.plugin_manifest import ManifestError, find_manifest

    source = Path(src)
    if source.exists() and source.is_dir():
        manifests = _local_package_plugin_manifests(source)
        if manifests:
            return manifests

    project = _package_project_name(src)
    try:
        dist = importlib.metadata.distribution(project)
        entry_points = [
            ep for ep in dist.entry_points
            if getattr(ep, "group", "") == "relaydeck.plugins"
        ]
    except importlib.metadata.PackageNotFoundError:
        eps = importlib.metadata.entry_points()
        selected = eps.select(group="relaydeck.plugins") if hasattr(eps, "select") else eps.get(
            "relaydeck.plugins", ()
        )
        entry_points = list(selected)

    manifests = []
    seen: set[str] = set()
    for ep in entry_points:
        module_name = (getattr(ep, "value", "") or "").split(":", 1)[0].strip()
        if not module_name:
            continue
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, AttributeError, ValueError):
            continue
        if spec is None or spec.origin is None:
            continue
        try:
            manifest = find_manifest(Path(spec.origin).parent)
        except ManifestError:
            continue
        if manifest is None or manifest.name in seen:
            continue
        manifests.append(manifest)
        seen.add(manifest.name)
    return manifests


def _local_package_plugin_manifests(root: Path):
    from relaydeck.plugin_manifest import ManifestError, find_manifest

    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.exists():
        return []
    try:
        import tomllib

        pyproject = tomllib.loads(pyproject_path.read_text())
    except Exception:
        return []
    entry_points = (
        (pyproject.get("project") or {})
        .get("entry-points", {})
        .get("relaydeck.plugins", {})
    )
    if not isinstance(entry_points, dict):
        return []

    manifests = []
    seen: set[str] = set()
    for value in entry_points.values():
        module_name = str(value or "").split(":", 1)[0].strip()
        if not module_name:
            continue
        module_parts = module_name.split(".")
        candidates: list[Path] = []
        for base in (root, root / "src"):
            module_path = base.joinpath(*module_parts)
            candidates.extend([
                module_path.with_suffix(".py"),
                module_path / "__init__.py",
            ])
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                manifest = find_manifest(candidate.parent)
            except ManifestError:
                continue
            if manifest is None or manifest.name in seen:
                continue
            manifests.append(manifest)
            seen.add(manifest.name)
            break
    return manifests


def _parse_git_plugin_source(src: str) -> tuple[str, str]:
    raw = src[4:] if src.startswith("git+") else src
    if ".git@" in raw:
        url, ref = raw.rsplit(".git@", 1)
        return f"{url}.git", ref
    if raw.startswith(("http://", "https://")) and "@" in raw:
        url, ref = raw.rsplit("@", 1)
        return url, ref
    return raw, ""


def _git_rev_parse(path: Path) -> str:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return proc.stdout.strip()
