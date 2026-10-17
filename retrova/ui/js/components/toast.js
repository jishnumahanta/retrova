// Toasts: short messages at the bottom of the window. Errors stay a little longer.
import { h } from '../util.js';
import { icon } from './icons.js';

const ICON = { info: 'info', ok: 'checkCircle', error: 'error', warn: 'warning' };

export function toast(message, { kind = 'info', action = null, timeout } = {}) {
  const region = document.getElementById('toasts');
  const close = () => {
    if (!el.isConnected) return;
    el.classList.add('out');
    setTimeout(() => el.remove(), 200);
  };
  const el = h('div.toast', { class: kind, role: kind === 'error' ? 'alert' : 'status' },
    icon(ICON[kind] || 'info'),
    h('div.msg.selectable', { text: message }),
    action ? h('button.btn.sm', { text: action.label, onclick: () => { close(); action.onClick(); } }) : null,
    h('button.btn.sm.icon.ghost', { 'aria-label': 'Dismiss', onclick: close }, icon('x')));
  region.append(el);
  while (region.children.length > 4) region.firstChild.remove();
  setTimeout(close, timeout ?? (kind === 'error' ? 8000 : action ? 7000 : 4200));
  return close;
}

/** Show an API (or any) error. The server's message is written for people, so show it as is. */
export function toastError(e, prefix = '') {
  const msg = (e && e.message) || String(e || 'Something went wrong.');
  return toast(prefix ? `${prefix} ${msg}` : msg, { kind: 'error' });
}

/** await-able wrapper: run fn, toast any error, return result or undefined */
export async function attempt(fn, prefix) {
  try { return await fn(); } catch (e) { toastError(e, prefix); return undefined; }
}
