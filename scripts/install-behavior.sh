#!/usr/bin/env bash
# install-behavior.sh — probe how relaydeck behaves on a bare box with NO
# harness CLIs installed (pi/claude/codex/cursor-agent/opencode all absent).
# This is the realistic "fresh Ubuntu server" case: relaydeck must install,
# boot, and degrade gracefully — clear about what's missing, never crashing.
#
# Run inside tests/install/Dockerfile.ubuntu. Informational (doesn't exit
# non-zero on the "expected" degradations) — read the output.

set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

h() { printf '\n\033[36m══ %s\033[0m\n' "$*"; }
api() { curl -fsS "http://127.0.0.1:8765$1" -H "Authorization: Bearer $TOKEN" "${@:2}"; }

h "relaydeck --version (installed from scratch on $(. /etc/os-release; echo "$PRETTY_NAME"))"
relaydeck --version

h "which harness CLIs are on PATH? (expect: none)"
for c in pi claude codex cursor-agent opencode; do
  printf '  %-10s ' "$c"; command -v "$c" >/dev/null 2>&1 && echo "FOUND" || echo "—"
done

h "boot the daemon"
relaydeck daemon start
ok=""
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:8765/healthz >/dev/null 2>&1 && { ok=1; break; }; sleep 1; done
[ "$ok" = 1 ] || { echo "daemon never came up"; relaydeck daemon logs -n 40 2>/dev/null; exit 1; }
TOKEN="$(curl -fsS http://127.0.0.1:8765/api/auth/bootstrap | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')"

h "relaydeck doctor — does it tell me harness CLIs are missing?"
relaydeck doctor || true

h "/api/harnesses — does the new-agent catalog flag CLI availability?"
api /api/harnesses | python3 -m json.tool

h "create a workspace + a pi agent, then START it (CLI is absent)"
mkdir -p /tmp/proj
relaydeck workspace add /tmp/proj --name proj || true
relaydeck agent create probe --type pi --workspace proj || true
relaydeck agent start probe || true
sleep 3

h "agent status after start (expect: errored, with a clear reason)"
api /api/agents/probe | python3 -c 'import sys,json; a=json.load(sys.stdin); print("  status      :", a.get("status")); print("  semantic    :", a.get("semantic_status")); print("  status_msg  :", a.get("status_message") or a.get("last_error") or a.get("error") or "(none)")'

h "agent screen (what the operator sees)"
relaydeck agent screen probe 2>&1 | head -8 || true

h "is the daemon still healthy after the failed spawn?"
curl -fsS http://127.0.0.1:8765/healthz && echo "  ✓ daemon alive"
api /api/agents | python3 -c 'import sys,json; print("  agents tracked:", len(json.load(sys.stdin)))'

h "doctor again, now with an errored agent"
relaydeck doctor || true

relaydeck daemon stop || true
printf '\n\033[32m✓ behavior probe complete\033[0m\n'
