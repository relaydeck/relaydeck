// lenses/plugins.js — the Plugins lens. Discover, enable (daemon-wide AND
// per-workspace), CONFIGURE (typed settings forms), see UI contributions, and
// install/uninstall.
//
// REFERENCE MIGRATION (a full two-pane lens on the @relaydeck/ui kit). The
// pattern for a dashboard lens:
//   • extend RelayLens — keep the app.js contract (renderSidebar/renderDetail/
//     unmount + onAgentsChanged/onWorkspacesChanged hooks) for free;
//   • implement sidebar()/detail() returning Lit templates — no innerHTML, no
//     querySelector, no manual addEventListener;
//   • read shared collections from this.host.state; load lens-private data
//     async into instance fields + this.requestUpdate();
//   • subscribe to the SSE bus with this.onEvent() (auto-cleans on unmount);
//   • use kit components for the standardized bits — sideHead/sideSearch/
//     sideFilter, button, chip, <rd-toggle>, <rd-settings-form>, confirm().

import {
  RelayLens, html, nothing, icon, button, chip,
  sideHead, sideSearch, sideFilter, empty, confirm,
} from '@relaydeck/ui';

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'workspace', label: 'Workspace' },
  { id: 'global', label: 'Global' },
  { id: 'gate', label: 'Gates' },
  { id: 'active', label: 'Active' },
  { id: 'settings', label: 'Has settings' },
];

export class PluginsLens extends RelayLens {
  constructor(host, def) {
    super(host, def || { id: 'plugins' });
    this.search = '';
    this.filter = 'all';
    this.activeName = null;
    this.plugins = [];        // /api/plugins (loaded code plugins)
    this.catalog = [];        // /api/workspace-plugins (incl. gates + recommended)
    this.ui = { tabs: [], agent_tiles: [], widgets: [], header_chips: [] };
    this._gates = [];
    this._recommended = new Set();
    this.ws = host.state.workspace || (host.state.workspaces?.[0]?.name || '');
    this._loaded = false;
    this._settings = null;    // {name, schema, values, sources} for activeName
    this._installSource = '';
    this._installStatus = null;
    this._load();
    // Live: react to enable/disable + workspace plugin changes.
    this.onEvent((evt) => {
      if (/plugin|workspace/.test(evt?.type || '')) this._load();
    });
  }

  // ── data ──────────────────────────────────────────────────────────
  async _load() {
    const [plugins, catalog, ui] = await Promise.all([
      this.host.api.getJSON('/api/plugins').catch(() => []),
      this.host.api.getJSON('/api/workspace-plugins').catch(() => []),
      this.host.api.getJSON('/api/plugins/ui').catch(() => ({})),
    ]);
    this.plugins = plugins || [];
    this.catalog = catalog || [];
    this.ui = ui || {};
    const known = new Set(this.plugins.map((p) => p.name));
    this._gates = (this.catalog || [])
      .filter((c) => c.kind === 'harness-gate' && !known.has(c.name))
      .map((c) => ({
        name: c.name, description: c.description || '', category: 'harness-gate',
        kind: 'harness-gate', workspace_scoped: true, enabled: c.globally_enabled !== false,
        recommended: !!c.recommended, has_settings: false, _gate: true,
      }));
    this._recommended = new Set((this.catalog || []).filter((c) => c.recommended).map((c) => c.name));
    this._loaded = true;
    this.requestUpdate();
  }

  _all() { return [...this.plugins, ...(this._gates || [])]; }

  _wsPlugins(name) {
    const w = (this.host.state.workspaces || []).find((x) => x.name === name);
    return new Set((w && w.plugins) || []);
  }

  _select(name) {
    this.activeName = name;
    this._settings = null;
    const p = this._all().find((x) => x.name === name);
    if (p && p.has_settings && !p._gate) this._loadSettings(name);
    this.requestUpdate();
  }

