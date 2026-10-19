"""/api/recover/* endpoints (the generalised T5 Rescue)."""
from __future__ import annotations

import os
import sys
import threading
import time

from retrova.jobs import JOBS
from retrova.server import ApiError, Reply, guess_type, route
from retrova.util import REAL_HOME, is_admin

from .common import check_out_root, expand_out, is_device_path, norm_src
from .drives import list_drives
from .fsview import FsError, open_view
from .partitions import FS_NAMES, SUPPORTED, UNSUPPORTED_WHY, best_part, describe_part, detect_fs, scan, \
    container_format
from .sessions import Session, add_session, close_session, get_session, source_exists
from .source import ReadOnlySource, can_open, device_size

NEEDS_ADMIN = ("Retrova needs administrator rights to read this drive directly. Use “Allow access” "
               "(you'll be asked for your password) and try again.")
TEXT_EXTS = (".json", ".js", ".ts", ".tsx", ".jsx", ".py", ".c", ".cpp", ".h", ".hpp", ".ino", ".kt", ".java",
             ".swift", ".go", ".rs", ".yml", ".yaml", ".toml", ".ini", ".md", ".log", ".sh", ".gcode", ".csv",
             ".xml", ".html", ".htm", ".css", ".txt", ".cfg", ".conf", ".rtf", ".tex", ".sql", ".bat", ".ps1")
PREVIEW_MAX = 16 * 1024 * 1024


def _session(sid):
    s = get_session(sid)
    if s is None:
        raise ApiError("This drive isn't open any more - please open it again.", 404)
    return s


def _int(v, default=None):
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(f"“{v}” isn't a number.")


def _float(v, default):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ApiError(f"“{v}” isn't a number.")


def _norm_source(source: str) -> str:
    source = str(source or "").strip()
    if not source:
        raise ApiError("Choose a drive or a disk image first.")
    if is_device_path(source):
        return source
    return expand_out(source)


def _scan(source):
    try:
        return scan(source)
    except PermissionError:
        raise ApiError(NEEDS_ADMIN, 403)
    except FileNotFoundError:
        raise ApiError(f"Can't find {source} - is the drive still connected?", 404)
    except OSError as ex:
        raise ApiError(f"Can't read {source}: {ex.strerror or ex}", 400)


def _part_public(p):
    return {k: p.get(k) for k in ("index", "offset", "size", "type_name", "name", "fs", "fs_name", "label",
                                  "supported", "why", "volumes", "encrypted", "damaged")}


# ====================================================================== sources
_SCAN_BUDGET = 25.0


def _scan_with_timeout(dev, timeout):
    box = {}

    def go():
        try:
            box["r"] = scan(dev)
        except Exception as ex:
            box["e"] = ex
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, "This drive is very slow to respond - it may be failing."
    if "e" in box:
        ex = box["e"]
        return None, NEEDS_ADMIN if isinstance(ex, PermissionError) else f"Can't read it: {ex}"
    return box["r"], ""


def _entry(source, offset, size, fs, label, disk, mount="", detail="", needs_admin=False, extra=None):
    supported = fs in SUPPORTED
    small = (size or 0) < 64 * 1024 * 1024 and fs in ("fat12", "fat16", "fat32")   # EFI-style system areas
    e = {"source": source, "offset": int(offset or 0), "size": size, "fs": fs, "fs_name": FS_NAMES.get(fs or "", "Unknown"),
         "label": label or "", "disk": disk.get("name") or disk.get("id"), "disk_id": disk.get("id"),
         "mount": mount or "", "needs_admin": bool(needs_admin), "supported": supported,
         "removable": bool(disk.get("removable")), "health": (disk.get("health") or {}).get("state", "unknown")}
    bits = []
    if mount:
        bits.append(f"Opened by your computer at {mount} - you can also copy files from it normally.")
    elif supported:
        bits.append("Your computer hasn't opened this one - Retrova can read it directly.")
    if detail:
        bits.append(detail)
    if not supported and fs:
        bits.append(UNSUPPORTED_WHY.get(fs, ""))
    e["detail"] = " ".join(b for b in bits if b)
    e["recommended"] = bool(supported and not small and (not mount or (extra or {}).get("damaged")))
    if extra:
        e.update(extra)
    return e


