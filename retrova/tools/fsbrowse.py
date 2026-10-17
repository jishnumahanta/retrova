"""Local file browsing for the in-app pickers and previews.

GET  /api/fs/places            -> {places: [{name, path, icon, kind, size?, free?}]}
GET  /api/fs/ls?path=&hidden=&dirs=&limit=
                               -> {path, parent, entries: [{name, path, kind, size, mtime, hidden, package, target?}],
                                   total, more}
POST /api/fs/size {paths}      -> {sizes: {path: {state, bytes, allocated, files, errors}}}   (non-blocking; poll)
GET  /api/file?path=&download= -> file bytes, Range supported (206), correct Content-Type
GET  /api/thumb?path=&size=    -> JPEG/PNG thumbnail (macOS: sips / qlmanage, cached); elsewhere the original
                                  when the web view can show it, else 404
POST /api/reveal {path}        -> {ok: true}   (Finder / Explorer)
"""
from __future__ import annotations

import hashlib
import os
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote

from .. import util
from ..server import ApiError, Reply, guess_type, route
from ..util import IS_MAC, IS_WIN
from .common import app_data, clean_path, clean_paths, friendly_error, is_package_name, user_home, as_bool, \
    as_int, walk_files, allocated

# Types a browser would execute or render as an active document: served sandboxed.
_ACTIVE_TYPES = ("text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml",
                 "text/javascript", "application/javascript", "application/x-javascript")
_SANDBOX_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; media-src 'none'"

BROWSER_IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".avif", ".svg", ".jfif", ".pjpeg"}
MAC_SIPS_TYPES = BROWSER_IMAGES | {".heic", ".heif", ".tif", ".tiff", ".psd", ".dng", ".cr2", ".cr3", ".nef",
                                   ".arw", ".orf", ".rw2", ".raf", ".jp2", ".tga", ".exr", ".pdf", ".icns"}
MAC_QL_TYPES = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp", ".mts", ".key", ".pages", ".numbers",
                ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".rtf", ".txt", ".md", ".html", ".usdz"}


# ---------------------------------------------------------------- places
def _vol_info(path):
    try:
        u = shutil.disk_usage(path)
        return u.total, u.free
    except OSError:
        return None, None


def _mac_volumes():
    out = []
    try:
        ents = sorted(os.scandir("/Volumes"), key=lambda e: e.name.lower())
    except OSError:
        ents = []
    seen_root = False
    for e in ents:
        if e.name.startswith(".") or e.name.startswith("com.apple."):
            continue
        p = e.path
        if e.is_symlink():
            try:
                p = os.path.realpath(p)
            except OSError:
                continue
        if not os.path.isdir(p):
            continue
        icon = "drive" if p == "/" else "external"
        seen_root = seen_root or p == "/"
        out.append((e.name, p, icon))
    if not seen_root:
        out.insert(0, ("Macintosh HD", "/", "drive"))
    return out


def _win_volumes():
    out = []
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetErrorMode(0x0001 | 0x8000)   # no "insert a disk" dialogs
        mask = k32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            dtype = k32.GetDriveTypeW(ctypes.c_wchar_p(root))
            if dtype in (0, 1):
                continue
            label = ctypes.create_unicode_buffer(261)
            ok = k32.GetVolumeInformationW(ctypes.c_wchar_p(root), label, 261, None, None, None, None, 0)
            if not ok:
                continue                    # not ready (empty card reader / DVD drive)
            icon = {2: "external", 3: "drive", 4: "network", 5: "disc"}.get(dtype, "drive")
            name = f"{label.value or ('Local Disk' if dtype == 3 else 'Drive')} ({root[:2]})"
            out.append((name, root, icon))
    except Exception:
        for i in range(26):
            root = f"{chr(65 + i)}:\\"
            if os.path.exists(root):
                out.append((f"Drive ({root[:2]})", root, "drive"))
    return out


