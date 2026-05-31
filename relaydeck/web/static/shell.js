// shell.js — Header, ActivityRail, StatusBar
//
// MIGRATION (the @relaydeck/ui kit, build-less light-DOM Lit). The three chrome
// surfaces are now RelayElements (<rd-header>/<rd-rail>/<rd-statusbar>), each with
// a reactive `state` prop. The legacy controller contract is preserved exactly:
//
//   const header = mountHeader(host);   // app.js
//   appEl.appendChild(header.el);       // el === the custom element instance
//   header.update(state);               // sets el.state → reactive re-render
//
// Light DOM means the existing global CSS classes + every e2e selector keep
// matching for free (.hdr/[data-act]/.ws-pill/.plug-pill/.notif-dot/.live-dot,
// .rail/.rail-btn, .bar/[data-k], .plug-pop, …).
//
// The workspace switcher popover stays IMPERATIVE — it's a popover lifecycle
// (outside-click + Esc listeners, focus-trap-free transient overlay) that Lit
// would only endanger. It's rendered with a one-shot lit-html template but
// mounted/torn-down by hand against the static `.ws-pill-wrap` anchor the
// reactive layer never re-renders, and its document listeners are cleaned up on
// close / disconnect — exactly the pattern lenses/agents.js uses for popovers.

import {
  RelayElement, html, nothing, render, unsafeHTML,
  icon, button, BRAND_SVG,
} from '@relaydeck/ui';
import { openAddWorkspaceModal } from './overlays.js';
import { uptimeStr } from './primitives.js';

// ─── Header ────────────────────────────────────────────────────────
class RdHeader extends RelayElement {
  static properties = { state: { attribute: false } };

  constructor() {
    super();
    this.host = null;
    this.state = {};
    this._pop = null;           // active workspace popover element
    this._removePopOutside = () => {};
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._closeWorkspacePop();
  }

  render() {
    const s = this.state || {};
    const sse = s.sseStatus || 'connected';
    const liveCls = 'live-dot' + (sse === 'connected' ? '' : ' ' + sse);
    const liveTxt = sse === 'connected' ? 'LIVE' : sse === 'reconnecting' ? 'RECONNECT' : 'OFFLINE';
    const wsPluginsCount = (s.workspacePlugins || []).length;
    const hasUnread = (s.unreadNotifications || 0) > 0;
    return html`
      <div class="brand">${unsafeHTML(BRAND_SVG)}<span class="brand-name">relaydeck<span class="dot">.</span></span></div>
      <div class="hdr-divider"></div>
      <div class="ws-pill-wrap" style="position:relative;flex-shrink:0">
        <button class="ws-pill" data-act="ws" @click=${this._toggleWorkspacePop}>
          ${icon('workspace', 12)}
          <span class="ws-name">${s.workspace || 'all workspaces'}</span>
          <span class="chev">▾</span>
        </button>
      </div>
      <div class="plug-pill-wrap" style="position:relative;flex-shrink:0">
        <button class="plug-pill ${s.lens === 'plugins' ? 'on' : ''}" data-act="plugins"
          @click=${() => this.host.openPlugins()}>
          <span class="lbl">plugins</span>
          <span class="count">${wsPluginsCount}</span>
        </button>
      </div>
      <button class="hdr-search" data-act="cmdk" @click=${() => this.host.openCmdK()}>
        ${icon('search', 13)}
        <span class="text">Search agents, workspaces, models, actions…</span>
        <kbd>⌘K</kbd>
      </button>
      <div class="hdr-spacer"></div>
      <div class="hdr-actions">
        ${button({ variant: 'primary', act: 'new-agent', onClick: () => this.host.openNewAgent() },
          icon('plus', 12), html`<span>New Agent</span>`)}
        <button class="btn icon" data-act="notifs" title="Notifications" style="position:relative"
          @click=${() => this.host.openNotifications()}>
          ${icon('bell', 13)}
          <span class="notif-dot" style="display:${hasUnread ? 'block' : 'none'};position:absolute;top:4px;right:4px;width:6px;height:6px;border-radius:50%;background:var(--warn);box-shadow:0 0 8px rgba(251,191,36,.55)"></span>
        </button>
        <button class="btn icon" data-act="settings" title="Settings (⌘,)"
          @click=${() => this.host.openSettings()}>${icon('cog', 13)}</button>
        <div class=${liveCls} data-live="connected">${liveTxt}</div>
      </div>`;
  }

