// app.js — top-level dashboard shell. Owns global state, the lens
// router, SSE wiring, and overlay invocation. Each lens is a class
// loaded via dynamic import; this module only knows the registry.

import { acquireToken, connectSSE, api, events, live, liveOnEvent, loadPreferences, savePreferences, stamped } from './data.js';
import { mountHeader, mountRail, mountStatusBar } from './shell.js';
import { setPluginTiles } from './tile_system.js';
import {
  openCommandPalette, openNotificationsDrawer,
  openNewAgentModal, openSettings, openShortcutsCheatsheet,
  openRestartModal, SHORTCUT_DEFAULTS,
} from './overlays.js';
import { maybeShowOnboarding } from './onboarding.js';
import { checkForUpdate } from './update_banner.js';

import { AgentsLens } from './lenses/agents.js';
import { MessagesLens } from './lenses/messages.js';
import { ModelsLens } from './lenses/models.js';
import { WorkersLens } from './lenses/workers.js';
import { WorkspacesLens } from './lenses/workspaces.js';
import { PluginsLens } from './lenses/plugins.js';
import { applyAppearance, setAppearance, invalidateThemeCache } from './theme.js';
import { iconSVG, knownIconNames, fmtBytes } from './primitives.js';

const CORE_LENSES = [
  { id: 'agents',      label: 'Agents',      icon: 'agent',     ctor: AgentsLens },
  { id: 'messages',    label: 'Messages',    icon: 'message',   ctor: MessagesLens,   requiresPlugin: 'messaging' },
  { id: 'models',      label: 'Models',      icon: 'diamond',   ctor: ModelsLens },
  { id: 'workers',     label: 'Workers',     icon: 'cpu',       ctor: WorkersLens },
  { id: 'workspaces',  label: 'Workspaces',  icon: 'workspace', ctor: WorkspacesLens },
  { id: 'plugins',     label: 'Plugins',     icon: 'plugin',    ctor: PluginsLens },
  // Theme/appearance lives in Settings → General now (picker + granular tokens
  // behind a sub-tab), not as a standalone rail lens. See overlays.js.
];

class App {
  constructor() {
    this.api = api;
    this.state = {
      version: '',
      lens: 'agents',
      workspace: '',
      workspaces: [],
      workspacePlugins: [],
      agents: [],
      allPlugins: [],
      pluginsCount: 0,
      events: [],
      notifications: [],
      homeWidgets: [],
      lenses: CORE_LENSES,
      tile_states: {},
      tile_order: [],
      tile_max_tabs: 7,
      stat_order: [],
      stat_hidden: [],
      density: 'regular',
      accent: 'cyan',
      glow: 'on',
      theme: 'base',
      appearanceScope: 'global',
      sseStatus: 'connecting',
      eventsRate: 0,
      tokensPerSec: 0,
      agentsActive: 0,
      agentsTotal: 0,
      dbSize: '—',
      cpu: '—',
      mem: '—',
      procCount: null,
      bootTs: null,
      totals: { tokens24h: 0, cost24h: 0 },
      unreadNotifications: 0,
    };
    this.lensInstances = {};
    this._eventSubs = new Set();
    this._eventBuffer = [];
    this._eventsRateBuf = [];
  }

