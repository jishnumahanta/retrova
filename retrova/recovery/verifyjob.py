"""Verify: compare a recovered copy with the original, file by file (quick / sample /
every byte), report problems, keep SHA-256 manifests, resume, and repair the copy
from the original. The source is read through the same read-only filesystem
views as the copy; only the copy is ever written (by Repair)."""
from __future__ import annotations

import collections
import csv
import io
import json
import os
import re
import sys
import threading
import time

from retrova.jobs import Job, StopJob

from .common import INCOMPLETE_SUFFIX, LEGACY_LOG_DIRS, LOG_DIR, MAX_WORKERS, VERIFY_DIR, WIN_RULES, NameMap, \
    excluded, give_to_user, human, join_src, long_path, makedirs_owned_safe, norm_src, safe_name
from .fsview import open_view
from .pool import WorkerPool
from .workers import SrcReader, verify_worker

DISK_NAMES = {"disk", "datadisk", "diffdisk", "basedisk"}
DISK_EXTS = {".qcow2", ".img", ".raw", ".vmdk", ".vhd", ".vhdx"}
V_RANK = {"quick": 0, "sample": 1, "full": 2}
V_FIXABLE = {"MISSING", "SIZE", "CONTENT"}
V_LABEL = {"OK": "Matches", "MISSING": "Missing in copy", "EXTRA": "Only in copy", "SIZE": "Size differs",
           "CONTENT": "Content differs", "SRC_BAD": "Source unreadable", "DST_BAD": "Copy unreadable",
           "LINK": "Link differs", "ERROR": "Error"}
V_ORDER = ["MISSING", "SIZE", "CONTENT", "DST_BAD", "ERROR", "SRC_BAD", "LINK", "EXTRA"]
V_SERIOUS = {"MISSING", "SIZE", "CONTENT", "DST_BAD", "ERROR"}


def is_disk_image(name: str) -> bool:
    n = name.lower()
    return n in DISK_NAMES or os.path.splitext(n)[1] in DISK_EXTS


# ---------------------------------------------------------------- disk image headers
def _u32(b, o, big=False):
    return int.from_bytes(b[o:o + 4], "big" if big else "little")


def _u64(b, o, big=False):
    return int.from_bytes(b[o:o + 8], "big" if big else "little")


def inspect_disk_header(head: bytes, size: int) -> dict:
    """Recognise a VM disk from its first 8 KB. verdict: ok / warn / bad."""
    if size == 0:
        return {"verdict": "warn", "detail": "Empty file (0 bytes)"}
    if len(head) < 512:
        return {"verdict": "bad", "detail": f"Only {len(head)} bytes - too small to be a disk image"}
    if head[:4] == b"QFI\xfb":
        ver, cbits, vsize = _u32(head, 4, True), _u32(head, 20, True), _u64(head, 24, True)
        l1, refc = _u64(head, 40, True), _u64(head, 48, True)
        d = f"qcow2 v{ver}, virtual disk {human(vsize)}, file {human(size)}"
        if ver not in (2, 3) or not 9 <= cbits <= 21:
            return {"verdict": "bad", "detail": d + " - header values are out of range"}
        if l1 >= size or refc >= size:
            return {"verdict": "bad", "detail": d + " - the header points past the end of the file (truncated?)"}
        return {"verdict": "ok", "detail": d}
    if head[:4] == b"KDMV":
        return {"verdict": "ok", "detail": f"VMDK sparse disk, file {human(size)}"}
    if head[:8] == b"vhdxfile":
        return {"verdict": "ok", "detail": f"VHDX disk, file {human(size)}"}
    if len(head) >= 1024 + 0x160 and head[1080:1082] == b"\x53\xef":
        sb = head[1024:]
        bsize = 1024 << min(_u32(sb, 24), 16)
        blocks = _u32(sb, 4)
        if _u32(sb, 0x60) & 0x80:  # 64-bit feature: high half of the block count
            blocks |= _u32(sb, 0x150) << 32
        fs_bytes = blocks * bsize
        label = sb[0x78:0x88].split(b"\0")[0].decode("utf-8", "replace")
        d = f"Raw ext4 filesystem{(' ' + repr(label)) if label else ''}, {human(fs_bytes)}, file {human(size)}"
        if size < fs_bytes:
            return {"verdict": "bad", "detail": d + " - the file is shorter than the filesystem (truncated)"}
        return {"verdict": "ok", "detail": d}
    for ss in (512, 4096):
        if len(head) >= ss + 92 and head[ss:ss + 8] == b"EFI PART":
            disk = (_u64(head, ss + 32) + 1) * ss
            d = f"Raw disk with a GPT partition table, {human(disk)}, file {human(size)}"
            if size < disk:
                return {"verdict": "bad", "detail": d + " - the file is shorter than the disk (truncated)"}
            return {"verdict": "ok", "detail": d}
    if head[510:512] == b"\x55\xaa":
        end = max((_u32(head, 446 + 16 * i + 8) + _u32(head, 446 + 16 * i + 12)) * 512 for i in range(4))
        d = f"Raw disk with an MBR partition table, file {human(size)}"
        if end > size:
            return {"verdict": "warn", "detail": d + f" - partitions reach {human(end)}, past the end of the file"}
        return {"verdict": "ok", "detail": d}
    if head.count(0) == len(head):
        return {"verdict": "warn", "detail": f"The first {len(head) // 1024} KB are all zeros - an empty "
                                              f"disk, or a damaged header"}
    return {"verdict": "warn", "detail": "Unrecognised header - not necessarily broken, just an unknown format"}


