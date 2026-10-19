"""Shared helpers for Retrova's disk-care tools.

Everything here is standard library only. Optional dependencies (xxhash) are
imported lazily and replaced by hashlib when missing.
"""
from __future__ import annotations

import collections
import errno
import hashlib
import os
import re
import stat
import threading

from .. import util
from ..jobs import Job
from ..server import ApiError
from ..util import IS_MAC, IS_WIN, IS_LINUX

CHUNK = 8 * 1024 * 1024          # sequential read size
BLOCK = 64 * 1024                # zero-detection / sample granularity
ZERO_BLOCK = bytes(BLOCK)


# ---------------------------------------------------------------- locations
def user_home() -> str:
    """The real user's home folder (also correct when Retrova runs elevated).
    RETROVA_HOME overrides it (used by tests and development only)."""
    return os.environ.get("RETROVA_HOME") or util.REAL_HOME


def app_data(*sub: str) -> str:
    """Retrova's own data folder (created on demand), optionally a sub-folder of it."""
    base = os.environ.get("RETROVA_APP_DATA")
    if not base:
        h = util.REAL_HOME
        if IS_MAC:
            base = os.path.join(h, "Library", "Application Support", "Retrova")
        elif IS_WIN:
            base = os.path.join(os.environ.get("LOCALAPPDATA", h), "Retrova")
        else:
            base = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(h, ".local", "share")), "retrova")
    d = os.path.join(base, *sub)
    os.makedirs(d, exist_ok=True)
    return d


def clean_path(p, what: str = "file or folder") -> str:
    """Validate and normalise a path that came from the UI."""
    if not isinstance(p, str) or not p.strip():
        raise ApiError(f"Choose a {what} first.")
    p = p.strip()
    if "\x00" in p:
        raise ApiError("That path contains an invalid character.")
    if p == "~" or p.startswith("~/") or p.startswith("~\\"):
        p = user_home() + p[1:]
    if not os.path.isabs(p):
        raise ApiError(f"Please choose the {what} with the picker (a full path is needed).")
    return os.path.normpath(p)


