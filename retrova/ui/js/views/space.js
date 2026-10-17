// Space: analyse a folder or drive, show where the space goes, and point at what can be cleared.
import { api, watchJob, jobResult, isLive, stat, latestJob } from '../api.js';
import { store, emit } from '../store.js';
import { h, clear, bytes, num, plural, basename, dirname, pathCrumbs, tildify, fileKind, date } from '../util.js';
import { icon, kindIcon, KIND_TONE } from '../components/icons.js';
import { capBar, segmented, emptyState } from '../components/controls.js';
import { jobPanel } from '../components/jobpanel.js';
import { treemap } from '../components/treemap.js';
import { toastError, attempt } from '../components/toast.js';
import { pickFolder } from '../components/filepicker.js';
import { setToolbar } from '../shell.js';
import { go } from '../router.js';

const TYPE_COLORS = ['#2BD9A0', '#5C9CE6', '#9C8CF0', '#E58FB0', '#E8A456', '#6FC3C9', '#C9B458', '#8A9690', '#B98AE0', '#7FA87A'];
const FINDING = {
  dupes: { icon: 'dupes', label: 'Review duplicates' },
  sparse: { icon: 'shrink', label: 'Shrink them' },
  clean: { icon: 'cleanup', label: 'Review in Cleanup' },
  reveal: { icon: 'reveal', label: null },
  none: { icon: 'info', label: null },
};

const S = () => store.space;
let ctx = null;

export default {
  async mount(root, params) {
    ctx = { root, stops: [], params };
    const st = S();
    if (params.path) { await startAnalysis(params.path); return () => cleanup(); }
    if (params.flow === 'full') { st.job = null; st.choose = true; }
    else if (!st.job && !st.choose) {
      try { const j = await latestJob('analyse'); if (j) st.job = j.id; } catch { /* none yet */ }
    }
    render();
    return () => cleanup();
  },
};
function cleanup() { ctx?.stops.forEach(f => f()); ctx = null; }

function render() {
  if (!ctx) return;
  ctx.stops.forEach(f => f());
  ctx.stops = [];
  clear(ctx.root);
  const st = S();
  if (st.job && !st.choose) renderJob(ctx.root, st);
  else renderChooser(ctx.root, st);
}

async function startAnalysis(path) {
  const st = S();
  try {
    const r = await api.post('/api/analyse', { path });
    st.job = r.id; st.node = null; st.trail = []; st.choose = false; st.tab = 'folders';
    emit('jobs-changed');
    history.replaceState(null, '', '#/space');
  } catch (e) { toastError(e); st.choose = true; }
  render();
}

// ------------------------------------------------------------------ choose what to analyse
async function renderChooser(root, st) {
  setToolbar({ title: 'Space', actions: st.job ? [h('button.btn.sm.ghost', { onclick: () => { st.choose = false; render(); } }, 'Back to results')] : [] });
  const grid = h('div.target-grid');
  const flow = ctx.params.flow === 'full';
  root.append(
    h('header.page-head',
      h('h2.page-title', flow ? "Let's find what's taking up space" : 'What should Retrova look at?'),
      h('p.page-sub', 'It measures every folder, then points out what you can safely clear.'),
      h('p.safe-line', icon('shieldLock'), 'Looking changes nothing. You decide what to remove, and it goes to the ' + store.words.trash + '.')),
    grid);
  const info = store.info;
  const target = (ic, title, sub, path, extra) => h('button.target', { type: 'button', onclick: () => startAnalysis(path) },
    h('span.target-ic', icon(ic)), h('span.target-main', h('span.target-title', { text: title }), h('span.target-sub', { text: sub }), extra || null), icon('chevronRight', { cls: 'src-chev' }));
  clear(grid, target('home', 'Your home folder', tildify(info.home, info.home) === '~' ? info.home : info.home, info.home), h('div.skel', { style: 'height:96px;border-radius:14px' }));
  let vols = [];
  try { const d = store.drives || await api.get('/api/drives'); store.drives = d; vols = d.volumes || []; } catch { /* only home */ }
  if (!root.isConnected) return;
  clear(grid,
    target('home', 'Your home folder', info.home, info.home),
    vols.filter(v => v.mount).map(v => {
      const used = v.size ? (v.size - (v.free || 0)) / v.size : null;
      return target(v.mount === '/' || /^[A-Z]:\\$/.test(v.mount) ? 'drive' : 'driveExt', v.label || v.mount, `${bytes(v.free)} free of ${bytes(v.size)}`, v.mount,
        capBar(used, used > 0.92 ? 'bad' : used > 0.85 ? 'warn' : ''));
    }),
    h('button.target.dashed', { type: 'button', onclick: async () => { const p = await pickFolder({ title: 'Choose a folder to analyse', start: info.home, confirmLabel: 'Analyse' }); if (p) startAnalysis(p); } },
      h('span.target-ic', icon('folder')), h('span.target-main', h('span.target-title', 'Choose a folder…'), h('span.target-sub', 'Any folder or drive you can open.')), icon('chevronRight', { cls: 'src-chev' })));
}

