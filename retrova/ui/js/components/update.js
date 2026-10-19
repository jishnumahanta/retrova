// "Check for updates": the one time Retrova goes online, and only when the user clicks.
// The engine asks GitHub for the latest release; downloads open in the user's own browser.
import { h } from '../util.js';
import { api } from '../api.js';
import { openSheet } from './sheet.js';
import { toast, toastError } from './toast.js';

const openUrl = url => api.post('/api/update/open', { url }).catch(e => toastError(e));

function plainNotes(md) {
  return md.split(/\r?\n/)
    .map(l => l.replace(/^#+\s*/, '').replace(/\*\*(.+?)\*\*/g, '$1').replace(/`([^`]+)`/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').trimEnd())
    .filter((l, i, a) => l || (a[i - 1] && a[i - 1] !== ''))
    .slice(0, 14).join('\n').trim();
}

let busy = false;

export async function checkForUpdates(btn) {
  if (busy) return;
  busy = true;
  const label = btn?.textContent;
  if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  try {
    const r = await api.get('/api/update/check');
    if (!r.newer) {
      toast(`You have the latest version (Retrova ${r.current}).`, { kind: 'ok', timeout: 4000 });
      return;
    }
    const a = r.asset;
    const size = a && a.size ? ` (${Math.round(a.size / 1048576)} MB)` : '';
    const notes = plainNotes(r.notes || '');
    openSheet({
      icon: 'download', title: `Retrova ${r.latest} is available`,
      subtitle: `You have ${r.current}. Updating keeps your settings and search index.`,
      body: h('div.stack',
        notes ? h('div.update-notes', { text: notes }) : null,
        h('p.muted', a
          ? `This downloads ${a.name}${size} in your browser. Open it to install, replacing the old version.`
          : 'Your browser will open the release page, where you can pick the right download.')),
      footNote: 'Only from Retrova’s GitHub releases',
      closeButton: true,
      actions: [
        { label: 'Release notes', onClick: () => { openUrl(r.page); return false; } },
        { label: a ? 'Download update' : 'Open release page', kind: 'primary', autofocus: true, icon: 'download',
          onClick: () => openUrl(a ? a.url : r.page) },
      ],
    });
  } catch (e) {
    toast(e.message || "Couldn't check for updates.", {
      kind: 'error', timeout: 8000,
      action: { label: 'Open releases page', onClick: () => openUrl('https://github.com/jishnumahanta/retrova/releases') },
    });
  } finally {
    busy = false;
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

/** Small text button for the sidebar and the Support sheet. */
export const updateButton = (cls = 'link-btn') => {
  const b = h(`button.${cls}`, { type: 'button', title: 'Asks GitHub for the latest version. Nothing about you is sent.' },
    'Check for updates');
  b.addEventListener('click', () => checkForUpdates(b));
  return b;
};
