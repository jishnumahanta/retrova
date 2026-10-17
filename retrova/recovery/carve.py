"""Deep scan (signature carving, PhotoRec-style): read a raw device or image
read-only in big chunks, find file starts by their signatures, work out each
file's end by parsing its format (see carve_formats), validate it and save it to
out/<type>/retrova-<offset>.<ext>. Zero areas are skipped fast, unreadable areas
are retried in small pieces and treated as zeros, the scan is resumable."""
from __future__ import annotations

import collections
import json
import os
import time

from retrova.jobs import Job, StopJob
from retrova.util import free_bytes

from .carve_formats import MAX_MAGIC_SPAN, SIGNATURES, TYPES
from .common import PART_SUFFIX, give_to_user, human, makedirs_owned_safe, unique_name
from .source import ReadOnlySource

CHUNK = int(os.environ.get("RETROVA_CARVE_CHUNK", 16 * 1024 * 1024))
OVERLAP = max(64, MAX_MAGIC_SPAN)
BLOCK = 1024 * 1024
PROGRESS_FILE = ".retrova-carve-progress.json"


class CarveReader:
    """Random-access reads over [0, end) of the source with a small block cache.
    Unreadable spots come back as zeros and are counted."""

    def __init__(self, src: ReadOnlySource, end: int, on_bad):
        self.src, self.scan_end, self.on_bad = src, end, on_bad
        self.cache: collections.OrderedDict[int, bytes] = collections.OrderedDict()
        self.chunk_off, self.chunk = 0, b""

    def _pread(self, off, n):
        n = max(0, min(n, self.scan_end - off))
        if n == 0:
            return b""
        try:
            data = self.src.pread(off, n)
            if len(data) == n:
                return data
        except OSError:
            data = None
        out = bytearray()
        step = 64 * 1024
        for o in range(off, off + n, step):
            m = min(step, off + n - o)
            piece = None
            for _ in range(2):
                try:
                    piece = self.src.pread(o, m)
                    if len(piece) == m:
                        break
                except OSError:
                    piece = None
            if piece is None or len(piece) != m:
                self.on_bad(o, m)
                piece = bytes(m)
            out += piece
        return bytes(out)

    def set_chunk(self, off, data):
        self.chunk_off, self.chunk = off, data

    def read_chunk(self, off, n):
        return self._pread(off, n)

    def _block(self, b):
        data = self.cache.get(b)
        if data is not None:
            self.cache.move_to_end(b)
            return data
        data = self._pread(b * BLOCK, BLOCK)
        self.cache[b] = data
        if len(self.cache) > 48:
            self.cache.popitem(last=False)
        return data

    def read(self, off, n):
        if off < 0 or n <= 0 or off >= self.scan_end:
            return b""
        n = min(n, self.scan_end - off)
        c0 = self.chunk_off
        if c0 <= off and off + n <= c0 + len(self.chunk):
            return self.chunk[off - c0:off - c0 + n]
        if n > 4 * BLOCK:
            return self._pread(off, n)
        out, o, left = [], off, n
        while left > 0:
            b = o // BLOCK
            data = self._block(b)
            i = o - b * BLOCK
            piece = data[i:i + left]
            if not piece:
                break
            out.append(piece)
            o += len(piece)
            left -= len(piece)
        return b"".join(out)