  async _loadSettings(name) {
    try {
      const data = await this.host.api.getJSON(`/api/plugins/${encodeURIComponent(name)}/settings`);
      this._settings = { name, schema: data.schema || [], values: data.values || {}, sources: data.sources || {} };
    } catch (_) {
      this._settings = { name, schema: [], values: {}, sources: {}, error: true };
    }
    this.requestUpdate();
  }

  // ── sidebar ───────────────────────────────────────────────────────
  sidebar() {
    const all = this._all().filter((p) => this._match(p))
      .sort((a, b) => String(a.name).localeCompare(String(b.name)));
    const wsOn = this._wsPlugins(this.ws);
    const groups = [
      ['Workspace-scoped', all.filter((p) => p.workspace_scoped && p.kind !== 'harness-gate')],
      ['Harness gates', all.filter((p) => p.kind === 'harness-gate')],
      ['Global · daemon-wide', all.filter((p) => !p.workspace_scoped)],
    ];
    return html`
      ${sideHead(html`Plugins`, {
        count: this._all().length,
        actions: button({ variant: 'icon', size: 'sm', title: 'Install a plugin',
          onClick: () => this._select(null) }, icon('plus', 12)),
      })}
      ${sideSearch(this.search, (v) => { this.search = v; this.requestUpdate(); }, 'Search plugins…')}
      ${sideFilter(FILTERS, this.filter, (id) => { this.filter = id; this.requestUpdate(); })}
      <div class="side-list">
        ${!this._loaded ? html`<div class="plx-muted" style="padding:14px">loading…</div>`
          : !all.length ? html`<div class="plx-muted" style="padding:14px">No plugins match.</div>`
          : groups.map(([label, items]) => items.length ? html`
              <div class="plx-group">${label}<span class="n">${items.length}</span></div>
              ${items.map((p) => this._row(p, wsOn))}` : nothing)}
      </div>`;
  }

  _match(p) {
    const q = this.search.trim().toLowerCase();
    if (q && !(`${p.name} ${p.description || ''} ${p.category || ''}`.toLowerCase().includes(q))) return false;
    const wsOn = this._wsPlugins(this.ws);
    switch (this.filter) {
      case 'workspace': return !!p.workspace_scoped;
      case 'global': return !p.workspace_scoped;
      case 'gate': return p.kind === 'harness-gate';
      case 'active': return p.workspace_scoped ? wsOn.has(p.name) : p.enabled !== false;
      case 'settings': return !!p.has_settings;
      default: return true;
    }
  }

  _row(p, wsOn) {
    const loaded = p.enabled !== false;
    const listed = p.workspace_scoped ? wsOn.has(p.name) : null;
    const on = p.workspace_scoped ? (listed && loaded) : loaded;
    const inactive = !!(p.workspace_scoped && listed && !loaded);
    let dotTitle;
    if (p.workspace_scoped) {
      dotTitle = listed
        ? (loaded ? `active on @${this.ws}` : `listed on @${this.ws} · inactive (disabled daemon-wide)`)
        : `off on @${this.ws}`;
    } else {
      dotTitle = loaded ? 'enabled daemon-wide' : 'disabled daemon-wide';
    }
    const rec = (this._recommended.has(p.name) && !on && !inactive)
      ? html`<span class="plx-tag rec">rec</span>` : nothing;
    const inactiveTag = inactive ? html`<span class="plx-tag warn">inactive</span>` : nothing;
    return html`
      <button class="plx-row ${p.name === this.activeName ? 'active' : ''}"
        data-name=${p.name} @click=${() => this._select(p.name)}>
        <span class="plx-dot ${on ? 'on' : 'off'}" title=${dotTitle}></span>
        <span class="plx-rowbody">
          <span class="plx-rowname">${p.name} ${rec}${inactiveTag}</span>
          <span class="plx-rowdesc">${p.description || (p.kind === 'harness-gate' ? 'harness capability gate' : '')}</span>
        </span>
        ${p.has_settings ? html`<span class="plx-rowcog" title="has settings">${icon('sliders', 11)}</span>` : nothing}
      </button>`;
  }