# ---------------------------------------------------------------- manifest helpers
def _man_line(sha, rel):
    if "\\" in rel or "\n" in rel:
        return "\\" + sha + "  " + rel.replace("\\", "\\\\").replace("\n", "\\n") + "\n"
    return f"{sha}  {rel}\n"


def read_manifest(path) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                esc = line.startswith("\\")
                if esc:
                    line = line[1:]
                sha, sep, rel = line.partition("  ")
                if not sep or len(sha) != 64:
                    continue
                if esc:
                    rel = re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), rel)
                out[rel] = sha
    except OSError:
        pass
    return out


def write_manifest(path, entries: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for rel in sorted(entries):
            fh.write(_man_line(entries[rel], rel))
    os.replace(tmp, path)
    give_to_user(path)


def previously_skipped(out_root) -> set:
    """Folders an earlier recovery deliberately skipped as regenerable (from its reports)."""
    found = set()
    for logname in (LOG_DIR,) + LEGACY_LOG_DIRS:
        d = os.path.join(long_path(out_root), logname)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if n.startswith("SKIPPED-REGENERABLE") and n.endswith(".md"):
                try:
                    with open(os.path.join(d, n), encoding="utf-8") as fh:
                        for line in fh:
                            m = re.match(r"^\| `(/[^`]*)` \|", line)
                            if m:
                                found.add(m.group(1))
                except OSError:
                    pass
    return found


_V_README = """Recovery Verify reports (written by Retrova)

report-<date>.csv                 every problem found in that run
manifest-sha256.txt               SHA-256 of every file verified byte-for-byte against the original
manifest-sha256-unverified.txt    SHA-256 of copies whose original had unreadable areas
                                  (the copy could not be fully checked, but you can still
                                  detect later changes to it)
state.json                        lets the next Verify skip files already checked

To re-check the copy later without the original drive, run this from the folder that
contains _recovery-verify (Git Bash, WSL, macOS or Linux):
    sha256sum -c _recovery-verify/manifest-sha256.txt          (macOS: shasum -a 256 -c ...)
"""


class VerifyJob(Job):
    kind = "verify"
    title = "Verifying the copy"
    exclusive = True

    def __init__(self, spec, label, paths, out_root, patterns, excluded_paths, show_hidden, mode, workers,
                 resume=True, repair_tasks=None, carry=None, params=None):
        super().__init__(params)
        self.spec = dict(spec)
        self.src_label = label
        self.paths, self.out_root = [norm_src(p) for p in paths], out_root
        self.patterns = [str(p) for p in patterns]
        self.excluded_paths = {norm_src(x).lower() for x in excluded_paths}
        self.show_hidden = bool(show_hidden)
        self.repair_tasks = repair_tasks
        self.mode = "full" if repair_tasks is not None else (mode if mode in V_RANK else "full")
        self.resume_cache = bool(resume) and repair_tasks is None
        self.nworkers = max(1, min(int(workers or 4), MAX_WORKERS))
        self.title = (f"Repairing the copy from {label}" if repair_tasks is not None
                      else f"Verifying the copy against {label}" if label else "Verifying the copy")
        self.pool = WorkerPool(maxsize=2000)
        self.stop_reason = ""
        self.listing = False
        self.found = 0
        self.total_files = self.done_files = 0
        self.total_bytes = self.done_bytes = 0
        self.cached = self.repaired = 0
        self.counts: collections.Counter = collections.Counter()
        self.issues: list = []
        self.current: dict = {}
        self.disk_images: list = []
        self.mappings: list = []
        self.roots: list = []
        self.cache: dict = {}
        self.man_new: dict = {}
        self.unverified: dict = {}
        self.dropped: set = set()      # keys whose old checksum must not be kept
        self.expected: dict = {}
        self.ignore: set = set()
        self.io_root = long_path(out_root)
        self.rep_dir = os.path.join(self.io_root, VERIFY_DIR)
        self.report_path = ""
        self.save_lock = threading.Lock()
        if carry:   # repair run: keep the earlier run's other findings in view
            self.issues = [dict(i) for i in carry["issues"]]
            self.counts.update(carry["counts"])

    # ---------------- control
    def pause(self):
        super().pause()
        if self.state == "paused":
            self.pool.pause()

    def resume(self):
        super().resume()
        self.pool.resume()

    def stop(self, reason="Stopped by you - press Verify again to continue (finished files are skipped)."):
        if not self.stopping:
            self.stop_reason = reason
        super().stop()
        self.pool.stop()

    # ---------------- helpers
    @staticmethod
    def _k(path):
        return path.lower() if (WIN_RULES or sys.platform == "darwin") else path

    def _rel(self, dp):
        return dp[len(self.io_root):].lstrip("\\/").replace("\\", "/")

    def _issue(self, status, sp="", key="", detail="", ranges=(), src_size=None, dst_size=None, task=None):
        with self.lock:
            self.counts[status] += 1
            self.issues.append({"status": status, "label": V_LABEL[status], "sp": sp, "key": key,
                                "detail": detail, "ranges": list(ranges), "src_size": src_size,
                                "dst_size": dst_size, "fixable": status in V_FIXABLE and task is not None,
                                "task": task})

    def _dest_root(self, p, volname):
        parts = [x for x in p.strip("/").split("/") if x]
        if not parts:
            cands = [os.path.join(self.io_root, safe_name(volname or "Recovered")), self.io_root]
        else:  # where the recovery puts it, or the same item inside a folder that was recovered whole
            cands = [os.path.join(self.io_root, *[safe_name(x) for x in parts[i:]])
                     for i in range(len(parts) - 1, -1, -1)]
        for c in cands:
            if os.path.lexists(c) or os.path.lexists(c + INCOMPLETE_SUFFIX):
                return c
        for c in reversed(cands[1:]):   # missing: the deepest place whose folder exists
            if os.path.isdir(os.path.dirname(c)):
                return c
        return cands[0]

    # ---------------- walking the source (mirrors CopyJob._walk so names map identically)
    def _add_file(self, node, sp, dp, tasks):
        size = int(node.size)
        self.expected[self._k(dp)] = "file"
        if node.handle is None:
            self._issue("SRC_BAD", sp, self._rel(dp), f"Can't read this file's details on the source: {node.error}")
            return
        tasks.append((self._rel(dp), node.handle, None, str(sp), str(dp), size, int(node.mtime_ns or 0),
                      int(node.atime_ns or 0), False))
        self.found += 1

    def _check_link(self, view, node, sp, dp):
        self.expected[self._k(dp)] = "link"
        with self.lock:
            self.total_files += 1
            self.done_files += 1
        try:
            target = view.readlink(node.handle)
        except Exception as ex:
            return self._issue("SRC_BAD", sp, self._rel(dp), f"Can't read this link on the source: {ex}")
        if not os.path.islink(dp):
            return self._issue("LINK", sp, self._rel(dp), f"Link to {target} is not in the copy" +
                               (" (a file or folder is there instead)" if os.path.lexists(dp) else ""))
        have = os.readlink(dp)
        if have != target:
            return self._issue("LINK", sp, self._rel(dp), f"Copy points to {have}, original points to {target}")
        with self.lock:
            self.counts["OK"] += 1

    def _walk_source(self, view):
        tasks = []
        for logname in (LOG_DIR, VERIFY_DIR) + LEGACY_LOG_DIRS:
            self.ignore.add(self._k(os.path.join(self.io_root, logname)))
        for p in self.paths:
            self.checkpoint()
            try:
                node = view.resolve(p)
            except Exception as ex:
                self._issue("SRC_BAD", p, "", f"Not found on the source: {ex}")
                continue
            dst = self._dest_root(p, view.label)
            self.roots.append(dst)
            self.mappings.append({"src": p, "dst": dst.replace("\\\\?\\", "")})
            if node.kind == "file":
                self._add_file(node, p, dst, tasks)
                continue
            if node.kind == "link":
                self._check_link(view, node, p, dst)
                continue
            if node.kind != "dir":
                continue
            self.expected[self._k(dst)] = "dir"
            stack = [(node, p, dst)]
            while stack:
                self.checkpoint()
                dnode, sp, dp = stack.pop()
                try:
                    entries = sorted(view.iterdir(dnode), key=lambda e: e.name)
                except StopJob:
                    raise
                except Exception as ex:
                    self._issue("SRC_BAD", sp + "/", self._rel(dp), f"Can't list this folder on the source: {ex}")
                    self.ignore.add(self._k(dp))
                    continue
                subdirs = []
                names = NameMap()
                for e in entries:
                    if excluded(e.name, self.patterns):
                        self.ignore.add(self._k(os.path.join(dp, safe_name(e.name))))
                        continue
                    csp = join_src(sp, e.name)
                    if csp.lower() in self.excluded_paths:
                        self.ignore.add(self._k(os.path.join(dp, safe_name(e.name))))
                        continue
                    cdp = os.path.join(dp, names.get(e.name))
                    if not self.show_hidden and e.name.startswith("."):
                        self.ignore.add(self._k(cdp))
                        continue
                    try:
                        if e.kind == "dir":
                            self.expected[self._k(cdp)] = "dir"
                            subdirs.append((e, csp, cdp))
                        elif e.kind == "file":
                            self._add_file(e, csp, cdp, tasks)
                        elif e.kind == "link":
                            self._check_link(view, e, csp, cdp)
                        else:
                            self.ignore.add(self._k(cdp))
                    except StopJob:
                        raise
                    except Exception as ex:
                        self.ignore.add(self._k(cdp))
                        self._issue("SRC_BAD", csp, self._rel(cdp), f"Can't read this entry on the source: {ex}")
                stack.extend(reversed(subdirs))
        return tasks

    # ---------------- walking the copy: files that exist only there
    _LEFTOVER = ((".retrova.part", "Unfinished piece left by an interrupted recovery"),
                 (".t5r-part", "Unfinished piece left by an interrupted recovery"),
                 (INCOMPLETE_SUFFIX, "Partial copy kept because the original couldn't be read in full"))

    def _extra_detail(self, name):
        for suf, why in self._LEFTOVER:
            if name.endswith(suf):
                return why
        if re.search(r"\.(retrova|t5r)-\d+$", name):
            return "Second version saved because a different file already had this name"
        return "Exists only in the recovered copy"

    def _walk_dest(self):
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            stack = [root]
            while stack:
                self.checkpoint()
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        ents = list(it)
                except OSError as ex:
                    self._issue("DST_BAD", "", self._rel(d), f"Can't list this folder in the copy: {ex}")
                    continue
                for e in ents:
                    k = self._k(e.path)
                    if k in self.ignore or (not self.show_hidden and e.name.startswith(".")):
                        continue
                    want = self.expected.get(k)
                    if want is None and e.name.endswith(INCOMPLETE_SUFFIX) and \
                            self.expected.get(k[:-len(INCOMPLETE_SUFFIX)]) == "file":
                        continue   # partial copy of a known file: reported with that file
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if is_dir and want == "dir":
                        stack.append(e.path)
                    elif want is None:
                        if is_dir:
                            n, b = self._count_tree(e.path)
                            self._issue("EXTRA", "", self._rel(e.path) + "/",
                                        f"Folder exists only in the recovered copy ({n:,} files, {human(b)})",
                                        dst_size=b)
                        else:
                            try:
                                sz = e.stat(follow_symlinks=False).st_size
                            except OSError:
                                sz = None
                            self._issue("EXTRA", "", self._rel(e.path), self._extra_detail(e.name), dst_size=sz)

    @staticmethod
    def _count_tree(path):
        n = b = 0
        for dp, dns, fns in os.walk(path):
            for f in fns:
                n += 1
                try:
                    b += os.lstat(os.path.join(dp, f)).st_size
                except OSError:
                    pass
        return n, b

    # ---------------- resume state
    def _load_cache(self):
        try:
            with open(os.path.join(self.rep_dir, "state.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            self.cache = data.get("files", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.cache = {}

    def _save_cache(self):
        with self.save_lock:
            try:
                p = os.path.join(self.rep_dir, "state.json")
                with self.lock:
                    blob = json.dumps({"version": 1, "files": self.cache})
                with open(p + ".tmp", "w", encoding="utf-8") as fh:
                    fh.write(blob)
                os.replace(p + ".tmp", p)
                give_to_user(p)
            except OSError:
                pass

    def _cached_ok(self, t):
        c = self.cache.get(t[0])
        if not (self.resume_cache and c and len(c) >= 4 and c[3] == "OK" and c[0] == t[5]
                and V_RANK.get(c[2], -1) >= V_RANK[self.mode]):
            return False
        try:
            st = os.stat(t[4])
        except OSError:
            return False
        return st.st_size == c[0] and st.st_mtime_ns == c[1]

    # ---------------- main
    def run(self):
        view = None
        failed = False
        try:
            makedirs_owned_safe(self.rep_dir)
            self._load_cache()
            if self.repair_tasks is None:
                view = open_view(self.spec, cache_blocks=16384)
                self.listing = True
                self.phase = "Listing files on the source…"
                tasks = self._walk_source(view)
                self.phase = "Looking for files that are only in the copy…"
                self._walk_dest()
                self.listing = False
            else:
                tasks = [tuple(t[:8]) + (True,) for t in self.repair_tasks]
            tasks.sort(key=lambda t: (is_disk_image(t[3].rsplit("/", 1)[-1]), t[5]))  # small first, VM disks last
            self.phase = "Checking which files were already verified…"
            todo = []
            for t in tasks:
                if self._cached_ok(t):
                    with self.lock:
                        self.counts["OK"] += 1
                        self.cached += 1
                    continue
                todo.append(t)
            with self.lock:
                self.total_files += len(tasks)
                self.done_files += len(tasks) - len(todo)
                if self.mode == "full":
                    self.total_bytes = sum(t[5] for t in todo) * (2 if self.repair_tasks is not None else 1)
            self.phase = {"quick": "Checking names and sizes", "sample": "Spot-checking samples",
                          "full": "Comparing every byte"}[self.mode]
            if self.repair_tasks is not None:
                self.phase = f"Repairing {len(todo)} file(s) from the original, then checking them again"
            if todo:
                self.pool.spawn(verify_worker, min(self.nworkers, len(todo)), before=(self.spec,),
                                after=(self.mode,))
                collector = threading.Thread(target=self._collect, daemon=True, name="verify-collector")
                collector.start()
                try:
                    for t in todo:
                        if not self.pool.put(t, lambda: self.stopping):
                            break
                finally:
                    self.pool.finish()
                collector.join()
                self.pool.join()
            if not self.stopping:
                if view is None:
                    view = open_view(self.spec, cache_blocks=4096)
                self.phase = "Checking VM disk image headers…"
                self._disk_check(view, tasks)
        except StopJob:
            pass
        except Exception:
            failed = True
            raise
        finally:
            if view:
                view.close()
            self.current.clear()
            try:
                self._save_cache()
                self._write_reports()
            except Exception as ex:
                self.message = self.message or f"Could not write the reports: {ex}"
            self.phase = "Error" if failed else "Stopped" if self.stopping else "Finished"
            if self.stopping and self.stop_reason:
                self.message = self.message or self.stop_reason

    def _collect(self):
        alive, last_save = len(self.pool.workers), time.time()
        res_q = self.pool.res_q
        import queue as _queue
        while alive > 0:
            try:
                msg = res_q.get(timeout=1.0)
            except _queue.Empty:
                if not self.pool.alive():
                    break
                continue
            except (EOFError, OSError):
                break
            kind, idx = msg[0], msg[1]
            if kind == "bytes":
                with self.lock:
                    self.done_bytes += msg[2]
                    if idx in self.current:
                        self.current[idx]["done"] += msg[2]
            elif kind == "cur":
                with self.lock:
                    self.current[idx] = {"path": msg[2], "size": msg[3], "done": 0}
            elif kind == "res":
                self._result(msg[2])
                with self.lock:
                    self.current.pop(idx, None)
            elif kind == "drop":
                with self.lock:
                    self.current.pop(idx, None)
            elif kind == "exit":
                alive -= 1
                with self.lock:
                    self.current.pop(idx, None)
            elif kind == "fatal":
                self._issue("ERROR", "", "", f"A verify worker could not start: {msg[2]}")
                self.stop(f"A verify worker could not start: {msg[2]}")
            if time.time() - last_save > 30:
                self._save_cache()
                last_save = time.time()

    def _result(self, r):
        key, st = r["key"], r["status"]
        with self.lock:
            self.done_files += 1
            if r.get("repaired"):
                self.repaired += 1
            if st == "OK":
                self.counts["OK"] += 1
                if r.get("dst_mtime") is not None:
                    self.cache[key] = [r["dst_size"], r["dst_mtime"], self.mode, "OK", r.get("sha")]
                if r.get("sha"):
                    self.man_new[key] = r["sha"]
                else:
                    self.dropped.add(key)
                return
            self.cache.pop(key, None)
            self.dropped.add(key)
            if st == "SRC_BAD" and r.get("sha"):
                self.unverified[key] = r["sha"]
        task = r["task"]
        if task is not None:
            task = [tuple(x) if isinstance(x, list) else x for x in task]   # handles stay tuples
        self._issue(st, r["sp"], key, r["detail"], r["ranges"], r["src_size"], r["dst_size"], task)

    def _disk_check(self, view, tasks):
        out = []
        for t in tasks:
            if self.stopping:
                break
            if not is_disk_image(t[3].rsplit("/", 1)[-1]) or not os.path.isfile(t[4]):
                continue
            try:
                with open(t[4], "rb") as fh:
                    head = fh.read(8192)
                size = os.path.getsize(t[4])
            except OSError as ex:
                out.append({"path": t[3], "verdict": "bad", "detail": f"Can't read the copy: {ex}"})
                continue
            r = inspect_disk_header(head, size)
            r["path"] = t[3]
            if r["verdict"] != "ok":
                sd, _ = SrcReader(view, t[1]).read(0, min(8192, t[5])) if t[5] else (b"", [])
                if sd is None:
                    r["detail"] += ". The original's header can't be read to compare."
                elif sd == head[:len(sd)]:
                    r["detail"] += ". The original starts exactly the same way, so this is how the disk was."
                else:
                    o = inspect_disk_header(sd, t[5])
                    r["verdict"] = "bad"
                    r["detail"] += f". The original's header is different ({o['detail']}) - repair this file."
            out.append(r)
        self.disk_images = out

    # ---------------- reports
    def _write_reports(self):
        if not os.path.isdir(self.rep_dir):
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.report_path = os.path.join(self.rep_dir, f"report-{stamp}.csv")
        with open(self.report_path, "w", newline="", encoding="utf-8-sig") as fh:
            fh.write(self.csv())
        give_to_user(self.report_path)
        with self.lock:
            good = {k: c[4] for k, c in self.cache.items() if len(c) > 4 and c[3] == "OK" and c[4]}
            good.update(self.man_new)
            dropped, unv_new = set(self.dropped), dict(self.unverified)
        mp_ = os.path.join(self.rep_dir, "manifest-sha256.txt")
        man = read_manifest(mp_)
        for k in dropped:
            if k not in good:
                man.pop(k, None)
        man.update(good)
        if man:
            write_manifest(mp_, man)
        up = os.path.join(self.rep_dir, "manifest-sha256-unverified.txt")
        unv = read_manifest(up)
        for k in list(unv):
            if k in good or (k in dropped and k not in unv_new):
                unv.pop(k)
        unv.update(unv_new)
        if unv or os.path.exists(up):
            write_manifest(up, unv)
        rp = os.path.join(self.rep_dir, "README.txt")
        with open(rp, "w", encoding="utf-8") as fh:
            fh.write(_V_README)
        give_to_user(rp)

    def _select(self, status="", q=""):
        q = (q or "").lower()
        with self.lock:
            return [i for i in self.issues
                    if (not status or i["status"] == status)
                    and (not q or q in i["sp"].lower() or q in i["key"].lower())]

    def csv(self, status="", q=""):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["result", "code", "path_on_source", "path_in_copy", "original_bytes", "copy_bytes",
                    "detail", "areas (start-end bytes)"])
        rank = {s: i for i, s in enumerate(V_ORDER)}
        for i in sorted(self._select(status, q), key=lambda i: (rank.get(i["status"], 99), i["sp"] or i["key"])):
            w.writerow([i["label"], i["status"], i["sp"], i["key"], i["src_size"], i["dst_size"], i["detail"],
                        "; ".join(f"{a}-{b}" for a, b in i["ranges"])])
        w.writerow([])
        with self.lock:
            counts = dict(self.counts)
        for k in ["OK"] + V_ORDER:
            if counts.get(k):
                w.writerow(["summary", k, V_LABEL[k], counts[k]])
        return buf.getvalue()

    def result(self, q):
        """q: status (code filter), q (path text), limit, offset -> {items, more, total}."""
        status, text = str(q.get("status") or ""), str(q.get("q") or "")
        limit = max(1, min(int(q.get("limit") or 300), 5000))
        off = max(0, int(q.get("offset") or 0))
        rank = {s: i for i, s in enumerate(V_ORDER)}
        items = sorted(self._select(status, text), key=lambda i: (rank.get(i["status"], 99), i["sp"] or i["key"]))
        page = items[off:off + limit]
        return {"items": [{k: v for k, v in i.items() if k != "task"} for i in page],
                "more": max(0, len(items) - off - len(page)), "total": len(items)}

    def repair_plan(self):
        with self.lock:
            fix = [i for i in self.issues if i["fixable"] and i["task"]]
            rest = [i for i in self.issues if not (i["fixable"] and i["task"])]
            counts = collections.Counter(self.counts)
        for i in fix:
            counts[i["status"]] -= 1
        return [i["task"] for i in fix], {"issues": rest, "counts": +counts}

    # ---------------- status
    def _refresh(self):
        full = self.total_bytes > 0
        prog = self.done_bytes if full else self.done_files
        rate = self.rate(prog)
        left = (self.total_bytes - self.done_bytes) if full else (self.total_files - self.done_files)
        eta = (left / rate) if rate > 0 and self.state == "running" and not self.listing else None
        counts = dict(self.counts)
        self.stats = {"files_done": self.done_files, "files_total": self.total_files,
                      "bytes_done": self.done_bytes, "bytes_total": self.total_bytes,
                      "speed": rate if full else 0, "files_per_sec": rate if not full else 0, "eta": eta,
                      "found": self.found, "failed": sum(v for k, v in counts.items() if k in V_SERIOUS),
                      "ok": counts.get("OK", 0), "skipped": self.cached}
        if self.state == "done":
            self.progress = 1.0
        elif full:
            self.progress = min(1.0, self.done_bytes / max(1, self.total_bytes))
        elif self.total_files:
            self.progress = self.done_files / self.total_files
        else:
            self.progress = None

    def status(self):
        with self.lock:
            self._refresh()
        return super().status()

    def extra_status(self):
        counts = dict(self.counts)
        fixable = sum(1 for i in self.issues if i["fixable"])
        return {
            "mode": self.mode, "repair": self.repair_tasks is not None, "listing": self.listing,
            "stop_reason": self.stop_reason, "found": self.found, "total_files": self.total_files,
            "done_files": self.done_files, "total_bytes": self.total_bytes, "done_bytes": self.done_bytes,
            "counts": counts, "cached": self.cached, "repaired": self.repaired,
            "current": [dict(c) for c in self.current.values()], "disk_images": list(self.disk_images),
            "issue_count": len(self.issues), "fixable": fixable,
            "problems": sum(v for k, v in counts.items() if k in V_SERIOUS),
            "out_root": self.out_root, "report_dir": os.path.join(self.out_root, VERIFY_DIR),
            "report": self.report_path.replace("\\\\?\\", ""), "mappings": list(self.mappings),
            "engine": f"{self.nworkers} {self.pool.engine_name}",
        }
