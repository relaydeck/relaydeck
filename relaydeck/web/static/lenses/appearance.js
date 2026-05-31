// lenses/appearance.js — the Appearance lens (Settings → General → Customize).
//
// Sidebar: theme gallery (builtin + user, with swatches). Detail: an
// appearance control bar (scope · active theme · density · glow) above a
// live-preview token editor for the selected theme. Backed by
// /api/themes, /api/themes/contract, and /api/appearance. Edits paint the
// whole dashboard live via theme.js applyTokens; leaving without saving
// reverts to the active appearance.
//
// MIGRATION NOTE. On the @relaydeck/ui kit: extends RelayLens so the
// renderSidebar/renderDetail/unmount + onAppearanceChanged() contract (used
// both by app.js and by overlays.js's Customize sub-tab via new
// AppearanceLens(host).renderDetail(pane)) is preserved for free, and live
// subscriptions auto-release on unmount. Sidebar + detail are lit-html
// templates. The one genuinely-procedural bit kept imperative is per-keystroke
// token state + live preview: a text @input updates this.working and repaints
// the dashboard via applyTokens without re-rendering the whole pane (so the
// caret never jumps mid-edit) — same effect as the old direct-DOM sync. Color
// pickers / resets, which must push a value back into the text field, do
// requestUpdate().

import {
  RelayLens, html, nothing, esc, icon, live, ifDefined, classMap,
} from '@relaydeck/ui';
import {
  applyTokens, resolveTheme, applyAppearance, setAppearance, invalidateThemeCache,
} from '../theme.js';

const DENSITIES = ['compact', 'regular', 'comfy'];
const GLOWS = ['on', 'off'];

export class AppearanceLens extends RelayLens {
  constructor(host, def) {
    super(host, def || { id: 'appearance' });
    this.themes = [];
    this.contract = null;
    this.selected = null;       // theme name being edited
    this.scope = host.state.workspace || '';   // '' = global
    this.appearance = null;     // resolved appearance for the scope
    this.working = {};          // selected theme's own token overrides (editable)
    this.baseResolved = {};     // resolved tokens of the extends chain (for inherited display)
    this._previewing = false;
    this._loaded = false;
    this._themesSub = null;
  }

  async _load() {
    try {
      if (!this.contract) this.contract = await this.host.api.getJSON('/api/themes/contract');
      this.themes = await this.host.api.getJSON('/api/themes');
      await this._loadAppearance();
      if (!this.selected) this.selected = this.appearance?.theme || 'base';
      await this._loadEditing();
    } catch (e) { console.warn('appearance load failed', e); }
    this._loaded = true;
    this.requestUpdate();
  }

  async _loadAppearance() {
    const q = this.scope ? ('?workspace=' + encodeURIComponent(this.scope)) : '';
    try {
      const r = await this.host.api.getJSON('/api/appearance' + q);
      this.appearance = r.resolved || {};
    } catch (_) { this.appearance = {}; }
  }

  // Resolve the working overrides + inherited base for the selected theme.
  async _loadEditing() {
    const t = this._themeByName(this.selected) || this._themeByName('base');
    if (!t) return;
    this.selected = t.name;
    this.working = { ...(t.tokens || {}) };
    this.baseResolved = t.extends ? await resolveTheme(t.extends) : {};
  }

  _themeByName(n) { return this.themes.find((t) => t.name === n) || null; }

  // ── sidebar ────────────────────────────────────────────────────
  // Kept async for the historical renderSidebar(container) contract; the base
  // mounts our root + paints sidebar() synchronously, the load fills it in.
  async renderSidebar(container) {
    super.renderSidebar(container);
    // Live: refresh the gallery when themes change anywhere (CLI, another tab).
    // Subscribed lazily HERE (not in the constructor) and repainting only the
    // sidebar, so (a) the detail token-editor caret is never disturbed by the
    // 12s heartbeat, and (b) the Settings "Customize" embed — which calls only
    // renderDetail — never opens this subscription (no leak).
    if (!this._themesSub) {
      this._themesSub = live.subscribe('/api/themes', (rows) => {
        if (Array.isArray(rows)) { this.themes = rows; this.requestSidebarUpdate(); }
      });
      this.addCleanup(() => { this._themesSub?.(); this._themesSub = null; });
    }
    if (!this._loaded) await this._load();
  }

