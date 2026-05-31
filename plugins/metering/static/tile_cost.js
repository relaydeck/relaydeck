// Cost-24h tile — contributed by the metering plugin.
//
// Migrated to the @relaydeck/ui kit. This tile is special: it does NOT own its
// render root. The host tile template provides outer .tile-val / .tile-sub
// nodes, and this module writes the formatted values into them (the metering
// tiles share that DOM contract). So it keeps the framework-neutral
// default-export CLASS with mount(container, api, ctx)/unmount() — authoring it
// as a RelayElement + defineTile would replaceChildren() and destroy those
// host-provided anchor nodes. From the kit we take `events` (the shared SSE
// bus, same instance the dashboard uses) for live usage.record updates; the
// 30s timer + unsubscribe teardown are preserved.

import { events } from '@relaydeck/ui';

const fmtUSD = (n) => {
  if (n == null) return '—';
  if (n === 0) return '$0.00';
  if (n < 0.01) return '$' + n.toFixed(4);
  return '$' + n.toFixed(2);
};

export default class CostTile {
  constructor() {
    this.unsubEvent = null;
    this.timer = null;
    this.container = null;
    this.ctx = null;
    this.api = null;
  }

  _agentId() {
    return this.ctx?.agent?.id || this.ctx?.agentId || null;
  }

  async mount(container, api, ctx) {
    this.container = container;
    this.ctx = ctx;
    this.api = api;
    this._render(null, 0);
    await this.refresh();
    this.unsubEvent = events.subscribe((ev) => {
      if (!ev) return;
      const aid = this._agentId();
      if (ev.type === 'usage.record' && (!aid || ev.agent_id === aid)) {
        this.refresh();
      }
    });
    this.timer = setInterval(() => this.refresh(), 30000);
  }

  unmount() {
    if (this.unsubEvent) this.unsubEvent();
    if (this.timer) clearInterval(this.timer);
    this.unsubEvent = null;
    this.timer = null;
  }

  async refresh() {
    try {
      const agentId = this._agentId();
      const url = agentId
        ? `/api/usage?agent_id=${encodeURIComponent(agentId)}`
        : `/api/usage`;
      const r = await this.api.fetch(url);
      const rows = await r.json();
      let cost = 0, combos = 0;
      for (const row of rows) {
        cost += row.total_cost || 0;
        combos++;
      }
      this._render(cost, combos);
    } catch (e) { /* ignore */ }
  }

  _render(cost, combos) {
    const valNode = this.container.querySelector('.tile-val');
    const subNode = this.container.querySelector('.tile-sub');
    if (valNode) valNode.textContent = fmtUSD(cost);
    if (subNode) {
      subNode.textContent =
        cost == null ? 'no usage yet'
        : combos === 0 ? '$0.00 spent'
        : `${combos} model${combos === 1 ? '' : 's'}`;
    }
  }
}
