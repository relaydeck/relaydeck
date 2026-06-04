"""Context-watch: emit `agent.context` fullness so a manager can compact or
start a fresh session BEFORE a harness auto-compacts (which blows the KV cache
and can drop instructions)."""
