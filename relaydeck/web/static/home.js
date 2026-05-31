// home.js — customizable Home dashboard.
//
// Replaces the old "no agent selected" fleet overview with a widget grid
// the operator personalizes: drag to rearrange, drag the corner to
// resize, add/remove from a gallery, reset to defaults. Layout is part of
// the per-workspace appearance bundle (server-backed via /api/appearance),
// so it travels across devices and each workspace can have its own
// dashboard; localStorage is a first-paint cache + migration source.
//
// Widget sources:
//   - core      — always available, backed by real runtime data
//   - personal  — self-contained, browser-local (clock, notes, focus, coffee)
//   - <plugin>  — contributed widgets. Built-in plugin widgets (inbox,
//                 files) are gated on the plugin being enabled in the
//                 workspace; third-party plugins can ship their own via
//                 `[plugin.ui] widgets` in their manifest (host.state.homeWidgets),
//                 mounted by dynamic import like agent-detail tiles.
//
// MIGRATION NOTE — light-DOM Lit on @relaydeck/ui.
//   • This is NOT a lens or a tile: it keeps the framework-neutral
//     mount(container)/unmount() contract the Agents lens fleet view calls,
//     plus the window.__relaydeckHome hook + applyCommand(cmd) reshaping.
//   • Widget BODIES render via lit-html render(html`…`, el) into each
//     widget's stable `.wgt-body` node — no innerHTML, no querySelector
//     wiring. The same dashboard classes/data-attrs are emitted (light DOM
//     makes that free), so the e2e selectors keep matching.
//   • The genuinely-procedural lifecycle stays IMPERATIVE against stable
//     anchor nodes the reactive layer never re-renders: the grid shell
//     build, drag/resize (document mousemove/mouseup), the gallery drawer
//     modal, the personal-widget timers, and the plugin-widget dynamic
//     import that mounts a foreign mount(el, api, ctx) — exactly like
//     lenses/agents.js hosts a tile body in [data-body].

import {
  html, render, nothing, unsafeHTML,
  icon, spark, fmtNum, fmtCost, relTime, formatTs, visualStatus, uptimeStr,
  button, live, stamped,
} from '@relaydeck/ui';
import { openNewWorktreeModal } from './overlays.js';
import { agentMark } from './harness_brand.js';

// Widgets that render live data (state-derived or endpoint-fetching) and
// should re-render when the underlying data changes. feed/files already
// self-subscribe to events; clock/notes/focus/coffee are personal/local.
const LIVE_WIDGET_KEYS = new Set(['fleet', 'usage', 'heatmap', 'agents', 'workspaces', 'workers', 'inbox', 'worktrees']);

const LS_KEY = 'relaydeck.home.layout.v1';
const COLS = 12;
const ROW_H = 80;
const GAP = 12;

// ── Built-in widget registry ─────────────────────────────────────────
// Each: key, title, icon, source, desc, def {w,h}, min {w,h}.
const BUILTIN_DEFS = [
  // Core — real runtime data
  { key: 'fleet', title: 'Fleet pulse', icon: 'activity', source: 'core',
    desc: "Editorial status — who's running, awaiting, errored.", def: { w: 8, h: 3 }, min: { w: 5, h: 2 } },
  { key: 'usage', title: 'Usage', icon: 'bolt', source: 'core',
    desc: 'Tokens consumed and dollars burnt, with a spark.', def: { w: 4, h: 3 }, min: { w: 3, h: 2 } },
  { key: 'heatmap', title: 'Usage heatmap', icon: 'activity', source: 'core',
    desc: 'Fleet token usage as a 7-day × 24-hour calendar heatmap.', def: { w: 6, h: 3 }, min: { w: 4, h: 2 } },
  { key: 'agents', title: 'Agents', icon: 'agent', source: 'core',
    desc: 'Compact list of every agent with live status.', def: { w: 6, h: 5 }, min: { w: 4, h: 3 } },
  { key: 'feed', title: 'Live activity', icon: 'activity', source: 'core',
    desc: 'Streaming firehose of every runtime event.', def: { w: 6, h: 5 }, min: { w: 4, h: 3 } },
  { key: 'workspaces', title: 'Workspaces', icon: 'workspace', source: 'core',
    desc: "Quick-switch cards for every workspace you've loaded.", def: { w: 6, h: 2 }, min: { w: 4, h: 2 } },
  { key: 'workers', title: 'Worker pulse', icon: 'cpu', source: 'core',
    desc: 'Heartbeat for background workers — watchers, pollers, gateways.', def: { w: 6, h: 3 }, min: { w: 4, h: 2 } },
  { key: 'spawn', title: 'Quick spawn', icon: 'plus', source: 'core',
    desc: 'One-tap shortcuts to spawn a new agent.', def: { w: 4, h: 3 }, min: { w: 3, h: 2 } },
  { key: 'worktrees', title: 'Worktrees', icon: 'git', source: 'core',
    desc: 'Git worktrees in play — branch, dirty/ahead-behind, quick create.', def: { w: 4, h: 3 }, min: { w: 3, h: 2 } },

  // Plugin-gated — only offered when the source plugin is enabled here
  { key: 'inbox', title: 'Inbox', icon: 'inbox', source: 'messaging',
    desc: 'Unread agent-to-agent threads awaiting your reply.', def: { w: 4, h: 4 }, min: { w: 3, h: 2 } },
  { key: 'files', title: 'File activity', icon: 'workspace', source: 'file-watcher',
    desc: 'Latest filesystem events the watcher emitted.', def: { w: 4, h: 4 }, min: { w: 3, h: 2 } },

  // Personal — browser-local, no backend
  { key: 'clock', title: 'Clock', icon: 'clock', source: 'personal',
    desc: 'Just the time. For when you lose track heads-down.', def: { w: 3, h: 2 }, min: { w: 2, h: 2 } },
  { key: 'notes', title: 'Scratchpad', icon: 'edit', source: 'personal',
    desc: 'Personal sticky note — saved to your browser, never shared.', def: { w: 4, h: 3 }, min: { w: 3, h: 2 } },
  { key: 'focus', title: 'Focus timer', icon: 'target', source: 'personal',
    desc: 'A pomodoro for heads-down sessions.', def: { w: 4, h: 4 }, min: { w: 3, h: 3 } },
  { key: 'coffee', title: 'Coffee log', icon: 'coffee', source: 'personal',
    desc: 'Tap to log a cup. Persisted in your browser.', def: { w: 3, h: 3 }, min: { w: 2, h: 2 } },
];

const DEFAULT_LAYOUT = [
  { id: 'w-fleet', key: 'fleet', x: 0, y: 0, w: 8, h: 3 },
  { id: 'w-usage', key: 'usage', x: 8, y: 0, w: 4, h: 3 },
  { id: 'w-agents', key: 'agents', x: 0, y: 3, w: 6, h: 4 },
  { id: 'w-feed', key: 'feed', x: 6, y: 3, w: 6, h: 4 },
  { id: 'w-workspaces', key: 'workspaces', x: 0, y: 7, w: 6, h: 2 },
  { id: 'w-spawn', key: 'spawn', x: 6, y: 7, w: 6, h: 2 },
];

// A blank-state body for the deferred/empty widgets — one helper so every
// widget renders the same `.w-blank` placeholder the CSS targets.
const blank = (msg, style) => html`<div class="w-blank" style=${style || nothing}>${msg}</div>`;

export class HomeDashboard {
  constructor(host) {
    this.host = host;
    this.editing = false;
    this.galleryOpen = false;
    this.ghost = null;
    this.draggingId = null;
    this._cleanups = [];
    this._liveRerenders = [];   // per-mounted-widget re-render closures (live keys)
    this._liveUnsubs = [];      // store subscriptions (torn down on unmount)
    this._usageRollup = {};     // {agent_id:{tokens,spark[24]}} from live store
    this.layout = this._loadLayout();
  }

  // Merge built-in defs with plugin-contributed manifest widgets.
  defs() {
    const manifest = (this.host.state.homeWidgets || []).map(w => ({
      key: w.key || w.id,
      title: w.title || w.label || w.key || w.id,
      icon: w.icon || 'grid',
      source: w.source || (String(w.id || '').split(':')[0]) || 'plugin',
      desc: w.description || w.desc || '',
      def: { w: w.def_w || w.w || 4, h: w.def_h || w.h || 3 },
      min: { w: w.min_w || 3, h: w.min_h || 2 },
      module: w.module ? this._resolveModule(w) : null,
    }));
    const map = {};
    for (const d of [...BUILTIN_DEFS, ...manifest]) map[d.key] = d;
    return map;
  }

  _resolveModule(w) {
    const src = (w.id || w.key || '').split(':')[0];
    const mod = w.module || '';
    if (!mod) return null;
    if (mod.startsWith('/') || mod.startsWith('http')) return mod;
    return `/static/plugins/${encodeURIComponent(src)}/${mod}`;
  }

  // A widget source is available if it's core/personal, OR its plugin is
  // enabled in the current workspace (or globally when in "all" mode).
  _sourceActive(source) {
    if (source === 'core' || source === 'personal') return true;
    const wsPlugins = this.host.state.workspacePlugins || [];
    if (wsPlugins.includes(source)) return true;
    // "All workspaces" mode: offer it if any loaded plugin matches.
    return (this.host.state.allPlugins || []).some(p => p.name === source && p.enabled !== false);
  }

  // Layout is part of the per-workspace appearance bundle (server-backed)
  // so it travels across devices and a workspace can have its own
  // dashboard. localStorage is kept as a fast first-paint cache and the
  // migration source for pre-server layouts.
  _ws() { return this.host.state.workspace || ''; }
  _lsKey() { const w = this._ws(); return w ? `${LS_KEY}:${w}` : LS_KEY; }

