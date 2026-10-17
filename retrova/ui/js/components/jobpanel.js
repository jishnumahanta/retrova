// Shared presentation of a running job: segmented ring, headline, stats, pause/stop.
import { h, clear } from '../util.js';
import { icon } from './icons.js';
import { progressRing } from './ring.js';
import { statsGrid } from './controls.js';
import { jobControl, progressOf, isLive } from '../api.js';
import { toastError } from './toast.js';
import { emit } from '../store.js';

export const STATE_WORD = {
  queued: 'Waiting to start', running: 'Working', paused: 'Paused', stopping: 'Stopping…',
  stopped: 'Stopped', done: 'Finished', error: "Couldn't finish",
};

/**
 * jobPanel({describe(st) -> {headline, sub, stats, ringLabel}, actions(st) -> [nodes], ringSize, compact, controls})
 * returns {el, extra, update(st)}
 */
export function jobPanel({ describe, actions, ringSize = 168, compact = false, controls = true, onControl } = {}) {
  const ring = progressRing({ size: ringSize, value: 0 });
  const headline = h('h2.jp-headline');
  const sub = h('p.jp-sub');
  const ctrl = h('div.jp-controls');
  const stats = statsGrid([]);
  const extra = h('div.jp-extra');
  const el = h('section.jobpanel.panel', { class: compact ? 'compact' : '' },
    h('div.jp-top', ring.el, h('div.jp-text', headline, sub, ctrl)),
    stats, extra);
  let last = null;
  let busy = false;

  async function control(action) {
    if (!last || busy) return;
    busy = true;
    try {
      const st = await jobControl(last.id, action);
      update(st);
      onControl?.(action, st);
      emit('jobs-changed');
    } catch (e) { toastError(e); }
    busy = false;
  }

  function update(st) {
    if (!st) return;
    last = st;
    const d = describe(st) || {};
    const p = progressOf(st);
    ring.set(p, st.state);
    ring.setTones(d.ringTone ? Array(12).fill(d.ringTone) : null);
    // centre of ring
    if (d.ringIcon) {
      clear(ring.valueEl, icon(d.ringIcon, { cls: `ring-check ${d.ringTone === 'warn' ? 'warn-text' : d.ringTone === 'bad' ? 'bad-text' : ''}` }));
      ring.valueEl.classList.add('is-icon');
    } else if (st.state === 'done') {
      clear(ring.valueEl, icon('check', { cls: 'ring-check' }));
      ring.valueEl.classList.add('is-icon');
    } else if (st.state === 'error') {
      clear(ring.valueEl, icon('warning', { cls: 'ring-check bad-text' }));
      ring.valueEl.classList.add('is-icon');
    } else if (p === null) {
      clear(ring.valueEl, h('span.ring-dots', { text: '' }));
      ring.valueEl.classList.remove('is-icon');
    } else {
      ring.valueEl.classList.remove('is-icon');
      clear(ring.valueEl, String(Math.floor(p * 100)), h('small', { text: '%' }));
    }
    ring.labelEl.textContent = d.ringLabel ?? (st.state === 'running' ? '' : STATE_WORD[st.state] || '');
    headline.textContent = d.headline || st.title || '';
    clear(sub, d.sub || st.phase || '');
    stats.set(d.stats || []);
    stats.classList.toggle('hidden', !(d.stats && d.stats.length));
    const live = isLive(st);
    const btns = [];
    if (controls && live && st.state !== 'stopping') {
      if (st.state === 'paused') btns.push(h('button.btn', { onclick: () => control('resume') }, icon('play'), 'Resume'));
      else if (st.state === 'running') btns.push(h('button.btn', { onclick: () => control('pause') }, icon('pause'), 'Pause'));
      btns.push(h('button.btn', { onclick: () => control('stop') }, icon('stop'), 'Stop'));
    }
    const extraBtns = actions ? actions(st) : [];
    clear(ctrl, btns, extraBtns);
    el.dataset.state = st.state;
  }
  return { el, extra, update, ring, get status() { return last; } };
}

/** A compact one-line progress strip for jobs that run alongside a screen (e.g. text indexing). */
export function jobStrip({ describe }) {
  const ringBox = h('div.js-ring');
  const ring = progressRing({ size: 34 });
  ringBox.append(ring.el);
  const title = h('div.js-title');
  const sub = h('div.js-sub');
  const ctrl = h('div.row');
  const el = h('div.jobstrip', ringBox, h('div.grow', title, sub), ctrl);
  let last = null;
  async function control(action) {
    try { update(await jobControl(last.id, action)); emit('jobs-changed'); } catch (e) { toastError(e); }
  }
  function update(st) {
    last = st;
    const d = describe(st) || {};
    ring.set(progressOf(st), st.state);
    title.textContent = d.headline || st.title;
    clear(sub, d.sub || st.phase || '');
    const btns = [];
    if (st.state === 'running') btns.push(h('button.btn.sm', { onclick: () => control('pause') }, 'Pause'));
    if (st.state === 'paused') btns.push(h('button.btn.sm', { onclick: () => control('resume') }, 'Resume'));
    if (isLive(st) && st.state !== 'stopping') btns.push(h('button.btn.sm', { onclick: () => control('stop') }, 'Stop'));
    clear(ctrl, btns, d.actions || []);
  }
  return { el, update };
}
