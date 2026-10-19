"""Deleted files on FAT12/16/32 and exFAT.

FAT: deleting a file writes 0xE5 over the first letter of its folder entry and zeroes its chain in the
allocation table. The rest of the entry survives: long name pieces, first cluster, size, dates. The
data is read as one unbroken stretch from the first cluster, which is right unless the file was fragmented.

exFAT: deleting clears the "in use" bit of each entry in the file's entry set; the whole set (name,
first cluster, length, "no FAT chain" flag) survives. A file with that flag is contiguous, so it comes
back exactly; others are followed through the allocation table if it still has the chain, else read as
one stretch.
"""
from __future__ import annotations

import datetime
import struct

from .undelete import GOOD, HANDLE_TAG, INCOMPLETE, OVERWRITTEN, DeletedFile, any_bit_set, clean_name

OVERWRITTEN_NOTE = "Some of the space this file used has been taken by other files since."
FRAGMENT_NOTE = "Found as one unbroken stretch of the disk. If the file was split into pieces, the end may be wrong."
MAX_DELETED_DIR_CLUSTERS = 64


def _fat_time(date: int, tm: int = 0):
    from .fsview import _dt_ns
    try:
        return _dt_ns(datetime.datetime(1980 + (date >> 9), (date >> 5) & 15, date & 31, tm >> 11, (tm >> 5) & 63,
                                        (tm & 31) * 2))
    except ValueError:
        return None


def _lfn_checksum(short: bytes) -> int:
    s = 0
    for b in short:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def _lfn_text(pieces) -> str:
    """Long-name entries in the order they sit on disk (last piece first)."""
    chars = []
    for e in reversed(pieces):
        raw = e[1:11] + e[14:26] + e[28:32]
        for i in range(0, 26, 2):
            ch = raw[i:i + 2]
            if ch == b"\x00\x00" or ch == b"\xff\xff":
                break
            chars.append(ch)
        else:
            continue
        break
    return b"".join(chars).decode("utf-16-le", "replace")


def entry_name(e: bytes, pieces, deleted: bool):
    """-> (name, first letter lost?). Uses the long name when its checksum fits this entry."""
    short = bytearray(e[:11])
    sums = {p[13] for p in pieces}
    if pieces and len(sums) == 1:
        want = sums.pop()
        if short[0] == 0xE5 and deleted:
            for c in range(0x21, 0x100):          # the deleted letter is whichever one makes the checksum fit
                short[0] = c
                if _lfn_checksum(short) == want:
                    break
            else:
                short[0] = 0xE5
        if _lfn_checksum(short) == want:
            text = _lfn_text(pieces)
            if text:
                return text, False
    lost = short[0] == 0xE5
    if lost:
        short[0] = ord("_")
    elif short[0] == 0x05:
        short[0] = 0xE5
    base = bytes(short[:8]).rstrip(b" ").decode("cp437", "replace")
    ext = bytes(short[8:11]).rstrip(b" ").decode("cp437", "replace")
    if e[12] & 0x08:
        base = base.lower()
    if e[12] & 0x10:
        ext = ext.lower()
    return (f"{base}.{ext}" if ext else base), lost


