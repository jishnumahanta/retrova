// Recover > Browse files: pick what to copy, see what fits, start.
import { api, previewUrl, watchJob, jobResult, isLive } from '../api.js';
import { store, emit } from '../store.js';
import { h, clear, bytes, num, date, basename, fileKind, previewable, pathCrumbs, debounce, plural, tildify } from '../util.js';
import { icon, kindIcon, KIND_TONE } from '../components/icons.js';
import { checkbox, toggle, segmented, disclosure, emptyState } from '../components/controls.js';
import { vlist } from '../components/vlist.js';
import { spinner } from '../components/ring.js';
import { previewSheet } from '../components/sheet.js';
import { toastError, toast } from '../components/toast.js';
import { pickFolder } from '../components/filepicker.js';
import { render as renderAll, onDispose, openFailed } from './recover.js';

// ------------------------------------------------------------------ selection model
// selection: Map(path -> {kind, size}) of ticked roots. excluded: Set of paths inside a ticked root that are skipped.
const under = (path, root) => path === root || path.startsWith(root === '/' ? '/' : root + '/');
function coveredBy(S, path) { for (const p of S.selection.keys()) if (under(path, p)) return p; return null; }
function excludedBy(S, path) { for (const x of S.excluded) if (under(path, x)) return x; return null; }
function dropExclusionsUnder(S, root) { for (const x of [...S.excluded]) if (under(x, root)) S.excluded.delete(x); }
function toggleSel(S, r, on) {
  const cov = coveredBy(S, r.path);
  if (cov && cov !== r.path) {              // inside a ticked folder: untick = skip, tick = include again
    if (on) { S.excluded.delete(r.path); dropExclusionsUnder(S, r.path); } else S.excluded.add(r.path);
  } else if (on) {
    for (const p of [...S.selection.keys()]) if (p !== r.path && under(p, r.path)) S.selection.delete(p);
    dropExclusionsUnder(S, r.path);
    if (!coveredBy(S, r.path)) S.selection.set(r.path, { kind: r.kind, size: r.size });
  } else {
    S.selection.delete(r.path);
    dropExclusionsUnder(S, r.path);
  }
}
const patterns = S => S.patterns.split(',').map(s => s.trim()).filter(Boolean);
const patKey = S => patterns(S).slice().sort().join(',');
function sizeOf(S, path, kind, size) {
  if (kind !== 'dir') return { state: 'done', bytes: size || 0, files: 1 };
  return S.sizes.get(path + '|' + patKey(S)) || null;
}

