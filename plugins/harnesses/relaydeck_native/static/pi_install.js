// pi_install.js — shared "Install pi" button + live-polling helper used by
// the three pi-missing surfaces (new-agent modal, native chat widget banner,
// Context tile banner). Each surface gives us a container and an API client;
// we render the warning + button, POST `/install-pi` on click with progress
// feedback, and re-poll `/status` every ~2s until pi is detected so an
// out-of-band install (operator ran `npm i -g …` in another terminal) is
// also picked up without a manual refresh.
//
//   import { mountPiInstall } from '.../pi_install.js';
//   const handle = mountPiInstall({ container, api, onReady });
//   // ... when the parent unmounts:
//   handle.unmount();
//
// MIGRATED to @relaydeck/ui (build-less light-DOM Lit). This stays a factory
// returning { unmount, recheck } — NOT a custom element — because core modules
// (lenses/agents.js, new_agent.js) and sibling plugins import the function and
// drive the returned handle directly. The hand-rolled esc()/innerHTML/manual
// addEventListener are replaced by html``/render(tpl, container) + @click; the
// `na-warn pi-install-warn` classes and inline styles are preserved verbatim.

import { html, render, button } from '@relaydeck/ui';

const POLL_MS = 2000;

function _npmInstallLine(status) {
  const hint = status?.install_hint || '';
  const m = hint.match(/npm install -g (\S+)/);
  return m ? `npm install -g ${m[1]}` : 'npm install -g @mariozechner/pi-coding-agent';
}

export function mountPiInstall({ container, api, onReady, onStatus, compact = false }) {
  if (!container || !api) return { unmount() {} };

  let unmounted = false;
  let pollTimer = null;
  let installing = false;
  let installedOnce = false;
  // Mode drives which template paints: 'checking' (neutral first paint),
  // 'warn' (pi missing — install UI), or 'hidden' (pi present).
  let mode = 'checking';
  let lastStatus = null;
  // Progress-pane state. Lit renders this as a reactive value, so a failed
  // install no longer needs to dodge a re-render to preserve the message —
  // we just keep the text in a variable and re-paint the same template.
  let progressVisible = false;
  let progressText = '';

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function paint() {
    if (unmounted) return;
    if (mode === 'hidden') {
      container.style.display = 'none';
      render(html``, container);
      return;
    }
    if (mode === 'checking') {
      container.style.display = '';
      container.className = 'na-warn pi-install-warn';
      container.style.opacity = '0.7';
      render(html`
        <div style="display:flex;gap:10px;align-items:center;font-family:var(--f-mono);font-size:10px;color:var(--t-3)">
          <span>checking pi…</span>
        </div>`, container);
      return;
    }
    // mode === 'warn'
    container.style.display = '';
    container.className = 'na-warn pi-install-warn';
    container.style.opacity = '';
    const npmLine = _npmInstallLine(lastStatus);
    const hint = lastStatus?.install_hint
      || `Install pi (${npmLine}) — needed for relaydeck-native agents.`;
    render(html`
      <div style="display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap">
        <div style="flex:1;min-width:240px">
          <div>⚠ <b>pi</b> not on PATH ${compact ? '' : '— native agents need the pi CLI.'}</div>
          <div style="margin-top:4px;font-family:var(--f-mono);font-size:10px;color:var(--t-4)">${hint}</div>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          ${button({ size: 'sm', disabled: installing, act: 'pi-install', onClick: doInstall },
            installing ? 'Installing…' : 'Install pi')}
          ${button({ variant: 'ghost', size: 'sm', disabled: installing, title: 'Recheck',
            act: 'pi-recheck', onClick: () => probe(true) }, '↻')}
        </div>
      </div>
      <div data-pi-progress style="display:${progressVisible ? 'block' : 'none'};margin-top:8px;padding:8px;border-radius:var(--r-2);background:var(--bg-2);font-family:var(--f-mono);font-size:10px;color:var(--t-3);max-height:120px;overflow:auto;white-space:pre-wrap">${progressText}</div>`, container);
  }

  function showStatus(status) {
    if (unmounted) return;
    try { onStatus?.(status); } catch (_) {}
    lastStatus = status;
    const piOk = !!status?.pi_installed;
    if (piOk) {
      mode = 'hidden';
      paint();
      // Stop polling: pi installs are sticky enough that we don't need to
      // re-probe forever after success (the rare uninstall is rare; an SSE
      // event would be a cleaner signal than a 2s heartbeat).
      stopPolling();
      if (!installedOnce) {
        installedOnce = true;
        try { onReady?.(status); } catch (_) {}
      }
      return;
    }
    mode = 'warn';
    paint();
  }

  async function probe(force = false) {
    if (unmounted || installing) return;
    try {
      const st = await api.getJSON('/api/plugins/relaydeck-native/status');
      showStatus(st);
    } catch (_) {
      // Probe failure: leave whatever we last rendered (the neutral
      // "checking…" first paint, or the last good state); next tick
      // retries. Never flash the install warning on a transient blip.
    }
  }

  async function doInstall() {
    if (installing || unmounted) return;
    installing = true;
    let npmLine = 'npm install -g @mariozechner/pi-coding-agent';
    try {
      const st = await api.getJSON('/api/plugins/relaydeck-native/status');
      npmLine = _npmInstallLine(st);
    } catch (_) {}
    // Show progress + disable the buttons. We're definitely in 'warn' mode
    // here (install can only be triggered from the warning UI).
    progressVisible = true;
    progressText = `$ ${npmLine}\n(this can take 10-90s — npm pulls ~200 packages)\n`;
    mode = 'warn';
    paint();
    try {
      const r = await api.postJSON('/api/plugins/relaydeck-native/install-pi', {});
      installing = false;
      if (r.ok && r.pi_installed) {
        // Success: hide the banner. showStatus() handles the success path; the
        // user sees "pi installed" disappearance + the new-agent flow
        // unblocks (the parent's onReady runs).
        showStatus(r);
        return;
      }
      // Failure: keep the warning visible AND the progress message. With the
      // Lit re-render model we just update progressText + re-enable the
      // buttons (installing=false) and re-paint — no innerHTML to clobber.
      // The 2s poll keeps running so an out-of-band install still flips the UI.
      const out = (r.output || '').trim();
      progressText = `✗ install failed\n\n${r.error || 'unknown error'}\n\n${out}`;
      mode = 'warn';
      paint();
      try { onStatus?.(r); } catch (_) {}
    } catch (e) {
      installing = false;
      progressText = `✗ install request failed: ${e?.message || String(e)}\n\nFallback: run \`${npmLine}\` in a terminal, then click ↻ to recheck.`;
      mode = 'warn';
      paint();
    }
  }

  // First paint: a neutral "checking…" state instead of flashing the
  // alarming "pi not on PATH" warning. The probe will correct on the
  // first response — operators with pi installed never see the warning.
  paint();
  probe(true);
  pollTimer = setInterval(() => probe(false), POLL_MS);

  return {
    unmount() {
      unmounted = true;
      stopPolling();
    },
    // Caller can force an immediate recheck (e.g. after the user reports
    // they installed pi manually).
    recheck: () => probe(true),
  };
}
