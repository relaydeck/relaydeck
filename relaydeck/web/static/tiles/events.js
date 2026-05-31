// tiles/events.js — recent events scoped to one agent.
//
// MIGRATION NOTE (RelayElement + EventsController + defineTile). The list now
// lives in reactive state and renders via lit-html — no innerHTML, no
// querySelector wiring, no manual addEventListener. Two sources feed it:
//   1. Initial fetch from /api/agents/{id}/events — rows in DB order:
//      { id, type, payload (JSON string), ts (epoch seconds) }.
//   2. The raw SSE bus (NOT the live store) via EventsController, filtered to
//      this agent. Plugin-bus events arrive as { type, data, ts, source_plugin }
//      with the agent_id inside `data` (not at top level); we accept either
//      shape for forward-compat. New events prepend; the list is capped at 80.
//
// We parse `payload` lazily and render a compact summary line:
// timestamp + glyph + type + the most relevant payload field. Clicking a row
// toggles an inline pretty-printed payload (tracked in reactive state).

import {
  RelayElement, defineTile, EventsController, html, nothing, esc, repeat, when,
} from '@relaydeck/ui';

const ROW_CAP = 80;

class EventsTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    mode: { type: String },
    _events: { state: true },
    _expanded: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this.agent = {};
    this.mode = '';
    this._events = [];
    this._expanded = null;   // id of the currently-expanded row (single)
    this._error = '';
    // Raw SSE bus — same source the old api.events.subscribe used. Filtered
    // to this agent in _onEvent; auto-unsubscribes on disconnect.
    this._bus = new EventsController(this, { onEvent: (e) => this._onEvent(e), rerender: false });
  }

  willUpdate(changed) {
    if (changed.has('agent') && this.agent?.id && this.agent.id !== this._loadedFor) {
      this._loadedFor = this.agent.id;
      this._load();
    }
  }

  async _load() {
    try {
      const r = await this.api.fetch(`/api/agents/${encodeURIComponent(this.agent.id)}/events?limit=50`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const events = await r.json();
      this._error = '';
      // Preserve the original ordering: the API returns rows oldest→newest and
      // the old tile appended them in that order (newest fetched at the bottom);
      // live events then prepend to the top. Keep both behaviours intact.
      this._events = (events || []).slice(0, ROW_CAP);
    } catch (e) {
      this._error = e.message;
    }
  }

  _onEvent(event) {
    const agentId = event.agent_id || event.data?.agent_id || event.data?.id || '';
    if (agentId !== this.agent?.id) return;
    this._events = [event, ...this._events].slice(0, ROW_CAP);
  }

  _toggle(id) { this._expanded = this._expanded === id ? null : id; }

  _row(event) {
    const g = _eventGlyph(event.type);
    const time = _fmtTime(event.ts);
    const payload = _parsePayload(event);
    const summary = _summarize(event.type || '', payload);
    const open = event.id != null && this._expanded === event.id;
    let pretty = '';
    try { pretty = JSON.stringify(payload, null, 2); } catch (_) { pretty = String(payload); }
    return html`
      <div style="display:grid;grid-template-columns:64px 16px 1fr;gap:8px;padding:4px 0;font-family:var(--f-mono);font-size:var(--t-xs);align-items:baseline;min-width:0;cursor:pointer"
        title="Click to expand payload" @click=${() => this._toggle(event.id)}>
        <span style="color:var(--t-4);font-size:10px">${time}</span>
        <span style="color:var(--${g.color});text-align:center">${g.glyph}</span>
        <span style="color:var(--t-1);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          <span style="color:var(--t-2)">${event.type || 'event'}</span>${summary
            ? html` <span style="color:var(--t-3)">· ${summary}</span>` : nothing}
        </span>
      </div>
      ${when(open, () => html`
        <div data-expand=${event.id}
          style="padding:6px 12px 10px 88px;font-family:var(--f-mono);font-size:10px;color:var(--t-3);white-space:pre-wrap;word-break:break-word;border-bottom:1px solid var(--line-1)">${pretty}</div>`)}`;
  }

  _list() {
    if (this._error) {
      return html`<div style="padding:10px;color:var(--err);font-family:var(--f-mono);font-size:var(--t-xs)">Failed to load: ${esc(this._error)}</div>`;
    }
    if (this._events.length === 0) {
      return html`<div style="padding:14px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center">No events recorded yet.</div>`;
    }
    return repeat(this._events, (e, i) => (e.id != null ? `${e.id}` : `i${i}`), (e) => this._row(e));
  }

  render() {
    if (this.mode === 'pop') {
      return html`<div data-list style="min-height:0;display:flex;flex-direction:column;gap:2px">${this._list()}</div>`;
    }
    const count = this._error ? '—' : `${this._events.length} events`;
    return html`
      <div class="card" style="flex:1;overflow:hidden;min-height:0;padding:0;display:flex;flex-direction:column">
        <div style="padding:10px 14px;border-bottom:1px solid var(--line-1);display:flex;justify-content:space-between;align-items:center;flex-shrink:0">
          <span class="card-title">Events · scoped to ${esc(this.agent?.id || '')}</span>
          <span class="card-sub" data-rate>${count}</span>
        </div>
        <div style="padding:6px 14px;flex:1;overflow:auto;min-height:0" data-list>${this._list()}</div>
      </div>`;
  }
}

