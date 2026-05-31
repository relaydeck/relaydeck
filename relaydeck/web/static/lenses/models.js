// lenses/models.js — Models lens. Three surfaces behind sidebar tabs:
//   • Presets  — pinned provider/model combos, each with real
//                request/token telemetry (usage_records + model_invocations).
//   • Providers — registered provider plugins (built-in + known + custom)
//                with key/base-url config and a live model catalog.
//   • Defaults — model roles (fast/frontier/…) and what each resolves to.
//
// MIGRATION NOTE — this is a RelayLens on @relaydeck/ui (build-less Lit). The
// data-driven chrome (sidebar tabs/rows, detail headers, pipeline, stat
// strips) is declarative lit-html; the genuinely-procedural pieces stay
// IMPERATIVE, mounted into stable anchor nodes the reactive layer never
// rebuilds (exactly like lenses/agents.js):
//   • the model-role picker (mountModelSelect) + the add-provider form sync;
//   • telemetry charts / recent calls / used-by (loaded async per preset);
//   • the provider model catalog (loaded async per provider).
// Stale async is guarded by a per-load seq counter. All numbers are real;
// nothing is fabricated; zero renders as 0.

import {
  RelayLens, html, nothing, ref, createRef, liveDirective,
  esc, icon, iconSVG, providerIcon, spark, areaChartSVG,
  fmtNum, fmtCost, live, confirm, openModal,
} from '@relaydeck/ui';
import { mountModelSelect } from '../model_select.js';

// Per-role glyphs (keyed by role name, capability fallback). Gives each
// default-model slot a distinct icon instead of one generic grid.
const ROLE_ICONS = {
  fast: 'bolt', frontier: 'star', classifier: 'filter',
  embedding: 'grid', vision: 'eye', voice: 'activity', image: 'palette',
};
const _CAP_ICONS = { text: 'bolt', embedding: 'grid', vision: 'eye', audio: 'activity', image: 'palette' };
function roleIcon(role) {
  return ROLE_ICONS[role?.name] || _CAP_ICONS[role?.capability] || 'grid';
}

function provColor(name) {
  let h = 0;
  for (const c of String(name || '')) h = (h * 31 + c.charCodeAt(0)) % 360;
  return {
    fg: `hsl(${h} 60% 62%)`,
    line: `hsl(${h} 60% 62% / .4)`,
    bg: `hsl(${h} 60% 50% / .14)`,
  };
}

