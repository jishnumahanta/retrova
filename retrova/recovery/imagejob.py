"""Image job: sector-by-sector copy of a whole disk or one partition into a single
image file. Sequential (fastest), captures everything, resumable, sparse (zero
areas are skipped), unreadable areas are retried in small pieces, then filled with
zeros and listed next to the image."""
from __future__ import annotations

import hashlib
import json
import os

from retrova.jobs import Job
from retrova.util import IS_WIN, free_bytes, run as run_cmd

from .common import give_to_user, human, long_path, makedirs_owned_safe
from .source import ReadOnlySource

PROGRESS_SUFFIX = ".retrova-progress"


class ImageJob(Job):
    kind = "image"
    title = "Creating a disk image"
    exclusive = True
    CHUNK = int(os.environ.get("RETROVA_IMG_CHUNK", os.environ.get("T5R_IMG_CHUNK", 8 * 1024 * 1024)))

    def __init__(self, source, offset, total, out_path, reserve_gb, label="", params=None):
        super().__init__(params)
        self.source, self.offset, self.total = source, int(offset or 0), int(total)
        self.out_path = out_path
        self.io_path = long_path(out_path)
        self.prog_path = self.io_path + PROGRESS_SUFFIX
        self.bad_path = self.io_path + ".unreadable.txt"
        self.reserve = int(float(reserve_gb) * 1024 ** 3)
        self.title = f"Creating a disk image of {label}" if label else "Creating a disk image"
        self.stop_reason = ""
        self.pos = 0
        self.start_pos = 0
        self.written = self.skipped_zero = 0
        self.bad: list = []
        self.src_id = None

    def stop(self, reason="Stopped by you - start the image again to continue where it stopped."):
        if not self.stopping:
            self.stop_reason = reason
        super().stop()

    # ---------------- resume
    def _load_resume(self):
        if not os.path.exists(self.io_path):
            return 0
        try:
            with open(self.prog_path, encoding="utf-8") as fh:
                data = json.loads(fh.read())
            if int(data.get("total", -1)) == self.total and data.get("src_id") == self.src_id:
                return min(int(data.get("pos", 0)), self.total)
        except (OSError, ValueError):
            pass
        if not os.path.exists(self.prog_path) and os.path.getsize(self.io_path) >= self.total:
            raise RuntimeError(f"{self.out_path} already exists. Choose another file name - nothing is overwritten.")
        raise RuntimeError(f"{self.out_path} already exists and isn't an unfinished image of this drive. "
                           "Choose another file name - nothing is overwritten.")

    def _save_resume(self):
        tmp = self.prog_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pos": self.pos, "total": self.total, "src_id": self.src_id,
                                 "source": self.source, "offset": self.offset}))
        os.replace(tmp, self.prog_path)
        give_to_user(self.prog_path)

    def _read(self, src, off, n):
        try:
            data = src.pread(off, n)
            if len(data) == n:
                return data
        except OSError:
            pass
        # retry in small pieces so one bad spot doesn't lose the whole chunk
        out, step = bytearray(), 64 * 1024
        for o in range(off, off + n, step):
            m = min(step, off + n - o)
            piece = None
            for _ in range(2):
                try:
                    piece = src.pread(o, m)
                    if len(piece) == m:
                        break
                except OSError:
                    piece = None
            if piece is None or len(piece) != m:
                with self.lock:
                    self.bad.append(o)
                self._log_bad(o, m)
                piece = bytes(m)
            out += piece
        return bytes(out)

    def _log_bad(self, off, n):
        try:
            new = not os.path.exists(self.bad_path)
            with open(self.bad_path, "a", encoding="utf-8") as fh:
                if new:
                    fh.write("# Areas of the source that could not be read; they are zeros in the image.\n"
                             "# offset (bytes)\tlength\n")
                fh.write(f"{off}\t{n}\n")
            if new:
                give_to_user(self.bad_path)
        except OSError:
            pass

    def run(self):
        src = ReadOnlySource(self.source, self.offset, self.total)
        try:
            self.phase = "Preparing…"
            head = src.pread(0, 65536)
            self.src_id = f"{self.total}:{hashlib.sha1(head).hexdigest()}"
            self.pos = self.start_pos = self._load_resume()
            makedirs_owned_safe(os.path.dirname(self.io_path))
            if not os.path.exists(self.io_path):
                open(self.io_path, "xb").close()
                give_to_user(self.io_path)
                if IS_WIN:  # sparse file: skipped zero areas take no space on NTFS
                    run_cmd(["fsutil", "sparse", "setflag", self.out_path], timeout=20)
                self._save_resume()  # mark it as our unfinished image straight away
            self.phase = "Copying every sector…" if not self.start_pos else "Continuing the image…"
            chunks = 0
            with open(self.io_path, "r+b") as out:
                while self.pos < self.total:
                    self.checkpoint()
                    n = min(self.CHUNK, self.total - self.pos)
                    if free_bytes(os.path.dirname(self.out_path)) - n < self.reserve:
                        self.stop(f"The output drive is down to the {human(self.reserve)} you asked to keep free - "
                                  "free up space, then start the image again to continue.")
                        break
                    data = self._read(src, self.pos, n)
                    if data.count(0) == len(data):
                        with self.lock:
                            self.skipped_zero += n
                    else:
                        out.seek(self.pos)
                        out.write(data)
                    with self.lock:
                        self.pos += n
                        self.written += n
                    chunks += 1
                    if chunks % 32 == 0:
                        out.flush()
                        self._save_resume()
                if self.pos >= self.total:
                    out.truncate(self.total)
                    out.flush()
                    os.fsync(out.fileno())
            if self.pos >= self.total:
                try:
                    os.remove(self.prog_path)  # our own progress marker, not user data
                except OSError:
                    pass
                self.phase = "Finished"
            else:
                self._save_resume()
                self.phase = "Stopped"
                self.message = self.stop_reason
        except Exception:
            if self.pos and os.path.exists(self.io_path):
                try:
                    self._save_resume()
                except OSError:
                    pass
            raise
        finally:
            src.close()

    def status(self):
        with self.lock:
            speed = self.rate(self.written)
            eta = (self.total - self.pos) / speed if speed > 0 and self.state == "running" else None
            self.stats = {"bytes_done": self.pos, "bytes_total": self.total, "speed": speed, "eta": eta,
                          "zero": self.skipped_zero, "failed": len(self.bad)}
            self.progress = self.pos / self.total if self.total else None
        return super().status()

    def extra_status(self):
        return {"stop_reason": self.stop_reason, "pos": self.pos, "total": self.total, "zero": self.skipped_zero,
                "bad": len(self.bad), "bad_first": [hex(b) for b in self.bad[:20]], "out": self.out_path,
                "bad_log": self.bad_path if self.bad else "", "source": self.source, "offset": self.offset,
                "free": free_bytes(os.path.dirname(self.out_path) or ".")}

    def result(self, q):
        off = max(0, int(q.get("offset") or 0))
        lim = max(1, min(int(q.get("limit") or 1000), 100000))
        with self.lock:
            return {"bad": self.bad[off:off + lim], "total": len(self.bad), "chunk": 64 * 1024}
