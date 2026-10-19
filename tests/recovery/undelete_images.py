"""Test images with deleted files, built without root, FUSE or mounting.

  FAT12/16/32  mkfs.vfat + mtools: mcopy files in, mdel / mdeltree some
  NTFS         mkntfs + ntfscp (files in the root); folders and deletions are made by editing the MFT
               records in the image bytes (flip the in-use flag, clear the $Bitmap bits of its clusters)
  exFAT        mkfs.exfat, then entry sets, FAT chains and bitmap bits are written by ExfatWriter below;
               "deleting" clears the in-use bit of the entry set (as real drivers do)
"""
from __future__ import annotations

import os
import random
import shutil
import struct
import subprocess
import tempfile

KB = 1024


def rand(seed, n):
    return random.Random(seed).randbytes(n)


# what each image holds. DELETED are removed, LIVE stay.
FAT_LIVE = {
    "docs/notes.txt": rand(20, 3000),
    "keep/live copy.txt": rand(21, 9000),
}
FAT_DELETED = {
    "victim.bin": rand(30, 5000),                              # first on the disk: the next file takes its place
    "docs/Quarterly Report 2024.txt": rand(31, 20000),
    "photos/Holiday Photo.jpg": rand(32, 60000),
    "SHORT.TXT": rand(33, 3000),                               # no long name
    "tiny.txt": b"ten bytes!",
    "olddir/inner/deep file.bin": rand(34, 7000),
}
FAT_EMPTY = "empty.txt"
FAT_DECOY = "decoy.txt"                                         # its folder slot is the one the next file takes
FAT_SQUATTER = ("squatter.bin", rand(35, 5000))


def _run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def build_fat(path, fat, size_kb):
    if os.path.exists(path):
        os.remove(path)
    _run(["mkfs.vfat", "-C", "-F", str(fat), "-n", "UNDEL", path, str(size_kb)])
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
    tmp = tempfile.mkdtemp()

    def put(rel, data):
        d = os.path.dirname(rel)
        parts = []
        for p in [x for x in d.split("/") if x]:
            parts.append(p)
            subprocess.run(["mmd", "-i", path, "::/" + "/".join(parts)], env=env, capture_output=True)
        src = os.path.join(tmp, "f")
        with open(src, "wb") as fh:
            fh.write(data)
        _run(["mcopy", "-m", "-i", path, src, "::/" + rel], env=env)

    try:
        put(FAT_DECOY, b"")
        put("victim.bin", FAT_DELETED["victim.bin"])
        for rel, data in {**FAT_LIVE, **{k: v for k, v in FAT_DELETED.items() if k != "victim.bin"}}.items():
            put(rel, data)
        put(FAT_EMPTY, b"")
        for rel in FAT_DELETED:
            if rel.startswith("olddir/"):
                continue
            _run(["mdel", "-i", path, "::/" + rel], env=env)
        for rel in (FAT_EMPTY, FAT_DECOY):
            _run(["mdel", "-i", path, "::/" + rel], env=env)
        _run(["mdeltree", "-i", path, "::/olddir"], env=env)
        put(*FAT_SQUATTER)
    finally:
        shutil.rmtree(tmp)
    _claim_clusters(path, fat, "/_ictim.bin", 1)
    return path


