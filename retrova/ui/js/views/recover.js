// Recover: choose a source, then copy files off it, image it whole, or search it for deleted files.
// State lives in store.recover so leaving and coming back keeps everything (and running jobs).
import { api, latestJob, isLive } from '../api.js';
import { elevate as elevateShared } from '../elevate.js';
import { store } from '../store.js';
import { h, clear, append, bytes, joinPath } from '../util.js';
import { icon } from '../components/icons.js';
import { segmented, emptyState, popover } from '../components/controls.js';
import { spinner } from '../components/ring.js';
import { pickFile } from '../components/filepicker.js';
import { setToolbar, setContentMode, setToolbarCenter } from '../shell.js';
import { renderFiles } from './recover-browse.js';
import { renderCopyRun, renderImage, renderCarve } from './recover-run.js';

export function recoverState() {
  if (!store.recover) {
    store.recover = {
      mode: 'files',
      sources: null, sourcesError: null,
      source: null,            // the chosen entry from /api/recover/sources (or {source, label, image: true})
      session: null,           // /api/recover/open result
      opening: false, openError: null,
      cwd: '/', entries: [], showHidden: false, history: [],
      selection: new Map(), excluded: new Set(),
      regen: { job: null, state: 'idle', found: [], info: new Map() },
      sizes: new Map(),
      patterns: '',
      out: '', reserve: 15, workers: 4,
      copyJob: null, showRun: false,
      imageJob: null, imageOut: '',
      carveJob: null, carveTypes: null, carveOut: '',
    };
  }
  return store.recover;
}

const IMAGE_EXTS = ['img', 'dmg', 'iso', 'bin', 'dd', 'raw', 'e01', 'vhd', 'vhdx', 'vmdk', 'sparseimage', 'hdd', 'qcow2', 'vdi', 'toast'];

let ctx = null;   // {root, params, disposers}

export default {
  async mount(root, params) {
    const S = recoverState();
    if (params.mode && ['files', 'image', 'carve'].includes(params.mode)) S.mode = params.mode;
    ctx = { root, params, disposers: [] };
    await adoptJobs(S);
    render();
    return () => { dispose(); ctx = null; };
  },
};

function dispose() {
  if (!ctx) return;
  for (const d of ctx.disposers.splice(0)) { try { d(); } catch { /* ignore */ } }
}
/** Views in other modules register cleanups (timers, job watchers) here. */
export function onDispose(fn) { ctx?.disposers.push(fn); }

/** Re-attach jobs that are running (e.g. after a reload or from another screen). */
async function adoptJobs(S, { quiet = false } = {}) {
  const kinds = [['copy', 'copyJob'], ['image', 'imageJob'], ['carve', 'carveJob']];
  await Promise.all(kinds.map(async ([kind, key]) => {
    if (S[key]) return;
    try {
      const j = await latestJob(kind);
      if (j && isLive(j)) {
        S[key] = j.id;
        if (quiet) return;
        if (kind === 'copy') S.showRun = true;
        if (!ctx?.params.mode) S.mode = kind === 'copy' ? 'files' : kind;
      }
    } catch { /* engine not ready */ }
  }));
}

export function render() {
  if (!ctx) return;
  dispose();
  const S = recoverState();
  const { root } = ctx;
  clear(root);
  const hasWork = S.source || (S.mode === 'files' && S.copyJob && S.showRun) || (S.mode === 'image' && S.imageJob) || (S.mode === 'carve' && S.carveJob);
  if (!hasWork) {
    setContentMode(false);
    setToolbar({ title: 'Recover', actions: [h('button.btn.ghost.sm', { onclick: () => { S.sources = null; render(); } }, icon('refresh'), 'Refresh')] });
    renderSources(root, S);
    return;
  }
  setContentMode(true);
  setToolbar({ title: 'Recover' });
  setToolbarCenter(segmented({
    label: 'How to recover',
    value: S.mode,
    options: [
      { value: 'files', label: 'Browse files', icon: 'folder' },
      { value: 'image', label: 'Whole-drive image', icon: 'disc' },
      { value: 'carve', label: 'Deleted files', icon: 'scan' },
    ],
    onChange: v => { S.mode = v; history.replaceState(null, '', `#/recover?mode=${v}`); render(); },
  }));
  const body = h('div.rc-body');
  root.append(sourceBar(S), body);
  if (S.mode === 'files') {
    if (S.copyJob && S.showRun) renderCopyRun(body, S);
    else renderFiles(body, S);
  } else if (S.mode === 'image') renderImage(body, S);
  else renderCarve(body, S);
}

