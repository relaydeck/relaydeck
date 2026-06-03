#!/usr/bin/env bash
# migration_test.sh — real OLD→NEW in-place upgrade migration test. No mocks:
# installs the PREVIOUS published release, boots it, seeds a realistic state
# (workspace + a couple of agents, optionally running on a real OpenRouter
# model), snapshots the live DB + daemon, then installs the NEW build over the
# SAME config home and asserts the schema migrated and nothing was lost.
#
# Both install paths are covered via RD_METHOD:
#   RD_METHOD=pip          isolated venvs: uv pip install relaydeck==<prev> /
#                          uv pip install <local checkout>   (default; local-safe)
#   RD_METHOD=install_sh   the website installer: scripts/install.sh with
#                          RELAYDECK_SOURCE pinned to <prev> then the checkout
#                          (global uv-tool install — use in a container)
#
# Isolation: HOME is redirected to a scratch dir so ~/.relaydeck is sandboxed,
# and RELAYDECK_DAEMON_URL pins the CLI to our private port — it never touches
# an operator's real daemon.
set -euo pipefail

METHOD="${RD_METHOD:-pip}"
NEW_SRC="${RD_NEW_SRC:-$(cd "$(dirname "$0")/../.." && pwd)}"

# Previous version = the latest PyPI release strictly below this checkout's
# version (so it always tracks N-1 as releases ship, with no hardcoded pin).
# Falls back to the highest published version if none is strictly below, then
# to 0.1.3 if PyPI is unreachable. Override with RD_PREV_VERSION.
_derive_prev(){
  python3 - "$NEW_SRC" 2>/dev/null <<'PY'
import json, re, sys, urllib.request
try:
    cur = re.search(r'^version\s*=\s*"([^"]+)"',
                    open(sys.argv[1] + "/pyproject.toml").read(), re.M).group(1)
    def parse(v):
        n = re.findall(r"\d+", v)
        return tuple(int(x) for x in n[:3]) if n else (0,)
    d = json.load(urllib.request.urlopen("https://pypi.org/pypi/relaydeck/json", timeout=15))
    vers = [v for v, files in d["releases"].items() if files]
    below = sorted((parse(v), v) for v in vers if parse(v) < parse(cur))
    if below:
        print(below[-1][1])
    elif vers:
        print(sorted((parse(v), v) for v in vers)[-1][1])
except Exception:
    pass
PY
}
PREV="${RD_PREV_VERSION:-$(_derive_prev)}"
PREV="${PREV:-0.1.3}"

# Expected post-upgrade schema = the new checkout's _SCHEMA_VERSION (derived so
# this test never rots when the schema bumps). Override with RD_EXPECT_SCHEMA.
EXPECT_SCHEMA="${RD_EXPECT_SCHEMA:-$(grep -oE '_SCHEMA_VERSION = [0-9]+' "$NEW_SRC/relaydeck/db.py" | grep -oE '[0-9]+' | head -1)}"
EXPECT_SCHEMA="${EXPECT_SCHEMA:-18}"
PORT="${RD_PORT:-8799}"
WORK="${RD_WORK:-$(mktemp -d)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MODEL="${RELAYDECK_MIG_MODEL:-}"

export HOME="$WORK/home"; mkdir -p "$HOME"
export RELAYDECK_DAEMON_URL="http://127.0.0.1:$PORT"
DB="$HOME/.relaydeck/runtime/relaydeck.db"
BASE_URL="http://127.0.0.1:$PORT"
OLD_VENV="$WORK/old-venv"; NEW_VENV="$WORK/new-venv"

# CRITICAL: run from a neutral cwd. relaydeck's daemon re-execs `python -m
# relaydeck`, which puts cwd on sys.path[0] — so if cwd were the repo, BOTH
# the old and new daemons would import the repo's package (always the newest
# schema) instead of the installed version, silently defeating the test.
cd "$WORK"

say(){ printf '\033[36m::\033[0m %s\n' "$*"; }

