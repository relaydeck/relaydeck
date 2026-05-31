#!/usr/bin/env bash
# Vendor the Lit bundle the dashboard + plugin UIs build on.
#
# relaydeck is local-first — the daemon serves the dashboard and frequently
# runs with no internet. So we do NOT hot-link a Lit CDN at runtime (the same
# reasoning as scripts/gen_web_icons.py for icons). Instead this DEV-TIME
# script downloads the official single-file Lit distribution bundle and commits
# it to relaydeck/web/static/vendor/. Runtime stays 100% local.
#
# `lit-all.min.js` is Lit's all-in-one bundle (github.com/lit/dist): a single
# self-contained ES module exporting the full public API as NAMED exports —
# LitElement, ReactiveElement, html, svg, css, unsafeCSS, nothing, noChange,
# render, plus every directive (repeat, classMap, styleMap, when, unsafeHTML,
# ifDefined, ref, guard, cache, keyed, map, until, …). It deliberately omits
# the TS decorators (@customElement/@property); build-less relaydeck uses the
# static `properties` getter + customElements.define() instead — no transpile.
#
# The dashboard reaches it via the bare `lit` importmap specifier injected by
# transports/api.py (see _BARE_MODULES), so components and plugins both write:
#   import { LitElement, html, css } from 'lit';
#
# Re-run to bump the pinned version:  bash scripts/vendor_lit.sh [version]
set -euo pipefail

VERSION="${1:-v3.3.3}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/relaydeck/web/static/vendor/lit-all.min.js"
URL="https://cdn.jsdelivr.net/gh/lit/dist@${VERSION}/all/lit-all.min.js"

echo "Vendoring Lit ${VERSION} from ${URL}"
mkdir -p "$(dirname "$DEST")"
curl -fsSL -o "$DEST" "$URL"

# Sanity: must be a self-contained ESM bundle (export-only, no bare imports).
if grep -qE 'from"[^"]+"' "$DEST"; then
  echo "ERROR: vendored bundle has external imports — not self-contained" >&2
  exit 1
fi
if ! grep -q 'export{' "$DEST"; then
  echo "ERROR: vendored bundle has no exports — wrong file?" >&2
  exit 1
fi

echo "OK → $DEST ($(wc -c < "$DEST" | tr -d ' ') bytes), Lit ${VERSION}"
