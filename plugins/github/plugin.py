"""
Bundled GitHub integration.

Workspace-scoped plugin that polls `gh api` for repository events and
routes them through a per-workspace rules file (`github.yaml`) to
agents, scripts, or arbitrary bus events.

## How it fits

  - On `workspace.added` / `workspace.updated`, the plugin checks for
    `github.yaml` with a valid `repo:`. If present, it spawns a
    `GithubPoller` worker via `host.workers.spawn(...)`. No agent.toml
    opt-in — configure from the GitHub lens or write the yaml directly.

  - On `workspace.removed` or `system.shutdown`, all pollers stop.

  - Each poller is a single background loop visible in
    `relaydeck workers list` as `github:<workspace>` — operators can see
    last tick time, last error, and recent log lines via the workers
    surface, same as every other infrastructure plugin.

## Subcommands

    relaydeck github status              # all pollers + cursors + rule counts
    relaydeck github rules list          # rule names for the active workspace
    relaydeck github rules show NAME     # full rule YAML
    relaydeck github rules test FILE     # dry-run an event JSON through rules
    relaydeck github sync                # one-shot poll for the active workspace
    relaydeck github auth check          # gh auth status

The `sync` and `auth check` commands shell out to `gh` — they're
deliberately thin wrappers so they share gh's auth + rate-limit
handling rather than duplicating it.

## Config

`~/.relaydeck/workspaces/<ws>/github.yaml`:

    repo: relaydeck/relaydeck
    poll_interval_s: 30
    rules:
      - name: pr-review
        when: { event: PullRequestEvent, action: opened }
        do:
          - agent.message:
              to: reviewer
              body: "Review PR #{{ pull_request.number }}: {{ pull_request.title }}"
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path

from relaydeck.plugin import Event, PluginContext, PluginEventBus
from relaydeck.sdk import Plugin, PluginHost

from .poller import GithubPoller, cursor_path, load_cursor
from .rules import RulesConfig, evaluate, load_config

PLUGIN_NAME = "github"
logger = logging.getLogger(__name__)


# ── Workspace plugin-list reader ───────────────────────────────────


def _workspace_plugins(config_home: Path, workspace: str) -> list[str]:
    """Return the `plugins` list from a workspace's agent.toml."""
    toml_path = config_home / "workspaces" / workspace / "agent.toml"
    if not toml_path.exists():
        return []
    try:
        from relaydeck.config import _load_yaml_or_toml
        data = _load_yaml_or_toml(toml_path)
    except Exception:
        return []
    plugins = data.get("workspace", {}).get("plugins", [])
    return [str(p) for p in plugins] if isinstance(plugins, list) else []


def _workspace_github_configured(config_home: Path, workspace: str) -> bool:
    """True when github.yaml exists and parses to a config with a repo.
    No agent.toml opt-in — configure from the GitHub lens or drop a yaml."""
    cfg_path = config_home / "workspaces" / workspace / "github.yaml"
    if not cfg_path.is_file():
        return False
    try:
        config = load_config(cfg_path)
    except ValueError:
        return False
    return config is not None and bool(config.repo)


def _workspace_has_github(config_home: Path, workspace: str) -> bool:
    """Legacy alias — github is active when configured, not agent.toml."""
    return _workspace_github_configured(config_home, workspace)


def _registered_workspaces(config_home: Path) -> list[dict[str, str]]:
    """Read top-level `config.toml` for the registered workspaces list."""
    config_path = config_home / "config.toml"
    if not config_path.exists():
        return []
    try:
        from relaydeck.config import _load_yaml_or_toml
        data = _load_yaml_or_toml(config_path)
    except Exception:
        return []
    out: list[dict[str, str]] = []
    for block in data.get("workspace", []):
        if not isinstance(block, dict):
            continue
        name = block.get("name")
        path = block.get("path")
        if name and path:
            out.append({
                "name": str(name),
                "path": str(Path(path).expanduser().resolve()),
            })
    return out


