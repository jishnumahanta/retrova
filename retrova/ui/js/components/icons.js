// Inline SVG icons drawn for Retrova on a 20x20 grid, 1.5px strokes.
// icon(name) -> SVGElement. Only static, trusted markup lives here.

const P = {
  home: '<path d="M3.5 9 10 3.6 16.5 9v6.7a1.3 1.3 0 0 1-1.3 1.3h-3V12.6H7.8V17h-3a1.3 1.3 0 0 1-1.3-1.3z"/>',
  recover: '<path d="M14.9 5.3A6.6 6.6 0 1 0 16.6 10"/><path d="m13 2.6 3.4 1.2-.9 3.4" /><circle cx="10" cy="10" r="2"/>',
  verify: '<path d="M10 2.6 4.3 4.7v4.7c0 3.5 2.4 6.2 5.7 7.6 3.3-1.4 5.7-4.1 5.7-7.6V4.7z"/><path d="m7.3 10.1 1.9 1.9 3.6-3.8"/>',
  space: '<path d="M9.2 3.3a6.9 6.9 0 1 0 7.5 7.5H9.2z"/><path d="M12 2.6v5.4h5.4A5.6 5.6 0 0 0 12 2.6z"/>',
  dupes: '<rect x="7" y="7" width="10" height="10" rx="2.2"/><path d="M13 7V5.2A2.2 2.2 0 0 0 10.8 3H5.2A2.2 2.2 0 0 0 3 5.2v5.6A2.2 2.2 0 0 0 5.2 13H7"/>',
  cleanup: '<path d="M8.6 2.8 9.9 7l4.2 1.3-4.2 1.3-1.3 4.2-1.3-4.2L3.1 8.3 7.3 7z"/><path d="m14.6 11.6.7 2.1 2.1.7-2.1.7-.7 2.1-.7-2.1-2.1-.7 2.1-.7z"/>',
  findtext: '<circle cx="8.6" cy="8.6" r="5.6"/><path d="m12.7 12.7 4.3 4.3"/><path d="M6.2 7.4h4.8M6.2 9.9h3"/>',
  system: '<path d="M3.2 14.2a7 7 0 1 1 13.6 0"/><path d="m10 13.8 3.3-4.2"/><circle cx="10" cy="13.8" r="1.2"/><path d="M5.6 9.6l.9.5M10 6.4v1M14.4 9.6l-.9.5"/>',
  activity: '<path d="M2.5 10.5h3l2.2-5.5 3.6 10 2.2-4.5h4"/>',
  drive: '<rect x="2.6" y="5.6" width="14.8" height="8.8" rx="2.2"/><path d="M2.6 11h14.8"/><circle cx="14.1" cy="12.7" r=".5" fill="currentColor"/>',
  driveExt: '<rect x="4.2" y="2.8" width="11.6" height="14.4" rx="2.6"/><path d="M7.4 6h5.2"/><circle cx="10" cy="13.2" r="1.1"/>',
  sdcard: '<path d="M7 2.8h6.6l2.6 2.6v10.4a1.4 1.4 0 0 1-1.4 1.4H5.2a1.4 1.4 0 0 1-1.4-1.4V6z"/><path d="M8.2 3v3M10.6 3v3M13 3v3"/>',
  disc: '<circle cx="10" cy="10" r="7.2"/><circle cx="10" cy="10" r="2"/><path d="M10 5.2a4.8 4.8 0 0 1 4.8 4.8"/>',
  folder: '<path d="M2.8 6.2A1.7 1.7 0 0 1 4.5 4.5h3.3l1.7 1.9h6a1.7 1.7 0 0 1 1.7 1.7v6.2a1.7 1.7 0 0 1-1.7 1.7H4.5a1.7 1.7 0 0 1-1.7-1.7z"/>',
  folderOpen: '<path d="M2.8 14.2V6.2a1.7 1.7 0 0 1 1.7-1.7h3.3l1.7 1.9h5a1.7 1.7 0 0 1 1.7 1.7v.6"/><path d="M2.8 14.4 4.6 9.3a1.6 1.6 0 0 1 1.5-1.1h10.2a1.1 1.1 0 0 1 1 1.4l-1.6 5.1a1.6 1.6 0 0 1-1.5 1.1H4.3a1.5 1.5 0 0 1-1.5-1.4z"/>',
  file: '<path d="M5.7 2.8h5.9l3.6 3.6v9.4a1.4 1.4 0 0 1-1.4 1.4H5.7a1.4 1.4 0 0 1-1.4-1.4V4.2a1.4 1.4 0 0 1 1.4-1.4z"/><path d="M11.4 2.9v3.7h3.7"/>',
  doc: '<path d="M5.7 2.8h5.9l3.6 3.6v9.4a1.4 1.4 0 0 1-1.4 1.4H5.7a1.4 1.4 0 0 1-1.4-1.4V4.2a1.4 1.4 0 0 1 1.4-1.4z"/><path d="M11.4 2.9v3.7h3.7M7.2 10.3h5.6M7.2 13.2h4"/>',
  image: '<rect x="2.8" y="3.8" width="14.4" height="12.4" rx="2.2"/><circle cx="7.4" cy="8" r="1.4"/><path d="m3.3 14.6 4.1-3.8 3 2.8 2.2-2 4.1 3.6"/>',
  video: '<rect x="2.6" y="5" width="10.8" height="10" rx="2.2"/><path d="m13.4 8.5 4-2.2v7.4l-4-2.2"/>',
  audio: '<path d="M7.6 14.8V5.4l8-1.6v9.6"/><circle cx="5.6" cy="14.8" r="2"/><circle cx="13.6" cy="13.4" r="2"/>',
  archive: '<rect x="3.2" y="3" width="13.6" height="14" rx="2.2"/><path d="M9.2 3v1.8h1.6v1.8H9.2v1.8h1.6v1.8H9.2"/><rect x="8.6" y="11.4" width="2.8" height="2.6" rx=".8"/>',
  pdf: '<path d="M5.7 2.8h5.9l3.6 3.6v9.4a1.4 1.4 0 0 1-1.4 1.4H5.7a1.4 1.4 0 0 1-1.4-1.4V4.2a1.4 1.4 0 0 1 1.4-1.4z"/><path d="M11.4 2.9v3.7h3.7"/><path d="M6.8 13.6c1.8-.6 4.4-2.6 5-5.2.3-1.2-1-1.3-1.1-.2-.2 2 1.6 4.7 3.1 4.7"/>',
  link: '<path d="M8.6 11.4a3 3 0 0 0 4.2 0l2.5-2.5a3 3 0 0 0-4.2-4.2l-.8.8"/><path d="M11.4 8.6a3 3 0 0 0-4.2 0l-2.5 2.5a3 3 0 0 0 4.2 4.2l.8-.8"/>',
  chevronRight: '<path d="m8 4.8 5.2 5.2L8 15.2"/>',
  chevronLeft: '<path d="M12 4.8 6.8 10l5.2 5.2"/>',
  chevronDown: '<path d="m4.8 8 5.2 5.2L15.2 8"/>',
  chevronUp: '<path d="m4.8 12 5.2-5.2 5.2 5.2"/>',
  arrowLeft: '<path d="M16 10H4.5M9 5 4 10l5 5"/>',
  arrowUp: '<path d="M10 16V4.5M5 9l5-5 5 5"/>',
  check: '<path d="m4.4 10.4 3.6 3.6 7.6-8"/>',
  checkCircle: '<circle cx="10" cy="10" r="7.2"/><path d="m6.9 10.2 2.2 2.2 4-4.4"/>',
  x: '<path d="m5.4 5.4 9.2 9.2M14.6 5.4l-9.2 9.2"/>',
  xCircle: '<circle cx="10" cy="10" r="7.2"/><path d="m7.6 7.6 4.8 4.8M12.4 7.6l-4.8 4.8"/>',
  pause: '<path d="M7.4 5v10M12.6 5v10"/>',
  play: '<path d="M6.6 4.6v10.8l8.6-5.4z"/>',
  stop: '<rect x="5.2" y="5.2" width="9.6" height="9.6" rx="1.8"/>',
  eye: '<path d="M2.4 10S5.2 4.8 10 4.8s7.6 5.2 7.6 5.2-2.8 5.2-7.6 5.2S2.4 10 2.4 10z"/><circle cx="10" cy="10" r="2.3"/>',
  eyeOff: '<path d="M8.2 5a7.7 7.7 0 0 1 1.8-.2c4.8 0 7.6 5.2 7.6 5.2a13 13 0 0 1-1.9 2.5M12.6 12.6A2.3 2.3 0 0 1 9 9.2M5.7 6.4C3.6 7.9 2.4 10 2.4 10s2.8 5.2 7.6 5.2c1.4 0 2.7-.4 3.8-1M3.4 3.4l13.2 13.2"/>',
  reveal: '<path d="M11.2 3.4h5.4v5.4"/><path d="m16.4 3.6-7 7"/><path d="M14.4 12v2.8a1.8 1.8 0 0 1-1.8 1.8H5.2a1.8 1.8 0 0 1-1.8-1.8V7.4a1.8 1.8 0 0 1 1.8-1.8H8"/>',
  lock: '<rect x="4.4" y="8.6" width="11.2" height="8.4" rx="2.2"/><path d="M6.9 8.6V6.4a3.1 3.1 0 0 1 6.2 0v2.2"/><path d="M10 12v1.6"/>',
  unlock: '<rect x="4.4" y="8.6" width="11.2" height="8.4" rx="2.2"/><path d="M6.9 8.6V6.4a3.1 3.1 0 0 1 6-1.1"/><path d="M10 12v1.6"/>',
  shieldLock: '<path d="M10 2.6 4.3 4.7v4.7c0 3.5 2.4 6.2 5.7 7.6 3.3-1.4 5.7-4.1 5.7-7.6V4.7z"/><rect x="7.6" y="9.4" width="4.8" height="3.8" rx=".9"/><path d="M8.6 9.4V8.3a1.4 1.4 0 0 1 2.8 0v1.1"/>',
  trash: '<path d="M3.4 5.6h13.2M8 5.6V4.1a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v1.5M5 5.6l.8 10.3a1.5 1.5 0 0 0 1.5 1.4h5.4a1.5 1.5 0 0 0 1.5-1.4L15 5.6"/><path d="M8.4 9v5M11.6 9v5"/>',
  info: '<circle cx="10" cy="10" r="7.2"/><path d="M10 9v4.6"/><circle cx="10" cy="6.6" r=".55" fill="currentColor"/>',
  warning: '<path d="M8.6 3.6a1.6 1.6 0 0 1 2.8 0l6.1 10.7a1.6 1.6 0 0 1-1.4 2.4H3.9a1.6 1.6 0 0 1-1.4-2.4z"/><path d="M10 7.8v3.6"/><circle cx="10" cy="13.9" r=".55" fill="currentColor"/>',
  error: '<circle cx="10" cy="10" r="7.2"/><path d="M10 6.2v4.6"/><circle cx="10" cy="13.4" r=".55" fill="currentColor"/>',
  search: '<circle cx="8.8" cy="8.8" r="5.6"/><path d="m13 13 4 4"/>',
  plus: '<path d="M10 4.4v11.2M4.4 10h11.2"/>',
  minus: '<path d="M4.4 10h11.2"/>',
  refresh: '<path d="M16.2 10a6.2 6.2 0 1 1-1.9-4.5"/><path d="M16.4 3.4v3.4H13"/>',
  sliders: '<path d="M3.5 6.2h8M15.5 6.2h1M3.5 13.8h1M8.5 13.8h8"/><circle cx="13.5" cy="6.2" r="2"/><circle cx="6.5" cy="13.8" r="2"/>',
  memory: '<rect x="2.6" y="5.6" width="14.8" height="8.4" rx="1.6"/><path d="M5.8 8.4v2.8M8.6 8.4v2.8M11.4 8.4v2.8M14.2 8.4v2.8M5 14v2M8.3 14v2M11.7 14v2M15 14v2"/>',
  cpu: '<rect x="5.4" y="5.4" width="9.2" height="9.2" rx="1.8"/><rect x="8.2" y="8.2" width="3.6" height="3.6" rx=".6"/><path d="M8 2.6v2.8M12 2.6v2.8M8 14.6v2.8M12 14.6v2.8M2.6 8h2.8M2.6 12h2.8M14.6 8h2.8M14.6 12h2.8"/>',
  bolt: '<path d="M11.2 2.6 4.6 11h5l-.9 6.4L15.4 9h-5z"/>',
  clone: '<rect x="2.8" y="2.8" width="9.4" height="9.4" rx="2"/><rect x="7.8" y="7.8" width="9.4" height="9.4" rx="2"/><path d="M10.4 12.6h4.4M12.6 10.4v4.4"/>',
  filter: '<path d="M3.4 4.4h13.2l-5.1 6v4.9l-3 1.4v-6.3z"/>',
  download: '<path d="M10 3v9.2M6 8.6l4 4 4-4"/><path d="M3.6 13.6v1.6a1.8 1.8 0 0 0 1.8 1.8h9.2a1.8 1.8 0 0 0 1.8-1.8v-1.6"/>',
  sparkle: '<path d="M10 2.6 11.6 8.4 17.4 10l-5.8 1.6L10 17.4l-1.6-5.8L2.6 10l5.8-1.6z"/>',
  clock: '<circle cx="10" cy="10" r="7.2"/><path d="M10 5.8V10l2.8 1.8"/>',
  more: '<circle cx="5" cy="10" r=".9" fill="currentColor"/><circle cx="10" cy="10" r=".9" fill="currentColor"/><circle cx="15" cy="10" r=".9" fill="currentColor"/>',
  shrink: '<path d="M8.2 3.4v4.8H3.4M11.8 16.6v-4.8h4.8M8 8 3.2 3.2M12 12l4.8 4.8"/>',
  grid: '<rect x="3" y="3" width="6" height="6" rx="1.4"/><rect x="11" y="3" width="6" height="6" rx="1.4"/><rect x="3" y="11" width="6" height="6" rx="1.4"/><rect x="11" y="11" width="6" height="6" rx="1.4"/>',
  list: '<path d="M7 5.5h10M7 10h10M7 14.5h10"/><circle cx="3.8" cy="5.5" r=".7" fill="currentColor"/><circle cx="3.8" cy="10" r=".7" fill="currentColor"/><circle cx="3.8" cy="14.5" r=".7" fill="currentColor"/>',
  layers: '<path d="m10 3 7.2 3.8L10 10.6 2.8 6.8z"/><path d="m2.8 10.2 7.2 3.8 7.2-3.8M2.8 13.4l7.2 3.8 7.2-3.8"/>',
  copy: '<rect x="6.6" y="6.6" width="10.2" height="10.2" rx="2"/><path d="M13.4 6.6V5a1.8 1.8 0 0 0-1.8-1.8H5a1.8 1.8 0 0 0-1.8 1.8v6.6A1.8 1.8 0 0 0 5 13.4h1.6"/>',
  wand: '<path d="m3.4 16.6 9.4-9.4M11.2 5.6l1.6-1.6 3.2 3.2-1.6 1.6z"/><path d="M5.6 3.2v2.4M4.4 4.4h2.4M15.6 12.8v2.4M14.4 14h2.4"/>',
  quit: '<path d="M10 3.4v6"/><path d="M6 5.6a6.2 6.2 0 1 0 8 0"/>',
  heart: '<path d="M10 16.4S3.2 12.4 3.2 7.6A3.6 3.6 0 0 1 10 6a3.6 3.6 0 0 1 6.8 1.6c0 4.8-6.8 8.8-6.8 8.8z"/>',
  photo: '<rect x="2.8" y="3.8" width="14.4" height="12.4" rx="2.2"/><circle cx="7.4" cy="8" r="1.4"/><path d="m3.3 14.6 4.1-3.8 3 2.8 2.2-2 4.1 3.6"/>',
  type: '<path d="M4 5.4V4h12v1.4M10 4v12M7.6 16h4.8"/>',
  scan: '<path d="M3.4 7V5.2a1.8 1.8 0 0 1 1.8-1.8H7M13 3.4h1.8a1.8 1.8 0 0 1 1.8 1.8V7M16.6 13v1.8a1.8 1.8 0 0 1-1.8 1.8H13M7 16.6H5.2a1.8 1.8 0 0 1-1.8-1.8V13M3.4 10h13.2"/>',
};

