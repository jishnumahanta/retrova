"""Deleted files on NTFS: MFT records whose "in use" flag is clear.

The record still holds the name ($FILE_NAME), the folder it was in (parent reference), the dates and
where the data is (the $DATA runs, or the data itself when it is small). The records are parsed here
directly (a deleted record is not something a normal NTFS reader will hand out); dissect.ntfs only
provides the $MFT, $Bitmap and the volume streams.
"""
from __future__ import annotations

import io
import struct

from .undelete import GOOD, HANDLE_TAG, LOST, OVERWRITTEN, DeletedFile, any_bit_set, clean_name

_FILETIME_EPOCH = 116_444_736_000_000_000
_REF_MASK = 0xFFFFFFFFFFFF
FIRST_USER_RECORD = 24            # everything below is NTFS's own metadata
ATTR_STD, ATTR_LIST, ATTR_NAME, ATTR_DATA, ATTR_END = 0x10, 0x20, 0x30, 0x80, 0xFFFFFFFF
NAME_RANK = {1: 0, 3: 0, 0: 1, 2: 2}          # Win32 (and Win32+DOS) first, then POSIX, then the 8.3 name
BATCH = 4096                                   # records per read


class ResidentStream(io.BytesIO):
    """A small file stored inside its own record."""

    @property
    def size(self):
        return len(self.getvalue())


class Rec:
    """What a (possibly deleted) MFT record says."""
    __slots__ = ("seg", "seq", "in_use", "is_dir", "name", "parent", "mtime", "atime", "size", "resident",
                 "runs", "skip")


def _filetime_ns(ft: int):
    return (ft - _FILETIME_EPOCH) * 100 if ft > _FILETIME_EPOCH else None


def apply_fixups(rec: bytearray):
    """Put back the last two bytes of every 512-byte sector that NTFS swapped for a check value."""
    off, count = struct.unpack_from("<HH", rec, 4)
    if count < 2 or off + count * 2 > len(rec):
        return False
    usn = rec[off:off + 2]
    for i in range(1, count):
        end = i * 512
        if end > len(rec) or rec[end - 2:end] != usn:
            return False
        rec[end - 2:end] = rec[off + 2 * i:off + 2 * i + 2]
    return True


def parse_runs(raw: bytes):
    """Data runs -> [(lcn | None for a sparse run, clusters)]."""
    runs, pos, lcn = [], 0, 0
    while pos < len(raw) and raw[pos]:
        head = raw[pos]
        ln, off = head & 0x0F, head >> 4
        pos += 1
        count = int.from_bytes(raw[pos:pos + ln], "little")
        pos += ln
        if off:
            lcn += int.from_bytes(raw[pos:pos + off], "little", signed=True)
            runs.append((lcn, count))
        else:
            runs.append((None, count))
        pos += off
    return runs


def parse_record(raw: bytes, seg: int) -> Rec | None:
    """-> Rec, or None when this isn't a usable base record. rec.skip says why a deleted file can't be offered."""
    if raw[:4] != b"FILE":
        return None
    rec = bytearray(raw)
    if not apply_fixups(rec):
        return None
    flags = struct.unpack_from("<H", rec, 0x16)[0]
    if struct.unpack_from("<Q", rec, 0x20)[0]:
        return None                                  # an extension record of some other file
    r = Rec()
    r.seg, r.seq = seg, struct.unpack_from("<H", rec, 0x10)[0]
    r.in_use, r.is_dir = bool(flags & 1), bool(flags & 2)
    r.name = r.parent = r.mtime = r.atime = None
    r.size, r.resident, r.runs, r.skip = 0, None, None, ""
    best = 9
    have_data = False
    pos = struct.unpack_from("<H", rec, 0x14)[0]
    while pos + 16 <= len(rec):
        atype, alen = struct.unpack_from("<II", rec, pos)
        if atype == ATTR_END or alen < 16 or pos + alen > len(rec):
            break
        nonres, nlen, aflags = rec[pos + 8], rec[pos + 9], struct.unpack_from("<H", rec, pos + 12)[0]
        if atype == ATTR_LIST:
            r.skip = "its details are spread over several records"
        elif atype == ATTR_STD and not nonres:
            cs, = struct.unpack_from("<H", rec, pos + 20)
            body = pos + cs
            r.mtime, r.atime = (_filetime_ns(struct.unpack_from("<Q", rec, body + o)[0]) for o in (8, 24))
        elif atype == ATTR_NAME and not nonres:
            body = pos + struct.unpack_from("<H", rec, pos + 20)[0]
            ns = rec[body + 65]
            if NAME_RANK.get(ns, 9) < best:
                best = NAME_RANK[ns]
                r.parent = struct.unpack_from("<Q", rec, body)[0]
                r.name = bytes(rec[body + 66:body + 66 + rec[body + 64] * 2]).decode("utf-16-le", "replace")
        elif atype == ATTR_DATA and nlen == 0 and not have_data:
            have_data = True
            if aflags & 0x0001 or aflags & 0x4000:
                r.skip = "it was compressed or encrypted"
            elif not nonres:
                n, o = struct.unpack_from("<IH", rec, pos + 16)
                r.resident = bytes(rec[pos + o:pos + o + n])
                r.size = n
            else:
                if struct.unpack_from("<Q", rec, pos + 16)[0] != 0:
                    r.skip = "its data starts in another record"
                run_off = struct.unpack_from("<H", rec, pos + 32)[0]
                r.size = struct.unpack_from("<Q", rec, pos + 48)[0]
                r.runs = parse_runs(bytes(rec[pos + run_off:pos + alen]))
        pos += alen
    if not have_data and not r.is_dir:
        r.skip = r.skip or "it has no data"
    return r


