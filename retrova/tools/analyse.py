"""Space analyser: where did the space go?  (job kind "analyse")

POST /api/analyse {path}  -> job status
GET  /api/job/result?id=&view=summary|tree|largest|variants|dupe_dirs|sparse

Walks one folder or drive with os.scandir (links are never followed, other volumes are
not entered) and keeps only a compact per-folder aggregate plus the top-N largest
files, so half a million files cost a few tens of MB. Sizes are *allocated* bytes
(what the files really take on disk; st_blocks * 512) with the apparent size
(the file length) alongside.
"""
from __future__ import annotations

import hashlib
import heapq
import os
import shutil
from array import array

from .. import util
from ..jobs import JOBS
from ..server import ApiError, route
from ..util import IS_MAC, IS_WIN, human
from . import regen_rules
from .common import (BLOCK, SKIP_ABS, ZERO_BLOCK, ToolJob, allowed_devs, entry_stat, as_int, clean_path, friendly_error,
                     is_package_name, plural, sample_hash, variant_of, walk_files)

# ---------------------------------------------------------------- file types
_TYPES = {
    "photos": ("Photos & images", "jpg jpeg jfif png heic heif gif tif tiff bmp webp avif cr2 cr3 nef arw dng orf "
                                  "rw2 raf srw pef psd psb xcf ico icns svg ai eps exr"),
    "videos": ("Videos", "mp4 mov m4v avi mkv wmv flv webm mts m2ts 3gp mpg mpeg vob braw r3d insv lrv mxf"),
    "audio": ("Music & audio", "mp3 m4a m4b aac wav aif aiff flac ogg oga opus wma alac caf mid midi amr"),
    "documents": ("Documents", "pdf doc docx xls xlsx ppt pptx pages numbers key txt rtf md odt ods odp csv tsv "
                               "epub mobi tex"),
    "archives": ("Archives", "zip rar 7z tar gz tgz bz2 xz zst lz4 tbz txz cab"),
    "disk_images": ("Disk images & virtual machines", "dmg iso img raw qcow qcow2 vmdk vdi vhd vhdx hdd hds "
                                                      "sparseimage toast bin cue"),
    "apps": ("Apps & installers", "pkg mpkg exe msi dll dylib so apk ipa appx msix deb rpm jar"),
    "code": ("Code & developer files", "py pyc js mjs cjs ts tsx jsx json map java class c h cc cpp hpp go rs rb "
                                       "php swift kt m mm cs html css scss less sql sh yml yaml toml lock xml "
                                       "wasm o a lib obj pdb node"),
    "data": ("App data & databases", "db sqlite sqlite3 sqlitedb realm ldb log plist dat idx pack"),
}
EXT_GROUP = {}
for _gid, (_label, _exts) in _TYPES.items():
    for _x in _exts.split():
        EXT_GROUP["." + _x] = _gid
TYPE_LABEL = {g: lab for g, (lab, _x) in _TYPES.items()}
TYPE_LABEL["other"] = "Other files"

VM_DISK_NAMES = {"datadisk", "diffdisk", "basedisk", "docker.raw", "data.img", "disk.img", "rootfs.img",
                 "ext4.vhdx", "swap.vhdx"}
VM_DISK_EXTS = {".img", ".raw", ".qcow2", ".qcow", ".vmdk", ".vdi", ".vhd", ".vhdx", ".hdd", ".hds"}
TRASH_NAMES = {".trash", ".trashes", "$recycle.bin", "old-trash", "recycler"}

F_DEV = 1        # inside a regenerable developer folder
F_REGEN = 2      # the regenerable folder itself
F_BAD = 4        # couldn't be listed
F_INPKG = 8      # strictly inside a macOS package (.app, .photoslibrary ...)

MB = 1024 * 1024


def type_of(name: str) -> str:
    low = name.lower()
    if low in VM_DISK_NAMES:
        return "disk_images"
    dot = low.rfind(".")
    if dot <= 0:
        return "other"
    return EXT_GROUP.get(low[dot:], "other")


def _quick_variant_candidate(name: str) -> bool:
    return (" " in name or ".t5r" in name or ".restore-" in name or ".retrova-" in name
            or name.endswith((".part", ".INCOMPLETE", ".crdownload")))