const cache = new Map();

export function icon(name, { size, cls, title } = {}) {
  const body = P[name] || P.file;
  let tpl = cache.get(name);
  if (!tpl) {
    tpl = document.createElement('template');
    tpl.innerHTML = `<svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
    cache.set(name, tpl);
  }
  const svg = tpl.content.firstChild.cloneNode(true);
  if (size) { svg.setAttribute('width', size); svg.setAttribute('height', size); svg.style.width = svg.style.height = size + 'px'; }
  if (cls) svg.classList.add(...cls.split(' '));
  if (title) { svg.removeAttribute('aria-hidden'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', title); }
  return svg;
}

const KIND_ICON = { dir: 'folder', image: 'image', video: 'video', audio: 'audio', pdf: 'pdf', archive: 'archive', doc: 'doc', text: 'doc', link: 'link', file: 'file', other: 'file' };
export const kindIcon = k => KIND_ICON[k] || 'file';

/** A colour per file kind so lists stay scannable without being loud. */
export const KIND_TONE = { dir: 'var(--accent-ink)', image: '#E58FB0', video: '#9C8CF0', audio: '#E8A456', pdf: '#E66F5C', archive: '#9AA6A0', doc: '#5C9CE6', text: '#7FB3A3', file: 'var(--text-3)', link: 'var(--text-3)', other: 'var(--text-3)' };

export function driveIcon(d) {
  if (!d) return 'drive';
  if (/\bsd\b|sd ?card|\bcard\b|mmc/i.test(`${d.protocol || ''} ${d.name || ''} ${d.model || ''}`)) return 'sdcard';
  if (d.removable || d.internal === false || /usb|thunderbolt/i.test(d.protocol || '')) return 'driveExt';
  return 'drive';
}
