// calls.js — per-prompt LLM call log for a Relaydeck-native agent.
// Surfaces model_invocations: each turn's model, latency, tokens, ok, and
// click-to-expand prompt + response. The "extended tracking" a native
// agent records on every completion.
//
// Migrated to @relaydeck/ui: a RelayElement authored body, exposed via the
// framework-neutral mount(container, api, ctx)/unmount() contract through
// defineTile(). No hand-rolled esc()/innerHTML/addEventListener — Lit
// templates auto-escape, @click wires the refresh button, and the kit's
// button/icon primitives replace the bespoke ones. The .card/.chip/.btn
// classes and the tile's inline styles are preserved verbatim.

import { RelayElement, defineTile, html, nothing, button, icon } from '@relaydeck/ui';

const fmtMs = (ms) => (ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms');

class RelaydeckCallsTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    api: { attribute: false },
    _rows: { state: true },
    _error: { state: true },
    _loading: { state: true },
  };

  constructor() {
    super();
    this.agent = null;
    this.api = null;
    this._rows = null;     // null = not loaded yet; [] = loaded, empty
    this._error = '';
    this._loading = false;
  }

  willUpdate(changed) {
    if ((changed.has('agent') || changed.has('api')) && this.api && this.agent?.id && this._rows === null && !this._loading) {
      this._load();
    }
  }

  async _load() {
    if (!this.api || !this.agent?.id) return;
    this._loading = true;
    this._error = '';
    this.requestUpdate();
    try {
      const r = await this.api.getJSON(`/api/plugins/relaydeck-native/${encodeURIComponent(this.agent.id)}/invocations`);
      this._rows = (r && r.invocations) || [];
    } catch (e) {
      this._error = e?.message || String(e);
    } finally {
      this._loading = false;
      this.requestUpdate();
    }
  }

  _row(c) {
    const tok = c.tokens_known
      ? html`${c.prompt_tokens}+${c.completion_tokens} tok`
      : html`<span style="color:var(--t-4)">tokens n/a</span>`;
    const okdot = c.ok
      ? html`<span style="color:var(--ok)">●</span>`
      : html`<span style="color:var(--err)">●</span>`;
    const when = new Date((c.ts || 0) * 1000).toLocaleTimeString();
    return html`<details style="border:1px solid var(--line-2);border-radius:var(--r-2)">
      <summary style="cursor:pointer;padding:6px 10px;display:flex;gap:10px;align-items:center;font-family:var(--f-mono);font-size:11px">
        ${okdot} <span style="color:var(--t-3)">${when}</span>
        <span class="chip accent" style="font-size:9px">${c.model || '?'}</span>
        <span>${fmtMs(c.latency_ms || 0)}</span>
        <span>${tok}</span>
        ${c.error ? html`<span style="color:var(--err)">${String(c.error).slice(0, 40)}</span>` : nothing}
      </summary>
      <div style="padding:8px 12px;border-top:1px solid var(--line-1);display:flex;flex-direction:column;gap:6px">
        <div style="font-size:9px;color:var(--t-4);font-family:var(--f-mono);text-transform:uppercase">prompt</div>
        <pre style="margin:0;white-space:pre-wrap;word-break:break-word;font-family:var(--f-mono);font-size:11px;color:var(--t-2);max-height:160px;overflow:auto">${c.prompt || '—'}</pre>
        <div style="font-size:9px;color:var(--t-4);font-family:var(--f-mono);text-transform:uppercase">response</div>
        <pre style="margin:0;white-space:pre-wrap;word-break:break-word;font-family:var(--f-mono);font-size:11px;color:var(--t-1)">${c.response || '—'}</pre>
      </div>
    </details>`;
  }

  _body() {
    if (this._error) {
      return html`<div style="color:var(--err);font-family:var(--f-mono);font-size:var(--t-xs)">failed: ${this._error}</div>`;
    }
    if (this._rows === null) {
      return html`loading…`;
    }
    if (!this._rows.length) {
      return html`<div style="color:var(--t-4)">no calls yet</div>`;
    }
    return this._rows.map((c) => this._row(c));
  }

  render() {
    return html`<div class="card" style="flex:1;overflow:auto;padding:0;min-height:0">
      <div style="padding:10px 14px;border-bottom:1px solid var(--line-1);display:flex;justify-content:space-between;align-items:center">
        <span class="card-title">LLM calls</span>
        ${button({ variant: 'ghost', size: 'sm', act: 'refresh', onClick: () => this._load() }, 'refresh')}
      </div>
      <div data-body style="padding:8px 12px;display:flex;flex-direction:column;gap:6px">${this._body()}</div>
    </div>`;
  }
}

if (!customElements.get('rd-native-calls')) customElements.define('rd-native-calls', RelaydeckCallsTile);

export default defineTile('rd-native-calls', (el, { api, ctx }) => {
  el.api = api;
  el.agent = ctx.agent;
});
