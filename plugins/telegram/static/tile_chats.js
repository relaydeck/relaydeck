// tile_chats.js — Agents-lens tile contributed by the telegram plugin.
// Lists Telegram chats currently routed to this agent so operators
// (and the agent's own awareness via /api/preferences) understand
// the blast radius.
//
// Migrated to the @relaydeck/ui kit: authored as a light-DOM RelayElement and
// exposed via defineTile() (which provides the framework-neutral
// mount(container, api, ctx)/unmount() contract the dashboard host loads).
// Routes are fetched once on mount; the hand-rolled esc()/innerHTML are gone —
// Lit auto-escapes template interpolations. The empty state links back to the
// Telegram lens so route creation stays web-primary.

import { RelayElement, defineTile, html } from '@relaydeck/ui';

class TelegramChatsTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    api: { attribute: false },
    _routes: { state: true },
    _error: { state: true },
    _loading: { state: true },
  };

  constructor() {
    super();
    this.agent = {};
    this.api = null;
    this._routes = [];
    this._error = null;
    this._loading = true;
  }

  willUpdate(changed) {
    if ((changed.has('agent') || changed.has('api')) && this.agent?.id && this.api && !this._fetched) {
      this._fetched = true;
      this._load();
    }
  }

  async _load() {
    try {
      const data = await this.api.getJSON('/api/plugins/telegram/routes');
      this._routes = (data.routes || []).filter((r) =>
        (r.agent === this.agent.id) ||
        (!r.agent && r.workspace === this.agent.workspace),
      );
      this._error = null;
    } catch (e) {
      this._error = e.message;
    } finally {
      this._loading = false;
    }
  }

  _routeRow(r) {
    const chat = r.chat_id == null ? '(any chat)' : r.chat_id;
    const topic = r.thread_id != null ? ` · topic ${r.thread_id}` : '';
    const cmd = r.command ? ` · /${r.command}` : '';
    const target = r.agent === this.agent.id
      ? html`<span style="color:var(--acc)">direct</span>`
      : html`<span style="color:var(--t-3)">broadcast</span>`;
    return html`
      <div style="padding:10px 12px;border-bottom:1px solid var(--line-1);font-family:var(--f-mono);font-size:var(--t-xs);min-width:0">
        <div style="color:var(--t-1)">${chat}${topic}${cmd}</div>
        <div style="color:var(--t-3);font-size:10px;margin-top:2px">workspace: @${r.workspace} · ${target} · ${r.direction || 'in+out'}</div>
      </div>`;
  }

  _openTelegramRoutes() {
    const u = new URL(window.location.href);
    u.searchParams.set('lens', 'telegram');
    u.searchParams.set('section', 'routes');
    window.location.href = u.toString();
  }

  render() {
    if (this._loading) {
      return html`<div style="padding:10px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs)">loading…</div>`;
    }
    if (this._error) {
      return html`<div style="padding:14px;color:var(--err);font-family:var(--f-mono);font-size:var(--t-xs)">${this._error}</div>`;
    }
    if (!this._routes.length) {
      return html`
        <div style="padding:14px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center">
          No Telegram routes target <b>${this.agent.id}</b> or its workspace.
          <br><br>
          <button
            style="background:var(--acc);border:1px solid var(--acc);color:var(--acc-text);font:600 var(--t-xs) var(--f-mono);padding:5px 10px;border-radius:var(--r-1);cursor:pointer"
            @click=${() => this._openTelegramRoutes()}>Open Telegram routes</button>
        </div>`;
    }
    return html`
      <div style="display:flex;flex-direction:column;min-height:0">
        ${this._routes.map((r) => this._routeRow(r))}
      </div>`;
  }
}

if (!customElements.get('rd-telegram-chats')) {
  customElements.define('rd-telegram-chats', TelegramChatsTile);
}

export default defineTile('rd-telegram-chats', (el, { api, ctx }) => {
  el.agent = ctx.agent;
  el.api = api;
});