  sidebar() {
    const activeName = this.appearance?.theme || 'base';
    return html`
      <div class="side-head">
        <div class="side-title">Themes <span class="count">${this._loaded ? this.themes.length : '—'}</span></div>
        <div style="display:flex;gap:4px">
          <button class="btn icon sm" title="Import theme" data-act="import" @click=${() => this._importTheme()}>${icon('arrow_r', 12)}</button>
          <button class="btn icon sm" title="New theme" data-act="new" @click=${() => this._newTheme()}>${icon('plus', 12)}</button>
        </div>
      </div>
      <div class="side-list ap-list">
        ${!this._loaded
          ? html`<div style="padding:14px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs)">loading…</div>`
          : this.themes.map((t) => this._sideRow(t, activeName))}
      </div>`;
  }

  _swatch(t) {
    const r = t.resolved || {};
    const acc = r.acc || '#67e8f9';
    const bg = r['bg-0'] || '#000';
    const line = r['line-3'] || '#2a303c';
    return html`<span class="ap-swatch" style="background:${esc(bg)};border-color:${esc(line)}">
      <span style="background:${esc(acc)}"></span></span>`;
  }

  _sideRow(t, activeName) {
    return html`
      <div class="srow ap-row ${t.name === this.selected ? 'sel' : ''}"
        style="grid-template-columns:28px 1fr auto" @click=${() => this._select(t.name)}>
        ${this._swatch(t)}
        <div class="info">
          <div class="name truncate">${t.display_name || t.name}</div>
          <div class="sub truncate">${t.builtin ? 'builtin' : 'user'}${t.extends ? html` · ⊃ ${t.extends}` : nothing}</div>
        </div>
        <div class="right">${t.name === activeName ? html`<span class="ap-active" title="Active in this scope">●</span>` : nothing}</div>
      </div>`;
  }

  async _select(name) {
    if (this._previewing) await this._revertPreview();
    this.selected = name;
    await this._loadEditing();
    this.requestUpdate();
  }

  // ── detail ─────────────────────────────────────────────────────
  // Async for the historical renderDetail(container) contract (overlays.js
  // awaits it). The base mounts our root + paints detail() immediately; the
  // load resolves the contract/theme and requestUpdate() repaints.
  async renderDetail(container, opts) {
    super.renderDetail(container, opts);
    if (!this._loaded) await this._load();
  }

  detail() {
    const t = this._themeByName(this.selected) || this._themeByName('base');
    if (this._loaded && !t) return html`<div class="pane-empty"><div class="ttl">No themes</div></div>`;
    if (!t || !this.contract) return html`<div class="pane ap-pane"><div data-tokens><div class="pane-empty"><div class="sub">loading…</div></div></div></div>`;

    const wsList = (this.host.state.workspaces || []).map((w) => w.name);
    const ap = this.appearance || {};
    const activeTheme = ap.theme || 'base';
    const density = ap.density || 'regular';
    const glow = ap.glow || 'on';

    return html`
      <div class="pane ap-pane">
        <div class="ap-bar">
          <div class="ap-bar-grp">
            <label>Scope</label>
            <select data-ap="scope" @change=${this._onScope}>
              <option value="" ?selected=${this.scope === ''}>Global default</option>
              ${wsList.map((w) => html`<option value=${w} ?selected=${this.scope === w}>@${w}</option>`)}
            </select>
          </div>
          <div class="ap-bar-grp">
            <label>Active theme</label>
            <select data-ap="active" @change=${this._onActive}>
              ${this.themes.map((x) => html`<option value=${x.name} ?selected=${activeTheme === x.name}>${x.display_name || x.name}</option>`)}
            </select>
          </div>
          <div class="ap-bar-grp">
            <label>Density</label>
            <div class="ap-seg" data-ap="density">
              ${DENSITIES.map((d) => html`<button data-v=${d} class=${classMap({ on: density === d })}
                @click=${() => this._setAppearance({ density: d })}>${d}</button>`)}
            </div>
          </div>
          <div class="ap-bar-grp">
            <label>Glow</label>
            <div class="ap-seg" data-ap="glow">
              ${GLOWS.map((g) => html`<button data-v=${g} class=${classMap({ on: glow === g })}
                @click=${() => this._setAppearance({ glow: g })}>${g}</button>`)}
            </div>
          </div>
          <div class="ap-bar-note">${this.scope
            ? html`editing @${this.scope} — unset falls back to global`
            : 'global default — workspaces inherit unless overridden'}</div>
        </div>

        <div class="ap-edit">
          <div class="ap-edit-head">
            <div class="ap-edit-title">
              <h2>${t.display_name || t.name} <span class="ap-kind">${t.builtin ? 'builtin' : 'user'}</span></h2>
              <input class="ap-meta-name" data-meta="display_name" placeholder="Display name" .value=${t.display_name || ''}>
              <input class="ap-meta-desc" data-meta="description" placeholder="Description" .value=${t.description || ''}>
              <div class="ap-meta-ext"><label>extends</label>
                <select data-meta="extends" @change=${(e) => this._onExtends(e, t)}>
                  <option value="">— none (over base) —</option>
                  ${this.themes.filter((x) => x.name !== t.name).map((x) =>
                    html`<option value=${x.name} ?selected=${t.extends === x.name}>${x.name}</option>`)}
                </select>
              </div>
            </div>
            <div class="ap-edit-actions">
              <button class="btn primary" data-act="save" @click=${() => this._save(t)}>${icon('bolt', 11)} Save</button>
              <button class="btn" data-act="saveas" @click=${() => this._saveAs(t)}>Save as…</button>
              <button class="btn" data-act="export" @click=${() => this._export(t)}>Export</button>
              ${t.builtin ? nothing : html`<button class="btn icon" data-act="delete" title="Delete theme"
                @click=${() => this._delete(t)}>${icon('delete', 12)}</button>`}
            </div>
          </div>
          ${t.builtin ? html`<div class="ap-builtin-note">Editing a builtin saves a user copy that shadows it. Delete the copy to restore the builtin.</div>` : nothing}
          <div class="ap-tokens" data-tokens>${this.contract.categories.map((cat) => this._category(cat))}</div>
        </div>
      </div>`;
  }

