// Small DOM + formatting helpers shared by every module.

/**
 * h('div.panel.big', {on: {click}, text, attrs}, ...children)
 * - props.class: string | array | {name: bool}
 * - props.on: {event: handler}; keys like onClick are also accepted
 * - props.style: object or string
 * - props.dataset: object
 * - boolean/property keys: checked, disabled, value, indeterminate, hidden, selected
 * - props.html: TRUSTED markup only (icons drawn in this codebase)
 */
export function h(tag, props, ...kids) {
  if (props instanceof Node || Array.isArray(props) || typeof props !== 'object' || props === null) {
    if (props !== undefined && props !== null) kids.unshift(props);
    props = {};
  }
  const [name, ...classes] = tag.split('.');
  const el = name === 'svg' || name === 'path' ? document.createElementNS('http://www.w3.org/2000/svg', name) : document.createElement(name || 'div');
  if (classes.length) el.classList.add(...classes);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false && !PROPS.has(k)) continue;
    if (k === 'class') addClass(el, v);
    else if (k === 'text') el.textContent = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'style') {
      if (typeof v === 'string') el.style.cssText = v;
      else for (const [sk, sv] of Object.entries(v)) { if (sk.startsWith('--')) el.style.setProperty(sk, sv); else el.style[sk] = sv; }
    }
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (PROPS.has(k)) el[k] = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  append(el, kids);
  return el;
}
const PROPS = new Set(['checked', 'disabled', 'value', 'indeterminate', 'hidden', 'selected', 'tabIndex', 'open']);

function addClass(el, v) {
  if (!v) return;
  if (typeof v === 'string') { for (const c of v.split(/\s+/)) if (c) el.classList.add(c); }
  else if (Array.isArray(v)) v.forEach(c => addClass(el, c));
  else for (const [c, on] of Object.entries(v)) if (on) el.classList.add(c);
}

export function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false || k === '') continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

export function clear(el, ...kids) { el.replaceChildren(); return append(el, kids); }
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ---------------------------------------------------------------- formatting
let BASE = 1000;   // Finder shows decimal sizes; Windows Explorer binary
export function setSizeBase(platform) { BASE = platform === 'windows' ? 1024 : 1000; }

