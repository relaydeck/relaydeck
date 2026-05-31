// theme.js — the theme runtime.
//
// A theme is a flat map of design-token overrides (the `--*` custom
// properties from styles.css). We apply it by setting those vars INLINE
// on <html>, which wins over the :root defaults and the [data-accent]
// attribute rules — so an arbitrary user theme can override any token
// without regenerating CSS. Density + glow stay as orthogonal attributes
// ([data-density] / [data-glow]); a theme MAY also bake those tokens in.
//
// Appearance = the resolved view for a workspace (theme + density + glow
// + dashboard layout), with a global default workspaces inherit. The
// server (relaydeck/preferences.py) owns resolution; this module fetches
// it and paints.

import { api } from './data.js';

let _appliedNames = [];   // token names currently set inline (for clean revert)

/** Set the given token map as inline CSS vars on <html>, removing any
 *  previously-applied token that's not in the new map. Idempotent. */
export function applyTokens(tokenMap) {
  const root = document.documentElement;
  const next = tokenMap || {};
  for (const name of _appliedNames) {
    if (!(name in next)) root.style.removeProperty('--' + name);
  }
  _appliedNames = [];
  for (const [k, v] of Object.entries(next)) {
    root.style.setProperty('--' + k, v);
    _appliedNames.push(k);
  }
}

// Resolved-token cache, keyed by theme name. Invalidated on themes.changed.
const _resolvedCache = new Map();

export function invalidateThemeCache() { _resolvedCache.clear(); }

/** Flattened token map for a theme (after `extends`), from the server. */
export async function resolveTheme(name) {
  if (!name || name === 'base') return {};
  if (_resolvedCache.has(name)) return _resolvedCache.get(name);
  try {
    const t = await api.getJSON('/api/themes/' + encodeURIComponent(name));
    const r = (t && t.resolved) || {};
    _resolvedCache.set(name, r);
    return r;
  } catch (_) {
    return {};
  }
}

/** Fetch + apply the appearance for a workspace ('' or null = global).
 *  Paints the theme tokens, sets density/glow, and syncs app.state so the
 *  editors and Settings reflect the active values. Returns the resolved
 *  appearance object. */
export async function applyAppearance(app, workspace) {
  let ap = {};
  try {
    const q = workspace ? ('?workspace=' + encodeURIComponent(workspace)) : '';
    const r = await api.getJSON('/api/appearance' + q);
    ap = (r && r.resolved) || {};
  } catch (_) { ap = {}; }
  const themeName = ap.theme || 'base';
  const density = ap.density || 'regular';
  const glow = ap.glow || 'on';
  const tokens = await resolveTheme(themeName);
  applyTokens(tokens);
  document.documentElement.setAttribute('data-density', density);
  document.documentElement.setAttribute('data-glow', glow);
  if (app && app.state) {
    app.state.theme = themeName;
    app.state.density = density;
    app.state.glow = glow;
    app.state.appearanceScope = ap.scope || 'global';
  }
  return ap;
}

/** Persist an appearance patch (theme/density/glow/dashboard) for a
 *  workspace ('' = global) and re-paint. Used by the Appearance lens +
 *  Settings + the dashboard `dashboard.command` tool. */
export async function setAppearance(app, patch, workspace) {
  const q = workspace ? ('?workspace=' + encodeURIComponent(workspace)) : '';
  try {
    await api.putJSON('/api/appearance' + q, patch || {});
  } catch (_) { /* surfaced by caller if needed */ }
  // Re-apply only if we changed the scope the user is currently viewing.
  const active = (app && app.state && app.state.workspace) || '';
  if ((workspace || '') === active) await applyAppearance(app, active);
}
