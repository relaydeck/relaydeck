// overlays.js — Command palette, notifications drawer, settings, add-workspace
// / new-worktree modals, keyboard cheatsheet. (The new-agent wizard lives in
// new_agent.js.)
//
// MIGRATED to build-less, light-DOM Lit on @relaydeck/ui. These are imperative,
// document.body-attached overlays (not lenses, not tiles), so each builder still
// owns its scrim's lifecycle — but the BODIES are now lit-html templates
// rendered with `render()` (no innerHTML), events wired with @click/@input (no
// querySelector + addEventListener), and the kit's icon()/classMap/relTime used
// throughout (Lit auto-escapes interpolations, so no manual esc()).
// Genuinely-procedural lifecycle (debounced path probes, the directory browser,
// the plugin grid, healthz polling, the AppearanceLens mount, live theme-swatch
// resolution) stays imperative against static anchor nodes the reactive layer
// never re-renders — mirroring lenses/agents.js.
//
// PRESERVED: every export's name + signature, and the bespoke DOM classes /
// data-* attributes the rest of the app + the Playwright e2e suite rely on
// (.cmdk/.settings-overlay/.drawer/.kbd-cheatsheet/.addws-modal/.restart-modal,
// [data-act]/[data-f]/[data-s]/.theme-card[data-theme]/.set-toggle[data-name]/…).

import { html, render, nothing, classMap, icon, relTime, liveDirective } from '@relaydeck/ui';
import { AppearanceLens } from './lenses/appearance.js';
import { resolveTheme, setAppearance } from './theme.js';

// The new-agent wizard moved to its own module (it grew into a rich,
// type-first flow). Re-exported here so existing
// `import { openNewAgentModal } from './overlays.js'` callers (app.js)
// keep working unchanged.
export { openNewAgentModal } from './new_agent.js';

// Small helper: mount a bespoke-class scrim+panel pair on document.body and
// wire Esc + scrim-click close. Returns { scrim, panel, close } so the caller
// can render() lit templates into `panel` and own the rest of the lifecycle.
// (Mirrors the original hand-rolled scrim pattern, minus the boilerplate.)
function _mountScrim(panelClass, { scrimClose = true } = {}) {
  const scrim = document.createElement('div');
  scrim.className = 'overlay-scrim';
  const panel = document.createElement('div');
  panel.className = panelClass;
  document.body.appendChild(scrim);
  document.body.appendChild(panel);
  let closed = false;
  const extra = [];
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey, true);
    scrim.remove();
    panel.remove();
    for (const fn of extra) { try { fn(); } catch (_) {} }
  };
  const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); } };
  document.addEventListener('keydown', onKey, true);
  if (scrimClose) scrim.addEventListener('click', close);
  return { scrim, panel, close, onClose: (fn) => extra.push(fn) };
}

// ── Keyboard shortcuts — single source of truth ─────────────────────
// Each entry is individually toggleable in Settings → Shortcuts. Only a
// minimal set ships enabled by default (`defaultOn`); the rest are opt-in.
// app.js's key handler gates every action on host.shortcutOn(id), so this
// registry is the ONE place keys + defaults live. `locked` = can't disable.
export const SHORTCUT_DEFS = [
  { id: 'space-focus', group: 'Global', keys: ['Space'], desc: 'Focus the terminal on an agent page — otherwise open search', defaultOn: true },
  { id: 'term-focus',  group: 'Global', keys: ['/'], desc: 'Focus the terminal (on an agent page with a terminal)', defaultOn: true },
  { id: 'search',      group: 'Global', keys: ['⌘', 'K'], desc: 'Search · command palette', defaultOn: true },
  { id: 'help',        group: 'Global', keys: ['?'], desc: 'Shortcuts overview', defaultOn: true },
  { id: 'esc',         group: 'Global', keys: ['Esc'], desc: 'Close the open overlay', defaultOn: true, locked: true },
  { id: 'new-agent',   group: 'Global', keys: ['⌘', 'N'], desc: 'New agent', defaultOn: false },
  { id: 'settings',    group: 'Global', keys: ['⌘', ','], desc: 'Open settings', defaultOn: false },
  { id: 'lens-nav',    group: 'Navigate', keys: ['1'], thru: ['9'], desc: 'Switch lens by number', defaultOn: false },
  { id: 'next-agent',  group: 'Navigate', keys: ['↓'], alt: ']', desc: 'Next agent', defaultOn: false },
  { id: 'prev-agent',  group: 'Navigate', keys: ['↑'], alt: '[', desc: 'Previous agent', defaultOn: false },
];
export const SHORTCUT_DEFAULTS = Object.fromEntries(SHORTCUT_DEFS.map(s => [s.id, !!s.defaultOn]));

// Group the flat registry into [{group, items}] for the cheatsheet + settings.
function _shortcutGroups() {
  const groups = [];
  for (const it of SHORTCUT_DEFS) {
    let g = groups.find(x => x.group === it.group);
    if (!g) { g = { group: it.group, items: [] }; groups.push(g); }
    g.items.push(it);
  }
  return groups;
}

function _injectCheatsheetCSS() {
  if (document.getElementById('relaydeck-kbd-css')) return;
  const s = document.createElement('style');
  s.id = 'relaydeck-kbd-css';
  s.textContent = `
    .kbd-cheatsheet{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);width:min(560px,92vw);max-height:80vh;display:flex;flex-direction:column;background:var(--bg-1);border:1px solid var(--line-3);border-radius:var(--r-4);box-shadow:0 0 0 1px var(--line-2),0 30px 60px rgba(0,0,0,.12);z-index:120;animation:cmdk-in .14s cubic-bezier(.2,.9,.3,1)}
    .kbd-cheatsheet .kc-head{display:flex;align-items:center;justify-content:space-between;padding:16px 18px 12px;border-bottom:1px solid var(--line-1)}
    .kbd-cheatsheet .kc-head h3{margin:0;font-family:var(--f-sans);font-size:16px;font-weight:600;letter-spacing:-.01em}
    .kbd-cheatsheet .kc-head .x{background:transparent;border:0;color:var(--t-3);cursor:pointer;padding:4px}
    .kbd-cheatsheet .kc-head .x:hover{color:var(--t-1)}
    .kbd-cheatsheet .kc-body{overflow:auto;padding:8px 18px 16px}
    .kbd-cheatsheet .kc-group{margin-top:12px}
    .kbd-cheatsheet .kc-group .lbl{font-family:var(--f-mono);font-size:9px;letter-spacing:.16em;text-transform:uppercase;color:var(--t-4);margin:8px 0 4px}
    .kbd-cheatsheet .kc-row{display:flex;align-items:center;gap:12px;padding:5px 0;font-size:var(--t-sm);color:var(--t-1)}
    .kbd-cheatsheet .kc-row .keys{display:flex;gap:4px;align-items:center;flex-shrink:0;min-width:120px}
    .kbd-cheatsheet .kc-row .desc{color:var(--t-2);font-size:var(--t-xs)}
    .kbd-cheatsheet kbd{font-family:var(--f-mono);font-size:10px;color:var(--t-1);background:var(--bg-2);border:1px solid var(--line-2);border-bottom-color:var(--line-3);padding:2px 6px;border-radius:4px;min-width:18px;text-align:center}
    .kbd-cheatsheet .sep{color:var(--t-4);font-size:10px}
    .kbd-cheatsheet .kc-foot{padding:10px 18px;border-top:1px solid var(--line-1);font-family:var(--f-mono);font-size:10px;color:var(--t-4)}
  `;
  document.head.appendChild(s);
}

// Render one shortcut's key glyphs as a lit fragment (keys / thru-range / alts).
// `or` is the separator between key variants: "or" in the cheatsheet, "/" in
// the settings list. Thru-ranges always use an en dash.
function _keyGlyphs(it, { or = 'or' } = {}) {
  const kb = (k) => html`<kbd>${k}</kbd>`;
  return html`${it.keys.map(kb)}${it.thru ? html`<span class="sep">–</span>${it.thru.map(kb)}` : nothing}${
    it.alt ? html`<span class="sep">${or}</span>${kb(it.alt)}` : nothing}${
    it.alt2 ? html`<span class="sep">${or}</span>${it.alt2.map(kb)}` : nothing}`;
}

export function openShortcutsCheatsheet(host) {
  const existing = document.querySelector('.kbd-cheatsheet');
  if (existing) { existing.parentNode && existing._close(); return; }
  _injectCheatsheetCSS();
  const { panel, close, onClose } = _mountScrim('kbd-cheatsheet rd');
  panel._close = close;
  // Cheatsheet uses "or" between key variants.
  const keys = (it) => _keyGlyphs(it, { or: 'or' });
  const on = (id) => (host.shortcutOn ? host.shortcutOn(id) : true);
  render(html`
    <div class="kc-head">
      <h3>Keyboard shortcuts</h3>
      <button class="x" data-act="close" @click=${close}>${icon('x', 16)}</button>
    </div>
    <div class="kc-body">
      ${_shortcutGroups().map(g => html`
        <div class="kc-group">
          <div class="lbl">${g.group}</div>
          ${g.items.map(it => html`<div class="kc-row" style=${on(it.id) ? '' : 'opacity:.4'}>
            <span class="keys">${keys(it)}</span>
            <span class="desc">${it.desc}${on(it.id) ? nothing : html` <span style="color:var(--t-4)">· off</span>`}</span>
          </div>`)}
        </div>`)}
    </div>
    <div class="kc-foot">Enable or disable any of these in <kbd>Settings → Shortcuts</kbd>. Shortcuts pause while typing in a field or the terminal.</div>`, panel);
  // The cheatsheet additionally closes on "?" (toggle), alongside the Esc +
  // scrim-click handled by _mountScrim. Registered for teardown via onClose.
  const onQ = (e) => { if (e.key === '?') { e.preventDefault(); e.stopPropagation(); close(); } };
  document.addEventListener('keydown', onQ, true);
  onClose(() => document.removeEventListener('keydown', onQ, true));
}