  // ── detail ────────────────────────────────────────────────────────
  detail() {
    if (!this._loaded) return html`<div class="plx-detail"><div class="plx-muted" style="padding:30px">loading…</div></div>`;
    const p = this._all().find((x) => x.name === this.activeName);
    return p ? this._pluginDetail(p) : this._overview();
  }

  _overview() {
    const all = this.plugins;
    const wsScoped = all.filter((p) => p.workspace_scoped);
    const active = all.filter((p) => !p.workspace_scoped && p.enabled !== false).length;
    const withSettings = all.filter((p) => p.has_settings);
    const wsOn = this._wsPlugins(this.ws);
    const recoNotOn = (this.catalog || []).filter((c) =>
      c.recommended && c.globally_enabled !== false && !wsOn.has(c.name));
    const stat = (n, label) => html`<div class="plx-stat"><div class="plx-stat-n num">${n}</div><div class="plx-stat-l">${label}</div></div>`;
    return html`
      <div class="plx-detail">
        <div class="plx-overview-head">
          <div>
            <div class="plx-eyebrow">Plugins</div>
            <h1 class="plx-h1">Extend the fleet</h1>
            <div class="plx-lede">Plugins add harnesses, lenses, tiles, skills, and automation. Toggle them daemon-wide or per workspace, and configure each one here.</div>
          </div>
        </div>
        <div class="plx-stats">
          ${stat(all.length, 'installed')}
          ${stat(active, 'enabled')}
          ${stat(wsScoped.length, 'workspace-scoped')}
          ${stat(withSettings.length, 'configurable')}
        </div>
        <div class="plx-block">
          <div class="plx-block-h">${icon('plus', 13)} Install a plugin</div>
          <form class="plx-install" @submit=${this._install}>
            <input data-source .value=${this._installSource}
              @input=${(e) => { this._installSource = e.target.value; }}
              placeholder="relaydeck-plugin-example  ·  or a path / git URL" />
            <button class="btn primary sm" type="submit">${icon('plus', 12)} Install</button>
          </form>
          ${this._installStatus
            ? html`<div class="plx-install-status ${this._installStatus.cls}">${this._installStatus.text}</div>` : nothing}
          <div class="plx-muted" style="margin-top:6px">Installed plugins load on the next daemon start.</div>
        </div>
        ${recoNotOn.length ? html`
        <div class="plx-block">
          <div class="plx-block-h">${icon('star', 13)} Recommended for @${this.ws || '—'}</div>
          <div class="plx-reco">${recoNotOn.map((c) => html`
            <button class="plx-reco-i" @click=${() => this._reco(c.name)}>
              <span class="plx-reco-name">${c.name}</span>
              <span class="plx-reco-desc">${c.description || ''}</span>
              <span class="plx-reco-add">${icon('plus', 11)} enable</span>
            </button>`)}</div>
        </div>` : nothing}
        <div class="plx-block">
          <div class="plx-block-h">${icon('command', 13)} All plugins</div>
          <div class="plx-muted">Pick a plugin from the left to enable, configure, or inspect what it contributes.</div>
        </div>
      </div>`;
  }

