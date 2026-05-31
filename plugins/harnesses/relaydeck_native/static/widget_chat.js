// widget_chat.js — Home-dashboard widget to chat with a Relaydeck-native
// agent inline. Pick a relaydeck agent, talk to it, and (if it has the
// `dashboard` tool) watch it reshape this very dashboard live.
//
// Reuses the /chat endpoint (one shared thread with the Terminal tab +
// `relaydeck chat`). Per-agent thread is cached in this module so the Home grid
// re-rendering (when the agent adds/moves a widget!) doesn't wipe the
// visible conversation. Live "🔧 …" step lines stream in via
// relaydeck.native.step SSE events so you see things happening mid-turn.
//
// MIGRATED to @relaydeck/ui (build-less, light-DOM Lit): the view renders with
// Lit templates (no innerHTML, no hand-rolled esc), but the singleton/SSE
// routing stays imperative. The public contract is unchanged — a default-export
// class with mount(el, api, ctx) + unmount().

import { html, render } from '@relaydeck/ui';

const _threads = new Map(); // agentId -> [{role, text, steps, pending}]
let _active = null;          // the currently-mounted instance (survives remount)

function _pending(turns) {
  for (let i = turns.length - 1; i >= 0; i--) if (turns[i].pending) return turns[i];
  return null;
}

export default class NativeChatWidget {
  async mount(el, api, ctx) {
    this.api = api;
    this.host = ctx.host;
    this.el = el;
    _active = this;

    // Build the static shell once with a Lit template. The log body, agent
    // selector, textarea, and send button keep their data-* hooks so the
    // imperative paths (selector wiring, SSE re-render, pi-install banner)
    // can still find their anchors.
    render(html`
      <div style="display:flex;flex-direction:column;height:100%;min-height:0">
        <div data-pi-banner style="display:none;margin-bottom:8px"></div>
        <div style="display:flex;gap:6px;align-items:center;padding:2px 2px 8px">
          <span style="font-family:var(--f-mono);font-size:10px;color:var(--t-3);text-transform:uppercase;letter-spacing:.08em">native chat</span>
          <select data-f="agent" style="flex:1;font-family:var(--f-mono);font-size:11px"
            @change=${() => this._render()}></select>
        </div>
        <div data-log style="flex:1;overflow:auto;display:flex;flex-direction:column;gap:8px;padding:2px"></div>
        <div style="display:flex;gap:6px;padding-top:8px">
          <textarea data-f="msg" rows="1" placeholder="ask it… (try: move the chat widget to the top)"
            style="flex:1;font-family:var(--f-sans);font-size:var(--t-sm);resize:none"
            @keydown=${(e) => this._onKeydown(e)}></textarea>
          <button class="btn primary sm" data-act="send" @click=${() => this._send(this._ta)}>Send</button>
        </div>
      </div>`, el);

    this.sel = el.querySelector('[data-f="agent"]');
    this.log = el.querySelector('[data-log]');
    this.piBanner = el.querySelector('[data-pi-banner]');
    this._ta = el.querySelector('[data-f="msg"]');

    this._fillAgents();
    this._piOk = undefined;
    this._mountPiInstall();
    this._render();

    // Live updates: streamed reasoning/answer tokens + tool-run lines.
    this._unsub = (api && typeof api.onEvent === 'function')
      ? api.onEvent(ev => this._onEvent(ev)) : null;
  }