// ── Settings → Shortcuts (per-shortcut enable/disable) ──────────────
function _injectShortcutsCSS() {
  if (document.getElementById('relaydeck-sc-css')) return;
  const s = document.createElement('style');
  s.id = 'relaydeck-sc-css';
  s.textContent = `
    .sc-list{margin-top:14px;border:1px solid var(--line-3);border-radius:var(--r-1);overflow:hidden}
    .sc-grp-lbl{font-family:var(--f-mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--t-3);padding:10px 14px 4px;background:var(--bg-2)}
    .sc-row{display:grid;grid-template-columns:160px 1fr auto;gap:14px;align-items:center;padding:10px 14px;border-top:1px solid var(--line-1)}
    .sc-keys{display:flex;gap:4px;align-items:center;flex-wrap:wrap}
    .sc-keys kbd{font-family:var(--f-mono);font-size:10px;color:var(--t-1);background:var(--bg-2);border:1px solid var(--line-3);padding:2px 6px;border-radius:4px;min-width:18px;text-align:center}
    .sc-keys .sep{color:var(--t-4);font-size:10px}
    .sc-desc{font-size:var(--t-sm);color:var(--t-2)}
    .sc-lock{font-family:var(--f-mono);font-size:9px;color:var(--t-4);letter-spacing:.04em;margin-left:6px}
    .sc-tog{width:34px;height:18px;border-radius:9px;background:var(--t-4);position:relative;cursor:pointer;border:0;transition:background .15s;flex:0 0 auto;padding:0}
    .sc-tog.on{background:var(--acc)}
    .sc-tog.locked{opacity:.5;cursor:default}
    .sc-tog .knob{position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--bg-1);transition:left .15s}
    .sc-tog.on .knob{left:18px}
  `;
  document.head.appendChild(s);
}

export function renderShortcutsSection(container, host) {
  _injectShortcutsCSS();
  // Settings list uses "/" between key variants.
  const keys = (it) => _keyGlyphs(it, { or: '/' });
  const paint = () => {
    render(html`
      <div class="settings-section">
        <h3>Keyboard shortcuts</h3>
        <div class="sub">A minimal set ships enabled. Toggle any on or off — saved per browser. Shortcuts pause while you're typing in a field or the terminal.</div>
        <div class="sc-list">
          ${_shortcutGroups().map(g => html`
            <div class="sc-grp-lbl">${g.group}</div>
            ${g.items.map(it => {
              const on = host.shortcutOn(it.id);
              return html`<div class="sc-row">
                <div class="sc-keys">${keys(it)}</div>
                <div class="sc-desc">${it.desc}${it.locked ? html`<span class="sc-lock">always on</span>` : nothing}</div>
                <button class=${classMap({ 'sc-tog': true, on, locked: !!it.locked })}
                  data-id=${it.id} ?disabled=${!!it.locked} role="switch" aria-checked=${on}
                  @click=${it.locked ? null : () => { host.toggleShortcut(it.id); paint(); }}>
                  <span class="knob"></span>
                </button>
              </div>`;
            })}`)}
        </div>
        <div style="margin-top:14px"><button class="btn sm" data-act="reset"
          @click=${() => { host.resetShortcuts(); paint(); }}>Reset to defaults</button></div>
      </div>`, container);
  };
  paint();
}

// ── Settings → Appearance: simple theme picker ──────────────────────
function _injectThemeCardCSS() {
  if (document.getElementById('relaydeck-tc-css')) return;
  const s = document.createElement('style');
  s.id = 'relaydeck-tc-css';
  s.textContent = `
    .theme-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin:8px 0 18px}
    .theme-card{display:flex;align-items:center;gap:9px;padding:10px 12px;border:1px solid var(--line-3);
      border-radius:var(--r-1);background:var(--bg-1);cursor:pointer;text-align:left;
      font-family:var(--f-sans);font-size:var(--t-sm);color:var(--t-1);position:relative}
    .theme-card:hover{border-color:var(--line-4);background:var(--bg-2)}
    .theme-card.on{border-color:var(--acc);background:var(--acc-soft)}
    .theme-card .sw{width:20px;height:20px;border-radius:5px;border:1px solid var(--line-3);flex:0 0 auto;
      position:relative;overflow:hidden}
    .theme-card .sw::after{content:"";position:absolute;right:0;bottom:0;width:9px;height:9px;
      border-top-left-radius:4px;background:var(--sw-acc,var(--acc))}
    .theme-card .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .theme-card .bi{font-family:var(--f-mono);font-size:8px;letter-spacing:.08em;text-transform:uppercase;color:var(--t-4)}
    .theme-card .ck{color:var(--acc);font-weight:700;flex:0 0 auto;font-size:11px;line-height:1}
  `;
  document.head.appendChild(s);
}

async function renderThemePicker(container, host, refresh) {
  _injectThemeCardCSS();
  let themes = [];
  try { themes = await host.api.getJSON('/api/themes'); } catch (_) {}
  const scope = (host.state.appearanceScope === 'workspace' && host.state.workspace)
    ? host.state.workspace : '';
  let resolved = {};
  try {
    resolved = (await host.api.getJSON('/api/appearance' + (scope ? '?workspace=' + encodeURIComponent(scope) : ''))).resolved || {};
  } catch (_) {}
  const active = resolved.theme || 'base';
  const density = resolved.density || host.state.density || 'regular';
  const glow = resolved.glow || host.state.glow || 'on';
  const ws = host.state.workspace;
  const apply = async (patch) => { await setAppearance(host, patch, scope || undefined); host.render(); refresh(); };

  // A segmented control row (Scope / Density / Glow). seg-i[data-v] preserved.
  const seg = (k, items) => html`<div class="ctrl"><div class="seg" data-k=${k}>
    ${items.map(o => html`<button class=${classMap({ 'seg-i': true, on: o.on })} data-v=${o.v}
      ?disabled=${!!o.disabled} @click=${o.onClick}>${o.label}</button>`)}
  </div></div>`;

  render(html`
    <div class="set-row" style="border:0;padding-top:0">
      <div><div class="lbl">Scope</div><div class="desc">Set for all workspaces, or override just this one.</div></div><div></div>
      ${seg('scope', [
        { v: 'global', label: 'All workspaces', on: scope === '', onClick: () => { host.state.appearanceScope = 'global'; refresh(); } },
        { v: 'workspace', label: html`@${ws || '—'}`, on: scope !== '', disabled: !ws, onClick: () => { host.state.appearanceScope = 'workspace'; refresh(); } },
      ])}
    </div>
    <div class="theme-cards" data-theme-cards></div>
    <div class="set-row">
      <div><div class="lbl">Density</div><div class="desc">Row heights, padding, sidebar width.</div></div><div></div>
      ${seg('density', [
        { v: 'compact', label: 'Compact', on: density === 'compact', onClick: () => { host.state.density = 'compact'; apply({ density: 'compact' }); } },
        { v: 'regular', label: 'Regular', on: density === 'regular', onClick: () => { host.state.density = 'regular'; apply({ density: 'regular' }); } },
        { v: 'comfy', label: 'Comfy', on: density === 'comfy', onClick: () => { host.state.density = 'comfy'; apply({ density: 'comfy' }); } },
      ])}
    </div>
    <div class="set-row">
      <div><div class="lbl">Glow</div><div class="desc">Soft accent glow — off in the paper redesign.</div></div><div></div>
      ${seg('glow', [
        { v: 'on', label: 'On', on: glow === 'on', onClick: () => { host.state.glow = 'on'; apply({ glow: 'on' }); } },
        { v: 'off', label: 'Off', on: glow === 'off', onClick: () => { host.state.glow = 'off'; apply({ glow: 'off' }); } },
      ])}
    </div>
    <div class="set-row">
      <div><div class="lbl">Daemon</div><div class="desc">Restart relaydeck. Interrupts running agents, terminals, and event streams.</div></div><div></div>
      <div class="ctrl"><button class="btn" data-act="restart-daemon" @click=${() => host.openRestart()}>${icon('restart', 11)} Restart daemon…</button></div>
    </div>`, container);

  // Theme cards: card list is reactive, but each swatch's resolved canvas +
  // accent are filled IMPERATIVELY (async, cached) into the rendered nodes.
  const cards = container.querySelector('[data-theme-cards]');
  render(html`${themes.map(t => html`
    <button class=${classMap({ 'theme-card': true, on: t.name === active })} data-theme=${t.name}
      @click=${() => apply({ theme: t.name })}>
      <span class="sw" data-sw=${t.name}></span>
      <span class="nm">${t.label || t.name}</span>
      ${t.builtin ? html`<span class="bi">built-in</span>` : nothing}
      ${t.name === active ? html`<span class="ck">✓</span>` : nothing}
    </button>`)}`, cards);
  const rootCS = getComputedStyle(document.documentElement);
  for (const t of themes) {
    const sel = (window.CSS && CSS.escape) ? CSS.escape(t.name) : t.name;
    const sw = cards.querySelector(`[data-sw="${sel}"]`);
    if (!sw) continue;
    resolveTheme(t.name).then(toks => {
      sw.style.background = toks['bg-0'] || rootCS.getPropertyValue('--bg-0').trim();
      sw.style.setProperty('--sw-acc', toks.acc || rootCS.getPropertyValue('--acc').trim());
    }).catch(() => {});
  }
}

