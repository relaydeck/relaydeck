"""
relaydeck-native harness plugin — registers the `relaydeck` agent type (pi-backed)
and the chat/context HTTP surface the dashboard tiles call.

Mirrors the loop/pi plugin shape: `on_load` hands the agent class to the
host's harness registrar. It also registers two routes:

  POST /api/plugins/relaydeck-native/chat
       {agent_id, text} -> {reply, sent, model}   (one chat turn)
  GET  /api/plugins/relaydeck-native/{agent_id}/context
       -> {layers, composed}                       (what gets injected)

The context route runs the same prompt builder as a real turn WITHOUT
completing, so the Context tile shows exactly what the model would see.
"""

from __future__ import annotations

import logging

from relaydeck.plugin import PluginContext, PluginEventBus
from relaydeck.sdk import Plugin, PluginHost

from .agent import RelaydeckNativeAgent

PLUGIN_NAME = "relaydeck-native"
logger = logging.getLogger(__name__)


class RelaydeckNativePlugin(Plugin):
    workspace_scoped = False
    description = "relaydeck-native operator agent (type: relaydeck) — pi-backed with fleet tools."

    def on_load(self, host: PluginHost) -> None:
        import asyncio
        self.host = host
        self.config_home = host.config_home
        self.db_path = str(host.config_home / "runtime" / "relaydeck.db")
        # Serializes concurrent /install-pi calls (double-click / two-tab).
        self._install_pi_lock: asyncio.Lock = asyncio.Lock()
        host.harnesses.register("relaydeck", RelaydeckNativeAgent)
        try:
            self._register_routes(host)
        except Exception:
            # Route registration is best-effort: a constrained host (the
            # legacy test shim, or a host without an HTTP surface) must
            # still get the `relaydeck` agent type wired.
            logger.debug("relaydeck-native: route registration skipped", exc_info=True)

    def _register_routes(self, host: PluginHost) -> None:
        if getattr(host, "api", None) is None:
            return
        config_home = self.config_home
        db_path = self.db_path

        @host.api.route("/status", methods=["GET"])
        async def relaydeck_native_status():
            import asyncio
            from .pi_engine import pi_runtime_status
            status = await asyncio.to_thread(pi_runtime_status)
            return {"ok": True, **status}

        @host.api.route("/install-pi", methods=["POST"])
        async def relaydeck_install_pi():
            """One-click pi installer — invokes the npm subprocess in a
            thread so the daemon event loop stays responsive while npm
            chews through 200+ packages. Idempotent: running again on an
            already-installed pi is a no-op for npm and returns the same
            success shape (ok + pi_installed=True).

            Serialized via an asyncio lock so a double-click or two-tab
            race can't fire two concurrent `npm install -g` against the
            same global prefix (npm's per-prefix lock would abort one or
            leave a partial install). The second caller waits for the
            first and returns the same final status."""
            import asyncio
            from .pi_engine import install_pi_npm
            lock = self._install_pi_lock
            async with lock:
                return await asyncio.to_thread(install_pi_npm)

        @host.api.route("/chat", methods=["POST"])
        async def relaydeck_chat(body: dict):
            # chat_endpoint runs the model (blocking) + tool loop — offload
            # to a thread so a turn never stalls the daemon event loop (which
            # would freeze the dashboard + other agents while it thinks).
            import asyncio
            from .agent import chat_endpoint
            return await asyncio.to_thread(chat_endpoint, config_home, db_path, body or {})

        @host.api.route("/{agent_id}/chat/new", methods=["POST"])
        async def relaydeck_chat_new(agent_id: str):
            """Start a fresh chat session (the `/new` REPL command). Sets a
            non-destructive session boundary so prior turns are no longer
            injected; the durable record is preserved."""
            from .agent import reset_session
            since = reset_session(config_home, agent_id)
            return {"ok": True, "agent_id": agent_id, "since": since}

        @host.api.route("/{agent_id}/context", methods=["GET"])
        async def relaydeck_context(agent_id: str):
            import asyncio
            from .agent import context_endpoint
            return await asyncio.to_thread(context_endpoint, config_home, db_path, agent_id)

        @host.api.route("/{agent_id}/invocations", methods=["GET"])
        async def relaydeck_invocations(agent_id: str, limit: int = 40):
            """Per-call LLM log for this agent: prompt, response, tokens,
            latency, ok — the extended tracking a native agent records on
            every turn (model_invocations)."""
            from relaydeck.model_invocations import list_invocations
            rows = list_invocations(agent_id, limit=limit, db_path=db_path)
            return {"ok": True, "invocations": [r.to_dict() for r in rows]}


PLUGIN = RelaydeckNativePlugin()


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compat shim mirroring the loop/pi pattern so tests can wire the
    agent type without the full SDK adapter."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "harnesses.register", "events.subscribe", "events.emit",
            "agents.send", "models.complete",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
    )
    PLUGIN.on_load(host)
    from relaydeck.orchestrator import register_agent_type
    for type_name, cls in host.harnesses.registrations:
        register_agent_type(type_name, cls)