def list_sources():
    drives = list_drives()
    out, t_end = [], time.time() + _SCAN_BUDGET
    for d in drives.get("disks", []):
        dev = d.get("device")
        if not dev:
            continue
        ok, why = can_open(dev)
        parts_os = d.get("partitions") or []
        layout = None
        if ok:
            layout, err = _scan_with_timeout(dev, max(2.0, t_end - time.time()))
            if layout is None and err:
                out.append(_entry(dev, 0, d.get("size"), None, "", d, detail=err))
                continue
        if layout is not None and layout.get("parts"):
            by_off = {p.get("offset"): p for p in parts_os if p.get("offset") is not None}
            for p in layout["parts"]:
                if not p.get("fs"):
                    continue
                osp = by_off.get(p["offset"], {})
                extra = {"volumes": p.get("volumes"), "damaged": bool(p.get("damaged")),
                         "encrypted": p.get("encrypted"), "partition": p.get("index")}
                detail = "It looks damaged." if p.get("damaged") else ""
                out.append(_entry(dev, p["offset"], p.get("size"), p.get("fs"), p.get("label") or osp.get("label"),
                                  d, osp.get("mount", ""), detail, False, extra))
            continue
        # can't read the raw disk (no admin rights): describe what the OS knows
        for p in parts_os:
            fs = p.get("fs")
            if not fs and not p.get("size"):
                continue
            pdev = str(p.get("device") or dev)
            off = (p.get("offset") or 0) if pdev.startswith("\\\\.\\") else 0   # Windows: disk + offset
            out.append(_entry(pdev, off, p.get("size"), fs, p.get("label"), d, p.get("mount", ""),
                              needs_admin=not ok and why == "admin"))
        if not parts_os:
            out.append(_entry(dev, 0, d.get("size"), None, "", d, needs_admin=not ok and why == "admin"))
    start = os.environ.get("RETROVA_START_SOURCE")
    if start:
        out.insert(0, {"source": start, "offset": 0, "label": os.path.basename(start), "size": device_size(start),
                       "fs": None, "fs_name": "", "detail": "Opened from the command line.", "recommended": True,
                       "needs_admin": False, "supported": True, "start": True})
    return out


@route("GET", "/api/recover/sources")
def api_sources(q, body):
    return {"sources": list_sources(), "admin": is_admin()}


@route("GET", "/api/recover/scan")
def api_scan(q, body):
    """Extra: the partitions of a disk or image without opening one."""
    source = _norm_source(q.get("source"))
    if not source_exists(source):
        raise ApiError(f"Can't find {source}.", 404)
    lay = _scan(source)
    return {"source": source, "scheme": lay["scheme"], "size": lay["size"], "notes": lay["notes"],
            "partitions": [_part_public(p) for p in lay["parts"]]}