// ── Daemon restart (responsible: warn before interrupting) ──────────
export function openRestartModal(host) {
  if (document.querySelector('.restart-modal')) return;
  const { panel, close } = _mountScrim('restart-modal');
  panel.style.cssText = `
    position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);
    width:min(480px,92vw);background:var(--bg-1);border:1px solid var(--line-3);
    border-radius:var(--r-4);box-shadow:0 0 0 1px var(--line-2),0 30px 60px rgba(0,0,0,.12);
    z-index:130;padding:20px;animation:cmdk-in .14s cubic-bezier(.2,.9,.3,1);`;

  // Static shell + a [data-body] anchor we re-render into as the flow advances
  // (checking → unmanaged | confirm | restarting). The polling loop is
  // procedural and registered for teardown via the scrim's onClose.
  render(html`
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">
      <div style="font-size:var(--t-xl);font-weight:600">Restart daemon</div>
      <button class="btn icon" data-act="close" @click=${close}>${icon('x', 12)}</button>
    </div>
    <div data-body style="color:var(--t-2);font-size:var(--t-sm);line-height:1.5"></div>`, panel);
  const body = panel.querySelector('[data-body]');
  const paintBody = (tpl) => render(tpl, body);

  paintBody(html`<div style="color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs)">checking what will be interrupted…</div>`);

  (async () => {
    let info;
    try {
      const r = await host.api.fetch('/api/daemon/restart-info');
      info = await r.json();
    } catch (e) {
      paintBody(html`<div style="color:var(--err);font-family:var(--f-mono);font-size:var(--t-xs)">Couldn't reach the daemon: ${e.message}</div>`);
      return;
    }
    if (!info.managed) {
      paintBody(html`
        <div style="background:var(--warn-soft,rgba(245,200,90,.12));border:1px solid rgba(245,200,90,.35);border-radius:var(--r-2);padding:10px 12px;font-size:var(--t-xs)">
          This daemon isn't under <code>relaydeck daemon</code> supervision (it's running in the foreground, no PID file), so it can't restart itself from the web. Restart it from your terminal:
          <div style="margin-top:6px;font-family:var(--f-mono);color:var(--acc)">relaydeck daemon stop && relaydeck daemon start</div>
        </div>
        <div style="display:flex;justify-content:flex-end;margin-top:14px"><button class="btn ghost" data-act="cancel" @click=${close}>Close</button></div>`);
      return;
    }
    const n = info.running_agent_count || 0;
    const agentsLine = n
      ? html`<b style="color:var(--warn)">${n} running agent${n === 1 ? '' : 's'}</b> will be stopped${
          info.running_agents?.length ? html` (${info.running_agents.slice(0, 6).join(', ')}${info.running_agents.length > 6 ? '…' : ''})` : nothing}.`
      : 'No agents are currently running.';
    let errMsg = '';
    let busy = false;
    const paintConfirm = () => paintBody(html`
      <div style="background:var(--warn-soft,rgba(245,200,90,.12));border:1px solid rgba(245,200,90,.35);border-radius:var(--r-2);padding:10px 12px;font-size:var(--t-sm);line-height:1.5">
        ${info.warning || ''}
      </div>
      <ul style="margin:12px 0 0;padding-left:18px;font-size:var(--t-xs);color:var(--t-2);line-height:1.7">
        <li>${agentsLine}</li>
        <li>Live terminals + event streams will disconnect and need to reconnect.</li>
        <li>Agents with <code>auto_start</code> come back automatically; others stay stopped.</li>
      </ul>
      ${errMsg ? html`<div style="color:var(--err);font-size:var(--t-xs);font-family:var(--f-mono);margin-top:10px">${errMsg}</div>` : nothing}
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        <button class="btn ghost" data-act="cancel" @click=${close}>Cancel</button>
        <button class="btn" style="border-color:var(--warn);color:var(--warn)" data-act="confirm"
          ?disabled=${busy} @click=${doRestart}>${icon('restart', 11)} Restart now</button>
      </div>`);
    async function doRestart() {
      busy = true; errMsg = ''; paintConfirm();
      try {
        const r = await host.api.fetch('/api/daemon/restart', { method: 'POST' });
        if (!r.ok) {
          let msg = `Restart failed (${r.status})`;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          errMsg = msg; busy = false; paintConfirm();
          return;
        }
      } catch (e) { errMsg = 'Restart failed: ' + e.message; busy = false; paintConfirm(); return; }
      paintBody(html`
        <div style="text-align:center;padding:18px 0">
          <div style="font-size:var(--t-lg);color:var(--acc);margin-bottom:8px">Restarting…</div>
          <div style="color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs)">The daemon is going down for a moment. This page will reconnect automatically.</div>
        </div>`);
      // Poll /healthz until it comes back, then reload to pick up the new build
      // stamp. The interval is procedural — register teardown on close.
      let tries = 0;
      const poll = setInterval(async () => {
        tries++;
        try {
          const hr = await fetch('/healthz', { cache: 'no-store' });
          if (hr.ok) { clearInterval(poll); setTimeout(() => location.reload(), 400); return; }
        } catch (_) {}
        if (tries > 40) { clearInterval(poll); location.reload(); }
      }, 700);
    }
    paintConfirm();
  })();
}

// ── Command palette ─────────────────────────────────────────────────
function workspaceNameFromPath(path) {
  const parts = String(path || '').split(/[\\/]+/).filter(Boolean);
  const raw = (parts[parts.length - 1] || 'workspace').trim();
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48) || 'workspace';
}

function uniqueWorkspaceName(base, workspaces) {
  const used = new Set((workspaces || []).map(w => w.name));
  if (!used.has(base)) return base;
  for (let i = 2; i < 1000; i++) {
    const next = `${base}-${i}`;
    if (!used.has(next)) return next;
  }
  return `${base}-${Date.now()}`;
}

function shortPluginKind(p) {
  if (p.kind === 'harness-gate') return 'gate';
  if (p.globally_enabled === false) return 'off';
  return p.category || p.source || '';
}