install_old(){
  if [ "$METHOD" = pip ]; then
    say "pip: relaydeck==$PREV → isolated venv"
    uv venv "$OLD_VENV" >/dev/null
    uv pip install --python "$OLD_VENV/bin/python" "relaydeck==$PREV" >/dev/null
  else
    say "install.sh: relaydeck==$PREV (RELAYDECK_SOURCE pin)"
    RELAYDECK_SOURCE="relaydeck==$PREV" bash "$NEW_SRC/scripts/install.sh"
  fi
}
install_new(){
  if [ "$METHOD" = pip ]; then
    say "pip: new build from local checkout → isolated venv"
    uv venv "$NEW_VENV" >/dev/null
    uv pip install --python "$NEW_VENV/bin/python" "$NEW_SRC" >/dev/null
  else
    say "install.sh: upgrade to the local checkout (RELAYDECK_SOURCE=$NEW_SRC)"
    RELAYDECK_SOURCE="$NEW_SRC" bash "$NEW_SRC/scripts/install.sh"
  fi
}
rd_old(){ if [ "$METHOD" = pip ]; then "$OLD_VENV/bin/relaydeck" "$@"; else relaydeck "$@"; fi; }
rd_new(){ if [ "$METHOD" = pip ]; then "$NEW_VENV/bin/relaydeck" "$@"; else relaydeck "$@"; fi; }

wait_health(){
  for _ in $(seq 1 40); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/api/agents" || true)
    if [ "$code" = 200 ] || [ "$code" = 401 ]; then return 0; fi
    sleep 1
  done
  echo "daemon never became healthy on $BASE_URL" >&2
  cat "$HOME/.relaydeck/daemon.log" 2>/dev/null | tail -30 >&2 || true
  return 1
}

cleanup(){ rd_new daemon stop >/dev/null 2>&1 || true; rd_old daemon stop >/dev/null 2>&1 || true; }
trap cleanup EXIT

# ── OLD: install, boot, seed, snapshot ────────────────────────────────
install_old
say "boot OLD daemon (relaydeck $PREV) on :$PORT  HOME=$HOME"
rd_old daemon start --port "$PORT" >/dev/null
wait_health

say "seed: workspace + 2 agents"
mkdir -p "$WORK/demo"
rd_old workspace add "$WORK/demo" --name demo --plugin skills || true
PRESET_ARGS=()
if [ -n "$MODEL" ] && [ -n "${OPENROUTER_API_KEY:-}" ]; then
  say "configure a real OpenRouter model preset ($MODEL)"
  rd_old preset create mig-model --provider openrouter --model "$MODEL" || true
  PRESET_ARGS=(--config preset=mig-model)
fi
rd_old agent create alpha --type pi --workspace demo --purpose "migration seed A" ${PRESET_ARGS[@]+"${PRESET_ARGS[@]}"} || true
rd_old agent create beta  --type pi --workspace demo --purpose "migration seed B" ${PRESET_ARGS[@]+"${PRESET_ARGS[@]}"} || true

# Realistic run: only when a real model + key are present (CI). One short
# prompt produces real usage/message rows that must survive the upgrade.
if [ ${#PRESET_ARGS[@]} -gt 0 ]; then
  say "run agent alpha on the real model + one prompt"
  rd_old agent start alpha || true
  sleep 3
  rd_old agent send alpha "Reply with one word: ok" || true
  sleep 10
  rd_old agent stop alpha || true
fi

# Re-boot OLD so the orchestrator syncs the just-created specs into the
# `agents` table — that's the data the upgrade must preserve.
say "re-boot OLD daemon to sync seeded specs into the DB"
rd_old daemon stop >/dev/null 2>&1 || true
sleep 2
rd_old daemon start --port "$PORT" >/dev/null
wait_health
rd_old agent list >/dev/null 2>&1 || true

say "snapshot BEFORE"
python3 "$HERE/_migcheck.py" snapshot --db "$DB" --base-url "$BASE_URL" --out "$WORK/before.json"

say "stop OLD daemon"
rd_old daemon stop >/dev/null 2>&1 || true
sleep 2

# ── NEW: install over the same config home, boot (migrate), verify ────
install_new
say "boot NEW daemon (this checkout) on :$PORT — triggers migration"
rd_new daemon start --port "$PORT" >/dev/null
wait_health

say "VERIFY upgrade (schema → $EXPECT_SCHEMA, no data loss, agents + API intact)"
python3 "$HERE/_migcheck.py" verify --db "$DB" --base-url "$BASE_URL" \
  --before "$WORK/before.json" --expect-schema "$EXPECT_SCHEMA"
say "migration test passed (method=$METHOD, $PREV → local)"
