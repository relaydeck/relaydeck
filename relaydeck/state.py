"""
Per-machine runtime state outside of agent specs and plugin settings.

What lives here:
  - The user's "current workspace" — what a CLI command without an
    explicit `--workspace` flag should operate on.
  - The daemon's listening URL — written by `relaydeck serve` on startup
    so CLI commands in separate processes can find the daemon and
    talk over HTTP.

Both follow a kubectl-style resolution order so a shell session can
override the persistent file via env, and a single command can
override env via a CLI flag.

Mirrors the shape of `relaydeck/plugin_disabled.py` — small yaml at
`~/.relaydeck/state.yaml`, mode 0600, single module lock. Settings
that an agent or plugin should react to belong in
`relaydeck/plugin_settings.py` instead.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DAEMON_URL = "http://127.0.0.1:8765"

_LOCK = threading.RLock()


def _store_path() -> Path:
    return Path.home() / ".relaydeck" / "state.yaml"


def _load() -> dict[str, Any]:
    p = _store_path()
    if not p.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(p.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("state.yaml: load failed: %s", exc)
        return {}


def _save(data: dict[str, Any]) -> None:
    import yaml
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=True, default_flow_style=False))
    try:
        p.chmod(0o600)
    except OSError:
        pass


# ── Current workspace ───────────────────────────────────────────────


def get_current_workspace(cwd: Path | None = None) -> str | None:
    """Resolve the user's active workspace.

    Order, highest first:

      1. `RELAYDECK_WORKSPACE` env var — explicit per-shell override.
      2. **cwd-based detection** — if the current directory (or any
         ancestor) IS a registered workspace's `path`, that
         workspace wins. Mirrors how `git` finds `.git`: a `cd`
         into the project IS the gesture that picks it. Nested
         workspaces resolve to the deepest match (the more
         specific one).
      3. `state.yaml.current_workspace` — durable default written
         by `relaydeck workspace set <name>`. Applies when cwd doesn't
         match any registered workspace (e.g. a fresh shell in
         `/tmp`).
      4. First workspace in the registry — last-resort fallback so
         single-workspace setups still "just work" without any
         configuration.

    `cwd` defaults to `Path.cwd()`; pass it explicitly for tests
    or for an in-process caller that wants to ask "what workspace
    would apply for this path" without changing the real cwd.
    """
    env = os.environ.get("RELAYDECK_WORKSPACE")
    if env:
        s = env.strip()
        if s:
            return s

    # cwd lookup. Walks up from `cwd` and matches against the
    # registered workspaces' paths. Deepest match wins.
    inferred = _resolve_workspace_from_cwd(cwd or Path.cwd())
    if inferred:
        return inferred

    with _LOCK:
        v = _load().get("current_workspace")
    if isinstance(v, str) and v.strip():
        return v.strip()

    # Last-resort fallback: first workspace in the registry. Lazy
    # import so the CLI startup doesn't pay for config parsing when
    # the env var or state.yaml already answers.
    try:
        from relaydeck.config import load_workspace_registry
        registry = load_workspace_registry()
        if registry:
            return registry[0].name
    except Exception as exc:
        logger.debug("get_current_workspace fallback failed: %s", exc)

    return None


def resolve_workspace_source(cwd: Path | None = None) -> tuple[str | None, str]:
    """Like `get_current_workspace` but also returns *how* the
    resolution happened, so commands like `relaydeck workspace info` and
    `relaydeck doctor` can explain themselves.

    Returns `(name_or_None, source)` where `source` is one of:
      `"env"` | `"cwd"` | `"state"` | `"registry-default"` | `"unset"`.

    A cwd answer beats a state.yaml answer the same way it does in
    `get_current_workspace` — we don't reach into multiple sources;
    we report the same one we'd use.
    """
    env = os.environ.get("RELAYDECK_WORKSPACE")
    if env and env.strip():
        return env.strip(), "env"

    inferred = _resolve_workspace_from_cwd(cwd or Path.cwd())
    if inferred:
        return inferred, "cwd"

    with _LOCK:
        v = _load().get("current_workspace")
    if isinstance(v, str) and v.strip():
        return v.strip(), "state"

    try:
        from relaydeck.config import load_workspace_registry
        registry = load_workspace_registry()
        if registry:
            return registry[0].name, "registry-default"
    except Exception as exc:
        logger.debug("resolve_workspace_source fallback failed: %s", exc)

    return None, "unset"


def _resolve_workspace_from_cwd(cwd: Path) -> str | None:
    """Find the registered workspace (if any) whose `path` is `cwd`
    itself or an ancestor of `cwd`. On multiple matches (nested
    workspaces — e.g. `~/code/monorepo` plus
    `~/code/monorepo/frontend`), the deepest path wins so a
    `cd ~/code/monorepo/frontend/src` resolves to `frontend`, not
    the outer monorepo.

    Returns None on any error (missing registry, unresolvable path,
    etc.) — the caller falls back to the next resolution step.
    """
    try:
        from relaydeck.config import load_workspace_registry
        registry = load_workspace_registry()
    except Exception as exc:
        logger.debug("cwd resolution: registry load failed: %s", exc)
        return None
    if not registry:
        return None

    try:
        cwd = cwd.resolve()
    except (OSError, RuntimeError):
        # `cwd.resolve()` can fail if a path component vanished
        # under us — fall through to the next resolution step.
        return None

    best: tuple[int, str] | None = None  # (path_depth, workspace_name)
    for w in registry:
        try:
            w_path = Path(w.path).resolve()
        except (OSError, RuntimeError):
            continue
        if cwd == w_path or w_path in cwd.parents:
            depth = len(w_path.parts)
            if best is None or depth > best[0]:
                best = (depth, w.name)
    return best[1] if best else None


def set_current_workspace(name: str) -> None:
    """Persist the active workspace to state.yaml. Pass an empty string
    to clear (next read will fall back to env / registry)."""
    with _LOCK:
        data = _load()
        if name:
            data["current_workspace"] = name
        else:
            data.pop("current_workspace", None)
        _save(data)


# ── Daemon URL ──────────────────────────────────────────────────────


def get_daemon_url() -> str:
    """Resolve the daemon's HTTP base URL.

    Order, highest first:
      1. RELAYDECK_DAEMON_URL env var
      2. state.yaml `daemon_url` (written by `relaydeck serve` on startup)
      3. DEFAULT_DAEMON_URL constant
    """
    env = os.environ.get("RELAYDECK_DAEMON_URL")
    if env:
        return env.strip() or DEFAULT_DAEMON_URL

    with _LOCK:
        v = _load().get("daemon_url")
    if isinstance(v, str) and v.strip():
        return v.strip()

    return DEFAULT_DAEMON_URL


def set_daemon_url(url: str) -> None:
    """`relaydeck serve` calls this once it knows what host+port it's bound
    to so CLI commands in other processes can locate it."""
    with _LOCK:
        data = _load()
        if url:
            data["daemon_url"] = url
        else:
            data.pop("daemon_url", None)
        _save(data)


# ── Daemon bind host (persisted preference) ─────────────────────────


def get_daemon_bind_host() -> str | None:
    """The bind address `relaydeck daemon start` should use when no `--host`
    flag is passed. Operators who want the dashboard reachable from
    other devices (LAN, tailscale, etc.) set this once instead of
    remembering `--host 0.0.0.0` every restart.

    Returns None when no preference has been persisted; the caller
    falls back to its own default (`127.0.0.1`)."""
    with _LOCK:
        v = _load().get("daemon_bind_host")
    return v.strip() if isinstance(v, str) and v.strip() else None


def set_daemon_bind_host(host: str | None) -> None:
    """Persist (or clear) the bind-host preference. Called by
    `relaydeck daemon start --host X` so subsequent restarts inherit the
    choice. Pass None or "" to clear."""
    with _LOCK:
        data = _load()
        if host and host.strip():
            data["daemon_bind_host"] = host.strip()
        else:
            data.pop("daemon_bind_host", None)
        _save(data)


# ── TLS trust ───────────────────────────────────────────────────────


def get_daemon_ca() -> str | None:
    """If `relaydeck serve` is running over HTTPS with a self-signed cert,
    return the cert path so sibling CLIs can pin verification against
    it. Returns None if the daemon is plain HTTP or the operator
    provided a publicly-trusted cert (use the system trust store)."""
    env = os.environ.get("RELAYDECK_DAEMON_CA")
    if env and env.strip():
        return env.strip()
    with _LOCK:
        v = _load().get("daemon_ca")
    return v if isinstance(v, str) and v.strip() else None


def set_daemon_ca(path: str | None) -> None:
    """Record (or clear) the cert path the daemon CLIs should verify
    against. Called by `relaydeck serve --tls-self-signed`."""
    with _LOCK:
        data = _load()
        if path:
            data["daemon_ca"] = path
        else:
            data.pop("daemon_ca", None)
        _save(data)
