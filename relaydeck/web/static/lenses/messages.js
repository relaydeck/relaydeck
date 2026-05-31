// lenses/messages.js — Messages lens.
// READ-ONLY peer message threads (the dashboard NEVER sends here — authoring a
// peer message stays on the agent's Compose tile, so we never impersonate one
// agent into another's thread). Threads = unique pairs (or singletons) of
// agents, backed by /api/agents/{id}/inbox per agent; we aggregate client-side.
//
// MIGRATION (a full two-pane lens on the @relaydeck/ui kit). The pattern:
//   • extend RelayLens — keep the app.js contract (renderSidebar/renderDetail/
//     unmount + onAgentsChanged/onWorkspacesChanged) for free;
//   • implement sidebar()/detail() returning Lit templates — no innerHTML, no
//     querySelector, no manual addEventListener;
//   • subscribe to each scoped agent's inbox with this.use(path) — that
//     auto-subscribes to the live store (SSE-fed), re-renders on every push,
//     and releases on unmount; threads are aggregated from those caches on
//     each render, so a new agent.message streams in without a polling loop;
//   • read the shared agent collection from this.host.state.agents.
// Genuinely-procedural bits stay imperative against the reactive layer:
//   • the typing-window countdown is a setTimeout (registered via addCleanup);
//   • sticky-bottom scroll is applied to the .msg-list ref after each paint.

import {
  RelayLens, html, nothing, ref, createRef,
  icon, relTime, button, sideHead, sideSearch, empty,
} from '@relaydeck/ui';

// "<peer> is typing…" window: a participant who was the last recipient of a
// message in the thread within WINDOW_MS (and hasn't replied) is "typing".
const TYPING_WINDOW_MS = 30_000;

export class MessagesLens extends RelayLens {
  constructor(host, def) {
    super(host, def || { id: 'messages' });
    this.search = '';
    this.filter = 'all';
    this.activeThreadId = null;
    this.threads = [];
    // Message ids currently showing the rendered_body (PTY header) instead of
    // the raw body — toggled by each bubble's "view raw" button. Reactive so a
    // repaint never clobbers an imperative innerHTML swap (lit owns .body).
    this._rawShown = new Set();
    // Ref to the scrollable .msg-list — used for sticky-bottom scroll after a
    // paint without querying the DOM.
    this._listRef = createRef();
    // Whether the operator was pinned to the bottom before the last re-render.
    this._stick = true;
    // Re-aggregate + repaint when any subscribed inbox is invalidated by an
    // SSE message event (the live store also pushes, but agent set / typing
    // state can change without a resource push too).
    this.onEvent((evt) => {
      if (/message|agent/.test(evt?.type || '')) this.requestUpdate();
    });
  }

  // Detail reads agent semantic status for typing indicators — repaint both
  // panes when the shared collection refreshes (app.js contract is sidebar-
  // only by default; messages is the exception).
  onAgentsChanged() { this.requestUpdate(); }

  // Path shared by every render + the live subscription so the cache and the
  // fetcher use one key (no double-fetch).
  _inboxPath(agentId) {
    return `/api/agents/${encodeURIComponent(agentId)}/inbox?limit=100`;
  }

  // Agents in scope for the current workspace filter (host.state is kept fresh
  // by app.js; "" workspace = all).
  _scopedAgents() {
    const agents = this.host.state.agents || [];
    const ws = this.host.state.workspace;
    return agents.filter((a) => !ws || a.workspace === ws);
  }

  // Aggregate per-agent inboxes into peer threads. Calls this.use() for every
  // scoped agent, which subscribes to that inbox (re-rendering on each push)
  // and returns the latest cached value. Pure-derive: safe to run every render.
  _buildThreads() {
    const agents = this._scopedAgents();
    // Release inbox subscriptions for agents that left scope (this lens
    // instance is reused across workspace-filter switches) — otherwise their
    // inbox keys keep being heartbeat-refetched until unmount.
    const wanted = new Set(agents.map((a) => this._inboxPath(a.id)));
    for (const key of [...this._subs.keys()]) {
      if (key.includes('/inbox') && !wanted.has(key)) this.drop(key);
    }
    const seen = new Map();
    for (const a of agents) {
      const msgs = this.use(this._inboxPath(a.id), []);
      if (!Array.isArray(msgs)) continue;
      for (const m of msgs) {
        const fromId = m.from || m.from_agent || a.id;
        const toId = m.to || m.to_agent || a.id;
        const pair = [fromId, toId].sort();
        const key = pair.join('↔');
        if (!seen.has(key)) seen.set(key, { id: key, pair, messages: [], unread: 0, last: 0 });
        const t = seen.get(key);
        if (!t.messages.some((x) => x.id === m.id)) {
          t.messages.push({ ...m, from: fromId, to: toId });
        }
        // delivery_state: 'pending' | 'delivered' | 'failed'. Any non-delivered
        // is "unread" for badge purposes.
        if (m.delivery_state && m.delivery_state !== 'delivered') t.unread++;
        // Timestamps are floats (epoch seconds) in the API.
        const ts = typeof m.ts === 'number' ? m.ts * 1000 : Date.parse(m.ts || '') || 0;
        if (ts > t.last) t.last = ts;
      }
    }
    this.threads = Array.from(seen.values()).sort((a, b) => b.last - a.last);
    if (!this.activeThreadId && this.threads.length > 0) {
      this.activeThreadId = this.threads[0].id;
    }
    return this.threads;
  }