  async boot() {
    document.documentElement.setAttribute('data-density', 'regular');
    // Accent is owned by the theme engine now (the `:root` terracotta default
    // + paper/ink/mono themes). The legacy `data-accent` presets are dormant —
    // forcing one here would override the redesign's accent. See setAccent().

    // Auth + SSE + initial data load.
    await acquireToken();

    // Persisted prefs (tile_states, density, accent, last workspace, last lens)
    const prefs = await loadPreferences();
    // density + glow paint immediately (no-flash); the theme tokens are
    // applied via applyAppearance below once the workspace is resolved.
    if (prefs.density) { this.state.density = prefs.density; document.documentElement.setAttribute('data-density', prefs.density); }
    if (prefs.accent)  { this.state.accent = prefs.accent; }
    if (prefs.glow)    { this.state.glow = prefs.glow; }
    document.documentElement.setAttribute('data-glow', this.state.glow);
    this._legacyAccent = prefs.accent;
    this._hadAppearance = !!prefs.appearance;
    if (prefs.tile_states) this.state.tile_states = prefs.tile_states;
    if (Array.isArray(prefs.tile_order)) this.state.tile_order = prefs.tile_order;
    if (Number.isFinite(prefs.tile_max_tabs)) this.state.tile_max_tabs = prefs.tile_max_tabs;
    if (Array.isArray(prefs.stat_order)) this.state.stat_order = prefs.stat_order;
    if (Array.isArray(prefs.stat_hidden)) this.state.stat_hidden = prefs.stat_hidden;
    // Keyboard shortcuts: minimal defaults, overridden by saved per-shortcut prefs.
    this.state.shortcuts = { ...SHORTCUT_DEFAULTS, ...(prefs.shortcuts || {}) };
    // `appearance` was removed as a rail lens (now in Settings → General).
    if (prefs.last_lens && prefs.last_lens !== 'appearance') this.state.lens = prefs.last_lens;
    if (prefs.last_workspace) this.state.workspace = prefs.last_workspace;

    // URL query string is the explicit-intent signal — wins over
    // preferences. `?workspace=demo` scopes to demo; `?workspace=`
    // (empty value present) means "all workspaces". Bookmarkable.
    // `?lens=models` jumps to a lens on open.
    const url = new URL(location.href);
    if (url.searchParams.has('workspace')) {
      this.state.workspace = url.searchParams.get('workspace') || "";
    }
    if (url.searchParams.has('lens')) {
      this.state.lens = url.searchParams.get('lens') || this.state.lens;
    }
    // `?agent=<id>` deep-links straight to an agent's detail. Applied
    // after the first agents load so the selection sticks. Shareable.
    if (url.searchParams.has('agent')) {
      this._pendingAgent = url.searchParams.get('agent') || null;
      if (this._pendingAgent) this.state.lens = 'agents';
    }
    // `?section=<id>` deep-links to a section within a plugin lens that
    // provides sidebar navigation (consumed once by PluginLens on mount).
    this._pendingSection = url.searchParams.get('section') || null;

    // Theme engine: legacy migration (pre-theme-engine users have an
    // `accent` pref but no `appearance` block — the accent values are
    // exactly the builtin theme names, so promote it once) then paint the
    // resolved appearance for the active workspace.
    if (!this._hadAppearance && this._legacyAccent && this._legacyAccent !== 'cyan') {
      try { await api.putJSON('/api/appearance', { theme: this._legacyAccent }); } catch (_) {}
    }
    try { await applyAppearance(this, this.state.workspace); } catch (_) {}

    this._buildShell();
    this._mountSSE();

    // Initial fetches in parallel (cheap to refetch later).
    await Promise.allSettled([
      this.reloadWorkspaces(),
      this.reloadAgents(),
      this.reloadPlugins(),
      this._loadHealth(),
      this._loadRuntimeStats(),
      this._loadPluginsUI(),
      this._loadUsageTotals(),
    ]);
    // Daemon vitals refresh on a slow cadence — SRE-glance, not real-time.
    // 5s keeps CPU% current without hammering `ps`. App is a page-lifetime
    // singleton (a plain class, not a LitElement — no disconnectedCallback),
    // so we tear the interval down on `pagehide` to avoid a dangling timer
    // under HMR/hot-reload.
    if (!this._statsTimer) {
      this._statsTimer = setInterval(() => this._loadRuntimeStats(), 5000);
      window.addEventListener('pagehide', () => {
        if (this._statsTimer) { clearInterval(this._statsTimer); this._statsTimer = null; }
      }, { once: true });
    }
    // First-launch fallback: if the operator hasn't picked a
    // workspace yet (no URL param, no preference), pick the first
    // registered one rather than landing in "all workspaces" mode
    // — that's almost never what an empty-state operator wants.
    if (!this.state.workspace && this.state.workspaces.length
        && !url.searchParams.has('workspace') && !prefs.last_workspace) {
      this.state.workspace = this.state.workspaces[0].name;
    }
    // Apply a ?agent= deep-link now that agents are loaded.
    if (this._pendingAgent && (this.state.agents || []).some(a => a.id === this._pendingAgent)) {
      const id = this._pendingAgent;
      this._pendingAgent = null;
      this.state.lens = 'agents';
      this._mountActiveLens();
      if (this._currentLens) this._currentLens.activeAgentId = id;
      this._markAgentViewed(id);
    }
    this._syncUrl();
    this._refreshComputedState();
    this.render();

    // First-run: if no model provider is usable yet, encourage setup
    // (detect local servers / connect a key). Non-blocking, dismissable.
    maybeShowOnboarding(this);

    // Show the update banner if a newer GitHub release exists (cached, fail-open).
    checkForUpdate(this);

    // Keyboard.
    window.addEventListener('keydown', (e) => {
      const inField = ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName);
      const inTerminal = !!document.activeElement?.closest?.('.term-host, .xterm');
      if (inTerminal) return;
      const mod = e.metaKey || e.ctrlKey;
      const on = (id) => this.shortcutOn(id);

      // Modifier combos (work even when a field is focused, except where noted).
      if (on('search') && mod && e.key === 'k') { e.preventDefault(); this.openCmdK(); return; }
      if (on('new-agent') && mod && e.key === 'n' && !inField) { e.preventDefault(); this.openNewAgent(); return; }
      if (on('settings') && mod && e.key === ',') { e.preventDefault(); this.openSettings(); return; }

      // Single-key shortcuts — only in "nav mode": not typing in a field, not
      // in the terminal (covered above), no modifier. Each is individually
      // gated on its enabled state (Settings → Shortcuts).
      if (inField || mod || e.altKey) return;
      if (on('help') && e.key === '?') { e.preventDefault(); this.openShortcuts(); return; }
      if (this._overlayOpen()) return;  // let the open overlay own its keys
      // `/` does ONE thing: focus the terminal on an agent page. It is NOT a
      // search shortcut (search is ⌘K) — a no-op when no terminal is in view,
      // so it never hijacks the key elsewhere. (Inside the terminal, the early
      // `inTerminal` return above lets `/` type normally — e.g. harness slash
      // commands.)
      if (on('term-focus') && e.key === '/') { if (this._focusTerminal()) e.preventDefault(); return; }
      if (on('lens-nav') && e.key >= '1' && e.key <= '9') { e.preventDefault(); this._switchLensIndex(parseInt(e.key, 10)); return; }
      if (on('next-agent') && (e.key === 'ArrowDown' || e.key === ']')) { e.preventDefault(); this._cycleAgent(1); return; }
      if (on('prev-agent') && (e.key === 'ArrowUp' || e.key === '[')) { e.preventDefault(); this._cycleAgent(-1); return; }
      // Space: focus the terminal prompt on an agent page; otherwise open
      // search. Only when focus isn't on an interactive control (so Space
      // still activates buttons / checkboxes normally).
      if (on('space-focus') && (e.key === ' ' || e.code === 'Space')) {
        const ae = document.activeElement;
        const interactive = ae && ae.closest && ae.closest('button, a, select, [role="button"], [tabindex]');
        if (interactive) return;
        e.preventDefault();
        if (!this._focusTerminal()) this.openCmdK();
      }
    });
  }

  // ── keyboard helpers ──────────────────────────────────────────────
  /** Any modal/drawer/palette open? (Each adds an `.overlay-scrim`; the
   *  home widget gallery uses `.gallery-scrim`.) Used to pause nav keys
   *  so the open surface owns the keyboard. */
  _overlayOpen() {
    return !!document.querySelector('.overlay-scrim, .gallery-scrim');
  }
  /** Switch to the Nth visible lens (1-based). No-op past the end. */
  _switchLensIndex(n) {
    const lens = (this.state.lenses || [])[n - 1];
    if (lens) this.setLens(lens.id);
  }
  /** Move the agent selection by `dir` (+1 next, -1 prev) and open it.
   *  Operates on the workspace-scoped agent list; wraps around. With no
   *  current selection, lands on the first (next) or last (prev). */
  _cycleAgent(dir) {
    const agents = this.scopedAgents();
    if (!agents.length) return;
    if (this.state.lens !== 'agents') { this.setLens('agents'); }
    const cur = this._currentLens?.activeAgentId;
    const idx = agents.findIndex(a => a.id === cur);
    let next;
    if (idx === -1) next = dir > 0 ? 0 : agents.length - 1;
    else next = (idx + dir + agents.length) % agents.length;
    this.selectAgent(agents[next].id);
  }
  /** Focus the on-screen terminal so the operator can type into the
   *  agent. Focuses the existing xterm element only — does not touch the
   *  terminal/PTY implementation. To leave: ⌘K or click outside it. */
  _focusTerminal() {
    const ta = this.detailEl?.querySelector('.xterm-helper-textarea')
      || this.detailEl?.querySelector('.xterm textarea');
    if (ta) { ta.focus(); return true; }
    return false;
  }
  /** Is a keyboard shortcut enabled? Saved per-shortcut prefs override the
   *  minimal defaults (SHORTCUT_DEFAULTS). */
  shortcutOn(id) {
    const s = this.state.shortcuts || {};
    return (id in s) ? !!s[id] : (SHORTCUT_DEFAULTS[id] !== false);
  }
  /** Toggle (or set) a shortcut's enabled state + persist. `esc` is locked. */
  toggleShortcut(id, on) {
    if (id === 'esc') return;
    const s = this.state.shortcuts || (this.state.shortcuts = { ...SHORTCUT_DEFAULTS });
    s[id] = (on === undefined) ? !this.shortcutOn(id) : !!on;
    savePreferences({ shortcuts: s });
  }
  /** Restore the minimal default shortcut set. */
  resetShortcuts() {
    this.state.shortcuts = { ...SHORTCUT_DEFAULTS };
    savePreferences({ shortcuts: this.state.shortcuts });
  }
  openShortcuts() { openShortcutsCheatsheet(this); }

  _buildShell() {
    document.body.innerHTML = '';
    const appEl = document.createElement('div'); appEl.className = 'app rd';
    const main = document.createElement('div'); main.className = 'main';
    this.header = mountHeader(this);
    this.rail = mountRail(this);
    this.statusbar = mountStatusBar(this);
    this.sideEl = document.createElement('div'); this.sideEl.className = 'side';
    // `.detail-host` is just a fill container for whatever the active
    // lens mounts (which is itself a `.pane`). Using `.pane` on this
    // outer node too would double-nest the flex chain — the inner
    // pane would only take content height and `.dbody`'s `flex:1`
    // would have nothing to grow into (terminal would render tiny).
    this.detailEl = document.createElement('div'); this.detailEl.className = 'detail-host';

    appEl.appendChild(this.header.el);
    // Update-available banner slot (filled by checkForUpdate on boot).
    this.bannerEl = document.createElement('div'); this.bannerEl.className = 'banner-host';
    appEl.appendChild(this.bannerEl);
    main.appendChild(this.rail.el);
    main.appendChild(this.sideEl);
    main.appendChild(this.detailEl);
    appEl.appendChild(main);
    appEl.appendChild(this.statusbar.el);
    document.body.appendChild(appEl);
  }

  _mountSSE() {
    connectSSE();
    // Make the agents + workspaces collections live via the store: they
    // refresh on the matching SSE events (agent.*, workspace.*) and on the
    // heartbeat, then update state + re-render. No manual reload-on-event.
    this._wireLiveCollections();
    events.onStatusChange((s) => {
      const wasDown = this.state.sseStatus && this.state.sseStatus !== 'connected';
      this.state.sseStatus = s;
      // On (re)connect, revalidate everything subscribed so we never show
      // stale data after a dropped stream.
      if (s === 'connected' && wasDown) live.refreshAll();
      this.header.update(this.state);
      this.statusbar.update(this.state);
    });
    events.subscribe((event) => {
      // Real-time foundation: route every event into the live store so it
      // invalidates the resources it touches (cost/tokens/agents/etc.).
      liveOnEvent(event);

      // A relaydeck agent's `dashboard` tool reshapes the UI live via this event.
      if (event.type === 'dashboard.command') {
        this._applyDashboardCommand(event.payload || event.data || {});
      }

      // Theme / appearance changed elsewhere (CLI, another tab, an agent)
      // — drop the resolved-token cache and re-paint the active workspace.
      if (event.type === 'themes.changed' || event.type === 'appearance.changed') {
        invalidateThemeCache();
        applyAppearance(this, this.state.workspace).catch(() => {});
        try { this._currentLens?.onAppearanceChanged?.(); } catch (_) {}
      }

      if (event.type === 'system.plugin.unloaded' || event.type === 'system.plugin.loaded') {
        this.refreshPluginManifest().catch(() => {});
      }

      // ring buffer
      this._eventBuffer.unshift(event);
      while (this._eventBuffer.length > 200) this._eventBuffer.pop();
      this.state.events = this._eventBuffer;

      // rolling 60-event/sec window
      const now = Date.now();
      this._eventsRateBuf.push(now);
      while (this._eventsRateBuf.length && now - this._eventsRateBuf[0] > 10_000) this._eventsRateBuf.shift();
      this.state.eventsRate = this._eventsRateBuf.length / 10;

      // notifications: only the ones operator should care about
      this._maybeNotify(event);

      // refresh things that need it
      this._fanoutEvent(event);
      this._refreshComputedState();

      // tiny status-bar tick (cheap)
      this.statusbar.update(this.state);
      this.header.update(this.state);
    });
  }

  _applyDashboardCommand(d) {
    // Theme ops apply globally (any lens) AND persist via the appearance
    // store so they survive a reload. Widget ops go to the live Home
    // dashboard instance if it's mounted.
    const op = d.op, val = d.value;
    if (op === 'theme' && val) this.setActiveTheme(val);
    else if (op === 'accent' && val) this.setAccent(val);
    else if (op === 'density' && val) this.setDensity(val);
    else if (op === 'glow' && val) this.setGlow(val);
    else { try { window.__relaydeckHome?.applyCommand?.(d); } catch (_) {} }
  }

  _wireLiveCollections() {
    // Keep the core collections fresh from the live store. We update
    // state + cheap chrome (header/status/rail) here but DO NOT re-render
    // the detail pane — that would remount tiles (incl. the terminal).
    // The active lens opts into refreshing its own sidebar via the
    // onAgentsChanged / onWorkspacesChanged hooks (sidebar only, never
    // the detail), so live updates never disturb a running terminal.
    const chrome = () => {
      this._refreshComputedState();
      try { this.header.update(this.state); } catch (_) {}
      try { this.statusbar.update(this.state); } catch (_) {}
      try { this.rail?.update?.(this.state); } catch (_) {}
    };
    live.subscribe('/api/agents', (agents) => {
      this.state.agents = agents || [];
      chrome();
      try { this._currentLens?.onAgentsChanged?.(); } catch (_) {}
    });
    live.subscribe('/api/workspaces', (ws) => {
      this.state.workspaces = ws || [];
      chrome();
      try { this._currentLens?.onWorkspacesChanged?.(); } catch (_) {}
    });
  }

  _maybeNotify(event) {
    const t = event.type || '';
    // Backend bus events carry `ts` in epoch SECONDS (time.time()), but
    // relTime/formatTs expect milliseconds — every other call site converts
    // with `* 1000`. Normalize once here so the Inbox drawer doesn't render
    // "20000d ago". Absent ts falls back to the already-ms Date.now().
    const ts = event.ts ? event.ts * 1000 : Date.now();
    if (t === 'agent.error') {
      this.state.notifications.unshift({
        ts,
        kind: 'err',
        title: `${event.agent_id || 'agent'} errored`,
        body: event.data?.error || event.data?.message || '',
        agent: event.agent_id || '',
        workspace: event.workspace || event.data?.workspace || '',
      });
    } else if (t === 'agent.status_changed') {
      const d = event.data || {};
      const to = d.to || d.semantic_status || d.status;
      const aid = d.agent_id || event.agent_id || '';
      // Operator is already on this agent's detail — treat the result as read
      // without requiring a re-click (engine may flag complete-unread while
      // the terminal tile is in view).
      if (to === 'complete-unread' && aid && this.state.lens === 'agents'
          && this._currentLens?.activeAgentId === aid) {
        this._markAgentViewed(aid);
      }
      if (to === 'awaiting-input') {
        this.state.notifications.unshift({
          ts,
          kind: 'awaiting',
          title: `${aid || 'agent'} awaiting input`,
          body: 'Paused — needs operator response.',
          agent: aid,
          workspace: event.workspace || d.workspace || '',
        });
      }
    } else if (t === 'hitl.escalation') {
      // Human-in-the-loop: an agent needs the operator. The bell is the
      // always-on HITL channel (other homes — telegram, email — subscribe
      // to the same hitl.escalation event server-side).
      const d = event.data || event.payload || {};
      const stopped = d.stopped ? ' (agent stopped)' : '';
      this.state.notifications.unshift({
        ts,
        kind: 'awaiting',
        title: `${d.agent_id || 'agent'} needs you${stopped}`,
        body: d.message || `Escalation (${d.kind || 'hitl'}) — needs operator response.`,
        agent: d.agent_id || '',
        workspace: d.workspace || event.workspace || '',
      });
    } else if (t === 'gateway.webhook') {
      this.state.notifications.unshift({
        ts,
        kind: 'info',
        title: 'Webhook ingress',
        body: event.data?.channel || '',
        workspace: event.workspace || '',
      });
    } else if (t === 'relaydeck-native.credentials_rotated') {
      // A provider key was rotated/removed in the vault. auth.json + HTTP
      // chat pick up the new value automatically, but a running pi PTY
      // child captured the old env at spawn time — it needs a restart.
      const d = event.payload || event.data || {};
      const ids = Array.isArray(d.stale_agent_ids) ? d.stale_agent_ids : [];
      if (ids.length) {
        this.state.notifications.unshift({
          ts,
          kind: 'awaiting',
          title: 'Restart agents to apply key change',
          body: `${d.key || 'A provider key'} changed. Running native ${ids.length === 1 ? 'agent' : 'agents'} ${ids.join(', ')} still ${ids.length === 1 ? 'uses' : 'use'} the old key until restarted.`,
          agent: ids[0] || '',
          workspace: '',
        });
      }
    }
    while (this.state.notifications.length > 30) this.state.notifications.pop();
    this.state.unreadNotifications = this.state.notifications.length;
  }

  _fanoutEvent(event) {
    for (const sub of this._eventSubs) { try { sub(event); } catch (_) {} }
  }

  subscribeEvents(cb) {
    this._eventSubs.add(cb);
    return () => this._eventSubs.delete(cb);
  }

  // ── data loaders ──────────────────────────────────────────────────
  async reloadAgents() {
    try {
      const agents = await api.getJSON('/api/agents');
      this.state.agents = agents || [];
    } catch (_) { this.state.agents = []; }
  }
  async reloadWorkspaces() {
    try {
      const r = await api.fetch('/api/workspaces');
      this.state.workspaces = r.ok ? await r.json() : [];
    } catch (_) { this.state.workspaces = []; }
    await this._loadWorkspacePlugins();
  }
  async _loadWorkspacePlugins() {
    // "All workspaces" mode (empty workspace): the plugins pill
    // shows the *union* of plugins enabled across every workspace,
    // because there's no single workspace to manage. Toggling a
    // plugin would be ambiguous, so the pill is read-only in this
    // mode — handled in shell.js by hiding the click target.
    if (!this.state.workspace) {
      const union = new Set();
      for (const w of (this.state.workspaces || [])) {
        for (const p of (w.plugins || [])) union.add(p);
      }
      this.state.workspacePlugins = [...union].sort();
      return;
    }
    // Per-workspace plugin list lives in /api/workspaces; pick the
    // matching row. (The /api/workspace-plugins endpoint is the
    // *catalog* of toggleable names, not the per-workspace selection.)
    const ws = (this.state.workspaces || []).find(w => w.name === this.state.workspace);
    this.state.workspacePlugins = ws ? (ws.plugins || []) : [];
  }
  async reloadPlugins() {
    try {
      const plugins = await api.getJSON('/api/plugins');
      this.state.allPlugins = plugins || [];
      this.state.pluginsCount = this.state.allPlugins.length;
    } catch (_) { this.state.allPlugins = []; }
  }
  /** Re-fetch plugin list + UI manifest and repaint rail/tiles after enable/disable. */
  async refreshPluginManifest() {
    await Promise.all([this.reloadPlugins(), this._loadPluginsUI()]);
    this._refreshComputedState();
    this.render();
  }
  async _loadHealth() {
    try {
      const h = await api.getJSON('/healthz');
      this.state.version = h.version || '';
    } catch (_) {}
  }
  /** Poll daemon vitals (db size, cpu, mem, uptime, proc count) for the
   *  status bar. Cheap + fault-tolerant — a failed fetch leaves the last
   *  good values in place rather than blanking the bar. */
  async _loadRuntimeStats() {
    try {
      const r = await api.getJSON('/api/runtime-stats');
      this.state.dbSize = fmtBytes(r.db_size_bytes);
      this.state.cpu = r.cpu_percent != null ? `${Math.round(r.cpu_percent)}%` : '—';
      this.state.mem = fmtBytes(r.mem_rss_bytes);
      this.state.procCount = r.proc_count;
      this.state.bootTs = r.boot_ts ?? null;
      try { this.statusbar.update(this.state); } catch (_) {}
    } catch (_) {}
  }
  async _loadUsageTotals() {
    // Aggregate /api/usage into totals (real numbers, all-time). Scoped to the
    // active workspace so a fresh workspace shows its own usage (zero), not the
    // fleet figure; empty workspace ("All workspaces") sums everything.
    try {
      const ws = this.state.workspace;
      const q = ws ? ('?workspace=' + encodeURIComponent(ws)) : '';
      const rows = await api.getJSON('/api/usage' + q);
      let tokens = 0, cost = 0;
      for (const r of (rows || [])) {
        tokens += r.total_tokens || 0;
        cost += r.total_cost || 0;
      }
      this.state.usageTotals = { tokens, cost };
    } catch (_) { this.state.usageTotals = null; }
  }
  async _loadPluginsUI() {
    // Aggregates tabs/tiles/header_chips contributed by plugins.
    try {
      const ui = await api.getJSON('/api/plugins/ui');
      // Home-dashboard widgets a plugin contributes via `[plugin.ui] widgets`.
      // The Home view's gallery merges these with its built-in widgets.
      this.state.homeWidgets = ui.widgets || [];
      const tiles = (ui.agent_tiles || []).map(t => ({
        id: t.id,
        label: t.title || t.label || t.id,
        icon: t.icon || 'agent',
        source: t.source || t.id.split(':')[0],
        default_state: t.default_state || t.default || 'hidden',
        description: t.description || '',
        module: this._resolveStaticUrl(t),
        protected: !!t.protected,
        applies_to: t.applies_to || [],
      }));
      setPluginTiles(tiles);

      // Plugin-contributed top-level lenses (e.g., github plugin's rail
      // icon). A plugin tab only earns a rail slot when it OPTS IN with
      // `default_state = "lens"` — matching the documented contract in
      // AGENTS.md. The old default promoted every tab to a lens, which
      // turned a 20-plugin workspace's rail into a wall of fallback
      // star icons. Plugins that want to live as agent-detail tiles
      // (the common case) just omit default_state or set "tab"/"hidden".
      const lenses = [...CORE_LENSES];
      for (const tab of (ui.tabs || [])) {
        const state = tab.default_state || 'tab';
        if (state !== 'lens') continue;
        const url = this._resolveStaticUrl(tab);
        if (!url) continue;
        lenses.push({
          id: tab.id,
          label: tab.title || tab.id,
          icon: _safeIconName(tab.icon),
          ctor: PluginLens,
          module: url,
        });
      }
      this.state.lenses = lenses;
    } catch (_) {}
  }
  _resolveStaticUrl(item) {
    // Plugin static dirs are auto-mounted at /static/plugins/<name>/.
    const src = (item.id || '').split(':')[0];
    const mod = item.module || '';
    if (!mod) return '';
    if (mod.startsWith('/') || mod.startsWith('http')) return mod;
    return `/static/plugins/${encodeURIComponent(src)}/${mod}`;
  }

  // ── computed state ────────────────────────────────────────────────
  //
  // `state.workspace === ""` means "All workspaces" — every lens that
  // scopes by workspace should treat empty as "no filter". When the
  // user picks a real workspace from the switcher, every counter and
  // list narrows to that workspace; toggling back to "All" reveals
  // the full fleet. This mirrors `relaydeck agent list` (cwd-scoped) vs.
  // `relaydeck agent list -A` (all workspaces).
  _refreshComputedState() {
    const scoped = this.scopedAgents();
    this.state.agentsTotal = scoped.length;
    this.state.agentsActive = scoped.filter(a => a.status === 'running').length;
    this.state.tokensPerSec = 0;  // no per-workspace rollup endpoint yet

    // Filter visible lenses by enabled plugins. A globally-disabled plugin
    // drops its lens even if a workspace still lists it in agent.toml —
    // the plugin isn't loaded so the lens would be empty/broken.
    //
    // Three states for `p` (the row in allPlugins): undefined (manifest not
    // loaded yet), enabled, or explicitly disabled. We only hide on explicit
    // false; when p is undefined we trust workspacePlugins as the sole signal
    // (allPlugins resolves early on boot, before this flickers).
    this.state.lenses = (this.state.lenses || CORE_LENSES).filter(l => {
      if (!l.requiresPlugin) return true;
      const p = this.state.allPlugins.find(x => x.name === l.requiresPlugin);
      if (p && p.enabled === false) return false;
      return this.state.workspacePlugins.includes(l.requiresPlugin) ||
             (p && p.enabled !== false);
    });

    // Inject workspace_plugins onto every agent (across workspaces) so
    // the tile-system gate works even when "All workspaces" is on.
    const wsMap = Object.fromEntries((this.state.workspaces || []).map(w => [w.name, w.plugins || []]));
    for (const a of (this.state.agents || [])) a.__workspace_plugins = wsMap[a.workspace] || [];

    const messagesLens = this.state.lenses.find(l => l.id === 'messages');
    if (messagesLens) messagesLens.badge = 0; // populated by MessagesLens once it loads
  }

  // ── scoping helpers (used by every lens) ──────────────────────────

  /** Agents in the current workspace, or every agent when state.workspace
   *  is empty ("All workspaces" mode). */
  scopedAgents() {
    const all = this.state.agents || [];
    const ws = this.state.workspace;
    if (!ws) return all;
    return all.filter(a => (a.workspace || "") === ws);
  }

  /** Workers belonging to the current workspace. A worker belongs to
   *  a workspace iff:
   *    - its `config.workspace` matches, OR
   *    - its `agent_id` resolves to an agent in that workspace.
   *  Infra workers (no workspace, no agent_id — e.g. db.maintenance)
   *  always show: they're daemon-wide and live under every workspace
   *  view by definition.
   *  In "All workspaces" mode (workspace=""), every worker is in scope.
   */
  scopedWorkers(workers) {
    const ws = this.state.workspace;
    if (!ws) return workers || [];
    const agentWs = new Map();
    for (const a of (this.state.agents || [])) agentWs.set(a.id, a.workspace || "");
    return (workers || []).filter(w => {
      const cfgWs = (w.config && w.config.workspace) || "";
      if (cfgWs) return cfgWs === ws;
      const agentId = w.agent_id || w.agent || "";
      if (agentId) return agentWs.get(agentId) === ws;
      // Infra worker (no scope) — always visible.
      return true;
    });
  }

  /** Is "All workspaces" mode on? */
  isAllWorkspaces() { return !this.state.workspace; }

  // ── render ────────────────────────────────────────────────────────
  render() {
    this.header.update(this.state);
    this.rail.update(this.state);
    this.statusbar.update(this.state);
    this._mountActiveLens();
  }

  /** Resolve a possibly-shorthand or stale lens id against the live lens
   *  list. Plugin lenses are server-namespaced (`skills` → `skills:skills`),
   *  so `?lens=skills` deep-links must map to the real id. Falls through
   *  unchanged when nothing matches (the caller defaults to lenses[0]). */
  _resolveLensId(id) {
    const lenses = this.state.lenses || [];
    if (!id || lenses.some(l => l.id === id)) return id;
    const hit = lenses.find(l =>
      l.id === `${id}:${id}` || l.id.endsWith(`:${id}`) || l.id.split(':')[0] === id);
    return hit ? hit.id : id;
  }
  _mountActiveLens() {
    this.state.lens = this._resolveLensId(this.state.lens);
    const lensDef = this.state.lenses.find(l => l.id === this.state.lens) || this.state.lenses[0];
    if (!lensDef) {
      this.sideEl.innerHTML = '';
      this.detailEl.innerHTML = '<div class="pane-empty"><div class="ttl">No lenses available</div></div>';
      return;
    }
    // Re-use lens instance if same id; recreate on lens change to drop subscriptions.
    let lensChanged = false;
    if (this._currentLensId !== lensDef.id) {
      if (this._currentLens && this._currentLens.unmount) try { this._currentLens.unmount(); } catch (_) {}
      this._currentLens = new lensDef.ctor(this, lensDef);
      this._currentLensId = lensDef.id;
      this._detailRenderKey = null;
      lensChanged = true;
    }
    this._currentLens.renderSidebar(this.sideEl);
    const agent = (this._currentLensId === 'agents')
      ? (this.state.agents || []).find(a => a.id === this._currentLens.activeAgentId)
      : null;
    if (this._currentLensId === 'agents' && this._currentLens.activeAgentId && !agent) {
      this._currentLens.activeAgentId = null;
    }
    const detailKey = this._currentLensId === 'agents'
      ? `agents:${this._currentLens.activeAgentId || ''}`
      : `lens:${this._currentLensId}`;
    if (!lensChanged && this._detailRenderKey === detailKey) return;
    this._detailRenderKey = detailKey;

    // Host-level detail generation. Every detail (re)render bumps it; an
    // async renderDetail (from this OR a previously-switched lens — they all
    // target the same shared detail pane) checks it and bails if a newer one
    // started, so a stale mount can't wipe/clobber the current lens.
    this._detailGen = (this._detailGen || 0) + 1;
    if (this._currentLensId === 'agents') {
      // Pass the scoped agent list to the fleet view; the selected
      // agent itself is allowed to come from outside the scope (we
      // still need to render its detail if the operator navigated
      // there via the palette before switching workspaces).
      this._currentLens.renderDetail(this.detailEl, { agent, agents: this.scopedAgents() });
    } else {
      this._currentLens.renderDetail(this.detailEl);
    }
  }

  // ── intent surface (used by shell + lenses + overlays) ────────────
  setLens(id, selectionId) {
    const wasLens = this.state.lens;
    if (id !== this.state.lens) {
      this.state.lens = id;
      savePreferences({ last_lens: id });
      this._syncUrl();
    }
    if (id === 'agents' && selectionId) {
      // We swap lens then set the agent selection on the lens instance.
      this._mountActiveLens();
      if (this._currentLens?.activeAgentId !== undefined) this._currentLens.activeAgentId = selectionId;
      this._syncUrl();   // reflect the focused agent in the URL
      this._markAgentViewed(selectionId);
    } else if (id === 'agents' && !selectionId && wasLens === 'agents'
               && this._currentLens && this._currentLens.activeAgentId) {
      // Re-activating Agents (rail icon / palette "Go to Agents") while an
      // agent is focused drops the selection → back to the fleet/home
      // dashboard. This is the way out of a detail view.
      this._currentLens.activeAgentId = null;
      this._syncUrl();
    }
    this.render();
  }
  /** Clear any focused agent and show the fleet/home dashboard. */
  goHome() {
    if (this.state.lens !== 'agents') { this.setLens('agents'); return; }
    if (this._currentLens && this._currentLens.activeAgentId) {
      this._currentLens.activeAgentId = null;
      this._syncUrl();
      this.render();
    }
  }
  setWorkspace(name) {
    if (this.state.workspace === name) return;
    this.state.workspace = name;
    savePreferences({ last_workspace: name });
    this._syncUrl();
    // Re-paint appearance for the new workspace (it may have its own
    // theme/density/glow override; otherwise it inherits the global one).
    applyAppearance(this, name).catch(() => {});
    this._loadWorkspacePlugins().then(() => { this._refreshComputedState(); this.render(); });
    // Usage totals are workspace-scoped — re-fetch for the new workspace.
    this._loadUsageTotals().then(() => this.render());
  }

  /** Keep the URL in sync with the active workspace + lens so the
   *  current view is bookmarkable / shareable. Uses replaceState so
   *  the browser history isn't spammed for every switch. */
  _syncUrl() {
    try {
      const url = new URL(location.href);
      if (this.state.workspace) url.searchParams.set('workspace', this.state.workspace);
      else url.searchParams.delete('workspace');
      if (this.state.lens && this.state.lens !== 'agents') {
        url.searchParams.set('lens', this.state.lens);
      } else {
        url.searchParams.delete('lens');
      }
      // Reflect the focused agent so the detail view is shareable.
      const agentId = (this.state.lens === 'agents') ? this._currentLens?.activeAgentId : null;
      if (agentId) url.searchParams.set('agent', agentId);
      else url.searchParams.delete('agent');
      // Plugin-lens sub-section (sidebar nav). Only meaningful on the
      // *currently mounted* plugin lens — guard on instance identity so a
      // stale section from the previous lens doesn't leak onto the new one
      // (e.g. switching telegram→external used to keep `?section=connections`).
      const lensMounted = this._currentLens?.def?.id === this.state.lens;
      const section = (this.state.lens !== 'agents' && lensMounted)
        ? this._currentLens?._section : null;
      if (section) url.searchParams.set('section', section);
      else url.searchParams.delete('section');
      window.history.replaceState({}, '', url.toString());
    } catch (_) {}
  }
  // Appearance setters. These persist into the appearance store (global
  // or per the active workspace) so they're part of the theme bundle and
  // survive reloads / travel across devices. `scope` defaults to the
  // active workspace; pass '' explicitly to force a global change.
  setDensity(v, scope) {
    this.state.density = v;
    document.documentElement.setAttribute('data-density', v);
    setAppearance(this, { density: v }, scope === undefined ? this.state.workspace : scope);
  }
  setGlow(v, scope) {
    this.state.glow = v;
    document.documentElement.setAttribute('data-glow', v);
    setAppearance(this, { glow: v }, scope === undefined ? this.state.workspace : scope);
  }
  setActiveTheme(name, scope) {
    this.state.theme = name;
    setAppearance(this, { theme: name }, scope === undefined ? this.state.workspace : scope);
  }
  // Legacy accent setter — the 5 accent presets are builtin themes of the
  // same name, so an accent change is just a theme change.
  setAccent(v) {
    this.state.accent = v;
    this.setActiveTheme(v);
  }

  /** Read-transition: collapse `complete-unread` → `idle` when the operator
   *  focuses an agent. Backend is narrow + idempotent — safe on every focus. */
  _markAgentViewed(agentId) {
    if (!agentId) return;
    this.api.fetch(`/api/agents/${encodeURIComponent(agentId)}/viewed`, { method: 'POST' })
      .catch(() => {});
  }

  selectAgent(id) { this.setLens('agents', id); }
  openCmdK(prefill) { openCommandPalette(this, prefill); }
  openNewAgent(prefill) { openNewAgentModal(this, prefill || {}); }
  openNotifications() { openNotificationsDrawer(this); }
  openSettings(section) { openSettings(this, section); }
  openPlugins() { this.setLens('plugins'); }
  openRestart() { openRestartModal(this); }
  openWorkspaceSwitcher() { this.openCmdK('ws:'); }

  async toggleWorkspacePlugin(name) {
    const cur = this.state.workspacePlugins.slice();
    const has = cur.includes(name);
    const next = has ? cur.filter(p => p !== name) : [...cur, name];
    try {
      // PATCH /api/workspaces/{name} writes both config.toml and the
      // workspace's agent.toml; subscribers (messaging, recipes,
      // github, …) react via the workspace.updated event.
      await api.fetch(`/api/workspaces/${encodeURIComponent(this.state.workspace)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plugins: next }),
      });
      this.state.workspacePlugins = next;
      await this.reloadWorkspaces();
      this.render();
    } catch (e) { alert('Failed: ' + e.message); }
  }

  async controlAgent(id, action) {
    try {
      await api.postJSON(`/api/agents/${encodeURIComponent(id)}/${action}`, {});
      await this.reloadAgents();
      this._refreshComputedState();
      this.render();
    } catch (e) { alert('Failed: ' + e.message); }
  }
  async deleteAgent(id) {
    const keepHistory = await this._confirmDeleteAgent(id);
    if (keepHistory === null) return;   // cancelled
    try {
      const r = await api.fetch(
        `/api/agents/${encodeURIComponent(id)}?purge_history=${keepHistory ? 'false' : 'true'}`,
        { method: 'DELETE' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await this.reloadAgents();
      this._refreshComputedState();
      // If we were viewing this agent, drop back to fleet.
      if (this._currentLensId === 'agents' && this._currentLens?.activeAgentId === id) {
        this._currentLens.activeAgentId = null;
      }
      this.render();
    } catch (e) { alert('Delete failed: ' + e.message); }
  }

  // Delete confirmation that surfaces the consequences + lets the
  // operator keep history for audit. Resolves to `keepHistory` (bool) or
  // `null` if cancelled.
  _confirmDeleteAgent(id) {
    return new Promise((resolve) => {
      const scrim = document.createElement('div');
      scrim.className = 'overlay-scrim';
      const modal = document.createElement('div');
      modal.style.cssText = 'position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);width:min(440px,92vw);background:var(--bg-1);border:1px solid var(--line-3);border-radius:var(--r-4);padding:20px;z-index:200;box-shadow:0 0 0 1px var(--line-2),0 30px 60px rgba(0,0,0,.55)';
      modal.innerHTML = `
        <div style="font-size:var(--t-xl);font-weight:600;margin-bottom:8px">Delete agent "${esc(id)}"?</div>
        <div style="color:var(--t-2);font-size:var(--t-sm);line-height:1.5;margin-bottom:12px">
          Stops the process and removes the YAML spec, DB row, and per-agent
          runtime files (sessions, fleet-context, prompt addendum). Cannot be undone.
        </div>
        <label style="display:flex;gap:8px;align-items:flex-start;font-size:var(--t-sm);color:var(--t-2);margin-bottom:16px;cursor:pointer">
          <input type="checkbox" data-keep style="margin-top:2px">
          <span>Keep history for audit <span style="color:var(--t-4)">(events, usage, cost, messages, runs). Otherwise these are purged too.</span></span>
        </label>
        <div style="display:flex;justify-content:flex-end;gap:8px">
          <button class="btn ghost" data-act="cancel">Cancel</button>
          <button class="btn danger" data-act="confirm">Delete</button>
        </div>`;
      document.body.appendChild(scrim);
      document.body.appendChild(modal);
      const done = (v) => { scrim.remove(); modal.remove(); resolve(v); };
      scrim.addEventListener('click', () => done(null));
      modal.querySelector('[data-act="cancel"]').addEventListener('click', () => done(null));
      modal.querySelector('[data-act="confirm"]').addEventListener('click', () => {
        done(modal.querySelector('[data-keep]').checked);
      });
    });
  }
}

// PluginLens — wraps a plugin's static module as a lens.
//
// Sidebar navigation (standardized, opt-in): a plugin lens class MAY define
// `sections()` returning nav entries — flat `{id,label,icon?,badge?}` items
// and/or groups `{group:"Title", items:[...]}`. When non-empty, the host
// renders a consistent left-sidebar nav (active highlight, icons, badge
// counts, group headers), tracks the active section (deep-linked via
// `?section=`), and hands the plugin the active id + callbacks through
// `mount`'s ctx:
//   ctx.section            — active section id (or null when no sections)
//   ctx.onSectionChange(cb)— register; host calls cb(id) when nav changes
//   ctx.setSection(id)     — switch section programmatically
//   ctx.refreshNav()       — re-render the sidebar (e.g. to update badges)
// A plugin that doesn't define `sections()` keeps the whole UI in the detail
// pane (the previous behavior), with a placeholder sidebar.
class PluginLens {
  constructor(host, def) {
    this.host = host;
    this.def = def;
    this.instance = null;
    this._instanceP = null;     // memoized import
    this._section = null;       // active section id
    this._navSpec = [];         // last sections() snapshot
    this._sectionCbs = [];      // onSectionChange subscribers
    this._sideEl = null;
    this._detailEl = null;
  }

  _flatten(spec) {
    const out = [];
    for (const s of (spec || [])) {
      if (s && Array.isArray(s.items)) out.push(...s.items.filter(i => i && i.id));
      else if (s && s.id) out.push(s);
    }
    return out;
  }

  // Import once; resolve the initial active section from the first sections()
  // snapshot so the sidebar and detail agree (no render-order race).
  _load() {
    if (!this._instanceP) {
      this._instanceP = (async () => {
        const mod = await import(stamped(this.def.module));
        const Klass = mod.default || mod.Lens || mod.Panel;
        if (!Klass) throw new Error('no default export');
        const inst = new Klass();
        this.instance = inst;
        let items = [];
        try { items = this._flatten(typeof inst.sections === 'function' ? inst.sections() : []); }
        catch (_) { items = []; }
        if (items.length) {
          const ids = items.map(i => i.id);
          const fromUrl = this.host._pendingSection;
          this.host._pendingSection = null;   // consume once
          if (!this._section || !ids.includes(this._section)) {
            this._section = (fromUrl && ids.includes(fromUrl)) ? fromUrl : ids[0];
          }
        } else {
          this._section = null;
        }
        return inst;
      })();
    }
    return this._instanceP;
  }

  async renderSidebar(container) {
    this._sideEl = container;
    let inst;
    try { inst = await this._load(); }
    catch (e) {
      container.innerHTML = `<div class="side-head"><div class="side-title">${esc(this.def.label)}</div></div>`;
      return;
    }
    let spec = [];
    try { spec = (typeof inst.sections === 'function' ? inst.sections() : []) || []; }
    catch (_) { spec = []; }
    this._navSpec = spec;
    const items = this._flatten(spec);
    if (!items.length) {
      container.innerHTML = `
        <div class="side-head"><div class="side-title">${esc(this.def.label)}</div></div>
        <div class="pl-nav-empty">Plugin lens · all UI in the detail pane.</div>`;
      return;
    }
    const ids = items.map(i => i.id);
    if (!this._section || !ids.includes(this._section)) this._section = ids[0];
    // The section is resolved asynchronously (after the lens module loads),
    // so the synchronous _syncUrl at switch-time couldn't know it — write it
    // now that this lens is the mounted one.
    if (this.host.state.lens === this.def.id) { try { this.host._syncUrl(); } catch (_) {} }
    container.innerHTML =
      `<div class="side-head"><div class="side-title">${esc(this.def.label)}</div></div>`
      + `<div class="pl-nav">${this._navHtml(spec)}</div>`;
    container.querySelectorAll('[data-pl-section]').forEach(el =>
      el.addEventListener('click', () => this.setSection(el.dataset.plSection)));
  }

  _navHtml(spec) {
    const item = (it) => {
      const active = it.id === this._section ? ' active' : '';
      const ic = it.icon ? `<span class="pl-nav-ic">${iconSVG(_safeIconName(it.icon))}</span>` : '';
      const badge = (it.badge != null && it.badge !== '')
        ? `<span class="pl-nav-badge">${esc(it.badge)}</span>` : '';
      return `<button class="pl-nav-item${active}" data-pl-section="${esc(it.id)}" title="${esc(it.label || it.id)}">`
        + `${ic}<span class="pl-nav-label">${esc(it.label || it.id)}</span>${badge}</button>`;
    };
    let out = '';
    for (const s of spec) {
      if (s && Array.isArray(s.items)) {
        if (s.group) out += `<div class="pl-nav-group">${esc(s.group)}</div>`;
        out += s.items.filter(i => i && i.id).map(item).join('');
      } else if (s && s.id) {
        out += item(s);
      }
    }
    return out;
  }

  async renderDetail(container) {
    this._detailEl = container;
    // Generation guard against the mount race. renderDetail is async (awaits
    // the module import + the lens's own mount, which fetches). The shell
    // re-renders on live events / nav, and EVERY lens — including a previous
    // one mid-switch — writes to this same shared pane. The generation lives
    // on the host (not per-PluginLens) so a stale renderDetail from any lens
    // bails before its `innerHTML=''` wipes the current lens's lit markers
    // (which would throw "Cannot read properties of null (insertBefore)").
    const host = this.host;
    const gen = host._detailGen;
    const current = () => gen === host._detailGen;
    container.innerHTML = '<div class="pane-empty"><div class="sub">loading…</div></div>';
    try {
      const inst = await this._load();
      if (!current()) return;   // a newer detail render superseded us
      // Mount into a DEDICATED child (display:contents so it adds no layout
      // box), not the shared pane. If a stale lens render resumes later it
      // renders into its own — now detached — node instead of this shared one,
      // so lit never inserts against a wiped marker (the insertBefore crash).
      const mountEl = document.createElement('div');
      mountEl.style.display = 'contents';
      container.replaceChildren(mountEl);
      this._sectionCbs = [];
      await inst.mount(mountEl, host.api, {
        host,
        section: this._section,
        onSectionChange: (cb) => { if (typeof cb === 'function') this._sectionCbs.push(cb); },
        setSection: (id) => this.setSection(id),
        refreshNav: () => { if (this._sideEl) this.renderSidebar(this._sideEl); },
        isCurrent: current,
      });
    } catch (e) {
      if (!current()) return;   // superseded — the error is from a wiped mount
      container.innerHTML = `<div class="pane-empty"><div class="ttl">Plugin lens failed</div><div class="sub">${esc(e.message || String(e))}</div></div>`;
    }
  }

  setSection(id) {
    if (!id || id === this._section) return;
    this._section = id;
    if (this._sideEl) this.renderSidebar(this._sideEl);   // re-highlight
    for (const cb of this._sectionCbs) { try { cb(id); } catch (_) {} }
    try { this.host._syncUrl(); } catch (_) {}            // ?section= deep-link
  }

  unmount() { try { this.instance?.unmount?.(); } catch (_) {} }
}

// Local esc shim because we can't import inside the same module after
// import declarations (we already import primitives.esc into other
// files; here we just need a tiny inline copy for PluginLens).
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// Plugin manifests may set `icon` to either an ICONS-keyed name
// ("workspace", "git", "diamond", …) or a literal glyph like "⎇".
// Glyphs aren't in our SVG set — fall back to a generic rail icon
// when we don't recognize the name so a manifest typo doesn't render
// blank.
// Derived from the live icon registries (heroicons + hand-rolled) so newly
// vendored icons (plugin/sliders/check/folder/…) are valid for plugin
// manifests without hand-maintaining a second list that silently drifts.
const _KNOWN_ICONS = knownIconNames();
function _safeIconName(name) {
  if (!name) return 'star';
  if (_KNOWN_ICONS.has(name)) return name;
  return 'star';
}

const app = new App();
window.__relaydeckApp = app;
app.boot();
