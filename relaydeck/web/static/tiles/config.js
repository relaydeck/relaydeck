// tiles/config.js — quick-edit model / plugins / workspace summary.
// The full editor lives in `relaydeck agent edit`; this tile shows the
// current values and links back.
//
// REFERENCE MIGRATION (read-only, single live resource). The pattern for an
// agent-detail tile on the @relaydeck/ui kit:
//   • extend RelayElement (light DOM — global CSS + tokens apply directly);
//   • declare reactive props with the static `properties` getter (no
//     decorators — relaydeck is build-less);
//   • use a LiveController for the live-store subscription (auto-unsubscribes
//     on disconnect — no manual teardown);
//   • render() returns a Lit template — no innerHTML, no querySelector;
//   • export the legacy mount(container, api, ctx)/unmount() contract via
//     defineTile(), which feeds props from ctx and appends the element.

import { RelayElement, defineTile, LiveController, html, nothing, chip, icon } from '@relaydeck/ui';

class ConfigTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    mode: { type: String },
  };

  constructor() {
    super();
    this.agent = {};
    this.mode = '';
    // Subscribed lazily once the agent id arrives (see willUpdate).
    this._full = new LiveController(this);
  }

  willUpdate(changed) {
    if (changed.has('agent') && this.agent?.id) {
      this._full.setKey(`/api/agents/${encodeURIComponent(this.agent.id)}`);
    }
  }

  _row(k, value) {
    return html`<div class="cfg-row"><span class="k">${k}</span><span class="v">${value}</span></div>`;
  }

  render() {
    const full = this._full.value || this.agent || {};
    const modelRef = full.config?.preset || full.config?.model || full.model;
    const plugins = Array.isArray(full.plugins) ? full.plugins : (full.config?.plugins || []);
    return html`
      <div style="padding:${this.mode === 'pop' ? '0' : '8px'}">
        ${this._row('model', modelRef
          ? chip(modelRef, 'accent')
          : html`<span class="dim">— inherits —</span>`)}
        ${this._row('plugins', plugins.length
          ? plugins.map((p) => chip(p))
          : html`<span class="dim">— none —</span>`)}
        ${this._row('workspace', html`<span class="mono-pill">@${full.workspace || ''}</span>`)}
        ${this._row('preamble', full.inject_identity_preamble === false
          ? chip('off', 'muted') : chip('on', 'ok'))}
        ${this._row('purpose', html`<span style="font-size:var(--t-xs)">${full.purpose || '—'}</span>`)}
        <div style="margin-top:10px;padding:8px 10px;border:1px dashed var(--line-2);border-radius:var(--r-2);font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-3)">
          Full edit: <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">relaydeck agent edit ${this.agent?.id || ''}</code>
        </div>
      </div>`;
  }
}

if (!customElements.get('rd-tile-config')) customElements.define('rd-tile-config', ConfigTile);

export default defineTile('rd-tile-config', (el, { ctx }) => {
  el.agent = ctx.agent;
  el.mode = ctx.mode || '';
});
