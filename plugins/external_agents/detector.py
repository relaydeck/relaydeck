"""Detect whether a path is a Hermes or OpenClaw runtime.

Detection is **filesystem-first and side-effect-free**: it reads a few marker
files and directory names and scores the evidence. The only host probe is an
injectable `which(<cli>)` check ("is the runtime's CLI installed?") which is a
soft signal — it never executes the CLI. We deliberately do NOT run
`hermes`/`openclaw` here (that's `probes.py`, and only on demand) so `detect`
stays fast and safe to call while scanning many directories.

Returns a `Detection` (scored evidence). The caller decides whether the
confidence is high enough to register or surface as a candidate.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .models import (
    HERMES,
    OPENCLAW,
    T_GATEWAY_WS,
    T_MCP,
    UNKNOWN,
    Detection,
)

# Confidence weights per signal. Tuned so a single strong marker (a
# pyproject naming hermes-agent, an openclaw.mjs, or a `.hermes`/`.openclaw`
# home) clears the 0.5 "candidate" bar, and two independent markers clear
# the 0.9 "confident" bar.
_W_HOME_NAME = 0.6        # dir literally named .hermes / .openclaw
_W_STRONG_REPO = 0.6      # pyproject/package.json/openclaw.mjs naming the project
_W_CONFIG_FILE = 0.35     # config.yaml in a plausible home
_W_CONTEXT_FILE = 0.15    # .hermes.md / SOUL.md etc.
_W_DIR_HINT = 0.12        # a hermes/openclaw-looking subdir
_W_CLI_ON_PATH = 0.25     # the runtime's CLI is installed on this host

_CONFIDENCE_CAP = 0.99
CANDIDATE_THRESHOLD = 0.5


def _which(name: str) -> str | None:
    """Indirection point so tests can monkeypatch CLI presence without
    depending on whether hermes/openclaw is actually installed."""
    return shutil.which(name)


def _read_head(path: Path, limit: int = 8192) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _score_hermes(path: Path) -> tuple[float, list[str], str | None]:
    signals: list[str] = []
    score = 0.0
    config_home: str | None = None

    if path.name == ".hermes":
        score += _W_HOME_NAME
        signals.append("dir named .hermes")
        config_home = str(path)

    cfg = path / "config.yaml"
    if cfg.exists():
        score += _W_CONFIG_FILE
        signals.append("config.yaml present")
        config_home = config_home or str(path)

    pyproject = path / "pyproject.toml"
    if pyproject.exists():
        head = _read_head(pyproject).lower()
        if "hermes-agent" in head or "hermes_agent" in head or "hermes agent" in head:
            score += _W_STRONG_REPO
            signals.append("pyproject names hermes-agent")
        elif "hermes" in head:
            score += _W_DIR_HINT
            signals.append("pyproject mentions hermes")

    for ctx in (".hermes.md", "SOUL.md"):
        if (path / ctx).exists():
            score += _W_CONTEXT_FILE
            signals.append(f"{ctx} present")

    for d in ("hermes_cli", "hermes_agent"):
        if (path / d).is_dir():
            score += _W_DIR_HINT
            signals.append(f"{d}/ dir")

    if _which("hermes"):
        score += _W_CLI_ON_PATH
        signals.append("hermes CLI on PATH")

    # A conventional home alongside a repo checkout.
    if config_home is None:
        home = Path.home() / ".hermes"
        if home.exists():
            config_home = str(home)

    return score, signals, config_home


def _score_openclaw(path: Path) -> tuple[float, list[str], str | None]:
    signals: list[str] = []
    score = 0.0
    config_home: str | None = None

    if path.name == ".openclaw":
        score += _W_HOME_NAME
        signals.append("dir named .openclaw")
        config_home = str(path)

    if (path / "config.yaml").exists() or (path / "config.json").exists():
        score += _W_CONFIG_FILE
        signals.append("config file present")
        config_home = config_home or str(path)

    if (path / "openclaw.mjs").exists():
        score += _W_STRONG_REPO
        signals.append("openclaw.mjs present")

    pkg = path / "package.json"
    if pkg.exists():
        head = _read_head(pkg).lower()
        if "openclaw" in head:
            score += _W_STRONG_REPO
            signals.append("package.json names openclaw")

    if (path / "pnpm-workspace.yaml").exists():
        score += _W_DIR_HINT
        signals.append("pnpm-workspace.yaml")

    for d in ("extensions", "apps"):
        if (path / d).is_dir():
            score += _W_DIR_HINT * 0.5
            signals.append(f"{d}/ dir")

    if _which("openclaw"):
        score += _W_CLI_ON_PATH
        signals.append("openclaw CLI on PATH")

    if config_home is None:
        home = Path.home() / ".openclaw"
        if home.exists():
            config_home = str(home)

    return score, signals, config_home


def detect(path: str | Path) -> Detection:
    """Score `path` as a hermes/openclaw runtime. Filesystem-only."""
    p = Path(path).expanduser()
    root = str(p)
    if not p.exists():
        return Detection(
            kind=UNKNOWN, confidence=0.0, root=root,
            warnings=[f"path does not exist: {root}"],
        )
    if not p.is_dir():
        return Detection(
            kind=UNKNOWN, confidence=0.0, root=root,
            warnings=[f"not a directory: {root}"],
        )

    h_score, h_signals, h_home = _score_hermes(p)
    o_score, o_signals, o_home = _score_openclaw(p)

    if h_score == 0.0 and o_score == 0.0:
        return Detection(
            kind=UNKNOWN, confidence=0.0, root=root,
            warnings=["no hermes/openclaw markers found"],
        )

    if h_score >= o_score:
        kind, score, signals, home, transport = (
            HERMES, h_score, h_signals, h_home, T_MCP,
        )
    else:
        kind, score, signals, home, transport = (
            OPENCLAW, o_score, o_signals, o_home, T_GATEWAY_WS,
        )

    return Detection(
        kind=kind,
        confidence=min(score, _CONFIDENCE_CAP),
        signals=signals,
        recommended_transport=transport,
        root=root,
        config_home=home,
    )


def scan_candidates(extra_roots: list[str] | None = None) -> list[Detection]:
    """Auto-detect external runtimes on this machine.

    Scans the conventional config homes (`~/.hermes`, `~/.openclaw`) plus any
    `extra_roots` (e.g. registered relaydeck workspace paths) and returns
    detections at or above the candidate threshold. The caller filters out
    anything already registered before surfacing "detected — add?" affordances.
    """
    seen: set[str] = set()
    out: list[Detection] = []
    roots: list[Path] = [Path.home() / ".hermes", Path.home() / ".openclaw"]
    for r in extra_roots or []:
        try:
            roots.append(Path(r).expanduser())
        except (OSError, ValueError):
            continue
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if not root.exists() or not root.is_dir():
            continue
        det = detect(root)
        if det.matched and det.confidence >= CANDIDATE_THRESHOLD:
            out.append(det)
    out.sort(key=lambda d: d.confidence, reverse=True)
    return out
