// Verify: compare a copy with its original, list problems, repair from the original, export CSV.
import { api, watchJob, jobResult, isLive, stat, rawUrl, latestJob } from '../api.js';
import { store, emit } from '../store.js';
import { h, clear, append, bytes, num, plural, speed, etaText, debounce, basename, dirname, ago } from '../util.js';
import { icon } from '../components/icons.js';
import { segmented, emptyState, checkbox } from '../components/controls.js';
import { jobPanel } from '../components/jobpanel.js';
import { confirmSheet, whatList } from '../components/sheet.js';
import { toastError, attempt } from '../components/toast.js';
import { setToolbar } from '../shell.js';
import { go } from '../router.js';
import { startedAt } from '../jobs-meta.js';
import { supportButton } from '../components/support.js';

const LABEL = { OK: 'Matches', MISSING: 'Missing in copy', EXTRA: 'Only in copy', SIZE: 'Size differs', CONTENT: 'Content differs', SRC_BAD: 'Original unreadable', DST_BAD: 'Copy unreadable', LINK: 'Link differs', ERROR: 'Error' };
const ORDER = ['MISSING', 'SIZE', 'CONTENT', 'DST_BAD', 'ERROR', 'SRC_BAD', 'LINK', 'EXTRA'];
const TONE = { MISSING: 'bad', SIZE: 'bad', CONTENT: 'bad', DST_BAD: 'bad', ERROR: 'bad', SRC_BAD: 'warn', LINK: '', EXTRA: '' };
const MODES = {
  quick: { label: 'Quick', name: 'quick check', hint: "Names and sizes only. Fast, but it won't notice damaged contents." },
  sample: { label: 'Spot check', name: 'spot check', hint: 'Size, plus the start, the end and 8 random 1 MB pieces of every file. Quickly catches copies that were cut short or filled with zeros.' },
  full: { label: 'Every byte', name: 'every-byte check', hint: 'Reads both sides completely and compares every byte. The slowest and the only exact check. It also saves checksums, so the copy can be re-checked later without the original drive.' },
};

const S = () => {
  store.verify.mode ||= 'full';
  store.verify.workers ||= 4;
  if (store.verify.resume === undefined) store.verify.resume = true;
  return store.verify;
};

let ctx = null;

export default {
  async mount(root, params) {
    const st = S();
    ctx = { root, stops: [] };
    if (params.job) { st.candidate = params.job; st.job = null; }
    else if (!st.job && !st.setup) {
      try { const j = await latestJob('verify'); if (j) st.job = j.id; } catch { /* none */ }
    }
    render();
    return () => { ctx.stops.forEach(f => f()); ctx = null; };
  },
};

function render() {
  if (!ctx) return;
  ctx.stops.forEach(f => f());
  ctx.stops = [];
  const st = S();
  clear(ctx.root);
  if (st.job) {
    setToolbar({ title: 'Verify', actions: [h('button.btn.sm', { onclick: () => { st.job = null; st.setup = true; render(); } }, icon('plus'), 'New check')] });
    renderResults(ctx.root, st);
  } else {
    setToolbar({ title: 'Verify' });
    renderSetup(ctx.root, st);
  }
}

