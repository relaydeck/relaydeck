// Fleet panel — contributed by the metering plugin.
// Aggregates running/errored agents + token usage + cost across the fleet.
//
// Migrated to the @relaydeck/ui kit. This panel OWNS its render root (unlike the
// metering tiles, which write into host-provided .tile-val/.tile-sub anchors), so
// it's authored as a RelayElement and exposed through the framework-neutral
// mount(container, api)/unmount() contract via defineTile(). The cards + usage
// table render through lit-html (no innerHTML, no querySelector wiring, no
// hand-rolled esc — Lit auto-escapes interpolations). The bespoke .fleet-* CSS
// is preserved and injected once (idempotent <style id="fleet-panel-css">). The
// EventsController carries the live agent.*/usage.record refresh and the 15s
// safety-net timer is torn down on disconnect.

import { RelayElement, defineTile, EventsController, html, nothing } from '@relaydeck/ui';

const fmtN = (n) => (n == null ? '—' : n.toLocaleString());
const fmtUSD = (n) => (n == null ? '$0.00' : '$' + Number(n).toFixed(n < 1 ? 4 : 2));

const PANEL_CSS = `
  .fleet-wrap{display:flex;flex-direction:column;flex:1;min-height:0;overflow:hidden;font-family:var(--f-sans)}
  .fleet-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;padding:18px;border-bottom:1px solid var(--line-1)}
  .fleet-card{background:var(--bg-1);border:1px solid var(--line-2);border-radius:6px;padding:14px 16px}
  .fleet-card .lbl{font-family:var(--f-mono);font-size:var(--t-xxs);text-transform:uppercase;letter-spacing:.08em;color:var(--t-3)}
  .fleet-card .val{font-size:24px;font-weight:600;color:var(--t-1);margin-top:6px;font-family:var(--f-mono)}
  .fleet-card .sub{font-size:var(--t-xs);color:var(--t-3);margin-top:2px}
  .fleet-card.ok .val{color:var(--ok)}
  .fleet-card.err .val{color:var(--err)}
  .fleet-card.acc .val{color:var(--acc)}
  .fleet-table-wrap{flex:1;overflow:auto;padding:0 18px 18px}
  .fleet-table-wrap::-webkit-scrollbar{width:6px}
  .fleet-table-wrap::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:3px}
  .fleet-h{display:flex;align-items:center;justify-content:space-between;padding:16px 18px 8px}
  .fleet-h h3{margin:0;font-size:var(--t-md);color:var(--t-2);text-transform:uppercase;letter-spacing:.06em;font-weight:600}
  .fleet-h .muted{font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-3)}
  table.fleet{width:100%;border-collapse:collapse;font-family:var(--f-mono);font-size:var(--t-xs)}
  table.fleet th{text-align:left;color:var(--t-3);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line-2);text-transform:uppercase;letter-spacing:.06em;font-size:var(--t-xxs)}
  table.fleet td{padding:6px 8px;border-bottom:1px solid var(--line-1);color:var(--t-1)}
  table.fleet td.num{text-align:right;color:var(--t-2)}
  table.fleet tr:hover td{background:var(--bg-1)}
  .fleet-empty{padding:40px;text-align:center;color:var(--t-3);font-size:var(--t-sm)}
`;

function injectCSS() {
  if (document.getElementById('fleet-panel-css')) return;
  const style = document.createElement('style');
  style.id = 'fleet-panel-css';
  style.textContent = PANEL_CSS;
  document.head.appendChild(style);
}

class FleetPanel extends RelayElement {
  constructor() {
    super();
    this.api = null;
    this._agents = [];
    this._usage = [];
    this._timer = null;
    // Live: refresh on agent.* lifecycle + usage.record metering events.
    // (EventsController takes an options object; refresh() requestUpdates
    // itself, so rerender:false avoids a repaint on every unrelated event.)
    this._events = new EventsController(this, {
      rerender: false,
      onEvent: (ev) => {
        if (!ev || !ev.type) return;
        if (ev.type.startsWith('agent.') || ev.type === 'usage.record') this.refresh();
      },
    });
  }