def _linux_volumes():
    out = [("Computer", "/", "drive")]
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if mnt.startswith(("/media/", "/mnt/", "/run/media/")):
                    out.append((os.path.basename(mnt) or mnt, mnt, "external"))
    except OSError:
        pass
    return out


def list_volumes():
    vols = _mac_volumes() if IS_MAC else _win_volumes() if IS_WIN else _linux_volumes()
    out = []
    for name, path, icon in vols:
        total, free = _vol_info(path)
        out.append({"name": name, "path": path, "icon": icon, "kind": "volume", "size": total, "free": free})
    return out


@route("GET", "/api/fs/places")
def places(q, body):
    home = user_home()
    out = [{"name": "Home", "path": home, "icon": "home", "kind": "place"}]
    for name, icon in (("Desktop", "desktop"), ("Documents", "documents"), ("Downloads", "downloads"),
                       ("Pictures", "pictures"), ("Movies" if IS_MAC else "Videos", "videos"), ("Music", "music")):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            out.append({"name": name, "path": p, "icon": icon, "kind": "place"})
    out += list_volumes()
    return {"places": out}


# ---------------------------------------------------------------- listing
def _parent(path):
    par = os.path.dirname(path.rstrip("/\\")) or path
    if IS_WIN and re.fullmatch(r"[A-Za-z]:", par):
        par += "\\"
    if not path.rstrip("/\\") or par == path or os.path.normpath(par) == os.path.normpath(path):
        return None
    return par


def _entry(e):
    try:
        st = e.stat(follow_symlinks=False)
    except OSError:
        return {"name": e.name, "path": e.path, "kind": "other", "size": None, "mtime": None,
                "hidden": e.name.startswith("."), "package": False, "error": "unreadable"}
    m = st.st_mode
    if stat.S_ISLNK(m):
        kind = "link"
    elif stat.S_ISDIR(m):
        kind = "dir"
    elif stat.S_ISREG(m):
        kind = "file"
    else:
        kind = "other"
    hidden = e.name.startswith(".") or bool(getattr(st, "st_file_attributes", 0) & 0x2)
    d = {"name": e.name, "path": e.path, "kind": kind, "size": st.st_size if kind == "file" else None,
         "mtime": int(st.st_mtime * 1000), "hidden": hidden,
         "package": kind == "dir" and is_package_name(e.name)}
    if kind == "link":
        try:
            d["target"] = "dir" if e.is_dir() else "file" if e.is_file() else None
        except OSError:
            d["target"] = None
    return d


@route("GET", "/api/fs/ls")
def ls(q, body):
    path = clean_path(q.get("path") or user_home(), "folder")
    show_hidden = as_bool(q.get("hidden"))
    dirs_only = as_bool(q.get("dirs"))
    limit = as_int(q.get("limit"), 5000, 1, 100000)
    if not os.path.isdir(path):
        if os.path.exists(path):
            raise ApiError("That's a file, not a folder.", 400)
        raise ApiError("That folder isn't there any more - was the drive disconnected?", 404)
    try:
        with os.scandir(util.long_path(path)) as it:
            raw = list(it)
    except OSError as ex:
        raise ApiError(friendly_error(ex), 403 if isinstance(ex, PermissionError) else 400)
    entries = []
    for e in raw:
        d = _entry(e)
        d["path"] = os.path.join(path, e.name)
        if d["hidden"] and not show_hidden:
            continue
        if dirs_only and not (d["kind"] == "dir" or (d["kind"] == "link" and d.get("target") == "dir")):
            continue
        entries.append(d)
    entries.sort(key=lambda d: (0 if d["kind"] == "dir" or d.get("target") == "dir" else 1, d["name"].casefold()))
    total = len(entries)
    return {"path": path, "parent": _parent(path), "entries": entries[:limit], "total": total, "more": total > limit}


