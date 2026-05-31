// tile_prompts.js — Approvals tile/lens for the `prompts` plugin.
// Renders open interactive prompts as cards with tap-able choice buttons.
// Clicking a button POSTs the structured decision back; the agent that
// raised the prompt resumes. Doubles as a lens (all open prompts in the
// workspace) and an agent-detail tile (that agent's open prompts).
//
// Migrated to the @relaydeck/ui kit: a light-DOM RelayElement authored with
// Lit templates (no innerHTML, no hand-rolled esc/addEventListener), exposed
// through the legacy mount(container, api, ctx)/unmount() contract via
// defineTile. Live wiring stays on the `live` store (subscribe + invalidate),
// driven through a LiveController that auto-unsubscribes on disconnect.

import {
  RelayElement, defineTile, LiveController, html, nothing, button, live,
} from '@relaydeck/ui';

// Choice-style → kit button variant. (success folds onto the primary look,
// matching the original `.btn.sm` styling.)
const STYLE_VARIANT = { primary: 'primary', danger: 'danger', success: 'primary', default: '' };

class PromptsTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    api: { attribute: false },
    workspace: { type: String },
  };

  constructor() {
    super();
    this.agent = null;
    this.api = null;
    this.workspace = null;
    this._status = {};                 // prompt id -> { text, cls, busy }
    this._prompts = new LiveController(this);
  }

  _promptsPath() {
    let path = '/api/plugins/prompts/?state=open&limit=50';
    if (this.workspace) path += `&workspace=${encodeURIComponent(this.workspace)}`;
    if (this.agent) path += `&agent=${encodeURIComponent(this.agent.id)}`;
    return path;
  }

  willUpdate(changed) {
    if (changed.has('agent') || changed.has('workspace')) {
      this._prompts.setKey(this._promptsPath());
    }
  }

  render() {
    const prompts = this._prompts.value;
    let body;
    if (!Array.isArray(prompts) || prompts.length === 0) {
      body = html`<div style="color:var(--t-4);font-family:var(--f-mono);font-size:var(--t-xxs)">No open approvals.</div>`;
    } else {
      body = prompts.map((p) => this._card(p));
    }
    return html`<div style="display:flex;flex-direction:column;gap:10px;padding:6px">${body}</div>`;
  }

  _card(p) {
    const askedBy = p.agent_id
      ? html`<span style="color:var(--acc)">${p.agent_id}</span> asks`
      : 'A worker asks';
    const st = this._status[p.id];
    return html`
      <div style="border:1px solid var(--line-2);border-radius:6px;padding:10px;background:var(--bg-1);display:flex;flex-direction:column;gap:8px">
        <div style="font-family:var(--f-mono);font-size:10px;color:var(--t-4)">${askedBy} · <span title=${p.id}>${p.id}</span></div>
        <div style="font-family:var(--f-sans);font-size:var(--t-sm);color:var(--t-1)">${p.body}</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">
          ${(p.choices || []).map((c) => button(
            { variant: STYLE_VARIANT[c.style] || '', size: 'sm',
              disabled: !!(st && st.busy), onClick: () => this._respond(p, c) },
            c.label || c.id,
          ))}
        </div>
        ${st
          ? html`<div style="font-family:var(--f-mono);font-size:var(--t-xxs);color:${st.cls === 'ok' ? 'var(--ok)' : st.cls === 'err' ? 'var(--err)' : 'var(--t-3)'}">${st.text}</div>`
          : nothing}
      </div>`;
  }

  async _respond(p, choice) {
    this._status = { ...this._status, [p.id]: { text: 'submitting…', cls: '', busy: true } };
    this.requestUpdate();
    try {
      const r = await this.api.fetch(`/api/plugins/prompts/${encodeURIComponent(p.id)}/respond`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ choice: choice.id, by: 'web' }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.ok) {
        this._status = { ...this._status, [p.id]: { text: `✓ ${choice.label || choice.id}`, cls: 'ok', busy: true } };
        live.invalidate('/api/plugins/prompts');
      } else {
        this._status = { ...this._status, [p.id]: { text: data.error || `failed (${r.status})`, cls: 'err', busy: true } };
        live.invalidate('/api/plugins/prompts');
      }
    } catch (e) {
      this._status = { ...this._status, [p.id]: { text: 'error: ' + e.message, cls: 'err', busy: false } };
    }
    this.requestUpdate();
  }
}

if (!customElements.get('rd-prompts-tile')) customElements.define('rd-prompts-tile', PromptsTile);

export default defineTile('rd-prompts-tile', (el, { api, ctx }) => {
  el.api = api;
  el.agent = (ctx && ctx.agent) ? ctx.agent : null;
  el.workspace = (el.agent && el.agent.workspace) || api.workspace || null;
});