  _onKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._send(this._ta); }
  }

  _relaydeckAgents() {
    return (this.host.state.agents || []).filter(a => a.type === 'relaydeck');
  }

  _mountPiInstall() {
    // The pi-install widget polls /status every 2s and renders an
    // "Install pi" button when pi's missing. When it flips to installed
    // it hides itself and fires onReady — we don't need a separate
    // _checkPi anymore; the widget IS the check.
    if (!this.piBanner || !this.api) return;
    import('./pi_install.js').then(mod => {
      if (!this.el?.isConnected) return;  // widget unmounted mid-import
      this._piHandle = mod.mountPiInstall({
        container: this.piBanner,
        api: this.api,
        compact: true,
        onStatus: (st) => { this._piOk = !!st?.pi_installed; },
        onReady: () => { this._piOk = true; },
      });
    }).catch(() => { /* import-failure: stale banner is non-fatal */ });
  }

  _fillAgents() {
    const agents = this._relaydeckAgents();
    if (!agents.length) {
      render(html`<option value="">no relaydeck agents — create one (type: relaydeck)</option>`, this.sel);
      this.sel.disabled = true;
      return;
    }
    const prev = this.sel.value;
    render(html`${agents.map(a => html`<option value=${a.id}>${a.id}</option>`)}`, this.sel);
    this.sel.disabled = false;
    if (prev && agents.some(a => a.id === prev)) this.sel.value = prev;
  }

  get _agentId() { return this.sel.value; }
  get _turns() {
    const id = this._agentId;
    if (!id) return [];
    if (!_threads.has(id)) _threads.set(id, []);
    return _threads.get(id);
  }

  _placeholder(text) {
    return html`<div style="margin:auto;color:var(--t-4);font-family:var(--f-mono);font-size:11px">${text}</div>`;
  }

  _turnTpl(t) {
    const me = t.role === 'user';
    const reason = t.reasoning
      ? html`<div style="font-size:10px;color:var(--t-3);font-style:italic;font-family:var(--f-mono);white-space:pre-wrap;word-break:break-word;max-height:90px;overflow:auto;margin-bottom:3px;opacity:.85">💭 ${t.reasoning}</div>`
      : '';
    const steps = (t.steps && t.steps.length)
      ? html`<div style="margin-top:4px;display:flex;flex-direction:column;gap:2px">${
          t.steps.map(s => {
            const o = (typeof s === 'string') ? { text: s, kind: 'tool' } : s;
            const isR = o.kind === 'reasoning';
            const glyph = isR ? '💭' : '🔧';
            const css = isR
              ? 'color:var(--t-3);font-style:italic'
              : 'color:var(--acc)';
            return html`<div style="font-size:10px;font-family:var(--f-mono);white-space:pre-wrap;word-break:break-word;${css}">${glyph} ${o.text}</div>`;
          })
        }</div>` : '';
    return html`<div style="align-self:${me ? 'flex-end' : 'flex-start'};max-width:88%">
      ${reason}<div style="background:${me ? 'var(--bg-2)' : 'var(--bg-1)'};border:1px solid var(--line-2);border-radius:8px;padding:6px 9px;font-size:var(--t-sm);white-space:pre-wrap;word-break:break-word">${t.text}</div>${steps}
    </div>`;
  }

  _render() {
    if (!this.log) return;
    if (!this._agentId) { render(this._placeholder('pick a relaydeck agent'), this.log); return; }
    const turns = this._turns;
    if (!turns.length) { render(this._placeholder('say hi 👋'), this.log); return; }
    render(html`${turns.map(t => this._turnTpl(t))}`, this.log);
    this.log.scrollTop = this.log.scrollHeight;
  }

  _onEvent(ev) {
    if (!ev) return;
    if (ev.type === 'relaydeck.native.step') this._onStep(ev);
    else if (ev.type === 'relaydeck.native.token') this._onToken(ev);
  }

  _onStep(ev) {
    const d = ev.payload || ev.data || {};
    if (!d.text || d.agent_id !== this._agentId) return;
    const p = _pending(this._turns);
    if (p) { (p.steps = p.steps || []).push({ text: d.text, kind: d.kind || 'tool' }); (_active || this)._render(); }
  }

  _onToken(ev) {
    // The model is generating — show the reasoning + answer typing in live.
    const d = ev.payload || ev.data || {};
    if (d.agent_id !== this._agentId) return;
    const p = _pending(this._turns);
    if (!p) return;
    if (d.reasoning) p.reasoning = d.reasoning;
    if (d.content) p.text = d.content;
    (_active || this)._render();
  }

  async _send(ta) {
    const text = (ta.value || '').trim();
    const id = this._agentId;
    if (!text || !id) return;
    // Fail-open the send gate: only refuse to send when we have positive
    // evidence pi is missing (_piOk === false). undefined (widget still
    // booting / transient probe error) lets the message through — the
    // /chat endpoint will surface a real error if pi is genuinely absent,
    // which is better feedback than the textarea silently swallowing the
    // message during the dynamic-import window.
    if (this._piOk === false) {
      // Probe again before refusing — pi may have just been installed.
      try { await this._piHandle?.recheck?.(); } catch (_) {}
      if (this._piOk === false) {
        const turns = this._turns;
        turns.push({ role: 'user', text });
        turns.push({
          role: 'assistant', pending: false,
          text: 'pi is not installed — install it from the banner above first.',
        });
        ta.value = '';
        this._render();
        return;
      }
    }
    ta.value = '';
    const turns = this._turns;
    turns.push({ role: 'user', text });
    turns.push({ role: 'assistant', text: '…', steps: [], pending: true });
    this._render();
    try {
      // Send the full Home grid (positions + sizes) so the agent has a
      // spatial mental model — the layout lives in localStorage otherwise.
      const layout = ((window.__relaydeckHome && window.__relaydeckHome.layout) || [])
        .map(w => ({ key: w.key, x: w.x, y: w.y, w: w.w, h: w.h }));
      const r = await this.api.postJSON('/api/plugins/relaydeck-native/chat',
        { agent_id: id, text, dashboard_layout: layout });
      const p = _pending(turns);
      if (p) {
        p.pending = false;
        p.text = (r && r.ok) ? (r.reply || '(no reply)') : ('error: ' + ((r && r.error) || 'unknown'));
        // Keep the live steps (reasoning 💭 + tools 🔧) we streamed in; only
        // fall back to the response's tool_log if no live steps arrived.
        if ((!p.steps || !p.steps.length) && r && r.tools && r.tools.length) {
          p.steps = r.tools.map(t => ({ text: t.observations || (t.calls || []).join(', '), kind: 'tool' }));
        }
      }
    } catch (e) {
      const p = _pending(turns);
      if (p) { p.pending = false; p.text = 'failed: ' + e.message; }
    }
    (_active || this)._render();
  }

  unmount() {
    try { this._unsub?.(); } catch (_) {}
    try { this._piHandle?.unmount?.(); } catch (_) {}
    this._piHandle = null;
    if (_active === this) _active = null;
  }
}
