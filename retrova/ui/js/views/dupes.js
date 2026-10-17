// Duplicates: find identical files, choose which copy to keep, move the rest to the Trash (or clone them on a Mac).
import { api, watchJob, jobResult, isLive, stat, thumbUrl, latestJob } from '../api.js';
import { store, emit } from '../store.js';
import { h, clear, bytes, num, plural, basename, dirname, date, fileKind, tildify, append } from '../util.js';
import { icon, kindIcon, KIND_TONE } from '../components/icons.js';
import { segmented, checkbox, emptyState } from '../components/controls.js';
import { jobPanel } from '../components/jobpanel.js';
import { confirmSheet, whatList, summaryRow } from '../components/sheet.js';
import { toast, toastError, attempt } from '../components/toast.js';
import { pickFolder } from '../components/filepicker.js';
import { setToolbar } from '../shell.js';

const SIZES = [{ value: 1, label: 'Any size' }, { value: 100e3, label: '100 KB' }, { value: 1e6, label: '1 MB' }, { value: 10e6, label: '10 MB' }, { value: 100e6, label: '100 MB' }];
const PAGE = 60;
const S = () => {
  const d = store.dupes;
  d.paths ||= [];
  d.minSize ||= 1e6;
  d.keep ||= new Map();
  return d;
};
let ctx = null;

export default {
  async mount(root, params) {
    ctx = { root, stops: [] };
    const st = S();
    if (params.paths) {
      try { st.paths = JSON.parse(params.paths).filter(Boolean); } catch { /* ignore */ }
      st.job = null; st.setup = true;
    } else if (!st.job && !st.setup) {
      try { const j = await latestJob('dupes'); if (j) { st.job = j.id; st.paths = j.params?.paths || st.paths; } } catch { /* none */ }
    }
    if (!st.paths.length) st.paths = [store.info.home];
    render();
    return () => { ctx.stops.forEach(f => f()); ctx = null; };
  },
};

function render() {
  if (!ctx) return;
  ctx.stops.forEach(f => f());
  ctx.stops = [];
  clear(ctx.root);
  const st = S();
  if (st.job) renderJob(ctx.root, st);
  else renderSetup(ctx.root, st);
}

// ------------------------------------------------------------------ setup
function renderSetup(root, st) {
  setToolbar({ title: 'Duplicates', actions: st.lastJob ? [h('button.btn.sm.ghost', { onclick: () => { st.job = st.lastJob; st.setup = false; render(); } }, 'Back to results')] : [] });
  const folders = h('div.folder-chips');
  const startBtn = h('button.btn.primary.lg', { onclick: () => start() }, icon('search'), 'Find duplicates');
  const renderFolders = () => {
    clear(folders, st.paths.map(p => h('span.folder-chip', { title: p }, icon('folder'), h('span.ellipsis', { text: tildify(p, store.info.home) }),
      h('button.btn.icon.sm.ghost', { 'aria-label': `Remove ${p}`, onclick: () => { st.paths = st.paths.filter(x => x !== p); renderFolders(); } }, icon('x')))),
    h('button.btn.sm', { onclick: async () => { const p = await pickFolder({ title: 'Add a folder to search', start: store.info.home, confirmLabel: 'Add' }); if (p && !st.paths.includes(p)) { st.paths.push(p); renderFolders(); } } }, icon('plus'), 'Add folder…'));
    startBtn.disabled = !st.paths.length;
  };
  renderFolders();
  root.append(
    h('header.page-head',
      h('h2.page-title', 'Find duplicate files'),
      h('p.page-sub', 'Retrova finds files with exactly the same contents, even when their names are different.'),
      h('p.safe-line', icon('shieldLock'), `Nothing is removed until you confirm. Extras go to the ${store.words.trash}, so you can put them back.`)),
    h('section.panel.setup-card.dupes-setup',
      h('div.setup-rows',
        h('div.setup-row', h('span.label', 'Folders to search'), folders),
        h('div.setup-row', h('span.label', 'Ignore files smaller than'), segmented({ value: st.minSize, options: SIZES, onChange: v => { st.minSize = v; } })),
        checkbox({ checked: !!st.hidden, label: 'Include hidden files', onChange: v => { st.hidden = v; } })),
      h('div.setup-foot', startBtn, h('p.hint', 'Files are compared by size first, then by their contents. Only files that match byte for byte count as duplicates.'))));

  async function start() {
    try {
      const r = await api.post('/api/dupes', { paths: st.paths, min_size: st.minSize, include_hidden: !!st.hidden });
      st.job = r.id; st.setup = false; st.groups = null; st.keep = new Map(); st.shown = PAGE;
      emit('jobs-changed');
      render();
    } catch (e) { toastError(e); }
  }
}

