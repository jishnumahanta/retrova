// Virtual list for long, fixed-height rows (folders with tens of thousands of files).
import { h } from '../util.js';

/**
 * vlist({rowHeight, count, render(i) -> Element, overscan})
 * returns {el, setCount(n), refresh(), scrollTo(i), visibleRange()}
 */
export function vlist({ rowHeight = 36, count = 0, render, overscan = 8, cls = '' }) {
  const spacer = h('div.vl-spacer', { style: { position: 'relative', width: '100%' } });
  const el = h('div.tbody', { class: cls }, spacer);
  let n = count;
  let first = -1, last = -1;
  const rows = new Map();

  function layout(force = false) {
    spacer.style.height = n * rowHeight + 'px';
    const top = el.scrollTop, height = el.clientHeight || 600;
    const f = Math.max(0, Math.floor(top / rowHeight) - overscan);
    const l = Math.min(n - 1, Math.ceil((top + height) / rowHeight) + overscan);
    if (!force && f === first && l === last) return;
    first = f; last = l;
    for (const [i, r] of rows) if (i < f || i > l || force) { r.remove(); rows.delete(i); }
    for (let i = f; i <= l; i++) {
      if (rows.has(i)) continue;
      const r = render(i);
      r.style.position = 'absolute';
      r.style.top = i * rowHeight + 'px';
      r.style.left = '0';
      r.style.right = '0';
      r.dataset.index = i;
      spacer.append(r);
      rows.set(i, r);
    }
  }
  el.addEventListener('scroll', () => layout(), { passive: true });
  new ResizeObserver(() => layout(true)).observe(el);

  return {
    el,
    setCount(c) { n = c; layout(true); },
    refresh() { layout(true); },
    scrollTo(i) {
      const y = i * rowHeight;
      if (y < el.scrollTop) el.scrollTop = y;
      else if (y + rowHeight > el.scrollTop + el.clientHeight) el.scrollTop = y + rowHeight - el.clientHeight;
      layout();
    },
    row: i => rows.get(i),
    get count() { return n; },
  };
}
