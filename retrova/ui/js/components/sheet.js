// Sheets (modal dialogs) built on <dialog>: focus is trapped, Esc closes.
import { h, bytes, basename, ext, fileKind } from '../util.js';
import { icon } from './icons.js';
import { store } from '../store.js';

/**
 * openSheet({title, subtitle, icon, tone, body, actions, size, dismissible, footNote, onClose})
 * actions: [{label, kind: 'primary'|'danger'|'', icon, onClick(close) -> any|Promise, autofocus, close: true}]
 */
export function openSheet(o) {
  const dlg = h('dialog.sheet', { class: o.size || '', 'aria-label': o.title || 'Dialog' });
  let closed = false;
  const close = (result) => {
    if (closed) return;
    closed = true;
    dlg.close();
    dlg.remove();
    o.onClose?.(result);
  };
  const head = (o.title || o.icon) ? h('div.sheet-head',
    o.icon ? h('div.sheet-icon', { class: o.tone || '' }, icon(o.icon)) : null,
    h('div.grow', h('h2', { text: o.title }), o.subtitle ? h('p', o.subtitle) : null),
    o.closeButton ? h('button.btn.icon.ghost.sheet-close', { 'aria-label': 'Close', onclick: () => close() }, icon('x')) : null) : null;
  const foot = (o.actions && o.actions.length) || o.footNote ? h('div.sheet-foot',
    o.footNote ? h('div.left', icon('shieldLock'), o.footNote) : null,
    (o.actions || []).map(a => {
      const b = h('button.btn', { class: [a.kind || '', o.compact ? '' : ''], autofocus: a.autofocus || undefined },
        a.icon ? icon(a.icon) : null, a.label);
      b.addEventListener('click', async () => {
        if (a.onClick) {
          b.disabled = true;
          try {
            const r = await a.onClick(close);
            if (r === false) { b.disabled = false; return; }
          } catch { b.disabled = false; return; }
          b.disabled = false;
        }
        if (a.close !== false) close(a.value);
      });
      return b;
    })) : null;
  dlg.append(...[head, o.body ? h('div.sheet-body', o.body) : null, o.raw || null, foot].filter(Boolean));
  dlg.addEventListener('cancel', e => { e.preventDefault(); if (o.dismissible !== false) close(); });
  dlg.addEventListener('mousedown', e => {
    if (e.target === dlg && o.dismissible !== false) {
      const r = dlg.getBoundingClientRect();
      const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
      if (!inside) close();
    }
  });
  document.body.append(dlg);
  dlg.showModal();
  const af = dlg.querySelector('[autofocus]');
  if (af) af.focus();
  return { el: dlg, close };
}

/** Ask before doing something. Resolves true/false. */
export function confirmSheet({ title, message, body, confirmLabel = 'Continue', cancelLabel = 'Cancel', danger = false, icon: ic = 'info', tone = '', footNote, size }) {
  return new Promise(resolve => {
    openSheet({
      title, subtitle: message, icon: ic, tone, body, footNote, size,
      onClose: v => resolve(v === true),
      actions: [
        { label: cancelLabel, value: false },
        { label: confirmLabel, kind: danger ? 'danger solid' : 'primary', value: true, autofocus: true },
      ],
    });
  });
}

/** A list of exactly what will happen, used inside confirm sheets. */
export function whatList(items, { home, max = 400 } = {}) {
  // items: [{path, size, note}]
  const rows = items.slice(0, max).map(it => h('div.list-row',
    icon(it.icon || (fileKind(it.path) === 'file' ? 'file' : 'file'), { size: 15 }),
    h('div.grow.ellipsis', { title: it.path, text: it.label || it.path.replace(home || '\u0000', '~') }),
    it.note ? h('span.faint.ellipsis', { style: 'max-width:45%', text: it.note }) : null,
    it.size !== undefined ? h('span.faint.num', { text: bytes(it.size) }) : null));
  if (items.length > max) rows.push(h('div.list-row.faint', `and ${(items.length - max).toLocaleString()} more`));
  return h('div.what-list.selectable', rows);
}

export function summaryRow(pairs) {
  return h('div.what-sum', pairs.map(([v, k]) => h('div', h('b', { text: v }), h('span', { text: k }))));
}

/** Preview a file: image, video, audio, pdf or text. */
export function previewSheet({ name, url, size, path, onReveal, extraActions = [] }) {
  const e = ext(name);
  const kind = fileKind(name);
  let content;
  if (kind === 'image') content = h('img', { src: url, alt: name, onerror: () => content.replaceWith(noPreview(name)) });
  else if (kind === 'video') content = h('video', { src: url, controls: true, autoplay: true, playsinline: true });
  else if (kind === 'audio') content = h('audio', { src: url, controls: true, autoplay: true });
  else if (e === 'pdf') content = h('iframe', { src: url, title: name });
  else if (kind === 'text' || !e) {
    content = h('pre.selectable', { text: 'Loading…' });
    if (size > 2 * 1024 * 1024) content.textContent = 'This file is too large to show as text here.';
    else fetch(url).then(r => r.ok ? r.text() : Promise.reject(new Error(r.statusText)))
      .then(t => { content.textContent = t.slice(0, 400000) || '(empty file)'; })
      .catch(() => { content.textContent = "This file couldn't be read for a preview."; });
  } else content = noPreview(name);
  const sheet = openSheet({
    size: 'xwide',
    title: name,
    subtitle: [path ? h('span.selectable', { text: path }) : null, size !== undefined ? ` - ${bytes(size)}` : ''],
    closeButton: true,
    raw: h('div.preview-body', content),
    actions: [
      ...extraActions,
      onReveal ? { label: store.words.showIn || 'Show in folder', icon: 'reveal', onClick: onReveal, close: false } : null,
      { label: 'Done', kind: 'primary', autofocus: true },
    ].filter(Boolean),
  });
  return sheet;
}

function noPreview(name) {
  return h('div.empty', icon('eyeOff', { size: 28 }), h('h3', { text: 'No preview for this kind of file' }),
    h('p', { text: `${basename(name)} can't be shown here, but it can still be recovered.` }));
}
