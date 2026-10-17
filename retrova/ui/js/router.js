// Hash routes: #/name?key=value
import { h, clear, $ } from './util.js';
import { setActiveNav, setToolbar, setContentMode } from './shell.js';
import { closePopovers } from './components/controls.js';
import { emptyState } from './components/controls.js';

const VIEWS = {
  home: () => import('./views/home.js'),
  recover: () => import('./views/recover.js'),
  verify: () => import('./views/verify.js'),
  space: () => import('./views/space.js'),
  duplicates: () => import('./views/dupes.js'),
  cleanup: () => import('./views/cleanup.js'),
  find: () => import('./views/findtext.js'),
  system: () => import('./views/system.js'),
  activity: () => import('./views/activity.js'),
};

let current = { name: null, cleanup: null };
let seq = 0;

export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [name, query = ''] = raw.split('?');
  const params = Object.fromEntries(new URLSearchParams(query));
  return { name: VIEWS[name] ? name : 'home', params };
}

export function go(name, params = {}) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')).toString();
  const hash = `#/${name}${q ? '?' + q : ''}`;
  if (location.hash === hash) render(); else location.hash = hash;
}

async function render() {
  const my = ++seq;
  const { name, params } = parseHash();
  closePopovers();
  try { current.cleanup?.(); } catch (e) { console.warn(e); }
  const mine = { name, cleanup: null };
  current = mine;
  setActiveNav(name);
  const content = $('#content');
  let mod;
  try {
    mod = await VIEWS[name]();
  } catch (e) {
    console.error(e);
    clear(content, emptyState({ title: "This screen couldn't load", body: String(e.message || e) }));
    return;
  }
  if (my !== seq) return;
  setToolbar({});
  setContentMode(false);
  content.scrollTop = 0;
  const root = h('div.view', { class: `view-${name}` });
  clear(content, root);
  let cleanup = null;
  try {
    cleanup = (await mod.default.mount(root, params)) || null;
  } catch (e) {
    console.error(e);
    clear(root, emptyState({ title: "This screen couldn't load", body: String(e.message || e) }));
  }
  if (my !== seq || current !== mine) { try { cleanup?.(); } catch { /* ignore */ } return; }
  mine.cleanup = cleanup;
}

export function start() {
  window.addEventListener('hashchange', render);
  render();
}
