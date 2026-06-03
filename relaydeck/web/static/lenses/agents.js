// lenses/agents.js — Agents lens.
// Sidebar: search + filter chips + scrollable list.
// Detail: editorial dhdr + stat strip + sub-tabs (tile system) + body.
// Fleet view: when no agent is selected.
//
// MIGRATION NOTE — terminal safety. This lens hosts the agent-detail tiles,
// including the xterm Terminal tile (tiles/terminal.js — do NOT touch). The
// migration keeps the exact terminal-safety contract that already exists:
//   • the detail is rebuilt ONLY on an explicit render() (lens switch / control
//     action) — same as before; live data never rebuilds it;
//   • the sidebar re-renders reactively (lit-html diffs, keeps input focus);
//   • stat-strip vitals, sub-tabs/popovers, and the tile body (`[data-body]`)
//     are populated IMPERATIVELY into static anchor nodes that lit-html never
//     reactively re-renders, so a mounted terminal is never disturbed.
// So: the structural skeleton + sidebar move to lit-html; the procedural tile/
// popover/stat-ticking lifecycle (which Lit would only endanger) stays as-is.

import {
  RelayLens, html, nothing, render, unsafeHTML, liveDirective,
  icon, button, api, live,
} from '@relaydeck/ui';
import { h, esc, iconSVG, fmtNum, fmtCost, uptimeStr, relTime, visualStatus, sparklineSVG } from '../primitives.js';
import { agentMark } from '../harness_brand.js';
import { HomeDashboard } from '../home.js';
import {
  visibleTilesFor, computeSlots, renderPanelsManager,
  mountTile, persistTileStates, persistTileOrder, persistTileMaxTabs,
  persistStatOrder, persistStatHidden, DEFAULT_MAX_TABS,
} from '../tile_system.js';

// ── Stat-strip catalog ───────────────────────────────────────────────
// Fixed CORE vitals. Order/visibility are user-customizable (Panels
// manager → Stats section), persisted as `stat_order` + `stat_hidden`.
export const STAT_CELLS = [
  { id: 'uptime',   label: 'Uptime' },
  { id: 'tokens',   label: 'Tokens · 24h' },
  { id: 'cost',     label: 'Cost · 24h' },
  { id: 'events',   label: 'Events' },
  { id: 'tick',     label: 'Last tick' },
  { id: 'activity', label: 'Activity' },
];

// Skeleton HTML for one stat cell. The fill pass (in _patchStats) targets
// the `data-c` markers — rendered imperatively into the [data-stats] anchor so
// live patches never go through the reactive layer (terminal safety).
function _statCellHTML(id, running) {
  switch (id) {
    case 'uptime':
      return `<div class="stat-cell"><div class="k">Uptime</div><div class="v" data-c="uptime">—</div><div class="sub">since spawn</div></div>`;
    case 'tokens':
      return `<div class="stat-cell"><div class="k">Tokens · 24h</div><div class="v" data-c="tokens">—</div><div class="sub" data-c="tokens-sub">in · out</div></div>`;
    case 'cost':
      return `<div class="stat-cell"><div class="k">Cost · 24h</div><div class="v" data-c="cost">—</div><div class="sub" data-c="cost-sub">—</div></div>`;
    case 'events':
      return `<div class="stat-cell"><div class="k">Events</div><div class="v acc" data-c="events">—</div><div class="sub" data-c="events-sub">—</div></div>`;
    case 'tick':
      return `<div class="stat-cell"><div class="k">Last tick</div><div class="v" data-c="tick">—</div><div class="sub" data-c="tick-sub">—</div></div>`;
    case 'activity':
      return `<div class="stat-cell"><div class="k">Activity</div><div data-c="spark" style="height:18px"></div><div class="sub">30m</div></div>`;
    default:
      return '';
  }
}

export class AgentsLens extends RelayLens {
  constructor(host, def) {
    super(host, def || { id: 'agents' });
    this.filter = 'all';
    this.search = '';
    this.activeAgentId = null;
    this.activeTab = 'core:terminal';
    this.openPop = null;
    this.tileStates = host.state.tile_states || {};
    this.tileOrder = Array.isArray(host.state.tile_order) ? host.state.tile_order.slice() : [];
    this.maxTabs = Number.isFinite(host.state.tile_max_tabs) ? host.state.tile_max_tabs : DEFAULT_MAX_TABS;
    this.statOrder = Array.isArray(host.state.stat_order) ? host.state.stat_order.slice() : [];
    this.statHidden = Array.isArray(host.state.stat_hidden) ? host.state.stat_hidden.slice() : [];
    this._activeTileCleanup = null;
    this._activePopCleanup = null;
    this._nativePiHandle = null;
    this._removePopOutside = () => {};
  }

