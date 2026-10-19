// Find Text: search the words inside photos, screenshots and PDFs (an on-device index).
import { api, watchJob, isLive, stat, thumbUrl, fileUrl, latestJob } from '../api.js';
import { store, emit } from '../store.js';
import { h, clear, num, plural, basename, dirname, tildify, highlight, debounce, joinPath } from '../util.js';
import { icon } from '../components/icons.js';
import { emptyState, sectorArt } from '../components/controls.js';
import { jobStrip } from '../components/jobpanel.js';
import { previewSheet } from '../components/sheet.js';
import { toastError, attempt } from '../components/toast.js';
import { pickFolder } from '../components/filepicker.js';
import { setToolbar } from '../shell.js';

const S = () => store.find;
const TRIES = ['wifi password', 'receipt', 'boarding pass', 'invoice', 'recipe'];
let ctx = null;

export default {
  async mount(root) {
    ctx = { root, stops: [] };
    const st = S();
    const input = h('input.field.ft-input', { type: 'search', placeholder: 'Search the text in your photos, screenshots and PDFs', value: st.q || '', 'aria-label': 'Search text', spellcheck: 'false', autocomplete: 'off' });
    const strip = h('div.ft-strip');
    const meta = h('div.ft-meta');
    const body = h('div.ft-body');
    root.append(h('div.ft-search', h('div.field-wrap.ft-wrap', icon('search'), input), meta), strip, body);
    ctx.input = input; ctx.body = body; ctx.meta = meta; ctx.strip = strip;
    setToolbar({ title: 'Find Text', actions: [h('button.btn.sm', { onclick: () => indexFolder() }, icon('plus'), 'Make a folder searchable…')] });

    await loadStatus();
    const run = debounce(() => search(input.value), 180);
    input.addEventListener('input', () => { st.q = input.value; run(); });
    input.addEventListener('keydown', e => { if (e.key === 'Escape' && input.value) { e.stopPropagation(); input.value = ''; st.q = ''; search(''); } });
    requestAnimationFrame(() => input.focus());
    if (st.q) search(st.q); else renderIdle();
    try { const j = await latestJob('ocr'); if (j && isLive(j)) watchIndex(j.id); } catch { /* none */ }
    return () => { ctx.stops.forEach(f => f()); ctx = null; };
  },
};

async function loadStatus() {
  const st = S();
  try { st.status = await api.get('/api/ocr/status'); } catch (e) { st.status = { available: false, reason: e.message }; }
  const s = st.status;
  if (!ctx) return;
  ctx.input.disabled = !s.available || !s.indexed;
  const folders = foldersOf(s);
  clear(ctx.meta, s.available && s.indexed
    ? [icon('checkCircle', { cls: 'ok-text' }), h('span', `${plural(s.indexed, 'file')} searchable`), folders.length ? h('span.faint', ` in ${folders.slice(0, 3).map(f => basename(f.path)).join(', ')}${folders.length > 3 ? ` and ${folders.length - 3} more` : ''}`) : null,
      s.engine ? h('span.faint.ft-engine', `Read on this ${store.words.computer} with ${s.engine}`) : null]
    : null);
}
function foldersOf(s) {
  return (s.folders || []).map(f => typeof f === 'string' ? { path: f } : f);
}

