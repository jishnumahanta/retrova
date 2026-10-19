"""Small, lazy, read-only HFS+ / HFSX reader (Mac OS Extended, old Macs, Time Machine drives).

No maintained pure-Python HFS+ library exists (there is no dissect.hfs), so this reads the
volume header, the catalog / extents-overflow / attributes B-trees and the file forks
directly. It lists one folder at a time, never parses the whole catalog, and works around
damage instead of giving up: a bad folder or a lost extent only affects that item.

Covered: HFS+ (case-insensitive) and HFSX (case-sensitive), the old HFS wrapper around an
embedded HFS+ volume, extents-overflow, hard links (iNode files) and Time Machine's
directory hard links, symlinks, resource forks, and transparent decompression (decmpfs:
zlib, LZVN, LZFSE, LZBITMAP, inline or in the resource fork).

Names are stored decomposed (NFD) and may contain "/"; they're returned as the Mac's POSIX layer
shows them (":" for "/"), in composed (NFC) form. Not covered: replaying the journal, HFS-only (pre-1998)
volumes. Timestamps are seconds since 1904 (UTC).
"""
from __future__ import annotations

import bisect
import functools
import stat
import struct
import unicodedata
import zlib
from typing import NamedTuple

HFS_EPOCH_DELTA = 2082844800        # seconds from 1904-01-01 to 1970-01-01
ROOT_PARENT, ROOT_ID = 1, 2
CATALOG_ID, ATTRIBUTES_ID = 4, 8                          # ids of the volume's own B-tree files
DATA_FORK, RSRC_FORK = 0x00, 0xFF
# the Mac's two hidden folders that hold the real content of hard links
_JOURNAL_FILES = (".journal", ".journal_info_block")
_LINK_DIRS = {"file": "HFS+ Private Data", "dir": ".HFS+ Private Directory Data\r"}
DECMPFS_BLOCK = 0x10000
_NODE_CACHE = 512


class HfsError(Exception):
    pass


def _read_at(stream, off, n):
    stream.seek(off)
    return stream.read(n)


def _u16(b, o=0):
    return struct.unpack_from(">H", b, o)[0]


def _u32(b, o=0):
    return struct.unpack_from(">I", b, o)[0]


def _u64(b, o=0):
    return struct.unpack_from(">Q", b, o)[0]


def hfs_ns(secs: int) -> int | None:
    """HFS timestamp (seconds since 1904) -> ns since 1970, None when unset or before 1970."""
    if secs < HFS_EPOCH_DELTA:
        return None
    return (secs - HFS_EPOCH_DELTA) * 1_000_000_000


def posix_name(raw: bytes) -> str:
    """On-disk UTF-16BE name -> the name a Mac program sees: "/" shown as ":", composed form."""
    s = raw.decode("utf-16-be", "replace").translate(_SLASH)
    return unicodedata.normalize("NFC", s)


_SLASH = str.maketrans("/", ":")


@functools.lru_cache(maxsize=4096)
def _fold(raw: bytes) -> bytes:
    """Case-insensitive sort form of an on-disk name (NULs ignored, per-character lower case)."""
    out = []
    for ch in raw.decode("utf-16-be", "surrogatepass"):
        if ch != "\0":
            low = ch.lower()
            out.append(low if len(low) == 1 else ch)
    return "".join(out).encode("utf-16-be", "surrogatepass")


class Fork(NamedTuple):
    """HFSPlusForkData: logical size, blocks in use, and the first eight extents (start, count)."""
    size: int
    blocks: int
    extents: tuple


def parse_fork(b: bytes, o: int = 0) -> Fork:
    ext = tuple((_u32(b, o + 16 + 8 * i), _u32(b, o + 20 + 8 * i)) for i in range(8) if _u32(b, o + 20 + 8 * i))
    return Fork(_u64(b, o), _u32(b, o + 12), ext)


class ForkStream:
    """Read-only stream over a list of (start block, block count) runs; `covered` is how many
    bytes the runs actually reach (less than `size` when an extent record has been lost)."""

    def __init__(self, pread, runs, size, block_size):
        self._pread, self.block_size = pread, block_size
        self._runs = [(s, n) for s, n in runs if n]
        self.covered = sum(n for _, n in self._runs) * block_size
        self.size = int(size)
        self._starts, off = [], 0
        for _, n in self._runs:
            self._starts.append(off)
            off += n * block_size
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
        while n > 0:
            if self.pos >= self.covered:
                raise IOError(f"The file's data runs out at byte {self.covered:,} of {self.size:,}")
            i = bisect.bisect_right(self._starts, self.pos) - 1
            start, count = self._runs[i]
            into = self.pos - self._starts[i]
            take = min(n, count * self.block_size - into)
            data = self._pread(start * self.block_size + into, take)
            if len(data) < take:
                raise IOError(f"The drive stopped answering at byte {self.pos:,}")
            out.append(data)
            self.pos += take
            n -= take
        return b"".join(out)