// ------------------------------------------------------------------ setup
async function renderSetup(root, st) {
  const listBox = h('div.cand-list', h('div.skel', { style: 'height:64px' }));
  const modeHint = h('p.hint.mode-hint', { text: MODES[st.mode].hint });
  const startBtn = h('button.btn.primary.lg', { onclick: () => start() }, icon('verify'), 'Start check');
  root.append(
    h('header.page-head',
      h('h2.page-title', 'Check that a copy matches the original'),
      h('p.page-sub', 'Retrova compares the copy with the drive it came from, file by file.'),
      h('p.safe-line', icon('shieldLock'), 'The original is only read. Nothing changes unless you choose Repair.')),
    h('div.verify-setup',
      h('section.panel.vs-block', h('h3.vs-title', 'What to check'), listBox),
      h('section.panel.vs-block',
        h('h3.vs-title', 'How thorough'),
        segmented({ cls: 'block', value: st.mode, label: 'How thorough', options: Object.entries(MODES).map(([value, m]) => ({ value, label: m.label })), onChange: v => { st.mode = v; modeHint.textContent = MODES[v].hint; } }),
        modeHint,
        h('div.vs-opts',
          h('div.opt', h('span.label', 'Files checked at a time'), segmented({ cls: 'sm', value: st.workers, options: [1, 2, 4, 8].map(n => ({ value: n, label: String(n) })), onChange: v => { st.workers = v; } }),
            h('span.hint', 'Use 1 or 2 if the original drive starts throwing read errors.')),
          checkbox({ checked: st.resume, label: 'Skip files that already passed an earlier check', onChange: v => { st.resume = v; } })),
        h('div.vs-foot', startBtn))));

  // candidates: finished recoveries, and whatever is ticked in Recover right now
  let jobs = [];
  try { jobs = ((await api.get('/api/jobs', { kind: 'copy' })).jobs || []).filter(j => !isLive(j)); } catch (e) { toastError(e); }
  const rec = store.recover;
  const cands = jobs.map(j => ({
    id: j.id, icon: 'recover',
    title: /(?:from|of)\s+(.+)$/.test(j.title || '') ? `Files recovered from ${/(?:from|of)\s+(.+)$/.exec(j.title)[1]}` : (j.title || 'Recovered files'),
    sub: [j.out_root || j.params?.out, stat(j, 'files_total') ? plural(stat(j, 'files_total'), 'file') : null, startedAt(j) ? ago(startedAt(j)) : null].filter(Boolean).join(', '),
  }));
  if (rec?.session && rec.selection.size) cands.push({ id: 'selection', icon: 'check', title: `What's ticked in Recover (${plural(rec.selection.size, 'item')})`, sub: `${rec.session.label} compared with ${rec.out}` });
  if (!st.candidate || !cands.some(c => c.id === st.candidate)) st.candidate = cands[0]?.id;
  if (!cands.length) {
    clear(listBox, emptyState({ title: 'Nothing to check yet', body: 'Recover some files first. Then come back here to make sure the copy is perfect.', actions: [h('a.btn', { href: '#/recover' }, icon('recover'), 'Go to Recover')] }));
    startBtn.disabled = true;
    return;
  }
  const renderCands = () => clear(listBox, cands.map(c => h('button.cand', { type: 'button', role: 'radio', 'aria-checked': String(st.candidate === c.id), onclick: () => { st.candidate = c.id; renderCands(); } },
    h('span.cand-radio'), h('span.cand-ic', icon(c.icon)),
    h('span.cand-main', h('span.cand-title', { text: c.title }), h('span.cand-sub.ellipsis', { text: c.sub })))));
  renderCands();

  async function start() {
    const body = { mode: st.mode, workers: st.workers, resume: st.resume };
    if (st.candidate === 'selection') {
      const pats = rec.patterns.split(',').map(s => s.trim()).filter(Boolean);
      Object.assign(body, { sid: rec.session.sid, paths: [...rec.selection.keys()], excluded: [...rec.excluded], patterns: pats, out: rec.out, show_hidden: true });
    } else body.from_job = st.candidate;
    try {
      const r = await api.post('/api/recover/verify', body);
      st.job = r.id; st.setup = false; st.filter = ''; st.q = '';
      emit('jobs-changed');
      render();
    } catch (e) { toastError(e); }
  }
}

