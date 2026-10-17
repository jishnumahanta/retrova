// Choosing folders and files: the system dialog when the app window can show one,
// otherwise an in-app browser over /api/fs/places + /api/fs/ls.
import { api } from '../api.js';
import { h, clear, bytes, date, basename, ext, fileKind, pathCrumbs } from '../util.js';
import { icon, kindIcon } from './icons.js';
import { openSheet } from './sheet.js';
import { spinner } from './ring.js';
import { toastError } from './toast.js';

let nativeWorks = true;   // flips off after the first 409 so we don't keep asking

async function tryNative(body) {
  if (!nativeWorks) return undefined;
  try {
    const r = await api.post('/api/native/pick', body);
    return (r.paths || [])[0] || null;          // null = cancelled
  } catch (e) {
    if (e.status === 409 || e.status === 404) { nativeWorks = false; return undefined; }
    toastError(e);
    return undefined;
  }
}

/** Resolve to a folder path or null. */
export async function pickFolder({ title = 'Choose a folder', start = '', confirmLabel = 'Choose' } = {}) {
  const n = await tryNative({ kind: 'folder', directory: start || undefined });
  if (n !== undefined) return n;
  return browse({ mode: 'folder', title, start, confirmLabel });
}

/** Resolve to a file path or null. exts: ['img','dmg',...] to highlight matching files. */
export async function pickFile({ title = 'Choose a file', start = '', exts = null, confirmLabel = 'Open' } = {}) {
  const n = await tryNative({ kind: 'file', directory: start || undefined });
  if (n !== undefined) return n;
  return browse({ mode: 'file', title, start, exts, confirmLabel });
}

const PLACE_ICON = { home: 'home', desktop: 'grid', documents: 'doc', downloads: 'download', pictures: 'image', drive: 'drive', volume: 'driveExt', disk: 'drive' };

