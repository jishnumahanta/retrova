"""Transfer: copy recovered files back onto a drive (ported from T5 Restore).

Big files go through a single lane (kind to hard drives), small files in parallel.
Resumable (same size + date = skipped), never overwrites (different file -> name.restore-N),
restores original Mac names from T5 Rescue / Retrova rename logs, fixes permissions,
optional read-back checksum check. The source is only read.
"""
from __future__ import annotations

import collections
import hashlib
import os
import queue
import shutil
import stat
import threading
import time

from ..jobs import JOBS, StopJob
from ..server import route, ApiError
from ..util import IS_WIN
from .common import ToolJob, app_data, clean_path, as_bool, as_int

try:
    import fcntl  # type: ignore
except ImportError:  # Windows
    fcntl = None

CHUNK = 8 * 1024 * 1024
BIG = 64 * 1024 * 1024                  # files at least this big go through the single "big file" lane
SKIP_TOP = {".DocumentRevisions-V100", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
            "_t5rescue_logs", "_recovery-verify", "System Volume Information", "$RECYCLE.BIN"}
SKIP_ANY = {".DS_Store"}
TOP_RENAME = {".Trashes": "Old-Trash"}  # macOS manages .Trashes itself; keep the old Trash visible instead
EXEC_MAGIC = (b"#!", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
              b"\xca\xfe\xba\xbe", b"\x7fELF")
F_NOCACHE = 48                          # macOS: read the real disk, not the memory cache
LOGDIR = app_data("transfer-logs")
SPARSE_MIN = 64 * 1024 * 1024    # files this big get zero regions written as holes


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


# ------------------------------------------------------------------ original names
_WIN_BAD = set('<>:"/\\|?*') | {chr(i) for i in range(32)}
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def win_safe(name):
    """Exactly how T5 Rescue made a Mac name valid on Windows."""
    s = "".join("_" if ch in _WIN_BAD else ch for ch in name)
    if s.endswith((" ", ".")):
        s += "_"
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return s or "_"


