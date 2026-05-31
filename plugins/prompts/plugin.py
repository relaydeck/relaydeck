"""
Interactive prompts plugin — user/agent-facing surface for structured
human-in-the-loop approvals.

Architecture mirrors `messaging`: the data + orchestration layers live in
core (`relaydeck/prompts.py` + `relaydeck/channels.py` + the
`interactive_prompts` table). This plugin owns the **surface**:

  - The built-in ``web`` channel provider — an always-interactive,
    zero-config destination so prompts work in the dashboard even with no
    Telegram/Discord/Slack integration configured. (Native messaging
    providers register themselves the same way: see the telegram plugin.)
  - CLI: ``relaydeck prompts ask|list|show|answer``.
  - HTTP: ``/api/plugins/prompts`` (create/list/show) and
    ``/api/plugins/prompts/{id}/respond`` (the dashboard button click).
  - ``SKILL.md`` so a harnessed agent discovers the ``ask`` command.

CLI commands reach the running daemon over HTTP (the orchestrator +
channel workers live there); the daemon-side handlers call
``host.prompts``, which is wired to the orchestrator's resume primitive.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

import click

from relaydeck.channels import Address, ChannelCapabilities, DeliveryResult
from relaydeck.prompts import (
    STATE_OPEN,
    Choice,
)
from relaydeck.sdk import Plugin, PluginHost

logger = logging.getLogger(__name__)

PLUGIN_NAME = "prompts"

# Floor for a positive sweep interval so a fat-fingered setting (e.g. 0.1)
# can't hammer SQLite. 0 still disables the sweeper entirely.
MIN_SWEEP_INTERVAL = 5.0


# ── Built-in `web` channel provider ──────────────────────────────────


class WebChannelProvider:
    """The dashboard as a messaging channel.

    Always interactive (the browser renders real buttons) and always
    available, so an agent can raise an approval prompt with zero external
    setup — the bundled safety net beneath Telegram/Discord/etc.

    Delivery is implicit: the prompt already lives in the store and the
    dashboard subscribes to ``prompt.created`` / ``prompt.answered`` over
    SSE, rendering buttons from the prompt row and POSTing the click back
    to ``/api/plugins/prompts/{id}/respond``. So ``deliver_prompt`` just
    acknowledges; there's no outbound API call to make.
    """

    channel = "web"

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            interactive_buttons=True,
            editable=True,
            rich_text=True,
            max_buttons=0,
            max_per_row=3,
        )

    def connections(self) -> list[dict[str, Any]]:
        return [{"id": "dashboard", "name": "Dashboard", "ready": True}]

    def deliver_prompt(self, address: Address, prompt: Any) -> DeliveryResult:
        # The dashboard reads the prompt from the store via SSE; nothing
        # to push. Ref is the prompt id so the (no-op) close can match.
        return DeliveryResult(ok=True, ref=prompt.id, mode="buttons")

    def deliver_text(
        self, address: Address, text: str, *, in_reply_to: str | None = None
    ) -> DeliveryResult:
        return DeliveryResult(ok=True, ref="", mode="text")

    def close_prompt(self, address: Address, ref: str, prompt: Any) -> None:
        # The dashboard reacts to the `prompt.answered` SSE event and
        # disables its own buttons — no server-side edit needed.
        return None


# ── Daemon HTTP helpers (CLI runs in a separate process) ─────────────


def _daemon_base() -> str:
    from relaydeck.state import get_daemon_url

    return get_daemon_url().rstrip("/")


def _auth_headers() -> dict[str, str]:
    from relaydeck.auth import read_token

    headers = {"Content-Type": "application/json"}
    tok = read_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def _request(path: str, *, method: str = "GET", body: dict | None = None,
             timeout: float = 10.0) -> tuple[bool, Any]:
    url = _daemon_base() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_auth_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _resolve_agent(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("RELAYDECK_AGENT_ID")
    return env.strip() if env and env.strip() else ""


def _resolve_workspace(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    from relaydeck.state import get_current_workspace

    return get_current_workspace()


# ── Plugin ───────────────────────────────────────────────────────────


class PromptsPlugin(Plugin):
    def __init__(self) -> None:
        self.host: PluginHost | None = None
        self._web = WebChannelProvider()
        self._sweeper: Any = None

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        # Register the always-on web channel provider.
        host.channels.register(self._web)
        # Prime the core service singleton now so its resume_fn (the
        # orchestrator's send_message_to) is wired before any channel
        # provider calls respond() — otherwise a Telegram callback that
        # arrives before the first `ask` couldn't resume the agent.
        try:
            host.prompts._service()  # noqa: SLF001 — own host, intentional priming
        except Exception:
            logger.debug("prompts: service prime skipped", exc_info=True)
        self._register_cli(host)
        self._register_api(host)
        self._spawn_sweeper(host)

    # -- expiry sweeper ------------------------------------------------

    def _sweep_interval(self) -> float:
        """Seconds between expiry sweeps; 0 disables the worker. A positive
        value is floored to MIN_SWEEP_INTERVAL so a bad setting can't pin a
        CPU sweeping SQLite every fraction of a second."""
        if self.host is None:
            return 0.0
        try:
            raw = float(self.host.settings.get("sweep_interval_seconds") or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(MIN_SWEEP_INTERVAL, raw) if raw > 0 else 0.0

    def _spawn_sweeper(self, host: PluginHost) -> None:
        """Run `sweep_expired()` on a supervised timer so prompts created
        with `--expires` actually transition to EXPIRED (and resume their
        askers) without a human ever touching them. No-op when the
        interval is 0 or the host doesn't grant `workers.spawn` (e.g. a
        unit-test host) — the plugin still loads, just without auto-expiry.
        """
        interval = self._sweep_interval()
        if interval <= 0:
            return
        try:
            self._sweeper = host.workers.spawn(
                "expiry-sweeper",
                self._sweep_tick,
                interval=interval,
                description="Expire overdue interactive prompts and resume their askers.",
                restart_policy="restart",
            )
        except Exception:
            logger.debug("prompts: expiry sweeper not started", exc_info=True)

    def _sweep_tick(self, worker: Any = None) -> None:
        if self.host is None:
            return
        try:
            expired = self.host.prompts.sweep()
        except Exception:
            logger.debug("prompts: sweep tick failed", exc_info=True)
            return
        if expired:
            logger.info("prompts: expired %d overdue prompt(s)", len(expired))

    # -- settings helpers ---------------------------------------------

    def _default_channels(self) -> list[str]:
        if self.host is None:
            return ["web"]
        raw = str(self.host.settings.get("default_channels") or "web")
        out = [a.strip() for a in raw.split(",") if a.strip()]
        return out or ["web"]

    def _default_expires(self) -> float | None:
        if self.host is None:
            return None
        try:
            v = float(self.host.settings.get("default_expires_seconds") or 0)
        except (TypeError, ValueError):
            v = 0.0
        return v if v > 0 else None

    # -- CLI -----------------------------------------------------------

    def _register_cli(self, host: PluginHost) -> None:
        @host.cli.command(
            "ask",
            help="Ask a human a question with tap-able choices and "
                 "(optionally) block until they pick. Choices: -c id:Label:style.",
        )
        @click.argument("body", nargs=-1, required=True)
        @click.option("-c", "--choice", "choices", multiple=True, required=True,
                      help="A choice as id[:label[:style]] (repeatable). "
                           "style ∈ default|primary|danger|success.")
        @click.option("--to", "addresses", multiple=True,
                      help="Fan-out address channel[:conn[:target[:thread]]] "
                           "(repeatable). Defaults to the plugin's default_channels.")
        @click.option("--agent", "agent_id", default=None,
                      help="Asking agent id (defaults to $RELAYDECK_AGENT_ID).")
        @click.option("--workspace", default=None, help="Workspace (defaults to current).")
        @click.option("--expires", "expires", type=float, default=None,
                      help="Seconds until the prompt auto-expires.")
        @click.option("--wait", is_flag=True, default=False,
                      help="Block until answered/expired and print the decision as JSON.")
        @click.option("--wait-timeout", type=float, default=3600.0,
                      help="Max seconds to block in --wait mode (default 3600).")
        def _ask(body, choices, addresses, agent_id, workspace, expires, wait, wait_timeout):
            from rich.console import Console

            console = Console()
            text = " ".join(body).strip()
            if not text:
                console.print("[red]✗[/] empty prompt body")
                raise SystemExit(1)
            try:
                parsed = [Choice.parse(c) for c in choices]
            except ValueError as exc:
                console.print(f"[red]✗[/] bad choice: {exc}")
                raise SystemExit(1) from exc

            payload = {
                "body": text,
                "choices": [c.to_dict() for c in parsed],
                "addresses": list(addresses) or self._default_channels(),
                "agent": _resolve_agent(agent_id),
                "workspace": _resolve_workspace(workspace),
                "expires_in": expires if expires else self._default_expires(),
                # --wait: don't also inject a relay line; the CLI returns
                # the answer to the (blocking) caller directly.
                "notify_agent": not wait,
            }
            ok, resp = _request("/api/plugins/prompts/", method="POST", body=payload)
            if not ok or not isinstance(resp, dict):
                console.print(f"[red]✗[/] {resp}")
                raise SystemExit(1)
            pid = resp.get("id")
            console.print(
                f"[green]✓[/] prompt [bold]{pid}[/] sent to "
                f"{', '.join(payload['addresses'])}"
            )
            if not wait:
                return
            # Poll until resolved.
            deadline = time.time() + wait_timeout
            while time.time() < deadline:
                ok, got = _request(f"/api/plugins/prompts/{pid}", timeout=10.0)
                if ok and isinstance(got, dict) and got.get("state") != STATE_OPEN:
                    console.print_json(data={
                        "id": pid,
                        "state": got.get("state"),
                        "choice": got.get("answer_choice"),
                        "answered_by": got.get("answered_by"),
                    })
                    return
                time.sleep(1.0)
            console.print(f"[yellow]…[/] timed out waiting on {pid}")
            raise SystemExit(2)

        @host.cli.command("list", help="List prompts (open by default).")
        @click.option("--state", default="open",
                      type=click.Choice(["open", "answered", "expired", "canceled", "all"]))
        @click.option("--workspace", default=None)
        @click.option("--limit", type=int, default=30)
        def _list(state, workspace, limit):
            from rich.console import Console
            from rich.table import Table

            console = Console()
            q = f"?limit={int(limit)}"
            if state != "all":
                q += f"&state={state}"
            ws = _resolve_workspace(workspace)
            if ws:
                q += f"&workspace={ws}"
            ok, resp = _request(f"/api/plugins/prompts/{q}")
            if not ok or not isinstance(resp, list):
                console.print(f"[red]✗[/] {resp}")
                raise SystemExit(1)
            if not resp:
                console.print("[dim]no prompts[/]")
                return
            t = Table(box=None)
            for col in ("id", "state", "agent", "question", "choices", "answer"):
                t.add_column(col)
            for p in resp:
                t.add_row(
                    p.get("id", ""), p.get("state", ""), p.get("agent_id", ""),
                    (p.get("body", "")[:40]),
                    "/".join(c.get("id", "") for c in p.get("choices", [])),
                    p.get("answer_choice") or "",
                )
            console.print(t)

        @host.cli.command("show", help="Show one prompt by id.")
        @click.argument("prompt_id")
        def _show(prompt_id):
            from rich.console import Console

            console = Console()
            ok, resp = _request(f"/api/plugins/prompts/{prompt_id}")
            if not ok:
                console.print(f"[red]✗[/] {resp}")
                raise SystemExit(1)
            console.print_json(data=resp)

        @host.cli.command("answer", help="Answer a prompt on a human's behalf (operator).")
        @click.argument("prompt_id")
        @click.argument("choice_id")
        @click.option("--by", default="cli", help="Who is answering (audit attribution).")
        def _answer(prompt_id, choice_id, by):
            from rich.console import Console

            console = Console()
            ok, resp = _request(
                f"/api/plugins/prompts/{prompt_id}/respond",
                method="POST", body={"choice": choice_id, "by": by},
            )
            if not ok or not isinstance(resp, dict):
                console.print(f"[red]✗[/] {resp}")
                raise SystemExit(1)
            if resp.get("ok"):
                console.print(f"[green]✓[/] {prompt_id} → {choice_id}")
            else:
                console.print(f"[yellow]…[/] {resp.get('error') or 'already resolved'}")

    # -- HTTP API ------------------------------------------------------

    def _register_api(self, host: PluginHost) -> None:
        @host.api.route("/", methods=["POST"])
        async def create(body: dict[str, Any]):
            from fastapi import HTTPException

            text = str(body.get("body") or "").strip()
            if not text:
                raise HTTPException(400, "body required")
            raw_choices = body.get("choices") or []
            try:
                choices = [
                    Choice.parse(c) if isinstance(c, str) else Choice.from_dict(c)
                    for c in raw_choices
                ]
            except ValueError as exc:
                raise HTTPException(400, f"bad choice: {exc}") from exc
            if not choices:
                raise HTTPException(400, "at least one choice required")
            prompt = host.prompts.ask(
                text, choices,
                addresses=list(body.get("addresses") or ["web"]),
                agent_id=str(body.get("agent") or ""),
                workspace=body.get("workspace"),
                expires_in=body.get("expires_in"),
                multi=bool(body.get("multi", False)),
                notify_agent=bool(body.get("notify_agent", True)),
            )
            return prompt.to_dict()

        @host.api.route("/", methods=["GET"])
        async def list_prompts(
            state: str | None = None,
            workspace: str | None = None,
            agent: str | None = None,
            limit: int = 50,
        ):
            rows = host.prompts.list(
                state=(state if state and state != "all" else None),
                agent_id=agent, workspace=workspace, limit=limit,
            )
            return [p.to_dict() for p in rows]

        @host.api.route("/{prompt_id}", methods=["GET"])
        async def show(prompt_id: str):
            from fastapi import HTTPException

            p = host.prompts.get(prompt_id)
            if p is None:
                raise HTTPException(404, "no such prompt")
            return p.to_dict()

        @host.api.route("/{prompt_id}/respond", methods=["POST"])
        async def respond(prompt_id: str, body: dict[str, Any]):
            from fastapi import HTTPException

            choice = str(body.get("choice") or "").strip()
            reply = str(body.get("reply") or "").strip()
            who = str(body.get("by") or "web").strip() or "web"
            if not choice and not reply:
                raise HTTPException(400, "choice or reply required")
            if choice:
                resolved = host.prompts.respond(
                    prompt_id, choice, answered_by=who, value=body.get("value"),
                )
            else:
                resolved = host.prompts.respond_text(
                    prompt_id, reply, answered_by=who,
                )
            if resolved is None:
                # Already answered, expired, or the reply matched nothing.
                cur = host.prompts.get(prompt_id)
                return {
                    "ok": False,
                    "error": "already resolved" if (cur and cur.state != STATE_OPEN)
                             else "no matching choice",
                    "state": (cur.state if cur else "missing"),
                }
            return {"ok": True, "prompt": resolved.to_dict()}

        @host.api.route("/{prompt_id}/cancel", methods=["POST"])
        async def cancel(prompt_id: str, body: dict[str, Any]):
            res = host.prompts.cancel(prompt_id, reason=str(body.get("reason") or ""))
            return {"ok": res is not None}


PLUGIN = PromptsPlugin()
