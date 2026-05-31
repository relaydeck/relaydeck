// onboarding.js — first-run setup wizard. Shown when no model provider is
// usable yet. Three quick, skippable steps so a fresh install lands somewhere
// useful: (1) connect a model provider, (2) add a first workspace, (3) pick a
// default "fast" model. Assumes nothing — no models are pulled, no defaults
// guessed.
//
//   import { maybeShowOnboarding } from './onboarding.js';
//   maybeShowOnboarding(app);   // fire-and-forget after boot
//
// MIGRATION NOTE. This is a bespoke overlay (not a lens/tile): the wizard owns
// the full-height `.onb-modal` chrome the CSS styles, so it renders a Lit
// template into the existing `.onb-scrim` (kept exactly) rather than the kit's
// `.rd-modal`. Step state is REACTIVE — a single paint() re-renders the whole
// modal via lit-html with @click/@input bindings, so changing step no longer
// re-attaches listeners (the old innerHTML+addEventListener render() leaked a
// fresh listener set on every step change). The two genuinely-procedural bits
// stay imperative against static anchor nodes the reactive layer never touches:
// the embedded mountModelSelect (step 3) and the inline provider key-form +
// quick-default offer (step 1), exactly like lenses/agents.js does for its
// tile body. Esc + scrim-click-skip + the SKIP_KEY sessionStorage guard are
// preserved; the document keydown listener is torn down on close.

import { html, render, nothing, icon, iconSVG, esc } from '@relaydeck/ui';
import { mountModelSelect } from './model_select.js';
import { openAddWorkspaceModal } from './overlays.js';

const SKIP_KEY = 'relaydeck.onboarding.skipped';

function isLocalUrl(u) {
  if (!u) return false;
  // Prepend a scheme if missing — `new URL('localhost:11434')` parses
  // 'localhost' as the protocol and yields hostname='', filtering local
  // Ollama-style endpoints out of `usableProviders` and re-showing the
  // onboarding wizard on every page reload despite a working setup.
  const u2 = /^[a-z][a-z0-9+.-]*:\/\//i.test(u) ? u : `http://${u}`;
  try { return ['localhost', '127.0.0.1', '::1', '0.0.0.0'].includes(new URL(u2).hostname); }
  catch (_) { return false; }
}

// A provider is "usable" if a model could run through it right now: it has a
// key, OR it's reachable with a live catalog and runs keyless (a local
// endpoint). Remote providers with a public catalog still need a key.
function usableProviders(providers) {
  return (providers || []).filter(p => {
    if (p.has_key) return true;
    if ((p.model_count || 0) <= 0) return false;
    return !p.needs_key || isLocalUrl(p.base_url || p.default_base_url || '');
  });
}

export async function maybeShowOnboarding(host) {
  if (sessionStorage.getItem(SKIP_KEY)) return;
  if (document.querySelector('.onb-scrim')) return;
  let providers = [];
  try { providers = await host.api.getJSON('/api/providers') || []; } catch (_) {}
  if (usableProviders(providers).length) return;  // already set up
  showOnboarding(host);
}