// ------------------------------------------------------------------ running / results
function renderJob(root, st) {
  const progressBox = h('div');
  const resultsBox = h('div.space-results');
  root.append(progressBox, resultsBox);
  let panel = null;
  let shown = false;
  const w = watchJob(st.job, s => {
    const rootPath = s.params?.path || '';
    setToolbar({
      title: 'Space', sub: rootPath === store.info.home ? 'Your home folder' : rootPath,
      actions: isLive(s) ? [] : [
        h('button.btn.sm', { onclick: () => startAnalysis(rootPath) }, icon('refresh'), 'Analyse again'),
        h('button.btn.sm', { onclick: () => { st.choose = true; render(); } }, icon('plus'), 'Something else'),
      ],
    });
    if (isLive(s) || s.state === 'error') {
      if (!panel) {
        panel = jobPanel({
          describe: x => ({
            headline: x.state === 'error' ? "The analysis couldn't finish" : x.state === 'paused' ? 'Analysis paused' : `Measuring ${basename(rootPath) || rootPath}`,
            sub: x.state === 'error' ? x.error : [x.phase || 'Measuring folders', h('br'), h('span.safe-line', icon('shieldLock'), 'Nothing is changed while Retrova looks.')],
            stats: [
              { k: 'Files', v: num(stat(x, 'files_done', 'files') || 0) },
              { k: 'Folders', v: num(stat(x, 'dirs') || 0) },
              { k: 'Size so far', v: bytes(stat(x, 'bytes_done', 'bytes') || 0) },
            ],
          }),
          actions: x => x.state === 'error' ? [h('button.btn', { onclick: () => { st.choose = true; render(); } }, 'Choose something else')] : [],
        });
        clear(progressBox, h('div.space-run', panel.el));
      }
      panel.update(s);
      return;
    }
    clear(progressBox);
    if (!shown) { shown = true; renderResults(resultsBox, st, s); }
    emit('jobs-changed');
  }, { interval: 700, onError: () => { st.job = null; st.choose = true; render(); } });
  ctx.stops.push(() => w.stop());
}

