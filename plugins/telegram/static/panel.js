// Telegram lens — contributed by the telegram plugin.
//
// Surfaces every `relaydeck telegram ...` CLI command in the dashboard.
// Sections, top to bottom:
//
//   1. Status header: bot username, mode, stub reason if any, a health
//      line (last msg / error), and "Verify token" + "Restart".
//   2. Sign-in / Setup card (only when the bot token isn't set yet).
//      Operator pastes the token + an initial allowed user; the
//      plugin writes to the vault and restarts the worker.
//   3. Routes table: add/edit/remove rows, "test" button per row.
//   4. Allowlists: user IDs and group/chat IDs.
//   5. Send: chat picker (from existing routes) + free-form body for
//      one-shot replies / outbound DMs.
//   6. Activity feed: recent inbound + disposition, one-click allow/route.
//   7. Settings (always shown): mode (polling/webhook), webhook URL +
//      secret, mention requirement, reactions, poll timeout.
//
// All edits round-trip through /api/plugins/telegram/* — the same
// endpoints the CLI uses behind the scenes — so anything an operator
// can do in CLI is doable from the web UI.
//
// MIGRATED to the @relaydeck/ui kit (build-less, light-DOM Lit). Keeps the
// framework-neutral mount(container, api, ctx)/unmount() contract and the
// sections() sidebar nav; renders head + body via lit-html into the stable
// .tg-head / .tg-body anchors (the body re-paints on section change + live
// reload — there is no terminal here to disturb). The bespoke .tg-* CSS is
// preserved verbatim and injected once; hand-rolled buttons/confirm/toggle
// dialogs swap to kit primitives where it's a clean substitution.

import {
  html, render, nothing, repeat,
  button, confirm,
} from '@relaydeck/ui';
import { uniqueChats, resolveRouteChatId } from './conversations.js';

// Relative time for a unix-seconds timestamp.
const _ago = (ts) => {
  if (!ts) return '—';
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 60) return `${Math.round(d)}s ago`;
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
};

const STYLE_ID = 'tg-panel-css';
const CSS = `
  /* flex:1 + min-height:0 (NOT height:100%) so this fills the flex-column
     .detail-host AND lets .tg-body actually scroll — height:100% without
     min-height:0 makes the body overflow and get clipped (settings cut off
     at the bottom) instead of scrolling. */
  .tg-wrap{display:flex;flex-direction:column;flex:1;min-height:0;overflow:hidden;background:var(--bg-0)}
  .tg-head{padding:18px 22px 14px;border-bottom:1px solid var(--line-1);flex-shrink:0}
  .tg-head .eyebrow{font-family:var(--f-mono);font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--t-4);margin-bottom:4px}
  .tg-head h1{margin:0;font-family:var(--f-sans);font-size:30px;font-weight:600;letter-spacing:-.025em;line-height:1.05}
  .tg-head .row{display:flex;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap}
  .tg-head .actions{display:flex;gap:6px;margin-left:auto}
  .tg-head .actions button{font:inherit;background:transparent;border:1px solid var(--line-2);color:var(--t-2);border-radius:var(--r-1);padding:3px 10px;font-size:var(--t-xs);font-family:var(--f-mono);cursor:pointer}
  .tg-head .actions button:hover{color:var(--t-1);border-color:var(--line-3)}
  .tg-head .actions button.primary{background:var(--acc);border-color:var(--acc);color:var(--acc-text);font-weight:600}
  .tg-head .actions button.primary:hover{background:var(--acc-d);border-color:var(--acc-d)}
  .tg-stub{padding:10px 14px;background:var(--warn-soft);border:1px solid rgba(251,191,36,.30);border-radius:var(--r-3);color:var(--warn);font-size:var(--t-sm);margin-top:10px;display:flex;align-items:center;gap:10px}
  .tg-stub code{background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc);font-size:var(--t-xs)}
  .tg-stub .sp{flex:1}

  /* Scroll container is a plain block (NOT display:grid). A grid that is
     also a flex:1/min-height:0 flex item has its height resolved by flex
     first, and its auto rows then collapse — the section cards "mush"
     down to a sliver and clip their own content. Keep the scroll on
     .tg-body and do the 2-column layout on the .tg-grid child, whose
     height is its content (matches the github lens's .gh-body/.gh-grid). */
  .tg-body{flex:1;min-height:0;overflow-y:auto;padding:18px 22px}
  .tg-body::-webkit-scrollbar{width:6px}
  .tg-body::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:3px}
  .tg-grid{display:grid;gap:18px;grid-template-columns:1fr 1fr;align-content:start}
  .tg-section{background:var(--bg-1);border:1px solid var(--line-2);border-radius:var(--r-3);overflow:hidden}
  .tg-section.full{grid-column:1 / span 2}
  .tg-section .head{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line-1);gap:10px}
  .tg-section .head .ttl{font-family:var(--f-mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--t-3);display:flex;align-items:center;gap:8px}
  .tg-section .head .pill{background:var(--acc-soft);border:1px solid var(--acc-line);color:var(--acc);padding:1px 6px;border-radius:3px;font-size:9px;text-transform:none;letter-spacing:.04em}
  .tg-section .body{padding:8px 0;min-height:0}
  .tg-section .body.pad{padding:12px 14px}

  .tg-route{display:grid;grid-template-columns:60px 1fr auto;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line-1);font-family:var(--f-mono);font-size:var(--t-xs);min-width:0;align-items:center}
  .tg-route:last-child{border-bottom:0}
  .tg-route .ix{color:var(--t-4);font-size:10px}
  .tg-route .b{min-width:0;display:flex;flex-direction:column;gap:2px}
  .tg-route .b .a{color:var(--t-1);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tg-route .b .a .dim{color:var(--t-3)}
  .tg-route .b .a .acc{color:var(--acc)}
  .tg-route .b .sub{color:var(--t-3);font-size:10px}
  .tg-route .x{display:flex;gap:4px}
  .tg-route .x button{font:inherit;background:transparent;border:1px solid var(--line-2);color:var(--t-2);border-radius:3px;padding:2px 7px;font-size:10px;cursor:pointer}
  .tg-route .x button:hover{color:var(--t-1);border-color:var(--line-3)}
  .tg-route .x button.danger:hover{color:var(--err);border-color:var(--err)}
  .tg-route-visual{grid-column:span 4;display:grid;grid-template-columns:minmax(0,1fr) 32px minmax(0,1fr) 32px minmax(0,1fr);gap:8px;align-items:stretch;margin-bottom:2px}
  .tg-route-visual .node{border:1px solid var(--line-1);background:var(--bg-1);border-radius:5px;padding:8px 10px;min-width:0}
  .tg-route-visual .node .k{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--t-4);margin-bottom:4px}
  .tg-route-visual .node .v{font-size:var(--t-xs);color:var(--t-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tg-route-visual .node .s{font-size:10px;color:var(--t-3);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tg-route-visual .arr{display:flex;align-items:center;justify-content:center;color:var(--acc);font-size:18px}
  @media (max-width: 900px){.tg-route-visual{grid-template-columns:1fr}.tg-route-visual .arr{height:20px;transform:rotate(90deg)}}

  .tg-allow{padding:10px 14px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
  .tg-allow .chip{font-family:var(--f-mono);font-size:var(--t-xxs);background:var(--bg-2);border:1px solid var(--line-2);padding:2px 8px;border-radius:var(--r-1);color:var(--t-2);display:inline-flex;align-items:center;gap:6px}
  .tg-allow .chip .rm{color:var(--t-4);cursor:pointer}
  .tg-allow .chip .rm:hover{color:var(--err)}
  .tg-allow input{font-family:var(--f-mono);font-size:var(--t-xs);background:var(--bg-2);border:1px solid var(--line-2);border-radius:var(--r-1);padding:4px 8px;color:var(--t-1);width:160px}

  .tg-row-form{padding:10px 14px;display:grid;grid-template-columns:repeat(4,1fr);gap:8px;border-top:1px dashed var(--line-1);background:var(--bg-2);font-family:var(--f-mono);font-size:var(--t-xxs)}
  .tg-row-form label{display:flex;flex-direction:column;gap:3px}
  .tg-row-form label span{color:var(--t-4);font-size:9px;letter-spacing:.08em;text-transform:uppercase}
  .tg-row-form input, .tg-row-form select{background:var(--bg-1);border:1px solid var(--line-2);border-radius:var(--r-1);padding:4px 8px;color:var(--t-1);font:inherit;width:100%}
  .tg-row-form .full{grid-column:span 4;display:flex;justify-content:flex-end;gap:6px;padding-top:4px}
  .tg-route-form .span2{grid-column:span 2}
  .tg-route-form .hidden{display:none!important}
  .tg-route-form optgroup{font-style:normal;color:var(--t-3);font-size:9px}

  .tg-empty{padding:24px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center}
  .tg-empty code{background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)}

  .tg-setup{padding:14px;display:grid;gap:10px}
  .tg-setup .lede{font-size:var(--t-sm);color:var(--t-2);line-height:1.55}
  .tg-setup .lede code{background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc);font-size:var(--t-xs)}
  .tg-setup label{display:flex;flex-direction:column;gap:4px;font-family:var(--f-mono);font-size:var(--t-xxs)}
  .tg-setup label span{color:var(--t-4);letter-spacing:.08em;text-transform:uppercase;font-size:9px}
  .tg-setup input{font-family:var(--f-mono);font-size:var(--t-xs);background:var(--bg-2);border:1px solid var(--line-2);border-radius:var(--r-1);padding:6px 10px;color:var(--t-1)}
  .tg-setup .err{display:none;color:var(--err);font-size:var(--t-xxs);font-family:var(--f-mono)}
  .tg-setup .err.on{display:block}
  .tg-setup .actions{display:flex;justify-content:flex-end;gap:6px}
  .tg-block{padding:14px;display:grid;gap:10px;font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-2);line-height:1.5}
  .tg-block .msg{color:var(--err)}
  .tg-block .cmds{display:flex;gap:8px;flex-wrap:wrap}
  .tg-block code{background:var(--bg-2);border:1px solid var(--line-2);padding:3px 7px;border-radius:3px;color:var(--acc)}
  .tg-block .actions{display:flex;justify-content:flex-end}

  .tg-send{padding:14px;display:grid;gap:10px}
  .tg-send select, .tg-send input, .tg-send textarea{font-family:var(--f-mono);font-size:var(--t-xs);background:var(--bg-2);border:1px solid var(--line-2);border-radius:var(--r-1);padding:6px 10px;color:var(--t-1);width:100%}
  .tg-send textarea{font-family:var(--f-sans);font-size:var(--t-sm);min-height:64px;resize:vertical}
  .tg-send .row{display:flex;gap:8px;align-items:center}
  .tg-send .row label{flex:1;display:flex;flex-direction:column;gap:4px;font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-3)}
  .tg-send .footer{display:flex;justify-content:space-between;align-items:center;font-family:var(--f-mono);font-size:10px;color:var(--t-4)}
  .tg-send .footer button{font:inherit;background:var(--acc);border:1px solid var(--acc);color:var(--acc-text);font-weight:600;border-radius:var(--r-1);padding:5px 14px;font-size:var(--t-xs);font-family:var(--f-mono);cursor:pointer}
  .tg-send .footer button:disabled{opacity:.4;cursor:not-allowed}
  .tg-send .status{color:var(--t-3);font-size:var(--t-xxs)}
  .tg-send .status.ok{color:var(--ok)}
  .tg-send .status.err{color:var(--err)}

  .tg-settings{padding:6px 0}
  .tg-settings .row{display:grid;grid-template-columns:180px 1fr auto;gap:14px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line-1)}
  .tg-settings .row:last-child{border-bottom:0}
  .tg-settings .row .lbl{font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-2)}
  .tg-settings .row .desc{color:var(--t-3);font-family:var(--f-mono);font-size:10px;margin-top:3px}
  .tg-settings .ctrl{display:flex;justify-content:flex-end;align-items:center;gap:8px}
  .tg-settings .seg{display:inline-flex;background:var(--bg-2);border:1px solid var(--line-2);border-radius:var(--r-1);padding:1px}
  .tg-settings .seg button{padding:3px 10px;font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-3);border-radius:3px;letter-spacing:.04em;background:transparent;border:0;cursor:pointer}
  .tg-settings .seg button.on{background:var(--acc);color:var(--acc-text);font-weight:600}
  .tg-settings input[type="text"], .tg-settings input[type="number"]{font-family:var(--f-mono);font-size:var(--t-xs);background:var(--bg-2);border:1px solid var(--line-2);border-radius:var(--r-1);padding:4px 8px;color:var(--t-1);min-width:240px}
  .tg-settings .toggle{width:32px;height:18px;border-radius:9px;background:var(--bg-3);border:1px solid var(--line-2);position:relative;transition:background .12s;cursor:pointer}
  .tg-settings .toggle::after{content:"";position:absolute;top:1px;left:1px;width:14px;height:14px;border-radius:50%;background:var(--t-3);transition:transform .14s, background .14s}
  .tg-settings .toggle.on{background:var(--acc);border-color:var(--acc)}
  .tg-settings .toggle.on::after{transform:translateX(14px);background:var(--acc-text)}

  .tg-act{display:grid;grid-template-columns:84px 1fr auto auto;gap:10px;padding:8px 14px;border-bottom:1px solid var(--line-1);align-items:center;font-family:var(--f-mono);font-size:var(--t-xs);min-width:0}
  .tg-act:last-child{border-bottom:0}
  .tg-act .badge{font-size:9px;letter-spacing:.04em;text-transform:uppercase;padding:2px 6px;border-radius:3px;text-align:center;border:1px solid var(--line-2);color:var(--t-3)}
  .tg-act .badge.ok{color:var(--ok);background:var(--ok-soft);border-color:transparent}
  .tg-act .badge.warn{color:var(--warn);background:var(--warn-soft);border-color:transparent}
  .tg-act .badge.err{color:var(--err);background:var(--err-soft);border-color:transparent}
  .tg-act .b{min-width:0;display:flex;flex-direction:column;gap:2px}
  .tg-act .b .a{color:var(--t-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tg-act .b .a .dim{color:var(--t-4)} .tg-act .b .a .acc{color:var(--acc)}
  .tg-act .b .sub{color:var(--t-3);font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tg-act .t{color:var(--t-4);font-size:10px;white-space:nowrap}
  .tg-act .act button{font:inherit;background:var(--acc-soft);border:1px solid var(--acc-line);color:var(--acc);border-radius:3px;padding:2px 8px;font-size:10px;cursor:pointer;white-space:nowrap}
  .tg-act .act button:hover{background:var(--acc-line)}
`;