  // ── sidebar ───────────────────────────────────────────────────────
  sidebar() {
    const threads = this._buildThreads();
    const totalUnread = threads.reduce((s, t) => s + t.unread, 0);
    let rows = threads;
    if (this.filter === 'unread') rows = rows.filter((t) => t.unread > 0);
    if (this.search) {
      const q = this.search.toLowerCase();
      rows = rows.filter((t) => t.pair.some((p) => p.toLowerCase().includes(q)));
    }
    return html`
      ${sideHead(html`Messages`, { count: threads.length })}
      ${sideSearch(this.search, (v) => { this.search = v; this.requestUpdate(); }, 'Search threads…')}
      <div class="side-filter">
        <button data-f="all" class=${this.filter === 'all' ? 'on' : ''}
          @click=${() => { this.filter = 'all'; this.requestUpdate(); }}>All<span class="n">${threads.length}</span></button>
        <button data-f="unread" class=${this.filter === 'unread' ? 'on' : ''}
          @click=${() => { this.filter = 'unread'; this.requestUpdate(); }}>Unread<span class="n">${totalUnread}</span></button>
      </div>
      <div class="side-list">
        ${rows.length ? rows.map((t) => this._row(t)) : html`
          <div style="padding:14px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center">
            No threads yet. Agents talk to each other via
            <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">relaydeck workspace message</code>.
          </div>`}
      </div>`;
  }

  _row(t) {
    // Pick the chronologically latest message for the preview, not the last in
    // the array — t.messages is populated by iterating agents (one inbox each),
    // so insertion order doesn't match time order.
    const last = t.messages.reduce(
      (best, m) => (!best || (m.ts || 0) > (best.ts || 0)) ? m : best,
      null,
    );
    const preview = (last?.body || '').slice(0, 48).replace(/\n/g, ' ');
    return html`
      <div class="srow ${t.id === this.activeThreadId ? 'sel' : ''}"
        style="grid-template-columns:28px 1fr auto"
        @click=${() => { this.activeThreadId = t.id; this.requestUpdate(); }}>
        <div class="av acc" style="background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc)">${icon('message', 13)}</div>
        <div class="info">
          <div class="name truncate">${t.pair[0]} ↔ ${t.pair[1]}</div>
          <div class="sub truncate">${preview}${preview ? '…' : ''}</div>
        </div>
        <div class="right" style="align-items:flex-end">
          <span style="font-family:var(--f-mono);font-size:10px;color:var(--t-4)">${relTime(t.last)}</span>
          ${t.unread > 0 ? html`<span style="background:var(--acc);color:var(--acc-text);font-family:var(--f-mono);font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px">${t.unread}</span>` : nothing}
        </div>
      </div>`;
  }