  _loadLayout() {
    try {
      const raw = localStorage.getItem(this._lsKey()) || localStorage.getItem(LS_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) { this._hadLocal = true; return parsed; }
      }
    } catch (_) {}
    return DEFAULT_LAYOUT.map(w => ({ ...w }));
  }

  _overlaps(a, b) {
    return a.x < b.x + b.w && a.x + a.w > b.x
      && a.y < b.y + b.h && a.y + a.h > b.y;
  }

  _layoutHasOverlaps(layout) {
    for (let i = 0; i < layout.length; i++) {
      for (let j = i + 1; j < layout.length; j++) {
        if (this._overlaps(layout[i], layout[j])) return true;
      }
    }
    return false;
  }

  _clampWidget(widget) {
    const def = this.defs()[widget.key];
    const minW = def?.min?.w || 2;
    const minH = def?.min?.h || 2;
    let { x, y, w, h } = widget;
    w = Math.max(minW, Math.min(COLS, Number(w) || minW));
    h = Math.max(minH, Number(h) || minH);
    x = Math.max(0, Math.min(COLS - w, Number(x) || 0));
    y = Math.max(0, Number(y) || 0);
    return { ...widget, x, y, w, h };
  }

  // Bounds-only clamp for saved layouts — do not expand to widget minW/minH
  // (that silently shifts x and rewrites positions the operator saved).
  _clampBounds(widget) {
    let { x, y, w, h } = widget;
    w = Math.max(1, Math.min(COLS, Number(w) || 1));
    h = Math.max(1, Number(h) || 1);
    x = Math.max(0, Math.min(COLS - w, Number(x) || 0));
    y = Math.max(0, Number(y) || 0);
    return { ...widget, x, y, w, h };
  }

  // Drop unknown widgets, clamp to the 12-col grid, and pack when cells
  // overlap or a widget hangs past the right edge (CSS grid stacks overlaps
  // instead of reflowing — looks like bleed into the neighbor).
  _sanitizeLayout({ autoTidy = false } = {}) {
    const defs = this.defs();
    const layout = this.layout.filter(w => defs[w.key]).map(w => this._clampBounds(w));
    const needsTidy = autoTidy
      || this._layoutHasOverlaps(layout)
      || layout.some(w => w.x + w.w > COLS);
    this.layout = layout;
    if (needsTidy) {
      this._tidy();
      return true;
    }
    return false;
  }

  // Pull the server's stored layout for this workspace. If the server has
  // none but we have a local one, migrate it up once so it starts
  // travelling. Returns true if the in-memory layout changed.
  async _loadServerLayout() {
    try {
      const q = this._ws() ? ('?workspace=' + encodeURIComponent(this._ws())) : '';
      const r = await this.host.api.getJSON('/api/appearance' + q);
      const dash = r && r.resolved && r.resolved.dashboard;
      if (Array.isArray(dash) && dash.length) {
        this.layout = dash;
        this._serverLoaded = true;
        return true;
      }
      // Server has no layout. Only promote a *real* prior customization
      // (a localStorage entry) — don't write the default layout up, so a
      // fresh workspace stays on dashboard:null and inherits the default.
      if (this._hadLocal) this._persistServer();
    } catch (_) {}
    return false;
  }

  _persist() {
    try { localStorage.setItem(this._lsKey(), JSON.stringify(this.layout)); } catch (_) {}
    this._persistServer();
  }

  _persistServer() {
    try {
      const q = this._ws() ? ('?workspace=' + encodeURIComponent(this._ws())) : '';
      this.host.api.putJSON('/api/appearance' + q, { dashboard: this.layout });
    } catch (_) {}
  }

  // ── lifecycle ───────────────────────────────────────────────────
  // The static shell is the only DOM rendered imperatively up front: the
  // header lede/actions + grid + scroll anchors. The header content and the
  // widget bodies render reactively into their own nodes (so live patches
  // never touch the grid shell that hosts drag/resize / plugin mounts).
  mount(container) {
    this._injectCSS();
    this.container = container;
    container.innerHTML = '';
    const pane = document.createElement('div');
    pane.className = `pane home${this.editing ? ' editing' : ''}`;
    pane.setAttribute('data-screen-label', 'Home');
    pane.innerHTML = `
      <div class="home-hdr">
        <div class="lede">
          <div class="eyebrow"></div>
          <div class="greeting"></div>
        </div>
        <div class="actions"></div>
      </div>
      <div class="home-scroll"><div class="home-grid"></div></div>`;
    container.appendChild(pane);
    this.pane = pane;
    this.gridEl = pane.querySelector('.home-grid');
    this.scrollEl = pane.querySelector('.home-scroll');
    this._renderHeader();
    this._sanitizeLayout({ autoTidy: this._layoutHasOverlaps(this.layout) });
    this._renderGrid();
    this._installLiveDriver();
    // Adopt the server-stored (per-workspace) layout once it arrives; the
    // sync local cache already painted, so this only re-renders if the
    // server had a different layout.
    this._loadServerLayout().then(changed => {
      if (!changed || !this.container) return;
      const tidied = this._sanitizeLayout({
        autoTidy: this._layoutHasOverlaps(this.layout)
          || this.layout.some(w => (Number(w.x) || 0) + (Number(w.w) || 0) > COLS),
      });
      if ((changed || tidied) && this.container) {
        if (tidied || changed) this._persist();
        this._renderHeader();
        this._renderGrid();
      }
    });
    // Expose the live instance so a `dashboard.command` SSE event (from a
    // relaydeck agent's `dashboard` tool) can reshape the grid in place.
    window.__relaydeckHome = this;
  }

  unmount() {
    if (this._activeDragStop) { try { this._activeDragStop(); } catch (_) {} this._activeDragStop = null; }
    this._runCleanups();
    for (const u of this._liveUnsubs) { try { u(); } catch (_) {} }
    this._liveUnsubs = [];
    // Re-arm the guard so a remount (the Agents lens reuses one HomeDashboard
    // instance: unmount() then mount()) re-subscribes the live driver. Without
    // this, _installLiveDriver() early-returns and the fleet/usage/agents
    // widgets stop updating on SSE/heartbeat until a full page reload.
    this._liveDriverInstalled = false;
    if (this._liveDebounce) { clearTimeout(this._liveDebounce); this._liveDebounce = null; }
    if (this._galleryEls) this._closeGallery();
    if (window.__relaydeckHome === this) window.__relaydeckHome = null;
  }

  // Apply a programmatic command (the whole {op, value, x, y, w, h} dict)
  // from a relaydeck agent's `dashboard` tool. Widget/layout ops live here;
  // theme ops (accent/density) are applied in app.js since they work
  // regardless of which lens is showing.
  applyCommand(cmd) {
    const { op, value } = cmd || {};
    if (op === 'add_widget' && value) {
      if (!this.layout.some(w => w.key === value)) this._addWidget(value);
    } else if (op === 'remove_widget' && value) {
      const w = this.layout.find(w => w.key === value);
      if (w) this._removeWidget(w.id);
    } else if (op === 'move_widget' && value) {
      this.layout = this.layout.map(w => w.key === value
        ? { ...w, x: cmd.x ?? w.x, y: cmd.y ?? w.y } : w);
      this._persist(); this._renderGrid();
    } else if (op === 'resize_widget' && value) {
      this.layout = this.layout.map(w => w.key === value
        ? { ...w, w: cmd.w ?? w.w, h: cmd.h ?? w.h } : w);
      this._persist(); this._renderGrid();
    } else if (op === 'tidy') {
      this._tidy();
    } else if (op === 'reset') {
      this.layout = DEFAULT_LAYOUT.map(w => ({ ...w }));
      this._persist(); this._renderHeader(); this._renderGrid();
    }
  }

  _runCleanups() {
    for (const fn of this._cleanups) { try { fn(); } catch (_) {} }
    this._cleanups = [];
    this._liveRerenders = [];
  }

  // Real-time: when the live collections change (or the heartbeat fires),
  // re-render the live widgets in place. Home lives in the Agents lens
  // detail (no terminal), so re-rendering widget bodies is safe. Skipped
  // while editing so a refresh can't disrupt a drag/resize.
  _installLiveDriver() {
    if (this._liveDriverInstalled) return;
    this._liveDriverInstalled = true;
    const fire = () => {
      if (this.editing) return;
      if (this._liveDebounce) return;
      this._liveDebounce = setTimeout(() => {
        this._liveDebounce = null;
        for (const fn of this._liveRerenders) { try { fn(); } catch (_) {} }
      }, 250);
    };
    this._liveUnsubs = [
      live.subscribe('/api/agents', fire),
      live.subscribe('/api/workspaces', fire),
      live.subscribe('/api/workers', fire),
      // Real per-agent 24h token rollup ({agent_id:{tokens,spark[24]}}),
      // invalidated on usage events — backs the Usage + Agents sparklines.
      live.subscribe('/api/agents/usage-rollup', (data) => {
        this._usageRollup = data || {};
        fire();
      }),
    ];
  }

  // ── header ──────────────────────────────────────────────────────
  // A warm, time-of-day greeting. Rotates by calendar day so there's
  // variety, but stays stable across the frequent live re-renders (no
  // flicker) — never random per paint.
  _greeting() {
    const now = new Date();
    const h = now.getHours();
    const band = h < 5 ? 'night' : h < 12 ? 'morning' : h < 18 ? 'afternoon' : h < 22 ? 'evening' : 'night';
    const lines = {
      morning: [
        'Good morning. The fleet woke up with you.',
        'Morning. Fresh start — let’s make something good.',
        'Rise and shine. The agents are stretching their legs.',
      ],
      afternoon: [
        'Good afternoon. The fleet’s humming along.',
        'Afternoon. Steady hands, steady agents.',
        'Hope the day’s treating you well.',
      ],
      evening: [
        'Good evening. Winding down, or just getting started?',
        'Evening. The fleet keeps watch while you breathe.',
        'Golden hour. A fine time to ship something.',
      ],
      night: [
        'Burning the midnight oil? The fleet’s right here with you.',
        'Late one tonight — the agents don’t sleep either.',
        'Quiet hours. Just you, the fleet, and the glow.',
      ],
    };
    const pool = lines[band];
    const day = Math.floor((now - new Date(now.getFullYear(), 0, 0)) / 86400000);
    return pool[day % pool.length];
  }

  // The header (lede + actions) renders reactively into the static .lede /
  // .actions nodes — never into the grid shell. Buttons bind @click here
  // instead of post-hoc onclick wiring.
  _renderHeader() {
    const tod = this._greeting();
    const n = this.layout.length;
    const rate = (this.host.state.eventsRate ?? 0).toFixed(1);
    this.pane.querySelector('.eyebrow').textContent = `home · ${n} widget${n === 1 ? '' : 's'} · ${rate} events/sec`;
    this.pane.querySelector('.greeting').textContent = tod;

    const actions = this.pane.querySelector('.actions');
    const act = (a) => () => this._headerAction(a);
    render(this.editing
      ? html`
        ${button({ size: 'sm', act: 'tidy', title: 'Snap widgets to top-left', onClick: act('tidy') }, icon('filter', 12), ' Tidy')}
        ${button({ size: 'sm', act: 'reset', title: 'Restore defaults', onClick: act('reset') }, icon('restart', 12), ' Reset')}
        ${button({ variant: 'primary', size: 'sm', act: 'add', onClick: act('add') }, icon('plus', 12), ' Add widget')}
        ${button({ variant: 'edit-on', size: 'sm', act: 'done', onClick: act('done') }, icon('cog', 12), ' Done')}`
      : html`
        ${button({ variant: 'ghost', size: 'sm', act: 'add', onClick: act('add') }, icon('plus', 12), ' Add widget')}
        ${button({ size: 'sm', act: 'customize', onClick: act('customize') }, icon('edit', 12), ' Customize')}`,
      actions);
  }

  _headerAction(act) {
    if (act === 'customize') { this.editing = true; this._reflectEditing(); this._renderHeader(); this._renderGrid(); }
    else if (act === 'done') { this.editing = false; this._reflectEditing(); this._renderHeader(); this._renderGrid(); }
    else if (act === 'add') this._openGallery();
    else if (act === 'tidy') { this._tidy(); }
    else if (act === 'reset') { this.layout = DEFAULT_LAYOUT.map(w => ({ ...w })); this._persist(); this._renderHeader(); this._renderGrid(); }
  }

  _reflectEditing() {
    this.pane.classList.toggle('editing', this.editing);
  }

  // ── grid render (IMPERATIVE — stable shells host drag/resize + mounts) ─
  // The grid is built imperatively: each widget shell is a stable node we
  // pin to grid cells (gridColumn/gridRow), wire drag/resize on, and mount
  // a widget body into. lit-html only renders the body CONTENT, never the
  // shells — so a dragging/resizing/plugin-mounted widget is never disturbed
  // by a live re-render.
  _renderGrid() {
    this._runCleanups();
    const defs = this.defs();
    this.gridEl.innerHTML = '';
    if (this.layout.length === 0) {
      this.gridEl.style.display = 'none';
      this._renderEmpty();
      return;
    }
    this.gridEl.style.display = 'grid';
    if (this._emptyEl) { this._emptyEl.remove(); this._emptyEl = null; }

    for (const w of this.layout) {
      const def = defs[w.key];
      if (!def) continue;
      const shell = this._buildShell(w, def);
      this.gridEl.appendChild(shell);
    }
    if (this.ghost) {
      const g = document.createElement('div');
      g.className = 'wgt-ghost';
      g.style.gridColumn = `${this.ghost.x + 1} / span ${this.ghost.w}`;
      g.style.gridRow = `${this.ghost.y + 1} / span ${this.ghost.h}`;
      this.gridEl.appendChild(g);
    }
  }

  _renderEmpty() {
    if (this._emptyEl) this._emptyEl.remove();
    const el = document.createElement('div');
    el.className = 'home-empty';
    render(html`
      <div class="icon-tile">${icon('grid', 28)}</div>
      <div class="ttl">Your dashboard is empty</div>
      <div class="dsc">Add widgets from the gallery — fleet status, inbox, scratchpad, anything. Plugins contribute their own.</div>
      ${button({ variant: 'primary', onClick: () => this._openGallery() }, icon('plus', 12), ' Browse widgets')}`, el);
    this.scrollEl.appendChild(el);
    this._emptyEl = el;
  }

  // Build one widget shell (stable node). The head + resize handle are
  // rendered with lit-html (events bound inline); the `.wgt-body` is the
  // anchor each widget renders its body into.
  _buildShell(w, def) {
    const shell = document.createElement('div');
    shell.className = `wgt${this.draggingId === w.id ? ' dragging' : ''}${this.ghost && this.ghost.id === w.id ? ' faded' : ''}`;
    shell.style.gridColumn = `${w.x + 1} / span ${w.w}`;
    shell.style.gridRow = `${w.y + 1} / span ${w.h}`;
    const isPlugin = def.source !== 'core' && def.source !== 'personal';
    render(html`
      <div class="wgt-head">
        ${this.editing ? html`<span class="drag-grip">⋮⋮</span>` : nothing}
        <span class="ic-w">${icon(def.icon, 12)}</span>
        <span class="ttl">${def.title}</span>
        <span class="sp"></span>
        ${isPlugin ? html`<span class="src-chip">${def.source}</span>` : nothing}
        ${this.editing ? html`<div class="ed-actions"><button class="iconbtn remove" title="Remove widget"
          @mousedown=${(e) => e.stopPropagation()}
          @click=${(e) => { e.stopPropagation(); this._removeWidget(w.id); }}>${icon('x', 12)}</button></div>` : nothing}
      </div>
      <div class="wgt-body scroll"></div>
      ${this.editing
        ? html`<div class="wgt-resize" title="Drag to resize" @mousedown=${(e) => this._onResizeStart(e, w.id)}></div>`
        : html`<div class="wgt-resize" title="Drag to resize"></div>`}`, shell);

    const body = shell.querySelector('.wgt-body');
    const cleanup = this._renderWidget(w.key, def, body);
    if (typeof cleanup === 'function') this._cleanups.push(cleanup);
    // Live widgets re-render in place when their data changes (driven by
    // the live store / heartbeat). Re-invoking returns null for these
    // (state-derived + endpoint-fetching), so cleanups don't accumulate.
    // NOTE: register unconditionally — `body` is still detached here (the
    // shell is appended to the grid by the caller), so checking
    // `isConnected` now would wrongly skip every widget. The closure
    // guards against a stale/removed body at call time instead.
    if (LIVE_WIDGET_KEYS.has(w.key)) {
      this._liveRerenders.push(() => {
        if (!body.isConnected) return;
        const c = this._renderWidget(w.key, def, body);
        if (typeof c === 'function') this._cleanups.push(c);
      });
    }

    // Drag (whole shell) — guarded so clicks on controls don't drag.
    shell.addEventListener('mousedown', (e) => this._onDragStart(e, w.id));
    return shell;
  }

  // ── drag / resize (IMPERATIVE — document mousemove/mouseup) ────────
  _geom() {
    const r = this.gridEl.getBoundingClientRect();
    const colW = (r.width - (COLS - 1) * GAP) / COLS;
    return { colW, rowH: ROW_H, gap: GAP };
  }

  _onDragStart(e, id) {
    if (!this.editing) return;
    if (e.target.closest('.iconbtn') || e.target.closest('.wgt-resize') ||
        e.target.closest('textarea') || e.target.closest('input') || e.target.closest('button')) return;
    e.preventDefault();
    const wgt = this.layout.find(w => w.id === id);
    if (!wgt) return;
    const { colW, rowH, gap } = this._geom();
    const start = { mx: e.clientX, my: e.clientY, x: wgt.x, y: wgt.y };
    this.draggingId = id;
    this._renderGrid();

    const onMove = (ev) => {
      const dcx = Math.round((ev.clientX - start.mx) / (colW + gap));
      const dcy = Math.round((ev.clientY - start.my) / (rowH + gap));
      const nx = Math.max(0, Math.min(COLS - wgt.w, start.x + dcx));
      const ny = Math.max(0, start.y + dcy);
      this.ghost = { id, x: nx, y: ny, w: wgt.w, h: wgt.h };
      this._renderGrid();
    };
    const stop = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      this._activeDragStop = null;
    };
    const onUp = () => {
      stop();
      if (this.ghost) {
        const g = this.ghost;
        this.layout = this.layout.map(w => w.id === id ? { ...w, x: g.x, y: g.y } : w);
        this._persist();
      }
      this.ghost = null; this.draggingId = null;
      this._renderGrid();
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    this._activeDragStop = stop;   // so unmount() mid-drag releases the doc listeners
  }

  _onResizeStart(e, id) {
    e.stopPropagation(); e.preventDefault();
    const wgt = this.layout.find(w => w.id === id);
    if (!wgt) return;
    const def = this.defs()[wgt.key];
    const minW = def?.min?.w || 2, minH = def?.min?.h || 2;
    const { colW, rowH, gap } = this._geom();
    const start = { mx: e.clientX, my: e.clientY, w: wgt.w, h: wgt.h };
    this.draggingId = id;

    const onMove = (ev) => {
      const nw = Math.max(minW, Math.min(COLS - wgt.x, start.w + Math.round((ev.clientX - start.mx) / (colW + gap))));
      const nh = Math.max(minH, start.h + Math.round((ev.clientY - start.my) / (rowH + gap)));
      this.ghost = { id, x: wgt.x, y: wgt.y, w: nw, h: nh };
      this._renderGrid();
    };
    const stop = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      this._activeDragStop = null;
    };
    const onUp = () => {
      stop();
      if (this.ghost) {
        const g = this.ghost;
        this.layout = this.layout.map(w => w.id === id ? { ...w, w: g.w, h: g.h } : w);
        this._persist();
      }
      this.ghost = null; this.draggingId = null;
      this._renderGrid();
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    this._activeDragStop = stop;   // so unmount() mid-resize releases the doc listeners
  }

  _removeWidget(id) {
    this.layout = this.layout.filter(w => w.id !== id);
    this._persist(); this._renderHeader(); this._renderGrid();
  }

  _addWidget(key) {
    const def = this.defs()[key];
    if (!def) return;
    const { x, y } = this._firstFit(def.def.w, def.def.h);
    this.layout = [...this.layout, { id: `w-${key}-${Date.now()}`, key, x, y, w: def.def.w, h: def.def.h }];
    this._persist(); this._renderHeader(); this._renderGrid();
  }

  // First (x, y) where a wW×wH widget fits without overlapping — so a new
  // widget fills an existing gap instead of always dropping to the bottom
  // (which left big empty bands). Falls back to a fresh row at the bottom.
  _firstFit(wW, wH) {
    const occupied = (x, y) => this.layout.some(w =>
      x >= w.x && x < w.x + w.w && y >= w.y && y < w.y + w.h);
    const fits = (x, y) => {
      for (let dy = 0; dy < wH; dy++)
        for (let dx = 0; dx < wW; dx++)
          if (occupied(x + dx, y + dy)) return false;
      return true;
    };
    const maxY = this.layout.reduce((m, w) => Math.max(m, w.y + w.h), 0);
    for (let y = 0; y <= maxY; y++)
      for (let x = 0; x <= COLS - wW; x++)
        if (fits(x, y)) return { x, y };
    return { x: 0, y: maxY };
  }

  _tidy() {
    const sorted = [...this.layout].sort((a, b) => a.y - b.y || a.x - b.x);
    const occ = [];
    const fits = (x, y, w, h) => {
      for (let r = y; r < y + h; r++) {
        const row = occ[r] || [];
        for (let c = x; c < x + w; c++) if (row[c]) return false;
      }
      return true;
    };
    const place = (x, y, w, h) => {
      for (let r = y; r < y + h; r++) {
        if (!occ[r]) occ[r] = [];
        for (let c = x; c < x + w; c++) occ[r][c] = true;
      }
    };
    const out = [];
    for (const wgt of sorted) {
      const w = Math.min(wgt.w, COLS);
      let placed = false;
      for (let y = 0; y < 400 && !placed; y++) {
        for (let x = 0; x <= COLS - w; x++) {
          if (fits(x, y, w, wgt.h)) {
            place(x, y, w, wgt.h);
            out.push({ ...wgt, x, y, w });
            placed = true;
            break;
          }
        }
      }
    }
    this.layout = out;
    this._persist();
    this._renderGrid();
  }

  // ── gallery drawer (IMPERATIVE overlay — bespoke .gallery-scrim) ────
  // Kept as an in-pane drawer (not openModal) — it lives over the home
  // pane, anchors to it, and keeps the .gallery-scrim/.gallery classes the
  // CSS + e2e selectors expect. Body content renders with lit-html.
  _openGallery() {
    if (this._galleryEls) return;
    const scrim = document.createElement('div');
    scrim.className = 'gallery-scrim';
    scrim.onclick = () => this._closeGallery();
    const drawer = document.createElement('div');
    drawer.className = 'gallery';
    this.pane.appendChild(scrim);
    this.pane.appendChild(drawer);
    this._galleryEls = { scrim, drawer };
    this._galleryQuery = '';
    this._galleryOnKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); this._closeGallery(); } };
    document.addEventListener('keydown', this._galleryOnKey);
    this._renderGallery();
  }

  _closeGallery() {
    if (!this._galleryEls) return;
    if (this._galleryOnKey) {
      document.removeEventListener('keydown', this._galleryOnKey);
      this._galleryOnKey = null;
    }
    this._galleryEls.scrim.remove();
    this._galleryEls.drawer.remove();
    this._galleryEls = null;
  }

  _renderGallery() {
    const drawer = this._galleryEls.drawer;
    const defs = this.defs();
    const q = (this._galleryQuery || '').toLowerCase();
    const usedKeys = new Set(this.layout.map(w => w.key));

    // available + matching query, grouped by source
    const groups = {};
    for (const d of Object.values(defs)) {
      if (!this._sourceActive(d.source)) continue;
      if (q && !(d.title.toLowerCase().includes(q) || (d.desc || '').toLowerCase().includes(q) || d.source.includes(q))) continue;
      (groups[d.source] ||= []).push(d);
    }
    const order = (a, b) => (a === 'core' ? -1 : b === 'core' ? 1 : a === 'personal' ? -1 : b === 'personal' ? 1 : a.localeCompare(b));
    const sources = Object.keys(groups).sort(order);
    const totalAvail = Object.values(defs).filter(d => this._sourceActive(d.source)).length;
    const pluginCount = sources.filter(s => s !== 'core' && s !== 'personal').length;

    const groupLabel = (s) => s === 'core' ? 'Core widgets' : s === 'personal' ? 'Personal' : `${s} plugin`;

    render(html`
      <div class="gallery-head">
        <div>
          <h3>Add widget</h3>
          <div class="sub">Plugins contribute their own. Pick what's useful, drop the rest.</div>
        </div>
        <button class="x" data-act="close" @click=${() => this._closeGallery()}>${icon('x', 16)}</button>
      </div>
      <div class="gallery-search">
        <span class="ic-s">${icon('search', 12)}</span>
        <input placeholder="Search widgets..." .value=${this._galleryQuery || ''}
          @input=${(e) => { this._galleryQuery = e.target.value; this._renderGallery(); }}>
      </div>
      <div class="gallery-body">
        ${sources.map(src => html`
          <div class="gallery-group">
            <div class="gallery-group-head">
              <span class="lbl">${groupLabel(src)}</span>
              <span class="src${src === 'core' ? ' core' : ''}">${groups[src].length}</span>
            </div>
            ${groups[src].map(d => {
              const added = usedKeys.has(d.key);
              return html`<div class="gallery-card${added ? ' added' : ''}" data-key=${d.key}
                @click=${() => this._addWidget(d.key)}>
                <div class="ic-tile">${icon(d.icon, 18)}</div>
                <div class="info">
                  <div class="name">${d.title}</div>
                  <div class="desc">${d.desc || ''}</div>
                  <div class="size">DEFAULT ${d.def.w}×${d.def.h}</div>
                </div>
                <button class="add-btn">${icon('plus', 10)} ${added ? 'Add again' : 'Add'}</button>
              </div>`;
            })}
          </div>`)}
        ${sources.length === 0 ? html`<div class="gallery-none">No widgets match "${this._galleryQuery || ''}"</div>` : nothing}
      </div>
      <div class="gallery-foot">
        <span>${totalAvail} widget${totalAvail === 1 ? '' : 's'} · ${pluginCount} plugin${pluginCount === 1 ? '' : 's'}</span>
        <span class="sp"></span>
        <span class="hint">plugins ship widgets in their manifest</span>
      </div>`, drawer);

    const input = drawer.querySelector('.gallery-search input');
    setTimeout(() => { try { input?.focus(); } catch (_) {} }, 0);
  }

  // ── widget dispatch ─────────────────────────────────────────────
  _renderWidget(key, def, el) {
    const m = {
      fleet: () => this._wFleet(el),
      usage: () => this._wUsage(el),
      heatmap: () => this._wHeatmap(el),
      agents: () => this._wAgents(el),
      feed: () => this._wFeed(el),
      workspaces: () => this._wWorkspaces(el),
      workers: () => this._wWorkers(el),
      spawn: () => this._wSpawn(el),
      worktrees: () => this._wWorktrees(el),
      inbox: () => this._wInbox(el),
      files: () => this._wFiles(el),
      clock: () => this._wClock(el),
      notes: () => this._wNotes(el),
      focus: () => this._wFocus(el),
      coffee: () => this._wCoffee(el),
    };
    if (m[key]) return m[key]();
    if (def.module) return this._wPluginModule(def, el);
    render(blank(html`Unknown widget: ${key}`), el);
    return null;
  }

  // Third-party plugin widget: dynamic-import its module and mount it,
  // mirroring the agent-detail tile lifecycle. The module gets a raw
  // anchor node (its own mount(el, api, ctx) owns it) — the reactive layer
  // never touches `el` again, so the foreign widget is never disturbed.
  _wPluginModule(def, el) {
    render(blank('loading…'), el);
    let inst = null;
    import(stamped(def.module)).then(mod => {
      const Klass = mod.default || mod.Widget || mod.Panel;
      if (!Klass) { render(blank('widget has no default export'), el); return; }
      el.innerHTML = '';
      inst = new Klass();
      inst.mount?.(el, this.host.api, { host: this.host });
    }).catch(e => { render(blank(html`failed: ${e.message || e}`), el); });
    return () => { try { inst?.unmount?.(); } catch (_) {} };
  }

  // ── core widgets ────────────────────────────────────────────────
  _wFleet(el) {
    const agents = this.host.scopedAgents();
    const by = (s) => agents.filter(a => visualStatus(a) === s).length;
    const running = by('running') + by('working');
    const awaiting = by('awaiting-input');
    const errored = by('errored');
    const idle = by('idle');
    const parts = [];
    if (running) parts.push(html`<span><span class="acc">${running}</span> running</span>`);
    if (awaiting) parts.push(html`<span><span class="warn">${awaiting}</span> awaiting</span>`);
    if (errored) parts.push(html`<span><span class="err">${errored}</span> errored</span>`);
    if (idle) parts.push(html`<span><span class="dim">${idle}</span> idle</span>`);
    if (!parts.length) parts.push(html`<span class="dim">fleet quiet</span>`);
    // Interleave the dotted separator between the status fragments.
    const heading = [];
    parts.forEach((p, i) => { if (i) heading.push(html`<span class="dim">  ·  </span>`); heading.push(p); });
    const u = this.host.state.usageTotals;
    const wsLabel = this.host.isAllWorkspaces() ? 'all workspaces · fleet status' : `@${this.host.state.workspace} · fleet status`;
    const sub = awaiting ? `${awaiting} paused waiting on you. Open an agent to triage, or ⌘K to jump.`
      : errored ? `${errored} need attention — check the live activity widget for the latest errors.`
      : `${running} running. ${(this.host.state.eventsRate ?? 0).toFixed(0)} events/sec across the runtime.`;
    el.classList.remove('scroll');
    render(html`<div class="w-fleet">
      <div class="eyebrow">${wsLabel}</div>
      <h1 class="h">${heading}</h1>
      <div class="sub">${sub}</div>
      <div class="meta">
        ${u ? html`<span><span class="v">${fmtNum(u.tokens || 0)}</span> tokens</span>
        <span><span class="v">${fmtCost(u.cost || 0)}</span> spent</span>` : nothing}
        <span><span class="v">${agents.length}</span> agents total</span>
      </div></div>`, el);
    return null;
  }

  // Element-wise sum of the per-agent 24h hourly token sparks for the
  // agents in scope — the real fleet token series from usage_records.
  _fleetSpark() {
    const out = new Array(24).fill(0);
    for (const a of this.host.scopedAgents()) {
      const s = this._usageRollup[a.id]?.spark;
      if (Array.isArray(s)) for (let i = 0; i < out.length && i < s.length; i++) out[i] += s[i] || 0;
    }
    return out;
  }

  _wUsage(el) {
    const u = this.host.state.usageTotals || { tokens: 0, cost: 0 };
    const sparkData = this._fleetSpark();
    const hasUsage = sparkData.some(v => v > 0);
    el.classList.remove('scroll');
    render(html`<div class="w-usage">
      <div class="row">
        <div><div class="num">${fmtNum(u.tokens || 0)}</div><div class="k">tokens · all time</div></div>
      </div>
      <div class="secondary">
        <div class="ucell"><span class="v">${fmtCost(u.cost || 0)}</span><span class="k">cost</span></div>
        <div class="ucell"><span class="v">${this.host.scopedAgents().length}</span><span class="k">agents</span></div>
        <div class="ucell"><span class="v">${(this.host.state.eventsRate ?? 0).toFixed(1)}</span><span class="k">evt/s</span></div>
      </div>
      <div class="sparkwrap">${hasUsage
        ? spark(sparkData, { height: 28, dot: true, strokeWidth: 1.4 })
        : html`<div class="spark-empty">no token usage in the last 24h</div>`}</div>
    </div>`, el);
    return null;
  }

  _wHeatmap(el) {
    el.classList.remove('scroll');
    render(blank('loading…'), el);
    this.host.api.fetch('/api/usage-heatmap?days=7').then(r => r.ok ? r.json() : null).then(d => {
      if (!el.isConnected) return;
      if (!d) { render(blank('heatmap unavailable'), el); return; }
      const max = d.max || 1;
      const bucket = (v) => !v ? 0 : (v / max > 0.66 ? 4 : v / max > 0.33 ? 3 : v / max > 0.1 ? 2 : 1);
      const fmtDay = (iso) => { try { return new Date(iso + 'T00:00:00').toLocaleDateString(undefined, { weekday: 'short' }); } catch (_) { return iso.slice(5); } };
      if ((d.total || 0) === 0) { render(blank('No token usage in the last 7 days.'), el); return; }
      render(html`<div class="w-heatmap">
        <div class="hm-grid">${(d.rows || []).map(r => html`
          <div class="hm-row"><span class="hm-day">${fmtDay(r.date)}</span><div class="hm-cells">${
            r.cells.map((v, h) => html`<span class="hm-cell l${bucket(v)}" title="${r.date} ${String(h).padStart(2, '0')}:00 · ${fmtNum(v)} tok"></span>`)
          }</div></div>`)}</div>
        <div class="hm-foot"><span>${fmtNum(d.total)} tokens · 7 days</span>
          <span class="hm-legend">less ${[0, 1, 2, 3, 4].map(l => html`<i class="hm-cell l${l}"></i>`)} more</span></div>
      </div>`, el);
    }).catch(() => { render(blank('Failed to load.'), el); });
    return null;
  }

  _wAgents(el) {
    const order = { running: 0, working: 0, 'awaiting-input': 1, errored: 2, idle: 3 };
    const agents = [...this.host.scopedAgents()].sort((a, b) => (order[visualStatus(a)] ?? 9) - (order[visualStatus(b)] ?? 9));
    if (!agents.length) { render(blank('No agents in this workspace.'), el); return null; }
    const allWs = !this.host.state.workspace;   // show @workspace only when unscoped
    const row = (a) => {
      const st = visualStatus(a);
      const cls = st === 'running' || st === 'working' ? 'running' : st === 'awaiting-input' ? 'awaiting' : st === 'errored' ? 'errored' : 'idle';
      const color = st === 'errored' ? 'var(--err)' : st === 'awaiting-input' ? 'var(--warn)' : st === 'idle' ? 'var(--t-3)' : 'var(--acc)';
      const usage = this._usageRollup[a.id] || {};
      const sp = usage.spark;
      const hasUsage = Array.isArray(sp) && sp.some(v => v > 0);
      const toks = usage.tokens > 0 ? usage.tokens : 0;
      const label = st === 'awaiting-input' ? 'awaiting input' : st;
      const sub = cls === 'running' ? 'ok' : cls === 'awaiting' ? 'warn' : cls === 'errored' ? 'err' : 'dim';
      const model = a.config?.preset || a.config?.model || a.model || '';
      const up = a.started_at ? uptimeStr(a.started_at * 1000) : '';
      // meta: harness · model · (uptime, or @workspace when viewing all)
      const metaBits = [a.type || 'agent', model, allWs && a.workspace ? '@' + a.workspace : up].filter(Boolean);
      const meta = [];
      metaBits.forEach((b, i) => { if (i) meta.push(html`<span class="dot">·</span>`); meta.push(b); });
      return html`<div class="row" data-agent=${a.id} @click=${() => this.host.selectAgent(a.id)}>
        <div class="av-h">${agentMarkTpl(a, { size: 30, radius: 7 })}<span class="ind ${cls}"></span></div>
        <div class="info">
          <div class="nameline"><span class="name truncate">${a.name || a.id}</span><span class="st ${sub}">${label}</span></div>
          <div class="sub truncate">${meta}</div>
          ${a.purpose ? html`<div class="purpose truncate">${a.purpose}</div>` : nothing}
        </div>
        <div class="right">
          ${toks ? html`<div class="toks">${fmtNum(toks)}<span class="u">tok</span></div>` : nothing}
          ${hasUsage ? spark(sp, { width: 56, height: 16, dot: false, strokeWidth: 1.1, color, fill: false }) : nothing}
        </div>
      </div>`;
    };
    render(html`<div class="w-alist">${agents.map(row)}</div>`, el);
    return null;
  }

  _wFeed(el) {
    render(html`<div class="w-feed"></div>`, el);
    const feed = el.querySelector('.w-feed');
    const draw = () => {
      // The fleet feed is for agent/worker/plugin activity — drop local UI-only
      // noise (the operator's own theme/density toggles spam appearance.changed).
      const raw = (this.host.state.events || []).filter(e => !_FEED_HIDE.has(e.type));
      // Collapse a burst of identical (type + agent) events into one row with a
      // ×N count so it doesn't bury everything else.
      const evs = [];
      for (const e of raw) {
        const last = evs[evs.length - 1];
        if (last && last.type === e.type && (last.agent_id || '') === (e.agent_id || '')) {
          last._n = (last._n || 1) + 1; continue;
        }
        evs.push({ ...e });
        if (evs.length >= 60) break;
      }
      if (!evs.length) {
        render(blank('No events yet. Activity from agents, workers, and plugins will stream here.'), feed);
        return;
      }
      render(evs.map(e => {
        const g = _glyph(e.type);
        const ws = e.workspace || e.data?.workspace || '';
        return html`<div class="evt"><span class="t">${formatTs(e.ts || e.created_at)}</span>
          <span class="g ${g.color}">${icon(g.icon, 12)}</span>
          <span class="msg">${e.agent_id ? html`<span class="ref">${e.agent_id}</span>` : nothing}<span class="lbl">${_eventLabel(e)}</span>${e._n > 1 ? html`<span class="n">×${e._n}</span>` : nothing}</span>
          <span class="ws">${ws ? '@' + ws : ''}</span></div>`;
      }), feed);
    };
    draw();
    const unsub = this.host.subscribeEvents(() => draw());
    return unsub;
  }

  _wWorkspaces(el) {
    const wss = this.host.state.workspaces || [];
    render(html`<div class="w-ws">${wss.map(w => {
      const active = w.name === this.host.state.workspace;
      const nActive = (w.active != null) ? w.active : (this.host.state.agents || []).filter(a => a.workspace === w.name && (visualStatus(a) === 'running' || visualStatus(a) === 'working')).length;
      const nAgents = (w.agents != null) ? w.agents : (this.host.state.agents || []).filter(a => a.workspace === w.name).length;
      return html`<div class="ws-card${active ? ' active' : ''}" data-ws=${w.name}
        @click=${() => this.host.setWorkspace(w.name)}>
        <div class="name">@${w.name}</div>
        <div class="meta"><span><span class="ok">${nActive}</span>/${nAgents} active</span><span>${(w.plugins || []).length} plugins</span></div>
      </div>`;
    })}</div>`, el);
    return null;
  }

  _wWorktrees(el) {
    render(blank('loading…'), el);
    this.host.api.fetch('/api/worktrees').then(r => r.ok ? r.json() : []).then(rows => {
      if (!el.isConnected) return;
      rows = rows || [];
      const dirty = rows.filter(w => w.status && w.status.dirty).length;
      const head = html`<div class="w-wt-head">
        <span><b>${rows.length}</b> worktree${rows.length === 1 ? '' : 's'}${dirty ? html` · <span class="warn">${dirty} dirty</span>` : nothing}</span>
        <button class="w-wt-new" data-act="new-wt"
          @click=${(e) => { e.stopPropagation(); openNewWorktreeModal(this.host, () => live.invalidate('/api/worktrees')); }}>${icon('plus', 10)} new</button></div>`;
      if (!rows.length) {
        render(html`${head}${blank('No git worktrees. Create one to work a branch in parallel.', 'padding-top:6px')}`, el);
        return;
      }
      const goto = (name) => { this.host.setWorkspace(name); this.host.setLens('workspaces'); };
      render(html`${head}<div class="w-wt-list">${rows.slice(0, 8).map(w => {
        const s = w.status || {};
        const flags = [];
        if (s.ahead) flags.push(`↑${s.ahead}`);
        if (s.behind) flags.push(`↓${s.behind}`);
        return html`<div class="w-wt-row" data-ws=${w.name} @click=${() => goto(w.name)}>
          <div class="info"><div class="name">${w.name}</div>
            <div class="sub">${icon('git', 9)} ${s.branch || '—'} ${flags.join(' ')}</div></div>
          ${s.dirty ? html`<span class="wt-dot" title="uncommitted changes"></span>` : nothing}
          <span class="ag">${(w.agents || []).length} ag</span>
        </div>`;
      })}</div>`, el);
    }).catch(() => { render(blank('worktrees unavailable'), el); });
    return null;
  }

  _wWorkers(el) {
    render(blank('loading…'), el);
    this.host.api.fetch('/api/workers').then(r => r.ok ? r.json() : []).then(workers => {
      if (!el.isConnected) return;
      const list = this.host.scopedWorkers ? this.host.scopedWorkers(workers) : (workers || []);
      if (!list.length) { render(blank('No background workers running.'), el); return; }
      render(html`<div class="w-workers">${list.slice(0, 8).map((w) => {
        const name = w.name || w.id || 'worker';
        const plugin = w.plugin || (w.config && w.config.workspace) || '';
        const ticks = (w.tick_count != null ? Number(w.tick_count) : 0).toLocaleString();
        const interval = w.interval_s ? `every ${w.interval_s}s` : 'event-driven';
        const lastTs = w.last_tick_at || 0;
        const last = lastTs ? relTime(lastTs * 1000) : 'idle';
        const cls = w.last_error ? 'err' : (w.alive || w.status === 'running') ? 'ok' : 'idle';
        const sub = [plugin, interval].filter(Boolean).join(' · ');
        return html`<div class="wk-row">
          <div class="dot ${cls}" title=${w.last_error || w.status || ''}></div>
          <div class="info"><div class="name">${name}</div><div class="sub">${sub}</div></div>
          <div class="ticks"><span class="v">${ticks}</span> ticks</div>
          <span class="tick">${last}</span>
        </div>`;
      })}</div>`, el);
    }).catch(() => { render(blank('Failed to load workers.'), el); });
    return null;
  }

  _wSpawn(el) {
    el.classList.remove('scroll');
    render(html`<div class="w-spawn">
      <div class="prompt" @click=${() => this.host.openNewAgent()}><span class="chev">›</span><span>spawn an agent...</span></div>
      <div class="row">${['codex-cli', 'claude-code', 'pi'].map(t =>
        html`<button class="tag" @click=${() => this.host.openNewAgent({ type: t })}>${t}</button>`)}</div>
      <div class="hint">Or press <kbd>⌘N</kbd> from anywhere.</div></div>`, el);
    return null;
  }

  // ── plugin-gated widgets (real data) ────────────────────────────
  _wInbox(el) {
    const ws = this.host.state.workspace;
    if (!ws) { render(blank('Pick a workspace to see its inbox.'), el); return null; }
    render(blank('loading…'), el);
    this.host.api.fetch(`/api/workspaces/${encodeURIComponent(ws)}/messages?limit=40`).then(r => r.ok ? r.json() : []).then(rows => {
      if (!el.isConnected) return;
      const msgs = Array.isArray(rows) ? rows : (rows.messages || []);
      // Group into threads by unordered pair.
      const threads = new Map();
      for (const m of msgs) {
        const from = m.from_id || m.from || '?', to = m.to_id || m.to || '?';
        const key = [from, to].sort().join('↔');
        if (!threads.has(key)) threads.set(key, { pair: [from, to], last: m, ts: m.ts || m.created_at || 0, count: 0, unread: 0 });
        const t = threads.get(key);
        t.count++;
        const ts = m.ts || m.created_at || 0;
        if (ts >= t.ts) { t.ts = ts; t.last = m; }
        if (m.delivery_state === 'queued' || m.read === false) t.unread++;
      }
      const list = [...threads.values()].sort((a, b) => (b.unread - a.unread) || (b.ts - a.ts));
      if (!list.length) { render(blank('No messages yet.'), el); return; }
      render(html`<div class="w-inbox">${list.slice(0, 20).map(t => {
        const body = (t.last.body || '').split('\n')[0];
        return html`<div class="thr">
          <div class="av">${(t.pair[0] || '?')[0].toUpperCase()}</div>
          <div class="info"><div class="name truncate">${t.pair[0]} <span style="color:var(--t-4)">↔</span> ${t.pair[1]}</div>
            <div class="preview">${(t.last.from_id || t.last.from || '?') + ': ' + body}</div></div>
          <div class="meta">${t.unread ? html`<span class="ub">${t.unread}</span>` : nothing}<span>${relTime(t.ts)}</span></div>
        </div>`;
      })}</div>`, el);
    }).catch(() => { render(blank('Failed to load inbox.'), el); });
    return null;
  }

  _wFiles(el) {
    render(html`<div class="w-files"></div>`, el);
    const box = el.querySelector('.w-files');
    const ops = { added: 'A', created: 'A', modified: 'M', changed: 'M', deleted: 'D', removed: 'D' };
    const draw = () => {
      const evs = (this.host.state.events || []).filter(e => (e.type || '').includes('file')).slice(0, 30);
      if (!evs.length) { render(blank('No file events yet. The file-watcher emits them as files change.'), box); return; }
      render(evs.map(e => {
        const path = e.data?.path || e.data?.file || '';
        const opRaw = (e.data?.op || e.data?.change || (e.type || '').split('.').pop() || 'M');
        const op = ops[String(opRaw).toLowerCase()] || (String(opRaw)[0] || 'M').toUpperCase();
        const cls = op === 'A' ? 'add' : op === 'D' ? 'del' : 'mod';
        const slash = path.lastIndexOf('/');
        const dir = slash >= 0 ? path.slice(0, slash + 1) : '';
        const base = slash >= 0 ? path.slice(slash + 1) : path;
        return html`<div class="fl"><span class="g ${cls}">${op}</span>
          <span class="path"><span class="dir">${dir}</span>${base}</span>
          <span class="t">${formatTs(e.ts || e.created_at)}</span></div>`;
      }), box);
    };
    draw();
    return this.host.subscribeEvents((e) => { if ((e.type || '').includes('file')) draw(); });
  }

  // ── personal widgets (IMPERATIVE timers — registered for cleanup) ──
  _wClock(el) {
    el.classList.remove('scroll');
    render(html`<div class="w-clock"><div class="time"></div><div class="date"></div></div>`, el);
    const timeEl = el.querySelector('.time'), dateEl = el.querySelector('.date');
    const tick = () => {
      const n = new Date();
      const p = (x) => String(x).padStart(2, '0');
      render(html`${p(n.getHours())}:${p(n.getMinutes())}<span class="secs">:${p(n.getSeconds())}</span>`, timeEl);
      dateEl.textContent = n.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }).toUpperCase();
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }

  _wNotes(el) {
    const NK = 'relaydeck.home.notes';
    el.classList.remove('scroll');
    let text = '';
    try { text = localStorage.getItem(NK) || ''; } catch (_) {}
    let timer = null;
    const onInput = (e) => {
      const ta = e.target;
      chars.textContent = `${ta.value.length} chars`;
      state.textContent = 'saving...'; state.classList.remove('saved');
      clearTimeout(timer);
      timer = setTimeout(() => { try { localStorage.setItem(NK, ta.value); } catch (_) {} state.textContent = '✓ saved'; state.classList.add('saved'); }, 400);
    };
    render(html`<div class="w-notes">
      <textarea placeholder="// Quick notes — todo, ideas, half-thoughts.&#10;// Only stored in your browser. Agents never see this."
        .value=${text} @mousedown=${(e) => e.stopPropagation()} @input=${onInput}></textarea>
      <div class="foot"><span class="chars">${text.length} chars</span><span class="state saved">✓ saved</span></div></div>`, el);
    const chars = el.querySelector('.chars'), state = el.querySelector('.state');
    return () => clearTimeout(timer);
  }

  _wFocus(el) {
    el.classList.remove('scroll');
    const TOTAL = 25 * 60;
    let secs = 25 * 60, running = false, id = null;
    const C = 2 * Math.PI * 36;
    // Static structure (incl. the SVG dial) renders once; paint() ticks the
    // readout/progress imperatively each second (no reactive churn on the SVG).
    render(html`<div class="w-focus">
      <div class="dial">
        <svg viewBox="0 0 90 90">
          <circle cx="45" cy="45" r="36" fill="none" stroke="var(--line-2)" stroke-width="3.5"/>
          <circle class="prog" cx="45" cy="45" r="36" fill="none" stroke="var(--acc)" stroke-width="3.5" stroke-linecap="round" transform="rotate(-90 45 45)" style="filter:drop-shadow(0 0 4px var(--acc-glow));transition:stroke-dashoffset .5s linear"/>
        </svg>
        <div class="readout"><div class="t"></div><div class="lbl"></div></div>
      </div>
      <div class="ctl"><div class="btns">
        <button class="fbtn toggle"
          @mousedown=${(e) => e.stopPropagation()}
          @click=${(e) => { e.stopPropagation(); running = !running; running ? start() : stop(); paint(); }}></button>
        <button class="fbtn ghost reset"
          @mousedown=${(e) => e.stopPropagation()}
          @click=${(e) => { e.stopPropagation(); stop(); secs = TOTAL; running = false; paint(); }}>${icon('restart', 11)} Reset</button>
      </div><div class="meta"><span class="dim">heads-down pomodoro</span></div></div></div>`, el);
    const prog = el.querySelector('.prog'), tEl = el.querySelector('.readout .t'), lbl = el.querySelector('.readout .lbl');
    const toggle = el.querySelector('.toggle');
    prog.setAttribute('stroke-dasharray', C);
    const paint = () => {
      const pct = (TOTAL - secs) / TOTAL;
      prog.setAttribute('stroke-dashoffset', C * (1 - pct));
      const p = (x) => String(x).padStart(2, '0');
      tEl.textContent = `${p(Math.floor(secs / 60))}:${p(secs % 60)}`;
      lbl.textContent = running ? 'FOCUS' : 'PAUSED';
      render(html`${icon(running ? 'stop' : 'play', 11)} ${running ? 'Pause' : 'Start'}`, toggle);
    };
    const start = () => { if (id) return; id = setInterval(() => { secs = secs > 0 ? secs - 1 : 0; paint(); }, 1000); };
    const stop = () => { clearInterval(id); id = null; };
    paint();
    return () => stop();
  }

  _wCoffee(el) {
    const CK = 'relaydeck.home.coffee';
    el.classList.remove('scroll');
    const load = () => {
      try {
        const r = JSON.parse(localStorage.getItem(CK) || '{}');
        if (r.day !== new Date().toDateString()) return { count: 0, lastAt: null };
        return { count: r.count || 0, lastAt: r.lastAt || null };
      } catch (_) { return { count: 0, lastAt: null }; }
    };
    let state = load();
    const add = (e) => {
      e.stopPropagation();
      state = { count: state.count + 1, lastAt: Date.now() };
      try { localStorage.setItem(CK, JSON.stringify({ day: new Date().toDateString(), ...state })); } catch (_) {}
      paint();
    };
    const reset = (e) => {
      e.stopPropagation();
      state = { count: 0, lastAt: null };
      try { localStorage.removeItem(CK); } catch (_) {}
      paint();
    };
    const paint = () => {
      const display = Math.max(1, Math.min(state.count || 1, 8));
      const cups = [];
      for (let i = 0; i < display; i++) cups.push(html`<span class=${i >= state.count ? 'ghost' : ''}>☕</span>`);
      const ago = state.lastAt ? Math.max(0, Math.floor((Date.now() - state.lastAt) / 60000)) : null;
      render(html`<div class="w-coffee" @mousedown=${(e) => e.stopPropagation()}>
        <div class="cups">${cups}</div>
        <div class="num">${state.count}<span class="lbl">today</span></div>
        <div class="meta">${state.lastAt ? `last cup ${ago < 1 ? 'just now' : ago + 'm ago'}` : 'no caffeine yet'}</div>
        <div class="actions"><button class="add" @click=${add}>+ log a cup</button>${state.count > 0 ? html`<button class="reset" @click=${reset}>reset</button>` : nothing}</div>
      </div>`, el);
    };
    paint();
    return null;
  }

  // ── CSS ─────────────────────────────────────────────────────────
  _injectCSS() {
    if (document.getElementById('relaydeck-home-css')) return;
    const s = document.createElement('style');
    s.id = 'relaydeck-home-css';
    s.textContent = HOME_CSS;
    document.head.appendChild(s);
  }
}

