// usage-limits — agent tile chip.
//
// Renders a compact "session: 42% · weekly: 8%" line under the agent
// card. Refreshes every 15s. The data comes from
// /api/plugins/usage-limits/state?agent=<id> which the plugin
// registers (and the auth middleware gates).
//
// MIGRATED to @relaydeck/ui (build-less, light-DOM Lit). The default export
// stays an OBJECT literal with a mount(container, { agentId }) method — the
// host calls .mount on the default export object. We render the chip with a
// Lit template via render() instead of hand-rolled innerHTML, replace the
// per-window inline hex with status tokens (--err/--warn/--t-3), and keep the
// 15s setInterval refresh + teardown. mount() still returns its teardown
// closure (back-compat); the object also exposes unmount() per the contract.

import { html, render } from '@relaydeck/ui';

// Per-window state → design token (was #c33 / #c80 / #888).
function windowColor(state) {
  if (state === 'exceeded') return 'var(--err)';
  if (state === 'warn') return 'var(--warn)';
  return 'var(--t-3)';
}

// One template for the whole chip line. Lit auto-escapes interpolations, so the
// previous esc()/innerHTML string-building is gone. `text` carries an optional
// plain status string (loading / error / no-data); `windows` carries the
// per-window percentage spans when we have live data.
function tile({ text, windows }) {
  if (text != null) return html`${text}`;
  return html`${(windows || []).map((w, i) => html`${
    i > 0 ? html` · ` : ''
  }${
    w.disabled
      ? html`${w.name}: ∞`
      : html`<span style="color:${windowColor(w.state)}">${w.name}: ${w.pct}%</span>`
  }`)}`;
}

// Track per-mount teardown so the object's unmount() can also tear down the
// most recent mount (the host may call either the returned closure or unmount).
let _active = null;

export default {
  applies_to: ['*'],

  mount(container, { agentId }) {
    const el = document.createElement('div');
    el.className = 'usage-limits-tile';
    el.style.fontSize = 'var(--t-xs, 11px)';
    el.style.color = 'var(--t-3, #888)';
    el.style.padding = '4px 6px';
    container.appendChild(el);

    render(tile({ text: '…' }), el);

    let stopped = false;

    async function refresh() {
      try {
        const r = await fetch(
          `/api/plugins/usage-limits/state?agent=${encodeURIComponent(agentId)}`,
        );
        if (!r.ok) {
          render(tile({ text: `usage-limits: ${r.status}` }), el);
          return;
        }
        const j = await r.json();
        const a = (j.agents || [])[0];
        if (!a || a.error) {
          render(tile({ text: a?.error ? `error: ${a.error}` : 'no data' }), el);
          return;
        }
        const windows = Object.entries(a.windows || {}).map(([name, w]) => ({
          name,
          disabled: w.state === 'disabled',
          state: w.state,
          pct: w.pct_used.toFixed(0),
        }));
        render(tile({ windows }), el);
      } catch (e) {
        // swallow — tile is best-effort decoration
      }
    }

    refresh();
    const t = setInterval(() => {
      if (stopped) return;
      refresh();
    }, 15000);

    const teardown = () => {
      stopped = true;
      clearInterval(t);
      if (el.parentNode === container) container.removeChild(el);
      if (_active === teardown) _active = null;
    };
    _active = teardown;
    return teardown;
  },

  unmount() {
    if (_active) _active();
  },
};
