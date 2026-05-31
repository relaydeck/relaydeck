// tile_compose.js — Compose tile for the agents lens.
// Plugin contribution from `messaging`. Renders a compact form for
// sending a message to a peer agent in the same workspace.
//
// Migrated to the @relaydeck/ui kit: authored as a light-DOM RelayElement and
// exposed via defineTile() (which provides the framework-neutral
// mount(container, api, ctx)/unmount() contract the dashboard host loads). The
// peer list is read from the live `/api/agents` store via a LiveController
// (auto-unsubscribes on disconnect); the Send button uses the kit button()
// helper; validation + error display and the bespoke inline styles / data-*
// hooks are preserved.

import {
  RelayElement, defineTile, LiveController, html, nothing, button,
} from '@relaydeck/ui';

class MessagingComposeTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    api: { attribute: false },
    _to: { state: true },
    _body: { state: true },
    _err: { state: true },
  };

  constructor() {
    super();
    this.agent = {};
    this.api = null;
    this._to = '';
    this._body = '';
    this._err = null;           // { text, tone:'err'|'ok' } | null
    this._agents = new LiveController(this);
    this._agents.setKey('/api/agents');
  }

  _peers() {
    const all = this._agents.value || [];
    return all.filter((a) => a.workspace === this.agent?.workspace && a.id !== this.agent?.id);
  }

  async _send() {
    const to = (this._to || '').trim();
    const body = (this._body || '').trim();
    if (!to) { this._err = { text: 'Pick a peer first.', tone: 'err' }; return; }
    if (!body) { this._err = { text: 'Message body required.', tone: 'err' }; return; }
    try {
      const r = await this.api.fetch(`/api/workspaces/${encodeURIComponent(this.agent.workspace)}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body, from: this.agent.id, agent: to }),
      });
      if (!r.ok) throw new Error(`${r.status}`);
      this._body = '';
      this._err = { text: '✓ sent', tone: 'ok' };
    } catch (e) {
      this._err = { text: 'Send failed: ' + e.message, tone: 'err' };
    }
  }

  _onKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      this._send();
    }
  }

  render() {
    const peers = this._peers();
    const errColor = this._err?.tone === 'ok' ? 'var(--ok)' : 'var(--err)';
    return html`
      <div style="display:flex;flex-direction:column;gap:8px;padding:4px">
        <div style="font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-3)">
          Send from <span style="color:var(--acc)">${this.agent?.id || ''}</span> →
        </div>
        <select data-f="to" style="width:100%;font-family:var(--f-mono);font-size:var(--t-xs)"
          .value=${this._to}
          @change=${(e) => { this._to = e.target.value; }}>
          <option value="">— pick a peer —</option>
          ${peers.map((p) => html`<option value=${p.id}>${p.id} · ${p.status || ''}</option>`)}
        </select>
        <textarea data-f="body" rows="4" placeholder="Type your message…"
          style="width:100%;font-family:var(--f-sans);font-size:var(--t-sm);resize:vertical"
          .value=${this._body}
          @input=${(e) => { this._body = e.target.value; }}
          @keydown=${(e) => this._onKeydown(e)}></textarea>
        ${this._err
          ? html`<div data-err style="color:${errColor};font-size:var(--t-xxs);font-family:var(--f-mono)">${this._err.text}</div>`
          : html`<div data-err style="display:none"></div>`}
        <div style="display:flex;justify-content:flex-end;gap:6px">
          ${button({ variant: 'primary', size: 'sm', act: 'send', onClick: () => this._send() }, 'Send')}
        </div>
        <div style="font-family:var(--f-mono);font-size:10px;color:var(--t-4);margin-top:4px">
          CLI equivalent:
          <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">relaydeck workspace message "&lt;body&gt;" --agent &lt;peer&gt;</code>
        </div>
      </div>`;
  }
}

if (!customElements.get('rd-messaging-compose')) {
  customElements.define('rd-messaging-compose', MessagingComposeTile);
}

export default defineTile('rd-messaging-compose', (el, { api, ctx }) => {
  el.agent = ctx.agent;
  el.api = api;
});
