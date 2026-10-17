"""Small, lazy, read-only exFAT reader.

dissect.fat.exfat parses the whole directory tree when it is opened (minutes on a
large SD card, and one bad directory makes the whole volume unreadable) and reads
contiguous ("NoFatChain") multi-cluster directories incorrectly. This reader
parses one directory at a time, caches the FAT, honours ValidDataLength and
guards against looping cluster chains.
"""
from __future__ import annotations

import bisect
import calendar
import collections
import struct
import threading
import time


class ExFatError(Exception):
    pass


EOC = 0xFFFFFFFF
BAD = 0xFFFFFFF7
ATTR_DIR = 0x10


class ExEntry:
    __slots__ = ("name", "attrs", "first_cluster", "size", "valid_size", "nofat", "mtime_ns", "atime_ns",
                 "ctime_ns", "checksum_ok")

    @property
    def is_dir(self):
        return bool(self.attrs & ATTR_DIR)


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def _ts_ns(ts: int, inc10ms: int, utc: int) -> int | None:
    if not ts:
        return None
    sec = (ts & 0x1F) * 2
    minute = (ts >> 5) & 0x3F
    hour = (ts >> 11) & 0x1F
    day = (ts >> 16) & 0x1F
    month = (ts >> 21) & 0xF
    year = 1980 + ((ts >> 25) & 0x7F)
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and sec < 60):
        return None
    tup = (year, month, day, hour, minute, sec, 0, 0, -1)
    try:
        if utc & 0x80:
            off = utc & 0x7F
            if off >= 64:
                off -= 128
            t = calendar.timegm(tup) - off * 15 * 60
        else:
            t = time.mktime(tup)          # local time of the machine that wrote it
    except (OverflowError, ValueError):
        return None
    return int(t * 1_000_000_000) + min(inc10ms, 199) * 10_000_000


def _entry_set_checksum(raw: bytes) -> int:
    s = 0
    for i, byte in enumerate(raw):
        if i in (2, 3):
            continue
        s = (((s << 15) | (s >> 1)) + byte) & 0xFFFF
    return s