  // ── Sidebar (reactive lit-html via RelayLens) ───────────────────────
  renderSidebar(container) {
    super.renderSidebar(container);
    // Fleet token rollup (one query for all agents) → per-row token count +
    // sparkline. Cached briefly so chip clicks / searches don't refetch.
    this._loadRollup().then(() => this.requestUpdate());
  }

  sidebar() {
    const agents = this.host.scopedAgents().filter((a) => a.type !== 'loop');
    const total = agents.length;
    const running = agents.filter((a) => visualStatus(a) === 'running' || visualStatus(a) === 'working').length;
    const attn = agents.filter((a) => visualStatus(a) === 'awaiting-input' || visualStatus(a) === 'errored').length;
    const idle = agents.filter((a) => visualStatus(a) === 'idle').length;
    const filtered = this._filter(agents);
    const fbtn = (id, label, n) => html`<button data-f=${id} class=${this.filter === id ? 'on' : ''}
      @click=${() => { this.filter = id; this.requestUpdate(); }}>${label}<span class="n">${n}</span></button>`;
    return html`
      <div class="side-head">
        <div class="side-title">
          Agents <span class="count">${total}</span>
          ${this.host.isAllWorkspaces()
            ? html`<span class="chip muted" title="Showing every workspace" style="margin-left:6px">all ws</span>`
            : html`<span class="chip muted" title="Scoped to @${this.host.state.workspace}" style="margin-left:6px">@${this.host.state.workspace}</span>`}
        </div>
        <button class="btn icon sm" title="New agent" data-act="new" @click=${() => this.host.openNewAgent()}>${icon('plus', 12)}</button>
      </div>
      <div class="side-search">
        ${icon('search', 12)}
        <input placeholder="Search agents…" .value=${liveDirective(this.search)}
          @input=${(e) => { this.search = e.target.value; this.requestUpdate(); }}>
      </div>
      <div class="side-filter">
        ${fbtn('all', 'All', total)}${fbtn('running', 'Running', running)}${fbtn('attn', 'Attn', attn)}${fbtn('idle', 'Idle', idle)}
      </div>
      <div class="side-list">
        ${filtered.length
          ? filtered.map((a) => this._sideRow(a))
          : html`<div style="padding:20px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center">${
              this.host.isAllWorkspaces() ? 'No agents anywhere.' : `No agents in @${this.host.state.workspace}.`}</div>`}
      </div>`;
  }

  _sideRow(a) {
    const status = visualStatus(a);
    const usage = (this._rollup || {})[a.id];
    const toks = usage && usage.tokens > 0 ? usage.tokens : 0;
    const sparkTone = status === 'errored' ? 'var(--err)'
      : status === 'awaiting-input' ? 'var(--warn)' : 'var(--acc)';
    const right = toks
      ? html`<div class="toks">${fmtNum(toks)}</div>${(usage.spark && usage.spark.some((v) => v > 0))
          ? unsafeHTML(sparklineSVG(usage.spark, { height: 14, width: 50, dot: false, strokeWidth: 1, color: sparkTone, fill: false })) : nothing}`
      : html`<span class="srow-when">${a.last_active_at ? relTime(a.last_active_at * 1000) : '—'}</span>`;
    return html`
      <div class="srow ${a.id === this.activeAgentId ? 'sel' : ''}" @click=${() => this.host.selectAgent(a.id)}>
        <div class="av av-brand">${unsafeHTML(agentMark(a, { size: 28, radius: 6 }))}<span class="ind ${status}"></span></div>
        <div class="info">
          <div class="name truncate">${a.id}</div>
          <div class="sub truncate">${a.type || ''} · @${a.workspace || ''}</div>
        </div>
        <div class="right">${right}</div>`;
  }

