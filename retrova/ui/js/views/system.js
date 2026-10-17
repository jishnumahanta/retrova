// System: memory, the apps using it, CPU and disks. Free up memory; quit an app.
import { api } from '../api.js';
import { store } from '../store.js';
import { h, clear, bytes, num, dur, pct } from '../util.js';
import { icon, driveIcon } from '../components/icons.js';
import { segRing } from '../components/ring.js';
import { capBar, emptyState, statsGrid } from '../components/controls.js';
import { confirmSheet } from '../components/sheet.js';
import { toast, toastError } from '../components/toast.js';
import { setToolbar } from '../shell.js';
import { elevate } from '../elevate.js';

const PRESSURE = {
  ok: { word: 'Normal', head: 'Memory is fine', sub: 'Apps have the room they need.' },
  warn: { word: 'Raised', head: 'Memory is getting tight', sub: 'Your computer is starting to swap to disk, which slows things down. Quitting an app or two near the top of the list helps.' },
  bad: { word: 'High', head: 'Memory is running out', sub: 'Your computer is swapping heavily. Quit apps you are not using, starting at the top of the list.' },
};
const tone = p => /crit|high|red|bad/i.test(p || '') ? 'bad' : /warn|medium|yellow|elev/i.test(p || '') ? 'warn' : 'ok';

