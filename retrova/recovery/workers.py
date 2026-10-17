"""Per-file copy and verify work. Runs in threads (macOS/Linux) or in separate
processes (Windows, spawn): everything crossing the queues is plain ints/strs/
tuples, and each worker opens its own read-only filesystem view from the spec.

Copy task:   (handle, src_path, dst_path, size, mtime_ns, atime_ns, mode)
Verify task: (key, handle, None, src_path, dst_path, size, mtime_ns, atime_ns, fix)
"""
from __future__ import annotations

import hashlib
import os
import random
import stat
import time

from .common import CHUNK, INCOMPLETE_SUFFIX, IS_WIN, PART_SUFFIX, give_to_user, human, makedirs_owned_safe, \
    unique_name


class StopCopy(Exception):
    pass


def wait_run(run_ev, stop_ev):
    while not run_ev.wait(0.5):
        if stop_ev.is_set():
            break
    if stop_ev.is_set():
        raise StopCopy()


def _open_view(spec):
    from .fsview import open_view
    return open_view(spec, cache_blocks=8192)


# =========================================================================== copy
def copy_file_core(view, task, run_ev, stop_ev, on_bytes):
    """Copy one file from the read-only view to dst. Returns ('copied'|'skipped', final_dst).
    Never overwrites: a different existing file makes us write '<name>.retrova-N'."""
    handle, sp, dp, size, mt, at, mode = task
    if os.path.lexists(dp):
        if os.path.isfile(dp) and os.path.getsize(dp) == size:
            return "skipped", dp
        dp = unique_name(dp)
    part = dp + PART_SUFFIX
    for attempt in (1, 2):  # one automatic retry for transient read problems
        written = 0
        try:
            fh = view.open_handle(handle)
            with open(part, "wb") as out:
                give_to_user(part)
                while written < size:
                    if not run_ev.is_set() or stop_ev.is_set():
                        wait_run(run_ev, stop_ev)
                    chunk = fh.read(min(CHUNK, size - written))
                    if not chunk:
                        raise IOError(f"unexpected end of data at {written:,} of {size:,} bytes")
                    out.write(chunk)
                    written += len(chunk)
                    on_bytes(len(chunk))
            break
        except StopCopy:
            raise
        except Exception as ex:
            on_bytes(-written)
            if attempt == 2:
                if os.path.exists(part):
                    bad = dp + INCOMPLETE_SUFFIX
                    if os.path.lexists(bad):
                        bad = unique_name(bad)
                    os.rename(part, bad)
                raise IOError(f"{ex} (got {written:,} of {size:,} bytes, kept as {INCOMPLETE_SUFFIX})")
            time.sleep(0.2)
    os.rename(part, dp)
    if not IS_WIN and mode:
        try:
            os.chmod(dp, (mode & 0o777) | 0o200)     # keep it writable for its owner
        except OSError:
            pass
    if mt:
        try:
            os.utime(dp, ns=(at or mt, mt))
        except (OSError, OverflowError, ValueError):
            pass
    return "copied", dp


def copy_worker(idx, spec, task_q, res_q, run_ev, stop_ev):
    """Runs in a thread (macOS/Linux) or a separate process (Windows)."""
    try:
        view = _open_view(spec)
    except Exception as ex:
        res_q.put(("fatal", idx, f"{type(ex).__name__}: {ex}"))
        res_q.put(("exit", idx))
        return
    pending = [0]

    def on_bytes(n):
        pending[0] += n
        if abs(pending[0]) >= 4 * 1024 * 1024:
            res_q.put(("bytes", idx, pending[0]))
            pending[0] = 0

    def flush():
        if pending[0]:
            res_q.put(("bytes", idx, pending[0]))
            pending[0] = 0

    try:
        while True:
            item = task_q.get()
            if item is None:
                break
            sp, size = item[1], item[3]
            if stop_ev.is_set():
                res_q.put(("drop", idx, size))
                continue
            if size > CHUNK:
                res_q.put(("cur", idx, sp, size))
            try:
                if not run_ev.is_set():
                    wait_run(run_ev, stop_ev)
                result, final = copy_file_core(view, item, run_ev, stop_ev, on_bytes)
                flush()
                res_q.put((result, idx, sp, size, final))
            except StopCopy:
                flush()
                res_q.put(("drop", idx, size))
            except Exception as ex:
                flush()
                res_q.put(("err", idx, sp, f"{type(ex).__name__}: {ex}", size))
    finally:
        view.close()
        res_q.put(("exit", idx))