# ====================================================================== FAT
class _Geometry:
    def __init__(self, view):
        boot = view.src.pread(0, 512)
        self.bps, self.spc, rsvd, nfats, self.root_ents, tot16, _, fat16 = struct.unpack_from("<HBHBHHBH", boot, 11)
        tot32, fat32, _, _, self.root_cluster = struct.unpack_from("<IIHHI", boot, 32)
        fat_sectors = fat16 or fat32
        root_sectors = -(-self.root_ents * 32 // self.bps)
        self.root_off = (rsvd + nfats * fat_sectors) * self.bps
        self.data_off = self.root_off + root_sectors * self.bps
        self.cs = self.bps * self.spc
        total = tot16 or tot32
        self.clusters = (total * self.bps - self.data_off) // self.cs
        self.is32 = not self.root_ents

    def offset(self, cluster):
        return self.data_off + (cluster - 2) * self.cs


def _fat_dir_data(view, g, first: int, deleted: bool, limit=1 << 20):
    """The bytes of a folder. A live one follows its chain; a deleted one is the clusters from its start."""
    src = view.src
    if first == 0:
        return src.pread(g.root_off, g.root_ents * 32)
    out = []
    if not deleted:
        for n, c in enumerate(view._chain(first)):
            if n >= limit or not 2 <= c < g.clusters + 2:
                break
            out.append(src.pread(g.offset(c), g.cs))
        return b"".join(out)
    for c in range(first, min(first + MAX_DELETED_DIR_CLUSTERS, g.clusters + 2)):
        if view._fat_get(c) != 0:
            break
        chunk = src.pread(g.offset(c), g.cs)
        out.append(chunk)
        if any(chunk[i] == 0 for i in range(0, len(chunk), 32)):
            break
    return b"".join(out)


def _dir_is_genuine(data: bytes, first: int) -> bool:
    """A deleted folder's clusters still start with its own '.' entry (not yet reused)."""
    if len(data) < 64 or data[:11] != b".          ":
        return False
    return ((struct.unpack_from("<H", data, 20)[0] << 16) | struct.unpack_from("<H", data, 26)[0]) == first


def scan_fat(view, notes: list):
    g = _Geometry(view)
    seen = set()
    stack = [(0 if not g.is32 else g.root_cluster, (), False)]
    unreadable = 0
    while stack:
        first, parts, parent_deleted = stack.pop()
        if first in seen:
            continue
        seen.add(first)
        try:
            data = _fat_dir_data(view, g, first, parent_deleted)
        except (ValueError, OSError):
            unreadable += 1
            continue
        if parent_deleted and not _dir_is_genuine(data, first):
            continue
        pieces = []
        for i in range(0, len(data) - 31, 32):
            e = data[i:i + 32]
            if e[0] == 0:
                break
            if e[11] & 0x3F == 0x0F:
                pieces.append(e)
                continue
            lfn, pieces = pieces, []
            if e[11] & 0x08 or e[0] == 0x2E:
                continue
            deleted = parent_deleted or e[0] == 0xE5
            name, lost = entry_name(e, lfn, deleted)
            cluster = (struct.unpack_from("<H", e, 20)[0] << 16 if g.is32 else 0) | struct.unpack_from("<H", e, 26)[0]
            size = struct.unpack_from("<I", e, 28)[0]
            if e[11] & 0x10:
                if cluster >= 2:
                    stack.append((cluster, parts + (clean_name(name),), deleted))
                continue
            if not deleted:
                continue
            f = _deleted_fat_file(view, g, e, parts, name, lost, cluster, size)
            if f is not None:
                yield f
    if unreadable:
        notes.append(f"{unreadable} folder{'s' if unreadable != 1 else ''} couldn't be searched for deleted files.")


def _deleted_fat_file(view, g, e, parts, name, lost, cluster, size):
    need = -(-size // g.cs)
    if size and not (2 <= cluster and cluster + need - 1 < g.clusters + 2):
        return None
    level, note = GOOD, ""
    if size:
        if any(view._fat_get(c) != 0 for c in range(cluster, cluster + need)):
            level, note = OVERWRITTEN, OVERWRITTEN_NOTE
        elif need > 1:
            level, note = INCOMPLETE, FRAGMENT_NOTE
    if lost:
        note = ("The first letter of the name was lost when it was deleted; it shows as _. " + note).strip()
    wrt_time, wrt_date, = struct.unpack_from("<HH", e, 22)
    acc_date, = struct.unpack_from("<H", e, 18)
    mtime = _fat_time(wrt_date, wrt_time) if wrt_date else None
    atime = _fat_time(acc_date) if acc_date else None
    return DeletedFile(parts, clean_name(name), size, mtime, atime, (HANDLE_TAG, cluster if size else 0, size),
                       level, note)


def open_deleted_fat(view, handle):
    from dissect.util.stream import RunlistStream

    from .fsview import EmptyStream
    _, cluster, size = handle
    if not size:
        return EmptyStream(b"")
    cs = view.fs.cluster_size
    need = -(-size // cs)
    if cluster < 2 or cluster - 2 + need > view._fat_len * 8 // view._bits:
        raise IOError("This file's data location is outside the disk")
    return RunlistStream(view.fs.data_stream, [(cluster - 2, need)], size, cs)


# ====================================================================== exFAT
class _ClusterBits:
    """The exFAT allocation bitmap: which clusters are in use now."""

    def __init__(self, fs):
        self.data = b""
        if fs.bitmap:
            first, nbytes = fs.bitmap
            nbytes = min(nbytes, 256 << 20)
            try:
                self.data = fs.open(first, nbytes, nbytes, False).read(nbytes)
            except Exception:
                self.data = fs.open(first, nbytes, nbytes, True).read(nbytes)

    def any_used(self, first: int, count: int) -> bool:
        return any_bit_set(self.data, first - 2, count)


def _exfat_sets(data: bytes):
    """-> (entry-set parts with the in-use bit put back, was it deleted)"""
    i, n = 0, len(data)
    while i + 32 <= n:
        t = data[i]
        if t == 0:
            return
        if t in (0x85, 0x05):
            sec = data[i + 1]
            parts = [bytearray(data[i + 32 * k:i + 32 * k + 32]) for k in range(sec + 1)] if 2 <= sec <= 18 else []
            if len(parts) == sec + 1 and all(len(p) == 32 for p in parts):
                deleted = t == 0x05
                if deleted:
                    for p in parts:
                        p[0] |= 0x80
                if parts[1][0] == 0xC0:
                    yield [bytes(p) for p in parts], deleted
                    i += 32 * (sec + 1)
                    continue
        i += 32


def _exfat_dir_data(fs, bits, d, deleted):
    from .exfat import ClusterStream, ExFatError
    if d is fs.root or not deleted:
        return fs.open_entry(d).read(1 << 28)
    size = d.size
    need = -(-size // fs.cluster_size)
    if need == 0 or d.first_cluster < 2 or d.first_cluster - 2 + need > fs.cluster_count or \
            bits.any_used(d.first_cluster, need):
        return b""
    if not d.nofat:
        try:
            return fs.open(d.first_cluster, size, size, False).read(size)
        except ExFatError:
            pass
    return ClusterStream(fs, [(d.first_cluster, need)], size, size).read(size)


def scan_exfat(view, notes: list):
    fs = view.fs
    bits = _ClusterBits(fs)
    stack = [(fs.root, (), False)]
    damaged = 0
    while stack:
        d, parts, parent_deleted = stack.pop()
        try:
            data = _exfat_dir_data(fs, bits, d, parent_deleted)
        except Exception:
            damaged += 1
            continue
        for set_parts, set_deleted in _exfat_sets(data):
            e = fs._parse_set(set_parts)
            if e is None or not e.checksum_ok:
                damaged += set_deleted
                continue
            deleted = set_deleted or parent_deleted
            name = clean_name(e.name)
            if e.is_dir:
                stack.append((e, parts + (name,), deleted))
            elif deleted:
                f = _deleted_exfat_file(fs, bits, e, parts, name)
                if f is not None:
                    yield f
    if damaged:
        notes.append(f"{damaged} deleted entr{'ies were' if damaged != 1 else 'y was'} left out because "
                     f"{'they were' if damaged != 1 else 'it was'} damaged.")


def _deleted_exfat_file(fs, bits, e, parts, name):
    from .exfat import ExFatError
    need = -(-e.size // fs.cluster_size)
    if e.size and not (2 <= e.first_cluster and e.first_cluster - 2 + need <= fs.cluster_count):
        return None
    level, note = GOOD, ""
    if e.size:
        runs = [(e.first_cluster, need)] if e.nofat else None
        if runs is None:
            try:
                runs = fs.runs(e.first_cluster, e.size, False)
            except ExFatError:
                pass
        guessed = runs is None
        if guessed:
            runs = [(e.first_cluster, need)]
        if any(bits.any_used(c, n) for c, n in runs):
            level, note = OVERWRITTEN, OVERWRITTEN_NOTE
        elif guessed and need > 1:
            level, note = INCOMPLETE, FRAGMENT_NOTE
    return DeletedFile(parts, name, e.size, e.mtime_ns, e.atime_ns,
                       (HANDLE_TAG, e.first_cluster if e.size else 0, e.size, e.valid_size, e.nofat), level, note)


def open_deleted_exfat(view, handle):
    from .exfat import ClusterStream, ExFatError
    from .fsview import EmptyStream
    _, first, size, valid, nofat = handle
    fs = view.fs
    if not size:
        return EmptyStream(b"")
    valid = min(valid, size)
    if not nofat:
        try:
            return ClusterStream(fs, fs.runs(first, size, False), size, valid)
        except ExFatError:
            pass
    need = -(-size // fs.cluster_size)
    if first < 2 or first - 2 + need > fs.cluster_count:
        raise IOError("This file's data location is outside the disk")
    return ClusterStream(fs, [(first, need)], size, valid)
