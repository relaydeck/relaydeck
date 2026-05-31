// new_agent.js — the type-first "New agent" wizard, in two curated steps.
//
// Flow: STEP 1 pick a harness TYPE (branded cards, incl. the relaydeck
// operator + any linked Hermes/OpenClaw runtimes) → the dialog cross-fades to
// STEP 2, a step curated to *that* harness: its logo + accent up top, then the
// essentials (id · workspace · model) always in view, with purpose / the
// harness's own launch options / workspace plugins / identity preamble below.
//
// Two paces: GUIDED (default) lays everything out with a live "what gets baked"
// preview; QUICK hides the deep options and spawns on smart defaults — one
// glance from card to Spawn. The choice is remembered.
//
// All harness-specific config is *pushed up* from /api/harnesses (the harness
// plugin's own metadata: brand, accent, model policy, launch options) — the
// wizard never branches on harness type names, so a new harness plugin gets a
// curated, branded step for free.
//
// Everything lands in the same POST /api/agents the CLI uses (config keys +
// config.args + system_prompt), so it stays at CLI parity. External runtimes
// spawn via the external plugin's spawn endpoint instead.
//
// MIGRATION NOTE (build-less light-DOM Lit on @relaydeck/ui). The wizard is a
// single reactive RelayElement (`<rd-new-agent>`): step / pace / selection /
// the field values are reactive props, so the cards, the branded config head,
// and every section render declaratively from state (no innerHTML / no
// querySelector wiring). Three regions stay IMPERATIVE — they host foreign
// widgets the reactive layer must never re-render — mounted into static anchor
// nodes exactly like lenses/agents.js does for [data-body]:
//   • the flex / relaydeck model picker (mountModelSelect),
//   • the native-harness "pick…" model picker (mountModelSelect),
//   • the pi-install widget (dynamic import, polls /status).
// `openNewAgentModal(host, prefill)` keeps its exact signature + the bespoke
// `.overlay-scrim`/`.na-modal` shell, the single-instance guard, Escape close,
// and pi-widget teardown on close.

import {
  RelayElement, html, nothing, render, unsafeHTML, ifDefined, liveDirective,
} from '@relaydeck/ui';
import { esc, iconSVG } from './primitives.js';
import { mountModelSelect } from './model_select.js';
import { openAddWorkspaceModal } from './overlays.js';
import { harnessMark, harnessAccent } from './harness_brand.js';

const LBL = 'display:block;font-family:var(--f-mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--t-3);margin-bottom:5px';
const PANEL = 'padding:10px 12px;background:var(--bg-2);border:1px solid var(--line-2);border-radius:var(--r-2)';

// Mirror of relaydeck.config.AGENT_ID_RE — keep the two in sync. The
// server re-validates (POST /api/agents → 400), this is just live UX.
const AID_RE = /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/;
const AID_RULE = 'lowercase letters, numbers, dashes · start with a letter · no spaces/underscores';

const MODE_KEY = 'rd.naMode';   // 'guided' | 'quick'

// Lit template for one vendored icon (string SVG → safe inline). The icons are
// self-authored trusted markup (primitives.iconSVG).
const ic = (name, size = 14) => html`${unsafeHTML(iconSVG(name, size) || '')}`;

// Default operator persona seeded into the relaydeck-native "soul" field — an
// editable suggestion (saved as config.soul on spawn unless cleared). Pre-fills
// the textarea so a native agent spawns with a sensible persona by default.
const SUGGESTED_SOUL = "You are the operator for this workspace. Track in-flight and blocked work "
  + "across its agents, summarize status and risk concisely, and coordinate by messaging the "
  + "right agent. Be terse and concrete — prefer specifics over filler.";

// ── the wizard element ──────────────────────────────────────────────────
class NewAgentModal extends RelayElement {
  static properties = {
    // Primitive UI state (drive declarative re-render).
    step: { type: String },
    mode: { type: String },
    id: { type: String },
    workspace: { type: String },
    purpose: { type: String },
    harnessModel: { type: String },
    systemPrompt: { type: String },
    relaydeckSoul: { type: String },
    inject: { type: Boolean },
    err: { type: String },
    keyWarn: { type: String },
    modelWarn: { type: String },
    idHint: { type: String },
    idHintBad: { type: Boolean },
    preamble: { type: String },
    cliEq: { type: String },
    // Collections + handles (imperative — request updates explicitly).
    harnesses: { attribute: false },
    catalog: { attribute: false },
    wsList: { attribute: false },
    selected: { attribute: false },
    loaded: { attribute: false },
    noWorkspaces: { attribute: false },
    injections: { attribute: false },
  };

  constructor() {
    super();
    this.host = null;
    this.prefill = {};
    // UI state
    this.step = 'harness';
    this.mode = 'guided';
    this.id = '';
    this.workspace = '';
    this.purpose = '';
    this.harnessModel = '';
    this.systemPrompt = '';
    this.relaydeckSoul = SUGGESTED_SOUL;
    this.inject = true;
    this.err = '';
    this.keyWarn = '';
    this.modelWarn = '';
    this.idHint = AID_RULE;
    this.idHintBad = false;
    this.preamble = 'composing…';
    this.cliEq = 'pick a harness to start';
    // Collections
    this.harnesses = [];      // /api/harnesses
    this.catalog = [];        // /api/workspace-plugins (globally enabled)
    this.wsList = [];         // /api/workspaces
    this.selected = null;     // selected harness entry
    this.loaded = false;
    this.noWorkspaces = false;
    this.injections = [];
    // Workspace → current plugins[]; chosen plugin set (Set of names).
    this.wsPlugins = {};
    this.chosen = new Set();
    // Launch-option values keyed by option key (text/select/bool/args).
    this.launchVals = {};
    // Relaydeck native tool toggles (default all on).
    this.tools = { read: true, write: true, bash: true, relaydeck: true, manage: true, dashboard: true };
    // Imperative widget handles.
    this._modelSel = null;        // flex / relaydeck picker handle
    this._nativePicker = null;    // native "pick…" picker handle
    this._nativePickerOpen = false;
    this._piWidget = null;
    this._provs = null;
    this._previewTimer = null;
    this._closer = null;          // set by openNewAgentModal (scrim teardown)
  }

