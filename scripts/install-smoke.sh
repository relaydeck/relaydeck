#!/usr/bin/env bash
# install-smoke.sh — verify a FRESH relaydeck install actually works.
#
# Runs inside the install-test container (tests/install/Dockerfile) after
# `uv tool install` has put `relaydeck` on PATH. No API keys, no agents —
# this proves the package installed cleanly and the daemon boots + serves
# the dashboard + API + passes its own self-diagnostic. Loud on the first
# failure (set -e), prints daemon logs when the daemon won't come up.

set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

step() { printf '\n\033[36m::\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

step "relaydeck on PATH + --version (single-sourced from package metadata)"
command -v relaydeck >/dev/null 2>&1 || fail "relaydeck not on PATH after install"
relaydeck --version
relaydeck --help >/dev/null || fail "relaydeck --help failed"

step "boot the daemon (writes a fresh ~/.relaydeck)"
relaydeck daemon start

step "wait for /healthz"
ok=""
for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8765/healthz >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
done
[ "${ok:-}" = 1 ] || { relaydeck daemon logs -n 60 2>/dev/null || true; fail "daemon never answered /healthz"; }

step "/healthz reports a version"
HEALTH="$(curl -fsS http://127.0.0.1:8765/healthz)"
printf '  %s\n' "$HEALTH"
echo "$HEALTH" | grep -q '"version"' || fail "/healthz missing version"

step "authed API works (loopback bootstrap token)"
TOKEN="$(curl -fsS http://127.0.0.1:8765/api/auth/bootstrap \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')"
[ -n "$TOKEN" ] || fail "could not bootstrap an auth token"
curl -fsS http://127.0.0.1:8765/api/agents -H "Authorization: Bearer $TOKEN" >/dev/null \
  || fail "/api/agents failed"

step "the dashboard HTML serves"
curl -fsS http://127.0.0.1:8765/ | grep -qi "relaydeck" || fail "dashboard did not serve"

step "update check endpoint responds (fail-open offline)"
curl -fsS "http://127.0.0.1:8765/api/version" -H "Authorization: Bearer $TOKEN" \
  | grep -q '"update_available"' || fail "/api/version malformed"

step "self-diagnostic (relaydeck doctor)"
relaydeck doctor || true   # advisory: warns about no providers/workspaces, never fatal here

step "stop the daemon"
relaydeck daemon stop || true

printf '\n\033[32m✓ INSTALL SMOKE OK\033[0m\n'