def load_renames(src_root):
    """{path on the recovery drive (relative, Windows names): original Mac name} from T5 Rescue's logs."""
    logdir = os.path.join(src_root, "_t5rescue_logs")
    out_of = {}                                  # original source path -> name used on Windows
    try:
        files = sorted(f for f in os.listdir(logdir) if f.startswith("copied-") and f.endswith(".tsv"))
    except OSError:
        return {}
    for f in files:
        try:
            with open(os.path.join(logdir, f), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("RENAMED\t"):
                        parts = line.rstrip("\n").split("\t")
                        if len(parts) == 3 and parts[1].startswith("/"):
                            out_of[parts[1]] = parts[2]
        except OSError:
            pass
    result = {}
    for csp, out in out_of.items():
        comps = csp.strip("/").split("/")
        win = []
        for i, c in enumerate(comps):
            prefix = "/" + "/".join(comps[:i + 1])
            win.append(out if i == len(comps) - 1 else out_of.get(prefix, win_safe(c)))
        result["/".join(win)] = comps[-1]
    return result


# ------------------------------------------------------------------ the copy job
Stop = StopJob


class _Engine:
    def __init__(self, src, dst, items, workers, fix_perms, restore_names, check):
        self.src, self.dst, self.items = src.rstrip("/"), dst.rstrip("/"), items
        self.workers = max(1, min(int(workers), 8))
        self.fix_perms, self.check = fix_perms, check
        self.renames = load_renames(self.src) if restore_names else {}
        self.lock = threading.Lock()
        self.run_ev, self.stop_ev = threading.Event(), threading.Event()
        self.run_ev.set()
        self.small_q, self.big_q = queue.Queue(maxsize=5000), queue.Queue(maxsize=200)
        self.state, self.reason = "running", ""
        self.found = self.found_bytes = 0
        self.walk_done = False
        self.copied = self.skipped = self.failed = self.renamed = self.checked = 0
        self.bytes = self.done_bytes = 0
        self.current = {}
        self.errors = collections.deque(maxlen=300)
        self.samples = collections.deque(maxlen=40)
        self.dir_times = []
        self.t0 = time.time()
        os.makedirs(LOGDIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.err_log = open(os.path.join(LOGDIR, f"errors-{stamp}.txt"), "a", encoding="utf-8")
        self.err_path = self.err_log.name

    # control
    def start(self):
        threading.Thread(target=self._main, daemon=True).start()

    def pause(self):
        if self.state == "running":
            self.run_ev.clear()
            self.state = "paused"

    def resume(self):
        if self.state == "paused":
            self.state = "running"
            self.run_ev.set()

    def stop(self, reason="Stopped by you - press Start again to continue (finished files are skipped)"):
        if not self.stop_ev.is_set():
            self.reason = reason
            self.stop_ev.set()
            self.run_ev.set()
            if self.state in ("running", "paused"):
                self.state = "stopping"

    def _wait(self):
        while not self.run_ev.wait(0.5):
            pass
        if self.stop_ev.is_set():
            raise Stop()

    def _error(self, path, msg):
        with self.lock:
            self.failed += 1
            self.errors.append({"path": path, "msg": str(msg)})
            self.err_log.write(f"{path}\t{msg}\n")
            self.err_log.flush()

    # walking
    def _name(self, rel_parent, name):
        rel = f"{rel_parent}/{name}" if rel_parent else name
        orig = self.renames.get(rel)
        if orig and orig != name:
            with self.lock:
                self.renamed += 1
            return orig
        if not rel_parent and name in TOP_RENAME:
            return TOP_RENAME[name]
        return name

    def _walk(self):
        for item in self.items:
            self._wait()
            sp = os.path.join(self.src, item)
            dp = os.path.join(self.dst, self._name("", item))
            if os.path.isdir(sp) and not os.path.islink(sp):
                stack = [(sp, dp, item)]
                while stack:
                    self._wait()
                    s, d, rel = stack.pop()
                    try:
                        os.makedirs(d, exist_ok=True)
                        st = os.stat(s)
                        self.dir_times.append((d, st.st_atime_ns, st.st_mtime_ns))
                    except OSError as ex:
                        self._error(rel + "/", ex)
                        continue
                    try:
                        with os.scandir(s) as it:
                            ents = sorted(it, key=lambda e: e.name)
                    except OSError as ex:
                        self._error(rel + "/", f"can't list folder: {ex}")
                        continue
                    subs = []
                    for e in ents:
                        if e.name in SKIP_ANY or e.name.endswith((".t5r-part",)):
                            continue
                        crel = f"{rel}/{e.name}"
                        cd = os.path.join(d, self._name(rel, e.name))
                        try:
                            if e.is_symlink():
                                self._link(e.path, cd, crel)
                            elif e.is_dir():
                                subs.append((e.path, cd, crel))
                            elif e.is_file():
                                self._enqueue(e.path, cd, crel, e.stat())
                        except Stop:
                            raise
                        except OSError as ex:
                            self._error(crel, ex)
                    stack.extend(reversed(subs))
            elif os.path.islink(sp):
                self._link(sp, dp, item)
            elif os.path.isfile(sp):
                self._enqueue(sp, dp, item, os.stat(sp))

    def _link(self, sp, dp, rel):
        try:
            if not os.path.lexists(dp):
                os.symlink(os.readlink(sp), dp)
        except OSError as ex:
            self._error(rel, ex)

    def _enqueue(self, sp, dp, rel, st):
        size = st.st_size
        with self.lock:
            self.found += 1
            self.found_bytes += size
        try:
            d = os.stat(dp)
            if stat.S_ISREG(d.st_mode) and d.st_size == size and abs(d.st_mtime - st.st_mtime) < 2.5:
                with self.lock:
                    self.skipped += 1
                    self.done_bytes += size
                return
        except OSError:
            pass
        q = self.big_q if size >= BIG else self.small_q
        while True:
            self._wait()
            try:
                q.put((sp, dp, rel, size, st.st_atime_ns, st.st_mtime_ns), timeout=0.5)
                return
            except queue.Full:
                continue

    # copying
    def _worker(self, q, wid):
        while True:
            t = q.get()
            if t is None:
                return
            sp, dp, rel, size, at, mt = t
            if self.stop_ev.is_set():
                continue
            if size >= CHUNK:
                with self.lock:
                    self.current[wid] = {"path": rel, "size": size, "done": 0}
            try:
                self._copy(sp, dp, rel, size, at, mt, wid)
            except Stop:
                pass
            except Exception as ex:
                self._error(rel, f"{type(ex).__name__}: {ex}")
            finally:
                with self.lock:
                    self.current.pop(wid, None)

    def _copy(self, sp, dp, rel, size, at, mt, wid):
        if os.path.lexists(dp):        # different file already there: never overwrite
            n = 2
            while os.path.lexists(f"{dp}.restore-{n}"):
                n += 1
            dp = f"{dp}.restore-{n}"
        part = dp + ".t5r-part"
        h = hashlib.sha256() if self.check else None
        head = b""
        written = 0
        try:
            with open(sp, "rb") as fi, open(part, "wb") as fo:
                while True:
                    if not self.run_ev.is_set() or self.stop_ev.is_set():
                        self._wait()
                    b = fi.read(CHUNK)
                    if not b:
                        break
                    if not head:
                        head = b[:4]
                    if size >= SPARSE_MIN and b.count(0) == len(b):
                        fo.seek(len(b), 1)      # keep empty regions empty (VM disks stay small)
                    else:
                        fo.write(b)
                    if h:
                        h.update(b)
                    written += len(b)
                    with self.lock:
                        self.bytes += len(b)
                        self.done_bytes += len(b)
                        if wid in self.current:
                            self.current[wid]["done"] = written
                fo.truncate(written)
            if written != size:
                raise IOError(f"read {written:,} of {size:,} bytes")
            if h and not self._same(part, h.hexdigest()):
                raise IOError("the copy reads back differently from the original - left as .t5r-part")
        except Stop:
            with self.lock:
                self.bytes -= written
                self.done_bytes -= written
            raise
        except Exception:
            with self.lock:
                self.done_bytes += size - written
            raise
        os.rename(part, dp)
        if self.fix_perms:
            os.chmod(dp, 0o755 if head.startswith(EXEC_MAGIC) else 0o644)
        try:
            os.utime(dp, ns=(at, mt))
        except OSError:
            pass
        with self.lock:
            self.copied += 1
            if h:
                self.checked += 1

    def _same(self, path, digest):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            try:
                fcntl.fcntl(f.fileno(), F_NOCACHE, 1) if fcntl else None
            except (OSError, AttributeError):
                pass
            for b in iter(lambda: f.read(CHUNK), b""):
                h.update(b)
        return h.hexdigest() == digest

    def _main(self):
        lanes = [threading.Thread(target=self._worker, args=(self.small_q, i), daemon=True)
                 for i in range(self.workers)]
        lanes.append(threading.Thread(target=self._worker, args=(self.big_q, 99), daemon=True))
        for t in lanes:
            t.start()
        try:
            self._walk()
        except Stop:
            pass
        except Exception as ex:
            self._error("(scan)", ex)
        self.walk_done = True
        for _ in range(self.workers):
            self.small_q.put(None)
        self.big_q.put(None)
        for t in lanes:
            t.join()
        for d, at, mt in reversed(self.dir_times):
            try:
                os.utime(d, ns=(at, mt))
            except OSError:
                pass
        self.err_log.close()
        self.state = "stopped" if self.stop_ev.is_set() else "done"

    def status(self):
        now = time.time()
        with self.lock:
            self.samples.append((now, self.bytes))
            t, b = self.samples[0]
            speed = (self.bytes - b) / (now - t) if now - t > 1 else 0
            left = self.found_bytes - self.done_bytes
            return {"state": self.state, "reason": self.reason, "found": self.found,
                    "found_bytes": self.found_bytes, "walk_done": self.walk_done, "copied": self.copied,
                    "skipped": self.skipped, "failed": self.failed, "renamed": self.renamed,
                    "checked": self.checked, "bytes": self.bytes, "done_bytes": self.done_bytes,
                    "speed": speed, "eta": left / speed if speed > 0 and self.walk_done else None,
                    "current": list(self.current.values()), "errors": list(self.errors)[-60:],
                    "free": free(self.dst), "err_log": self.err_path, "elapsed": now - self.t0}


def free(p):
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0




class TransferJob(ToolJob):
    kind = "transfer"
    title = "Copying files back"

    def __init__(self, params):
        super().__init__(params)
        p = params
        self.eng = _Engine(p["src"], p["dst"], p["items"], p["workers"], p["fix_perms"], p["restore_names"], p["check"])

    def run(self):
        self.phase = "Copying…"
        self.eng._main()
        if self.eng.stop_ev.is_set():
            raise StopJob()

    def pause(self):
        super().pause()
        self.eng.pause()

    def resume(self):
        super().resume()
        self.eng.resume()

    def stop(self):
        super().stop()
        self.eng.stop()

    def extra_status(self):
        s = self.eng.status()
        st = {"files_done": s["copied"] + s["skipped"] + s["failed"], "files_total": s["found"],
              "bytes_done": s["done_bytes"], "bytes_total": s["found_bytes"], "speed": s["speed"],
              "eta": s["eta"], "failed": s["failed"], "skipped": s["skipped"], "found": s["found"],
              "copied": s["copied"], "renamed": s["renamed"], "checked": s["checked"]}
        if s["walk_done"] and s["found_bytes"]:
            self.progress = min(1.0, s["done_bytes"] / max(1, s["found_bytes"]))
        return {"stats": st, "current": s["current"], "errors": s["errors"], "free": s["free"],
                "err_log": s["err_log"], "walk_done": s["walk_done"]}


@route("POST", "/api/transfer")
def start_transfer(q, body):
    src = clean_path(body.get("src"), "folder to copy from")
    dst = clean_path(body.get("dst"), "destination folder")
    if not os.path.isdir(src):
        raise ApiError(f"{src} isn't there - is the drive connected?", 404)
    if not os.path.isdir(dst):
        raise ApiError(f"{dst} isn't there - is the drive connected?", 404)
    if dst == src or dst.startswith(src + os.sep) or src.startswith(dst + os.sep):
        raise ApiError("The source and destination must be different folders.")
    items = body.get("items") or [e for e in sorted(os.listdir(src)) if e not in SKIP_TOP and e not in SKIP_ANY]
    items = [i for i in items if isinstance(i, str) and "/" not in i and "\\" not in i and i not in ("", ".", "..")]
    if not items:
        raise ApiError("Nothing to copy - choose what to copy first.")
    if any(j.kind == "transfer" and j.live() for j in JOBS.jobs.values()):
        raise ApiError("A copy is already running - wait for it to finish (or stop it) first.", 409)
    job = TransferJob({"src": src, "dst": dst, "items": items, "workers": as_int(body.get("workers"), 4, 1, 8),
                       "fix_perms": as_bool(body.get("fix_perms"), True) and not IS_WIN,
                       "restore_names": as_bool(body.get("restore_names"), True),
                       "check": as_bool(body.get("check"), False)})
    JOBS.start(job)
    return job.status()
