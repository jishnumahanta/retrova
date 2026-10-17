// Home: problem-first. "What do you need?" starts a guided flow; below, your drives and recent work.
import { api, isLive, progressOf } from '../api.js';
import { store, on } from '../store.js';
import { h, clear, bytes, ago, plural } from '../util.js';
import { icon, driveIcon } from '../components/icons.js';
import { segRing } from '../components/ring.js';
import { capBar, popover } from '../components/controls.js';
import { setToolbar } from '../shell.js';
import { go } from '../router.js';
import { kindMeta, jobSummary, startedAt } from '../jobs-meta.js';
import { STATE_WORD } from '../components/jobpanel.js';

const HEALTH_WORD = { ok: 'Healthy', warn: 'Needs attention', bad: 'Failing', unknown: 'No health info' };

function greeting() {
  const hr = new Date().getHours();
  const part = hr < 5 ? 'Hello' : hr < 12 ? 'Good morning' : hr < 18 ? 'Good afternoon' : 'Good evening';
  const u = store.info?.user || '';
  const name = /^[a-z][a-z-]{1,14}$/i.test(u) ? u[0].toUpperCase() + u.slice(1) : '';
  return name ? `${part}, ${name}` : part;
}

function problems() {
  const c = store.words.computer;
  return [
    { icon: 'recover', title: "A drive won't open", body: "Copy your files off a drive your computer can't mount.", go: ['recover', { flow: 'unmountable' }] },
    { icon: 'scan', title: 'I deleted something', body: 'Search a drive for deleted photos, videos and documents.', go: ['recover', { mode: 'carve', flow: 'deleted' }] },
    { icon: 'space', title: "I'm running out of space", body: "See what's taking up room and what's safe to clear.", go: ['space', { flow: 'full' }] },
    { icon: 'findtext', title: 'Find a photo or document by its text', body: 'Search the words inside screenshots, scans and PDFs.', go: ['find'] },
    { icon: 'system', title: `My ${c} feels slow`, body: 'See which apps use the most memory and free some up.', go: ['system'] },
    { icon: 'verify', title: 'Check a copy is perfect', body: 'Compare a copy with its original, file by file.', go: ['verify'] },
  ];
}

export default {
  async mount(root) {
    setToolbar({});
    const drivesBox = h('div.home-drives');
    const recentBox = h('div.panel.home-recent');
    root.append(
      h('header.home-hero',
        h('p.home-greet', { text: greeting() }),
        h('h1.home-ask', { text: 'What do you need?' })),
      h('div.problem-grid', problems().map(p => h('button.problem', { type: 'button', onclick: () => go(...p.go) },
        h('span.problem-icon', icon(p.icon)),
        h('span.problem-title', { text: p.title }),
        h('span.problem-body', { text: p.body }),
        icon('chevronRight', { cls: 'problem-chev' })))),
      h('section.section', h('h2.section-title', 'Your drives'), drivesBox),
      h('section.section', h('h2.section-title', 'Recent activity', h('a.aside', { href: '#/activity', text: 'Show all' })), recentBox),
    );

    renderDrivesLoading(drivesBox);
    loadDrives(drivesBox);
    const stopJobs = on('jobs', jobs => renderRecent(recentBox, jobs));
    try { renderRecent(recentBox, (await api.get('/api/jobs')).jobs || []); } catch { renderRecent(recentBox, []); }
    return () => stopJobs();
  },
};

function renderDrivesLoading(box) {
  clear(box, h('div.panel.drive-panel', h('div.drive-summary', h('div.skel', { style: 'width:132px;height:132px;border-radius:50%' })),
    h('div.drive-list', [1, 2, 3].map(() => h('div.drive-row', h('div.skel', { style: 'height:40px;width:100%' }))))));
}