export function openAddWorkspaceModal(host, onDone) {
  if (document.querySelector('.addws-modal')) return;
  const { scrim, panel: modal, close } = _mountScrim('addws-modal');
  // The onboarding wizard's scrim+modal sit at z-index 1000 (inline styles in
  // onboarding.js). The addws-modal's class CSS pins it at z-index: 130, so when
  // the wizard opens this modal (step 2's "Browse + add workspace" CTA), the
  // modal would land BEHIND the wizard. Inline-bump above the wizard so this
  // modal always wins regardless of who launched it.
  scrim.style.zIndex = '1090';
  modal.style.zIndex = '1100';
  const state = {
    path: '',
    name: '',
    browsePath: '',
    nameTouched: false,
    plugins: new Set(),
    pluginCatalog: [],
    setActive: true,
    exists: null,  // null=unknown, true, false (folder will be created on add)
    pathStatus: undefined,  // last /api/fs/browse response (or null/undefined)
    discOpen: { browse: false, adv: false },
    parentDisabled: true,
  };

  // The modal's outer chrome + flow is one lit template re-rendered on state
  // change. Path + name are fully controlled (.value bound to state, so caret
  // is preserved across re-renders). The directory list ([data-dirs]) is the
  // only IMPERATIVE region (rows come from async browse() calls), kept in a
  // static anchor the reactive layer never owns.
  const els = {};
  function paint() {
    const name = (state.name || '').trim();
    const willCreate = state.exists === false && !!state.path;
    render(html`
      <div class="addws-head">
        <div>
          <div class="addws-title">Add workspace</div>
          <div class="addws-sub">Register a folder on the daemon host. It's created if it doesn't exist.</div>
        </div>
        <button class="btn icon" data-act="close" @click=${close}>${icon('x', 12)}</button>
      </div>
      <div class="addws-body addws-flow">
        <div class="addws-label"><span>Folder on daemon host</span><span data-name-mode>${state.nameTouched ? 'custom name' : 'auto name'}</span></div>
        <div class="addws-path-row">
          <input data-path placeholder="/abs/path/on/daemon/host" spellcheck="false" .value=${liveDirective(state.path)} @input=${onPathInput}>
          <button class="btn" data-act="browse" @click=${onBrowseClick}>${icon('folder_open', 12)} Browse</button>
        </div>
        <div class="addws-status" data-status>${_pathStatusTpl(state.pathStatus)}</div>

        <div class="addws-label" style="margin-top:14px"><span>Name</span></div>
        <div class="addws-name-row">
          <input data-name placeholder="workspace-name" spellcheck="false" .value=${liveDirective(state.name)} @input=${onNameInput}>
        </div>

        <div class="addws-disc" data-disc="browse">
          <button class="addws-disc-head ${state.discOpen.browse ? 'open' : ''}" data-disc-toggle="browse"
            @click=${() => toggleDisc('browse')}>
            <span class="chev">${icon('chev_r', 11)}</span>
            <span class="lbl">Browse folders</span>
            <span class="addws-disc-meta" data-current>${state.browsePath || '~'}</span>
          </button>
          <div class="addws-disc-body" data-disc-body="browse" ?hidden=${!state.discOpen.browse}>
            <div class="addws-browser">
              <div class="addws-browser-bar">
                <button class="btn icon sm" data-act="home" title="Home" @click=${() => browse('', { syncInput: false })}>${icon('home', 12)}</button>
                <button class="btn icon sm" data-act="parent" title="Parent" ?disabled=${state.parentDisabled} @click=${onParentClick}>${icon('up', 12)}</button>
                <div class="addws-current"><span data-current-bar>${state.browsePath || 'loading...'}</span></div>
              </div>
              <!-- Imperative anchor: browse() owns this subtree (async dir
                   rows + navigation), so paint() must never render into it. -->
              <div class="addws-dir-list" data-dirs></div>
            </div>
          </div>
        </div>

        <div class="addws-disc" data-disc="adv">
          <button class="addws-disc-head ${state.discOpen.adv ? 'open' : ''}" data-disc-toggle="adv"
            @click=${() => toggleDisc('adv')}>
            <span class="chev">${icon('chev_r', 11)}</span>
            <span class="lbl">Advanced</span>
            <span class="addws-disc-meta">plugins · <span data-plugin-count>${state.plugins.size}</span></span>
          </button>
          <div class="addws-disc-body" data-disc-body="adv" ?hidden=${!state.discOpen.adv}>
            <div class="addws-plugin-tools">
              <button class="btn sm" data-preset="recommended" @click=${() => applyPreset('recommended')}>${icon('bolt', 10)} Recommended</button>
              <button class="btn sm" data-preset="all" @click=${() => applyPreset('all')}>All</button>
              <button class="btn ghost sm" data-preset="none" @click=${() => applyPreset('none')}>None</button>
            </div>
            <div class="addws-plugins" data-plugins>${_pluginsTpl()}</div>
          </div>
        </div>

        <div class="addws-settings">
          <div class="addws-setting ${state.setActive ? 'on' : ''}" data-setting="set-active"
            @click=${() => { state.setActive = !state.setActive; paint(); }}>
            <div class="addws-switch"></div>
            <div>
              <div class="sname">Set active workspace</div>
              <div class="sdesc">Switch the dashboard to the new workspace after adding it.</div>
            </div>
          </div>
        </div>
        <div data-err class="addws-err" style=${state.error ? '' : 'display:none'}>${state.error || ''}</div>
      </div>
      <div class="addws-foot">
        <div class="addws-count" data-foot>${state.path ? html`${name || 'workspace'} -> ${state.path}` : 'Type or pick a folder to continue.'}</div>
        <div class="addws-actions">
          <button class="btn ghost" data-act="cancel" @click=${close}>Cancel</button>
          <button class="btn primary" data-act="confirm" @click=${onConfirm}>${icon('plus', 11)} ${willCreate ? 'Create & add' : 'Add workspace'}</button>
        </div>
      </div>`, modal);
    // Cache the static anchors lit reuses across renders (path/name/dir list).
    els.path = modal.querySelector('[data-path]');
    els.name = modal.querySelector('[data-name]');
    els.dirs = modal.querySelector('[data-dirs]');
  }

  function _pathStatusTpl(data) {
    // Status line under the path input. `data` is /api/fs/browse's response
    // when the path resolves (✓ exists · ✓ writable · ✓ git repo · ⚠ already a
    // workspace). `null` → the path doesn't exist yet (created on add). `undefined`
    // clears the line (empty input).
    if (data === undefined) return nothing;
    if (!data) {
      return html`<span class="addws-will-create">${icon('plus', 10)} folder doesn't exist — it'll be created on add</span>`;
    }
    const chip = (color, body) => html`<span class="chip" style="color:var(--${color});background:var(--bg-2)">${body}</span>`;
    return html`
      ${chip('ok', '✓ exists')}
      ${data.writable ? chip('ok', '✓ writable') : chip('warn', '⚠ read-only')}
      ${data.is_git_repo ? chip('ok', '✓ git repo') : nothing}
      ${data.existing_workspace ? chip('warn', html`⚠ already workspace <b>${data.existing_workspace}</b>`) : nothing}`;
  }

  function _pluginsTpl() {
    const catalog = state.pluginCatalog || [];
    if (!catalog.length) return html`<div class="addws-empty">No workspace plugins available.</div>`;
    return catalog.map(p => {
      const on = state.plugins.has(p.name);
      const disabled = p.globally_enabled === false;
      return html`<div class=${classMap({ 'addws-plugin': true, on, disabled })} data-plugin=${p.name}
        @click=${() => {
          if (disabled) return;
          if (state.plugins.has(p.name)) state.plugins.delete(p.name);
          else state.plugins.add(p.name);
          paint();
        }}>
        <div class="addws-switch"></div>
        <div style="min-width:0">
          <div class="pname">${p.name}</div>
          <div class="pdesc">${disabled ? 'Disabled daemon-wide.' : (p.description || '')}</div>
        </div>
        <div class="pkind">${shortPluginKind(p)}</div>
      </div>`;
    });
  }

  function setError(msg) { state.error = msg || ''; paint(); }

  function refreshDerivedName() {
    if (state.nameTouched) return;
    const base = workspaceNameFromPath(state.path);
    state.name = state.path ? uniqueWorkspaceName(base, host.state.workspaces || []) : '';
  }

  function setPath(path, { browse: fromBrowse = false } = {}) {
    state.path = path || '';
    if (fromBrowse) state.browsePath = state.path;
    refreshDerivedName();
    paint();
  }

  function renderPathStatus(data) {
    if (data === undefined) { state.exists = null; }
    else if (!data) { state.exists = false; }
    else { state.exists = true; }
    state.pathStatus = data;
    paint();
  }

  function applyPreset(kind) {
    const enabledNames = (state.pluginCatalog || [])
      .filter(p => p.globally_enabled !== false)
      .map(p => p.name);
    if (kind === 'none') state.plugins.clear();
    else if (kind === 'all') state.plugins = new Set(enabledNames);
    else {
      const preferred = ['messaging', 'recipes', 'skills', 'fleet-context'];
      state.plugins = new Set(preferred.filter(p => enabledNames.includes(p)));
    }
    paint();
  }

  async function loadPlugins() {
    try {
      let r = await host.api.fetch('/api/workspace-plugins');
      let plugins = r.ok ? await r.json() : [];
      if (!plugins.length) {
        r = await host.api.fetch('/api/plugins');
        plugins = r.ok ? (await r.json()).filter(p => p.workspace_scoped) : [];
      }
      state.pluginCatalog = (plugins || []).sort((a, b) => String(a.name).localeCompare(String(b.name)));
    } catch (_) {
      state.pluginCatalog = [];
    }
    // Pre-select the Recommended preset on initial load so the operator doesn't
    // see an all-off grid. They can still override via None/All or per-row.
    // Only fires when state.plugins is genuinely empty (initial open).
    if (state.plugins.size === 0) applyPreset('recommended');
    else paint();
  }

  // The directory list rows are populated IMPERATIVELY into [data-dirs] —
  // browse() is async + the rows fire navigation, so keep them out of the
  // reactive path (parity with the original querySelector loop).
  async function browse(path, { syncInput = true } = {}) {
    const target = (path || '').trim();
    const dirsEl = els.dirs || modal.querySelector('[data-dirs]');
    if (dirsEl) render(html`<div class="addws-empty">loading directories...</div>`, dirsEl);
    try {
      const url = '/api/fs/browse' + (target ? `?path=${encodeURIComponent(target)}` : '');
      const r = await host.api.fetch(url);
      if (!r.ok) throw new Error('browse failed');
      const data = await r.json();
      state.browsePath = data.path || '';
      state.parentDisabled = !data.parent;
      if (syncInput) { setPath(state.browsePath, { browse: true }); renderPathStatus(data); }
      else paint();
      const list = els.dirs || modal.querySelector('[data-dirs]');
      if (list) {
        if (!data.entries || !data.entries.length) {
          render(html`<div class="addws-empty">No child directories.</div>`, list);
          return true;
        }
        render(html`${data.entries.map(entry => html`
          <div class="addws-dir-row" @click=${() => browse(entry.path, { syncInput: true })}>
            <span class="ic">${icon('folder', 13)}</span>
            <span class="name">${entry.name}</span>
            <span class="chev">${icon('chev_r', 12)}</span>
          </div>`)}`, list);
      }
      return true;
    } catch (e) {
      const list = els.dirs || modal.querySelector('[data-dirs]');
      if (list) render(html`<div class="addws-empty">folder not found</div>`, list);
      return false;
    }
  }

  // Debounced revalidation when the operator types a path manually, with an
  // epoch guard so a slow in-flight fetch for an older value can't stomp a
  // newer one.
  let _pathDebounce = null;
  let _pathFetchEpoch = 0;
  function onPathInput(e) {
    setPath(e.target.value.trim());
    clearTimeout(_pathDebounce);
    const target = e.target.value.trim();
    if (!target) { renderPathStatus(undefined); return; }
    _pathDebounce = setTimeout(async () => {
      const myEpoch = ++_pathFetchEpoch;
      try {
        const r = await host.api.fetch('/api/fs/browse?path=' + encodeURIComponent(target));
        if (myEpoch !== _pathFetchEpoch) return;  // a newer query superseded us
        if (!r.ok) { renderPathStatus(null); return; }
        const data = await r.json();
        if (myEpoch !== _pathFetchEpoch) return;
        renderPathStatus(data);
      } catch (_) {
        if (myEpoch === _pathFetchEpoch) renderPathStatus(null);
      }
    }, 350);
  }
  function onNameInput(e) { state.nameTouched = true; state.name = e.target.value; paint(); }

  function toggleDisc(key) { state.discOpen[key] = !state.discOpen[key]; paint(); }

  async function onBrowseClick() {
    state.discOpen.browse = true;
    paint();
    // Browse the typed path if it resolves; else fall back to home.
    const ok = await browse(state.path || state.browsePath || '', { syncInput: false });
    if (!ok) await browse('', { syncInput: false });
  }

  async function onParentClick() {
    try {
      const r = await host.api.fetch('/api/fs/browse?path=' + encodeURIComponent(state.browsePath || ''));
      const data = r.ok ? await r.json() : null;
      if (data && data.parent) browse(data.parent, { syncInput: false });
    } catch (_) {}
  }

  async function onConfirm(e) {
    const name = (state.name || '').trim();
    const path = (state.path || '').trim();
    const plugins = [...state.plugins];
    if (!name || !path) { setError('name and path are required'); return; }
    const btn = e.currentTarget; btn.disabled = true;
    try {
      const r = await host.api.fetch('/api/workspaces', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, path, plugins, create_dir: state.exists === false }),
      });
      if (!r.ok) {
        let msg = `Failed (${r.status})`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        setError(msg); btn.disabled = false; return;
      }
      if (state.setActive) {
        try {
          await host.api.fetch('/api/state/active-workspace', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
          });
        } catch (_) {}
      }
    } catch (err) { setError(err.message); btn.disabled = false; return; }
    close();
    if (typeof onDone === 'function') onDone(name, { setActive: state.setActive });
  }

  paint();
  els.path?.focus();
  loadPlugins();
  browse('', { syncInput: false });
}