class BTree:
    """One HFS+ B-tree. `keyfn(key bytes)` turns a record's key into a sortable tuple."""

    def __init__(self, stream, keyfn):
        self.stream, self.keyfn, self._cache = stream, keyfn, {}
        if stream.size < 512:
            raise HfsError("A B-tree file is missing")
        head = _read_at(stream, 0, 512)
        self.root, self.first_leaf = _u32(head, 16), _u32(head, 24)
        self.node_size, self.total_nodes = _u16(head, 32), _u32(head, 36)
        if self.node_size < 512 or self.node_size & (self.node_size - 1):
            raise HfsError("The B-tree has an invalid node size")

    def _node(self, n):
        node = self._cache.get(n)
        if node is None:
            if n >= self.total_nodes:
                raise HfsError(f"B-tree node {n} is outside the tree")
            self.stream.seek(n * self.node_size)
            node = self.stream.read(self.node_size)
            if len(self._cache) >= _NODE_CACHE:
                self._cache.clear()
            self._cache[n] = node
        return node

    def _records(self, node):
        """-> [(key bytes, data bytes)] of a node (a damaged offset table gives fewer records)."""
        count, size = _u16(node, 10), len(node)
        offs = [_u16(node, size - 2 * (i + 1)) for i in range(min(count, (size - 14) // 2) + 1)]
        out = []
        for i in range(len(offs) - 1):
            a, b = offs[i], offs[i + 1]
            if not (14 <= a < b <= size - 2 * (len(offs))) or a + 2 > b:
                break
            klen = _u16(node, a)
            d = (a + 2 + klen + 1) & ~1
            if d > b:
                break
            out.append((node[a + 2:a + 2 + klen], node[d:b]))
        return out

    def seek(self, target):
        """Yield (key bytes, data bytes) for every leaf record whose key is >= `target`, in order."""
        if not self.root:
            return
        n, depth = self.root, 0
        while True:
            node = self._node(n)
            kind = struct.unpack_from(">b", node, 8)[0]
            recs = self._records(node)
            keys = [self.keyfn(k) for k, _ in recs]
            if kind == -1:
                break
            if kind != 0 or not recs or depth > 16:
                raise HfsError("The B-tree is damaged")
            i = max(bisect.bisect_right(keys, target) - 1, 0)
            n, depth = _u32(recs[i][1]), depth + 1
        i, seen = bisect.bisect_left(keys, target), set()
        while True:
            yield from recs[i:]
            n = _u32(node, 0)                      # forward link to the next leaf
            if not n or n in seen:
                return
            seen.add(n)
            node = self._node(n)
            recs, i = self._records(node), 0


class DecmpfsStream:
    """The uncompressed bytes of a transparently compressed file (com.apple.decmpfs)."""

    def __init__(self, attr: bytes, rsrc):
        if len(attr) < 16 or attr[:4] != b"fpmc":
            raise HfsError("The compressed file's header isn't recognised")
        self.algo, self.size = struct.unpack_from("<IQ", attr, 4)
        if self.algo not in range(3, 15):
            raise HfsError(f"This file uses an unknown compression type ({self.algo})")
        self.pos, self._last = 0, (-1, b"")
        self._blocks = []                         # (offset, length) of each compressed block
        if self.algo & 1:                         # odd types keep the data in the attribute itself
            self._inline = attr[16:]
            return
        self._inline, self._rsrc = None, rsrc
        if rsrc is None or not rsrc.size:
            raise HfsError("The compressed file's resource fork is missing")
        if self.algo in (4, 10):                  # zlib / plain: a small resource-map header first
            data_off = _u32(_read_at(rsrc, 0, 16), 0)
            for i in range(struct.unpack_from("<I", _read_at(rsrc, data_off, 8), 4)[0]):
                off, ln = struct.unpack("<II", _read_at(rsrc, data_off + 8 + 8 * i, 8))
                self._blocks.append((data_off + 4 + off, ln))
        else:                                     # others: a table of block offsets from the start
            n = -(-self.size // DECMPFS_BLOCK) + 1
            offs = struct.unpack(f"<{n}I", _read_at(rsrc, 0, 4 * n))
            self._blocks = [(offs[i], offs[i + 1] - offs[i]) for i in range(n - 1)]

    def _block(self, i):
        if self._last[0] == i:
            return self._last[1]
        if self._inline is not None:
            chunk = self._inline
        else:
            if i >= len(self._blocks):
                raise IOError("The compressed data ends early")
            off, ln = self._blocks[i]
            chunk = _read_at(self._rsrc, off, ln)
        data = self._decode(chunk)
        self._last = (i, data)
        return data

    def _decode(self, chunk):
        if not chunk:
            return b""
        a, c0 = self.algo, chunk[0]
        try:
            if a in (3, 4):
                return zlib.decompress(chunk) if c0 == 0x78 else chunk[1:] if c0 & 0x0F == 0x0F else _bad(c0)
            if a in (9, 10):
                return chunk[1:]
            if a in (7, 8):
                from dissect.util.compression import lzvn
                return chunk[1:] if c0 == 0x06 else lzvn.decompress(chunk)
            if a in (11, 12):
                from dissect.util.compression import lzfse
                return chunk[1:] if c0 == 0xFF else lzfse.decompress(chunk)
            from dissect.util.compression import lzbitmap
            return chunk[1:] if c0 & 0x0F == 0x0F else lzbitmap.decompress(chunk)
        except (zlib.error, ValueError, EOFError, IndexError, struct.error) as ex:
            raise IOError(f"A compressed block can't be decoded ({ex})") from ex

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
        while n > 0:
            i, off = divmod(self.pos, DECMPFS_BLOCK)
            piece = self._block(i)[off:off + n]
            if not piece:
                raise IOError("The compressed data ends early")
            out.append(piece)
            self.pos += len(piece)
            n -= len(piece)
        return b"".join(out)


def _bad(c0):
    raise ValueError(f"unknown zlib block marker {c0:#x}")


class Entry:
    """One catalog item. kind: dir / file / link / other. `cid` is the id its children hang off
    (dirs) or whose forks hold its data (files); for hard links both come from the target."""
    __slots__ = ("name", "parent", "cid", "kind", "mode", "size", "mtime_ns", "atime_ns", "data", "rsrc",
                 "compressed", "error")

    def __init__(self, name, parent):
        self.name, self.parent = name, parent
        self.cid = self.mode = self.mtime_ns = self.atime_ns = self.data = self.rsrc = self.error = None
        self.kind, self.size, self.compressed = "other", 0, False


class HfsPlus:
    def __init__(self, src):
        self.src = src
        self.base = 0
        vh = self._find_header()
        self.hfsx = vh[:2] == b"HX"
        self.attributes = _u32(vh, 4)
        self.block_size, self.total_blocks = _u32(vh, 40), _u32(vh, 44)
        if self.block_size < 512 or self.block_size & (self.block_size - 1):
            raise HfsError("The volume header has an invalid block size")
        forks = {name: parse_fork(vh, 112 + 80 * i) for i, name in enumerate(("alloc", "extents", "catalog", "attrs"))}
        if not forks["catalog"].size:
            raise HfsError("The volume has no catalog")
        self.extents_tree = self.attrs_tree = None
        if forks["extents"].size:
            self.extents_tree = BTree(self._stream(forks["extents"].extents, forks["extents"].size), self._ext_key)
        cat = self._fork_stream(forks["catalog"], CATALOG_ID, DATA_FORK)
        self.catalog = BTree(cat, self._cat_key_cs if self.hfsx else self._cat_key)
        if forks["attrs"].size:
            self.attrs_tree = BTree(self._fork_stream(forks["attrs"], ATTRIBUTES_ID, DATA_FORK), self._attr_key)
        self._link_dirs: dict[str, int | None] = {}
        self._label = None

    # ---------------------------------------------------------------- volume
    def _find_header(self):
        for _ in range(2):
            vh = self.src.pread(self.base + 1024, 512)
            if vh[:2] in (b"H+", b"HX"):
                return vh
            if vh[:2] != b"BD" or vh[124:126] != b"H+":       # old HFS wrapper around an HFS+ volume
                break
            self.base += _u16(vh, 28) * 512 + _u16(vh, 126) * _u32(vh, 20)
        raise HfsError("This isn't an HFS+ volume")

    @property
    def label(self) -> str:
        if self._label is None:
            t = self._thread(ROOT_ID)
            self._label = t[1] if t else ""
        return self._label

    @property
    def unclean(self) -> bool:
        """Journaled and not shut down cleanly: the newest changes may be in the unreplayed journal."""
        return bool(self.attributes & 0x2000) and not self.attributes & 0x100

    def _pread(self, off, n):
        return self.src.pread(self.base + off, n)

    def _stream(self, runs, size):
        return ForkStream(self._pread, runs, size, self.block_size)

    # ---------------------------------------------------------------- keys
    @staticmethod
    def _cat_key(kb):
        return _u32(kb), _fold(kb[6:6 + 2 * _u16(kb, 4)])

    @staticmethod
    def _cat_key_cs(kb):
        return _u32(kb), kb[6:6 + 2 * _u16(kb, 4)]

    @staticmethod
    def _ext_key(kb):
        return _u32(kb, 2), kb[0], _u32(kb, 6)

    @staticmethod
    def _attr_key(kb):
        return _u32(kb, 2), kb[12:12 + 2 * _u16(kb, 10)], _u32(kb, 6)

    def _target(self, parent, name_raw=b""):
        return parent, (name_raw if self.hfsx else _fold(name_raw))

    # ---------------------------------------------------------------- forks
    def _runs(self, fork: Fork, cnid: int, ftype: int):
        """All extents of a fork, following the extents-overflow file when the first eight aren't enough."""
        runs, have = list(fork.extents), sum(n for _, n in fork.extents)
        while have < fork.blocks and self.extents_tree is not None and len(runs) < 100000:
            hit = None
            for kb, data in self.extents_tree.seek((cnid, ftype, have)):
                if self._ext_key(kb) == (cnid, ftype, have):
                    hit = [(_u32(data, 8 * i), _u32(data, 8 * i + 4)) for i in range(8) if _u32(data, 8 * i + 4)]
                break
            if not hit:
                break
            runs += hit
            have += sum(n for _, n in hit)
        return runs

    def _fork_stream(self, fork: Fork, cnid: int, ftype: int):
        return self._stream(self._runs(fork, cnid, ftype), fork.size)

    def open_fork(self, fork: Fork, cnid: int, rsrc: bool = False):
        """Raw bytes of a data or resource fork. Its `.covered` says how much of it can be located."""
        return self._fork_stream(fork, cnid, RSRC_FORK if rsrc else DATA_FORK)

    def open_file(self, cid: int, data: Fork, rsrc: Fork, compressed: bool = False):
        """File contents (decompressed if the file is compressed)."""
        if compressed:
            attr = self.get_xattr(cid, "com.apple.decmpfs")
            if attr is not None:
                return DecmpfsStream(attr, self.open_fork(rsrc, cid, True) if rsrc.size else None)
        return self.open_fork(data, cid)

    def open_entry(self, e: Entry):
        return self.open_file(e.cid, e.data, e.rsrc, e.compressed)

    def get_xattr(self, cnid: int, name: str) -> bytes | None:
        """An extended attribute's value (inline attributes, or ones kept in a fork), or None."""
        if self.attrs_tree is None:
            return None
        raw = name.encode("utf-16-be")
        for kb, data in self.attrs_tree.seek((cnid, raw, 0)):
            if self._attr_key(kb)[:2] != (cnid, raw):
                return None
            rtype = _u32(data, 0)
            if rtype == 0x10:
                return data[16:16 + _u32(data, 12)]
            if rtype == 0x20:
                fork = parse_fork(data, 8)
                return self._stream(fork.extents, fork.size).read()
            return None
        return None

    # ---------------------------------------------------------------- catalog
    def _thread(self, cnid):
        """-> (parent id, name) of a file or folder id, from its thread record."""
        for kb, data in self.catalog.seek((cnid, b"")):
            if _u32(kb) == cnid and _u16(kb, 4) == 0 and _u16(data) in (3, 4):
                return _u32(data, 4), posix_name(data[10:10 + 2 * _u16(data, 8)])
            break
        return None

    def path_of(self, cnid: int) -> str:
        """Full path of a file or folder id, walking the thread records up to the root."""
        parts = []
        for _ in range(256):
            if cnid == ROOT_ID:
                return "/" + "/".join(reversed(parts))
            t = self._thread(cnid)
            if t is None or t[0] == ROOT_PARENT:
                break
            parts.append(t[1])
            cnid = t[0]
        raise HfsError("The folder structure above this item is damaged")

    def _records(self, parent):
        for kb, data in self.catalog.seek(self._target(parent)):
            if _u32(kb) != parent:
                return
            if _u16(data) in (1, 2):
                yield kb[6:6 + 2 * _u16(kb, 4)], data

    def _link_dir(self, kind):
        """Id of one of the two hidden hard-link folders in the root, or None."""
        if kind not in self._link_dirs:
            found = None
            for raw, data in self._records(ROOT_ID):
                if _u16(data) == 1 and self._link_dir_kind(raw.decode("utf-16-be", "replace")) == kind:
                    found = _u32(data, 8)
                    break
            self._link_dirs[kind] = found
        return self._link_dirs[kind]

    @staticmethod
    def _is_system_item(name):
        """The hard-link folders and the journal files, which the Mac itself never shows."""
        return name in _JOURNAL_FILES or HfsPlus._link_dir_kind(name) is not None

    @staticmethod
    def _link_dir_kind(name):
        """"file" / "dir" when `name` is one of the hidden hard-link folders (the file one starts with
        four NUL or U+2400 characters), else None."""
        for kind, base in _LINK_DIRS.items():
            if name.endswith(base) and not name[:-len(base)].strip("\0\u2400"):
                if kind == "dir" or name != base:
                    return kind
        return None

    def lookup(self, parent: int, name_raw: bytes):
        """The record (name bytes, data) for one name in a folder, or None."""
        for kb, data in self.catalog.seek(self._target(parent, name_raw)):
            if _u32(kb) != parent:
                break
            if kb[6:6 + 2 * _u16(kb, 4)] == name_raw and _u16(data) in (1, 2):
                return name_raw, data
            break
        want = _fold(name_raw)
        for raw, data in self._records(parent):       # a name the sort order disagrees on: scan the folder
            if (raw == name_raw) if self.hfsx else (_fold(raw) == want):
                return raw, data
        return None

    def iterdir(self, parent: int):
        """Entries of a folder (by id). Hidden system folders are skipped."""
        for raw, data in self._records(parent):
            if parent == ROOT_ID and self._is_system_item(raw.decode("utf-16-be", "replace")):
                continue
            yield self._entry(parent, raw, data)

    def root(self) -> Entry:
        e = Entry("", ROOT_PARENT)
        e.kind, e.cid = "dir", ROOT_ID
        return e

    def _entry(self, parent, raw, rec) -> Entry:
        e = Entry(posix_name(raw), parent)
        try:
            self._fill(e, rec)
        except (HfsError, IOError, struct.error, ValueError) as ex:
            e.kind, e.error = "other", f"{type(ex).__name__}: {ex}"
        return e

    def _fill(self, e: Entry, rec: bytes):
        kind = _u16(rec)
        e.cid = _u32(rec, 8)
        e.mtime_ns, e.atime_ns = hfs_ns(_u32(rec, 16)), hfs_ns(_u32(rec, 24))
        e.mode = _u16(rec, 42)
        if kind == 1:
            e.kind = "dir"
            return
        ftype, creator = rec[48:52], rec[52:56]
        if ftype == b"hlnk" and creator == b"hfs+":          # a hard link to a file: the data lives in iNode<n>
            target = self._link_target("file", "iNode%d" % _u32(rec, 44))
            if target is None:
                raise HfsError("this hard link's original file is missing")
            e.mtime_ns, e.atime_ns = hfs_ns(_u32(target, 16)), hfs_ns(_u32(target, 24))
            e.cid, rec = _u32(target, 8), target
            e.mode = _u16(rec, 42)
        elif ftype == b"fdrp" and creator == b"MACS":        # Time Machine's hard link to a folder
            target = self._link_target("dir", "dir_%d" % _u32(rec, 44))
            if target is None or _u16(target) != 1:
                raise HfsError("this folder link's original folder is missing")
            e.kind, e.cid, e.mode = "dir", _u32(target, 8), _u16(target, 42)
            e.mtime_ns, e.atime_ns = hfs_ns(_u32(target, 16)), hfs_ns(_u32(target, 24))
            return
        e.data, e.rsrc = parse_fork(rec, 88), parse_fork(rec, 168)
        e.compressed = bool(rec[41] & 0x20)                   # UF_COMPRESSED
        fmt = stat.S_IFMT(e.mode)
        e.kind = ("link" if fmt == stat.S_IFLNK or (not fmt and rec[48:56] == b"slnkrhap") else
                  "file" if fmt in (0, stat.S_IFREG) else "other")
        e.size = e.data.size
        if e.compressed and e.kind == "file":
            attr = self.get_xattr(e.cid, "com.apple.decmpfs")
            if attr is not None and len(attr) >= 16 and attr[:4] == b"fpmc":
                e.size = struct.unpack_from("<Q", attr, 8)[0]
            else:
                e.compressed = False

    def _link_target(self, kind, name):
        folder = self._link_dir(kind)
        if folder is None:
            return None
        rec = self.lookup(folder, name.encode("utf-16-be"))
        return rec[1] if rec else None
