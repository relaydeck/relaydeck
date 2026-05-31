// tiles/raw.js — JSON view of the agent record.
//
// Hits /api/agents/{id} for the full record (includes config_yaml,
// system_prompt, semantic_status_at, last_active_at — fields not on
// the list endpoint). Falls back to the slim row from /api/agents
// (ctx.agent) until the single-agent fetch resolves.
//
// MIGRATION (read-only, single live resource) — same shape as tiles/config.js:
//   • RelayElement (light DOM — global CSS + tokens apply directly);
//   • static `properties` getter for reactive props (no decorators);
//   • LiveController for the /api/agents/{id} subscription (auto-unsubscribes
//     on disconnect — no manual teardown);
//   • render() returns a Lit template — no innerHTML, no querySelector;
//   • copy uses navigator.clipboard with a brief reactive "copied" flash
//     (its restore timer is cleared in disconnectedCallback);
//   • exposed via defineTile() so the legacy mount/unmount tile contract and
//     the `export default class` shape stay identical.

import { RelayElement, defineTile, LiveController, html } from '@relaydeck/ui';

class RawTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    _copied: { state: true },
  };

  constructor() {
    super();
    this.agent = {};
    this._copied = false;
    this._copyTimer = null;
    // Subscribed lazily once the agent id arrives (see willUpdate).
    this._full = new LiveController(this);
  }

  willUpdate(changed) {
    if (changed.has('agent') && this.agent?.id) {
      this._full.setKey(`/api/agents/${encodeURIComponent(this.agent.id)}`);
    }
  }

  // The full record once it lands; the slim ctx.agent row until then.
  get _record() {
    return this._full.value || this.agent || {};
  }

  async _copy() {
    try {
      await navigator.clipboard.writeText(JSON.stringify(this._record, null, 2));
      this._copied = true;
      clearTimeout(this._copyTimer);
      this._copyTimer = setTimeout(() => { this._copied = false; }, 1200);
    } catch (e) {
      alert('Copy failed: ' + e.message);
    }
  }

  disconnectedCallback() {
    clearTimeout(this._copyTimer);
    super.disconnectedCallback();
  }

  render() {
    const record = this._record;
    return html`
      <div class="card" style="flex:1;overflow:hidden;min-height:0;padding:0;display:flex;flex-direction:column">
        <div style="padding:10px 14px;border-bottom:1px solid var(--line-1);display:flex;justify-content:space-between;align-items:center;flex-shrink:0">
          <span class="card-title">Raw record · ${this.agent?.id || ''}</span>
          <button class="btn ghost sm" data-act="copy" title="Copy JSON to clipboard" @click=${() => this._copy()}>
            ${this._copied ? 'copied' : 'copy'}
          </button>
        </div>
        <pre style="margin:0;padding:14px 16px;font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-2);white-space:pre-wrap;word-break:break-word;flex:1;overflow:auto;min-height:0">${JSON.stringify(record, null, 2)}</pre>
      </div>`;
  }
}

if (!customElements.get('rd-tile-raw')) customElements.define('rd-tile-raw', RawTile);

export default defineTile('rd-tile-raw', (el, { ctx }) => {
  el.agent = ctx.agent;
});