export function openNewWorktreeModal(host, onDone, prefill = {}) {
  if (document.querySelector('.newwt-modal')) return;
  const { panel: modal, close } = _mountScrim('newwt-modal');
  modal.style.cssText = `
    position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);
    width:min(540px,92vw);background:var(--bg-1);border:1px solid var(--line-3);
    border-radius:var(--r-4);box-shadow:0 0 0 1px var(--line-2),0 30px 60px rgba(0,0,0,.12);
    z-index:130;padding:20px;animation:cmdk-in .14s cubic-bezier(.2,.9,.3,1);`;
  const INP = 'width:100%;background:var(--bg-2);border:1px solid var(--line-2);color:var(--t-1);font:400 var(--t-sm) var(--f-mono);padding:7px 9px;border-radius:4px;margin-top:4px';
  const LBL = 'display:block;margin-top:12px;font:600 var(--t-xs)/1 var(--f-mono);color:var(--t-3);text-transform:uppercase;letter-spacing:.06em';

  let errTpl = nothing;
  let errColor = 'var(--err)';
  const els = {};
  function paint() {
    render(html`
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">
        <div style="font-size:var(--t-xl);font-weight:600">New worktree</div>
        <button class="btn icon" data-act="close" @click=${close}>${icon('x', 12)}</button>
      </div>
      <div style="color:var(--t-2);font-size:var(--t-sm);line-height:1.5;margin-bottom:14px">
        A worktree is a separate working tree off a repo, on its own branch, registered
        as a workspace — so a fleet can work several branches in parallel without
        trampling one checkout. Created <b>on the daemon host</b>; the repo's
        <code>.relaydeck/worktree.yaml</code> setup hook runs so an agent lands ready.
        <div style="margin-top:8px;color:var(--t-3);font-size:var(--t-xs)">
          Needs a <b>git repo</b>. Not a repo? <code>git init</code> it first (relaydeck
          re-checks each time, so a folder you init later just works). A non-git folder
          is still a fine plain workspace — you only need worktrees for parallel branches.
        </div>
      </div>
      <label style=${LBL.replace('margin-top:12px;', '')}>Repo path</label>
      <input data-repo placeholder="/abs/path/to/git/repo" style=${INP} .value=${prefill.repo || ''}>
      <label style=${LBL}>Branch</label>
      <input data-branch placeholder="feature/login-fix" style="${INP};text-transform:none">
      <div style="display:flex;gap:10px">
        <div style="flex:1">
          <label style=${LBL}>Name <span style="text-transform:none;color:var(--t-4)">(optional)</span></label>
          <input data-name placeholder="defaults to branch" style="${INP};text-transform:none">
        </div>
        <div style="flex:1">
          <label style=${LBL}>Base ref <span style="text-transform:none;color:var(--t-4)">(optional)</span></label>
          <input data-base placeholder="main" style="${INP};text-transform:none">
        </div>
      </div>
      <label style=${LBL}>Plugins <span style="text-transform:none;color:var(--t-4)">(comma-separated, optional)</span></label>
      <input data-plugins placeholder="messaging, recipes" style="${INP};text-transform:none">
      <label style="margin-top:12px;display:flex;align-items:center;gap:7px;font:400 var(--t-sm) var(--f-mono);color:var(--t-2)">
        <input type="checkbox" data-existing> Check out an existing branch (don't create)</label>
      <div data-err style="color:${errColor};font-size:var(--t-xs);font-family:var(--f-mono);margin-top:10px;${errTpl === nothing ? 'display:none' : ''}">${errTpl}</div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px">
        <button class="btn ghost" data-act="cancel" @click=${close}>Cancel</button>
        <button class="btn primary" data-act="confirm" @click=${onConfirm}>${icon('git', 11)} Create worktree</button>
      </div>`, modal);
    els.repo = modal.querySelector('[data-repo]');
    els.branch = modal.querySelector('[data-branch]');
    els.name = modal.querySelector('[data-name]');
    els.base = modal.querySelector('[data-base]');
    els.plugins = modal.querySelector('[data-plugins]');
    els.existing = modal.querySelector('[data-existing]');
  }

  function showErr(msg, { warn = false } = {}) {
    errTpl = msg; errColor = warn ? 'var(--warn)' : 'var(--err)';
    paint();
  }

  async function onConfirm(e) {
    const repo = (els.repo?.value || '').trim();
    const branch = (els.branch?.value || '').trim();
    if (!repo || !branch) { showErr('repo and branch are required'); return; }
    const body = {
      repo, branch,
      name: (els.name?.value || '').trim() || null,
      base: (els.base?.value || '').trim() || null,
      create_branch: !els.existing?.checked,
      plugins: (els.plugins?.value || '').split(',').map(s => s.trim()).filter(Boolean),
    };
    const btn = e.currentTarget; btn.disabled = true;
    let result;
    try {
      const r = await host.api.fetch('/api/worktrees', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        let msg = `Failed (${r.status})`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        showErr(msg); btn.disabled = false; return;
      }
      result = await r.json();
    } catch (err) { showErr(err.message); btn.disabled = false; return; }
    // Surface a failed setup hook without blocking (the worktree exists).
    const s = result && result.setup;
    if (s && !s.ok) {
      showErr(`Worktree created, but setup hook failed (code ${s.code}). See logs.`, { warn: true });
      setTimeout(() => { close(); if (typeof onDone === 'function') onDone(result.name); }, 1800);
      return;
    }
    close();
    if (typeof onDone === 'function') onDone(result ? result.name : null);
  }

  paint();
  (prefill.repo ? els.branch : els.repo)?.focus();
}

export function openCommandPalette(host, prefill = '') {
  if (document.querySelector('.cmdk')) return; // already open

  const { panel: root, close, onClose } = _mountScrim('cmdk rd');
  root.setAttribute('role', 'dialog');

  // The input row + footer are static; the result list is rebuilt on each
  // keystroke via render() (lit-html diffs in place). Highlight movement is
  // done by toggling .active classes WITHOUT rebuilding (rebuilding on hover
  // detaches the node under the cursor mid-click), exactly as before.
  render(html`
    <div class="cmdk-input-row">
      ${icon('search', 15)}
      <input class="cmdk-input" placeholder="Type a command, agent name, or jump to a tab…" autofocus
        @input=${() => { sel = 0; renderList(); }}>
      <kbd style="font-family:var(--f-mono);font-size:10px;color:var(--t-4);border:1px solid var(--line-2);padding:2px 6px;border-radius:4px">esc</kbd>
    </div>
    <div class="cmdk-list"></div>
    <div class="cmdk-foot">
      <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
      <span><kbd>↵</kbd> select</span>
      <span><kbd>esc</kbd> close</span>
      <span style="margin-left:auto">palette · v${host.state.version || '0.42'}</span>
    </div>`, root);

  const input = root.querySelector('.cmdk-input');
  const list = root.querySelector('.cmdk-list');
  let sel = 0;
  input.value = prefill;
  setTimeout(() => input.focus(), 30);

  const items = () => {
    const out = [];
    for (const l of host.state.lenses) {
      out.push({ id: `nav:${l.id}`, group: 'Navigate', label: `Go to ${l.label}`, ic: l.icon, hint: l.label,
        action: () => { host.setLens(l.id); close(); } });
    }
    // Default: only show agents in the active workspace. Once the operator
    // types a query, fall back to the full list — useful for jumping to an
    // agent in another workspace by name.
    const agentList = input.value.trim()
      ? (host.state.agents || [])
      : host.scopedAgents();
    for (const a of agentList) {
      out.push({
        id: `agent:${a.id}`,
        group: 'Agents' + (host.isAllWorkspaces() ? '' : input.value.trim() ? ' (any ws)' : ` (@${host.state.workspace})`),
        label: a.id,
        ic: 'agent',
        hint: `${a.type || ''} @${a.workspace || ''}`,
        action: () => {
          // If jumping to an agent outside the current scope, widen first so
          // the agent is actually rendered in the sidebar.
          if (host.state.workspace && a.workspace !== host.state.workspace) {
            host.setWorkspace(a.workspace || "");
          }
          host.setLens('agents', a.id); close();
        },
      });
    }
    out.push({
      id: 'ws:_all', group: 'Workspaces', label: 'All workspaces', ic: 'workspace',
      hint: 'show everything', action: () => { host.setWorkspace(""); close(); },
    });
    for (const w of (host.state.workspaces || [])) {
      out.push({ id: `ws:${w.name}`, group: 'Workspaces', label: `Switch to @${w.name}`, ic: 'workspace', hint: w.path || '',
        action: () => { host.setWorkspace(w.name); close(); } });
    }
    for (const x of [
      { id: 'new-agent', label: 'New agent…', ic: 'plus', hint: '⌘N', do: () => host.openNewAgent() },
      { id: 'new-worker', label: 'New worker…', ic: 'clock', hint: '', do: () => host.setLens('workers') },
      { id: 'settings', label: 'Open settings', ic: 'cog', hint: '⌘,', do: () => host.openSettings() },
      { id: 'notifications', label: 'Open notifications', ic: 'bell', hint: '', do: () => host.openNotifications() },
      { id: 'restart-daemon', label: 'Restart daemon…', ic: 'restart', hint: '', do: () => host.openRestart() },
      { id: 'copy-token', label: 'Copy daemon auth token', ic: 'copy', hint: '', do: () => navigator.clipboard?.writeText(window.__relaydeckToken || '') },
    ]) out.push({ id: x.id, group: 'Actions', label: x.label, ic: x.ic, hint: x.hint, action: () => { x.do(); close(); } });

    const q = input.value.trim().toLowerCase();
    if (!q) return out;
    return out.filter(i => i.label.toLowerCase().includes(q) || (i.hint || '').toLowerCase().includes(q));
  };

  // The visible (filtered) result set for the current input. Kept in sync by
  // renderList so hover/arrow/Enter index against the SAME array the DOM was
  // built from.
  let currentData = [];

  // Move the highlight WITHOUT rebuilding the list. Toggle classes in place.
  function setActive(n) {
    const els = list.querySelectorAll('.cmdk-item');
    if (!els.length) return;
    sel = Math.max(0, Math.min(els.length - 1, n));
    els.forEach((el, i) => el.classList.toggle('active', i === sel));
    els[sel]?.scrollIntoView({ block: 'nearest' });
  }

  function renderList() {
    currentData = items();
    if (currentData.length === 0) {
      render(html`<div style="padding:20px 14px;text-align:center;color:var(--t-3);font-size:var(--t-xs)">Nothing matches.</div>`, list);
      return;
    }
    // Group preserving first-seen order, then render label + items with a flat
    // running index so .active highlighting matches currentData ordering.
    const groups = [];
    for (const i of currentData) {
      let g = groups.find(x => x.group === i.group);
      if (!g) { g = { group: i.group, rows: [] }; groups.push(g); }
      g.rows.push(i);
    }
    let idx = -1;
    render(html`${groups.map(g => html`
      <div class="cmdk-group-label">${g.group}</div>
      ${g.rows.map(item => {
        idx++;
        const my = idx;
        return html`<div class=${classMap({ 'cmdk-item': true, active: my === sel })}
          @click=${item.action} @mouseenter=${() => setActive(my)}>
          ${icon(item.ic, 13)}
          <div class="label">${item.label}</div>
          ${item.hint ? html`<span class="hint">${item.hint}</span>` : nothing}
        </div>`;
      })}`)}`, list);
  }
  renderList();

  // Arrow/Enter navigation. Esc + scrim-click are handled by _mountScrim; the
  // palette adds Arrow/Enter and tears the listener down via onClose.
  function onNav(e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(sel + 1); }
    if (e.key === 'ArrowUp') { e.preventDefault(); setActive(sel - 1); }
    if (e.key === 'Enter') { e.preventDefault(); currentData[sel]?.action(); }
  }
  document.addEventListener('keydown', onNav);
  onClose(() => document.removeEventListener('keydown', onNav));
}

