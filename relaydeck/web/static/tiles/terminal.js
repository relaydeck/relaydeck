// tiles/terminal.js — live PTY mirror tile using xterm.js.
//
// We hand the raw PTY byte stream to a real terminal emulator so TUI
// harnesses (claude-code, codex, pi) render correctly — cursor moves,
// alt-screen toggles, colors, boxes, the lot. The previous "strip
// ANSI and dump as text" approach broke any harness that maintains
// its own framebuffer.
//
// Binary frame protocol (matches relaydeck/transports/api.py):
//   in:  0x00 = PTY output (bytes)
//        0x01 = lifecycle JSON ({event: "pty_closed" | "agent_not_running"})
//   out: 0x00 = stdin bytes
//        0x01 = "<cols> <rows>" resize
//        0x02 = ping (keepalive)
//
// xterm.js + FitAddon + (optional) WebglAddon are loaded as globals
// by index.html. We feature-detect: if `window.Terminal` is missing
// (e.g. CDN blocked), we fall back to a "xterm.js unavailable" hint
// rather than the old text-stripped renderer — half-renders just
// confuse the operator.

import { esc, iconSVG } from '../primitives.js';

const FRAME_PTY  = 0x00;
const FRAME_CTRL = 0x01;
const FRAME_PING = 0x02;

const IMAGE_EXTS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'tif', 'tiff', 'avif',
]);

function quoteIfSpaces(path) {
  return /\s/.test(path) ? `'${path}'` : path;
}

function isImageFile(file) {
  if (file.type && file.type.startsWith('image/')) return true;
  const name = String(file.name || '');
  const dot = name.lastIndexOf('.');
  if (dot < 0) return false;
  return IMAGE_EXTS.has(name.slice(dot + 1).toLowerCase());
}

function hasFilePayload(dt) {
  if (!dt?.types) return false;
  return [...dt.types].includes('Files');
}

export default class TerminalTile {
  constructor() {
    this.ws = null;
    this.term = null;
    this.fit = null;
    this.resizeObs = null;
    this.pingTimer = null;
    this.reconnectTimer = null;
    this.disposed = false;
    this.reconnectAttempts = 0;
    this._lastCtrlEvent = null;
    this._dropOverlay = null;
    this._dropHandlers = null;
    this._dropDepth = 0;
  }

  async mount(container, api, ctx) {
    this.api = api;
    this.ctx = ctx;
    this.agent = ctx.agent;

    container.innerHTML = '';
    // Zero chrome inside the tile. The tile IS the terminal — no
    // header strip, no bytes counter, no compose box. The user
    // emphasized: do not interfere with what the harness renders.
    // Tty info, byte counts, etc. live in the dhdr / status bar
    // outside the dbody — never inside the canvas area.
    //
    // The terminal tile is also the one tile that needs the dbody
    // to drop its padding so the canvas reaches all four edges of
    // the detail pane. We add `.fullbleed` to the parent dbody on
    // mount and strip it on unmount.
    this.host = document.createElement('div');
    this.host.className = 'term-host';
    this.host.tabIndex = 0;
    container.appendChild(this.host);
    // Climb to the dbody (the immediate parent for body-mode tiles)
    // and flip it to fullbleed for the lifetime of this tile.
    this._dbody = container.closest('.dbody');
    if (this._dbody) this._dbody.classList.add('fullbleed');

    if (typeof window.Terminal === 'undefined') {
      this.host.innerHTML = `<div style="padding:14px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);line-height:1.5">
        xterm.js failed to load. The terminal tile needs the CDN bundles in
        <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">index.html</code>
        to be reachable. Check your network proxy or run
        <code style="background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)">relaydeck attach ${esc(this.agent.id)}</code>
        in a terminal instead.
      </div>`;
      return;
    }

    this._initTerm();
    this._initImageDrop();
    this._connect();
    // Debug hook: inspect the live xterm geometry from the console /
    // Playwright (cols, rows, what we last sent). Harmless; mirrors
    // window.__relaydeckHome.
    window.__relaydeckTerm = this;
  }