  // ── Workspace switcher popover (imperative — transient overlay) ──────
  _toggleWorkspacePop = (e) => {
    e.stopPropagation();
    if (this._pop) { this._closeWorkspacePop(); return; }
    this._openWorkspacePop();
  };

  _openWorkspacePop() {
    const wrap = this.querySelector('.ws-pill-wrap');
    if (!wrap) return;
    const host = this.host;
    const state = host.state;
    const allActive = !state.workspace;
    const totalAgents = (state.agents || []).length;

    const pop = document.createElement('div');
    pop.className = 'plug-pop';
    const close = () => this._closeWorkspacePop();
    render(html`
      <div class="plug-pop-head">
        <span>Workspaces</span>
        <span style="letter-spacing:0;text-transform:none;color:var(--t-4)">${(state.workspaces || []).length} registered</span>
      </div>
      <div class="plug-pop-list">
        ${this._wsRow({
          name: 'All workspaces',
          desc: 'Show agents, workers, and messages across every workspace.',
          active: allActive, agents: totalAgents,
          onClick: () => { host.setWorkspace(''); close(); },
        })}
        ${(state.workspaces || []).map((w) => this._wsRow({
          name: w.name,
          desc: w.path || '',
          active: w.name === state.workspace,
          agents: (state.agents || []).filter((a) => a.workspace === w.name).length,
          truncate: true,
          onClick: () => { host.setWorkspace(w.name); close(); },
        }))}
      </div>
      <div class="plug-pop-foot">
        <button class="btn ghost sm" data-act="add" style="flex:1;justify-content:center"
          @click=${() => this._addWorkspace()}>${icon('plus', 11)} Add workspace</button>
      </div>`, pop);
    wrap.appendChild(pop);
    this._pop = pop;

    const closeIfOutside = (ev) => { if (!wrap.contains(ev.target)) close(); };
    const onKey = (ev) => { if (ev.key === 'Escape') close(); };
    // Defer the outside listeners past the opening click. Track whether they
    // actually attached + cancel the pending timer, so a same-tick open→close
    // (e.g. disconnect inside the 0ms window) can't leak a document listener.
    let added = false;
    const armTimer = setTimeout(() => {
      added = true;
      document.addEventListener('mousedown', closeIfOutside);
      document.addEventListener('keydown', onKey);
    }, 0);
    this._removePopOutside = () => {
      clearTimeout(armTimer);
      if (added) {
        document.removeEventListener('mousedown', closeIfOutside);
        document.removeEventListener('keydown', onKey);
      }
      this._removePopOutside = () => {};
    };
  }

  _wsRow({ name, desc, active, agents, truncate, onClick }) {
    return html`
      <div class="plug-row ${active ? 'on' : ''}" @click=${onClick}>
        <div class="ck">${active ? '✓' : ''}</div>
        <div class="body">
          <div class="name">${name}</div>
          <div class="desc ${truncate ? 'truncate' : ''}">${desc}</div>
        </div>
        <div class="agents">${agents} ag</div>
      </div>`;
  }

  _addWorkspace() {
    this._closeWorkspacePop();
    openAddWorkspaceModal(this.host, (name, opts) => {
      // The workspace was created server-side — always refresh the cached
      // list so it appears immediately (setWorkspace alone only switches the
      // active ws, it doesn't reload state.workspaces). Then honor the
      // modal's "Set active workspace" toggle.
      this.host.reloadWorkspaces().then(() => {
        if (opts && opts.setActive === false) this.host.render();
        else this.host.setWorkspace(name);
      });
    });
  }

  _closeWorkspacePop() {
    this._removePopOutside();
    if (this._pop) { try { this._pop.remove(); } catch (_) {} this._pop = null; }
  }
}

if (!customElements.get('rd-header')) customElements.define('rd-header', RdHeader);

export function mountHeader(host) {
  const el = document.createElement('rd-header');
  el.className = 'hdr';
  el.host = host;
  el.state = host.state || {};
  // app.js mutates a single `state` object in place and re-passes it on every
  // render(); since it's the same object reference, Lit's identity-based
  // reactivity would skip the update. Force a re-render so chrome reflects
  // live state (active lens, plugin enable/disable, sse status, …).
  return { el, update: (state) => { el.state = state; el.requestUpdate(); } };
}

