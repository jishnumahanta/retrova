// Talking to the local Retrova engine.
// Every request carries the session token; with ?mock=1 a built-in fake backend answers instead.

const params = new URLSearchParams(location.search);
export const TOKEN = params.get('t') || '';
export const MOCK = params.get('mock') === '1';

let mock = null;

export class ApiError extends Error {
  constructor(message, status = 0, data = {}) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

/** Load the fake backend when running with ?mock=1 (call once at boot). */
export async function ready() {
  if (MOCK && !mock) mock = await import('./mock.js');
}

function qs(q) {
  if (!q) return '';
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== '') p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : '';
}

async function request(method, path, body) {
  if (MOCK) {
    try {
      return await mock.handle(method, path, body);
    } catch (e) {
      throw e instanceof ApiError ? e : new ApiError(e.message || String(e), e.status || 400);
    }
  }
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { 'X-Token': TOKEN, 'Content-Type': 'application/json' } : { 'X-Token': TOKEN },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: 'no-store',
    });
  } catch {
    throw new ApiError("Retrova's engine isn't responding. If this keeps happening, quit Retrova and open it again.", 0);
  }
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text || res.statusText }; }
  if (!res.ok) throw new ApiError(data.error || `Something went wrong (error ${res.status}).`, res.status, data);
  return data;
}

export const api = {
  get: (path, q) => request('GET', path + qs(q)),
  post: (path, body = {}) => request('POST', path, body),
};

/** URL for <img>/<video>/downloads: token travels in the query string. */
export function rawUrl(path, q = {}) {
  if (MOCK) return mock.mediaUrl(path, q);
  return path + qs({ ...q, t: TOKEN });
}
export const previewUrl = (sid, path) => rawUrl('/api/recover/preview', { sid, path });
export const thumbUrl = (path, size = 256) => rawUrl('/api/thumb', { path, size });
export const fileUrl = path => rawUrl('/api/file', { path });

// ---------------------------------------------------------------- jobs
export const LIVE_STATES = new Set(['queued', 'running', 'paused', 'stopping']);
export const isLive = st => !!st && LIVE_STATES.has(st.state);

export const jobControl = (id, action) => api.post('/api/job/control', { id, action });
export const jobResult = (id, q = {}) => api.get('/api/job/result', { id, ...q });
export const jobStatus = id => api.get('/api/job', { id });
export async function latestJob(kind) {
  const r = await api.get('/api/jobs', { kind });
  return (r.jobs || [])[0] || null;   // server sorts newest first
}

/**
 * Poll a job until it is no longer live. onStatus(status) is called on every tick
 * (including the final one). Returns {stop(), poke()}.
 */
export function watchJob(id, onStatus, { interval = 800, onError } = {}) {
  let stopped = false;
  let timer = null;
  let failures = 0;
  async function tick() {
    timer = null;
    if (stopped) return;
    try {
      const st = await jobStatus(id);
      if (stopped) return;
      failures = 0;
      onStatus(st);
      if (isLive(st)) timer = setTimeout(tick, document.hidden ? interval * 3 : interval);
    } catch (e) {
      if (stopped) return;
      failures++;
      if (e.status === 404 || failures > 20) { onError?.(e); return; }
      timer = setTimeout(tick, interval * 2);
    }
  }
  tick();
  return {
    stop() { stopped = true; clearTimeout(timer); },
    poke() { if (stopped) return; clearTimeout(timer); tick(); },
  };
}

// Stats helpers: jobs report conventional keys in `stats`, older ports may put them top level.
export function stat(st, ...keys) {
  if (!st) return undefined;
  for (const k of keys) {
    if (st.stats && st.stats[k] !== undefined && st.stats[k] !== null) return st.stats[k];
    if (st[k] !== undefined && st[k] !== null) return st[k];
  }
  return undefined;
}

/** 0..1 or null (unknown) for a job status */
export function progressOf(st) {
  if (!st) return null;
  if (st.state === 'done') return 1;
  if (typeof st.progress === 'number') return Math.max(0, Math.min(1, st.progress));
  const bd = stat(st, 'bytes_done'), bt = stat(st, 'bytes_total');
  if (bt > 0 && bd !== undefined) return Math.min(1, bd / bt);
  const fd = stat(st, 'files_done'), ft = stat(st, 'files_total');
  if (ft > 0 && fd !== undefined) return Math.min(1, fd / ft);
  return null;
}
