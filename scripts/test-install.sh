#!/usr/bin/env bash
# test-install.sh — build the clean-install container and run the smoke.
# A green run means a fresh machine can `uv tool install relaydeck` and boot.
#
#   scripts/test-install.sh

set -euo pipefail
cd "$(dirname "$0")/.."   # repo root (build context)

IMAGE="relaydeck-install-test"

printf '\033[36m::\033[0m Building %s (clean uv-tool install of this checkout)…\n' "$IMAGE"
docker build -f tests/install/Dockerfile -t "$IMAGE" .

printf '\033[36m::\033[0m Running the install smoke…\n'
docker run --rm "$IMAGE"