// ── Notifications drawer ────────────────────────────────────────────
export function openNotificationsDrawer(host) {
  if (document.querySelector('.drawer')) return;
  const { panel: drawer, close } = _mountScrim('drawer');

  let filter = 'all';
  function paint() {
    const all = host.state.notifications || [];
    const items = filter === 'all' ? all : all.filter(n => (n.kind || 'info') === filter);
    render(html`
      <div class="drawer-head">
        <div class="drawer-title">Inbox</div>
        <div style="display:flex;gap:4px;align-items:center">
          <button class="btn ghost sm" data-act="mark" @click=${() => { host.state.notifications = []; host.render(); paint(); }}>Mark all read</button>
          <button class="btn icon sm" data-act="close" @click=${close}>${icon('x', 12)}</button>
        </div>
      </div>
      <div style="display:flex;gap:2px;padding:8px 12px;border-bottom:1px solid var(--line-1)">
        ${[['all', 'All'], ['awaiting', 'Awaiting'], ['err', 'Errors'], ['info', 'Info']].map(([f, label]) =>
          html`<button class=${classMap({ btn: true, ghost: true, sm: true, on: filter === f })} data-f=${f}
            @click=${() => { filter = f; paint(); }}>${label}</button>`)}
      </div>
      <div class="drawer-body">
        ${items.length === 0
          ? html`<div style="padding:24px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center">${
              filter === 'all' ? 'No notifications.' : `No ${filter === 'err' ? 'errors' : filter} notifications.`}</div>`
          : items.map(n => html`
            <div class="notif ${n.kind || 'info'}" @click=${n.agent ? () => { host.setLens('agents', n.agent); close(); } : null}>
              <div class="stripe"></div>
              <div class="body">
                <div class="title">${n.title || ''}</div>
                <div class="meta">${n.agent ? html`<span class="ref">${n.agent}</span> · ` : nothing}<span>@${n.workspace || ''}</span></div>
                ${n.body ? html`<div style="font-size:var(--t-xs);color:var(--t-2);margin-top:4px">${n.body}</div>` : nothing}
              </div>
              <div class="time">${relTime(n.ts)}</div>
            </div>`)}
      </div>
      <div class="drawer-foot">Notifications stream from the plugin bus. <kbd style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 4px;border-radius:3px;font-size:9px">⇧⌘N</kbd> to toggle.</div>`, drawer);
  }
  paint();
}