export default class TelegramLens {
  constructor() {
    this.host = null;
    this.api = null;
    this.status = null;
    this.settings = null;
    this.routes = { allowed_users: [], allowed_groups: [], routes: [] };
    this.connections = [];
    this.convs = [];
    this.activity = [];
    this._sub = null;
    this._section = 'connections';
    this._ctx = null;
    // Per-section transient UI state (inline forms, drafts) — replaces the old
    // imperative querySelector wiring. Each is null/closed until opened.
    this._connForm = null;        // {kind:'add'|'token'|'rename', conn?, status?}
    this._routeForm = null;       // {prefill, catalog, workspaces, agents, draft}
    this._pendingRoutePrefill = null;
    this._settingsDraft = null;   // working copy of editable settings fields
    this._settingsStatus = null;  // {text, tone:''|'ok'|'err'}
    this._sendStatus = null;      // {text, tone, html?}
  }

  // Sidebar navigation (standardized PluginLens contract). Returning sections
  // moves the per-area UI into the left rail so the detail pane shows ONE area
  // at a time instead of one long scroll. Badges reflect live counts.
  sections() {
    const r = this.routes || { routes: [], allowed_users: [], allowed_groups: [] };
    const allow = (r.allowed_users.length + r.allowed_groups.length) || '';
    return [
      { group: 'Bots', items: [
        { id: 'connections', label: 'Connections', icon: 'send', badge: (this.connections || []).length || '' },
        { id: 'conversations', label: 'Conversations', icon: 'message', badge: (this.convs || []).length || '' },
        { id: 'activity', label: 'Activity', icon: 'activity', badge: (this.activity || []).length || '' },
      ] },
      { group: 'Routing', items: [
        { id: 'routes', label: 'Routes', icon: 'git', badge: r.routes.length || '' },
        { id: 'allowlists', label: 'Allowlists', icon: 'vault', badge: allow },
      ] },
      { group: 'Tools', items: [
        { id: 'send', label: 'Send', icon: 'inbox' },
        { id: 'settings', label: 'Settings', icon: 'cog' },
      ] },
    ];
  }

  async mount(container, api, ctx) {
    this.api = api;
    this.host = ctx?.host || null;
    this._ctx = ctx || null;
    this.root = container;
    if (ctx?.section) this._section = ctx.section;
    if (!document.getElementById(STYLE_ID)) {
      const s = document.createElement('style');
      s.id = STYLE_ID;
      s.textContent = CSS;
      document.head.appendChild(s);
    }
    // Stable anchors: lit-html re-renders into [data-head] / [data-body]; the
    // wrapper structure itself never changes.
    container.innerHTML = `
      <div class="tg-wrap">
        <div class="tg-head" data-head></div>
        <div class="tg-body" data-body></div>
      </div>`;
    this._headAnchor = container.querySelector('[data-head]');
    this._bodyAnchor = container.querySelector('[data-body]');
    await this._reload();
    // Host tells us which sidebar section to show.
    ctx?.onSectionChange?.((id) => { this._section = id; this._resetTransient(); this._renderBody(); });
    this._sub = api.events.subscribe((e) => {
      if (!e.type) return;
      if (e.type.startsWith('telegram.') || e.type === 'system.plugin.loaded') {
        this._reload();
      }
    });
  }

  unmount() { this._sub?.(); }

  _resetTransient() {
    this._connForm = null;
    this._routeForm = null;
    this._sendStatus = null;
  }

  async _reload() {
    try {
      const [status, routes, settings, activity, convs] = await Promise.all([
        this.api.getJSON('/api/plugins/telegram/status'),
        this.api.getJSON('/api/plugins/telegram/routes'),
        this.api.getJSON('/api/plugins/telegram/config'),
        this.api.getJSON('/api/plugins/telegram/activity').catch(() => ({ activity: [] })),
        this.api.getJSON('/api/plugins/telegram/conversations').catch(() => ({ conversations: [] })),
      ]);
      this.status = status;
      this.routes = routes;
      this.settings = settings;
      this.activity = (activity && activity.activity) || [];
      this.connections = (status && status.connections) || [];
      this.convs = (convs && convs.conversations) || [];
      // Re-seed the settings draft from server truth (preserves edits only
      // until a reload — same as the old form, which re-read on every paint).
      this._settingsDraft = this._freshSettingsDraft();
    } catch (e) {
      this.status = { error: e.message };
    }
    this._renderHead();
    this._renderBody();
    // Refresh the sidebar nav so badge counts (bots/routes/conversations/…)
    // track live.
    this._ctx?.refreshNav?.();
  }