  // ── data load ─────────────────────────────────────────────────────────
  async load() {
    const host = this.host;
    const wsDefault = this.prefill.workspace || host.state.workspace
      || (host.state.workspaces?.[0]?.name || '');
    const [h, c, ws] = await Promise.all([
      host.api.getJSON('/api/harnesses').catch(() => ({ harnesses: [] })),
      host.api.getJSON('/api/workspace-plugins').catch(() => []),
      host.api.getJSON('/api/workspaces').catch(() => (host.state.workspaces || [])),
    ]);
    this.harnesses = h.harnesses || [];
    this.catalog = (c || []).filter((p) => p.globally_enabled !== false);
    const wsList = ws || host.state.workspaces || [];
    this.wsList = wsList;
    wsList.forEach((w) => { this.wsPlugins[w.name] = w.plugins || []; });
    this.workspace = wsDefault || (wsList[0]?.name || '');
    // No workspaces yet → a blank dropdown is a hostile dead-end. Surface a
    // clear empty state and route the operator to the addws-modal CTA. Spawn
    // stays disabled (updateSpawnGate) until a workspace exists.
    this.noWorkspaces = !wsList.length;
    this.loaded = true;
    this.requestUpdate();
  }

  // ── step navigation ─────────────────────────────────────────────────
  goToStep(step) {
    if (step === 'config' && !this.selected) return;
    this.step = step;
    // The config pane animates in; nudge focus to the id field on arrival.
    if (step === 'config') {
      setTimeout(() => { try { this.querySelector('[data-f="id"]')?.focus(); } catch (_) {} }, 60);
    }
  }

  selectType(entry) {
    this.selected = entry;
    // Reset per-harness imperative state for a clean curated step.
    this._teardownNativePicker();
    this._teardownPiWidget();
    this._modelSel = null;
    this.harnessModel = '';
    this.modelWarn = '';
    this.keyWarn = '';
    // Seed the plugin checkboxes to the workspace's ACTUAL enabled set (a
    // checked box = enabled on this workspace — what you see is what's saved).
    this.chosen = this._preselectedPlugins(this.workspace);
    this.requestUpdate();
    this.goToStep('config');
    // After the new config DOM exists, (re)mount imperative islands + warnings.
    this.updateComplete.then(() => {
      this._mountModelField();
      this._mountTypeSection();
      this.checkRelaydeckKey();
      this.checkModelCompat();
      this.refreshCli();
      this.checkId();
      this.doPreview();
    });
  }

  setMode(m) {
    this.mode = m;
    try { localStorage.setItem(MODE_KEY, m); } catch (_) {}
  }

  // ── card grid (step 1) ───────────────────────────────────────────────
  _cards() {
    if (!this.harnesses.length) return html`<div class="na-loading">No harnesses available.</div>`;
    return this.harnesses.map((e) => {
      const off = !e.available;
      // CLI wrapped by this harness isn't on PATH — selectable, but the agent
      // can't start until it's installed (the spawn errors "command not found").
      const noCli = e.cli && e.cli_installed === false;
      const cls = ['na-card'];
      if (off) cls.push('na-card-off');
      if (noCli) cls.push('na-card-nocli');
      const on = this.selected
        && this.selected.type === e.type
        && (this.selected.external_id || '') === (e.external_id || '');
      if (on) cls.push('na-card-on');
      const title = off ? 'Plugin not loaded — enable it in Settings → Plugins'
        : (noCli ? `The ${e.cli} CLI isn't on PATH — install it before this agent can run` : '');
      return html`<button class=${cls.join(' ')} data-type=${e.type} data-ext=${e.external_id || ''}
          style=${`--brand:${harnessAccent(e)}`}
          ?disabled=${off} title=${ifDefined(title || undefined)}
          @click=${() => this.selectType(e)}>
        <span class="na-card-top">
          ${unsafeHTML(harnessMark(e, { size: 38, radius: 9 }))}
          <span class="na-card-go">${ic('arrow_r', 14)}</span>
        </span>
        <span class="na-card-name">${e.label}</span>
        <span class="na-card-blurb">${e.blurb || ''}</span>
        <span class="na-card-tags">
          ${e.kind === 'external' ? html`<span class="na-tag na-tag-ext">linked</span>`
            : e.kind === 'operator' ? html`<span class="na-tag">native</span>` : nothing}
          ${noCli ? html`<span class="na-tag na-tag-warn">${e.cli} missing</span>` : nothing}
        </span>
      </button>`;
    });
  }

  // ── config head: branded harness identity + pace toggle (step 2) ──────
  _configHead() {
    const entry = this.selected;
    if (!entry) return nothing;
    const kindLabel = entry.kind === 'external' ? 'Linked runtime'
      : entry.kind === 'operator' ? 'Fleet operator' : 'Native harness';
    return html`<div class="na-config-head" data-config-head style=${`--brand:${harnessAccent(entry)}`}>
      <div class="na-ch-id">
        ${unsafeHTML(harnessMark(entry, { size: 44, radius: 11 }))}
        <div class="na-ch-text">
          <div class="na-ch-name">${entry.label}</div>
          <div class="na-ch-blurb">${entry.blurb || ''}</div>
        </div>
      </div>
      <div class="na-ch-aside">
        <span class="na-ch-kind">${kindLabel}</span>
        <div class="na-pace" role="tablist">
          <button type="button" class="na-pace-i ${this.mode === 'guided' ? 'on' : ''}"
            data-pace="guided" @click=${() => this.setMode('guided')}>Guided</button>
          <button type="button" class="na-pace-i ${this.mode === 'quick' ? 'on' : ''}"
            data-pace="quick" @click=${() => this.setMode('quick')}>Quick</button>
        </div>
      </div>
    </div>`;
  }