function renderIdle() {
  const st = S();
  const s = st.status || {};
  if (!ctx) return;
  if (!s.available) {
    clear(ctx.body, h('div.panel', emptyState({ iconName: 'findtext', title: "Text search isn't available on this system", body: s.reason || "Retrova couldn't find a text recognition engine here. Everything else still works." })));
    return;
  }
  if (!s.indexed) {
    const home = store.info.home;
    clear(ctx.body, h('div.panel.hero-empty',
      sectorArt(7),
      h('h2', 'Make your photos and PDFs searchable'),
      h('p', `Retrova reads the words in your images, screenshots and the text in PDFs once, on this ${store.words.computer}, and keeps a private index. Nothing is uploaded anywhere.`),
      h('div.row.wrap', ['Pictures', 'Documents', 'Desktop', 'Downloads'].map(n => h('button.btn', { onclick: () => startIndex([joinPath(home, n)]) }, icon('folder'), n)),
        h('button.btn.primary', { onclick: () => indexFolder() }, icon('plus'), 'Choose a folder…'))));
    return;
  }
  const folders = foldersOf(s);
  clear(ctx.body, h('div.ft-idle',
    h('section.ft-tries', h('h3', 'Try searching for'), h('div.row.wrap', TRIES.map(t => h('button.chip', { onclick: () => { ctx.input.value = t; st.q = t; search(t); } }, icon('search'), t)))),
    folders.length ? h('section.panel.ft-folders', h('div.vprob-head', h('h3', 'Searchable folders'), h('span.grow'), h('button.btn.sm', { onclick: () => indexFolder() }, icon('plus'), 'Add folder…')),
      folders.map(f => h('div.list-row', icon('folder', { cls: 'ok-text' }), h('div.pathcell.grow', h('span.n', { text: basename(f.path) }), h('span.p', h('bdi', { text: tildify(dirname(f.path), store.info.home) }))),
        f.files !== undefined ? h('span.faint.num', plural(f.files, 'file')) : null,
        h('button.btn.sm.ghost', { title: 'Read new and changed files', onclick: () => startIndex([f.path]) }, icon('refresh'), 'Update')))) : null));
}

let seq = 0;
async function search(q) {
  const st = S();
  const my = ++seq;
  q = q.trim();
  if (!q) { renderIdle(); return; }
  let r;
  try { r = await api.get('/api/ocr/search', { q, limit: 60 }); } catch (e) { if (my === seq) clear(ctx.body, h('div.callout.bad', icon('error'), h('div.grow', h('b', "Search didn't work"), h('span.muted', e.message)))); return; }
  if (my !== seq || !ctx) return;
  const results = r.results || [];
  if (!results.length) {
    clear(ctx.body, h('div.ft-none', icon('search', { size: 22 }), h('h3', `No text matching "${q}"`), h('p.muted', 'Try fewer or different words. Only folders you made searchable are included.')));
    return;
  }
  clear(ctx.body, h('p.ft-count.faint', `${plural(results.length, 'match', 'matches')}${results.length >= 60 ? ' (showing the first 60)' : ''}`),
    h('div.ft-grid', results.map(res => resultCard(res, q))));
}

function resultCard(res, q) {
  const reveal = () => attempt(() => api.post('/api/reveal', { path: res.path }));
  const name = basename(res.path);
  return h('button.ft-card', { type: 'button', title: res.path, onclick: () => previewSheet({ name, url: fileUrl(res.path), path: res.path, onReveal: reveal }) },
    h('span.ft-thumb', h('img', { src: thumbUrl(res.path, 256), alt: '', loading: 'lazy', decoding: 'async', onerror: e => e.target.replaceWith(h('span.ft-noimg', icon(res.kind === 'pdf' ? 'pdf' : 'image'))) })),
    h('span.ft-text',
      h('span.ft-name.ellipsis', { text: name }),
      h('span.ft-snippet', highlight(res.snippet, q)),
      h('span.ft-path.faint.ellipsis', { text: tildify(dirname(res.path), store.info.home) })));
}

async function indexFolder() {
  const p = await pickFolder({ title: 'Make a folder searchable', start: store.info.home, confirmLabel: 'Make searchable' });
  if (p) startIndex([p]);
}
async function startIndex(paths) {
  try {
    const r = await api.post('/api/ocr/index', { paths });
    emit('jobs-changed');
    watchIndex(r.id);
  } catch (e) { toastError(e); }
}
function watchIndex(id) {
  if (!ctx) return;
  const strip = jobStrip({
    describe: s => ({
      headline: s.state === 'done' ? 'Finished reading text' : s.title || 'Reading text',
      sub: stat(s, 'files_total') ? `${num(stat(s, 'files_done'))} of ${plural(stat(s, 'files_total'), 'file')}${s.state === 'done' ? ' - they are searchable now' : ' - you can search while this runs'}` : (s.phase || ''),
    }),
  });
  clear(ctx.strip, strip.el);
  const w = watchJob(id, async s => {
    strip.update(s);
    if (!isLive(s)) {
      emit('jobs-changed');
      await loadStatus();
      if (!S().q) renderIdle();
      setTimeout(() => { if (ctx && ctx.strip.contains(strip.el)) clear(ctx.strip); }, 4000);
    }
  });
  ctx.stops.push(() => w.stop());
}