  // A connection's display label: operator-given name → live @username (once
  // connected) → the bare id. Keeps "default" from leaking into the UI once
  // the bot is up.
  _connLabel(c) {
    if (c.name) return c.name;
    const b = c.bot || {};
    if (b.ready && b.bot_username) return '@' + b.bot_username;
    return c.id;
  }

  _userLabel(id) {
    const n = Number(id);
    const hit = (this.convs || []).find(c => Number(c.chat_id) === n && c.username);
    if (hit) return `@${hit.username} (${n})`;
    const act = (this.activity || []).find(a => Number(a.user_id) === n && a.username);
    if (act) return `@${act.username} (${n})`;
    return `id ${n}`;
  }

  _chatLabel(id) {
    const n = Number(id);
    const hit = (this.convs || []).find(c => Number(c.chat_id) === n);
    if (hit) {
      const name = hit.title || hit.last_user || (hit.username ? `@${hit.username}` : '');
      return name ? `${name} (${n})` : `chat ${n}`;
    }
    const act = (this.activity || []).find(a => Number(a.chat_id) === n);
    if (act?.chat_username) return `@${act.chat_username} (${n})`;
    return `chat ${n}`;
  }

  // ── Head ───────────────────────────────────────────────────────
  _renderHead() {
    if (!this._headAnchor) return;
    render(this._headTemplate(), this._headAnchor);
  }

  _headTemplate() {
    const s = this.status || {};
    const conns = s.connections || (s.bot ? [{ id: 'default', bot: s.bot }] : []);
    const ready = conns.filter(c => (c.bot || {}).ready);
    const multi = conns.length > 1;
    const anyReady = ready.length > 0;
    const bot = (conns[0] || {}).bot || {};
    // Single bot → show its @username; multiple → a neutral title + a count.
    const title = (!multi && bot.bot_username && bot.ready) ? '@' + bot.bot_username : 'Telegram';
    const errs = conns
      .filter(c => (c.bot || {}).last_error || c.stub_reason)
      .map(c => `${c.id}: ${(c.bot || {}).last_error || c.stub_reason}`);
    let stateBadge;
    if (anyReady && errs.length) {
      stateBadge = html`<span class="sbadge awaiting">${multi ? `${ready.length}/${conns.length} bots ready` : 'degraded'}</span>`;
    } else if (anyReady) {
      stateBadge = html`<span class="sbadge running">${multi ? `${ready.length}/${conns.length} bots ready` : 'connected'}</span>`;
    } else if (conns.length === 0) {
      stateBadge = html`<span class="sbadge idle">not configured</span>`;
    } else if (errs.length || s.errored) {
      stateBadge = html`<span class="sbadge errored">error</span>`;
    } else if (s.stub_reason) {
      stateBadge = html`<span class="sbadge errored">not running</span>`;
    } else {
      stateBadge = html`<span class="sbadge idle">starting…</span>`;
    }
    return html`
      <div class="eyebrow">gateway · ${s.mode || 'polling'}</div>
      <h1>${title}</h1>
      <div class="row">
        ${stateBadge}
        ${conns.length ? html`<span class="chip muted">bots ${conns.length}</span>` : nothing}
        <span class="chip muted">routes ${s.routes ?? 0}</span>
        <span class="chip muted">users ${s.allowed_users ?? 0}</span>
        <span class="chip muted">groups ${s.allowed_groups ?? 0}</span>
        ${!multi && bot.last_update_ts
          ? html`<span class="chip muted" title="last inbound update">last msg ${_ago(bot.last_update_ts)}</span>` : nothing}
        ${errs.length
          ? html`<span class="chip" style="color:var(--err);border-color:var(--err)" title=${errs.join('\n')}>${errs.length} error${errs.length > 1 ? 's' : ''}</span>` : nothing}
        <div class="actions">
          <button data-act="verify" ?disabled=${!anyReady} @click=${() => this._verify()}>Verify token${multi ? 's' : ''}</button>
          <button data-act="restart" @click=${() => this._restart()}>Restart</button>
        </div>
      </div>
      ${s.stub_reason ? html`
        <div class="tg-stub">
          <span>${s.stub_reason}</span>
          <span class="sp"></span>
          ${!this.settings?.has_bot_token ? html`<span style="font-size:var(--t-xs)">↓ paste your bot token below</span>` : nothing}
        </div>` : nothing}`;
  }

  // ── Body dispatch ──────────────────────────────────────────────
  _renderBody() {
    if (!this._bodyAnchor) return;
    render(this._bodyTemplate(), this._bodyAnchor);
  }

  _bodyTemplate() {
    const s = this.status || {};
    const hasToken = this._hasTokenConfigured();
    const needsSetup = !hasToken;
    const runtimeBlocked = hasToken && !!s.stub_reason && !s.ready;
    const sec = this._section || 'connections';
    let cards;
    if (sec === 'connections') {
      cards = html`
        ${needsSetup ? this._setupCard() : nothing}
        ${runtimeBlocked ? this._runtimeBlockCard(s.stub_reason) : nothing}
        ${this._connectionsCard()}`;
    } else if (sec === 'routes') {
      cards = this._routesCard();
    } else if (sec === 'conversations') {
      cards = this._conversationsCard();
    } else if (sec === 'allowlists') {
      cards = html`${this._allowCard('users')}${this._allowCard('groups')}`;
    } else if (sec === 'send') {
      cards = needsSetup
        ? this._setupCard()
        : (runtimeBlocked ? this._runtimeBlockCard(s.stub_reason) : this._sendCard());
    } else if (sec === 'activity') {
      cards = this._activityCard();
    } else if (sec === 'settings') {
      cards = this._settingsCard();
    } else {
      cards = nothing;
    }
    // Sections are DIRECT children of .tg-grid so `.tg-section.full` spans work.
    return html`<div class="tg-grid">${cards}</div>`;
  }

  _hasTokenConfigured() {
    const s = this.status || {};
    if (this.settings?.has_bot_token) return true;
    return (s.connections || []).some(c => c.has_token);
  }

  // Switch to the routes section and open the add-route form prefilled
  // (used by "route →" buttons in other sections, which may not have the
  // routes card mounted).
  _routeFrom(prefill) {
    this._pendingRoutePrefill = prefill;
    if (this._ctx?.setSection) this._ctx.setSection('routes');
    else { this._section = 'routes'; this._renderBody(); }
  }

  // ── Setup card ────────────────────────────────────────────────
  _setupCard() {
    const onSave = (e) => {
      const root = e.target.closest('.tg-section');
      this._submitSetup(root);
    };
    return html`
      <div class="tg-section full">
        <div class="head">
          <span class="ttl">Sign in <span class="pill">required</span></span>
        </div>
        <div class="body">
          <div class="tg-setup">
            <div class="lede">
              Create a bot through <code>@BotFather</code> in Telegram
              (<code>/newbot</code> → follow prompts → copy the API token), paste it
              below. You can optionally seed a Telegram user ID who can DM the bot;
              routes added later will allowlist their chat automatically.
              The token is stored in the daemon vault and never shown again.
            </div>
            <label><span>Bot token</span><input data-f="token" type="password" placeholder="123456789:AAH..." autocomplete="off" spellcheck="false"></label>
            <label><span>Allow user ID (optional — find via @userinfobot)</span><input data-f="user" inputmode="numeric" placeholder="e.g. 12345678"></label>
            <div class="err" data-err></div>
            <div class="actions">
              <button class="btn primary" data-act="save" @click=${onSave}>Sign in &amp; restart bot</button>
            </div>
          </div>
        </div>
      </div>`;
  }

  async _submitSetup(section) {
    const errEl = section.querySelector('[data-err]');
    errEl.classList.remove('on');
    const token = section.querySelector('[data-f="token"]').value.trim();
    const userRaw = section.querySelector('[data-f="user"]').value.trim();
    if (!token) {
      errEl.textContent = 'Bot token is required.';
      errEl.classList.add('on');
      return;
    }
    const body = { token };
    if (userRaw) {
      const v = parseInt(userRaw, 10);
      if (!Number.isFinite(v)) {
        errEl.textContent = 'User ID must be a number.';
        errEl.classList.add('on');
        return;
      }
      body.allowed_users = [v];
    }
    try {
      const res = await this.api.postJSON('/api/plugins/telegram/setup', body);
      if (!res.ok) {
        errEl.textContent = 'Token rejected: ' + (res.error || res.status?.stub_reason || 'check the value on the daemon log');
        errEl.classList.add('on');
      }
    } catch (e) {
      errEl.textContent = 'Setup failed: ' + e.message;
      errEl.classList.add('on');
      return;
    }
    await this._reload();
  }

  _runtimeBlockCard(reason) {
    return html`
      <div class="tg-section full">
        <div class="head">
          <span class="ttl">Runtime blocked <span class="pill">action needed</span></span>
        </div>
        <div class="body">
          <div class="tg-block">
            <div class="msg">${reason || 'Telegram worker is not ready.'}</div>
            <div class="actions">
              <button class="btn primary" @click=${() => this._restart()}>Restart after fixing</button>
            </div>
          </div>
        </div>
      </div>`;
  }

