"""Official relaydeck plugins — relaydeck-maintained, all in one tree.

Every relaydeck-managed plugin lives here: the infrastructure plugins the
daemon can't boot without (``vault``, ``github``, ``loop``, ``external_agents``,
``harnesses``) alongside the separable extensions (``messaging``, ``skills``,
``theme``, ``metering``, ``telegram``, ``gateway``, ``file_watcher``,
``usage_limits``, ``hitl``, ``dashboard``, ``providers``).

These load through real package import via the daemon plugin loader
(``PluginRegistry._scan_package("plugins")``). Core never imports this package
statically — the boundary is one-directional: plugins import the public core
facades (``relaydeck.sdk``, ``relaydeck.harness``, ``relaydeck.provider``,
``relaydeck.vault``, ``relaydeck.automation``, ``relaydeck.testing``), never the
other way around.

The tree ships in the single ``relaydeck`` wheel today; it is laid out so a
future split into a separately-versioned ``relaydeck-plugins`` distribution (or
per-plugin wheels) is mechanical. See ``AGENTS.md`` (Package separation).
"""