  // ── model field (step 2 essentials) ──────────────────────────────────
  // Harness-type model policy: native harnesses (codex/claude/cursor) use their
  // own account auth — optional harness-model override only. flex (pi/opencode)
  // and relaydeck-native use the shared preset picker. The picker(s) mount
  // imperatively into anchor nodes (see _mountModelField).
  _modelField() {
    const entry = this.selected;
    if (!entry || entry.kind === 'external') {
      return html`<div data-model-wrap style="display:none"></div>`;
    }
    const policy = entry.model_policy || 'flex';
    if (policy === 'native') {
      const help = entry.model_help || 'Leave empty for the harness account default.';
      // A native harness with no model_config_key has no CLI model flag to set
      // (e.g. agy — model is account-managed, switched in-session via /model).
      // Show the guidance only: no input AND no picker (nothing to pass it to).
      if (!entry.model_config_key) {
        return html`<div data-model-wrap>
          <label style=${LBL}>Model</label>
          <div style="font-family:var(--f-mono);font-size:9px;color:var(--t-4);line-height:1.35">${help}</div>
        </div>`;
      }
      // Native harnesses use their own account namespace (sonnet, gpt-5.3-codex,
      // composer-2, or an external/local id when routing), so the field is a
      // free-text override — NOT the preset-centric picker. An opt-in toggle
      // reveals the standard picker as an *assist*; whatever it yields lands in
      // the SAME text field as a plain id (provider prefix stripped). Placeholder
      // /help come from the harness plugin param (HARNESS_META).
      const ph = entry.model_placeholder || 'model id (optional)';
      return html`<div data-model-wrap>
        <label style=${LBL}>Model</label>
        <input data-f="harness_model" placeholder=${ph}
          style="width:100%;font-family:var(--f-mono);font-size:var(--t-xs)"
          .value=${liveDirective(this.harnessModel)}
          @input=${(e) => { this.harnessModel = e.target.value; this.refreshCli(); this.checkModelCompat(); }}>
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-top:3px">
          <div style="font-family:var(--f-mono);font-size:9px;color:var(--t-4);line-height:1.35;flex:1">${help}</div>
          <button type="button" data-pick-toggle @click=${() => this._toggleNativePicker()}
            style="flex:0 0 auto;background:none;border:none;color:var(--acc);cursor:pointer;font-family:var(--f-mono);font-size:9px;padding:0;white-space:nowrap">${this._nativePickerOpen ? '✕ type instead' : '⌕ pick…'}</button>
        </div>
        <div data-native-picker style=${this._nativePickerOpen ? 'margin-top:6px' : 'display:none;margin-top:6px'}></div>
      </div>`;
    }
    // flex / relaydeck → shared preset picker (mounted imperatively).
    return html`<div data-model-wrap>
      <label style=${LBL}>Model</label>
      <div data-model-select></div>
    </div>`;
  }

  // Mount/refresh the imperative model picker(s) for the current selection.
  _mountModelField() {
    const entry = this.selected;
    if (!entry || entry.kind === 'external') return;
    const policy = entry.model_policy || 'flex';
    if (policy === 'native') {
      // The native picker mounts lazily when the operator clicks "pick…".
      if (this._nativePickerOpen) this._openNativePicker();
      return;
    }
    const slot = this.querySelector('[data-model-select]');
    if (!slot) return;
    this._modelSel = null;
    mountModelSelect(this.host, slot, {
      value: '', allowDefault: true,
      onChange: () => { this.checkRelaydeckKey(); this.checkModelCompat(); this.refreshCli(); this.updateSpawnGate(); },
    }).then((h) => { this._modelSel = h; this.checkRelaydeckKey(); this.checkModelCompat(); this.updateSpawnGate(); });
  }

  _toggleNativePicker() {
    this._nativePickerOpen = !this._nativePickerOpen;
    try { localStorage.setItem('rd.nativeModelPicker', this._nativePickerOpen ? '1' : '0'); } catch (_) {}
    this.requestUpdate();
    if (this._nativePickerOpen) this.updateComplete.then(() => this._openNativePicker());
  }

  _openNativePicker() {
    const slot = this.querySelector('[data-native-picker]');
    if (!slot || this._nativePicker) return;
    mountModelSelect(this.host, slot, {
      value: '', allowDefault: false,
      onChange: async (spec) => {
        if (!spec) return;
        let val = spec;
        try {
          const r = await this.host.api.getJSON(`/api/models/resolve?spec=${encodeURIComponent(spec)}`);
          if (r && r.model) val = r.model;  // bare id — native namespace
        } catch (_) { /* fall back to the raw spec */ }
        this.harnessModel = val;
        this.refreshCli();
        this.checkModelCompat();
      },
    }).then((h) => { this._nativePicker = h; });
  }

  _teardownNativePicker() {
    if (this._nativePicker) { try { this._nativePicker.el?.remove(); } catch (_) {} this._nativePicker = null; }
    this._nativePickerOpen = false;
  }

  // ── type-specific section ────────────────────────────────────────────
  _typeSection() {
    const entry = this.selected;
    if (!entry) return nothing;
    if (entry.kind === 'external') return this._externalNote();
    if (entry.type === 'relaydeck') return this._relaydeckSection();
    return this._launchSection(entry.launch_options || []);
  }

  // Imperative side of the type section: mount the pi-install widget when the
  // relaydeck harness probe says pi's missing. The widget polls `/status` and
  // flips `cli_installed` so the Spawn button enables live — no reopen needed.
  _mountTypeSection() {
    const entry = this.selected;
    this._teardownPiWidget();
    if (!entry || entry.type !== 'relaydeck' || entry.cli_installed !== false) return;
    const slot = this.querySelector('[data-pi-install-slot]');
    if (!slot) return;
    import('/static/plugins/relaydeck-native/pi_install.js').then((mod) => {
      if (!this.isConnected) return;  // modal closed before import
      this._piWidget = mod.mountPiInstall({
        container: slot,
        api: this.host.api,
        onReady: (st) => {
          if (this.selected && this.selected.type === 'relaydeck') {
            this.selected.cli_installed = true;
            if (st && st.pi_version) this.selected.cli_version = st.pi_version;
          }
          this.updateSpawnGate();
        },
      });
    }).catch(() => { /* import-failure: stale warning is non-fatal */ });
  }