  _category(cat) {
    return html`<div class="ap-cat">
      <div class="ap-cat-h">${cat.name}</div>
      <div class="ap-cat-grid">${cat.tokens.map((tok) => this._tokenRow(tok))}</div>
    </div>`;
  }

  _tokenRow(tok) {
    const own = this.working[tok.name];
    const inherited = this.baseResolved[tok.name] || tok.default;
    const val = own !== undefined ? own : '';
    return html`
      <div class="ap-tok ${own !== undefined ? 'set' : ''}">
        <div class="ap-tok-label" title="--${tok.name}">${tok.label}</div>
        ${tok.type === 'color'
          ? html`<input type="color" class="ap-tok-color" data-tok=${tok.name}
              .value=${this._asHex(own || inherited)} @input=${(e) => this._onColor(e, tok.name)}>`
          : nothing}
        <input class="ap-tok-input" type="text" data-tok-text=${tok.name}
          .value=${val} placeholder=${ifDefined(inherited)} @input=${(e) => this._onText(e, tok.name)}>
        <button class="ap-tok-reset" data-tok-reset=${tok.name} title="Reset to inherited"
          @click=${() => this._onReset(tok.name)}>↺</button>
      </div>`;
  }

  // ── token editing (per-keystroke preview kept imperative; see note) ──
  _onColor(e, name) {
    this.working[name] = e.target.value;
    this._preview();
    this.requestUpdate();   // push the new value into the text field + .set state
  }

  _onText(e, name) {
    const v = e.target.value;
    if (v === '') delete this.working[name];
    else this.working[name] = v;
    this._markSet(e.target, v !== '');
    this._preview();        // repaint dashboard, but NOT this pane — keep the caret
  }

  _onReset(name) {
    delete this.working[name];
    this._preview();
    this.requestUpdate();
  }

  _markSet(el, on) {
    const row = el.closest('.ap-tok');
    if (row) row.classList.toggle('set', on);
  }

