// tile_inbox.js — Inbox tile for the agents lens.
// Plugin contribution from `messaging`. Chat-app layout: oldest at top,
// newest at the bottom (matches the Messages lens + iMessage/Slack/etc).
// Live: subscribes to its own inbox path so new messages stream in;
// `liveOnEvent` invalidates `/api/agents/<id>/inbox` on every
// `agent.message` SSE. Typing indicator: any peer whose
// `semantic_status === 'working'` shows up as "<peer> is typing…".
//
// MIGRATED to @relaydeck/ui (build-less, light-DOM Lit). The legacy
// mount(container, api, ctx)/unmount() contract is preserved verbatim via
// defineTile(); internally this is a RelayElement that subscribes through two
// LiveControllers (auto-unsubscribe on disconnect). The bespoke .mibx-* CSS
// and its one-time <style id="mibx-css"> injection are kept as-is; the
// sticky-bottom scroll, day separators, and the typing-window timer all carry
// over.

import {
  RelayElement, defineTile, LiveController, html, nothing, ref, createRef, esc, relTime,
} from '@relaydeck/ui';

class MessagingInboxTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    mode: { type: String },
  };

  constructor() {
    super();
    this.agent = {};
    this.mode = '';
    this._msgs = [];
    this._agents = [];
    this._stick = true;          // start pinned to the bottom
    this._prevLen = 0;           // last rendered message count (first-render detection)
    this._typingTimer = null;
    this._scrollRef = createRef();
    // Live inbox: every `agent.message` SSE invalidates this path; the
    // controller re-renders with the fresh payload. Initial subscribe also
    // primes the cache, so no explicit first-load fetch is needed.
    this._inbox = new LiveController(this);
    // Peer status (for typing indicator): a peer is "typing" when its
    // semantic_status is `working`. /api/agents updates on every
    // `agent.*` event including `status_changed`.
    this._allAgents = new LiveController(this);
  }

  connectedCallback() {
    super.connectedCallback();
    this._injectCSS();
    this._allAgents.setKey('/api/agents');
  }

  willUpdate(changed) {
    if (changed.has('agent') && this.agent?.id) {
      this._inbox.setKey(
        `/api/agents/${encodeURIComponent(this.agent.id)}/inbox?limit=50`,
      );
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._typingTimer) { clearTimeout(this._typingTimer); this._typingTimer = null; }
  }

  // ── derived data ─────────────────────────────────────────────────
  get _messages() { return this._inbox.value || []; }
  get _agentList() { return this._allAgents.value || []; }

  render() {
    const isPop = this.mode === 'pop';
    const msgs = this._messages;
    // Chat-app order: oldest first, newest at the bottom. The API returns
    // newest-first; flip a copy (don't mutate the live cache).
    const ordered = [...msgs].sort((a, b) => (a.ts || 0) - (b.ts || 0));
    return html`
      <div class="mibx${isPop ? ' mibx-pop' : ''}">
        ${isPop ? nothing : html`
          <div class="mibx-head">
            <span class="mibx-eyebrow">Inbox · ${esc(this.agent?.id ?? '')}</span>
            <span class="mibx-count" data-count>${msgs.length} message${msgs.length === 1 ? '' : 's'}</span>
          </div>`}
        <div class="mibx-scroll" data-list ${ref(this._scrollRef)} @scroll=${this._onScroll}>
          ${ordered.length ? this._bubbles(ordered) : html`<div class="mibx-empty">No messages yet.</div>`}
        </div>
        ${this._typingTemplate()}
      </div>`;
  }

  _bubbles(ordered) {
    const out = [];
    let lastDay = '';
    for (const m of ordered) {
      const ts = typeof m.ts === 'number' ? m.ts * 1000 : Date.parse(m.ts || '') || 0;
      const day = ts ? new Date(ts).toDateString() : '';
      if (day && day !== lastDay) {
        out.push(html`<div class="mibx-day">${day === new Date().toDateString() ? 'today' : day}</div>`);
        lastDay = day;
      }
      const from = m.from_agent || m.from || '?';
      out.push(html`
        <div class="mibx-bub">
          <div class="mibx-meta">
            <span class="mibx-who">${esc(from)}</span>
            <span class="mibx-when">${esc(relTime(ts || null))}</span>
          </div>
          <div class="mibx-body">${m.body || ''}</div>
        </div>`);
    }
    return out;
  }

  // Typing indicator. Two paths into "typing":
  // 1) Hook-driven (claude-code, future classifier plugins for other
  //    harnesses): semantic_status === 'working' is set by a vendor hook.
  //    Authoritative when present.
  // 2) Heuristic (pi/codex/opencode/cursor — anything with no hook): we last
  //    sent THEM something within the WINDOW, and they haven't sent us a reply
  //    since. Decays naturally past the window so a hung peer doesn't show
  //    "typing forever".
  _typingTemplate() {
    if (this._typingTimer) { clearTimeout(this._typingTimer); this._typingTimer = null; }
    const msgs = this._messages;
    const agents = this._agentList;
    const selfId = this.agent?.id;
    const peers = new Set();
    for (const m of msgs) {
      const f = m.from_agent || m.from;
      if (f && f !== selfId) peers.add(f);
    }
    const WINDOW_MS = 30_000;
    const now = Date.now();
    const lastFromSelfTo = new Map();   // peer -> ts of latest msg me→peer
    const lastFromPeer = new Map();     // peer -> ts of latest msg peer→me
    for (const m of msgs) {
      const ts = (typeof m.ts === 'number' ? m.ts * 1000 : Date.parse(m.ts || '') || 0);
      const f = m.from_agent || m.from;
      const t = m.to_agent || m.to;
      if (f === selfId && t && t !== selfId) {
        if (ts > (lastFromSelfTo.get(t) || 0)) lastFromSelfTo.set(t, ts);
      } else if (f && f !== selfId && t === selfId) {
        if (ts > (lastFromPeer.get(f) || 0)) lastFromPeer.set(f, ts);
      }
    }
    const typing = agents.filter((a) => {
      if (!peers.has(a.id)) return false;
      if (a.semantic_status === 'working') return true;
      const sent = lastFromSelfTo.get(a.id) || 0;
      const got = lastFromPeer.get(a.id) || 0;
      // Recently pinged AND hasn't replied yet AND still alive.
      return sent > got && (now - sent) < WINDOW_MS && a.status === 'running';
    });
    if (!typing.length) {
      return html`<div class="mibx-typing" data-typing hidden></div>`;
    }
    const verb = typing.length === 1 ? 'is typing' : 'are typing';
    // Schedule a re-eval at the end of the window so we hide the indicator
    // promptly if no reply arrives (no new SSE event to trigger a redraw
    // otherwise).
    const ages = typing.map((a) => now - (lastFromSelfTo.get(a.id) || now));
    const minAge = Math.min(...ages);
    const remaining = WINDOW_MS - minAge;
    if (remaining > 0 && remaining < WINDOW_MS) {
      this._typingTimer = setTimeout(() => this.requestUpdate(), remaining + 100);
    }
    const names = typing.map((a, i) => html`${i ? ', ' : nothing}<span class="mibx-typing-who">${esc(a.id)}</span>`);
    return html`
      <div class="mibx-typing" data-typing>
        <span class="mibx-typing-dots"><i></i><i></i><i></i></span>
        ${names} ${verb}…
      </div>`;
  }

  // ── scroll: sticky-bottom ─────────────────────────────────────────
  _onScroll() {
    const list = this._scrollRef.value;
    if (!list) return;
    // Track whether the user is reading near the bottom; if they scroll up to
    // read history we stop auto-pulling them back down on every new message.
    const near = (list.scrollHeight - list.scrollTop - list.clientHeight) < 40;
    this._stick = near;
  }

  updated() {
    const list = this._scrollRef.value;
    if (!list) return;
    const len = this._messages.length;
    // Scroll to the latest message if the user wasn't actively reading higher
    // up (sticky-bottom behavior). Also stick on the very first render —
    // there's nothing for the user to be reading.
    if (this._stick || this._prevLen === 0) {
      list.scrollTop = list.scrollHeight;
    }
    this._prevLen = len;
  }

  _injectCSS() {
    if (document.getElementById('mibx-css')) return;
    const s = document.createElement('style');
    s.id = 'mibx-css';
    s.textContent = `
.mibx { background:var(--bg-1); border:1px solid var(--line-2); border-radius:var(--r-3);
        flex:1; overflow:hidden; min-height:0; display:flex; flex-direction:column; }
.mibx.mibx-pop { background:transparent; border:0; border-radius:0; }
.mibx-head { padding:10px 14px; border-bottom:1px solid var(--line-1);
             display:flex; justify-content:space-between; align-items:center; flex-shrink:0; }
.mibx-eyebrow, .mibx-count {
  font-family:var(--f-mono); font-size:var(--t-xxs); letter-spacing:.14em;
  text-transform:uppercase; color:var(--t-3);
}
.mibx-scroll { flex:1; overflow-y:auto; padding:8px 14px 12px;
               display:flex; flex-direction:column; gap:8px; }
.mibx-empty { padding:14px; color:var(--t-3); font-family:var(--f-mono);
              font-size:var(--t-xs); text-align:center; }
.mibx-day { align-self:center; padding:4px 10px; margin:6px 0; border-radius:999px;
            background:var(--bg-2); border:1px solid var(--line-2);
            font-family:var(--f-mono); font-size:10px; color:var(--t-3);
            letter-spacing:.08em; text-transform:uppercase; }
.mibx-bub { padding:10px 12px; background:var(--bg-1); border:1px solid var(--line-2);
            border-radius:var(--r-2); min-width:0; }
.mibx-meta { display:flex; justify-content:space-between; gap:8px;
             font-family:var(--f-mono); font-size:var(--t-xxs); min-width:0;
             margin-bottom:4px; }
.mibx-who { color:var(--acc); }
.mibx-when { color:var(--t-4); }
.mibx-body { font-size:var(--t-xs); color:var(--t-1); word-break:break-word;
             white-space:pre-wrap; }
.mibx-typing { display:flex; align-items:center; gap:6px;
               padding:6px 14px 10px; flex-shrink:0;
               font-family:var(--f-mono); font-size:var(--t-xxs); color:var(--t-3);
               border-top:1px dashed var(--line-1); background:var(--bg-1); }
.mibx-typing[hidden] { display:none; }
.mibx-typing-who { color:var(--acc); }
.mibx-typing-dots { display:inline-flex; gap:2px; align-items:flex-end; height:10px; }
.mibx-typing-dots i { width:4px; height:4px; border-radius:50%; background:var(--acc);
                      opacity:.35; animation:mibxBounce 1.2s infinite ease-in-out; }
.mibx-typing-dots i:nth-child(2) { animation-delay:.15s; }
.mibx-typing-dots i:nth-child(3) { animation-delay:.30s; }
@keyframes mibxBounce { 0%, 60%, 100% { transform:translateY(0); opacity:.35; }
                        30% { transform:translateY(-3px); opacity:.95; } }
`;
    document.head.appendChild(s);
  }
}

if (!customElements.get('rd-messaging-inbox')) {
  customElements.define('rd-messaging-inbox', MessagingInboxTile);
}

export default defineTile('rd-messaging-inbox', (el, { ctx }) => {
  el.agent = ctx.agent;
  el.mode = ctx.mode || '';
});
