// Recover > progress of a copy, the whole-drive image, and the deleted-file search.
import { api, watchJob, jobResult, isLive, stat, thumbUrl, fileUrl } from '../api.js';
import { store, emit } from '../store.js';
import { h, clear, bytes, num, plural, speed, etaText, basename, dirname, fileKind, joinPath, tildify, previewable } from '../util.js';
import { icon, kindIcon, KIND_TONE } from '../components/icons.js';
import { jobPanel } from '../components/jobpanel.js';
import { capBar, emptyState } from '../components/controls.js';
import { previewSheet, confirmSheet } from '../components/sheet.js';
import { toastError, attempt } from '../components/toast.js';
import { pickFolder } from '../components/filepicker.js';
import { supportButton } from '../components/support.js';
import { go } from '../router.js';
import { render as renderAll, onDispose, openSource, showJobSource, labelFromJob, backToSources } from './recover.js';

const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;

const reveal = path => attempt(() => api.post('/api/reveal', { path }));
const showIn = () => store.words.showIn;

// ------------------------------------------------------------------ copy progress
export function renderCopyRun(body, S) {
  let label = S.session?.label || S.source?.label || '';
  const panel = jobPanel({
    describe: st => {
      label = label || labelFromJob(st) || 'the drive';
      const fd = stat(st, 'files_done'), ft = stat(st, 'files_total', 'found_files');
      const bd = stat(st, 'bytes_done', 'bytes_copied'), bt = stat(st, 'bytes_total', 'found_bytes');
      const failed = stat(st, 'failed') || 0, skipped = stat(st, 'skipped') || 0;
      const recovered = Math.max(0, (fd || 0) - failed);     // files_done counts copied + already there + failed
      const walking = st.walk_done === false;
      const out = st.out_root || st.params?.out || S.out;
      let headline = 'Recovering your files', sub;
      if (st.state === 'paused') headline = 'Recovery paused';
      if (st.state === 'stopping') headline = 'Stopping…';
      if (st.state === 'done') headline = failed ? `Recovered ${num(recovered)} of ${plural(ft, 'file')}` : `All ${plural(ft || recovered, 'file')} recovered`;
      if (st.state === 'stopped') headline = 'Recovery stopped';
      if (st.state === 'error') headline = "The recovery couldn't finish";
      if (st.state === 'done') sub = [`Saved in ${out}. Open a few files to check them, or check the whole copy against the original.`,
        st.renamed ? ` ${plural(st.renamed, 'name')} had characters this system doesn't allow and were adjusted (listed in the log).` : ''];
      else if (st.state === 'stopped') sub = st.stop_reason || 'Finished files are kept. Start again and Retrova carries on where it left off.';
      else if (st.state === 'error') sub = h('span.selectable', { text: st.error || st.message });
      else sub = [`Copying from ${label} to ${out}.`, h('br'), h('span.safe-line', icon('shieldLock'), `${cap(label)} is only read.`)];
      return {
        headline, sub,
        stats: [
          { k: 'Files', v: ft && (fd !== ft || walking) ? `${num(fd)} of ${num(ft)}${walking ? '+' : ''}` : num(ft || fd) },
          { k: 'Data', v: bt && bd !== bt ? `${bytes(bd)} of ${bytes(bt)}` : bytes(bt || bd) },
          isLive(st) ? { k: 'Speed', v: speed(stat(st, 'speed')) } : null,
          isLive(st) ? { k: 'Time left', v: walking ? 'Still counting…' : etaText(stat(st, 'eta')) } : null,
          { k: 'Already there', v: num(skipped), title: 'Files already in the destination with the same size were skipped' },
          { k: 'Failed', v: num(failed), tone: failed ? 'bad' : '' },
          st.free !== undefined && isLive(st) ? { k: 'Free on destination', v: bytes(st.free) } : null,
        ],
      };
    },
    actions: st => {
      const out = st.out_root || st.params?.out || S.out;
      const btns = [];
      if (!isLive(st)) btns.push(h('button.btn.primary', { onclick: () => go('verify', { job: st.id }) }, icon('verify'), 'Check this copy'));
      if (out) btns.push(h('button.btn', { onclick: () => reveal(out) }, icon('reveal'), showIn()));
      if (!isLive(st) && S.session) btns.push(h('button.btn.ghost', { onclick: () => { S.showRun = false; S.copyJob = null; renderAll(); } }, 'Recover more'));
      if (st.state === 'done' && (stat(st, 'files_done') || 0) > (stat(st, 'failed') || 0)) btns.push(supportButton());
      if (!isLive(st) && !S.session) btns.push(h('button.btn.ghost', { onclick: () => backToSources(S) }, 'Done'));
      if (isLive(st) && S.session) btns.push(h('button.btn.ghost', { onclick: () => { S.showRun = false; renderAll(); } }, 'Browse while it runs'));
      return btns;
    },
  });
  const cur = h('div.curlist.hidden');
  const errs = h('div.curlist.errlist.hidden');
  panel.extra.append(cur, errs);
  body.append(h('div.rc-run', panel.el));

  let errSig = '';
  const w = watchJob(S.copyJob, st => {
    S.copyRunning = isLive(st);
    showJobSource(S, st);
    panel.update(st);
    // files in flight
    const current = st.current || [];
    cur.classList.toggle('hidden', !current.length || !isLive(st));
    if (current.length && isLive(st)) {
      clear(cur, h('div.curlist-head', 'Copying now', h('span.faint', st.engine ? `(${st.engine})` : '')),
        current.slice(0, 6).map(c => {
          const p = c.size ? Math.min(1, (c.written || 0) / c.size) : 0;
          return h('div.cur-row', icon(kindIcon(fileKind(c.path))), h('div.pathcell', h('span.n', { text: basename(c.path) }), h('span.p', h('bdi', { text: dirname(c.path) }))),
            capBar(p, '', 'thin'), h('span.num', { text: `${Math.floor(p * 100)}%` }));
        }));
    }
    const list = st.errors || [];
    const sig = `${list.length}|${st.error_count || 0}`;
    if (sig !== errSig) {
      errSig = sig;
      errs.classList.toggle('hidden', !list.length);
      if (list.length) clear(errs, h('div.curlist-head', icon('warning', { cls: 'bad-text' }), `${plural(st.error_count || list.length, 'file')} couldn't be read completely`,
        h('span.grow'), st.error_log ? h('button.btn.sm.ghost', { onclick: () => reveal(st.error_log) }, 'Open the log') : null),
        h('div.err-scroll', list.slice().reverse().map(e => h('div.err-row', icon('xCircle'), h('div', { style: 'min-width:0' }, h('div.ellipsis.selectable', { text: e.path, title: e.path }), h('div.msg.selectable', { text: e.msg }))))));
    }
    if (!isLive(st)) emit('jobs-changed');
  }, { onError: () => { S.copyJob = null; S.showRun = false; renderAll(); } });
  onDispose(() => w.stop());
}