// ─── Activity Rail (vertical lens switcher) ───────────────────────
class RdRail extends RelayElement {
  static properties = { state: { attribute: false } };

  constructor() {
    super();
    this.host = null;
    this.state = {};
  }

  render() {
    const s = this.state || {};
    return html`
      ${(s.lenses || []).map((lens) => html`
        <button class="rail-btn ${s.lens === lens.id ? 'on' : ''}" title=${lens.label}
          @click=${() => this.host.setLens(lens.id)}>
          ${icon(lens.icon, 16)}
          ${lens.badge > 0 ? html`<span class="badge">${lens.badge > 99 ? '99+' : String(lens.badge)}</span>` : nothing}
          <span class="rail-tooltip">${lens.label}</span>
        </button>`)}
      <div class="rail-spacer"></div>
      <button class="rail-btn" title="Settings (⌘,)" @click=${() => this.host.openSettings()}>
        ${icon('cog', 15)}
        <span class="rail-tooltip">Settings</span>
      </button>`;
  }
}

if (!customElements.get('rd-rail')) customElements.define('rd-rail', RdRail);

export function mountRail(host) {
  const el = document.createElement('rd-rail');
  el.className = 'rail';
  el.host = host;
  el.state = host.state || {};
  return { el, update: (state) => { el.state = state; el.requestUpdate(); } };
}

// ─── Status Bar ────────────────────────────────────────────────────
class RdStatusBar extends RelayElement {
  static properties = { state: { attribute: false } };

  constructor() {
    super();
    this.host = null;
    this.state = {};
  }

  render() {
    const s = this.state || {};
    const sse = s.sseStatus || 'connected';
    const sseCls = sse === 'connected' ? 'ok'
      : sse === 'reconnecting' ? 'warn'
      : sse === 'connecting' ? 'warn' : 'err';
    const seg = (k, label, value, cls, title) => html`<div class="seg" title=${title || nothing}>
      <span class="k">${label}</span><span class="v ${cls || ''}" data-k=${k}>${value}</span></div>`;
    const procs = s.procCount;
    const uptime = uptimeStr(s.bootTs ? s.bootTs * 1000 : null);
    return html`
      <div class="seg beta" title="relaydeck is in early beta — things can break, data may change between versions">early beta</div>
      ${seg('sse', 'sse', sse, sseCls)}
      ${seg('ws', 'ws', s.workspace || 'all')}
      ${seg('agents', 'agents', `${s.agentsActive ?? 0}/${s.agentsTotal ?? 0}`)}
      ${seg('plugins', 'plugins', String(s.pluginsCount ?? 0))}
      ${seg('events', 'events', (s.eventsRate ?? 0).toFixed(1) + '/s')}
      ${seg('toks', 'tok/s', String(s.tokensPerSec ?? 0))}
      <span class="sep" aria-hidden="true"></span>
      ${seg('cpu', 'cpu', s.cpu ?? '—', null, 'Daemon process CPU usage')}
      ${seg('mem', 'mem', s.mem ?? '—', null, 'Daemon resident memory (RSS)')}
      ${seg('procs', 'procs', procs == null ? '—' : String(procs), null, 'Processes the daemon owns (agents + harness workers)')}
      ${seg('db', 'db', s.dbSize ?? '—', null, 'SQLite database size on disk')}
      ${seg('up', 'up', uptime, null, 'Daemon uptime')}
      <div class="right">
        <div class="seg hk"><kbd>⌘K</kbd> palette</div>
        <div class="seg hk" data-act="shortcuts" style="cursor:pointer" title="Keyboard shortcuts (?)"
          @click=${() => this.host.openShortcuts()}><kbd>?</kbd> shortcuts</div>
        <div class="seg hk" style="border-right:0" data-k="version">${s.version ? `relaydeck v${s.version}` : 'relaydeck'}</div>
      </div>`;
  }
}

if (!customElements.get('rd-statusbar')) customElements.define('rd-statusbar', RdStatusBar);

export function mountStatusBar(host) {
  const el = document.createElement('rd-statusbar');
  el.className = 'statusbar';
  el.host = host;
  el.state = host.state || {};
  return { el, update: (state) => { el.state = state; el.requestUpdate(); } };
}