  _teardownPiWidget() {
    if (this._piWidget) { try { this._piWidget.unmount(); } catch (_) {} this._piWidget = null; }
  }

  _externalNote() {
    const entry = this.selected;
    return html`<div style=${PANEL}>
      <div style=${LBL}>External runtime</div>
      <div style="font-size:var(--t-xs);color:var(--t-2);line-height:1.5">
        relaydeck will launch <b>${entry.label}</b>'s chat TUI in this workspace and
        attach it like any other agent. The external runtime owns its own model, prompt, and
        permissions — relaydeck only hosts the terminal. No model/prompt/plugin baking here.
      </div></div>`;
  }

  _launchSection(opts) {
    if (!opts.length) return nothing;
    return html`<div style=${PANEL}>
      <div style=${LBL}>${this.selected.label} options</div>
      ${opts.map((o) => this._optionRow(o))}</div>`;
  }

  _optionRow(o) {
    const help = html`<span class="na-opt-help">${o.help || ''}</span>`;
    const set = (v) => { this.launchVals = { ...this.launchVals, [o.key]: v }; this.refreshCli(); };
    if (o.kind === 'bool') {
      return html`<label class="na-opt${o.danger ? ' na-danger' : ''}">
        <input type="checkbox" data-opt=${o.key} .checked=${!!this.launchVals[o.key]}
          @change=${(e) => set(e.target.checked)}>
        <span><b>${o.label}</b><br>${help}</span></label>`;
    }
    if (o.kind === 'select') {
      return html`<div class="na-opt na-opt-field">
        <span class="na-opt-lbl"><b>${o.label}</b> ${help}</span>
        <select data-opt=${o.key} style="font-family:var(--f-mono);font-size:var(--t-xs)"
          @change=${(e) => set(e.target.value)}>
          ${(o.options || []).map((x) => html`<option value=${x.value}>${x.label}</option>`)}
        </select></div>`;
    }
    // text | args
    return html`<div class="na-opt na-opt-field">
      <span class="na-opt-lbl"><b>${o.label}</b> ${help}</span>
      <input data-opt=${o.key} placeholder=${o.placeholder || ''}
        style="font-family:var(--f-mono);font-size:var(--t-xs)"
        .value=${liveDirective(this.launchVals[o.key] || '')}
        @input=${(e) => set(e.target.value)}></div>`;
  }

  _relaydeckSection() {
    const entry = this.selected;
    const noPi = entry && entry.cli_installed === false;
    // The pi-install widget mounts into [data-pi-install-slot] AFTER render
    // (_mountTypeSection). Keeping the slot inline puts it right above the
    // persona/tool block, where the operator looks before clicking Spawn.
    const piWarn = noPi
      ? html`<div data-pi-install-slot style="margin-bottom:10px"></div>` : nothing;
    const tool = (k, label, note) => html`<label class="na-opt">
      <input type="checkbox" data-tool=${k} .checked=${!!this.tools[k]}
        @change=${(e) => { this.tools = { ...this.tools, [k]: e.target.checked }; this.refreshCli(); }}>
      <span><b>${k}</b> — ${label}${note ? html` <span style="color:var(--t-4)">${note}</span>` : nothing}</span></label>`;
    return html`${piWarn}<div style=${PANEL}>
      <div style=${LBL}>Persona / soul <span style="color:var(--t-4)">(a suggestion — edit freely)</span></div>
      <textarea data-f="relaydeck_soul" rows="4"
        style="width:100%;font-family:var(--f-sans);font-size:var(--t-sm);resize:vertical"
        .value=${liveDirective(this.relaydeckSoul)}
        @input=${(e) => { this.relaydeckSoul = e.target.value; this.refreshCli(); this.schedulePreview(); }}></textarea>
      <div style="${LBL};margin-top:10px">Tool access</div>
      ${tool('read', 'read files in the workspace')}
      ${tool('write', 'create / modify files')}
      ${tool('bash', 'run shell as the relaydeck user', '(NOT sandboxed — full shell access)')}
      ${tool('relaydeck', 'coordinate: message peer agents')}
      ${tool('manage', 'start/stop agents in this workspace')}
      ${tool('dashboard', 'reshape the live web dashboard')}
    </div>`;
  }

  // ── system prompt + "what gets baked" preview ────────────────────────
  _promptSection() {
    const entry = this.selected;
    if (!entry || entry.kind === 'external') return nothing;
    const isOp = entry.type === 'relaydeck';
    return html`
      <label style=${LBL}>Identity preamble <span style="color:var(--t-4)">· injected at spawn</span></label>
      ${isOp ? nothing : html`
        <label class="na-opt" style="margin-bottom:8px">
          <input type="checkbox" data-f="inject" .checked=${this.inject}
            @change=${(e) => { this.inject = e.target.checked; this.refreshCli(); this.schedulePreview(); }}>
          <span><b>Inject identity preamble</b> <span class="na-opt-help">— auto "you are X, your peers are…" block (id, purpose, peers, messaging contract)</span></span></label>
        <textarea data-f="system_prompt" rows="2"
          placeholder="Append to system prompt (optional) — e.g. Always check for SQL injection."
          style="width:100%;font-family:var(--f-sans);font-size:var(--t-sm);resize:vertical;margin-bottom:8px"
          .value=${liveDirective(this.systemPrompt)}
          @input=${(e) => { this.systemPrompt = e.target.value; this.refreshCli(); this.schedulePreview(); }}></textarea>`}
      <div class="na-pv-box"><pre class="na-pre" data-preamble>${this.preamble}</pre></div>
      <div class="na-pv-h" style="margin-top:8px">Workspace plugins shaping this prompt <span class="na-opt-help" data-preview-status></span></div>
      <div data-preview>
        ${this.injections.length
          ? html`<ul class="na-pv-list">${this.injections.map((i) =>
              html`<li><b>${i.plugin}</b> — ${i.detail || ''}</li>`)}</ul>`
          : html`<div class="na-opt-help">None active in this workspace.</div>`}
      </div>`;
  }

