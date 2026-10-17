// Activity: every task Retrova has run this session, with live progress and controls.
import { api, isLive, progressOf, jobControl } from '../api.js';
import { on, emit } from '../store.js';
import { h, clear, dur, ago } from '../util.js';
import { icon } from '../components/icons.js';
import { segRing } from '../components/ring.js';
import { emptyState } from '../components/controls.js';
import { toastError } from '../components/toast.js';
import { STATE_WORD } from '../components/jobpanel.js';
import { setToolbar } from '../shell.js';
import { kindMeta, jobSummary, startedAt } from '../jobs-meta.js';
import { go } from '../router.js';

export default {
  async mount(root) {
    setToolbar({ title: 'Activity' });
    const liveBox = h('section.act-sec');
    const doneBox = h('section.act-sec');
    root.append(liveBox, doneBox);
    const rings = new Map();

    function row(j) {
      const m = kindMeta(j.kind);
      let r = rings.get(j.id);
      if (!r) { r = segRing({ size: 40, value: progressOf(j), state: j.state }); rings.set(j.id, r); }
      else r.set(progressOf(j), j.state);
      const live = isLive(j);
      const p = progressOf(j);
      const ctl = [];
      if (j.state === 'running') ctl.push(h('button.btn.sm', { onclick: () => control(j, 'pause') }, icon('pause'), 'Pause'));
      if (j.state === 'paused') ctl.push(h('button.btn.sm', { onclick: () => control(j, 'resume') }, icon('play'), 'Resume'));
      if (live && j.state !== 'stopping') ctl.push(h('button.btn.sm', { onclick: () => control(j, 'stop') }, icon('stop'), 'Stop'));
      const t = startedAt(j);
      return h('div.act-row', { class: j.state },
        r.el,
        h('div.act-main',
          h('div.act-title', h('span.ellipsis', { text: j.title || m.label }), h('span.act-kind', icon(m.icon), m.label)),
          h('div.act-sub', { text: live ? [j.phase, jobSummary(j)].filter(Boolean).join(' - ') : j.state === 'error' ? (j.error || j.message) : (j.message || jobSummary(j)) })),
        h('div.act-side',
          h('span.badge', { class: j.state === 'done' ? 'ok' : j.state === 'error' ? 'bad' : j.state === 'stopped' ? 'warn' : '' }, live && p !== null && j.state === 'running' ? `${Math.floor(p * 100)}%` : STATE_WORD[j.state]),
          h('span.faint.act-when', { text: live ? (j.elapsed ? `Running ${dur(j.elapsed)}` : '') : (t ? ago(t) : j.elapsed ? `Took ${dur(j.elapsed)}` : '') })),
        h('div.act-ctl', ctl, h('button.btn.sm.ghost', { onclick: () => go(m.route, m.params) }, 'Open', icon('chevronRight'))));
    }
    async function control(j, action) {
      try { await jobControl(j.id, action); emit('jobs-changed'); } catch (e) { toastError(e); }
    }
    function paint(jobs) {
      if (!root.isConnected) return;
      const live = jobs.filter(isLive), done = jobs.filter(j => !isLive(j));
      clear(liveBox, h('h2.section-title', 'Running now'),
        live.length ? h('div.panel.act-list', live.map(row)) : h('div.act-none', icon('checkCircle'), h('span', 'Nothing is running. Tasks keep going in the background while you use other parts of Retrova.')));
      clear(doneBox, done.length ? [h('h2.section-title', 'Earlier'), h('div.panel.act-list', done.map(row))] : null);
      if (!jobs.length) clear(doneBox, h('div.panel', emptyState({ title: 'No activity yet', body: 'Recoveries, checks and scans you start show up here, with their progress.' })));
    }
    const off = on('jobs', paint);
    try { paint((await api.get('/api/jobs')).jobs || []); } catch (e) { toastError(e); }
    const t = setInterval(() => emit('jobs-changed'), 1500);
    return () => { off(); clearInterval(t); };
  },
};