def _post_daemon(path: str, payload: dict) -> tuple[bool, dict | str]:
    """POST `payload` to the running daemon. Spawn affects live state
    (the orchestrator), so the CLI can't do it in-process — it has to go
    through the daemon, like `relaydeck workspace message`. Returns
    (ok, response_or_error)."""
    import json
    import urllib.error
    import urllib.request

    from relaydeck.auth import read_token
    from relaydeck.state import get_daemon_ca, get_daemon_url

    base = get_daemon_url().rstrip("/")
    # Honor the pinned daemon CA (set by `relaydeck serve --tls-self-signed`)
    # so spawn works against an HTTPS daemon, same as the core CLI.
    ctx = None
    if base.startswith("https://"):
        import ssl
        ca = get_daemon_ca()
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()

    headers = {"Content-Type": "application/json"}
    tok = read_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return True, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ── Plugin ──────────────────────────────────────────────────────────


class GithubPlugin(Plugin):
    # Daemon-wide, configured per-workspace via github.yaml — NOT a
    # per-workspace agent.toml opt-in. The plugin watches every
    # registered workspace's github.yaml (see _on_workspace_file_changed)
    # and starts/stops pollers based on file presence + a valid `repo:`.
    # `workspace_scoped = False` keeps github out of the per-workspace
    # plugin toggles UI (addws-modal + Workspaces lens) where the toggle
    # would be a misleading no-op. Same shape as telegram.
    workspace_scoped = False
    description = (
        "Bundled GitHub integration: poll `gh` for events, route through "
        "rules to agents, scripts, and bus. Configured per-workspace via "
        "workspaces/<ws>/github.yaml (the GitHub lens writes it)."
    )

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self.config_home = host.config_home
        self._pollers: dict[str, GithubPoller] = {}
        host.events.subscribe("workspace.added", self._on_workspace_added)
        host.events.subscribe("workspace.updated", self._on_workspace_updated)
        host.events.subscribe("workspace.removed", self._on_workspace_removed)
        host.events.subscribe("workspace.file.changed", self._on_workspace_file_changed)
        host.events.subscribe("system.shutdown", self._on_shutdown)
        for ws in _registered_workspaces(host.config_home):
            self._ensure_workspace(ws["name"], ws["path"])
        self._register_cli()
        self._register_api()

    def on_unload(self) -> None:
        for name in list(self._pollers):
            self._stop_workspace(name)

    # ── Per-workspace lifecycle ────────────────────────────────────

    def _ensure_workspace(self, name: str, path: str) -> None:
        if not _workspace_github_configured(self.config_home, name):
            self._stop_workspace(name)
            return
        # Reconcile: stop any stale poller for this workspace before
        # starting fresh. workspace.updated may fire with no real
        # change, but a stop+start is cheap and guarantees we pick up
        # config edits.
        if name in self._pollers:
            return
        gh_binary = self._gh_binary()
        default_interval = self._default_interval_s()
        poller = GithubPoller(
            workspace=name,
            config_home=self.config_home,
            workspace_path=Path(path),
            bus=self.host.events,
            send_message=self._send_message_fn(),
            emit_event=self._emit_event_fn(),
            gh_binary=gh_binary,
            default_interval_s=default_interval,
        )
        try:
            poller.start(self.host.workers)
        except Exception:
            logger.exception("github: failed to start poller for %s", name)
            return
        self._pollers[name] = poller
        logger.info("github: started poller for workspace %s", name)

    def _stop_workspace(self, name: str) -> None:
        poller = self._pollers.pop(name, None)
        if poller is None:
            return
        with contextlib.suppress(Exception):
            poller.stop()
        logger.info("github: stopped poller for workspace %s", name)

    # ── Event handlers ────────────────────────────────────────────

    def _on_workspace_added(self, event: Event) -> None:
        data = event.data or {}
        name = data.get("name")
        path = data.get("path")
        if name and path:
            self._ensure_workspace(str(name), str(path))

    def _on_workspace_updated(self, event: Event) -> None:
        data = event.data or {}
        name = data.get("name")
        path = data.get("path")
        if not (name and path):
            return
        # An updated workspace may have toggled the github plugin off
        # OR changed its rules config. Cheapest path: stop and let
        # _ensure_workspace decide whether to start fresh.
        self._stop_workspace(str(name))
        self._ensure_workspace(str(name), str(path))

    def _on_workspace_removed(self, event: Event) -> None:
        name = (event.data or {}).get("name")
        if name:
            self._stop_workspace(str(name))

    def _on_workspace_file_changed(self, event: Event) -> None:
        """Reconcile when github.yaml is edited on disk (config dir or repo)."""
        data = event.data or {}
        fpath = Path(str(data.get("path") or ""))
        if fpath.name != "github.yaml":
            return
        ws_name: str | None = None
        ws_path: str | None = None
        # ~/.relaydeck/workspaces/<name>/github.yaml
        parts = fpath.parts
        if "workspaces" in parts:
            idx = parts.index("workspaces")
            if idx + 1 < len(parts):
                ws_name = parts[idx + 1]
        if not ws_name:
            root = str(data.get("root") or "")
            if root:
                for ws in _registered_workspaces(self.config_home):
                    if ws["path"] == root or root.startswith(ws["path"]):
                        ws_name = ws["name"]
                        ws_path = ws["path"]
                        break
        if not ws_name:
            return
        if ws_path is None:
            ws_path = next(
                (w["path"] for w in _registered_workspaces(self.config_home)
                 if w["name"] == ws_name),
                None,
            )
        if not ws_path:
            return
        self._stop_workspace(ws_name)
        self._ensure_workspace(ws_name, ws_path)

    def _on_shutdown(self, _event: Event) -> None:
        for name in list(self._pollers):
            self._stop_workspace(name)

    # ── Helpers ───────────────────────────────────────────────────

    def _gh_binary(self) -> str:
        try:
            value = self.host.settings.get("gh_binary", "gh")
        except Exception:
            value = "gh"
        return str(value or "gh")

    def _default_interval_s(self) -> float:
        try:
            value = self.host.settings.get("poll_interval_seconds", 30.0)
        except Exception:
            value = 30.0
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return 30.0

    def _send_message_fn(self):
        """Return a bound function the dispatcher can call to send a
        peer message. Hides the host.agents check + the optional
        `agents` host attribute (None on a daemon-less context)."""
        agents = getattr(self.host, "agents", None)
        if agents is None:
            return None
        return agents.send_message

    def _emit_event_fn(self):
        return self.host.events.emit

    # ── HTTP API (consumed by the dashboard panel) ────────────────

    def _register_api(self) -> None:
        host = self.host

        @host.api.route("/state", methods=["GET"])
        async def github_state():
            """Per-workspace github state for every registered workspace.
            Pollers run when github.yaml exists with a repo — no agent.toml
            opt-in required."""
            from .poller import cursor_path, load_cursor
            rows = []
            for ws in _registered_workspaces(self.config_home):
                name = ws["name"]
                cfg_path = self.config_home / "workspaces" / name / "github.yaml"
                configured = _workspace_github_configured(self.config_home, name)
                repo = None
                interval = None
                rules: list[dict] = []
                config_error: str | None = None
                if cfg_path.is_file():
                    try:
                        config = load_config(cfg_path)
                        if config is not None:
                            repo = config.repo
                            interval = config.poll_interval_s
                            rules = [
                                {
                                    "name": r.name,
                                    "when": r.when,
                                    "do": [
                                        list(a.keys())[0]
                                        for a in r.do
                                        if isinstance(a, dict) and a
                                    ],
                                }
                                for r in config.rules
                            ]
                        else:
                            config_error = "github.yaml empty"
                    except ValueError as exc:
                        config_error = f"github.yaml: {exc}"
                elif not configured:
                    config_error = "not configured"
                cursor = load_cursor(cursor_path(self.config_home, name))
                rows.append({
                    "workspace": name,
                    "configured": configured,
                    "running": name in self._pollers,
                    "repo": repo,
                    "poll_interval_s": interval,
                    "rules": rules,
                    "config_error": config_error,
                    "cursor": {
                        "last_event_id": cursor.last_event_id,
                        "last_poll_ts": cursor.last_poll_ts,
                        "last_error": cursor.last_error,
                    },
                })
            return {"workspaces": rows}

        @host.api.route("/spawn", methods=["POST"])
        async def github_spawn(body: dict):
            """Spawn a working agent for an issue/PR. Creates a git
            worktree off `workspace`'s repo, registers it as a workspace,
            starts an agent in it, and records a task. Runs in the daemon
            (live orchestrator). Body: workspace, branch, agent_id, and
            optional agent_type / purpose / kind / external_ref / title /
            plugins."""
            from fastapi import HTTPException

            from .spawn import spawn_worktree_agent

            workspace = str(body.get("workspace") or "").strip()
            branch = str(body.get("branch") or "").strip()
            agent_id = str(body.get("agent_id") or "").strip()
            if not workspace or not branch or not agent_id:
                raise HTTPException(400, "workspace, branch, and agent_id are required")
            repo_path = next(
                (w["path"] for w in _registered_workspaces(self.config_home)
                 if w["name"] == workspace),
                None,
            )
            if not repo_path:
                raise HTTPException(404, f"unknown workspace: {workspace}")
            try:
                return spawn_worktree_agent(
                    self.host,
                    repo_path=repo_path,
                    branch=branch,
                    agent_id=agent_id,
                    agent_type=str(body.get("agent_type") or "claude-code"),
                    purpose=str(body.get("purpose") or ""),
                    kind=str(body.get("kind") or "issue"),
                    external_ref=body.get("external_ref"),
                    title=str(body.get("title") or ""),
                    workspace_plugins=[str(p) for p in (body.get("plugins") or [])],
                )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(400, f"spawn failed: {exc}") from exc

        @host.api.route("/config", methods=["GET"])
        async def github_config_get(workspace: str):
            """Read raw github.yaml contents for a workspace.

            Returns `{exists, yaml, parsed, path}`. `parsed` is the
            structured form ({repo, poll_interval_s, rules: [...]})
            when the file exists and parses cleanly — the panel uses
            it to render the form-driven editor without having to do
            YAML parsing in the browser. `parsed` is None when the
            file is absent or has a parse error."""
            cfg_path = self.config_home / "workspaces" / workspace / "github.yaml"
            if not cfg_path.exists():
                return {
                    "exists": False, "yaml": "", "parsed": None,
                    "path": str(cfg_path),
                }
            try:
                text = cfg_path.read_text()
            except OSError as exc:
                return {
                    "exists": True, "yaml": "", "parsed": None,
                    "path": str(cfg_path), "error": str(exc),
                }
            parsed: dict | None = None
            try:
                config = load_config(cfg_path)
                if config is not None:
                    parsed = {
                        "repo": config.repo,
                        "poll_interval_s": config.poll_interval_s,
                        "rules": [
                            {"name": r.name, "when": r.when, "do": r.do}
                            for r in config.rules
                        ],
                    }
            except ValueError:
                parsed = None
            return {
                "exists": True, "yaml": text, "parsed": parsed,
                "path": str(cfg_path),
            }

        @host.api.route("/config", methods=["PUT"])
        async def github_config_put(body: dict):
            """Write a new github.yaml for a workspace. Validates via
            `load_config()` BEFORE writing — invalid YAML never lands
            on disk. Atomic via temp + rename so a concurrent reader
            sees either the old or new file, never a partial write.

            Accepts either:
              - `yaml`: raw github.yaml text (advanced editor path)
              - `structured`: {repo, poll_interval_s, rules: [...]} —
                serialized to YAML server-side so the form-driven
                editor doesn't need a JS YAML emitter.

            On success we trigger the workspace reconcile path
            (stop+start) so the poller picks up the new repo / rules /
            interval without a daemon restart."""
            import yaml as _yaml
            ws_name = (body or {}).get("workspace")
            yaml_text = (body or {}).get("yaml")
            structured = (body or {}).get("structured")
            if not isinstance(ws_name, str) or not ws_name:
                return {"ok": False, "error": "workspace required"}
            if structured is not None:
                if not isinstance(structured, dict):
                    return {"ok": False, "error": "structured must be a mapping"}
                # Reject obvious shape errors before YAML emission so
                # the operator gets a useful message faster than the
                # downstream load_config trip.
                if "repo" not in structured:
                    return {"ok": False, "error": "structured.repo required"}
                try:
                    yaml_text = _yaml.safe_dump(
                        structured, sort_keys=False, default_flow_style=False,
                    )
                except Exception as exc:
                    return {"ok": False, "error": f"serialize failed: {exc}"}
            if not isinstance(yaml_text, str):
                return {"ok": False, "error": "yaml or structured required"}
            ws_dir = self.config_home / "workspaces" / ws_name
            if not ws_dir.exists():
                return {"ok": False, "error": f"workspace {ws_name!r} not registered"}

            # Validate by parsing into a temp file. Using a temp keeps
            # the on-disk target intact if validation fails — atomic
            # rename only happens after load_config accepts the YAML.
            fd, tmp_name = tempfile.mkstemp(
                suffix=".yaml", dir=str(ws_dir), prefix=".github.yaml.",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(yaml_text)
                load_config(Path(tmp_name))  # raises ValueError on bad config
            except ValueError as exc:
                Path(tmp_name).unlink(missing_ok=True)
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                Path(tmp_name).unlink(missing_ok=True)
                return {"ok": False, "error": f"unexpected: {exc}"}

            # Atomic publish: rename the validated temp over the target.
            target = ws_dir / "github.yaml"
            try:
                Path(tmp_name).replace(target)
            except OSError as exc:
                Path(tmp_name).unlink(missing_ok=True)
                return {"ok": False, "error": f"write failed: {exc}"}

            # Reconcile the poller so the new config takes effect
            # immediately — same path workspace.updated takes.
            try:
                ws_path = next(
                    (w["path"] for w in _registered_workspaces(self.config_home)
                     if w["name"] == ws_name),
                    None,
                )
                if ws_path:
                    self._stop_workspace(ws_name)
                    self._ensure_workspace(ws_name, ws_path)
            except Exception as exc:
                logger.warning("github: post-write reconcile failed for %s: %s", ws_name, exc)
                return {"ok": True, "reconcile_error": str(exc)}
            return {"ok": True}

        @host.api.route("/auth", methods=["GET"])
        async def github_auth():
            """Probe for `gh` and its auth state.

            Three states surface in the panel:
              - `installed=false`              → user needs to install gh
              - `installed=true, auth_ok=false` → user needs to run `gh auth login`
              - `installed=true, auth_ok=true`  → green light, show user/host

            We deliberately don't try to *do* the login from here.
            Device-auth needs a TTY for the prompts, and OAuth-redirect
            login wants a browser. The panel just renders the right
            instruction string so the operator runs it themselves."""
            import shutil
            import subprocess
            gh = self._gh_binary()
            path = shutil.which(gh) if gh else None
            if not path:
                return {
                    "installed": False,
                    "binary": gh,
                    "auth_ok": False,
                    "user": None,
                    "host": None,
                    "raw": "",
                    "hint": (
                        "gh CLI not found on PATH. Install via your package "
                        "manager (pacman -S github-cli / brew install gh / "
                        "apt install gh)."
                    ),
                }
            try:
                proc = subprocess.run(
                    [path, "auth", "status"],
                    capture_output=True, timeout=8, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {
                    "installed": True, "binary": path, "auth_ok": False,
                    "user": None, "host": None, "raw": "",
                    "hint": f"`gh auth status` failed: {exc}",
                }
            raw = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()
            ok = proc.returncode == 0
            # Parse the user line: "✓ Logged in to <host> account <user> (...)"
            user = None
            host = None
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith(("✓", "X", "-", "*")):
                    # First non-bullet line is usually the host (e.g. "github.com").
                    host = host or stripped
                if "Logged in to" in line and "account" in line:
                    after = line.split("account", 1)[1].strip()
                    user = after.split()[0] if after else None
            return {
                "installed": True, "binary": path, "auth_ok": ok,
                "user": user, "host": host, "raw": raw,
                "hint": "" if ok else "Run `gh auth login` in a terminal to authenticate.",
            }

        @host.api.route("/sync", methods=["POST"])
        async def github_sync(body: dict):
            """One-shot poll for a single workspace. The panel calls
            this so operators can force a refresh without waiting for
            the next poll tick."""
            ws_name = (body or {}).get("workspace")
            if not isinstance(ws_name, str) or not ws_name:
                return {"ok": False, "error": "workspace required"}
            poller = self._pollers.get(ws_name)
            if poller is None:
                return {"ok": False, "error": f"no active poller for {ws_name!r}"}

            class _StubWorker:
                logs: list[str] = []

                def log(self, msg, level="info"):
                    _StubWorker.logs.append(f"{level}: {msg}")

            try:
                poller._tick(_StubWorker())
            except Exception as exc:
                return {"ok": False, "error": str(exc), "logs": _StubWorker.logs}
            return {"ok": True, "logs": _StubWorker.logs}

    # ── CLI ────────────────────────────────────────────────────────

    def _register_cli(self) -> None:
        host = self.host
        import click
        from rich.console import Console
        from rich.table import Table

        console = Console()

        @host.cli.command("status", help="Show github poller status per workspace.")
        def _status():
            from relaydeck.state import get_current_workspace

            active = get_current_workspace()
            table = Table(title="GitHub pollers", show_lines=False)
            table.add_column("workspace", style="cyan")
            table.add_column("repo")
            table.add_column("rules", justify="right")
            table.add_column("last_id")
            table.add_column("last_poll")
            table.add_column("last_error", style="red")
            for ws in sorted(_registered_workspaces(self.config_home), key=lambda x: x["name"]):
                name = ws["name"]
                if not _workspace_has_github(self.config_home, name):
                    continue
                cfg_path = self.config_home / "workspaces" / name / "github.yaml"
                repo = "-"
                rules_n = 0
                try:
                    config = load_config(cfg_path)
                    if config:
                        repo = config.repo
                        rules_n = len(config.rules)
                except ValueError as exc:
                    repo = f"[red]config error: {exc}[/]"
                cursor = load_cursor(cursor_path(self.config_home, name))
                marker = "● " if name == active else "  "
                table.add_row(
                    marker + name,
                    repo,
                    str(rules_n),
                    str(cursor.last_event_id or "-"),
                    str(cursor.last_poll_ts or "-"),
                    str(cursor.last_error or ""),
                )
            if not table.row_count:
                console.print(
                    "[dim]No GitHub pollers running. "
                    "Add a [bold]github.yaml[/] with a [bold]repo:[/] under "
                    "[bold]~/.relaydeck/workspaces/<name>/[/] "
                    "(GitHub lens or [bold]relaydeck github[/]).[/]"
                )
                return
            console.print(table)

        @host.cli.command(
            "spawn",
            help="Spawn a worktree + agent + task for an issue/PR.",
        )
        @click.argument("branch")
        @click.option("--agent-id", "agent_id", required=True,
                      help="Id for the agent to create and start.")
        @click.option("--workspace", "-w",
                      help="Source workspace whose repo to branch from "
                           "(defaults to active).")
        @click.option("--agent-type", "agent_type", default="claude-code",
                      show_default=True)
        @click.option("--purpose", default="", help="Agent purpose line.")
        @click.option("--kind", default="issue", show_default=True,
                      help="Task kind (issue|pr|...).")
        @click.option("--ref", "external_ref", default=None,
                      help="Upstream ref recorded on the task, e.g. owner/repo#42.")
        @click.option("--title", default="", help="Task title.")
        @click.option("--plugin", "plugins", multiple=True,
                      help="Plugin to enable on the new workspace (repeatable).")
        def _spawn(branch, agent_id, workspace, agent_type, purpose, kind,
                   external_ref, title, plugins):
            """Create a git worktree off the source workspace's repo on
            BRANCH, register it as a workspace, start an agent in it, and
            record a task. Routed through the daemon (it touches the live
            orchestrator)."""
            ws = workspace or _active_workspace_or_die(console)
            payload = {
                "workspace": ws, "branch": branch, "agent_id": agent_id,
                "agent_type": agent_type, "purpose": purpose, "kind": kind,
                "external_ref": external_ref, "title": title,
                "plugins": list(plugins),
            }
            ok, resp = _post_daemon("/api/plugins/github/spawn", payload)
            if ok and isinstance(resp, dict):
                console.print(
                    f"[green]✓[/] spawned [bold]{resp.get('agent_id')}[/] in "
                    f"workspace [bold]{resp.get('workspace')}[/] "
                    f"(branch {resp.get('branch')}, task {resp.get('task_id')})"
                )
                return
            console.print(f"[red]✗[/] spawn failed: {resp}")
            raise SystemExit(1)

        # ---- rules subgroup (rules list / show / test) ------------
        # The SDK doesn't yet support nested click groups via
        # host.cli.command, so we collapse the subgroup into hyphenated
        # commands: `relaydeck github rules-list`, `rules-show`, `rules-test`.

        @host.cli.command(
            "rules-list",
            help="List rules for the active workspace's github.yaml.",
        )
        @click.option("--workspace", "-w", help="Workspace name (defaults to active).")
        def _rules_list(workspace: str | None):
            ws = workspace or _active_workspace_or_die(console)
            config = _load_or_print(console, self.config_home, ws)
            if config is None:
                return
            table = Table(title=f"github.yaml rules for {ws}")
            table.add_column("name", style="cyan")
            table.add_column("when")
            table.add_column("do", style="green")
            for rule in config.rules:
                table.add_row(
                    rule.name,
                    ", ".join(f"{k}={v}" for k, v in rule.when.items()),
                    ", ".join(list(a.keys())[0] for a in rule.do),
                )
            console.print(table)

        @host.cli.command(
            "rules-show",
            help="Show the full rule body for one rule.",
        )
        @click.argument("name")
        @click.option("--workspace", "-w", help="Workspace name (defaults to active).")
        def _rules_show(name: str, workspace: str | None):
            ws = workspace or _active_workspace_or_die(console)
            config = _load_or_print(console, self.config_home, ws)
            if config is None:
                return
            for rule in config.rules:
                if rule.name == name:
                    console.print({"name": rule.name, "when": rule.when, "do": rule.do})
                    return
            console.print(f"[red]No rule named {name!r}[/]")

        @host.cli.command(
            "rules-test",
            help="Dry-run an event JSON file through the rules engine.",
        )
        @click.argument("event_file", type=click.Path(exists=True, dir_okay=False))
        @click.option("--workspace", "-w", help="Workspace name (defaults to active).")
        def _rules_test(event_file: str, workspace: str | None):
            ws = workspace or _active_workspace_or_die(console)
            config = _load_or_print(console, self.config_home, ws)
            if config is None:
                return
            try:
                event = json.loads(Path(event_file).read_text())
            except (OSError, ValueError) as exc:
                console.print(f"[red]Cannot read event file: {exc}[/]")
                return
            results = evaluate(config, event)
            if not results:
                console.print(f"[yellow]No rules matched {event.get('type', '<unknown>')}[/]")
                return
            for rule, rendered in results:
                console.print(f"[green]✓[/] {rule.name}")
                for action in rendered:
                    console.print(f"  → {action}")

        @host.cli.command(
            "sync",
            help="Run one poll cycle for the active workspace (foreground).",
        )
        @click.option("--workspace", "-w", help="Workspace name (defaults to active).")
        def _sync(workspace: str | None):
            ws = workspace or _active_workspace_or_die(console)
            poller = self._pollers.get(ws)
            if poller is None:
                console.print(
                    f"[red]No active poller for {ws}.[/] "
                    "Drop a `github.yaml` with a `repo:` into "
                    f"workspaces/{ws}/ (or use the GitHub lens) — github is "
                    "now daemon-wide and no longer driven by agent.toml."
                )
                return

            class _StubWorker:
                def log(self, message: str, level: str = "info") -> None:
                    color = "yellow" if level == "warn" else "dim"
                    console.print(f"[{color}]{message}[/]")

            poller._tick(_StubWorker())
            console.print("[green]✓[/] one-shot sync complete")

        @host.cli.command(
            "auth",
            help="Check that the `gh` CLI is authenticated (proxies `gh auth status`).",
        )
        def _auth():
            import subprocess
            gh = self._gh_binary()
            try:
                proc = subprocess.run([gh, "auth", "status"], capture_output=True, timeout=10)
            except FileNotFoundError:
                console.print(f"[red]gh binary not found at {gh!r}[/]")
                return
            text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
            console.print(text.strip() or "(no output)")


def _active_workspace_or_die(console) -> str:
    from relaydeck.state import get_current_workspace
    active = get_current_workspace()
    if not active:
        console.print(
            "[red]No active workspace.[/] "
            "Run [bold]relaydeck workspace set <name>[/] or pass [bold]-w <name>[/]."
        )
        raise SystemExit(1)
    return active


def _load_or_print(console, config_home: Path, workspace: str) -> RulesConfig | None:
    cfg_path = config_home / "workspaces" / workspace / "github.yaml"
    try:
        config = load_config(cfg_path)
    except ValueError as exc:
        console.print(f"[red]github.yaml error in {workspace}: {exc}[/]")
        return None
    if config is None:
        console.print(
            f"[yellow]No github.yaml in workspace {workspace!r}.[/] "
            f"Create [bold]{cfg_path}[/] to enable routing."
        )
        return None
    return config


PLUGIN = GithubPlugin()


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compatibility shim for the legacy PluginRegistry loader. Same
    pattern as file_watcher / handover — the SDK adapter is the real
    load path; this only fires when tests drive _legacy_on_load
    directly."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "events.subscribe",
            "events.emit",
            "workers.spawn",
            "agents.list",
            "agents.send",
            "cli.register",
            "api.register",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
    )
    PLUGIN.on_load(host)
