"""Duplicate finder (job "dupes") and duplicate removal (job "dupes_apply").

POST /api/dupes {paths, min_size?, include_hidden?, include_packages?, threads?} -> job status
     size -> first+last 64 KB hash -> full hash (xxh3-128, or blake2b when xxhash is missing)
     result(q: offset, limit, q (path filter), sort=reclaimable|size|count)
       -> {groups: [{id, size, hash, count, reclaimable, files: [{path, mtime, keep}]}],
           total_groups, reclaimable, scanned, bytes_scanned, hash, offset, limit}
POST /api/dupes/apply {action: trash|clone, remove: [paths], keep_map: {removed_path: kept_path}} -> job status
     result -> {done: [{path, kept, action, freed}], failed: [{path, error}], freed}

Safety: a file is only removed when the copy it is replaced by still exists, is a
different file, and is byte-for-byte identical right before acting. Nothing is
deleted permanently ("trash" uses the system Trash). "clone" (macOS APFS) replaces
the duplicate with a copy-on-write clone of the kept file: both names stay, the
space is shared. Hard links are never created.
"""
from __future__ import annotations

import concurrent.futures as cf
import ctypes
import os
import re
import stat
import threading

from .. import util
from ..jobs import JOBS
from ..server import ApiError, route
from ..util import IS_MAC, IS_WIN, human
from .common import (CHUNK, BLOCK, ToolJob, allocated, as_bool, as_int, clean_paths, files_equal, friendly_error,
                     hash_name, looks_like_copy, new_hasher, plural, protected_reason, sample_hash, walk_files)

_BAD_PLACES = re.compile(r"(^|[\\/])(\.trash|\.trashes|\$recycle\.bin|old-trash|recycler)([\\/]|$)", re.I)
_DOWNLOADS = re.compile(r"(^|[\\/])downloads([\\/]|$)", re.I)
_CACHEY = re.compile(r"(^|[\\/])(caches?|\.cache|tmp|temp|temporary items|\.temporaryitems)([\\/]|$)", re.I)
_RECOVERY_OUT = re.compile(r"(^|[\\/])(t5_recovered|recovered|retrova[^\\/]*|_t5rescue[^\\/]*|lost\+found)([\\/]|$)",
                           re.I)


def keep_rank(path: str, mtime: float):
    """Lower sorts first = better copy to keep."""
    penalty = 0
    if _BAD_PLACES.search(path):
        penalty += 100
    if looks_like_copy(path):
        penalty += 50
    if _CACHEY.search(path):
        penalty += 30
    if _DOWNLOADS.search(path):
        penalty += 20
    if _RECOVERY_OUT.search(path):
        penalty += 5
    depth = len(re.split(r"[\\/]+", path))
    return (penalty, depth, len(path), mtime, path)