  schedulePreview() {
    clearTimeout(this._previewTimer);
    this._previewTimer = setTimeout(() => this.doPreview(), 250);
  }

  async doPreview() {
    const entry = this.selected;
    if (!entry || entry.kind === 'external') return;
    const body = {
      agent_id: (this.id.trim() || 'new-agent'),
      workspace: this.workspace,
      type: entry.type,
      purpose: this.purpose,
      system_prompt: (entry.type === 'relaydeck' ? this.relaydeckSoul : this.systemPrompt) || '',
      inject_identity_preamble: entry.type === 'relaydeck' ? true : this.inject,
    };
    let r;
    try { r = await this.host.api.postJSON('/api/agents/preview-prompt', body); }
    catch (e) { this.preamble = '(preview unavailable)'; return; }
    const txt = [r.preamble, r.system_prompt].filter(Boolean).join('\n\n');
    this.preamble = txt || '— preamble off, no system prompt —';
    this.injections = r.injections || [];
  }

  // ── workspace plugins ────────────────────────────────────────────────
  // Reflect the workspace's ACTUAL enabled plugins — a checked box means
  // "enabled on this workspace", so what you see is exactly what gets saved.
  // Recommended-but-not-enabled show UNCHECKED with a badge (opt in to enable).
  _preselectedPlugins(wsName) { return new Set(this.wsPlugins[wsName] || []); }

  _pluginsSection() {
    const entry = this.selected;
    if (!entry || entry.kind === 'external') return nothing;
    if (!this.catalog.length) return nothing;
    const ws = this.workspace;
    return html`
      <div class="na-sec-head">
        <label style="${LBL};margin:0">Workspace plugins <span style="color:var(--t-4)">(checked = enabled on @${ws})</span></label>
        <div class="na-sec-actions">
          <button class="na-mini" type="button" data-pl="rec" @click=${() => this._bulkPlugins('rec')}>recommended</button>
          <button class="na-mini" type="button" data-pl="all" @click=${() => this._bulkPlugins('all')}>all</button>
          <button class="na-mini" type="button" data-pl="none" @click=${() => this._bulkPlugins('none')}>none</button>
        </div>
      </div>
      <div class="na-pl-grid">${this.catalog.map((p) => html`
        <label class="na-pl">
          <input type="checkbox" data-plugin=${p.name} .checked=${this.chosen.has(p.name)}
            @change=${(e) => this._setPlugin(p.name, e.target.checked)}>
          <span class="na-pl-main">
            <span class="na-pl-name">${p.name}
              ${p.recommended ? html`<span class="na-tag na-tag-rec">rec</span>` : nothing}
              ${p.kind === 'harness-gate' ? html`<span class="na-tag">gate</span>` : nothing}</span>
            <span class="na-opt-help">${p.description || ''}</span></span></label>`)}</div>`;
  }

  _setPlugin(name, want) {
    const next = new Set(this.chosen);
    if (want) next.add(name); else next.delete(name);
    this.chosen = next;
    this.refreshCli();
    this.requestUpdate();
  }

  _bulkPlugins(mode) {
    const next = new Set();
    for (const p of this.catalog) {
      if (mode === 'all') next.add(p.name);
      else if (mode === 'rec' && p.recommended) next.add(p.name);
      // 'none' → leave empty
    }
    this.chosen = next;
    this.refreshCli();
    this.requestUpdate();
  }

  // ── collection + CLI mirror ──────────────────────────────────────────
  _collectLaunch() {
    const config = {}; const args = [];
    for (const o of (this.selected.launch_options || [])) {
      const val = this.launchVals[o.key];
      const apply = o.apply || {};
      if (o.kind === 'bool') {
        if (!val) continue;
        if (apply.arg) args.push(...apply.arg.split(/\s+/));
        else if (apply.config) config[apply.config] = true;
      } else if (o.kind === 'select') {
        if (!val) continue;
        if (apply.arg) args.push(apply.arg, val);
        else if (apply.config) config[apply.config] = val;
      } else if (o.kind === 'args') {
        const v = (val || '').trim(); if (v) args.push(...v.split(/\s+/));
      } else { // text
        const v = (val || '').trim(); if (!v) continue;
        if (apply.from === 'text' && apply.arg) args.push(apply.arg, v);
        else if (apply.config) config[apply.config] = v;
      }
    }
    if (args.length) config.args = args;
    return config;
  }

  _chosenPlugins() { return [...this.chosen]; }

  buildConfig() {
    const cfg = {};
    const entry = this.selected;
    const policy = entry?.model_policy || 'flex';
    if (policy === 'native') {
      const hm = this.harnessModel.trim();
      if (hm && entry.model_config_key) cfg[entry.model_config_key] = hm;
    } else if (policy !== 'relaydeck') {
      const preset = this._modelSel ? this._modelSel.getValue() : '';
      if (preset) cfg.preset = preset;
    }
    if (entry.type === 'relaydeck') {
      const soul = this.relaydeckSoul.trim();
      if (soul) cfg.soul = soul;
      cfg.tools = Object.keys(this.tools).filter((k) => this.tools[k]);
      const spec = this._modelSel ? this._modelSel.getValue() : '';
      if (spec) {
        if (spec.includes('/')) cfg.model = spec;
        else cfg.preset = spec;
      } else {
        cfg.model = 'role:fast';
      }
    } else {
      Object.assign(cfg, this._collectLaunch());
    }
    return cfg;
  }