  // ── detail ────────────────────────────────────────────────────────
  detail() {
    const threads = this._buildThreads();
    const thread = threads.find((t) => t.id === this.activeThreadId) || threads[0];
    if (!thread) {
      return empty('No messages yet',
        html`When agents message each other via the <code>messaging</code> plugin, threads will appear here.`);
    }
    this.activeThreadId = thread.id;

    // Sticky-bottom: capture whether the operator was already reading near the
    // bottom BEFORE this paint replaces the list contents. Auto-scroll after
    // the paint only if so (or on first render) — never yank them off history.
    const oldList = this._listRef.value;
    if (oldList && oldList.isConnected) {
      const near = (oldList.scrollHeight - oldList.scrollTop - oldList.clientHeight) < 40;
      this._stick = near;
    } else {
      this._stick = true;
    }
    // After lit commits this template, pin to bottom if sticky. Coalesced with
    // a microtask so layout is settled.
    this._scheduleStick();

    // Oldest at top, newest at bottom — standard chat-thread reading order
    // (Slack/iMessage/Discord). Day dividers are emitted BEFORE the first
    // bubble of each calendar day (not after).
    const messages = [...thread.messages].sort((a, b) => (a.ts || 0) - (b.ts || 0));
    const rows = [];
    let lastDay = '';
    for (const m of messages) {
      const ts = typeof m.ts === 'number' ? m.ts * 1000 : Date.parse(m.ts || '') || 0;
      const day = ts ? new Date(ts).toDateString() : '';
      if (day && day !== lastDay) {
        rows.push(html`<div class="msg-day">${day}</div>`);
        lastDay = day;
      }
      rows.push(this._bubble(m, thread));
    }

    const typing = this._typing(thread);

    return html`
      <div class="pane">
        <div class="dhdr">
          <div class="dhdr-top">
            <div class="dhdr-avatar" style="background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc)">${icon('message', 26)}</div>
            <div class="dhdr-meta">
              <div class="dhdr-eyebrow">peer thread</div>
              <h1 class="dhdr-name truncate">${thread.pair[0]} <span style="color:var(--t-3)">↔</span> <span class="acc">${thread.pair[1]}</span></h1>
              <div class="dhdr-row">
                <span class="chip"><span class="sdot running"></span>${thread.messages.length} messages</span>
                <span class="chip muted">via <code style="color:var(--acc)">messaging</code> plugin</span>
                <span class="chip muted">last activity ${relTime(thread.last)}</span>
              </div>
            </div>
            <div class="dhdr-actions">
              ${button({ variant: 'ghost', act: 'copy', onClick: () => this._exportThread(thread) }, icon('copy', 11), ' Export')}
            </div>
          </div>
        </div>
        <div class="msg-list" data-list ${ref(this._listRef)}>${rows}</div>
        <div class="msg-typing" data-typing ?hidden=${!typing.length}>
          ${typing.length ? html`
            <span class="msg-typing-dots"><i></i><i></i><i></i></span>
            ${typing.map((id, i) => html`${i ? ', ' : ''}<span class="msg-typing-who">${id}</span>`)}
            ${typing.length === 1 ? ' is typing' : ' are typing'}…` : nothing}
        </div>
        <div class="msg-readonly">
          ${icon('eye', 11)}
          <span>Read-only — peer threads are authored by the agents. To send a
          message, open an agent and use its <strong>Compose</strong> tab.</span>
        </div>
      </div>`;
  }

  // Auto-scroll the list to the bottom after the next paint if sticky-bottom.
  _scheduleStick() {
    if (this._stickScheduled) return;
    this._stickScheduled = true;
    queueMicrotask(() => {
      this._stickScheduled = false;
      if (this._dead || !this._stick) return;
      const list = this._listRef.value;
      if (list && list.isConnected) list.scrollTop = list.scrollHeight;
    });
  }

  // Show "<peer> is typing…" for thread participants likely generating a reply
  // right now. Two signals:
  //   1) semantic_status === 'working' — authoritative when set (claude-code
  //      via its vendor hook; classifier plugins for other harnesses later);
  //   2) Heuristic — they were the LAST recipient of a message in this thread
  //      within WINDOW_MS and haven't replied. Covers pi/codex/opencode/cursor
  //      (no hook); decays on its own. Returns the list of typing agent ids.
  _typing(thread) {
    const agents = this.host.state.agents || [];
    const now = Date.now();
    const lastTo = new Map();    // participant -> latest ts of msg sent TO them
    const lastFrom = new Map();  // participant -> latest ts of msg sent FROM them
    for (const m of thread.messages) {
      const ts = typeof m.ts === 'number' ? m.ts * 1000 : Date.parse(m.ts || '') || 0;
      if (m.from && m.from !== m.to) {
        if (ts > (lastFrom.get(m.from) || 0)) lastFrom.set(m.from, ts);
        if (ts > (lastTo.get(m.to) || 0)) lastTo.set(m.to, ts);
      }
    }
    const typing = agents.filter((a) => {
      if (!thread.pair.includes(a.id)) return false;
      if (a.semantic_status === 'working') return true;
      const got = lastTo.get(a.id) || 0;
      const sent = lastFrom.get(a.id) || 0;
      return got > sent && (now - got) < TYPING_WINDOW_MS && a.status === 'running';
    });
    // Re-eval at the end of the window so the dots disappear promptly if no SSE
    // event arrives to redraw us. One pending timer at a time; unmount() clears
    // it (no per-render addCleanup, which would leak closures).
    if (this._typingTimer) { clearTimeout(this._typingTimer); this._typingTimer = null; }
    if (typing.length) {
      const ages = typing.map((a) => now - (lastTo.get(a.id) || now));
      const remaining = TYPING_WINDOW_MS - Math.min(...ages);
      if (remaining > 0 && remaining < TYPING_WINDOW_MS) {
        this._typingTimer = setTimeout(() => {
          this._typingTimer = null;
          if (!this._dead) this.requestUpdate();
        }, remaining + 100);
      }
    }
    return typing.map((a) => a.id);
  }