async function renderResults(box, st, job) {
  let sum;
  try { sum = await jobResult(job.id, { view: 'summary' }); } catch (e) { clear(box, emptyState({ title: "Couldn't load the results", body: e.message })); return; }
  if (!box.isConnected) return;
  const rootPath = sum.root || job.params?.path;
  st.root = rootPath;
  if (!st.node) st.node = rootPath;

  // summary + types
  const types = (sum.types || []).filter(t => t.bytes > 0).slice(0, 9);
  const typeTotal = types.reduce((t, x) => t + x.bytes, 0) || 1;
  const summary = h('section.panel.space-sum',
    h('div.sum-total', h('span.sum-k', 'Space used'), h('span.sum-v.num', { text: bytes(sum.total) }),
      h('span.sum-meta', `${plural(sum.files, 'file')} in ${plural(sum.dirs, 'folder')}`)),
    h('div.sum-types',
      h('div.typebar', types.map((t, i) => h('i', { style: { width: `${(t.bytes / typeTotal) * 100}%`, background: TYPE_COLORS[i % TYPE_COLORS.length] }, title: `${t.type}: ${bytes(t.bytes)}` }))),
      h('div.typelegend', types.map((t, i) => h('span.tl', h('i', { style: { background: TYPE_COLORS[i % TYPE_COLORS.length] } }), h('span', { text: t.type }), h('b.num', { text: bytes(t.bytes) }))))));

  // findings
  const findings = (sum.findings || []).filter(f => f.bytes > 0 || f.action === 'none').sort((a, b) => (b.bytes || 0) - (a.bytes || 0));
  const findingsBox = findings.length ? h('section.section.findings-sec',
    h('h2.section-title', 'What you could clear', h('span.aside.faint', { text: `about ${bytes(findings.reduce((t, f) => t + (f.bytes || 0), 0))} in total` })),
    h('div.findings', findings.map(f => findingCard(f)))) : null;

  // explorer
  const crumbs = h('div.crumbs');
  const tm = treemap({ onOpen: it => openNode(it.path), onHover: it => highlightRow(it?.path) });
  const listEl = h('div.space-list');
  const tabs = segmented({ cls: 'sm', value: st.tab || 'folders', options: [{ value: 'folders', label: 'Folders' }, { value: 'largest', label: 'Largest files' }], onChange: v => { st.tab = v; showTab(); } });
  const upBtn = h('button.btn.icon.sm.ghost', { 'aria-label': 'Up one folder', title: 'Up one folder', onclick: () => { if (st.trail?.length) openNode(st.trail.pop(), false); } }, icon('arrowUp'));
  const explorer = h('section.section',
    h('div.explorer-head', h('h2.section-title', 'Where the space goes'), h('span.grow'), tabs),
    h('div.panel.explorer',
      h('div.explorer-bar', upBtn, crumbs),
      h('div.explorer-body', h('div.tm-wrap', tm.el), listEl)));
  clear(box, summary, findingsBox, explorer);

  let rows = new Map();
  function highlightRow(path) { for (const [p, r] of rows) r.classList.toggle('hl', p === path); }
  async function openNode(path, push = true) {
    if (push && st.node && st.node !== path) (st.trail ||= []).push(st.node);
    st.node = path;
    st.tab = 'folders'; tabs.set('folders');
    await showFolders();
  }
  async function showFolders() {
    let r;
    try { r = await jobResult(job.id, { view: 'tree', node: st.node }); } catch (e) { toastError(e); return; }
    const kids = (r.children || []).map(c => ({ ...c, kind: c.kind === 'dir' || c.kind === 'file' ? c.kind : 'other' }));
    tm.set(kids);
    upBtn.disabled = !(st.trail && st.trail.length) && st.node === rootPath;
    if (!st.trail?.length && st.node !== rootPath && r.parent) st.trail = [rootPath];
    // crumbs relative to the analysed folder
    const all = pathCrumbs(st.node);
    const rootDepth = pathCrumbs(rootPath).length - 1;
    const parts = all.slice(Math.max(0, rootDepth));
    const cr = [];
    parts.forEach((c, i) => {
      if (i) cr.push(icon('chevronRight', { cls: 'crumb-sep' }));
      cr.push(h('button.crumb', { text: i === 0 ? (basename(rootPath) || rootPath) : c.name, onclick: () => { st.trail = []; openNode(c.path, false); if (c.path !== rootPath) st.trail = [rootPath]; } }));
    });
    clear(crumbs, cr, h('span.crumb-size.faint.num', { text: bytes(r.size) }));
    const max = Math.max(1, ...kids.map(k => k.size));
    rows = new Map();
    clear(listEl, kids.length ? kids.map(k => {
      const kindKey = k.kind === 'dir' ? 'dir' : k.kind === 'file' ? fileKind(k.name) : 'other';
      const row = h(k.kind === 'dir' ? 'button.sl-row.clickable' : 'div.sl-row', {
        type: k.kind === 'dir' ? 'button' : undefined, title: k.path || k.name,
        onclick: k.kind === 'dir' ? () => openNode(k.path) : undefined,
        onmouseenter: () => tm.highlight(k.path), onmouseleave: () => tm.highlight(null),
      },
      h('span.sl-ic', { style: { color: KIND_TONE[kindKey] || '' } }, icon(k.kind === 'other' ? 'layers' : kindIcon(kindKey))),
      h('span.sl-main',
        h('span.sl-name', h('span.ellipsis', { text: k.name }), k.regen ? h('span.badge.safe', { title: `${k.regen.what}. Recreate with: ${k.regen.regenerate}` }, 'Rebuildable') : null, k.unreadable ? h('span.badge.warn', 'Partly unreadable') : null),
        capBar(k.size / max, '', 'thin')),
      h('span.sl-size.num', { text: bytes(k.size) }),
      k.path ? h('button.btn.icon.sm.ghost.sl-reveal', { title: store.words.showIn, 'aria-label': store.words.showIn, onclick: e => { e.stopPropagation(); attempt(() => api.post('/api/reveal', { path: k.path })); } }, icon('reveal')) : h('span'));
      if (k.path) rows.set(k.path, row);
      return row;
    }) : h('div.empty.small', h('p', 'This folder is empty.')));
  }
  async function showLargest() {
    tm.set([]);
    clear(crumbs, h('span.crumb', 'Largest files in ', h('b', { text: basename(rootPath) || rootPath })));
    let r;
    try { r = await jobResult(job.id, { view: 'largest', limit: 200 }); } catch (e) { toastError(e); return; }
    const files = Array.isArray(r) ? r : (r.files || r.items || []);
    tm.set(files.slice(0, 40).map(f => ({ name: f.name || basename(f.path), path: f.path, size: f.allocated || f.size, kind: 'file' })));
    const max = Math.max(1, ...files.map(f => f.allocated || f.size));
    clear(listEl, files.map(f => h('div.sl-row', { title: f.path },
      h('span.sl-ic', { style: { color: KIND_TONE[fileKind(f.path)] || '' } }, icon(kindIcon(fileKind(f.path)))),
      h('span.sl-main', h('span.sl-name', h('span.ellipsis', { text: f.name || basename(f.path) })), h('span.sl-sub.faint.ellipsis', { text: `${tildify(dirname(f.path), store.info.home)}${f.mtime ? ` - ${date(f.mtime)}` : ''}` }), capBar((f.allocated || f.size) / max, '', 'thin')),
      h('span.sl-size.num', { text: bytes(f.allocated || f.size) }),
      h('button.btn.icon.sm.ghost.sl-reveal', { title: store.words.showIn, 'aria-label': store.words.showIn, onclick: () => attempt(() => api.post('/api/reveal', { path: f.path })) }, icon('reveal')))));
  }
  function showTab() { if ((st.tab || 'folders') === 'folders') showFolders(); else showLargest(); }
  showTab();
}

