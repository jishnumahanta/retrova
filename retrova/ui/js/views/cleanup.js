// Cleanup: caches, logs and leftovers you can clear (to the Trash), plus shrinking VM disks.
import { api, watchJob, jobResult, isLive, stat, latestJob } from '../api.js';
import { store, emit } from '../store.js';
import { h, clear, bytes, num, plural, basename, dirname, date, tildify, append } from '../util.js';
import { icon } from '../components/icons.js';
import { segmented, checkbox, emptyState, sectorArt } from '../components/controls.js';
import { jobPanel } from '../components/jobpanel.js';
import { confirmSheet, whatList, summaryRow } from '../components/sheet.js';
import { toast, toastError, attempt } from '../components/toast.js';
import { pickFolder } from '../components/filepicker.js';
import { setToolbar, setToolbarCenter } from '../shell.js';

const S = () => {
  const c = store.cleanup;
  c.tab ||= 'junk';
  c.sel ||= new Set();
  c.open ||= new Set();
  c.sparseSel ||= new Set();
  return c;
};
let ctx = null;

export default {
  async mount(root, params) {
    ctx = { root, stops: [] };
    const st = S();
    if (params.tab === 'sparse') st.tab = 'sparse';
    else if (params.tab === 'junk') st.tab = 'junk';
    if (params.paths) { try { st.sparsePaths = JSON.parse(params.paths); st.sparseJob = null; } catch { /* ignore */ } }
    if (!st.job) { try { const j = await latestJob('clean_scan'); if (j) st.job = j.id; } catch { /* none */ } }
    if (!st.sparseJob && !params.paths) { try { const j = await latestJob('sparse_scan'); if (j) st.sparseJob = j.id; } catch { /* none */ } }
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
  setToolbar({ title: 'Cleanup' });
  setToolbarCenter(segmented({
    value: st.tab, label: 'Cleanup',
    options: [{ value: 'junk', label: 'Caches & leftovers', icon: 'cleanup' }, { value: 'sparse', label: 'Shrink VM disks', icon: 'shrink' }],
    onChange: v => { st.tab = v; history.replaceState(null, '', `#/cleanup?tab=${v}`); render(); },
  }));
  if (st.tab === 'sparse') renderSparse(ctx.root, st);
  else renderJunk(ctx.root, st);
}

async function startScan(st) {
  try {
    const r = await api.post('/api/clean/scan', {});
    st.job = r.id; st.cats = null; st.sel = new Set();
    emit('jobs-changed');
    render();
  } catch (e) { toastError(e); }
}

// ------------------------------------------------------------------ caches & leftovers
function renderJunk(root, st) {
  if (!st.job) {
    root.append(h('div.panel.hero-empty',
      sectorArt(4),
      h('h2', `Find what your ${store.words.computer} can do without`),
      h('p', 'Retrova looks for app caches, logs, old installers and project folders that can be rebuilt, and tells you how safe each one is to remove.'),
      h('button.btn.primary.lg', { onclick: () => startScan(st) }, icon('search'), 'Scan for things to clear'),
      h('p.safe-line', icon('shieldLock'), `Scanning changes nothing. Anything you remove goes to the ${store.words.trash}.`)));
    return;
  }
  const progress = h('div');
  const results = h('div.clean-results');
  root.append(progress, results);
  let panel = null, loaded = false;
  const w = watchJob(st.job, s => {
    if (isLive(s) || s.state === 'error') {
      panel ||= jobPanel({
        describe: x => ({
          headline: x.state === 'error' ? "The scan couldn't finish" : 'Looking for things you can clear',
          sub: x.state === 'error' ? x.error : (x.message || x.phase || ''),
          stats: [{ k: 'Found so far', v: bytes(stat(x, 'bytes') || stat(x, 'bytes_done') || 0) }, { k: 'Categories', v: num(stat(x, 'categories', 'found') || 0) }],
        }),
        actions: x => x.state === 'error' ? [h('button.btn', { onclick: () => startScan(st) }, 'Try again')] : [],
      });
      if (!progress.firstChild) progress.append(h('div.clean-run', panel.el));
      panel.update(s);
      return;
    }
    clear(progress);
    if (!loaded) { loaded = true; loadJunk(results, st, s); emit('jobs-changed'); }
  }, { interval: 700, onError: () => { st.job = null; render(); } });
  ctx.stops.push(() => w.stop());
}

async function loadJunk(box, st, job) {
  if (!st.cats || st.catsFor !== job.id) {
    try {
      const r = await jobResult(job.id);
      st.cats = r.categories || [];
      st.catsFor = job.id;
      st.sel = new Set();
      for (const c of st.cats) if (c.risk === 'safe' && !c.report_only) for (const i of c.items || []) st.sel.add(i.path);
    } catch (e) { clear(box, emptyState({ title: "Couldn't load the results", body: e.message })); return; }
  }
  renderJunkResults(box, st, job);
}

function renderJunkResults(box, st, job) {
  const cats = st.cats;
  const actionable = cats.filter(c => !c.report_only);
  const total = actionable.reduce((t, c) => t + c.size, 0);
  const selBox = h('div.ab-info');
  const goBtn = h('button.btn.primary', { onclick: () => apply() }, icon('trash'), `Move to ${store.words.trash}`);
  const head = h('section.panel.cl-head',
    h('div.cl-total', h('span.sum-k', 'You could clear'), h('span.sum-v.num', { text: bytes(total) }), h('span.sum-meta', `in ${plural(actionable.length, 'category', 'categories')}`)),
    h('span.grow'),
    h('div.row.wrap',
      h('button.btn.sm', { onclick: () => { st.sel = new Set(); for (const c of actionable) if (c.risk === 'safe') for (const i of c.items) st.sel.add(i.path); refresh(); } }, icon('sparkle'), 'Select the safe ones'),
      h('button.btn.sm', { onclick: () => { st.sel = new Set(); refresh(); } }, 'Select none'),
      h('button.btn.sm.ghost', { onclick: () => startScan(st) }, icon('refresh'), 'Scan again')));
  const list = h('section.panel.cl-list');
  const actionbar = h('div.actionbar', selBox, h('span.grow'), goBtn);
  clear(box, head, list, actionbar);
  if (!cats.length) { clear(list, emptyState({ title: 'Nothing to clear', body: 'Your caches and downloads are already tidy.' })); }

  const catEls = new Map();
  function catRow(c) {
    const items = c.items || [];
    const selCount = items.filter(i => st.sel.has(i.path)).length;
    const cb = c.report_only ? h('span.cl-cb-spacer') : checkbox({
      checked: items.length > 0 && selCount === items.length, indeterminate: selCount > 0 && selCount < items.length, ariaLabel: `Select all in ${c.name}`,
      onChange: on => { for (const i of items) on ? st.sel.add(i.path) : st.sel.delete(i.path); refresh(); },
    });
    const open = st.open.has(c.id);
    const head = h('div.cl-cat-head', { class: { open } },
      cb,
      h('button.cl-expand', { type: 'button', 'aria-expanded': String(open), disabled: !items.length, onclick: () => { open ? st.open.delete(c.id) : st.open.add(c.id); rerenderCat(c); } },
        icon('chevronRight', { cls: 'disc-chev' }),
        h('span.cl-cat-main',
          h('span.cl-cat-name', c.name, c.report_only ? h('span.badge', 'Report only') : h('span.badge', { class: c.risk === 'safe' ? 'safe' : 'review' }, c.risk === 'safe' ? 'Safe to clear' : 'Check first')),
          h('span.cl-cat-detail', { text: c.detail })),
        h('span.cl-cat-size', h('b.num', { text: bytes(c.size) }), items.length ? h('span.faint', plural(c.count ?? items.length, 'item')) : null)));
    const body = open ? h('div.cl-items', items.map(i => h('label.cl-item',
      checkbox({ checked: st.sel.has(i.path), ariaLabel: `Select ${i.path}`, onChange: on => { on ? st.sel.add(i.path) : st.sel.delete(i.path); refresh(); } }),
      h('div.pathcell', { title: i.path }, h('span.n', { text: basename(i.path) }), h('span.p', h('bdi', { text: tildify(dirname(i.path), store.info.home) }))),
      i.note ? h('span.cl-note', { text: i.note }) : h('span'),
      h('span.faint.num.cl-date', { text: i.mtime ? date(i.mtime) : '' }),
      h('span.num.cl-size', { text: bytes(i.size) }),
      h('button.btn.icon.sm.ghost', { title: store.words.showIn, 'aria-label': store.words.showIn, onclick: e => { e.preventDefault(); attempt(() => api.post('/api/reveal', { path: i.path })); } }, icon('reveal'))))) : null;
    return h('div.cl-cat', { class: { report: c.report_only } }, head, body);
  }
  function rerenderCat(c) { const el = catRow(c); catEls.get(c.id).replaceWith(el); catEls.set(c.id, el); }
  function refresh() {
    clear(list, cats.map(c => { const el = catRow(c); catEls.set(c.id, el); return el; }));
    const items = selectedItems();
    const b = items.reduce((t, i) => t + i.size, 0);
    clear(selBox, items.length ? [h('span', 'Move '), h('b.num', { text: plural(items.length, 'item') }), h('span', ' to the ' + store.words.trash + ' to free '), h('b.num', { text: bytes(b) })] : h('span.faint', 'Tick what you want to remove.'));
    goBtn.disabled = !items.length;
  }
  const selectedItems = () => cats.filter(c => !c.report_only).flatMap(c => (c.items || []).filter(i => st.sel.has(i.path)).map(i => ({ ...i, cat: c.name, risk: i.risk || c.risk })));
  refresh();

  async function apply() {
    const items = selectedItems();
    const b = items.reduce((t, i) => t + i.size, 0);
    const review = items.filter(i => i.risk === 'review').length;
    const ok = await confirmSheet({
      icon: 'trash',
      title: `Move ${plural(items.length, 'item')} to the ${store.words.trash}?`,
      message: `These go to the ${store.words.trash}, so you can put any of them back. The space comes back when you empty it.`,
      body: append(h('div.stack'), [
        summaryRow([[plural(items.length, 'item'), 'to move'], [bytes(b), `freed after you empty the ${store.words.trash}`]]),
        review ? h('div.callout.warn', icon('warning'), h('div.grow', h('b', `${plural(review, 'item')} marked "Check first"`), h('span.muted', 'Make sure you don’t need them, for example an installer you might use again.'))) : null,
        whatList(items.map(i => ({ path: i.path, size: i.size, label: tildify(i.path, store.info.home), note: i.cat })))]),
      confirmLabel: `Move to ${store.words.trash}`,
      footNote: `You can put them back from the ${store.words.trash}`,
      size: 'wide',
    });
    if (!ok) return;
    try {
      const j = await api.post('/api/clean/apply', { paths: items.map(i => i.path) });
      emit('jobs-changed');
      goBtn.disabled = true;
      const w = watchJob(j.id, async x => {
        if (isLive(x)) { clear(selBox, h('span.faint', `Moving… ${num(stat(x, 'files_done') || 0)} of ${num(items.length)}`)); return; }
        let moved = items.map(i => i.path);
        try { const r = await jobResult(j.id); if (Array.isArray(r.moved)) moved = r.moved.map(m => typeof m === 'string' ? m : m.path); } catch { /* assume all */ }
        const gone = new Set(moved);
        for (const c of cats) { c.items = (c.items || []).filter(i => !gone.has(i.path)); if (!c.report_only) { c.size = c.items.reduce((t, i) => t + i.size, 0); c.count = c.items.length; } }
        for (const p of gone) st.sel.delete(p);
        toast(x.message || `Moved ${plural(gone.size, 'item')} to the ${store.words.trash}. You can put them back from there.`, { kind: x.state === 'done' ? 'ok' : 'warn', timeout: 7000 });
        emit('jobs-changed');
        renderJunkResults(box, st, job);
      });
      ctx?.stops.push(() => w.stop());
    } catch (e) { toastError(e); }
  }
}

// ------------------------------------------------------------------ shrink VM disks
function renderSparse(root, st) {
  if (!st.sparsePaths?.length) st.sparsePaths = [store.info.home];
  const chips = h('div.folder-chips');
  const results = h('div.sp-results');
  const scanBtn = h('button.btn.primary', { onclick: () => scan() }, icon('search'), 'Look for VM disks');
  const renderChips = () => clear(chips, st.sparsePaths.map(p => h('span.folder-chip', { title: p }, icon('folder'), h('span.ellipsis', { text: tildify(p, store.info.home) }),
    h('button.btn.icon.sm.ghost', { 'aria-label': `Remove ${p}`, onclick: () => { st.sparsePaths = st.sparsePaths.filter(x => x !== p); renderChips(); } }, icon('x')))),
  h('button.btn.sm', { onclick: async () => { const p = await pickFolder({ title: 'Add a folder or drive', start: store.info.home, confirmLabel: 'Add' }); if (p && !st.sparsePaths.includes(p)) { st.sparsePaths.push(p); renderChips(); } } }, icon('plus'), 'Add folder…'));
  renderChips();
  root.append(
    h('header.page-head',
      h('h2.page-title', 'Shrink virtual machine disks'),
      h('p.page-sub', 'Disk files for virtual machines and Docker often hold large empty areas that still take up room. Retrova rewrites them so the empty areas take no space.'),
      h('p.safe-line', icon('shieldLock'), 'What is inside stays exactly the same. Each file is checked before the original is replaced.')),
    h('section.panel.setup-card.sp-setup',
      h('div.setup-row', h('span.label', 'Look in'), chips),
      h('div.callout.warn', icon('warning'), h('div.grow', h('b', 'Close your virtual machines and Docker first'), h('span.muted', 'Files that are in use are skipped.'))),
      h('div.row', scanBtn)),
    results);

  async function scan() {
    try {
      const r = await api.post('/api/sparse/scan', { paths: st.sparsePaths });
      st.sparseJob = r.id; st.sparseSel = new Set(); st.sparseFiles = null;
      emit('jobs-changed');
      watch();
    } catch (e) { toastError(e); }
  }
  function watch() {
    if (!st.sparseJob) return;
    let panel = null;
    const w = watchJob(st.sparseJob, async s => {
      if (isLive(s)) {
        panel ||= jobPanel({ compact: true, ringSize: 96, describe: x => ({ headline: 'Reading disk files', sub: x.message || x.phase, stats: [{ k: 'Found', v: num(stat(x, 'found') || 0) }] }) });
        if (!results.contains(panel.el)) clear(results, panel.el);
        panel.update(s);
        return;
      }
      if (s.state === 'error') { clear(results, h('div.callout.bad', icon('error'), h('div.grow', h('b', "The scan couldn't finish"), h('span.muted', s.error)))); return; }
      try { const r = await jobResult(st.sparseJob); st.sparseFiles = r.files || []; } catch { st.sparseFiles = []; }
      if (!st.sparseSel.size) for (const f of st.sparseFiles) if (f.reclaimable > 64e6) st.sparseSel.add(f.path);
      showFiles();
      emit('jobs-changed');
    }, { interval: 700, onError: () => { st.sparseJob = null; } });
    ctx.stops.push(() => w.stop());
  }
  function showFiles() {
    const files = st.sparseFiles || [];
    if (!files.length) { clear(results, h('div.panel', emptyState({ title: 'No disks to shrink here', body: 'None of the big files in these folders have empty space to give back.' }))); return; }
    const goBtn = h('button.btn.primary', { onclick: () => apply() }, icon('shrink'), 'Shrink selected');
    const sum = h('span.sp-sum');
    const refresh = () => {
      const sel = files.filter(f => st.sparseSel.has(f.path));
      clear(sum, sel.length ? [h('b.num', { text: bytes(sel.reduce((t, f) => t + f.reclaimable, 0)) }), ` can be freed from ${plural(sel.length, 'file')}`] : h('span.faint', 'Nothing selected'));
      goBtn.disabled = !sel.length;
    };
    clear(results, h('section.panel.sp-table',
      h('div.thead.sp-cols', h('span'), h('span', 'Disk file'), h('span.r', 'Size'), h('span.r', 'Uses now'), h('span.r', 'Can free')),
      files.map(f => h('label.trow.sp-cols', checkbox({ checked: st.sparseSel.has(f.path), ariaLabel: `Shrink ${f.path}`, onChange: on => { on ? st.sparseSel.add(f.path) : st.sparseSel.delete(f.path); refresh(); } }),
        h('div.pathcell', { title: f.path }, h('span.n', { text: basename(f.path) }), h('span.p', h('bdi', { text: tildify(dirname(f.path), store.info.home) }))),
        h('span.r.cell-num', { text: bytes(f.size) }), h('span.r.cell-num', { text: bytes(f.allocated) }), h('span.r.cell-num.ok-text', { text: bytes(f.reclaimable) }))),
      h('div.sp-foot', sum, h('span.grow'), goBtn)));
    refresh();

    async function apply() {
      const sel = files.filter(f => st.sparseSel.has(f.path));
      const ok = await confirmSheet({
        icon: 'shrink',
        title: `Shrink ${plural(sel.length, 'disk file')}?`,
        message: 'Retrova writes a sparse copy of each file, checks that it matches the original exactly, and only then swaps it in. Dates and permissions are kept.',
        body: append(h('div.stack'), [summaryRow([[bytes(sel.reduce((t, f) => t + f.reclaimable, 0)), 'freed'], [plural(sel.length, 'file'), 'rewritten']]),
          whatList(sel.map(f => ({ path: f.path, size: f.reclaimable, label: basename(f.path), note: 'frees' }))),
          h('p.hint', 'Needs some free space while it works. Files that are open in another app are skipped.')]),
        confirmLabel: 'Shrink',
        footNote: 'Contents are verified before replacing',
      });
      if (!ok) return;
      try {
        const j = await api.post('/api/sparse/apply', { files: sel.map(f => ({ path: f.path, reclaimable: f.reclaimable })) });
        emit('jobs-changed');
        goBtn.disabled = true;
        const w = watchJob(j.id, x => {
          if (isLive(x)) { clear(sum, h('span.faint', `Shrinking ${num((stat(x, 'files_done') || 0) + 1)} of ${num(sel.length)}…`)); return; }
          toast(x.message || (x.state === 'done' ? `Shrunk ${plural(sel.length, 'file')}.` : x.error || 'Stopped.'), { kind: x.state === 'done' ? 'ok' : 'warn', timeout: 7000 });
          emit('jobs-changed');
          scan();
        });
        ctx?.stops.push(() => w.stop());
      } catch (e) { toastError(e); }
    }
  }
  if (st.sparseFiles && st.sparseJob) showFiles(); else if (st.sparseJob) watch();
}