  _bubble(m, thread) {
    const ts = typeof m.ts === 'number' ? m.ts * 1000 : Date.parse(m.ts || '') || 0;
    // The daemon stores raw `body` plus a `rendered_body` (with the
    // [relaydeck from=… id=…] header used to inject into the PTY). Operators
    // want the raw body; "view raw" swaps to rendered_body (reactive toggle).
    const body = m.body || '';
    const failed = m.delivery_state && m.delivery_state !== 'delivered';
    const raw = m.id != null && this._rawShown.has(m.id);
    return html`
      <div class="msg-bub">
        <div class="av">${(m.from || '?')[0].toUpperCase()}</div>
        <div class="stack">
          <div class="meta">
            <span class="who">${m.from || '?'}</span>
            <span class="t">${ts ? relTime(ts) : ''}</span>
            <span class="t">→ ${m.to || '?'}</span>
            ${failed ? html`<span class="t" style="color:var(--warn)">· ${m.delivery_state}${m.last_error ? ' · ' + String(m.last_error).slice(0, 40) : ''}</span>` : nothing}
          </div>
          <div class="body">${raw
            ? html`<pre style="margin:0;white-space:pre-wrap;word-break:break-word;font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-2)">${m.rendered_body || body}</pre>`
            : _renderBody(body)}</div>
          <div class="actions">
            <button data-act=${raw ? 'rendered' : 'raw'} @click=${() => this._toggleRaw(m)}>${raw ? 'rendered ↻' : 'view raw'}</button>
            <button data-act="copy" @click=${() => navigator.clipboard?.writeText(body)}>copy</button>
          </div>
        </div>
      </div>`;
  }

  // Toggle a bubble between the raw body and the rendered_body (PTY header).
  // Reactive — lit owns .body, so we never imperatively clobber its content.
  _toggleRaw(m) {
    if (m.id == null) return;
    if (this._rawShown.has(m.id)) this._rawShown.delete(m.id);
    else this._rawShown.add(m.id);
    this.requestUpdate();
  }

  // Export the whole thread as plain text to the clipboard.
  _exportThread(thread) {
    const messages = [...thread.messages].sort((a, b) => (a.ts || 0) - (b.ts || 0));
    const lines = messages.map((m) => {
      const ts = typeof m.ts === 'number' ? m.ts * 1000 : Date.parse(m.ts || '') || 0;
      const when = ts ? new Date(ts).toISOString() : '';
      return `[${when}] ${m.from || '?'} → ${m.to || '?'}\n${m.body || ''}`;
    });
    navigator.clipboard?.writeText(lines.join('\n\n'));
  }

  unmount() {
    if (this._typingTimer) { clearTimeout(this._typingTimer); this._typingTimer = null; }
    super.unmount();
  }
}

function _renderBody(body) {
  // Light markdown: fenced code, inline code. Everything else is text.
  // Returns a Lit template (auto-escaped) — no unsafeHTML needed.
  const blocks = [];
  const parts = body.split(/(```[\s\S]*?```)/g);
  for (const p of parts) {
    if (p.startsWith('```')) {
      blocks.push(html`<pre>${p.replace(/```/g, '').trim()}</pre>`);
    } else {
      const segs = p.split(/(`[^`]+`)/g);
      for (const s of segs) {
        if (s.startsWith('`') && s.endsWith('`') && s.length >= 2) {
          blocks.push(html`<code>${s.slice(1, -1)}</code>`);
        } else {
          // Preserve newlines as <br> within plain-text segments.
          const lines = s.split('\n');
          lines.forEach((ln, i) => {
            if (i) blocks.push(html`<br>`);
            blocks.push(ln);
          });
        }
      }
    }
  }
  return blocks;
}
