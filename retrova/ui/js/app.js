// Retrova UI entry point.
import { api, ready, MOCK } from './api.js';
import { store, setPlatformWords } from './store.js';
import { h, clear, $, setSizeBase } from './util.js';
import { buildSidebar, NAV_ITEMS } from './shell.js';
import { start as startRouter, go } from './router.js';
import { startActivity } from './components/activity.js';
import { emptyState } from './components/controls.js';
import { icon } from './components/icons.js';

async function boot() {
  await ready();
  try {
    store.info = await api.get('/api/info');
  } catch (e) {
    showFatal(e);
    return;
  }
  const info = store.info;
  document.documentElement.dataset.platform = info.platform || 'mac';
  if (MOCK) document.documentElement.dataset.mock = '1';
  setSizeBase(info.platform);
  setPlatformWords(info.platform);

  $('#boot').remove();
  $('#app').hidden = false;
  buildSidebar();
  startActivity();
  startRouter();
  wireKeys();
  wireScrollShadow();
}

function showFatal(e) {
  const boot = $('#boot');
  const unauth = e && e.status === 401;
  clear(boot, emptyState({
    title: unauth ? 'This window has expired' : "Retrova can't reach its engine",
    body: unauth
      ? 'Close this window and open Retrova again - it starts a fresh, private session each time.'
      : `${e?.message || ''} Quit Retrova and open it again. Nothing on your drives was changed.`,
    actions: [h('button.btn', { onclick: () => location.reload() }, icon('refresh'), 'Try again')],
  }));
}

function wireKeys() {
  document.addEventListener('keydown', e => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && !e.shiftKey && !e.altKey && /^[1-9]$/.test(e.key)) {
      const item = NAV_ITEMS[Number(e.key) - 1];
      if (item) { e.preventDefault(); go(item.id); }
    }
    // keep the browser-y shortcuts from leaking into a native-feeling window
    if (mod && ['p', 's', 'u'].includes(e.key.toLowerCase())) e.preventDefault();
  });
  // no browser context menu on chrome
  document.addEventListener('contextmenu', e => {
    if (!e.target.closest('input, textarea, .selectable')) e.preventDefault();
  });
  // dropping files onto the window shouldn't navigate away
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => e.preventDefault());
}

function wireScrollShadow() {
  const content = $('#content');
  const bar = $('#toolbar');
  content.addEventListener('scroll', () => bar.classList.toggle('scrolled', content.scrollTop > 4), { passive: true });
  window.addEventListener('hashchange', () => bar.classList.remove('scrolled'));
}

boot();
