// Asking for administrator rights: the engine restarts elevated (same token, new port)
// and the window moves to it, landing back on the same screen.
import { api, MOCK } from './api.js';
import { store } from './store.js';
import { h } from './util.js';
import { openSheet } from './components/sheet.js';
import { toast, toastError } from './components/toast.js';

export function elevate({ title, reason, returnHash } = {}) {
  const info = store.info;
  if (!info.can_elevate) {
    toast(info.admin ? 'Retrova already has administrator rights.' : info.platform === 'linux'
      ? 'Start Retrova with sudo to do this (for example: sudo python3 -m retrova).'
      : "Retrova can't ask for administrator rights here.", { kind: 'warn' });
    return;
  }
  const win = info.platform === 'windows';
  openSheet({
    icon: 'unlock',
    title: title || 'Allow Retrova to read drives directly',
    subtitle: reason || null,
    body: h('div.stack',
      h('p', `Your ${win ? 'PC will ask you to confirm' : 'Mac will ask for your password'}. Retrova then restarts its engine with administrator rights. Drives are still only ever read.`),
      h('p.muted', 'Files Retrova saves stay owned by you. The extra rights end when you quit Retrova.')),
    footNote: 'Read-only, always',
    actions: [
      { label: 'Not now' },
      {
        label: 'Allow access…', kind: 'primary', autofocus: true,
        onClick: async () => {
          const btn = document.querySelector('dialog.sheet[open] .sheet-foot .btn.primary');
          if (btn) btn.textContent = win ? 'Waiting for Windows…' : 'Waiting for your password…';
          try {
            const r = await api.post('/api/elevate', {});
            const u = new URL(r.url, location.href);
            if (MOCK) u.searchParams.set('mock', '1');
            u.hash = returnHash || location.hash;
            location.href = u.toString();
          } catch (e) {
            toastError(e);
            if (btn) btn.textContent = 'Allow access…';
          }
          return false;
        },
      },
    ],
  });
}