# ====================================================================== open / close
@route("POST", "/api/recover/open")
def api_open(q, body):
    source = _norm_source(body.get("source"))
    if not source_exists(source):
        raise ApiError(f"Can't find {source} - is the drive still connected?", 404)
    if os.path.isdir(source):
        raise ApiError("That's a folder. Choose a drive, or a disk image file (.img, .dmg, .iso, .bin…).")
    lay = _scan(source)
    offset = _int(body.get("offset"))
    volume = str(body.get("volume") or "") or None
    parts = lay["parts"]
    notes = list(lay.get("notes") or [])
    if offset is None:
        part = None
        if volume:      # e.g. "MacStorage": the APFS container that holds a volume with that name
            part = next((p for p in parts if p.get("supported") and volume in (p.get("volumes") or [])), None)
        part = part or best_part(parts)
        if part is None:
            fmt = container_format(source) if not is_device_path(source) else None
            if fmt:
                raise ApiError(fmt, 422)
            if parts:
                whys = [f"{describe_part(p)}: {p.get('why')}" for p in parts if p.get("why")]
                raise ApiError("Retrova can't read any partition on this drive. " + " ".join(whys[:4]) +
                               " Deep scan can still look for photos, videos and documents.", 422)
            raise ApiError("No partitions or filesystems were found on this drive - it may be blank, encrypted or "
                           "badly damaged. Deep scan can still look for photos, videos and documents.", 422)
        others = [p for p in parts if p is not part and p.get("supported")]
        if others:
            notes.append(f"This drive has {len(others) + 1} readable partitions; showing "
                         f"{describe_part(part)}. You can switch to another one.")
    else:
        part = next((p for p in parts if p.get("offset") == offset), None)
        if part is None:
            src = ReadOnlySource(source)
            try:
                det = detect_fs(src, offset)
            finally:
                src.close()
            if det is None:
                raise ApiError(f"No filesystem was recognised at byte {offset:,}.", 422)
            part = dict(det, offset=offset, index=None, type_name="", name="",
                        supported=det["fs"] in SUPPORTED, why=UNSUPPORTED_WHY.get(det["fs"]))
        if not part.get("supported"):
            raise ApiError(part.get("why") or "This partition can't be read.", 422)
    size = part.get("size") or None
    spec = {"source": source, "offset": int(part["offset"]), "size": size, "fs": part["fs"], "volume": volume}
    try:
        view = open_view(spec, cache_blocks=16384)
    except FsError as ex:
        raise ApiError(str(ex), 422)
    except PermissionError:
        raise ApiError(NEEDS_ADMIN, 403)
    spec["volume"] = view.volume or None
    notes += view.notes
    if part.get("damaged"):
        notes.append("Parts of this filesystem look damaged; some folders may be unreadable.")
    info = {"fs": view.fstype, "fs_name": view.fs_name, "label": view.label, "volumes": view.volumes,
            "volume": view.volume, "size": size, "source": source, "offset": spec["offset"], "notes": notes,
            "partitions": [_part_public(p) for p in parts], "is_image": not is_device_path(source),
            "read_only": True, "case_insensitive": view.case_insensitive,
            "can_list_deleted": bool(getattr(view, "supports_deleted", False)),
            "default_out": os.path.join(REAL_HOME, "Recovered")}
    s = Session(spec, view, info)
    add_session(s)
    return dict(info, sid=s.sid)


@route("POST", "/api/recover/close")
def api_close(q, body):
    return {"closed": close_session(body.get("sid"))}


@route("GET", "/api/recover/session")
def api_session(q, body):
    s = _session(q.get("sid"))
    return dict(s.info, sid=s.sid)


# ====================================================================== browse
@route("GET", "/api/recover/ls")
def api_ls(q, body):
    s = _session(q.get("sid"))
    try:
        return s.ls(q.get("path") or "/", deleted=q.get("deleted") in ("1", "true"))
    except FileNotFoundError as ex:
        raise ApiError(str(ex), 404)
    except NotADirectoryError as ex:
        raise ApiError(str(ex), 400)
    except Exception as ex:
        raise ApiError(f"This folder can't be read: {type(ex).__name__}: {ex}", 500)


class _PreviewStream:
    """Reads [start, start+length) of one file through the session's preview view, one
    locked read at a time, so a long video stream never blocks other previews for long."""

    def __init__(self, session, handle, start, length):
        self.s, self.handle, self.pos, self.left = session, handle, start, length
        self.fh = None

    def read(self, n):
        n = min(n, self.left)
        if n <= 0:
            return b""
        with self.s.preview_lock:
            if self.fh is None:
                self.fh = self.s.preview_view().open_handle(self.handle)
            self.fh.seek(self.pos)
            data = self.fh.read(n)
        self.pos += len(data)
        self.left -= len(data)
        return data

    def close(self):
        self.fh = None


def _parse_range(rng, size):
    """-> (start, end inclusive, partial) or raises ApiError(416)."""
    if not rng or not rng.startswith("bytes=") or size == 0:
        return 0, size - 1, False
    a, _, b = rng[6:].split(",")[0].strip().partition("-")
    try:
        if a:
            start = int(a)
            end = int(b) if b else size - 1
        else:
            start, end = max(0, size - int(b)), size - 1
    except ValueError:
        return 0, size - 1, False
    if start >= size or start < 0 or end < start:
        raise ApiError("Requested range not satisfiable", 416)
    return start, min(end, size - 1), True