function fmtClock(ts) {
  if (!ts) return '--:--:--';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export class ModelsLens extends RelayLens {
  constructor(host, def) {
    super(host, def || { id: 'models' });
    this.search = '';
    this.tab = 'presets';            // 'presets' | 'providers' | 'roles'
    this.presetFilter = 'all';       // all | healthy | issues
    this.providerFilter = 'all';     // all | configured | setup
    this.activeName = null;
    this.activeKind = 'preset';      // 'preset' | 'provider' | 'role'
    this.activeProvider = null;
    this.activeRole = null;
    this.presets = [];
    this.providers = [];
    this.roles = [];
    this.rolesUnmet = {};
    this.detected = [];
    this.healths = {};
    this._loaded = false;
    this._loadPromise = null;
    this._seq = 0;                   // stale-async guard for derived loads
    this._catalogCache = {};         // provider -> models[]
    this._statsCache = {};           // preset  -> stats
    this._liveUnsubs = [];
    // Imperative-mount bookkeeping: which selection each anchor is bound to,
    // so detail() re-paints don't re-mount the picker/charts/catalog.
    this._pickerRef = createRef();
    this._chartsRef = createRef();
    this._recentRef = createRef();
    this._usedRef = createRef();
    this._catalogRef = createRef();
    this._formRef = createRef();
    this._mounted = {};              // anchor-id -> bound key
    this._modelSel = null;          // active mountModelSelect handle (role)
    this._baseUrlDraft = {};        // provider name -> in-progress base URL edit
  }

  // ── host contract ────────────────────────────────────────────────────
  // Kick the first load + wire the live store the first time the sidebar
  // mounts, then defer to RelayLens for the reactive paint.
  renderSidebar(container) {
    this._wireLive();
    this._ensureLoaded().then(() => this.requestUpdate());
    super.renderSidebar(container);
  }

  // Real-time: keep providers/presets/roles fresh from the live store. On a
  // change we drop the per-preset stats cache (so the open detail re-pulls),
  // refresh derived data (healths/detected), and re-render — RelayLens's
  // requestUpdate is a no-op once the lens is unmounted, so a live tick never
  // touches another lens's detail (terminal safety).
  _wireLive() {
    if (this._liveWired) return;
    const onData = (which) => (d) => {
      if (!d) return;
      if (which === 'providers') {
        this.providers = d;
        // Keep the detail live too, so a provider change made elsewhere shows up
        // at once — UNLESS an input in the detail is focused (an active edit:
        // base URL, API key, …), where a repaint would disturb the keystroke /
        // caret; then refresh the sidebar only. (Base-URL edits are additionally
        // draft-protected; this guard also covers the key field + any input.)
        if (this._detailHasFocusedInput()) this.requestSidebarUpdate();
        else this.requestUpdate();
        return;
      }
      if (which === 'roles') {
        this.roles = d.roles || [];
        this.rolesUnmet = d.unmet || {};
      } else {
        this.presets = d;
        this._statsCache = {};
        this._mounted.stats = null;
      }
      this.requestUpdate();
    };
    this._liveUnsubs = [
      live.subscribe('/api/providers', onData('providers')),
      live.subscribe('/api/presets', onData('presets')),
      live.subscribe('/api/model-roles', onData('roles')),
    ];
    // Release these on unmount alongside onEvent/timer cleanups.
    this.addCleanup(() => {
      for (const u of this._liveUnsubs) { try { u(); } catch (_) {} }
      this._liveUnsubs = [];
      this._liveWired = false;
    });
    // Flip the guard only after subscriptions + teardown are registered, so a
    // throw mid-wiring leaves _liveWired=false and a remount can re-wire
    // (hardening from deepseekv4-coder's PR review).
    this._liveWired = true;
  }

  // True when an input/textarea/select inside the detail pane is focused — used
  // to hold a live provider repaint to the sidebar so an active edit (base URL,
  // API key, …) is never disturbed mid-keystroke.
  _detailHasFocusedInput() {
    const root = this._detailRoot;
    const a = document.activeElement;
    return !!(root && a && root.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  }

  async _load() {
    try { this.providers = await this.host.api.getJSON('/api/providers') || []; } catch (_) { this.providers = []; }
    try { this.presets = await this.host.api.getJSON('/api/presets') || []; } catch (_) { this.presets = []; }
    try { const r = await this.host.api.getJSON('/api/model-roles'); this.roles = r?.roles || []; this.rolesUnmet = r?.unmet || {}; } catch (_) { this.roles = []; }
    // Local model servers running but not yet registered as a provider.
    try { const d = await this.host.api.getJSON('/api/providers/detect'); this.detected = (d?.candidates || []).filter(c => !c.already_configured); } catch (_) { this.detected = []; }
    const checks = await Promise.all(this.presets.map(async (p) => {
      try {
        const r = await this.host.api.fetch(`/api/presets/${encodeURIComponent(p.name)}/check`);
        return [p.name, r.ok ? await r.json() : null];
      } catch (_) { return [p.name, null]; }
    }));
    this.healths = Object.fromEntries(checks);
    if (!this.activeName && this.presets.length) this.activeName = this.presets[0].name;
  }

  async _ensureLoaded(force) {
    if (force) { this._loaded = false; this._catalogCache = {}; this._statsCache = {}; }
    if (this._loaded) return;
    if (!this._loadPromise) {
      this._loadPromise = this._load().then(() => { this._loaded = true; this._loadPromise = null; });
    }
    await this._loadPromise;
  }

  // Reload + re-render (replaces the old `_ensureLoaded(true); this.host.render()`
  // refrain, which forced a full app re-mount on every mutation).
  async _reload() {
    await this._ensureLoaded(true);
    this.requestUpdate();
  }

  // ── Sidebar ────────────────────────────────────────────────────────
  sidebar() {
    const presetCount = this.presets.length;
    const providerCount = this.providers.length;
    const count = this._loaded
      ? (this.tab === 'providers' ? providerCount : presetCount)
      : '—';
    const tabN = (n) => this._loaded ? n : '—';
    const unmetCount = Object.keys(this.rolesUnmet).length;
    return html`
      <div class="side-head">
        <div class="side-title">Models <span class="count">${count}</span></div>
        <button class="btn icon sm" title="New preset" data-act="new"
          @click=${() => this._openForm({ name: '', provider: '', model: '' }, 'create')}>${icon('plus', 12)}</button>
      </div>
      <div class="side-search">${icon('search', 12)}
        <input placeholder="Search ${this.tab}…" .value=${liveDirective(this.search)}
          @input=${(e) => { this.search = e.target.value; this.requestUpdate(); }}></div>
      <div class="mdl-tabs">
        <button class="mdl-tab ${this.tab === 'presets' ? 'on' : ''}" data-tab="presets"
          @click=${() => this._switchTab('presets')}>Presets <span class="n">${tabN(presetCount)}</span></button>
        <button class="mdl-tab ${this.tab === 'providers' ? 'on' : ''}" data-tab="providers"
          @click=${() => this._switchTab('providers')}>Providers <span class="n">${tabN(providerCount)}</span></button>
        <button class="mdl-tab ${this.tab === 'roles' ? 'on' : ''}" data-tab="roles"
          @click=${() => this._switchTab('roles')}>Defaults <span class="n${unmetCount ? ' warn' : ''}">${tabN(this.roles.length)}</span></button>
      </div>
      <div class="mdl-subfilter">${this._subfilter()}</div>
      <div class="side-list">${this._loaded ? this._list() : html`<div class="mdl-empty">loading…</div>`}</div>`;
  }

  _switchTab(tab) {
    this.tab = tab;
    // Re-point the detail pane at the active tab's kind so switching tabs
    // swaps the detail too (not just the list), and auto-select the first
    // item so the detail isn't a bare empty state.
    this.activeKind = tab === 'roles' ? 'role' : tab === 'providers' ? 'provider' : 'preset';
    if (tab === 'providers' && !this.providers.some(p => p.name === this.activeProvider)) {
      this.activeProvider = this.providers[0]?.name || null;
    } else if (tab === 'roles' && !this.roles.some(r => r.name === this.activeRole)) {
      this.activeRole = this.roles[0]?.name || null;
    } else if (tab === 'presets' && !this.presets.some(p => p.name === this.activeName)) {
      this.activeName = this.presets[0]?.name || null;
    }
    this.requestUpdate();
  }

  _subfilter() {
    if (this.tab === 'roles') return nothing;
    const fbtn = (id, label, n, active, onPick) => html`<button data-f=${id}
      class=${id === active ? 'on' : ''} @click=${onPick}>${label}<span class="n">${n}</span></button>`;
    if (this.tab === 'providers') {
      const all = this.providers.length;
      const configured = this.providers.filter(p => !p.needs_key || p.has_key).length;
      const setup = all - configured;
      const pick = (f) => () => { this.providerFilter = f; this.requestUpdate(); };
      return html`
        ${fbtn('all', 'All', all, this.providerFilter, pick('all'))}
        ${fbtn('configured', 'Configured', configured, this.providerFilter, pick('configured'))}
        ${fbtn('setup', 'Setup', setup, this.providerFilter, pick('setup'))}`;
    }
    const all = this.presets.length;
    const healthy = this.presets.filter(p => this.healths[p.name]?.ok !== false).length;
    const issues = all - healthy;
    const pick = (f) => () => { this.presetFilter = f; this.requestUpdate(); };
    return html`
      ${fbtn('all', 'All', all, this.presetFilter, pick('all'))}
      ${fbtn('healthy', 'Healthy', healthy, this.presetFilter, pick('healthy'))}
      ${fbtn('issues', 'Issues', issues, this.presetFilter, pick('issues'))}`;
  }

  _list() {
    const q = this.search.trim().toLowerCase();
    if (this.tab === 'roles') return this._roleList(q);
    if (this.tab === 'providers') return this._providerList(q);
    return this._presetList(q);
  }

  _roleList(q) {
    let rows = this.roles.slice();
    if (q) rows = rows.filter(r => (r.name || '').toLowerCase().includes(q) || (r.capability || '').toLowerCase().includes(q));
    if (!rows.length) return html`<div class="mdl-empty">No roles match.</div>`;
    return rows.map((r) => {
      const sel = this.activeKind === 'role' && r.name === this.activeRole;
      const unmet = (this.rolesUnmet[r.name] || []).length > 0;
      const cls = r.source === 'default' ? 'ok' : (r.source === 'unset' ? (unmet ? 'err' : 'warn') : 'muted');
      const label = r.source === 'default' ? 'set' : (r.source === 'fallback' ? 'fallback' : 'unset');
      return html`
        <div class="mdl-row ${sel ? 'sel' : ''}"
          @click=${() => { this.activeKind = 'role'; this.activeRole = r.name; this.requestUpdate(); }}>
          <div class="mdl-av" style="color:var(--violet);border-color:var(--violet-soft,#3b2f55);background:rgba(139,92,246,.08)">${icon(roleIcon(r), 14)}</div>
          <div class="info">
            <div class="name truncate" title=${r.name}>${r.name}${unmet ? html` <span style="color:var(--err)">●</span>` : nothing}</div>
            <div class="sub truncate" title="${r.capability} · ${r.effective || 'unset'}">${r.capability} · ${r.effective || 'unset'}</div>
          </div>
          <div class="mdl-status ${cls === 'muted' ? 'warn' : cls}"><span class="dot"></span>${label}</div>
        </div>`;
    });
  }

  _providerList(q) {
    // Detected-but-unregistered local servers (Ollama/vLLM/LM Studio) → a
    // one-click "add" banner, mirroring the external-agents candidate cards.
    const detectedRows = (this.detected || []).map((c) => html`
      <div class="mdl-add" style="border-color:var(--ok-line,var(--acc-line));background:var(--ok-soft,var(--acc-soft))">
        ${icon('plus', 16)}
        <div style="flex:1">
          <div class="t">Detected ${c.label} — add?</div>
          <div class="h">${c.base_url} · ${fmtNum(c.model_count)} models</div>
        </div>
        <button class="btn sm primary" @click=${(ev) => this._addDetected(ev, c)}>Add + use</button>
      </div>`);

    let rows = this.providers.slice();
    if (this.providerFilter === 'configured') rows = rows.filter(p => !p.needs_key || p.has_key);
    else if (this.providerFilter === 'setup') rows = rows.filter(p => p.needs_key && !p.has_key);
    if (q) rows = rows.filter(p => (p.name || '').toLowerCase().includes(q));
    rows.sort((a, b) => (b.model_count || 0) - (a.model_count || 0) || a.name.localeCompare(b.name));

    const provRows = rows.map((pr) => {
      const sel = this.activeKind === 'provider' && this.activeProvider === pr.name;
      let cls = 'warn', label = 'needs key';
      if (!pr.needs_key) { cls = 'ok'; label = 'ready'; }
      else if (pr.has_key) { cls = 'ok'; label = 'key set'; }
      const presetBit = pr.preset_count ? ` · ${pr.preset_count} preset${pr.preset_count === 1 ? '' : 's'}` : '';
      const color = provColor(pr.name);
      const letter = (pr.name || '?')[0];
      // Avatar priority: models.dev logo (full-colour, via daemon proxy) →
      // simple-icons brand mark → coloured initial. A dead logo_url falls
      // back to the initial (onerror); no logo_url uses the brand mark.
      const av = pr.logo_url
        ? html`<img src=${pr.logo_url} alt="" width="18" height="18" loading="lazy"
             style="border-radius:3px" onerror="this.parentNode.textContent='${esc(letter)}'">`
        : (providerIcon(pr.name, 18) || letter);
      return html`
        <div class="mdl-row ${sel ? 'sel' : ''}"
          @click=${() => { this.activeKind = 'provider'; this.activeProvider = pr.name; this.requestUpdate(); }}>
          <div class="mdl-av" style="color:${color.fg};border-color:${color.line};background:${color.bg}">${av}</div>
          <div class="info">
            <div class="name truncate" title=${pr.name}>${pr.name}</div>
            <div class="sub truncate" title="${fmtNum(pr.model_count || 0)} models${presetBit}"><b>${fmtNum(pr.model_count || 0)}</b> models${presetBit}</div>
          </div>
          <div class="mdl-status ${cls}"><span class="dot"></span>${label}</div>
        </div>`;
    });

    const addCustom = html`
      <div class="mdl-add ${this.activeKind === 'provider' && this.activeProvider === '__add__' ? 'sel' : ''}"
        @click=${() => { this.activeKind = 'provider'; this.activeProvider = '__add__'; this.requestUpdate(); }}>
        ${icon('plus', 16)}
        <div><div class="t">Add custom provider</div><div class="h">local (ollama) or openai/anthropic-compatible</div></div>
      </div>`;

    return html`${detectedRows}${provRows}${addCustom}`;
  }

  async _addDetected(ev, c) {
    ev.stopPropagation();
    try {
      const r = await this.host.api.fetch('/api/providers/detect', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: c.suggested_name, base_url: c.base_url, api: c.api }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      this.activeKind = 'provider'; this.activeProvider = c.suggested_name;
      await this._reload();
    } catch (e) { /* surfaced via reload */ }
  }

  _presetList(q) {
    let rows = this.presets.slice();
    if (this.presetFilter === 'healthy') rows = rows.filter(p => this.healths[p.name]?.ok !== false);
    else if (this.presetFilter === 'issues') rows = rows.filter(p => this.healths[p.name]?.ok === false);
    if (q) rows = rows.filter(p => p.name.toLowerCase().includes(q) || (p.provider || '').toLowerCase().includes(q) || (p.model || '').toLowerCase().includes(q));

    const presetRows = rows.map((p) => {
      const healthy = this.healths[p.name]?.ok !== false;
      const sel = this.activeKind === 'preset' && p.name === this.activeName;
      const color = provColor(p.provider || p.name);
      const modelFull = p.model || '';
      const modelLeaf = modelFull.includes('/') ? modelFull.split('/').pop() : modelFull;
      const sparkHtml = (p.spark && p.spark.some(v => v))
        ? html`<div class="mdl-presetspark">${spark(p.spark, { color: color.fg, height: 16, fill: false, dot: false })}</div>` : nothing;
      const tok = p.tokens_24h ? html`<span class="mdl-presettok">${fmtNum(p.tokens_24h)} tok</span>` : nothing;
      const foot = (sparkHtml !== nothing || tok !== nothing || !healthy)
        ? html`<div class="mdl-row-foot">${sparkHtml}${tok}${healthy ? nothing : html`<span class="mdl-status err"><span class="dot"></span>issue</span>`}</div>`
        : nothing;
      return html`
        <div class="mdl-row mdl-row--preset ${sel ? 'sel' : ''}"
          @click=${() => { this.activeKind = 'preset'; this.activeName = p.name; this.requestUpdate(); }}>
          <div class="mdl-av" style="color:${color.fg};border-color:${color.line};background:${color.bg}">${icon('diamond', 14)}</div>
          <div class="info">
            <div class="name truncate" title=${p.name}>${p.name}</div>
            <div class="sub mdl-sub-split">
              <span class="mdl-prov truncate" title=${p.provider || ''}>${p.provider || '—'}</span>
              ${modelFull ? html`<span class="mdl-model truncate" title=${modelFull}>${modelLeaf || modelFull}</span>` : nothing}
            </div>
            ${foot}
          </div>
        </div>`;
    });

    const emptyRow = !rows.length ? html`<div class="mdl-empty">No presets match.</div>` : nothing;
    const addRow = html`
      <div class="mdl-add" @click=${() => this._openForm({ name: '', provider: '', model: '' }, 'create')}>
        ${icon('plus', 16)}
        <div><div class="t">New preset</div><div class="h">pin a provider/model combo</div></div>
      </div>`;
    return html`${emptyRow}${presetRows}${addRow}`;
  }

  // ── Detail dispatch ─────────────────────────────────────────────────
  detail() {
    if (!this._loaded) return html`<div class="pane-empty"><div class="sub">loading…</div></div>`;
    if (this.tab === 'roles' || this.activeKind === 'role') return this._roleDetail();
    if (this.activeKind === 'provider' && this.activeProvider === '__add__') return this._addProvider();
    if (this.activeKind === 'provider') return this._providerDetail();
    return this._presetDetail();
  }

  // ── Role (default-for-a-job) detail ─────────────────────────────────
  _roleDetail() {
    const role = this.roles.find(r => r.name === this.activeRole) || this.roles[0];
    if (!role) return html`<div class="pane-empty"><div class="ttl">No roles</div></div>`;
    this.activeRole = role.name;
    const unmet = this.rolesUnmet[role.name] || [];
    const srcLabel = role.source === 'default'
      ? html`<span class="chip ok">operator default</span>`
      : role.source === 'fallback'
        ? html`<span class="chip muted">built-in fallback</span>`
        : role.source === 'unset'
          ? html`<span class="chip" style="color:var(--err);border-color:var(--err)">needs config</span>`
          : nothing;

    return html`
      <div class="pane">
        <div class="cwk-hdr">
          <div class="cwk-hdr-left">
            <div class="dhdr-avatar" style="width:48px;height:48px;color:var(--violet);border-color:var(--violet-soft,#3b2f55);background:rgba(139,92,246,.08)">${icon(roleIcon(role), 24)}</div>
            <div class="cwk-hdr-meta">
              <div class="cwk-eyebrow">MODEL ROLE · ${role.capability}</div>
              <h1 class="cwk-name truncate">${role.name}</h1>
              <div class="cwk-chips">
                ${srcLabel}
                <span class="chip muted" style="text-transform:none"><span style="color:var(--t-4)">resolves to</span>&nbsp;${role.effective || 'unset'}</span>
              </div>
            </div>
          </div>
        </div>
        <div class="lens-body">
          <p style="color:var(--t-2);font-size:13px;line-height:1.55;max-width:680px;margin:0">${role.description}</p>
          ${unmet.length ? html`<div class="mdl-fhint warn" style="padding:8px 10px;border:1px solid var(--warn-soft);border-radius:var(--r-2);background:var(--warn-soft)">Needed by <b>${unmet.join(', ')}</b> but not configured — set a model below.</div>` : nothing}
          <div style="max-width:520px">
            <div style="font-family:var(--f-mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--t-3);margin-bottom:6px">Default model for this role</div>
            <div ${ref(this._pickerRef)} @rd-picker-change=${this._onRolePickerChange}></div>
            <div style="display:flex;gap:8px;margin-top:14px">
              <button class="btn primary" data-act="save" ?disabled=${!this._roleSpec} @click=${() => this._saveRole(role)}>Save default</button>
              ${role.configured ? html`<button class="btn" data-act="clear" @click=${() => this._clearRole(role)}>${icon('x', 12)} Clear (use ${role.has_fallback ? 'fallback' : 'none'})</button>` : nothing}
            </div>
            <div data-role-msg style="margin-top:10px;font-family:var(--f-mono);font-size:11px;min-height:14px">${this._roleMsg || nothing}</div>
          </div>
        </div>
      </div>`;
  }

  // Mount the imperative model picker into the stable anchor once per role
  // selection (re-paints keep the same anchor node, so we don't re-mount).
  _onRolePickerChange = (e) => { this._roleSpec = e.detail; this.requestUpdate(); };

  _mountRolePicker(role) {
    const el = this._pickerRef.value;
    if (!el) return;
    const key = `role:${role.name}`;
    if (this._mounted.picker === key) return;
    this._mounted.picker = key;
    try { this._modelSel = null; } catch (_) {}
    this._roleSpec = role.configured || '';
    mountModelSelect(this.host, el, {
      value: role.configured || '',
      allowDefault: false,
      onChange: (spec) => { el.dispatchEvent(new CustomEvent('rd-picker-change', { detail: spec })); },
    }).then((sel) => {
      this._modelSel = sel;
      // Seed the initial spec from the picker (so Save enables correctly).
      const v = sel.getValue();
      if (v && v !== this._roleSpec) { this._roleSpec = v; this.requestUpdate(); }
    });
  }

  async _saveRole(role) {
    const spec = this._modelSel ? this._modelSel.getValue() : this._roleSpec;
    if (!spec) return;
    try {
      const r = await this.host.api.fetch(`/api/model-roles/${encodeURIComponent(role.name)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ spec }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { this._roleMsg = html`<span style="color:var(--err)">${j.detail || 'Save failed'}</span>`; this.requestUpdate(); return; }
      this._roleMsg = j.warning
        ? html`<span style="color:var(--warn)">saved — ${j.warning}</span>`
        : html`<span style="color:var(--ok)">saved.</span>`;
      this._mounted.picker = null;     // re-mount the picker against fresh state
      await this._reload();
    } catch (e) { this._roleMsg = html`<span style="color:var(--err)">${e.message}</span>`; this.requestUpdate(); }
  }

  async _clearRole(role) {
    try {
      await this.host.api.fetch(`/api/model-roles/${encodeURIComponent(role.name)}`, { method: 'DELETE' });
      this._mounted.picker = null;
      this._roleMsg = null;
      await this._reload();
    } catch (e) { this._roleMsg = html`<span style="color:var(--err)">${e.message}</span>`; this.requestUpdate(); }
  }

  // ── Preset detail ───────────────────────────────────────────────────
  _presetDetail() {
    const preset = this.presets.find(p => p.name === this.activeName) || this.presets[0];
    if (!preset) {
      return html`<div class="pane-empty"><div class="ttl">No presets</div><div class="sub">Create one with the <b>New preset</b> button or <code>relaydeck preset create</code>.</div></div>`;
    }
    this.activeName = preset.name;
    const h = this.healths[preset.name] || {};
    const healthy = h.ok !== false;
    const color = provColor(preset.provider || preset.name);
    // Key + endpoint are owned by the provider, not the preset.
    const prov = (this.providers || []).find(p => p.name === preset.provider);
    const keyVal = prov
      ? (prov.needs_key ? html`\${vault:${prov.key_env}}` : html`<span style="color:var(--t-4)">local (no key)</span>`)
      : html`<span style="color:var(--t-4)">—</span>`;
    const keyTitle = prov
      ? (prov.needs_key ? `\${vault:${prov.key_env || ''}}` : 'local (no key)')
      : '—';
    const baseVal = prov ? (prov.base_url || prov.default_base_url || 'default') : (preset.base_url || 'default');

    return html`
      <div class="pane">
        <div class="cwk-hdr">
          <div class="cwk-hdr-left">
            <div class="dhdr-avatar" style="width:48px;height:48px;color:${color.fg};border-color:${color.line};background:${color.bg}">${icon('diamond', 24)}</div>
            <div class="cwk-hdr-meta">
              <div class="cwk-eyebrow">${(preset.provider || '').toUpperCase()} preset</div>
              <h1 class="cwk-name truncate">${preset.name}</h1>
              <div class="cwk-chips">
                <span class="chip ${healthy ? 'ok' : 'muted'}">${healthy ? 'healthy' : 'issue'}</span>
              </div>
              <div class="mdl-resolve truncate" title="${preset.provider || ''}/${preset.model || ''}">${preset.provider || '—'}<span class="mdl-resolve-sep">/</span>${preset.model || '—'}</div>
            </div>
          </div>
          <div class="cwk-hdr-actions">
            <button class="btn" data-act="edit" @click=${() => this._openForm(preset, 'edit')}>${icon('edit', 11)} Edit</button>
            <button class="btn ghost" data-act="clone" @click=${() => this._openForm(preset, 'clone')}>${icon('copy', 11)} Clone</button>
            <button class="btn icon danger" data-act="delete" title="Delete preset" @click=${() => this._deletePreset(preset)}>${icon('delete', 12)}</button>
          </div>
        </div>

        <div class="lens-body">
        ${!healthy && h.suggestion ? html`
          <div style="padding:11px 14px;background:var(--err-soft);border:1px solid rgba(248,113,113,.30);border-radius:var(--r-3);color:var(--err);font-size:var(--t-sm);display:flex;justify-content:space-between;align-items:center;gap:12px">
            <div><strong>Issue: </strong><span style="color:var(--t-1)"><code>${preset.model}</code> not in <b>${preset.provider}</b> catalog — did you mean <code style="color:var(--acc)">${h.suggestion}</code>?</span></div>
            <button class="btn sm primary" data-act="fix" @click=${() => this._applyFix(preset, h)}>Apply fix</button>
          </div>` : nothing}

        <div class="mdl-pipe" role="list" aria-label="Preset resolution pipeline">
          <div class="mdl-pipe-box accent" role="listitem"><div class="k">preset</div><div class="v" title=${preset.name}>${preset.name}</div></div>
          <div class="mdl-pipe-arrow" aria-hidden="true">${icon('arrow_r', 14)}</div>
          <div class="mdl-pipe-box" role="listitem"><div class="k">provider</div><div class="v" title=${preset.provider || ''}><span class="mdl-av" style="width:18px;height:18px;font-size:10px;color:${color.fg};border-color:${color.line};background:${color.bg}">${(preset.provider || '?')[0]}</span><span class="mdl-pipe-val">${preset.provider || '—'}</span></div></div>
          <div class="mdl-pipe-arrow" aria-hidden="true">${icon('arrow_r', 14)}</div>
          <div class="mdl-pipe-box" role="listitem"><div class="k">key <span style="color:var(--t-4);font-size:7px">· provider</span></div><div class="v mdl-pipe-key" title=${keyTitle}>${keyVal}</div></div>
          <div class="mdl-pipe-arrow" aria-hidden="true">${icon('arrow_r', 14)}</div>
          <div class="mdl-pipe-box" role="listitem"><div class="k">model id</div><div class="v" title=${preset.model || ''}><span class="mdl-pipe-val">${preset.model || '—'}</span></div></div>
        </div>

        <div class="mdl-charts" ${ref(this._chartsRef)}><div class="mdl-empty" style="grid-column:1/-1">loading telemetry…</div></div>

        <div class="mdl-bottom">
          <div class="cwk-card">
            <div class="cwk-card-head"><span class="ttl">Recent calls</span><span class="chip muted" style="font-size:9px" data-recent-note></span></div>
            <div class="cwk-card-body" style="padding:8px 14px" ${ref(this._recentRef)}><div class="mdl-empty">loading…</div></div>
          </div>
          <div class="mdl-side-col">
            <div class="cwk-card">
              <div class="cwk-card-head"><span class="ttl">Used by</span><span class="chip muted" style="font-size:9px" data-used-note></span></div>
              <div class="cwk-card-body" style="padding:8px 14px" ${ref(this._usedRef)}><div class="mdl-empty">loading…</div></div>
            </div>
            <div class="cwk-card">
              <div class="cwk-card-head"><span class="ttl">Provider config</span></div>
              <div class="cwk-card-body" style="padding:14px">
                <div class="mdl-cfg-slider"><div class="k">Key <span style="color:var(--t-4)">· ${preset.provider || ''}</span><b style="font-size:10px">${keyVal}</b></div></div>
                <div class="mdl-cfg-slider"><div class="k">Base URL <span style="color:var(--t-4)">· ${preset.provider || ''}</span><b style="font-size:10px">${baseVal}</b></div></div>
              </div>
            </div>
          </div>
        </div>
        </div>
      </div>`;
  }

  // Telemetry is loaded async into the stable anchors after each preset paint.
  // Stale renders are guarded by the active selection + a load seq.
  _loadPresetStats(preset) {
    const chartsEl = this._chartsRef.value;
    const recentEl = this._recentRef.value;
    const usedEl = this._usedRef.value;
    if (!chartsEl) return;
    const key = `preset:${preset.name}`;
    // Already painted for this selection, or a fetch is in flight → no-op.
    if (this._mounted.stats === key) return;
    this._mounted.stats = key;
    const seq = ++this._seq;
    const apply = (stats) => {
      // Bail if the pane was replaced by a newer render/selection.
      if (seq !== this._seq) return;
      if (this.activeName !== preset.name || this.activeKind !== 'preset') return;
      this._paintPresetStats(chartsEl, recentEl, usedEl, stats);
    };
    const cached = this._statsCache[preset.name];
    if (cached !== undefined) { apply(cached); return; }
    this.host.api.getJSON(`/api/presets/${encodeURIComponent(preset.name)}/stats`)
      .then((stats) => { this._statsCache[preset.name] = stats; apply(stats); })
      .catch(() => { this._statsCache[preset.name] = null; apply(null); });
  }

  _paintPresetStats(chartsEl, recentEl, usedEl, stats) {
    if (!chartsEl || !stats) { if (chartsEl) chartsEl.innerHTML = '<div class="mdl-empty" style="grid-column:1/-1">no telemetry</div>'; return; }
    const reqRate = stats.requests_hour ? (stats.requests_hour / 60).toFixed(1) : '0';
    const tokRate = stats.tokens_hour ? Math.round(stats.tokens_hour / 3600) : 0;
    const lat = stats.latency || {};
    const succ = lat.success_rate;
    const cost = stats.cost_24h || 0;
    const per1k = stats.tokens_24h ? (cost / stats.tokens_24h * 1000) : 0;
    chartsEl.innerHTML = `
      <div class="mdl-chart">
        <div class="mdl-chart-head"><span class="k">Requests · last hour</span><span class="rate">${reqRate}/min</span></div>
        <div class="mdl-chart-big">${esc(fmtNum(stats.requests_hour || 0))}</div>
        <div class="mdl-chart-svg">${areaChartSVG(stats.requests_series || [], { height: 100, color: 'var(--warn)' })}</div>
        <div class="mdl-chart-axis"><span>-60m</span><span>-30m</span><span>now</span></div>
      </div>
      <div class="mdl-chart">
        <div class="mdl-chart-head"><span class="k">Tokens · last hour</span><span class="rate">${tokRate}/sec</span></div>
        <div class="mdl-chart-big">${esc(fmtNum(stats.tokens_hour || 0))}</div>
        <div class="mdl-chart-svg">${areaChartSVG(stats.tokens_series || [], { height: 100, color: 'var(--acc)' })}</div>
        <div class="mdl-chart-axis"><span>-60m</span><span>-30m</span><span>now</span></div>
      </div>
      <div class="mdl-chart mdl-metrics">
        <div class="mdl-metric">
          <div class="k"><span>Success · 24h</span></div>
          <div class="v ${succ == null ? '' : (succ >= 0.95 ? 'ok' : 'warn')}">${succ == null ? '—' : (succ * 100).toFixed(1) + '%'}</div>
          <div class="mdl-bar ${succ == null ? '' : (succ >= 0.95 ? 'ok' : 'warn')}"><i style="width:${succ == null ? 0 : Math.round(succ * 100)}%"></i></div>
        </div>
        <div class="mdl-metric">
          <div class="k"><span>Avg latency</span></div>
          <div class="v">${lat.count ? lat.avg_latency_ms + '<span style="font-size:11px;color:var(--t-3)">ms</span>' : '—'}</div>
          <div class="mdl-bar warn"><i style="width:${lat.count ? Math.min(100, Math.round(lat.avg_latency_ms / 30)) : 0}%"></i></div>
        </div>
        <div class="mdl-metric">
          <div class="k"><span>Cost · 24h</span></div>
          <div class="v">${esc(fmtCost(cost))}</div>
          <div class="sub">${per1k ? '$' + per1k.toFixed(4) + '/1k tok' : 'no recorded cost'}</div>
        </div>
      </div>`;

    // Recent calls (traced model-action invocations only).
    const recentNote = recentEl?.parentElement?.querySelector('[data-recent-note]')
      || document.querySelector('[data-recent-note]');
    const recent = stats.recent || [];
    if (recentNote) {
      recentNote.textContent = recent.length ? `${recent.length} traced` : '';
      recentNote.style.display = recent.length ? '' : 'none';
    }
    if (recentEl) {
      if (!recent.length) {
        recentEl.innerHTML = '<div class="mdl-empty">No traced calls — prompt/response tracing covers automation <code>model</code> calls, not chat-agent usage.</div>';
      } else {
        recentEl.innerHTML = recent.map(r => {
          const inTok = r.tokens_known ? r.prompt_tokens : r.prompt_chars;
          const outTok = r.tokens_known ? r.completion_tokens : r.completion_chars;
          const unit = r.tokens_known ? '' : 'c';
          return `<div class="mdl-call">
            <span class="t">${fmtClock(r.ts)}</span>
            <span class="st ${r.ok ? '' : 'err'}">${r.ok ? '200' : 'ERR'}</span>
            <span class="ag truncate">${esc(r.agent_id || '')}</span>
            <span class="tok">${esc(fmtNum(inTok))}${unit} ↑ / ${esc(fmtNum(outTok))}${unit} ↓</span>
            <span class="lat">${r.latency_ms}ms</span>
            <span class="mdl-call-prompt">${esc(r.error || r.prompt || '')}</span>
          </div>`;
        }).join('');
      }
    }

    // Used by — agents that actually called this model in 24h.
    const usedNote = usedEl?.parentElement?.querySelector('[data-used-note]')
      || document.querySelector('[data-used-note]');
    const used = stats.used_by || [];
    if (usedNote) {
      usedNote.textContent = used.length ? `${used.length} agent${used.length === 1 ? '' : 's'}` : '';
      usedNote.style.display = used.length ? '' : 'none';
    }
    if (usedEl) {
      usedEl.innerHTML = used.length
        ? used.map(u => `<div class="mdl-usedby-row"><span class="ag"><span class="dot"></span>${esc(u.agent_id)}</span><span class="c">${esc(fmtNum(u.requests))} calls · ${esc(fmtNum(u.tokens))} tok</span></div>`).join('')
        : '<div class="mdl-empty">No recorded usage in the last 24h.</div>';
    }
  }

  async _deletePreset(preset) {
    const ok = await confirm({ title: `Delete preset "${preset.name}"?`, danger: true, confirmLabel: 'Delete' });
    if (!ok) return;
    try {
      const r = await this.host.api.fetch(`/api/presets/${encodeURIComponent(preset.name)}`, { method: 'DELETE' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      this.activeName = null;
      await this._ensureLoaded(true);
      this.activeName = this.presets[0]?.name || null;
      this.requestUpdate();
    } catch (e) { alert('Delete failed: ' + e.message); }
  }

  async _applyFix(preset, h) {
    if (!h.suggestion) return;
    try {
      await this.host.api.fetch(`/api/presets/${encodeURIComponent(preset.name)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: h.suggestion }),
      });
      this._statsCache[preset.name] = undefined;
      this._mounted.stats = null;
      await this._reload();
    } catch (e) { alert('Apply fix failed: ' + e.message); }
  }

  // ── Provider detail ─────────────────────────────────────────────────
  _providerDetail() {
    const pr = (this.providers || []).find(p => p.name === this.activeProvider) || this.providers[0];
    if (!pr) return html`<div class="pane-empty"><div class="ttl">No providers</div><div class="sub">Add one with <b>+ custom</b> or connect a key under Settings.</div></div>`;
    this.activeProvider = pr.name;
    const ok = !pr.needs_key || pr.has_key;
    const refreshed = pr.last_refresh_ts ? new Date(pr.last_refresh_ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'never';
    const color = provColor(pr.name);
    const statusChip = !pr.needs_key
      ? html`<span class="chip ok">● ready</span>`
      : (pr.has_key ? html`<span class="chip ok">● key set</span>` : html`<span class="chip warn">● needs key</span>`);

    return html`
      <div class="pane">
        <div class="cwk-hdr">
          <div class="cwk-hdr-left">
            <div class="dhdr-avatar" style="width:48px;height:48px;font-family:var(--f-mono);font-weight:600;font-size:20px;text-transform:uppercase;color:${color.fg};border-color:${color.line};background:${color.bg}">${(pr.name || '?')[0]}</div>
            <div class="cwk-hdr-meta">
              <div class="cwk-eyebrow">provider</div>
              <h1 class="cwk-name truncate">${pr.name}</h1>
              <div class="cwk-chips">
                ${statusChip}
                <span class="chip muted">${fmtNum(pr.model_count || 0)} models</span>
                <span class="chip muted">${pr.api || 'native'}-${pr.api && pr.api !== 'native' ? 'compatible' : 'api'}</span>
                ${pr.preset_count ? html`<span class="chip muted">${pr.preset_count} preset${pr.preset_count === 1 ? '' : 's'} use this</span>` : nothing}
                ${pr.custom ? html`<span class="chip accent" style="text-transform:none">custom</span>` : nothing}
              </div>
            </div>
          </div>
          <div class="cwk-hdr-actions">
            <button class="btn" data-act="refresh" @click=${() => this._refreshProvider(pr)}>${icon('restart', 11)} Refresh catalog</button>
            ${pr.custom ? html`<button class="btn icon danger" data-act="delete" title="Delete custom provider" @click=${() => this._deleteProvider(pr)}>${icon('delete', 12)}</button>` : nothing}
          </div>
        </div>

        <div class="lens-body">
        ${pr.description ? html`<div style="color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs)">${pr.description}</div>` : nothing}

        <div class="stat-strip" style="margin:0;--cols:4">
          <div class="stat-cell"><div class="k">Models in catalog</div><div class="v">${fmtNum(pr.model_count || 0)}</div><div class="sub">refreshed ${refreshed}</div></div>
          <div class="stat-cell"><div class="k">Presets using this</div><div class="v">${pr.preset_count || 0}</div><div class="sub">${(pr.presets || []).slice(0, 3).join(' · ') || 'none yet'}</div></div>
          <div class="stat-cell"><div class="k">Tokens · 24h</div><div class="v">${fmtNum(pr.tokens_24h || 0)}</div><div class="sub">across ${pr.preset_count || 0} preset${pr.preset_count === 1 ? '' : 's'}</div></div>
          <div class="stat-cell"><div class="k">Cost · 24h</div><div class="v">${fmtCost(pr.cost_24h || 0)}</div><div class="sub">${ok ? 'billed to vault key' : 'provider unavailable'}</div></div>
        </div>

        <div class="mdl-cols">
          <div class="cwk-card">
            <div class="cwk-card-head"><span class="ttl">Configuration</span></div>
            <div class="cwk-card-body" style="padding:16px;display:flex;flex-direction:column;gap:18px">
              ${pr.needs_key ? html`
              <div class="mdl-field">
                <label class="mdl-flbl">API key <span class="muted">(${pr.key_env} → vault)</span></label>
                <div class="mdl-frow"><input data-key type="password" placeholder=${pr.has_key ? '•••••••• — type to replace' : 'paste API key'}><button class="btn" data-act="savekey" @click=${(e) => this._saveKey(e, pr)}>Save</button></div>
                <div class="mdl-fhint ${pr.has_key ? 'ok' : ''}">${pr.has_key ? '● key stored in vault — never shown' : '● no key set — provider unavailable'}</div>
              </div>` : html`<div class="mdl-fhint">This provider is local and needs no API key.</div>`}
              <div class="mdl-field">
                <label class="mdl-flbl">Base URL <span class="muted">(override — default: ${pr.default_base_url || '—'})</span></label>
                <div class="mdl-frow"><input data-baseurl placeholder=${pr.default_base_url || 'https://…'}
                  .value=${liveDirective(this._baseUrlDraft[pr.name] ?? (pr.base_url && pr.base_url !== pr.default_base_url ? pr.base_url : ''))}
                  @input=${(e) => { this._baseUrlDraft = { ...this._baseUrlDraft, [pr.name]: e.target.value }; }}><button class="btn" data-act="saveurl" @click=${(e) => this._saveBaseUrl(e, pr)}>Save</button></div>
                <div class="mdl-fhint">resolves to <span class="acc">${pr.base_url || pr.default_base_url || '—'}</span> — leave blank to use the default</div>
              </div>
              <div class="mdl-fhint" data-msg style="min-height:12px"></div>
            </div>
          </div>
          <div class="cwk-card">
            <div class="cwk-card-head"><span class="ttl">Model catalog</span><button class="btn icon sm" data-act="refresh2" title="Refresh" @click=${() => this._refreshProvider(pr)}>${icon('restart', 11)}</button></div>
            <div class="cwk-card-body" style="padding:6px 14px" ${ref(this._catalogRef)}><div class="mdl-empty">loading catalog…</div></div>
          </div>
        </div>
        <div class="mdl-fhint">New provider plugins appear here automatically. Models resolve as <code>${pr.name}/&lt;model&gt;</code> (or via a preset).</div>
        </div>
      </div>`;
  }

  _provMsg(pr, t, c) {
    const m = this._detailRoot?.querySelector('[data-msg]');
    if (m) { m.textContent = t; m.style.color = c || 'var(--t-3)'; }
  }

  async _saveKey(e, pr) {
    const input = e.target.closest('.mdl-frow')?.querySelector('[data-key]')
      || this._detailRoot?.querySelector('[data-key]');
    const v = input?.value;
    if (!v) { this._provMsg(pr, 'Enter a key first.', 'var(--warn)'); return; }
    try {
      await this.host.api.fetch(`/api/vault/keys/${encodeURIComponent(pr.key_env)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: v }),
      });
      this._provMsg(pr, '✓ key saved to vault', 'var(--ok)');
      // Invalidate the catalog mount so it re-fetches once the key unlocks it
      // (mirrors _refreshProvider). _reload() clears _catalogCache but the
      // mount-key short-circuit would otherwise keep the "Set an API key…" pane.
      delete this._catalogCache[pr.name];
      this._mounted.catalog = null;
      await this._reload();
    } catch (err) { this._provMsg(pr, 'Save failed: ' + err.message, 'var(--err)'); }
  }

  async _saveBaseUrl(e, pr) {
    const input = e.target.closest('.mdl-frow')?.querySelector('[data-baseurl]')
      || this._detailRoot?.querySelector('[data-baseurl]');
    const v = (input?.value || '').trim();
    try {
      await this.host.api.fetch(`/api/providers/${encodeURIComponent(pr.name)}/config`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ base_url: v }),
      });
      this._provMsg(pr, '✓ base URL saved', 'var(--ok)');
      delete this._baseUrlDraft[pr.name];
      // Re-fetch the catalog from the new endpoint (see _saveKey).
      delete this._catalogCache[pr.name];
      this._mounted.catalog = null;
      await this._reload();
    } catch (err) { this._provMsg(pr, 'Save failed: ' + err.message, 'var(--err)'); }
  }

  async _refreshProvider(pr) {
    this._provMsg(pr, 'refreshing…');
    try {
      await this.host.api.fetch(`/api/providers/${encodeURIComponent(pr.name)}/refresh`, { method: 'POST' });
      delete this._catalogCache[pr.name];
      this._mounted.catalog = null;
      await this._reload();
    } catch (err) { this._provMsg(pr, 'Refresh failed: ' + err.message, 'var(--err)'); }
  }

  async _deleteProvider(pr) {
    const ok = await confirm({
      title: `Delete custom provider "${pr.name}"?`,
      body: 'Its vault key, if any, is left intact.', danger: true, confirmLabel: 'Delete',
    });
    if (!ok) return;
    try {
      await this.host.api.fetch(`/api/providers/${encodeURIComponent(pr.name)}`, { method: 'DELETE' });
    } catch (e) { this._provMsg(pr, 'Delete failed: ' + e.message, 'var(--err)'); return; }
    this.activeProvider = null; this.activeKind = 'preset';
    await this._reload();
  }

  async _catalog(name) {
    if (!name) return [];
    if (this._catalogCache[name]) return this._catalogCache[name];
    let models = [];
    try { models = await this.host.api.getJSON(`/api/providers/${encodeURIComponent(name)}/models`) || []; }
    catch (_) { models = []; }
    this._catalogCache[name] = models;
    return models;
  }

  // Load the provider catalog into its stable anchor (once per provider).
  _loadCatalog(pr) {
    const el = this._catalogRef.value;
    if (!el) return;
    const key = `prov:${pr.name}`;
    if (this._mounted.catalog === key) return;
    this._mounted.catalog = key;
    const seq = ++this._seq;
    this._catalog(pr.name).then((models) => {
      if (seq !== this._seq) return;
      if (this.activeProvider !== pr.name || this.activeKind !== 'provider') return;
      this._paintCatalog(el, pr, models);
    });
  }

  _paintCatalog(el, pr, models) {
    if (!el) return;
    if (!models.length) {
      el.innerHTML = `<div class="mdl-empty">${pr.needs_key && !pr.has_key ? 'Set an API key to load the catalog.' : 'No models — try Refresh catalog.'}</div>`;
      return;
    }
    const shown = models.slice(0, 8);
    el.innerHTML = `<div class="mdl-cat">${shown.map(m => {
      const id = m.id || m.name || '';
      return `<div class="mdl-cat-row"><span class="m truncate">${iconSVG('diamond', 11)}${esc(id)}</span><button class="mdl-cat-add" data-model="${esc(id)}">+ preset</button></div>`;
    }).join('')}</div>${models.length > 8 ? `<div class="mdl-cat-more">+ ${models.length - 8} more…</div>` : ''}`;
    el.querySelectorAll('.mdl-cat-add').forEach(b => b.addEventListener('click', () => {
      const model = b.dataset.model;
      this._openForm({ name: '', provider: pr.name, model }, 'create');
    }));
  }

  // ── Add custom provider ─────────────────────────────────────────────
  _addProvider() {
    return html`
      <div class="pane">
        <div class="cwk-hdr"><div class="cwk-hdr-left">
          <div class="dhdr-avatar" style="width:48px;height:48px;color:var(--acc);border-color:var(--acc-line);background:var(--acc-soft)">${icon('plus', 24)}</div>
          <div class="cwk-hdr-meta">
            <div class="cwk-eyebrow">provider · custom</div>
            <h1 class="cwk-name">Add custom provider</h1>
            <div class="cwk-chips"><span class="chip muted" style="text-transform:none">A local (Ollama) endpoint or an OpenAI/Anthropic-compatible API — no plugin needed.</span></div>
          </div>
        </div></div>
        <div class="lens-body">
        <div class="cwk-card" style="max-width:760px" ${ref(this._formRef)}><div class="cwk-card-body" style="padding:18px;display:flex;flex-direction:column;gap:16px">
          <div class="mdl-cols" style="grid-template-columns:1fr 1fr">
            <div class="mdl-field"><label class="mdl-flbl">Name (id)</label><input data-f="name" placeholder="my-llm"></div>
            <div class="mdl-field"><label class="mdl-flbl">API</label><select data-f="api" style="font-family:var(--f-mono);font-size:var(--t-xs)"><option value="ollama">ollama (local)</option><option value="openai">openai-compatible</option><option value="anthropic">anthropic-compatible</option></select></div>
          </div>
          <div class="mdl-field"><label class="mdl-flbl">Base URL</label><input data-f="base_url" placeholder="http://192.168.1.50:11434"></div>
          <div class="mdl-field" data-keyrow><label class="mdl-flbl">API key env <span class="muted">(vault key name — default NAME_API_KEY)</span></label><input data-f="key_env" placeholder="optional"></div>
          <div class="mdl-fhint err" data-err style="display:none"></div>
          <div style="display:flex;justify-content:flex-end;gap:8px">
            <button class="btn ghost" data-act="cancel" @click=${() => { this.activeKind = 'preset'; this.activeProvider = null; this.requestUpdate(); }}>Cancel</button>
            <button class="btn primary" data-act="create" @click=${() => this._createProvider()}>${icon('plus', 11)} Add provider</button>
          </div>
          <div class="mdl-fhint" data-addhint>A local Ollama endpoint needs no key. Models load live from its catalog.</div>
        </div></div>
        </div>
      </div>`;
  }

  // Wire the add-provider API selector once (toggles the key row + hints).
  _wireAddProvider() {
    const root = this._formRef.value;
    if (!root || this._mounted.addForm) return;
    this._mounted.addForm = true;
    const apiSel = root.querySelector('[data-f="api"]');
    const keyRow = root.querySelector('[data-keyrow]');
    const baseInp = root.querySelector('[data-f="base_url"]');
    const addHint = root.querySelector('[data-addhint]');
    if (!apiSel) return;
    const syncApi = () => {
      const local = apiSel.value === 'ollama';
      keyRow.style.display = local ? 'none' : 'block';
      baseInp.placeholder = local ? 'http://192.168.1.50:11434' : 'https://my-endpoint/v1';
      addHint.textContent = local
        ? 'A local Ollama endpoint needs no key. Models load live from /api/tags.'
        : 'After adding, open it to set the API key. Models load live from {base_url}/models.';
    };
    apiSel.addEventListener('change', syncApi);
    syncApi();
  }

  async _createProvider() {
    const root = this._formRef.value;
    if (!root) return;
    const err = (t) => { const e = root.querySelector('[data-err]'); if (e) { e.textContent = t; e.style.display = 'block'; } };
    const val = (k) => root.querySelector(`[data-f="${k}"]`)?.value.trim() || '';
    const name = val('name');
    const base_url = val('base_url');
    const api = root.querySelector('[data-f="api"]')?.value;
    const key_env = val('key_env');
    if (!name) return err('Name is required.');
    if (!base_url) return err('Base URL is required.');
    try {
      const r = await this.host.api.fetch('/api/providers', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, base_url, api, key_env: key_env || undefined }),
      });
      if (!r.ok) { let m = `Create failed (${r.status})`; try { const j = await r.json(); if (j.detail) m = j.detail; } catch (_) {} return err(m); }
    } catch (e) { return err('Create failed: ' + e.message); }
    this.tab = 'providers';
    this.activeProvider = name; this.activeKind = 'provider';
    this._mounted.addForm = false;
    await this._reload();
  }

  // ── Post-paint hook: drive imperative widgets into stable anchors ─────
  // RelayLens paints both panes on every requestUpdate. After each detail
  // paint we (re)attach the procedural widgets that lit-html doesn't own,
  // keyed so a mere re-paint never re-mounts them (terminal-safety pattern).
  detailDidPaint() {
    if (this.tab === 'roles' || this.activeKind === 'role') {
      const role = this.roles.find(r => r.name === this.activeRole) || this.roles[0];
      if (role) this._mountRolePicker(role);
    } else if (this.activeKind === 'provider' && this.activeProvider === '__add__') {
      this._wireAddProvider();
    } else if (this.activeKind === 'provider') {
      const pr = (this.providers || []).find(p => p.name === this.activeProvider) || this.providers[0];
      if (pr) this._loadCatalog(pr);
    } else {
      const preset = this.presets.find(p => p.name === this.activeName) || this.presets[0];
      if (preset) this._loadPresetStats(preset);
    }
  }

  // RelayLens calls _paintDetail() on render; wrap it to fire the post-paint
  // hook (anchors are in the DOM by then) without touching the base class.
  _paintDetail() {
    // Reset imperative mounts whenever the active selection changes so the
    // hook re-attaches the right widget into the (now-fresh) anchors.
    this._syncMountKeys();
    super._paintDetail();
    if (this._detailRoot) this.detailDidPaint();
  }

  // Invalidate per-anchor mount bookkeeping when the selection target moves,
  // so widgets re-mount for the new role/preset/provider/add-form.
  _syncMountKeys() {
    const roleKey = (this.tab === 'roles' || this.activeKind === 'role')
      ? `role:${this.activeRole}` : null;
    if (this._mounted.pickerSel !== roleKey) { this._mounted.picker = null; this._mounted.pickerSel = roleKey; this._roleSpec = ''; this._roleMsg = null; }
    const statKey = (this.activeKind === 'preset') ? `preset:${this.activeName}` : null;
    if (this._mounted.statsSel !== statKey) { this._mounted.stats = null; this._mounted.statsSel = statKey; }
    const catKey = (this.activeKind === 'provider' && this.activeProvider !== '__add__') ? `prov:${this.activeProvider}` : null;
    if (this._mounted.catalogSel !== catKey) { this._mounted.catalog = null; this._mounted.catalogSel = catKey; }
    const addKey = (this.activeKind === 'provider' && this.activeProvider === '__add__') ? 'add' : null;
    if (this._mounted.addSel !== addKey) { this._mounted.addForm = false; this._mounted.addSel = addKey; }
  }

  // ── Preset create/edit/clone form (modal) ───────────────────────────
  // Provider-aware: the provider is a dropdown of registered providers, the
  // model is driven by that provider's live catalog (with a free-text
  // fallback), and the vault key + base URL default from the chosen provider.
  // The form markup is static; its many interdependent updates (auto-name,
  // preview, resolve line, catalog-driven model select) stay imperative,
  // wired against the modal body via querySelector — exactly like before,
  // just hosted in the kit's openModal instead of a hand-rolled scrim.
  _openForm(preset, mode) {
    const isClone = mode === 'clone';
    const isCreate = mode === 'create';
    const needName = isClone || isCreate;
    const title = isCreate ? 'New preset' : (isClone ? 'Clone preset' : 'Edit preset');
    const FL = 'display:block;font-family:var(--f-mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--t-3);margin-bottom:5px';
    const IN = 'width:100%;font-family:var(--f-mono);font-size:var(--t-xs);background:var(--bg-2);border:1px solid var(--line-2);border-radius:var(--r-1);padding:6px 10px;color:var(--t-1)';
    const nameVal = isClone ? `${preset.name}-copy` : (preset.name || '');

    // A provider is selectable for a preset if it's keyless/keyed OR it has a
    // usable model catalog (e.g. openrouter's public catalog with no key
    // yet). The preset's own provider is always kept too, so editing a legacy
    // preset never silently drops its provider even if it's gone unconfigured.
    const selectableProviders = this.providers.filter(
      p => !p.needs_key || p.has_key || (p.model_count || 0) > 0
    );
    const provNames = selectableProviders.map(p => p.name);
    if (preset.provider && !provNames.includes(preset.provider)) provNames.unshift(preset.provider);
    const noConfigured = provNames.length === 0;
    const provOf = (name) => this.providers.find(p => p.name === name);
    const provOptions = (sel) => ['<option value="">— choose provider —</option>']
      .concat(provNames.map(n => `<option value="${esc(n)}" ${n === sel ? 'selected' : ''}>${esc(n)}</option>`)).join('');
    const initColor = provColor(preset.provider || 'preset');

    // Static skeleton (innerHTML string) rendered into the modal body, then
    // wired imperatively — keeps the dense, interdependent form logic intact.
    const bodyHTML = `
      <div style="display:flex;align-items:flex-start;gap:13px;margin-bottom:16px">
        <div class="dhdr-avatar" data-avatar style="width:44px;height:44px;flex-shrink:0;color:${initColor.fg};border-color:${initColor.line};background:${initColor.bg}">${iconSVG('diamond', 22)}</div>
        <div style="flex:1;min-width:0">
          <div style="font-size:var(--t-xl);font-weight:600">${esc(title)}</div>
          <div style="color:var(--t-3);font-size:var(--t-xs);font-family:var(--f-mono);margin-top:3px">${isCreate ? 'pin a provider + model into a reusable, shareable combo' : esc(preset.name)}</div>
        </div>
      </div>
      ${noConfigured ? `<div class="mdl-fhint warn" style="margin-bottom:12px;padding:8px 10px;border:1px solid var(--warn-soft);border-radius:var(--r-2);background:var(--warn-soft)">No configured providers yet — add an API key under <b>Providers</b> first, then the catalog can load here.</div>` : ''}
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
        <div style="grid-column:span 2"><label style="${FL}">Provider</label><select data-prov style="${IN}">${provOptions(preset.provider || '')}</select><div class="mdl-fhint" data-prov-note style="margin-top:5px"></div></div>
        <div style="grid-column:span 2">
          <label style="${FL}">Model <span style="color:var(--t-4);text-transform:none;letter-spacing:0" data-cat-note></span></label>
          <select data-msel style="${IN};margin-bottom:6px"></select>
          <input data-f="model" placeholder="model id" value="${esc(preset.model || '')}" style="${IN};display:none">
          <div class="mdl-fhint" style="margin-top:5px" data-resolve></div>
        </div>
        ${needName ? `<div style="grid-column:span 2"><label style="${FL}">Name <span style="color:var(--t-4);text-transform:none;letter-spacing:0">(auto — edit if you like)</span></label><input data-f="name" placeholder="pick a provider + model" value="${esc(nameVal)}" style="${IN}"></div>` : ''}
        <div style="grid-column:span 2" class="mdl-fhint" data-inherit></div>
      </div>
      <div style="${FL};margin-bottom:6px">Preview</div>
      <div class="mdl-row" data-preview style="cursor:default;border-left:2px solid ${initColor.line};background:var(--bg-2);border-radius:var(--r-2);margin-bottom:12px"></div>
      <div data-err style="display:none;color:var(--err);font-size:var(--t-xs);font-family:var(--f-mono);margin-bottom:10px"></div>
      <div style="display:flex;justify-content:flex-end;gap:8px">
        <button class="btn ghost" data-act="cancel">Cancel</button>
        <button class="btn primary" data-act="save">${isCreate ? 'Create preset' : (isClone ? 'Create clone' : 'Save')}</button>
      </div>`;

    const modalBody = document.createElement('div');
    modalBody.className = 'mp-form-modal-body';
    modalBody.innerHTML = bodyHTML;

    const m = openModal({ size: 'md', class: 'mp-form-modal-scrim', content: () => modalBody });
    const close = () => m.close();
    const modal = modalBody;

    modal.querySelector('[data-act="cancel"]').addEventListener('click', close);
    const errEl = modal.querySelector('[data-err]');
    const provSel = modal.querySelector('[data-prov]');
    const modelSel = modal.querySelector('[data-msel]');
    const modelInput = modal.querySelector('[data-f="model"]');
    const inheritEl = modal.querySelector('[data-inherit]');
    const catNote = modal.querySelector('[data-cat-note]');
    const resolveEl = modal.querySelector('[data-resolve]');
    const avatarEl = modal.querySelector('[data-avatar]');
    const previewEl = modal.querySelector('[data-preview]');
    const nameInput = modal.querySelector('[data-f="name"]');

    const CUSTOM = '__custom__';
    const showCustom = (on) => { modelInput.style.display = on ? 'block' : 'none'; };
    const currentModel = () =>
      (modelInput.style.display !== 'none')
        ? modelInput.value.trim()
        : (modelSel.value && modelSel.value !== CUSTOM ? modelSel.value : '');

    // Auto-generated name: <provider>-<model-leaf>, de-duped. The user can
    // override; once they type, we stop regenerating.
    const slug = (s) => String(s || '').toLowerCase().split('/').pop()
      .replace(/[^a-z0-9.]+/g, '-').replace(/^-+|-+$/g, '').replace(/\.+$/, '').slice(0, 32);
    const uniqueName = (base) => {
      const taken = new Set(this.presets.map(p => p.name).filter(n => n !== preset.name));
      if (!base) return '';
      if (!taken.has(base)) return base;
      let i = 2; while (taken.has(`${base}-${i}`)) i++;
      return `${base}-${i}`;
    };
    const genName = () => {
      const p = slug(provSel.value.trim());
      const mdl = slug(currentModel());
      return (p && mdl) ? uniqueName(`${p}-${mdl}`) : '';
    };
    let nameDirty = !isCreate;   // only auto-fill on a fresh New preset
    if (nameInput) nameInput.addEventListener('input', () => { nameDirty = true; renderPreview(); });
    const maybeAutoName = () => {
      if (nameInput && !nameDirty) { const g = genName(); if (g) nameInput.value = g; }
    };

    const renderPreview = () => {
      const p = provSel.value.trim();
      const mdl = currentModel();
      const c = provColor(p || preset.provider || 'preset');
      if (avatarEl) { avatarEl.style.color = c.fg; avatarEl.style.borderColor = c.line; avatarEl.style.background = c.bg; }
      if (previewEl) previewEl.style.borderLeftColor = c.line;
      const nm = (nameInput ? nameInput.value.trim() : preset.name) || (p && mdl ? genName() : '') || 'unnamed preset';
      previewEl.innerHTML = `
        <div class="mdl-av" style="color:${c.fg};border-color:${c.line};background:${c.bg}">${iconSVG('diamond', 14)}</div>
        <div class="info">
          <div class="name truncate">${esc(nm)}</div>
          <div class="sub truncate">${p ? esc(p) : '<span style="color:var(--t-4)">no provider</span>'} ${mdl ? '· ' + esc(mdl) : ''}</div>
        </div>`;
    };

    const renderResolve = () => {
      const p = provSel.value.trim();
      const mdl = currentModel();
      resolveEl.innerHTML = !p
        ? '<span style="color:var(--t-4)">choose a provider</span>'
        : (mdl ? `resolves to <span class="acc">${esc(p)}/${esc(mdl)}</span>`
               : '<span style="color:var(--t-4)">choose a model</span>');
      maybeAutoName();
      renderPreview();
    };

    const provNote = modal.querySelector('[data-prov-note]');
    const renderInherit = () => {
      const pr = provOf(provSel.value);
      // Inline note: a selectable provider that still needs a key can be
      // pinned now, but flag that it won't run until the key is added.
      if (provNote) {
        if (pr && pr.needs_key && !pr.has_key) {
          provNote.innerHTML = `<span style="color:var(--warn)">⚠ no API key yet — pinning is fine, but add a key under <b>Providers</b> before an agent can use this preset.</span>`;
        } else {
          provNote.textContent = '';
        }
      }
      if (!inheritEl) return;
      if (!pr) { inheritEl.innerHTML = '<span style="color:var(--t-4)">API key &amp; endpoint are inherited from the provider you choose.</span>'; return; }
      const keyBit = pr.needs_key
        ? `key <span class="acc">\${vault:${esc(pr.key_env)}}</span>${pr.has_key ? '' : ' <span style="color:var(--warn)">(not set)</span>'}`
        : 'no key (local)';
      const url = pr.base_url || pr.default_base_url || '—';
      inheritEl.innerHTML = `Inherited from <b>${esc(pr.name)}</b>: ${keyBit} · <span class="acc">${esc(url)}</span> — manage under Providers.`;
    };

    const populateModels = async (providerName) => {
      if (!providerName) {
        modelSel.innerHTML = '<option value="">— choose a provider first —</option>';
        modelSel.disabled = true;
        showCustom(false);
        catNote.textContent = '';
        return;
      }
      modelSel.disabled = false;
      modelSel.innerHTML = '<option value="">loading catalog…</option>';
      const models = await this._catalog(providerName);
      if (provSel.value !== providerName) return;   // provider changed mid-fetch
      const cur = modelInput.value.trim() || (preset.model || '');
      const ids = models.map(mm => mm.id || mm.name || '').filter(Boolean);
      const inCatalog = cur && ids.includes(cur);
      const opts = ['<option value="">— choose a model —</option>']
        .concat(ids.map(id => `<option value="${esc(id)}" ${id === cur ? 'selected' : ''}>${esc(id)}</option>`))
        .concat([`<option value="${CUSTOM}" ${(cur && !inCatalog) ? 'selected' : ''}>✎ Custom id…</option>`]);
      modelSel.innerHTML = opts.join('');
      catNote.textContent = models.length ? `· ${models.length} in catalog` : '· no catalog — enter a custom id';
      // A custom id (or a catalog-less provider) reveals the text box.
      const needCustom = (cur && !inCatalog) || models.length === 0;
      if (needCustom) { modelSel.value = CUSTOM; modelInput.value = cur; }
      showCustom(needCustom);
      renderResolve();
    };

    provSel.addEventListener('change', () => {
      // Key + endpoint are the provider's — nothing to copy into the preset.
      renderInherit();
      // Reset the model when switching providers — ids are provider-specific.
      modelInput.value = '';
      populateModels(provSel.value);
    });
    modelSel.addEventListener('change', () => {
      if (modelSel.value === CUSTOM) { showCustom(true); modelInput.focus(); }
      else { showCustom(false); }
      renderResolve();
    });
    modelInput.addEventListener('input', renderResolve);

    // Initial population for the preset's current provider.
    renderInherit();
    populateModels(provSel.value);
    renderResolve();   // seed preview + resolve line even with no provider yet

    modal.querySelector('[data-act="save"]').addEventListener('click', async () => {
      const val = (k) => modal.querySelector(`[data-f="${k}"]`)?.value.trim() || '';
      // A preset is purely name → provider/model. Key + base URL are the
      // provider's; nothing else is stored here.
      const body = { provider: provSel.value.trim(), model: currentModel() };
      if (!body.provider || !body.model) { errEl.textContent = 'provider and model are required'; errEl.style.display = 'block'; return; }
      try {
        if (needName) {
          const newName = val('name');
          if (!newName) { errEl.textContent = 'name is required'; errEl.style.display = 'block'; return; }
          body.name = newName;
          const r = await this.host.api.fetch('/api/presets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          this.tab = 'presets'; this.activeKind = 'preset'; this.activeName = newName;
        } else {
          const r = await this.host.api.fetch(`/api/presets/${encodeURIComponent(preset.name)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
        }
        close();
        await this._reload();
      } catch (e) { errEl.textContent = 'Save failed: ' + e.message; errEl.style.display = 'block'; }
    });
  }

  unmount() {
    try { this._modelSel = null; } catch (_) {}
    super.unmount();   // releases live subs (via addCleanup), onEvent, timers
  }
}