export function showOnboarding(host) {
  document.querySelector('.onb-scrim')?.remove();

  // Bespoke scrim + modal box (the CSS styles `.onb-modal` as a full-height
  // flex column with its own border/shadow). lit-html paints INTO `modal`, so
  // the scrim element itself is the stable mount the reactive layer never
  // replaces — its click handler keeps the skip-on-backdrop semantics.
  const scrim = document.createElement('div');
  scrim.className = 'overlay-scrim onb-scrim rd';
  scrim.style.cssText = 'position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.72);display:flex;align-items:center;justify-content:center;padding:24px';
  const modal = document.createElement('div');
  modal.className = 'onb-modal';
  modal.style.cssText = 'width:min(680px,96vw);max-height:90vh;display:flex;flex-direction:column;background:var(--bg-1);border:1px solid var(--line-1);border-radius:var(--r-4);box-shadow:0 24px 64px rgba(0,0,0,.45)';
  scrim.appendChild(modal);

  const STEPS = ['Provider', 'Workspace', 'Default model'];
  const state = { step: 0, providers: [], detected: [], workspaces: [], roles: [] };

  // Esc closes (skips) — registered in capture like the kit modal so it wins
  // over inner handlers; torn down on close so it never leaks.
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); skip(); } };
  document.addEventListener('keydown', onKey, true);

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey, true);
    scrim.remove();
  }
  const skip = () => { sessionStorage.setItem(SKIP_KEY, '1'); close(); };

  async function loadProviders() {
    try { state.providers = await host.api.getJSON('/api/providers') || []; } catch (_) {}
    try { const d = await host.api.getJSON('/api/providers/detect'); state.detected = (d?.candidates || []); } catch (_) {}
  }
  async function loadWorkspaces() {
    try { state.workspaces = await host.api.getJSON('/api/workspaces') || []; } catch (_) {}
  }
  async function loadRoles() {
    // Step 3 reads `fast.configured` to decide between "✓ already set"
    // and the model picker. Refreshed every time the wizard re-renders
    // so a step-1 preset-autocreate is reflected on step 3 without a
    // wizard reopen.
    try { const r = await host.api.getJSON('/api/model-roles'); state.roles = r?.roles || []; }
    catch (_) { state.roles = []; }
  }
  function fastRole() {
    return state.roles.find(r => r.name === 'fast');
  }

  function goStep(n) { state.step = n; paint(); }

  // ── reactive chrome (header + steps + footer) ───────────────────────
  function headerTpl() {
    const title = STEPS[state.step] === 'Provider' ? 'Connect a model provider'
      : STEPS[state.step] === 'Workspace' ? 'Add your first workspace'
      : 'Pick a default model';
    const dots = STEPS.map((label, i) => {
      const cls = i === state.step ? 'on' : (i < state.step ? 'done' : '');
      return html`<span class="onb-dot-step ${cls}"><span class="onb-dot-n">${i < state.step ? '✓' : i + 1}</span>${label}</span>`;
    });
    return html`
      <div class="onb-head">
        <div>
          <div class="onb-eyebrow">welcome to relaydeck</div>
          <h1 class="onb-title">${title}</h1>
        </div>
        <button class="btn icon" data-act="x" title="Close" @click=${skip}>${icon('x', 13)}</button>
      </div>
      <div class="onb-steps">${dots.map((d, i) => i ? [html`<span class="onb-dot-sep"></span>`, d] : d)}</div>`;
  }

  function footerTpl() {
    const last = state.step === STEPS.length - 1;
    const ready = usableProviders(state.providers).length > 0;
    return html`
      <div class="onb-foot">
        <button class="btn ghost" data-act="skip" @click=${skip}>Skip setup</button>
        <span style="flex:1"></span>
        ${state.step > 0 ? html`<button class="btn ghost" data-act="back" @click=${() => goStep(state.step - 1)}>Back</button>` : nothing}
        ${last
          ? html`<button class="btn primary" data-act="done" @click=${close}>Done</button>`
          : html`<button class="btn primary" data-act="next"
              title=${state.step === 0 && !ready ? 'Connect a provider, or skip' : nothing}
              @click=${() => goStep(state.step + 1)}>Next</button>`}
      </div>`;
  }

  // The body is a STATIC anchor: lit-html keeps the same `[data-body]` node
  // across paints (never reactively re-rendering its contents), so the
  // imperatively-mounted model picker / key-form survive a chrome re-paint —
  // same anchor discipline lenses/agents.js uses for its tile body.
  const bodyAnchor = document.createElement('div');
  bodyAnchor.className = 'onb-body';
  bodyAnchor.setAttribute('data-body', '');

  function paint() {
    render(html`${headerTpl()}${bodyAnchor}${footerTpl()}`, modal);
    if (state.step === 0) renderProviderStep(bodyAnchor);
    else if (state.step === 1) renderWorkspaceStep(bodyAnchor);
    else renderModelStep(bodyAnchor);
  }

  // Operator-facing recommendation order — surfaces the four providers
  // most new operators reach for first. These match the catalog entries in
  // relaydeck/providers_extra.py KNOWN; if any aren't in the registry,
  // they're silently skipped (no broken buttons).
  const RECOMMENDED_PROVIDERS = ['openrouter', 'anthropic', 'openai', 'deepseek'];

  function sortByRecommendation(providers) {
    // 1) key already set, alpha; 2) recommended, in RECOMMENDED_PROVIDERS
    // order; 3) everything else, alpha. The "More providers" section in the
    // UI hides #3 behind a disclosure so the recommended four are the
    // first thing the operator sees.
    const set = providers.filter(p => p.has_key).sort((a, b) => a.name.localeCompare(b.name));
    const rec = RECOMMENDED_PROVIDERS
      .map(n => providers.find(p => p.name === n && !p.has_key))
      .filter(Boolean);
    const recSet = new Set([...set, ...rec].map(p => p.name));
    const rest = providers
      .filter(p => !recSet.has(p.name))
      .sort((a, b) => a.name.localeCompare(b.name));
    return { primary: [...set, ...rec], rest };
  }

  async function autoCreatePresetAndSetRole(providerName, keyenv, msg) {
    // After a provider key is saved, propose ONE-CLICK "use this provider's
    // default model for role:fast" so the operator isn't stranded between
    // step 1 (key) and step 3 (model). The first catalog entry is a sensible
    // sentinel — every provider in `providers_extra.py:KNOWN` lists its
    // canonical model first, and custom providers fall back to whatever
    // they advertise.
    let models = [];
    try {
      models = await host.api.getJSON(`/api/providers/${encodeURIComponent(providerName)}/models`) || [];
    } catch (_) { /* network/auth — no autocreate offer; operator handles in step 3 */ }
    if (!Array.isArray(models) || !models.length) return null;
    return models;
  }

  // ── Step 1: provider ────────────────────────────────────────────────
  // The card list (recommended grid + local detections + key form) is built
  // IMPERATIVELY into the body anchor: it carries an inline, multi-stage flow
  // (paste key → save → reveal quick-default offer) that mutates in place
  // without re-painting the wizard chrome, so a re-paint never wipes a
  // half-typed key. A provider logo from the daemon's models.dev proxy;
  // onerror hides the img so a missing logo never breaks the card layout.
  function provLogo(p) {
    if (!p.logo_url) return '';
    return `<img src="${esc(p.logo_url)}" alt="" width="16" height="16" loading="lazy"
      style="flex:none;border-radius:3px" onerror="this.style.display='none'">`;
  }
  function renderProviderStep(body) {
    const localCands = state.detected.filter(c => !c.already_configured);
    const ready = usableProviders(state.providers);
    const keyProvidersAll = state.providers.filter(p => p.needs_key);
    const { primary, rest } = sortByRecommendation(keyProvidersAll);
    const keyProviders = primary;

    body.innerHTML = `
      <div class="onb-sub">relaydeck assumes nothing — pick where your models come from. Nothing is downloaded for you.</div>
      ${ready.length ? `<div class="onb-ready">${iconSVG('check', 13) || '✓'} Connected: <b>${ready.map(p => esc(p.name)).join(', ')}</b>.</div>` : ''}
      <div class="onb-sec">
        <div class="onb-sec-h">${iconSVG('cpu', 13) || ''}Local models on this machine</div>
        ${localCands.length ? localCands.map(c => `
          <div class="onb-card">
            <div class="onb-dot ok"></div>
            <div style="flex:1">
              <div class="onb-card-t">${esc(c.label)} <span class="onb-mono">${esc(c.base_url)}</span></div>
              <div class="onb-card-h">${c.model_count} model${c.model_count === 1 ? '' : 's'} available</div>
            </div>
            <button class="btn sm primary" data-add-local='${esc(JSON.stringify({ name: c.suggested_name, base_url: c.base_url, api: c.api }))}'>Add + use</button>
          </div>`).join('')
        : `<div class="onb-empty">No local server detected (Ollama 11434, vLLM 8000, LM Studio 1234). Start one, or connect an API provider below.</div>`}
      </div>
      <div class="onb-sec">
        <div class="onb-sec-h">${iconSVG('vault', 13) || ''}Connect a provider (API key)</div>
        <div class="onb-prov-label" style="font-family:var(--f-mono);font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--t-4);margin:4px 0 6px">Recommended</div>
        <div class="onb-grid">
          ${keyProviders.map(p => {
            const isRec = RECOMMENDED_PROVIDERS.includes(p.name);
            return `
            <button class="onb-prov ${p.has_key ? 'set' : ''}${isRec && !p.has_key ? ' rec' : ''}" data-prov="${esc(p.name)}" data-keyenv="${esc(p.key_env)}">
              ${provLogo(p)}<span class="onb-prov-n">${esc(p.name)}</span>
              <span class="onb-prov-s">${p.has_key ? '✓ key set' : 'add key'}</span>
            </button>`;
          }).join('')}
        </div>
        ${rest.length ? `
          <details class="onb-more" style="margin-top:10px">
            <summary style="cursor:pointer;font-family:var(--f-mono);font-size:10px;color:var(--t-3);letter-spacing:.05em">More providers (${rest.length})</summary>
            <div class="onb-grid" style="margin-top:8px">
              ${rest.map(p => `
                <button class="onb-prov ${p.has_key ? 'set' : ''}" data-prov="${esc(p.name)}" data-keyenv="${esc(p.key_env)}">
                  ${provLogo(p)}<span class="onb-prov-n">${esc(p.name)}</span>
                  <span class="onb-prov-s">${p.has_key ? '✓ key set' : 'add key'}</span>
                </button>`).join('')}
            </div>
          </details>` : ''}
        <div data-keyform></div>
      </div>`;

    body.querySelectorAll('[data-add-local]').forEach(btn => {
      btn.addEventListener('click', async () => {
        btn.disabled = true; btn.textContent = 'adding…';
        try {
          const cfg = JSON.parse(btn.getAttribute('data-add-local'));
          const r = await host.api.fetch('/api/providers/detect', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg),
          });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          await loadProviders(); paint(); try { host.render && host.render(); } catch (_) {}
        } catch (_) { btn.disabled = false; btn.textContent = 'retry'; }
      });
    });

    body.querySelectorAll('[data-prov]').forEach(btn => {
      btn.addEventListener('click', () => {
        const keyenv = btn.getAttribute('data-keyenv');
        const name = btn.getAttribute('data-prov');
        const kf = body.querySelector('[data-keyform]');
        kf.innerHTML = `
          <div class="onb-keyform">
            <label class="onb-klbl">${esc(name)} key <span class="onb-mono">\${vault:${esc(keyenv)}}</span></label>
            <div style="display:flex;gap:8px">
              <input data-keyval type="password" placeholder="paste API key" style="flex:1;font-family:var(--f-mono);font-size:var(--t-xs)">
              <button class="btn sm primary" data-savekey>Save</button>
            </div>
            <div class="onb-kmsg" data-kmsg></div>
            <div data-after-save></div>
          </div>`;
        const inp = kf.querySelector('[data-keyval]'); inp.focus();
        const after = kf.querySelector('[data-after-save]');
        const msg = (t, c) => { const m = kf.querySelector('[data-kmsg]'); m.textContent = t; m.style.color = c || 'var(--t-3)'; };

        async function showQuickDefault() {
          // Offer one-click "create a preset + set as fast role" with the
          // provider's recommended model. Skipping is fine — step 3 still
          // shows the full model picker.
          after.innerHTML = `<div class="onb-empty" style="margin-top:8px">Loading ${esc(name)} catalog…</div>`;
          let models = [];
          try {
            const r = await autoCreatePresetAndSetRole(name, keyenv, msg);
            models = r || [];
          } catch (_) { models = []; }
          if (!models.length) {
            after.innerHTML = `<div class="onb-empty" style="margin-top:8px">${esc(name)} is connected. Pick a default model on the last step.</div>`;
            return;
          }
          // Top 5: keep the dropdown short. Operator can pick a different
          // one later in Models → Defaults.
          const top = models.slice(0, 5);
          const presetName = `${name}-fast`;
          after.innerHTML = `
            <div class="onb-sub" style="margin-top:10px;color:var(--t-2)">${iconSVG('check', 12) || '✓'} key saved. Use a default model for the <b>fast</b> role now?</div>
            <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
              <select data-default-model style="flex:1;font-family:var(--f-mono);font-size:var(--t-xs)">
                ${top.map(m => `<option value="${esc(m.id)}">${esc(m.id)}</option>`).join('')}
              </select>
              <button class="btn sm primary" data-set-default>Set as default</button>
              <button class="btn sm ghost" data-skip-default>Skip</button>
            </div>
            <div class="onb-kmsg" data-default-msg></div>`;
          const sel = after.querySelector('[data-default-model]');
          const dmsg = (t, c) => { const m = after.querySelector('[data-default-msg]'); m.textContent = t; m.style.color = c || 'var(--t-3)'; };
          after.querySelector('[data-set-default]').addEventListener('click', async () => {
            const model = sel.value;
            if (!model) return;
            try {
              // Create the preset first (idempotent on the YAML write — the
              // file is overwritten). If it already exists with the same
              // provider/model, the operator's intent is preserved.
              const pr = await host.api.fetch('/api/presets', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: presetName, provider: name, model }),
              });
              if (!pr.ok) throw new Error(`preset create failed (HTTP ${pr.status})`);
              const rr = await host.api.fetch('/api/model-roles/fast', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ spec: presetName }),
              });
              if (!rr.ok) throw new Error(`role assign failed (HTTP ${rr.status})`);
              after.innerHTML = `<div class="onb-ready" style="margin-top:10px">${iconSVG('check', 13) || '✓'} Preset <b>${esc(presetName)}</b> created and assigned to <code>role:fast</code>. <code>role:fast</code> now resolves to <code>${esc(name)}/${esc(model)}</code>.</div>`;
              // Refresh role state so step 3 sees the assignment and
              // renders the "already configured" confirmation instead of
              // a redundant picker.
              await loadRoles();
            } catch (e) {
              dmsg(`Couldn't set default: ${e.message}. You can set it later in Models → Defaults.`, 'var(--err)');
            }
          });
          after.querySelector('[data-skip-default]').addEventListener('click', () => {
            after.innerHTML = `<div class="onb-empty" style="margin-top:8px">${esc(name)} connected. Pick a default model on the last step.</div>`;
          });
        }

        const save = async () => {
          if (!inp.value) { msg('Enter a key first.', 'var(--warn)'); return; }
          try {
            const r = await host.api.fetch(`/api/vault/keys/${encodeURIComponent(keyenv)}`, {
              method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: inp.value }),
            });
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            msg(`✓ Key saved for ${name}`, 'var(--ok)');
            // Refresh provider state in the background; reveal the
            // quick-default offer inline. We deliberately do NOT re-paint
            // here — that wiped the keyform on success and left the operator
            // hunting for what to click next.
            await loadProviders();
            try { host.render && host.render(); } catch (_) {}
            showQuickDefault();
          } catch (e) { msg('Save failed: ' + e.message, 'var(--err)'); }
        };
        kf.querySelector('[data-savekey]').addEventListener('click', save);
        inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
      });
    });
  }

  // ── Step 2: workspace ──────────────────────────────────────────────
  // The plain text-input that lived here had no validation, no directory
  // picker, and silently let operators paste typos that 4xx'd at submit.
  // We swap it for a CTA that opens the existing addws-modal — the same
  // surface the dashboard +N button uses — so the operator gets the rich
  // directory browser + the ✓ exists / ✓ git / ⚠ already-workspace chip
  // strip. The wizard returns to the workspaces step after the modal
  // closes, with the new workspace already present in the list.
  function renderWorkspaceStep(body) {
    const have = state.workspaces.length;
    body.innerHTML = `
      <div class="onb-sub">A workspace is a directory your agents work in. Point relaydeck at a project to spawn agents there.</div>
      ${have ? `<div class="onb-ready">${iconSVG('check', 13) || '✓'} ${have} workspace${have === 1 ? '' : 's'}: <b>${state.workspaces.slice(0, 5).map(w => esc(w.name)).join(', ')}</b></div>` : ''}
      <div class="onb-sec">
        <div class="onb-sec-h">${iconSVG('workspace', 13) || ''}Add a workspace</div>
        <div style="display:flex;flex-direction:column;gap:8px">
          <button class="btn primary" data-add-ws style="align-self:flex-start;display:inline-flex;align-items:center;gap:8px">
            ${iconSVG('folder_open', 13) || ''}Browse + add a workspace
          </button>
          <div class="onb-empty">
            Opens a directory picker on the daemon host — pick a folder and
            relaydeck validates it (✓ exists · ✓ git repo · ⚠ already a
            workspace) before you confirm. Defaults to <code>messaging</code>
            + <code>skills</code> plugins (change in the modal or later in
            Workspaces).
          </div>
        </div>
      </div>`;
    body.querySelector('[data-add-ws]').addEventListener('click', () => {
      openAddWorkspaceModal(host, async (name, opts) => {
        // The modal handled the POST + (optionally) the active-workspace
        // switch on the daemon side. The dashboard's in-memory
        // `host.state.workspace` lags behind that server update until the
        // SSE event lands — and the operator clicking "Done" on the
        // wizard right after wouldn't see the new workspace selected.
        // Eagerly call `setWorkspace(name)` so the dashboard switches
        // immediately. setWorkspace is a no-op when name === current, so
        // calling it unconditionally is safe when setActive was off.
        await loadWorkspaces();
        try { await host.reloadWorkspaces?.(); } catch (_) {}
        if (opts && opts.setActive !== false && name) {
          try { host.setWorkspace?.(name); } catch (_) {}
        }
        try { host.render && host.render(); } catch (_) {}
        paint();
      });
    });
  }

  // ── Step 3: default model ──────────────────────────────────────────
  function renderModelStep(body) {
    // If step 1's preset-autocreate flow already wired role:fast (or the
    // operator did it manually somewhere else), step 3 would be a
    // confusing second-place-to-do-the-same-thing. Detect the configured
    // state and show a confirmation card with an "Override" affordance
    // instead of dropping the picker back in front of the operator.
    const fast = fastRole();
    if (fast && fast.configured) {
      body.innerHTML = `
        <div class="onb-sub">${iconSVG('check', 13) || '✓'} <b>role:fast</b> is set. Every other role can be configured later in Models → Defaults.</div>
        <div class="onb-sec">
          <div class="onb-sec-h">${iconSVG('diamond', 13) || ''}Default "fast" model — configured</div>
          <div class="onb-card" style="display:flex;flex-direction:column;gap:6px">
            <div style="display:flex;align-items:center;gap:8px">
              <span class="onb-dot ok"></span>
              <div>
                <div class="onb-card-t"><code>${esc(fast.configured)}</code></div>
                <div class="onb-card-h">resolves to <code>${esc(fast.effective || fast.configured)}</code></div>
              </div>
            </div>
          </div>
          <div style="display:flex;justify-content:flex-end;margin-top:10px;gap:8px">
            <button class="btn ghost sm" data-override>Override</button>
          </div>
          <div data-override-slot></div>
        </div>`;
      body.querySelector('[data-override]').addEventListener('click', () => {
        // Render the picker into the slot below the confirmation. The
        // confirmation stays visible so the operator sees what they're
        // about to replace.
        renderPickerInto(body.querySelector('[data-override-slot]'));
      });
      return;
    }
    // Unset — full step chrome + picker below.
    body.innerHTML = `
      <div class="onb-sub">Optionally set a default model for the <b>fast</b> role, so <code>role:fast</code> resolves out of the box. You can set every role later in Models → Defaults.</div>
      <div class="onb-sec">
        <div class="onb-sec-h">${iconSVG('diamond', 13) || ''}Default "fast" model</div>
        <div data-picker-slot></div>
      </div>`;
    renderPickerInto(body.querySelector('[data-picker-slot]'));

    function renderPickerInto(target) {
      if (!target) return;
      target.innerHTML = `
        <div>
          <div data-modelpick></div>
          <div style="display:flex;justify-content:flex-end;margin-top:10px">
            <button class="btn sm primary" data-setrole disabled>Set as default</button>
          </div>
          <div class="onb-kmsg" data-rolemsg></div>
        </div>`;
      const pickEl = target.querySelector('[data-modelpick]');
      const setBtn = target.querySelector('[data-setrole]');
      const msg = (t, c) => { const m = target.querySelector('[data-rolemsg]'); m.textContent = t; m.style.color = c || 'var(--t-3)'; };
      let spec = '';
      // Imperative widget mount into a static anchor — kept procedural per the
      // dynamic-import/widget-hosting rule (parity with lenses/agents.js).
      mountModelSelect(host, pickEl, {
        allowDefault: false,
        onChange: (v) => { spec = (v || '').trim(); setBtn.disabled = !spec; },
      }).catch(() => { pickEl.innerHTML = '<div class="onb-empty">Model picker unavailable — set defaults later in Models → Defaults.</div>'; });
      setBtn.addEventListener('click', async () => {
        if (!spec) return;
        setBtn.disabled = true;
        try {
          const r = await host.api.fetch('/api/model-roles/fast', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ spec }),
          });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const j = await r.json().catch(() => ({}));
          msg('✓ Default fast model set' + (j.warning ? ` (note: ${j.warning})` : '') + '.', 'var(--ok)');
          // Refresh local roles + re-paint this step so the operator
          // sees the confirmation card instead of the picker.
          await loadRoles(); paint();
        } catch (e) { msg('Failed: ' + e.message, 'var(--err)'); setBtn.disabled = false; }
      });
    }
  }

  (async () => {
    await Promise.all([loadProviders(), loadWorkspaces(), loadRoles()]);
    paint();
  })();
  scrim.addEventListener('click', (e) => { if (e.target === scrim) skip(); });
  document.body.appendChild(scrim);
}