@route("GET", "/api/recover/preview")
def api_preview(q, body):
    s = _session(q.get("sid"))
    path = norm_src(q.get("path") or "")
    with s.preview_lock:
        try:
            node = s.preview_view().resolve(path)
        except FileNotFoundError as ex:
            raise ApiError(str(ex), 404)
        except Exception as ex:
            raise ApiError(f"This file can't be read: {ex}", 500)
    if node.kind != "file":
        raise ApiError("Only files can be previewed.")
    size = node.size
    ctype = guess_type(path)
    if ctype.startswith("text/") or path.lower().endswith(TEXT_EXTS):
        ctype = "text/plain; charset=utf-8"
    try:
        start, end, partial = _parse_range(q.get("_range"), size)
    except ApiError:
        return Reply(b"", ctype, 416, {"Content-Range": f"bytes */{size}"})
    if partial:
        end = min(end, start + PREVIEW_MAX - 1)
    length = max(0, end - start + 1) if size else 0
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if not ctype.startswith("application/pdf"):
        headers["Content-Security-Policy"] = "sandbox"
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    name = path.rsplit("/", 1)[-1].replace('"', "")
    headers["Content-Disposition"] = f'inline; filename="{name.encode("ascii", "replace").decode()}"'
    return Reply(stream=_PreviewStream(s, node.handle, start, length), length=length, ctype=ctype,
                 code=206 if partial else 200, headers=headers)


@route("POST", "/api/recover/size")
def api_size(q, body):
    s = _session(body.get("sid"))
    if body.get("cancel"):
        s.sizes.cancel()
    pats = [str(p) for p in body.get("patterns") or []]
    out = {}
    for p in body.get("paths") or []:
        p = norm_src(p)
        k = s.sizes.request(p, pats)
        r = s.sizes.get(k) or {"state": "queued", "bytes": 0, "files": 0, "errors": 0}
        out[p] = r
    return {"sizes": out}


@route("GET", "/api/recover/free")
def api_free(q, body):
    """Extra: free space where the output would go (the folder need not exist yet)."""
    from retrova.util import free_bytes
    out = expand_out(q.get("out") or os.path.join(REAL_HOME, "Recovered"))
    return {"out": out, "free": free_bytes(out), "exists": os.path.isdir(out)}


# ====================================================================== jobs
def _paths(body, required=True):
    paths = [norm_src(p) for p in (body.get("paths") or []) if str(p).strip()]
    if required and not paths:
        raise ApiError("Select at least one folder or file first.")
    return paths


@route("POST", "/api/recover/regen")
def api_regen(q, body):
    from .regenjob import RegenJob
    s = _session(body.get("sid"))
    paths = _paths(body)
    return JOBS.start(RegenJob(s.spec, paths, params={"sid": s.sid, "paths": paths})).status()


def _disk_key(dev: str) -> str:
    """'/dev/rdisk4s2' -> 'disk4', '\\\\.\\PhysicalDrive1' -> 'pd1', '/dev/sdb1' -> 'sdb', '/dev/nvme0n1p2' -> 'nvme0n1'."""
    import re
    d = str(dev or "")
    m = re.match(r"^/dev/r?(disk\d+)", d)
    if m:
        return m.group(1)
    m = re.match(r"^\\\\\.\\PhysicalDrive(\d+)", d, re.I)
    if m:
        return "pd" + m.group(1)
    m = re.match(r"^/dev/((?:nvme|mmcblk)\d+(?:n\d+)?)", d)
    if m:
        return m.group(1)
    m = re.match(r"^/dev/([a-z]+)\d*$", d)
    return m.group(1) if m else d


def check_not_on_source_drive(out: str, source: str):
    """Refuse an output folder on a mounted volume of the drive being recovered."""
    if not is_device_path(source):
        return
    try:
        drives = list_drives()
    except Exception:
        return
    real = os.path.realpath(out)
    norm = (lambda p: p.lower()) if os.name == "nt" or sys.platform == "darwin" else (lambda p: p)
    best, best_key = "", None
    for d in drives.get("disks", []):
        for p in d.get("partitions", []):
            for m in [p.get("mount")] + list(p.get("mounts") or []):
                if not m:
                    continue
                mm = norm(m.rstrip("\\/") or m)
                if (norm(real) == mm or norm(real).startswith(mm.rstrip("\\/") + os.sep)) and len(mm) > len(best):
                    best, best_key = mm, _disk_key(d.get("device"))
    if best_key and best_key == _disk_key(source):
        raise ApiError("That folder is on the drive you are recovering from. Save to a different drive - "
                       "writing to it can overwrite the very files you want back.")


def _out_dir(body, source):
    out = expand_out(body.get("out") or os.path.join(REAL_HOME, "Recovered"))
    try:
        check_out_root(out, source)
    except ValueError as ex:
        raise ApiError(str(ex))
    check_not_on_source_drive(out, source)
    return out


