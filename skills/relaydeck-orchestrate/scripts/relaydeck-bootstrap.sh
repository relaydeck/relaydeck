#!/usr/bin/env bash
# relaydeck-bootstrap — idempotent detect → install → daemon-up for the
# relaydeck-orchestrate skill. Safe to run repeatedly. Prints the dashboard
# URL on success. Exit codes: 0 ok, 2 refused (already inside relaydeck),
# 3 install failed, 4 daemon failed to come up.
set -euo pipefail

say()  { printf '\033[36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m· %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit "${2:-1}"; }

# ── 0. Refuse to bootstrap from inside a relaydeck-managed agent ──────
if [ -n "${RELAYDECK_AGENT_ID:-}" ]; then
  warn "RELAYDECK_AGENT_ID=$RELAYDECK_AGENT_ID — you are already a managed agent."
  warn "Do NOT bootstrap a second fleet. Use the relaydeck-fleet / relaydeck-cli"
  warn "skills to coordinate as a peer. (depth=${RELAYDECK_ORCHESTRATION_DEPTH:-0})"
  exit 2
fi

# ── 1. Find or install ────────────────────────────────────────────────
if command -v relaydeck >/dev/null 2>&1; then
  ok "relaydeck found: $(relaydeck --version 2>/dev/null || echo '?')"
else
  say "relaydeck not found — installing"
  if command -v uv >/dev/null 2>&1; then
    uv tool install relaydeck   || die "uv tool install failed" 3
  elif command -v pipx >/dev/null 2>&1; then
    pipx install relaydeck      || die "pipx install failed" 3
  elif command -v pip3 >/dev/null 2>&1; then
    pip3 install --user relaydeck || die "pip install failed" 3
  elif command -v pip >/dev/null 2>&1; then
    pip install --user relaydeck  || die "pip install failed" 3
  else
    die "no uv / pipx / pip on PATH — install one, or install relaydeck manually" 3
  fi
  command -v relaydeck >/dev/null 2>&1 \
    || die "installed but 'relaydeck' is not on PATH (check your tool bin dir)" 3
  ok "installed relaydeck $(relaydeck --version 2>/dev/null || echo '?')"
fi

# ── 2. Bring the daemon up (idempotent) ───────────────────────────────
if relaydeck daemon status >/dev/null 2>&1; then
  ok "daemon already running"
else
  say "starting daemon"
  relaydeck daemon start || die "daemon start failed — see 'relaydeck daemon logs'" 4
fi

# Confirm readiness; status also prints the dashboard URL.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if relaydeck status >/dev/null 2>&1; then
    ok "daemon ready"
    relaydeck status || true
    say "Next: relaydeck workspace add . --name proj --plugin messaging --plugin skills"
    exit 0
  fi
  sleep 1
done
die "daemon did not become ready in time — try 'relaydeck daemon logs'" 4
