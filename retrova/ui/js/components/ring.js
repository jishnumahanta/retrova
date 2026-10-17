// The Retrova ring: 12 sectors (from the app icon). As work completes the sectors fill
// clockwise; the sector being worked on lifts out of its slot and drops back in when
// it is done - "a recovered block going back".

import { h, reducedMotion } from '../util.js';

const NS = 'http://www.w3.org/2000/svg';
const R = 50;               // outer radius (viewBox units)
const RI = 37.3;            // inner radius, same ratio as the logo (200/268)
const GAP = 7;              // degrees between sectors
const LIFT = 6.5;           // how far the active sector lifts out
let gid = 0;

const rad = d => (d * Math.PI) / 180;
const pt = (r, d) => `${(r * Math.cos(rad(d))).toFixed(3)} ${(r * Math.sin(rad(d))).toFixed(3)}`;
function sector(a, b, ro = R, ri = RI) {
  if (b - a < 0.15) b = a + 0.15;
  return `M ${pt(ro, a)} A ${ro} ${ro} 0 0 1 ${pt(ro, b)} L ${pt(ri, b)} A ${ri} ${ri} 0 0 0 ${pt(ri, a)} Z`;
}
const svgEl = (name, attrs = {}) => {
  const e = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};

const TONE_FILL = {
  ok: null,            // gradient
  warn: 'var(--warn-fill)',
  bad: 'var(--bad-fill)',
  unknown: 'var(--text-3)',
  muted: 'var(--track-strong)',
};

/**
 * segRing({size, value (0..1 | null = indeterminate), state, segments, tones, center})
 *   tones: optional array (one per segment) of ok|warn|bad|unknown for per-segment colour
 * returns {el, center, set(value, state), setTones(tones)}
 */
export function segRing({ size = 120, value = 0, state = 'running', segments = 12, tones = null, center = null, label = '' } = {}) {
  const id = `rg${++gid}`;
  const span = 360 / segments;
  const svg = svgEl('svg', { viewBox: '-60 -60 120 120', role: 'img' });
  if (label) svg.setAttribute('aria-label', label);
  const defs = svgEl('defs');
  const grad = svgEl('linearGradient', { id, x1: '0.1', y1: '0', x2: '0.9', y2: '1' });
  grad.append(svgEl('stop', { offset: '0', class: 'ga' }), svgEl('stop', { offset: '1', class: 'gb' }));
  defs.append(grad);
  svg.append(defs);

  const segs = [];
  for (let i = 0; i < segments; i++) {
    const a = -90 + GAP / 2 + i * span;
    const b = a + span - GAP;
    const mid = rad((a + b) / 2);
    const g = svgEl('g', { class: 'seg-g' });
    const track = svgEl('path', { class: 'seg-track', d: sector(a, b) });
    const fill = svgEl('path', { class: 'seg-fill', d: sector(a, b), fill: `url(#${id})` });
    fill.style.animationDelay = `${(i * 1.8) / segments}s`;
    const slot = svgEl('path', { class: 'seg-slot', d: sector(a, b) });
    g.append(track, fill);
    svg.append(g, slot);
    segs.push({ a, b, g, fill, slot, dx: Math.cos(mid) * LIFT, dy: Math.sin(mid) * LIFT });
  }

  const centerEl = h('div.ring-center', center);
  const el = h('div.ring', { style: { width: size + 'px', height: size + 'px' } }, svg, centerEl);

  let shown = 0;
  let target = 0;
  let curState = state;
  let raf = 0;
  let curTones = tones;

  function draw(v) {
    const indeterminate = v === null || v === undefined;
    el.classList.toggle('indeterminate', indeterminate);
    el.classList.toggle('paused', curState === 'paused');
    const n = indeterminate ? 0 : Math.max(0, Math.min(1, v)) * segments;
    const full = Math.floor(n + 1e-9);
    const frac = n - full;
    const working = !indeterminate && ['running', 'queued', 'paused', 'stopping'].includes(curState) && full < segments;
    segs.forEach((s, i) => {
      let d = sector(s.a, s.b);
      let show = true;
      let lifted = false;
      if (indeterminate) {
        show = true;
      } else if (i < full) {
        show = true;
      } else if (i === full && (frac > 0.001 || working)) {
        d = sector(s.a, s.a + (s.b - s.a) * Math.max(frac, 0.02));
        lifted = working;
      } else {
        show = false;
      }
      s.fill.setAttribute('d', d);
      s.fill.style.display = show ? '' : 'none';
      const tone = curTones ? curTones[i] : null;
      let fillColor = tone && TONE_FILL[tone] ? TONE_FILL[tone] : `url(#${id})`;
      if (!curTones && i === full && !indeterminate) {
        if (curState === 'error') fillColor = 'var(--bad-fill)';
        else if (curState === 'stopped' || curState === 'stopping') fillColor = 'var(--warn-fill)';
      }
      s.fill.setAttribute('fill', fillColor);
      s.fill.style.opacity = tone === 'unknown' ? '.45' : '';
      s.g.classList.toggle('lift', lifted);
      s.g.style.transform = lifted ? `translate(${s.dx}px, ${s.dy}px)` : '';
      s.slot.classList.toggle('on', lifted);
    });
  }

  function animate() {
    cancelAnimationFrame(raf);
    if (target === null || shown === null || reducedMotion() || Math.abs(target - shown) < 0.002) {
      shown = target;
      draw(shown);
      return;
    }
    const from = shown, to = target, t0 = performance.now(), D = 520;
    const step = now => {
      const t = Math.min(1, (now - t0) / D);
      const e = 1 - Math.pow(1 - t, 3);
      shown = from + (to - from) * e;
      draw(shown);
      if (t < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
  }

  function set(v, st = curState) {
    curState = st;
    target = v === null || v === undefined ? null : v;
    if (shown === null && target !== null) shown = 0;
    animate();
  }

  shown = value === null ? null : 0;
  set(value, state);

  return {
    el,
    center: centerEl,
    set,
    setTones(t) { curTones = t; draw(shown); },
  };
}

/** Small indeterminate ring used as a spinner. */
export function spinner(size = 16) {
  const r = segRing({ size, value: null, state: 'running' });
  r.el.classList.add('spinner');
  return r.el;
}

/** Ring + big centred number, the common progress presentation. */
export function progressRing({ size = 168, value = 0, state = 'running' } = {}) {
  const valueEl = h('div.ring-value.num');
  const labelEl = h('div.ring-label');
  const ring = segRing({ size, value, state, center: h('div', valueEl, labelEl) });
  ring.valueEl = valueEl;
  ring.labelEl = labelEl;
  return ring;
}