function browse({ mode, title, start, exts, confirmLabel }) {
  return new Promise(resolve => {
    let cur = null, parent = null, chosenFile = null, entries = [];
    const placesEl = h('div.fp-places');
    const crumbs = h('div.fp-crumbs');
    const listEl = h('div.fp-list', { role: 'listbox', tabindex: '0' });
    const pathField = h('input.field.sm', { type: 'text', spellcheck: 'false', 'aria-label': 'Path' });
    const upBtn = h('button.btn.icon.sm.ghost', { 'aria-label': 'Up one folder', title: 'Up one folder', onclick: () => parent && go(parent) }, icon('arrowUp'));
    let chooseBtn;
    const body = h('div.fp',
      placesEl,
      h('div.fp-main', h('div.fp-bar', upBtn, crumbs), listEl,
        h('div.fp-path', h('span.faint', { text: mode === 'folder' ? 'Folder' : 'File' }), pathField)));

    const sheet = openSheet({
      title, size: 'wide', body,
      onClose: v => resolve(typeof v === 'string' ? v : null),
      actions: [
        { label: 'Cancel', value: null },
        { label: confirmLabel, kind: 'primary', onClick: () => {
          const p = pathField.value.trim();
          if (!p) return false;
          sheet.close(p);
          return false;
        } },
      ],
    });
    chooseBtn = sheet.el.querySelector('.sheet-foot .btn.primary');
    pathField.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); const v = pathField.value.trim(); if (mode === 'folder' || /\.[^\\/]+$/.test(v)) sheet.close(v); else go(v); } });

    api.get('/api/fs/places').then(r => {
      const places = r.places || [];
      clear(placesEl, places.map(p => h('button.fp-place', { title: p.path, onclick: () => go(p.path), dataset: { path: p.path } },
        icon(PLACE_ICON[p.icon] || 'folder'), h('span.ellipsis', { text: p.name }))));
      if (!start && places[0]) go(places[0].path);
    }).catch(e => clear(placesEl, h('div.hint', { text: e.message })));
    if (start) go(start);

    async function go(path, tries = 0) {
      clear(listEl, h('div.fp-loading', spinner(20)));
      if (tries === 0 && path === start) {
        // the suggested folder may not exist yet: show its nearest existing parent, keep the name in the field
        try { await api.get('/api/fs/ls', { path }); } catch {
          const parentPath = path.replace(/[\\/][^\\/]+[\\/]?$/, '') || '/';
          if (parentPath !== path) { await go(parentPath, 1); if (mode === 'folder') pathField.value = path; return; }
        }
      }
      try {
        const r = await api.get('/api/fs/ls', { path });
        cur = r.path; parent = r.parent; chosenFile = null;
        entries = (r.entries || []).filter(e => !e.name.startsWith('.'))
          .sort((a, b) => (a.kind === 'dir' ? 0 : 1) - (b.kind === 'dir' ? 0 : 1) || a.name.localeCompare(b.name, undefined, { numeric: true }));
        upBtn.disabled = !parent;
        renderCrumbs();
        renderList();
        pathField.value = mode === 'folder' ? cur : '';
        placesEl.querySelectorAll('.fp-place').forEach(b => b.classList.toggle('on', b.dataset.path === cur));
        updateChoose();
      } catch (e) {
        clear(listEl, h('div.empty', icon('warning', { size: 22 }), h('p', { text: e.message })));
      }
    }
    function renderCrumbs() {
      const out = [];
      pathCrumbs(cur).forEach((c, i) => {
        if (i) out.push(icon('chevronRight', { size: 12, cls: 'crumb-sep' }));
        out.push(h('button.crumb', { text: c.name, onclick: () => go(c.path) }));
      });
      clear(crumbs, out);
      crumbs.scrollLeft = 1e6;
    }
    function renderList() {
      if (!entries.length) { clear(listEl, h('div.empty.small', h('p', { text: 'This folder is empty.' }))); return; }
      clear(listEl, entries.map(e => {
        const isDir = e.kind === 'dir';
        const match = !exts || isDir || exts.includes(ext(e.name));
        const row = h('button.fp-row', {
          class: { dim: !isDir && (mode === 'folder' || !match) },
          role: 'option',
          tabindex: '-1',
          onclick: () => {
            if (isDir) { if (mode === 'folder') { pathField.value = e.path; select(row); } else go(e.path); }
            else if (mode === 'file' && match) { chosenFile = e.path; pathField.value = e.path; select(row); updateChoose(); }
          },
          ondblclick: () => {
            if (isDir) go(e.path);
            else if (mode === 'file' && match) sheet.close(e.path);
          },
        }, icon(isDir ? 'folder' : kindIcon(fileKind(e.name)), { cls: isDir ? 'tone-dir' : '' }),
        h('span.grow.ellipsis', { text: e.name }),
        h('span.faint.num', { text: isDir ? '' : bytes(e.size) }),
        h('span.faint.num.fp-date', { text: date(e.mtime) }));
        return row;
      }));
    }
    function select(row) {
      listEl.querySelectorAll('.fp-row.on').forEach(r => r.classList.remove('on'));
      row.classList.add('on');
    }
    function updateChoose() {
      if (chooseBtn) chooseBtn.disabled = mode === 'file' && !chosenFile && !pathField.value.trim();
    }
    pathField.addEventListener('input', updateChoose);
    listEl.addEventListener('keydown', e => {
      const rows = [...listEl.querySelectorAll('.fp-row')];
      const i = rows.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); (rows[i + 1] || rows[0])?.focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); (rows[i - 1] || rows[0])?.focus(); }
      else if (e.key === 'Enter' && i >= 0) { e.preventDefault(); rows[i].dispatchEvent(new MouseEvent('dblclick')); }
      else if (e.key === 'Backspace' && parent) { e.preventDefault(); go(parent); }
    });
  });
}

export const nameOf = basename;