function findingCard(f) {
  const m = FINDING[f.action] || FINDING.none;
  let btn = null;
  const paths = f.paths || [];
  if (f.action === 'dupes') btn = h('button.btn.sm', { onclick: () => go('duplicates', { paths: JSON.stringify(paths.slice(0, 20)) }) }, m.label);
  else if (f.action === 'sparse') btn = h('button.btn.sm', { onclick: () => go('cleanup', { tab: 'sparse', paths: JSON.stringify(paths.slice(0, 50)) }) }, m.label);
  else if (f.action === 'clean') btn = h('button.btn.sm', { onclick: () => go('cleanup') }, m.label);
  else if (f.action === 'reveal' && paths.length) btn = h('button.btn.sm', { onclick: () => attempt(() => api.post('/api/reveal', { path: paths[0] })) }, icon('reveal'), store.words.showIn);
  return h('article.finding', { class: f.action === 'none' ? 'note' : '' },
    h('div.finding-top', h('span.finding-ic', icon(f.action === 'none' ? 'info' : m.icon)), f.bytes ? h('span.finding-bytes.num', { text: bytes(f.bytes) }) : null),
    h('h3.finding-title', { text: f.title }),
    h('p.finding-detail', { text: f.detail }),
    btn ? h('div.finding-act', btn) : null);
}