  // ── Connections card (the bots) ───────────────────────────────
  _connectionsCard() {
    const conns = this.connections || [];
    return html`
      <div class="tg-section full">
        <div class="head">
          <span class="ttl">Connections <span class="pill">${conns.length}</span></span>
          ${button({ variant: 'ghost', size: 'sm', act: 'add-conn', onClick: () => this._openConnForm({ kind: 'add' }) }, '+ Add bot')}
        </div>
        <div class="body">
          ${!conns.length
            ? html`<div class="tg-empty">No bots yet. Click <b>+ Add bot</b> and paste a token from <code>@BotFather</code>. Each bot is one Telegram getUpdates poller — add several to manage multiple bots from one place.</div>`
            : conns.map((c) => this._connRow(c))}
        </div>
        ${this._connForm ? this._connFormTemplate() : nothing}
      </div>`;
  }

  _connRow(c) {
    const bot = c.bot || {};
    const ready = bot.bot_username && bot.ready;
    // Display identity (see _connLabel): operator name → live @username →
    // id, so the implicit "default" shows the real bot once connected.
    const ident = this._connLabel(c);
    const state = ready
      ? html`<span class="acc">connected</span>`
      : ((bot.last_error || c.stub_reason)
          ? html`<span style="color:var(--err)">${bot.last_error || c.stub_reason}</span>`
          : html`<span class="dim">starting…</span>`);
    const tok = c.has_token
      ? html`<span style="color:var(--ok)">token set</span>`
      : html`<span style="color:var(--err)">token missing</span>`;
    // When the operator set a custom name, still surface the real @username
    // (and the id handle) so the actual bot identity stays visible.
    const tag = ready && c.name && ('@' + bot.bot_username) !== c.name
      ? html` <span class="dim" style="font-family:var(--mono);font-size:var(--t-xxs)">@${bot.bot_username}</span>` : nothing;
    // Explicit connections drop their YAML row (token kept); the implicit
    // default has no row, so removing it clears its vault token instead.
    const rmLabel = c.explicit ? 'remove' : 'delete';
    const rmTitle = c.explicit
      ? 'Remove this bot (its token stays in the vault).'
      : 'Delete the default bot — clears its vault token and stops polling.';
    return html`
      <div class="tg-route">
        <span class="ix">${ready ? '●' : '○'}</span>
        <div class="b">
          <span class="a"><b>${ident}</b>${tag} <span class="dim">→</span> ${state}</span>
          <span class="sub">${tok} <span class="dim">·</span> vault: ${c.token_vault_key || ''}${c.enabled === false ? ' · disabled' : ''}</span>
        </div>
        <div class="x">
          <button data-act="rename" @click=${() => this._openConnForm({ kind: 'rename', conn: c })}>rename</button>
          <button data-act="token" @click=${() => this._openConnForm({ kind: 'token', conn: c })}>${c.has_token ? 'rotate token' : 'add token'}</button>
          <button class="danger" data-act="rm" title=${rmTitle} @click=${() => this._removeConnection(c)}>${rmLabel}</button>
        </div>
      </div>`;
  }

  _openConnForm(form) {
    this._connForm = { ...form, status: null, statusTone: '' };
    this._renderBody();
  }

  _connFormTemplate() {
    const f = this._connForm;
    const status = f.status
      ? html`<span class="status" data-status style="flex:1;font-size:var(--t-xxs);color:${f.statusTone === 'err' ? 'var(--err)' : 'var(--t-3)'}">${f.status}</span>`
      : html`<span class="status" data-status style="flex:1;color:var(--t-3);font-size:var(--t-xxs)">${f.kind === 'token'
          ? 'The token is verified, stored in the vault, and never shown.'
          : f.kind === 'rename' ? 'Cosmetic only — the bot keeps polling, no restart.' : ''}</span>`;
    if (f.kind === 'add') {
      return html`
        <div class="tg-row-form">
          <label><span>id</span><input data-f="id" placeholder="ops"></label>
          <label><span>name (optional)</span><input data-f="name" placeholder="Ops bot"></label>
          <label style="grid-column:span 2"><span>bot token (from @BotFather)</span><input data-f="token" type="password" placeholder="123456:AAH…" autocomplete="off"></label>
          <div class="full">
            ${status}
            <button class="btn ghost sm" data-act="cancel" @click=${() => this._closeConnForm()}>Cancel</button>
            <button class="btn primary sm" data-act="save" @click=${(e) => this._submitAddConn(e)}>Verify &amp; add</button>
          </div>
        </div>`;
    }
    if (f.kind === 'token') {
      const c = f.conn;
      return html`
        <div class="tg-row-form">
          <label style="grid-column:span 4"><span>${c.has_token ? 'rotate' : 'set'} token for <b>${c.id}</b> → vault ${c.token_vault_key}</span>
            <input data-f="token" type="password" placeholder="123456:AAH… (verified via getMe)" autocomplete="off"></label>
          <div class="full">
            ${status}
            <button class="btn ghost sm" data-act="cancel" @click=${() => this._closeConnForm()}>Cancel</button>
            <button class="btn primary sm" data-act="save" @click=${(e) => this._submitEditToken(e)}>Verify &amp; save</button>
          </div>
        </div>`;
    }
    // rename
    const c = f.conn;
    return html`
      <div class="tg-row-form">
        <label style="grid-column:span 4"><span>display name for <b>${c.id}</b></span>
          <input data-f="name" .value=${c.name || ''} placeholder="e.g. Ops bot" autocomplete="off"
            @keydown=${(e) => { if (e.key === 'Enter') this._submitRename(e); }}></label>
        <div class="full">
          ${status}
          <button class="btn ghost sm" data-act="cancel" @click=${() => this._closeConnForm()}>Cancel</button>
          <button class="btn primary sm" data-act="save" @click=${(e) => this._submitRename(e)}>Save</button>
        </div>
      </div>`;
  }

  _closeConnForm() { this._connForm = null; this._renderBody(); }

  _connFormStatus(text, tone = '') {
    if (this._connForm) { this._connForm.status = text; this._connForm.statusTone = tone; this._renderBody(); }
  }

  _connFormField(e, name) {
    const form = e.target.closest('.tg-row-form');
    return (form?.querySelector(`[data-f="${name}"]`)?.value || '').trim();
  }

  async _submitAddConn(e) {
    const id = this._connFormField(e, 'id');
    const name = this._connFormField(e, 'name');
    const token = this._connFormField(e, 'token');
    if (!id || !token) { this._connFormStatus('id and token required', 'err'); return; }
    e.target.disabled = true;
    this._connFormStatus('verifying…', '');
    try {
      const res = await this.api.postJSON('/api/plugins/telegram/connections', { id, name, token });
      if (res.ok) { this._connForm = null; await this._reload(); }
      else this._connFormStatus('✗ ' + (res.error || 'failed'), 'err');
    } catch (err) { this._connFormStatus('✗ ' + err.message, 'err'); }
  }

  async _submitEditToken(e) {
    const c = this._connForm.conn;
    const token = this._connFormField(e, 'token');
    if (!token) { this._connFormStatus('token required', 'err'); return; }
    e.target.disabled = true;
    this._connFormStatus('verifying…', '');
    try {
      // Re-POST with the SAME id + existing vault key → upserts the token
      // (and verifies it) without changing where it's stored.
      const res = await this.api.postJSON('/api/plugins/telegram/connections',
        { id: c.id, name: c.name || '', token, token_vault_key: c.token_vault_key });
      if (res.ok) { this._connForm = null; await this._reload(); }
      else this._connFormStatus('✗ ' + (res.error || 'failed'), 'err');
    } catch (err) { this._connFormStatus('✗ ' + err.message, 'err'); }
  }

