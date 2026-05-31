// tiles/identity.js — the agent's identity + how its system prompt is built.
//
// Two columns:
//   LEFT  · SPAWN COMPOSITION — the real components that build the system
//           prompt (identity preamble, operator system_prompt, each injected
//           skill, fleet context) with exact chars + ~chars/4 token estimate,
//           from GET /api/agents/{id}/prompt-composition (no fabrication).
//         · SYSTEM_PROMPT — the operator's YAML system_prompt, line-numbered,
//           with char/token counts, copy, edit (PATCH /api/agents/{id}).
//   RIGHT · IDENTITY · YAML — id/type/workspace/model/purpose/tags/preamble/
//           auto-start, inline-editable.
//         · ACTIVATED PLUGINS — workspace-scoped plugins + harness gates with
//           live toggles (PATCH /api/workspaces/{ws}) + per-agent contribution
//           badges derived from the composition.
//         · PEERS — agents visible in this agent's preamble (auto-discovered).
//
// MIGRATION NOTE (RelayElement + defineTile). The whole surface is reactive
// lit-html now: edit-mode (identity YAML / system_prompt) and the prompt
// show-all toggle are reactive props, so flipping them re-renders via Lit's
// diff (no innerHTML rebuild, no querySelector wiring, no addEventListener).
// Data is still loaded imperatively against the API (no single live resource
// to subscribe to) into reactive instance fields, then requestUpdate(). The
// one-time bespoke <style> injection is kept (light DOM, idempotent). The
// preserved classes/attrs the app + e2e rely on are rendered verbatim:
//   .idn, .idn-comp, .idn-cmp, .idn-cmp-label, .idn-cmp-tok, .idn-cmp-plug,
//   .idn-toggle, .idn-knob, .idn-sp, .idn-sp-code, .idn-peer[data-peer].

import { RelayElement, defineTile, html, nothing, icon, chip } from '@relaydeck/ui';

class IdentityTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    // reactive view/edit state — flipping any of these re-renders.
    editing: { state: true },        // identity-yaml edit mode
    promptEditing: { state: true },  // system_prompt edit mode
    showAllPrompt: { state: true },
    full: { state: true },
    comp: { state: true },
    catalog: { state: true },
    wsPlugins: { state: true },
    _copied: { state: true },
    _spErr: { state: true },
    _idErr: { state: true },
    _plugErr: { state: true },
  };

  constructor() {
    super();
    this.agent = {};
    this.api = null;
    this.editing = false;
    this.promptEditing = false;
    this.showAllPrompt = false;
    this.draft = {};            // edit buffer (not reactive — inputs are uncontrolled)
    this.full = null;
    this.comp = null;
    this.catalog = [];
    this.wsPlugins = [];
    this._copied = false;
    this._spErr = '';
    this._idErr = '';
    this._plugErr = '';
    this._copyTimer = null;
    this._loaded = false;
  }

  connectedCallback() {
    super.connectedCallback();
    _injectCSS();
    if (this.agent?.id) this._loadAll();
  }

  willUpdate(changed) {
    // (Re)load when the agent identity arrives or changes.
    if (changed.has('agent') && this.agent?.id && this.agent.id !== this._loadedId) {
      this._loadAll();
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._copyTimer) { clearTimeout(this._copyTimer); this._copyTimer = null; }
  }

  async _loadAll() {
    const id = encodeURIComponent(this.agent.id);
    this._loadedId = this.agent.id;
    const [full, comp, wsList, catalog] = await Promise.all([
      this.api.getJSON(`/api/agents/${id}`).catch(() => this.agent),
      this.api.getJSON(`/api/agents/${id}/prompt-composition`).catch(() => null),
      this.api.getJSON('/api/workspaces').catch(() => []),
      this.api.getJSON('/api/workspace-plugins').catch(() => []),
    ]);
    this.full = full || this.agent;
    this.comp = comp;
    this.catalog = catalog || [];
    const ws = (wsList || []).find((w) => w.name === this.full.workspace);
    this.wsPlugins = ws ? (ws.plugins || []) : [];
    this._loaded = true;
    this.requestUpdate();
  }

  // ── render ──────────────────────────────────────────────────────
  render() {
    if (!this._loaded || !this.full) {
      return html`<div class="idn"><div class="idn-loading">loading…</div></div>`;
    }
    return html`
      <div class="idn">
        <div class="idn-col idn-left">
          ${this._composition()}
          ${this._promptViewer()}
        </div>
        <div class="idn-col idn-right">
          ${this._identityYaml()}
          ${this._plugins()}
          ${this._peers()}
        </div>
      </div>`;
  }

  // LEFT — spawn composition ----------------------------------------
  _composition() {
    const c = this.comp;
    if (!c) return html`<div class="block idn-blank">composition unavailable</div>`;
    const total = c.total_est_tokens || 0;
    const rows = (c.components || []).map((comp) => html`
      <div class="idn-cmp" data-cmp=${comp.index}>
        <div class="idn-cmp-n">${String(comp.index).padStart(2, '0')}</div>
        <div class="idn-cmp-label">${comp.label}</div>
        <div class="idn-cmp-src">${comp.source}</div>
        <div class="idn-cmp-tok">${this._fmtTok(comp.est_tokens)} <span class="dim">t</span></div>
        <div class="idn-cmp-plug"><span class="ptag">${comp.source_plugin}</span></div>
      </div>`);
    return html`
      <div class="block idn-comp">
        <div class="block-head">
          <span class="eyebrow">spawn composition · how ${this.agent.id}'s system prompt is built</span>
          <span class="idn-total">${this._fmtTok(total)} tokens total</span>
        </div>
        <div class="idn-cmp-rows">${(c.components || []).length
          ? rows : html`<div class="idn-blank">nothing injected</div>`}</div>
        <div class="idn-comp-foot dim">${(c.total_chars || 0).toLocaleString()} chars · token counts are ~estimates (chars/${c.chars_per_token || 4})</div>
      </div>`;
  }

  // LEFT — system_prompt viewer -------------------------------------
  _promptViewer() {
    const a = this.full;
    const sp = a.system_prompt || '';
    const lines = sp ? sp.split('\n') : [];
    const tok = Math.round((sp.length || 0) / (this.comp?.chars_per_token || 4));
    if (this.promptEditing) {
      return html`
        <div class="block idn-sp">
          <div class="block-head">
            <span class="eyebrow">system_prompt · yaml · agents/${this.agent.id}.yaml</span>
            <span class="idn-sp-actions">
              <button class="btn sm" data-act="sp-cancel"
                @click=${this._cancelPrompt}>cancel</button>
              <button class="btn sm primary" data-act="sp-save"
                @click=${this._savePrompt}>save</button>
            </span>
          </div>
          <textarea class="idn-sp-edit" data-f="system_prompt" spellcheck="false"
            placeholder="(optional) free-form system prompt appended after the identity preamble"
            @input=${(e) => { this.draft.system_prompt = e.target.value; }}
          >${this.draft.system_prompt ?? sp}</textarea>
          ${this._spErr ? html`<div class="idn-sp-err" data-sp-err style="display:block">${this._spErr}</div>` : nothing}
        </div>`;
    }
    const shown = (this.showAllPrompt || lines.length <= 18) ? lines : lines.slice(0, 18);
    // empty lines render a literal nbsp so the .lc (white-space:pre-wrap) keeps height.
    const body = sp
      ? html`<div class="idn-sp-code">${shown.map((l, i) => html`
          <div class="idn-sp-line"><span class="ln">${i + 1}</span><span class="lc">${l || ' '}</span></div>`)}</div>`
      : html`<div class="idn-blank">— no custom system prompt —
          <button class="btn sm ghost" data-act="sp-edit" style="margin-left:8px"
            @click=${this._editPrompt}>${icon('edit', 11)} add one</button></div>`;
    return html`
      <div class="block idn-sp">
        <div class="block-head">
          <span class="eyebrow">system_prompt · yaml · agents/${this.agent.id}.yaml</span>
          ${sp ? html`<span class="idn-sp-actions">
            <span class="idn-mini">${sp.length.toLocaleString()} chars</span>
            <span class="idn-mini">~${this._fmtTok(tok)} tokens</span>
            <button class="btn sm ghost" data-act="sp-edit" @click=${this._editPrompt}>${icon('edit', 11)} edit</button>
            <button class="btn sm ghost" data-act="sp-copy" @click=${this._copyPrompt}>${this._copied ? 'copied' : 'copy'}</button>
          </span>` : nothing}
        </div>
        ${body}
        ${sp && lines.length > 18 ? html`<button class="idn-showall" data-act="sp-toggle"
          @click=${() => { this.showAllPrompt = !this.showAllPrompt; }}>${
          this.showAllPrompt ? 'show less' : `show all · ${lines.length} lines`}</button>` : nothing}
      </div>`;
  }

  // RIGHT — identity yaml -------------------------------------------
  _identityYaml() {
    const a = this.full;
    const tags = Array.isArray(a.tags) ? a.tags : [];
    const ed = this.editing;
    const row = (k, v) => html`<div class="idn-kv"><span class="idn-k">${k}</span><span class="idn-v">${v}</span></div>`;
    const model = a.config?.model
      ? html`<span class="chip accent">${a.config.model}</span>`
      : html`<span class="dim">inherits preset</span>`;
    const preamble = a.inject_identity_preamble === false
      ? html`<span class="chip muted">off</span>`
      : html`<span class="idn-on">on · auto-built</span>`;
    return html`
      <div class="block idn-yaml">
        <div class="block-head">
          <span class="eyebrow">identity · yaml · agents/${this.agent.id}.yaml</span>
          ${ed
            ? html`<span class="idn-sp-actions">
                <button class="btn sm" data-act="id-cancel" @click=${this._cancelIdentity}>cancel</button>
                <button class="btn sm primary" data-act="id-save" @click=${this._saveIdentity}>save</button>
              </span>`
            : html`<button class="btn sm ghost" data-act="id-edit" @click=${this._editIdentity}>edit</button>`}
        </div>
        ${row('id', html`<span class="idn-mono">${a.id}</span>`)}
        ${row('type', html`<span class="idn-mono">${a.type || ''}</span>`)}
        ${row('workspace', html`<span class="mono-pill">@${a.workspace || ''}</span>`)}
        ${row('model', model)}
        ${row('purpose', ed
          ? html`<textarea class="idn-inp" data-f="purpose" rows="2"
              @input=${(e) => { this.draft.purpose = e.target.value; }}
            >${this.draft.purpose ?? (a.purpose || '')}</textarea>`
          : (a.purpose ? a.purpose : html`<span class="dim">— none —</span>`))}
        ${row('tags', ed
          ? html`<input class="idn-inp" data-f="tags" type="text" placeholder="comma-separated"
              .value=${(this.draft.tags ?? tags).join(', ')}
              @input=${(e) => { this.draft.tags = e.target.value.split(',').map((s) => s.trim()).filter(Boolean); }}>`
          : (tags.length ? tags.map((t) => chip(t)) : html`<span class="dim">— none —</span>`))}
        ${row('preamble', ed
          ? html`<label class="idn-check"><input type="checkbox" data-f="inject_identity_preamble"
              ?checked=${this.draft.inject_identity_preamble ?? (a.inject_identity_preamble !== false)}
              @change=${(e) => { this.draft.inject_identity_preamble = e.target.checked; }}> inject at spawn</label>`
          : preamble)}
        ${row('auto-start', a.auto_start ? html`<span class="idn-on">on</span>` : html`<span class="dim">off</span>`)}
        ${this._idErr ? html`<div class="idn-sp-err" data-id-err style="display:block">${this._idErr}</div>` : nothing}
      </div>`;
  }

  // RIGHT — activated plugins ---------------------------------------
  _plugins() {
    const contrib = new Map(); // plugin -> [badges]
    for (const c of (this.comp?.components || [])) {
      const p = c.source_plugin;
      if (p === 'core') continue;
      if (!contrib.has(p)) contrib.set(p, []);
      contrib.get(p).push(c.label.startsWith('skill') ? `skill: ${c.label.replace('skill · ', '')}` : c.label);
    }
    const active = this.wsPlugins;
    const cat = this.catalog;
    const wsScoped = cat.filter((e) => e.kind === 'plugin');
    const gates = cat.filter((e) => e.kind === 'harness-gate');
    const activeCount = cat.filter((e) => active.includes(e.name)).length;

    const pluginRow = (e) => {
      const on = active.includes(e.name);
      const badges = (contrib.get(e.name) || []).map((b) => html`<span class="ptag sm">${b}</span>`);
      return html`
        <div class="idn-plug ${on ? 'on' : ''}">
          <span class="idn-plug-dot ${on ? 'on' : ''}"></span>
          <div class="idn-plug-main">
            <div class="idn-plug-name">${e.name} <span class="dim">· ${e.category || e.kind}</span></div>
            ${badges.length ? html`<div class="idn-plug-badges">${badges}</div>` : nothing}
          </div>
          <button class="idn-toggle ${on ? 'on' : ''}" data-toggle=${e.name} role="switch"
            aria-checked=${on} @click=${() => this._togglePlugin(e.name)}><span class="idn-knob"></span></button>
        </div>`;
    };
    const group = (label, entries) => entries.length
      ? html`<div class="idn-plug-group">${label}</div>${entries.map(pluginRow)}` : nothing;

    return html`
      <div class="block idn-plugins">
        <div class="block-head">
          <span class="eyebrow">activated plugins · contributing to ${this.agent.id}</span>
          <span class="ptag">${activeCount} active</span>
        </div>
        ${group('workspace', wsScoped)}
        ${group('harness gates', gates)}
        ${this._plugErr ? html`<div class="idn-sp-err" data-plug-err style="display:block">${this._plugErr}</div>` : nothing}
      </div>`;
  }

  // RIGHT — peers ---------------------------------------------------
  _peers() {
    const peers = this.comp?.peers || [];
    const off = this.comp && this.comp.inject_preamble === false;
    const rows = peers.length
      ? peers.map((p) => html`
        <div class="idn-peer" data-peer=${p.id} @click=${() => this._gotoPeer(p.id)}>
          <span class="idn-peer-av">${(p.id || '?')[0].toUpperCase()}</span>
          <span class="idn-peer-id">@${p.id}</span>
          <span class="idn-peer-purpose dim">${p.purpose || '— no purpose —'}</span>
        </div>`)
      : html`<div class="idn-blank">no peers in @${this.full.workspace || ''}</div>`;
    return html`
      <div class="block idn-peers">
        <div class="block-head">
          <span class="eyebrow">peers · visible in preamble · auto-discovered from db</span>
          ${off ? html`<span class="chip muted">preamble off</span>` : nothing}
        </div>
        ${rows}
      </div>`;
  }

  // ── actions ─────────────────────────────────────────────────────
  _editPrompt = () => { this.promptEditing = true; this.draft.system_prompt = this.full.system_prompt || ''; this._spErr = ''; };
  _cancelPrompt = () => { this.promptEditing = false; delete this.draft.system_prompt; this._spErr = ''; };
  _editIdentity = () => { this.editing = true; this.draft = {}; this._idErr = ''; };
  _cancelIdentity = () => { this.editing = false; this.draft = {}; this._idErr = ''; };

  _copyPrompt = () => {
    navigator.clipboard?.writeText(this.full.system_prompt || '');
    this._copied = true;
    if (this._copyTimer) clearTimeout(this._copyTimer);
    this._copyTimer = setTimeout(() => { this._copied = false; this._copyTimer = null; }, 1200);
  };

  _gotoPeer = (id) => {
    try { window.location.search = `?agent=${encodeURIComponent(id)}`; } catch (_) {}
  };

  _savePrompt = async () => {
    try {
      await this._patch({ system_prompt: this.draft.system_prompt ?? '' });
      this.promptEditing = false; delete this.draft.system_prompt; this._spErr = '';
      await this._loadAll();
    } catch (e) { this._spErr = 'save failed: ' + e.message; }
  };

  _saveIdentity = async () => {
    const body = {};
    if ('purpose' in this.draft) body.purpose = this.draft.purpose;
    if ('tags' in this.draft) body.tags = this.draft.tags;
    if ('inject_identity_preamble' in this.draft) body.inject_identity_preamble = this.draft.inject_identity_preamble;
    if (!Object.keys(body).length) { this.editing = false; return; }
    try {
      await this._patch(body);
      this.editing = false; this.draft = {}; this._idErr = '';
      await this._loadAll();
    } catch (e) { this._idErr = 'save failed: ' + e.message; }
  };

  async _togglePlugin(name) {
    const ws = this.full.workspace;
    if (!ws) return;
    const cur = new Set(this.wsPlugins);
    if (cur.has(name)) cur.delete(name); else cur.add(name);
    const next = [...cur];
    // optimistic — reactive prop assignment re-renders.
    this.wsPlugins = next;
    try {
      const r = await this.api.fetch(`/api/workspaces/${encodeURIComponent(ws)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plugins: next }),
      });
      if (!r.ok) throw new Error(await r.text().catch(() => `HTTP ${r.status}`));
      // composition may change (skills gate etc.) — refresh.
      await this._loadAll();
    } catch (e) {
      this._plugErr = 'toggle failed: ' + e.message;
      await this._loadAll();
    }
  }

  async _patch(body) {
    const r = await this.api.fetch(`/api/agents/${encodeURIComponent(this.agent.id)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text().catch(() => `HTTP ${r.status}`));
  }

  _fmtTok(n) {
    n = n || 0;
    return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);
  }
}

