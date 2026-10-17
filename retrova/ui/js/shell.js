// The window chrome: sidebar navigation and the toolbar each screen fills in.
import { h, clear, $ } from './util.js';
import { icon } from './components/icons.js';
import { store } from './store.js';
import { openSupport } from './components/support.js';

export const NAV = [
  { id: 'home', label: 'Home', icon: 'home' },
  { group: 'Rescue' },
  { id: 'recover', label: 'Recover', icon: 'recover' },
  { id: 'verify', label: 'Verify', icon: 'verify' },
  { group: 'Reclaim' },
  { id: 'space', label: 'Space', icon: 'space' },
  { id: 'duplicates', label: 'Duplicates', icon: 'dupes' },
  { id: 'cleanup', label: 'Cleanup', icon: 'cleanup' },
  { group: 'Care' },
  { id: 'find', label: 'Find Text', icon: 'findtext' },
  { id: 'system', label: 'System', icon: 'system' },
  { sep: true },
  { id: 'activity', label: 'Activity', icon: 'activity' },
];
export const NAV_ITEMS = NAV.filter(n => n.id);

export function buildSidebar() {
  const mod = store.info?.platform === 'mac' ? '⌘' : 'Ctrl+';
  const nav = $('#nav');
  clear(nav, NAV.map(n => {
    if (n.group) return [h('div.nav-group', { text: n.group }), h('div.nav-group-gap')];
    if (n.sep) return h('div.nav-sep');
    const idx = NAV_ITEMS.indexOf(n) + 1;
    return h('a.nav-item', { href: `#/${n.id}`, dataset: { route: n.id }, title: `${n.label}  ${mod}${idx}` },
      icon(n.icon), h('span.nav-label', { text: n.label }), h('span.nav-count.hidden', { dataset: { count: n.id } }));
  }));
  const info = store.info || {};
  clear($('#sidebar-foot'), icon('shieldLock'),
    h('div', h('b', { text: 'Your drives stay safe' }),
      `Drives you recover from are only read. Anything removed goes to the ${store.words.trash}.`));
  $('#sidebar-support')?.remove();
  $('#sidebar-ver').before(h('button.sidebar-support', { id: 'sidebar-support', type: 'button', onclick: openSupport }, icon('heart'), 'Support Retrova'));
  clear($('#sidebar-ver'),
    h('span', { text: `Retrova ${info.version || ''} · AGPL-3.0`,
      title: 'Copyright © 2026 Jishnu Mahanta. Free software under the GNU AGPL-3.0, with no warranty. Source code: github.com/jishnumahanta/retrova' }),
    info.admin ? h('span.badge.ok', { text: 'Disk access on', title: 'Running with administrator rights' }) : null);
}

export function setActiveNav(id) {
  document.querySelectorAll('.nav-item').forEach(a => {
    if (a.dataset.route === id) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
}

export function setNavCount(id, n) {
  const el = document.querySelector(`[data-count="${id}"]`);
  if (!el) return;
  el.textContent = n > 9 ? '9+' : String(n);
  el.classList.toggle('hidden', !n);
}

/** setToolbar({title, sub, center: Node, actions: [Node]}) */
export function setToolbar({ title = '', sub = '', center = null, actions = [] } = {}) {
  clear($('#toolbar-title'), title ? h('h1', { text: title }) : null, sub ? h('span.sub', sub) : null);
  clear($('#toolbar-center'), center);
  clear($('#toolbar-actions'), actions);
}
export function setToolbarActions(actions = []) { clear($('#toolbar-actions'), actions); }
export function setToolbarCenter(node) { clear($('#toolbar-center'), node); }
export function setToolbarSub(sub) {
  const t = $('#toolbar-title');
  const s = t.querySelector('.sub');
  if (s) clear(s, sub); else if (sub) t.append(h('span.sub', sub));
}

/** fill: the screen manages its own scrolling panes (no outer scroll). */
export function setContentMode(fill) {
  $('#content').classList.toggle('fill', !!fill);
}

export function navIcon(id) { return icon(NAV.find(n => n.id === id)?.icon || 'home'); }