def _sig_int(h) -> int:
    return int.from_bytes(h.digest(), "little")


class _Snapshot:
    """Folder totals computed from the per-folder records (bottom-up)."""

    def __init__(self, job: "AnalyseJob"):
        with job.lock:
            n = len(job.d_name)
            self.n = n
            self.parent = job.d_parent[:n]
            self.t_size = job.d_size[:n]
            self.t_alloc = job.d_alloc[:n]
            self.t_files = job.d_files[:n]
            self.flags = bytes(job.d_flags[:n])
        self.t_dirs = array("q", bytes(8 * n))
        self.kids: list[list[int]] = [[] for _ in range(n)]
        par = self.parent
        for i in range(n - 1, 0, -1):
            p = par[i]
            self.t_size[p] += self.t_size[i]
            self.t_alloc[p] += self.t_alloc[i]
            self.t_files[p] += self.t_files[i]
            self.t_dirs[p] += self.t_dirs[i] + 1
            self.kids[p].append(i)


class AnalyseJob(ToolJob):
    kind = "analyse"
    title = "Space analysis"
    LARGEST_N = 200
    SPARSE_MIN = 256 * MB
    SPARSE_CANDIDATES = 60
    VARIANTS_MAX = 50000
    DUPE_DIR_MIN = 1 * MB

    def __init__(self, params):
        super().__init__(params)
        self.root = params["path"]
        nm = os.path.basename(self.root.rstrip("/\\")) or self.root
        self.title = f"Space used in {nm}"
        self.d_name: list[str] = []
        self.d_parent = array("q")
        self.d_size = array("q")
        self.d_alloc = array("q")
        self.d_files = array("q")
        self.d_flags = bytearray()
        self.d_sig = array("Q")
        self.regen: dict[int, tuple] = {}
        self.trash_dirs: list[int] = []
        self.largest: list[tuple] = []
        self.types: dict[str, list[int]] = {}
        self.variants: list[dict] = []
        self.variant_total = 0
        self.sparse_cands: list[tuple] = []
        self.unreadable: list[str] = []
        self.n_unreadable = 0
        self.skipped_mounts: list[str] = []
        self.seq = 0
        self._snap: _Snapshot | None = None
        self._snap_t = 0.0
        self.dupe_groups: list[dict] | None = None
        self.sparse_files: list[dict] | None = None
        self.findings: list[dict] | None = None
        self.stats = {"files": 0, "dirs": 0, "bytes": 0, "apparent": 0, "unreadable": 0,
                      "files_done": 0, "bytes_done": 0}

    # ------------------------------------------------------------ walk
    def run(self):
        root = self.root
        devs = allowed_devs(root)
        est = None
        try:
            if os.path.ismount(root):
                du = shutil.disk_usage(root)
                est = max(1, du.total - du.free)
                self.stats["bytes_total"] = est
        except OSError:
            pass
        self.phase = "Measuring folders"
        seen_links: set = set()
        stack = [(root, -1, root, False, 0, None)]
        n_files = alloc_total = app_total = 0
        while stack:
            self.checkpoint()
            path, pid, name, lock_above, flags, mark = stack.pop()
            if mark is not None and mark != "trash":
                flags |= F_REGEN
            with self.lock:
                did = len(self.d_name)
                self.d_name.append(name)
                self.d_parent.append(pid)
                self.d_size.append(0)
                self.d_alloc.append(0)
                self.d_files.append(0)
                self.d_flags.append(flags)
                self.d_sig.append(0)
                if mark == "trash":
                    self.trash_dirs.append(did)
                elif mark is not None:
                    self.regen[did] = mark
            self.message = f"Looking in {path}"
            try:
                with os.scandir(util.long_path(path)) as it:
                    entries = list(it)
            except OSError as ex:
                self.d_flags[did] |= F_BAD
                self.n_unreadable += 1
                self.stats["unreadable"] = self.n_unreadable
                if len(self.unreadable) < 50:
                    self.unreadable.append(path)
                self.add_error(path, ex)
                continue
            names = [e.name for e in entries]
            lock_here = lock_above or regen_rules.has_lockfile(names)
            in_dev = bool(flags & F_DEV)
            in_pkg = bool(flags & F_INPKG)
            own_size = own_alloc = own_files = 0
            sig_items = []
            sizes_here: dict[str, int] = {}
            maybe_variants = []
            local_types: dict[str, list[int]] = {}
            subdirs = []
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        child = os.path.join(path, e.name)
                        if child in SKIP_ABS:
                            continue
                        dst = entry_stat(e)
                        if dst.st_dev not in devs:
                            if len(self.skipped_mounts) < 50:
                                self.skipped_mounts.append(child)
                            continue
                        cflags = flags & (F_DEV | F_INPKG)
                        if is_package_name(e.name):
                            cflags |= F_INPKG     # children of the package are "inside" it
                        if not in_dev and not in_pkg:
                            hit = regen_rules.classify(e.name, names, lambda c=child: _listdir(c), lock_here)
                            if hit:
                                cflags |= F_DEV
                                subdirs.append((child, did, e.name, lock_here, cflags, hit))
                                continue
                            if e.name.lower() in TRASH_NAMES:
                                subdirs.append((child, did, e.name, lock_here, cflags, "trash"))
                                continue
                        subdirs.append((child, did, e.name, lock_here, cflags, None))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        size = st.st_size
                        blocks = getattr(st, "st_blocks", None)
                        alloc = blocks * 512 if blocks is not None else size
                        if st.st_nlink > 1:
                            key = (st.st_dev, st.st_ino)
                            if key in seen_links:
                                alloc = 0
                            else:
                                seen_links.add(key)
                        own_size += size
                        own_alloc += alloc
                        own_files += 1
                        nm = e.name
                        t = type_of(nm)
                        lt = local_types.get(t)
                        if lt is None:
                            local_types[t] = [alloc, 1]
                        else:
                            lt[0] += alloc
                            lt[1] += 1
                        if not in_dev:
                            sig_items.append(f"{nm}\0{size}")
                            sizes_here[nm] = size
                            if _quick_variant_candidate(nm):
                                maybe_variants.append(nm)
                        if len(self.largest) < self.LARGEST_N or alloc > self.largest[0][0]:
                            item = (alloc, self.seq, os.path.join(path, nm), size, st.st_mtime)
                            self.seq += 1
                            with self.lock:
                                if len(self.largest) < self.LARGEST_N:
                                    heapq.heappush(self.largest, item)
                                else:
                                    heapq.heappushpop(self.largest, item)
                        if size >= self.SPARSE_MIN and alloc >= size // 2 and not in_pkg:
                            item = (size, self.seq, os.path.join(path, nm), alloc)
                            self.seq += 1
                            if len(self.sparse_cands) < self.SPARSE_CANDIDATES:
                                heapq.heappush(self.sparse_cands, item)
                            else:
                                heapq.heappushpop(self.sparse_cands, item)
                except OSError as ex:
                    self.add_error(e.path, ex)
            if maybe_variants:
                self._variants(path, maybe_variants, sizes_here)
            sig = 0
            if not in_dev:
                h = hashlib.blake2b(digest_size=8)
                sig_items.sort()
                for s in sig_items:
                    h.update(s.encode("utf-8", "surrogateescape"))
                    h.update(b"\1")
                sig = _sig_int(h)
            n_files += own_files
            alloc_total += own_alloc
            app_total += own_size
            with self.lock:
                self.d_size[did] = own_size
                self.d_alloc[did] = own_alloc
                self.d_files[did] = own_files
                self.d_sig[did] = sig
                for t, (b, c) in local_types.items():
                    g = self.types.get(t)
                    if g is None:
                        self.types[t] = [b, c]
                    else:
                        g[0] += b
                        g[1] += c
            stack.extend(reversed(subdirs))
            st_ = self.stats
            st_["files"] = st_["files_done"] = n_files
            st_["dirs"] = len(self.d_name)
            st_["bytes"] = st_["bytes_done"] = alloc_total
            st_["apparent"] = app_total
            if est:
                self.progress = min(0.85, 0.85 * alloc_total / est)
        self.stats.pop("bytes_total", None)
        self.message = ""
        self.phase = "Adding up"
        self.progress = 0.86
        self._snapshot(force=True)
        self.checkpoint()
        self.phase = "Looking for folders that were copied twice"
        self.progress = 0.88
        self.dupe_groups = self._dupe_dirs()
        self.checkpoint()
        self.phase = "Checking big files for empty space"
        self.progress = 0.94
        self.sparse_files = self._sample_sparse()
        self.phase = "Writing up findings"
        self.findings = self._findings()
        self.progress = 1.0
        snap = self._snapshot()
        self.phase = "Done"
        self.message = (f"{human(snap.t_alloc[0] if snap.n else 0)} in {plural(int(snap.t_files[0]) if snap.n else 0, 'file')}"
                        f" - {plural(len(self.findings), 'suggestion')}")

    # ------------------------------------------------------------ variants
    def _variants(self, path, names, sizes_here):
        out = []
        for nm in names:
            v = variant_of(nm)
            if not v:
                continue
            kind, orig = v
            osize = sizes_here.get(orig)
            exists = orig in sizes_here
            if kind in ("copy", "numbered") and not exists:
                continue
            if kind == "recovery_twin" and not exists:
                continue
            size = sizes_here[nm]
            out.append({"path": os.path.join(path, nm), "name": nm, "kind": kind, "original": orig,
                        "original_path": os.path.join(path, orig), "original_exists": exists,
                        "size": size, "original_size": osize, "same_size": exists and osize == size})
        if not out:
            return
        with self.lock:
            self.variant_total += len(out)
            room = self.VARIANTS_MAX - len(self.variants)
            if room > 0:
                self.variants.extend(out[:room])

    # ------------------------------------------------------------ snapshot / paths
    def _snapshot(self, force=False) -> _Snapshot:
        import time
        n = len(self.d_name)
        s = self._snap
        if s is not None and not force and s.n == n and (not self.live() or time.time() - self._snap_t < 2):
            return s
        if s is not None and not force and self.live() and time.time() - self._snap_t < 1.5:
            return s
        s = _Snapshot(self)
        self._snap, self._snap_t = s, time.time()
        return s

    def path_of(self, i: int) -> str:
        parts = []
        while i > 0:
            parts.append(self.d_name[i])
            i = self.d_parent[i]
        return os.path.join(self.root, *reversed(parts)) if parts else self.root

    def find(self, node: str, snap: _Snapshot):
        node = os.path.normpath(node)
        root = os.path.normpath(self.root)
        if (node.lower() == root.lower()) if IS_WIN else node == root:
            return 0
        rel = os.path.relpath(node, root) if not IS_WIN or os.path.splitdrive(node)[0].lower() == \
            os.path.splitdrive(root)[0].lower() else ".."
        if rel.startswith(".."):
            return None
        cur = 0
        for comp in rel.split(os.sep):
            for c in snap.kids[cur]:
                nm = self.d_name[c]
                if nm == comp or (IS_WIN and nm.lower() == comp.lower()):
                    cur = c
                    break
            else:
                return None
        return cur

    # ------------------------------------------------------------ duplicate folders
    def _dupe_dirs(self):
        snap = self._snapshot()
        n = snap.n
        if n < 2:
            return []
        full = array("Q", bytes(8 * n))
        bad = bytearray(n)
        names = self.d_name
        own = self.d_sig
        for i in range(n - 1, -1, -1):
            if i % 5000 == 0:
                self.checkpoint()
            fl = snap.flags[i]
            if fl & F_BAD:
                bad[i] = 1
            h = hashlib.blake2b(digest_size=8)
            h.update(own[i].to_bytes(8, "little"))
            for c in sorted(snap.kids[i], key=lambda c: names[c]):
                if bad[c]:
                    bad[i] = 1
                h.update(names[c].encode("utf-8", "surrogateescape"))
                if snap.flags[c] & F_DEV:
                    h.update(b"\2%d:%d" % (snap.t_size[c], snap.t_files[c]))
                else:
                    h.update(full[c].to_bytes(8, "little"))
            full[i] = _sig_int(h)
        groups: dict[tuple, list[int]] = {}
        for i in range(1, n):
            fl = snap.flags[i]
            if bad[i] or fl & F_DEV or snap.t_files[i] < 1 or snap.t_alloc[i] < self.DUPE_DIR_MIN:
                continue
            if fl & F_INPKG and not is_package_name(names[i]):
                continue
            groups.setdefault((full[i], snap.t_size[i], snap.t_files[i]), []).append(i)
        groups = {k: v for k, v in groups.items() if len(v) > 1}
        members = {i for v in groups.values() for i in v}
        kept = [v for v in groups.values() if not all(snap.parent[i] in members for i in v)]
        kept.sort(key=lambda v: -snap.t_alloc[v[0]] * (len(v) - 1))
        out = []
        for gi, ids in enumerate(kept[:300]):
            self.checkpoint()
            paths = [self.path_of(i) for i in ids]
            ok_paths, sampled = self._confirm(paths)
            if len(ok_paths) < 2:
                continue
            by_path = dict(zip(paths, ids))
            keep = min(ok_paths, key=_keep_key)
            dirs = [{"path": p, "allocated": int(snap.t_alloc[by_path[p]]), "keep": p == keep} for p in ok_paths]
            dirs.sort(key=lambda d: (not d["keep"], d["path"]))
            reclaim = sum(d["allocated"] for d in dirs if not d["keep"])
            i0 = ids[0]
            out.append({"id": f"d{gi}", "size": int(snap.t_size[i0]), "allocated": int(snap.t_alloc[i0]),
                        "files": int(snap.t_files[i0]), "reclaimable": reclaim, "confirmed": True,
                        "sampled": sampled, "dirs": dirs})
        out.sort(key=lambda g: -g["reclaimable"])
        return out

    def _confirm(self, paths):
        """Compare a sample of files (first+last 64 KB) between copies of a folder."""
        ref = paths[0]
        files = []
        try:
            for p, st in walk_files([ref], self):
                files.append((st.st_size, os.path.relpath(p, ref)))
                if len(files) >= 3000:
                    break
        except OSError:
            return [], 0
        if not files:
            return [], 0
        files.sort(reverse=True)
        pick = files[:6]
        rest = files[6:]
        if rest:
            step = max(1, len(rest) // 6)
            pick += rest[::step][:6]
        try:
            ref_h = {rel: (size, sample_hash(os.path.join(ref, rel), size)) for size, rel in pick}
        except OSError:
            return [], 0
        good = [ref]
        for other in paths[1:]:
            ok = True
            for rel, (size, hv) in ref_h.items():
                p = os.path.join(other, rel)
                try:
                    if os.path.getsize(p) != size or sample_hash(p, size) != hv:
                        ok = False
                        break
                except OSError:
                    ok = False
                    break
            if ok:
                good.append(other)
        return good, len(ref_h)

    # ------------------------------------------------------------ sparse candidates
    def _sample_sparse(self):
        out = []
        cands = sorted(self.sparse_cands, reverse=True)
        for size, _seq, path, alloc in cands:
            self.checkpoint()
            self.message = f"Checking {os.path.basename(path)}"
            try:
                frac = zero_fraction(path, size)
            except OSError:
                continue
            if frac < 0.2:
                continue
            est = int(frac * size)
            low = os.path.basename(path).lower()
            out.append({"path": path, "size": size, "allocated": alloc, "est_zero_bytes": est,
                        "est_reclaimable": min(est, alloc),
                        "vm_disk": low in VM_DISK_NAMES or os.path.splitext(low)[1] in VM_DISK_EXTS})
        self.message = ""
        out.sort(key=lambda f: -f["est_reclaimable"])
        return out

    # ------------------------------------------------------------ findings
    def _findings(self):
        snap = self._snapshot(force=True)
        total = int(snap.t_alloc[0]) if snap.n else 0
        cards = []
        if self.sparse_files:
            fs = self.sparse_files
            b = sum(f["est_reclaimable"] for f in fs)
            ex = os.path.basename(fs[0]["path"])
            vm = any(f["vm_disk"] for f in fs)
            cards.append({
                "id": "sparse", "bytes": b, "action": "sparse", "paths": [f["path"] for f in fs],
                "title": "Virtual disks full of empty space" if vm else "Big files that are mostly empty space",
                "detail": (f"{plural(len(fs), 'big file')} such as '{ex}' "
                           f"{'is' if len(fs) == 1 else 'are'} mostly empty space (zeros) that still takes room "
                           f"on the drive - typical for Docker or Colima disks ('datadisk') copied off a Mac. Making "
                           f"{'it' if len(fs) == 1 else 'them'} sparse again frees about {human(b)} without changing "
                           f"what's inside.")})
        if self.dupe_groups:
            gs = self.dupe_groups
            b = sum(g["reclaimable"] for g in gs)
            ex = [d["path"] for d in gs[0]["dirs"][:2]]
            paths = [d["path"] for g in gs for d in g["dirs"]][:500]
            cards.append({
                "id": "dupe_dirs", "bytes": b, "action": "dupes", "paths": paths,
                "title": "Folders copied more than once",
                "detail": (f"{plural(len(gs), 'folder')} {'has' if len(gs) == 1 else 'have'} an identical copy "
                           f"elsewhere - for example '{ex[0]}' and '{ex[1]}'. Removing the extra copies would free "
                           f"{human(b)}. The duplicate finder checks every byte before anything is moved to the "
                           f"Trash.")})
        twins = [v for v in self.variants if v["kind"] != "incomplete" and v["same_size"]]
        if twins:
            b = sum(v["size"] for v in twins)
            paths = []
            for v in twins:
                paths += [v["path"], v["original_path"]]
            if len(paths) > 4000:
                paths = [self.root]
            cards.append({
                "id": "variants", "bytes": b, "action": "dupes", "paths": paths,
                "title": "Files saved twice under a new name",
                "detail": (f"{plural(len(twins), 'file')} {'has' if len(twins) == 1 else 'have'} a name like "
                           f"'photo (1).jpg', 'report copy.pdf' or 'movie.mov.t5r-2' and the same size as the file "
                           f"next to {'it' if len(twins) == 1 else 'them'} - probably identical copies from copying "
                           f"or recovering twice ({human(b)}). The duplicate finder can confirm they match before "
                           f"anything is removed.")})
        pieces = [v for v in self.variants if v["kind"] == "incomplete"]
        if pieces:
            b = sum(v["size"] for v in pieces)
            alone = sum(1 for v in pieces if not v["original_exists"])
            cards.append({
                "id": "incomplete", "bytes": b, "action": "clean", "paths": [self.root],
                "title": "Unfinished copies",
                "detail": (f"{plural(len(pieces), 'file')} {'ends' if len(pieces) == 1 else 'end'} in .part, "
                           f".t5r-part or .INCOMPLETE - pieces left by copies or downloads that didn't finish "
                           f"({human(b)})." + (f" {alone} of them {'has' if alone == 1 else 'have'} no complete "
                                               f"version next to {'it' if alone == 1 else 'them'}, so keep those "
                                               f"unless you have the file elsewhere." if alone else ""))})
        if self.regen:
            ids = list(self.regen)
            b = sum(int(snap.t_alloc[i]) for i in ids if i < snap.n)
            kinds = sorted({self.d_name[i] for i in ids}, key=lambda k: -sum(
                int(snap.t_alloc[j]) for j in ids if self.d_name[j] == k and j < snap.n))[:3]
            if b >= MB:
                cards.append({
                    "id": "regen", "bytes": b, "action": "clean", "paths": [self.root],
                    "title": "Developer folders you can rebuild",
                    "detail": (f"{plural(len(ids), 'folder')} like {', '.join(kinds)} can be recreated with one "
                               f"command (for example 'npm install'). Together they take {human(b)}.")})
        if self.trash_dirs:
            b = sum(int(snap.t_alloc[i]) for i in self.trash_dirs if i < snap.n)
            if b >= MB:
                paths = [self.path_of(i) for i in self.trash_dirs]
                cards.append({
                    "id": "trash", "bytes": b, "action": "reveal", "paths": paths,
                    "title": "Old Trash folders",
                    "detail": (f"'{os.path.basename(paths[0])}'{' and similar folders' if len(paths) > 1 else ''} "
                               f"{'holds' if len(paths) == 1 else 'hold'} {human(b)} of files that were in a Trash "
                               f"or Recycle Bin. Look through them and remove what you don't need.")})
        top = self.largest_list(10)
        if top and total:
            b = sum(f["allocated"] for f in top)
            if b >= total * 0.1 and b >= 100 * MB:
                cards.append({
                    "id": "largest", "bytes": b, "action": "reveal", "paths": [f["path"] for f in top],
                    "title": "Your biggest files",
                    "detail": (f"The {len(top)} largest files take {human(b)} ({round(100 * b / total)}% of the "
                               f"space used here). The biggest is '{top[0]['name']}' ({human(top[0]['allocated'])}).")
                })
        cards.sort(key=lambda c: -c["bytes"])
        if self.n_unreadable:
            where = ("Give Retrova Full Disk Access in System Settings > Privacy & Security, then analyse again "
                     "for complete numbers." if IS_MAC else
                     "Running Retrova with admin rights may let it read them." if IS_WIN else
                     "They may belong to another user.")
            cards.append({"id": "unreadable", "bytes": 0, "action": "none", "paths": self.unreadable[:20],
                          "title": "Some folders couldn't be read",
                          "detail": (f"{plural(self.n_unreadable, 'folder')} couldn't be opened (for example "
                                     f"'{self.unreadable[0]}'), so the totals leave them out. {where}")})
        return cards

    # ------------------------------------------------------------ views
    def largest_list(self, limit=200):
        with self.lock:
            items = sorted(self.largest, reverse=True)[:limit]
        return [{"path": p, "name": os.path.basename(p), "size": size, "allocated": alloc,
                 "mtime": int(mt * 1000), "type": TYPE_LABEL[type_of(os.path.basename(p))]}
                for alloc, _s, p, size, mt in items]

    def result(self, q):
        view = q.get("view") or "summary"
        if view == "summary":
            return self._summary()
        if view == "tree":
            return self._tree(q.get("node") or self.root, as_int(q.get("limit"), 200, 1, 5000))
        if view == "largest":
            lim = as_int(q.get("limit"), 200, 1, self.LARGEST_N)
            items = self.largest_list(lim)
            return {"files": items, "total": len(items), "partial": self.live()}
        if view == "variants":
            return self._variants_view(q)
        if view in ("dupe_dirs", "sparse"):
            if self.live():
                raise ApiError("Still analysing - this list appears when the scan finishes.", 409)
            if view == "dupe_dirs":
                gs = self.dupe_groups or []
                return {"groups": gs, "reclaimable": sum(g["reclaimable"] for g in gs), "total": len(gs)}
            fs = self.sparse_files or []
            return {"files": fs, "reclaimable": sum(f["est_reclaimable"] for f in fs), "total": len(fs),
                    "estimate": True}
        raise ApiError("Unknown view.", 400)

    def _summary(self):
        snap = self._snapshot()
        n = snap.n
        root_children = sorted(snap.kids[0], key=lambda c: -snap.t_alloc[c]) if n else []
        biggest = [{"name": self.d_name[c], "path": self.path_of(c), "size": int(snap.t_alloc[c]),
                    "apparent": int(snap.t_size[c]), "files": int(snap.t_files[c])} for c in root_children[:20]]
        with self.lock:
            types = [{"id": t, "type": TYPE_LABEL.get(t, t), "bytes": b, "files": c} for t, (b, c) in self.types.items()]
        types.sort(key=lambda t: -t["bytes"])
        return {"root": self.root, "total": int(snap.t_alloc[0]) if n else 0,
                "apparent": int(snap.t_size[0]) if n else 0, "files": int(snap.t_files[0]) if n else 0,
                "dirs": max(0, n - 1), "biggest_dirs": biggest, "types": types,
                "findings": self.findings if self.findings is not None else [],
                "unreadable": self.n_unreadable, "skipped_mounts": list(self.skipped_mounts),
                "partial": self.live(), "state": self.state}

    def _tree(self, node, limit):
        snap = self._snapshot()
        node = clean_path(node, "folder")
        i = self.find(node, snap)
        if i is None or i >= snap.n:
            raise ApiError("That folder wasn't part of this analysis.", 404)
        path = self.path_of(i)
        children = []
        for c in snap.kids[i]:
            d = {"name": self.d_name[c], "path": self.path_of(c), "size": int(snap.t_alloc[c]),
                 "apparent": int(snap.t_size[c]), "files": int(snap.t_files[c]), "kind": "dir"}
            if c in self.regen:
                conf, what, regen = self.regen[c]
                d["regen"] = {"risk": conf, "what": what, "regenerate": regen}
            if snap.flags[c] & F_BAD:
                d["unreadable"] = True
            children.append(d)
        live_ok = True
        try:
            with os.scandir(util.long_path(path)) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            blocks = getattr(st, "st_blocks", None)
                            children.append({"name": e.name, "path": os.path.join(path, e.name),
                                             "size": blocks * 512 if blocks is not None else st.st_size,
                                             "apparent": st.st_size, "files": 1, "kind": "file",
                                             "mtime": int(st.st_mtime * 1000)})
                    except OSError:
                        pass
        except OSError:
            live_ok = False
        if not live_ok and self.d_files[i]:
            children.append({"name": "Files in this folder", "path": None, "size": int(self.d_alloc[i]),
                             "apparent": int(self.d_size[i]), "files": int(self.d_files[i]), "kind": "files"})
        children.sort(key=lambda d: -d["size"])
        if len(children) > limit:
            rest = children[limit:]
            children = children[:limit]
            children.append({"name": f"{len(rest):,} smaller items", "path": None, "kind": "more",
                             "size": sum(d["size"] for d in rest), "apparent": sum(d["apparent"] for d in rest),
                             "files": sum(d["files"] for d in rest)})
        parent = self.path_of(snap.parent[i]) if i > 0 else None
        return {"node": path, "parent": parent, "size": int(snap.t_alloc[i]), "apparent": int(snap.t_size[i]),
                "files": int(snap.t_files[i]), "dirs": int(snap.t_dirs[i]), "children": children,
                "partial": self.live()}

    def _variants_view(self, q):
        kind = q.get("kind")
        with self.lock:
            items = list(self.variants)
        if kind:
            items = [v for v in items if v["kind"] == kind]
        items.sort(key=lambda v: (-v["size"], v["path"]))
        off = as_int(q.get("offset"), 0, 0)
        lim = as_int(q.get("limit"), 500, 1, 5000)
        same = [v for v in items if v["same_size"] or v["kind"] == "incomplete"]
        return {"items": items[off:off + lim], "total": len(items), "found": self.variant_total,
                "bytes": sum(v["size"] for v in same), "kinds": _kind_counts(items), "partial": self.live()}


def _kind_counts(items):
    out = {}
    for v in items:
        out[v["kind"]] = out.get(v["kind"], 0) + 1
    return out


def _listdir(p):
    try:
        return os.listdir(util.long_path(p))
    except OSError:
        return []


def _keep_key(path: str):
    """Sort key: the copy to keep comes first (no copy-suffix, not in Trash/Downloads, shortest path)."""
    from .dupes import keep_rank
    return keep_rank(path, 0)


def zero_fraction(path: str, size: int, samples: int = 32) -> float:
    """Estimate the share of all-zero 64 KB blocks by reading `samples` evenly spread blocks."""
    nblocks = size // BLOCK
    if nblocks <= 0:
        return 0.0
    step = max(1, nblocks // samples)
    zeros = total = 0
    with open(util.long_path(path), "rb") as f:
        for k in range(0, nblocks, step):
            f.seek(k * BLOCK)
            b = f.read(BLOCK)
            if not b:
                break
            total += 1
            if b == ZERO_BLOCK[:len(b)]:
                zeros += 1
    return zeros / total if total else 0.0


@route("POST", "/api/analyse")
def start_analyse(q, body):
    path = clean_path(body.get("path"), "folder or drive")
    if not os.path.isdir(util.long_path(path)):
        if os.path.exists(path):
            raise ApiError("Choose a folder or a drive to analyse, not a single file.")
        raise ApiError("That folder isn't there - is the drive connected?", 404)
    try:
        os.listdir(util.long_path(path))
    except OSError as ex:
        raise ApiError(friendly_error(ex), 403)
    job = AnalyseJob({"path": path})
    JOBS.start(job)
    return job.status()