  _pluginDetail(p) {
    const wsOn = this._wsPlugins(this.ws);
    const scopeBadge = p.kind === 'harness-gate'
      ? html`<span class="plx-badge gate">harness gate</span>`
      : (p.workspace_scoped ? html`<span class="plx-badge ws">workspace-scoped</span>` : html`<span class="plx-badge gl">global</span>`);
    const meta = [p.version ? `v${p.version}` : '', p.category || '', p.installed_via || p.source || '']
      .filter(Boolean).join(' · ');
    return html`
      <div class="plx-detail">
        <button class="plx-back" @click=${() => this._select(null)}>${icon('arrow_l', 12)} All plugins</button>
        <div class="plx-phead">
          <div class="plx-pico">${icon(p.kind === 'harness-gate' ? 'bolt' : 'command', 20)}</div>
          <div class="plx-pinfo">
            <div class="plx-pname">${p.name} ${scopeBadge}</div>
            <div class="plx-pmeta">${meta || '—'}</div>
          </div>
        </div>
        <div class="plx-pdesc">${p.description || 'No description provided by this plugin.'}</div>
        ${this._enableBlock(p, wsOn)}
        ${p.has_settings && !p._gate ? this._settingsBlock(p) : nothing}
        ${this._contributions(p.name)}
        ${p.user_installed ? html`
        <div class="plx-block">
          <div class="plx-block-h">${icon('delete', 13)} Manage</div>
          <button class="btn danger sm" @click=${() => this._uninstall(p)}>${icon('delete', 11)} Uninstall ${p.name}</button>
          <div class="plx-muted" style="margin-top:6px">Removes the package; restart the daemon to unload.</div>
        </div>` : nothing}
      </div>`;
  }

