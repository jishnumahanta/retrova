// Small reusable controls: segmented control, switch, checkbox, empty state, popover menu.
import { h, clear } from '../util.js';
import { icon } from './icons.js';

export function segmented({ options, value, onChange, cls = '', label = '' }) {
  const el = h('div.seg', { class: cls, role: 'group', 'aria-label': label });
  const btns = options.map(o => h('button', {
    type: 'button',
    'aria-pressed': String(o.value === value),
    title: o.title || undefined,
    onclick: () => { set(o.value); onChange?.(o.value); },
  }, o.icon ? icon(o.icon) : null, o.label));
  el.append(...btns);
  function set(v) {
    value = v;
    btns.forEach((b, i) => b.setAttribute('aria-pressed', String(options[i].value === v)));
  }
  el.set = set;
  el.get = () => value;
  return el;
}

export function toggle({ label, checked = false, onChange, title }) {
  const input = h('input', { type: 'checkbox', role: 'switch', checked, onchange: () => onChange?.(input.checked) });
  const el = h('label.switch', { title }, input, label);
  el.input = input;
  return el;
}

export function checkbox({ checked = false, indeterminate = false, disabled = false, label, onChange, title, ariaLabel }) {
  const input = h('input.check', { type: 'checkbox', checked, disabled, title, 'aria-label': ariaLabel || (typeof label === 'string' ? label : undefined) });
  input.indeterminate = indeterminate;
  input.addEventListener('change', () => onChange?.(input.checked));
  input.addEventListener('click', e => e.stopPropagation());
  if (!label) return input;
  return h('label.check-label', input, label);
}

/** Decorative sector art for empty states: a quiet version of the ring. */
export function sectorArt(missing = 1) {
  const tpl = document.createElement('template');
  const segs = [];
  for (let i = 0; i < 12; i++) {
    const a = (-86.5 + i * 30) * Math.PI / 180, b = (-86.5 + i * 30 + 23) * Math.PI / 180;
    const p = (r, t) => `${(48 + r * Math.cos(t)).toFixed(2)} ${(48 + r * Math.sin(t)).toFixed(2)}`;
    const d = `M ${p(40, a)} A 40 40 0 0 1 ${p(40, b)} L ${p(29.8, b)} A 29.8 29.8 0 0 0 ${p(29.8, a)} Z`;
    if (i === missing) {
      segs.push(`<path d="${d}" fill="none" stroke="currentColor" stroke-opacity=".35" stroke-dasharray="2.5 2.5"/>`);
      const mid = (a + b) / 2, dx = Math.cos(mid) * 9, dy = Math.sin(mid) * 9;
      segs.push(`<path d="${d}" transform="translate(${dx.toFixed(2)} ${dy.toFixed(2)})" fill="var(--accent)" opacity=".9"/>`);
    } else segs.push(`<path d="${d}" fill="currentColor" fill-opacity="${0.10 + (i % 3) * 0.03}"/>`);
  }
  tpl.innerHTML = `<svg class="empty-art" viewBox="0 0 96 96" aria-hidden="true">${segs.join('')}</svg>`;
  return tpl.content.firstChild;
}

export function emptyState({ art = true, iconName, title, body, actions = [], cls = '' }) {
  return h('div.empty', { class: cls },
    art ? (iconName ? h('div.empty-icon', icon(iconName, { size: 26 })) : sectorArt()) : null,
    title ? h('h3', { text: title }) : null,
    body ? h('p', body) : null,
    actions.length ? h('div.row', actions) : null);
}

/** Popover anchored to an element. items: [{label, icon, onClick} | 'sep'] or a custom node */
export function popover(anchor, content, { align = 'end', width } = {}) {
  closePopovers();
  const pop = h('div.popover', { role: 'menu' });
  if (width) pop.style.width = width + 'px';
  if (Array.isArray(content)) {
    for (const it of content) {
      if (it === 'sep') { pop.append(h('div.menu-sep')); continue; }
      pop.append(h('button.menu-item', { role: 'menuitem', onclick: () => { close(); it.onClick?.(); } },
        it.icon ? icon(it.icon) : null, h('span.grow', it.label)));
    }
  } else pop.append(content);
  document.body.append(pop);
  const r = anchor.getBoundingClientRect();
  const pw = pop.offsetWidth;
  let left = align === 'end' ? r.right - pw : r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  pop.style.left = left + 'px';
  pop.style.top = (r.bottom + 6) + 'px';
  const maxH = window.innerHeight - r.bottom - 20;
  pop.style.maxHeight = maxH + 'px';
  pop.style.overflow = 'auto';
  const onDown = e => { if (!pop.contains(e.target) && !anchor.contains(e.target)) close(); };
  const onKey = e => { if (e.key === 'Escape') { close(); anchor.focus(); } };
  setTimeout(() => { document.addEventListener('mousedown', onDown); document.addEventListener('keydown', onKey); });
  function close() {
    pop.remove();
    document.removeEventListener('mousedown', onDown);
    document.removeEventListener('keydown', onKey);
  }
  pop.close = close;
  const first = pop.querySelector('button');
  if (first) first.focus();
  return pop;
}
export function closePopovers() { document.querySelectorAll('.popover').forEach(p => p.close ? p.close() : p.remove()); }

/** Disclosure section with a chevron. */
export function disclosure({ title, meta, open = false, body, onToggle, cls = '' }) {
  const chev = icon('chevronRight', { cls: 'disc-chev' });
  const metaEl = h('span.disc-meta', meta);
  const head = h('button.disc-head', { type: 'button', 'aria-expanded': String(open) }, chev, h('span.disc-title', title), metaEl);
  const bodyEl = h('div.disc-body', body);
  const el = h('div.disc', { class: [cls, open ? 'open' : ''] }, head, bodyEl);
  head.addEventListener('click', () => {
    open = !open;
    el.classList.toggle('open', open);
    head.setAttribute('aria-expanded', String(open));
    onToggle?.(open);
  });
  el.setMeta = m => clear(metaEl, m);
  el.body = bodyEl;
  return el;
}

/** Capacity bar. value 0..1; tone ok|warn|bad|unknown */
export function capBar(value, tone = '', cls = '') {
  const fill = h('i');
  const el = h('div.bar', { class: [tone, cls] }, fill);
  el.set = (v, t) => {
    fill.style.width = v === null || v === undefined ? '0%' : `${Math.max(0, Math.min(1, v)) * 100}%`;
    el.className = `bar ${t || ''} ${cls}`;
  };
  el.set(value, tone);
  return el;
}

/** Stats grid: [{k, v, tone, small}] */
export function statsGrid(items) {
  const el = h('div.stats');
  el.set = list => clear(el, list.filter(Boolean).map(s => h('div.stat', { class: s.tone || '' },
    h('div.k', { text: s.k }), h('div.v', { title: s.title || undefined }, s.v, s.small ? h('small', { text: s.small }) : null))));
  el.set(items || []);
  return el;
}