// ------------------------------------------------------------------ view
export function renderFiles(body, S) {
  if (S.opening) {
    body.append(h('div.panel.rc-loading', spinner(28), h('p', `Opening ${S.source?.label || 'the drive'}…`), h('p.hint', 'Reading its file system. Nothing is written to it.')));
    return;
  }
  if (!S.session) { openFailed(body, S); return; }

  const ui = {};
  const crumbs = h('div.crumbs');
  const upBtn = h('button.btn.icon.sm.ghost', { title: 'Up one folder (Backspace)', 'aria-label': 'Up one folder', onclick: () => goUp() }, icon('arrowUp'));
  const backBtn = h('button.btn.icon.sm.ghost', { title: 'Back', 'aria-label': 'Back', onclick: () => goBack() }, icon('chevronLeft'));
  const hiddenT = toggle({ label: 'Hidden files', checked: S.showHidden, onChange: v => { S.showHidden = v; refreshRows(); } });
  const canDeleted = !!S.session.can_list_deleted;
  const deletedT = canDeleted ? toggle({ label: 'Show deleted files', checked: S.showDeleted, title: 'Also list files that were deleted but may still be on the drive, with their names and folders',
    onChange: v => { S.showDeleted = v; load(S.cwd, { push: false }); } }) : null;
  const measureBtn = h('button.btn.sm', { title: 'Work out the size of every folder here', onclick: () => measureHere() }, icon('space'), 'Measure folders');
  const selAllBtn = h('button.btn.sm', { onclick: () => selectAllHere() }, icon('check'), 'Select all');
  const headCheck = checkbox({ ariaLabel: 'Select everything in this folder', onChange: on => { selectAllHere(on); } });
  const status = h('div.browser-status');
  const delHint = h('div.deleted-hint');
  const table = vlist({ rowHeight: 36, render: i => rowEl(i) });
  table.el.setAttribute('role', 'grid');
  table.el.setAttribute('aria-label', 'Files on the drive');
  table.el.tabIndex = 0;

  const browser = h('section.panel.browser',
    h('div.browser-bar', backBtn, upBtn, crumbs, h('span.grow'), deletedT, hiddenT, measureBtn, selAllBtn),
    delHint,
    h('div.vtable', h('div.thead.rc-cols', headCheck, h('span', 'Name'), h('span.r', 'Size'), h('span.r', 'Modified'), h('span')), table.el),
    status);

  const basket = h('aside.panel.basket');
  body.append(h('div.rc-files', browser, basket));

  let visible = [];
  let focus = -1;

  function rowEl(i) {
    const r = visible[i];
    const cov = coveredBy(S, r.path);
    const exBy = cov ? excludedBy(S, r.path) : null;
    const kind = r.kind === 'dir' ? 'dir' : r.kind === 'link' ? 'link' : fileKind(r.name);
    const cb = checkbox({
      checked: !!cov && !exBy,
      disabled: (!!exBy && exBy !== r.path) || r.kind === 'other',
      ariaLabel: `Recover ${r.name}`,
      title: cov && cov !== r.path ? (exBy ? 'Skipped. Tick to include it again.' : 'Untick to skip this') : undefined,
      onChange: on => { toggleSel(S, r, on); afterSelect(r, on); },
    });
    const nameText = h('span.ellipsis', { text: r.name });
    const row = h('div.trow.rc-cols', {
      role: 'row',
      class: { sel: !!cov && !exBy, skip: !!exBy, dim: r.name.startsWith('.'), focus: i === focus, isdir: r.kind === 'dir' },
      onclick: e => { if (e.target.closest('button, input')) return; setFocus(i); if (r.kind === 'dir' && e.target.closest('.name .ellipsis')) open(r.path); },
      ondblclick: e => { if (e.target.closest('button, input')) return; activate(r); },
    },
    cb,
    h('span.name', icon(kindIcon(kind), { cls: 'kind-ic' }), nameText,
      r.error ? h('span.badge.bad', { title: r.error }, 'Unreadable') : null,
      deletedBadge(r),
      regenBadge(r.path)),
    h('span.r.cell-num', sizeCell(r)),
    h('span.r.cell-num', { text: date(r.mtime) }),
    h('span.row-actions', r.kind === 'file' && previewable(r.name) ? h('button.btn.icon.sm.ghost', { title: 'Preview', 'aria-label': `Preview ${r.name}`, onclick: () => preview(r) }, icon('eye')) : null));
    row.querySelector('.kind-ic').style.color = KIND_TONE[kind] || '';
    return row;
  }
  const RECOVER = { good: ['ok', 'Good'], overwritten: ['bad', 'Possibly overwritten'], incomplete: ['warn', 'May be incomplete'] };
  function deletedBadge(r) {
    if (!r.deleted) return null;
    const [tone, text] = RECOVER[r.recover] || ['', 'Deleted'];
    return h('span.badge', { class: tone, title: r.recover_note || 'Deleted, but its name and folder are still on the drive.' }, r.kind === 'file' ? text : 'Deleted');
  }
  function regenBadge(path) {
    const f = S.regen.info.get(path);
    if (!f) return null;
    return h('span.badge', { class: S.excluded.has(path) ? 'safe' : 'review', title: `${f.what}. Recreate with: ${f.regen}` }, S.excluded.has(path) ? 'Skipped, rebuildable' : 'Rebuildable');
  }
  function sizeCell(r) {
    if (r.kind === 'file') return bytes(r.size);
    if (r.kind !== 'dir') return '';
    const s = sizeOf(S, r.path, 'dir');
    if (!s) return h('button.measure', { onclick: () => requestSizes([r.path]) }, 'Measure');
    if (s.state === 'done') return h('span', { title: `${num(s.files)} files${s.errors ? `, ${num(s.errors)} unreadable` : ''}` }, s.errors ? icon('warning', { size: 13, cls: 'warn-text' }) : null, ' ', bytes(s.bytes));
    if (s.state === 'error') return h('span.bad-text', { title: s.msg || '' }, "Can't measure");
    return h('span.measuring', spinner(12), s.bytes ? bytes(s.bytes) : 'Measuring');
  }

  function refreshRows() {
    visible = S.entries.filter(r => S.showHidden || !r.name.startsWith('.'))
      .sort((a, b) => (a.kind === 'dir' ? 0 : 1) - (b.kind === 'dir' ? 0 : 1) || a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }));
    if (focus >= visible.length) focus = visible.length - 1;
    table.setCount(visible.length);
    const ticked = visible.filter(r => { const c = coveredBy(S, r.path); return c && !excludedBy(S, r.path); }).length;
    headCheck.checked = visible.length > 0 && ticked === visible.length;
    headCheck.indeterminate = ticked > 0 && ticked < visible.length;
    const dirs = visible.filter(r => r.kind === 'dir').length;
    const inDeleted = under(S.cwd || '/', '/Deleted files');
    clear(delHint, inDeleted ? [icon('info', { size: 14 }), h('span', 'These files were deleted. Their names and folders come from what the drive still remembers, and nothing here is written back to it. Files marked “Possibly overwritten” or “May be incomplete” can open wrongly or not at all, so check the copies.')] : null);
    delHint.hidden = !inDeleted;
    clear(status, visible.length ? `${plural(dirs, 'folder')}, ${plural(visible.length - dirs, 'file')}` : '',
      S.entries.length !== visible.length ? h('span.faint', ` (${num(S.entries.length - visible.length)} hidden)`) : null,
      ...S.deletedNotes.map(n => h('span.faint', ` ${n}`)));
    table.el.querySelector('.vl-spacer > .empty')?.remove();
    if (!visible.length && !ui.loading) table.el.querySelector('.vl-spacer').append(h('div.empty.small', h('p', { text: S.entries.length ? 'Only hidden files here. Turn on Hidden files to see them.' : 'This folder is empty.' })));
  }
  function setFocus(i, scroll = false) {
    const prev = table.row(focus);
    focus = i;
    prev?.classList.remove('focus');
    table.row(i)?.classList.add('focus');
    if (scroll) table.scrollTo(i);
  }
  function activate(r) {
    if (r.kind === 'dir') open(r.path);
    else if (previewable(r.name)) preview(r);
  }

  async function load(path, { push = true } = {}) {
    ui.loading = true;
    if (S.showDeleted) clear(status, spinner(13), ' Looking for deleted files. A big drive can take a few minutes the first time.');
    try {
      const r = await api.get('/api/recover/ls', { sid: S.session.sid, path, ...(S.showDeleted && canDeleted ? { deleted: 1 } : {}) });
      if (push && S.cwd !== r.path) S.history.push(S.cwd);
      S.cwd = r.path || path;
      S.entries = r.entries || [];
      S.deletedNotes = r.deleted_notes || [];
      focus = -1;
      table.el.scrollTop = 0;
    } catch (e) {
      toastError(e);
    }
    ui.loading = false;
    renderCrumbs();
    refreshRows();
  }
  const open = path => load(path);
  function goUp() { if (S.cwd === '/' || !S.cwd) return; const parent = S.cwd.replace(/\/[^/]+\/?$/, '') || '/'; load(parent); }
  function goBack() { const p = S.history.pop(); if (p !== undefined) load(p, { push: false }); }
  function renderCrumbs() {
    const parts = pathCrumbs(S.cwd || '/', S.session.label || 'Drive');
    const out = [];
    parts.forEach((c, i) => {
      if (i) out.push(icon('chevronRight', { cls: 'crumb-sep' }));
      out.push(h('button.crumb', { text: c.name, onclick: () => load(c.path) }));
    });
    clear(crumbs, out);
    requestAnimationFrame(() => { crumbs.scrollLeft = crumbs.scrollWidth; });
    upBtn.disabled = S.cwd === '/';
    backBtn.disabled = !S.history.length;
  }

  function selectAllHere(on = true) {
    for (const r of visible) if (r.kind !== 'other') toggleSel(S, r, on);
    const dirs = visible.filter(r => r.kind === 'dir').map(r => r.path);
    if (on && dirs.length) requestSizes(dirs.filter(p => S.selection.has(p)));
    table.refresh(); refreshRows(); renderBasket();
  }
  function afterSelect(r, on) {
    if (on && r.kind === 'dir' && S.selection.has(r.path)) requestSizes([r.path]);
    table.refresh(); refreshRows(); renderBasket();
  }
  function measureHere() {
    const dirs = visible.filter(r => r.kind === 'dir').map(r => r.path);
    if (dirs.length) requestSizes(dirs);
    else toast('There are no folders to measure here.');
  }

  // ---- folder sizes: the engine measures in the background; we poll for answers
  async function requestSizes(paths) {
    const pk = patKey(S);
    for (const p of paths) if (!S.sizes.has(p + '|' + pk)) S.sizes.set(p + '|' + pk, { state: 'queued', bytes: 0, files: 0 });
    table.refresh();
    await pollSizes(paths);
  }
  async function pollSizes(paths) {
    const pk = patKey(S);
    const pending = paths || [...S.sizes.entries()].filter(([k, v]) => k.endsWith('|' + pk) && (v.state === 'queued' || v.state === 'running')).map(([k]) => k.slice(0, k.length - pk.length - 1));
    if (!pending.length) return;
    try {
      const r = await api.post('/api/recover/size', { sid: S.session.sid, paths: pending, patterns: patterns(S) });
      for (const [p, v] of Object.entries(r.sizes || {})) S.sizes.set(p + '|' + pk, v);
    } catch (e) {
      for (const p of pending) S.sizes.set(p + '|' + pk, { state: 'error', bytes: 0, files: 0, msg: e.message });
    }
    table.refresh();
    renderBasket();
  }
  const sizeTimer = setInterval(() => pollSizes(), 1000);
  onDispose(() => clearInterval(sizeTimer));

  function preview(r) {
    previewSheet({ name: r.name, url: previewUrl(S.session.sid, r.path), size: r.size, path: r.path });
  }

  // ---- keyboard: arrows move, space ticks, enter opens, backspace goes up
  table.el.addEventListener('keydown', e => {
    if (e.target !== table.el && e.target.closest('input,button') && e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const n = visible.length;
    if (e.key === 'ArrowDown') { e.preventDefault(); setFocus(Math.min(n - 1, focus + 1), true); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setFocus(Math.max(0, focus - 1), true); }
    else if (e.key === 'Home') { e.preventDefault(); setFocus(0, true); }
    else if (e.key === 'End') { e.preventDefault(); setFocus(n - 1, true); }
    else if (e.key === ' ' && focus >= 0) {
      e.preventDefault();
      const r = visible[focus];
      const cov = coveredBy(S, r.path);
      const on = !(cov && !excludedBy(S, r.path));
      toggleSel(S, r, on); afterSelect(r, on);
    } else if (e.key === 'Enter' && focus >= 0) { e.preventDefault(); activate(visible[focus]); }
    else if (e.key === 'Backspace' || (e.key === 'ArrowUp' && (e.metaKey || e.altKey))) { e.preventDefault(); goUp(); }
    else if (e.key === 'ArrowRight' && focus >= 0 && visible[focus].kind === 'dir') { e.preventDefault(); open(visible[focus].path); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); goUp(); }
  });

  // ------------------------------------------------------------------ basket
  const freeInfo = { free: null, out: null };
  const loadFree = debounce(async () => {
    if (!S.out) return;
    try {
      const r = await api.get('/api/recover/free', { out: S.out });
      freeInfo.free = r.free; freeInfo.out = S.out;
    } catch {
      const v = (store.drives?.volumes || []).filter(v => S.out.startsWith(v.mount)).sort((a, b) => b.mount.length - a.mount.length)[0];
      freeInfo.free = v ? v.free : null; freeInfo.out = S.out;
    }
    renderBasket();
  }, 250);
  if (!store.drives) api.get('/api/drives').then(r => { store.drives = r; renderBasket(); }).catch(() => {});

  function totals() {
    let total = 0, files = 0, unknown = 0;
    for (const [p, v] of S.selection) {
      const s = sizeOf(S, p, v.kind, v.size);
      if (s && s.state === 'done') { total += s.bytes; files += s.files || 0; } else unknown++;
      for (const x of S.excluded) if (x !== p && under(x, p)) {
        const sx = S.sizes.get(x + '|' + patKey(S));
        if (sx && sx.state === 'done') { total -= sx.bytes; files -= sx.files || 0; }
      }
    }
    return { total: Math.max(0, total), files: Math.max(0, files), unknown };
  }

  let regenWatch = null;
  onDispose(() => regenWatch?.stop());
  const regenDisc = disclosure({ title: 'Skip rebuildable folders', open: S.regen.found.length > 0, body: h('div') });
  const optDisc = disclosure({ title: 'Options', body: optionsBody() });
  const listBox = h('div.bk-list');
  const totalBox = h('div.bk-total');
  const destBox = h('div.bk-dest');
  const startBox = h('div.bk-foot');
  basket.append(h('div.bk-scroll',
    h('div.bk-section', h('div.bk-head', h('h3', 'To recover'), totalBox), listBox),
    regenDisc,
    h('div.bk-section.bk-dest-wrap', h('div.bk-head', h('h3', 'Save to')), destBox),
    optDisc), startBox);

  function renderBasket() {
    if (!basket.isConnected) return;
    const t = totals();
    clear(totalBox, S.selection.size ? h('span.num', { text: bytes(t.total) }) : null);
    // list
    if (!S.selection.size) {
      clear(listBox, h('div.bk-empty', icon('check'), h('span', 'Tick the folders and files you want back. Ticking a folder takes everything in it.')));
    } else {
      const rows = [];
      for (const [p, v] of S.selection) {
        const s = sizeOf(S, p, v.kind, v.size);
        const skips = [...S.excluded].filter(x => x !== p && under(x, p));
        const regenSkips = skips.filter(x => S.regen.info.has(x));
        rows.push(h('div.bk-item',
          icon(v.kind === 'dir' ? 'folder' : kindIcon(fileKind(p)), { cls: 'bk-ic' }),
          h('div.bk-item-main', h('div.ellipsis.bk-name', { text: basename(p) || S.session.label, title: p }),
            h('div.ellipsis.faint.bk-path', { text: p })),
          h('span.bk-size.num', s && s.state === 'done' ? bytes(s.bytes) : s ? spinner(12) : h('span.faint', '?')),
          h('button.btn.icon.sm.ghost', { 'aria-label': `Remove ${basename(p)}`, title: 'Remove', onclick: () => { S.selection.delete(p); dropExclusionsUnder(S, p); table.refresh(); refreshRows(); renderBasket(); } }, icon('x'))));
        if (regenSkips.length) rows.push(h('div.bk-skip', icon('minus'), `Skipping ${plural(regenSkips.length, 'rebuildable folder')}`));
        for (const x of skips.filter(x => !S.regen.info.has(x)).slice(0, 8)) {
          rows.push(h('div.bk-skip', icon('minus'), h('span.ellipsis', { title: x, text: `Skipping ${x.slice(p.length) || x}` }),
            h('button.btn.link.sm', { onclick: () => { S.excluded.delete(x); table.refresh(); renderBasket(); } }, 'Include')));
        }
      }
      clear(listBox, rows);
    }
    renderRegen();
    renderDest(t);
    renderStart(t);
  }

  // ---- regenerable folders
  function renderRegen() {
    const R = S.regen;
    const safe = R.found.filter(f => f.confidence === 'safe').length;
    const skipped = R.found.filter(f => S.excluded.has(f.path));
    const skippedBytes = skipped.reduce((t, f) => { const s = S.sizes.get(f.path + '|' + patKey(S)); return t + (s && s.state === 'done' ? s.bytes : 0); }, 0);
    regenDisc.setMeta(skipped.length ? `${skipped.length} skipped${skippedBytes ? `, ${bytes(skippedBytes)}` : ''}` : '');
    const dirsSelected = [...S.selection.entries()].filter(([, v]) => v.kind === 'dir').map(([p]) => p);
    const findBtn = h('button.btn.sm', { disabled: !dirsSelected.length || R.state === 'running', onclick: () => findRegen(dirsSelected) },
      R.state === 'running' ? spinner(13) : icon('search'), R.state === 'running' ? 'Looking…' : R.found.length ? 'Look again' : 'Find in my selection');
    const list = R.found.map(f => {
      const s = S.sizes.get(f.path + '|' + patKey(S));
      return h('label.rg-item',
        checkbox({ checked: S.excluded.has(f.path), ariaLabel: `Skip ${f.path}`, onChange: on => { if (on) S.excluded.add(f.path); else S.excluded.delete(f.path); table.refresh(); renderBasket(); } }),
        h('div.rg-main',
          h('div.rg-top', h('span.ellipsis.rg-name', { text: f.path.split('/').slice(-2).join('/'), title: f.path }), h('span.badge', { class: f.confidence }, f.confidence === 'safe' ? 'Safe to skip' : 'Check first')),
          h('div.rg-what', { text: f.what }),
          h('div.rg-regen', icon('refresh', { size: 12 }), h('span', { text: f.regen }), s && s.state === 'done' ? h('span.faint', { text: bytes(s.bytes) }) : null)));
    });
    clear(regenDisc.body,
      h('p.hint', 'Finds folders like node_modules, build output and caches that each project can recreate with one command, so you can skip them and recover faster. Your code, settings and .git folders are never skipped.'),
      h('div.row.wrap.rg-actions', findBtn,
        R.found.length ? h('button.btn.sm.ghost', { onclick: () => { for (const f of R.found) if (f.confidence === 'safe') S.excluded.add(f.path); table.refresh(); renderBasket(); } }, 'Skip safe ones') : null,
        R.found.length ? h('button.btn.sm.ghost', { onclick: () => { for (const f of R.found) S.excluded.delete(f.path); table.refresh(); renderBasket(); } }, 'Include all') : null),
      R.state === 'done' && !R.found.length ? h('p.hint', 'Nothing rebuildable in your selection.') : null,
      R.state === 'done' && R.found.length ? h('p.hint', `Found ${R.found.length}: ${safe} safe to skip (ticked), ${R.found.length - safe} to check first. Ticked means skipped.`) : null,
      R.error ? h('p.hint.bad-text', { text: R.error }) : null,
      h('div.rg-list', list));
  }
  async function findRegen(paths) {
    const R = S.regen;
    for (const f of R.found) if (!S.excluded.has(f.path)) R.info.delete(f.path);
    R.found = []; R.state = 'running'; R.error = '';
    renderBasket();
    try {
      const st = await api.post('/api/recover/regen', { sid: S.session.sid, paths });
      R.job = st.id;
      emit('jobs-changed');
      watchRegen();
    } catch (e) { R.state = 'idle'; R.error = e.message; renderBasket(); }
  }
  function watchRegen() {
    const R = S.regen;
    regenWatch?.stop();
    let lastFound = -1;
    regenWatch = watchJob(R.job, async st => {
      const n = st.stats?.found ?? 0;
      if (n !== lastFound || !isLive(st)) {
        lastFound = n;
        try {
          const r = await jobResult(R.job);
          const known = new Set(R.found.map(f => f.path));
          for (const f of r.found || []) {
            if (known.has(f.path)) continue;
            R.found.push(f); R.info.set(f.path, f);
            if (f.confidence === 'safe') S.excluded.add(f.path);
          }
        } catch { /* keep polling */ }
      }
      if (!isLive(st)) {
        R.state = st.state === 'error' ? 'idle' : 'done';
        if (st.state === 'error') R.error = st.error;
        if (R.found.length) requestSizes(R.found.map(f => f.path).slice(0, 80));
      }
      table.refresh();
      renderBasket();
    }, { interval: 700 });
  }
  if (S.regen.job && S.regen.state === 'running') watchRegen();

  // ---- destination
  function renderDest(t) {
    const home = store.info.home;
    const vols = (store.drives?.volumes || []).filter(v => v.mount && v.free !== null && v.free !== undefined && !(S.source?.mount && v.mount === S.source.mount));
    const usable = freeInfo.free !== null ? Math.max(0, freeInfo.free - S.reserve * 1e9) : null;
    let fit = null;
    if (S.selection.size && usable !== null) {
      if (t.total > usable) fit = h('div.fit.warn', icon('warning'), h('span', `About ${bytes(usable)} fits while keeping ${S.reserve} GB free. Recovery pauses by itself at that point - free up space or pick a bigger drive, then press Start again to carry on.`));
      else if (t.unknown) fit = h('div.fit', icon('info'), h('span', `${plural(t.unknown, 'folder')} not measured yet.`));
      else fit = h('div.fit.ok', icon('checkCircle'), h('span', `Fits - ${bytes(freeInfo.free - t.total)} will still be free afterwards.`));
    }
    clear(destBox,
      h('button.dest', { type: 'button', title: S.out, onclick: () => chooseOut() },
        h('span.dest-ic', icon('folder')),
        h('span.dest-main', h('span.ellipsis.dest-name', { text: basename(S.out) || S.out }), h('span.ellipsis.faint.dest-path', { text: tildify(S.out, home) })),
        h('span.btn.sm', 'Change…')),
      freeInfo.free !== null ? h('div.hint', `${bytes(freeInfo.free)} free there`) : null,
      fit,
      vols.length ? h('div.vol-chips', vols.slice(0, 4).map(v => h('button.chip', {
        'aria-pressed': String(S.out.startsWith(v.mount) && v.mount !== '/'),
        title: `Save into ${v.mount}`,
        onclick: () => { S.out = joinOut(v.mount, `Recovered from ${S.session.label || 'drive'}`); loadFree(); renderBasket(); },
      }, icon(v.mount === '/' || /^[A-Z]:\\$/.test(v.mount) ? 'drive' : 'driveExt'), v.label || v.mount, h('span.count', bytes(v.free))))) : null);
  }
  async function chooseOut() {
    const p = await pickFolder({ title: 'Where should the recovered files go?', start: S.out, confirmLabel: 'Save here' });
    if (p) { S.out = p; loadFree(); renderBasket(); }
  }

  // ---- options
  function optionsBody() {
    const reserve = h('input.field.sm', { type: 'number', min: '1', step: '1', value: String(S.reserve), 'aria-label': 'Keep free, in gigabytes', oninput: () => { S.reserve = Math.max(0.5, Number(reserve.value) || 15); renderBasket(); } });
    const workers = segmented({ cls: 'sm', value: S.workers, options: [1, 2, 4, 8].map(n => ({ value: n, label: String(n) })), onChange: v => { S.workers = v; } });
    const pats = h('input.field.sm', { type: 'text', value: S.patterns, placeholder: '*.tmp, .DS_Store, Thumbs.db', spellcheck: 'false', 'aria-label': 'Also skip these names',
      onchange: () => { S.patterns = pats.value; table.refresh(); renderBasket(); const dirs = [...S.selection.entries()].filter(([, v]) => v.kind === 'dir').map(([p]) => p); if (dirs.length) requestSizes(dirs); } });
    return h('div.opt-grid',
      h('label.opt', h('span.label', 'Keep free on the destination'), h('div.row', reserve, h('span.faint', 'GB')), h('span.hint', 'Recovery pauses before the drive gets this full.')),
      h('div.opt', h('span.label', 'Files at a time'), workers, h('span.hint', 'Use 1 or 2 if the drive throws read errors.')),
      h('label.opt.wide', h('span.label', 'Also skip these names'), pats, h('span.hint', 'Comma separated. * matches anything.')));
  }

  // ---- start
  function renderStart(t) {
    const running = S.copyJob && S.copyRunning;
    clear(startBox,
      running
        ? h('button.btn.lg.block', { onclick: () => { S.showRun = true; renderAll(); } }, spinner(16), 'Recovery running - view progress')
        : h('button.btn.primary.lg.block', { disabled: !S.selection.size, onclick: () => start() }, icon('recover'), S.selection.size ? `Recover ${t.files ? plural(t.files, 'file') : 'selection'}` : 'Recover'),
      h('p.safe-line.bk-safe', icon('shieldLock'), `${S.session.label || 'The drive'} is only read. Copies go to the folder above.`));
  }
  async function start() {
    const skipped_info = [...S.excluded].filter(x => S.regen.info.has(x)).map(x => S.regen.info.get(x));
    try {
      const st = await api.post('/api/recover/copy', {
        sid: S.session.sid, paths: [...S.selection.keys()], excluded: [...S.excluded], patterns: patterns(S),
        out: S.out, reserve_gb: S.reserve, workers: S.workers, skipped_info,
      });
      S.copyJob = st.id; S.showRun = true;
      emit('jobs-changed');
      renderAll();
    } catch (e) { toastError(e); }
  }

  // check whether a copy is already running (one disk job at a time)
  if (S.copyJob) api.get('/api/job', { id: S.copyJob }).then(st => { S.copyRunning = isLive(st); renderBasket(); }).catch(() => {});

  renderCrumbs();
  if (S.entries.length) refreshRows(); else load(S.cwd || '/', { push: false });
  loadFree();
  renderBasket();
  requestAnimationFrame(() => table.el.focus({ preventScroll: true }));
}

function joinOut(mount, name) {
  const sep = mount.includes('\\') ? '\\' : '/';
  return mount.replace(/[\\/]+$/, '') + sep + name;
}

export { emptyState };