  _initTerm() {
    const T = window.Terminal;
    const agentType = String(this.agent?.type || this.agent?.agent_type || '').toLowerCase();
    const isOpenCode = agentType.includes('opencode');
    this.term = new T({
      fontFamily: "'JetBrains Mono', ui-monospace, Menlo, monospace",
      fontSize: 13,
      lineHeight: 1.2,
      theme: {
        background: '#000000',
        foreground: '#ecedf2',
        cursor: '#67e8f9',
        cursorAccent: '#000000',
        selectionBackground: 'rgba(103,232,249,.25)',
      },
      cursorBlink: true,
      cursorStyle: 'block',
      // Full-screen TUIs already control CR/LF and cursor position.
      // convertEol can shift their framebuffer in xterm.js, so keep
      // OpenCode's PTY stream byte-for-byte.
      convertEol: !isOpenCode,
      allowProposedApi: true,
      allowTransparency: false,
      drawBoldTextInBrightColors: true,
      scrollback: 8000,
    });

    if (window.FitAddon?.FitAddon) {
      this.fit = new window.FitAddon.FitAddon();
      this.term.loadAddon(this.fit);
    }

    this.term.open(this.host);

    // Optional WebGL renderer. Has to load AFTER term.open() so the
    // canvas exists. If GPU context is lost (tab-switch, sleep) the
    // addon falls back gracefully — we don't crash if it can't init.
    try {
      if (!isOpenCode && window.WebglAddon?.WebglAddon) {
        const w = new window.WebglAddon.WebglAddon();
        w.onContextLoss = () => { try { w.dispose(); } catch (_) {} };
        this.term.loadAddon(w);
      }
    } catch (_) { /* canvas renderer fallback is automatic */ }

    // Keystrokes typed *into the terminal canvas* go straight to the
    // child PTY.
    this.term.onData((data) => this._sendText(data));

    // Resize handling. Two coupled problems:
    //   1. At mount the host might still have clientHeight=0 because
    //      the flex chain is mid-layout. fit() would compute 0 rows,
    //      and xterm would render an invisible 1×1 grid.
    //   2. The Panels manager opening / lens switch / window resize
    //      changes the host's size after the fact and we need to
    //      refit + tell the harness about the new dimensions.
    // Solution: do the first fit *inside a ResizeObserver* callback,
    // which is guaranteed to fire after layout has computed a real
    // size for the host. Subsequent observations debounce a refit.
    let lastDim = "";
    const refit = () => {
      try { this.fit?.fit(); } catch (_) {}
      this._sendResize();
    };
    let scheduled = null;
    this.resizeObs = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (!r) return;
      // Skip zero-sized observations (host hidden / pre-layout).
      if (r.width < 4 || r.height < 4) return;
      const dim = `${Math.round(r.width)}x${Math.round(r.height)}`;
      if (dim === lastDim) return;
      lastDim = dim;
      if (scheduled) clearTimeout(scheduled);
      scheduled = setTimeout(refit, 60);
    });
    this.resizeObs.observe(this.host);
    // Belt-and-suspenders initial fit in case the ResizeObserver fires
    // before xterm.open has its DOM ready.
    requestAnimationFrame(refit);
    // Re-fit once the mono font is actually loaded. xterm measures the
    // cell width from the font; if fit() ran with a fallback metric (font
    // still loading) it computes the wrong cols and the host never
    // changes size to trigger the ResizeObserver — so the terminal would
    // stay at the wrong width. fonts.ready fixes that exactly once.
    try { document.fonts?.ready?.then(refit); } catch (_) {}
  }

  _initImageDrop() {
    if (!this.host) return;
    this._dropDepth = 0;
    this._dropOverlay = document.createElement('div');
    this._dropOverlay.className = 'term-drop-overlay';
    Object.assign(this._dropOverlay.style, {
      position: 'absolute',
      inset: '0',
      display: 'none',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'color-mix(in srgb, var(--bg-2) 82%, transparent)',
      border: '2px dashed var(--acc)',
      color: 'var(--t-3)',
      fontFamily: 'var(--f-mono, ui-monospace, monospace)',
      fontSize: 'var(--t-xs, 11px)',
      letterSpacing: '0.04em',
      textTransform: 'uppercase',
      pointerEvents: 'none',
      zIndex: '10',
    });
    this._dropOverlay.textContent = 'Drop image to attach';
    this.host.appendChild(this._dropOverlay);

    const show = () => { this._dropOverlay.style.display = 'flex'; };
    const hide = () => {
      this._dropDepth = 0;
      this._dropOverlay.style.display = 'none';
    };

    const onDragEnter = (e) => {
      if (!hasFilePayload(e.dataTransfer)) return;
      e.preventDefault();
      this._dropDepth += 1;
      show();
    };
    const onDragOver = (e) => {
      if (!hasFilePayload(e.dataTransfer)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    };
    const onDragLeave = () => {
      if (this._dropDepth === 0) return;
      this._dropDepth = Math.max(0, this._dropDepth - 1);
      if (this._dropDepth === 0) hide();
    };
    const onDrop = (e) => {
      if (!hasFilePayload(e.dataTransfer)) return;
      e.preventDefault();
      hide();
      const files = e.dataTransfer?.files;
      if (files?.length) void this._uploadDroppedImages(files);
    };

    this._dropHandlers = [
      ['dragenter', onDragEnter],
      ['dragover', onDragOver],
      ['dragleave', onDragLeave],
      ['drop', onDrop],
    ];
    for (const [ev, fn] of this._dropHandlers) {
      this.host.addEventListener(ev, fn);
    }
  }

  _teardownImageDrop() {
    if (this._dropHandlers && this.host) {
      for (const [ev, fn] of this._dropHandlers) {
        this.host.removeEventListener(ev, fn);
      }
    }
    this._dropHandlers = null;
    try { this._dropOverlay?.remove(); } catch (_) {}
    this._dropOverlay = null;
    this._dropDepth = 0;
  }

  async _uploadDroppedImages(fileList) {
    const images = [...fileList].filter(isImageFile);
    if (!images.length) return;
    const paths = [];
    const agentId = encodeURIComponent(this.agent.id);
    for (const file of images) {
      try {
        const fd = new FormData();
        fd.append('file', file);
        const r = await fetch(`/api/agents/${agentId}/uploads`, { method: 'POST', body: fd });
        if (!r.ok) {
          let detail = String(r.status);
          try {
            const j = await r.json();
            detail = j.detail ?? j.message ?? detail;
            if (Array.isArray(detail)) {
              detail = detail.map((d) => d.msg || d).join('; ');
            }
          } catch (_) {}
          this._write(`\r\n\x1b[31m[upload failed: ${detail}]\x1b[0m\r\n`);
          continue;
        }
        const j = await r.json();
        if (j.path) paths.push(j.path);
      } catch (err) {
        const msg = err?.message || String(err);
        this._write(`\r\n\x1b[31m[upload failed: ${msg}]\x1b[0m\r\n`);
      }
    }
    if (paths.length) {
      this._sendText(paths.map(quoteIfSpaces).join(' ') + ' ');
    }
  }

  _connect() {
    if (this.disposed) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = this.api.tokenize(`${proto}//${location.host}/api/agents/${encodeURIComponent(this.agent.id)}/term`);
    try {
      this.ws = new WebSocket(url);
      this.ws.binaryType = 'arraybuffer';
    } catch (e) {
      this._write(`\r\n\x1b[31m[ws error: ${e.message}]\x1b[0m\r\n`);
      return;
    }

    this.ws.addEventListener('open', () => {
      this.host.classList.remove('dead');
      this._lastCtrlEvent = null;
      // Fit to the now-settled pane BEFORE sending the size. On initial
      // page load / launch the socket opens before the flex layout has
      // sized the pane, so xterm is still at its ~80-col default —
      // sending that makes the harness draw a narrow rectangle until a
      // restart/resize. Fitting first (plus a re-sync after layout
      // settles) sends the real width so it fills on the first paint.
      try { this.fit?.fit(); } catch (_) {}
      this._sendResize();
      setTimeout(() => { try { this.fit?.fit(); } catch (_) {} this._sendResize(); }, 150);
      if (this.pingTimer) clearInterval(this.pingTimer);
      this.pingTimer = setInterval(() => this._sendFrame(FRAME_PING, null), 25000);
    });

    this.ws.addEventListener('message', (e) => {
      if (!(e.data instanceof ArrayBuffer)) return;
      const view = new Uint8Array(e.data);
      if (view.length < 1) return;
      const kind = view[0];
      const payload = view.subarray(1);
      if (kind === FRAME_PTY) {
        this.reconnectAttempts = 0;
        this._lastCtrlEvent = null;
        this.term?.write(payload);
      } else if (kind === FRAME_CTRL) {
        let ev = null;
        try { ev = JSON.parse(new TextDecoder().decode(payload)); } catch (_) {}
        if (ev?.event === 'pty_closed') {
          this._lastCtrlEvent = 'pty_closed';
          this._write('\r\n\x1b[2m[harness exited — waiting for relaunch]\x1b[0m\r\n');
        } else if (ev?.event === 'agent_not_running') {
          this._lastCtrlEvent = 'agent_not_running';
          this._write('\r\n\x1b[33m[agent is not running — click ▶ Launch in the header]\x1b[0m\r\n');
        }
      }
    });

    this.ws.addEventListener('close', () => {
      if (this.pingTimer) { clearInterval(this.pingTimer); this.pingTimer = null; }
      this.ws = null;
      this.host?.classList.add('dead');
      // Auto-reconnect: harnesses often exit + relaunch in <1s
      // (pi especially). Stay open so the operator doesn't have to
      // re-click the tab.
      if (this.disposed) return;
      this.reconnectAttempts += 1;
      if (this._lastCtrlEvent === 'agent_not_running' && this.reconnectAttempts > 3) {
        this._write('\r\n\x1b[2m[terminal reconnect paused until the agent starts]\x1b[0m\r\n');
        return;
      }
      if (this._lastCtrlEvent === 'pty_closed' && this.reconnectAttempts > 20) {
        this._write('\r\n\x1b[2m[terminal reconnect paused after 20 attempts]\x1b[0m\r\n');
        return;
      }
      if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
      const delay = Math.min(5000, Math.round(800 * Math.pow(1.35, this.reconnectAttempts - 1)));
      this.reconnectTimer = setTimeout(() => this._connect(), delay);
    });

    this.ws.addEventListener('error', () => {
      // The 'close' event fires next and we reconnect there.
    });
  }

  _sendText(text) { this._sendFrame(FRAME_PTY, new TextEncoder().encode(text)); }
  _sendResize() {
    if (!this.term) return;
    const msg = `${this.term.cols} ${this.term.rows}`;
    this._lastResize = msg;
    this._sendFrame(FRAME_CTRL, new TextEncoder().encode(msg));
  }
  _sendFrame(kind, bytes) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const len = bytes ? bytes.length : 0;
    const frame = new Uint8Array(len + 1);
    frame[0] = kind;
    if (bytes) frame.set(bytes, 1);
    this.ws.send(frame.buffer);
  }
  _write(text) {
    // Convenience helper for inline messages — sends as if the PTY
    // produced it, so xterm parses the ANSI cleanly.
    this.term?.write(text);
  }

  unmount() {
    this.disposed = true;
    try { this._teardownImageDrop(); } catch (_) {}
    try { if (this.reconnectTimer) clearTimeout(this.reconnectTimer); } catch (_) {}
    try { if (this.pingTimer) clearInterval(this.pingTimer); } catch (_) {}
    try { if (this.ws) { this.ws.onclose = null; this.ws.close(); } } catch (_) {}
    try { this.resizeObs?.disconnect(); } catch (_) {}
    try { this.term?.dispose(); } catch (_) {}
    // Restore the dbody padding for whichever tile comes next.
    try { this._dbody?.classList.remove('fullbleed'); } catch (_) {}
    this.term = null; this.ws = null; this.fit = null; this.resizeObs = null;
  }
}