  // Live hooks (app.js). Sidebar re-renders reactively; detail header/stats
  // patch in place so the terminal tile is never remounted.
  onAgentsChanged() {
    const side = this._sideRoot;
    if (side && side.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;
    this.requestSidebarUpdate();
    this._patchDetailHeader();
  }

  onWorkspacesChanged() {
    this._patchDetailHeader();
    const agent = this._activeAgent();
    const ws = agent?.workspace;
    if (ws) live.invalidate(`/api/workspaces/${encodeURIComponent(ws)}/git-detail`);
  }

  _activeAgent() {
    if (!this.activeAgentId) return null;
    return (this.host.state.agents || []).find((a) => a.id === this.activeAgentId) || null;
  }

  _patchDetailHeader() {
    const agent = this._activeAgent();
    const pane = this._detailRoot?.querySelector('.pane');
    if (!agent || !pane) return;
    const status = visualStatus(agent);
    const badge = pane.querySelector('.sbadge');
    if (badge) {
      badge.className = `sbadge ${status}`;
      badge.textContent = status;
    }
    this._renderGitChips(pane.querySelector('[data-git-chips]'), agent);
    this._renderRestartChip(pane.querySelector('[data-restart-chip]'), agent);
    const ws = (this.host.state.workspaces || []).find((w) => w.name === agent.workspace);
    const nPlugins = ws ? (ws.plugins || []).length : 0;
    const plg = pane.querySelector('[data-plg-chip]');
    if (plg) {
      plg.style.display = nPlugins ? '' : 'none';
      const meta = plg.querySelector('.dhdr-chip-meta');
      if (meta) meta.textContent = String(nPlugins);
    }
  }

  async _loadRollup() {
    const now = Date.now();
    if (this._rollup && (now - (this._rollupAt || 0) < 15000)) return this._rollup;
    try { this._rollup = await api.getJSON('/api/agents/usage-rollup') || {}; }
    catch (_) { this._rollup = {}; }
    this._rollupAt = now;
    return this._rollup;
  }

  _filter(agents) {
    let r = agents;
    if (this.filter === 'running') r = r.filter((a) => visualStatus(a) === 'running' || visualStatus(a) === 'working');
    else if (this.filter === 'attn') r = r.filter((a) => visualStatus(a) === 'awaiting-input' || visualStatus(a) === 'errored');
    else if (this.filter === 'idle') r = r.filter((a) => visualStatus(a) === 'idle');
    if (this.search) {
      const q = this.search.toLowerCase();
      r = r.filter((a) => a.id.toLowerCase().includes(q) || (a.purpose || '').toLowerCase().includes(q));
    }
    return r;
  }

  // ── Detail pane (override — imperative tile host, see migration note) ─
  renderDetail(container, opts) {
    this._detailHost = container;
    this._detailRoot = this._mountRoot(container, this._detailRoot);
    if (opts !== undefined) this._opts = opts;
    const agent = this._opts?.agent;
    if (!agent) {
      this.activeAgentId = null;
      // Leaving an agent detail for the fleet view: tear down the prior agent's
      // imperative resources (stats live-sub + 1s uptime interval), else they
      // keep firing/refetching until the next selection or a lens switch.
      this._stopStatsTimers();
      this._renderFleet(this._detailRoot);
      return;
    }
    this.activeAgentId = agent.id;
    this._renderAgentDetail(this._detailRoot, agent);
  }

  // The "no agent selected" overview is the customizable Home dashboard.
  _renderFleet(root) {
    if (!this._home) this._home = new HomeDashboard(this.host);
    else this._home.unmount();
    root.replaceChildren();
    this._home.mount(root);
  }

  _gitHeaderChips(agent) {
    const ws = (this.host.state.workspaces || []).find((w) => w.name === agent.workspace);
    const git = ws?.git;
    const pathTitle = ws?.path || '';
    if (!git?.is_git) {
      return html`
        <span class="dhdr-chip-group dhdr-chip-group--git dim" title=${pathTitle}>
          ${icon('git', 10)}
          <span class="dhdr-chip-label">not a repo</span>
        </span>`;
    }
    const ins = git.insertions || 0;
    const dele = git.deletions || 0;
    const deltas = [];
    if (ins) deltas.push(html`<span class="dhdr-delta add">+${ins}</span>`);
    if (dele) deltas.push(html`<span class="dhdr-delta del">−${dele}</span>`);
    if (git.untracked_files) deltas.push(html`<span class="dhdr-delta neu">${git.untracked_files} new</span>`);
    if (git.modified_files) deltas.push(html`<span class="dhdr-delta mod">${git.modified_files} mod</span>`);
    if (git.added_files) deltas.push(html`<span class="dhdr-delta add">${git.added_files} add</span>`);
    if (git.deleted_files) deltas.push(html`<span class="dhdr-delta del">${git.deleted_files} del</span>`);
    const sibs = (git.sibling_workspaces || []).length;
    return html`
      <span class="dhdr-chip-group dhdr-chip-group--git ${git.kind === 'worktree' ? 'is-wt' : ''}" title=${pathTitle}>
        ${icon('git', 10)}
        <span class="dhdr-chip-kind">${git.kind === 'worktree' ? 'worktree' : 'main'}</span>
        ${git.branch ? html`<span class="dhdr-chip-branch">${git.branch}</span>` : html`<span class="dhdr-chip-branch dim">—</span>`}
        ${git.dirty ? html`<span class="dhdr-chip-dot dirty" title="dirty"></span>` : html`<span class="dhdr-chip-dot clean" title="clean"></span>`}
        ${deltas.length ? html`<span class="dhdr-chip-deltas">${deltas}</span>` : nothing}
        ${sibs ? html`<span class="dhdr-chip-meta">${sibs} tree${sibs === 1 ? '' : 's'}</span>` : nothing}
      </span>`;
  }

  _renderGitChips(el, agent) {
    if (!el || !agent) return;
    render(this._gitHeaderChips(agent), el);
  }

  // "Restart to apply" — a running agent is behind its edited config. The
  // chip lists what changed (tooltip) and confirms INLINE (morphs into a
  // tiny restart?/✕ — no blocking dialog) before restarting. Only shown for
  // running agents whose captured snapshot diverges from desired.
  _restartChip(agent) {
    const changes = (agent && agent.pending_changes) || [];
    if (!agent || !agent.restart_pending || !changes.length) return nothing;
    const title = 'Changed since this agent started:\n· '
      + changes.map((c) => c.summary).join('\n· ');
    if (this._confirmRestartId === agent.id) {
      return html`
        <span class="dhdr-chip-group dhdr-restart confirming" title=${title}>
          <span class="dhdr-chip-label">restart now?</span>
          <button class="dhdr-restart-yes" @click=${() => { this._confirmRestartId = null; this.host.controlAgent(agent.id, 'restart'); }}>restart</button>
          <button class="dhdr-restart-no" title="cancel" @click=${() => { this._confirmRestartId = null; this._rerenderRestartChip(agent); }}>✕</button>
        </span>`;
    }
    return html`
      <button class="dhdr-chip-group dhdr-restart" data-act="restart-apply" title=${title + '\n\nClick to restart and apply (interrupts current work).'}
        @click=${() => { this._confirmRestartId = agent.id; this._rerenderRestartChip(agent); }}>
        ${icon('restart', 10)}
        <span class="dhdr-chip-label">restart to apply</span>
        <span class="dhdr-chip-meta">${changes.length}</span>
      </button>`;
  }

  _renderRestartChip(el, agent) {
    if (!el) return;
    render(this._restartChip(agent), el);
  }

  _rerenderRestartChip(agent) {
    const el = this._detailRoot?.querySelector('.pane [data-restart-chip]');
    if (el) this._renderRestartChip(el, agent);
  }

  _renderAgentDetail(root, agent) {
    this._confirmRestartId = null;  // reset any inline restart confirm on (re)open
    const status = visualStatus(agent);
    const isRunning = agent.status === 'running';
    const ws = (this.host.state.workspaces || []).find((w) => w.name === agent.workspace);
    const nPlugins = ws ? (ws.plugins || []).length : 0;
    const modelRef = agent.config?.preset || agent.config?.model || '';
    // Fresh pane each render (parity with the old innerHTML rebuild) → lit
    // renders into a clean node, no stale-part contention with the fleet view.
    const pane = document.createElement('div');
    pane.className = 'pane pane--agent';
    render(html`
      <div class="dhdr dhdr--compact">
        <div class="dhdr-top">
          <div class="dhdr-avatar dhdr-brand dhdr-avatar--sm ${status === 'running' || status === 'working' ? 'running' : ''}">
            ${unsafeHTML(agentMark(agent, { size: 40, radius: 10 }))}
            <span class="ind ${status}"></span>
          </div>
          <div class="dhdr-meta">
            <div class="dhdr-eyebrow"><button class="dhdr-back" data-act="home" title="Back to the fleet dashboard"
              @click=${() => this.host.goHome()}>${icon('home', 10)} fleet</button> · ${agent.type || 'agent'} · @${agent.workspace || ''}</div>
            <h1 class="dhdr-name truncate">${agent.id}</h1>
            <div class="dhdr-row dhdr-row--tight">
              <span class="sbadge ${status}">${status}</span>
              <span data-restart-chip></span>
              <span class="dhdr-chip-group dhdr-chip-group--model" data-model-chip style="${modelRef ? '' : 'display:none'}">
                <span class="dhdr-chip-label">model</span>
                <span class="dhdr-chip-branch" data-model-name>${modelRef || '—'}</span>
              </span>
              <span class="dhdr-chip-mount" data-git-chips></span>
              <span class="dhdr-chip-group dim" data-plg-chip style="${nPlugins ? '' : 'display:none'}">
                <span class="dhdr-chip-label">plugins</span>
                <span class="dhdr-chip-meta">${nPlugins}</span>
              </span>
            </div>
            ${agent.purpose ? html`<div class="dhdr-purpose" title=${agent.purpose}>${agent.purpose}</div>` : nothing}
          </div>
          <div class="dhdr-actions">
            ${isRunning
              ? button({ onClick: () => this.host.controlAgent(agent.id, 'stop') }, icon('stop', 11), ' Stop')
              : button({ onClick: () => this.host.controlAgent(agent.id, 'start') }, icon('play', 11), ' Launch')}
            ${button({ onClick: () => this.host.controlAgent(agent.id, 'restart') }, icon('restart', 11), ' Restart')}
            ${button({ variant: 'danger', title: 'Delete this agent (removes its YAML + DB row)', onClick: () => this.host.deleteAgent(agent.id) }, icon('delete', 11))}
          </div>
        </div>
        <div class="stat-strip stat-strip--compact" style="--cols:6" data-stats></div>
      </div>
      <div data-native-pi-banner style="display:none"></div>
      <div class="subtabs subtabs--compact" data-subtabs></div>
      <div class="dbody" data-body></div>
    `, pane);
    this._detailRoot.replaceChildren(pane);

    // Imperative population of the live/procedural regions (UNCHANGED logic).
    this._renderGitChips(pane.querySelector('[data-git-chips]'), agent);
    this._renderRestartChip(pane.querySelector('[data-restart-chip]'), agent);
    this._renderStats(pane.querySelector('[data-stats]'), agent);
    if (agent.type === 'relaydeck') this._renderNativePiBanner(pane.querySelector('[data-native-pi-banner]'));
    this._renderSubtabs(pane.querySelector('[data-subtabs]'), agent);
    this._renderBody(pane.querySelector('[data-body]'), agent);
  }

  _renderNativePiBanner(el) {
    if (!el) return;
    try { this._nativePiHandle?.unmount?.(); } catch (_) {}
    this._nativePiHandle = null;
    const gen = (this._nativePiGen = (this._nativePiGen || 0) + 1);
    import('/static/plugins/relaydeck-native/pi_install.js').then((mod) => {
      if (!el.isConnected) return;
      const handle = mod.mountPiInstall({
        container: el,
        api: this.host.api,
        compact: true,
        onStatus: (st) => {
          if (st?.pi_installed) { el.style.display = 'none'; }
          else { el.style.display = 'block'; el.style.margin = '0 0 8px 0'; }
        },
        onReady: () => { if (el.isConnected) { el.style.display = 'none'; el.innerHTML = ''; } },
      });
      if (gen !== this._nativePiGen) { try { handle.unmount?.(); } catch (_) {} return; }
      this._nativePiHandle = handle;
    }).catch(() => {});
  }

  _renderStats(el, agent) {
    this._stopStatsTimers();
    const running = (agent.semantic_status || agent.status) === 'running'
      || visualStatus(agent) === 'running' || visualStatus(agent) === 'working';
    const visible = this._visibleStatCells();
    el.style.setProperty('--cols', Math.max(1, visible.length));
    el.innerHTML = visible.map((c) => _statCellHTML(c.id, running)).join('');
    const tone = agent.status === 'errored' ? 'var(--err)'
      : (visualStatus(agent) === 'awaiting-input') ? 'var(--warn)' : 'var(--acc)';
    const key = `/api/agents/${encodeURIComponent(agent.id)}/stats`;
    this._statsAgentId = agent.id;
    this._statsUnsub = live.subscribe(key, (s) => {
      if (s) this._patchStats(el, this._statsAgentId, s);
    });
  }

  _patchStats(el, agentId, s) {
    const agent = (this.host.state.agents || []).find((a) => a.id === agentId) || {};
    const running = (agent.semantic_status || agent.status) === 'running'
      || visualStatus(agent) === 'running' || visualStatus(agent) === 'working';
    const tone = agent.status === 'errored' ? 'var(--err)'
      : (visualStatus(agent) === 'awaiting-input') ? 'var(--warn)' : 'var(--acc)';
    const set = (k, v) => { const n = el.querySelector(`[data-c="${k}"]`); if (n) n.textContent = v; };
    const activeModel = s.model || agent.config?.preset || agent.config?.model || '';
    const pane = el.closest('.pane');
    const modelGrp = pane?.querySelector('[data-model-chip]');
    const nameEl = pane?.querySelector('[data-model-name]');
    if (modelGrp && nameEl) {
      if (activeModel) { nameEl.textContent = activeModel; modelGrp.style.display = ''; }
      else { modelGrp.style.display = 'none'; }
    }
    set('tokens', s.tokens_24h ? fmtNum(s.tokens_24h) : '0');
    set('tokens-sub', `${fmtNum(s.tokens_in || 0)} in · ${fmtNum(s.tokens_out || 0)} out`);
    set('cost', fmtCost(s.cost_24h || 0));
    set('cost-sub', activeModel || `${s.model_count || 0} model${(s.model_count === 1) ? '' : 's'}`);
    set('events', fmtNum(s.events_total || 0));
    set('events-sub', s.last_event_type ? `last: ${s.last_event_type}` : '—');
    const tickEl = el.querySelector('[data-c="tick"]');
    const tickSub = el.querySelector('[data-c="tick-sub"]');
    if (tickEl) {
      tickEl.textContent = s.last_event_ts ? relTime(s.last_event_ts * 1000) : '—';
      tickEl.style.color = running ? 'var(--ok)' : '';
    }
    if (tickSub) tickSub.textContent = running ? 'live' : '—';
    const sparkEl = el.querySelector('[data-c="spark"]');
    if (sparkEl) {
      const data = (s.activity && s.activity.some((v) => v > 0)) ? s.activity : [];
      sparkEl.innerHTML = data.length
        ? sparklineSVG(data, { height: 18, color: tone, dot: false, strokeWidth: 1.1 })
        : '<span style="color:var(--t-3);font-family:var(--f-mono);font-size:10px">no activity</span>';
    }
    const upEl = el.querySelector('[data-c="uptime"]');
    if (upEl) {
      if (running && s.spawn_ts) {
        this._spawnTs = s.spawn_ts;
        if (!this._uptimeTimer) {
          const tick = () => { const e = el.querySelector('[data-c="uptime"]'); if (e && this._spawnTs) e.textContent = uptimeStr(this._spawnTs * 1000); };
          tick();
          this._uptimeTimer = setInterval(tick, 1000);
        }
      } else {
        upEl.textContent = running ? 'live' : '—';
      }
    }
  }

  _stopStatsTimers() {
    if (this._uptimeTimer) { clearInterval(this._uptimeTimer); this._uptimeTimer = null; }
    if (this._statsUnsub) { try { this._statsUnsub(); } catch (_) {} this._statsUnsub = null; }
  }

  _orderedStatCells() {
    const rank = new Map(this.statOrder.map((id, i) => [id, i]));
    const hidden = new Set(this.statHidden);
    return [...STAT_CELLS]
      .sort((a, b) => {
        const ra = rank.has(a.id) ? rank.get(a.id) : Number.MAX_SAFE_INTEGER;
        const rb = rank.has(b.id) ? rank.get(b.id) : Number.MAX_SAFE_INTEGER;
        return ra - rb;
      })
      .map((c) => ({ ...c, hidden: hidden.has(c.id) }));
  }

  _visibleStatCells() { return this._orderedStatCells().filter((c) => !c.hidden); }

  _setStatHidden(id, hidden) {
    const set = new Set(this.statHidden);
    if (hidden) set.add(id); else set.delete(id);
    if (hidden && STAT_CELLS.length - set.size < 1) return;
    this.statHidden = [...set];
    this.host.state.stat_hidden = this.statHidden;
    persistStatHidden(this.statHidden);
  }

  _reorderStat(id, dir) {
    const ids = this._orderedStatCells().map((c) => c.id);
    const i = ids.indexOf(id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    this.statOrder = ids;
    this.host.state.stat_order = this.statOrder;
    persistStatOrder(this.statOrder);
  }

  // ── Sub-tabs row (tile system core) — imperative (popover lifecycle) ──
  _renderSubtabs(el, agent) {
    const tiles = visibleTilesFor(agent, this.tileStates, this.tileOrder);
    const { tabs, overflowTabs } = computeSlots(tiles, this.maxTabs);
    const inTabs = tabs.some((t) => t.id === this.activeTab);
    const inOverflow = overflowTabs.some((t) => t.id === this.activeTab);
    if (!inTabs && !inOverflow) this.activeTab = tabs[0]?.id || overflowTabs[0]?.id || null;

    el.innerHTML = '';
    for (const t of tabs) {
      const btn = document.createElement('button');
      btn.className = 'subtab' + (this.activeTab === t.id ? ' on' : '');
      btn.innerHTML = `${iconSVG(t.icon || 'agent', 11)}<span>${esc(t.label)}</span>${t.count != null ? `<span class="badge">${t.count}</span>` : ''}`;
      btn.addEventListener('click', () => {
        this.activeTab = t.id;
        this._renderSubtabs(el, agent);
        this._renderBody(el.parentElement.querySelector('[data-body]'), agent);
      });
      el.appendChild(btn);
    }
    if (overflowTabs.length > 0) {
      const wrap = h('div', { style: { position: 'relative', display: 'inline-flex' } });
      const btn = document.createElement('button');
      const activeInOverflow = overflowTabs.some((t) => t.id === this.activeTab);
      btn.className = 'subtab' + ((this.openPop === 'tab-overflow' || activeInOverflow) ? ' on' : '');
      btn.innerHTML = `<span>+${overflowTabs.length}</span>${iconSVG('chev_d', 10)}`;
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (this.openPop === 'tab-overflow') this._closePop(el, agent);
        else this._openOverflowMenu(el, agent, wrap, 'tab', overflowTabs);
      });
      wrap.appendChild(btn);
      el.appendChild(wrap);
    }
    el.appendChild(h('div', { class: 'sp' }));
    const rightCluster = document.createElement('div');
    rightCluster.className = 'right-cluster';
    const panelsWrap = h('div', { style: { position: 'relative' } });
    const panelsBtn = document.createElement('button');
    panelsBtn.className = 'panels-btn' + (this.openPop === 'panels-mgr' ? ' on' : '');
    panelsBtn.innerHTML = `${iconSVG('layout', 12)}<span>panels</span>`;
    panelsBtn.title = 'Manage panels';
    panelsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (this.openPop === 'panels-mgr') this._closePop(el, agent);
      else this._openPanelsMgr(el, agent);
    });
    panelsWrap.appendChild(panelsBtn);
    rightCluster.appendChild(panelsWrap);
    el.appendChild(rightCluster);
  }

  _anchorPop(pop, rect) {
    try {
      pop.style.position = 'fixed';
      pop.style.top = `${Math.round(rect.bottom + 8)}px`;
      pop.style.right = `${Math.round(Math.max(8, window.innerWidth - rect.right))}px`;
      pop.style.left = 'auto';
      pop.style.maxHeight = `${Math.max(180, Math.round(window.innerHeight - rect.bottom - 24))}px`;
    } catch (_) { /* bad rect — leave CSS defaults */ }
  }

  _openOverflowMenu(el, agent, wrap, kind, items) {
    const rect = wrap.getBoundingClientRect();
    this._closePop(el, agent);
    this.openPop = 'tab-overflow';
    const pop = document.createElement('div');
    pop.className = 'pop';
    pop.style.width = '280px';
    pop.innerHTML = `
      <div class="pop-head"><span class="pop-title"><span>More tabs</span><span class="pill">${items.length}</span></span></div>
      <div class="pop-body"></div>`;
    const body = pop.querySelector('.pop-body');
    for (const t of items) {
      const row = document.createElement('div');
      row.className = 'srow';
      row.style.gridTemplateColumns = '20px 1fr auto';
      row.innerHTML = `
        ${iconSVG(t.icon || 'agent', 13)}
        <div class="info"><div class="name">${esc(t.label)}</div><div class="sub">${esc(t.source)}</div></div>
        ${t.count != null ? `<div class="right"><span class="toks">${t.count}</span></div>` : ''}`;
      row.addEventListener('click', () => {
        this.activeTab = t.id;
        this._closePop(el, agent);
        this._renderSubtabs(el, agent);
        this._renderBody(el.parentElement.querySelector('[data-body]'), agent);
      });
      body.appendChild(row);
    }
    this._anchorPop(pop, rect);
    document.body.appendChild(pop);
    this._renderSubtabs(el, agent);
    this._installPopOutsideClick(el, agent, pop, pop);
  }

  _openPanelsMgr(el, agent) {
    this._closePop(el, agent);
    this.openPop = 'panels-mgr';
    const btn = el.querySelector('.panels-btn');
    const rect = btn ? btn.getBoundingClientRect() : null;
    const tiles = visibleTilesFor(agent, this.tileStates, this.tileOrder);
    const mgr = renderPanelsManager({
      tilesForAgent: tiles,
      maxTabs: this.maxTabs,
      onChange: (id, next) => {
        this.tileStates = { ...this.tileStates, [id]: next };
        this.host.state.tile_states = this.tileStates;
        persistTileStates(this.tileStates);
        this._openPanelsMgr(el, agent);
      },
      onReorder: (id, dir) => { this._reorderTile(tiles, id, dir); this._openPanelsMgr(el, agent); },
      onMaxTabs: (n) => {
        this.maxTabs = Math.max(1, Math.min(12, n));
        this.host.state.tile_max_tabs = this.maxTabs;
        persistTileMaxTabs(this.maxTabs);
        this._openPanelsMgr(el, agent);
      },
      stats: {
        cells: this._orderedStatCells(),
        onToggle: (id, hidden) => { this._setStatHidden(id, hidden); this._reRenderStats(el, agent); this._openPanelsMgr(el, agent); },
        onReorder: (id, dir) => { this._reorderStat(id, dir); this._reRenderStats(el, agent); this._openPanelsMgr(el, agent); },
      },
      onClose: () => this._closePop(el, agent),
    });
    if (rect) this._anchorPop(mgr, rect);
    document.body.appendChild(mgr);
    this._activeMgr = mgr;
    this._renderSubtabs(el, agent);
    this._renderBody(el.parentElement.querySelector('[data-body]'), agent);
    this._installPopOutsideClick(el, agent, mgr, mgr);
  }

  _reRenderStats(el, agent) {
    const stats = el.parentElement?.querySelector('[data-stats]');
    if (stats) this._renderStats(stats, agent);
  }

  _reorderTile(tiles, id, dir) {
    const tabIds = tiles.filter((t) => t.state === 'tab').map((t) => t.id);
    const i = tabIds.indexOf(id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= tabIds.length) return;
    [tabIds[i], tabIds[j]] = [tabIds[j], tabIds[i]];
    const rest = tiles.map((t) => t.id).filter((tid) => !tabIds.includes(tid));
    this.tileOrder = [...tabIds, ...rest];
    this.host.state.tile_order = this.tileOrder;
    persistTileOrder(this.tileOrder);
  }

  _installPopOutsideClick(el, agent, anchor, pop) {
    this._removePopOutside();
    const onDown = (e) => { if (!pop.contains(e.target) && !el.contains(e.target)) this._closePop(el, agent); };
    const onKey = (e) => { if (e.key === 'Escape') this._closePop(el, agent); };
    // Defer past the opening click; track attach + cancel the timer so a
    // same-tick open→close can't leak the document listeners.
    let added = false;
    const armTimer = setTimeout(() => {
      added = true;
      document.addEventListener('mousedown', onDown);
      document.addEventListener('keydown', onKey);
    }, 0);
    this._removePopOutside = () => {
      clearTimeout(armTimer);
      if (added) {
        document.removeEventListener('mousedown', onDown);
        document.removeEventListener('keydown', onKey);
      }
      this._removePopOutside = () => {};
    };
  }

  _closePop(el, agent) {
    this.openPop = null;
    this._removePopOutside();
    if (this._activePopCleanup) { try { this._activePopCleanup(); } catch (_) {} this._activePopCleanup = null; }
    document.querySelectorAll('.pop').forEach((n) => n.remove());
    if (this._activeMgr) { this._activeMgr.remove(); this._activeMgr = null; }
    this._renderSubtabs(el, agent);
  }

  _renderBody(el, agent) {
    if (!this.activeTab) {
      el.innerHTML = '<div class="pane-empty"><div class="ttl">No tile</div><div class="sub">Open Panels to assign one.</div></div>';
      return;
    }
    const tiles = visibleTilesFor(agent, this.tileStates, this.tileOrder);
    const active = tiles.find((t) => t.id === this.activeTab) || tiles.find((t) => t.state === 'tab');
    if (!active) { el.innerHTML = ''; return; }
    el.innerHTML = '<div class="pane-empty"><div class="sub">loading…</div></div>';
    if (this._activeTileCleanup) { try { this._activeTileCleanup(); } catch (_) {} this._activeTileCleanup = null; }
    // Guard the async mount against a rapid tab switch (or a lens unmount) landing
    // out of order: if a newer _renderBody ran — or the lens is gone — before this
    // mount resolved, unmount it immediately instead of storing (and orphaning) its
    // cleanup, else its subscriptions/timers — or a terminal's xterm/PTY — leak.
    // Mirrors the _renderNativePiBanner gen guard.
    const gen = (this._tileGen = (this._tileGen || 0) + 1);
    mountTile(active, el, { agent, tile: active, mode: 'body', host: this.host }).then((handle) => {
      if (gen !== this._tileGen || this._dead) { try { handle.unmount?.(); } catch (_) {} return; }
      this._activeTileCleanup = handle.unmount;
    });
  }

  unmount() {
    if (this._home) { try { this._home.unmount(); } catch (_) {} }
    try { this._nativePiHandle?.unmount?.(); } catch (_) {}
    this._nativePiHandle = null;
    this._stopStatsTimers();
    this._removePopOutside();
    if (this._activeTileCleanup) { try { this._activeTileCleanup(); } catch (_) {} }
    if (this._activePopCleanup) { try { this._activePopCleanup(); } catch (_) {} }
    if (this._activeMgr) { try { this._activeMgr.remove(); } catch (_) {} }
    super.unmount();
  }
}
