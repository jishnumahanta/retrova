"""Sparse reclaim: give back the space taken by runs of zeros inside big files.

Virtual-machine disks (Docker/Colima "datadisk", .raw, .img, .vmdk ...) are sparse on
the Mac that made them: the empty parts take no space. Copied off a damaged drive
they become fully written - 60 GB of mostly zeros really takes 60 GB. Rewriting such a
file as a sparse copy (seeking over the zero blocks instead of writing them) gives
the space back with identical content.

POST /api/sparse/scan {paths, min_size?, min_reclaim?} -> job "sparse_scan"
     result -> {files: [{path, size, allocated, zero_bytes, reclaimable, mtime}], reclaimable}
POST /api/sparse/apply {files: [path | {path}]} -> job "sparse_apply"
     result -> {done: [{path, before, after, freed}], skipped: [{path, reason}], failed: [{path, error}], freed}

zero_bytes  = bytes in all-zero 64 KB-aligned blocks (holes included)
reclaimable = allocated bytes minus the bytes the non-zero blocks need
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import subprocess

from .. import util
from ..jobs import JOBS
from ..server import ApiError, route
from ..util import IS_LINUX, IS_MAC, IS_WIN, human
from .common import (BLOCK, CHUNK, ZERO_BLOCK, ToolJob, allocated, as_int, clean_paths, friendly_error, plural,
                     protected_reason, walk_files)

MB = 1024 * 1024
ZERO_CHUNK = bytes(CHUNK)
UF_IMMUTABLE, SF_IMMUTABLE, UF_COMPRESSED = 0x2, 0x20000, 0x20
O_BINARY = getattr(os, "O_BINARY", 0)


# ---------------------------------------------------------------- scanning
def data_regions(fd: int, size: int):
    """(start, end) ranges that hold data; holes are skipped where the OS can tell us (SEEK_DATA)."""
    if not hasattr(os, "SEEK_DATA") or size == 0:
        if size:
            yield 0, size
        return
    pos = 0
    while pos < size:
        try:
            start = os.lseek(fd, pos, os.SEEK_DATA)
        except OSError as ex:
            if ex.errno == errno.ENXIO:     # only a hole left
                return
            yield pos, size                 # filesystem can't tell: treat as data
            return
        if start >= size:
            return
        try:
            end = os.lseek(fd, start, os.SEEK_HOLE)
        except OSError:
            end = size
        end = min(end, size)
        if end <= start:
            end = size
        yield start, end
        pos = end


def _aligned(regions, size):
    """Merge data regions into 64 KB-aligned ranges."""
    out = []
    for s, e in regions:
        s = s - s % BLOCK
        e = min(size, e + (-e) % BLOCK)
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _nonzero_bytes(buf, base: int, size: int) -> int:
    """Bytes in the non-zero 64 KB blocks of `buf` (which starts at file offset `base`, aligned)."""
    n = len(buf)
    if n == CHUNK and buf == ZERO_CHUNK:
        return 0
    mv = memoryview(buf)
    keep = 0
    for off in range(0, n, BLOCK):
        blk = mv[off:off + BLOCK].tobytes()
        if blk != ZERO_BLOCK[:len(blk)]:
            keep += len(blk)
    return keep


def measure(path: str, st=None, checkpoint=None, progress=None) -> dict:
    """Count zero blocks in a file (fast over holes). Returns the scan record."""
    lp = util.long_path(path)
    st = st or os.stat(lp)
    size = st.st_size
    alloc = allocated(st, path)
    keep = 0
    fd = os.open(lp, os.O_RDONLY | O_BINARY)
    try:
        for s, e in _aligned(data_regions(fd, size), size):
            os.lseek(fd, s, os.SEEK_SET)
            pos = s
            while pos < e:
                if checkpoint:
                    checkpoint()
                buf = os.read(fd, min(CHUNK, e - pos))
                if not buf:
                    break
                keep += _nonzero_bytes(buf, pos, size)
                pos += len(buf)
                if progress:
                    progress(len(buf))
    finally:
        os.close(fd)
    return {"path": path, "size": size, "allocated": alloc, "zero_bytes": max(0, size - keep),
            "reclaimable": max(0, alloc - keep), "mtime": int(st.st_mtime * 1000)}


class SparseScanJob(ToolJob):
    kind = "sparse_scan"
    title = "Looking for empty space in big files"

    def __init__(self, params):
        super().__init__(params)
        self.files: list[dict] = []
        self.stats = {"files_done": 0, "files_total": 0, "bytes_done": 0, "bytes_total": 0, "found": 0,
                      "reclaimable": 0, "scanned": 0}

    def run(self):
        p = self.params
        min_size = p.get("min_size", 64 * MB)
        min_reclaim = p.get("min_reclaim", 16 * MB)
        self.phase = "Looking for big files"
        big = []
        n = 0
        for path, st in walk_files(p["paths"], self, on_error=self.add_error):
            n += 1
            if n % 2000 == 0:
                self.stats["scanned"] = n
            if st.st_size >= min_size and not getattr(st, "st_flags", 0) & UF_COMPRESSED:
                big.append((path, st))
        self.stats["scanned"] = n
        self.stats["files_total"] = len(big)
        self.stats["bytes_total"] = sum(st.st_size for _p, st in big)
        self.phase = "Checking big files for empty space"
        done_before = 0

        def prog(k):
            self.stats["bytes_done"] += k
            if self.stats["bytes_total"]:
                self.progress = self.stats["bytes_done"] / self.stats["bytes_total"]
        for i, (path, st) in enumerate(big):
            self.checkpoint()
            self.message = f"Reading {os.path.basename(path)} ({human(st.st_size)})"
            try:
                rec = measure(path, st, self.checkpoint, prog)
            except OSError as ex:
                self.add_error(path, ex)
                continue
            finally:
                done_before += st.st_size
                self.stats["bytes_done"] = done_before
                self.stats["files_done"] = i + 1
            if rec["reclaimable"] >= min_reclaim:
                self.files.append(rec)
                self.stats["found"] = len(self.files)
                self.stats["reclaimable"] += rec["reclaimable"]
        self.files.sort(key=lambda f: -f["reclaimable"])
        self.progress = 1.0
        self.phase = "Done"
        r = self.stats["reclaimable"]
        self.message = (f"{plural(len(self.files), 'file')} could give back {human(r)}" if self.files else
                        "No big files with empty space found")

    def result(self, q):
        return {"files": self.files, "reclaimable": sum(f["reclaimable"] for f in self.files),
                "partial": self.live()}


# ---------------------------------------------------------------- in-use detection
def open_by_others(path: str) -> list[str]:
    """Names of other programs that have the file open ([] when none / unknown)."""
    if IS_WIN:
        return ["another program"] if _win_in_use(path) else []
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)
    if lsof:
        try:
            r = subprocess.run([lsof, "-w", "-F", "pc", "--", path], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            r = None
        if r is not None:
            names, pid = [], None
            for line in r.stdout.splitlines():
                if line.startswith("p"):
                    pid = int(line[1:]) if line[1:].isdigit() else None
                elif line.startswith("c") and pid is not None and pid != os.getpid():
                    names.append(line[1:])
            return sorted(set(names))
    if IS_LINUX:
        return _proc_scan(path)
    return []


def _proc_scan(path):
    real = os.path.realpath(path)
    out = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit() and int(p) != os.getpid()]
    except OSError:
        return []
    for pid in pids:
        d = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(d):
                try:
                    if os.readlink(os.path.join(d, fd)) == real:
                        with open(f"/proc/{pid}/comm") as fh:
                            out.add(fh.read().strip())
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return sorted(out)


def _win_in_use(path) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateFileW(util.long_path(path), 0x80000000, 0, None, 3, 0x80, None)   # GENERIC_READ, no sharing
        if h in (None, ctypes.c_void_p(-1).value):
            return ctypes.get_last_error() in (32, 33)      # sharing / lock violation
        k32.CloseHandle(h)
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- Windows sparse support
def _win_handle(fd):
    import msvcrt
    return msvcrt.get_osfhandle(fd)


def _win_ioctl(fd, code, inbuf=None):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                    wintypes.LPVOID]
    k32.DeviceIoControl.restype = wintypes.BOOL
    returned = wintypes.DWORD(0)
    ptr = ctypes.byref(inbuf) if inbuf is not None else None
    size = ctypes.sizeof(inbuf) if inbuf is not None else 0
    if not k32.DeviceIoControl(wintypes.HANDLE(_win_handle(fd)), code, ptr, size, None, 0,
                               ctypes.byref(returned), None):
        err = ctypes.get_last_error()
        raise OSError(errno.EIO, f"Windows refused the sparse-file request (error {err})")


def win_set_sparse(fd):
    """FSCTL_SET_SPARSE on an open file (NTFS/ReFS). Must be called before writing."""
    _win_ioctl(fd, 0x000900C4)


def win_zero_range(fd, start, end):
    """FSCTL_SET_ZERO_DATA: make sure a range of a sparse file is unallocated."""
    import ctypes

    class FILE_ZERO_DATA_INFORMATION(ctypes.Structure):
        _fields_ = [("FileOffset", ctypes.c_longlong), ("BeyondFinalZero", ctypes.c_longlong)]
    _win_ioctl(fd, 0x000980C8, FILE_ZERO_DATA_INFORMATION(start, end))


# ---------------------------------------------------------------- rewriting
def _pwrite(fd, data, off):
    if hasattr(os, "pwrite"):
        while data:
            n = os.pwrite(fd, data, off)
            data = data[n:]
            off += n
    else:
        os.lseek(fd, off, os.SEEK_SET)
        while data:
            n = os.write(fd, data)
            data = data[n:]


def _hash_file(path, checkpoint=None, nocache=False) -> str:
    h = hashlib.sha256()
    fd = os.open(util.long_path(path), os.O_RDONLY | O_BINARY)
    try:
        if nocache and IS_MAC:
            try:
                import fcntl
                fcntl.fcntl(fd, 48, 1)       # F_NOCACHE: read the disk, not memory
            except (OSError, ImportError):
                pass
        elif nocache and hasattr(os, "posix_fadvise"):
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
        while True:
            if checkpoint:
                checkpoint()
            b = os.read(fd, CHUNK)
            if not b:
                break
            h.update(b)
    finally:
        os.close(fd)
    return h.hexdigest()


def write_sparse_copy(src: str, dst: str, size: int, checkpoint=None, progress=None) -> tuple[str, int]:
    """Copy src to a new file dst, skipping all-zero 64 KB blocks. Returns (sha256 of src, data bytes written)."""
    h = hashlib.sha256()
    written = 0
    zero_runs = []
    fi = os.open(util.long_path(src), os.O_RDONLY | O_BINARY)
    try:
        fo = os.open(util.long_path(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600)
        try:
            if IS_WIN:
                win_set_sparse(fo)
            pos = 0
            pend_off, pend = 0, bytearray()
            zrun_start = None
            while True:
                if checkpoint:
                    checkpoint()
                buf = os.read(fi, CHUNK)
                if not buf:
                    break
                h.update(buf)
                if len(buf) == CHUNK and buf == ZERO_CHUNK:
                    if pend:
                        _pwrite(fo, bytes(pend), pend_off)
                        written += len(pend)
                        pend = bytearray()
                    if zrun_start is None:
                        zrun_start = pos
                else:
                    mv = memoryview(buf)
                    for off in range(0, len(buf), BLOCK):
                        blk = mv[off:off + BLOCK]
                        b = blk.tobytes()
                        if b == ZERO_BLOCK[:len(b)]:
                            if pend:
                                _pwrite(fo, bytes(pend), pend_off)
                                written += len(pend)
                                pend = bytearray()
                            if zrun_start is None:
                                zrun_start = pos + off
                        else:
                            if zrun_start is not None:
                                zero_runs.append((zrun_start, pos + off))
                                zrun_start = None
                            if not pend:
                                pend_off = pos + off
                            pend += b
                            if len(pend) >= CHUNK:
                                _pwrite(fo, bytes(pend), pend_off)
                                written += len(pend)
                                pend = bytearray()
                pos += len(buf)
                if progress:
                    progress(len(buf))
            if pend:
                _pwrite(fo, bytes(pend), pend_off)
                written += len(pend)
            if zrun_start is not None:
                zero_runs.append((zrun_start, pos))
            if pos != size:
                raise OSError(errno.EIO, f"read {pos:,} of {size:,} bytes - the file changed or couldn't be read")
            os.ftruncate(fo, size)
            if IS_WIN:
                for s, e in zero_runs:
                    try:
                        win_zero_range(fo, s, e)
                    except OSError:
                        break
            os.fsync(fo)
        finally:
            os.close(fo)
    finally:
        os.close(fi)
    return h.hexdigest(), written


def _copy_xattrs(src, dst):
    if not hasattr(os, "listxattr"):
        return
    try:
        for k in os.listxattr(src, follow_symlinks=False):
            try:
                os.setxattr(dst, k, os.getxattr(src, k, follow_symlinks=False), follow_symlinks=False)
            except OSError:
                pass
    except OSError:
        pass


class SparseApplyJob(ToolJob):
    kind = "sparse_apply"
    title = "Giving back empty space"

    def __init__(self, files: list[dict]):
        super().__init__({"files": [f["path"] for f in files][:50], "count": len(files)})
        self.files = files
        self.done: list[dict] = []
        self.skipped: list[dict] = []
        self.failed: list[dict] = []
        self.stats = {"files_done": 0, "files_total": len(files), "bytes_done": 0, "bytes_total": 0, "freed": 0,
                      "skipped": 0, "failed": 0}

    def _skip(self, path, reason):
        self.skipped.append({"path": path, "reason": reason})
        self.stats["skipped"] = len(self.skipped)

    def run(self):
        total = 0
        for f in self.files:
            try:
                total += os.stat(util.long_path(f["path"])).st_size * 2
            except OSError:
                pass
        self.stats["bytes_total"] = total
        for i, f in enumerate(self.files):
            self.checkpoint()
            path = f["path"]
            self.stats["files_done"] = i
            self.message = os.path.basename(path)
            try:
                self._one(path, f.get("reclaimable"))
            except ApiError as ex:
                self.failed.append({"path": path, "error": str(ex)})
                self.add_error(path, str(ex))
            except OSError as ex:
                self.failed.append({"path": path, "error": friendly_error(ex)})
                self.add_error(path, ex)
        self.stats["files_done"] = len(self.files)
        self.progress = 1.0
        self.phase = "Done"
        fr = self.stats["freed"]
        self.message = (f"Freed {human(fr)} in {plural(len(self.done), 'file')} - the contents are unchanged." if
                        self.done else "Nothing was changed.")
        if self.skipped:
            self.message += f" {plural(len(self.skipped), 'file')} skipped."
        if self.failed:
            self.message += f" {plural(len(self.failed), 'file')} failed."

    def _one(self, path, expected_reclaim):
        lp = util.long_path(path)
        why = protected_reason(path)
        if why:
            return self._skip(path, why)
        st = os.lstat(lp)
        if not stat.S_ISREG(st.st_mode):
            return self._skip(path, "It isn't an ordinary file.")
        if st.st_nlink > 1:
            return self._skip(path, "This file has other names (hard links), so rewriting it wouldn't free space.")
        flags = getattr(st, "st_flags", 0)
        if flags & (UF_IMMUTABLE | SF_IMMUTABLE):
            return self._skip(path, "This file is locked.")
        if flags & UF_COMPRESSED:
            return self._skip(path, "macOS already stores this file compressed.")
        users = open_by_others(path)
        if users:
            return self._skip(path, f"It's in use by {', '.join(users[:3])}. Quit "
                                    f"{'that app' if len(users) == 1 else 'those apps'} (for example stop Docker or "
                                    f"Colima) and try again.")
        before = allocated(st, path)
        need = max(0, before - int(expected_reclaim or 0)) + 256 * MB if expected_reclaim else 256 * MB
        free = util.free_bytes(os.path.dirname(path))
        if free < need:
            raise ApiError(f"Not enough free space to make the slimmer copy - it needs about {human(need)} free "
                           f"on this drive and {human(free)} is free.")
        d = os.path.dirname(path)
        tmp = os.path.join(d, f".{os.path.basename(path)}.retrova-sparse")
        if os.path.lexists(tmp):
            raise ApiError("A temporary file from an earlier attempt is in the way: "
                           f"{os.path.basename(tmp)}. Move it to the Trash and try again.")
        self.phase = "Writing a sparse copy"

        def prog(k):
            self.stats["bytes_done"] += k
            if self.stats["bytes_total"]:
                self.progress = min(0.999, self.stats["bytes_done"] / self.stats["bytes_total"])
        try:
            digest, _written = write_sparse_copy(path, tmp, st.st_size, self.checkpoint, prog)
            self.phase = "Checking the copy is identical"
            if _hash_file(tmp, self.checkpoint, nocache=True) != digest:
                raise ApiError("The new copy didn't read back identically, so the original was left as it was.")
            prog(st.st_size)
            now = os.lstat(lp)
            if (now.st_size, now.st_mtime_ns, now.st_ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
                os.unlink(tmp)
                return self._skip(path, "The file changed while Retrova was working on it (is an app using it?), "
                                        "so it was left as it was.")
            if util.is_admin() and not IS_WIN:
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except OSError:
                    pass
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            _copy_xattrs(path, tmp)
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            if flags and hasattr(os, "chflags"):
                try:
                    os.chflags(tmp, flags)
                except OSError:
                    pass
            os.replace(tmp, lp)
        except BaseException as ex:
            if os.path.lexists(tmp):
                try:
                    os.unlink(tmp)      # our own temporary copy; the original is untouched
                except OSError:
                    pass
            if isinstance(ex, OSError) and ex.errno == errno.ENOSPC:
                raise ApiError("The drive filled up while making the slimmer copy. The original is untouched.")
            raise
        after = allocated(os.stat(lp), path)
        freed = max(0, before - after)
        self.done.append({"path": path, "before": before, "after": after, "freed": freed})
        self.stats["freed"] += freed

    def result(self, q):
        return {"done": self.done, "skipped": self.skipped, "failed": self.failed, "freed": self.stats["freed"]}


# ---------------------------------------------------------------- endpoints
@route("POST", "/api/sparse/scan")
def start_scan(q, body):
    paths = clean_paths(body.get("paths"), "file or folder")
    paths = [p for p in paths if os.path.exists(util.long_path(p))]
    if not paths:
        raise ApiError("Those files or folders aren't there - is the drive connected?", 404)
    job = SparseScanJob({"paths": paths, "min_size": as_int(body.get("min_size"), 64 * MB, 1),
                         "min_reclaim": as_int(body.get("min_reclaim"), 16 * MB, 0)})
    JOBS.start(job)
    return job.status()


@route("POST", "/api/sparse/apply")
def start_apply(q, body):
    raw = body.get("files")
    if not isinstance(raw, list) or not raw:
        raise ApiError("Pick at least one file to slim down.")
    files = []
    for f in raw:
        if isinstance(f, dict):
            files.append({"path": clean_paths([f.get("path")], "file")[0], "reclaimable": f.get("reclaimable")})
        else:
            files.append({"path": clean_paths([f], "file")[0]})
    running = [j for j in JOBS.jobs.values() if j.kind == "sparse_apply" and j.live()]
    if running:
        raise ApiError("Retrova is already slimming files - wait for it to finish first.", 409)
    job = SparseApplyJob(files)
    JOBS.start(job)
    return job.status()
