// tiles/context.js — the agent's context window, at a glance.
//
// Two columns. Left: the context-window meter (limit from models.dev) + the
// effective-context preview (what the model sees — the composed system-prompt
// components). Right: the composition broken into contributing layers with
// token counts + %. Data from GET /api/agents/{id}/context.
//
// REFERENCE-STYLE (read-only, single live resource): RelayElement (light DOM)
// + LiveController (auto-unsubscribe) + defineTile() for the mount contract.

import {
  RelayElement, defineTile, LiveController, html, nothing, fmtNum,
} from '@relaydeck/ui';

// Layer key → accent. Muted/neutral for "free".
const LAYER_COLOR = {
  system: 'var(--acc)',
  memory: '#c98a3a',
  skills: '#5a9e6f',
  convo:  '#8a6fc9',
  free:   'var(--line-2)',
};

class ContextTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    _showAll: { state: true },
    _raw: { state: true },
  };

  constructor() {
    super();
    this.agent = {};
    this._showAll = false;
    this._raw = false;
    this._ctx = new LiveController(this);
  }

  connectedCallback() {
    super.connectedCallback();
    this._injectCSS();
  }

  willUpdate(changed) {
    if (changed.has('agent') && this.agent?.id) {
      this._ctx.setKey(`/api/agents/${encodeURIComponent(this.agent.id)}/context`);
    }
  }

  _effectiveText(components) {
    return (components || []).map((c) => `<${c.label}>\n${c.text || ''}`).join('\n\n');
  }

  _export(components) {
    const blob = new Blob([this._effectiveText(components)], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${this.agent?.id || 'agent'}-context.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // A share that never reads as a flat "0%" for a real-but-tiny value.
  _pct(p) {
    if (p >= 1) return `${Math.round(p)}%`;
    if (p > 0) return p >= 0.1 ? `${p.toFixed(1)}%` : '<0.1%';
    return '0%';
  }

  // ── left: context-window meter ────────────────────────────────────
  _windowCard(d) {
    const window = d.window;
    const used = d.used || 0;
    const usedPct = window ? (used / window) * 100 : null;
    // Stacked segments for the non-free layers, then free fills the rest.
    const denom = window || Math.max(1, used);
    const segs = (d.layers || [])
      .filter((l) => l.key !== 'free' && l.tokens > 0)
      .map((l) => html`<span class="cx-seg-fill" style="width:${(l.tokens / denom) * 100}%;background:${LAYER_COLOR[l.key]}" title="${l.label}: ${fmtNum(l.tokens)}"></span>`);
    return html`
      <div class="cx-card">
        <div class="cx-head">
          <div class="cx-eyebrow">context window ${d.model ? html`· <span class="acc">${d.model}</span>` : nothing}</div>
          <div class="cx-chips">
            <span class="cx-chip">used ${fmtNum(used)}</span>
            ${window ? html`<span class="cx-chip">free ${fmtNum(d.free)}</span>` : nothing}
            ${usedPct != null ? html`<span class="cx-chip ${usedPct >= 80 ? 'warn' : 'acc'}">${this._pct(usedPct)}</span>` : nothing}
          </div>
        </div>
        ${window
          ? html`<div class="cx-sub">${fmtNum(window)} token window</div>`
          : html`<div class="cx-sub dim">model not catalogued — window unknown, showing used only</div>`}
        <div class="cx-meter">${segs}</div>
        ${window ? html`
          <div class="cx-axis">
            <span>0</span><span>${fmtNum(window * 0.25)}</span><span>${fmtNum(window * 0.5)}</span>
            <span>${fmtNum(window * 0.75)}</span><span>${fmtNum(window)}</span>
          </div>` : nothing}
      </div>`;
  }

  // One preview section. For skills, the frontmatter (always in context) is
  // shown highlighted as "loaded" and the body — read on demand when the agent
  // invokes the skill — is dimmed, so it's clear what actually costs context.
  _block(c) {
    const isSkill = String(c.label || '').startsWith('skill · ');
    const text = c.text || '';
    const cap = (s) => (!this._showAll && s.length > 600 ? s.slice(0, 600) + '…' : s);
    if (isSkill && c.meta_chars != null) {
      const meta = text.slice(0, c.meta_chars);
      const body = text.slice(c.meta_chars).replace(/^\n+/, '');
      return html`
        <div class="cx-block">
          <div class="cx-tag">&lt;${c.label}&gt;
            <span class="dim">${fmtNum(c.loaded_tokens)} tok loaded · ${fmtNum(c.ondemand_tokens)} on demand</span></div>
          <pre class="cx-body cx-loaded">${cap(meta)}</pre>
          ${body ? html`<div class="cx-ondemand">
            <span class="cx-od-tag">body · loaded when the agent invokes this skill</span>
            <pre class="cx-body">${cap(body)}</pre>
          </div>` : nothing}
        </div>`;
    }
    return html`
      <div class="cx-block">
        <div class="cx-tag">&lt;${c.label}&gt; <span class="dim">${fmtNum(c.loaded_tokens ?? c.est_tokens)} tok</span></div>
        <pre class="cx-body cx-loaded">${cap(text)}</pre>
      </div>`;
  }

  // ── left: effective-context preview ───────────────────────────────
  _previewCard(d) {
    const comps = d.components || [];
    const full = this._effectiveText(comps);
    return html`
      <div class="cx-card cx-grow">
        <div class="cx-head">
          <div class="cx-eyebrow">effective context · what the model sees</div>
          <div class="cx-btns">
            <button class="cx-btn ${this._raw ? 'on' : ''}" @click=${() => { this._raw = !this._raw; }}>raw</button>
            <button class="cx-btn" @click=${() => this._export(comps)}>export</button>
          </div>
        </div>
        ${!comps.length
          ? html`<div class="cx-empty">No composed system prompt for this agent.</div>`
          : this._raw
            ? html`<pre class="cx-raw">${full}</pre>`
            : html`<div class="cx-preview ${this._showAll ? 'open' : ''}">
                ${comps.map((c) => this._block(c))}
              </div>`}
        ${comps.length && !this._raw ? html`
          <button class="cx-showall" @click=${() => { this._showAll = !this._showAll; }}>
            ${this._showAll ? 'collapse' : `show all · ${comps.length} section${comps.length === 1 ? '' : 's'}`}
          </button>` : nothing}
      </div>`;
  }

  // ── right: composition layers ─────────────────────────────────────
  _compositionCard(d) {
    const layers = d.layers || [];
    const total = d.window || (d.used || 0);
    const rows = layers.map((l) => html`
      <div class="cx-lrow">
        <span class="cx-dot" style="background:${LAYER_COLOR[l.key]}"></span>
        <span class="cx-lname"><span class="cx-lt">${l.label}</span><span class="cx-lsub">${l.sub}</span></span>
        <span class="cx-ltok">${fmtNum(l.tokens)}</span>
        <span class="cx-lpct">${this._pct(l.pct)}</span>
      </div>`);
    return html`
      <div class="cx-card">
        <div class="cx-head">
          <div class="cx-eyebrow">composition · contributing layers</div>
          <span class="cx-chip">${layers.length} layers</span>
        </div>
        <div class="cx-lhead">
          <span></span><span></span>
          <span class="cx-lhc">tokens</span>
          <span class="cx-lhc">share</span>
        </div>
        <div class="cx-layers">${rows}</div>
        <div class="cx-total">
          <span></span>
          <span class="cx-lname"><span class="cx-lt">Total · context window</span></span>
          <span class="cx-ltok">${fmtNum(total)}</span>
          <span class="cx-lpct">100%</span>
        </div>
      </div>`;
  }

  render() {
    const d = this._ctx.value;
    if (!this._ctx.key || d === undefined) {
      return html`<div class="cx"><div class="cx-loading">loading…</div></div>`;
    }
    if (!d) {
      return html`<div class="cx"><div class="cx-loading">context unavailable</div></div>`;
    }
    return html`
      <div class="cx">
        <div class="cx-grid">
          <div class="cx-col">
            ${this._windowCard(d)}
            ${this._previewCard(d)}
          </div>
          <div class="cx-col">
            ${this._compositionCard(d)}
          </div>
        </div>
      </div>`;
  }

  _injectCSS() {
    if (document.getElementById('cx-css')) return;
    const s = document.createElement('style');
    s.id = 'cx-css';
    s.textContent = `
.cx { padding:14px; height:100%; overflow:auto; }
.cx-loading,.cx-empty { padding:20px; color:var(--t-3); font-family:var(--f-mono); font-size:var(--t-xs); }
.cx-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; align-items:start; }
@media (max-width:880px){ .cx-grid { grid-template-columns:1fr; } }
.cx-col { display:flex; flex-direction:column; gap:14px; min-width:0; }
.cx-card { background:var(--bg-1); border:1px solid var(--line-2); border-radius:var(--r-1); padding:13px 15px; min-width:0; }
.cx-grow { display:flex; flex-direction:column; }
.cx-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:11px; }
.cx-eyebrow { font-family:var(--f-mono); font-size:var(--t-xxs); letter-spacing:.12em; text-transform:uppercase; color:var(--t-3); min-width:0; }
.cx-eyebrow .acc { color:var(--acc); }
.cx-sub { font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-4); margin:-4px 0 10px; }
.cx-chips,.cx-btns { display:flex; gap:6px; flex-shrink:0; }
.cx-chip { font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-2); background:var(--bg-2); border:1px solid var(--line-2); border-radius:999px; padding:2px 9px; font-feature-settings:"tnum" 1; }
.cx-chip.acc { color:var(--acc); border-color:var(--acc-line); }
.cx-chip.warn { color:var(--warn); border-color:var(--warn); }
.cx-btn { font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-3); background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-0); padding:2px 8px; cursor:pointer; }
.cx-btn:hover { color:var(--t-1); }
.cx-btn.on { color:var(--acc); border-color:var(--acc-line); background:var(--acc-soft); }
/* meter */
.cx-meter { display:flex; height:34px; background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-0); overflow:hidden; }
.cx-seg-fill { display:block; height:100%; min-width:1px; }
.cx-axis { display:flex; justify-content:space-between; margin-top:5px; font-family:var(--f-mono); font-size:9px; color:var(--t-4); }
/* layers */
.cx-lhead { display:grid; grid-template-columns:10px 1fr auto auto; gap:10px; padding-bottom:4px; }
.cx-lhc { font-family:var(--f-mono); font-size:9px; letter-spacing:.1em; text-transform:uppercase; color:var(--t-4); text-align:right; }
.cx-lhc:nth-child(4) { min-width:38px; }
.cx-layers { display:flex; flex-direction:column; }
.cx-lrow,.cx-total { display:grid; grid-template-columns:10px 1fr auto auto; align-items:center; gap:10px; padding:8px 0; border-top:1px solid var(--line-1); }
.cx-lrow:first-child { border-top:0; }
.cx-total { border-top:1px solid var(--line-2); margin-top:4px; }
.cx-dot { width:8px; height:8px; border-radius:2px; }
.cx-lname { display:flex; flex-direction:column; min-width:0; }
.cx-lt { font-family:var(--f-sans,inherit); font-size:var(--t-sm); color:var(--t-1); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cx-lsub { font-family:var(--f-mono); font-size:9px; color:var(--t-4); }
.cx-ltok { font-family:var(--f-mono); font-size:var(--t-sm); color:var(--t-1); text-align:right; font-feature-settings:"tnum" 1; }
.cx-total .cx-ltok { font-weight:600; }
.cx-lpct { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-3); text-align:right; min-width:38px; font-feature-settings:"tnum" 1; }
/* preview */
.cx-preview { display:flex; flex-direction:column; gap:9px; max-height:340px; overflow:auto; }
.cx-preview.open { max-height:none; }
.cx-block { border-left:2px solid var(--line-2); padding-left:10px; }
.cx-tag { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--acc); margin-bottom:3px; }
.cx-tag .dim { color:var(--t-4); }
.cx-body { margin:0; white-space:pre-wrap; word-break:break-word; font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-2); line-height:1.5; }
.cx-loaded { color:var(--t-1); }
.cx-ondemand { margin-top:5px; opacity:.55; border-left:2px dashed var(--line-2); padding-left:8px; }
.cx-od-tag { display:block; font-family:var(--f-mono); font-size:9px; letter-spacing:.04em; text-transform:uppercase; color:var(--t-4); margin-bottom:2px; }
.cx-raw { margin:0; white-space:pre-wrap; word-break:break-word; font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-2); max-height:360px; overflow:auto; background:var(--bg-2); border:1px solid var(--line-1); border-radius:var(--r-0); padding:10px; }
.cx-showall { margin-top:10px; align-self:center; font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-3); background:var(--bg-2); border:1px solid var(--line-2); border-radius:999px; padding:4px 14px; cursor:pointer; }
.cx-showall:hover { color:var(--acc); border-color:var(--acc-line); }
.cx .dim { color:var(--t-4); }
`;
    document.head.appendChild(s);
  }
}

if (!customElements.get('rd-tile-context')) customElements.define('rd-tile-context', ContextTile);

export default defineTile('rd-tile-context', (el, { ctx }) => {
  el.agent = ctx.agent;
});