  connectedCallback() {
    super.connectedCallback();
    injectCSS();
    // Periodic refresh as a safety net.
    this._timer = setInterval(() => this.refresh(), 15000);
    this.refresh();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
  }

  async refresh() {
    if (!this.api) return;
    try {
      const [agents, usage] = await Promise.all([
        this.api.fetch('/api/agents').then((r) => r.json()),
        this.api.fetch('/api/usage').then((r) => r.json()).catch(() => []),
      ]);
      this._agents = agents || [];
      this._usage = usage || [];
      this.requestUpdate();
    } catch (e) {
      console.warn('fleet refresh failed', e);
    }
  }

  _totals() {
    let prompt = 0, completion = 0, totalTokens = 0, cost = 0;
    for (const r of this._usage) {
      prompt += r.total_prompt || 0;
      completion += r.total_completion || 0;
      totalTokens += r.total_tokens || 0;
      cost += r.total_cost || 0;
    }
    return { prompt, completion, totalTokens, cost };
  }

  _cards() {
    const agents = this._agents;
    const usage = this._usage;
    const running = agents.filter((a) => a.status === 'running').length;
    const errored = agents.filter((a) => a.status === 'errored').length;
    const total = agents.length;
    const { prompt, completion, totalTokens, cost } = this._totals();
    return html`
      <div class="fleet-cards">
        <div class="fleet-card ok">
          <div class="lbl">running</div>
          <div class="val" id="fl-running">${running}</div>
          <div class="sub" id="fl-total-sub">of ${total} agent${total === 1 ? '' : 's'}</div>
        </div>
        <div class="fleet-card err">
          <div class="lbl">errored</div>
          <div class="val" id="fl-errored">${errored}</div>
          <div class="sub">need attention</div>
        </div>
        <div class="fleet-card acc">
          <div class="lbl">tokens (30d)</div>
          <div class="val" id="fl-tokens">${fmtN(totalTokens)}</div>
          <div class="sub" id="fl-tokens-sub">${fmtN(prompt)} in · ${fmtN(completion)} out</div>
        </div>
        <div class="fleet-card">
          <div class="lbl">cost (30d)</div>
          <div class="val" id="fl-cost">${fmtUSD(cost)}</div>
          <div class="sub" id="fl-cost-sub">${usage.length} model/provider combo${usage.length === 1 ? '' : 's'}</div>
        </div>
      </div>`;
  }

  _table() {
    const usage = this._usage;
    if (!usage || !usage.length) {
      return html`<div class="fleet-empty">No usage records yet. Hook metering into your harness model calls to populate this view.</div>`;
    }
    return html`
      <table class="fleet">
        <thead><tr>
          <th>model</th><th>provider</th>
          <th style="text-align:right">reqs</th>
          <th style="text-align:right">prompt</th>
          <th style="text-align:right">completion</th>
          <th style="text-align:right">total</th>
          <th style="text-align:right">cost</th>
        </tr></thead>
        <tbody>
          ${usage.map((r) => html`
            <tr>
              <td>${r.model}</td>
              <td>${r.provider}</td>
              <td class="num">${fmtN(r.requests)}</td>
              <td class="num">${fmtN(r.total_prompt)}</td>
              <td class="num">${fmtN(r.total_completion)}</td>
              <td class="num">${fmtN(r.total_tokens)}</td>
              <td class="num">${fmtUSD(r.total_cost)}</td>
            </tr>`)}
        </tbody>
      </table>`;
  }

  render() {
    return html`
      <div class="fleet-wrap">
        ${this._cards()}
        <div class="fleet-h">
          <h3>Model / Provider usage</h3>
          <span class="muted">last 30 days</span>
        </div>
        <div class="fleet-table-wrap">
          <div id="fl-table">${this._table()}</div>
        </div>
      </div>`;
  }
}

if (!customElements.get('rd-metering-fleet')) customElements.define('rd-metering-fleet', FleetPanel);

export default defineTile('rd-metering-fleet', (el, { api }) => {
  el.api = api;
});
