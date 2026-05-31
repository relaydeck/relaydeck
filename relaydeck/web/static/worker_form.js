// worker_form.js — Edit/New worker modal (2-pane, live pipeline preview).
// Recreated from the Claude Design handoff (.ewm-* classes in workers.css).
//
// Left: form (Identity → Trigger → Attached actions as typed cards).
// Right: sticky live pipeline preview that updates as you type + a nudge.
// Footer: restart-after-save + action count + Cancel/Save.
//
// Wired to the real API: create = POST /api/agents (type:loop, config),
// edit = PATCH /api/agents/{id} config, validated via
// POST /api/automations/validate (cron validity is server-side).
//
// MIGRATION NOTE — on the @relaydeck/ui kit, but it keeps its bespoke modal
// shell. The .ewm/.ewm-scrim chrome is self-positioned by workers.css (and the
// e2e suite waits on `.ewm` + its `data-f`/`data-k`/`data-act` hooks), so this
// renders a Lit template INTO the bespoke scrim rather than going through
// openModal's `.rd-modal`. The structural chrome (head / identity / trigger /
// footer / preview anchors) is lit-html with @click/@input bindings — no
// innerHTML, no manual addEventListener. The action-card LIST and the preview
// FLOW stay IMPERATIVE against static anchor nodes ([data-actions]/[data-pv-flow])
// the reactive layer never touches: a model action mounts the shared
// mountModelSelect widget imperatively into its card, so reactively re-rendering
// the cards on every keystroke would destroy that widget and lose input focus
// (same pattern as lenses/agents.js with [data-body]).

// `icon` (Lit template) is used for the reactive chrome; `iconSVG` (string) is
// used inside the imperative action-card innerHTML — both come from the kit.
import { html, render, icon, iconSVG, esc } from '@relaydeck/ui';
import { mountModelSelect } from './model_select.js';

const INP = 'width:100%;font-family:var(--f-mono);font-size:var(--t-xs)';

// ── draft <-> API action mapping ──────────────────────────────────────
function apiToDraftAction(act) {
  if (!act || typeof act !== 'object') return { type: 'code', lang: 'python', body: '' };
  const kind = Object.keys(act)[0];
  const p = act[kind] || {};
  if (kind === 'code') return { type: 'code', lang: p.lang || 'python', body: p.body || '', emit: p.emit || '' };
  if (kind === 'bus.emit') return { type: 'bus.emit', eventName: p.type || '', data: p.data ? (typeof p.data === 'string' ? p.data : JSON.stringify(p.data)) : '' };
  if (kind === 'model') return { type: 'model', prompt: p.prompt || '', model: p.model || '', maxTokens: p.max_tokens ?? 256, emit: p.emit || '', to: p.to || '', includeTrigger: !!p.include_event };
  if (kind === 'script') return { type: 'script', path: p.path || '' };
  if (kind === 'gh') return { type: 'gh', args: Array.isArray(p.args) ? p.args.join(' ') : (p.args || '') };
  if (kind === 'agent.message') return { type: 'agent.message', to: p.to || '', body: p.body || '' };
  return { type: kind, raw: JSON.stringify(p) };
}

function draftToApiAction(a) {
  switch (a.type) {
    case 'code': { const o = { lang: a.lang || 'python', body: a.body || '' }; if (a.emit) o.emit = a.emit; return { code: o }; }
    case 'bus.emit': { const o = { type: a.eventName || '' }; if (a.data) { try { o.data = JSON.parse(a.data); } catch (_) { o.data = {}; } } return { 'bus.emit': o }; }
    case 'model': { const o = { prompt: a.prompt || '' }; if (a.model) o.model = a.model; if (a.maxTokens) o.max_tokens = parseInt(a.maxTokens, 10); if (a.includeTrigger) o.include_event = true; if (a.emit) o.emit = a.emit; if (a.to) o.to = a.to; return { model: o }; }
    case 'script': return { script: { path: a.path || '' } };
    case 'gh': return { gh: { args: (a.args || '').split(/\s+/).filter(Boolean) } };
    case 'agent.message': return { 'agent.message': { to: a.to || '', body: a.body || '' } };
    default: return { [a.type]: {} };
  }
}