// ------------------------------------------------------------------ scan + results
function renderJob(root, st) {
  setToolbar({ title: 'Duplicates', actions: [h('button.btn.sm', { onclick: () => { st.lastJob = st.job; st.job = null; st.setup = true; render(); } }, icon('plus'), 'New search')] });
  const progress = h('div');
  const results = h('div.dupes-results');
  root.append(progress, results);
  let panel = null, loaded = false;
  const w = watchJob(st.job, s => {
    if (isLive(s) || s.state === 'error' || s.state === 'stopped') {
      if (!panel) {
        panel = jobPanel({
          describe: x => ({
            headline: x.state === 'error' ? "The search couldn't finish" : x.state === 'stopped' ? 'Search stopped' : x.state === 'paused' ? 'Search paused' : 'Looking for duplicates',
            sub: x.state === 'error' ? x.error : x.state === 'stopped' ? 'Duplicates found so far are listed below.' : (x.phase || ''),
            stats: [
              { k: 'Files looked at', v: num(stat(x, 'scanned') || 0) },
              stat(x, 'files_total') ? { k: 'Being compared', v: `${num(stat(x, 'files_done'))} of ${num(stat(x, 'files_total'))}` } : null,
              { k: 'Sets of duplicates', v: num(stat(x, 'found') || 0), tone: stat(x, 'found') ? 'ok' : '' },
              stat(x, 'reclaimable') ? { k: 'Could be freed', v: bytes(stat(x, 'reclaimable')) } : null,
            ],
          }),
        });
        clear(progress, h('div.dupes-run', panel.el));
      }
      panel.update(s);
    } else clear(progress);
    if (!isLive(s) && !loaded) { loaded = true; loadResults(results, st, s); emit('jobs-changed'); }
  }, { interval: 700, onError: () => { st.job = null; render(); } });
  ctx.stops.push(() => w.stop());
}

async function loadResults(box, st, job) {
  if (!st.groups || st.groupsFor !== job.id) {
    try {
      const r = await jobResult(job.id, { limit: 5000 });
      st.groups = (r.groups || []).map(g => ({ ...g, id: g.id || g.hash }));
      st.groupsFor = job.id;
      st.scanned = r.scanned;
      st.keep = new Map();
      for (const g of st.groups) for (const f of g.files) st.keep.set(f.path, !!f.keep);
      for (const g of st.groups) if (!g.files.some(f => st.keep.get(f.path))) st.keep.set(g.files[0].path, true);
      st.shown = PAGE;
    } catch (e) { clear(box, emptyState({ title: "Couldn't load the results", body: e.message })); return; }
  }
  renderResults(box, st);
}

function selection(st) {
  const remove = [], keepMap = {};
  let bytesFreed = 0;
  for (const g of st.groups) {
    const kept = g.files.filter(f => st.keep.get(f.path));
    if (!kept.length) continue;
    for (const f of g.files) if (!st.keep.get(f.path)) { remove.push({ path: f.path, size: g.size, keptAs: kept[0].path }); keepMap[f.path] = kept[0].path; bytesFreed += g.size; }
  }
  return { remove, keepMap, bytesFreed };
}