// ------------------------------------------------------------------ whole-drive image
export function renderImage(body, S) {
  if (S.imageJob) return imageRun(body, S);
  const src = S.source;
  const label = S.session?.label || src?.label || 'this drive';
  const size = S.session?.size || src?.size || 0;
  if (!S.imageOut) S.imageOut = S.out || store.info.default_out || joinPath(store.info.home, 'Recovered');
  const free = h('span.hint');
  const reserveIn = h('input.field.sm', { type: 'number', min: '1', value: String(S.reserve), style: 'width:80px', 'aria-label': 'Keep free, in gigabytes', oninput: () => { S.reserve = Number(reserveIn.value) || 15; } });
  const destBtn = h('button.dest', { type: 'button', onclick: async () => {
    const p = await pickFolder({ title: 'Where should the image file go?', start: S.imageOut, confirmLabel: 'Save here' });
    if (p) { S.imageOut = p; renderAll(); }
  } }, h('span.dest-ic', icon('folder')), h('span.dest-main', h('span.ellipsis.dest-name', { text: basename(S.imageOut) }), h('span.ellipsis.faint.dest-path', { text: tildify(S.imageOut, store.info.home) })), h('span.btn.sm', 'Change…'));
  api.get('/api/recover/free', { out: S.imageOut }).then(r => {
    clear(free, `${bytes(r.free)} free there.`, size && r.free < size ? h('span.warn-text', ' The image may not fit, but empty areas are skipped, so it often takes much less.') : ' Empty areas are skipped, so the file usually takes less space than the drive.');
  }).catch(() => {});
  const start = async () => {
    const b = { out: S.imageOut.replace(/[\\/]*$/, S.imageOut.includes('\\') ? '\\' : '/'), reserve_gb: S.reserve };
    if (S.session) b.sid = S.session.sid; else Object.assign(b, { source: src.source, offset: src.offset || 0, size: src.size || undefined });
    try {
      const st = await api.post('/api/recover/image', b);
      S.imageJob = st.id; emit('jobs-changed'); renderAll();
    } catch (e) { toastError(e); }
  };
  body.append(h('div.rc-setup',
    h('section.panel.setup-card',
      h('div.setup-art', icon('disc')),
      h('h2.setup-title', `Copy all of ${label} into one image file`),
      h('p.setup-lead', "Retrova copies every block, in order. It's the fastest way to save everything, nothing can be missed, and you can take files out of the image later, even with the drive unplugged."),
      h('div.setup-rows',
        h('div.setup-row', h('span.label', 'Save the image in'), destBtn, free),
        h('div.setup-row.inline', h('span.label', 'Size of the drive'), h('b.num', { text: size ? bytes(size) : 'Unknown' })),
        h('div.setup-row.inline', h('span.label', 'Keep free on the destination'), h('div.row', reserveIn, h('span.faint', 'GB')))),
      h('div.setup-foot', h('button.btn.primary.lg', { onclick: start }, icon('disc'), 'Create image'),
        h('p.safe-line', icon('shieldLock'), `${label} is only read. Unreadable spots are retried, then filled with zeros and listed.`))),
    h('aside.setup-aside',
      h('h4', 'When to choose this'),
      h('ul',
        h('li', 'The drive is failing and you want one careful pass over it.'),
        h('li', 'You want to recover everything, not just some folders.'),
        h('li', 'You need to give the drive back or send it away.')))));
}

