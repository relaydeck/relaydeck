#!/usr/bin/env bash
# relaydeck installer
#
# Intended usage (once the script is hosted):
#
#   curl -fsSL https://raw.githubusercontent.com/relaydeck/relaydeck/main/scripts/install.sh | sh
#
# The same script is hosted on the website:
#   curl -fsSL https://relaydeck.ai/install.sh | sh
#
# What this does:
#
#   1. Verifies a usable shell + sane environment (no piping into
#      Bourne `sh` with bashisms).
#   2. Ensures `uv` is present — if not, installs it from astral.sh
#      (the same one-liner Astral itself recommends).
#   3. Installs relaydeck as a `uv tool`, isolated in its own venv so
#      it doesn't pollute the user's site-packages.
#   4. Prints the next steps (`relaydeck serve`, `relaydeck doctor`).
#
# Idempotent — running this twice upgrades relaydeck in place rather
# than failing on "already installed". Quiet on success, loud on
# failure: every error path prints what it tried and why it
# couldn't, so an operator can fix the underlying issue without
# running the script under bash -x.
#
# Security notes:
#
#   - Source: by default this installs the published PyPI release
#     (`relaydeck`), falling back to the GitHub main branch if PyPI isn't
#     reachable yet. Override with RELAYDECK_SOURCE to pin a specific
#     version, a git ref, or a local checkout. Operators uneasy about
#     curl-pipe-sh should `curl -fsSL … | less` first.
#   - We never `eval` the response from any network call.
#   - We don't write anywhere outside the user's `$HOME` (uv
#     respects XDG dirs).

set -euo pipefail

# ── Colors / formatting (no-op if stdout isn't a TTY) ───────────────

if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"
    DIM="$(printf '\033[2m')"
    RED="$(printf '\033[31m')"
    GREEN="$(printf '\033[32m')"
    YELLOW="$(printf '\033[33m')"
    CYAN="$(printf '\033[36m')"
    RESET="$(printf '\033[0m')"
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi

say() { printf '%s\n' "$*"; }
info() { say "${CYAN}${BOLD}::${RESET} $*"; }
ok() { say "${GREEN}✓${RESET} $*"; }
warn() { say "${YELLOW}!${RESET} $*" >&2; }
die() { say "${RED}✗${RESET} $*" >&2; exit 1; }

# ── Pre-flight ─────────────────────────────────────────────────────

if [ -z "${HOME:-}" ]; then
    die "HOME is not set — refusing to install without a home directory."
fi

case "$(uname -s)" in
    Linux|Darwin) ;;
    *) die "Unsupported OS: $(uname -s). relaydeck currently targets macOS and Linux." ;;
esac

# Python is required by uv; uv will install its own but the user
# still needs a basic system Python for the install bootstrap.
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    die "Neither curl nor wget found — install one and re-run."
fi

# ── uv: install or upgrade ────────────────────────────────────────

install_uv() {
    info "installing uv (the Python tool/manager relaydeck uses)…"
    # Astral's recommended one-liner.
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    else
        wget -qO- https://astral.sh/uv/install.sh | sh
    fi
    # uv installs itself to ~/.local/bin; make sure that's on the
    # PATH for the rest of this script's invocation.
    if [ -d "$HOME/.local/bin" ] && ! echo ":$PATH:" | grep -q ":$HOME/.local/bin:"; then
        PATH="$HOME/.local/bin:$PATH"
        export PATH
        warn "Added $HOME/.local/bin to PATH for this session. Add it to your shell rc to make it permanent."
    fi
}

if ! command -v uv >/dev/null 2>&1; then
    install_uv
else
    info "uv already installed ($(uv --version)) — skipping uv bootstrap."
fi

if ! command -v uv >/dev/null 2>&1; then
    die "uv was installed but is not on PATH. Add ~/.local/bin to PATH and re-run."
fi

# ── relaydeck: install / upgrade ────────────────────────────────────────

# The default installs the published PyPI release. Override with
# RELAYDECK_SOURCE to pin a version, a git ref, or a local checkout:
#
#   RELAYDECK_SOURCE='relaydeck==0.1.0' ./install.sh             # pin a PyPI version
#   RELAYDECK_SOURCE='/path/to/local/checkout' ./install.sh      # local source tree
#   RELAYDECK_SOURCE='relaydeck @ git+https://github.com/relaydeck/relaydeck.git@v0.2.0' ./install.sh

RELAYDECK_GIT_SPEC='relaydeck @ git+https://github.com/relaydeck/relaydeck.git'
RELAYDECK_SPEC="${RELAYDECK_SOURCE:-relaydeck}"

_uv_tool_install() {
    if [ "$1" = "--reinstall" ]; then
        uv tool install --reinstall "$2"
    else
        uv tool install "$2"
    fi
}

# Default is PyPI; if the package isn't published yet (or PyPI is briefly
# unreachable) and the operator didn't pin RELAYDECK_SOURCE, fall back to git
# so curl|sh adopters aren't hard-blocked before the first release lands.
_install_relaydeck() {
    local mode="$1"   # empty or --reinstall
    if _uv_tool_install "$mode" "$RELAYDECK_SPEC"; then
        return 0
    fi
    if [ -n "${RELAYDECK_SOURCE:-}" ] || [ "$RELAYDECK_SPEC" != "relaydeck" ]; then
        return 1
    fi
    warn "PyPI install failed — falling back to the GitHub main branch."
    RELAYDECK_SPEC="$RELAYDECK_GIT_SPEC"
    _uv_tool_install "$mode" "$RELAYDECK_SPEC"
}

if uv tool list 2>/dev/null | grep -q '^relaydeck\b'; then
    info "relaydeck already installed — reinstalling from the current source…"
    # --reinstall (not `upgrade`) so re-running honors RELAYDECK_SOURCE and
    # re-pins the install source — e.g. migrating an old git install to PyPI.
    _install_relaydeck --reinstall || die "uv tool reinstall failed."
    ok "relaydeck reinstalled."
else
    info "installing relaydeck via uv tool…"
    _install_relaydeck "" || die "uv tool install failed."
    ok "relaydeck installed."
fi

if ! command -v relaydeck >/dev/null 2>&1; then
    warn "relaydeck installed but not on PATH. uv tool puts shims in \$HOME/.local/bin — make sure that's exported."
fi

# ── Next steps ─────────────────────────────────────────────────────

cat <<EOF

${BOLD}Done.${RESET}

Next steps:

  ${CYAN}relaydeck serve${RESET}            # boot the daemon (writes ~/.relaydeck)
  ${CYAN}relaydeck doctor${RESET}           # check your setup
  ${CYAN}relaydeck workspace add .${RESET}  # register the current dir as a workspace

For the TUI:                ${CYAN}relaydeck view${RESET}
For status of a single agent: ${CYAN}relaydeck agent status${RESET}

Later, to upgrade:          ${CYAN}relaydeck update${RESET}  (or the dashboard's Update banner)

Docs: ${DIM}https://relaydeck.ai${RESET}
EOF
