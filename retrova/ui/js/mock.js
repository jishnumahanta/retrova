// A fake Retrova engine for building and screenshotting the UI without the backend.
// Enabled with ?mock=1. Extra switches (all optional):
//   &fresh=1           start with no job history
//   &admin=1           pretend disk access is already unlocked
//   &platform=windows  pretend to be on Windows
//   &run=copy,verify   start these jobs right away (copy, image, carve, verify, analyse, dupes, clean_scan, ocr)
//   &native=1          pretend the native folder dialog exists (returns a fixed folder)
import { ApiError } from './api.js';

const Q = new URLSearchParams(location.search);
const FRESH = Q.get('fresh') === '1';
const ADMIN = Q.get('admin') === '1';
const PLATFORM = Q.get('platform') || 'mac';
const WIN = PLATFORM === 'windows';
const HOME = WIN ? 'C:\\Users\\Sam' : '/Users/sam';
const S = WIN ? '\\' : '/';
const KB = 1e3, MB = 1e6, GB = 1e9, TB = 1e12;
const now = () => Date.now();
const DAY = 86400e3;

// ------------------------------------------------------------------ random helpers
function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
function rng(seed) { let a = typeof seed === 'string' ? hash(seed) : seed; return () => { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
const pick = (r, arr) => arr[Math.floor(r() * arr.length)];
const clone = o => JSON.parse(JSON.stringify(o));
const jp = (...p) => p.join(S).replace(/[\\/]{2,}/g, S);

// ------------------------------------------------------------------ fake images
const PALETTES = [
  ['#9cc9d9', '#e8f1f2', '#5d7a85', '#2f4650', '#1b2a30'],   // Iceland
  ['#f7a072', '#ffd6a5', '#9b5d73', '#4f3a5c', '#2a1f33'],   // sunset
  ['#a8d5ba', '#e9f5db', '#5f8d4e', '#2e5339', '#18301f'],   // forest
  ['#7ec8e3', '#fff3d6', '#e3c58d', '#c9a66b', '#3f88a6'],   // beach
  ['#1d2b53', '#3d4d82', '#7e5a9b', '#25204a', '#0f0e24'],   // night
  ['#f2d0a9', '#f7e7ce', '#c97b63', '#8a4f3d', '#4a2c22'],   // desert
];
function photoSvg(seed, w = 400, h = 300) {
  const r = rng(seed);
  const p = PALETTES[Math.floor(r() * PALETTES.length)];
  const m1 = Array.from({ length: 7 }, (_, i) => `${(i / 6) * w},${h * (0.42 + r() * 0.2)}`).join(' ');
  const m2 = Array.from({ length: 6 }, (_, i) => `${(i / 5) * w},${h * (0.58 + r() * 0.16)}`).join(' ');
  const sun = r() > 0.4 ? `<circle cx="${w * (0.2 + r() * 0.6)}" cy="${h * (0.18 + r() * 0.15)}" r="${h * 0.07}" fill="#fff" opacity=".75"/>` : '';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"><defs><linearGradient id="s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${p[0]}"/><stop offset="1" stop-color="${p[1]}"/></linearGradient></defs><rect width="${w}" height="${h}" fill="url(#s)"/>${sun}<polygon points="0,${h} ${m1} ${w},${h}" fill="${p[2]}"/><polygon points="0,${h} ${m2} ${w},${h}" fill="${p[3]}"/><rect y="${h * 0.86}" width="${w}" height="${h * 0.14}" fill="${p[4]}" opacity=".85"/></svg>`;
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}
function docSvg(seed, title = '', w = 300, h = 380) {
  const r = rng(seed);
  let lines = '';
  let y = 92;
  while (y < h - 30) { const lw = 0.45 + r() * 0.45; lines += `<rect x="28" y="${y}" width="${(w - 56) * lw}" height="7" rx="3.5" fill="#c9d1cd"/>`; y += 16 + (r() > 0.85 ? 14 : 0); }
  const t = title.replace(/[<&>]/g, '').slice(0, 22);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"><rect width="${w}" height="${h}" fill="#fbfbf8"/><rect x="28" y="34" width="${w * 0.45}" height="14" rx="4" fill="#3c4a44"/><text x="28" y="72" font-family="-apple-system, Segoe UI, sans-serif" font-size="13" fill="#6b7872">${t}</text>${lines}</svg>`;
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}
function shotSvg(seed, w = 420, h = 270) {
  const r = rng(seed);
  const acc = pick(r, ['#2BD9A0', '#5C9CE6', '#E58FB0', '#E8A456']);
  let blocks = '';
  for (let i = 0; i < 5; i++) blocks += `<rect x="${130}" y="${46 + i * 40}" width="${180 + r() * 90}" height="12" rx="6" fill="#d7dcd9"/><rect x="${130}" y="${64 + i * 40}" width="${90 + r() * 80}" height="8" rx="4" fill="#e6e9e7"/>`;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"><rect width="${w}" height="${h}" fill="#f4f5f3"/><rect width="${w}" height="26" fill="#e3e6e4"/><circle cx="16" cy="13" r="5" fill="#ff6b5e"/><circle cx="32" cy="13" r="5" fill="#f5bf4f"/><circle cx="48" cy="13" r="5" fill="#61c554"/><rect x="0" y="26" width="110" height="${h}" fill="#e9ecea"/><rect x="14" y="44" width="70" height="9" rx="4.5" fill="${acc}"/><rect x="14" y="64" width="60" height="8" rx="4" fill="#cfd5d2"/><rect x="14" y="82" width="74" height="8" rx="4" fill="#cfd5d2"/>${blocks}</svg>`;
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}
function thumbFor(path) {
  const n = path.toLowerCase();
  if (/screenshot|screen shot|bildschirmfoto/.test(n)) return shotSvg(path);
  if (/\.(pdf|docx?|txt|md|pages|rtf|xlsx?|pptx?)$/.test(n) || /scan|receipt|invoice|lease|boarding|whiteboard|recipe|prescription/.test(n)) return docSvg(path, path.split(/[\\/]/).pop());
  return photoSvg(path);
}

export function mediaUrl(path, q = {}) {
  const p = q.path || '';
  if (path === '/api/recover/verify_csv') return 'data:text/csv;charset=utf-8,' + encodeURIComponent('status,label,path,detail\nMISSING,Missing in copy,/Users/sam/Pictures/IMG_2041.HEIC,Not in the copy\n');
  const e = (p.split('.').pop() || '').toLowerCase();
  if (/^(txt|md|json|js|py|csv|log|ini|yml|yaml|toml|html|css|c|h|cpp|sh)$/.test(e) || p.endsWith('/todo.txt')) {
    return 'data:text/plain;charset=utf-8,' + encodeURIComponent(textFor(p));
  }
  return thumbFor(p);
}
function textFor(p) {
  if (p.endsWith('package.json')) return JSON.stringify({ name: 'webapp', version: '2.4.1', private: true, scripts: { dev: 'next dev', build: 'next build' }, dependencies: { next: '14.2.3', react: '18.3.1' } }, null, 2);
  if (p.endsWith('.md')) return '# Notes\n\n- Back up the portable SSD before the trip\n- Renew passport (expires in June)\n- Ask Priya about the lease renewal\n';
  return 'Buy oat milk\nCall the dentist\nSend invoice #0423 to Martin\nWater the fern\n';
}

// ------------------------------------------------------------------ fake recover volume
function F(name, size, mtime) { return { name, kind: 'file', size, mtime }; }
function D(name, children) { return { name, kind: 'dir', children }; }
const T0 = now() - 40 * DAY;
function photos(prefix, n, avg, ext, seed) {
  return () => { const r = rng(seed); return Array.from({ length: n }, (_, i) => F(`${prefix}${String(1000 + i * 3 + Math.floor(r() * 3)).padStart(4, '0')}.${ext}`, Math.round(avg * (0.5 + r())), T0 - r() * 300 * DAY)); };
}
function blob(n, avg, seed, names = ['index.js', 'package.json', 'README.md', 'LICENSE', 'dist', 'lib', 'cjs']) {
  return () => { const r = rng(seed); return Array.from({ length: n }, (_, i) => i % 4 === 0 && n > 4 ? D(`${pick(r, ['@babel', 'react', 'lodash', 'next', 'webpack', 'postcss', 'eslint', 'typescript', 'zod', 'sharp', 'esbuild', 'chalk'])}${i ? '-' + i : ''}`, blob(Math.max(2, Math.floor(n / 6)), avg, seed + i, names)) : F(`${pick(r, names)}${i > 6 ? '.' + i : ''}`, Math.round(avg * (0.2 + r() * 1.6)), T0 - r() * 90 * DAY)); };
}
const VOLUME = D('', [
  D('Users', [D('sam', [
    D('Desktop', [F('Screenshot 2025-03-02 at 10.14.22.png', 2.1 * MB, T0), F('todo.txt', 1200, T0 + 3 * DAY), F('Invoice-0423.pdf', 240 * KB, T0 - 2 * DAY), F('Trip budget.numbers', 380 * KB, T0 - 9 * DAY)]),
    D('Documents', [F('Thesis final v7.docx', 4.2 * MB, T0 - 120 * DAY), F('Taxes 2025.pdf', 1.1 * MB, T0 - 30 * DAY), F('Lease agreement.pdf', 830 * KB, T0 - 200 * DAY), F('notes.md', 14 * KB, T0), D('Scans', photos('Scan ', 42, 1.4 * MB, 'pdf', 7)), D('Recipes', photos('Recipe ', 18, 120 * KB, 'pages', 8))]),
    D('Pictures', [D('2024 Iceland', photos('IMG_', 240, 3.4 * MB, 'jpg', 1)), D('Family', photos('IMG_', 180, 2.6 * MB, 'HEIC', 2)), D('Screenshots', photos('Screenshot ', 64, 1.8 * MB, 'png', 3)), F('Profile photo.jpg', 1.2 * MB, T0)]),
    D('Movies', [F('Wedding highlights.mov', 4.8 * GB, T0 - 400 * DAY), F('Drone - Vik beach.mp4', 1.9 * GB, T0 - 180 * DAY), D('Final Cut Projects', [F('Iceland edit.fcpbundle', 12.4 * GB, T0 - 170 * DAY)])]),
    D('Music', [D('Logic', [F('Demo 3 - piano.logicx', 840 * MB, T0 - 60 * DAY), F('Late night sketch.logicx', 420 * MB, T0 - 20 * DAY)])]),
    D('Projects', [
      D('webapp', [F('package.json', 2 * KB, T0), F('package-lock.json', 480 * KB, T0), F('next.config.js', 1 * KB, T0), D('node_modules', blob(120, 90 * KB, 11)), D('.next', blob(30, 1.2 * MB, 12, ['cache', 'server', 'static', 'build-manifest.json'])), D('src', [D('pages', blob(14, 6 * KB, 13, ['index.tsx', 'about.tsx', 'api.ts'])), D('components', blob(22, 4 * KB, 14, ['Button.tsx', 'Header.tsx', 'Card.tsx']))]), F('README.md', 4 * KB, T0)]),
      D('firmware-esp32', [F('platformio.ini', 1 * KB, T0), D('.pio', blob(40, 2.4 * MB, 21, ['firmware.bin', 'libdeps', 'build'])), D('src', [F('main.cpp', 18 * KB, T0), F('wifi.cpp', 6 * KB, T0)]), D('include', [F('config.h', 2 * KB, T0)])]),
      D('ml-notebooks', [F('requirements.txt', 600, T0), D('venv', blob(60, 4 * MB, 31, ['torch', 'numpy', 'site-packages', 'pip'])), D('data', [F('train.parquet', 2.4 * GB, T0), F('test.parquet', 610 * MB, T0)]), D('notebooks', [F('explore.ipynb', 2.2 * MB, T0), F('model.ipynb', 4.1 * MB, T0)])]),
      D('android-app', [F('build.gradle', 3 * KB, T0), F('settings.gradle', 1 * KB, T0), D('build', blob(30, 3 * MB, 41, ['outputs', 'intermediates', 'tmp'])), D('.gradle', blob(20, 1.6 * MB, 42, ['caches', '8.5'])), D('app', [D('src', blob(16, 8 * KB, 43, ['MainActivity.kt', 'App.kt']))])]),
    ]),
    D('Library', [D('Application Support', blob(40, 8 * MB, 51, ['Code', 'Slack', 'Logic', 'com.apple.sharedfilelist'])), D('Mail', blob(30, 20 * MB, 52, ['V10', 'MailData']))]),
    D('.Trash', [F('old resume.pdf', 120 * KB, T0)]),
    F('.zshrc', 3 * KB, T0),
  ])]),
  D('Applications', [D('Logic Pro.app', blob(10, 400 * MB, 61, ['Contents'])), D('Visual Studio Code.app', blob(10, 40 * MB, 62, ['Contents']))]),
  D('Shared', [F('Shared notes.txt', 3 * KB, T0)]),
]);
// files a deleted-file scan would find (NTFS / FAT / exFAT drives): original names and folders, with how likely the data is intact
function X(name, size, mtime, recover, note = '') { return { ...F(name, size, mtime), deleted: true, recover, note }; }
const SCAN_NOTE_FRAG = 'Found as one unbroken stretch of the disk. If the file was split into pieces, the end may be wrong.';
const SCAN_NOTE_OVER = 'Some of the space this file used has been taken by other files since.';
const DELETED_ROOT = 'Deleted files';
const DELETED = D(DELETED_ROOT, [
  D('Users', [D('sam', [
    D('Documents', [X('Thesis final v6.docx', 3.9 * MB, T0 - 130 * DAY, 'good'), X('Old budget.xlsx', 96 * KB, T0 - 75 * DAY, 'good'), X('Contract draft.pdf', 610 * KB, T0 - 40 * DAY, 'overwritten', SCAN_NOTE_OVER)]),
    D('Pictures', [D('Family', [X('IMG_2310.jpg', 3.1 * MB, T0 - 60 * DAY, 'good'), X('IMG_2311.jpg', 2.8 * MB, T0 - 60 * DAY, 'good'), X('IMG_2312.jpg', 3.3 * MB, T0 - 60 * DAY, 'incomplete', SCAN_NOTE_FRAG)]), X('Passport scan.jpg', 1.4 * MB, T0 - 300 * DAY, 'good')]),
    D('Desktop', [X('Receipts.zip', 18 * MB, T0 - 20 * DAY, 'incomplete', SCAN_NOTE_FRAG)]),
  ])]),
  D('Lost folders', [D('Folder 1204', [X('draft-notes.txt', 8 * KB, T0 - 90 * DAY, 'good'), X('presentation.key', 24 * MB, T0 - 90 * DAY, 'overwritten', SCAN_NOTE_OVER)])]),
]);
function kids(node) {
  if (typeof node.children === 'function') node.children = node.children();
  return node.children || [];
}
function findNode(path) {
  const parts = path.split('/').filter(Boolean);
  let n = VOLUME;
  if (parts[0] === DELETED_ROOT) { n = DELETED; parts.shift(); }
  for (const p of parts) { n = kids(n).find(c => c.name === p); if (!n) return null; }
  return n;
}
const sizeMemo = new Map();
function treeSize(node, path) {
  if (node.kind === 'file') return { bytes: node.size, files: 1 };
  if (sizeMemo.has(path)) return sizeMemo.get(path);
  let bytes = 0, files = 0;
  for (const c of kids(node)) { const s = treeSize(c, path.replace(/\/$/, '') + '/' + c.name); bytes += s.bytes; files += s.files; }
  const r = { bytes, files };
  sizeMemo.set(path, r);
  return r;
}

// ------------------------------------------------------------------ fake local file system (destination pickers)
const LOCAL = {
  [HOME]: ['Desktop', 'Documents', 'Downloads', 'Movies', 'Music', 'Pictures', 'Projects', 'Virtual Machines.localized'],
  [WIN ? 'E:\\' : '/Volumes/Backup Drive']: ['Recovered', 'Time Machine Backups', 'Photos archive', 'Projects backup'],
  [WIN ? 'C:\\' : '/']: WIN ? ['Users', 'Program Files', 'Windows'] : ['Applications', 'Library', 'System', 'Users', 'Volumes'],
  [WIN ? 'C:\\Users' : '/Volumes']: WIN ? ['Sam', 'Public'] : ['Backup Drive', 'Macintosh HD'],
};
function localLs(path) {
  const p = path || HOME;
  const names = LOCAL[p] || (() => { const r = rng(p); return Array.from({ length: 4 + Math.floor(r() * 6) }, () => pick(r, ['2023', '2024', 'Archive', 'Clients', 'Drafts', 'Exports', 'Photos', 'Scans', 'Work', 'Old stuff', 'Taxes', 'Travel'])).filter((v, i, a) => a.indexOf(v) === i); })();
  const r = rng(p + 'files');
  const files = p === HOME ? [] : Array.from({ length: Math.floor(r() * 5) }, (_, i) => pick(r, ['notes.txt', 'Budget.xlsx', 'disk-backup.img', 'Old Mac.dmg', 'photo.jpg', 'Report.pdf']) + (i ? ` ${i}` : '')).map(n => n.replace(/ (\d+)$/, '')).filter((v, i, a) => a.indexOf(v) === i);
  const parent = p === '/' || /^[A-Z]:\\$/.test(p) ? null : (p.slice(0, Math.max(p.lastIndexOf(S), WIN ? 3 : 1)) || (WIN ? p.slice(0, 3) : '/'));
  return {
    path: p, parent,
    entries: [...names.map(n => ({ name: n, path: jp(p, n), kind: 'dir', size: 0, mtime: now() - r() * 90 * DAY })),
      ...files.map(n => ({ name: n, path: jp(p, n), kind: 'file', size: Math.round(r() * 4 * GB), mtime: now() - r() * 90 * DAY }))],
  };
}

// ------------------------------------------------------------------ drives
const DRIVES = {
  disks: [
    { id: 'disk0', name: WIN ? 'Local Disk' : 'Macintosh HD', model: WIN ? 'NVMe SSD 1TB' : 'APPLE SSD AP1024Z', device: WIN ? '\\\\.\\PhysicalDrive0' : '/dev/disk0', size: 1000 * GB, internal: true, removable: false, protocol: WIN ? 'NVMe' : 'Apple Fabric', health: { state: 'ok', detail: 'SMART status: verified' },
      partitions: [{ id: 'disk3s1', device: '/dev/disk3s1', offset: 0, size: 994 * GB, fs: WIN ? 'NTFS' : 'APFS', label: WIN ? 'Windows' : 'Macintosh HD', mount: WIN ? 'C:\\' : '/', free: 212 * GB }] },
    { id: 'disk4', name: 'Portable SSD', model: 'Portable SSD', device: WIN ? '\\\\.\\PhysicalDrive2' : '/dev/disk4', size: 2000 * GB, internal: false, removable: true, protocol: 'USB', health: { state: 'warn', detail: `${WIN ? 'Windows' : 'macOS'} can't mount the volume on this drive` },
      partitions: [{ id: 'disk4s2', device: '/dev/disk4s2', offset: 209735680, size: 1999.8 * GB, fs: 'APFS', label: 'MacStorage', mount: null, free: null }] },
    { id: 'disk5', name: 'Backup Drive', model: 'USB HDD 4TB', device: WIN ? '\\\\.\\PhysicalDrive3' : '/dev/disk5', size: 4000 * GB, internal: false, removable: true, protocol: 'USB', health: { state: 'ok', detail: 'No problems reported' },
      partitions: [{ id: 'disk5s2', device: '/dev/disk5s2', offset: 0, size: 4000 * GB, fs: 'exFAT', label: 'Backup Drive', mount: WIN ? 'E:\\' : '/Volumes/Backup Drive', free: 2410 * GB }] },
    { id: 'disk6', name: 'SD card', model: 'SDXC card 64GB', device: WIN ? '\\\\.\\PhysicalDrive4' : '/dev/disk6', size: 64 * GB, internal: false, removable: true, protocol: 'SD Card', health: { state: 'unknown', detail: "This reader doesn't report drive health" },
      partitions: [{ id: 'disk6s1', device: '/dev/disk6s1', offset: 1048576, size: 63.9 * GB, fs: 'FAT32', label: 'EOS_DIGITAL', mount: WIN ? 'F:\\' : '/Volumes/EOS_DIGITAL', free: 41.2 * GB }] },
  ],
};
DRIVES.volumes = DRIVES.disks.flatMap(d => d.partitions.filter(p => p.mount).map(p => ({ mount: p.mount, label: p.label, fs: p.fs, size: p.size, free: p.free, device: p.device })));

const SRC = (source, offset, label, size, fs, fs_name, disk, detail, extra = {}) => ({ source, offset, label, size, fs, fs_name, disk, disk_id: disk, mount: '', needs_admin: !ADMIN, supported: !!fs, removable: true, health: 'ok', detail, recommended: false, ...extra });
const SOURCES = [
  SRC(WIN ? '\\\\.\\PhysicalDrive2' : '/dev/rdisk4', 209735680, 'MacStorage', 1999.8 * GB, 'apfs', 'APFS', 'Portable SSD', `Your computer hasn't opened this one - Retrova can read it directly. It looks damaged.`, { recommended: true, damaged: true, health: 'warn', volumes: ['MacStorage', 'MacStorage - Data'] }),
  SRC(WIN ? '\\\\.\\PhysicalDrive4' : '/dev/rdisk6', 1048576, 'EOS_DIGITAL', 63.9 * GB, 'fat32', 'FAT32', 'SD card', 'Opened by your computer at ' + (WIN ? 'F:\\' : '/Volumes/EOS_DIGITAL') + ' - you can also copy files from it normally.', { mount: WIN ? 'F:\\' : '/Volumes/EOS_DIGITAL', health: 'unknown' }),
  SRC(WIN ? '\\\\.\\PhysicalDrive3' : '/dev/rdisk5', 0, 'Backup Drive', 4000 * GB, 'exfat', 'exFAT', 'Backup Drive', 'Opened by your computer at ' + (WIN ? 'E:\\' : '/Volumes/Backup Drive') + ' - you can also copy files from it normally.', { mount: WIN ? 'E:\\' : '/Volumes/Backup Drive' }),
  SRC(WIN ? '\\\\.\\PhysicalDrive2' : '/dev/rdisk4', 2000 * GB, '', 120 * GB, null, 'Unknown', 'Portable SSD', "No file system Retrova recognises. A deep scan can still find files by their contents.", { health: 'warn' }),
  SRC(WIN ? '\\\\.\\PhysicalDrive5' : '/dev/rdisk7', 1048576, '', 238 * GB, 'bitlocker', 'BitLocker (encrypted)', 'USB stick', 'Locked with BitLocker. Retrova can open it if you have its password or 48-digit recovery key.', { locked: true, recommended: true, health: 'ok' }),
  SRC(WIN ? '\\\\.\\PhysicalDrive6' : '/dev/rdisk8', 0, '', 1000 * GB, 'luks', 'LUKS (encrypted)', 'External HDD', 'Locked with LUKS (Linux disk encryption). Retrova can open it if you have its passphrase.', { locked: true, health: 'ok' }),
  SRC(WIN ? '\\\\.\\PhysicalDrive0' : '/dev/rdisk0', 40960, WIN ? 'Windows' : 'Macintosh HD', 994 * GB, WIN ? 'ntfs' : 'apfs', WIN ? 'NTFS' : 'APFS', WIN ? 'Local Disk' : 'Macintosh HD', 'Opened by your computer at ' + (WIN ? 'C:\\' : '/') + ' - you can also copy files from it normally.', { mount: WIN ? 'C:\\' : '/', removable: false }),
];
const findSource = b => SOURCES.find(s => s.source === b.source && (b.offset === undefined || b.offset === null || s.offset === b.offset)) || (SOURCES.find(s => s.source === b.source && b.volume && (s.volumes || []).includes(b.volume)));

// ------------------------------------------------------------------ jobs
const jobs = new Map();
let jobSeq = 0;
const LIVE = new Set(['queued', 'running', 'paused', 'stopping']);

class Job {
  constructor(kind, title, params, duration, extra = {}) {
    this.id = `m${(++jobSeq).toString(36)}${Math.floor(Math.random() * 1e6).toString(36)}`;
    this.kind = kind; this.title = title; this.params = params;
    this.duration = duration; this.state = 'running';
    this.t = 0; this.last = now(); this.t0 = now(); this.tEnd = null;
    this.error = ''; this.message = '';
    Object.assign(this, extra);
    jobs.set(this.id, this);
  }
  tick() {
    const n = now();
    if (this.state === 'running') this.t += (n - this.last) / 1000;
    this.last = n;
    if (this.state === 'running' && this.t >= this.duration) { this.t = this.duration; this.finish('done'); }
    if (this.state === 'stopping' && n - this.stopAt > 700) this.finish('stopped');
    if (this.state === 'running' && this.failAt && this.t >= this.failAt) { this.error = this.failMsg; this.finish('error'); }
  }
  finish(state) { this.state = state; this.tEnd = now(); this.onFinish?.(); }
  get p() { return Math.min(1, this.t / this.duration); }
  status() {
    this.tick();
    const d = { id: this.id, kind: this.kind, title: this.title, state: this.state, phase: '', message: this.message, error: this.error, progress: this.p, stats: {}, elapsed: this.tEnd ? (this.tEnd - this.t0) / 1000 : this.t, params: this.params, started: this.t0 / 1000 };
    Object.assign(d, this.describe ? this.describe(this.p, this) : {});
    return d;
  }
  result(q) { return this.results ? this.results(q, this) : {}; }
}
function seeded(job, agoMs, state = 'done') {
  job.t = job.duration; job.state = state; job.t0 = now() - agoMs - job.duration * 1000; job.tEnd = now() - agoMs; job.last = now();
  return job;
}
const getJob = id => { const j = jobs.get(id); if (!j) throw new ApiError(`No such job: ${id}`, 404); return j; };

// --- copy
function copyJob(params) {
  let total = 0, files = 0;
  for (const p of params.paths || []) { const n = findNode(p); if (n) { const s = treeSize(n, p); total += s.bytes; files += s.files; } }
  for (const p of params.excluded || []) { const n = findNode(p); if (n) { const s = treeSize(n, p); total -= s.bytes; files -= s.files; } }
  if (!total) { total = 38.4 * GB; files = 12402; }
  const out = params.out || jp(HOME, 'Recovered');
  const dur = Q.get('fast') ? 12 : 70;
  const j = new Job('copy', `Recovering files from ${params.label || 'MacStorage'}`, params, dur);
  const picks = [];
  for (const p of params.paths || ['/Users/sam/Pictures']) {
    const n = findNode(p);
    const walk = (node, path, depth) => { if (picks.length > 60 || depth > 3) return; if (node.kind === 'file') picks.push({ path, size: node.size }); else for (const c of kids(node).slice(0, 30)) walk(c, path.replace(/\/$/, '') + '/' + c.name, depth + 1); };
    if (n) walk(n, p, 0);
  }
  if (!picks.length) picks.push({ path: '/Users/sam/Pictures/2024 Iceland/IMG_1043.jpg', size: 3.4 * MB });
  j.describe = (p, jb) => {
    const live = LIVE.has(jb.state);
    const bd = Math.round(total * p), fd = Math.round(files * p);
    const sp = live && jb.state === 'running' ? 88 * MB + Math.sin(now() / 900) * 14 * MB : 0;
    const k = Math.floor(now() / 700);
    const cur = live && jb.state !== 'stopping' ? [0, 1, 2, 3].map(i => { const f = picks[(k + i * 7) % picks.length]; return { path: f.path, size: f.size, written: Math.round(f.size * (((k * 37 + i * 23) % 100) / 100)) }; }) : [];
    const failed = p > 0.35 ? (p > 0.7 ? 3 : 2) : 0;
    return {
      phase: jb.state === 'done' ? 'Finished' : p < 0.03 ? 'Listing files…' : 'Copying files',
      stats: { files_done: fd, files_total: files, bytes_done: bd, bytes_total: total, speed: sp, eta: sp ? (total - bd) / sp : null, skipped: Math.round(fd * 0.01), failed },
      copied: fd - failed - Math.round(fd * 0.01), skipped: Math.round(fd * 0.01), failed, renamed: 2, current: cur, free: 2410 * GB - bd, out_root: out,
      errors: [
        { path: '/Users/sam/Pictures/Family/IMG_1187.HEIC', msg: 'Input/output error - 4 KB of this file could not be read; the readable parts were saved.' },
        { path: '/Users/sam/Library/Mail/V10/MailData/Envelope Index-wal', msg: 'Checksum mismatch in the file system record' },
        { path: '/Users/sam/Movies/Final Cut Projects/Iceland edit.fcpbundle/Render Files/clip-0042.mov', msg: 'Input/output error' },
      ].slice(0, failed),
      stop_reason: jb.state === 'stopped' ? 'You stopped the recovery. Finished files are kept - press Start again to continue where it left off.' : '',
      walk_done: p > 0.03,
    };
  };
  return j;
}

// --- image
function imageJob(params) {
  const total = params.total || 1999.8 * GB;
  const j = new Job('image', `Creating a disk image of ${params.label || 'MacStorage'}`, params, 90);
  // copy -> trim -> scrape -> retry, like the real job; a little damage is left at the end
  const PH = [[0.55, 'copy', 'Copying - reading the healthy parts first'], [0.75, 'trim', 'Trimming - checking the edges of damaged areas'], [0.92, 'scrape', 'Scraping - reading damaged areas sector by sector'], [1, 'retry', 'Retrying - one more try at the unreadable sectors']];
  j.describe = (p, jb) => {
    const done = jb.state === 'done';
    const [, id, text] = PH.find(x => p < x[0]) || PH[3];
    const sp = jb.state === 'running' ? (id === 'copy' ? 410 * MB : id === 'trim' ? 38 * MB : 0.4 * MB) : 0;
    const bad = done ? 1.5 * MB : p < 0.3 ? 0 : p < 0.75 ? 48 * MB * Math.min(1, (p - 0.3) / 0.2) : p < 0.92 ? 3 * MB : 1.5 * MB;
    const areas = p < 0.3 ? 0 : done ? 2 : p < 0.75 ? 3 : 2;
    const bytesDone = Math.round(total * Math.min(p / 0.55, 1) * 0.98);
    return {
      phase: done ? 'Finished' : text, phase_id: done ? 'done' : id, pass: 1, sha256: '',
      stats: { bytes_done: bytesDone, rescued: bytesDone, bytes_total: total, speed: sp, eta: sp && id === 'copy' ? total * (0.55 - p) / sp : null, zero_bytes: Math.round(total * p * 0.62), bad_bytes: bad, error_areas: areas, failed: areas },
      bad_bytes: bad, error_areas: areas, out: params.out, free: 2410 * GB - total * p * 0.38,
      message: done ? '1.5 MB could not be read and are zeros in the image. They are listed in the log next to it; the .map file lets GNU ddrescue keep trying.' : '',
    };
  };
  return j;
}

// --- carve
const CARVE_NAMES = { jpg: 'jpg', png: 'png', gif: 'gif', heic: 'heic', mp4: 'mp4', mov: 'mov', pdf: 'pdf', zip: 'zip', docx: 'docx', xlsx: 'xlsx', pptx: 'pptx', mp3: 'mp3', wav: 'wav', sqlite: 'sqlite' };
function carveJob(params) {
  const types = params.types?.length ? params.types : ['jpg', 'png', 'heic', 'mp4', 'mov', 'pdf', 'docx'];
  const total = params.total || 63.9 * GB;
  const weights = { jpg: 1200, heic: 640, png: 210, gif: 18, mp4: 46, mov: 31, pdf: 88, docx: 40, xlsx: 22, pptx: 9, zip: 14, mp3: 120, wav: 6, sqlite: 12 };
  const j = new Job('carve', `Deep scan of ${params.label || 'EOS_DIGITAL'}`, params, Q.get('fast') ? 10 : 60);
  const counts = p => Object.fromEntries(types.map(t => [t, Math.round((weights[t] || 10) * p)]));
  j.describe = (p, jb) => {
    const c = counts(p);
    const found = Object.values(c).reduce((a, b) => a + b, 0);
    const sp = jb.state === 'running' ? 260 * MB : 0;
    return { phase: jb.state === 'done' ? 'Scan finished' : 'Reading every block', stats: { bytes_done: Math.round(total * p), bytes_total: total, speed: sp, eta: sp ? total * (1 - p) / sp : null, found }, counts: c, out: params.out };
  };
  j.results = (q, jb) => {
    const c = counts(jb.p);
    const items = [];
    for (const t of types) {
      if (q.type && q.type !== t) continue;
      const r = rng(t);
      for (let i = 0; i < Math.min(c[t], 160); i++) {
        const off = Math.floor(r() * total);
        items.push({ path: jp(params.out || jp(HOME, 'Recovered files'), t, `retrova-${off.toString(16)}.${CARVE_NAMES[t]}`), type: t, size: Math.round((t === 'mp4' || t === 'mov' ? 180 * MB : t === 'jpg' || t === 'heic' ? 3 * MB : 400 * KB) * (0.3 + r())), offset: off });
      }
    }
    return { counts: c, items };
  };
  return j;
}

// --- regen
function regenJob(params) {
  const j = new Job('regen', 'Looking for rebuildable folders', params, 3.5);
  const found = [
    { path: '/Users/sam/Projects/webapp/node_modules', what: 'JavaScript packages', regen: 'npm install  (or yarn / pnpm install)', confidence: 'safe' },
    { path: '/Users/sam/Projects/webapp/.next', what: 'Next.js build cache', regen: 'npm run build', confidence: 'safe' },
    { path: '/Users/sam/Projects/firmware-esp32/.pio', what: 'PlatformIO libraries + build', regen: 'pio run  (re-downloads lib_deps)', confidence: 'safe' },
    { path: '/Users/sam/Projects/android-app/build', what: 'Gradle/Android build output', regen: './gradlew build', confidence: 'safe' },
    { path: '/Users/sam/Projects/android-app/.gradle', what: 'Gradle cache', regen: './gradlew build', confidence: 'safe' },
    { path: '/Users/sam/Projects/ml-notebooks/venv', what: 'Python virtual environment - check nothing was installed by hand', regen: 'python -m venv venv && pip install -r requirements.txt', confidence: 'review' },
  ].filter(f => (params.paths || []).some(p => f.path === p || f.path.startsWith(p.replace(/\/$/, '') + '/')));
  j.describe = (p) => ({ phase: 'Checking project folders', stats: { found: Math.round(found.length * p), dirs: Math.round(4200 * p) } });
  j.results = (q, jb) => ({ found: found.slice(0, Math.round(found.length * jb.p)) });
  return j;
}

// --- verify
const V_LABEL = { OK: 'Matches', MISSING: 'Missing in copy', EXTRA: 'Only in copy', SIZE: 'Size differs', CONTENT: 'Content differs', SRC_BAD: 'Source unreadable', DST_BAD: 'Copy unreadable', LINK: 'Link differs', ERROR: 'Error' };
function verifyJob(params, opts = {}) {
  const mode = params.mode || 'full';
  const totalFiles = 12402, totalBytes = 38.4 * GB;
  const repair = !!opts.repair;
  const j = new Job('verify', repair ? 'Repairing the copy from MacStorage' : 'Verifying the copy', params, repair ? 8 : (mode === 'quick' ? 8 : mode === 'sample' ? 25 : 55));
  const out = params.out || '/Volumes/Backup Drive/Recovered';
  const issues = repair ? [
    { status: 'SRC_BAD', sp: '/Users/sam/Pictures/Family/IMG_1187.HEIC', detail: 'The original has unreadable areas (4 KB at offset 1,204,224); those parts were left as they are in the copy.' },
  ] : [
    { status: 'MISSING', sp: '/Users/sam/Pictures/Family/IMG_1190.HEIC', detail: 'Not in the copy', fixable: true },
    { status: 'MISSING', sp: '/Users/sam/Documents/Scans/Scan 1031.pdf', detail: 'Not in the copy', fixable: true },
    { status: 'MISSING', sp: '/Users/sam/Projects/webapp/src/pages/about.tsx', detail: 'Not in the copy', fixable: true },
    { status: 'MISSING', sp: '/Users/sam/Desktop/Trip budget.numbers', detail: 'Not in the copy', fixable: true },
    { status: 'SIZE', sp: '/Users/sam/Movies/Drone - Vik beach.mp4', detail: 'Original 1.9 GB, copy 1.2 GB - the copy stopped early', fixable: true },
    { status: 'CONTENT', sp: '/Users/sam/Pictures/2024 Iceland/IMG_1043.jpg', detail: '2 areas differ (128 KB at 0x1A000, 64 KB at 0x2F0000)', fixable: true },
    { status: 'CONTENT', sp: '/Users/sam/Documents/Thesis final v7.docx', detail: '1 area differs (4 KB at 0x3E000)', fixable: true },
    { status: 'CONTENT', sp: '/Users/sam/Music/Logic/Demo 3 - piano.logicx', detail: 'Copy is all zeros from 512 MB onward', fixable: true },
    ...Array.from({ length: 6 }, (_, i) => ({ status: 'SRC_BAD', sp: `/Users/sam/Pictures/Family/IMG_${1180 + i * 3}.HEIC`, detail: 'The original drive could not read part of this file (Input/output error).' })),
    { status: 'EXTRA', sp: '', key: 'Retrova-log.txt', detail: 'Only in the copy (written by Retrova)' },
    { status: 'EXTRA', sp: '', key: '.DS_Store', detail: 'Only in the copy' },
  ];
  issues.forEach(i => { i.label = V_LABEL[i.status]; i.key = i.key || i.sp; i.fixable = !!i.fixable; i.src_size = null; i.dst_size = null; });
  const SERIOUS = new Set(['MISSING', 'SIZE', 'CONTENT', 'DST_BAD', 'ERROR']);
  const visible = p => issues.slice(0, Math.ceil(issues.length * Math.min(1, p * 1.15)));
  j.describe = (p, jb) => {
    const vis = visible(p);
    const counts = { OK: Math.round((repair ? 9 : totalFiles - issues.length) * p) };
    for (const i of vis) counts[i.status] = (counts[i.status] || 0) + 1;
    const sp = jb.state === 'running' ? (mode === 'quick' ? 0 : 120 * MB) : 0;
    const fd = Math.round((repair ? 9 : totalFiles) * p), bd = Math.round(totalBytes * p);
    const cur = LIVE.has(jb.state) ? [0, 1].map(i => ({ path: `/Users/sam/Pictures/2024 Iceland/IMG_${1000 + Math.floor(now() / 400 + i * 13) % 700}.jpg`, size: 3.4 * MB, done: Math.round(3.4 * MB * (((now() / 30) + i * 40) % 100) / 100) })) : [];
    return {
      phase: jb.state === 'done' ? 'Finished' : p < 0.05 ? 'Listing files on both sides…' : repair ? 'Copying the parts that differ, then checking again' : 'Comparing',
      stats: { files_done: fd, files_total: repair ? 9 : totalFiles, bytes_done: mode === 'quick' ? undefined : bd, bytes_total: mode === 'quick' ? undefined : totalBytes, speed: sp, eta: sp ? (totalBytes - bd) / sp : (jb.state === 'running' ? (1 - p) * jb.duration : null) },
      mode, repair, counts, problems: vis.filter(i => SERIOUS.has(i.status)).length, fixable: jb.state === 'done' ? vis.filter(i => i.fixable).length : 0,
      issue_count: vis.length, cached: 0, repaired: repair && jb.state === 'done' ? 8 : 0, current: cur,
      disk_images: p > 0.6 && !repair ? [
        { path: '/Users/sam/Virtual Machines.localized/Windows 11.vmwarevm/Virtual Disk.vmdk', verdict: 'ok', detail: 'VMDK header and grain tables look intact' },
        { path: '/Users/sam/Projects/firmware-esp32/.pio/build/flash.img', verdict: 'warn', detail: 'Starts with zeros. The original starts exactly the same way, so this is how the disk was.' },
      ] : [],
      out_root: out, report_dir: jp(out, 'Retrova verify'), report: jb.state === 'done' ? jp(out, 'Retrova verify', 'report.txt') : '',
    };
  };
  j.results = (q, jb) => {
    let list = visible(jb.p);
    if (q.status) list = list.filter(i => i.status === q.status);
    else list = list.filter(i => i.status !== 'OK');
    if (q.q) list = list.filter(i => (i.sp + i.key).toLowerCase().includes(q.q.toLowerCase()));
    const limit = Number(q.limit) || 300;
    return { items: list.slice(0, limit), more: Math.max(0, list.length - limit), total: list.length };
  };
  return j;
}

// --- analyse
const TOP = WIN
  ? [['AppData', 64 * GB, 210000], ['Videos', 82 * GB, 410], ['Projects', 58 * GB, 412000], ['Pictures', 47 * GB, 18400], ['Virtual Machines', 96 * GB, 24], ['Downloads', 31 * GB, 640], ['Documents', 12 * GB, 4200], ['Music', 8.4 * GB, 2100], ['Desktop', 2.2 * GB, 120]]
  : [['Library', 118 * GB, 410000], ['Movies', 82 * GB, 410], ['Projects', 58 * GB, 412000], ['Pictures', 47 * GB, 18400], ['Virtual Machines.localized', 96 * GB, 24], ['Downloads', 31 * GB, 640], ['Documents', 12 * GB, 4200], ['Music', 8.4 * GB, 2100], ['Desktop', 2.2 * GB, 120], ['.Trash', 3.4 * GB, 210]];
const SUBNAMES = ['Photos Library.photoslibrary', 'Caches', 'Application Support', 'Containers', 'Developer', 'Mail', 'Messages', 'Final Cut Projects', 'node_modules', 'Archive', '2023', '2024', 'Exports', 'Raw', 'Backups', 'Logs', 'Docker', 'Xcode', 'iOS DeviceSupport', 'Group Containers', 'Steam', 'Old projects', 'Client work', 'Screenshots', 'Scans', 'Recordings'];
function analyseTree(node, size, files) {
  if (node === HOME || node === '/' || /^[A-Z]:\\$/.test(node)) {
    return TOP.map(([n, s, f]) => ({ name: n, path: jp(node, n), size: s, files: f, kind: 'dir' })).concat([{ name: '.zsh_history', path: jp(node, '.zsh_history'), size: 2 * MB, files: 1, kind: 'file' }]);
  }
  const r = rng(node);
  const n = 6 + Math.floor(r() * 14);
  let remaining = size || 20 * GB;
  const out = [];
  for (let i = 0; i < n && remaining > 1e5; i++) {
    const share = i === n - 1 ? remaining : remaining * (0.18 + r() * 0.35);
    remaining -= share;
    const isFile = r() > 0.72;
    const nm = isFile ? pick(r, ['backup.zip', 'export.mov', 'disk.img', 'dataset.parquet', 'archive.tar.gz', 'recording.wav', 'scan.pdf', 'IMG_4402.mov']) : pick(r, SUBNAMES);
    if (out.some(o => o.name === nm)) continue;
    out.push({ name: nm, path: jp(node, nm), size: Math.round(share), files: isFile ? 1 : Math.max(1, Math.round((files || 1000) * share / (size || 1))), kind: isFile ? 'file' : 'dir' });
  }
  return out.sort((a, b) => b.size - a.size);
}
const analyseSizes = new Map();
function analyseJob(params) {
  const root = params.path || HOME;
  const j = new Job('analyse', `Space used in ${root.split(/[\\/]/).filter(Boolean).pop() || root}`, params, Q.get('fast') ? 6 : 18);
  const total = TOP.reduce((a, t) => a + t[1], 0) + 2 * MB;
  j.describe = (p, jb) => ({ phase: jb.state === 'done' ? 'Finished' : p < 0.7 ? 'Measuring folders' : 'Looking for things you can clear', stats: { files_done: Math.round(1068000 * p), dirs: Math.round(184000 * p), bytes_done: Math.round(total * p) } });
  j.results = (q) => {
    const view = q.view || 'summary';
    if (view === 'summary') return {
      total, files: 1068000, dirs: 184000,
      biggest_dirs: TOP.slice(0, 5).map(([n, s]) => ({ path: jp(root, n), size: s })),
      types: [{ type: 'Videos', bytes: 104 * GB, files: 1240 }, { type: 'Disk images & VMs', bytes: 101 * GB, files: 48 }, { type: 'Apps & app data', bytes: 96 * GB, files: 380000 }, { type: 'Photos', bytes: 52 * GB, files: 21400 }, { type: 'Code & packages', bytes: 49 * GB, files: 610000 }, { type: 'Documents', bytes: 14 * GB, files: 8200 }, { type: 'Music & audio', bytes: 9.2 * GB, files: 2600 }, { type: 'Other', bytes: total - 425.2 * GB, files: 44500 }],
      findings: [
        { id: 'sparse', title: 'Virtual machine disks that can shrink', detail: '2 VM disks are mostly empty space that still takes room. Shrinking keeps every byte of what is inside.', bytes: 58.7 * GB, action: 'sparse', paths: [jp(root, WIN ? 'Virtual Machines' : 'Virtual Machines.localized')] },
        { id: 'dupes', title: 'Duplicate files', detail: '1,284 files have an identical copy somewhere else in this folder.', bytes: 14.2 * GB, action: 'dupes', paths: [root] },
        { id: 'installers', title: 'Old installers in Downloads', detail: '23 .dmg and .pkg files you downloaded more than 30 days ago.', bytes: 12.4 * GB, action: 'clean', paths: [jp(root, 'Downloads')] },
        { id: 'regen', title: 'Rebuildable project folders', detail: 'node_modules, build and cache folders in 14 projects. Each project can recreate them with one command.', bytes: 9.8 * GB, action: 'clean', paths: [jp(root, 'Projects')] },
        { id: 'variants', title: 'Files copied twice', detail: '312 files like "IMG_2041 copy.jpg" and "Report (1).pdf" sit next to an identical original.', bytes: 3.1 * GB, action: 'dupes', paths: [root] },
        { id: 'dupe_dirs', title: 'A folder copied twice', detail: '"Photos backup 2023" is an identical copy of "Pictures/2023".', bytes: 21.6 * GB, action: 'reveal', paths: [jp(root, 'Documents', 'Photos backup 2023')] },
      ],
    };
    if (view === 'tree') {
      const node = q.node || root;
      const meta = analyseSizes.get(node) || { size: total, files: 1068000 };
      const children = analyseTree(node, meta.size, meta.files);
      children.forEach(c => analyseSizes.set(c.path, { size: c.size, files: c.files }));
      return { node, size: meta.size, children };
    }
    if (view === 'largest') {
      const r = rng('largest');
      return { files: Array.from({ length: 60 }, (_, i) => ({ path: jp(root, pick(r, ['Movies', 'Downloads', 'Virtual Machines.localized/Windows 11.vmwarevm', 'Projects/ml-notebooks/data', 'Library/Containers/com.docker.docker/Data/vms/0']), pick(r, ['Virtual Disk.vmdk', 'Docker.raw', 'Wedding highlights.mov', 'train.parquet', 'Xcode_15.4.xip', 'macOS Sonoma.dmg', 'Iceland edit export.mov', 'backup-2023.zip']) + (i > 7 ? ` ${i}` : '')), size: Math.round(64 * GB / (1 + i * 0.6) * (0.8 + r() * 0.3)), mtime: now() - r() * 400 * DAY })) };
    }
    return {};
  };
  return j;
}

// --- dupes
function dupesJob(params) {
  const j = new Job('dupes', 'Finding duplicates', params, Q.get('fast') ? 5 : 14);
  const r = rng('dupes');
  const groups = [];
  const roots = params.paths?.length ? params.paths : [jp(HOME, 'Pictures')];
  const usedNames = new Set();
  for (let g = 0; g < 46; g++) {
    const img = g < 30;
    const name = img ? `IMG_${2000 + g * 7}.${pick(r, ['jpg', 'HEIC', 'jpg', 'png'])}` : pick(r, ['Thesis final v7.docx', 'Taxes 2025.pdf', 'Lease agreement.pdf', 'Wedding highlights.mov', 'Xcode_15.4.xip', 'Logic Pro backup.zip', 'Invoice-0423.pdf', 'Mixdown v3.wav', 'Budget 2024.xlsx', 'Keynote talk.key', 'Old Mac.dmg', 'Scan 0042.pdf', 'Voice memo.m4a', 'IMG_4402.mov', 'Design final FINAL.psd', 'Pitch deck.pdf']) + (g > 40 ? '' : '');
    let name2 = name;
    for (let k = 2; usedNames.has(name2); k++) name2 = name.replace(/(\.[^.]+)$/, ` ${k}$1`);
    usedNames.add(name2);
    const n = 2 + (r() > 0.75 ? Math.floor(r() * 3) : 0);
    const size = img ? Math.round(1.5 * MB + r() * 4 * MB) : Math.round(r() > 0.6 ? 200 * MB + r() * 3 * GB : 200 * KB + r() * 8 * MB);
    const places = img
      ? [jp(roots[0], '2024 Iceland'), jp(HOME, 'Desktop'), jp(HOME, 'Downloads'), jp(roots[0], 'Exports'), jp(HOME, 'Documents', 'Photos backup 2023')]
      : [jp(HOME, 'Documents'), jp(HOME, 'Downloads'), jp(HOME, 'Desktop'), jp('/Volumes/Backup Drive', 'Projects backup'), jp(HOME, 'Documents', 'Old stuff')];
    const files = Array.from({ length: n }, (_, i) => ({ path: jp(places[(g + i) % places.length], i === 1 && r() > 0.5 ? name2.replace(/(\.[^.]+)$/, ' copy$1') : name2), mtime: now() - (200 - i * 30 + r() * 20) * DAY, keep: i === 0 }));
    groups.push({ size, hash: Math.floor(r() * 1e16).toString(16) + g, count: files.length, reclaimable: size * (files.length - 1), files });
  }
  groups.sort((a, b) => b.size * (b.files.length - 1) - a.size * (a.files.length - 1));
  const reclaimable = groups.reduce((t, g) => t + g.size * (g.files.length - 1), 0);
  j.describe = (p, jb) => ({ phase: jb.state === 'done' ? 'Finished' : p < 0.4 ? 'Grouping files by size' : p < 0.75 ? 'Comparing the start and end of each file' : 'Comparing full contents', stats: { files_done: Math.round(84000 * p), found: Math.round(groups.length * p), bytes_done: Math.round(reclaimable * p) } });
  j.results = () => ({ groups, total_groups: groups.length, reclaimable, scanned: 84000, partial: false });
  return j;
}

// --- cleanup
function cleanScanJob(params) {
  const j = new Job('clean_scan', 'Looking for things you can clean up', params, Q.get('fast') ? 4 : 9);
  const L = WIN ? jp(HOME, 'AppData', 'Local') : jp(HOME, 'Library');
  const item = (path, size, days) => ({ path, size, mtime: now() - days * DAY });
  const cats = [
    { id: 'user_caches', name: 'App caches', detail: 'Temporary files apps rebuild when they need them.', risk: 'safe', items: [item(jp(L, 'Caches', 'com.spotify.client'), 2.4 * GB, 1), item(jp(L, 'Caches', 'com.apple.Music'), 840 * MB, 4), item(jp(L, 'Caches', 'Slack'), 610 * MB, 1), item(jp(L, 'Caches', 'com.figma.Desktop'), 420 * MB, 2), item(jp(L, 'Caches', 'pip'), 380 * MB, 12)] },
    { id: 'browser', name: 'Browser caches', detail: 'Cached web pages and images. Sites load them again as you browse.', risk: 'safe', items: [item(jp(L, 'Caches', 'Google', 'Chrome', 'Default', 'Cache'), 1.9 * GB, 0), item(jp(L, 'Caches', 'com.apple.Safari'), 520 * MB, 0), item(jp(L, 'Caches', 'Firefox', 'Profiles'), 310 * MB, 3)] },
    { id: 'xcode', name: 'Xcode build data', detail: 'DerivedData and old simulator caches. Xcode rebuilds them on the next build.', risk: 'safe', items: [item(jp(L, 'Developer', 'Xcode', 'DerivedData'), 8.2 * GB, 2), item(jp(L, 'Developer', 'CoreSimulator', 'Caches'), 3.1 * GB, 20)] },
    { id: 'pkg', name: 'Package manager caches', detail: 'Download caches for npm, pip, Homebrew and Gradle. They download again when needed.', risk: 'safe', items: [item(jp(HOME, '.npm', '_cacache'), 2.2 * GB, 5), item(jp(L, 'Caches', 'Homebrew'), 1.4 * GB, 9), item(jp(HOME, '.gradle', 'caches'), 3.6 * GB, 14)] },
    { id: 'logs', name: 'Logs', detail: 'Diagnostic logs apps write while they run.', risk: 'safe', items: [item(jp(L, 'Logs', 'DiagnosticReports'), 120 * MB, 3), item(jp(L, 'Logs', 'JetBrains'), 340 * MB, 7)] },
    { id: 'crash', name: 'Crash reports', detail: 'Reports from apps that quit unexpectedly.', risk: 'safe', items: [item(jp(L, 'Logs', 'CrashReporter'), 46 * MB, 10)] },
    { id: 'installers', name: 'Old installers', detail: 'Disk images and installers in Downloads older than 30 days. Keep any you might need again.', risk: 'review', items: [item(jp(HOME, 'Downloads', 'Xcode_15.4.xip'), 7.9 * GB, 140), item(jp(HOME, 'Downloads', 'Docker.dmg'), 1.6 * GB, 90), item(jp(HOME, 'Downloads', 'Zoom.pkg'), 98 * MB, 210), item(jp(HOME, 'Downloads', 'Install Spotify.app.zip'), 4 * MB, 60), item(jp(HOME, 'Downloads', 'GoogleChrome.dmg'), 230 * MB, 300)] },
    { id: 'regen', name: 'Rebuildable project folders', detail: 'node_modules, build and cache folders. Each project can recreate them with one command.', risk: 'review', items: [item(jp(HOME, 'Projects', 'webapp', 'node_modules'), 820 * MB, 3), item(jp(HOME, 'Projects', 'android-app', 'build'), 380 * MB, 6), item(jp(HOME, 'Projects', 'firmware-esp32', '.pio'), 640 * MB, 15), item(jp(HOME, 'Projects', 'old-site', 'node_modules'), 1.1 * GB, 400)] },
    { id: 'trash', name: WIN ? 'Recycle Bin' : 'Trash', detail: `Already in the ${WIN ? 'Recycle Bin' : 'Trash'}. Empty it yourself when you're sure.`, risk: 'review', report_only: true, items: [] , size: 3.4 * GB },
  ];
  cats.forEach(c => { if (c.size === undefined) c.size = c.items.reduce((t, i) => t + i.size, 0); c.count = c.items.length; c.report_only = !!c.report_only; });
  j.describe = (p, jb) => ({ phase: jb.state === 'done' ? 'Finished' : 'Checking caches, logs and downloads', stats: { found: Math.round(cats.length * p), bytes_done: Math.round(cats.reduce((t, c) => t + c.size, 0) * p) } });
  j.results = () => ({ categories: cats, total: cats.filter(c => !c.report_only).reduce((t, c) => t + c.size, 0), partial: false });
  return j;
}
function sparseScanJob(params) {
  const j = new Job('sparse_scan', 'Looking for VM disks that can shrink', params, 4);
  const files = [
    { path: jp(HOME, WIN ? 'Virtual Machines' : 'Virtual Machines.localized', 'Windows 11.vmwarevm', 'Virtual Disk.vmdk'), size: 64 * GB, allocated: 61.2 * GB, zero_bytes: 38.4 * GB, reclaimable: 37.2 * GB },
    { path: jp(HOME, 'Library', 'Containers', 'com.utmapp.UTM', 'Data', 'Documents', 'Ubuntu 24.04.utm', 'Data', 'disk.img'), size: 32 * GB, allocated: 29.6 * GB, zero_bytes: 22.1 * GB, reclaimable: 21.5 * GB },
    { path: jp(HOME, 'Library', 'Containers', 'com.docker.docker', 'Data', 'vms', '0', 'data', 'Docker.raw'), size: 64 * GB, allocated: 18.2 * GB, zero_bytes: 0.4 * GB, reclaimable: 0.2 * GB },
  ];
  j.describe = (p) => ({ phase: 'Reading disk files', stats: { found: Math.round(files.length * p) } });
  j.results = () => ({ files });
  return j;
}
function quickJob(kind, title, params, n, extra) {
  const j = new Job(kind, title, params, 3.5);
  j.describe = (p) => ({ phase: 'Working', stats: { files_done: Math.round(n * p), files_total: n }, ...(extra || {}) });
  return j;
}

// --- ocr
const OCR = { indexed: 8412, folders: [{ path: jp(HOME, 'Pictures'), files: 6210 }, { path: jp(HOME, 'Documents'), files: 2202 }] };
const CORPUS = [
  ['Pictures/Screenshots/Screenshot 2025-02-11 at 08.42.10.png', 'Boarding pass - Icelandair FI 450 - KEF to LHR - Gate D14 - Seat 22A - Boarding 07:35'],
  ['Pictures/Screenshots/Screenshot 2025-01-03 at 19.02.55.png', 'Wi-Fi network Hygge_5G - password tomatoes-and-basil-42 - guest network for the cabin'],
  ['Documents/Scans/Scan 0042.pdf', 'Lease agreement - monthly rent 1,450 - deposit due on signing - landlord Priya Raman'],
  ['Pictures/2024 Iceland/IMG_1102.jpg', 'Sign: Seljalandsfoss - please stay on the path - slippery rocks'],
  ['Documents/Scans/Receipt Blue Bottle.pdf', 'Blue Bottle Coffee - 2 x New Orleans iced - 1 x waffle - total 14.50 - card ending 4402'],
  ['Pictures/Screenshots/Screenshot 2024-11-20 at 14.10.03.png', 'Order confirmation - standing desk frame - delivery between Nov 26 and Nov 29 - order 88-41-2207'],
  ['Pictures/Family/IMG_2215.HEIC', "Grandma's recipe card - cardamom buns - 500 g flour, 2 tsp cardamom, 75 g butter, bake 12 minutes"],
  ['Documents/Scans/Prescription.pdf', 'Prescription - amoxicillin 500 mg - take three times a day with food - 7 days'],
  ['Pictures/Screenshots/Screenshot 2025-03-02 at 10.14.22.png', 'Whiteboard: Q3 roadmap - offline mode - faster sync - password reset flow - beta in August'],
  ['Documents/Scans/Scan 0017.pdf', 'Car insurance certificate - policy RT-4429-881 - valid until 14 March 2026'],
  ['Pictures/Screenshots/Screenshot 2025-01-28 at 21.33.40.png', 'Recovery codes - keep these somewhere safe - 4f9k-22pd 8x1m-qq7r'],
  ['Pictures/2024 Iceland/IMG_1311.jpg', 'Menu - lamb soup 2,900 ISK - rye bread ice cream - coffee refills free'],
  ['Documents/Scans/Receipt Hardware store.pdf', 'Hardware store receipt - wood glue, 2 clamps, sandpaper 120 grit - total 38.20'],
  ['Pictures/Screenshots/Screenshot 2024-12-24 at 09.12.18.png', 'Train tickets - Oxford to London Paddington - coach C seat 41 - Wi-Fi password GWR-guest'],
];
function ocrSearch(q, limit) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  return CORPUS.filter(([, t]) => words.every(w => t.toLowerCase().includes(w))).slice(0, limit).map(([p, t]) => {
    const i = t.toLowerCase().indexOf(words[0]);
    const s = Math.max(0, i - 40);
    return { path: jp(HOME, ...p.split('/')), snippet: (s ? '…' : '') + t.slice(s, s + 140) + (s + 140 < t.length ? '…' : ''), kind: p.endsWith('.pdf') ? 'pdf' : 'image' };
  });
}
function ocrJob(params) {
  const j = new Job('ocr', `Reading text in ${(params.paths || []).map(p => p.split(/[\\/]/).pop()).join(', ') || 'your files'}`, params, Q.get('fast') ? 6 : 24);
  const total = 3120;
  j.describe = (p) => ({ phase: p < 0.1 ? 'Finding images and PDFs' : 'Reading text', stats: { files_done: Math.round(total * p), files_total: total } });
  j.onFinish = () => { OCR.indexed += total; for (const p of params.paths || []) if (!OCR.folders.some(f => f.path === p)) OCR.folders.push({ path: p, files: total }); };
  return j;
}

// ------------------------------------------------------------------ system
function system() {
  const t = now() / 4000;
  const total = 16 * GB;
  const used = (10.6 + Math.sin(t) * 0.5) * GB;
  return {
    os: WIN ? 'Windows 11 Pro 23H2' : 'macOS 15.1 Sequoia', model: WIN ? 'Surface Laptop 5' : 'MacBook Pro (14-inch, 2023)',
    cpu: { percent: 18 + Math.abs(Math.sin(t * 1.7)) * 22, cores: 12 },
    memory: { total, used, available: total - used, percent: (used / total) * 100, pressure: 'normal', swap_used: 1.3 * GB },
    top: [['Google Chrome', 2.4 * GB, 6.1], ['Xcode', 1.7 * GB, 1.2], ['Docker Desktop', 1.4 * GB, 3.4], ['Final Cut Pro', 1.1 * GB, 0.4], ['Photos', 880 * MB, 0.2], ['Slack', 640 * MB, 0.9], ['Safari', 590 * MB, 1.4], ['Spotify', 330 * MB, 2.1], ['Messages', 190 * MB, 0.1], ['Retrova', 96 * MB, 0.8]]
      .map(([name, rss, cpu], i) => ({ pid: 400 + i * 37, name, rss: rss * (1 + Math.sin(t + i) * 0.02), cpu: cpu * (0.7 + Math.abs(Math.sin(t * 2 + i)) * 0.6) })),
    disks: DRIVES.volumes.map(v => ({ mount: v.mount, total: v.size, used: v.size - v.free, free: v.free })),
    uptime: 4 * 86400 + 7 * 3600 + 22 * 60,
  };
}

// ------------------------------------------------------------------ routes
let sessionSeq = 0;
const sessions = new Map();
const sizeState = new Map();

const routes = {
  'GET /api/update/check': () => (Q.get('update') === 'none'
    ? { current: '1.0.2', latest: '1.0.2', newer: false, page: 'https://github.com/jishnumahanta/retrova/releases', notes: '', asset: null }
    : { current: '1.0.2', latest: '1.0.3', newer: true, page: 'https://github.com/jishnumahanta/retrova/releases/tag/v1.0.3',
        notes: '### Retrova 1.0.3\n\n- **Faster** deep scans on big drives\n- Fixed a rare crash when a USB drive is unplugged mid-copy',
        asset: { name: PLATFORM === 'windows' ? 'Retrova-1.0.3-win-x64-Setup.exe' : 'Retrova-1.0.3-mac-arm64.dmg', url: 'https://github.com/jishnumahanta/retrova/releases/download/v1.0.3/x', size: 18000000 } }),
  'POST /api/update/open': () => ({ ok: true }),
  'GET /api/info': () => ({ version: '1.0.1', platform: PLATFORM, admin: ADMIN, can_elevate: !ADMIN, user: 'sam', home: HOME, app_data: jp(HOME, 'Library', 'Application Support', 'Retrova'), elevated: ADMIN, default_out: jp(HOME, 'Recovered'), start_source: null }),
  'POST /api/elevate': () => { const u = new URL(location.href); u.searchParams.set('admin', '1'); return { url: u.toString() }; },
  'GET /api/drives': () => DRIVES,
  'GET /api/recover/sources': () => ({ sources: SOURCES, admin: ADMIN }),
  'POST /api/recover/open': (q, b) => {
    const src = findSource(b);
    const lockedKind = src && (src.fs === 'bitlocker' || src.fs === 'luks' ? src.fs : null);
    if (lockedKind) {
      // demo rules: the password is "letmein"; any 48-digit recovery key is accepted for BitLocker
      if (!b.key) return { needs_key: { kind: lockedKind, label: lockedKind === 'bitlocker' ? 'WORK-PC 3/14/2025' : '', accepts: lockedKind === 'bitlocker' ? ['password', 'recovery'] : ['password'],
        hint: (lockedKind === 'bitlocker' ? "Enter the drive's password, or its 48-digit recovery key." : 'Enter the passphrase for this encrypted Linux drive.') + ' (Demo: the password is letmein.)', volume: null },
        source: src.source, offset: src.offset, size: src.size, fs: src.fs, fs_name: src.fs_name, partitions: [] };
      const recovery = lockedKind === 'bitlocker' && b.key.replace(/\D/g, '').length === 48;
      if (b.key !== 'letmein' && !recovery) throw new ApiError(lockedKind === 'bitlocker' ? "That password or recovery key didn't unlock the drive. Check it and try again." : "That passphrase didn't unlock the drive. Check it and try again.", 400);
    }
    if (src && src.needs_admin) throw new ApiError("Retrova needs administrator rights to read this drive directly. Use \u201cAllow access\u201d (you'll be asked for your password) and try again.", 403);
    if (src && !src.fs) throw new ApiError('No partitions or filesystems were found on this drive - it may be blank, encrypted or badly damaged. Deep scan can still look for photos, videos and documents.', 422);
    const sid = `s${++sessionSeq}`;
    const isImg = !src;
    const inner = lockedKind === 'bitlocker' ? ['ntfs', 'NTFS'] : lockedKind === 'luks' ? ['ext4', 'ext4'] : null;
    const label0 = lockedKind ? (lockedKind === 'bitlocker' ? 'Work' : 'Backup') : null;
    const label = label0 || b.volume || (src ? src.label : (b.source || '').split(/[\\/]/).pop().replace(/\.[^.]+$/, ''));
    const apfs = !src || src.fs === 'apfs';
    const s = { sid, fs: apfs ? 'apfs' : inner ? inner[0] : src.fs, fs_name: apfs ? 'APFS' : inner ? inner[1] : src.fs_name, label, volumes: apfs ? ['MacStorage', 'MacStorage - Data'] : [label], volume: apfs ? label : null,
      can_list_deleted: !apfs, size: src ? src.size : 500 * GB, source: b.source, offset: src ? src.offset : 0, is_image: isImg, read_only: true, default_out: jp(HOME, 'Recovered'),
      partitions: src ? SOURCES.filter(x => x.source === src.source).map((x, i) => ({ index: i, offset: x.offset, size: x.size, fs: x.fs, fs_name: x.fs_name, label: x.label, supported: !!x.fs })) : [],
      encryption: lockedKind ? { kind: lockedKind, name: lockedKind === 'bitlocker' ? 'BitLocker' : 'LUKS', unlocked: true } : null,
      notes: lockedKind ? [`This ${lockedKind === 'bitlocker' ? 'BitLocker' : 'LUKS'} drive is unlocked with the key you entered. Retrova only reads it, and forgets the key when you leave this drive.`] : apfs ? ['The newest checkpoint was damaged, so Retrova is using the one from 3 minutes earlier.', 'Parts of this filesystem look damaged; some folders may be unreadable.'] : [] };
    sessions.set(sid, s);
    return s;
  },
  'GET /api/recover/free': (q) => { const v = DRIVES.volumes.filter(v => (q.out || '').startsWith(v.mount)).sort((a, b) => b.mount.length - a.mount.length)[0] || DRIVES.volumes[0]; return { out: q.out, free: v.free, exists: false }; },
  'GET /api/recover/carve_types': () => ({ groups: ['photos', 'videos', 'music', 'documents', 'archives', 'other'], types: [
    ['jpg', 'jpg', 'photos', 'JPEG photos'], ['png', 'png', 'photos', 'PNG images'], ['heic', 'heic', 'photos', 'HEIC photos (iPhone)'], ['gif', 'gif', 'photos', 'GIF images'], ['cr2', 'cr2', 'photos', 'Canon RAW'], ['cr3', 'cr3', 'photos', 'Canon RAW (CR3)'], ['nef', 'nef', 'photos', 'Nikon RAW'], ['arw', 'arw', 'photos', 'Sony RAW'], ['raf', 'raf', 'photos', 'Fujifilm RAW'],
    ['mp4', 'mp4', 'videos', 'MP4 videos'], ['mov', 'mov', 'videos', 'QuickTime videos'], ['avi', 'avi', 'videos', 'AVI videos'],
    ['mp3', 'mp3', 'music', 'MP3 audio'], ['wav', 'wav', 'music', 'WAV audio'], ['m4a', 'm4a', 'music', 'AAC audio'],
    ['pdf', 'pdf', 'documents', 'PDF documents'], ['docx', 'docx', 'documents', 'Word documents'], ['xlsx', 'xlsx', 'documents', 'Excel spreadsheets'], ['pptx', 'pptx', 'documents', 'PowerPoint decks'],
    ['zip', 'zip', 'archives', 'ZIP archives'], ['sqlite', 'sqlite', 'other', 'SQLite databases']].map(([type, ext, group, name]) => ({ type, ext, group, name })) }),
  'GET /api/recover/ls': (q) => {
    if (!sessions.has(q.sid)) throw new ApiError('This drive is no longer open. Choose it again.', 404);
    const path = q.path || '/';
    const n = findNode(path);
    if (!n || n.kind !== 'dir') throw new ApiError(`Can't open ${path}`, 404);
    const base = path.replace(/\/$/, '');
    const row = (c, at) => ({ name: c.name, path: `${at}/${c.name}`, kind: c.kind, size: c.kind === 'file' ? c.size : 0, mtime: c.mtime || T0, ...(c.name === 'Envelope Index-wal' ? { error: 'Unreadable record' } : {}),
      ...(c.deleted ? { deleted: true, recover: c.recover, recover_note: c.note } : {}) });
    const inDeleted = path === '/' + DELETED_ROOT || path.startsWith('/' + DELETED_ROOT + '/');
    const entries = kids(n).map(c => row(c, base));
    if (q.deleted && !inDeleted && sessions.get(q.sid)?.can_list_deleted) {
      const live = new Set(entries.filter(e => e.kind === 'dir').map(e => e.name));
      const twin = findNode('/' + DELETED_ROOT + (path === '/' ? '' : path));
      if (twin) entries.push(...kids(twin).filter(c => c.kind !== 'dir' || !live.has(c.name)).map(c => ({ ...row(c, `/${DELETED_ROOT}${path === '/' ? '' : path}`), deleted: true })));
      if (path === '/') entries.push({ name: DELETED_ROOT, path: '/' + DELETED_ROOT, kind: 'dir', size: 0, mtime: T0, deleted: true });
    }
    return { path: path === '' ? '/' : path, entries };
  },
  'POST /api/recover/size': (q, b) => {
    const out = {};
    for (const p of b.paths || []) {
      const key = p + '|' + (b.patterns || []).join(',');
      let st = sizeState.get(key);
      if (!st) { const n = findNode(p); const s = n ? treeSize(n, p) : { bytes: 0, files: 0 }; st = { t0: now(), dur: 500 + Math.min(2500, s.files * 2), ...s }; sizeState.set(key, st); }
      const f = Math.min(1, (now() - st.t0) / st.dur);
      out[p] = f >= 1 ? { state: 'done', bytes: st.bytes, files: st.files, errors: p.includes('Family') ? 1 : 0 } : { state: f < 0.1 ? 'queued' : 'running', bytes: Math.round(st.bytes * f), files: Math.round(st.files * f), errors: 0 };
    }
    return { sizes: out };
  },
  'POST /api/recover/regen': (q, b) => regenJob(b).status(),
  'POST /api/recover/close': (q, b) => ({ closed: sessions.delete(b.sid) }),
  'POST /api/recover/copy': (q, b) => {
    if (!b.out) throw new ApiError('Choose where to save the recovered files first.');
    if (!b.paths?.length) throw new ApiError('Tick something to recover first.');
    if (busyDisk()) throw new ApiError(`Wait for '${busyDisk().title}' to finish (or stop it) first - only one job reads a disk at a time.`, 409);
    const s = sessions.get(b.sid);
    return copyJob({ ...b, label: s?.label }).status();
  },
  'POST /api/recover/image': (q, b) => {
    if (busyDisk()) throw new ApiError(`Wait for '${busyDisk().title}' to finish (or stop it) first - only one job reads a disk at a time.`, 409);
    const s = sessions.get(b.sid); const src = findSource(b);
    return imageJob({ ...b, label: s?.label || src?.label, total: s?.size || src?.size }).status();
  },
  'POST /api/recover/carve': (q, b) => {
    if (busyDisk()) throw new ApiError(`Wait for '${busyDisk().title}' to finish (or stop it) first - only one job reads a disk at a time.`, 409);
    if (!b.types?.length) throw new ApiError('Choose at least one kind of file to look for.');
    const src = findSource(b) || (b.sid && sessions.get(b.sid));
    return carveJob({ ...b, label: src?.label, total: src?.size }).status();
  },
  'POST /api/recover/verify': (q, b) => verifyJob(b).status(),
  'POST /api/recover/repair': (q, b) => { const src = getJob(b.job); return verifyJob({ ...src.params, from_job: b.job }, { repair: true }).status(); },
  'GET /api/fs/places': () => ({ places: [
    { name: 'Home', path: HOME, icon: 'home' }, { name: 'Desktop', path: jp(HOME, 'Desktop'), icon: 'desktop' }, { name: 'Documents', path: jp(HOME, 'Documents'), icon: 'documents' },
    { name: 'Downloads', path: jp(HOME, 'Downloads'), icon: 'downloads' }, { name: 'Pictures', path: jp(HOME, 'Pictures'), icon: 'pictures' },
    ...DRIVES.volumes.map(v => ({ name: v.label, path: v.mount, icon: v.mount === '/' || v.mount === 'C:\\' ? 'drive' : 'volume' })),
  ] }),
  'GET /api/fs/ls': (q) => localLs(q.path),
  'POST /api/reveal': () => ({ ok: true }),
  'POST /api/open_link': () => ({ ok: true }),
  'POST /api/native/pick': (q, b) => {
    if (Q.get('native') === '1') return { paths: [b.kind === 'file' ? jp(HOME, 'Desktop', 'MacStorage.img') : jp('/Volumes/Backup Drive', 'Recovered')] };
    throw new ApiError("Native dialogs aren't available - use the in-app browser.", 409);
  },
  'POST /api/analyse': (q, b) => analyseJob(b).status(),
  'POST /api/dupes': (q, b) => { if (!b.paths?.length) throw new ApiError('Add at least one folder to search.'); return dupesJob(b).status(); },
  'POST /api/dupes/apply': (q, b) => quickJob('dupes_apply', b.action === 'clone' ? 'Replacing duplicates with clones' : `Moving duplicates to the ${WIN ? 'Recycle Bin' : 'Trash'}`, b, (b.remove || []).length).status(),
  'POST /api/sparse/scan': (q, b) => sparseScanJob(b).status(),
  'POST /api/sparse/apply': (q, b) => quickJob('sparse_apply', 'Shrinking VM disks', b, (b.files || []).length).status(),
  'POST /api/clean/scan': (q, b) => cleanScanJob(b).status(),
  'POST /api/clean/apply': (q, b) => quickJob('clean_apply', `Moving items to the ${WIN ? 'Recycle Bin' : 'Trash'}`, b, (b.paths || []).length).status(),
  'GET /api/system': () => system(),
  'POST /api/system/free_memory': () => ADMIN ? { ok: true, before: 5.1 * GB, after: 6.8 * GB, method: 'purge', message: 'Freed 1.7 GB of memory.' } : { ok: false, needs_admin: true, message: 'Freeing memory on macOS needs administrator rights.' },
  'POST /api/system/quit': (q, b) => { if (b.pid === 400 + 9 * 37) throw new ApiError("Retrova can't quit itself from here - close its window instead."); return { ok: true }; },
  'GET /api/ocr/status': () => ({ available: Q.get('ocr') !== 'off', engine: WIN ? 'Windows OCR' : 'Apple Vision', indexed: Q.get('ocr') === 'empty' ? 0 : OCR.indexed, langs: ['en-US', 'de-DE'], folders: Q.get('ocr') === 'empty' ? [] : OCR.folders, reason: Q.get('ocr') === 'off' ? 'No text recognition engine was found. On Windows, add an OCR language in Settings > Time & language.' : undefined }),
  'POST /api/ocr/index': (q, b) => ocrJob(b).status(),
  'GET /api/ocr/search': (q) => ({ results: ocrSearch(q.q || '', Number(q.limit) || 50) }),
  'GET /api/jobs': (q) => ({ jobs: [...jobs.values()].sort((a, b) => b.t0 - a.t0).filter(j => !q.kind || j.kind === q.kind).map(j => j.status()) }),
  'GET /api/job': (q) => getJob(q.id).status(),
  'GET /api/job/result': (q) => getJob(q.id).result(q),
  'POST /api/job/control': (q, b) => {
    const j = getJob(b.id);
    j.tick();
    if (b.action === 'pause' && j.state === 'running') j.state = 'paused';
    else if (b.action === 'resume' && j.state === 'paused') { j.state = 'running'; j.last = now(); }
    else if (b.action === 'stop' && LIVE.has(j.state)) { j.state = 'stopping'; j.stopAt = now(); }
    return j.status();
  },
};
function busyDisk() { return [...jobs.values()].find(j => ['copy', 'image', 'carve'].includes(j.kind) && LIVE.has(j.state)); }

export async function handle(method, fullPath, body) {
  const [path, query = ''] = fullPath.split('?');
  const q = Object.fromEntries(new URLSearchParams(query));
  await new Promise(r => setTimeout(r, 40 + Math.random() * 120));
  const fn = routes[`${method} ${path}`];
  if (!fn) throw new ApiError('not found', 404);
  return clone(fn(q, body || {}));
}

// ------------------------------------------------------------------ seed history
if (!FRESH) {
  seeded(copyJob({ sid: 's0', paths: ['/Users/sam/Pictures', '/Users/sam/Documents'], out: '/Volumes/Backup Drive/Recovered', label: 'MacStorage' }), 26 * 3600e3);
  const v = seeded(verifyJob({ from_job: 'x', mode: 'full', out: '/Volumes/Backup Drive/Recovered' }), 25 * 3600e3);
  seeded(ocrJob({ paths: [jp(HOME, 'Pictures')] }), 50 * 3600e3).onFinish = null;
  seeded(dupesJob({ paths: [jp(HOME, 'Pictures'), jp(HOME, 'Documents')], min_size: MB }), 20 * 3600e3);
  seeded(cleanScanJob({}), 3 * 3600e3);
  seeded(analyseJob({ path: HOME }), 2 * 3600e3);
}
for (const k of (Q.get('run') || '').split(',').filter(Boolean)) {
  const make = {
    copy: () => copyJob({ sid: 's0', paths: ['/Users/sam/Pictures', '/Users/sam/Documents', '/Users/sam/Projects'], excluded: ['/Users/sam/Projects/webapp/node_modules'], out: '/Volumes/Backup Drive/Recovered', label: 'MacStorage' }),
    image: () => imageJob({ source: SOURCES[0].source, out: '/Volumes/Backup Drive/MacStorage.img', label: 'MacStorage' }),
    carve: () => carveJob({ source: SOURCES[1].source, out: jp(HOME, 'Recovered files'), types: ['jpg', 'heic', 'png', 'mov', 'mp4', 'pdf'], label: 'EOS_DIGITAL' }),
    verify: () => verifyJob({ mode: 'full', out: '/Volumes/Backup Drive/Recovered' }),
    analyse: () => analyseJob({ path: HOME }),
    dupes: () => dupesJob({ paths: [jp(HOME, 'Pictures')] }),
    clean_scan: () => cleanScanJob({}),
    ocr: () => ocrJob({ paths: [jp(HOME, 'Downloads')] }),
  }[k];
  if (make) { const j = make(); j.t = j.duration * Number(Q.get('at') || 0.34); }
}