// ------------------------------------------------------------------ source bar
/** "Recovering files from MacStorage" -> "MacStorage" (used when the job outlived the page). */
export function labelFromJob(st) {
  const m = /(?:from|of)\s+(.+)$/.exec(st?.title || '');
  return m ? m[1] : '';
}
export function showJobSource(S, st) {
  if (S.source || S.session) return;
  const el = ctx?.root.querySelector('.rc-src-name');
  const label = labelFromJob(st);
  if (el && label) el.textContent = label;
}

function sourceBar(S) {
  const src = S.source;
  const sess = S.session;
  const label = sess?.label || src?.label || src?.disk || (src ? 'Drive' : 'Recovery in progress');
  const facts = [src?.disk && src.disk !== label ? src.disk : null, sess?.fs_name || src?.fs_name || null, (sess?.size || src?.size) ? bytes(sess?.size || src?.size) : null].filter(Boolean);
  const vols = sess?.volumes || [];
  const notes = sess?.notes || [];
  const bar = h('div.rc-sourcebar',
    h('button.btn.ghost.sm', { title: 'Choose a different drive', onclick: () => backToSources(S) }, icon('chevronLeft'), 'Drives'),
    h('div.rc-src-id',
      h('span.rc-src-icon', icon(src?.image ? 'disc' : 'driveExt')),
      h('div.rc-src-text', h('div.rc-src-name.ellipsis', { text: label }), h('div.rc-src-facts', facts.map(f => h('span', { text: f }))))),
    vols.length > 1 ? h('button.btn.sm', { onclick: e => volumeMenu(e.currentTarget, S) }, h('span.faint', 'Volume'), sess.volume || sess.label, icon('chevronDown')) : null,
    notes.length ? h('button.btn.sm.ghost', { onclick: e => notesPop(e.currentTarget, notes) }, icon('info'), `${notes.length} ${notes.length === 1 ? 'note' : 'notes'}`) : null,
    h('span.grow'),
    h('span.ro-badge', { title: 'Retrova opens this drive read-only. Nothing is ever written to it.' }, icon('lock'), 'Read-only'));
  return bar;
}

function notesPop(anchor, notes) {
  popover(anchor, h('div.notes-pop', h('h4', 'About this drive'), notes.map(n => h('p', { text: n }))), { align: 'start', width: 360 });
}

function volumeMenu(anchor, S) {
  popover(anchor, S.session.volumes.map(v => ({ label: v, icon: v === (S.session.volume || S.session.label) ? 'check' : 'disc', onClick: () => openSource(S, S.source, { volume: v }) })), { align: 'start' });
}

export function backToSources(S) {
  S.source = null; S.session = null; S.openError = null; S.showRun = false;
  // let finished jobs go; quietly re-attach running ones (only one disk job can run at a time)
  S.copyJob = S.imageJob = S.carveJob = null;
  history.replaceState(null, '', '#/recover');
  render();
  adoptJobs(S, { quiet: true });
}

// ------------------------------------------------------------------ step 1: choose a source
function flowCopy(flow, mode) {
  if (mode === 'carve' || flow === 'deleted') return {
    title: 'Which drive were the files on?',
    sub: 'Retrova searches it block by block for photos, videos and documents.',
    callout: { tone: 'warn', icon: 'warning', title: "Don't save anything new to that drive for now", text: 'New files can overwrite the deleted ones you want back. Save what Retrova finds to a different drive.' },
  };
  if (flow === 'unmountable') return {
    title: "Which drive won't open?",
    sub: "Pick it below. Retrova reads it directly, even when your computer can't.",
  };
  return { title: 'Which drive do you want to recover from?', sub: 'Pick a drive or a disk image. You choose what to copy next.' };
}