  _enableBlock(p, wsOn) {
    const wsList = this.host.state.workspaces || [];
    const wsScoped = !!p.workspace_scoped && p.kind !== 'harness-gate';
    const gate = p.kind === 'harness-gate';
    const globalOn = p.enabled !== false;
    const wsActive = wsOn.has(p.name);
    const wsInactive = wsScoped && !globalOn;
    return html`<div class="plx-block">
      <div class="plx-block-h">${icon('bolt', 13)} Enablement</div>
      ${gate ? nothing : html`
        <div class="plx-erow">
          <div>
            <div class="plx-erow-l">Loaded daemon-wide</div>
            <div class="plx-erow-d">Off unloads it everywhere${wsScoped ? ' (and removes its lens/tiles)' : ''}. Takes effect live.</div>
          </div>
          <rd-toggle ?on=${globalOn} name=${p.name} @change=${() => this._toggleGlobal(p)}></rd-toggle>
        </div>`}
      ${(wsScoped || gate) ? html`
        <div class="plx-erow">
          <div>
            <div class="plx-erow-l">Active in workspace</div>
            <div class="plx-erow-d">Turn this plugin on for a single workspace's agents.${
              wsInactive ? html` <b style="color:var(--warn)">Inactive until “Loaded daemon-wide” is on.</b>` : nothing}</div>
          </div>
          <div class="plx-ws-ctrl">
            <select @change=${(e) => { this.ws = e.target.value; this.requestUpdate(); }}>
              ${wsList.length ? wsList.map((w) => html`<option ?selected=${w.name === this.ws}>${w.name}</option>`)
                : html`<option disabled>no workspaces</option>`}
            </select>
            <rd-toggle ?on=${wsActive} ?disabled=${!wsList.length}
              @change=${(e) => this._setWorkspacePlugin(p.name, e.detail.on)}></rd-toggle>
          </div>
        </div>` : nothing}
      ${gate ? html`<div class="plx-muted">A harness capability gate — enable it per workspace above.</div>` : nothing}
    </div>`;
  }

  _settingsBlock(p) {
    const s = this._settings;
    return html`<div class="plx-block">
      <div class="plx-block-h">${icon('sliders', 13)} Settings</div>
      ${!s || s.name !== p.name ? html`<div class="plx-muted">loading…</div>`
        : s.error ? html`<div class="plx-muted">Settings unavailable.</div>`
        : !s.schema.length ? html`<div class="plx-muted">This plugin has no configurable settings.</div>`
        : html`<rd-settings-form .schema=${s.schema} .values=${s.values} .sources=${s.sources}
            @save=${(e) => this._saveSettings(e, p)}></rd-settings-form>`}
    </div>`;
  }

  _contributions(name) {
    const ui = this.ui || {};
    const bits = [];
    const add = (items, kind, ic) => (items || [])
      .filter((t) => (t.source || (t.id || '').split(':')[0]) === name)
      .forEach((t) => bits.push(html`<div class="plx-contrib"><span class="plx-contrib-ic">${icon(ic, 12)}</span>
        <span><b>${t.title || t.label || t.id}</b> <span class="plx-muted">${kind}</span></span></div>`));
    add(ui.tabs, 'lens / tab', 'layout');
    add(ui.agent_tiles, 'agent tile', 'grid');
    add(ui.widgets, 'home widget', 'activity');
    add(ui.header_chips, 'header chip', 'bolt');
    return bits.length ? html`<div class="plx-block">
      <div class="plx-block-h">${icon('layout', 13)} Contributes to the UI</div>
      <div class="plx-contribs">${bits}</div></div>` : nothing;
  }

  // ── actions ───────────────────────────────────────────────────────
  async _toggleGlobal(p) {
    const action = (p.enabled !== false) ? 'disable' : 'enable';
    try {
      await this.host.api.postJSON(`/api/plugins/${encodeURIComponent(p.name)}/${action}`, {});
      if (this.host.refreshPluginManifest) await this.host.refreshPluginManifest();
      await this._load();
    } catch (e) {
      alert('Failed: ' + (e.message || e));
      await this._load();
    }
  }

  async _setWorkspacePlugin(name, want) {
    const ws = this.ws;
    if (!ws) return;
    const cur = [...this._wsPlugins(ws)];
    const next = want ? (cur.includes(name) ? cur : [...cur, name]) : cur.filter((x) => x !== name);
    try {
      const r = await this.host.api.fetch(`/api/workspaces/${encodeURIComponent(ws)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plugins: next }),
      });
      if (!r.ok) {
        let detail = `HTTP ${r.status}`;
        try { const j = await r.json(); if (j && j.detail) detail = j.detail; } catch (_) {}
        throw new Error(detail);
      }
      await this.host.reloadWorkspaces();
      if (ws === this.host.state.workspace) {
        this.host.state.workspacePlugins = next;
        this.host._refreshComputedState?.();
      }
      this.requestUpdate();
    } catch (e) {
      alert('Failed to update workspace plugins: ' + (e.message || e));
      try { await this.host.reloadWorkspaces(); } catch (_) {}
      this.requestUpdate();
    }
  }

  async _reco(name) {
    await this._setWorkspacePlugin(name, true);
    await this._load();
  }

  _install = async (e) => {
    e.preventDefault();
    const source = (this._installSource || '').trim();
    if (!source) return;
    this._installStatus = { text: 'installing…', cls: '' };
    this.requestUpdate();
    try {
      const resp = await this.host.api.postJSON('/api/plugins/install', { source });
      const names = (resp.plugins || []).join(', ') || source;
      this._installStatus = { text: `✓ ${names} installed · restart the daemon to load`, cls: 'ok' };
      this._installSource = '';
      if (this.host.reloadPlugins) await this.host.reloadPlugins();
      await this._load();
    } catch (err) {
      this._installStatus = { text: 'failed: ' + (err.message || err), cls: 'err' };
      this.requestUpdate();
    }
  };

  async _uninstall(p) {
    const ok = await confirm({
      title: `Uninstall ${p.name}?`,
      body: 'Removes the package. Restart the daemon to fully unload.',
      danger: true, confirmLabel: 'Uninstall',
    });
    if (!ok) return;
    try {
      await this.host.api.deleteJSON(`/api/plugins/${encodeURIComponent(p.name)}`);
      this.activeName = null;
      if (this.host.reloadPlugins) await this.host.reloadPlugins();
      await this._load();
    } catch (e) { alert('Failed: ' + (e.message || e)); }
  }

  _saveSettings(e, p) {
    const form = e.target;
    this.host.api.postJSON(`/api/plugins/${encodeURIComponent(p.name)}/settings`, e.detail)
      .then(() => form.setStatus('✓ saved', 'ok'))
      .catch((err) => form.setStatus('failed: ' + (err.message || err), 'err'));
  }

  onWorkspacesChanged() { this.requestUpdate(); }
}