@route("POST", "/api/recover/copy")
def api_copy(q, body):
    from .copyjob import CopyJob
    s = _session(body.get("sid"))
    paths = _paths(body)
    out = _out_dir(body, s.source)
    skipped = [dict(path=str(i.get("path", "")), what=str(i.get("what", "")), regen=str(i.get("regen", "")))
               for i in body.get("skipped_info") or [] if isinstance(i, dict)]
    reserve = max(0.5, _float(body.get("reserve_gb"), 15.0))
    try:
        job = CopyJob(s.spec, s.info.get("label") or os.path.basename(s.source), paths, out,
                      [str(p) for p in body.get("patterns") or []], reserve, _int(body.get("workers"), 4),
                      [str(x) for x in body.get("excluded") or []], skipped,
                      params={"sid": s.sid, "paths": paths, "out": out, "reserve_gb": reserve,
                              "source": s.source, "offset": s.spec["offset"], "volume": s.spec.get("volume")})
    except OSError as ex:
        raise ApiError(f"Can't create the output folder {out}: {ex.strerror or ex}")
    return JOBS.start(job).status()


@route("POST", "/api/recover/image")
def api_image(q, body):
    from .imagejob import ImageJob, PROGRESS_SUFFIX
    sid = body.get("sid")
    if sid:
        s = _session(sid)
        source, offset = s.source, s.spec["offset"]
        total = s.spec.get("size") or max(0, device_size(source) - offset)
        label = s.info.get("label") or os.path.basename(source)
    else:
        source = _norm_source(body.get("source"))
        if not source_exists(source):
            raise ApiError(f"Can't find {source} - is the drive still connected?", 404)
        offset = _int(body.get("offset"), 0)
        total = _int(body.get("size")) or max(0, device_size(source) - offset)
        label = os.path.basename(source.rstrip("/\\")) or source
    ok, why = can_open(source)
    if not ok:
        raise ApiError(NEEDS_ADMIN if why == "admin" else f"Can't read {source}: {why}", 403 if why == "admin" else 400)
    if not total:
        raise ApiError("Can't tell how big this drive is, so it can't be imaged.")
    out = expand_out(body.get("out") or "")
    if not out:
        raise ApiError("Choose where to save the image file.")
    if os.path.isdir(out) or str(body.get("out", "")).endswith(("/", "\\")):
        safe = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in label).strip() or "disk"
        out = os.path.join(out, f"{safe}-{time.strftime('%Y%m%d')}.img")
    try:
        check_out_root(os.path.dirname(out) or out, source)
    except ValueError as ex:
        raise ApiError(str(ex))
    if os.path.exists(out) and not os.path.exists(out + PROGRESS_SUFFIX):
        raise ApiError(f"{out} already exists. Choose another file name - nothing is overwritten.", 409)
    reserve = max(0.5, _float(body.get("reserve_gb"), 15.0))
    job = ImageJob(source, offset, total, out, reserve, label,
                   params={"source": source, "offset": offset, "size": total, "out": out, "reserve_gb": reserve,
                           "sid": sid})
    return JOBS.start(job).status()


@route("POST", "/api/recover/verify")
def api_verify(q, body):
    from .copyjob import CopyJob
    from .verifyjob import VerifyJob, previously_skipped
    fj = body.get("from_job")
    if fj:
        if fj is True or str(fj).lower() in ("true", "1", "latest"):
            cj = JOBS.latest("copy")
        else:
            try:
                cj = JOBS.get(str(fj))
            except KeyError:
                cj = None
        if not isinstance(cj, CopyJob):
            raise ApiError("No recovery has run since Retrova started - choose what to verify instead.")
        if cj.live():
            raise ApiError("Wait for the recovery to finish (or stop it) before verifying it.", 409)
        spec, label = cj.spec, cj.src_label
        paths, out = list(cj.paths), cj.out_root
        patterns, excl = list(cj.patterns), list(cj.excluded_paths)
        sid = None
    else:
        s = _session(body.get("sid"))
        spec, label, sid = s.spec, s.info.get("label") or os.path.basename(s.source), s.sid
        paths = _paths(body)
        out = _out_dir(body, s.source)
        patterns = [str(p) for p in body.get("patterns") or []]
        excl = [str(x) for x in body.get("excluded") or []]
    if not os.path.isdir(out):
        raise ApiError(f"The recovered folder {out} doesn't exist - choose the folder you recovered into.")
    excl += sorted(previously_skipped(out))
    mode = str(body.get("mode") or "full")
    if mode not in ("quick", "sample", "full"):
        raise ApiError("mode must be quick, sample or full")
    job = VerifyJob(spec, label, paths, out, patterns, excl, bool(body.get("show_hidden", True)), mode,
                    _int(body.get("workers"), 4), bool(body.get("resume", True)),
                    params={"sid": sid, "paths": paths, "out": out, "mode": mode, "from_job": fj or None})
    return JOBS.start(job).status()