  refreshCli() {
    const entry = this.selected;
    if (!entry) return;
    const id = this.id.trim() || '<id>';
    const ws = this.workspace;
    if (entry.kind === 'external') {
      this.cliEq = `relaydeck external spawn ${entry.external_id} -w ${ws}${id !== '<id>' ? ` --agent-id ${id}` : ''}`;
      return;
    }
    const cfg = this.buildConfig();
    const flags = [];
    for (const [k, v] of Object.entries(cfg)) {
      if (k === 'args') flags.push(`--config args="${(v || []).join(' ')}"`);
      else if (Array.isArray(v)) flags.push(`--config ${k}=${v.join(',')}`);
      else flags.push(`--config ${k}=${typeof v === 'string' ? JSON.stringify(v).slice(0, 30) : v}`);
    }
    const purpose = this.purpose.trim();
    const sp = this.systemPrompt.trim();
    this.cliEq = `relaydeck agent create --type ${entry.type} --workspace ${ws}`
      + (purpose ? ` --purpose "${purpose.slice(0, 24)}…"` : '')
      + (sp ? ` --system-prompt "…"` : '')
      + (entry.type !== 'relaydeck' && !this.inject ? ' --no-identity' : '')
      + (flags.length ? ' ' + flags.join(' ') : '')
      + ` ${id}`;
  }

  // Live agent-id validation: show the rule, flag invalid input, and gate
  // Spawn. The server re-checks (POST /api/agents → 400) so this is purely
  // a fast, friendly mirror.
  checkId() {
    const v = this.id.trim();
    if (!v) { this.idHint = AID_RULE; this.idHintBad = false; if (this.selected) this.updateSpawnGate(); return false; }
    const ok = v.length <= 64 && AID_RE.test(v);
    this.idHint = ok ? AID_RULE : `✗ use ${AID_RULE}`;
    this.idHintBad = !ok;
    this.updateSpawnGate();
    return ok;
  }

  spawnDisabled() {
    if (!this.selected || this.step !== 'config') return true;
    const id = this.id.trim();
    const idOk = id.length <= 64 && AID_RE.test(id);
    const piOk = this.selected.type !== 'relaydeck' || this.selected.cli_installed !== false;
    return !idOk || !piOk;
  }

  updateSpawnGate() {
    const spawn = this.querySelector('[data-act="spawn"]');
    if (spawn) spawn.disabled = this.spawnDisabled();
  }

  _onWorkspaceChange(v) {
    this.workspace = v;
    // Re-sync the chosen plugin set to the new workspace's actual enabled set.
    this.chosen = this._preselectedPlugins(v);
    this.refreshCli();
    this.schedulePreview();
    this.requestUpdate();
  }

  // ── provider-key guard (relaydeck only) ──────────────────────────────
  async checkRelaydeckKey() {
    if (!this.selected) { this.keyWarn = ''; return; }
    if (this.selected.type !== 'relaydeck') { this.keyWarn = ''; return; }
    const spec = this._modelSel ? this._modelSel.getValue() : '';
    let r; try { r = await this.host.api.getJSON(`/api/models/resolve?spec=${encodeURIComponent(spec)}`); }
    catch (_) { this.keyWarn = ''; return; }
    if (!this._provs) { try { this._provs = await this.host.api.getJSON('/api/providers'); } catch (_) { this._provs = []; } }
    const p = this._provs.find((x) => x.name === r.provider);
    if (p && p.needs_key && !p.has_key) {
      this.keyWarn = `⚠ Provider ${r.provider} has no API key in relaydeck's vault — this agent will fail at chat time. `
        + `Set ${p.key_env || 'its key'} in Settings → Models → Providers, or pick a local (ollama) model.`;
    } else { this.keyWarn = ''; }
  }

  // ── harness ↔ model-provider compatibility guard ─────────────────────
  // flex harnesses (pi/opencode): advisory when preset resolves elsewhere.
  // native harnesses use the harness-model field — no relaydeck preset picker.
  async checkModelCompat() {
    const entry = this.selected;
    if (!entry) { this.modelWarn = ''; return; }
    if (entry.model_policy === 'native' || entry.kind === 'external') { this.modelWarn = ''; return; }
    const native = entry.native_providers;
    const spec = this._modelSel ? this._modelSel.getValue() : '';
    if (!native || !spec || entry.type === 'relaydeck') { this.modelWarn = ''; return; }
    let r; try { r = await this.host.api.getJSON(`/api/models/resolve?spec=${encodeURIComponent(spec)}`); }
    catch (_) { this.modelWarn = ''; return; }
    if (r.provider && !native.includes(r.provider)) {
      this.modelWarn = `⚠ ${entry.label} only speaks ${native.join(' / ')}, but this model resolves to ${r.provider} — `
        + `it won't work unless you've configured ${entry.label}'s own providers. `
        + `Pick a ${native.join('/')} model, or use pi/opencode/relaydeck-native.`;
    } else { this.modelWarn = ''; }
  }

  fail(msg) { this.err = msg; }

