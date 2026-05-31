#!/usr/bin/env python3
"""Vendor the dashboard's icon set from heroicons + simple-icons.

relaydeck is local-first — the daemon serves the dashboard and frequently runs
with no internet. So we do NOT hot-link an icon CDN at runtime; instead this
DEV-TIME script downloads exactly the icons we use and writes their path data
into a committed module (``relaydeck/web/static/icon_data.js``). Runtime stays
100% local and the inline SVG tints with ``currentColor`` for theming.

  UI icons   → heroicons (outline, stroke, currentColor) — the general glyphs.
  Brand marks→ simple-icons (single fill path + brand hex) — harnesses/providers.

Re-run after adding an icon:  uv run python scripts/gen_web_icons.py
Needs network. Reports any slug that 404s; that name keeps its hand-rolled
fallback in primitives.js (UI) or a monogram (brands).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

HEROICONS_VER = "2.1.5"
HERO_BASE = f"https://cdn.jsdelivr.net/npm/heroicons@{HEROICONS_VER}/24/outline"
# Path from jsDelivr's raw SVGs (reliable for every slug); brand hex from the
# package metadata (the cdn.simpleicons.org render service 404s on a few slugs
# e.g. openai, so we don't depend on it).
SIMPLE_ICON = "https://cdn.jsdelivr.net/npm/simple-icons@latest/icons"
SIMPLE_META = "https://cdn.jsdelivr.net/npm/simple-icons@latest/data/simple-icons.json"

# our icon NAME (stable, used across the dashboard) → heroicons outline slug.
# Names absent here keep their existing hand-rolled body in primitives.js
# (command=⌘, git=branch, coffee — heroicons has no good match).
HERO_MAP = {
    "search": "magnifying-glass", "plus": "plus", "folder_open": "folder-open",
    "folder": "folder", "home": "home", "up": "arrow-up", "cog": "cog-6-tooth",
    "bell": "bell", "workspace": "squares-2x2", "activity": "signal",
    "agent": "command-line", "diamond": "sparkles", "message": "chat-bubble-left-right",
    "handover": "arrow-uturn-right", "vault": "lock-closed", "cpu": "cpu-chip",
    "send": "paper-airplane", "play": "play", "stop": "stop", "restart": "arrow-path",
    "x": "x-mark", "chev_r": "chevron-right", "chev_d": "chevron-down",
    "arrow_r": "arrow-right", "arrow_l": "arrow-left", "edit": "pencil-square",
    "delete": "trash", "bolt": "bolt", "star": "star", "copy": "document-duplicate",
    "filter": "funnel", "inbox": "inbox", "alert": "exclamation-triangle",
    "layout": "window", "dots": "ellipsis-horizontal", "eye": "eye", "clock": "clock",
    "target": "viewfinder-circle", "sliders": "adjustments-horizontal", "check": "check",
    "plugin": "puzzle-piece", "grid": "squares-2x2", "palette": "swatch",
}

# simple-icons slugs we vendor — harness brands + model providers.
BRAND_SLUGS = [
    "claude", "openai", "cursor", "googlegemini", "google",
    "anthropic", "ollama", "deepseek", "mistralai", "openrouter",
    "meta", "x", "perplexity", "huggingface",
]

OUT = Path(__file__).resolve().parents[1] / "relaydeck" / "web" / "static" / "icon_data.js"


def fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "relaydeck-icon-gen"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {url} -> {exc}", file=sys.stderr)
        return None


def paths_from_svg(svg: str) -> list[str]:
    # heroicons d values contain no double-quotes — a plain capture is safe.
    return re.findall(r'\sd="([^"]+)"', svg)


def main() -> int:
    hero: dict[str, str] = {}
    missing_hero: list[str] = []
    print(f"heroicons @ {HEROICONS_VER}")
    for name, slug in HERO_MAP.items():
        svg = fetch(f"{HERO_BASE}/{slug}.svg")
        ds = paths_from_svg(svg) if svg else []
        if not ds:
            missing_hero.append(f"{name} ({slug})")
            continue
        hero[name] = "".join(f'<path d="{d}"/>' for d in ds)

    # slug -> brand hex, from the package metadata (one fetch).
    hexmap: dict[str, str] = {}
    meta = fetch(SIMPLE_META)
    if meta:
        try:
            for row in json.loads(meta):
                if row.get("slug") and row.get("hex"):
                    hexmap[row["slug"]] = "#" + str(row["hex"]).lstrip("#")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! metadata parse failed: {exc}", file=sys.stderr)
    # A few brands ship an icon file but no metadata row (e.g. OpenAI, whose
    # mark is monochrome) — pin those so they don't fall back to grey.
    hexmap.setdefault("openai", "#000000")

    brands: dict[str, dict[str, str]] = {}
    missing_brand: list[str] = []
    print("simple-icons")
    for slug in BRAND_SLUGS:
        svg = fetch(f"{SIMPLE_ICON}/{slug}.svg")
        ds = paths_from_svg(svg) if svg else []
        if not ds:
            missing_brand.append(slug)
            continue
        brands[slug] = {"path": ds[0], "hex": hexmap.get(slug, "#888888")}

    def dump(d: dict) -> str:
        # json.dumps escapes the inner double-quotes in `d="…"` correctly.
        return ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(d.items()))

    def dump_brands(d: dict) -> str:
        rows = []
        for k, v in sorted(d.items()):
            rows.append(f'  {json.dumps(k)}: {{ path: {json.dumps(v["path"])}, hex: {json.dumps(v["hex"])} }}')
        return ",\n".join(rows)

    OUT.write_text(
        "// icon_data.js — GENERATED by scripts/gen_web_icons.py. Do not edit by hand.\n"
        "// UI glyphs from heroicons (outline, stroke=currentColor); brand marks from\n"
        "// simple-icons (single fill path + brand hex). Vendored so the dashboard\n"
        "// renders fully offline. Re-run the script to refresh.\n\n"
        "// our icon name -> heroicons inner body (rendered stroke/currentColor by iconSVG)\n"
        "export const HEROICONS = {\n" + dump(hero) + "\n};\n\n"
        "// simple-icons slug -> { path (fill), hex (brand colour) }\n"
        "export const BRANDS = {\n" + dump_brands(brands) + "\n};\n"
    )
    print(f"\nwrote {OUT.relative_to(OUT.parents[3])}: {len(hero)} UI icons, {len(brands)} brands")
    if missing_hero:
        print("  heroicons MISSING (kept hand-rolled fallback): " + ", ".join(missing_hero))
    if missing_brand:
        print("  brands MISSING (monogram fallback): " + ", ".join(missing_brand))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
