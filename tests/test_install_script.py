"""
Sanity tests for `scripts/install.sh`.

The install script is the front door for `curl | sh` adopters —
a broken script is far worse than no script, because it
discourages users and stamps "relaydeck is unreliable" in their muscle
memory. We don't run the script (it would actually install
things) but we DO pin its structural properties so the file
can't drift into a broken state under refactors.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"


def test_install_script_exists():
    """The install script is committed at a stable, advertised
    path. If we move it, every README + docs link breaks."""
    assert SCRIPT.exists(), f"install script missing at {SCRIPT}"


def test_install_script_is_executable():
    """`curl … | sh` doesn't need this, but operators who
    download and inspect (the recommended pattern) want to
    `./install.sh` directly."""
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "install.sh must be executable"


def test_install_script_passes_bash_syntax_check():
    """The cheapest possible smoke test: `bash -n` parses without
    actually running. Catches typos and unbalanced quoting before
    they hit a user's terminal."""
    if shutil.which("bash") is None:
        # Test environment without bash is unusual but possible.
        return
    res = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        timeout=10,
    )
    assert res.returncode == 0, (
        f"install.sh failed bash -n:\nstdout: {res.stdout!r}\n"
        f"stderr: {res.stderr!r}"
    )


def test_install_script_uses_set_euo_pipefail():
    """Strict-mode bash — fail fast on errors, undefined vars, and
    pipeline failures. The whole reason this script exists is to
    leave the user's machine in a known state; silently swallowing
    a partial failure is the worst outcome."""
    body = SCRIPT.read_text()
    assert re.search(r"^\s*set -euo pipefail\b", body, re.MULTILINE), (
        "install.sh must use `set -euo pipefail` for strict-mode bash"
    )


def test_install_script_supports_relaydeck_source_override():
    """RELAYDECK_SOURCE is the documented escape hatch for installing
    from a git ref or local checkout instead of pypi. If we drop
    it, CI users and contributors lose the ability to install
    pre-release builds without modifying the script."""
    body = SCRIPT.read_text()
    assert "RELAYDECK_SOURCE" in body, (
        "install.sh must honor RELAYDECK_SOURCE for git/local installs"
    )


def test_install_script_checks_macos_or_linux():
    """We don't (yet) support Windows. The script must check and
    bail out cleanly rather than running through the uv install
    and then half-breaking on a missing posix tool."""
    body = SCRIPT.read_text()
    assert "Linux" in body and "Darwin" in body, (
        "install.sh must explicitly check for Linux/Darwin"
    )


def test_install_script_uses_uv_tool_install():
    """relaydeck installs via `uv tool install`, NOT `pip install`,
    because uv tool isolates the install in its own venv (no
    site-packages collision with whatever the user has). Pin so
    a refactor can't quietly regress to a global pip install."""
    body = SCRIPT.read_text()
    assert "uv tool install" in body


def test_install_script_falls_back_to_git_when_pypi_unset():
    """Before PyPI exists (or during a brief outage), the default install
    must not hard-fail — it falls back to the GitHub main branch unless
    RELAYDECK_SOURCE is pinned."""
    body = SCRIPT.read_text()
    assert "RELAYDECK_GIT_SPEC" in body
    assert "PyPI install failed" in body
    assert "git+https://github.com/relaydeck/relaydeck.git" in body
