// usage-limits — full tab panel.
//
// Lists every agent with both windows side-by-side: used / budget /
// percentage / reset-in. Polls every 10 seconds. Shares the
// /api/plugins/usage-limits/state endpoint with the tile chip.
//
// MIGRATED to @relaydeck/ui (build-less, light-DOM Lit). The dashboard host
// loads this via the framework-neutral contract, so the default export stays an
// OBJECT literal with mount(container) returning a teardown fn. Internals now
// render with html``/render() instead of innerHTML+querySelector, escaping comes
// from the kit (Lit auto-escapes interpolations), the hand-rolled state pill is
// the kit's badge() primitive, and the bespoke fmtEta formatter is preserved.

import { html, render, chip } from '@relaydeck/ui';

function fmtEta(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  return h ? `${d}d ${h}h` : `${d}d`;
}

// Map a usage-limit state onto the kit's chip tones (.chip.ok/.warn/.err/.muted
// — green/amber/red/gray). NOTE: use chip(), not badge(): .sbadge only defines
// agent-status tones (running/idle/…), so badge() would render these uncolored.
const STATE_TONE = {
  ok: 'ok',
  warn: 'warn',
  exceeded: 'err',
  disabled: 'muted',
};

function stateBadge(state) {
  return chip(state, STATE_TONE[state] || '');
}

function rowTpl(agentId, window, w) {
  const budget = w.budget_tokens > 0 ? w.budget_tokens.toLocaleString() : '—';
  const used = w.used_tokens.toLocaleString();
  const pct = w.budget_tokens > 0 ? `${w.pct_used.toFixed(1)}%` : '—';
  return html`
    <tr>
      <td style="padding:4px 8px">${agentId}</td>
      <td style="padding:4px 8px">${window}</td>
      <td style="padding:4px 8px;text-align:right">${used}</td>
      <td style="padding:4px 8px;text-align:right">${budget}</td>
      <td style="padding:4px 8px;text-align:right">${pct}</td>
      <td style="padding:4px 8px">${stateBadge(w.state)}</td>
      <td style="padding:4px 8px;text-align:right;color:#888">${fmtEta(w.seconds_until_reset)}</td>
    </tr>
  `;
}

function bodyTpl(agents) {
  const rows = [];
  for (const a of agents || []) {
    if (a.error) {
      rows.push(html`<tr><td colspan="7" style="color:#c33">${a.agent_id}: ${a.error}</td></tr>`);
      continue;
    }
    for (const [name, w] of Object.entries(a.windows || {})) {
      rows.push(rowTpl(a.agent_id, name, w));
    }
  }
  return rows.length ? rows : html`<tr><td colspan="7" style="padding:12px;color:#888">No agents.</td></tr>`;
}

function panelTpl(state) {
  return html`
    <div style="padding:16px">
      <h2 style="margin:0 0 12px 0">Usage limits</h2>
      <div id="usage-limits-status" style="font-size:12px;color:#888;margin-bottom:12px">${state.status}</div>
      <table style="border-collapse:collapse;width:100%;font-size:13px">
        <thead>
          <tr style="border-bottom:1px solid #444;text-align:left">
            <th style="padding:6px 8px">Agent</th>
            <th style="padding:6px 8px">Window</th>
            <th style="padding:6px 8px;text-align:right">Used</th>
            <th style="padding:6px 8px;text-align:right">Budget</th>
            <th style="padding:6px 8px;text-align:right">%</th>
            <th style="padding:6px 8px">State</th>
            <th style="padding:6px 8px;text-align:right">Resets in</th>
          </tr>
        </thead>
        <tbody id="usage-limits-tbody">${bodyTpl(state.agents)}</tbody>
      </table>
    </div>
  `;
}

export default {
  mount(container) {
    let stopped = false;
    const state = { status: '', agents: [] };

    const paint = () => render(panelTpl(state), container);

    async function refresh() {
      try {
        const r = await fetch('/api/plugins/usage-limits/state');
        if (!r.ok) {
          state.status = `error: HTTP ${r.status}`;
          paint();
          return;
        }
        const j = await r.json();
        state.agents = j.agents || [];
        state.status = `Updated ${new Date().toLocaleTimeString()}`;
        paint();
      } catch (e) {
        state.status = `error: ${e}`;
        paint();
      }
    }

    paint();
    refresh();
    const t = setInterval(() => {
      if (stopped) return;
      refresh();
    }, 10000);

    return () => {
      stopped = true;
      clearInterval(t);
    };
  },
};