class DupesJob(ToolJob):
    kind = "dupes"
    title = "Finding duplicates"

    def __init__(self, params):
        super().__init__(params)
        self.paths = params["paths"]
        self.min_size = params.get("min_size", 100 * 1024)
        self.hidden = params.get("include_hidden", False)
        self.packages = params.get("include_packages", False)
        self.threads = params.get("threads", 4)
        self.groups: list[dict] = []
        self.blk = threading.Lock()
        self.stats = {"files_done": 0, "files_total": 0, "bytes_done": 0, "bytes_total": 0, "found": 0,
                      "scanned": 0, "reclaimable": 0}

    def _err(self, path, ex):
        self.add_error(path, ex)

    def run(self):
        self.phase = "Listing files"
        by_size: dict[int, list] = {}
        seen_inodes = set()
        scanned = scanned_bytes = 0
        for path, st in walk_files(self.paths, self, hidden=self.hidden, packages=self.packages,
                                   on_error=self._err):
            scanned += 1
            if scanned % 2000 == 0:
                self.stats["scanned"] = scanned
                self.message = f"Found {scanned:,} files"
            size = st.st_size
            if size < max(1, self.min_size):
                continue
            key = (st.st_dev, st.st_ino)
            if st.st_ino and key in seen_inodes:
                continue                      # another name of the same file (hard link): nothing to free
            seen_inodes.add(key)
            scanned_bytes += size
            by_size.setdefault(size, []).append((path, st.st_mtime, st.st_nlink))
        self.stats["scanned"] = scanned
        self.stats["bytes_scanned"] = scanned_bytes
        cands = [(s, fl) for s, fl in by_size.items() if len(fl) > 1]
        by_size = None
        n_cand = sum(len(fl) for _s, fl in cands)
        self.progress = 0.1
        self.stats["files_total"] = n_cand

        # stage 2: beginning + end
        self.phase = "Comparing the start and end of same-size files"
        self.message = plural(n_cand, "file") + " to compare"
        partial_groups = []
        with cf.ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="dupes") as pool:
            futs = {}
            for size, fl in cands:
                for f in fl:
                    futs[pool.submit(self._sample, f[0], size)] = (size, f)
            done = 0
            res: dict[tuple, list] = {}
            for fut in cf.as_completed(futs):
                size, f = futs[fut]
                done += 1
                self.stats["files_done"] = done
                if done % 200 == 0:
                    self.checkpoint()
                    self.progress = 0.1 + 0.1 * done / max(1, n_cand)
                try:
                    h = fut.result()
                except OSError as ex:
                    self.add_error(f[0], ex)
                    continue
                res.setdefault((size, h), []).append(f)
            for (size, h), fl in res.items():
                if len(fl) > 1:
                    partial_groups.append((size, h, fl))
        self.checkpoint()

        # stage 3: full hash (files bigger than the two samples)
        full_needed = [(size, h, fl) for size, h, fl in partial_groups if size > 2 * BLOCK]
        final = [(size, h, fl) for size, h, fl in partial_groups if size <= 2 * BLOCK]
        total_bytes = sum(size * len(fl) for size, _h, fl in full_needed)
        self.stats["bytes_total"] = total_bytes
        self.stats["bytes_done"] = 0
        self.stats["files_total"] = sum(len(fl) for _s, _h, fl in full_needed)
        self.stats["files_done"] = 0
        self.phase = "Comparing full contents"
        self.message = f"{human(total_bytes)} to read"
        self.progress = 0.2
        with cf.ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="dupes") as pool:
            futs = {}
            for size, _h, fl in full_needed:
                for f in fl:
                    futs[pool.submit(self._full, f[0], size)] = (size, f)
            res = {}
            done = 0
            for fut in cf.as_completed(futs):
                size, f = futs[fut]
                done += 1
                self.stats["files_done"] = done
                try:
                    h = fut.result()
                except OSError as ex:
                    self.add_error(f[0], ex)
                    continue
                res.setdefault((size, h), []).append(f)
                if total_bytes:
                    self.progress = 0.2 + 0.8 * self.stats["bytes_done"] / total_bytes
            for (size, h), fl in res.items():
                if len(fl) > 1:
                    final.append((size, h, fl))
        self.checkpoint()
        groups = []
        for size, h, fl in final:
            files = sorted(fl, key=lambda f: keep_rank(f[0], f[1]))
            items = [{"path": p, "mtime": int(mt * 1000), "keep": i == 0} for i, (p, mt, _n) in enumerate(files)]
            groups.append({"size": size, "hash": h, "count": len(items), "reclaimable": size * (len(items) - 1),
                           "files": items})
        groups.sort(key=lambda g: (-g["reclaimable"], g["files"][0]["path"]))
        for i, g in enumerate(groups):
            g["id"] = f"g{i}"
        self.groups = groups
        rec = sum(g["reclaimable"] for g in groups)
        self.stats.update(found=len(groups), reclaimable=rec)
        self.progress = 1.0
        self.phase = "Done"
        self.message = (f"{plural(len(groups), 'set')} of duplicates - {human(rec)} could be freed" if groups
                        else "No duplicates found")

    def _sample(self, path, size):
        self.checkpoint()
        return sample_hash(path, size)

    def _full(self, path, size):
        h = new_hasher()
        done = 0
        with open(util.long_path(path), "rb") as f:
            while True:
                self.checkpoint()
                b = f.read(CHUNK)
                if not b:
                    break
                h.update(b)
                done += len(b)
                with self.blk:
                    self.stats["bytes_done"] += len(b)
        if done != size:
            with self.blk:
                self.stats["bytes_done"] += size - done
            raise OSError(5, "The file changed size while it was being read")
        return h.hexdigest()

    def result(self, q):
        groups = self.groups
        text = (q.get("q") or "").strip().lower()
        if text:
            groups = [g for g in groups if any(text in f["path"].lower() for f in g["files"])]
        sort = q.get("sort") or "reclaimable"
        if sort == "size":
            groups = sorted(groups, key=lambda g: -g["size"])
        elif sort == "count":
            groups = sorted(groups, key=lambda g: (-g["count"], -g["size"]))
        off = as_int(q.get("offset"), 0, 0)
        lim = as_int(q.get("limit"), 200, 1, 5000)
        return {"groups": groups[off:off + lim], "total_groups": len(groups), "offset": off, "limit": lim,
                "reclaimable": sum(g["reclaimable"] for g in groups), "scanned": self.stats.get("scanned", 0),
                "bytes_scanned": self.stats.get("bytes_scanned", 0), "hash": hash_name(), "partial": self.live()}