def clean_paths(ps, what: str = "file or folder") -> list[str]:
    if isinstance(ps, str):
        ps = [ps]
    if not isinstance(ps, (list, tuple)) or not ps:
        raise ApiError(f"Choose at least one {what} first.")
    out, seen = [], set()
    for p in ps:
        c = clean_path(p, what)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def as_int(v, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def as_bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


# ---------------------------------------------------------------- sizes
def allocated(st, path: str | None = None) -> int:
    """Bytes a file really occupies on disk (sparse/compressed aware where the OS tells us)."""
    b = getattr(st, "st_blocks", None)
    if b is not None:
        return b * 512
    if IS_WIN and path:
        return win_allocated(path, st.st_size)
    return st.st_size


def win_allocated(path: str, default: int) -> int:
    """Windows: GetCompressedFileSizeW gives the allocated size of sparse/compressed files."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        fn = k32.GetCompressedFileSizeW
        fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.DWORD
        high = wintypes.DWORD(0)
        low = fn(util.long_path(path), ctypes.byref(high))
        if low == 0xFFFFFFFF and ctypes.GetLastError() != 0:
            return default
        return (high.value << 32) + low
    except Exception:
        return default


# ---------------------------------------------------------------- hashing
_XX = util.Once(lambda: _load_xxhash())


def _load_xxhash():
    try:
        import xxhash  # type: ignore
        return xxhash
    except Exception:
        return None


def new_hasher():
    """Fast non-cryptographic content hash: xxh3-128 if xxhash is installed, else blake2b-128."""
    xx = _XX.get()
    if xx is not None:
        return xx.xxh3_128()
    return hashlib.blake2b(digest_size=16)


def hash_name() -> str:
    return "xxh3_128" if _XX.get() is not None else "blake2b-128"


def sample_hash(path: str, size: int, sample: int = BLOCK) -> str:
    """Hash of the first and last `sample` bytes (whole file when small)."""
    h = new_hasher()
    with open(util.long_path(path), "rb") as f:
        if size <= 2 * sample:
            h.update(f.read())
        else:
            h.update(f.read(sample))
            f.seek(size - sample)
            h.update(f.read(sample))
    h.update(str(size).encode())
    return h.hexdigest()


def files_equal(a: str, b: str, checkpoint=None, progress=None) -> bool:
    """Byte-for-byte comparison of two files."""
    with open(util.long_path(a), "rb") as fa, open(util.long_path(b), "rb") as fb:
        while True:
            if checkpoint:
                checkpoint()
            x = fa.read(CHUNK)
            y = fb.read(CHUNK)
            if x != y:
                return False
            if not x:
                return True
            if progress:
                progress(len(x))


# ---------------------------------------------------------------- safety
_PKG_APP = (".app", ".framework", ".bundle", ".kext", ".plugin", ".xpc", ".appex", ".prefpane",
            ".qlgenerator", ".mdimporter", ".saver", ".wdgt", ".dext", ".systemextension")
_PKG_LIBRARY = (".photoslibrary", ".photolibrary", ".migratedphotolibrary", ".aplibrary", ".musiclibrary",
                ".tvlibrary", ".imovielibrary", ".theater", ".fcpbundle", ".logicx", ".band", ".xcarchive",
                ".sparsebundle", ".rtfd", ".pages", ".numbers", ".key", ".graffle", ".scriv", ".lrdata",
                ".lrlibrary", ".cptx")
PACKAGE_EXTS = _PKG_APP + _PKG_LIBRARY


def is_package_name(name: str) -> bool:
    """macOS package/library folders: their insides must never be edited piecemeal."""
    return name.lower().endswith(PACKAGE_EXTS)


def inside_package(path: str) -> str | None:
    """Name of the app/library package that contains `path`, if any."""
    parts = re.split(r"[\\/]+", path)
    for comp in parts[:-1]:
        if is_package_name(comp):
            return comp
    return None


def _mac_protected():
    return ("/System", "/usr", "/bin", "/sbin", "/private/var/db", "/Library", "/private/etc",
            "/private/var/root", "/private/var/vm", "/private/var/protected", "/cores", "/Applications/Utilities",
            "/etc", "/var/db", "/var/vm")


def _linux_protected():
    return ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/boot", "/proc", "/sys",
            "/dev", "/run", "/var/lib", "/var/db", "/var/cache/apt", "/snap", "/srv")


def _win_protected():
    env = os.environ
    sysdrive = env.get("SystemDrive", "C:")
    out = [env.get("SystemRoot") or sysdrive + "\\Windows", env.get("ProgramData") or sysdrive + "\\ProgramData"]
    for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)"):
        if env.get(k):
            out.append(env[k])
    out += [sysdrive + "\\Program Files", sysdrive + "\\Program Files (x86)", sysdrive + "\\$Recycle.Bin",
            sysdrive + "\\System Volume Information", sysdrive + "\\Recovery", sysdrive + "\\Boot",
            sysdrive + "\\pagefile.sys", sysdrive + "\\hiberfil.sys", sysdrive + "\\swapfile.sys",
            sysdrive + "\\DumpStack.log.tmp"]
    return tuple(out)


def _under(path: str, roots) -> bool:
    if IS_WIN:
        p = path.lower().rstrip("\\")
        return any(p == r.lower().rstrip("\\") or p.startswith(r.lower().rstrip("\\") + "\\") for r in roots)
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def _real(path: str) -> str:
    """Resolve symlinks in the folder part only (the item itself may be a link)."""
    d, n = os.path.split(os.path.abspath(path))
    try:
        d = os.path.realpath(d)
    except OSError:
        pass
    return os.path.join(d, n)


def protected_reason(path: str, allow_packages: bool = False) -> str | None:
    """Why Retrova must never remove or rewrite `path` (None when it's an ordinary user file)."""
    p = util.display_path(path)
    rp = _real(p)
    roots = _win_protected() if IS_WIN else _mac_protected() if IS_MAC else _linux_protected()
    for cand in {p, rp}:
        if _under(cand, roots):
            return "It's part of the operating system, so Retrova won't touch it."
        if os.path.dirname(cand.rstrip("/\\")) == cand.rstrip("/\\") or cand in ("/", "") or \
                re.fullmatch(r"[A-Za-z]:\\?", cand):
            return "That's the top of a drive."
    home = user_home()
    norm = (lambda s: s.lower().rstrip("\\/")) if IS_WIN else (lambda s: s.rstrip("/"))
    specials = {norm(home)} | {norm(os.path.join(home, n)) for n in (
        "Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music", "Videos", "Library", "AppData",
        "Applications", "Public", "OneDrive", "iCloud Drive")}
    if norm(p) in specials or norm(rp) in specials:
        return "That's one of your main folders, so Retrova won't remove it."
    try:
        if os.path.ismount(p):
            return "That's the top of a drive."
    except OSError:
        pass
    ad = os.environ.get("RETROVA_APP_DATA")
    if ad and _under(rp, (os.path.realpath(ad),)):
        return "That belongs to Retrova itself."
    if not allow_packages:
        pkg = inside_package(rp)
        if pkg:
            return f"It's inside '{pkg}', which macOS treats as a single item - changing its insides could break it."
    return None


# ---------------------------------------------------------------- friendly errors
def friendly_error(ex: BaseException) -> str:
    """Plain-English explanation of an OS error, shown verbatim to the user."""
    if isinstance(ex, ApiError):
        return str(ex)
    if isinstance(ex, OSError):
        e = ex.errno
        win = getattr(ex, "winerror", None)
        if e in (errno.EACCES, errno.EPERM) or isinstance(ex, PermissionError):
            if IS_MAC:
                return ("macOS didn't allow Retrova to access this. Give Retrova Full Disk Access in System Settings "
                        "> Privacy & Security, or use 'Run with admin rights'.")
            if IS_WIN:
                return "Windows didn't allow access to this (it may be in use, or need admin rights)."
            return "Permission denied - Retrova isn't allowed to access this."
        if e == errno.ENOENT or isinstance(ex, FileNotFoundError):
            return "It isn't there any more (moved, renamed or removed)."
        if e == errno.ENOSPC:
            return "The drive is full."
        if e == errno.EROFS:
            return "That drive is read-only."
        if e in (errno.EBUSY, getattr(errno, "ETXTBSY", -1)) or win in (32, 33):
            return "It's in use by another app. Quit that app and try again."
        if e == errno.EIO:
            return "The drive couldn't read this part - it may be damaged."
        if e == errno.ENAMETOOLONG:
            return "The name or path is too long for this drive."
        if e == errno.EXDEV:
            return "These are on different drives."
        if e == errno.ENOTSUP or e == getattr(errno, "EOPNOTSUPP", -2):
            return "This drive doesn't support that operation."
        msg = ex.strerror or str(ex)
        return msg[0].upper() + msg[1:] if msg else "Unexpected error."
    return f"{type(ex).__name__}: {ex}"


# ---------------------------------------------------------------- copy-suffix variants
_EXT = r"(?P<ext>\.[^.\s/\\]{1,12})?"
_VARIANT_RES = (
    ("recovery_twin", re.compile(r"^(?P<orig>.+)\.(?:t5r|restore|retrova)-\d+$")),
    ("incomplete", re.compile(r"^(?P<orig>.+)\.(?:t5r-part|retrova-part|INCOMPLETE|part|crdownload)$")),
    ("copy", re.compile(r"^(?P<stem>.+?) copy(?: \d+)?" + _EXT + "$")),
    ("copy", re.compile(r"^(?P<stem>.+?) - Copy(?: \(\d+\))?" + _EXT + "$")),
    ("numbered", re.compile(r"^(?P<stem>.+?) \(\d{1,3}\)" + _EXT + "$")),
    ("numbered", re.compile(r"^(?P<stem>.+?) (?:[2-9]|[1-9]\d)" + _EXT + "$")),
)
VARIANT_LABELS = {
    "recovery_twin": "Second copy saved by a recovery (name ends in .t5r-N, .restore-N or .retrova-N)",
    "incomplete": "Unfinished or partial copy",
    "copy": "\"copy\" added to the name",
    "numbered": "Number added to the name",
}


def variant_of(name: str):
    """(kind, original_name) when `name` looks like a copy-suffix twin of another file, else None."""
    for kind, rx in _VARIANT_RES:
        m = rx.match(name)
        if not m:
            continue
        if "orig" in m.groupdict():
            orig = m.group("orig")
        else:
            orig = m.group("stem") + (m.group("ext") or "")
        if orig and orig != name:
            return kind, orig
    return None


def looks_like_copy(path: str) -> bool:
    return variant_of(os.path.basename(path)) is not None


# ---------------------------------------------------------------- walking
SKIP_ABS = {"/proc", "/dev", "/sys", "/run", "/System/Volumes/Data", "/System/Volumes/VM",
            "/System/Volumes/Preboot", "/System/Volumes/Update", "/System/Volumes/xarts",
            "/System/Volumes/iSCPreboot", "/System/Volumes/Hardware", "/private/var/vm", "/Volumes/.timemachine",
            "/.MobileBackups", "/.Spotlight-V100", "/.fseventsd", "/.DocumentRevisions-V100"}
SKIP_NAMES = {".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100", ".TemporaryItems",
              "System Volume Information", "$RECYCLE.BIN", ".Trashes", "$Recycle.Bin"}


def entry_stat(e):
    """lstat of an os.scandir entry. On Windows DirEntry.stat() leaves st_dev, st_ino and st_nlink
    at 0, so the one-volume check skipped every subfolder and file identity checks misfired;
    os.lstat() fills them in."""
    return os.lstat(e.path) if IS_WIN else e.stat(follow_symlinks=False)


def allowed_devs(root: str) -> set:
    """Devices a walk of `root` may enter (one volume; on macOS the system+data volume pair)."""
    devs = set()
    try:
        devs.add(os.stat(root).st_dev)
    except OSError:
        return devs
    if IS_MAC:
        try:
            sys_dev = os.stat("/").st_dev
            data_dev = os.stat("/System/Volumes/Data").st_dev
            if sys_dev in devs or data_dev in devs:
                devs |= {sys_dev, data_dev}
        except OSError:
            pass
    return devs


def is_hidden(entry_name: str, st=None) -> bool:
    if entry_name.startswith("."):
        return True
    if IS_WIN and st is not None:
        return bool(getattr(st, "st_file_attributes", 0) & 0x2)
    return False


def walk_files(roots, job: Job | None = None, *, hidden: bool = True, packages: bool = True,
               skip_dir=None, on_error=None, on_dir=None, one_volume: bool = True):
    """Yield (path, stat) for every regular file under `roots` (files or folders).

    Never follows symbolic links; stays on each root's volume; skips OS bookkeeping
    folders. skip_dir(path, name) -> True prunes a folder. on_error(path, exc) is called
    for unreadable folders."""
    for root in roots:
        try:
            st = os.lstat(root)
        except OSError as ex:
            if on_error:
                on_error(root, ex)
            continue
        if stat.S_ISREG(st.st_mode):
            yield root, st
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        devs = allowed_devs(root) if one_volume else None
        stack = [root]
        while stack:
            if job is not None:
                job.checkpoint()
            d = stack.pop()
            if on_dir:
                on_dir(d)
            try:
                with os.scandir(d) as it:
                    entries = list(it)
            except OSError as ex:
                if on_error:
                    on_error(d, ex)
                continue
            subs = []
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name in SKIP_NAMES or e.path in SKIP_ABS:
                            continue
                        if not hidden and e.name.startswith("."):
                            continue
                        if not packages and is_package_name(e.name):
                            continue
                        if skip_dir and skip_dir(e.path, e.name):
                            continue
                        if devs is not None:
                            dst = entry_stat(e)
                            if dst.st_dev not in devs:
                                continue
                        subs.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        fst = entry_stat(e)
                        if not hidden and is_hidden(e.name, fst):
                            continue
                        yield e.path, fst
                except OSError as ex:
                    if on_error:
                        on_error(e.path, ex)
            subs.sort(reverse=True)
            stack.extend(subs)


def tree_size(path: str, job: Job | None = None) -> tuple[int, int, int]:
    """(allocated bytes, apparent bytes, files) of a file or folder; links not followed, same volume."""
    alloc = app = n = 0
    seen = set()
    for _p, st in walk_files([path], job):
        n += 1
        app += st.st_size
        if st.st_nlink > 1:
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
        alloc += allocated(st, _p if IS_WIN else None)
    return alloc, app, n


# ---------------------------------------------------------------- job base
class ToolJob(Job):
    """Job with an error list and automatic speed / ETA from stats['bytes_done']."""

    def __init__(self, params=None):
        super().__init__(params)
        self.errors: collections.deque = collections.deque(maxlen=300)
        self.err_lock = threading.Lock()

    def add_error(self, path: str, ex) -> None:
        msg = ex if isinstance(ex, str) else friendly_error(ex)
        with self.err_lock:
            self.errors.append({"path": path, "error": msg})
            self.stats["failed"] = self.stats.get("failed", 0) + 1

    def extra_status(self) -> dict:
        out = {"errors": list(self.errors)[-50:]}
        st = dict(self.stats)
        bd = st.get("bytes_done")
        if bd is not None and self.state in ("running", "paused"):
            sp = self.rate(bd)
            st["speed"] = sp
            bt = st.get("bytes_total")
            st["eta"] = (bt - bd) / sp if sp > 0 and bt and bt > bd else None
        elif bd is not None:
            st["speed"], st["eta"] = 0, None
        out["stats"] = st
        return out


def wait_job(job: Job, timeout: float = 600.0) -> Job:
    """Block until a job ends (tests and internal helpers)."""
    import time
    t = time.time()
    while job.live():
        if time.time() - t > timeout:
            raise TimeoutError(f"job {job.kind} did not finish")
        time.sleep(0.02)
    return job


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


def platform_name() -> str:
    return "Mac" if IS_MAC else "PC" if IS_WIN else "computer"


__all__ = ["CHUNK", "BLOCK", "ZERO_BLOCK", "IS_MAC", "IS_WIN", "IS_LINUX", "ToolJob", "user_home", "app_data"]