// ── Settings overlay ─────────────────────────────────────────────────
export function openSettings(host, section = 'general') {
  if (document.querySelector('.settings-overlay')) return;
  // The settings overlay is full-bleed (no scrim) — mount the panel only.
  const root = document.createElement('div');
  root.className = 'settings-overlay rd';
  document.body.appendChild(root);

  function close() { root.remove(); document.removeEventListener('keydown', onKey); }
  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);

  let current = section;
  const NAV = [
    { grp: 'Appearance' },
    { s: 'general', ic: 'cog', label: 'General' },
    { s: 'shortcuts', ic: 'command', label: 'Shortcuts' },
    { grp: 'Plugins' },
    { s: 'plugins', ic: 'command', label: 'Installed' },
    { s: 'integrations', ic: 'bolt', label: 'Integrations' },
    { grp: 'Daemon' },
    { s: 'auth', ic: 'vault', label: 'Auth tokens' },
    { s: 'vault', ic: 'vault', label: 'Vault' },
    { s: 'about', ic: 'alert', label: 'About' },
    { s: 'credits', ic: 'copy', label: 'Credits & Licenses' },
    { grp: 'Danger zone', danger: true },
    { s: 'danger', ic: 'delete', label: 'Wipe data', danger: true },
  ];

  function paintShell() {
    render(html`
      <div class="settings-head">
        <div class="lede">
          <h2>System settings</h2>
          <div class="sub">Plugins, models, workspaces, and machine-wide configuration.</div>
        </div>
        <button class="btn icon" data-act="close" title="Close (Esc)" @click=${close}>${icon('x', 14)}</button>
      </div>
      <div class="settings-body">
        <div class="settings-nav">
          ${NAV.map(n => n.grp
            ? html`<div class="nav-grp" style=${n.danger ? 'color:var(--err)' : ''}>${n.grp}</div>`
            : html`<button data-s=${n.s} class=${current === n.s ? 'on' : ''}
                style=${n.danger ? 'color:var(--err)' : ''}
                @click=${() => { current = n.s; paintShell(); renderSection(n.s); }}>${icon(n.ic, 13)} ${n.label}</button>`)}
        </div>
        <div class="settings-content" data-content></div>
      </div>`, root);
  }
  paintShell();
  const content = () => root.querySelector('[data-content]');

  async function renderSection(s) {
    const el = content();
    render(html`<div style="color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs)">loading…</div>`, el);
    if (s === 'shortcuts') { renderShortcutsSection(el, host); return; }
    if (s === 'general') return renderGeneral(el);
    if (s === 'plugins') return renderPlugins(el);
    if (s === 'integrations') return renderIntegrations(el);
    if (s === 'auth') return renderAuth(el);
    if (s === 'vault') return renderVault(el);
    if (s === 'about') return renderAbout(el);
    if (s === 'credits') return renderCredits(el);
    if (s === 'danger') return renderDangerZone(el, host);
  }

  async function renderGeneral(el) {
    const tab = host._settingsAppTab || 'theme';
    render(html`
      <div class="settings-section">
        <h3>Appearance</h3>
        <div class="sub">Pick a theme, density and glow. Most operators stop here — agents (or you) can author fully custom token themes under <b>Customize</b>.</div>
        <div class="seg app-subtabs" data-k="apptab" style="margin:16px 0 8px">
          <button class=${classMap({ 'seg-i': true, on: tab === 'theme' })} data-v="theme"
            @click=${() => { host._settingsAppTab = 'theme'; renderSection('general'); }}>Theme</button>
          <button class=${classMap({ 'seg-i': true, on: tab === 'customize' })} data-v="customize"
            @click=${() => { host._settingsAppTab = 'customize'; renderSection('general'); }}>Customize tokens</button>
        </div>
        <div data-apptab></div>
      </div>`, el);
    const pane = el.querySelector('[data-apptab]');
    if (tab === 'customize') {
      // Reuse the full granular token editor (imperative lens mount) — the
      // complexity lives here, out of the way of the simple picker.
      const lens = new AppearanceLens(host);
      await lens.renderDetail(pane);
    } else {
      await renderThemePicker(pane, host, () => renderSection('general'));
    }
  }

  async function renderPlugins(el) {
    let installStatus = '';
    let installBusy = false;
    let plugins = null;
    let loadError = '';

    const onInstall = async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const source = (form.querySelector('[data-source]')?.value || '').trim();
      if (!source) return;
      installBusy = true; installStatus = 'installing...'; paint();
      try {
        const resp = await host.api.postJSON('/api/plugins/install', { source });
        const names = (resp.plugins || []).join(', ') || source;
        installStatus = `${names} installed · restart daemon to load`;
        if (host.reloadPlugins) await host.reloadPlugins();
      } catch (err) {
        installStatus = `failed: ${err.message}`;
      } finally {
        installBusy = false; paint();
      }
    };

    const toggle = async (p) => {
      const action = p.enabled ? 'disable' : 'enable';
      try {
        await host.api.postJSON(`/api/plugins/${encodeURIComponent(p.name)}/${action}`, {});
        // Belt-and-suspenders: the toggle emits system.plugin.* → SSE →
        // refreshPluginManifest too, but call it here so the rail updates
        // instantly even if SSE delivery is briefly delayed.
        if (host.refreshPluginManifest) await host.refreshPluginManifest();
        renderSection('plugins');
      } catch (err) { alert('Failed: ' + err.message); }
    };

    const uninstall = async (p) => {
      if (!window.confirm(`Uninstall ${p.name}?`)) return;
      try {
        await host.api.deleteJSON(`/api/plugins/${encodeURIComponent(p.name)}`);
        renderSection('plugins');
      } catch (err) { alert('Failed: ' + err.message); }
    };

    const pluginRow = (p) => html`
      <div class="set-row">
        <div><div class="lbl">${p.name}</div><div class="desc">${p.description || ''}</div></div>
        <div style="font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-3)">v${p.version || ''} · ${p.category || ''}${p.installed_via ? ` · ${p.installed_via}` : ''}</div>
        <div class="ctrl">
          ${p.user_installed ? html`<button class="btn sm" data-uninstall=${p.name} @click=${() => uninstall(p)}>${icon('delete', 11)} Uninstall</button>` : nothing}
          <button class=${classMap({ 'set-toggle': true, on: !!p.enabled })} data-name=${p.name} @click=${() => toggle(p)}></button>
        </div>
      </div>`;

    function paint() {
      const ws = (plugins || []).filter(p => p.workspace_scoped);
      const global = (plugins || []).filter(p => !p.workspace_scoped);
      render(html`
        <div class="settings-section">
          <h3>Installed plugins</h3>
          <div class="sub">Enable or disable plugins daemon-wide. Per-workspace toggles live in the workspace drawer.</div>
          <form class="set-row" data-plugin-install style="grid-template-columns:1fr auto;margin-top:14px" @submit=${onInstall}>
            <div>
              <div class="lbl">Install package</div>
              <input data-source placeholder="relaydeck-plugin-example" style="margin-top:8px;width:100%;background:var(--bg-1);border:1px solid var(--line-2);color:var(--t-1);border-radius:6px;padding:8px 10px;font-family:var(--f-mono);font-size:var(--t-xs);text-transform:none">
              <div class="desc" data-install-status>${installStatus}</div>
            </div>
            <div class="ctrl"><button class="btn sm" type="submit" ?disabled=${installBusy}>${icon('plus', 12)} Install</button></div>
          </form>
          <div data-list>
            ${loadError ? html`<div style="color:var(--err);font-family:var(--f-mono);font-size:var(--t-xs)">Failed to load: ${loadError}</div>`
              : plugins == null ? nothing
              : html`
                ${ws.length ? html`<div class="set-group-label"><span>Workspace-scoped</span><span class="n">${ws.length}</span></div>${ws.map(pluginRow)}` : nothing}
                ${global.length ? html`<div class="set-group-label"><span>Global · daemon-wide</span><span class="n">${global.length}</span></div>${global.map(pluginRow)}` : nothing}`}
          </div>
        </div>`, el);
    }
    paint();
    try {
      plugins = await host.api.getJSON('/api/plugins');
    } catch (e) {
      loadError = e.message;
    }
    paint();
  }

  async function renderIntegrations(el) {
    render(html`<div class="settings-section"><h3>Vendor integrations</h3>
      <div class="sub">Telemetry sources that feed the semantic-status axis. <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">relaydeck integration install/uninstall</code> CLI equivalents.</div>
      <div data-list></div></div>`, el);
    const list = el.querySelector('[data-list]');
    let integrations;
    try {
      integrations = await host.api.getJSON('/api/integrations');
    } catch (e) {
      render(html`<div style="color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs)">Endpoint not available; use <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">relaydeck integration list</code>.</div>`, list);
      return;
    }
    const act = async (name, action) => {
      try { await host.api.postJSON(`/api/integrations/${encodeURIComponent(name)}/${action}`, {}); renderSection('integrations'); }
      catch (e) { alert(e.message); }
    };
    render(html`${integrations.map(i => {
      const st = i.state || (i.installed ? 'installed' : 'not-installed');
      // `error` comes from api.py:561-563 when integration_state(i) threw — the
      // probe couldn't determine state, so anything we'd POST is guessing.
      // Surface it and disable the button.
      const isError = st === 'error';
      const isOutdated = st === 'outdated';
      const isOrphan = st.startsWith('orphaned');
      const isDrifted = isOutdated || isOrphan;
      const statusColor = st === 'installed' ? 'var(--ok)'
        : isError ? 'var(--warn)'
        : isDrifted ? 'var(--warn)'
        : 'var(--t-3)';
      const statusLabel = st === 'installed' ? '✓ installed'
        : st === 'not-installed' ? '— not installed'
        : isError ? '✗ error'
        : isDrifted ? `⚠ ${st}` : st;
      // Outdated: rewrite the body via the idempotent install endpoint; don't
      // uninstall (that disables telemetry until next claude-code spawn).
      // Orphaned: one half is missing; uninstall the remaining half. Error:
      // don't guess — disable until the operator clears the probe failure.
      const action = isError ? ''
        : st === 'installed' ? 'uninstall'
        : isOutdated ? 'install'
        : isOrphan ? 'uninstall'
        : 'install';
      const label = isError ? 'Unavailable'
        : st === 'installed' ? 'Uninstall'
        : isOutdated ? 'Regenerate'
        : isOrphan ? 'Clean up'
        : 'Install';
      return html`<div class="set-row">
        <div><div class="lbl">${i.harness}</div><div class="desc">${i.kind === 'hook' ? 'Vendor hook installer' : 'Classifier bridge (no-op installer)'}</div></div>
        <div style="font-family:var(--f-mono);font-size:var(--t-xxs);color:${statusColor}">${statusLabel}</div>
        <div class="ctrl">
          <button class="btn sm" ?disabled=${isError} data-name=${i.harness} data-action=${action}
            @click=${isError ? null : () => act(i.harness, action)}>${label}</button>
        </div>
      </div>`;
    })}`, list);
  }

  async function renderAuth(el) {
    const tok = window.__relaydeckToken || '';
    const rotate = async () => {
      if (!window.confirm('Rotate the token? Live clients will need the new one.')) return;
      try { await host.api.postJSON('/api/auth/rotate', {}); alert('Rotated. Reload your tabs and paste the new token from `relaydeck auth token`.'); }
      catch (e) { alert('Endpoint not available; use `relaydeck auth rotate` on the daemon host. (' + e.message + ')'); }
    };
    render(html`
      <div class="settings-section">
        <h3>Daemon authentication</h3>
        <div class="sub">The daemon protects every <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">/api/*</code> route with a Bearer token. Browsers on the same host bootstrap the token automatically; remote browsers paste it.</div>
        <div class="set-row">
          <div><div class="lbl">Current token</div><div class="desc">Stored in <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">~/.relaydeck/auth-token</code> (mode 0600).</div></div>
          <div style="font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-2)" data-tok>${tok.slice(0, 8)}…${tok.slice(-4)}</div>
          <div class="ctrl">
            <button class="btn" data-act="copy" @click=${() => navigator.clipboard?.writeText(window.__relaydeckToken || '')}>${icon('copy', 11)} Copy</button>
          </div>
        </div>
        <div class="set-row">
          <div><div class="lbl">Rotate token</div><div class="desc">Mints a new token; all connected dashboards re-prompt. CLI on the same host reads from disk so it's unaffected.</div></div>
          <div></div>
          <div class="ctrl">
            <button class="btn danger" data-act="rotate" @click=${rotate}>Rotate</button>
          </div>
        </div>
        <div class="set-row">
          <div><div class="lbl">Issued tokens</div><div class="desc">Scoped tokens issued via <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">relaydeck auth issue</code>.</div></div>
          <div></div>
          <div class="ctrl"><span style="font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-3)" data-issued>—</span></div>
        </div>
      </div>`, el);
    try {
      const tokens = await host.api.getJSON('/api/auth/tokens');
      const issued = el.querySelector('[data-issued]');
      if (issued) issued.textContent = `${tokens.length} issued`;
    } catch (_) {}
  }

  async function renderVault(el) {
    const VINP = 'background:var(--bg-2);border:1px solid var(--line-2);color:var(--t-1);font:400 var(--t-xs) var(--f-mono);padding:5px 8px;border-radius:3px';
    let keys = null;
    let msgTpl = nothing;

    const setMsg = (color, text) => { msgTpl = html`<span style="color:var(--${color})">${text}</span>`; paint(); };

    const onSet = async () => {
      const kEl = el.querySelector('[data-vk]');
      const vEl = el.querySelector('[data-vv]');
      const k = (kEl?.value || '').trim();
      const v = vEl?.value || '';
      if (!k) { setMsg('warn', 'key is required'); return; }
      if (!v) { setMsg('warn', 'value is required'); return; }
      try { await host.api.postJSON(`/api/vault/keys/${encodeURIComponent(k)}`, { value: v }); }
      catch (e) { setMsg('err', e.message); return; }
      if (kEl) kEl.value = ''; if (vEl) vEl.value = '';
      setMsg('ok', `set ${k}.`);
      loadVault();
    };

    const onDelete = async (key) => {
      if (!window.confirm(`Delete vault key "${key}"? Configs referencing it will fail until re-set.`)) return;
      try {
        const r = await host.api.fetch(`/api/vault/keys/${encodeURIComponent(key)}`, { method: 'DELETE' });
        if (!r.ok) {
          let msg = `Delete failed (${r.status})`;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          setMsg('err', msg); return;
        }
      } catch (e) { setMsg('err', e.message); return; }
      setMsg('ok', `deleted ${key}.`);
      loadVault();
    };

    function paint() {
      render(html`<div class="settings-section"><h3>Vault</h3>
        <div class="sub">Secrets referenced as <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">\${vault:NAME}</code> in plugin/agent configs. Set them here — values are written into the daemon and <b>never read back</b> (the dashboard only ever sees key names).</div>
        <div class="set-row" style="align-items:flex-end;gap:8px">
          <div style="flex:1"><div class="lbl">New / update secret</div>
            <div style="display:flex;gap:6px;margin-top:4px">
              <input data-vk placeholder="KEY (e.g. ANTHROPIC_API_KEY)" style="${VINP};flex:0 0 240px;text-transform:none">
              <input data-vv type="password" placeholder="value" style="${VINP};flex:1">
            </div>
          </div>
          <button class="btn primary" data-vset @click=${onSet}>Set</button>
        </div>
        <div data-vmsg style="font-family:var(--f-mono);font-size:11px;min-height:14px;margin:2px 0 8px">${msgTpl}</div>
        <div data-list>
          ${keys == null ? nothing
            : keys.length === 0
              ? html`<div style="color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);padding:10px 0">No vault keys yet — add one above.</div>`
              : keys.map(k => html`<div class="set-row">
                  <div><div class="lbl">${k}</div><div class="desc">Referenced as <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">\${vault:${k}}</code></div></div>
                  <div></div>
                  <div class="ctrl"><button class="btn ghost sm" data-vdel=${k} @click=${() => onDelete(k)}>${icon('delete', 11)} Delete</button></div>
                </div>`)}
        </div></div>`, el);
    }

    async function loadVault() {
      try { keys = await host.api.getJSON('/api/vault/keys'); }
      catch (e) {
        render(html`<div style="color:var(--err);font-family:var(--f-mono);font-size:var(--t-xs)">Failed to load.</div>`, el.querySelector('[data-list]'));
        return;
      }
      paint();
    }
    paint();
    loadVault();
  }

  async function renderAbout(el) {
    render(html`
      <div class="settings-section">
        <h3>About relaydeck</h3>
        <div class="sub">Local-first fleet OS for CLI coding agents.</div>
        <div class="set-row"><div><div class="lbl">Version</div></div><div></div><div class="ctrl" style="font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-2)">${host.state.version || '—'}</div></div>
        <div class="set-row"><div><div class="lbl">Dashboard</div></div><div></div><div class="ctrl" style="font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-2)">${location.host}</div></div>
        <div class="set-row"><div><div class="lbl">SSE</div></div><div></div><div class="ctrl" style="font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-2)">${host.state.sseStatus || '—'}</div></div>
      </div>`, el);
  }

  // Static, in-bundle attribution page (no backend). Mirrors CREDITS.md — keep
  // the two in sync when libraries change.
  function renderCredits(el) {
    const row = (name, sub, lic, url) => html`
      <div class="set-row">
        <div><div class="lbl">${name}</div>${sub ? html`<div class="sub" style="margin:0">${sub}</div>` : nothing}</div>
        <div></div>
        <div class="ctrl" style="display:flex;align-items:center;gap:10px">
          <span style="font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-3)">${lic}</span>
          ${url ? html`<a class="btn ghost sm" href=${url} target="_blank" rel="noopener noreferrer">view</a>` : nothing}
        </div>
      </div>`;
    render(html`
      <div class="settings-section">
        <h3>Credits &amp; Licenses</h3>
        <div class="sub">relaydeck is MIT-licensed and built on a lot of excellent open source. Product names, logos, and trademarks belong to their respective owners; their presence here denotes integration — not affiliation or endorsement.</div>
        ${row('relaydeck', host.state.version ? `v${host.state.version}` : '', 'MIT', 'https://github.com/relaydeck/relaydeck/blob/main/CREDITS.md')}

        <h3 style="margin-top:18px">Fonts</h3>
        ${row('IBM Plex Sans · IBM Plex Mono', '© IBM Corp.', 'OFL-1.1', 'https://github.com/IBM/plex')}
        ${row('JetBrains Mono', 'JetBrains Mono Project Authors', 'OFL-1.1', 'https://github.com/JetBrains/JetBrainsMono')}

        <h3 style="margin-top:18px">Frontend libraries</h3>
        ${row('Lit', '3.3.3', 'BSD-3-Clause', 'https://lit.dev')}
        ${row('xterm.js (+ addons)', '5.5.0', 'MIT', 'https://xtermjs.org')}
        ${row('Heroicons', '2.1.5', 'MIT', 'https://heroicons.com')}
        ${row('Simple Icons', 'brand marks', 'CC0-1.0', 'https://simpleicons.org')}

        <h3 style="margin-top:18px">Harnesses &amp; ecosystem</h3>
        <div class="sub">Thanks to the projects relaydeck wraps and learns from — especially the <b>pi</b> coding agent (relaydeck's reference harness), plus Claude Code, Codex, Cursor, OpenCode &amp; Antigravity. We were also inspired, in part, by ideas from across the community — including Nous Research's Hermes Agent and OpenClaw — gratefully acknowledged.</div>
        ${row('pi (pi-coding-agent)', 'Mario Zechner · earendil-works', 'reference harness', 'https://github.com/earendil-works/pi')}
        ${row('Hermes Agent', 'Nous Research', 'inspiration', 'https://github.com/NousResearch/hermes-agent')}

        <div class="sub" style="margin-top:16px">Python runtime: FastAPI, Uvicorn, Click, Rich, Textual, Pyte, Pydantic, cryptography, NumPy and more — see the <a href="https://github.com/relaydeck/relaydeck/blob/main/CREDITS.md" target="_blank" rel="noopener noreferrer">full credits</a>.</div>
      </div>`, el);
  }

  renderSection(current);
}