function imageRun(body, S) {
  const panel = jobPanel({
    describe: st => {
      const bd = stat(st, 'bytes_done', 'pos'), bt = stat(st, 'bytes_total', 'total');
      const bad = st.bad ?? stat(st, 'failed') ?? 0, zero = stat(st, 'zero', 'zero_bytes') ?? st.zero;
      const out = st.out || st.params?.out;
      let headline = 'Copying the whole drive into an image';
      if (st.state === 'paused') headline = 'Image paused';
      if (st.state === 'done') headline = 'Image finished';
      if (st.state === 'stopped') headline = 'Image stopped';
      if (st.state === 'error') headline = "The image couldn't be finished";
      const sub = st.state === 'done'
        ? [`Saved as ${out}.`, bad ? ` ${plural(bad, 'unreadable spot')} were filled with zeros and listed in the log.` : ' Every block was read.', ' Open it in Retrova to take files out.']
        : st.state === 'stopped' ? (st.stop_reason || 'Start again and Retrova continues where it stopped.')
        : st.state === 'error' ? (st.error || st.stop_reason)
        : [`Writing to ${out}.`, h('br'), h('span.safe-line', icon('shieldLock'), 'The drive is only read.')];
      return {
        headline, sub,
        stats: [
          { k: 'Copied', v: bt ? `${bytes(bd)} of ${bytes(bt)}` : bytes(bd) },
          isLive(st) ? { k: 'Speed', v: speed(stat(st, 'speed')) } : null,
          isLive(st) ? { k: 'Time left', v: etaText(stat(st, 'eta')) } : null,
          { k: 'Empty areas skipped', v: bytes(zero || 0) },
          { k: 'Unreadable spots', v: num(bad), tone: bad ? 'warn' : '' },
        ],
      };
    },
    actions: st => {
      const out = st.out || st.params?.out;
      const b = [];
      if (st.state === 'done' && out) b.push(h('button.btn.primary', { onclick: () => openSource(S, { source: out, label: basename(out), image: true }).then(() => { S.mode = 'files'; renderAll(); }) }, icon('folderOpen'), 'Open the image'));
      if (out) b.push(h('button.btn', { onclick: () => reveal(out) }, icon('reveal'), showIn()));
      if (!isLive(st)) b.push(h('button.btn.ghost', { onclick: () => { S.imageJob = null; renderAll(); } }, st.state === 'stopped' ? 'Change settings' : 'Done'));
      return b;
    },
  });
  body.append(h('div.rc-run', panel.el));
  const w = watchJob(S.imageJob, st => { showJobSource(S, st); panel.update(st); if (!isLive(st)) emit('jobs-changed'); }, { onError: () => { S.imageJob = null; renderAll(); } });
  onDispose(() => w.stop());
}

