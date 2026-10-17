// Squarified treemap drawn with positioned buttons (focusable, labels can ellipsise).
import { h, clear, bytes, num } from '../util.js';

function worst(row, side) {
  let s = 0, mx = 0, mn = Infinity;
  for (const r of row) { s += r.a; mx = Math.max(mx, r.a); mn = Math.min(mn, r.a); }
  const s2 = s * s, side2 = side * side;
  return Math.max((side2 * mx) / s2, s2 / (side2 * mn));
}

function placeRow(row, rect, out) {
  const s = row.reduce((t, r) => t + r.a, 0);
  if (rect.w >= rect.h) {
    const cw = s / rect.h;
    let y = rect.y;
    for (const r of row) { const hh = r.a / cw; out.push({ ...r, x: rect.x, y, w: cw, h: hh }); y += hh; }
    return { x: rect.x + cw, y: rect.y, w: rect.w - cw, h: rect.h };
  }
  const rh = s / rect.w;
  let x = rect.x;
  for (const r of row) { const ww = r.a / rh; out.push({ ...r, x, y: rect.y, w: ww, h: rh }); x += ww; }
  return { x: rect.x, y: rect.y + rh, w: rect.w, h: rect.h - rh };
}

export function squarify(items, W, H) {
  const total = items.reduce((t, i) => t + i.size, 0) || 1;
  const area = W * H;
  const scaled = items.filter(i => i.size > 0).map(it => ({ it, a: (it.size / total) * area }));
  const out = [];
  let rect = { x: 0, y: 0, w: W, h: H };
  let row = [];
  let i = 0;
  while (i < scaled.length) {
    const side = Math.min(rect.w, rect.h);
    const next = scaled[i];
    if (!row.length || worst([...row, next], side) <= worst(row, side)) { row.push(next); i++; }
    else { rect = placeRow(row, rect, out); row = []; }
  }
  if (row.length) placeRow(row, rect, out);
  return out;
}

/**
 * treemap({onOpen(item), onHover(item|null)})  ->  {el, set(items), highlight(path)}
 * items: [{name, path, size, kind: dir|file|other, files}]
 */
export function treemap({ onOpen, onHover } = {}) {
  const el = h('div.treemap', { role: 'group', 'aria-label': 'Space used, largest first' });
  let items = [];
  const tiles = new Map();

  function render() {
    const W = el.clientWidth, H = el.clientHeight;
    if (!W || !H) return;
    clear(el);
    tiles.clear();
    // fold the long tail into one tile so labels stay readable
    const total = items.reduce((t, i) => t + i.size, 0);
    let shown = items.slice(0, 48).filter(i => i.size / total > 0.0035);
    const rest = items.slice(shown.length);
    if (rest.length) {
      const restSize = rest.reduce((t, i) => t + i.size, 0);
      if (restSize > 0) shown = [...shown, { name: `${num(rest.length)} smaller items`, path: null, size: restSize, kind: 'other', files: rest.reduce((t, i) => t + (i.files || 0), 0) }];
    }
    const GAP = 3;
    const rects = squarify(shown, W, H);
    rects.forEach((r, rank) => {
      const it = r.it;
      const w = Math.max(0, r.w - GAP), hh = Math.max(0, r.h - GAP);
      const big = w > 92 && hh > 44;
      const tiny = w < 34 || hh < 22;
      const tile = h('button.tm-tile', {
        class: [`k-${it.kind || 'file'}`, big ? 'big' : '', tiny ? 'tiny' : ''],
        style: {
          left: r.x + 'px', top: r.y + 'px', width: w + 'px', height: hh + 'px',
          '--tint': it.kind === 'dir' ? String(Math.max(0.12, 0.46 - rank * 0.03)) : '0',
        },
        title: `${it.name}\n${bytes(it.size)}${it.files ? ` - ${num(it.files)} files` : ''}`,
        disabled: it.kind !== 'dir' || !it.path ? undefined : undefined,
        'aria-label': `${it.name}, ${bytes(it.size)}`,
        onclick: () => { if (it.kind === 'dir' && it.path) onOpen?.(it); },
        onmouseenter: () => onHover?.(it),
        onmouseleave: () => onHover?.(null),
        onfocus: () => onHover?.(it),
      }, tiny ? null : h('span.tm-name', { text: it.name }), big ? h('span.tm-size.num', { text: bytes(it.size) }) : null);
      if (it.path) tiles.set(it.path, tile);
      el.append(tile);
    });
  }
  new ResizeObserver(() => render()).observe(el);
  return {
    el,
    set(list) { items = [...list].sort((a, b) => b.size - a.size); render(); },
    highlight(path) {
      for (const [p, t] of tiles) t.classList.toggle('hl', p === path);
    },
  };
}