async function renderSources(root, S) {
  const params = ctx.params;
  const fc = flowCopy(params.flow, S.mode);
  const grid = h('div.src-grid');
  const banner = h('div.src-banner');
  append(root, [
    h('header.page-head',
      h('h2.page-title', { text: fc.title }),
      h('p.page-sub', fc.sub),
      h('p.safe-line', icon('shieldLock'), 'Retrova only ever reads this drive. Nothing on it is changed.')),
    fc.callout ? h('div.callout.page-callout', { class: fc.callout.tone }, icon(fc.callout.icon), h('div.grow', h('b', fc.callout.title), h('span.muted', fc.callout.text))) : null,
    banner, grid,
    lastRecovery(S)]);

  if (!S.sources) {
    clear(grid, [0, 1, 2].map(() => h('div.src-card.skeleton', h('div.skel', { style: 'width:40px;height:40px;border-radius:11px' }), h('div', { style: 'flex:1' }, h('div.skel', { style: 'width:50%;height:14px;margin-bottom:10px' }), h('div.skel', { style: 'width:80%;height:10px' })))),
      h('p.hint.src-wait', spinner(14), 'Looking at your drives. A failing drive can take a little while to answer.'));
    try {
      const r = await api.get('/api/recover/sources');
      S.sources = r.sources || [];
      S.sourcesError = null;
    } catch (e) {
      S.sourcesError = e.message;
      S.sources = [];
    }
    if (!root.isConnected) return;
  }
  const info = store.info;
  const needAdmin = S.sources.some(s => s.needs_admin) && !info.admin;
  clear(banner, needAdmin ? h('div.callout.admin-callout', icon('unlock'),
    h('div.grow', h('b', 'Some drives need permission to be read directly'),
      h('span.muted', info.can_elevate ? `Your ${store.words.computer} will ask for your password. Retrova still only reads drives.` : 'Start Retrova with administrator rights to read these drives.')),
    info.can_elevate ? h('button.btn.primary', { onclick: () => elevate() }, icon('unlock'), 'Allow access') : null) : null);

  const hint = (params.hint || '').toLowerCase();
  const list = [...S.sources].sort((a, b) => (b.recommended ? 1 : 0) - (a.recommended ? 1 : 0));
  const cards = list.map(s => sourceCard(S, s, hint && [s.disk, s.label].some(x => (x || '').toLowerCase() === hint)));
  cards.push(h('button.src-card.src-image', { type: 'button', onclick: () => openImageFile(S) },
    h('span.src-icon', icon('disc')),
    h('span.src-main', h('span.src-name', 'Open a disk image file…'), h('span.src-detail', { text: 'A .img, .dmg, .iso or similar copy of a drive, for example one Retrova made earlier.' }))));
  clear(grid, S.sourcesError ? h('div.callout.bad', icon('error'), h('div.grow', h('b', "Couldn't list your drives"), h('span.muted', S.sourcesError))) : null,
    !S.sources.length && !S.sourcesError ? h('p.hint', 'No drives found. Connect the drive, wait a moment, then choose Refresh.') : null,
    cards);
}

function sourceCard(S, s, highlight) {
  const title = s.label || (s.fs ? `${s.fs_name || s.fs} partition` : 'Unknown partition');
  const facts = [s.disk && s.disk !== title ? s.disk : null, s.fs_name || (s.fs ? s.fs.toUpperCase() : null), s.size ? bytes(s.size) : null].filter(Boolean);
  const tags = [];
  if (s.recommended) tags.push(h('span.badge.ok', 'Suggested'));
  if (s.damaged) tags.push(h('span.badge.warn', 'Looks damaged'));
  if (s.encrypted) tags.push(h('span.badge', 'Encrypted'));
  if (s.supported === false || !s.fs) tags.push(h('span.badge', 'Deep scan only'));
  const foot = s.needs_admin && !store.info.admin
    ? h('span.src-foot.warn-text', icon('lock'), 'Needs permission to read')
    : s.mount ? h('span.src-foot', icon('folderOpen'), `Open at ${s.mount}`) : null;
  const card = h('button.src-card', { type: 'button', class: { hl: highlight, rec: s.recommended }, onclick: () => chooseSource(S, s, card) },
    h('span.src-icon', icon(s.removable === false ? 'drive' : /card|sd/i.test(s.disk || '') ? 'sdcard' : 'driveExt')),
    h('span.src-main',
      h('span.src-name', h('span.ellipsis', { text: title }), tags),
      h('span.src-facts', facts.map(f => h('span', { text: f }))),
      s.detail ? h('span.src-detail', { text: s.detail }) : null,
      foot),
    icon('chevronRight', { cls: 'src-chev' }));
  return card;
}