if (!customElements.get('rd-tile-events')) customElements.define('rd-tile-events', EventsTile);

export default defineTile('rd-tile-events', (el, { api, ctx }) => {
  el.api = api;
  el.agent = ctx.agent;
  el.mode = ctx.mode || '';
});

// Payload from `/api/agents/{id}/events` is a JSON string; from SSE
// it's already a parsed object on event.data. Normalize.
function _parsePayload(event) {
  if (event.data && typeof event.data === 'object') return event.data;
  const raw = event.payload;
  if (!raw) return {};
  if (typeof raw === 'object') return raw;
  try { return JSON.parse(raw); } catch (_) { return { _raw: String(raw) }; }
}

function _summarize(type, p) {
  // Pull the most relevant field for the row's one-line summary.
  if (!p) return '';
  if (type.includes('error') || type === 'agent.error') {
    return String(p.error || p.message || p.detail || '').slice(0, 80);
  }
  if (type === 'usage.record') {
    const m = p.model || '';
    const pin = p.prompt ?? p.tokens_in ?? '?';
    const pout = p.completion ?? p.tokens_out ?? '?';
    return `${m} · ${pin} in / ${pout} out`;
  }
  if (type === 'agent.message') {
    return `${p.from || '?'} → ${p.to || '?'}`;
  }
  if (type === 'agent.status_changed') {
    return `${p.previous || '—'} → ${p.status || '?'}`;
  }
  if (type === 'harness.exit') {
    return `rc=${p.returncode ?? '?'}`;
  }
  if (type === 'harness.spawn') {
    const cmd = (p.command && p.command[0]) || '';
    return cmd ? `command=${cmd}` : '';
  }
  if (type === 'gateway.webhook') {
    return p.channel || '';
  }
  // Fallback: first useful-looking field.
  const k = ['action', 'channel', 'event', 'message', 'name'].find(x => p[x]);
  return k ? `${k}=${p[k]}` : '';
}

function _fmtTime(ts) {
  if (!ts) return '';
  // API ts is epoch seconds (float). Multiply for the Date constructor.
  const n = typeof ts === 'number' ? ts * 1000 : Date.parse(ts);
  if (Number.isNaN(n)) return '';
  return new Date(n).toTimeString().slice(0, 8);
}

function _eventGlyph(type) {
  if (!type) return { glyph: '●', color: 't-3' };
  if (type.includes('error')) return { glyph: '✗', color: 'err' };
  if (type.includes('start') || type.includes('complete')) return { glyph: '▶', color: 'ok' };
  if (type.includes('exit') || type.includes('stop')) return { glyph: '■', color: 't-3' };
  if (type.includes('message')) return { glyph: '✉', color: 'acc' };
  if (type.includes('webhook') || type.includes('gateway')) return { glyph: '⇣', color: 'info' };
  if (type.startsWith('harness')) return { glyph: '◇', color: 'warn' };
  if (type === 'usage.record') return { glyph: 'Σ', color: 'info' };
  if (type.includes('status_changed')) return { glyph: '↻', color: 'warn' };
  return { glyph: '●', color: 't-3' };
}