let timer = null;
export default {
  async mount(root) {
    setToolbar({ title: 'System' });
    const ring = segRing({ size: 176, value: 0, state: 'running', center: h('div', h('div.ring-value.num', '–'), h('div.ring-label', 'memory used')) });
    const memHead = h('h2.mem-head');
    const memSub = h('p.mem-sub');
    const memStats = statsGrid([]);
    const freeBtn = h('button.btn', { onclick: () => freeMemory() }, icon('bolt'), 'Free up memory');
    const freeNote = h('p.hint', 'Asks apps to hand back memory they are not using. Nothing is closed.');
    const cpuBox = h('div.cpu-box');
    const topBox = h('div.top-list');
    const disksBox = h('div.disk-list');
    root.append(h('div.sys-grid',
      h('section.panel.sys-mem', h('div.mem-ring', ring.el), h('div.mem-text', memHead, memSub, memStats, h('div.mem-actions', freeBtn, freeNote))),
      h('section.panel.sys-cpu', h('h3.sys-title', icon('cpu'), 'Processor'), cpuBox),
      h('section.panel.sys-top', h('h3.sys-title', icon('memory'), 'Apps using the most memory'), topBox),
      h('section.panel.sys-disks', h('h3.sys-title', icon('drive'), 'Disks'), disksBox)));

    let health = {};
    api.get('/api/drives').then(r => {
      store.drives = r;
      for (const d of r.disks || []) for (const p of d.partitions || []) if (p.mount) health[p.mount] = { state: d.health?.state || 'unknown', detail: d.health?.detail, disk: d, label: p.label };
    }).catch(() => {});

    async function tick() {
      let s;
      try { s = await api.get('/api/system'); } catch (e) {
        clear(root, h('div.panel', emptyState({ title: "Couldn't read this computer's status", body: e.message, actions: [h('button.btn', { onclick: () => tick() }, 'Try again')] })));
        return;
      }
      if (!root.isConnected) return;
      setToolbar({ title: 'System', sub: [s.model, s.os].filter(Boolean).join(', ') });
      const m = s.memory || {};
      const p = (m.percent ?? (m.total ? (m.used / m.total) * 100 : 0)) / 100;
      const t = tone(m.pressure);
      ring.set(p, 'done');
      ring.setTones(Array(12).fill(t === 'ok' ? null : t).map(x => x || 'ok'));
      clear(ring.center.querySelector('.ring-value'), String(Math.round(p * 100)), h('small', '%'));
      memHead.textContent = PRESSURE[t].head;
      memSub.textContent = PRESSURE[t].sub;
      memStats.set([
        { k: 'Used', v: bytes(m.used), small: m.total ? `of ${bytes(m.total)}` : '' },
        { k: 'Available', v: bytes(m.available) },
        { k: 'Pressure', v: m.pressure ? PRESSURE[t].word : '–', tone: t === 'ok' ? 'ok' : t },
        { k: 'Swap used', v: bytes(m.swap_used || 0) },
      ]);
      const cpu = s.cpu || {};
      const fact = (k, v) => v ? h('div.fact', h('span.faint', k), h('span.ellipsis', { text: v })) : null;
      clear(cpuBox,
        h('div.cpu-big', h('span.num', String(Math.round(cpu.percent || 0))), h('small', '%'), h('span.cpu-word', 'busy')),
        capBar((cpu.percent || 0) / 100, cpu.percent > 85 ? 'warn' : ''),
        h('div.facts', fact('Computer', s.model), fact('System', s.os), fact('Cores', cpu.cores ? String(cpu.cores) : ''), fact('Turned on', s.uptime ? `${dur(s.uptime)} ago` : '')));
      const top = (s.top || []).slice(0, 10);
      const maxRss = Math.max(1, ...top.map(a => a.rss || 0));
      clear(topBox, top.length ? top.map(a => h('div.app-row',
        h('span.app-name.ellipsis', { text: a.name }),
        h('span.app-bar', capBar((a.rss || 0) / maxRss, '', 'thin')),
        h('span.num.app-mem', { text: bytes(a.rss) }),
        h('span.num.faint.app-cpu', { text: a.cpu !== undefined ? `${(a.cpu).toFixed(a.cpu < 10 ? 1 : 0)}% CPU` : '' }),
        h('button.btn.sm.ghost.app-quit', { 'aria-label': `Quit ${a.name}`, onclick: () => quit(a) }, 'Quit'))) : h('p.hint', 'No app list available.'));
      clear(disksBox, (s.disks || []).map(d => {
        const used = d.total ? d.used / d.total : null;
        const hl = health[d.mount];
        const st = hl?.state || 'unknown';
        return h('div.disk-row',
          h('span.drive-icon', icon(hl ? driveIcon(hl.disk) : 'drive')),
          h('div.disk-main',
            h('div.disk-top', h('span.ellipsis.disk-name', { text: hl?.label || d.mount, title: d.mount }), h('span.faint.num.nowrap', { text: `${bytes(d.free)} free of ${bytes(d.total)}` })),
            capBar(used, used > 0.95 ? 'bad' : used > 0.88 ? 'warn' : '')),
          h('span.drive-health', { class: st, title: hl?.detail || '' }, h('span.dot', { class: st }), st === 'ok' ? 'Healthy' : st === 'warn' ? 'Needs attention' : st === 'bad' ? 'Failing' : 'No health info'));
      }));
    }

    async function freeMemory() {
      freeBtn.disabled = true;
      try {
        const r = await api.post('/api/system/free_memory', {});
        if (r.needs_admin && !r.ok) {
          elevate({ title: 'Freeing memory needs administrator rights', reason: r.message, returnHash: '#/system' });
        } else {
          const gained = r.after && r.before ? r.after - r.before : 0;
          toast(r.message || (gained > 0 ? `Freed ${bytes(gained)} of memory.` : 'Done. Apps handed back what they could.'), { kind: r.ok ? 'ok' : 'warn' });
          tick();
        }
      } catch (e) { toastError(e); }
      freeBtn.disabled = false;
    }

    async function quit(a) {
      const ok = await confirmSheet({
        icon: 'quit', title: `Quit ${a.name}?`,
        message: `Retrova asks ${a.name} to quit, just like choosing Quit in the app. If you have unsaved work, it may ask you about it first.`,
        confirmLabel: `Quit ${a.name}`,
      });
      if (!ok) return;
      try {
        const r = await api.post('/api/system/quit', { pid: a.pid });
        toast(r.message || `Asked ${a.name} to quit.`, { kind: 'ok' });
        setTimeout(tick, 900);
      } catch (e) { toastError(e); }
    }

    await tick();
    timer = setInterval(() => { if (!document.hidden) tick(); }, 3000);
    return () => clearInterval(timer);
  },
};

export { pct, num };