async function loadDrives(box) {
  let r;
  try {
    r = await api.get('/api/drives');
    store.drives = r;
  } catch (e) {
    clear(box, h('div.panel.panel-pad', h('div.callout.warn', icon('warning'), h('div.grow', h('b', "Couldn't list your drives"), h('span.muted', e.message)))));
    return;
  }
  const disks = r.disks || [];
  if (!disks.length) {
    clear(box, h('div.panel.panel-pad.muted', 'No drives found.'));
    return;
  }
  // Health ring: the 12 sectors are shared out between drives, each coloured by its health.
  const tones = [];
  const per = disks.map((d, i) => Math.floor(12 / disks.length) + (i < 12 % disks.length ? 1 : 0));
  disks.forEach((d, i) => { for (let k = 0; k < per[i]; k++) tones.push(d.health?.state || 'unknown'); });
  const okN = disks.filter(d => d.health?.state === 'ok').length;
  const issues = disks.filter(d => ['warn', 'bad'].includes(d.health?.state));
  const unknownN = disks.length - okN - issues.length;
  const ring = segRing({ size: 132, value: 1, state: 'done', tones: tones.length === 12 ? tones : null, label: 'Drive health',
    center: h('div', h('div.ring-value.num', String(okN), h('small', { text: `/${disks.length}` })), h('div.ring-label', { text: 'healthy' })) });

  const summary = h('div.drive-summary', ring.el,
    h('div.drive-summary-text',
      h('h3', { text: healthHeadline(disks.length, okN, issues.length) }),
      issues.length
        ? issues.map(d => h('p.muted', h('b', { text: d.name }), ` ${d.health?.detail ? '- ' + lower(d.health.detail) : ''}`))
        : h('p.muted', { text: unknownN
          ? `None are reporting problems. ${plural(unknownN, 'drive', 'drives')} (such as disk images or some USB adapters) can't report health.`
          : 'None of your drives are reporting problems.' }),
      issues.length ? h('button.btn.sm', { onclick: () => go('recover', { hint: issues[0].name }) }, icon('recover'), 'Recover files') : null));

  const list = h('div.drive-list', disks.map(d => driveRow(d)));
  clear(box, h('div.panel.drive-panel', summary, list));
}

const lower = s => s ? s[0].toLowerCase() + s.slice(1) : '';

// The headline only claims "healthy" for drives that actually reported it.
function healthHeadline(total, okN, issueN) {
  if (issueN) return `${plural(issueN, 'drive needs', 'drives need')} attention`;
  if (okN === total) return total === 1 ? 'Your drive looks healthy' : `All ${plural(total, 'drive')} look healthy`;
  if (!okN) return 'No health info from your drives';
  return `${plural(okN, 'drive looks', 'drives look')} healthy`;
}

function driveRow(d) {
  const parts = d.partitions || [];
  const mounted = parts.filter(p => p.mount && p.free !== null && p.free !== undefined);
  const size = mounted.reduce((t, p) => t + (p.size || 0), 0);
  const free = mounted.reduce((t, p) => t + (p.free || 0), 0);
  const used = size ? (size - free) / size : null;
  const state = d.health?.state || 'unknown';
  const tone = used === null ? 'unknown' : used > 0.95 ? 'bad' : used > 0.88 ? 'warn' : '';
  const kind = [d.internal ? 'Internal' : d.protocol || 'External', bytes(d.size)].filter(Boolean).join(', ');
  const btn = h('button.drive-row', { type: 'button', title: d.health?.detail || '', onclick: () => menu(btn, d, mounted) },
    h('span.drive-icon', icon(driveIcon(d))),
    h('span.drive-main',
      h('span.drive-name', h('span.ellipsis', { text: d.name }), h('span.faint.drive-kind', { text: kind })),
      capBar(used, tone),
      h('span.drive-free.faint', { text: mounted.length ? `${bytes(free)} free of ${bytes(size)}` : 'Not mounted' })),
    h('span.drive-health', { class: state }, h('span.dot', { class: state }), h('span', { text: HEALTH_WORD[state] })));
  return btn;
}

function menu(anchor, d, mounted) {
  const items = [];
  for (const p of mounted) items.push({ label: `Analyse space on ${p.label || p.mount}`, icon: 'space', onClick: () => go('space', { path: p.mount }) });
  items.push({ label: 'Recover files from this drive', icon: 'recover', onClick: () => go('recover', { hint: d.name }) });
  items.push({ label: 'Search it for deleted files', icon: 'scan', onClick: () => go('recover', { mode: 'carve', hint: d.name }) });
  popover(anchor, items, { align: 'start' });
}

function renderRecent(box, jobs) {
  if (!box.isConnected) return;
  const list = jobs.slice(0, 5);
  if (!list.length) {
    clear(box, h('div.recent-empty', icon('clock'), h('span', 'Nothing yet. Scans, recoveries and checks you run appear here, and keep running while you do other things.')));
    return;
  }
  clear(box, list.map(j => {
    const m = kindMeta(j.kind);
    const ring = segRing({ size: 30, value: progressOf(j), state: j.state });
    const live = isLive(j);
    const t = startedAt(j);
    return h('button.list-row.recent-row', { type: 'button', onclick: () => go(m.route, m.params) },
      ring.el,
      h('div.grow', { style: 'min-width:0' },
        h('div.recent-title.ellipsis', { text: j.title || m.label }),
        h('div.faint.ellipsis.recent-sub', { text: live ? (j.phase || jobSummary(j)) : jobSummary(j) })),
      t && !live ? h('span.faint.recent-when', { text: ago(t) }) : null,
      h('span.badge', { class: j.state === 'done' ? 'ok' : j.state === 'error' ? 'bad' : j.state === 'stopped' ? 'warn' : '', text: STATE_WORD[j.state] || j.state }));
  }));
}
