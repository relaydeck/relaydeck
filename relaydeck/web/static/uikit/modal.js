// uikit/modal.js — themed overlay primitives.
//
// openModal() mounts a centered modal on a scrim over document.body with Esc +
// scrim-click close, and a reactive body you can re-`update()`. confirm()
// is the yes/no dialog that replaces the bare window.confirm()/prompt() calls
// scattered through the plugins. Both render the shared `.overlay-scrim` +
// `.rd-modal` styling (uikit.css) and theme via the design tokens.
//
//   const m = openModal({ heading: 'Edit route', size: 'md',
//     content: ({ close }) => html`… ${button({onClick: close}, 'Done')}` });
//   m.update(newContentTemplate);   // re-render the body reactively
//   m.close();
//
//   if (await confirm({ title: 'Delete?', body: '…', danger: true })) { … }

import { html, render, nothing } from 'lit';
import { button } from './widgets.js';
import { icon } from './format.js';

export function openModal(opts = {}) {
  const { heading = '', size = 'md', scrimClose = true, class: cls = '', onClose } = opts;
  let _content = opts.content;
  let _footer = opts.footer;

  const scrim = document.createElement('div');
  scrim.className = ('overlay-scrim rd-scrim ' + cls).trim();
  document.body.appendChild(scrim);

  let closed = false;
  const close = (reason) => {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey, true);
    scrim.remove();
    try { onClose?.(reason); } catch (_) {}
  };
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close('escape'); } };
  document.addEventListener('keydown', onKey, true);
  if (scrimClose) scrim.addEventListener('click', (e) => { if (e.target === scrim) close('scrim'); });

  const paint = () => {
    const body = typeof _content === 'function' ? _content({ close, update }) : _content;
    const foot = typeof _footer === 'function' ? _footer({ close, update }) : _footer;
    render(html`
      <div class="rd-modal ${size}" role="dialog" aria-modal="true" @click=${(e) => e.stopPropagation()}>
        ${heading ? html`<div class="rd-modal-head">
          <div class="rd-modal-ttl">${heading}</div>
          <button class="btn icon sm" title="Close" @click=${() => close('button')}>${icon('x', 14)}</button>
        </div>` : nothing}
        <div class="rd-modal-body">${body ?? nothing}</div>
        ${foot ? html`<div class="rd-modal-foot">${foot}</div>` : nothing}
      </div>`, scrim);
  };

  function update(content, footer) {
    if (content !== undefined) _content = content;
    if (footer !== undefined) _footer = footer;
    paint();
  }

  paint();
  // Focus the first focusable control for keyboard users.
  queueMicrotask(() => {
    try { scrim.querySelector('input, textarea, select, button')?.focus(); } catch (_) {}
  });
  return { el: scrim, close, update };
}

export function confirm({ title, body = '', danger = false, confirmLabel = 'Confirm', cancelLabel = 'Cancel' } = {}) {
  return new Promise((resolve) => {
    let result = false;
    openModal({
      heading: title,
      size: 'sm',
      onClose: () => resolve(result),
      content: ({ close }) => html`
        <div class="rd-confirm-body">${body}</div>
        <div class="rd-modal-foot">
          ${button({ variant: 'ghost', size: 'sm', onClick: () => close() }, cancelLabel)}
          ${button({ variant: danger ? 'danger' : 'primary', size: 'sm', onClick: () => { result = true; close(); } }, confirmLabel)}
        </div>`,
    });
  });
}