function renderResults(box, st) {
  if (!box.isConnected) return;
  const groups = st.groups;
  if (!groups.length) {
    clear(box, h('div.panel', emptyState({ title: 'No duplicates found', body: `Every file in ${plural(st.paths.length, 'folder')} is one of a kind${st.scanned ? ` (${plural(st.scanned, 'file')} checked)` : ''}.`, actions: [h('button.btn', { onclick: () => { st.lastJob = st.job; st.job = null; st.setup = true; render(); } }, 'Search other folders')] })));
    return;
  }
  const total = groups.reduce((t, g) => t + (g.reclaimable ?? g.size * (g.files.length - 1)), 0);
  const selInfo = h('div.ab-info');
  const trashBtn = h('button.btn.primary', { onclick: () => apply('trash') }, icon('trash'), `Move extras to ${store.words.trash}`);
  const cloneBtn = store.info.platform === 'mac' ? h('button.btn', { title: 'Keep every name, but store the contents once (APFS drives)', onclick: () => apply('clone') }, icon('clone'), 'Replace with clones') : null;
  const bar = h('div.dbar',
    h('div.dbar-total', h('b.num', { text: bytes(total) }), h('span', `could be freed in ${plural(groups.length, 'set')} of duplicates`)),
    h('span.grow'),
    h('button.btn.sm', { title: 'Keep the copy in the most sensible place: shortest path, oldest, not in Downloads or caches', onclick: () => { for (const g of groups) g.files.forEach((f, i) => st.keep.set(f.path, f.keep ?? i === 0)); for (const g of groups) if (!g.files.some(f => st.keep.get(f.path))) st.keep.set(g.files[0].path, true); refresh(); } }, icon('sparkle'), 'Smart select'),
    h('button.btn.sm', { onclick: () => { for (const g of groups) for (const f of g.files) st.keep.set(f.path, true); refresh(); } }, 'Keep every copy'));
  const actionbar = h('div.actionbar', selInfo, h('span.grow'), cloneBtn, trashBtn);
  const list = h('div.dgroups');
  const more = h('div.dmore');
  clear(box, bar, list, more, actionbar);

  function refresh() {
    const s = selection(st);
    clear(selInfo, s.remove.length ? [h('span', 'Remove '), h('b.num', { text: plural(s.remove.length, 'extra copy', 'extra copies') }), h('span', ' to free '), h('b.num', { text: bytes(s.bytesFreed) })] : h('span.faint', 'Untick a copy to remove it. At least one copy of each file is always kept.'));
    trashBtn.disabled = !s.remove.length;
    if (cloneBtn) cloneBtn.disabled = !s.remove.length;
    for (const el of list.querySelectorAll('[data-path]')) {
      const kept = st.keep.get(el.dataset.path);
      el.classList.toggle('removing', !kept);
      const cb = el.querySelector('input'); if (cb) cb.checked = kept;
      const tag = el.querySelector('.d-tag');
      if (tag) { tag.textContent = kept ? 'Keep' : 'Remove'; tag.className = `d-tag badge ${kept ? 'ok' : 'bad'}`; }
    }
  }

  function groupCard(g) {
    const first = g.files[0].path;
    const kind = fileKind(first);
    const thumb = kind === 'image'
      ? h('img', { src: thumbUrl(first, 128), alt: '', loading: 'lazy', onerror: e => e.target.replaceWith(icon('image')) })
      : h('span', { style: { color: KIND_TONE[kind] || '' } }, icon(kindIcon(kind)));
    return h('article.dgroup',
      h('header.dg-head', h('span.dg-thumb', thumb),
        h('div.dg-title', h('span.ellipsis', { text: basename(first) }), h('span.faint', `${plural(g.files.length, 'copy', 'copies')}, ${bytes(g.size)} each`)),
        h('span.dg-free.num', { text: `${bytes(g.reclaimable ?? g.size * (g.files.length - 1))}` })),
      g.files.map(f => h('label.dg-file', { dataset: { path: f.path } },
        checkbox({ checked: st.keep.get(f.path), ariaLabel: `Keep ${f.path}`, onChange: on => {
          if (!on && g.files.filter(x => x.path !== f.path && st.keep.get(x.path)).length === 0) { toast('Keep at least one copy of each file.', { kind: 'warn' }); refresh(); return; }
          st.keep.set(f.path, on); refresh();
        } }),
        h('span.d-tag.badge.ok', 'Keep'),
        h('div.pathcell', h('span.n', { text: tildify(dirname(f.path), store.info.home) }), h('span.p', h('bdi', { text: basename(f.path) }))),
        f.keep ? h('span.badge', { title: 'Retrova suggests keeping this one' }, icon('sparkle'), 'Suggested') : h('span'),
        h('span.faint.num.dg-date', { text: date(f.mtime) }),
        h('button.btn.icon.sm.ghost', { title: store.words.showIn, 'aria-label': store.words.showIn, onclick: e => { e.preventDefault(); attempt(() => api.post('/api/reveal', { path: f.path })); } }, icon('reveal')))));
  }
  function renderList() {
    clear(list, groups.slice(0, st.shown).map(groupCard));
    clear(more, st.shown < groups.length ? h('button.btn', { onclick: () => { st.shown += PAGE * 2; renderList(); refresh(); } }, `Show more (${num(groups.length - st.shown)} left)`) : null);
  }
  renderList();
  refresh();

  async function apply(action) {
    const s = selection(st);
    if (!s.remove.length) return;
    const home = store.info.home;
    const clone = action === 'clone';
    const ok = await confirmSheet({
      icon: clone ? 'clone' : 'trash',
      title: clone ? `Replace ${plural(s.remove.length, 'duplicate')} with clones?` : `Move ${plural(s.remove.length, 'duplicate')} to the ${store.words.trash}?`,
      message: clone
        ? 'Each file below becomes a clone of the copy you keep. Both names stay where they are, but the contents are stored once.'
        : `Each file below goes to the ${store.words.trash}. The copy you keep stays where it is.`,
      body: append(h('div.stack'), [
        summaryRow([[plural(s.remove.length, 'file'), clone ? 'to clone' : 'to move'], [bytes(s.bytesFreed), clone ? 'freed' : `freed after you empty the ${store.words.trash}`]]),
        whatList(s.remove.map(r => ({ path: r.path, size: r.size, label: tildify(r.path, home), note: `same as ${basename(dirname(r.keptAs))}/${basename(r.keptAs)}` })), { home }),
        h('p.hint', 'Right before each one moves, Retrova checks again that it matches the kept copy byte for byte. Anything that changed is left alone.')]),
      confirmLabel: clone ? 'Replace with clones' : `Move to ${store.words.trash}`,
      footNote: clone ? 'Works on APFS drives; others are skipped' : `You can put them back from the ${store.words.trash}`,
      size: 'wide',
    });
    if (!ok) return;
    try {
      const j = await api.post('/api/dupes/apply', { action, remove: s.remove.map(r => r.path), keep_map: s.keepMap });
      emit('jobs-changed');
      trashBtn.disabled = true;
      const w = watchJob(j.id, async x => {
        if (isLive(x)) { clear(selInfo, h('span.faint', `${clone ? 'Cloning' : 'Moving'}… ${num(stat(x, 'files_done') || 0)} of ${num(stat(x, 'files_total') || s.remove.length)}`)); return; }
        let done = s.remove.map(r => r.path);
        try { const r = await jobResult(j.id); if (Array.isArray(r.done)) done = r.done.map(d => typeof d === 'string' ? d : d.path); } catch { /* assume all */ }
        const gone = new Set(done);
        st.groups = st.groups.map(g => ({ ...g, files: g.files.filter(f => !gone.has(f.path)) })).filter(g => g.files.length > 1);
        toast(x.message || (clone ? `Replaced ${plural(gone.size, 'file')} with clones.` : `Moved ${plural(gone.size, 'file')} to the ${store.words.trash}. You can put them back from there.`), { kind: x.state === 'done' ? 'ok' : 'warn' });
        emit('jobs-changed');
        renderResults(box, st);
      });
      ctx?.stops.push(() => w.stop());
    } catch (e) { toastError(e); }
  }
}