@route("POST", "/api/recover/repair")
def api_repair(q, body):
    from .verifyjob import VerifyJob
    try:
        v = JOBS.get(str(body.get("job") or body.get("id") or ""))
    except KeyError:
        raise ApiError("Run Verify first and let it finish.", 404)
    if not isinstance(v, VerifyJob):
        raise ApiError("That isn't a Verify job.")
    if v.live():
        raise ApiError("Let Verify finish (or stop it) before repairing.", 409)
    tasks, carry = v.repair_plan()
    if not tasks:
        raise ApiError("Nothing to repair.")
    job = VerifyJob(v.spec, v.src_label, v.paths, v.out_root, v.patterns, v.excluded_paths, v.show_hidden,
                    "full", _int(body.get("workers"), v.nworkers), False, repair_tasks=tasks, carry=carry,
                    params={"repair_of": v.id, "files": len(tasks), "out": v.out_root})
    job.mappings, job.roots = list(v.mappings), list(v.roots)
    return JOBS.start(job).status()


@route("GET", "/api/recover/verify_csv")
def api_verify_csv(q, body):
    from .verifyjob import VerifyJob
    try:
        v = JOBS.get(q.get("id") or "")
    except KeyError:
        raise ApiError("Nothing verified yet.", 404)
    if not isinstance(v, VerifyJob):
        raise ApiError("That isn't a Verify job.")
    data = ("﻿" + v.csv(q.get("status") or "", q.get("q") or "")).encode("utf-8")
    return Reply(data, "text/csv; charset=utf-8",
                 headers={"Content-Disposition": f'attachment; filename="retrova-verify-{v.id}.csv"'})


# ====================================================================== carving
@route("GET", "/api/recover/carve_types")
def api_carve_types(q, body):
    from .carve_formats import TYPES
    return {"types": [{"type": t, "ext": v[0], "group": v[1], "name": v[2]} for t, v in TYPES.items()],
            "groups": ["photos", "videos", "music", "documents", "archives", "other"]}


@route("POST", "/api/recover/carve")
def api_carve(q, body):
    from .carve import CarveJob
    from .carve_formats import expand_types
    sid = body.get("sid")
    if sid and not body.get("source"):
        s = _session(sid)
        source = s.source
        base = s.spec["offset"]
        default_end = base + (s.spec.get("size") or max(0, device_size(source) - base))
    else:
        source = _norm_source(body.get("source"))
        if not source_exists(source):
            raise ApiError(f"Can't find {source} - is the drive still connected?", 404)
        base, default_end = 0, device_size(source)
    ok, why = can_open(source)
    if not ok:
        raise ApiError(NEEDS_ADMIN if why == "admin" else f"Can't read {source}: {why}", 403 if why == "admin" else 400)
    start = _int(body.get("start"), base)
    end = _int(body.get("end"), default_end) or default_end
    if not end or end <= start:
        raise ApiError("Nothing to scan: the start is past the end of the drive.")
    types = expand_types(body.get("types"))
    if not types:
        raise ApiError("Choose at least one kind of file to look for.")
    out = _out_dir(body, source)
    reserve = max(0.2, _float(body.get("reserve_gb"), 2.0))
    try:
        job = CarveJob(source, start, end, out, types, reserve, aligned=bool(body.get("aligned", False)),
                       params={"source": source, "start": start, "end": end, "out": out, "types": sorted(types),
                               "sid": sid})
    except OSError as ex:
        raise ApiError(f"Can't create the output folder {out}: {ex.strerror or ex}")
    return JOBS.start(job).status()