# =========================================================================== verify
V_SAMPLE_BLOCK = 1024 * 1024        # spot-check block
V_SAMPLE_COUNT = 8                  # random blocks per file (plus the first and last)
V_RETRIES = 3                       # attempts per read before an area counts as unreadable
V_NARROW = 1024 * 1024              # an unreadable 8 MB chunk is re-tried in pieces this big
V_PATCH = 64 * 1024                 # repair writes only the 64 KB pieces that differ


def merge_ranges(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def good_ranges(start, end, bad):
    """The parts of [start, end) not covered by the sorted, merged bad ranges."""
    out, pos = [], start
    for a, b in bad:
        a, b = max(a, start), min(b, end)
        if b <= a:
            continue
        if a > pos:
            out.append((pos, a))
        pos = max(pos, b)
    if pos < end:
        out.append((pos, end))
    return out


def diff_blocks(s: bytes, d: bytes, base: int, blk: int = 4096):
    """Compare original bytes s with copy bytes d (d may be shorter: truncated copy).
    Returns (first differing byte, differing ranges, bytes of the copy that are zeros
    where the original has data)."""
    first, ranges, zeros = None, [], 0
    for i in range(0, len(s), blk):
        x, y = s[i:i + blk], d[i:i + blk]
        if x == y:
            continue
        if first is None:
            m = min(len(x), len(y))
            first = base + i + next((j for j in range(m) if x[j] != y[j]), m)
        if ranges and ranges[-1][1] == base + i:
            ranges[-1][1] = base + i + len(x)
        else:
            ranges.append([base + i, base + i + len(x)])
        if y and y.count(0) == len(y) and x[:len(y)].count(0) != len(y):
            zeros += len(y)
    return first, ranges, zeros


class SrcReader:
    """Reads one file from the source through the read-only view. Each read is tried
    V_RETRIES times; a chunk that still fails is narrowed down to 1 MB pieces so only
    the truly unreadable parts are marked."""

    def __init__(self, view, handle):
        self.view, self.handle = view, handle
        self.fh = None
        self.dead = False   # the file can't even be opened (e.g. unsupported compression)
        self.err = ""

    def _try(self, off, n):
        open_fails = 0
        for attempt in range(V_RETRIES):
            try:
                if self.fh is None:
                    try:
                        self.fh = self.view.open_handle(self.handle)
                    except Exception:
                        open_fails += 1
                        raise
                self.fh.seek(off)
                data = self.fh.read(n)
                if len(data) == n:
                    return data
                raise IOError(f"short read: {len(data)} of {n} bytes at byte {off:,}")
            except Exception as ex:
                self.err = f"{type(ex).__name__}: {ex}"
                self.fh = None
                if isinstance(ex, OSError) and "short read" not in str(ex):   # device trouble: give it a moment
                    time.sleep(0.2 * (attempt + 1))
        if open_fails == V_RETRIES and "short read" not in self.err:
            self.dead = True
        return None

    def read(self, off, n):
        """-> (data, bad_ranges). data is None when nothing in the chunk could be read;
        otherwise unreadable parts are zero placeholders listed in bad_ranges."""
        if not self.dead:
            d = self._try(off, n)
            if d is not None:
                return d, []
        if self.dead or n <= V_NARROW:
            return None, [[off, off + n]]
        parts, bad = [], []
        for o in range(off, off + n, V_NARROW):
            m = min(V_NARROW, off + n - o)
            d = None if self.dead else self._try(o, m)
            if d is None:
                bad.append([o, o + m])
                parts.append(bytes(m))
            else:
                parts.append(d)
        if sum(b - a for a, b in bad) >= n:
            return None, merge_ranges(bad)
        return b"".join(parts), merge_ranges(bad)


class DstReader:
    """Reads the copy, retrying read errors."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        self.err = ""

    def read(self, off, n):
        for attempt in range(V_RETRIES):
            try:
                if self.f is None:
                    self.f = open(self.path, "rb")
                self.f.seek(off)
                return self.f.read(n)
            except OSError as ex:
                self.err = str(ex)
                try:
                    self.f.close()
                except Exception:
                    pass
                self.f = None
                time.sleep(0.2 * (attempt + 1))
        return None

    def close(self):
        if self.f:
            self.f.close()


class VCtl:
    """Pause / stop / progress plumbing for one worker."""

    def __init__(self, idx, res_q, run_ev, stop_ev):
        self.idx, self.res_q, self.run_ev, self.stop_ev = idx, res_q, run_ev, stop_ev
        self.pending = 0
        self.file_bytes = 0

    def wait(self):
        if not self.run_ev.is_set() or self.stop_ev.is_set():
            wait_run(self.run_ev, self.stop_ev)

    def progress(self, n):
        self.pending += n
        self.file_bytes += n
        if self.pending >= 4 * 1024 * 1024:
            self.flush()

    def flush(self):
        if self.pending:
            self.res_q.put(("bytes", self.idx, self.pending))
            self.pending = 0


def vres(t, status, detail="", **kw):
    r = {"key": t[0], "sp": t[3], "status": status, "detail": detail, "ranges": [],
         "src_size": t[5], "dst_size": None, "sha": None, "repaired": False, "dst_mtime": None,
         "task": list(t)}
    r.update(kw)
    return r


def _dst_stat(dp):
    """-> (size, mtime_ns) or raises FileNotFoundError / OSError."""
    st = os.stat(dp)
    if stat.S_ISDIR(st.st_mode):
        raise IsADirectoryError(dp)
    return st.st_size, st.st_mtime_ns


def _src_note(src_bad):
    if not src_bad:
        return ""
    n = sum(b - a for a, b in src_bad)
    return (f" Also: {len(src_bad)} area(s) of the original ({human(n)}) can't be read, so those parts "
            f"of the copy are kept as they are.")


def _missing(view, t):
    """A file that isn't in the copy: fixable, unless the source can't read the original either."""
    inc = t[4] + INCOMPLETE_SUFFIX
    note = f" A partial copy is kept as {os.path.basename(inc)}." if os.path.lexists(inc) else ""
    if view is not None and t[5] > 0:
        src = SrcReader(view, t[1])
        if src.read(0, min(t[5], 65536))[0] is None:
            return vres(t, "SRC_BAD", f"Not in the recovered copy, and the original can't be read "
                                      f"either ({src.err}).{note}")
    return vres(t, "MISSING", "Not in the recovered copy." + note)


def v_quick(t, view=None):
    try:
        dsz, mt = _dst_stat(t[4])
    except FileNotFoundError:
        return _missing(view, t)
    except IsADirectoryError:
        return vres(t, "ERROR", "The copy has a folder where the original has a file")
    except OSError as ex:
        return vres(t, "DST_BAD", f"Cannot read the copy's details: {ex}")
    if dsz != t[5]:
        return vres(t, "SIZE", f"Original {human(t[5])} ({t[5]:,} bytes), copy {human(dsz)} ({dsz:,} bytes)",
                    dst_size=dsz)
    return vres(t, "OK", dst_size=dsz, dst_mtime=mt)


def v_sample(view, t, ctl):
    key, handle, _, sp, dp, size = t[:6]
    r = v_quick(t, view)
    if r["status"] != "OK" or size == 0:
        return r
    offs = {0, max(0, size - V_SAMPLE_BLOCK)}
    rnd = random.Random(f"{key}:{size}")
    for _ in range(V_SAMPLE_COUNT):
        offs.add(rnd.randrange(0, max(1, size - V_SAMPLE_BLOCK + 1)))
    src = SrcReader(view, handle)
    try:
        dst = DstReader(dp)
    except OSError as ex:
        return vres(t, "DST_BAD", f"Cannot open the copy: {ex}", dst_size=r["dst_size"])
    bad, src_bad, first, zeros = [], [], None, 0
    try:
        for o in sorted(offs):
            ctl.wait()
            m = min(V_SAMPLE_BLOCK, size - o)
            sd, sbad = src.read(o, m)
            dd = dst.read(o, m)
            if dd is None:
                return vres(t, "DST_BAD", f"The copy can't be read at byte {o:,}: {dst.err}",
                            ranges=[[o, o + m]], dst_size=r["dst_size"])
            if sd is None:
                src_bad.append([o, o + m])
                continue
            src_bad += sbad
            for a, b in good_ranges(o, o + m, sbad):
                f, rg, z = diff_blocks(sd[a - o:b - o], dd[a - o:b - o], a)
                if rg:
                    bad += rg
                    zeros += z
                    first = f if first is None else min(first, f)
    finally:
        dst.close()
    if bad:
        d = f"{len(merge_ranges(bad))} differing area(s) in the sampled blocks, first at byte {first:,}."
        if zeros:
            d += f" {human(zeros)} of it is zeros in the copy where the original has data."
        return vres(t, "CONTENT", d + " Run Every byte (or Repair) for this file." + _src_note(src_bad),
                    ranges=merge_ranges(bad)[:40], dst_size=r["dst_size"])
    if src_bad:
        return vres(t, "SRC_BAD", f"The original can't be read at byte {src_bad[0][0]:,} ({src.err}). "
                                  f"The copy may be the only good version - keep it.",
                    ranges=merge_ranges(src_bad)[:40], dst_size=r["dst_size"])
    return r


def v_full(view, t, ctl, want_hash=True):
    key, handle, _, sp, dp, size = t[:6]
    try:
        dsz, _ = _dst_stat(dp)
    except FileNotFoundError:
        return _missing(view, t)
    except IsADirectoryError:
        ctl.progress(size)
        return vres(t, "ERROR", "The copy has a folder where the original has a file")
    except OSError as ex:
        ctl.progress(size)
        return vres(t, "DST_BAD", f"Cannot read the copy's details: {ex}")
    try:
        dst = DstReader(dp)
    except OSError as ex:
        ctl.progress(size)
        return vres(t, "DST_BAD", f"Cannot open the copy: {ex}", dst_size=dsz)
    src = SrcReader(view, handle)
    h = hashlib.sha256() if want_hash else None
    diffs, src_bad, dst_bad, zeros, first = [], [], [], 0, None
    try:
        off = 0
        while off < size:
            ctl.wait()
            n = min(CHUNK, size - off)
            sd, sbad = src.read(off, n)
            dd = dst.read(off, n)
            if dd is None:
                dst_bad.append([off, off + n])
            else:
                if h is not None:
                    h.update(dd)
                if sd is None:
                    src_bad += sbad
                elif not sbad:
                    if sd != dd:
                        f, rg, z = diff_blocks(sd, dd, off)
                        diffs += rg
                        zeros += z
                        first = f if first is None else first
                else:
                    src_bad += sbad
                    for a, b in good_ranges(off, off + n, sbad):
                        f, rg, z = diff_blocks(sd[a - off:b - off], dd[a - off:b - off], a)
                        if rg:
                            diffs += rg
                            zeros += z
                            first = f if first is None else min(first, f)
            ctl.progress(n)
            off += n
        if h is not None and dsz > size and not dst_bad:
            h = None   # copy is longer than the original: no checksum
    finally:
        dst.close()
    try:
        dsz, mt = _dst_stat(dp)
    except OSError:
        mt = None
    src_bad = merge_ranges(src_bad)
    diffs = merge_ranges(diffs)
    if dst_bad:
        return vres(t, "DST_BAD", f"{len(merge_ranges(dst_bad))} area(s) of the copy can't be read "
                                  f"({dst.err}). The drive holding the copy may be failing.",
                    ranges=merge_ranges(dst_bad)[:40], dst_size=dsz)
    if dsz != size:
        d = f"Original {human(size)} ({size:,} bytes), copy {human(dsz)} ({dsz:,} bytes)."
        if dsz < size:
            d += f" The copy stops at byte {dsz:,}."
        if first is not None and first < min(dsz, size):
            d += f" The content also differs before that, from byte {first:,}."
        if zeros:
            d += f" {human(zeros)} of the copy is zeros where the original has data."
        return vres(t, "SIZE", d + _src_note(src_bad), ranges=diffs[:40], dst_size=dsz)
    if diffs:
        d = f"{len(diffs)} differing area(s), {human(sum(b - a for a, b in diffs))} in total, first at byte {first:,}."
        if zeros:
            d += (f" {human(zeros)} of the copy is zeros where the original has data "
                  f"(typical of a read that failed during copying).")
        return vres(t, "CONTENT", d + _src_note(src_bad), ranges=diffs[:40], dst_size=dsz)
    if src_bad:
        n = sum(b - a for a, b in src_bad)
        return vres(t, "SRC_BAD", f"{len(src_bad)} area(s) of the original ({human(n)}, first at byte "
                                  f"{src_bad[0][0]:,}) can't be read ({src.err}), so those parts can't be "
                                  f"checked. Everything readable matches. The copy may be the only good "
                                  f"version - keep it.",
                    ranges=src_bad[:40], dst_size=dsz, sha=h.hexdigest() if h else None, dst_mtime=mt)
    return vres(t, "OK", dst_size=dsz, dst_mtime=mt, sha=h.hexdigest() if h else None)


def v_repair(view, t, ctl, want_hash=True):
    """Patch only the parts of the copy that differ from the original, then check it again."""
    key, handle, _, sp, dp, size, mt, at = t[:8]
    if os.path.isdir(dp):
        return vres(t, "ERROR", "The copy has a folder where the original has a file - not touched")
    exists = os.path.isfile(dp)
    inc = dp + INCOMPLETE_SUFFIX
    use_inc = not exists and os.path.isfile(inc)   # fill in the partial copy an earlier run kept
    target = dp if exists else inc if use_inc else dp + PART_SUFFIX
    makedirs_owned_safe(os.path.dirname(dp))
    if not exists and not use_inc:
        open(target, "wb").close()
        give_to_user(target)
    src = SrcReader(view, handle)
    patched, areas, src_bad = 0, 0, []
    with open(target, "r+b") as f:
        off = 0
        while off < size:
            ctl.wait()
            n = min(CHUNK, size - off)
            sd, sbad = src.read(off, n)
            try:
                f.seek(off)
                dd = f.read(n)
            except OSError:
                dd = None
            src_bad += sbad
            if sd is not None:
                for a, b in good_ranges(off, off + n, sbad):
                    s = sd[a - off:b - off]
                    d = dd[a - off:b - off] if dd is not None else None
                    if s == d:
                        continue
                    for p in range(0, len(s), V_PATCH):
                        sp_ = s[p:p + V_PATCH]
                        if d is None or d[p:p + V_PATCH] != sp_:
                            f.seek(a + p)
                            f.write(sp_)
                            patched += len(sp_)
                            areas += 1
            ctl.progress(n)
            off += n
        f.truncate(size)
        f.flush()
        os.fsync(f.fileno())
    src_bad = merge_ranges(src_bad)
    note = f"Repaired from the original: rewrote {human(patched)}"
    if not exists:
        if src_bad and use_inc:
            return vres(t, "SRC_BAD", f"Filled in what can be read from the original ({human(patched)}) into "
                                      f"{os.path.basename(inc)}; {len(src_bad)} area(s) still can't be read, "
                                      f"so it keeps that name.", ranges=src_bad[:40], repaired=bool(patched))
        if src_bad and not patched:   # nothing at all could be read: don't leave an empty shell behind
            os.remove(target)          # (our own temporary file, created just above)
            return vres(t, "SRC_BAD", f"Can't recreate it: the original can't be read ({src.err}).",
                        ranges=src_bad[:40])
        if src_bad:
            bad = inc if not os.path.lexists(inc) else unique_name(inc)
            os.rename(target, bad)
            return vres(t, "SRC_BAD", f"Recreated from the original, but {len(src_bad)} area(s) can't be read "
                                      f"there. Saved as {os.path.basename(bad)} (unreadable parts are zeros).",
                        ranges=src_bad[:40], repaired=True)
        if os.path.lexists(dp):        # something appeared there meanwhile: never overwrite it
            other = unique_name(dp)
            os.rename(target, other)
            return vres(t, "ERROR", f"A different file appeared at this place in the copy while repairing; "
                                    f"the recreated file was saved as {os.path.basename(other)}.")
        os.rename(target, dp)
        note = (f"Was missing - completed the partial copy from the original ({human(patched)} written)"
                if use_inc else f"Was missing - recreated from the original ({human(size)})")
    elif areas:
        note += f" in {areas} piece(s)"
    if mt:
        try:
            os.utime(dp, ns=(at or mt, mt))
        except (OSError, OverflowError, ValueError):
            pass
    r = v_full(view, t, ctl, want_hash)     # check what is on disk now
    r["repaired"] = True
    r["detail"] = (note + ". " + r["detail"]).strip() if r["status"] != "OK" else note + ", checked again: matches."
    return r


def verify_one(view, t, mode, ctl, want_hash=True):
    if t[8]:
        return v_repair(view, t, ctl, want_hash)
    if mode == "quick":
        return v_quick(t, view)
    if mode == "sample":
        return v_sample(view, t, ctl)
    return v_full(view, t, ctl, want_hash)


def verify_worker(idx, spec, task_q, res_q, run_ev, stop_ev, mode):
    """Runs in a thread (macOS/Linux) or a separate process (Windows)."""
    try:
        view = _open_view(spec)
    except Exception as ex:
        res_q.put(("fatal", idx, f"{type(ex).__name__}: {ex}"))
        res_q.put(("exit", idx))
        return
    ctl = VCtl(idx, res_q, run_ev, stop_ev)
    try:
        while True:
            t = task_q.get()
            if t is None:
                break
            if stop_ev.is_set():
                res_q.put(("drop", idx, t[0]))
                continue
            expect = t[5] * (2 if t[8] else 1) if (mode == "full" or t[8]) else 0
            if t[5] > CHUNK:
                res_q.put(("cur", idx, t[3], t[5] * (2 if t[8] else 1)))
            ctl.file_bytes = 0
            try:
                ctl.wait()
                r = verify_one(view, t, mode, ctl)
            except StopCopy:
                ctl.flush()
                res_q.put(("drop", idx, t[0]))
                continue
            except Exception as ex:
                r = vres(t, "ERROR", f"{type(ex).__name__}: {ex}")
            if expect and ctl.file_bytes < expect:
                ctl.progress(expect - ctl.file_bytes)        # keep the totals honest after early exits
            ctl.flush()
            res_q.put(("res", idx, r))
    finally:
        view.close()
        res_q.put(("exit", idx))