# ---------------------------------------------------------------- apply
CLONE_NOFOLLOW = 0x0001
CLONE_NOOWNERCOPY = 0x0002
_FS_CACHE: dict[str, str] = {}


def mac_fs_type(path: str) -> str:
    """Filesystem type of the volume holding `path` on macOS ('apfs', 'hfs', 'exfat', ...)."""
    out = util.run(["/sbin/mount"], timeout=10) if IS_MAC else ""
    best, kind = "", ""
    real = os.path.realpath(path)
    for line in out.splitlines():
        m = re.match(r"^(.+?) on (.+?) \(([^,)]+)", line)
        if not m:
            continue
        mnt = m.group(2)
        if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
            best, kind = mnt, m.group(3).strip().lower()
    return kind


def clonefile(src: str, dst: str):
    """macOS clonefile(2): copy-on-write clone. Raises OSError."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    fn = libc.clonefile
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    fn.restype = ctypes.c_int
    if fn(os.fsencode(src), os.fsencode(dst), CLONE_NOFOLLOW | CLONE_NOOWNERCOPY) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), dst)


def clone_replace(kept: str, dup: str, dup_st, checkpoint=None):
    """Replace `dup` by an APFS clone of `kept` (atomic rename), keeping dup's name, dates and permissions."""
    d = os.path.dirname(dup)
    tmp = os.path.join(d, f".{os.path.basename(dup)}.retrova-clone-{os.getpid()}")
    if os.path.lexists(tmp):
        raise OSError(17, "A temporary file from an earlier attempt is in the way", tmp)
    clonefile(kept, tmp)
    try:
        now = os.lstat(dup)
        if (now.st_size, now.st_mtime_ns, now.st_ino) != (dup_st.st_size, dup_st.st_mtime_ns, dup_st.st_ino):
            raise ApiError("The file changed while Retrova was working on it, so it was left as it was.")
        if os.path.getsize(tmp) != dup_st.st_size:
            raise ApiError("The clone came out a different size, so nothing was changed.")
        if util.is_admin():
            try:
                os.chown(tmp, dup_st.st_uid, dup_st.st_gid)
            except OSError:
                pass
        os.chmod(tmp, stat.S_IMODE(dup_st.st_mode))
        os.utime(tmp, ns=(dup_st.st_atime_ns, dup_st.st_mtime_ns))
        flags = getattr(dup_st, "st_flags", 0) & ~0x00000002 & ~0x00020000   # never copy immutable flags
        if flags and hasattr(os, "chflags"):
            try:
                os.chflags(tmp, flags)
            except OSError:
                pass
        os.replace(tmp, dup)
    except BaseException:
        try:
            os.unlink(tmp)          # our own temporary clone, never user data
        except OSError:
            pass
        raise


