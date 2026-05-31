// tiles/context.js — per-session context usage for an agent.
//
// The context a model carries each turn is the prompt_tokens it's sent
// (system prompt + accumulated history + tool defs), so the LATEST turn's
// prompt_tokens is the thread's *current context fill*. This tab lists the
// agent's sessions/threads with their current + peak context, turns, total
// tokens spent, and model — real data from GET /api/agents/{id}/sessions
// (usage_records). No model context-window limit is assumed; bars scale
// relative to the busiest thread. Live: re-fetches on usage events.
//
// REFERENCE-STYLE MIGRATION (read-only, single live resource) — same shape as
// tiles/config.js: RelayElement (light DOM) + LiveController (auto-unsubscribe)
// + defineTile() for the legacy mount/unmount contract. render() returns a Lit
// template (no innerHTML, no querySelector). The one-time `.ctx` CSS injection
// and `.cs-*` row markup are preserved verbatim for theming + e2e selectors.

import {
  RelayElement, defineTile, LiveController, html, nothing,
  fmtNum, fmtCost, relTime,
} from '@relaydeck/ui';

class ContextTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
  };

  constructor() {
    super();
    this.agent = {};
    // Subscribed lazily once the agent id arrives (see willUpdate).
    this._sessions = new LiveController(this);
  }

  connectedCallback() {
    super.connectedCallback();
    this._injectCSS();
  }

  willUpdate(changed) {
    if (changed.has('agent') && this.agent?.id) {
      this._sessions.setKey(`/api/agents/${encodeURIComponent(this.agent.id)}/sessions`);
    }
  }

  _row(s, peak) {
    const cur = s.current_context || 0;
    const pk = s.peak_context || 0;
    const pct = Math.min(100, Math.round((cur / peak) * 100));
    const pkPct = Math.min(100, Math.round((pk / peak) * 100));
    const label = s.label || s.session_id || 'session';
    // Lit auto-escapes interpolations, so values go in raw (no esc()).
    return html`
      <div class="cs-row">
        <div class="cs-top">
          <span class="cs-label" title=${s.session_id ?? ''}>${label}</span>
          <span class="cs-cur">${fmtNum(cur)} <span class="dim">tok ctx</span></span>
        </div>
        <div class="cs-bar"><span class="cs-peak" style="width:${pkPct}%"></span><span class="cs-fill" style="width:${pct}%"></span></div>
        <div class="cs-meta">
          <span>${fmtNum(s.turns)} turns</span>
          <span>peak ${fmtNum(pk)}</span>
          <span>${fmtNum(s.total_tokens)} total</span>
          ${s.cost_usd ? html`<span>${fmtCost(s.cost_usd)}</span>` : nothing}
          ${s.model ? html`<span class="cs-model">${s.model}</span>` : nothing}
          <span class="cs-when dim">${s.last_ts ? relTime(s.last_ts * 1000) : ''}</span>
        </div>
      </div>`;
  }

  render() {
    // No key yet (agent id still arriving) → loading; key set but value not yet
    // resolved → loading; explicit null payload → unavailable.
    const data = this._sessions.value;
    if (!this._sessions.key || data === undefined) {
      return html`<div class="ctx"><div class="ctx-loading">loading…</div></div>`;
    }
    if (!data) {
      return html`<div class="ctx"><div class="ctx-loading">sessions unavailable</div></div>`;
    }

    const sessions = data.sessions || [];
    const peak = sessions.reduce((m, s) => Math.max(m, s.peak_context || 0), 0) || 1;
    const totalTokens = sessions.reduce((a, s) => a + (s.total_tokens || 0), 0);

    return html`
      <div class="ctx">
        <div class="block ctx-block">
          <div class="block-head">
            <span class="eyebrow">context · per thread · current fill = last prompt sent</span>
            <span class="ctx-total">${sessions.length} thread${sessions.length === 1 ? '' : 's'}</span>
          </div>
          ${sessions.length
            ? html`
                <div class="cs-list">${sessions.map((s) => this._row(s, peak))}</div>
                <div class="ctx-foot"><span class="dim">bars scale to the busiest thread (${fmtNum(peak)} tok) — no model limit assumed</span><span class="dim">${fmtNum(totalTokens)} tokens all threads</span></div>`
            : html`<div class="ctx-empty">No sessions with recorded usage yet. Threads appear here once the agent makes its first model call.</div>`}
        </div>
      </div>`;
  }

  _injectCSS() {
    if (document.getElementById('ctx-css')) return;
    const s = document.createElement('style');
    s.id = 'ctx-css';
    s.textContent = `
.ctx { padding:14px; height:100%; overflow:auto; }
.ctx-loading { padding:24px; color:var(--t-3); font-family:var(--f-mono); font-size:var(--t-xs); }
.ctx .block { background:var(--bg-1); border:1px solid var(--line-2); border-radius:var(--r-1); padding:14px 16px; max-width:760px; }
.ctx .block-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:14px; }
.ctx .eyebrow { font-family:var(--f-mono); font-size:var(--t-xxs); letter-spacing:.13em; text-transform:uppercase; color:var(--t-3); }
.ctx-total { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-1); background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--r-0); padding:3px 9px; }
.ctx-empty { font-family:var(--f-mono); font-size:var(--t-xs); color:var(--t-4); padding:18px 0; }
.cs-list { display:flex; flex-direction:column; }
.cs-row { padding:12px 0; border-top:1px solid var(--line-1); }
.cs-row:first-child { border-top:0; padding-top:2px; }
.cs-top { display:flex; align-items:baseline; justify-content:space-between; gap:10px; margin-bottom:7px; }
.cs-label { font-family:var(--f-mono); font-size:var(--t-sm); color:var(--t-1); min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.cs-cur { font-family:var(--f-mono); font-size:var(--t-sm); color:var(--t-1); flex-shrink:0; font-feature-settings:"tnum" 1; }
.cs-bar { position:relative; height:8px; background:var(--bg-2); border:1px solid var(--line-2); border-radius:999px; overflow:hidden; }
.cs-peak { position:absolute; left:0; top:0; bottom:0; background:var(--acc-soft); }
.cs-fill { position:absolute; left:0; top:0; bottom:0; background:var(--acc); border-radius:999px; }
.cs-meta { display:flex; flex-wrap:wrap; gap:12px; margin-top:7px; font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-3); }
.cs-meta .cs-model { color:var(--acc); }
.cs-meta .cs-when { margin-left:auto; }
.ctx-foot { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:12px; padding-top:10px; border-top:1px dashed var(--line-1); font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-4); }
`;
    document.head.appendChild(s);
  }
}

if (!customElements.get('rd-tile-context')) customElements.define('rd-tile-context', ContextTile);

export default defineTile('rd-tile-context', (el, { ctx }) => {
  el.agent = ctx.agent;
});