  async _submitRename(e) {
    const c = this._connForm.conn;
    const name = this._connFormField(e, 'name');
    this._connFormStatus('saving…', '');
    try {
      const r = await this.api.fetch(`/api/plugins/telegram/connections/${encodeURIComponent(c.id)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const res = await r.json();
      if (res.ok) { this._connForm = null; await this._reload(); }
      else this._connFormStatus('✗ ' + (res.error || 'failed'), 'err');
    } catch (err) { this._connFormStatus('✗ ' + err.message, 'err'); }
  }

  async _removeConnection(conn) {
    const label = conn.name || conn.id;
    const body = conn.explicit
      ? `Remove Telegram bot "${label}"? Its token stays in the vault; the bot stops polling.`
      : `Delete the default Telegram bot? Its token will be cleared from the vault and the bot stops polling.`;
    const ok = await confirm({
      title: conn.explicit ? `Remove ${label}?` : 'Delete default bot?',
      body, danger: true, confirmLabel: conn.explicit ? 'Remove' : 'Delete',
    });
    if (!ok) return;
    try {
      const res = await this.api.deleteJSON(`/api/plugins/telegram/connections/${encodeURIComponent(conn.id)}`);
      if (res && res.ok === false) { alert('Remove failed: ' + (res.error || 'unknown error')); return; }
      await this._reload();
    } catch (e) { alert('Remove failed: ' + e.message); }
  }

  // ── Conversations card (global registry) ──────────────────────
  _conversationsCard() {
    const convs = this.convs || [];
    const ICON = { private: '👤', group: '👥', supergroup: '👥', channel: '📣' };
    return html`
      <div class="tg-section full">
        <div class="head">
          <span class="ttl">Conversations <span class="pill">${convs.length}</span></span>
          <span style="display:flex;align-items:center;gap:8px">
            <span style="font-family:var(--f-mono);font-size:10px;color:var(--t-4)">discovered from inbound · route any to an agent</span>
            ${convs.length
              ? button({ variant: 'ghost', size: 'sm', danger: true, act: 'forget-all', onClick: () => this._forgetAllConversations() }, 'Forget all')
              : nothing}
          </span>
        </div>
        <div class="body">
          ${!convs.length
            ? html`<div class="tg-empty">No conversations yet. The Bot API can't list chats — they appear here as people message your bots. Then click <b>route →</b> to wire one to an agent.</div>`
            : convs.map((c) => {
                const handle = c.username ? html` <span class="dim">@${c.username}</span>` : nothing;
                return html`
                  <div class="tg-act">
                    <span class="badge">${ICON[c.chat_type] || '•'} ${c.chat_type || ''}</span>
                    <div class="b">
                      <span class="a">${c.title || c.last_user || c.chat_id}${handle} <span class="dim">· ${c.connection_id}:${this._chatLabel(c.chat_id)}</span></span>
                      <span class="sub">${c.message_count} msg${c.last_disposition ? ' · ' + c.last_disposition : ''}</span>
                    </div>
                    <span class="t">${c.last_activity ? _ago(c.last_activity) : ''}</span>
                    <span class="act">
                      <button data-act="route" @click=${() => this._routeFrom({
                        connection: c.connection_id, chat_id: c.chat_id,
                        thread_id: (c.thread_ids && c.thread_ids[0]) || null,
                      })}>route →</button>
                      <button class="danger" data-act="forget" title="Forget this conversation"
                        @click=${() => this._forgetConversation(c)}>✕</button>
                    </span>
                  </div>`;
              })}
        </div>
      </div>`;
  }

  async _forgetConversation(c) {
    try {
      const res = await this.api.deleteJSON(
        `/api/plugins/telegram/conversations/${encodeURIComponent(c.connection_id)}/${encodeURIComponent(c.chat_id)}`);
      if (res && res.ok === false) { alert('Forget failed: ' + (res.error || 'unknown error')); return; }
      await this._reload();
    } catch (e) { alert('Forget failed: ' + e.message); }
  }

  async _forgetAllConversations() {
    const ok = await confirm({
      title: 'Forget all conversations?',
      body: 'Clears the discovered-chat registry. Chats reappear as people message your bots again. Routes are not affected.',
      danger: true, confirmLabel: 'Forget all',
    });
    if (!ok) return;
    try {
      const res = await this.api.deleteJSON('/api/plugins/telegram/conversations');
      if (res && res.ok === false) { alert('Purge failed: ' + (res.error || 'unknown error')); return; }
      await this._reload();
    } catch (e) { alert('Purge failed: ' + e.message); }
  }

  // ── Routes card ────────────────────────────────────────────────
  _routesCard() {
    // Honor a pending prefill (arriving from another section's "route →").
    if (this._pendingRoutePrefill) {
      const pf = this._pendingRoutePrefill;
      this._pendingRoutePrefill = null;
      void this._openRouteForm(pf);
    }
    const routes = this.routes.routes;
    return html`
      <div class="tg-section full">
        <div class="head">
          <span class="ttl">Routes <span class="pill">${routes.length}</span></span>
          ${button({ variant: 'ghost', size: 'sm', act: 'new-route', onClick: () => this._openRouteForm() }, '+ Add route')}
        </div>
        <div class="body">
          ${routes.length === 0
            ? html`<div class="tg-empty">No routes. Click <b>+ Add route</b> to wire a Telegram chat, command, or topic to a workspace agent.</div>`
            : routes.map((r, i) => this._routeRow(r, i))}
        </div>
        ${this._routeForm ? this._routeFormTemplate() : nothing}
      </div>`;
  }

  _routeRow(r, i) {
    const chat = r.chat_id == null ? html`<span class="dim">— any chat —</span>` : this._chatLabel(r.chat_id);
    const topic = r.thread_id != null ? html` <span class="dim">#</span>${r.thread_id}` : nothing;
    const cmd = r.command ? html` <span class="dim">/</span>${r.command}` : nothing;
    const agent = r.agent ? html`<span class="acc">${r.agent}</span>` : html`<span class="dim">(broadcast)</span>`;
    const conn = r.connection ? html`<span class="dim">[${r.connection}]</span> ` : nothing;
    return html`
      <div class="tg-route">
        <span class="ix">#${i}</span>
        <div class="b">
          <span class="a">${conn}${chat}${topic}${cmd} <span class="dim">→</span> @${r.workspace} ${agent}</span>
          <span class="sub">direction: ${r.direction || 'in+out'}${r.require_mention != null ? ` · mention: ${r.require_mention ? 'required' : 'relaxed'}` : ''}</span>
        </div>
        <div class="x">
          <button data-act="test" @click=${() => this._testRoute(r)}>test</button>
          <button data-act="edit" @click=${() => this._openRouteForm(r, i)}>edit</button>
          <button class="danger" data-act="rm" @click=${() => this._removeRoute(i)}>remove</button>
        </div>
      </div>`;
  }

  // Chats eligible for routing: conversations + allowlists + recent activity.
  _chatCatalog() {
    const byKey = new Map();
    const merge = (entry) => {
      const k = `${entry.connection || ''}:${entry.chat_id}`;
      const prev = byKey.get(k);
      if (!prev) {
        byKey.set(k, { ...entry, thread_ids: [...(entry.thread_ids || [])] });
        return;
      }
      for (const t of (entry.thread_ids || [])) {
        if (!prev.thread_ids.includes(t)) prev.thread_ids.push(t);
      }
    };
    for (const c of (this.convs || [])) {
      const who = c.title || c.last_user || String(c.chat_id);
      merge({
        connection: c.connection_id || '',
        chat_id: c.chat_id,
        thread_ids: c.thread_ids || [],
        label: who + (c.username ? ` @${c.username}` : ''),
        group: 'Conversations',
      });
    }
    for (const uid of (this.routes?.allowed_users || [])) {
      merge({
        connection: '', chat_id: uid, thread_ids: [],
        label: `allowed user ${this._userLabel(uid)}`, group: 'Allowlisted users',
      });
    }
    for (const gid of (this.routes?.allowed_groups || [])) {
      merge({
        connection: '', chat_id: gid, thread_ids: [],
        label: `allowed group ${this._chatLabel(gid)}`, group: 'Allowlisted groups',
      });
    }
    for (const a of (this.activity || [])) {
      if (a.chat_id == null) continue;
      merge({
        connection: a.connection_id || '',
        chat_id: a.chat_id,
        thread_ids: a.thread_id != null ? [a.thread_id] : [],
        label: `${a.username ? '@' + a.username : 'id ' + a.user_id} · ${a.chat_id}`,
        group: 'Recent activity',
      });
    }
    return [...byKey.values()].sort((a, b) => a.label.localeCompare(b.label));
  }

  _commandChoices() {
    const seen = new Set();
    const out = [{ v: '', l: '— none —' }];
    const add = (cmd) => {
      const c = String(cmd || '').replace(/^\//, '').trim();
      if (!c || seen.has(c)) return;
      seen.add(c);
      out.push({ v: c, l: `/${c}` });
    };
    for (const r of (this.routes?.routes || [])) add(r.command);
    for (const a of (this.activity || [])) add(a.command);
    out.push({ v: '__custom__', l: 'Custom command…' });
    return out;
  }

  _chatPickerKey(connection, chat_id) {
    return `${connection || ''}|${chat_id}`;
  }

  // Open the add-route form, loading workspaces + agents first. Drives a
  // reactive draft (this._routeForm) instead of imperative querySelector wiring.
  async _openRouteForm(prefill = {}, editIndex = null) {
    const catalog = this._chatCatalog();
    const [workspaces, agents] = await Promise.all([
      this.api.getJSON('/api/workspaces').catch(() => []),
      this.api.getJSON('/api/agents').catch(() => []),
    ]);
    // Derive the chat-picker selection from the prefill (mirrors the old
    // _chatPickerOptionsHtml selected-detection + applyChatPick logic).
    const hasChatProp = Object.prototype.hasOwnProperty.call(prefill, 'chat_id');
    let chatPick = '';
    if (prefill.chat_id != null) {
      const hit = catalog.find(c =>
        Number(c.chat_id) === Number(prefill.chat_id)
        && String(c.connection || '') === String(prefill.connection || ''));
      chatPick = hit ? this._chatPickerKey(hit.connection, hit.chat_id) : '__custom__';
    } else if (hasChatProp) {
      chatPick = '__any__';
    }
    // Command pick: a known choice → its value; an unknown one → custom.
    let cmdPick = '';
    let cmdCustom = '';
    if (prefill.command) {
      const known = this._commandChoices().some(c => c.v === prefill.command);
      if (known) cmdPick = prefill.command;
      else { cmdPick = '__custom__'; cmdCustom = prefill.command; }
    }
    let requireMention = '';
    if (prefill.require_mention === true) requireMention = 'true';
    else if (prefill.require_mention === false) requireMention = 'false';

    this._routeForm = {
      catalog, workspaces: workspaces || [], agents: agents || [], prefill, editIndex,
      draft: {
        chatPick,
        connection: chatPick === '__any__' ? '' : (prefill.connection || ''),
        chatId: prefill.chat_id != null ? String(prefill.chat_id) : '',
        threadPick: prefill.thread_id != null ? String(prefill.thread_id) : '',
        threadManual: '',
        cmdPick, cmdCustom,
        direction: prefill.direction || 'in+out',
        workspace: prefill.workspace || '',
        agent: prefill.agent || '',
        requireMention,
      },
    };
    this._renderBody();
  }

  _closeRouteForm() { this._routeForm = null; this._renderBody(); }

  // Catalog row currently selected in the chat picker (null for any/custom).
  _routePickedRow() {
    const f = this._routeForm; if (!f) return null;
    const v = f.draft.chatPick;
    if (!v || v === '__any__' || v === '__custom__') return null;
    return f.catalog.find(c => this._chatPickerKey(c.connection, c.chat_id) === v) || null;
  }

  _routeThreadIds() {
    const row = this._routePickedRow();
    return row ? (row.thread_ids || []) : [];
  }

  _routeAgentPool() {
    const f = this._routeForm; if (!f) return [];
    const ws = f.draft.workspace;
    return (f.agents || []).filter(a => !ws || (a.workspace || '') === ws);
  }

  _routePreviewTemplate() {
    const f = this._routeForm;
    if (!f) return nothing;
    const d = f.draft;
    const picked = this._routePickedRow();
    const chat = d.chatPick === '__any__'
      ? 'Any chat'
      : (picked ? picked.label : (d.chatId ? `chat ${d.chatId}` : 'Select a chat'));
    const topic = d.threadPick === '__custom__'
      ? (d.threadManual ? `topic #${d.threadManual}` : 'custom topic')
      : (d.threadPick ? `topic #${d.threadPick}` : 'any topic');
    const command = d.cmdPick === '__custom__'
      ? (d.cmdCustom ? `/${d.cmdCustom.replace(/^\//, '')}` : 'custom command')
      : (d.cmdPick ? `/${d.cmdPick}` : 'any message');
    const bot = d.connection
      ? (this.connections || []).find(c => c.id === d.connection)
      : null;
    const botLabel = bot ? this._connLabel(bot) : (d.connection ? d.connection : 'any bot');
    const target = d.workspace
      ? `${d.workspace}${d.agent ? ' / ' + d.agent : ' / all agents'}`
      : 'Select workspace';
    const dir = d.direction || 'in+out';
    return html`
      <div class="tg-route-visual">
        <div class="node">
          <div class="k">From Telegram</div>
          <div class="v">${chat}</div>
          <div class="s">${botLabel}${topic !== 'any topic' ? ' · ' + topic : ''}</div>
        </div>
        <div class="arr">→</div>
        <div class="node">
          <div class="k">Match</div>
          <div class="v">${command}</div>
          <div class="s">${dir}</div>
        </div>
        <div class="arr">→</div>
        <div class="node">
          <div class="k">Relaydeck</div>
          <div class="v">${target}</div>
          <div class="s">${d.requireMention === 'true' ? 'mention required' : d.requireMention === 'false' ? 'mention relaxed' : 'mention inherits default'}</div>
        </div>
      </div>`;
  }

  _routeFormTemplate() {
    const f = this._routeForm;
    const d = f.draft;
    const set = (k) => (e) => { d[k] = e.target.value; this._renderBody(); };
    const setQuiet = (k) => (e) => { d[k] = e.target.value; }; // no re-render (text typing)
    // Picking a chat must carry the chosen row's chat_id + connection into the
    // draft (parity with the old applyChatPick). Without this the submit reads
    // the hidden/empty manual chat_id field and saves a chat_id=null wildcard.
    const onChatPick = (e) => {
      d.chatPick = e.target.value;
      if (d.chatPick === '__any__') { d.chatId = ''; d.connection = ''; }
      else if (d.chatPick && d.chatPick !== '__custom__') {
        const row = this._routePickedRow();
        if (row) { d.chatId = String(row.chat_id); d.connection = row.connection || ''; }
      }
      this._renderBody();
    };

    // Chat picker options (grouped), mirroring _chatPickerOptionsHtml.
    const groups = new Map();
    for (const c of f.catalog) {
      const g = c.group || 'Chats';
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(c);
    }
    const chatCustomOn = d.chatPick === '__custom__';
    // Thread select: option for each thread of the picked row + a custom
    // override; if the prefill thread isn't among them, add a sticky option.
    const threadIds = this._routeThreadIds();
    const threadCustomOn = d.threadPick === '__custom__';
    const prefillThread = f.prefill.thread_id;
    const extraThread = (prefillThread != null && !threadIds.map(Number).includes(Number(prefillThread)))
      ? Number(prefillThread) : null;
    const cmdCustomOn = d.cmdPick === '__custom__';
    const pool = this._routeAgentPool();
    const stickyAgent = (d.agent && !pool.some(a => a.id === d.agent)) ? d.agent : null;

    return html`
      <div class="tg-row-form tg-route-form">
        ${this._routePreviewTemplate()}
        <label class="span2"><span>chat</span>
          <select data-f="chat_pick" .value=${d.chatPick} @change=${onChatPick}>
            <option value="" ?selected=${d.chatPick === ''}>— select chat —</option>
            <option value="__any__" ?selected=${d.chatPick === '__any__'}>— any chat (wildcard) —</option>
            ${[...groups.entries()].map(([g, items]) => html`
              <optgroup label=${g}>
                ${items.map((c) => {
                  const v = this._chatPickerKey(c.connection, c.chat_id);
                  return html`<option value=${v} ?selected=${d.chatPick === v}>${c.label} (${c.chat_id})</option>`;
                })}
              </optgroup>`)}
            <option value="__custom__" ?selected=${d.chatPick === '__custom__'}>Custom chat_id…</option>
          </select>
        </label>
        <label class=${chatCustomOn ? '' : 'hidden'} data-f="chat_custom"><span>chat_id (manual)</span>
          <input data-f="chat_id" inputmode="numeric" placeholder="-100… or user_id"
            .value=${d.chatId} @input=${setQuiet('chatId')}>
        </label>
        <label><span>connection (bot)</span>
          <select data-f="connection" .value=${d.connection} @change=${set('connection')}>
            <option value="">any bot</option>
            ${(this.connections || []).map((c) => html`<option value=${c.id} ?selected=${d.connection === c.id}>${this._connLabel(c)}</option>`)}
          </select>
        </label>
        <label><span>thread / topic</span>
          <select data-f="thread_id" .value=${d.threadPick} @change=${set('threadPick')}>
            <option value="" ?selected=${d.threadPick === ''}>— no topic —</option>
            <option value="__custom__" ?selected=${d.threadPick === '__custom__'}>Custom thread_id…</option>
            ${threadIds.map((t) => html`<option value=${t} ?selected=${Number(d.threadPick) === Number(t)}>topic #${t}</option>`)}
            ${extraThread != null ? html`<option value=${extraThread} ?selected=${Number(d.threadPick) === Number(extraThread)}>topic #${extraThread}</option>` : nothing}
          </select>
        </label>
        <label class=${threadCustomOn ? '' : 'hidden'} data-f="thread_custom"><span>thread_id (manual)</span>
          <input data-f="thread_manual" inputmode="numeric" placeholder="forum topic id"
            .value=${d.threadManual} @input=${setQuiet('threadManual')}>
        </label>
        <label><span>command</span>
          <select data-f="command_pick" .value=${d.cmdPick} @change=${set('cmdPick')}>
            ${this._commandChoices().map((c) => html`<option value=${c.v} ?selected=${d.cmdPick === c.v}>${c.l}</option>`)}
          </select>
        </label>
        <label class=${cmdCustomOn ? '' : 'hidden'} data-f="command_custom"><span>command (manual)</span>
          <input data-f="command" placeholder="triage (no slash)" .value=${d.cmdCustom} @input=${setQuiet('cmdCustom')}>
        </label>
        <label><span>direction</span>
          <select data-f="direction" .value=${d.direction} @change=${set('direction')}>
            <option ?selected=${d.direction === 'in+out'}>in+out</option>
            <option ?selected=${d.direction === 'in'}>in</option>
            <option ?selected=${d.direction === 'out'}>out</option>
          </select>
        </label>
        <label><span>workspace</span>
          <select data-f="workspace" .value=${d.workspace} @change=${(e) => { d.workspace = e.target.value; d.agent = ''; this._renderBody(); }}>
            <option value="" ?selected=${d.workspace === ''}>— select workspace —</option>
            ${(f.workspaces || []).map((w) => html`<option value=${w.name} ?selected=${d.workspace === w.name}>${w.name}</option>`)}
          </select>
        </label>
        <label><span>agent</span>
          <select data-f="agent" .value=${d.agent} @change=${set('agent')}>
            <option value="" ?selected=${d.agent === ''}>— broadcast to workspace —</option>
            ${pool.map((a) => html`<option value=${a.id} ?selected=${d.agent === a.id}>${a.id}</option>`)}
            ${stickyAgent ? html`<option value=${stickyAgent} selected>${stickyAgent}</option>` : nothing}
          </select>
        </label>
        <label><span>require mention (groups)</span>
          <select data-f="require_mention" .value=${d.requireMention} @change=${set('requireMention')}>
            <option value="" ?selected=${d.requireMention === ''}>inherit default</option>
            <option value="true" ?selected=${d.requireMention === 'true'}>required</option>
            <option value="false" ?selected=${d.requireMention === 'false'}>relaxed (any msg)</option>
          </select>
        </label>
        <div class="full">
          <button class="btn ghost sm" data-act="cancel" @click=${() => this._closeRouteForm()}>Cancel</button>
          <button class="btn primary sm" data-act="save" @click=${() => this._submitRoute()}>${f.editIndex != null ? 'Save route' : 'Add route'}</button>
        </div>
      </div>`;
  }

  _submitRoute() {
    const form = this._routeForm;
    const d = form.draft;
    // chat_id resolution lives in conversations.js (node-tested) — when a real
    // chat is picked the catalog row is authoritative, not the hidden manual field.
    const resolved = resolveRouteChatId(d, this._routePickedRow());
    if (resolved.missing) { alert('chat_id is required (or pick a chat from the list)'); return; }
    const chat_id = resolved.chat_id;
    let thread_id = null;
    const tPick = d.threadPick;
    if (tPick === '__custom__') {
      const v = parseInt(d.threadManual, 10);
      thread_id = Number.isFinite(v) ? v : null;
    } else if (tPick) {
      const v = parseInt(tPick, 10);
      thread_id = Number.isFinite(v) ? v : null;
    }
    const ws = d.workspace;
    if (!ws) { alert('workspace is required'); return; }
    let command = null;
    if (d.cmdPick === '__custom__') command = (d.cmdCustom || '').replace(/^\//, '') || null;
    else if (d.cmdPick) command = d.cmdPick;
    const row = {
      workspace: ws,
      connection: d.connection || null,
      chat_id,
      thread_id,
      command,
      agent: d.agent || null,
      direction: d.direction || 'in+out',
    };
    if (d.requireMention === 'true') row.require_mention = true;
    else if (d.requireMention === 'false') row.require_mention = false;
    if (form.editIndex != null && form.editIndex >= 0 && form.editIndex < this.routes.routes.length) {
      this.routes.routes.splice(form.editIndex, 1, row);
    } else {
      this.routes.routes.push(row);
    }
    if (Number.isFinite(chat_id)) {
      const key = chat_id < 0 ? 'allowed_groups' : 'allowed_users';
      if (!this.routes[key].includes(chat_id)) this.routes[key].push(chat_id);
    }
    this._routeForm = null;
    this._save();
  }

  // ── Activity card (monitoring + onboarding) ───────────────────
  _activityCard() {
    const acts = this.activity || [];
    const DISP = {
      routed:           { label: 'routed',       cls: 'ok'   },
      unrouted:         { label: 'no route',     cls: 'warn' },
      rejected_user:    { label: 'user blocked', cls: 'err'  },
      rejected_group:   { label: 'chat blocked', cls: 'err'  },
      rejected_mention: { label: 'no mention',   cls: 'warn' },
    };
    return html`
      <div class="tg-section full">
        <div class="head">
          <span class="ttl">Activity <span class="pill">${acts.length}</span></span>
          <span style="font-family:var(--f-mono);font-size:10px;color:var(--t-4)">live · last ${acts.length} inbound</span>
        </div>
        <div class="body" data-acts>
          ${!acts.length
            ? html`<div class="tg-empty">No inbound messages yet. <b>Message your bot on Telegram</b> — every message shows here with what happened to it, and you can <b>allow / route</b> the sender in one click.</div>`
            : repeat(acts, (a, i) => `${a.ts}:${a.chat_id}:${i}`, (a) => this._activityRow(a, DISP))}
        </div>
      </div>`;
  }

  _activityRow(a, DISP) {
    const d = DISP[a.disposition] || { label: a.disposition, cls: 'muted' };
    const where = html`${a.chat_type || 'chat'} ${this._chatLabel(a.chat_id)}${
      a.thread_id != null ? html` <span class="dim">#</span>${a.thread_id}` : nothing}${
      a.command ? html` <span class="dim">/</span>${a.command}` : nothing}`;
    const who = a.username ? `@${a.username} (${a.user_id})` : `id ${a.user_id}`;
    const tail = a.disposition === 'routed' && (a.target || []).length
      ? html`<span class="acc">→ ${a.target.join(', ')}</span>` : nothing;
    // The one-click onboarding affordance per disposition.
    let action = nothing;
    if (a.disposition === 'rejected_user') {
      action = html`<button data-act="allow-user" @click=${() => this._addAllow('users', a.user_id)}>+ allow user</button>`;
    } else if (a.disposition === 'rejected_group') {
      action = html`<button data-act="allow-group" @click=${() => this._addAllow('groups', a.chat_id)}>+ allow chat</button>`;
    } else if (a.disposition === 'unrouted' || a.disposition === 'rejected_mention') {
      action = html`<button data-act="route" @click=${() => this._routeFrom({
        connection: a.connection_id, chat_id: a.chat_id,
        thread_id: a.thread_id, command: a.command,
      })}>+ route</button>`;
    }
    return html`
      <div class="tg-act">
        <span class="badge ${d.cls}">${d.label}</span>
        <div class="b">
          <span class="a">${who} <span class="dim">·</span> ${where} ${tail}</span>
          <span class="sub">${a.preview ? a.preview : html`<span class="dim">(no text)</span>`}</span>
        </div>
        <span class="t">${_ago(a.ts)}</span>
        <span class="act">${action}</span>
      </div>`;
  }

  // ── Allow card ────────────────────────────────────────────────
  _allowCard(kind) {
    const list = kind === 'users' ? this.routes.allowed_users : this.routes.allowed_groups;
    const onAdd = (e) => {
      const input = e.target.closest('.tg-allow').querySelector('input[data-add]');
      const v = parseInt(input.value, 10);
      if (Number.isFinite(v)) this._addAllow(kind, v);
    };
    return html`
      <div class="tg-section">
        <div class="head">
          <span class="ttl">Allowed ${kind} <span class="pill">${list.length}</span></span>
        </div>
        <div class="body tg-allow" data-list>
          ${list.map((id) => html`<span class="chip">${kind === 'users' ? this._userLabel(id) : this._chatLabel(id)}
            <span class="rm" title="remove" @click=${() => this._removeAllow(kind, id)}>✕</span></span>`)}
          <span style="display:flex;gap:6px;align-items:center">
            <input data-add type="text" inputmode="numeric"
              placeholder=${kind === 'users' ? 'user_id' : 'chat_id (neg for groups)'}
              @keydown=${(e) => { if (e.key === 'Enter') onAdd(e); }}>
            <button class="btn ghost sm" data-act="add" @click=${onAdd}>+</button>
          </span>
        </div>
      </div>`;
  }

  // ── Send card ─────────────────────────────────────────────────
  _sendCard() {
    const chats = this._uniqueChats();
    const conns = this.connections || [];
    const onPick = (e) => {
      const v = e.target.value;
      if (!v) return;
      const [conn, c, t] = v.split('|');
      const root = e.target.closest('.tg-send');
      if (conn) root.querySelector('[data-f="connection"]').value = conn;
      root.querySelector('[data-f="chat_id"]').value = c;
      root.querySelector('[data-f="thread_id"]').value = t || '';
    };
    const st = this._sendStatus;
    return html`
      <div class="tg-section full">
        <div class="head">
          <span class="ttl">Send a message <span class="pill">outbound</span></span>
        </div>
        <div class="body">
          <div class="tg-send">
            <div class="row">
              <label><span>Chat (pick from conversations)</span>
                <select data-f="picker" @change=${onPick}>
                  <option value="">— pick a chat —</option>
                  ${chats.map((c) => html`<option value="${c.connection}|${c.chat_id}|${c.thread_id ?? ''}">${c.label}</option>`)}
                </select>
              </label>
              <label style="max-width:160px"><span>from bot</span>
                <select data-f="connection">
                  <option value="">auto (infer)</option>
                  ${conns.map((c) => html`<option value=${c.id}>${this._connLabel(c)}</option>`)}
                </select>
              </label>
              <label style="max-width:160px"><span>chat_id (manual)</span>
                <input data-f="chat_id" inputmode="numeric" placeholder="-100… or user_id">
              </label>
              <label style="max-width:110px"><span>thread_id</span>
                <input data-f="thread_id" inputmode="numeric" placeholder="optional">
              </label>
            </div>
            <textarea data-f="body" placeholder="Body. Markdown isn't auto-parsed — Telegram will render whatever you paste verbatim."></textarea>
            <div class="footer">
              ${st
                ? html`<span class="status ${st.tone || ''}" data-status>${st.text}</span>`
                : html`<span class="status" data-status>Equivalent CLI: <code>relaydeck telegram reply &lt;chat_id&gt; "&lt;body&gt;" [--connection &lt;id&gt;]</code></span>`}
              <button data-act="send" @click=${(e) => this._send(e.target.closest('.tg-send'))}>Send</button>
            </div>
          </div>
        </div>
      </div>`;
  }

  _uniqueChats() {
    // Pure dedup logic lives in conversations.js so it stays node-testable
    // (this Lit module can't load outside the browser).
    return uniqueChats(this.convs, this.routes);
  }

  async _send(section) {
    const chat_id = parseInt(section.querySelector('[data-f="chat_id"]').value, 10);
    const threadRaw = section.querySelector('[data-f="thread_id"]').value.trim();
    const thread_id = threadRaw ? parseInt(threadRaw, 10) : null;
    const connection = section.querySelector('[data-f="connection"]').value || null;
    const bodyEl = section.querySelector('[data-f="body"]');
    const body = bodyEl.value.trim();
    if (!Number.isFinite(chat_id) || !body) {
      this._sendStatus = { text: 'chat_id and body required', tone: 'err' };
      this._renderBody();
      return;
    }
    try {
      const res = await this.api.postJSON('/api/plugins/telegram/reply', {
        chat_id, thread_id, body, connection,
      });
      if (res.ok) {
        this._sendStatus = { text: `✓ sent (message_id=${res.message_id ?? '?'})`, tone: 'ok' };
        bodyEl.value = '';
      } else {
        this._sendStatus = { text: '✗ ' + (res.error || 'send failed'), tone: 'err' };
      }
    } catch (e) {
      this._sendStatus = { text: '✗ ' + e.message, tone: 'err' };
    }
    this._renderBody();
  }

  // ── Settings card ─────────────────────────────────────────────
  _freshSettingsDraft() {
    const s = this.settings || {};
    return {
      mode: s.mode || 'polling',
      webhook_url: s.webhook_url || '',
      require_mention_in_groups: !!s.require_mention_in_groups,
      reactions: !!s.reactions,
      open_access: !!s.open_access,
      poll_timeout_s: s.poll_timeout_s ?? 30,
    };
  }

  _settingsCard() {
    const s = this.settings || {};
    if (!this._settingsDraft) this._settingsDraft = this._freshSettingsDraft();
    const d = this._settingsDraft;
    const seg = (val, label) => html`<button class=${d.mode === val ? 'on' : ''} data-v=${val}
      @click=${() => { d.mode = val; this._renderBody(); }}>${label}</button>`;
    const toggle = (k) => html`<div class="toggle ${d[k] ? 'on' : ''}" data-k=${k}
      @click=${() => { d[k] = !d[k]; this._renderBody(); }}></div>`;
    const st = this._settingsStatus;
    return html`
      <div class="tg-section full">
        <div class="head">
          <span class="ttl">Plugin settings <span class="pill">applies on save</span></span>
        </div>
        <div class="body">
          <div class="tg-settings">
            <div class="row">
              <div><div class="lbl">Mode</div><div class="desc">Polling needs no public URL; webhook needs a reachable HTTPS endpoint and a secret.</div></div>
              <div></div>
              <div class="ctrl">
                <div class="seg" data-k="mode">${seg('polling', 'Polling')}${seg('webhook', 'Webhook')}</div>
              </div>
            </div>
            <div class="row">
              <div><div class="lbl">Webhook URL</div><div class="desc">Public HTTPS URL where Telegram POSTs updates. Required when mode=webhook.</div></div>
              <div></div>
              <div class="ctrl">
                <input type="text" data-k="webhook_url" placeholder="https://your.host/api/plugins/telegram/webhook"
                  .value=${d.webhook_url} @input=${(e) => { d.webhook_url = e.target.value; }}>
              </div>
            </div>
            <div class="row">
              <div><div class="lbl">Require mention in groups</div><div class="desc">Bot only responds to @mention or reply-to-bot in group chats.</div></div>
              <div></div>
              <div class="ctrl">${toggle('require_mention_in_groups')}</div>
            </div>
            <div class="row">
              <div><div class="lbl">Reactions</div><div class="desc">React with 👀 / ❓ to routed / unrouted messages (best-effort; PTB 21+).</div></div>
              <div></div>
              <div class="ctrl">${toggle('reactions')}</div>
            </div>
            <div class="row">
              <div><div class="lbl">Open access <span style="color:var(--err)">⚠</span></div><div class="desc">Bypass the allowlists — the bot responds to <b>anyone</b> who messages it. Leave off unless this is a demo or an intentionally public bot.</div></div>
              <div></div>
              <div class="ctrl">${toggle('open_access')}</div>
            </div>
            <div class="row">
              <div><div class="lbl">Poll timeout (s)</div><div class="desc">Telegram getUpdates long-poll timeout.</div></div>
              <div></div>
              <div class="ctrl">
                <input type="number" data-k="poll_timeout_s" min="1" max="120"
                  .value=${String(d.poll_timeout_s)} @input=${(e) => { d.poll_timeout_s = e.target.value; }}>
              </div>
            </div>
            <div class="row">
              <div><div class="lbl">Vault keys</div><div class="desc">Where the bot token and webhook secret are stored.</div></div>
              <div></div>
              <div class="ctrl" style="font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-3)">
                ${s.bot_token_vault_key || ''} ${s.has_bot_token ? html`<span style="color:var(--ok)">●</span>` : html`<span style="color:var(--err)">●</span>`} ·
                ${s.webhook_secret_vault_key || ''} ${s.has_webhook_secret ? html`<span style="color:var(--ok)">●</span>` : html`<span style="color:var(--t-4)">○</span>`}
              </div>
            </div>
            <div class="row">
              <div><div class="lbl">Webhook secret</div><div class="desc">Validates inbound webhook POSTs (X-Telegram-Bot-Api-Secret-Token). Optional but recommended for webhook mode. Stored in the vault.</div></div>
              <div></div>
              <div class="ctrl">
                <input type="password" data-k="webhook_secret" style="min-width:200px"
                  placeholder=${s.has_webhook_secret ? '•••••• (set — paste to rotate)' : 'paste a secret'}>
                <button class="btn ghost sm" data-act="set-secret" @click=${(e) => this._setWebhookSecret(e)}>Save</button>
              </div>
            </div>
          </div>
          <div style="padding:10px 14px;border-top:1px solid var(--line-1);display:flex;justify-content:flex-end;gap:6px">
            <span style="flex:1;font-family:var(--f-mono);font-size:10px;align-self:center;color:${st?.tone === 'ok' ? 'var(--ok)' : st?.tone === 'err' ? 'var(--err)' : 'var(--t-4)'}" data-status>${st?.text ?? ''}</span>
            <button class="btn ghost sm" data-act="rotate-token" @click=${() => this._gotoConnections()}>Rotate token</button>
            <button class="btn primary sm" data-act="apply" @click=${() => this._applySettings()}>Apply &amp; restart</button>
          </div>
        </div>
      </div>`;
  }

  _gotoConnections() {
    // Per-bot token rotation lives in the Connections section now (each
    // connection has its own token); send the operator there.
    if (this._ctx?.setSection) this._ctx.setSection('connections');
    else { this._section = 'connections'; this._renderBody(); }
  }

  _setSettingsStatus(text, tone = '') {
    this._settingsStatus = { text, tone };
    this._renderBody();
  }

  async _setWebhookSecret(e) {
    const root = e.target.closest('.tg-section');
    const inp = root.querySelector('[data-k="webhook_secret"]');
    const secret = inp.value.trim();
    if (!secret) { this._setSettingsStatus('enter a secret first', 'err'); return; }
    this._setSettingsStatus('saving secret…', '');
    try {
      const res = await this.api.postJSON('/api/plugins/telegram/webhook-secret', { secret });
      inp.value = '';
      if (res.ok) { this._setSettingsStatus('✓ webhook secret saved', 'ok'); await this._reload(); }
      else this._setSettingsStatus('✗ failed', 'err');
    } catch (err) { this._setSettingsStatus('✗ ' + err.message, 'err'); }
  }

  async _applySettings() {
    const d = this._settingsDraft || this._freshSettingsDraft();
    this._setSettingsStatus('applying…', '');
    try {
      const res = await this.api.putJSON('/api/plugins/telegram/config', {
        mode: d.mode,
        webhook_url: (d.webhook_url || '').trim(),
        require_mention_in_groups: !!d.require_mention_in_groups,
        reactions: !!d.reactions,
        open_access: !!d.open_access,
        poll_timeout_s: parseFloat(d.poll_timeout_s) || 30,
      });
      if (res.ok) this._setSettingsStatus('✓ applied', 'ok');
      else this._setSettingsStatus('✗ failed', 'err');
    } catch (e) {
      this._setSettingsStatus('✗ ' + e.message, 'err');
    }
    await this._reload();
  }

  // ── Header buttons ───────────────────────────────────────────
  async _verify() {
    try {
      const res = await this.api.postJSON('/api/plugins/telegram/auth', {});
      const per = res.connections || [];
      if (per.length > 1) {
        const lines = per.map(c => `${c.ok ? '✓' : '✗'} ${c.connection}: ${c.ok ? '@' + c.bot_username + ' (id=' + c.bot_id + ')' : 'not ready'}`);
        alert(lines.join('\n'));
      } else if (res.ok) {
        alert(`✓ Verified: @${res.bot_username} (id=${res.bot_id})`);
      } else {
        alert(`✗ ${res.error || 'verification failed'}`);
      }
    } catch (e) {
      alert('✗ ' + e.message);
    }
  }

  async _restart() {
    try {
      await this.api.postJSON('/api/plugins/telegram/restart', {});
    } catch (e) {
      alert('Restart failed: ' + e.message);
      return;
    }
    await this._reload();
  }

  // ── Mutations ─────────────────────────────────────────────────
  async _removeRoute(i) { this.routes.routes.splice(i, 1); await this._save(); }

  async _addAllow(kind, id) {
    const key = kind === 'users' ? 'allowed_users' : 'allowed_groups';
    if (this.routes[key].includes(id)) return;
    this.routes[key].push(id);
    await this._save();
  }

  async _removeAllow(kind, id) {
    const key = kind === 'users' ? 'allowed_users' : 'allowed_groups';
    this.routes[key] = this.routes[key].filter(x => x !== id);
    await this._save();
  }

  async _save() {
    try {
      await this.api.putJSON('/api/plugins/telegram/routes', this.routes);
    } catch (e) {
      alert('Save failed: ' + e.message);
    }
    await this._reload();
  }

  async _testRoute(r) {
    try {
      const res = await this.api.postJSON('/api/plugins/telegram/routes/test', {
        chat_id: r.chat_id, thread_id: r.thread_id, command: r.command,
        connection: r.connection || null,
      });
      const lines = (res.matches || []).map(m =>
        `  ${m.winner ? '→' : '·'} workspace=${m.workspace} agent=${m.agent || '*'} specificity=${m.specificity}`,
      );
      alert(`${(res.matches || []).length} match(es):\n` + (lines.join('\n') || '(none)'));
    } catch (e) {
      alert('Test failed: ' + e.message);
    }
  }
}