# ---------------------------------------------------------------- background sizes
class _Sizer:
    def __init__(self):
        self.res: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.q: queue.Queue = queue.Queue()
        self.started = False

    def request(self, paths):
        with self.lock:
            if not self.started:
                for i in range(2):
                    threading.Thread(target=self._run, daemon=True, name=f"fs-size-{i}").start()
                self.started = True
            now = time.time()
            for p in paths:
                r = self.res.get(p)
                if r is None or (r["state"] in ("done", "error") and now - r.get("t", now) > 60):
                    self.res[p] = {"state": "queued", "bytes": 0, "allocated": 0, "files": 0, "errors": 0}
                    self.q.put(p)
            return {p: dict(self.res[p]) for p in paths}

    def _run(self):
        while True:
            p = self.q.get()
            r = self.res[p]
            r["state"] = "running"
            seen = set()

            def err(_path, _ex, r=r):
                r["errors"] += 1
            try:
                for fp, st in walk_files([p], on_error=err):
                    r["files"] += 1
                    r["bytes"] += st.st_size
                    if st.st_nlink > 1:
                        k = (st.st_dev, st.st_ino)
                        if k in seen:
                            continue
                        seen.add(k)
                    r["allocated"] += allocated(st, fp if IS_WIN else None)
                r["state"] = "done"
            except Exception as ex:
                r["state"], r["error"] = "error", friendly_error(ex)
            r["t"] = time.time()


SIZER = _Sizer()


@route("POST", "/api/fs/size")
def fs_size(q, body):
    paths = clean_paths(body.get("paths"), "file or folder")[:500]
    return {"sizes": SIZER.request(paths)}


# ---------------------------------------------------------------- file bytes with Range
class RangeError(Exception):
    pass


def parse_range(header, size: int):
    """(start, end_inclusive) for a single 'bytes=' range, None for the whole file.
    Raises RangeError when the range can't be satisfied."""
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*(?:,.*)?", header)
    if not m:
        return None
    a, b = m.groups()
    if a == "" and b == "":
        return None
    if a == "":
        n = int(b)
        if n <= 0 or size == 0:
            raise RangeError()
        return max(0, size - n), size - 1
    start = int(a)
    end = min(int(b), size - 1) if b else size - 1
    if start >= size or start > end:
        raise RangeError()
    return start, end