// ── Settings → Danger Zone (wipe messages + history) ────────────────
async function renderDangerZone(content, host) {
  let data;
  try { data = await host.api.getJSON('/api/maintenance/history'); }
  catch (e) {
    render(html`<div class="settings-section"><div class="sub" style="color:var(--err)">Couldn't load: ${e.message}</div></div>`, content);
    return;
  }
  const counts = data.counts || {}, labels = data.labels || {};
  const scopes = Object.keys(labels);
  const HIST = new Set(['events', 'usage', 'invocations', 'runs']);
  // Selection state lives outside the template; checkbox refs are read on demand
  // (parity with the original [data-scope] querySelector approach).
  let errMsg = '';

  const boxes = () => [...content.querySelectorAll('[data-scope]')];
  const selected = () => boxes().filter(b => b.checked && !b.disabled).map(b => b.dataset.scope);
  const sync = () => { const w = content.querySelector('[data-act="wipe"]'); if (w) w.disabled = selected().length === 0; };
  const setOnly = (pred) => { boxes().forEach(b => { b.checked = !b.disabled && pred(b.dataset.scope); }); sync(); };

  const onWipe = () => {
    const sc = selected();
    if (!sc.length) return;
    const n = sc.reduce((a, s) => a + (counts[s] || 0), 0);
    openWipeConfirmModal(host, sc, n, labels, async () => {
      try {
        const r = await host.api.postJSON('/api/maintenance/wipe', { scopes: sc });
        const del = (r && r.deleted) || {};
        const tot = Object.values(del).reduce((a, v) => a + v, 0);
        host._toast?.(`Wiped ${tot.toLocaleString()} rows`);
        renderDangerZone(content, host);  // refresh counts → 0
      } catch (e) {
        errMsg = e.message; paint();
      }
    });
  };

  function paint() {
    render(html`
      <div class="settings-section">
        <h3 style="color:var(--err)">Danger zone</h3>
        <div class="sub">Permanently delete messages + activity history. This can't be undone.
          Your agents, workspaces, plugins, secrets, and the audit log are <b>not</b> touched.</div>
        <div class="dz-box">
          <div class="dz-pick">
            <button class="btn sm" data-act="msgs" @click=${() => setOnly(s => s === 'messages')}>Messages only</button>
            <button class="btn sm" data-act="hist" @click=${() => setOnly(s => HIST.has(s))}>All history</button>
            <button class="btn sm" data-act="all" @click=${() => setOnly(() => true)}>Everything</button>
            <button class="btn sm ghost" data-act="none" @click=${() => setOnly(() => false)}>Clear</button>
          </div>
          ${scopes.map(s => html`
            <label class="dz-row">
              <input type="checkbox" data-scope=${s} ?disabled=${!(counts[s] || 0)} @change=${sync}>
              <span class="dz-lbl">${labels[s]}</span>
              <span class="dz-count">${(counts[s] || 0).toLocaleString()} rows</span>
            </label>`)}
          <div data-err style="color:var(--err);font-family:var(--f-mono);font-size:var(--t-xs);margin-top:8px;${errMsg ? '' : 'display:none'}">${errMsg}</div>
          <button class="btn danger" data-act="wipe" style="margin-top:12px" disabled @click=${onWipe}>${icon('delete', 11)} Wipe selected</button>
        </div>
      </div>`, content);
    sync();
  }
  paint();
}

function openWipeConfirmModal(host, scopes, count, labels, onConfirm) {
  if (document.querySelector('.wipe-modal')) return;
  const { panel: modal, close } = _mountScrim('wipe-modal');
  modal.style.cssText = `position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);
    width:min(460px,92vw);background:var(--bg-1);border:1px solid var(--err);
    border-radius:var(--r-4);box-shadow:0 0 0 1px var(--line-2),0 30px 60px rgba(0,0,0,.12);
    z-index:140;padding:20px;animation:cmdk-in .14s cubic-bezier(.2,.9,.3,1);`;

  let canGo = false;
  function paint() {
    render(html`
      <div style="font-size:var(--t-xl);font-weight:600;color:var(--err)">Wipe ${count.toLocaleString()} rows?</div>
      <div style="color:var(--t-2);font-size:var(--t-sm);line-height:1.5;margin:10px 0">
        Permanently deletes:<ul style="margin:6px 0 0 18px">${scopes.map(s => html`<li>${labels[s] || s}</li>`)}</ul>
        This can't be undone.
      </div>
      <label style="display:block;font:600 var(--t-xs)/1 var(--f-mono);color:var(--t-3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Type <b style="color:var(--err)">wipe</b> to confirm</label>
      <input data-confirm placeholder="wipe" style="width:100%;background:var(--bg-2);border:1px solid var(--line-2);color:var(--t-1);font:400 var(--t-sm) var(--f-mono);padding:7px 9px;border-radius:4px"
        @input=${onInput}>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        <button class="btn ghost" data-act="cancel" @click=${close}>Cancel</button>
        <button class="btn danger" data-act="go" ?disabled=${!canGo} @click=${onGo}>${icon('delete', 11)} Wipe</button>
      </div>`, modal);
  }
  function onInput(e) {
    const next = e.target.value.trim().toLowerCase() === 'wipe';
    if (next !== canGo) { canGo = next; modal.querySelector('[data-act="go"]').disabled = !canGo; }
  }
  function onGo() {
    const inp = modal.querySelector('[data-confirm]');
    if (inp.value.trim().toLowerCase() === 'wipe') { close(); onConfirm(); }
  }
  paint();
  modal.querySelector('[data-confirm]')?.focus();
}