class ExFAT:
    def __init__(self, src):
        """src: object with pread(offset, length) (ReadOnlySource)."""
        self.src = src
        boot = src.pread(0, 512)
        if len(boot) < 512 or boot[3:11] != b"EXFAT   ":
            raise ExFatError("Not an exFAT volume")
        self.volume_length = _u64(boot, 72)
        fat_off, fat_len = _u32(boot, 80), _u32(boot, 84)
        heap_off, self.cluster_count = _u32(boot, 88), _u32(boot, 92)
        self.root_cluster = _u32(boot, 96)
        self.serial = _u32(boot, 100)
        bps, spc = boot[108], boot[109]
        if not 9 <= bps <= 12 or bps + spc > 25 or self.cluster_count == 0 or self.root_cluster < 2:
            raise ExFatError("The exFAT boot sector is damaged")
        self.sector_size = 1 << bps
        self.cluster_size = self.sector_size << spc
        self.fat_offset = fat_off * self.sector_size
        self.fat_bytes = fat_len * self.sector_size
        self.heap_offset = heap_off * self.sector_size
        self._fat_cache: collections.OrderedDict[int, bytes] = collections.OrderedDict()
        self._lock = threading.Lock()
        self.label = ""
        root = ExEntry()
        root.name, root.attrs, root.first_cluster = "", ATTR_DIR, self.root_cluster
        root.size = root.valid_size = 0          # unknown: follow the chain
        root.nofat = False
        root.mtime_ns = root.atime_ns = root.ctime_ns = None
        root.checksum_ok = True
        self.root = root
        for e in self._scan(root, want_label=True):
            pass

    # ------------------------------------------------------------------ FAT
    _FAT_BLOCK = 64 * 1024

    def fat_entry(self, cluster: int) -> int:
        off = cluster * 4
        if off + 4 > self.fat_bytes:
            raise ExFatError(f"Cluster {cluster} is outside the allocation table")
        blk = off // self._FAT_BLOCK
        with self._lock:
            data = self._fat_cache.get(blk)
            if data is not None:
                self._fat_cache.move_to_end(blk)
        if data is None:
            data = self.src.pread(self.fat_offset + blk * self._FAT_BLOCK,
                                  min(self._FAT_BLOCK, self.fat_bytes - blk * self._FAT_BLOCK))
            with self._lock:
                self._fat_cache[blk] = data
                if len(self._fat_cache) > 256:
                    self._fat_cache.popitem(last=False)
        i = off - blk * self._FAT_BLOCK
        if i + 4 > len(data):
            raise ExFatError("The allocation table can't be read")
        return _u32(data, i)

    def runs(self, first: int, nbytes: int | None, nofat: bool) -> list[tuple[int, int]]:
        """[(cluster, count)] covering nbytes (None = follow the whole chain)."""
        if first < 2:
            return []
        need = None if nbytes is None else -(-nbytes // self.cluster_size)
        if need == 0:
            return []
        if nofat:
            if first - 2 + need > self.cluster_count:
                raise ExFatError("The file runs past the end of the volume")
            return [(first, need)]
        runs: list[list[int]] = []
        cur, count, limit = first, 0, self.cluster_count + 2
        while True:
            if cur < 2 or cur - 2 >= self.cluster_count:
                raise ExFatError(f"The file's cluster chain is broken (cluster {cur})")
            if runs and runs[-1][0] + runs[-1][1] == cur:
                runs[-1][1] += 1
            else:
                runs.append([cur, 1])
            count += 1
            if need is not None and count >= need:
                break
            if count > limit:
                raise ExFatError("The file's cluster chain loops")
            nxt = self.fat_entry(cur)
            if nxt == EOC or nxt >= 0xFFFFFFF8:
                if need is not None:
                    raise ExFatError(f"The file's cluster chain ends early ({count} of {need} clusters)")
                break
            if nxt == BAD:
                raise ExFatError(f"The file's data is in a cluster marked bad ({cur})")
            if nxt < 2:
                raise ExFatError(f"The file's cluster chain is broken after cluster {cur}")
            cur = nxt
        return [(a, b) for a, b in runs]

    def partial_runs(self, first: int) -> list[tuple[int, int]]:
        """The runs of a damaged chain up to where it breaks."""
        runs: list[list[int]] = []
        cur, n = first, 0
        try:
            while 2 <= cur < self.cluster_count + 2 and n <= self.cluster_count:
                if runs and runs[-1][0] + runs[-1][1] == cur:
                    runs[-1][1] += 1
                else:
                    runs.append([cur, 1])
                n += 1
                nxt = self.fat_entry(cur)
                if nxt >= 0xFFFFFFF7 or nxt < 2:
                    break
                cur = nxt
        except ExFatError:
            pass
        return [(a, b) for a, b in runs]

    def open(self, first: int, size: int, valid: int | None = None, nofat: bool = False):
        return ClusterStream(self, self.runs(first, size, nofat) if size else [], size,
                             size if valid is None else min(valid, size))

    def open_entry(self, e: ExEntry):
        if e is self.root:
            r = self.runs(self.root_cluster, None, False)
            n = sum(c for _, c in r) * self.cluster_size
            return ClusterStream(self, r, n, n)
        return self.open(e.first_cluster, e.size, e.valid_size, e.nofat)

    # ------------------------------------------------------------------ directories
    def iterdir(self, d: ExEntry):
        if not d.is_dir:
            raise NotADirectoryError(d.name)
        return self._scan(d)

    def _scan(self, d: ExEntry, want_label=False):
        fh = self.open_entry(d)
        size = fh.size
        pos = 0
        buf = b""
        bpos = 0

        def entry_at(p):
            nonlocal buf, bpos
            if not (bpos <= p < bpos + len(buf)):
                bpos = p - (p % (64 * 1024))
                fh.seek(bpos)
                buf = fh.read(min(64 * 1024, size - bpos))
            i = p - bpos
            return buf[i:i + 32]

        while pos + 32 <= size:
            raw = entry_at(pos)
            if len(raw) < 32:
                break
            t = raw[0]
            if t == 0x00:
                break
            if t == 0x83 and want_label:
                n = min(raw[1], 11)
                self.label = raw[2:2 + n * 2].decode("utf-16-le", "replace")
            if t != 0x85:
                pos += 32
                continue
            sec = raw[1]
            if sec < 2 or sec > 18:
                pos += 32
                continue
            parts = [raw] + [entry_at(pos + 32 * i) for i in range(1, sec + 1)]
            pos += 32 * (sec + 1)
            if any(len(p) < 32 for p in parts) or parts[1][0] != 0xC0:
                continue
            e = self._parse_set(parts)
            if e is not None:
                yield e

    def _parse_set(self, parts):
        f, st = parts[0], parts[1]
        name_len = st[3]
        chars = []
        for p in parts[2:]:
            if p[0] != 0xC1:
                break
            chars.append(p[2:32])
        name = b"".join(chars).decode("utf-16-le", "replace")[:name_len]
        if not name:
            return None
        e = ExEntry()
        e.name = name
        e.attrs = _u16(f, 4)
        e.ctime_ns = _ts_ns(_u32(f, 8), f[20], f[22])
        e.mtime_ns = _ts_ns(_u32(f, 12), f[21], f[23])
        e.atime_ns = _ts_ns(_u32(f, 16), 0, f[24])
        e.nofat = bool(st[1] & 0x02)
        e.valid_size = _u64(st, 8)
        e.first_cluster = _u32(st, 20)
        e.size = _u64(st, 24)
        e.checksum_ok = _entry_set_checksum(b"".join(parts)) == _u16(f, 2)
        return e

    def get(self, path: str) -> ExEntry:
        node = self.root
        for part in [p for p in path.replace("\\", "/").split("/") if p and p != "."]:
            low = part.casefold()
            for e in self.iterdir(node):
                if e.name.casefold() == low:
                    node = e
                    break
            else:
                raise FileNotFoundError(f"File not found: {path}")
        return node


class ClusterStream:
    """Read-only stream over a list of cluster runs; bytes past ValidDataLength read as zeros."""

    def __init__(self, fs: ExFAT, runs, size: int, valid: int):
        self.fs, self.size, self.valid = fs, int(size), int(valid)
        self.runs = []
        lo = 0
        for c, n in runs:
            self.runs.append((lo, c, n))
            lo += n * fs.cluster_size
        self.starts = [r[0] for r in self.runs]
        self.pos = 0

    def seek(self, pos, whence=0):
        self.pos = pos if whence == 0 else self.pos + pos if whence == 1 else self.size + pos
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        out = []
        off, left = self.pos, n
        cs = self.fs.cluster_size
        while left > 0:
            if off >= self.valid:
                out.append(bytes(left))
                break
            run = None
            i = bisect.bisect_right(self.starts, off) - 1
            if i >= 0:
                lo, c, cnt = self.runs[i]
                if lo <= off < lo + cnt * cs:
                    run = (lo, c, cnt)
            if run is None:
                raise ExFatError(f"No data location for byte {off}")
            lo, c, cnt = run
            m = min(left, lo + cnt * cs - off, self.valid - off)
            phys = self.fs.heap_offset + (c - 2) * cs + (off - lo)
            data = self.fs.src.pread(phys, m)
            if len(data) < m:
                raise ExFatError(f"Short read at byte {off}")
            out.append(data)
            off += m
            left -= m
        data = b"".join(out)
        self.pos += len(data)
        return data

    def close(self):
        pass
