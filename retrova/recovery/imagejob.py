"""Image job: copy of a whole disk or one partition into a single image file, careful
with failing drives (the way GNU ddrescue works). Read-only on the source.

  1. Copy   big reads; on an error the area is marked and skipped (the skip grows while
            errors continue), so the healthy data is saved first, before the drive
            degrades further. Skipped areas are read afterwards.
  2. Trim   each failed area is read inward from both edges in small blocks.
  3. Scrape what is left is read sector by sector.
  4. Retry  sectors that still fail are tried again (default one extra pass).

Progress lives in a ddrescue-compatible mapfile (`<image>.map`), so Stop and a restart of
the app continue exactly where it stopped, and GNU ddrescue can carry on from the same
map. Zero areas are skipped (sparse image). Unreadable areas are zeros in the image and
listed in `<image>.unreadable.txt`. A complete image with nothing unreadable also gets a
SHA-256 fingerprint in `<image>.sha256`."""
from __future__ import annotations

import hashlib
import json
import os
import time

from retrova.jobs import Job, StopJob
from retrova.util import IS_WIN, free_bytes, run as run_cmd

from .common import give_to_user, human, long_path, makedirs_owned_safe
from .rescuemap import BAD, FINISHED, NON_SCRAPED, NON_TRIED, NON_TRIMMED, RescueMap
from .source import ReadOnlySource, logical_sector_size

PROGRESS_SUFFIX = ".retrova-progress"
MAP_SUFFIX = ".map"
PHASES = {NON_TRIED: ("copy", "Copying"), NON_TRIMMED: ("trim", "Trimming"),
          NON_SCRAPED: ("scrape", "Scraping"), BAD: ("retry", "Retrying")}
PHASE_TEXT = {"copy": "Copying - reading the healthy parts first",
              "trim": "Trimming - checking the edges of damaged areas",
              "scrape": "Scraping - reading damaged areas sector by sector",
              "retry": "Retrying - one more try at the unreadable sectors"}


