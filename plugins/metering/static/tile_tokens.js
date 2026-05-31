// Tokens-24h tile — contributed by the metering plugin.
// Subscribes to usage.record SSE events for live updates.
//
// Migrated to the @relaydeck/ui kit: a light-DOM RelayElement exposed through
// the legacy mount(container, api, ctx)/unmount() contract via defineTile().
//   • the .tile-val/.tile-sub DOM hooks are PRESERVED (rendered by the template
//     instead of hand-rolled innerHTML);
//   • the usage.record SSE subscription rides an EventsController (auto-cleans
//     on disconnect) and the 30s safety-net refresh timer is registered for
//     teardown — no manual unsubscribe bookkeeping;
//   • fmtNum from the kit replaces the bespoke number formatter.

import { RelayElement, defineTile, EventsController, html, fmtNum } from '@relaydeck/ui';

class TokensTile extends RelayElement {
  static properties = {
    total: { attribute: false },
    sub: { attribute: false },
  };

  constructor() {
    super();
    this.api = null;
    this.agentId = null;
    this.total = null;          // null → "no usage yet" placeholder
    this.sub = 'no usage yet';
    // Live: refresh on usage.record events scoped to this agent (or the fleet).
    this._events = new EventsController(this, {
      types: ['usage.record'],
      rerender: false,
      onEvent: (ev) => {
        if (!ev) return;
        if (!this.agentId || ev.agent_id === this.agentId) this.refresh();
      },
    });
  }

  connectedCallback() {
    super.connectedCallback();
    // Safety-net periodic refresh; released on disconnect.
    this._timer = setInterval(() => this.refresh(), 30000);
    this.addController({
      hostDisconnected: () => {
        if (this._timer) clearInterval(this._timer);
        this._timer = null;
      },
    });
  }

  async refresh() {
    if (!this.api) return;
    try {
      if (this.agentId) {
        const r = await this.api.fetch(
          `/api/agents/${encodeURIComponent(this.agentId)}/stats`,
        );
        const stats = await r.json();
        this._apply(stats.tokens_in, stats.tokens_out);
        return;
      }
      const r = await this.api.fetch('/api/agents/usage-rollup');
      const rollup = await r.json();
      let total = 0;
      for (const rec of Object.values(rollup || {})) {
        total += rec?.tokens || 0;
      }
      this._apply(total, 0, { fleet: true });
    } catch (e) { /* ignore */ }
  }

  _apply(prompt, completion, opts = {}) {
    const total = (prompt || 0) + (completion || 0);
    this.total = prompt == null ? null : total;
    this.sub =
      prompt == null ? 'no usage yet'
      : opts.fleet ? `${fmtNum(total)} tokens · fleet 24h`
      : `${fmtNum(prompt)} in · ${fmtNum(completion)} out`;
  }

  render() {
    return html`
      <div class="tile-val">${this.total == null ? '—' : fmtNum(this.total)}</div>
      <div class="tile-sub">${this.sub}</div>`;
  }
}

if (!customElements.get('rd-metering-tokens')) {
  customElements.define('rd-metering-tokens', TokensTile);
}

export default defineTile('rd-metering-tokens', (el, { api, ctx }) => {
  el.api = api;
  el.agentId = ctx?.agent?.id || ctx?.agentId || null;
  el.refresh();
});