class CarveJob(Job):
    kind = "carve"
    title = "Deep scan for lost files"
    exclusive = True

    def __init__(self, source, start, end, out, types, reserve_gb=2.0, aligned=False, params=None):
        super().__init__(params)
        self.source, self.scan_start, self.scan_end = source, int(start), int(end)
        self.out = out
        self.types = set(types)
        self.reserve = int(float(reserve_gb) * 1024 ** 3)
        self.aligned = bool(aligned)
        name = os.path.basename(source.rstrip("/\\")) or source
        self.title = f"Deep scan of {name}"
        self.pos = self.scan_start
        self.resumed_from = None
        self.zero_bytes = 0
        self.bad_areas = 0
        self.bad_bytes = 0
        self.skipped = 0
        self.counts: collections.Counter = collections.Counter()
        self.items: list = []
        self.stop_reason = ""
        makedirs_owned_safe(out)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.log_path = os.path.join(out, f"retrova-deep-scan-{stamp}.csv")
        self.prog_path = os.path.join(out, PROGRESS_FILE)
        self.sigs = [s for s in SIGNATURES if s[3] & self.types]
        self._log = None
        self._last_save = 0.0

    def stop(self, reason="Stopped by you - start the deep scan again with the same settings to continue."):
        if not self.stopping:
            self.stop_reason = reason
        super().stop()

    # ---------------- resume
    def _key(self):
        return {"source": self.source, "start": self.scan_start, "end": self.scan_end, "types": sorted(self.types),
                "aligned": self.aligned}

    def _load_resume(self):
        try:
            with open(self.prog_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return self.scan_start
        if {k: data.get(k) for k in self._key()} != self._key():
            return self.scan_start
        pos = int(data.get("pos", self.scan_start))
        return pos if self.scan_start <= pos < self.scan_end else self.scan_start

    def _save_resume(self, done=False):
        try:
            if done:
                if os.path.exists(self.prog_path):
                    os.remove(self.prog_path)      # our own progress marker
                return
            with open(self.prog_path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(dict(self._key(), pos=self.pos), fh)
            os.replace(self.prog_path + ".tmp", self.prog_path)
            give_to_user(self.prog_path)
        except OSError:
            pass

    def _on_bad(self, off, n):
        with self.lock:
            self.bad_areas += 1
            self.bad_bytes += n

    # ---------------- main loop
    def run(self):
        src = ReadOnlySource(self.source)
        rd = CarveReader(src, self.scan_end, self._on_bad)
        try:
            self.pos = self._load_resume()
            if self.pos > self.scan_start:
                self.resumed_from = self.pos
            self.phase = "Scanning for lost files…"
            skip_until = self.pos
            zeros = bytes(CHUNK)
            while self.pos < self.scan_end:
                self.checkpoint()
                pos = self.pos
                n = min(CHUNK, self.scan_end - pos)
                buf = rd.read_chunk(pos, n + OVERLAP)
                rd.set_chunk(pos, buf)
                if buf[:n] == zeros[:n]:
                    with self.lock:
                        self.zero_bytes += n
                        self.pos = pos + n
                    self._maybe_save()
                    continue
                hits = self._aligned_hits(buf, pos, n, skip_until) if self.aligned else \
                    self._all_hits(buf, pos, n, skip_until)
                hits.sort(key=lambda h: h[0])
                for a, fn, ftypes in hits:
                    if a < skip_until:
                        continue
                    self.checkpoint()
                    cap = min(max(TYPES[t][3] for t in ftypes), self.scan_end - a)
                    try:
                        r = fn(rd, a, cap)
                    except StopJob:
                        raise
                    except Exception:
                        r = None
                    if not r:
                        continue
                    length, typ = r
                    if typ not in self.types or length <= 0 or length > TYPES[typ][3]:
                        continue
                    self._save(rd, a, length, typ)
                    skip_until = a + length
                    rd.set_chunk(pos, buf)
                with self.lock:
                    self.pos = max(pos + n, min(skip_until, self.scan_end))
                self._maybe_save()
            self.phase = "Finished"
            self._save_resume(done=True)
        except StopJob:
            self.phase = "Stopped"
            self.message = self.stop_reason
            self._save_resume()
        except Exception:
            self._save_resume()
            raise
        finally:
            src.close()
            if self._log:
                try:
                    self._log.close()
                except OSError:
                    pass

    def _all_hits(self, buf, pos, n, skip_until):
        """Signature hits at any byte offset (thorough: also finds files embedded at odd offsets)."""
        hits = []
        for magic, moff, fn, ftypes in self.sigs:
            i = buf.find(magic)
            while i >= 0:
                st = i - moff
                if 0 <= st < n and pos + st >= skip_until:
                    hits.append((pos + st, fn, ftypes))
                i = buf.find(magic, i + 1)
        return hits

    def _aligned_hits(self, buf, pos, n, skip_until):
        """Only at 512-byte sector starts, where filesystems put files (several times faster)."""
        if not hasattr(self, "_by2"):
            self._by2 = {}
            self._at4 = []
            for sig in self.sigs:
                if sig[1] == 0:
                    self._by2.setdefault(sig[0][:2], []).append(sig)
                else:
                    self._at4.append(sig)
        hits = []
        by2, at4 = self._by2, self._at4
        first = (-pos) % 512
        for st in range(first, n, 512):
            sigs = by2.get(buf[st:st + 2])
            if sigs:
                for magic, moff, fn, ftypes in sigs:
                    if buf.startswith(magic, st):
                        hits.append((pos + st, fn, ftypes))
            for magic, moff, fn, ftypes in at4:
                if buf.startswith(magic, st + moff):
                    hits.append((pos + st, fn, ftypes))
        return [h for h in hits if h[0] >= skip_until]

    def _maybe_save(self):
        if time.time() - self._last_save > 20:
            self._last_save = time.time()
            self._save_resume()

    def _save(self, rd, off, length, typ):
        ext = TYPES[typ][0]
        d = os.path.join(self.out, typ)
        makedirs_owned_safe(d)
        dp = os.path.join(d, f"retrova-{off}.{ext}")
        if os.path.lexists(dp):
            if os.path.isfile(dp) and os.path.getsize(dp) == length:
                with self.lock:
                    self.skipped += 1
                    self.counts[typ] += 1
                    self.items.append({"path": dp, "type": typ, "size": length, "offset": off})
                return
            dp = unique_name(dp)
        if free_bytes(self.out) - length < self.reserve:
            self.stop(f"The output drive is down to the {human(self.reserve)} you asked to keep free. Free up "
                      f"space or choose another drive, then start the deep scan again to continue.")
            raise StopJob()
        part = dp + PART_SUFFIX
        with open(part, "wb") as fh:
            give_to_user(part)
            o, left = off, length
            while left > 0:
                self.checkpoint()
                m = min(8 * 1024 * 1024, left)
                data = rd.read(o, m)
                if not data:
                    break
                fh.write(data)
                o += len(data)
                left -= len(data)
        os.rename(part, dp)
        item = {"path": dp, "type": typ, "size": length, "offset": off}
        with self.lock:
            self.counts[typ] += 1
            self.items.append(item)
        self._write_log(item)

    def _write_log(self, item):
        try:
            if self._log is None:
                new = not os.path.exists(self.log_path)
                self._log = open(self.log_path, "a", encoding="utf-8")
                if new:
                    give_to_user(self.log_path)
                    self._log.write("type,size,offset,path\n")
            p = item["path"].replace('"', '""')
            self._log.write(f'{item["type"]},{item["size"]},{item["offset"]},"{p}"\n')
            self._log.flush()
        except OSError:
            pass

    # ---------------- status
    def status(self):
        with self.lock:
            done = self.pos - self.scan_start
            total = self.scan_end - self.scan_start
            speed = self.rate(done)
            self.stats = {"bytes_done": done, "bytes_total": total, "speed": speed,
                          "eta": (total - done) / speed if speed > 0 and self.state == "running" else None,
                          "found": len(self.items), "failed": self.bad_areas, "skipped": self.skipped,
                          "zero": self.zero_bytes}
            self.progress = done / total if total else None
        return super().status()

    def extra_status(self):
        return {"counts": dict(self.counts), "found": len(self.items), "out": self.out, "pos": self.pos,
                "start": self.scan_start, "end": self.scan_end, "zero_bytes": self.zero_bytes, "bad_areas": self.bad_areas,
                "bad_bytes": self.bad_bytes, "resumed_from": self.resumed_from, "types": sorted(self.types),
                "recent": self.items[-8:], "log": self.log_path if self.items else "",
                "stop_reason": self.stop_reason, "source": self.source}

    def result(self, q):
        """q: offset, limit, type -> {counts, items, total, more}."""
        off = max(0, int(q.get("offset") or 0))
        lim = max(1, min(int(q.get("limit") or 500), 5000))
        typ = str(q.get("type") or "")
        with self.lock:
            items = [i for i in self.items if not typ or i["type"] == typ] if typ else list(self.items)
            counts = dict(self.counts)
        page = items[off:off + lim]
        return {"counts": counts, "items": page, "total": len(items), "more": max(0, len(items) - off - len(page))}
