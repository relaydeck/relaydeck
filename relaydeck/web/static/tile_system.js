// tile_system.js — the plugin-driven tile system for the Agents lens.
//
// Concept (Design.md §11):
//   Every panel inside an agent's detail body is a "tile". Tiles are
//   contributed by plugins. The user controls *whether* each tile shows:
//
//     tab     — sub-tab in the sub-tabs row; clicking shows the tile's
//               full view in the body.
//     hidden  — not surfaced; re-enable via the Panels manager.
//
//   The `pop` state (right-cluster pill + popover) was removed — it was
//   a third concept + two overflow buckets for little gain. Everything
//   is now a tab or hidden. Legacy `pop` assignments coerce to `tab`.
//
//   Tile assignments are per-user (not per-agent), persisted via
//   /api/preferences. They override the tile's `default_state`.
//
//   The first MAX_TABS tab tiles render directly; the rest collapse
//   into a "+N ▾" overflow menu.
//
// MIGRATION NOTE — the catalog/slot/persist logic stays plain functions; only
// renderPanelsManager() moved to lit-html. It still creates a `.panels-mgr`
// <div> and RETURNS it (the agents lens body-mounts + anchors that node), but
// builds its DOM via a single render() pass with @click bindings instead of
// innerHTML + querySelector + addEventListener. mountTile() stays imperative
// (it hosts a tile's mount()/unmount() lifecycle into a cleared container).

import { html, render, nothing, icon, button } from '@relaydeck/ui';
import { esc, iconSVG } from './primitives.js';
import { api, savePreferences, stamped } from './data.js';

// Core tile catalog. Plugin-contributed tiles get merged in at boot via
// /api/plugins/ui. Each entry mirrors the plugin manifest shape.
const CORE_TILES = [
  { id: 'core:terminal',  label: 'Terminal',  icon: 'play',     source: 'core', default_state: 'tab', protected: true,  description: "Live tty mirror of the agent's harness", module: '/static/tiles/terminal.js' },
  { id: 'core:git',       label: 'Git',       icon: 'git',      source: 'core', default_state: 'tab',                    description: 'Branch, worktree, GitHub workflow, file changes', module: '/static/tiles/git.js' },
  { id: 'core:identity',  label: 'Identity',  icon: 'agent',    source: 'core', default_state: 'tab',                    description: 'Model, plugins, purpose, prompt',          module: '/static/tiles/identity.js' },
  { id: 'core:context',   label: 'Context',   icon: 'activity', source: 'core', default_state: 'tab',                    description: 'Token-usage heatmap over time',            module: '/static/tiles/context.js' },
  { id: 'core:events',    label: 'Events',    icon: 'bolt',     source: 'core', default_state: 'tab',                    description: 'Recent events emitted by this agent',      module: '/static/tiles/events.js' },
  { id: 'core:config',    label: 'Config',    icon: 'cog',      source: 'core', default_state: 'tab',                    description: 'Quick-edit model, plugins, workspace',      module: '/static/tiles/config.js' },
  { id: 'core:raw',       label: 'Raw',       icon: 'command',  source: 'core', default_state: 'hidden',                 description: 'JSON view of the agent record',            module: '/static/tiles/raw.js' },
];

// Tabs that fit before the "+N ▾" overflow. The 5 core tiles
// (Terminal/Identity/Context/Events/Config) plus the 2 messaging tiles
// (Inbox/Compose) fit at 7; the messages-lens copy points operators at the
// Compose tab so it has to be visible by default.
const MAX_TABS = 7;

let pluginTiles = [];

export function setPluginTiles(tiles) { pluginTiles = tiles || []; }

export function allTiles() {
  return [...CORE_TILES, ...pluginTiles];
}

export function visibleTilesFor(agent, tileStates, order = null) {
  // Build the per-agent effective state map: merge defaults, plugin
  // contributions, and the user's per-tile overrides. Tiles whose source
  // plugin isn't enabled for the agent's workspace are filtered out (so
  // a "github-prs" tile only shows when the workspace has github).
  const out = [];
  for (const t of allTiles()) {
    if (!_tileAppliesTo(t, agent)) continue;
    let state = tileStates[t.id] || t.default_state || 'hidden';
    // `pop` was removed — coerce legacy persisted/plugin values to `tab`
    // so those tiles stay reachable instead of silently vanishing.
    if (state === 'pop') state = 'tab';
    out.push({ ...t, state });
  }
  // Apply the user's saved order (a list of tile ids). Tiles present in
  // `order` sort by their index there; anything not listed keeps catalog
  // order and trails after. Lets the Panels manager reorder tabs.
  if (Array.isArray(order) && order.length) {
    const rank = new Map(order.map((id, i) => [id, i]));
    out.sort((a, b) => {
      const ra = rank.has(a.id) ? rank.get(a.id) : Number.MAX_SAFE_INTEGER;
      const rb = rank.has(b.id) ? rank.get(b.id) : Number.MAX_SAFE_INTEGER;
      return ra - rb;
    });
  }
  return out;
}