class DupesApplyJob(ToolJob):
    kind = "dupes_apply"
    title = "Removing duplicates"

    def __init__(self, action: str, remove: list, keep_map: dict):
        super().__init__({"action": action, "count": len(remove)})
        self.action = action
        self.remove = remove
        self.keep_map = keep_map
        self.title = "Moving duplicates to the Trash" if self.action == "trash" else "Sharing space between copies"
        self.done: list[dict] = []
        self.failed: list[dict] = []
        self.stats = {"files_done": 0, "files_total": len(self.remove), "bytes_done": 0, "bytes_total": 0,
                      "freed": 0, "failed": 0}

    def _fail(self, path, msg):
        self.failed.append({"path": path, "error": msg})
        self.add_error(path, msg)

    def run(self):
        removing = set(self.remove)
        sizes = 0
        for p in self.remove:
            try:
                sizes += os.lstat(p).st_size * 2
            except OSError:
                pass
        self.stats["bytes_total"] = sizes
        self.phase = "Checking each copy is identical"
        fs_ok: dict[str, str] = {}
        for i, path in enumerate(self.remove):
            self.checkpoint()
            self.stats["files_done"] = i
            self.progress = i / max(1, len(self.remove))
            self.message = os.path.basename(path)
            kept = self.keep_map.get(path)
            try:
                freed = self._one(path, kept, removing, fs_ok)
            except ApiError as ex:
                self._fail(path, str(ex))
                continue
            except OSError as ex:
                self._fail(path, friendly_error(ex))
                continue
            self.done.append({"path": path, "kept": kept, "action": self.action, "freed": freed})
            self.stats["freed"] += freed
        self.stats["files_done"] = len(self.remove)
        self.progress = 1.0
        self.phase = "Done"
        n = len(self.done)
        if self.action == "trash":
            self.message = (f"Moved {plural(n, 'duplicate')} to the Trash ({human(self.stats['freed'])}). Empty the "
                            f"Trash to get the space back - until then you can put them back." if n else
                            "Nothing was moved.")
        else:
            self.message = (f"{plural(n, 'duplicate')} now share space with the copy you kept - "
                            f"{human(self.stats['freed'])} freed. Both names still work." if n else "Nothing changed.")
        if self.failed:
            self.message += f" {plural(len(self.failed), 'file')} could not be handled - see the list."

    def _one(self, path, kept, removing, fs_ok) -> int:
        if not kept or not isinstance(kept, str):
            raise ApiError("Retrova doesn't know which copy to keep for this file, so it was left alone.")
        if os.path.normcase(os.path.abspath(kept)) == os.path.normcase(os.path.abspath(path)):
            raise ApiError("This is the copy you chose to keep, so it was left alone.")
        if kept in removing:
            raise ApiError("The copy chosen to keep is also on the remove list, so this one was left alone "
                           "to make sure one copy survives.")
        why = protected_reason(path)
        if why:
            raise ApiError(why)
        st = os.lstat(util.long_path(path))
        if not stat.S_ISREG(st.st_mode):
            raise ApiError("This isn't an ordinary file any more, so it was left alone.")
        try:
            kst = os.lstat(util.long_path(kept))
        except FileNotFoundError:
            raise ApiError("The copy you chose to keep isn't there any more, so this one was left alone.")
        if not stat.S_ISREG(kst.st_mode):
            raise ApiError("The copy you chose to keep isn't an ordinary file, so this one was left alone.")
        if (kst.st_dev, kst.st_ino) == (st.st_dev, st.st_ino):
            raise ApiError("These two names already point to the same file, so there's nothing to free.")
        if kst.st_size != st.st_size:
            raise ApiError("The two files are no longer the same size, so nothing was changed.")
        if getattr(st, "st_flags", 0) & (0x2 | 0x20000):
            raise ApiError("This file is locked, so it was left alone.")
        if self.action == "clone":
            dev_key = str(st.st_dev)
            if dev_key not in fs_ok:
                fs_ok[dev_key] = mac_fs_type(path)
            if fs_ok[dev_key] != "apfs":
                raise ApiError("This drive isn't formatted as APFS, so copies can't share space here. "
                               "Use 'Move to Trash' instead.")
            if kst.st_dev != st.st_dev:
                raise ApiError("The two copies are on different drives, so they can't share space. "
                               "Use 'Move to Trash' instead.")

        def prog(n):
            self.stats["bytes_done"] += n
        if not files_equal(kept, path, self.checkpoint, prog):
            raise ApiError("These files are no longer identical, so nothing was changed.")
        st2 = os.lstat(util.long_path(path))
        if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            raise ApiError("The file changed while Retrova was checking it, so it was left alone.")
        freed = allocated(st, path if IS_WIN else None) if st.st_nlink <= 1 else 0
        if self.action == "trash":
            r = util.to_trash([path])
            if r["failed"]:
                raise ApiError("Couldn't move it to the Trash: " + _short(r["failed"][0]["error"]))
            return freed
        clone_replace(kept, path, st, self.checkpoint)
        return freed

    def result(self, q):
        return {"done": self.done, "failed": self.failed, "freed": self.stats.get("freed", 0)}