def _claim_clusters(path, fat, victim, count):
    """Mark the first `count` clusters of a deleted file as allocated, as if a new file had moved onto them
    (mtools always picks a different free cluster, so the test does it directly in the allocation table)."""
    from retrova.recovery.fsview import open_view
    with open_view({"source": path, "offset": 0}) as v:
        first = v.resolve("/Deleted files" + victim).handle[1]
    with open(path, "r+b") as fh:
        boot = fh.read(512)
        bps, _, rsvd, nfats = struct.unpack_from("<HBHB", boot, 11)
        fat_sectors = struct.unpack_from("<H", boot, 22)[0] or struct.unpack_from("<I", boot, 36)[0]
        for copy in range(nfats):
            base = rsvd * bps + copy * fat_sectors * bps
            for c in range(first, first + count):
                value = 0xFFF if fat == 12 else 0xFFFF if fat == 16 else 0x0FFFFFFF
                if fat == 12:
                    fh.seek(base + c + c // 2)
                    old = struct.unpack("<H", fh.read(2))[0]
                    new = (old & 0xF000 | value) if c % 2 == 0 else (old & 0x000F | value << 4)
                    fh.seek(base + c + c // 2)
                    fh.write(struct.pack("<H", new))
                else:
                    width = fat // 8
                    fh.seek(base + c * width)
                    fh.write(value.to_bytes(width, "little"))


# ====================================================================== NTFS
def _seal(rec: bytearray):
    """Swap the last two bytes of every sector for the update sequence number again."""
    off, count = struct.unpack_from("<HH", rec, 4)
    usn = rec[off:off + 2]
    for i in range(1, count):
        end = i * 512
        rec[off + 2 * i:off + 2 * i + 2] = rec[end - 2:end]
        rec[end - 2:end] = usn


class NtfsEditor:
    """Reads and patches MFT records in an NTFS image file."""

    def __init__(self, path):
        from retrova.recovery.undelete_ntfs import apply_fixups, parse_record, parse_runs
        self.path, self.apply, self.parse, self.runs = path, apply_fixups, parse_record, parse_runs
        self.fh = open(path, "r+b")
        boot = self._pread(0, 512)
        bps, spc = struct.unpack_from("<HB", boot, 11)
        self.cs = bps * spc
        self.mft_off = struct.unpack_from("<Q", boot, 48)[0] * self.cs
        cpr = struct.unpack_from("<b", boot, 64)[0]
        self.rs = 1 << -cpr if cpr < 0 else cpr * self.cs
        self.count = 256

    def close(self):
        self.fh.close()

    def _pread(self, off, n):
        self.fh.seek(off)
        return self.fh.read(n)

    def _pwrite(self, off, data):
        self.fh.seek(off)
        self.fh.write(data)

    def raw(self, seg):
        return bytearray(self._pread(self.mft_off + seg * self.rs, self.rs))

    def find(self, name):
        for seg in range(24, self.count):
            raw = self.raw(seg)
            r = self.parse(bytes(raw), seg)
            if r is not None and r.name == name:
                return seg
        raise KeyError(name)

    def edit(self, name_or_seg, fn):
        seg = name_or_seg if isinstance(name_or_seg, int) else self.find(name_or_seg)
        rec = self.raw(seg)
        assert self.apply(rec)
        fn(rec, seg)
        _seal(rec)
        self._pwrite(self.mft_off + seg * self.rs, bytes(rec))
        return seg

    def info(self, name):
        seg = self.find(name)
        return self.parse(bytes(self.raw(seg)), seg)

    def _attr(self, rec, atype):
        pos = struct.unpack_from("<H", rec, 0x14)[0]
        while pos + 8 <= len(rec):
            t, ln = struct.unpack_from("<II", rec, pos)
            if t == 0xFFFFFFFF or ln == 0:
                return None
            if t == atype:
                return pos
            pos += ln

    def _bitmap_set(self, runs, on):
        brec = self.parse(bytes(self.raw(6)), 6)
        pos = 0
        for lcn, count in brec.runs:
            span = count * self.cs
            for lc, n in runs:
                for c in range(lc, lc + n):
                    byte = c >> 3
                    if pos <= byte < pos + span:
                        off = lcn * self.cs + (byte - pos)
                        v = self._pread(off, 1)[0]
                        v = v | (1 << (c & 7)) if on else v & ~(1 << (c & 7))
                        self._pwrite(off, bytes([v]))
            pos += span

    # ---- operations
    def delete(self, name):
        seg = self.find(name)
        info = self.parse(bytes(self.raw(seg)), seg)

        def go(rec, _):
            rec[0x16] &= ~1
        self.edit(seg, go)
        self._bitmap_set([r for r in (info.runs or []) if r[0] is not None], False)

    def take_clusters(self, runs):
        """Mark clusters as in use again (another file moved in)."""
        self._bitmap_set([r for r in runs if r[0] is not None], True)

    def make_dir(self, name):
        self.edit(name, lambda rec, _: rec.__setitem__(0x16, rec[0x16] | 2))

    def set_parent(self, name, parent_seg, parent_seq=None):
        def go(rec, _):
            p = self._attr(rec, 0x30)
            body = p + struct.unpack_from("<H", rec, p + 20)[0]
            seq = parent_seq
            if seq is None:
                seq = struct.unpack_from("<H", self.raw(parent_seg), 0x10)[0]
            struct.pack_into("<Q", rec, body, parent_seg | (seq << 48))
        self.edit(name, go)

    def bump_seq(self, name):
        def go(rec, _):
            struct.pack_into("<H", rec, 0x10, struct.unpack_from("<H", rec, 0x10)[0] + 1)
        self.edit(name, go)

    def seg_of(self, name):
        return self.find(name)


NTFS_FILES = {
    "readme.txt": rand(40, 300),                 # small enough to live inside its record
    "movie.bin": rand(41, 300_000),
    "report final.docx": rand(42, 40_000),
    "live.txt": rand(43, 5000),
    "dirA": b"x",                                # becomes a folder
    "inner.txt": rand(44, 20_000),               # in dirA
    "orphan.txt": rand(45, 9000),                # parent is an ordinary file: can't be placed
    "reused.txt": rand(46, 9000),                # parent folder record was reused (sequence mismatch)
    "dirB": b"x",                                # a folder that is itself deleted
    "kid.txt": rand(47, 12_000),
    "taken.bin": rand(48, 60_000),               # its clusters are in use again
}
NTFS_DELETED = ["readme.txt", "movie.bin", "report final.docx", "inner.txt", "orphan.txt", "reused.txt", "kid.txt",
                "taken.bin", "dirB"]


def build_ntfs(path, size=24 * 1024 * 1024):
    if os.path.exists(path):
        os.remove(path)
    with open(path, "wb") as fh:
        fh.truncate(size)
    _run(["mkntfs", "-q", "-F", "-L", "UNDELNTFS", path])
    tmp = tempfile.mkdtemp()
    try:
        for name, data in NTFS_FILES.items():
            src = os.path.join(tmp, "f")
            with open(src, "wb") as fh:
                fh.write(data)
            _run(["ntfscp", path, src, name])
    finally:
        shutil.rmtree(tmp)
    ed = NtfsEditor(path)
    try:
        taken_runs = ed.info("taken.bin").runs
        ed.make_dir("dirA")
        ed.make_dir("dirB")
        ed.set_parent("inner.txt", ed.seg_of("dirA"))
        ed.set_parent("orphan.txt", ed.seg_of("live.txt"))
        ed.set_parent("reused.txt", ed.seg_of("dirA"), parent_seq=ed.info("dirA").seq + 7)
        ed.set_parent("kid.txt", ed.seg_of("dirB"))
        for name in NTFS_DELETED:
            ed.delete(name)
        ed.bump_seq("dirB")                      # a deleted folder has its sequence number raised
        ed.take_clusters(taken_runs)
    finally:
        ed.close()
    return path


# ====================================================================== exFAT
def _hash_name(name: str) -> int:
    h = 0
    for ch in name.upper().encode("utf-16-le"):
        h = (((h << 15) | (h >> 1)) + ch) & 0xFFFF
    return h


def _set_checksum(raw: bytes) -> int:
    s = 0
    for i, b in enumerate(raw):
        if i in (2, 3):
            continue
        s = (((s << 15) | (s >> 1)) + b) & 0xFFFF
    return s


class ExfatWriter:
    """Adds files and folders to a freshly formatted exFAT image by writing the structures directly."""

    def __init__(self, path):
        self.fh = open(path, "r+b")
        boot = self._pread(0, 512)
        self.fat_off = struct.unpack_from("<I", boot, 80)[0] * 512
        heap = struct.unpack_from("<I", boot, 88)[0] * 512
        self.heap = heap
        self.clusters = struct.unpack_from("<I", boot, 92)[0]
        root = struct.unpack_from("<I", boot, 96)[0]
        self.cs = 512 << boot[109]
        self.dirs = {"": root}
        self.dir_slot: dict[str, int] = {}
        self.entry_pos: dict[str, tuple[int, int]] = {}    # path -> (offset of entry set, entries)
        data = self._pread(self._coff(root), self.cs)
        for i in range(0, self.cs, 32):
            if data[i] == 0x81:
                self.bitmap = (struct.unpack_from("<I", data, i + 20)[0], struct.unpack_from("<Q", data, i + 24)[0])
        self.chains: dict[str, tuple[list[int], bool]] = {}
        self.next_free = 2
        while self._used(self.next_free):
            self.next_free += 1

    def close(self):
        self.fh.close()

    def _coff(self, c):
        return self.heap + (c - 2) * self.cs

    def _pread(self, off, n):
        self.fh.seek(off)
        return self.fh.read(n)

    def _pwrite(self, off, data):
        self.fh.seek(off)
        self.fh.write(data)

    def _bit(self, cluster, on):
        off = self._coff(self.bitmap[0]) + ((cluster - 2) >> 3)
        v = self._pread(off, 1)[0]
        v = v | (1 << ((cluster - 2) & 7)) if on else v & ~(1 << ((cluster - 2) & 7))
        self._pwrite(off, bytes([v]))

    def _used(self, cluster):
        return self._pread(self._coff(self.bitmap[0]) + ((cluster - 2) >> 3), 1)[0] >> ((cluster - 2) & 7) & 1

    def _fat_set(self, cluster, value):
        self._pwrite(self.fat_off + cluster * 4, struct.pack("<I", value))

    def alloc(self, n, skip=0):
        """n free clusters in a row (after leaving `skip` free ones as a hole)."""
        start = self.next_free + skip
        for c in range(start, start + n):
            self._bit(c, True)
        self.next_free = start + n
        return start

    def _stamp(self):
        return (2024 - 1980) << 25 | 6 << 21 | 15 << 16 | 12 << 11 | 30 << 5 | 10

    def _entry_set(self, name, attrs, first, size, nofat):
        utf = name.encode("utf-16-le")
        n_name = -(-len(name) // 15)
        f = bytearray(32)
        f[0] = 0x85
        f[1] = 1 + n_name
        struct.pack_into("<H", f, 4, attrs)
        for off in (8, 12, 16):
            struct.pack_into("<I", f, off, self._stamp())
        s = bytearray(32)
        s[0] = 0xC0
        s[1] = 0x01 | (0x02 if nofat else 0)
        s[3] = len(name)
        struct.pack_into("<H", s, 4, _hash_name(name))
        struct.pack_into("<Q", s, 8, size)
        struct.pack_into("<I", s, 20, first)
        struct.pack_into("<Q", s, 24, size)
        parts = [f, s]
        for i in range(n_name):
            e = bytearray(32)
            e[0] = 0xC1
            chunk = utf[i * 30:(i + 1) * 30]
            e[2:2 + len(chunk)] = chunk
            parts.append(e)
        struct.pack_into("<H", parts[0], 2, _set_checksum(b"".join(bytes(p) for p in parts)))
        return b"".join(bytes(p) for p in parts)

    def _append(self, folder, entry_set):
        cluster = self.dirs[folder]
        base = self._coff(cluster)
        used = self.dir_slot.get(folder)
        if used is None:
            data = self._pread(base, self.cs)
            used = next(i for i in range(0, self.cs, 32) if data[i] == 0)
        assert used + len(entry_set) <= self.cs, "test folder outgrew one cluster"
        self._pwrite(base + used, entry_set)
        self.dir_slot[folder] = used + len(entry_set)
        return base + used

    def mkdir(self, path):
        parent, _, name = path.rpartition("/")
        c = self.alloc(1)
        self._pwrite(self._coff(c), bytes(self.cs))
        self.dirs[path] = c
        self.chains[path] = ([c], True)
        entry = self._entry_set(name, 0x10, c, self.cs, True)
        self.entry_pos[path] = (self._append(parent, entry), len(entry) // 32)

    def add(self, path, data, fragments=None, nofat=True):
        """fragments: split the file into this many pieces with a gap between each (needs a FAT chain)."""
        parent, _, name = path.rpartition("/")
        need = -(-len(data) // self.cs) if data else 0
        pieces = []
        if fragments:
            per = -(-need // fragments)
            left = need
            while left > 0:
                n = min(per, left)
                pieces.append((self.alloc(n, skip=1), n))
                left -= n
            nofat = False
        elif need:
            pieces.append((self.alloc(need), need))
        pos = 0
        chain = []
        for first, n in pieces:
            for k in range(n):
                chain.append(first + k)
            chunk = data[pos:pos + n * self.cs]
            self._pwrite(self._coff(first), chunk)
            pos += n * self.cs
        if not nofat:
            for a, b in zip(chain, chain[1:] + [0xFFFFFFFF]):
                self._fat_set(a, b)
        entry = self._entry_set(name, 0x20, chain[0] if chain else 0, len(data), nofat)
        self.entry_pos[path] = (self._append(parent, entry), len(entry) // 32)
        self.chains[path] = (chain, nofat)

    def delete(self, path, keep_fat=False):
        off, n = self.entry_pos[path]
        for k in range(n):
            b = self._pread(off + 32 * k, 1)[0]
            self._pwrite(off + 32 * k, bytes([b & 0x7F]))
        chain, nofat = self.chains.get(path, ([], True))
        for c in chain:
            self._bit(c, False)
            if not nofat and not keep_fat:
                self._fat_set(c, 0)


EXFAT_LIVE = {"docs/live.txt": rand(50, 4000), "movie-live.bin": rand(51, 70_000)}
EXFAT_DELETED = {
    "Holiday Video.bin": rand(52, 150_000),               # contiguous (NoFatChain): comes back exactly
    "docs/Meeting notes ✓.txt": rand(53, 12_000),
    "olddir/inner/deep file.bin": rand(54, 30_000),
    "tiny.txt": b"ten bytes!",
}
EXFAT_FRAG_KEPT = ("frag-chain-kept.bin", rand(55, 90_000))      # fragmented, its FAT chain still there
EXFAT_FRAG_LOST = ("frag-chain-lost.bin", rand(56, 90_000))      # fragmented, chain zeroed: can't be rebuilt
EXFAT_TAKEN = ("taken.bin", rand(57, 20_000))                    # another file moved onto its clusters
EXFAT_EMPTY = "empty.txt"


def build_exfat(path, size=24 * 1024 * 1024):
    if os.path.exists(path):
        os.remove(path)
    with open(path, "wb") as fh:
        fh.truncate(size)
    _run(["mkfs.exfat", "-L", "UNDELEX", "-c", "4096", path])
    w = ExfatWriter(path)
    try:
        for d in ("docs", "olddir", "olddir/inner"):
            w.mkdir(d)
        for rel, data in EXFAT_LIVE.items():
            w.add(rel, data)
        for rel, data in EXFAT_DELETED.items():
            w.add(rel, data)
        w.add(EXFAT_FRAG_KEPT[0], EXFAT_FRAG_KEPT[1], fragments=3)
        w.add(EXFAT_FRAG_LOST[0], EXFAT_FRAG_LOST[1], fragments=3)
        w.add(*EXFAT_TAKEN)
        w.add(EXFAT_EMPTY, b"")
        for rel in EXFAT_DELETED:
            w.delete(rel)
        w.delete(EXFAT_FRAG_KEPT[0], keep_fat=True)
        w.delete(EXFAT_FRAG_LOST[0])
        w.delete(EXFAT_TAKEN[0])
        w.delete(EXFAT_EMPTY)
        w.delete("olddir/inner")
        w.delete("olddir")
        taken_chain, _ = w.chains[EXFAT_TAKEN[0]]
        for c in taken_chain[:2]:
            w._bit(c, True)                     # a newer file now sits on the start of it
    finally:
        w.close()
    return path