// ------------------------------------------------------------------ deleted files (carving)
const GROUP_NAME = { photos: 'Photos', videos: 'Videos', music: 'Music', documents: 'Documents', archives: 'Archives', other: 'Other' };
const FALLBACK_TYPES = [['jpg', 'photos', 'JPEG'], ['png', 'photos', 'PNG'], ['heic', 'photos', 'HEIC'], ['gif', 'photos', 'GIF'], ['mp4', 'videos', 'MP4'], ['mov', 'videos', 'MOV'],
  ['mp3', 'music', 'MP3'], ['wav', 'music', 'WAV'], ['pdf', 'documents', 'PDF'], ['docx', 'documents', 'Word'], ['xlsx', 'documents', 'Excel'], ['pptx', 'documents', 'PowerPoint'], ['zip', 'archives', 'ZIP'], ['sqlite', 'other', 'SQLite']]
  .map(([type, group, name]) => ({ type, ext: type, group, name }));
let carveTypesCache = null;

export function renderCarve(body, S) {
  if (S.carveJob) return carveRun(body, S);
  const src = S.source;
  if (!src) { body.append(emptyState({ title: 'Choose a drive first', actions: [h('a.btn', { href: '#/recover?mode=carve' }, 'Choose a drive')] })); return; }
  const label = S.session?.label || src.label || src.disk || 'this drive';
  if (!S.carveOut) S.carveOut = joinPath(store.info.home, `Found on ${label}`.replace(/[\\/:*?"<>|]/g, '-'));
  const chipsBox = h('div.type-groups');
  const startBtn = h('button.btn.primary.lg', { onclick: () => start() }, icon('scan'), 'Start the search');
  const renderTypes = types => {
    if (!S.carveTypes) S.carveTypes = new Set(types.filter(t => ['photos', 'videos', 'documents'].includes(t.group)).map(t => t.type));
    const groups = {};
    for (const t of types) (groups[t.group] ||= []).push(t);
    clear(chipsBox, Object.entries(groups).map(([g, list]) => {
      const all = list.every(t => S.carveTypes.has(t.type));
      return h('div.type-group',
        h('button.type-group-name', { title: all ? 'Untick all' : 'Tick all', onclick: () => { for (const t of list) all ? S.carveTypes.delete(t.type) : S.carveTypes.add(t.type); renderTypes(types); } }, GROUP_NAME[g] || g),
        h('div.row.wrap', list.map(t => h('button.chip', { 'aria-pressed': String(S.carveTypes.has(t.type)), title: t.name, onclick: () => { S.carveTypes.has(t.type) ? S.carveTypes.delete(t.type) : S.carveTypes.add(t.type); renderTypes(types); } },
          S.carveTypes.has(t.type) ? icon('check') : null, (t.ext || t.type).toUpperCase()))));
    }));
    startBtn.disabled = !S.carveTypes.size;
  };
  if (carveTypesCache) renderTypes(carveTypesCache);
  else api.get('/api/recover/carve_types').then(r => { carveTypesCache = r.types?.length ? r.types : FALLBACK_TYPES; renderTypes(carveTypesCache); })
    .catch(() => { carveTypesCache = FALLBACK_TYPES; renderTypes(carveTypesCache); });

  const destBtn = h('button.dest', { type: 'button', onclick: async () => {
    const p = await pickFolder({ title: 'Where should found files go?', start: S.carveOut, confirmLabel: 'Save here' });
    if (p) { S.carveOut = p; renderAll(); }
  } }, h('span.dest-ic', icon('folder')), h('span.dest-main', h('span.ellipsis.dest-name', { text: basename(S.carveOut) }), h('span.ellipsis.faint.dest-path', { text: tildify(S.carveOut, store.info.home) })), h('span.btn.sm', 'Change…'));

  async function start() {
    const b = { types: [...S.carveTypes], out: S.carveOut };
    if (S.session) b.sid = S.session.sid;     // the engine scans just the open partition
    else {
      b.source = src.source;
      if (src.offset) { b.start = src.offset; if (src.size) b.end = src.offset + src.size; }
    }
    try {
      const st = await api.post('/api/recover/carve', b);
      S.carveJob = st.id; emit('jobs-changed'); renderAll();
    } catch (e) { toastError(e); }
  }

  body.append(h('div.rc-setup',
    h('section.panel.setup-card',
      h('div.setup-art', icon('scan')),
      h('h2.setup-title', `Search ${label} for deleted files`),
      h('p.setup-lead', 'Retrova reads every block of the drive and recognises files by what is inside them. Found files get new names like retrova-1a2b3c.jpg, because the original names are usually gone.'),
      h('div.callout.warn', icon('warning'), h('div.grow', h('b', "Don't save anything new to this drive until you're done"), h('span.muted', 'New files can overwrite the ones you want back.'))),
      h('div.setup-rows',
        h('div.setup-row', h('span.label', 'What to look for'), chipsBox),
        h('div.setup-row', h('span.label', 'Save found files in'), destBtn, h('span.hint', 'Use a different drive from the one you are searching.'))),
      h('div.setup-foot', startBtn, h('p.safe-line', icon('shieldLock'), `${label} is only read.`))),
    h('aside.setup-aside',
      h('h4', 'Good to know'),
      h('ul',
        h('li', 'The search takes about as long as reading the whole drive once.'),
        h('li', 'Photos and videos come back best. Large files that were split up on the drive may come back incomplete.'),
        h('li', 'You can stop at any time and keep what has been found.')))));
}

function carveRun(body, S) {
  const typeBar = h('div.found-types');
  const grid = h('div.found-grid');
  const more = h('div.found-more');
  let curType = '';
  let lastFound = -1, lastFetch = 0, lastStatus = null;
  const panel = jobPanel({
    ringSize: 148,
    describe: st => {
      const found = stat(st, 'found') ?? 0;
      let headline = `Searching for deleted files`;
      if (st.state === 'paused') headline = 'Search paused';
      if (st.state === 'done') headline = found ? `Found ${plural(found, 'file')}` : 'No deleted files found';
      if (st.state === 'stopped') headline = `Search stopped - ${plural(found, 'file')} found`;
      if (st.state === 'error') headline = "The search couldn't finish";
      const out = st.out || st.params?.out;
      const sub = st.state === 'done' ? (found ? `Saved in ${out}. Open them below or in ${store.words.finder}.` : 'Nothing with a recognisable file signature was found on this drive.')
        : st.state === 'error' ? (st.error || st.stop_reason)
        : st.state === 'stopped' ? (st.stop_reason || 'Everything found so far is kept.')
        : [`Saving to ${out}.`, h('br'), h('span.safe-line', icon('shieldLock'), 'The drive is only read.')];
      return {
        headline, sub,
        stats: [
          { k: 'Searched', v: `${bytes(stat(st, 'bytes_done'))} of ${bytes(stat(st, 'bytes_total'))}` },
          isLive(st) ? { k: 'Speed', v: speed(stat(st, 'speed')) } : null,
          isLive(st) ? { k: 'Time left', v: etaText(stat(st, 'eta')) } : null,
          { k: 'Files found', v: num(found), tone: found ? 'ok' : '' },
          stat(st, 'failed') ? { k: 'Unreadable areas', v: num(stat(st, 'failed')), tone: 'warn' } : null,
        ],
      };
    },
    actions: st => {
      const out = st.out || st.params?.out;
      const b = [];
      if (out && (stat(st, 'found') || 0) > 0) b.push(h('button.btn', { onclick: () => reveal(out) }, icon('reveal'), showIn()));
      if (!isLive(st)) b.push(h('button.btn.ghost', { onclick: () => { S.carveJob = null; renderAll(); } }, 'New search'));
      return b;
    },
  });
  const results = h('section.found', h('div.found-head', h('h3', 'Found files'), typeBar), grid, more);
  body.append(h('div.rc-run.scroll', panel.el, results));

  async function fetchItems(force = false) {
    if (!force && Date.now() - lastFetch < 3500) return;
    lastFetch = Date.now();
    try {
      const r = await jobResult(S.carveJob, { type: curType, limit: 300 });
      renderTypes(r.counts || {});
      renderItems(r.items || [], r.more || 0);
    } catch { /* not ready */ }
  }
  function renderTypes(counts) {
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const types = Object.entries(counts).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
    clear(typeBar, h('button.chip', { 'aria-pressed': String(!curType), onclick: () => { curType = ''; fetchItems(true); } }, 'All', h('span.count', num(total))),
      types.map(([t, n]) => h('button.chip', { 'aria-pressed': String(curType === t), onclick: () => { curType = t; fetchItems(true); } }, t.toUpperCase(), h('span.count', num(n)))));
    results.classList.toggle('hidden', !total);
  }
  function renderItems(items, moreN) {
    clear(grid, items.map(it => {
      const kind = fileKind(it.path);
      const thumb = kind === 'image'
        ? h('img', { src: thumbUrl(it.path, 256), alt: '', loading: 'lazy', decoding: 'async', onerror: e => e.target.replaceWith(h('span.found-ic', icon('image'))) })
        : h('span.found-ic', { style: `color:${KIND_TONE[kind] || ''}` }, icon(kindIcon(kind)));
      return h('button.found-card', { type: 'button', title: it.path, onclick: () => previewSheet({ name: basename(it.path), url: fileUrl(it.path), size: it.size, path: it.path, onReveal: () => reveal(it.path) }) },
        h('span.found-thumb', thumb),
        h('span.found-name.ellipsis', { text: basename(it.path) }),
        h('span.found-meta.faint', { text: bytes(it.size) }));
    }));
    clear(more, moreN ? `${num(moreN)} more in this list. Open the folder to see everything.` : '');
  }

  const w = watchJob(S.carveJob, st => {
    lastStatus = st;
    showJobSource(S, st);
    panel.update(st);
    const found = stat(st, 'found') ?? 0;
    if (found !== lastFound || !isLive(st)) { lastFound = found; fetchItems(!isLive(st)); }
    if (!isLive(st)) emit('jobs-changed');
  }, { interval: 1000, onError: () => { S.carveJob = null; renderAll(); } });
  onDispose(() => w.stop());
}