class ClusterMap:
    """Which clusters are in use now ($Bitmap), read in pieces."""

    BLOCK = 1 << 20

    def __init__(self, fs):
        self.fh = fs.mft.get("$Bitmap").open()
        self.blocks: dict[int, bytes] = {}

    def _slice(self, a, b) -> bytes:
        out = []
        while a < b:
            blk = a // self.BLOCK
            data = self.blocks.get(blk)
            if data is None:
                if len(self.blocks) > 8:
                    self.blocks.clear()
                self.fh.seek(blk * self.BLOCK)
                data = self.blocks[blk] = self.fh.read(self.BLOCK)
            j = a - blk * self.BLOCK
            take = data[j:j + (b - a)]
            if not take:                       # past the end of the bitmap: nothing is in use there
                out.append(bytes(b - a))
                break
            out.append(take)
            a += len(take)
        return b"".join(out)

    def any_used(self, lcn, count) -> bool:
        a = lcn >> 3
        return any_bit_set(self._slice(a, ((lcn + count - 1) >> 3) + 1), lcn - a * 8, count)


def _runs_in_use(runs, cmap: ClusterMap, total_clusters: int) -> bool:
    for lcn, count in runs:
        if lcn is None:
            continue
        if lcn < 0 or lcn + count > total_clusters or cmap.any_used(lcn, count):
            return True
    return False


class _Paths:
    """Rebuilds a deleted file's folder from the parent references, checking sequence numbers."""

    def __init__(self, dirs):
        self.dirs = dirs                   # seg -> Rec (folders, live or deleted)
        self.cache: dict[int, tuple] = {}

    def parent_parts(self, ref: int) -> tuple:
        if ref in self.cache:
            return self.cache[ref]
        chain, cur, parts = [], ref, None
        while parts is None and len(chain) < 128:
            seg, seq = cur & _REF_MASK, cur >> 48
            if seg == 5:
                parts = ()
                break
            d = self.dirs.get(seg)
            # a folder deleted later has its sequence number raised by one; a different number on a live
            # folder means the slot now belongs to something else
            if d is None or not (d.seq == seq or (not d.in_use and d.seq == seq + 1)) or d.parent is None:
                parts = (LOST, f"Folder {seg}")
                break
            chain.append(clean_name(d.name))
            cur = d.parent
        else:
            parts = (LOST, "Folder (too deep)")
        result = parts + tuple(reversed(chain))
        self.cache[ref] = result
        return result


def scan(view, notes: list):
    fs = view.fs
    rs = fs._record_size
    mft = fs.mft.fh
    total_records = mft.size // rs
    total_clusters = view.src.size // fs.cluster_size
    cmap = ClusterMap(fs)
    dirs: dict[int, Rec] = {}
    files: list[tuple] = []
    skipped: dict[str, int] = {}
    for first in range(0, total_records, BATCH):
        n = min(BATCH, total_records - first)
        mft.seek(first * rs)
        block = mft.read(n * rs)
        for i in range(len(block) // rs):
            raw = block[i * rs:(i + 1) * rs]
            seg = first + i
            if raw[:4] != b"FILE":
                continue
            flags = raw[0x16]
            if flags & 1 and not flags & 2:
                continue                                  # a live file: nothing to find here
            r = parse_record(raw, seg)
            if r is None or r.name is None or r.parent is None:
                continue
            if r.is_dir:
                dirs[seg] = r
                continue
            if r.in_use or seg < FIRST_USER_RECORD:
                continue
            if r.skip:
                skipped[r.skip] = skipped.get(r.skip, 0) + 1
                continue
            level, note = GOOD, ""
            if r.runs and _runs_in_use(r.runs, cmap, total_clusters):
                level, note = OVERWRITTEN, "Some of the space this file used has been taken by other files since."
            files.append((seg, r.seq, r.name, r.parent, r.size, r.mtime, r.atime, level, note))
    paths = _Paths(dirs)
    for seg, seq, name, parent, size, mtime, atime, level, note in files:
        yield DeletedFile(paths.parent_parts(parent), clean_name(name), size, mtime, atime, (HANDLE_TAG, seg, seq),
                          level, note)
    for why, count in skipped.items():
        notes.append(f"{count} deleted file{'s' if count != 1 else ''} can't be listed because {why}.")


def open_deleted(view, handle):
    from dissect.util.stream import RunlistStream
    _, seg, seq = handle
    fs = view.fs
    rs = fs._record_size
    fs.mft.fh.seek(seg * rs)
    r = parse_record(fs.mft.fh.read(rs), seg)
    if r is None or r.seq != seq:
        raise IOError("This file's record on the disk has been reused by something else")
    if r.in_use:
        raise IOError("This file's record is in use again - it isn't deleted any more")
    if r.skip:
        raise IOError(f"This deleted file can't be read because {r.skip}")
    if r.resident is not None:
        return ResidentStream(r.resident)
    return RunlistStream(fs.fh, r.runs, r.size, fs.cluster_size)