def file_reply(path: str, range_header=None, download: bool = False, ctype: str | None = None,
               cache: str = "no-cache") -> Reply:
    lp = util.long_path(path)
    try:
        st = os.stat(lp)
    except OSError as ex:
        raise ApiError("This file isn't there any more." if isinstance(ex, FileNotFoundError) else friendly_error(ex),
                       404 if isinstance(ex, FileNotFoundError) else 403)
    if stat.S_ISDIR(st.st_mode):
        raise ApiError("That's a folder, not a file.", 400)
    size = st.st_size
    ctype = ctype or guess_type(path)
    if ctype.startswith("text/") and "charset" not in ctype:
        ctype += "; charset=utf-8"
    name = os.path.basename(path)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": cache,
               "Content-Disposition": f"{'attachment' if download else 'inline'}; filename*=UTF-8''{quote(name)}",
               "Last-Modified": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(st.st_mtime))}
    if ctype.split(";")[0].strip() in _ACTIVE_TYPES:
        headers["Content-Security-Policy"] = _SANDBOX_CSP
    try:
        rng = parse_range(range_header, size)
    except RangeError:
        return Reply(b"", "text/plain", 416, {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
    try:
        fh = open(lp, "rb")
    except OSError as ex:
        raise ApiError(friendly_error(ex), 403)
    if rng is None:
        return Reply(ctype=ctype, code=200, headers=headers, stream=fh, length=size)
    start, end = rng
    fh.seek(start)
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Reply(ctype=ctype, code=206, headers=headers, stream=fh, length=end - start + 1)


@route("GET", "/api/file")
def file_bytes(q, body):
    path = clean_path(q.get("path"), "file")
    return file_reply(path, q.get("_range"), as_bool(q.get("download")))


# ---------------------------------------------------------------- thumbnails
_THUMB_SEM = threading.BoundedSemaphore(3)
_THUMB_FAIL: dict[str, float] = {}
_THUMB_MADE = [0]


def _thumb_key(path, st, size):
    raw = f"{path}|{size}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8", "surrogateescape")
    return hashlib.sha1(raw).hexdigest()


def _prune_thumbs(d):
    try:
        ents = [(e.stat().st_mtime, e.path) for e in os.scandir(d) if e.is_file()]
    except OSError:
        return
    if len(ents) <= 20000:
        return
    ents.sort()
    for _t, p in ents[: len(ents) // 4]:
        try:
            os.remove(p)        # our own cache files only
        except OSError:
            pass


def _mac_make_thumb(path, ext, size, out_base):
    """Create <out_base>.jpg (sips) or <out_base>.png (Quick Look). Returns the file or None."""
    if ext in MAC_SIPS_TYPES:
        out = out_base + ".jpg"
        tmp = out + ".tmp.jpg"
        r = subprocess.run(["sips", "-Z", str(size), "-s", "format", "jpeg", "-s", "formatOptions", "75",
                            path, "--out", tmp], capture_output=True, timeout=30)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            return out
        if os.path.exists(tmp):
            os.remove(tmp)
    if ext in MAC_QL_TYPES or ext in MAC_SIPS_TYPES:
        with tempfile.TemporaryDirectory(dir=os.path.dirname(out_base)) as td:
            subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", td, path], capture_output=True, timeout=30)
            for n in os.listdir(td):
                if n.lower().endswith(".png"):
                    out = out_base + ".png"
                    os.replace(os.path.join(td, n), out)
                    return out
    return None


@route("GET", "/api/thumb")
def thumb(q, body):
    path = clean_path(q.get("path"), "file")
    size = as_int(q.get("size"), 256, 32, 1024)
    try:
        st = os.stat(util.long_path(path))
    except OSError:
        raise ApiError("This file isn't there any more.", 404)
    if not stat.S_ISREG(st.st_mode):
        raise ApiError("No preview for folders.", 404)
    ext = os.path.splitext(path)[1].lower()
    if IS_MAC and (ext in MAC_SIPS_TYPES or ext in MAC_QL_TYPES):
        d = app_data("thumbs")
        key = _thumb_key(path, st, size)
        base = os.path.join(d, key)
        for cand, ct in ((base + ".jpg", "image/jpeg"), (base + ".png", "image/png")):
            if os.path.exists(cand):
                return file_reply(cand, None, ctype=ct, cache="private, max-age=600")
        if key in _THUMB_FAIL:
            raise ApiError("No preview for this file.", 404)
        with _THUMB_SEM:
            try:
                made = _mac_make_thumb(path, ext, size, base)
            except (OSError, subprocess.SubprocessError):
                made = None
        if made:
            _THUMB_MADE[0] += 1
            if _THUMB_MADE[0] % 500 == 0:
                _prune_thumbs(d)
            return file_reply(made, None, ctype="image/png" if made.endswith(".png") else "image/jpeg",
                              cache="private, max-age=600")
        if len(_THUMB_FAIL) > 5000:
            _THUMB_FAIL.clear()
        _THUMB_FAIL[key] = time.time()
        raise ApiError("No preview for this file.", 404)
    if ext in BROWSER_IMAGES:
        return file_reply(path, None, cache="private, max-age=600")
    raise ApiError("No preview for this file type.", 404)


# ---------------------------------------------------------------- reveal
@route("POST", "/api/reveal")
def reveal(q, body):
    path = clean_path(body.get("path"), "file or folder")
    if not os.path.lexists(util.long_path(path)):
        raise ApiError("That isn't there any more.", 404)
    try:
        util.reveal(path)
    except OSError:
        raise ApiError("Couldn't open the file manager on this system.", 501)
    return {"ok": True}