  // ── spawn ────────────────────────────────────────────────────────────
  async doSpawn() {
    this.err = '';
    const entry = this.selected;
    if (!entry) { this.fail('Pick a harness.'); this.goToStep('harness'); return; }
    const id = this.id.trim();
    const workspace = this.workspace;
    if (!id) { this.fail('ID is required.'); return; }
    if (!(id.length <= 64 && AID_RE.test(id))) { this.fail('Invalid agent id — use ' + AID_RULE + '.'); return; }
    if (!workspace) { this.fail('Workspace is required.'); return; }
    const spawnBtn = this.querySelector('[data-act="spawn"]');
    if (spawnBtn) spawnBtn.disabled = true;
    let finalId = id;   // the backend may dedup an external id (e.g. hermes-2)
    try {
      if (entry.kind === 'external') {
        const resp = await this.host.api.postJSON(
          `/api/plugins/external/agents/${encodeURIComponent(entry.external_id)}/spawn`,
          { workspace, agent_id: id });
        if (resp && resp.agent_id) finalId = resp.agent_id;
      } else {
        // Create the agent FIRST — it fails fast on a bad id / duplicate /
        // unknown type, so a failed spawn never mutates the shared workspace.
        // create_agent doesn't start the agent (it reads agent.toml at start),
        // so patching plugins after is in time.
        const body = {
          id, name: id, type: entry.type, workspace, auto_start: true,
          purpose: this.purpose.trim() || '',
        };
        if (entry.type !== 'relaydeck' && !this.inject) body.inject_identity_preamble = false;
        const sp = this.systemPrompt.trim();
        if (sp && entry.type !== 'relaydeck') body.system_prompt = sp;
        const cfg = this.buildConfig();
        if (Object.keys(cfg).length) body.config = cfg;
        const resp = await this.host.api.postJSON('/api/agents', body);
        if (resp && resp.id) finalId = resp.id;
        // Persist workspace plugin changes as a DELTA, not a wholesale replace
        // of a stale snapshot. create_agent may have just auto-enabled
        // `messaging`; re-read the post-create set and apply only the
        // operator's add/remove. No user change → no read, no PATCH.
        const original = this.wsPlugins[workspace] || [];
        const chosen = this._chosenPlugins();
        const added = chosen.filter((p) => !original.includes(p));
        const removed = original.filter((p) => !chosen.includes(p));
        if (added.length || removed.length) {
          let currentAfter = original;
          try {
            const wss = await this.host.api.getJSON('/api/workspaces');
            const w = (wss || []).find((x) => x.name === workspace);
            if (w && Array.isArray(w.plugins)) currentAfter = w.plugins;
          } catch (_) {}
          const merged = [
            ...currentAfter.filter((p) => !removed.includes(p)),
            ...added.filter((p) => !currentAfter.includes(p)),
          ];
          if (merged.slice().sort().join(',') !== currentAfter.slice().sort().join(',')) {
            await this.host.api.fetch(`/api/workspaces/${encodeURIComponent(workspace)}`, {
              method: 'PATCH', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ plugins: merged }),
            });
          }
          this.wsPlugins[workspace] = merged;
        }
        // "Spawn" means create AND start. create_agent only persists the spec
        // (auto_start governs daemon-boot, not this session), so kick the
        // harness off now in the running daemon. A start failure is non-fatal —
        // the agent still exists and the detail shows a Launch button.
        try { await this.host.api.postJSON(`/api/agents/${encodeURIComponent(finalId)}/start`, {}); }
        catch (_) { /* leaves the agent created-but-stopped; Launch recovers */ }
      }
      this._closer?.();
      await this.host.reloadAgents();
      this.host.setLens('agents', finalId);
    } catch (err) {
      if (spawnBtn) spawnBtn.disabled = false;
      this.fail('Spawn failed: ' + (err && err.message ? err.message : err));
    }
  }

  // ── render ───────────────────────────────────────────────────────────
  render() {
    const entry = this.selected;
    const onHarness = this.step === 'harness';
    const wsDisplay = this.workspace || this.prefill.workspace
      || this.host.state.workspace || (this.host.state.workspaces?.[0]?.name || '');

    // No workspaces yet → a focused empty state replaces the body.
    if (this.noWorkspaces) {
      return html`
        <div class="na-head">
          <div class="na-head-main">
            <div class="na-title">New agent</div>
            <div class="na-sub">Spawn in <span style="color:var(--acc)" data-ws-display>@${wsDisplay}</span></div>
          </div>
          <button class="btn icon" data-act="close" @click=${() => this._closer?.()}>${ic('x', 12)}</button>
        </div>
        <div class="na-body">
          <div style="display:flex;flex-direction:column;align-items:center;gap:14px;padding:32px 16px;text-align:center">
            <div style="font-size:32px">${ic('workspace', 32)}</div>
            <div>
              <div style="font:600 var(--t-lg) var(--f-mono);color:var(--t-1);margin-bottom:6px">No workspaces yet</div>
              <div style="font-size:var(--t-sm);color:var(--t-3);max-width:380px;line-height:1.5">
                An agent runs IN a workspace — a directory on the daemon host.
                Add one to spawn agents.
              </div>
            </div>
            <button class="btn primary" data-add-ws-empty
              style="display:inline-flex;align-items:center;gap:8px;margin-top:4px"
              @click=${() => this._addWorkspace()}>
              ${ic('folder_open', 13)}Add a workspace
            </button>
          </div>
        </div>
        <div class="na-foot">
          <div class="na-cli" data-cli>💡 <span style="color:var(--acc)" data-cli-eq>${this.cliEq}</span></div>
          <div class="na-foot-actions">
            <button class="btn ghost" data-act="cancel" @click=${() => this._closer?.()}>Cancel</button>
            <button class="btn primary" data-act="spawn" disabled>Spawn agent</button>
          </div>
        </div>`;
    }

    return html`
      <div class="na-head">
        <div class="na-head-main">
          <div class="na-title">New agent</div>
          <div class="na-sub">Spawn in <span style="color:var(--acc)" data-ws-display>@${wsDisplay}</span></div>
        </div>
        <div class="na-steps" data-steps>
          <button class="na-stepchip ${onHarness ? 'on' : ''}" data-goto="harness"
            @click=${() => this.goToStep('harness')}><span class="n">1</span> Harness</button>
          <span class="na-step-sep">${ic('chev_r', 11)}</span>
          <button class="na-stepchip ${!onHarness ? 'on' : ''}" data-goto="config" ?disabled=${!entry}
            @click=${() => { if (entry) this.goToStep('config'); }}><span class="n">2</span> Configure</button>
        </div>
        <button class="btn icon" data-act="close" @click=${() => this._closer?.()}>${ic('x', 12)}</button>
      </div>
      <div class="na-body">
        <div class="na-step na-step-harness" data-pane="harness" ?hidden=${!onHarness}>
          <div class="na-pick-intro">Pick a harness. Each is a coding agent CLI relaydeck drives in a real terminal — you'll tune it next.</div>
          <div class="na-type-grid" data-type-grid>
            ${!this.loaded ? html`<div class="na-loading">Loading harnesses…</div>` : this._cards()}
          </div>
        </div>
        <div class="na-step na-step-config" data-pane="config" ?hidden=${onHarness}>
          ${this._configHead()}
          <div data-key-warn class="na-warn" style=${this.keyWarn ? 'display:block' : 'display:none'}>${this.keyWarn}</div>
          <div data-model-warn class="na-warn" style=${this.modelWarn ? 'display:block' : 'display:none'}>${this.modelWarn}</div>
          <div class="na-essentials">
            <div class="na-field">
              <label style=${LBL}>Identity</label>
              <div class="na-row3">
                <div>
                  <input data-f="id" placeholder="pr-reviewer"
                    style="width:100%;font-family:var(--f-mono);font-size:var(--t-xs)"
                    .value=${liveDirective(this.id)}
                    @input=${(e) => { this.id = e.target.value; this.checkId(); this.refreshCli(); this.schedulePreview(); }}>
                  <div data-id-hint style=${`font-family:var(--f-mono);font-size:9px;color:${this.idHintBad ? 'var(--err)' : 'var(--t-4)'};margin-top:3px;line-height:1.3`}>${this.idHint}</div>
                </div>
                <div>
                  <select data-f="workspace" style="width:100%;font-family:var(--f-mono);font-size:var(--t-xs)"
                    .value=${liveDirective(this.workspace)}
                    @change=${(e) => this._onWorkspaceChange(e.target.value)}>
                    ${this.wsList.map((w) => html`<option ?selected=${w.name === this.workspace}>${w.name}</option>`)}
                  </select>
                </div>
                ${this._modelField()}
              </div>
            </div>
          </div>
          <div class="na-advanced" data-advanced>
            <div class="na-field">
              <label style=${LBL}>Purpose <span style="color:var(--t-4)">(one-line "what I'm for")</span></label>
              <textarea data-f="purpose" rows="2" placeholder="Senior reviewer — gate PRs for clarity and risk"
                style="width:100%;font-family:var(--f-sans);font-size:var(--t-sm);resize:vertical"
                .value=${liveDirective(this.purpose)}
                @input=${(e) => { this.purpose = e.target.value; this.refreshCli(); this.schedulePreview(); }}></textarea>
            </div>
            <div class="na-field" data-type-section>${this._typeSection()}</div>
            <div class="na-field" data-plugins-section>${this._pluginsSection()}</div>
            <div class="na-field" data-prompt-section>${this._promptSection()}</div>
          </div>
          <div class="na-quicknote" data-quicknote>
            Spawning with smart defaults. <button type="button" data-act="customize"
              @click=${() => this.setMode('guided')}>Customize options →</button>
          </div>
        </div>
        <div data-err class="na-err" style=${this.err ? 'display:block' : 'display:none'}>${this.err}</div>
      </div>
      <div class="na-foot">
        <div class="na-cli" data-cli>💡 <span style="color:var(--acc)" data-cli-eq>${this.cliEq}</span></div>
        <div class="na-foot-actions">
          <button class="btn ghost" data-act="back" ?hidden=${onHarness}
            @click=${() => this.goToStep('harness')}>${ic('arrow_l', 12)} Back</button>
          <button class="btn ghost" data-act="cancel" @click=${() => this._closer?.()}>Cancel</button>
          <button class="btn primary" data-act="spawn" ?disabled=${this.spawnDisabled()}
            @click=${() => this.doSpawn()}>Spawn agent</button>
        </div>
      </div>`;
  }

  _addWorkspace() {
    openAddWorkspaceModal(this.host, async (name) => {
      // Refresh local list. If a new workspace landed, re-bootstrap the modal
      // so the operator continues seamlessly with the type grid + form. We
      // close + re-open as the simplest path — the next open sees the
      // workspace and skips this empty state.
      try { await this.host.reloadWorkspaces?.(); this.host.render && this.host.render(); } catch (_) {}
      const prefill = { workspace: name };
      this._closer?.();
      setTimeout(() => openNewAgentModal(this.host, prefill), 50);
    });
  }

  // Reflect step/mode as data-* on the modal node so the bespoke CSS keys off
  // them exactly as the hand-rolled version did (.na-modal[data-mode="quick"]
  // hides .na-advanced; [data-mode="guided"] hides .na-quicknote). The
  // Backspace-back handler in openNewAgentModal reads this.step directly.
  updated(changed) {
    super.updated?.(changed);
    this.dataset.step = this.step;
    this.dataset.mode = this.mode;
  }

  disconnectedCallback() {
    clearTimeout(this._previewTimer);
    this._teardownPiWidget();
    this._teardownNativePicker();
    super.disconnectedCallback();
  }
}