function _tileAppliesTo(tile, agent) {
  if (tile.source === 'core') return true;
  // Agent-type-scoped tiles (manifest `applies_to = ["relaydeck", ...]`) show
  // for matching agent types regardless of workspace plugins — this is how
  // a daemon-wide plugin (e.g. relaydeck-native) targets its own agent type
  // without cluttering every other agent's detail pane.
  if (Array.isArray(tile.applies_to) && tile.applies_to.length) {
    return tile.applies_to.includes(agent.type);
  }
  // Otherwise plugin tiles only show when the plugin is enabled in the
  // agent's workspace. agent.__workspace_plugins is provided by the host.
  const wsPlugins = agent.__workspace_plugins || [];
  return wsPlugins.includes(tile.source);
}

// Compute the visible tab slot assignment from a tile-state map.
// Returns { tabs, overflowTabs, hidden }. (`pop` was removed.)
// `maxTabs` is how many tabs render before the "+N ▾" overflow —
// user-configurable via the Panels manager, default MAX_TABS.
export function computeSlots(tiles, maxTabs = MAX_TABS) {
  const n = Math.max(1, Math.min(12, maxTabs | 0 || MAX_TABS));
  const tabs = []; const hidden = [];
  for (const t of tiles) {
    if (t.state === 'tab') tabs.push(t);
    else hidden.push(t);
  }
  return {
    tabs: tabs.slice(0, n),
    overflowTabs: tabs.slice(n),
    hidden,
  };
}

export const DEFAULT_MAX_TABS = MAX_TABS;

// ─── Tile lifecycle (load + mount) ──────────────────────────────────
// Each tile module default-exports a class with `mount(container, api, ctx)`
// and `unmount()`. The container is an empty <div>; `ctx` carries
// {agent, tile, mode: 'body' | 'pop', host}.
const tileModuleCache = new Map();

async function loadTileModule(tile) {
  if (tileModuleCache.has(tile.id)) return tileModuleCache.get(tile.id);
  const mod = await import(stamped(tile.module));
  const Klass = mod.default || mod.Tile;
  tileModuleCache.set(tile.id, Klass);
  return Klass;
}

export async function mountTile(tile, container, ctx) {
  try {
    const Klass = await loadTileModule(tile);
    if (!Klass) throw new Error('tile module has no default export');
    const inst = new Klass();
    container.innerHTML = '';
    const cleanup = await inst.mount(container, api, ctx);
    return {
      instance: inst,
      unmount: () => {
        try { inst.unmount?.(); } catch (_) {}
        if (typeof cleanup === 'function') { try { cleanup(); } catch (_) {} }
      },
    };
  } catch (e) {
    console.error('tile mount failed', tile.id, e);
    container.innerHTML = `
      <div class="tile-stub">
        <div class="tile-icon">${iconSVG('alert', 26)}</div>
        <div class="ttl">${esc(tile.label)}</div>
        <div class="src">tile failed to load · ${esc(tile.source)}</div>
        <div class="dsc">${esc(e.message || String(e))}</div>
      </div>`;
    return { unmount: () => {} };
  }
}