  _asHex(v) {
    // <input type=color> only accepts #rrggbb. Pull a leading hex if present;
    // otherwise fall back to black so the swatch renders (text input keeps
    // the real value incl. rgba()).
    const m = String(v || '').match(/#([0-9a-fA-F]{6})/);
    return m ? '#' + m[1] : '#000000';
  }

  // ── live preview ───────────────────────────────────────────────
  _preview() {
    applyTokens({ ...this.baseResolved, ...this.working });
    this._previewing = true;
  }

  async _revertPreview() {
    this._previewing = false;
    await applyAppearance(this.host, this.host.state.workspace);
  }

  // ── appearance control bar ─────────────────────────────────────
  _onScope = async (e) => {
    if (this._previewing) await this._revertPreview();
    this.scope = e.target.value;
    await this._loadAppearance();
    this.requestUpdate();
  };

  _onActive = async (e) => {
    await setAppearance(this.host, { theme: e.target.value }, this.scope);
    await this._loadAppearance();
    this.requestUpdate();
  };

  async _setAppearance(patch) {
    await setAppearance(this.host, patch, this.scope);
    await this._loadAppearance();
    this.requestUpdate();
  }

  async _onExtends(e, t) {
    const tt = this._themeByName(this.selected);
    if (tt) tt.extends = e.target.value || null;
    if (t) t.extends = e.target.value || null;
    this.baseResolved = e.target.value ? await resolveTheme(e.target.value) : {};
    this._preview();
    this.requestUpdate();
  }

  // ── save / export / delete ─────────────────────────────────────
  _collectMeta() {
    const root = this._detailRoot;
    const q = (sel) => root?.querySelector(sel);
    return {
      display_name: q('[data-meta="display_name"]')?.value || '',
      description: q('[data-meta="description"]')?.value || '',
      extends: q('[data-meta="extends"]')?.value || null,
    };
  }

  async _put(name, body) {
    return this.host.api.putJSON('/api/themes/' + encodeURIComponent(name), body);
  }

  async _save(t) {
    const meta = this._collectMeta();
    try {
      await this._put(t.name, { ...meta, tokens: this.working });
      this._previewing = false;
      invalidateThemeCache();
      await this._loadAppearance();
      this.requestUpdate();
      this.host._toast?.(`Saved ${t.name}`);
    } catch (e) { alert('Save failed: ' + e.message); }
  }

  async _saveAs(t) {
    const name = prompt('New theme name:', t.name + '-copy');
    if (!name) return;
    const meta = this._collectMeta();
    try {
      await this._put(name, { ...meta, display_name: meta.display_name || name, tokens: this.working });
      this._previewing = false;
      this.selected = name;
      invalidateThemeCache();
      live.invalidate('/api/themes');
      this.themes = await this.host.api.getJSON('/api/themes');
      await this._loadEditing();
      this.requestUpdate();
    } catch (e) { alert('Save failed: ' + e.message); }
  }

  async _export(t) {
    try {
      const full = await this.host.api.getJSON('/api/themes/' + encodeURIComponent(t.name));
      const body = { name: full.name, display_name: full.display_name, description: full.description,
                     author: full.author, extends: full.extends, tokens: full.tokens };
      const blob = new Blob([JSON.stringify(body, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = `${t.name}.theme.json`; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (e) { alert('Export failed: ' + e.message); }
  }

  async _delete(t) {
    if (!confirm(`Delete theme "${t.name}"? ${this.host.state.theme === t.name ? 'It is currently active — the scope will fall back.' : ''}`)) return;
    try {
      await this.host.api.deleteJSON('/api/themes/' + encodeURIComponent(t.name));
      this._previewing = false;
      this.selected = 'base';
      invalidateThemeCache();
      this.themes = await this.host.api.getJSON('/api/themes');
      await this._loadAppearance();
      await this._loadEditing();
      await applyAppearance(this.host, this.host.state.workspace);
      this.requestUpdate();
    } catch (e) { alert('Delete failed: ' + e.message); }
  }

  async _newTheme() {
    const name = prompt('New theme name:');
    if (!name) return;
    try {
      await this._put(name, { display_name: name, extends: 'base', tokens: {} });
      this.selected = name;
      invalidateThemeCache();
      this.themes = await this.host.api.getJSON('/api/themes');
      await this._loadEditing();
      this.requestUpdate();
    } catch (e) { alert('Create failed: ' + e.message); }
  }

  _importTheme() {
    // Web import is JSON-only (what the lens Export produces). The browser
    // has no YAML parser bundled; a YAML theme from `relaydeck theme export`
    // is imported with `relaydeck theme import` instead.
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = '.json,application/json';
    inp.addEventListener('change', async () => {
      const file = inp.files?.[0];
      if (!file) return;
      try {
        const text = await file.text();
        let data;
        try { data = JSON.parse(text); }
        catch (_) { alert('Import expects a JSON theme (export from here, or use `relaydeck theme import` for YAML).'); return; }
        const name = data.name || file.name.replace(/\.(theme\.)?json$/i, '');
        await this._put(name, data);  // server validates tokens + extends
        this.selected = name;
        invalidateThemeCache();
        this.themes = await this.host.api.getJSON('/api/themes');
        await this._loadEditing();
        this.requestUpdate();
      } catch (e) { alert('Import failed: ' + e.message); }
    });
    inp.click();
  }

  onAppearanceChanged() {
    this._loadAppearance().then(() => {
      // Don't yank the token-editor caret if an external change lands mid-edit.
      if (this._previewing) this.requestSidebarUpdate(); else this.requestUpdate();
    });
  }

  unmount() {
    // If the user navigated away mid-edit, restore the saved appearance.
    if (this._previewing) applyAppearance(this.host, this.host.state.workspace).catch(() => {});
    super.unmount();   // releases the /api/themes subscription + cleanups
  }
}