if (!customElements.get('rd-new-agent')) customElements.define('rd-new-agent', NewAgentModal);

// ── public contract ─────────────────────────────────────────────────────
export function openNewAgentModal(host, prefill = {}) {
  if (document.querySelector('.new-agent-modal')) return;
  let mode = 'guided';
  try { if (localStorage.getItem(MODE_KEY) === 'quick') mode = 'quick'; } catch (_) {}

  const scrim = document.createElement('div');
  scrim.className = 'overlay-scrim';
  // The bespoke modal shell — keep the exact classes the app + e2e suite
  // target (.new-agent-modal / .na-modal). The reactive element renders its
  // body into this shell via light DOM (the element IS the shell node).
  const modal = document.createElement('rd-new-agent');
  modal.className = 'new-agent-modal na-modal';
  modal.host = host;
  modal.prefill = prefill;
  modal.mode = mode;
  // Seed data-* before the first paint so the bespoke CSS (which keys on
  // [data-mode]) is correct from frame one; the element keeps them in sync via
  // its own updated() hook thereafter.
  modal.dataset.step = 'harness';
  modal.dataset.mode = mode;

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey);
    try { scrim.remove(); } catch (_) {}
    try { modal.remove(); } catch (_) {}   // disconnectedCallback tears widgets down
  }
  function onKey(e) {
    if (e.key === 'Escape') { close(); return; }
    // Esc-less back: Backspace on the config step (when not typing) returns
    // to the harness picker.
    if (e.key === 'Backspace' && modal.step === 'config'
        && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')) {
      e.preventDefault(); modal.goToStep('harness');
    }
  }
  modal._closer = close;

  document.body.appendChild(scrim);
  document.body.appendChild(modal);

  document.addEventListener('keydown', onKey);
  scrim.addEventListener('click', close);

  modal.load();
}