function lastRecovery(S) {
  const box = h('div.src-last');
  latestJob('copy').then(j => {
    if (!j || isLive(j) || !box.isConnected) return;
    const out = j.out_root || j.params?.out || '';
    clear(box, icon('clock'), h('span', `Your last recovery copied files to ${out}.`),
      h('button.btn.sm', { onclick: () => { S.copyJob = j.id; S.showRun = true; S.mode = 'files'; render(); } }, 'View it'),
      h('a.btn.sm.ghost', { href: `#/verify?job=${j.id}` }, 'Check that copy'));
  }).catch(() => {});
  return box;
}

async function chooseSource(S, s, card) {
  if (s.needs_admin && !store.info.admin) { elevate(s); return; }
  if (!s.fs || s.supported === false) {
    // nothing to browse: go straight to the tools that work on raw blocks
    S.source = s; S.session = null; S.openError = null;
    if (S.mode === 'files') S.mode = 'carve';
    render();
    return;
  }
  card?.classList.add('busy');
  card?.querySelector('.src-chev')?.replaceWith(spinner(18));
  await openSource(S, s, {});
}

export async function openSource(S, s, { volume } = {}) {
  S.source = s;
  S.opening = true;
  S.openError = null;
  const body = { source: s.source };
  if (s.offset !== undefined && s.offset !== null && !volume) body.offset = s.offset;
  if (volume) body.volume = volume;
  try {
    const r = await api.post('/api/recover/open', body);
    resetBrowse(S);
    S.session = r;
    if (!S.out) S.out = r.default_out || store.info.default_out || joinPath(store.info.home, 'Recovered');
  } catch (e) {
    S.session = null;
    S.openError = e;
    if (e.status === 403 && !store.info.admin && store.info.can_elevate) { S.source = null; S.opening = false; render(); elevate(s); return; }
  }
  S.opening = false;
  render();
}

function resetBrowse(S) {
  Object.assign(S, { cwd: '/', entries: [], history: [], selection: new Map(), excluded: new Set(), sizes: new Map(),
    regen: { job: null, state: 'idle', found: [], info: new Map() } });
}

async function openImageFile(S) {
  const path = await pickFile({ title: 'Open a disk image', exts: IMAGE_EXTS, start: store.info.home });
  if (!path) return;
  const s = { source: path, label: path.split(/[\\/]/).pop(), image: true, fs: 'image', supported: true };
  await openSource(S, s);
  if (S.openError) {
    // An image with no readable file system can still be deep-scanned.
    S.mode = 'carve';
    render();
  }
}

/** Ask for admin rights, then come back to Recover on the elevated engine. */
export function elevate(forSource) {
  elevateShared({
    reason: forSource ? `${forSource.label || forSource.disk || 'This drive'} can only be read with administrator rights.` : null,
    returnHash: '#/recover' + (S_mode() ? `?mode=${S_mode()}` : ''),
  });
}
const S_mode = () => (ctx?.params?.mode || '');

/** Shown in the Files tab when the drive couldn't be opened. */
export function openFailed(body, S) {
  const e = S.openError;
  body.append(h('div.panel.rc-fail', emptyState({
    title: "Retrova can't show the folders on this drive",
    body: [h('span.selectable', { text: e?.message || 'There is no file system Retrova can read here.' }), h('br'), h('br'), 'You can still get files back:'],
    actions: [
      h('button.btn.primary', { onclick: () => { S.mode = 'carve'; render(); } }, icon('scan'), 'Search for deleted files'),
      h('button.btn', { onclick: () => { S.mode = 'image'; render(); } }, icon('disc'), 'Copy the whole drive to an image'),
      h('button.btn.ghost', { onclick: () => openSource(S, S.source) }, icon('refresh'), 'Try again'),
    ],
  })));
}
