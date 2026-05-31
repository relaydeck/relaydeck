"""Tests for scripts/bump_version.py — version bump + CHANGELOG roll."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bump_version.py"


def _tree(tmp_path: Path) -> Path:
    """Minimal project tree so bump_version.py resolves ROOT = tmp_path."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "bump_version.py")
    (tmp_path / "relaydeck").mkdir()
    (tmp_path / "relaydeck" / "__init__.py").write_text(
        'def _resolve_version() -> str:\n    return "0.1.0"\n'
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n### Added\n\n- something\n\n## [0.0.1] - 2020-01-01\n"
    )
    return tmp_path


def _run(root: Path, level: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "bump_version.py"), level],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_bump_version_patch(tmp_path):
    root = _tree(tmp_path)
    res = _run(root, "patch")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "0.1.1"
    assert 'version = "0.1.1"' in (root / "pyproject.toml").read_text()
    assert 'return "0.1.1"' in (root / "relaydeck" / "__init__.py").read_text()
    cl = (root / "CHANGELOG.md").read_text()
    assert "## [Unreleased]" in cl
    assert "## [0.1.1] -" in cl
    assert cl.index("## [Unreleased]") < cl.index("## [0.1.1]")
    assert "- something" in cl


def test_bump_version_explicit(tmp_path):
    root = _tree(tmp_path)
    res = _run(root, "0.2.5")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "0.2.5"
    assert 'version = "0.2.5"' in (root / "pyproject.toml").read_text()
