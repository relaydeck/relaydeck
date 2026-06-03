#!/usr/bin/env bash
# test-migration.sh — run the OLD→NEW in-place upgrade migration test.
#
# With Docker: builds the pi image and runs BOTH install paths (pip +
#   install.sh website script), exactly as CI does. Set OPENROUTER_API_KEY +
#   RELAYDECK_MIG_MODEL to exercise a live agent run.
# Without Docker: runs the pip path locally in isolated venvs (no global
#   install; sandboxed HOME + a private port) so it's safe on a dev machine.
#
#   scripts/test-migration.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  printf '\033[36m::\033[0m Docker present — building pi image + running both install paths\n'
  docker build -f tests/docker/Dockerfile.base -t relaydeck-test:base .
  docker build -f tests/docker/Dockerfile.pi -t relaydeck-test:pi .
  for m in pip install_sh; do
    printf '\033[36m::\033[0m method=%s\n' "$m"
    docker run --rm \
      -e RD_METHOD="$m" \
      -e OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}" \
      -e RELAYDECK_MIG_MODEL="${RELAYDECK_MIG_MODEL:-}" \
      relaydeck-test:pi \
      bash /src/tests/migration/migration_test.sh
  done
else
  printf '\033[36m::\033[0m No Docker — running the pip path locally (isolated venvs + HOME + port)\n'
  RD_METHOD=pip bash tests/migration/migration_test.sh
fi
