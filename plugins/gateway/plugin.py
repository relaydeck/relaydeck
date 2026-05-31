"""
Gateway plugin — external event ingress and egress.

Bridges relaydeck's internal event bus to the outside world. Three
transport surfaces today:

  - **webhook**  — HTTP endpoint at `/api/gateway/webhook/<channel>`
    that receives external events (GitHub, Slack, …) and emits them
    as `gateway.webhook` events. Other plugins subscribe to react.
  - **message**  — CLI-driven manual emission for testing.
  - **teleport** — `/api/gateway/teleport` for one relaydeck instance to
    forward an event into another's bus (still a stub).

External systems POST to `/api/gateway/webhook/<channel>`. The path
is publicly registered with those systems (webhook URL in GitHub
settings, Slack app config, etc.), so the plugin opts out of the
`/api/plugins/gateway/...` default prefix via `top_level_api = true`
in plugin.toml. Without that flag the URL would shift and external
integrations would silently break.

Declared capabilities: api.register (routes), events.emit (inject incoming
webhooks onto the bus), cli.register (`relaydeck gateway`). Channel config
lives in the daemon's gateway.toml and is reloaded explicitly.

Configuration (per daemon, in `~/.relaydeck/gateway.toml`):

    [gateway]
    enabled = true

    [gateway.channels]
    github = { secret = "gh-wh-secret" }
    slack = { secret = "slack-signing-secret" }
    custom = {}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from relaydeck.sdk import EventType, Plugin, PluginContext, PluginEventBus, PluginHost

PLUGIN_NAME = "gateway"

logger = logging.getLogger(__name__)


def _load_channels(config_home) -> dict[str, dict[str, Any]]:
    """Read the channel table from `<config_home>/gateway.toml`.
    Returns an empty dict if the file is missing or malformed —
    misconfiguration silently disables the gateway rather than
    crashing the daemon."""
    path = config_home / "gateway.toml"
    if not path.exists():
        return {}
    try:
        from relaydeck.config import load_config_file
        data = load_config_file(path)
    except Exception as exc:
        logger.warning("gateway: failed to load %s: %s", path, exc)
        return {}
    gw = data.get("gateway", {}) if isinstance(data, dict) else {}
    channels = gw.get("channels", {}) if isinstance(gw, dict) else {}
    return dict(channels) if isinstance(channels, dict) else {}


class GatewayPlugin(Plugin):
    """External webhook + teleport gateway.

    Owns three top-level routes (under `/api/gateway/*`) and a small
    CLI subgroup. The channel registry is reloaded from disk at
    on_load — operators add/remove channels by editing gateway.toml
    and restarting the daemon, which is the same UX as before the
    migration.
    """

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self.channels: dict[str, dict[str, Any]] = _load_channels(host.config_home)
        self._register_routes()
        self._register_cli()

    def on_unload(self) -> None:
        self.channels = {}

    # ── API routes ──────────────────────────────────────────────────

    def _register_routes(self) -> None:
        host = self.host

        @host.api.route("/api/gateway/webhook/{channel}", methods=["POST"], top_level=True)
        async def gateway_webhook(channel: str, request):  # type: ignore[no-untyped-def]
            """Receive an external webhook. The body is captured raw for
            HMAC verification (when the channel has a `secret`), then
            parsed as JSON and emitted on the plugin bus as
            `gateway.webhook`."""
            from fastapi import HTTPException

            if not self.channels or channel not in self.channels:
                raise HTTPException(404, f"Channel '{channel}' not configured")

            ch_config = self.channels[channel]
            secret = ch_config.get("secret", "")
            raw = await request.body()

            if secret:
                sig = request.headers.get("X-Relaydeck-Signature", "")
                expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(sig, expected):
                    raise HTTPException(401, "Invalid signature")

            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"raw": raw.decode("utf-8", errors="replace")}

            host.events.emit(
                EventType.GATEWAY_WEBHOOK,
                {
                    "channel": channel,
                    "body": body,
                    "headers": dict(request.headers),
                },
            )
            return {"status": "received", "channel": channel}

        @host.api.route("/api/gateway/channels", methods=["GET"], top_level=True)
        async def list_channels():
            """List configured gateway channels. Secrets are never
            exposed — only whether a secret is set."""
            return [
                {"name": name, "has_secret": bool(cfg.get("secret"))}
                for name, cfg in self.channels.items()
            ]

        @host.api.route("/api/gateway/teleport", methods=["POST"], top_level=True)
        async def gateway_teleport(body: dict[str, Any]):
            """Receive a teleport event from another relaydeck instance.
            Currently a stub — the body is emitted onto the bus as
            `gateway.teleport` so subscribers can react, but no
            authentication or routing layer is in place yet.
            A future federation layer will add authentication and routing."""
            host.events.emit(EventType.GATEWAY_TELEPORT, dict(body))
            return {"status": "received"}

    # ── CLI subgroup ────────────────────────────────────────────────

    def _register_cli(self) -> None:
        host = self.host
        import click

        # host.cli.command stores the bare function; HostPluginAdapter
        # wraps it with click.command(name=...) at registration time.
        # Stacking click.argument BELOW host.cli.command means the
        # argument decorators run first and populate __click_params__
        # before the SDK registrar sees the function — same pattern as
        # bare click usage, just with the registration at the top.

        @host.cli.command("channels", help="List configured gateway channels.")
        def _channels_list():
            from rich.console import Console

            console = Console()
            if not self.channels:
                console.print("[dim]No gateway channels configured.[/]")
                console.print(
                    "Create [bold]~/.relaydeck/gateway.toml[/] to add channels.",
                )
                return
            for name, cfg in self.channels.items():
                secret_status = "🔒" if cfg.get("secret") else "🔓"
                console.print(f"  {secret_status} [cyan]{name}[/]")

        @host.cli.command("emit", help="Manually emit a gateway event for testing.")
        @click.argument("channel")
        @click.argument("payload", required=False)
        def _emit(channel: str, payload: str | None = None):
            from rich.console import Console

            console = Console()
            data: dict[str, Any] = {}
            if payload:
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    data = {"raw": payload}
            host.events.emit(
                EventType.GATEWAY_MESSAGE,
                {"channel": channel, "body": data},
            )
            console.print(
                f"[green]✓[/] Emitted gateway event on channel [bold]{channel}[/]",
            )


PLUGIN = GatewayPlugin()


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compat shim for the existing PluginRegistry loader. The real
    loader path is the SDK adapter (HostPluginAdapter) — this is
    only used by tests that drive `_legacy_on_load` directly."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "api.register",
            "events.emit",
            "cli.register",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
        top_level_api=True,
    )
    PLUGIN.on_load(host)
