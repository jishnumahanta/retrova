"""Cleanup: caches, logs, leftovers and rebuildable developer folders.

POST /api/clean/scan {paths?: [folders]}  -> job "clean_scan"
     Without paths: the standard user-level locations for this OS, plus developer folders
     and recovery leftovers found inside your home folder.
     With paths: only developer folders and recovery leftovers inside those folders.
     result -> {categories: [{id, name, detail, risk: safe|review, size, count, report_only,
                              items: [{path, size, mtime, note?, risk?}]}], total}
POST /api/clean/apply {paths} -> job "clean_apply": moves the items to the Trash.
     Refuses anything that wasn't listed by the latest finished cleanup scan.
     result -> {moved: [{path, size}], failed: [{path, error}], skipped: [{path, reason}], freed_after_empty}

Only user-level locations are ever offered; operating-system folders never are.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import stat
import tempfile
import time

from .. import util
from ..jobs import JOBS
from ..server import ApiError, route
from ..util import IS_MAC, IS_WIN, human
from . import regen_rules
from .common import (ToolJob, clean_paths, friendly_error, is_package_name, plural, protected_reason, tree_size,
                     user_home)

DAY = 86400
LEFTOVER_PATTERNS = ("*.t5r-part", "*.retrova-part", "*.part", "*.INCOMPLETE", ".*.retrova-sparse",
                     ".*.retrova-clone-*")
INSTALLER_EXTS = (".dmg", ".pkg", ".mpkg", ".exe", ".msi")


# ---------------------------------------------------------------- categories
CATEGORIES = {
    "user_caches": ("App caches", "safe",
                    "Files apps keep to start or load faster. Apps rebuild them when needed - quit an app first for "
                    "the best result."),
    "browser_caches": ("Web browser caches", "safe",
                       "Copies of web pages and images your browsers saved. Close the browser first; you stay "
                       "signed in."),
    "package_caches": ("Package manager caches", "safe",
                       "Downloads kept by npm, pip, Homebrew, Gradle, Yarn, pnpm and similar tools. They download "
                       "again when needed."),
    "logs": ("Logs", "safe", "Diagnostic text files written by apps. Nothing needs them to run."),
    "crash_reports": ("Crash reports", "safe", "Reports saved when an app crashed. Only useful for developers."),
    "temp": ("Old temporary files", "review",
             "Temporary files older than a week. Apps that are still running may use some of them."),
    "xcode": ("Xcode build data", "safe",
              "Xcode's DerivedData, device support files and simulator caches. Xcode recreates them."),
    "xcode_archives": ("Old Xcode archives", "review",
                       "Archived app builds. Keep the ones you may need to read crash reports for."),
    "installers": ("Old installers in Downloads", "review",
                   "Disk images and installers (.dmg, .pkg, .exe, .msi) downloaded more than 30 days ago. "
                   "You can usually download them again."),
    "dev_folders": ("Rebuildable developer folders", "safe",
                    "node_modules, .pnpm-store, virtual environments and build folders that one command recreates."),
    "dev_review": ("Developer folders to check", "review",
                   "Build or package folders that can probably be recreated - check the note first."),
    "recovery_leftovers": ("Unfinished copies and downloads", "review",
                           "Pieces left by copies, recoveries or downloads that didn't finish (.part, .t5r-part, "
                           ".INCOMPLETE). An .INCOMPLETE file can be the only copy of a damaged file - keep it unless "
                           "you have the file elsewhere."),
    "trash": ("Trash" if not IS_WIN else "Recycle Bin", "safe",
              "Files you already moved to the Trash. Empty it yourself to free this space - Retrova only reports it."),
}


def _cat(cid, items, size=None, report_only=False, extra_detail=""):
    name, risk, detail = CATEGORIES[cid]
    items.sort(key=lambda i: -i["size"])
    return {"id": cid, "name": name, "detail": detail + extra_detail, "risk": risk,
            "size": size if size is not None else sum(i["size"] for i in items), "count": len(items),
            "report_only": report_only, "items": items}


def _children(path):
    try:
        return [os.path.join(path, n) for n in sorted(os.listdir(path))]
    except OSError:
        return []


def _exists(p):
    return os.path.lexists(p)


# ---------------------------------------------------------------- per-platform locations
def locations(home: str) -> dict:
    """{category id: [paths to offer as items]} for this OS (only paths that exist)."""
    H = lambda *p: os.path.join(home, *p)  # noqa: E731
    out: dict[str, list] = {}
    if IS_MAC:
        caches = H("Library", "Caches")
        out["browser_caches"] = [os.path.join(caches, n) for n in (
            "Google/Chrome", "com.apple.Safari", "Firefox", "Microsoft Edge", "BraveSoftware", "Chromium",
            "company.thebrowser.Browser", "com.operasoftware.Opera", "Vivaldi")]
        out["browser_caches"] += glob.glob(H("Library", "Application Support", "Firefox", "Profiles", "*", "cache2"))
        out["package_caches"] = [os.path.join(caches, n) for n in (
            "pip", "Homebrew", "Yarn", "CocoaPods", "go-build", "pnpm", "ms-playwright", "node-gyp", "deno",
            "com.github.Electron.ShipIt")] + [
            H(".npm", "_cacache"), H(".gradle", "caches"), H(".cargo", "registry", "cache"), H("Library", "pnpm", "store"),
            H(".pnpm-store"), H(".bun", "install", "cache"), H(".yarn", "berry", "cache"), H(".cache", "pip"),
            H(".cache", "yarn"), H("miniconda3", "pkgs"), H("anaconda3", "pkgs"), H("miniforge3", "pkgs")]
        out["xcode"] = (_children(H("Library", "Developer", "Xcode", "DerivedData")) +
                        _children(H("Library", "Developer", "Xcode", "iOS DeviceSupport")) +
                        _children(H("Library", "Developer", "Xcode", "watchOS DeviceSupport")) +
                        _children(H("Library", "Developer", "CoreSimulator", "Caches")))
        out["xcode_archives"] = _children(H("Library", "Developer", "Xcode", "Archives"))
        out["crash_reports"] = _children(H("Library", "Logs", "DiagnosticReports"))
        out["logs"] = [p for p in _children(H("Library", "Logs")) if os.path.basename(p) != "DiagnosticReports"]
        out["user_caches"] = _children(caches)
    elif IS_WIN:
        la = os.environ.get("LOCALAPPDATA") or H("AppData", "Local")
        ra = os.environ.get("APPDATA") or H("AppData", "Roaming")
        L = lambda *p: os.path.join(la, *p)  # noqa: E731
        bc = []
        for base in (L("Google", "Chrome", "User Data"), L("Microsoft", "Edge", "User Data"),
                     L("BraveSoftware", "Brave-Browser", "User Data"), L("Chromium", "User Data"),
                     L("Vivaldi", "User Data")):
            for prof in _children(base):
                if os.path.isdir(prof):
                    bc += [os.path.join(prof, n) for n in ("Cache", "Code Cache", "GPUCache", "Service Worker\\CacheStorage")]
        bc += glob.glob(L("Mozilla", "Firefox", "Profiles", "*", "cache2"))
        out["browser_caches"] = bc
        out["package_caches"] = [L("pip", "Cache"), L("Yarn", "Cache"), L("npm-cache"), os.path.join(ra, "npm-cache", "_cacache"),
                                 L("pnpm", "store"), L("go-build"), L("NuGet", "v3-cache"), L("ms-playwright"),
                                 H(".gradle", "caches"), H(".cargo", "registry", "cache"), H(".npm", "_cacache"),
                                 H(".pnpm-store"), H(".bun", "install", "cache"), H("miniconda3", "pkgs"),
                                 H("anaconda3", "pkgs")]
        out["user_caches"] = [L("Microsoft", "Windows", "INetCache"), L("D3DSCache"), L("NVIDIA", "DXCache"),
                              L("NVIDIA", "GLCache"), L("AMD", "DxCache"), L("AMD", "GLCache"),
                              L("Microsoft", "Windows", "WebCache.old"), L("CrashDumps.old")]
        out["crash_reports"] = (_children(L("CrashDumps")) +
                                _children(L("Microsoft", "Windows", "WER", "ReportArchive")) +
                                _children(L("Microsoft", "Windows", "WER", "ReportQueue")))
    else:
        cache = os.environ.get("XDG_CACHE_HOME") or H(".cache")
        C = lambda *p: os.path.join(cache, *p)  # noqa: E731
        out["browser_caches"] = [C("google-chrome"), C("chromium"), C("mozilla"), C("BraveSoftware"),
                                 C("microsoft-edge"), C("vivaldi"), C("opera")]
        out["package_caches"] = [C("pip"), C("yarn"), C("go-build"), C("pnpm"), C("ms-playwright"), C("node-gyp"),
                                 C("deno"), C("pypoetry"), C("uv"), H(".npm", "_cacache"), H(".gradle", "caches"),
                                 H(".cargo", "registry", "cache"), H(".local", "share", "pnpm", "store"),
                                 H(".pnpm-store"), H(".bun", "install", "cache"), H(".yarn", "berry", "cache"),
                                 H("miniconda3", "pkgs"), H("anaconda3", "pkgs"), H("miniforge3", "pkgs")]
        out["user_caches"] = _children(cache)
        out["logs"] = []
        out["crash_reports"] = []
    for k in list(out):
        out[k] = [p for p in out[k] if _exists(p)]
    return out


def trash_size(home: str):
    """(bytes, note) for the Trash / Recycle Bin, report only."""
    if IS_WIN:
        try:
            import ctypes

            class SHQUERYRBINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint32), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]
            info = SHQUERYRBINFO()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
                return int(info.i64Size), ""
        except Exception:
            pass
        return None, " Retrova couldn't measure it."
    t = os.path.join(home, ".Trash") if IS_MAC else os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "Trash")
    if not os.path.isdir(t):
        return 0, ""
    try:
        os.listdir(t)
    except OSError:
        return None, " macOS doesn't let Retrova look inside the Trash without Full Disk Access."
    return tree_size(t)[0], ""


# ---------------------------------------------------------------- scanning
class CleanScanJob(ToolJob):
    kind = "clean_scan"
    title = "Looking for things you can clean up"
    MAX_DEV_DEPTH = 10
    MAX_DEV_DIRS = 400000
    MIN_DEV_SIZE = 1024 * 1024

    def __init__(self, params):
        super().__init__(params)
        self.categories: list[dict] = []
        self.stats = {"found": 0, "bytes": 0, "categories": 0, "dirs": 0}

    def _item(self, path, note=None, risk=None):
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if protected_reason(path):
            return None
        if stat.S_ISLNK(st.st_mode):
            size = 0
        elif stat.S_ISDIR(st.st_mode):
            try:
                size = tree_size(path, self)[0]
            except OSError:
                return None
        else:
            from .common import allocated
            size = allocated(st, path if IS_WIN else None)
        it = {"path": path, "size": size, "mtime": int(st.st_mtime * 1000)}
        if note:
            it["note"] = note
        if risk:
            it["risk"] = risk
        return it

    def _items(self, paths, claimed, older_than=None, uid=None):
        out = []
        for p in paths:
            self.checkpoint()
            if p in claimed or any(c.startswith(p.rstrip("/\\") + os.sep) for c in claimed):
                # a folder that contains something another category offers: offer its other children instead
                if os.path.isdir(p) and not os.path.islink(p) and p not in claimed:
                    out += self._items(_children(p), claimed, older_than, uid)
                continue
            if older_than is not None or uid is not None:
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if older_than is not None and time.time() - st.st_mtime < older_than:
                    continue
                if uid is not None and hasattr(st, "st_uid") and st.st_uid != uid:
                    continue
            self.message = f"Measuring {os.path.basename(p)}"
            it = self._item(p)
            if it and it["size"] > 0:
                out.append(it)
                claimed.add(p)
        return out

    def run(self):
        home = user_home()
        extra = self.params.get("paths") or []
        standard = not extra
        cats = []
        claimed: set = set()
        steps = 9 if standard else 1
        step = 0

        def advance(label):
            nonlocal step
            step += 1
            self.phase = label
            self.progress = min(0.95, step / (steps + 1))
        if standard:
            locs = locations(home)
            for cid, label in (("browser_caches", "Checking browser caches"),
                               ("package_caches", "Checking package manager caches"),
                               ("xcode", "Checking Xcode data"), ("xcode_archives", "Checking Xcode archives"),
                               ("crash_reports", "Checking crash reports"), ("logs", "Checking logs"),
                               ("user_caches", "Checking app caches")):
                if cid not in locs:
                    continue
                advance(label)
                items = self._items(locs[cid], claimed)
                if items:
                    cats.append(_cat(cid, items))
            advance("Checking temporary files")
            tmp_items = self._items(_children(_user_tmp()), claimed, older_than=7 * DAY,
                                    uid=util.REAL_UID if not IS_WIN else None)
            if tmp_items:
                cats.append(_cat("temp", tmp_items))
            advance("Checking old installers")
            inst = []
            for p in _children(os.path.join(home, "Downloads")):
                if p.lower().endswith(INSTALLER_EXTS):
                    try:
                        if time.time() - os.lstat(p).st_mtime > 30 * DAY:
                            it = self._item(p)
                            if it:
                                inst.append(it)
                    except OSError:
                        pass
            if inst:
                cats.append(_cat("installers", inst))
        advance("Looking for developer folders and unfinished copies")
        roots = [home] if standard else extra
        dev_safe, dev_review, leftovers = self._dev_and_leftovers(roots, home, claimed, standard)
        if dev_safe:
            cats.append(_cat("dev_folders", dev_safe))
        if dev_review:
            cats.append(_cat("dev_review", dev_review))
        if leftovers:
            cats.append(_cat("recovery_leftovers", leftovers))
        if standard:
            self.phase = "Measuring the Trash"
            size, note = trash_size(home)
            if size is None or size > 0:
                cats.append(_cat("trash", [], size=size or 0, report_only=True, extra_detail=note))
        order = {c: i for i, c in enumerate(CATEGORIES)}
        cats.sort(key=lambda c: (c["report_only"], order.get(c["id"], 99)))
        self.categories = cats
        total = sum(c["size"] for c in cats if not c["report_only"])
        self.stats.update(found=sum(c["count"] for c in cats), bytes=total, categories=len(cats))
        self.progress = 1.0
        self.phase = "Done"
        self.message = (f"{human(total)} can be cleaned up in {plural(len([c for c in cats if not c['report_only']]), 'category', 'categories')}"
                        if total else "Nothing to clean up")

    def _dev_and_leftovers(self, roots, home, claimed, standard):
        dev_safe, dev_review, leftovers = [], [], []
        skip_top = set()
        if standard:
            skip_top = {os.path.join(home, n) for n in ("Library", "AppData", "Applications", ".Trash", ".cache",
                                                        ".local", ".npm", ".cargo", ".rustup", ".gradle", ".m2",
                                                        ".nuget", ".vscode", ".cursor", ".docker", ".colima",
                                                        ".lima", ".orbstack", "miniconda3", "anaconda3",
                                                        "miniforge3", "go", ".bun", "snap")}
        now = time.time()
        visited = 0
        for root in roots:
            if not os.path.isdir(root):
                continue
            # the chosen folder itself may be a dev folder (e.g. the user picked node_modules)
            par = os.path.dirname(root.rstrip("/\\"))
            if not standard and par and par != root:
                try:
                    pnames = os.listdir(par)
                except OSError:
                    pnames = []
                hit = regen_rules.classify(os.path.basename(root), pnames, lambda: _listdir(root),
                                           regen_rules.has_lockfile(pnames))
                if hit:
                    self._add_dev(root, hit, dev_safe, dev_review, claimed)
                    continue
            stack = [(root, 0, False)]
            while stack:
                self.checkpoint()
                d, depth, lock_above = stack.pop()
                visited += 1
                if visited % 500 == 0:
                    self.stats["dirs"] = visited
                    self.message = f"Looking in {d}"
                if visited > self.MAX_DEV_DIRS:
                    break
                try:
                    with os.scandir(d) as it:
                        entries = list(it)
                except OSError:
                    continue
                names = [e.name for e in entries]
                if depth > 0 and "pyvenv.cfg" in names:
                    # a Python environment with an unusual name (e.g. .release-venv)
                    try:
                        pn = os.listdir(os.path.dirname(d))
                    except OSError:
                        pn = []
                    self._add_dev(d, regen_rules.venv_verdict(os.path.basename(d), pn), dev_safe, dev_review,
                                  claimed)
                    continue
                lock_here = lock_above or regen_rules.has_lockfile(names)
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.path in skip_top or e.path in claimed:
                                continue
                            hit = regen_rules.classify(e.name, names, lambda e=e: _listdir(e.path), lock_here)
                            if hit:
                                self._add_dev(e.path, hit, dev_safe, dev_review, claimed)
                                continue
                            if e.name.startswith(".") or e.name == ".git" or is_package_name(e.name):
                                continue
                            if e.name in ("node_modules", "site-packages"):
                                continue
                            if depth + 1 <= self.MAX_DEV_DEPTH:
                                stack.append((e.path, depth + 1, lock_here))
                        elif e.is_file(follow_symlinks=False):
                            if any(fnmatch.fnmatchcase(e.name, pat) for pat in LEFTOVER_PATTERNS):
                                st = e.stat(follow_symlinks=False)
                                if now - st.st_mtime < DAY or e.path in claimed or protected_reason(e.path):
                                    continue
                                note = _leftover_note(e.name, names)
                                leftovers.append({"path": e.path, "size": st.st_size,
                                                  "mtime": int(st.st_mtime * 1000), "note": note})
                                claimed.add(e.path)
                    except OSError:
                        continue
        self.stats["dirs"] = visited
        return dev_safe, dev_review, leftovers

    def _add_dev(self, path, hit, dev_safe, dev_review, claimed):
        conf, what, regen = hit
        if path in claimed or protected_reason(path):
            return
        self.message = f"Measuring {path}"
        it = self._item(path, note=f"{what} - recreate with: {regen}", risk=conf)
        if not it or it["size"] < self.MIN_DEV_SIZE:
            return
        claimed.add(path)
        (dev_safe if conf == "safe" else dev_review).append(it)

    def result(self, q):
        cats = self.categories
        return {"categories": cats, "total": sum(c["size"] for c in cats if not c["report_only"]),
                "partial": self.live()}

    def item_paths(self) -> set:
        return {i["path"] for c in self.categories if not c["report_only"] for i in c["items"]}


def _leftover_note(name, names_here):
    base = None
    for suf in (".t5r-part", ".retrova-part", ".INCOMPLETE", ".part", ".crdownload"):
        if name.endswith(suf):
            base = name[: -len(suf)]
            break
    complete = base is not None and base in names_here
    if name.endswith(".INCOMPLETE"):
        return ("Partial copy of a damaged file. " +
                ("The complete file is right next to it." if complete else
                 "No complete version is next to it - this may be the only copy you have."))
    if name.endswith((".retrova-sparse",)) or ".retrova-clone-" in name:
        return "Temporary file Retrova left behind when it was interrupted."
    return "Unfinished piece of a copy or download." + (" The complete file is right next to it." if complete else "")


def _listdir(p):
    try:
        return os.listdir(p)
    except OSError:
        return []


def _user_tmp():
    if IS_WIN:
        return os.environ.get("TEMP") or tempfile.gettempdir()
    if IS_MAC:
        return os.environ.get("TMPDIR") or tempfile.gettempdir()
    return tempfile.gettempdir()


# ---------------------------------------------------------------- apply
class CleanApplyJob(ToolJob):
    kind = "clean_apply"
    title = "Moving to the Trash"

    def __init__(self, paths, sizes):
        super().__init__({"count": len(paths)})
        self.paths = paths
        self.sizes = sizes
        self.moved: list[dict] = []
        self.failed: list[dict] = []
        self.skipped: list[dict] = []
        self.stats = {"files_done": 0, "files_total": len(paths), "freed": 0, "failed": 0, "skipped": 0}

    def run(self):
        self.phase = "Moving to the Trash"
        for i, p in enumerate(self.paths):
            self.checkpoint()
            self.progress = i / max(1, len(self.paths))
            self.stats["files_done"] = i
            self.message = os.path.basename(p)
            if not os.path.lexists(p):
                self.skipped.append({"path": p, "reason": "It was already gone."})
                self.stats["skipped"] = len(self.skipped)
                continue
            why = protected_reason(p)
            if why:
                self.failed.append({"path": p, "error": why})
                self.add_error(p, why)
                continue
            r = util.to_trash([p])
            if r["failed"]:
                err = r["failed"][0]["error"]
                msg = err.split(": ", 1)[-1]
                if "Permission" in err or "Errno 13" in err or "Errno 1]" in err:
                    msg = friendly_error(PermissionError(13, "denied"))
                self.failed.append({"path": p, "error": msg})
                self.add_error(p, msg)
            else:
                self.moved.append({"path": p, "size": self.sizes.get(p, 0)})
                self.stats["freed"] += self.sizes.get(p, 0)
        self.stats["files_done"] = len(self.paths)
        self.progress = 1.0
        self.phase = "Done"
        n = len(self.moved)
        bin_name = "Recycle Bin" if IS_WIN else "Trash"
        self.message = (f"Moved {plural(n, 'item')} ({human(self.stats['freed'])}) to the {bin_name}. Empty the "
                        f"{bin_name} to get the space back - until then you can put anything back." if n else
                        "Nothing was moved.")
        if self.failed:
            self.message += f" {plural(len(self.failed), 'item')} couldn't be moved. {self.failed[0]['error']}"

    def result(self, q):
        return {"moved": self.moved, "failed": self.failed, "skipped": self.skipped,
                "freed_after_empty": self.stats["freed"]}


@route("POST", "/api/clean/scan")
def start_scan(q, body):
    paths = body.get("paths") or []
    if paths:
        paths = [p for p in clean_paths(paths, "folder") if os.path.isdir(p)]
        if not paths:
            raise ApiError("Those folders aren't there - is the drive connected?", 404)
    job = CleanScanJob({"paths": paths})
    JOBS.start(job)
    return job.status()


@route("POST", "/api/clean/apply")
def start_apply(q, body):
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ApiError("Pick at least one item to clean up.")
    scan = JOBS.latest("clean_scan")
    if scan is None or scan.state != "done":
        raise ApiError("Run a cleanup scan first, then pick from its list.", 409)
    allowed = scan.item_paths()
    sizes = {i["path"]: i["size"] for c in scan.categories for i in c["items"]}
    bad = [p for p in paths if not isinstance(p, str) or p not in allowed]
    if bad:
        raise ApiError("Retrova only removes items from its latest cleanup scan, and "
                       f"{plural(len(bad), 'item')} wasn't on that list. Scan again and pick from the new list.", 400)
    uniq = list(dict.fromkeys(paths))
    job = CleanApplyJob(uniq, sizes)
    JOBS.start(job)
    return job.status()
