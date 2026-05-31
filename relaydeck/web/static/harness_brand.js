// harness_brand.js — per-harness brand marks for the new-agent wizard.
//
// Real vendor logos come from simple-icons (vendored in icon_data.js, fill
// paths) keyed off the stable `brand` id the backend stamps on each
// /api/harnesses entry. pi / opencode / relaydeck have no simple-icon, so they
// keep a hand-drawn stroke mark; anything else falls back to a tidy MONOGRAM.
// The frontend never branches on harness *type* — declare `brand`/`accent` in
// a harness plugin's metadata and it gets a branded step for free.
//
// The glyph rides on `color: var(--brand)` (set by .na-brand from the entry
// accent), so fill- and stroke-based marks tint from one source.

import { esc } from './primitives.js';
import { BRANDS } from './icon_data.js';

// harness brand id -> simple-icons slug (fill mark). antigravity is Google's
// Gemini-powered CLI, so it borrows the Gemini mark.
const BRAND_SLUG = {
  claude: 'claude', openai: 'openai', cursor: 'cursor', antigravity: 'googlegemini',
};

// Hand-drawn stroke marks for harnesses simple-icons doesn't carry.
const CUSTOM = {
  pi: '<path d="M4.5 7.5h15"/><path d="M9 7.5v9"/><path d="M15.5 7.5v7.2c0 1.2.6 1.8 1.7 1.8"/>',
  opencode: '<path d="M8.5 8 4 12l4.5 4M15.5 8 20 12l-4.5 4M13.5 5.5l-3 13"/>',
  // rd. monogram — rounded paper tile with the terracotta accent dot,
  // echoing the favicon (relaydeck/web/static/favicon.svg).
  relaydeck: '<rect x="3.5" y="3.5" width="17" height="17" rx="5"/><path d="M8.5 15.5v-6M8.5 11.5c.6-1.3 1.6-2 3-2"/><path d="M15.5 8v7.5M15.5 15.5a2.4 2.4 0 1 1 0-4.8 2.4 2.4 0 0 1 0 4.8z"/><circle cx="17.6" cy="16.4" r="1.1" fill="currentColor" stroke="none"/>',
};
const LINKED = '<path d="M9.5 14.5 14.5 9.5"/><path d="M7.8 12.2 6 14a3.1 3.1 0 0 0 4.4 4.4l1.8-1.8M16.2 11.8 18 10a3.1 3.1 0 0 0-4.4-4.4l-1.8 1.8"/>';

// Fallback brand id + accent keyed by harness TYPE, mirroring the backend
// HARNESS_META. Resilience to version skew: the dashboard JS is served fresh
// from disk (no-cache), but `/api/harnesses` comes from the running daemon
// process — if it predates the `brand`/`accent` fields (daemon not restarted
// after an update), entries arrive without them and harnesses would fall back
// to monograms. The backend values still win; these only fill the gap so logos
// + colours render correctly on a plain browser reload.
const TYPE_BRAND = {
  'claude-code': 'claude', 'codex-cli': 'openai', 'cursor-cli': 'cursor',
  'antigravity': 'antigravity', 'pi': 'pi', 'opencode-cli': 'opencode',
  'relaydeck': 'relaydeck',
};
const TYPE_ACCENT = {
  'claude-code': '#C1623E', 'codex-cli': '#0E8C6E', 'pi': '#6D5BA8',
  'opencode-cli': '#2F6F8F', 'cursor-cli': '#5A6373', 'antigravity': '#3B6FB0',
  'relaydeck': '#B7410E',
};
// Registered harness ALIASES → canonical type. Agents persist whatever type
// they were created with (often an alias, e.g. `claude`, `agy`), so normalize
// before the brand/accent lookups.
const TYPE_ALIAS = {
  claude: 'claude-code', codex: 'codex-cli', cursor: 'cursor-cli',
  'cursor-agent': 'cursor-cli', agy: 'antigravity', opencode: 'opencode-cli',
  'pi-harness': 'pi',
};
function canonType(t) { return (t && TYPE_ALIAS[t]) || t; }

/** The harness's brand id — backend `brand`, else derived from `type` (alias-aware). */
function brandId(entry) {
  return (entry && entry.brand) || TYPE_BRAND[canonType(entry && entry.type)] || null;
}

/** The harness's brand accent colour (catalog `accent`, else by type, else UI accent). */
export function harnessAccent(entry) {
  return (entry && entry.accent) || TYPE_ACCENT[canonType(entry && entry.type)] || 'var(--acc)';
}

/**
 * Brand tile for an AGENT, keyed by its harness type — the default agent
 * avatar across the dashboard. Falls back to the agent's monogram for an
 * unknown harness. Same options as harnessMark ({ size, radius }).
 */
export function agentMark(agent, opts = {}) {
  return harnessMark(
    { type: agent && agent.type, label: (agent && (agent.name || agent.id)) || '?' },
    opts,
  );
}

function fillSVG(path, glyph) {
  return `<svg width="${glyph}" height="${glyph}" viewBox="0 0 24 24" fill="currentColor" style="display:block"><path d="${path}"/></svg>`;
}
function strokeSVG(body, glyph) {
  return `<svg width="${glyph}" height="${glyph}" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="display:block">${body}</svg>`;
}

function monogram(entry) {
  const label = (entry && entry.label) || '?';
  const words = String(label).trim().split(/[\s/_-]+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return String(label).replace(/[^a-z0-9]/gi, '').slice(0, 2).toUpperCase() || '?';
}

/** Inner SVG for the harness mark, or null when only a monogram applies. */
function harnessGlyph(entry, glyph) {
  if (!entry) return null;
  const bid = brandId(entry);
  const slug = BRAND_SLUG[bid];
  if (slug && BRANDS[slug]) return fillSVG(BRANDS[slug].path, glyph);
  if (entry.kind === 'external') return strokeSVG(LINKED, glyph);
  if (CUSTOM[bid]) return strokeSVG(CUSTOM[bid], glyph);
  return null;
}

/**
 * Brand tile HTML for a harness catalog entry.
 *   harnessMark(entry, { size, radius })
 * Real logo (simple-icons) or hand-drawn mark tinted to the harness accent, or
 * a monogram tile. The accent rides on a `--brand` custom property so .na-brand
 * tints background / border / glyph from one source.
 */
export function harnessMark(entry, { size = 40, radius = 10 } = {}) {
  const accent = harnessAccent(entry);
  // simple-icons fill the full viewbox; stroke marks have built-in padding —
  // size the fill marks a touch smaller so they read at the same weight.
  const slug = BRAND_SLUG[brandId(entry)];
  const isFill = !!(slug && BRANDS[slug]);
  const glyph = Math.round(size * (isFill ? 0.5 : 0.58));
  const inner = harnessGlyph(entry, glyph)
    || `<span class="na-brand-mono" style="font-size:${Math.round(size * 0.4)}px">${esc(monogram(entry))}</span>`;
  return `<span class="na-brand" style="--brand:${esc(accent)};width:${size}px;height:${size}px;border-radius:${radius}px">${inner}</span>`;
}
