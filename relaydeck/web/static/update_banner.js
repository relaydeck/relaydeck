// update_banner.js — "update available" banner + in-web Update-now flow.
//
// On boot the app fetches GET /api/version (current vs latest GitHub release,
// cached server-side ~1h). When a newer release exists we show a thin dismissible
// banner under the header. "Update now" runs POST /api/update (uv tool reinstall
// + daemon restart) behind a confirm that spells out the interruption — the
// same responsibility bar as the daemon-restart modal — then polls /healthz
// and reloads to pick up the new build.
//
// Migrated to @relaydeck/ui: the banner is a lit-html render into the host's
// static bannerEl anchor; the modal uses openModal (reactive body via update()).
// Both exports keep their signatures: checkForUpdate(host), openUpdateModal(host, info).

import { html, render, nothing, icon, button, openModal } from '@relaydeck/ui';

const DISMISS_KEY = 'relaydeck.update.dismissed';

// Cap the restart poll so a daemon that never returns can't loop forever.
const POLL_MAX = 90;          // ~90s before we force a reload
const POLL_MIN_TRIES = 4;     // ignore the first few OKs (we want the NEW build)

export async function checkForUpdate(host) {
  if (!host.bannerEl) return;
  let info;
  try { info = await host.api.getJSON('/api/version'); } catch (_) { return; }
  if (!info || !info.update_available || !info.latest) { render(nothing, host.bannerEl); return; }
  // Respect a per-version dismissal (re-shows when an even newer one lands).
  if (sessionStorage.getItem(DISMISS_KEY) === info.latest) { render(nothing, host.bannerEl); return; }
  renderBanner(host, info);
}

function renderBanner(host, info) {
  const el = host.bannerEl;
  const dismiss = () => {
    sessionStorage.setItem(DISMISS_KEY, info.latest);
    render(nothing, el);
  };
  render(html`
    <div class="upd-banner">
      <span class="upd-ic">${icon('bolt', 13)}</span>
      <span class="upd-msg">Update available — <b>v${info.current}</b> → <b>v${info.latest}</b></span>
      <span class="upd-sp"></span>
      <button class="btn primary sm" data-act="update" @click=${() => openUpdateModal(host, info)}>Update now</button>
      <button class="btn ghost sm upd-x" data-act="dismiss" title="Dismiss until a newer release"
        @click=${dismiss}>${icon('x', 11)}</button>
    </div>`, el);
}

// Confirm + run the upgrade. Mirrors the daemon-restart modal: it surfaces what
// will be interrupted (via /api/daemon/restart-info) before doing it.
export function openUpdateModal(host, info) {
  if (document.querySelector('.rd-scrim.upd')) return;

  // The body re-renders reactively as we resolve restart-info → confirm → poll.
  const modal = openModal({
    heading: 'Update relaydeck',
    size: 'md',
    class: 'upd',
    content: () => html`
      <div class="upd-checking" style="color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs)">
        checking what will be interrupted…</div>`,
  });

  (async () => {
    let rinfo = {};
    try { rinfo = await host.api.getJSON('/api/daemon/restart-info'); } catch (_) {}

    // Unmanaged daemons can't self-update from the web — point at the CLI.
    if (rinfo && rinfo.managed === false) {
      modal.update(({ close }) => html`
        <div class="upd-warn">
          This daemon isn't under <code>relaydeck daemon</code> supervision, so it can't update itself
          from the web. Update from your terminal:
          <div style="margin-top:6px;font-family:var(--f-mono);color:var(--acc)">relaydeck update</div>
        </div>
        <div style="display:flex;justify-content:flex-end;margin-top:14px">
          ${button({ variant: 'ghost', act: 'cancel', onClick: () => close() }, 'Close')}
        </div>`);
      return;
    }

    const n = rinfo.running_agent_count || 0;
    const running = rinfo.running_agents || [];
    const agentsLine = n
      ? html`<b style="color:var(--warn)">${n} running agent${n === 1 ? '' : 's'}</b> will be stopped${
          running.length ? html` (${running.slice(0, 6).join(', ')}${running.length > 6 ? '…' : ''})` : nothing}.`
      : 'No agents are currently running.';

    paintConfirm(host, info, modal, agentsLine, '');
  })();
}

// The confirm step. errMsg is shown (if any) when re-painted after a failure so
// the operator sees why the last attempt didn't go through.
function paintConfirm(host, info, modal, agentsLine, errMsg) {
  let busy = false;

  const onConfirm = async () => {
    if (busy) return;
    busy = true;
    modal.update(); // re-render to disable the button
    try {
      const r = await host.api.fetch('/api/update', { method: 'POST' });
      if (!r.ok) {
        let msg = `Update failed (${r.status})`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        busy = false;
        paintConfirm(host, info, modal, agentsLine, msg);
        return;
      }
    } catch (e) {
      busy = false;
      paintConfirm(host, info, modal, agentsLine, 'Update failed: ' + e.message);
      return;
    }
    paintUpdating(modal);
    startRestartPoll(modal);
  };

  modal.update(({ close }) => html`
    <div data-body style="color:var(--t-2);font-size:var(--t-sm);line-height:1.5">
      <div style="font-size:var(--t-sm);margin-bottom:10px">Upgrade <b>v${info.current}</b> → <b>v${info.latest}</b> and restart the daemon.</div>
      <div class="upd-warn">Runs <code>uv tool install --reinstall relaydeck</code>, then restarts. This interrupts everything running:</div>
      <ul style="margin:12px 0 0;padding-left:18px;font-size:var(--t-xs);color:var(--t-2);line-height:1.7">
        <li>${agentsLine}</li>
        <li>Live terminals + event streams disconnect and reconnect.</li>
        <li>Agents with <code>auto_start</code> come back automatically; others stay stopped.</li>
      </ul>
      ${errMsg ? html`<div data-err style="color:var(--err);font-size:var(--t-xs);font-family:var(--f-mono);margin-top:10px">${errMsg}</div>` : nothing}
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        ${button({ variant: 'ghost', act: 'cancel', onClick: () => close() }, 'Cancel')}
        ${button({ variant: 'primary', act: 'confirm', disabled: busy, onClick: onConfirm },
          icon('bolt', 11), ' Update + restart')}
      </div>
    </div>`);
}

function paintUpdating(modal) {
  modal.update(() => html`
    <div data-body style="text-align:center;padding:18px 0">
      <div style="font-size:var(--t-lg);color:var(--acc);margin-bottom:8px">Updating…</div>
      <div style="color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs)">Upgrading + restarting. This page reloads automatically when it's back.</div>
    </div>`);
}

// Wait for the upgrade+restart, then reload to load the new build. The window is
// longer than a plain restart (the upgrade runs first), so be patient before
// forcing a reload. Capped at POLL_MAX so a dead daemon can't loop forever.
function startRestartPoll(modal) {
  let tries = 0;
  const poll = setInterval(async () => {
    tries++;
    try {
      const hr = await fetch('/healthz', { cache: 'no-store' });
      if (hr.ok && tries > POLL_MIN_TRIES) {
        clearInterval(poll);
        try { modal?.close?.(); } catch (_) {}
        setTimeout(() => location.reload(), 500);
        return;
      }
    } catch (_) { /* daemon down mid-restart — keep polling */ }
    if (tries > POLL_MAX) {
      clearInterval(poll);
      try { modal?.close?.(); } catch (_) {}
      location.reload();
    }
  }, 1000);
}