function parseTrigger(schedule) {
  if (!schedule || schedule.indexOf(':') < 0) return { kind: 'interval', value: '30s' };
  const i = schedule.indexOf(':');
  return { kind: schedule.slice(0, i), value: schedule.slice(i + 1) };
}

function newAction(type = 'code') {
  if (type === 'code') return { type: 'code', lang: 'python', body: '# new action\n', emit: '' };
  if (type === 'bus.emit') return { type: 'bus.emit', eventName: '', data: '' };
  if (type === 'model') return { type: 'model', prompt: '', model: 'role:fast', maxTokens: 256, emit: '', to: '', includeTrigger: false };
  if (type === 'script') return { type: 'script', path: '' };
  if (type === 'gh') return { type: 'gh', args: '' };
  if (type === 'agent.message') return { type: 'agent.message', to: '', body: '' };
  return { type };
}

const ACTION_TYPES = ['code', 'bus.emit', 'model', 'script', 'gh', 'agent.message'];

const HINTS = {
  interval: 'e.g. 30s · 5m · 2h — fires repeatedly on that interval',
  cron: 'e.g. 0 9 * * 1-5 — standard 5-field cron',
  on_event: 'e.g. agent.error · github.* — fires on a matching bus event',
};

export function openWorkerForm(host, { worker = null, onSaved } = {}) {
  if (document.querySelector('.ewm')) return;
  const editing = !!worker;

  // draft state
  const draft = {
    id: editing ? worker.automation_id : '',
    name: editing ? (worker.name || worker.automation_id) : '',
    workspace: editing ? (worker.workspace || '') : (host.state.workspace || ''),
    trigger: editing ? parseTrigger(worker.trigger && worker.trigger.raw) : { kind: 'interval', value: '30s' },
    actions: editing ? [] : [newAction('model')],
    restart: true,
  };

  // Bespoke modal shell (self-positioned by workers.css; e2e waits on `.ewm`).
  const scrim = document.createElement('div'); scrim.className = 'ewm-scrim';
  const modal = document.createElement('div'); modal.className = 'ewm'; modal.setAttribute('role', 'dialog');
  document.body.appendChild(scrim);
  document.body.appendChild(modal);

  const wsList = host.state.workspaces || [];

  // ── close / esc ──────────────────────────────────────────────────
  function close() { scrim.remove(); modal.remove(); document.removeEventListener('keydown', onKey); }
  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);
  scrim.addEventListener('click', close);

  // Patch the error line in place (imperative) — never re-render the chrome,
  // which would tear down the mounted model-select widgets.
  function showErr(msg) {
    draft._err = msg || '';
    if (!errEl) return;
    errEl.textContent = draft._err;
    errEl.style.display = draft._err ? 'block' : 'none';
  }

  // Test run (edit only) → run-now.
  async function onTestRun() {
    try {
      const r = await host.api.fetch(`/api/automations/${encodeURIComponent(draft.id)}/run`, { method: 'POST' });
      if (!r.ok) { let m = `Test run failed (${r.status})`; try { const j = await r.json(); if (j.detail) m = j.detail; } catch (_) {} showErr(m); }
      else showErr('');
    } catch (e) { showErr('Test run failed: ' + e.message); }
  }

  // ── save ─────────────────────────────────────────────────────────
  async function onSave() {
    draft._err = '';
    if (!editing && !draft.id.trim()) { showErr('ID is required.'); return; }
    const schedule = `${draft.trigger.kind}:${draft.trigger.value.trim()}`;
    const actions = draft.actions.map(draftToApiAction);

    try {
      const vr = await host.api.postJSON('/api/automations/validate', { schedule, actions });
      if (!vr.ok) { showErr(vr.errors.join('\n')); return; }
    } catch (e) { showErr('Validation failed: ' + e.message); return; }

    try {
      if (editing) {
        await host.api.fetch(`/api/agents/${encodeURIComponent(draft.id)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ config: { schedule, actions } }),
        });
        if (draft.restart) {
          await host.api.fetch(`/api/automations/${encodeURIComponent(draft.id)}/pause`, { method: 'POST' });
          await host.api.fetch(`/api/automations/${encodeURIComponent(draft.id)}/resume`, { method: 'POST' });
        }
      } else {
        await host.api.postJSON('/api/agents', {
          id: draft.id.trim(), name: draft.name.trim() || draft.id.trim(), type: 'loop',
          workspace: draft.workspace || null, config: { schedule, actions },
        });
        if (draft.restart) await host.api.fetch(`/api/agents/${encodeURIComponent(draft.id.trim())}/start`, { method: 'POST' });
      }
      close();
      if (onSaved) onSaved(draft.id.trim());
    } catch (e) { showErr('Save failed: ' + e.message); }
  }

  // ── chrome (reactive lit-html into the bespoke scrim) ─────────────
  // Note: identity/trigger inputs are NOT `.value=`-bound — they're populated
  // once from the draft and left uncontrolled so typing never re-renders and
  // loses caret. The async edit-load below sets them imperatively.
  function renderChrome() {
    render(html`
      <div class="ewm-head">
        <div class="ttl">
          <div>
            <div class="eyebrow">worker · configurable</div>
            <h2>${editing ? 'Edit worker' : 'New worker'}</h2>
          </div>
          <div class="vline"></div>
          ${editing ? html`<span class="id-pill">${draft.id}</span>` : ''}
          <span class="chip muted">A worker runs its attached actions when a trigger fires.</span>
        </div>
        <div class="ewm-head-r">
          ${editing ? html`<button class="btn ghost" data-act="testrun" @click=${onTestRun}>${icon('play', 11)} Test run</button>` : ''}
          <button class="btn icon" data-act="close" @click=${close}>${icon('x', 12)}</button>
        </div>
      </div>

      <div class="ewm-body">
        <div class="ewm-main">
          <div class="ewm-section">
            <div class="ewm-section-head"><span class="h">Identity</span></div>
            <div class="ewm-grid three">
              <div class="ewm-field"><span class="lbl">ID</span><input data-f="id" .value=${draft.id} ?disabled=${editing} placeholder="nightly-triage"
                @input=${(e) => { draft.id = e.target.value; }}></div>
              <div class="ewm-field"><span class="lbl">Name</span><input data-f="name" .value=${draft.name} placeholder="Nightly triage"
                @input=${(e) => { draft.name = e.target.value; }}></div>
              <div class="ewm-field"><span class="lbl">Workspace</span>
                <select data-f="workspace" @change=${(e) => { draft.workspace = e.target.value; }}>
                  <option value="" ?selected=${!draft.workspace}>— none —</option>
                  ${wsList.map((w) => html`<option value=${w.name} ?selected=${w.name === draft.workspace}>${w.name}</option>`)}
                </select>
              </div>
            </div>
          </div>

          <div class="ewm-section">
            <div class="ewm-section-head"><span class="h">Trigger <span class="dim">— a schedule is just a trigger</span></span></div>
            <div class="ewm-grid trig">
              <div class="ewm-field"><span class="lbl">Kind</span>
                <select data-f="trig_kind" @change=${(e) => { draft.trigger.kind = e.target.value; renderPreview(); }}>
                  ${['interval', 'cron', 'on_event'].map((k) => html`<option ?selected=${draft.trigger.kind === k}>${k}</option>`)}
                </select>
              </div>
              <div class="ewm-field"><span class="lbl">Value</span>
                <input data-f="trig_value" .value=${draft.trigger.value} @input=${(e) => { draft.trigger.value = e.target.value; renderPreview(); }}>
                <span class="hint" data-trig-hint></span>
              </div>
            </div>
          </div>

          <div class="ewm-section">
            <div class="ewm-section-head">
              <span class="h">Attached actions <span class="dim">· runs top-to-bottom on every tick</span></span>
              <button class="btn ghost sm" data-act="add" @click=${() => { draft.actions.push(newAction('model')); renderActions(); }}>${icon('plus', 11)} Add action</button>
            </div>
            <div class="ewm-act-list" data-actions></div>
            <button class="ewm-add-action" data-act="add2" @click=${() => { draft.actions.push(newAction('code')); renderActions(); }}>${icon('plus', 11)} Add another action</button>
          </div>
        </div>

        <div class="ewm-aside">
          <div class="h">Live preview</div>
          <div class="preview-card">
            <div class="preview-trigger">
              ${icon('clock', 12)}
              <span style="color:var(--t-1)" data-pv-trig>—</span>
              <span class="lab" style="margin-left:auto" data-pv-kind>${draft.trigger.kind}</span>
            </div>
          </div>
          <div class="preview-flow" data-pv-flow></div>
          <div class="pv-warn">
            ${icon('alert', 11)}
            <span>Saving with <b style="color:var(--t-1)">restart after save</b> reloads the worker so changes take effect on the next tick.${editing ? html` <b style="color:var(--t-1)">Test run</b> fires it once now.` : ''}</span>
          </div>
          <div data-err style="${draft._err ? '' : 'display:none;'}color:var(--err);font-family:var(--f-mono);font-size:10px;white-space:pre-wrap;line-height:1.5">${draft._err || ''}</div>
        </div>
      </div>

      <div class="ewm-foot">
        <div class="ewm-foot-l">
          <label data-act="toggle-restart" @click=${(e) => { draft.restart = !draft.restart; e.currentTarget.querySelector('[data-restart]')?.classList.toggle('on', draft.restart); }}><span class="ewm-chk ${draft.restart ? 'on' : ''}" data-restart></span> restart after save</label>
          <span style="color:var(--t-4)" data-foot-count></span>
        </div>
        <div class="ewm-foot-r">
          <button class="btn ghost" data-act="cancel" @click=${close}>Cancel</button>
          <button class="btn primary" data-act="save" @click=${onSave}>${icon('edit', 11)} ${editing ? 'Save worker' : 'Create worker'}</button>
        </div>
      </div>`, modal);
  }

  // Anchors live inside the lit-rendered chrome but are NEVER reactively
  // re-rendered themselves — we populate them imperatively (model-select +
  // focus safety, mirroring lenses/agents.js [data-body]).
  let actionsEl, errEl;

  function field(label, inner) {
    return `<div class="ewm-field"><span class="lbl">${label}</span>${inner}</div>`;
  }

  // ── render action cards (imperative) ───────────────────────────────
  function renderActions() {
    actionsEl = modal.querySelector('[data-actions]');
    if (!actionsEl) return;
    actionsEl.innerHTML = '';
    draft.actions.forEach((a, idx) => actionsEl.appendChild(actionCard(a, idx)));
    renderPreview();
  }

  function actionCard(a, idx) {
    const card = document.createElement('div');
    card.className = 'ewm-act-card' + (a.type === 'model' ? ' model' : '');
    let bodyHtml = '';
    if (a.type === 'code') {
      bodyHtml = `
        <div class="ewm-grid">
          ${field('Lang', `<select data-k="lang">${['python', 'sh', 'bash'].map(l => `<option ${a.lang === l ? 'selected' : ''}>${l}</option>`).join('')}</select>`)}
          ${field('Emit (event)', `<input data-k="emit" placeholder="optional" value="${esc(a.emit || '')}">`)}
        </div>
        ${field('Body', `<textarea data-k="body" rows="5" style="${INP};line-height:1.55">${esc(a.body || '')}</textarea>`)}`;
    } else if (a.type === 'bus.emit') {
      bodyHtml = `
        ${field('Type (event name)', `<input data-k="eventName" value="${esc(a.eventName || '')}">`)}
        ${field('Data (json, optional)', `<textarea data-k="data" rows="2" style="${INP}">${esc(a.data || '')}</textarea>`)}`;
    } else if (a.type === 'model') {
      bodyHtml = `
        ${field('Prompt', `<textarea data-k="prompt" rows="3" style="${INP}">${esc(a.prompt || '')}</textarea>`)}
        <div class="ewm-grid">
          ${field('Model', `<div data-model-select></div>`)}
          ${field('Max tokens', `<input data-k="maxTokens" type="number" value="${esc(String(a.maxTokens ?? 256))}">`)}
        </div>
        <div class="ewm-grid">
          ${field('Emit (event)', `<input data-k="emit" placeholder="optional" value="${esc(a.emit || '')}">`)}
          ${field('To (agent)', `<input data-k="to" placeholder="optional" value="${esc(a.to || '')}">`)}
        </div>
        <label style="display:flex;align-items:center;gap:8px;font-family:var(--f-mono);font-size:11px;color:var(--t-2);margin-top:2px;cursor:pointer">
          <span class="ewm-chk ${a.includeTrigger ? 'on' : ''}" data-k-chk="includeTrigger"></span> include the trigger event in the prompt
        </label>`;
    } else if (a.type === 'script') {
      bodyHtml = field('Path (relative to workspace)', `<input data-k="path" value="${esc(a.path || '')}" placeholder="scripts/notify.py">`);
    } else if (a.type === 'gh') {
      bodyHtml = field('Args (space-separated)', `<input data-k="args" value="${esc(a.args || '')}" placeholder="pr comment 42 --body ack">`);
    } else if (a.type === 'agent.message') {
      bodyHtml = `${field('To (agent id)', `<input data-k="to" value="${esc(a.to || '')}">`)}${field('Body', `<textarea data-k="body" rows="2" style="${INP}">${esc(a.body || '')}</textarea>`)}`;
    }
    card.innerHTML = `
      <div class="ewm-act-head">
        <span class="drag" title="reorder (use ↑/↓)">⋮⋮</span>
        <span class="ix">${idx + 1}</span>
        <select data-k="type">${ACTION_TYPES.map(t => `<option ${a.type === t ? 'selected' : ''}>${t}</option>`).join('')}</select>
        <span class="sp"></span>
        <button class="x" data-rm>${iconSVG('x', 11)}</button>
      </div>
      <div class="ewm-act-body">${bodyHtml}</div>`;

    // type change → swap action shape
    card.querySelector('[data-k="type"]').addEventListener('change', (e) => {
      draft.actions[idx] = newAction(e.target.value);
      renderActions();
    });
    card.querySelector('[data-rm]').addEventListener('click', () => { draft.actions.splice(idx, 1); renderActions(); });
    // field inputs
    card.querySelectorAll('[data-k]:not([data-k="type"])').forEach(inp => {
      inp.addEventListener('input', () => {
        const k = inp.getAttribute('data-k');
        a[k] = inp.type === 'number' ? parseInt(inp.value, 10) || 0 : inp.value;
        renderPreview();
      });
    });
    const chk = card.querySelector('[data-k-chk]');
    if (chk) chk.addEventListener('click', () => {
      a.includeTrigger = !a.includeTrigger;
      chk.classList.toggle('on', a.includeTrigger);
      renderPreview();
    });
    // Standardized model picker (shared component) for model actions.
    if (a.type === 'model') {
      const msc = card.querySelector('[data-model-select]');
      if (msc) mountModelSelect(host, msc, {
        value: a.model || '', allowDefault: true,
        onChange: (spec) => { a.model = spec; renderPreview(); },
      });
    }
    return card;
  }

  // ── live preview (imperative) ──────────────────────────────────────
  function renderPreview() {
    const hintEl = modal.querySelector('[data-trig-hint]');
    if (hintEl) hintEl.textContent = HINTS[draft.trigger.kind] || '';
    const trigEl = modal.querySelector('[data-pv-trig]');
    if (trigEl) trigEl.textContent = draft.trigger.value ? `${draft.trigger.kind === 'interval' ? 'every ' : ''}${draft.trigger.value}` : '—';
    const kindEl = modal.querySelector('[data-pv-kind]');
    if (kindEl) kindEl.textContent = draft.trigger.kind;
    const countEl = modal.querySelector('[data-foot-count]');
    if (countEl) countEl.textContent = `· ${draft.actions.length} action${draft.actions.length === 1 ? '' : 's'}`;

    const flow = modal.querySelector('[data-pv-flow]');
    if (!flow) return;
    flow.innerHTML = '';
    if (!draft.actions.length) {
      flow.innerHTML = '<div style="padding:8px 10px;font-family:var(--f-mono);font-size:10px;color:var(--t-4)">no actions yet</div>';
      return;
    }
    draft.actions.forEach((a, i) => {
      const cls = a.type === 'model' ? 'model' : a.type === 'code' ? 'code' : a.type === 'bus.emit' ? 'bus' : '';
      const meta = a.type === 'code' ? (a.lang || '') : a.type === 'bus.emit' ? (a.eventName || '—') : a.type === 'model' ? (a.model || '—') : '';
      let desc = '';
      if (a.type === 'model' && a.prompt) desc = `"${a.prompt.slice(0, 56)}${a.prompt.length > 56 ? '…' : ''}"`;
      else if (a.type === 'bus.emit' && a.data) desc = a.data;
      else if (a.type === 'code') desc = (a.body || '').split('\n')[0];
      else if (a.type === 'script') desc = a.path || '';
      const pv = document.createElement('div');
      pv.className = 'pv-act ' + cls;
      pv.innerHTML = `<div class="row1"><span class="ix">${i + 1}</span><span class="ty">${esc(a.type)}</span><span class="meta">${esc(meta)}</span></div>${desc ? `<div class="desc">${esc(desc)}</div>` : ''}`;
      flow.appendChild(pv);
      if (i < draft.actions.length - 1) {
        const link = document.createElement('div'); link.className = 'pv-link'; flow.appendChild(link);
      }
    });
  }

  // ── first paint ────────────────────────────────────────────────────
  renderChrome();
  errEl = modal.querySelector('[data-err]');

  // ── init: on edit, load full config; else render seed ────────────
  if (editing) {
    actionsEl = modal.querySelector('[data-actions]');
    if (actionsEl) actionsEl.innerHTML = '<div style="color:var(--t-4);font-family:var(--f-mono);font-size:10px;padding:6px">loading actions…</div>';
    (async () => {
      try {
        const a = await host.api.getJSON(`/api/agents/${encodeURIComponent(draft.id)}`);
        if (a.config && typeof a.config.schedule === 'string') draft.trigger = parseTrigger(a.config.schedule);
        const acts = (a.config && Array.isArray(a.config.actions)) ? a.config.actions : [];
        draft.actions = acts.length ? acts.map(apiToDraftAction) : [newAction('model')];
        const kEl = modal.querySelector('[data-f="trig_kind"]');
        const vEl = modal.querySelector('[data-f="trig_value"]');
        if (kEl) kEl.value = draft.trigger.kind;
        if (vEl) vEl.value = draft.trigger.value;
      } catch (_) {
        draft.actions = [newAction('model')];
      }
      renderActions();
    })();
  } else {
    renderActions();
  }
}