// agentMark() (harness_brand.js) returns an SVG string — wrap it as a Lit
// template via the kit's unsafeHTML so it interpolates raw, not escaped.
function agentMarkTpl(agent, opts) {
  return html`${unsafeHTML(agentMark(agent, opts))}`;
}

// Local UI-only events that aren't fleet activity — filtered from the feed.
const _FEED_HIDE = new Set(['appearance.changed']);

// event type → { heroicon name, colour class }. Vector glyphs, not unicode.
function _glyph(type) {
  const t = type || '';
  if (t.includes('error')) return { icon: 'alert', color: 'err' };
  if (t.includes('hitl') || t.includes('escalation')) return { icon: 'bell', color: 'warn' };
  if (t.includes('awaiting')) return { icon: 'clock', color: 'warn' };
  if (t.includes('spawn') || t.includes('start') || t.includes('created')) return { icon: 'play', color: 'ok' };
  if (t.includes('stop') || t.includes('exit')) return { icon: 'stop', color: 'dim' };
  if (t.includes('message')) return { icon: 'message', color: 'acc' };
  if (t.includes('status')) return { icon: 'activity', color: 'info' };
  if (t.includes('webhook')) return { icon: 'send', color: 'acc' };
  if (t.includes('plugin')) return { icon: 'plugin', color: 'info' };
  if (t.includes('workspace')) return { icon: 'workspace', color: 'dim' };
  if (t.includes('file')) return { icon: 'edit', color: 'info' };
  return { icon: 'diamond', color: 'dim' };
}