class ImageJob(Job):
    kind = "image"
    title = "Creating a disk image"
    exclusive = True
    CHUNK = int(os.environ.get("RETROVA_IMG_CHUNK", os.environ.get("T5R_IMG_CHUNK", 8 * 1024 * 1024)))
    MIN_SKIP = 64 * 1024            # first jump after a read error; doubles while errors continue
    TRIM_BLOCK = 4096
    SAVE_EVERY = 2.0                # seconds between map saves

    def __init__(self, source, offset, total, out_path, reserve_gb, label="", params=None,
                 retries=1, reverse=False, sector=None):
        super().__init__(params)
        self.source, self.offset, self.total = source, int(offset or 0), int(total)
        self.out_path = out_path
        self.io_path = long_path(out_path)
        self.prog_path = self.io_path + PROGRESS_SUFFIX
        self.map_path = self.io_path + MAP_SUFFIX
        self.bad_path = self.io_path + ".unreadable.txt"
        self.sha_path = self.io_path + ".sha256"
        self.reserve = int(float(reserve_gb) * 1024 ** 3)
        self.retries = max(0, int(retries))
        self.reverse = bool(reverse)
        self.sector = int(sector) if sector else None   # None: the device's logical sector size, found when the job starts
        self.title = f"Creating a disk image of {label}" if label else "Creating a disk image"
        self.stop_reason = ""
        self.map = RescueMap(self.total)
        self.mode, self.cur_pass, self.pos = NON_TRIED, 1, 0
        self.phase_total = 0
        self.read_ok = self.skipped_zero = 0
        self.sha256 = ""
        self.src_id = None
        self.started = time.time()
        self.last_save = 0.0
        self.ready = False   # True once the image file is ours (created or resumed)

    def stop(self, reason="Stopped by you - start the image again to continue where it stopped."):
        if not self.stopping:
            self.stop_reason = reason
        super().stop()

    def _open_source(self):
        return ReadOnlySource(self.source, self.offset, self.total, align=self.sector)

    # ---------------- resume
    def _load_resume(self):
        """Pick up an unfinished image of this drive; refuse anything else (nothing is overwritten)."""
        if not os.path.exists(self.io_path):
            if os.path.exists(self.map_path):
                raise RuntimeError(f"{self.out_path + MAP_SUFFIX} already exists. Choose another file name - nothing is overwritten.")
            return False
        try:
            with open(self.prog_path, encoding="utf-8") as fh:
                data = json.loads(fh.read())
            if int(data.get("total", -1)) != self.total or data.get("src_id") != self.src_id:
                data = None
        except (OSError, ValueError):
            data = None
        if data is None:
            if not os.path.exists(self.prog_path) and os.path.getsize(self.io_path) >= self.total:
                raise RuntimeError(f"{self.out_path} already exists. Choose another file name - nothing is overwritten.")
            raise RuntimeError(f"{self.out_path} already exists and isn't an unfinished image of this drive. "
                               "Choose another file name - nothing is overwritten.")
        try:
            with open(self.map_path, encoding="utf-8") as fh:
                self.map, self.pos, self.mode, self.cur_pass = RescueMap.from_text(fh.read(), self.total)
        except FileNotFoundError:   # progress file from an older Retrova: everything before `pos` is copied
            self.map.set(0, min(int(data.get("pos", 0)), self.total), FINISHED)
            self.pos = min(int(data.get("pos", 0)), self.total)
        except (OSError, ValueError) as ex:
            raise RuntimeError(f"The map file {self.out_path + MAP_SUFFIX} can't be read ({ex}). "
                               "Choose another file name - nothing is overwritten.")
        return True

    def _write_atomic(self, path, text):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)

    def _save(self, out=None):
        """Flush the image, then record the map (so the map never claims more than the file holds)."""
        if out is not None:
            out.flush()
            os.fsync(out.fileno())
        new = not os.path.exists(self.map_path)
        with self.lock:
            text = self.map.to_text(self.pos, self.mode, self.cur_pass, self.started, "Retrova (compatible with GNU ddrescue)")
        self._write_atomic(self.map_path, text)
        if new:
            give_to_user(self.map_path)
        self.last_save = time.monotonic()

    def _tick(self, out):
        self.checkpoint()
        if time.monotonic() - self.last_save >= self.SAVE_EVERY:
            self._save(out)

    # ---------------- reading and writing
    def _try_read(self, src, off, n):
        """One attempt; None when the drive can't give these bytes."""
        try:
            data = src.pread(off, n)
        except OSError:
            return None
        return data if len(data) == n else None

    def _store(self, out, off, data):
        if data.count(0) == len(data):
            self.skipped_zero += len(data)
        else:
            out.seek(off)
            out.write(data)
        with self.lock:
            self.read_ok += len(data)
            self.map.set(off, len(data), FINISHED)

    def _mark(self, off, n, status):
        with self.lock:
            self.map.set(off, n, status)

    def _check_space(self, n):
        if free_bytes(os.path.dirname(self.out_path)) - n < self.reserve:
            self.stop(f"The output drive is down to the {human(self.reserve)} you asked to keep free - "
                      "free up space, then start the image again to continue.")
            self.checkpoint()

    def _begin(self, mode, pass_no=1, pos=0):
        self.mode, self.cur_pass, self.pos = mode, pass_no, pos
        self.phase_total = self.map.bytes_of(mode)
        self.phase = PHASE_TEXT[PHASES[mode][0]] if mode in PHASES else "Finishing"

    # ---------------- passes
    def _copy(self, src, out):
        """Pass 1 jumps over damage; pass 2 reads whatever was jumped over."""
        while self.cur_pass <= 2:
            skipping, skip = self.cur_pass == 1, self.MIN_SKIP
            max_skip = max(self.CHUNK, min(1 << 30, self.total // 100))
            while True:
                self._tick(out)
                blk = self.map.find(NON_TRIED, self.pos)
                if not blk:
                    break
                start, n = blk[0], min(self.CHUNK, blk[1])
                self._check_space(n)
                data = self._try_read(src, start, n)
                self.pos = start + n
                if data is None:
                    self._mark(start, n, NON_TRIMMED)
                    if skipping:
                        self.pos += skip
                        skip = min(skip * 2, max_skip)
                else:
                    self._store(out, start, data)
                    skip = self.MIN_SKIP
            self._begin(NON_TRIED, self.cur_pass + 1)
        self._begin(NON_TRIMMED)

    def _trim(self, src, out):
        """Read each failed area from its start, then from its end, until errors stop it."""
        while True:
            self._tick(out)
            blk = self.map.find(NON_TRIMMED)
            if not blk:
                break
            lo, end = blk[0], blk[0] + blk[1]
            hi = end
            while lo < end:
                n = min(self.TRIM_BLOCK, end - lo)
                data = self._try_read(src, lo, n)
                if data is None:
                    break
                self._store(out, lo, data)
                lo += n
                self.pos = lo
                self._tick(out)
            while hi > lo:
                n = min(self.TRIM_BLOCK, hi - lo)
                data = self._try_read(src, hi - n, n)
                if data is None:
                    break
                self._store(out, hi - n, data)
                hi -= n
                self.pos = hi
                self._tick(out)
            if hi > lo:
                self._mark(lo, hi - lo, NON_SCRAPED)
        self._begin(NON_SCRAPED, pos=self.total if self.reverse else 0)

    def _sectors(self, start, size):
        offs = range(start, start + size, self.sector)
        return reversed(offs) if self.reverse else offs

    def _scrape(self, src, out):
        """Sector by sector; a sector that fails becomes 'bad'."""
        while True:
            self._tick(out)
            blk = self.map.find(NON_SCRAPED, self.total if self.reverse else 0, self.reverse)
            if not blk:
                break
            for off in self._sectors(*blk):
                n = min(self.sector, blk[0] + blk[1] - off)
                data = self._try_read(src, off, n)
                self.pos = off
                if data is None:
                    self._mark(off, n, BAD)
                else:
                    self._store(out, off, data)
                self._tick(out)
        self._after_scrape()

    def _after_scrape(self):
        if self.retries and self.map.bytes_of(BAD):
            self._begin(BAD, 1, self.total if self.reverse else 0)
        else:
            self._begin(FINISHED)

    def _retry(self, src, out):
        """Another try at every bad sector; those that fail stay bad."""
        while self.cur_pass <= self.retries and self.map.bytes_of(BAD):
            while True:
                blk = self.map.find(BAD, self.pos, self.reverse)
                if not blk:
                    break
                for off in self._sectors(*blk):
                    n = min(self.sector, blk[0] + blk[1] - off)
                    data = self._try_read(src, off, n)
                    self.pos = off + n if not self.reverse else off
                    if data is not None:
                        self._store(out, off, data)
                    self._tick(out)
            self._begin(BAD, self.cur_pass + 1, self.total if self.reverse else 0)
        self._begin(FINISHED)

    def _work(self, src, out):
        steps = {NON_TRIED: self._copy, NON_TRIMMED: self._trim, NON_SCRAPED: self._scrape, BAD: self._retry}
        self._begin(self.mode, self.cur_pass, self.pos)
        while self.mode in steps:
            steps[self.mode](src, out)

    # ---------------- results
    def unfinished(self):
        return self.map.areas(NON_TRIED, NON_TRIMMED, NON_SCRAPED, BAD)

    def _write_bad_log(self):
        areas = self.unfinished()
        try:
            if not areas:
                if os.path.exists(self.bad_path):
                    os.remove(self.bad_path)  # our own list, nothing is left to list
                return
            new = not os.path.exists(self.bad_path)
            self._write_atomic(self.bad_path, "# Areas of the source that could not be read; they are zeros in the image.\n"
                               "# offset (bytes)\tlength\n" + "".join(f"{s}\t{z}\n" for s, z, _ in areas))
            if new:
                give_to_user(self.bad_path)
        except OSError:
            pass

    def _fingerprint(self):
        """SHA-256 of the finished image, in `sha256sum` format."""
        self.phase = "Saving a fingerprint (SHA-256) of the finished image…"
        digest, done = hashlib.sha256(), 0
        try:
            with open(self.io_path, "rb") as fh:
                while True:
                    self.checkpoint()
                    block = fh.read(8 * 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    done += len(block)
                    self.pos = done
        except StopJob:
            self.message = "The image is complete. You stopped before its fingerprint was saved."
            return
        self.sha256 = digest.hexdigest()
        self._write_atomic(self.sha_path, f"{self.sha256}  {os.path.basename(self.out_path)}\n")
        give_to_user(self.sha_path)

    def _finish(self):
        with open(self.io_path, "r+b") as fh:
            fh.truncate(self.total)
            fh.flush()
            os.fsync(fh.fileno())
        self._save()
        self._write_bad_log()
        try:
            os.remove(self.prog_path)  # our own progress marker, not user data
        except OSError:
            pass
        bad = self.map.bytes_of(BAD, NON_TRIMMED, NON_SCRAPED, NON_TRIED)
        if bad:
            self.phase = "Finished"
            self.message = (f"{human(bad)} could not be read and are zeros in the image. They are listed in "
                            f"{os.path.basename(self.bad_path)}; the .map file lets GNU ddrescue keep trying.")
        else:
            self._fingerprint()
            self.phase = "Finished"

    def run(self):
        if self.sector is None:
            self.sector = logical_sector_size(self.source)
        src = self._open_source()
        out = None
        try:
            self.phase = "Preparing…"
            head = src.pread(0, 65536)
            self.src_id = f"{self.total}:{hashlib.sha1(head).hexdigest()}"
            resumed = self._load_resume()
            makedirs_owned_safe(os.path.dirname(self.io_path))
            if not resumed:
                open(self.io_path, "xb").close()
                give_to_user(self.io_path)
                if IS_WIN:  # sparse file: skipped zero areas take no space on NTFS
                    run_cmd(["fsutil", "sparse", "setflag", self.out_path], timeout=20)
                self._write_atomic(self.prog_path, json.dumps({"total": self.total, "src_id": self.src_id,
                                                               "source": self.source, "offset": self.offset}))
                give_to_user(self.prog_path)
                self._save()  # mark it as our unfinished image straight away
            self.ready = True
            out = open(self.io_path, "r+b")
            self._work(src, out)
            out.close()
            out = None
            self._finish()
        finally:
            try:
                if out is not None:
                    self._save(out)
                    out.close()
                if self.ready and self.mode != FINISHED:
                    self._write_bad_log()
                    if self.stopping:
                        self.phase, self.message = "Stopped", self.stop_reason
            except OSError:
                pass
            src.close()

    # ---------------- reporting
    def _progress(self):
        if self.mode == FINISHED:
            return 1.0
        if self.mode == BAD:
            frac = self.pos / self.total if self.total else 0
            return 1 - frac if self.reverse else frac
        return 1 - self.map.bytes_of(self.mode) / self.phase_total if self.phase_total else 1.0

    def status(self):
        with self.lock:
            speed = self.rate(self.read_ok)
            rescued = self.map.bytes_of(FINISHED)
            todo = self.map.bytes_of(NON_TRIED)
            eta = todo / speed if speed > 0 and self.state == "running" and self.mode == NON_TRIED else None
            bad = self.map.bytes_of(BAD, NON_TRIMMED, NON_SCRAPED)
            self.stats = {"bytes_done": rescued, "bytes_total": self.total, "speed": speed, "eta": eta,
                          "zero": self.skipped_zero, "failed": self._error_areas(), "rescued": rescued,
                          "bad_bytes": bad, "error_areas": self._error_areas()}
            self.progress = self._progress()
        return super().status()

    def _error_areas(self):
        return len(self.map.areas(NON_TRIMMED, NON_SCRAPED, BAD))

    def extra_status(self):
        phase_id = PHASES.get(self.mode, ("done", ""))[0]
        return {"stop_reason": self.stop_reason, "pos": self.pos, "total": self.total, "zero": self.skipped_zero,
                "bad": self._error_areas(), "bad_bytes": self.map.bytes_of(BAD, NON_TRIMMED, NON_SCRAPED),
                "rescued": self.map.bytes_of(FINISHED), "error_areas": self._error_areas(),
                "phase_id": phase_id, "pass": self.cur_pass, "retries": self.retries, "out": self.out_path,
                "map_path": self.map_path, "bad_log": self.bad_path if os.path.exists(self.bad_path) else "",
                "sha256": self.sha256, "sha256_path": self.sha_path if self.sha256 else "",
                "source": self.source, "offset": self.offset, "free": free_bytes(os.path.dirname(self.out_path) or ".")}

    def result(self, q):
        off = max(0, int(q.get("offset") or 0))
        lim = max(1, min(int(q.get("limit") or 1000), 100000))
        with self.lock:
            areas = self.map.areas(NON_TRIMMED, NON_SCRAPED, BAD)
            return {"areas": [{"offset": s, "length": z, "status": t} for s, z, t in areas[off:off + lim]],
                    "total": len(areas)}