// ------------------------------------------------------------------ results
function renderResults(root, st) {
  let last = null;
  const panel = jobPanel({
    describe: s => {
      const c = s.counts || {};
      const problems = s.problems ?? ORDER.filter(k => TONE[k] === 'bad').reduce((t, k) => t + (c[k] || 0), 0);
      const mode = MODES[s.mode || s.params?.mode] || MODES.full;
      let headline = s.repair ? 'Repairing the copy' : 'Checking the copy';
      if (s.state === 'paused') headline = 'Check paused';
      if (s.state === 'done') headline = problems ? `${plural(problems, 'problem')} found` : c.SRC_BAD ? 'Everything readable matches' : 'Everything matches';
      if (s.state === 'stopped') headline = 'Check stopped';
      if (s.state === 'error') headline = "The check couldn't finish";
      let sub;
      if (s.state === 'done') sub = problems ? (s.fixable ? `${plural(s.fixable, 'file')} can be repaired from the original.` : 'These need a look - the original can’t fix them.') : `The copy in ${s.out_root || s.params?.out} matches the original${c.SRC_BAD ? ', apart from files the original drive couldn’t read' : ''}.`;
      else if (s.state === 'error') sub = s.error || s.message;
      else if (s.state === 'stopped') sub = s.stop_reason || 'Start again with "Skip files that already passed" to continue.';
      else sub = [s.listing ? 'Listing files on both sides…' : `${mode.label}: ${s.phase || 'comparing'}.`, h('br'), h('span.safe-line', icon('shieldLock'), 'The original is only read.')];
      const fd = stat(s, 'files_done', 'done_files'), ft = stat(s, 'files_total', 'total_files');
      const bd = stat(s, 'bytes_done', 'done_bytes'), bt = stat(s, 'bytes_total', 'total_bytes');
      return {
        headline, sub,
        ringLabel: s.state === 'done' ? (problems ? 'Needs a look' : 'All good') : undefined,
        ringIcon: s.state === 'done' && problems ? 'warning' : undefined,
        ringTone: s.state === 'done' && problems ? 'warn' : undefined,
        stats: [
          { k: 'Files', v: ft && fd !== ft ? `${num(fd)} of ${num(ft)}` : num(ft || fd || s.found || 0) },
          bt ? { k: 'Data', v: bd !== bt ? `${bytes(bd)} of ${bytes(bt)}` : bytes(bt) } : null,
          isLive(s) ? { k: 'Speed', v: stat(s, 'speed') ? speed(stat(s, 'speed')) : stat(s, 'files_per_sec') ? `${num(stat(s, 'files_per_sec'))} files/s` : '–' } : null,
          isLive(s) ? { k: 'Time left', v: etaText(stat(s, 'eta')) } : null,
          { k: 'Matches', v: num(c.OK || 0), tone: 'ok', small: s.repaired ? `${num(s.repaired)} repaired` : '' },
          { k: 'Problems', v: num(problems), tone: problems ? 'bad' : '' },
          c.SRC_BAD ? { k: 'Original unreadable', v: num(c.SRC_BAD), tone: 'warn' } : null,
          s.cached ? { k: 'From an earlier check', v: num(s.cached) } : null,
        ],
      };
    },
    actions: s => {
      const b = [];
      if (!isLive(s) && s.fixable) b.push(h('button.btn.primary', { onclick: () => repair(s) }, icon('wand'), `Repair ${plural(s.fixable, 'file')}`));
      if (!isLive(s) && s.report_dir) b.push(h('button.btn', { onclick: () => attempt(() => api.post('/api/reveal', { path: s.report || s.report_dir })) }, icon('reveal'), 'Open report'));
      const problems = s.problems ?? ORDER.filter(k => TONE[k] === 'bad').reduce((t, k) => t + ((s.counts || {})[k] || 0), 0);
      if (s.state === 'done' && !problems) b.push(supportButton());
      return b;
    },
  });

  const chips = h('div.vchips');
  const search = h('input.field', { type: 'search', placeholder: 'Filter by path', value: st.q || '', 'aria-label': 'Filter problems by path' });
  const csv = h('a.btn.sm', { download: 'retrova-verify.csv' }, icon('download'), 'Export CSV');
  const list = h('div.vlist');
  const moreEl = h('div.hint.vmore');
  const problemsPanel = h('section.panel.vprob',
    h('div.vprob-head', h('h3', 'Problems'), h('span.grow'), h('div.field-wrap.vsearch', icon('search'), search), csv),
    chips, list, moreEl);
  const disks = h('section.panel.vdisks.hidden');
  root.append(h('div.verify-results', panel.el, problemsPanel, disks));

  search.addEventListener('input', debounce(() => { st.q = search.value.trim(); sig = ''; tick(last); }, 250));
  let sig = '';

  async function loadItems(s) {
    try {
      const r = await jobResult(s.id, { status: st.filter || '', q: st.q || '', limit: 300 });
      const items = r.items || [];
      if (!items.length) {
        clear(list, h('div.vempty', isLive(s) ? h('span', 'Problems appear here as they are found.') : [icon('checkCircle', { cls: 'ok-text' }), h('span', st.q || st.filter ? 'Nothing matches this filter.' : 'No problems found.')]));
      } else {
        clear(list, items.map(i => {
          const path = i.sp || i.key || '';
          return h('div.vrow',
            h('span.badge', { class: TONE[i.status] || '' }, h('span.dot', { class: TONE[i.status] || 'unknown' }), i.label || LABEL[i.status] || i.status),
            h('div.pathcell.selectable', { title: path }, h('span.n', { text: basename(path) || path }), h('span.p', h('bdi', { text: dirname(path) }))),
            h('div.vdetail.selectable', { text: i.detail || '' }),
            i.fixable ? h('span.badge.ok', { title: 'Repair can fix this from the original' }, icon('wand'), 'Fixable') : h('span'));
        }));
      }
      clear(moreEl, r.more ? `${num(r.more)} more. Export the CSV to see them all.` : '');
    } catch { /* job gone */ }
  }

  function tick(s) {
    if (!s) return;
    const c = s.counts || {};
    const total = ORDER.reduce((t, k) => t + (c[k] || 0), 0);
    clear(chips, h('button.chip', { 'aria-pressed': String(!st.filter), onclick: () => { st.filter = ''; sig = ''; tick(s); } }, 'All problems', h('span.count', num(total))),
      ORDER.filter(k => c[k]).map(k => h('button.chip', { 'aria-pressed': String(st.filter === k), onclick: () => { st.filter = k; sig = ''; tick(s); } },
        h('span.dot', { class: TONE[k] || 'unknown' }), LABEL[k], h('span.count', num(c[k])))));
    csv.href = rawUrl('/api/recover/verify_csv', { id: s.id, status: st.filter || '', q: st.q || '' });
    problemsPanel.classList.toggle('hidden', !total && isLive(s) && !s.issue_count);
    const ns = `${s.issue_count}|${s.state}|${st.filter}|${st.q}`;
    if (ns !== sig) { sig = ns; loadItems(s); }
    const di = s.disk_images || [];
    disks.classList.toggle('hidden', !di.length);
    if (di.length) clear(disks, h('div.vprob-head', h('h3', 'Virtual machine disks'), h('span.hint', 'Their headers were checked as well, so you know they will start.')),
      di.map(d => h('div.vrow.vdisk', h('span.badge', { class: d.verdict === 'ok' ? 'ok' : d.verdict === 'warn' ? 'warn' : 'bad' }, h('span.dot', { class: d.verdict === 'ok' ? 'ok' : d.verdict }), d.verdict === 'ok' ? 'Looks right' : d.verdict === 'warn' ? 'Check' : 'Damaged'),
        h('div.pathcell', h('span.n', { text: basename(d.path) }), h('span.p', h('bdi', { text: dirname(d.path) }))), h('div.vdetail', { text: d.detail }), h('span'))));
  }

  async function repair(s) {
    let items = [];
    try { items = (await jobResult(s.id, { limit: 2000 })).items.filter(i => i.fixable); } catch { /* list optional */ }
    const ok = await confirmSheet({
      icon: 'wand',
      title: `Repair ${plural(s.fixable, 'file')} from the original?`,
      message: 'Retrova copies only the parts that differ from the original into the copy, then checks those files again.',
      body: append(h('div.stack'), [
        items.length ? whatList(items.map(i => ({ path: i.sp || i.key, label: i.sp || i.key, note: LABEL[i.status] }))) : null,
        h('p.hint', 'The original drive is only read. Parts the original itself can’t read are left as they are in the copy.')]),
      confirmLabel: `Repair ${plural(s.fixable, 'file')}`,
      footNote: 'Only the copy is changed',
    });
    if (!ok) return;
    try {
      const r = await api.post('/api/recover/repair', { job: s.id, workers: S().workers });
      st.job = r.id; st.filter = ''; emit('jobs-changed'); render();
    } catch (e) { toastError(e); }
  }

  const w = watchJob(st.job, s => { last = s; panel.update(s); tick(s); if (!isLive(s)) emit('jobs-changed'); },
    { onError: () => { st.job = null; render(); } });
  ctx.stops.push(() => w.stop());
}