// A human one-liner for a feed event — the detail that makes the feed worth
// reading (status transition, message snippet, error, plugin name) instead of
// a bare dotted event type.
function _eventLabel(e) {
  const t = e.type || 'event';
  const d = e.data || {};
  if (t === 'agent.status_changed') {
    const to = d.to || d.semantic_status || d.status;
    return to ? `status → ${to}` : 'status changed';
  }
  if (t.includes('message')) {
    const body = String(d.body || d.text || '').replace(/\s+/g, ' ').trim();
    return body ? `“${body.slice(0, 64)}${body.length > 64 ? '…' : ''}”` : 'message';
  }
  if (t === 'agent.error') return d.error ? `error: ${String(d.error).slice(0, 64)}` : 'error';
  if (t.includes('hitl') || t.includes('escalation')) {
    return d.message ? `needs you: ${String(d.message).slice(0, 52)}` : 'human-in-the-loop';
  }
  if (t.startsWith('system.plugin')) return `plugin ${t.split('.').pop()} · ${d.plugin || d.name || ''}`.trim();
  if (t === 'workspace.updated') return 'workspace updated';
  return t.replace(/[._]/g, ' ');
}

const HOME_CSS = `
.home { flex:1; min-height:0; min-width:0; display:flex; flex-direction:column; background:var(--bg-0); overflow:hidden; }
.home-hdr { display:flex; align-items:center; justify-content:space-between; padding:18px 24px 14px; border-bottom:1px solid var(--line-1); flex-shrink:0; gap:16px; min-width:0; }
.home-hdr .lede { min-width:0; }
.home-hdr .eyebrow { font-family:var(--f-mono); font-size:9px; letter-spacing:.18em; text-transform:uppercase; color:var(--t-4); margin-bottom:4px; }
.home-hdr .greeting { font-family:var(--f-mono); font-size:30px; font-weight:500; letter-spacing:-.02em; color:var(--t-1); line-height:1.05; }
.home-hdr .greeting .acc { color:var(--acc); }
.home-hdr .greeting .dim { color:var(--t-3); font-weight:400; }
.home-hdr .actions { display:flex; align-items:center; gap:6px; flex-shrink:0; }
.btn.edit-on { background:var(--acc-soft); border-color:var(--acc-line); color:var(--acc); }

.home-scroll { flex:1; min-height:0; overflow:auto; padding:16px 22px 32px; position:relative; }
.home-grid { position:relative; display:grid; grid-template-columns:repeat(12, minmax(0,1fr)); grid-auto-rows:${ROW_H}px; gap:${GAP}px; min-height:100%; align-content:start; }
.home.editing .home-scroll { background-image:linear-gradient(var(--acc-soft) 1px, transparent 1px), linear-gradient(90deg, var(--acc-soft) 1px, transparent 1px); background-size:100% ${ROW_H + GAP}px; background-position:0 16px; }

.wgt { background:var(--bg-1); border:1px solid var(--line-3); border-radius:var(--r-0); display:flex; flex-direction:column; overflow:hidden; position:relative; min-width:0; min-height:0; transition:border-color .12s, box-shadow .12s; }
.wgt:hover { border-color:var(--line-4); }
.home.editing .wgt { cursor:move; border-color:var(--line-4); border-style:dashed; }
.home.editing .wgt:hover { border-color:var(--acc-line); border-style:solid; box-shadow:0 0 0 1px var(--acc-line); }
.wgt.dragging { z-index:20; opacity:.95; border-color:var(--acc) !important; border-style:solid !important; box-shadow:0 0 0 1px var(--acc), 0 12px 32px rgba(0,0,0,.12); cursor:grabbing; }
.wgt.faded { opacity:.25; pointer-events:none; }
.wgt-ghost { background:var(--acc-soft); border:1px dashed var(--acc); border-radius:var(--r-0); pointer-events:none; z-index:15; }

.wgt-head { display:flex; align-items:center; gap:8px; padding:11px 14px; border-bottom:1px solid var(--line-3); flex-shrink:0; min-width:0; }
.wgt-head .ic-w { display:none; }
.wgt-head .ttl { font-family:var(--f-mono); font-size:10.5px; letter-spacing:.06em; text-transform:uppercase; color:var(--t-3); font-weight:500; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.wgt-head .src-chip { font-family:var(--f-mono); font-size:10px; color:var(--t-3); padding:0; background:transparent; border:0; letter-spacing:.04em; flex-shrink:0; display:inline-flex; align-items:center; gap:5px; }
.wgt-head .src-chip::before { content:""; width:5px; height:5px; border-radius:50%; background:var(--acc); flex:0 0 auto; }
.wgt-head .sp { flex:1; min-width:0; }
.wgt-head .ed-actions { display:flex; align-items:center; gap:2px; flex-shrink:0; }
.home.editing .wgt-head .src-chip { display:none; }
.wgt-head .ed-actions .iconbtn { width:22px; height:22px; display:grid; place-items:center; border-radius:3px; color:var(--t-3); background:transparent; border:0; cursor:pointer; }
.wgt-head .ed-actions .iconbtn:hover { color:var(--t-1); background:var(--bg-3); }
.wgt-head .ed-actions .iconbtn.remove:hover { color:var(--err); background:var(--err-soft); }
.wgt-head .drag-grip { color:var(--t-4); letter-spacing:-1px; font-family:var(--f-mono); font-size:12px; line-height:1; padding:0 6px 0 0; cursor:grab; user-select:none; flex-shrink:0; }

.wgt-body { flex:1; min-height:0; min-width:0; display:flex; flex-direction:column; overflow:hidden; }
.wgt-body.scroll { overflow:auto; }
.w-blank { padding:20px; color:var(--t-3); font-family:var(--f-mono); font-size:11px; text-align:center; }

.wgt-resize { display:none; position:absolute; bottom:0; right:0; width:18px; height:18px; cursor:nwse-resize; z-index:5; }
.wgt-resize::after { content:""; position:absolute; right:4px; bottom:4px; width:8px; height:8px; background:linear-gradient(135deg, transparent 0%, transparent 30%, var(--acc) 30%, var(--acc) 45%, transparent 45%, transparent 60%, var(--acc) 60%, var(--acc) 75%, transparent 75%); }
.home.editing .wgt-resize { display:block; }

/* fleet */
.w-fleet { padding:18px 20px; display:flex; flex-direction:column; justify-content:center; flex:1; min-width:0; }
.w-fleet .eyebrow { font-family:var(--f-mono); font-size:9px; letter-spacing:.18em; text-transform:uppercase; color:var(--t-4); margin-bottom:8px; }
.w-fleet .h { font-family:var(--f-sans); font-size:34px; font-weight:600; letter-spacing:-.03em; line-height:1.05; margin:0; }
.w-fleet .h .acc { color:var(--acc); } .w-fleet .h .warn { color:var(--warn); } .w-fleet .h .err { color:var(--err); } .w-fleet .h .dim { color:var(--t-3); }
.w-fleet .sub { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-3); margin-top:14px; max-width:560px; line-height:1.55; }
.w-fleet .meta { display:flex; gap:14px; margin-top:12px; font-family:var(--f-mono); font-size:10px; color:var(--t-4); }
.w-fleet .meta .v { color:var(--t-2); }

/* usage */
.w-usage { padding:14px 16px; gap:4px; }
.w-usage .row { display:flex; align-items:baseline; justify-content:space-between; gap:10px; }
.w-usage .num { font-family:var(--f-mono); font-size:34px; font-weight:500; letter-spacing:-.03em; color:var(--t-1); font-feature-settings:"tnum" 1; line-height:1; }
.w-usage .k { font-family:var(--f-mono); font-size:9px; letter-spacing:.14em; text-transform:uppercase; color:var(--t-4); }
.w-usage .sparkwrap { margin-top:auto; padding-top:8px; }
.w-usage .spark-empty { font-family:var(--f-mono); font-size:10px; color:var(--t-4); padding:8px 0 2px; }

/* heatmap widget */
.w-heatmap { display:flex; flex-direction:column; height:100%; padding:12px 14px; gap:8px; }
.w-heatmap .hm-grid { display:flex; flex-direction:column; gap:3px; flex:1; justify-content:center; }
.w-heatmap .hm-row { display:flex; align-items:center; gap:8px; }
.w-heatmap .hm-day { width:30px; flex-shrink:0; font-family:var(--f-mono); font-size:9px; color:var(--t-4); text-align:right; }
.w-heatmap .hm-cells { display:grid; grid-template-columns:repeat(24,1fr); gap:2px; flex:1; }
.w-heatmap .hm-cell { aspect-ratio:1; border-radius:2px; background:var(--bg-2); border:1px solid var(--line-1); }
.w-heatmap .hm-cell.l1 { background:var(--acc-soft); border-color:var(--acc-line); }
.w-heatmap .hm-cell.l2 { background:var(--acc-line); border-color:var(--acc-line); }
.w-heatmap .hm-cell.l3 { background:color-mix(in srgb, var(--acc) 60%, var(--bg-1)); border-color:var(--acc); }
.w-heatmap .hm-cell.l4 { background:var(--acc); border-color:var(--acc); }
.w-heatmap .hm-foot { display:flex; align-items:center; justify-content:space-between; gap:8px; font-family:var(--f-mono); font-size:9px; color:var(--t-4); }
.w-heatmap .hm-legend { display:flex; align-items:center; gap:2px; }
.w-heatmap .hm-legend .hm-cell { display:inline-block; width:9px; height:9px; aspect-ratio:auto; }
.w-usage .secondary { display:flex; gap:0; margin-top:12px; border-top:1px solid var(--line-1); padding-top:12px; }
.w-usage .secondary .ucell { flex:1; min-width:0; display:flex; flex-direction:column; gap:3px; font-family:var(--f-mono); padding:0 14px; }
.w-usage .secondary .ucell:first-child { padding-left:0; }
.w-usage .secondary .ucell + .ucell { border-left:1px solid var(--line-1); }
.w-usage .secondary .ucell .v { color:var(--t-1); font-size:15px; font-feature-settings:"tnum" 1; line-height:1; }
.w-usage .secondary .ucell .k { color:var(--t-4); font-size:9px; letter-spacing:.12em; text-transform:uppercase; }

/* agents list */
.w-alist { padding:4px 0; }
.w-alist .row { display:grid; grid-template-columns:30px 1fr auto; gap:11px; align-items:center; padding:8px 14px; min-width:0; cursor:pointer; }
.w-alist .row:hover { background:var(--bg-2); }
.w-alist .av-h { position:relative; width:30px; height:30px; flex-shrink:0; }
.w-alist .av-h .ind { position:absolute; right:-2px; bottom:-2px; width:9px; height:9px; border-radius:50%; border:2px solid var(--bg-1); }
.w-alist .av-h .ind.running { background:var(--ok); } .w-alist .av-h .ind.awaiting { background:var(--warn); } .w-alist .av-h .ind.errored { background:var(--err); } .w-alist .av-h .ind.idle { background:var(--t-3); }
.w-alist .info { min-width:0; }
.w-alist .nameline { display:flex; align-items:baseline; gap:8px; min-width:0; }
.w-alist .nameline .name { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-1); font-weight:500; }
.w-alist .nameline .st { font-family:var(--f-mono); font-size:9px; letter-spacing:.04em; flex-shrink:0; }
.w-alist .nameline .st.ok { color:var(--ok); } .w-alist .nameline .st.warn { color:var(--warn); } .w-alist .nameline .st.err { color:var(--err); } .w-alist .nameline .st.dim { color:var(--t-4); }
.w-alist .sub { font-family:var(--f-mono); font-size:10px; color:var(--t-3); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.w-alist .sub .dot { color:var(--t-4); margin:0 5px; }
.w-alist .purpose { font-family:var(--f-sans); font-size:11px; color:var(--t-3); margin-top:3px; line-height:1.3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.w-alist .right { display:flex; flex-direction:column; align-items:flex-end; gap:2px; flex-shrink:0; }
.w-alist .right .toks { font-family:var(--f-mono); font-size:11px; color:var(--t-2); font-variant-numeric:tabular-nums; }
.w-alist .right .toks .u { color:var(--t-4); font-size:9px; margin-left:2px; }

/* feed */
.w-feed { padding:2px 0; }
.w-feed .evt { display:grid; grid-template-columns:54px 16px 1fr auto; gap:8px; padding:5px 14px; font-family:var(--f-mono); font-size:11px; align-items:center; min-width:0; }
.w-feed .evt .t { color:var(--t-4); font-size:10px; }
.w-feed .evt .g { display:grid; place-items:center; color:var(--t-3); }
.w-feed .evt .g.ok { color:var(--ok); } .w-feed .evt .g.err { color:var(--err); } .w-feed .evt .g.warn { color:var(--warn); } .w-feed .evt .g.acc { color:var(--acc); } .w-feed .evt .g.info { color:var(--info); } .w-feed .evt .g.dim { color:var(--t-4); }
.w-feed .evt .msg { color:var(--t-1); min-width:0; display:flex; align-items:center; gap:6px; }
.w-feed .evt .msg .ref { color:var(--acc); flex-shrink:0; }
.w-feed .evt .msg .lbl { color:var(--t-2); min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.w-feed .evt .msg .n { color:var(--t-4); font-size:9px; background:var(--bg-2); border:1px solid var(--line-2); border-radius:999px; padding:0 5px; flex-shrink:0; }
.w-feed .evt .ws { color:var(--t-4); font-size:10px; flex-shrink:0; }

/* workspaces */
.w-ws { display:grid; grid-template-columns:repeat(auto-fill, minmax(160px,1fr)); gap:8px; padding:12px; }
.w-ws .ws-card { background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-2); padding:10px 12px; min-width:0; position:relative; cursor:pointer; }
.w-ws .ws-card:hover { background:var(--bg-3); border-color:var(--line-3); }
.w-ws .ws-card.active { border-color:var(--acc-line); background:var(--acc-soft); }
.w-ws .ws-card.active::before { content:""; position:absolute; left:-1px; top:8px; bottom:8px; width:2px; background:var(--acc); border-radius:1px; }
.w-ws .ws-card .name { font-family:var(--f-mono); font-size:var(--t-sm); color:var(--t-1); font-weight:500; }
.w-ws .ws-card.active .name { color:var(--acc); }
.w-ws .ws-card .meta { font-family:var(--f-mono); font-size:10px; color:var(--t-3); margin-top:4px; display:flex; gap:10px; }
.w-ws .ws-card .meta .ok { color:var(--ok); }

/* workers */
.w-workers { padding:4px 0; min-width:0; overflow:hidden; }
.w-workers .wk-row { display:grid; grid-template-columns:6px minmax(0,1fr) auto auto; gap:8px; align-items:center; padding:7px 14px; min-width:0; overflow:hidden; }
.w-workers .wk-row:hover { background:var(--bg-2); }
.w-workers .wk-row .dot { width:6px; height:6px; border-radius:50%; background:var(--t-4); flex-shrink:0; }
.w-workers .wk-row .dot.ok { background:var(--ok); }
.w-workers .wk-row .dot.err { background:var(--err); }
.w-workers .wk-row .dot.idle { background:var(--t-4); }
.w-workers .wk-row .info { min-width:0; }
.w-workers .wk-row .info .name { font-family:var(--f-mono); font-size:11px; color:var(--t-1); min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.w-workers .wk-row .info .sub { font-family:var(--f-mono); font-size:10px; color:var(--t-4); margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.w-workers .wk-row .ticks { font-family:var(--f-mono); font-size:10px; color:var(--t-4); flex-shrink:0; white-space:nowrap; }
.w-workers .wk-row .ticks .v { color:var(--t-2); font-size:11px; }
.w-workers .wk-row .tick { font-family:var(--f-mono); font-size:10px; color:var(--t-3); flex-shrink:0; white-space:nowrap; max-width:72px; overflow:hidden; text-overflow:ellipsis; }

/* spawn */
.w-spawn { padding:12px 14px; display:flex; flex-direction:column; gap:10px; flex:1; min-height:0; }
.w-spawn .prompt { background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-2); padding:8px 10px; font-family:var(--f-mono); font-size:11px; color:var(--t-4); display:flex; align-items:center; gap:6px; cursor:pointer; }
.w-spawn .prompt:hover { border-color:var(--acc-line); color:var(--t-2); }
.w-spawn .prompt .chev { color:var(--acc); }
.w-spawn .row { display:flex; gap:6px; flex-wrap:wrap; }
.w-spawn .row .tag { font-family:var(--f-mono); font-size:10px; background:var(--bg-2); border:1px solid var(--line-2); padding:4px 8px; border-radius:var(--r-1); color:var(--t-2); cursor:pointer; }
.w-spawn .row .tag:hover { color:var(--acc); border-color:var(--acc-line); }
.w-spawn .hint { font-family:var(--f-mono); font-size:10px; color:var(--t-4); margin-top:auto; }
.w-spawn .hint kbd { border:1px solid var(--line-2); padding:0 4px; border-radius:3px; background:var(--bg-2); color:var(--t-2); font-size:9px; }

/* inbox */
.w-inbox .thr { display:grid; grid-template-columns:26px 1fr auto; gap:10px; align-items:center; padding:9px 14px; border-bottom:1px solid var(--line-1); min-width:0; }
.w-inbox .thr:last-child { border-bottom:0; }
.w-inbox .thr:hover { background:var(--bg-2); }
.w-inbox .av { width:26px; height:26px; border-radius:var(--r-2); display:grid; place-items:center; font-family:var(--f-mono); font-size:11px; font-weight:600; background:var(--bg-2); border:1px solid var(--line-2); color:var(--t-2); flex-shrink:0; }
.w-inbox .info { min-width:0; }
.w-inbox .info .name { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-1); }
.w-inbox .info .preview { font-size:11px; color:var(--t-3); margin-top:2px; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.w-inbox .meta { display:flex; flex-direction:column; align-items:flex-end; gap:4px; font-family:var(--f-mono); font-size:9px; color:var(--t-4); flex-shrink:0; }
.w-inbox .meta .ub { background:var(--acc); color:var(--acc-text); font-size:9px; font-weight:700; min-width:16px; padding:1px 5px; border-radius:8px; text-align:center; }

/* files */
.w-files .fl { display:grid; grid-template-columns:20px 1fr auto; gap:10px; align-items:baseline; padding:6px 14px; font-family:var(--f-mono); font-size:11px; min-width:0; }
.w-files .fl:hover { background:var(--bg-2); }
.w-files .fl .g { color:var(--t-3); }
.w-files .fl .g.add { color:var(--ok); } .w-files .fl .g.mod { color:var(--warn); } .w-files .fl .g.del { color:var(--err); }
.w-files .fl .path { color:var(--t-1); min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.w-files .fl .path .dir { color:var(--t-4); }
.w-files .fl .t { color:var(--t-4); font-size:10px; }

/* clock */
.w-clock { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; padding:14px; }
.w-clock .time { font-family:var(--f-mono); font-size:40px; font-weight:500; letter-spacing:-.04em; color:var(--t-1); font-feature-settings:"tnum" 1; line-height:1; }
.w-clock .time .secs { color:var(--t-3); font-size:22px; }
.w-clock .date { font-family:var(--f-mono); font-size:11px; color:var(--t-3); letter-spacing:.04em; }

/* notes */
.w-notes { padding:12px 14px; display:flex; flex-direction:column; flex:1; min-height:0; }
.w-notes textarea { flex:1; min-height:0; background:transparent; border:0; resize:none; padding:0; font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-1); line-height:1.65; outline:none; }
.w-notes textarea::placeholder { color:var(--t-4); }
.w-notes .foot { display:flex; justify-content:space-between; align-items:center; font-family:var(--f-mono); font-size:9px; color:var(--t-4); margin-top:8px; padding-top:8px; border-top:1px dashed var(--line-1); letter-spacing:.08em; text-transform:uppercase; }
.w-notes .foot .saved { color:var(--ok); }

/* focus */
.w-focus { padding:14px; flex:1; min-height:0; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:14px; }
.w-focus .dial { position:relative; width:130px; height:130px; flex-shrink:0; }
.w-focus .dial svg { width:100%; height:100%; display:block; }
.w-focus .dial .readout { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:2px; }
.w-focus .dial .readout .t { font-family:var(--f-mono); font-size:26px; font-weight:500; letter-spacing:-.03em; color:var(--t-1); font-feature-settings:"tnum" 1; line-height:1; }
.w-focus .dial .readout .lbl { font-family:var(--f-mono); font-size:9px; letter-spacing:.22em; color:var(--acc); }
.w-focus .ctl { display:flex; flex-direction:column; align-items:center; gap:8px; }
.w-focus .ctl .btns { display:flex; gap:6px; }
.w-focus .fbtn { display:inline-flex; align-items:center; gap:6px; height:26px; padding:0 10px; background:var(--acc); color:var(--acc-text); border:0; border-radius:var(--r-2); font-family:var(--f-mono); font-size:10px; font-weight:600; letter-spacing:.06em; text-transform:uppercase; cursor:pointer; }
.w-focus .fbtn.ghost { background:transparent; border:1px solid var(--line-2); color:var(--t-2); }
.w-focus .fbtn.ghost:hover { background:var(--bg-2); color:var(--t-1); border-color:var(--line-3); }
.w-focus .meta { font-family:var(--f-mono); font-size:10px; color:var(--t-3); }

/* coffee */
.w-coffee { padding:14px; flex:1; min-height:0; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:6px; }
.w-coffee .cups { font-size:17px; line-height:1.1; display:flex; flex-wrap:wrap; justify-content:center; gap:1px; max-width:140px; }
.w-coffee .cups .ghost { opacity:.18; filter:grayscale(1); }
.w-coffee .num { font-family:var(--f-mono); font-size:32px; font-weight:500; color:var(--t-1); letter-spacing:-.03em; font-feature-settings:"tnum" 1; display:flex; align-items:baseline; gap:6px; line-height:1; margin-top:4px; }
.w-coffee .num .lbl { font-family:var(--f-mono); font-size:9px; color:var(--t-4); letter-spacing:.18em; text-transform:uppercase; }
.w-coffee .meta { font-family:var(--f-mono); font-size:10px; color:var(--t-3); }
.w-coffee .actions { display:flex; gap:6px; margin-top:4px; }
.w-coffee .add { padding:6px 12px; background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-2); font-family:var(--f-mono); font-size:10px; color:var(--t-2); letter-spacing:.04em; cursor:pointer; }
.w-coffee .add:hover { background:var(--acc-soft); border-color:var(--acc-line); color:var(--acc); }
.w-coffee .reset { padding:6px 8px; font-family:var(--f-mono); font-size:9px; color:var(--t-4); background:transparent; border:0; cursor:pointer; }
.w-coffee .reset:hover { color:var(--t-2); }

/* empty */
.home-empty { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:14px; padding:80px 20px; text-align:center; }
.home-empty .icon-tile { width:64px; height:64px; display:grid; place-items:center; background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-3); color:var(--acc); }
.home-empty .ttl { font-family:var(--f-sans); font-size:20px; font-weight:600; letter-spacing:-.01em; color:var(--t-1); }
.home-empty .dsc { font-family:var(--f-mono); font-size:11px; color:var(--t-3); max-width:340px; line-height:1.5; }

/* gallery */
.gallery-scrim { position:absolute; inset:0; background:rgba(0,0,0,.40); z-index:30; }
.gallery { position:absolute; top:0; right:0; bottom:0; width:min(460px,90%); background:var(--bg-1); border-left:1px solid var(--line-3); box-shadow:0 1px 0 var(--line-2), -20px 0 60px rgba(0,0,0,.12); z-index:31; display:flex; flex-direction:column; }
.gallery-head { padding:16px 18px 14px; border-bottom:1px solid var(--line-1); display:flex; align-items:center; justify-content:space-between; flex-shrink:0; }
.gallery-head h3 { margin:0; font-family:var(--f-sans); font-size:18px; font-weight:600; letter-spacing:-.015em; }
.gallery-head .sub { font-family:var(--f-mono); font-size:10px; color:var(--t-3); margin-top:4px; }
.gallery-head .x { padding:4px; color:var(--t-3); background:transparent; border:0; cursor:pointer; }
.gallery-head .x:hover { color:var(--t-1); }
.gallery-search { padding:10px 14px; border-bottom:1px solid var(--line-1); position:relative; flex-shrink:0; }
.gallery-search input { width:100%; height:30px; padding-left:28px; background:var(--bg-2); border:1px solid var(--line-2); font-family:var(--f-mono); font-size:11px; color:var(--t-1); border-radius:var(--r-1); }
.gallery-search .ic-s { position:absolute; left:22px; top:50%; transform:translateY(-50%); color:var(--t-3); display:inline-flex; }
.gallery-body { flex:1; overflow:auto; padding:8px 0 24px; min-height:0; }
.gallery-group { padding:6px 14px 2px; }
.gallery-group-head { display:flex; align-items:center; justify-content:space-between; padding:10px 4px 6px; }
.gallery-group-head .lbl { font-family:var(--f-mono); font-size:9px; letter-spacing:.16em; text-transform:uppercase; color:var(--t-4); }
.gallery-group-head .src { font-family:var(--f-mono); font-size:9px; background:var(--bg-2); border:1px solid var(--line-2); padding:1px 6px; border-radius:3px; color:var(--t-3); }
.gallery-card { display:grid; grid-template-columns:36px 1fr auto; gap:12px; align-items:center; padding:10px; border-radius:var(--r-2); margin-bottom:2px; min-width:0; position:relative; border:1px solid transparent; cursor:pointer; }
.gallery-card:hover { background:var(--bg-2); border-color:var(--line-3); }
.gallery-card .ic-tile { width:36px; height:36px; display:grid; place-items:center; background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-2); color:var(--acc); flex-shrink:0; }
.gallery-card.added .ic-tile { background:var(--acc-soft); border-color:var(--acc-line); }
.gallery-card .info { min-width:0; }
.gallery-card .info .name { font-family:var(--f-sans); font-size:13px; color:var(--t-1); font-weight:500; }
.gallery-card .info .desc { font-family:var(--f-mono); font-size:10px; color:var(--t-3); margin-top:3px; line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.gallery-card .info .size { font-family:var(--f-mono); font-size:9px; color:var(--t-4); margin-top:4px; letter-spacing:.08em; }
.gallery-card .add-btn { height:26px; padding:0 10px; background:var(--acc); color:var(--acc-text); border:0; border-radius:var(--r-1); font-family:var(--f-mono); font-size:10px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; display:inline-flex; align-items:center; gap:4px; flex-shrink:0; cursor:pointer; }
.gallery-card.added .add-btn { background:transparent; color:var(--ok); border:1px solid rgba(74,222,128,.30); }
.gallery-none { padding:40px; text-align:center; color:var(--t-3); font-family:var(--f-mono); font-size:11px; }
.gallery-foot { padding:12px 14px; border-top:1px solid var(--line-1); display:flex; align-items:center; gap:10px; font-family:var(--f-mono); font-size:10px; color:var(--t-3); flex-shrink:0; }
.gallery-foot .sp { flex:1; }
.gallery-foot .hint { color:var(--t-4); }
`;