// One-time, idempotent style injection (light DOM — global tokens apply).
function _injectCSS() {
  if (document.getElementById('idn-css')) return;
  const s = document.createElement('style');
  s.id = 'idn-css';
  s.textContent = `
.idn { display:grid; grid-template-columns: minmax(0,1.35fr) minmax(0,1fr); gap:14px; padding:14px; height:100%; overflow:auto; align-content:start; }
@media (max-width: 1080px) { .idn { grid-template-columns: 1fr; } }
.idn-col { display:flex; flex-direction:column; gap:14px; min-width:0; }
.idn-loading { padding:24px; color:var(--t-3); font-family:var(--f-mono); font-size:var(--t-xs); }
.idn .block { background:var(--bg-1); border:1px solid var(--line-2); border-radius:var(--r-1); padding:14px 16px; }
.idn .block-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:12px; }
.idn .eyebrow { font-family:var(--f-mono); font-size:var(--t-xxs); letter-spacing:.13em; text-transform:uppercase; color:var(--t-3); }
.idn-blank { color:var(--t-3); font-family:var(--f-mono); font-size:var(--t-xs); padding:6px 0; }
.idn-mono { font-family:var(--f-mono); font-size:var(--t-sm); color:var(--t-1); }
.idn-mini { font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-4); }
.idn-on { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--ok); }
.ptag { font-family:var(--f-mono); font-size:var(--t-xxs); letter-spacing:.04em; color:var(--t-2); background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-0); padding:2px 7px; white-space:nowrap; }
.ptag.sm { font-size:9px; padding:1px 5px; }

/* composition */
.idn-total { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-1); background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-0); padding:3px 9px; }
.idn-cmp-rows { display:flex; flex-direction:column; }
.idn-cmp { display:grid; grid-template-columns: 28px 1.6fr 1.8fr auto auto; gap:12px; align-items:center; padding:9px 0; border-top:1px solid var(--line-1); }
.idn-cmp:first-child { border-top:0; }
.idn-cmp-n { font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-4); }
.idn-cmp-label { font-family:var(--f-mono); font-size:var(--t-sm); color:var(--t-1); min-width:0; overflow:hidden; text-overflow:ellipsis; }
.idn-cmp-src { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-3); min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.idn-cmp-tok { font-family:var(--f-mono); font-size:var(--t-sm); color:var(--t-1); text-align:right; font-feature-settings:"tnum" 1; }
.idn-cmp-plug { justify-self:end; }
.idn-comp-foot { font-family:var(--f-mono); font-size:var(--t-xxs); margin-top:10px; padding-top:10px; border-top:1px dashed var(--line-1); }

/* system_prompt */
.idn-sp-actions { display:flex; align-items:center; gap:8px; }
.idn-sp-code { background:var(--bg-0); border:1px solid var(--line-2); border-radius:var(--r-1); overflow:auto; max-height:520px; padding:8px 0; }
.idn-sp-line { display:flex; gap:14px; padding:0 14px; line-height:1.55; }
.idn-sp-line .ln { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-4); text-align:right; width:24px; flex-shrink:0; user-select:none; }
.idn-sp-line .lc { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-1); white-space:pre-wrap; word-break:break-word; }
.idn-sp-edit { width:100%; min-height:240px; background:var(--bg-0); border:1px solid var(--line-2); border-radius:var(--r-1); padding:10px 12px; color:var(--t-1); font-family:var(--f-mono); font-size:var(--t-xs); line-height:1.55; resize:vertical; }
.idn-showall { display:block; width:100%; margin-top:8px; background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-0); color:var(--t-2); font-family:var(--f-mono); font-size:var(--t-xxs); padding:6px; cursor:pointer; }
.idn-showall:hover { color:var(--t-1); border-color:var(--line-3); }

/* identity yaml */
.idn-kv { display:grid; grid-template-columns: 96px 1fr; gap:12px; align-items:start; padding:7px 0; border-top:1px solid var(--line-1); }
.idn-kv:first-of-type { border-top:0; }
.idn-k { font-family:var(--f-mono); font-size:var(--t-xxs); letter-spacing:.1em; text-transform:uppercase; color:var(--t-4); padding-top:2px; }
.idn-v { font-family:var(--f-sans); font-size:var(--t-sm); color:var(--t-1); min-width:0; word-break:break-word; }
.idn-inp { width:100%; background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-1); padding:5px 9px; color:var(--t-1); font-family:var(--f-mono); font-size:var(--t-xs); resize:vertical; }
.idn-check { display:inline-flex; align-items:center; gap:6px; font-size:var(--t-xs); color:var(--t-2); cursor:pointer; }

/* plugins */
.idn-plug-group { font-family:var(--f-mono); font-size:9px; letter-spacing:.12em; text-transform:uppercase; color:var(--t-4); margin:10px 0 4px; }
.idn-plug-group:first-of-type { margin-top:0; }
.idn-plug { display:flex; align-items:center; gap:10px; padding:7px 0; }
.idn-plug-dot { width:6px; height:6px; border-radius:50%; background:var(--t-4); flex-shrink:0; }
.idn-plug-dot.on { background:var(--ok); }
.idn-plug-main { flex:1; min-width:0; }
.idn-plug-name { font-family:var(--f-mono); font-size:var(--t-sm); color:var(--t-1); }
.idn-plug.on .idn-plug-name { color:var(--t-1); }
.idn-plug:not(.on) .idn-plug-name { color:var(--t-3); }
.idn-plug-badges { display:flex; flex-wrap:wrap; gap:4px; margin-top:3px; }
.idn-toggle { width:34px; height:18px; border-radius:9px; background:var(--bg-2); border:1px solid var(--line-3); position:relative; cursor:pointer; flex-shrink:0; transition:background .12s; }
.idn-toggle.on { background:var(--acc); border-color:var(--acc); }
.idn-knob { position:absolute; top:1px; left:1px; width:14px; height:14px; border-radius:50%; background:var(--bg-1); transition:left .12s; }
.idn-toggle.on .idn-knob { left:17px; background:#fff; }

/* peers */
.idn-peer { display:flex; align-items:center; gap:10px; padding:7px 0; cursor:pointer; border-top:1px solid var(--line-1); }
.idn-peer:first-of-type { border-top:0; }
.idn-peer:hover { background:var(--bg-2); margin:0 -8px; padding:7px 8px; border-radius:var(--r-0); }
.idn-peer-av { width:22px; height:22px; border-radius:var(--r-0); background:var(--acc-soft); color:var(--acc); display:flex; align-items:center; justify-content:center; font-family:var(--f-mono); font-size:10px; flex-shrink:0; }
.idn-peer-id { font-family:var(--f-mono); font-size:var(--t-sm); color:var(--t-1); }
.idn-peer-purpose { font-family:var(--f-sans); font-size:var(--t-xs); min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.idn-sp-err { display:none; color:var(--err); font-family:var(--f-mono); font-size:var(--t-xs); margin-top:8px; }
`;
  document.head.appendChild(s);
}

if (!customElements.get('rd-tile-identity')) customElements.define('rd-tile-identity', IdentityTile);

export default defineTile('rd-tile-identity', (el, { api, ctx }) => {
  el.agent = ctx.agent;
  el.api = api;
});