// ─── Panels Manager popover ──────────────────────────────────────────
// opts:
//   tilesForAgent  ordered list of {id,label,icon,source,state,...}
//   maxTabs        how many tabs show before "+N" overflow
//   onChange(id, 'tab'|'hidden')   toggle a tile's state
//   onReorder(id, -1|+1)           move a tab up/down in display order
//   onMaxTabs(n)                   set the visible-tab count
//   onClose()                      dismiss
//
// Returns the `.panels-mgr` <div> (the agents lens body-mounts + anchors it).
// Built via a single lit render() into that node — the returned element stays
// the `.panels-mgr` host the rest of the app positions, appends, and removes.
export function renderPanelsManager(opts) {
  const { onChange, onReorder, onMaxTabs, onClose, tilesForAgent } = opts;
  // Optional stat-strip section: { cells:[{id,label,hidden}], onToggle, onReorder }
  const statCfg = opts.stats || null;
  const maxTabs = Math.max(1, Math.min(12, (opts.maxTabs | 0) || DEFAULT_MAX_TABS));
  const tabs = tilesForAgent.filter(t => t.state === 'tab');   // already in order
  const hidden = tilesForAgent.filter(t => t.state !== 'tab');

  const groupLabel = (label, count) =>
    html`<div class="tile-group-label"><span>${label}</span><span class="source-chip">${count}</span></div>`;

  // ── Tabs (in display order, reorderable) ──
  const tabRow = (t, i) => {
    const overflow = i >= maxTabs;
    return html`
      <div class="tile-row${t.protected ? ' protected' : ''}${overflow ? ' overflowed' : ''}">
        <span class="reorder">
          <button data-mv="up" title="Move up" ?disabled=${i === 0} @click=${() => onReorder(t.id, -1)}>↑</button>
          <button data-mv="down" title="Move down" ?disabled=${i === tabs.length - 1} @click=${() => onReorder(t.id, +1)}>↓</button>
        </span>
        <span class="ic">${icon(t.icon || 'agent', 13)}</span>
        <div class="info">
          <div class="name">${t.label}${overflow ? html` <span class="ofl">+N</span>` : nothing}</div>
          <div class="desc">${t.source === 'core' ? (t.description || '') : t.source}</div>
        </div>
        <div class="state-seg">
          <button data-state="hidden" ?disabled=${t.protected}
            @click=${t.protected ? null : () => onChange(t.id, 'hidden')}>Hide</button>
        </div>
      </div>`;
  };

  // ── Hidden ──
  const hiddenRow = (t) => html`
    <div class="tile-row">
      <span class="ic">${icon(t.icon || 'agent', 13)}</span>
      <div class="info">
        <div class="name">${t.label}</div>
        <div class="desc">${t.source === 'core' ? (t.description || '') : t.source}</div>
      </div>
      <div class="state-seg">
        <button data-state="tab" @click=${() => onChange(t.id, 'tab')}>Show</button>
      </div>`;

  // ── Stat strip (vitals row) ──
  const statRow = (c, i) => html`
    <div class="tile-row${c.hidden ? ' overflowed' : ''}">
      <span class="reorder">
        <button data-mv="up" title="Move up" ?disabled=${i === 0} @click=${() => statCfg.onReorder(c.id, -1)}>↑</button>
        <button data-mv="down" title="Move down" ?disabled=${i === statCfg.cells.length - 1} @click=${() => statCfg.onReorder(c.id, +1)}>↓</button>
      </span>
      <span class="ic">${icon('activity', 13)}</span>
      <div class="info"><div class="name">${c.label}</div></div>
      <div class="state-seg">
        <button data-st="show" class=${c.hidden ? '' : 'on'} @click=${() => statCfg.onToggle(c.id, false)}>Show</button>
        <button data-st="hide" class=${c.hidden ? 'on hidden-on' : ''} @click=${() => statCfg.onToggle(c.id, true)}>Hide</button>
      </div>`;

  const statShown = (statCfg && Array.isArray(statCfg.cells))
    ? statCfg.cells.filter(c => !c.hidden).length : 0;

  const mgr = document.createElement('div');
  mgr.className = 'panels-mgr';
  render(html`
    <div class="panels-mgr-head">
      <h4>Panels</h4>
      <div class="sub">Reorder tabs, set how many show before "+N", or hide tiles.</div>
    </div>
    <div class="layout-summary">
      <div class="col tab"><span class="lbl">Tabs</span><span class="v">${tabs.length}</span></div>
      <div class="col"><span class="lbl">Hidden</span><span class="v">${hidden.length}</span></div>
      <div class="col maxtabs">
        <span class="lbl">Visible</span>
        <span class="stepper">
          <button data-mt="dec" title="Fewer visible tabs" @click=${() => onMaxTabs(maxTabs - 1)}>−</button>
          <span class="v" data-mt-val>${maxTabs}</span>
          <button data-mt="inc" title="More visible tabs" @click=${() => onMaxTabs(maxTabs + 1)}>+</button>
        </span>
      </div>
    </div>
    <div class="panels-mgr-body">
      ${groupLabel('Tabs · in order', tabs.length)}
      ${tabs.map((t, i) => tabRow(t, i))}
      ${hidden.length ? html`
        ${groupLabel('Hidden', hidden.length)}
        ${hidden.map((t) => hiddenRow(t))}` : nothing}
      ${(statCfg && Array.isArray(statCfg.cells)) ? html`
        ${groupLabel('Stat strip · vitals', html`${statShown} shown`)}
        ${statCfg.cells.map((c, i) => statRow(c, i))}` : nothing}
    </div>
    <div class="panels-mgr-foot">
      <span>Per-user · synced via preferences</span>
      <span class="sp"></span>
      ${button({ variant: 'ghost', size: 'sm', act: 'reset',
        onClick: () => { for (const t of tilesForAgent) onChange(t.id, t.default_state || 'hidden'); } }, 'Reset')}
      ${button({ variant: 'ghost', size: 'sm', act: 'done', onClick: onClose }, 'Done')}
    </div>`, mgr);
  return mgr;
}

// ─── Tile state persistence helpers ─────────────────────────────────
// tile_states are a flat { tileId: 'tab'|'pop'|'hidden' } map persisted
// via the preferences endpoint. We debounce writes so flipping six
// toggles in a row doesn't post six times.
let savePending = null;
let pendingPatch = {};
function _persist(patch) {
  pendingPatch = { ...pendingPatch, ...patch };
  if (savePending) clearTimeout(savePending);
  savePending = setTimeout(() => {
    savePending = null;
    const p = pendingPatch;
    pendingPatch = {};
    savePreferences(p);
  }, 300);
}
export function persistTileStates(tileStates) { _persist({ tile_states: tileStates }); }
export function persistTileOrder(order) { _persist({ tile_order: order }); }
export function persistTileMaxTabs(n) { _persist({ tile_max_tabs: n }); }
export function persistStatOrder(order) { _persist({ stat_order: order }); }
export function persistStatHidden(hidden) { _persist({ stat_hidden: hidden }); }