def _short(msg: str) -> str:
    return msg.split(": ", 1)[-1] if msg.startswith(("OSError", "PermissionError", "RuntimeError")) else msg


# ---------------------------------------------------------------- endpoints
@route("POST", "/api/dupes")
def start_dupes(q, body):
    paths = clean_paths(body.get("paths"), "folder")
    missing = [p for p in paths if not os.path.exists(util.long_path(p))]
    if missing and len(missing) == len(paths):
        raise ApiError("Those folders aren't there - is the drive connected?", 404)
    params = {"paths": [p for p in paths if p not in missing],
              "min_size": as_int(body.get("min_size"), 100 * 1024, 1),
              "include_hidden": as_bool(body.get("include_hidden")),
              "include_packages": as_bool(body.get("include_packages")),
              "threads": as_int(body.get("threads"), min(4, os.cpu_count() or 2), 1, 16)}
    job = DupesJob(params)
    JOBS.start(job)
    return job.status()


@route("POST", "/api/dupes/apply")
def apply_dupes(q, body):
    action = body.get("action")
    if action not in ("trash", "clone"):
        raise ApiError("Choose what to do with the duplicates: move them to the Trash, or share space (clone).")
    if action == "clone" and not IS_MAC:
        raise ApiError("Space-sharing copies (clones) only work on a Mac with an APFS drive. "
                       "Use 'Move to Trash' instead.")
    remove = body.get("remove") or []
    keep_map = body.get("keep_map") or {}
    if not isinstance(remove, list) or not remove:
        raise ApiError("Pick at least one duplicate to remove.")
    if not isinstance(keep_map, dict):
        raise ApiError("Retrova needs to know which copy to keep for each file.")
    remove = clean_paths(remove, "file")
    km = {}
    for k, v in keep_map.items():
        try:
            km[os.path.normpath(k)] = os.path.normpath(v) if isinstance(v, str) else None
        except (TypeError, ValueError):
            continue
    job = DupesApplyJob(action, remove, km)
    JOBS.start(job)
    return job.status()


__all__ = ["DupesJob", "DupesApplyJob", "keep_rank", "clonefile", "clone_replace", "mac_fs_type"]
