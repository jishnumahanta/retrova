// What each job kind is called, its icon, and which screen owns it.
import { stat } from './api.js';
import { bytes, num, plural } from './util.js';

export const KIND = {
  copy: { label: 'Recovery', icon: 'recover', route: 'recover', params: { mode: 'files' } },
  image: { label: 'Disk image', icon: 'disc', route: 'recover', params: { mode: 'image' } },
  carve: { label: 'Deleted file scan', icon: 'scan', route: 'recover', params: { mode: 'carve' } },
  regen: { label: 'Rebuildable folder check', icon: 'search', route: 'recover', params: { mode: 'files' } },
  verify: { label: 'Copy check', icon: 'verify', route: 'verify' },
  analyse: { label: 'Space analysis', icon: 'space', route: 'space' },
  dupes: { label: 'Duplicate search', icon: 'dupes', route: 'duplicates' },
  dupes_apply: { label: 'Duplicate cleanup', icon: 'dupes', route: 'duplicates' },
  clean_scan: { label: 'Cleanup scan', icon: 'cleanup', route: 'cleanup' },
  clean_apply: { label: 'Cleanup', icon: 'trash', route: 'cleanup' },
  sparse_scan: { label: 'VM disk scan', icon: 'shrink', route: 'cleanup', params: { tab: 'sparse' } },
  sparse_apply: { label: 'VM disk shrink', icon: 'shrink', route: 'cleanup', params: { tab: 'sparse' } },
  ocr: { label: 'Text indexing', icon: 'findtext', route: 'find' },
  transfer: { label: 'Transfer', icon: 'copy', route: 'activity' },
};
export const kindMeta = k => KIND[k] || { label: 'Task', icon: 'activity', route: 'activity' };

/** When the job started, in ms, if the engine reports it (`started`, epoch seconds or ms). */
export function startedAt(st) {
  const s = st && (st.started ?? st.t0);
  if (!s) return null;
  return s < 1e12 ? s * 1000 : s;
}

/** One short line summarising a job's numbers, for lists. */
export function jobSummary(st) {
  if (!st) return '';
  if (st.state === 'error') return st.error || st.message || "Couldn't finish";
  const fd = stat(st, 'files_done', 'copied', 'done_files'), ft = stat(st, 'files_total', 'found_files', 'total_files');
  const bd = stat(st, 'bytes_done', 'bytes_copied', 'done_bytes', 'pos'), bt = stat(st, 'bytes_total', 'total_bytes', 'total');
  const found = stat(st, 'found');
  const parts = [];
  if (bt) parts.push(`${bytes(bd || 0)} of ${bytes(bt)}`);
  else if (bd) parts.push(bytes(bd));
  if (ft) parts.push(`${num(fd || 0)} of ${plural(ft, 'file')}`);
  else if (fd) parts.push(plural(fd, 'file'));
  if (found !== undefined && !ft) parts.push(`${num(found)} found`);
  if (!parts.length) return st.phase || st.message || '';
  return parts.join(', ');
}
