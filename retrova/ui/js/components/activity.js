// Global activity pill: shows jobs running anywhere in the app, from any screen.
import { api, isLive, progressOf } from '../api.js';
import { h, clear, $ } from '../util.js';
import { segRing } from './ring.js';
import { popover } from './controls.js';
import { icon } from './icons.js';
import { on, emit } from '../store.js';
import { setNavCount } from '../shell.js';
import { kindMeta, jobSummary } from '../jobs-meta.js';
import { go } from '../router.js';
import { STATE_WORD } from './jobpanel.js';

let jobs = [];
let timer = null;
let ring = null;

export function startActivity() {
  const host = $('#activity-pill');
  ring = segRing({ size: 18, value: 0 });
  const text = h('span.pill-text');
  const pctEl = h('span.pill-pct.num');
  const pill = h('button.pill.hidden', { type: 'button', 'aria-label': 'Running tasks', onclick: () => openList(pill) }, ring.el, text, pctEl);
  host.append(pill);

  async function poll() {
    clearTimeout(timer);
    try {
      const r = await api.get('/api/jobs');
      jobs = r.jobs || [];
      emit('jobs', jobs);
    } catch { /* engine busy; try again */ }
    const live = jobs.filter(isLive);
    setNavCount('activity', live.length);
    pill.classList.toggle('hidden', !live.length);
    if (live.length) {
      const top = live[0];
      const p = progressOf(top);
      ring.set(p, top.state);
      text.textContent = live.length > 1 ? `${live.length} tasks running` : (top.state === 'paused' ? `${kindMeta(top.kind).label} paused` : top.title || kindMeta(top.kind).label);
      pctEl.textContent = live.length === 1 && p !== null ? `${Math.floor(p * 100)}%` : '';
    }
    timer = setTimeout(poll, live.length ? 1500 : 5000);
  }
  on('jobs-changed', () => setTimeout(poll, 150));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  poll();
}

export const currentJobs = () => jobs;

function openList(anchor) {
  const live = jobs.filter(isLive);
  const recent = jobs.filter(j => !isLive(j)).slice(0, 3);
  const row = j => {
    const r = segRing({ size: 26, value: progressOf(j), state: j.state });
    const m = kindMeta(j.kind);
    return h('button.jobmini', { onclick: () => { pop.close(); go(m.route, m.params); } },
      r.el, h('div.grow', { style: 'min-width:0' },
        h('div.t', { text: j.title || m.label }),
        h('div.s', { text: isLive(j) ? (j.phase || jobSummary(j) || STATE_WORD[j.state]) : STATE_WORD[j.state] })));
  };
  const box = h('div', { style: 'padding:2px' },
    live.length ? live.map(row) : h('div.hint', { style: 'padding:8px 10px', text: 'Nothing running right now.' }),
    recent.length ? [h('div.menu-sep'), recent.map(row)] : null,
    h('div.menu-sep'),
    h('button.menu-item', { onclick: () => { pop.close(); go('activity'); } }, icon('activity'), h('span.grow', 'Open Activity')));
  const pop = popover(anchor, box, { width: 320 });
}