export function bytes(n, { zero = '0 bytes' } = {}) {
  if (n === null || n === undefined || Number.isNaN(n)) return '–';
  n = Number(n);
  if (n === 0) return zero;
  const units = ['bytes', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  let v = Math.abs(n);
  while (v >= BASE && i < units.length - 1) { v /= BASE; i++; }
  if (i === 0) return `${Math.round(n)} ${Math.abs(n) === 1 ? 'byte' : 'bytes'}`;
  const s = v >= 100 ? Math.round(v).toString() : v >= 10 ? v.toFixed(1) : v.toFixed(v < 1.995 && i >= 3 ? 2 : 1);
  return `${n < 0 ? '-' : ''}${s.replace(/\.0+$/, '')} ${units[i]}`;
}
export function bytesParts(n) {
  const s = bytes(n);
  const i = s.lastIndexOf(' ');
  return i > 0 ? [s.slice(0, i), s.slice(i + 1)] : [s, ''];
}

export const num = n => (n === null || n === undefined || Number.isNaN(n)) ? '–' : Math.round(Number(n)).toLocaleString();
export const pct = (x, digits = 0) => (x === null || x === undefined) ? '–' : `${(x * 100).toFixed(digits)}%`;
export const plural = (n, one, many = one + 's') => `${num(n)} ${n === 1 ? one : many}`;

export function dur(s) {
  if (s === null || s === undefined || !isFinite(s) || s < 0) return '–';
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} min`;
  return `${Math.floor(h / 24)} days`;
}
export function etaText(s) {
  if (s === null || s === undefined || !isFinite(s) || s <= 0) return '–';
  if (s < 50) return 'Under a minute';
  return `About ${dur(s)}`;
}
export function speed(bps) { return bps > 0 ? `${bytes(bps)}/s` : '–'; }

export function date(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  if (sameDay) return `Today ${time}`;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
}
export function ago(ms) {
  const s = (Date.now() - ms) / 1000;
  if (s < 45) return 'Just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return date(ms);
}

// ---------------------------------------------------------------- paths
export const sepOf = p => (p && p.includes('\\') && !p.includes('/')) ? '\\' : '/';
export function basename(p) {
  if (!p) return '';
  const t = p.replace(/[\\/]+$/, '');
  const i = Math.max(t.lastIndexOf('/'), t.lastIndexOf('\\'));
  return (i >= 0 ? t.slice(i + 1) : t) || p;
}
export function dirname(p) {
  if (!p) return '';
  const t = p.replace(/[\\/]+$/, '');
  const i = Math.max(t.lastIndexOf('/'), t.lastIndexOf('\\'));
  return i > 0 ? t.slice(0, i) : (i === 0 ? t[0] : '');
}
export function joinPath(a, b) {
  const s = sepOf(a);
  return a.replace(/[\\/]+$/, '') + s + b;
}
/** '/Users/sam/x' -> [{name:'/',path:'/'},{name:'Users',path:'/Users'},...]; 'C:\\a' -> [{C:\\},{a}] */
export function pathCrumbs(p, rootName) {
  if (!p) return [];
  const win = /^[A-Za-z]:[\\/]?/.test(p) || p.startsWith('\\\\');
  const s = win ? '\\' : '/';
  const parts = p.split(/[\\/]+/).filter(Boolean);
  const out = [];
  let acc;
  if (!win) { acc = '/'; out.push({ name: rootName || '/', path: '/' }); }
  parts.forEach((part, i) => {
    if (win && i === 0) { acc = part + '\\'; out.push({ name: rootName || part, path: acc }); return; }
    acc = acc.endsWith(s) ? acc + part : acc + s + part;
    out.push({ name: part, path: acc });
  });
  return out;
}
export const ext = name => { const m = /\.([^.\\/]+)$/.exec(name || ''); return m ? m[1].toLowerCase() : ''; };
export function tildify(p, home) {
  if (home && p && (p === home || p.startsWith(home + '/') || p.startsWith(home + '\\'))) return '~' + p.slice(home.length);
  return p;
}

export const FILE_KINDS = {
  image: /^(jpe?g|png|gif|webp|heic|heif|bmp|tiff?|svg|avif|ico|raw|cr2|cr3|nef|arw|dng|orf|rw2|psd)$/,
  video: /^(mp4|m4v|mov|webm|avi|mkv|mts|m2ts|3gp|wmv)$/,
  audio: /^(mp3|m4a|wav|aac|flac|ogg|aiff?|opus)$/,
  pdf: /^pdf$/,
  text: /^(txt|md|json|js|mjs|ts|tsx|jsx|py|c|cc|cpp|h|hpp|ino|kt|java|swift|go|rs|html?|css|xml|ya?ml|toml|ini|cfg|conf|csv|tsv|log|sh|bat|ps1|gcode|sql|rb|php|r|tex|srt|plist)$/,
  archive: /^(zip|rar|7z|tar|gz|tgz|bz2|xz|dmg|iso|pkg|msi|exe|img|vmdk|vdi|vhdx?|qcow2|hdd|sparseimage)$/,
  doc: /^(docx?|xlsx?|pptx?|pages|numbers|key|odt|ods|odp|rtf|epub)$/,
};
export function fileKind(name) {
  const e = ext(name);
  for (const [k, re] of Object.entries(FILE_KINDS)) if (re.test(e)) return k;
  return 'file';
}
// what the built-in viewer can show
export const previewable = name => /^(jpe?g|png|gif|webp|bmp|svg|avif|heic|heif|tiff?|mp4|m4v|mov|webm|mp3|m4a|wav|aac|flac|ogg|pdf)$/.test(ext(name)) || FILE_KINDS.text.test(ext(name));

// ---------------------------------------------------------------- misc
export function debounce(fn, ms = 200) {
  let t;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.cancel = () => clearTimeout(t);
  return d;
}
export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const sleep = ms => new Promise(r => setTimeout(r, ms));
export const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** Wrap occurrences of the query words in <mark>. Returns a fragment (text-safe). */
export function highlight(text, query) {
  const frag = document.createDocumentFragment();
  const clean = String(text || '').replace(/<\/?b>|\[|\]/g, '');
  const words = String(query || '').trim().split(/\s+/).filter(w => w.length > 1).map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!words.length) { frag.append(clean); return frag; }
  const re = new RegExp(`(${words.join('|')})`, 'gi');
  let last = 0;
  for (const m of clean.matchAll(re)) {
    if (m.index > last) frag.append(clean.slice(last, m.index));
    frag.append(h('mark', { text: m[0] }));
    last = m.index + m[0].length;
  }
  if (last < clean.length) frag.append(clean.slice(last));
  return frag;
}

/** Name on top, containing folder below (folder ellipsised from the left). */
export function pathCell(path, { name, home, extra } = {}) {
  const folder = tildify(dirname(path), home);
  return h('div.pathcell', { title: path },
    h('span.n', { text: name || basename(path) }),
    h('span.p', h('bdi', { text: folder || '' })),
    extra || null);
}

let uid = 0;
export const nextId = (p = 'id') => `${p}-${++uid}`;
