"""One read-only browsing/reading interface over every supported filesystem.

    view = open_view({"source": "/dev/rdisk4s2", "offset": 0, "fs": "apfs", "volume": None})
    node = view.resolve("/Users/me")          # Node(name, kind, size, mtime_ns, handle, ...)
    for child in view.iterdir(node): ...
    fh = view.open_handle(child.handle)       # seek()/read() stream of the file's data
    view.readlink(link.handle)

Node.handle is a small picklable value (ints/str/tuples) that lets a worker
*process* re-open the filesystem from the same spec and find the file again:
APFS (oid, sibling_id), NTFS MFT segment, FAT/exFAT (first cluster, size, ...),
ext/XFS inode number, Btrfs (subvolume id, inode number).

Each view is single-threaded: callers either own it or hold a lock around it.
Optional dissect modules are imported lazily, so a missing one only disables that
filesystem.
"""
from __future__ import annotations

import io
import stat
import time

from .partitions import FS_NAMES, SUPPORTED, UNSUPPORTED_WHY, detect_fs
from .source import ReadOnlySource


class FsError(Exception):
    """A plain-English problem opening or reading a filesystem."""


class Node:
    __slots__ = ("name", "kind", "size", "mtime_ns", "atime_ns", "mode", "handle", "obj", "error", "rec")

    def __init__(self, name, kind, size=0, mtime_ns=None, atime_ns=None, mode=None, handle=None, obj=None,
                 error=None, rec=None):
        self.name, self.kind, self.size = name, kind, int(size or 0)
        self.mtime_ns, self.atime_ns, self.mode = mtime_ns, atime_ns, mode
        self.handle, self.obj, self.error = handle, obj, error
        self.rec = rec          # deleted files only: (how likely the data is intact, a note)

    def __repr__(self):
        return f"<Node {self.name!r} {self.kind} {self.size}>"


def _kind_from_mode(mode) -> str:
    if mode is None:
        return "other"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "link"
    return "other"


def _dt_ns(dt) -> int | None:
    """datetime -> ns since epoch (naive datetimes are local time, as on FAT)."""
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            return int(time.mktime(dt.timetuple()) * 1_000_000_000) + dt.microsecond * 1000
        return int(dt.timestamp() * 1_000_000_000)
    except (OverflowError, ValueError, OSError):
        return None


class EmptyStream(io.BytesIO):
    size = 0


class PartialStream:
    """A file of which only the first `avail` bytes can be located (broken allocation chain):
    reads inside that part work, reading past it raises an OSError with the reason, so a
    copy keeps the readable part as .INCOMPLETE and Verify marks only the rest unreadable."""

    def __init__(self, inner, avail, size, problem):
        self.inner, self.avail, self.size, self.problem = inner, int(avail), int(size), problem
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
        if n == 0:
            return b""
        if self.pos >= self.avail:
            raise IOError(self.problem)
        self.inner.seek(self.pos)
        data = self.inner.read(min(n, self.avail - self.pos))
        self.pos += len(data)
        return data


class FSView:
    fstype = ""

    def __init__(self, spec: dict):
        from .apfs_engine import patch_dissect
        patch_dissect()          # cstruct's expression evaluator isn't thread-safe: serialised once per process
        self.spec = dict(spec)
        self.offset = int(spec.get("offset") or 0)
        # not limited to the partition size: a damaged table may understate it, and the
        # filesystem knows its own extent
        self.src = ReadOnlySource(spec["source"], self.offset)
        self.label = ""
        self.volumes: list[str] = []
        self.volume = ""
        self.notes: list[str] = []
        self.case_insensitive = False

    @property
    def fs_name(self):
        return FS_NAMES.get(self.fstype, self.fstype)

    # interface ---------------------------------------------------------
    def root(self) -> Node:
        raise NotImplementedError

    def resolve(self, path: str) -> Node:
        """Default: walk iterdir() name by name."""
        node = self.root()
        for part in [p for p in path.split("/") if p and p != "."]:
            low = part.casefold() if self.case_insensitive else part
            for c in self.iterdir(node):
                if (c.name.casefold() if self.case_insensitive else c.name) == low:
                    node = c
                    break
            else:
                raise FileNotFoundError(f"“{part}” isn't there any more.")
            if node.kind == "dir":
                continue
        return node

    def iterdir(self, node: Node):
        raise NotImplementedError

    def open_handle(self, handle):
        raise NotImplementedError

    def readlink(self, handle) -> str:
        raise FsError("Links aren't supported on this filesystem.")

    def scan_deleted(self, notes: list):
        """Deleted files found in the filesystem's own records (see undelete.py)."""
        raise NotImplementedError

    def open_deleted(self, handle):
        raise NotImplementedError

    def close(self):
        self.src.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ====================================================================== APFS
class ApfsView(FSView):
    fstype = "apfs"

    def __init__(self, spec, cache_blocks=8192):
        super().__init__(spec)
        from .apfs_engine import ApfsContainer, pick_volume
        try:
            self.cont = ApfsContainer(self.src, cache_blocks)
            vols = self.cont.volumes
        except Exception as ex:
            self.src.close()
            raise FsError(f"The APFS container can't be read ({type(ex).__name__}: {ex}).") from ex
        if not vols:
            self.src.close()
            raise FsError("This APFS container has no volumes.")
        try:
            self.v = pick_volume(vols, spec.get("volume"))
        except LookupError as ex:
            self.src.close()
            raise FsError(str(ex)) from ex
        self.volumes = [v.name for v in vols]
        self.volume = self.label = self.v.name
        self.case_insensitive = bool(getattr(self.v, "is_case_insensitive", False))
        try:
            enc = self.v.is_encrypted
        except Exception:
            enc = False
        if enc:
            self.notes.append(f"The volume “{self.v.name}” is encrypted (FileVault). Retrova can't read encrypted "
                              f"APFS volumes yet, so its files will show as unreadable.")
        self.block_size = self.cont.c.block_size
        self.checkpoint = getattr(self.cont.c.sb, "xid", None)

    def _node(self, name, ino, kind=None, oid=None, sib=None):
        from .apfs_engine import kind_of
        try:
            k = kind or kind_of(ino)
            raw = ino.inode
            size = ino.size if k == "file" else 0
            return Node(name, k, size, raw.mod_time, raw.access_time, ino.mode & 0o7777,
                        (int(ino.oid), None if ino.sibling_id is None else int(ino.sibling_id)), ino)
        except Exception as ex:
            return Node(name, kind or "other", 0, handle=(oid, sib) if oid is not None else None, obj=ino,
                        error=f"{type(ex).__name__}: {ex}")

    def root(self):
        return self._node("", self.v.root, "dir")

    def resolve(self, path):
        from dissect.apfs.exception import FileNotFoundError as AFNF
        p = "/" + path.strip("/")
        if p == "/":
            return self.root()
        try:
            ino = self.v.get(p)
        except (AFNF, KeyError) as ex:
            raise FileNotFoundError(f"{p} isn't there any more.") from ex
        return self._node(p.rsplit("/", 1)[-1], ino)

    def iterdir(self, node):
        from .apfs_engine import dirent_kind
        for e in node.obj.iterdir():
            try:
                k = dirent_kind(e)
                ino = e.inode
            except Exception as ex:
                yield Node(getattr(e, "name", "?"), "other", error=f"{type(ex).__name__}: {ex}")
                continue
            yield self._node(e.name, ino, k, e.file_id, e.sibling_id)

    def open_handle(self, handle):
        oid, sib = handle
        return self.v.inode(oid, sib).open()

    def readlink(self, handle):
        oid, sib = handle
        return self.v.inode(oid, sib).readlink()


# ====================================================================== NTFS
class NtfsView(FSView):
    fstype = "ntfs"

    def __init__(self, spec, **_):
        super().__init__(spec)
        from dissect.ntfs import NTFS
        try:
            self.fs = NTFS(self.src)
        except Exception as ex:
            self.src.close()
            raise FsError(f"The NTFS volume can't be read ({type(ex).__name__}: {ex}).") from ex
        try:
            self.label = self.fs.volume_name or ""
        except Exception:
            self.label = ""
        self.case_insensitive = True

    def _node(self, name, rec, seg):
        from dissect.ntfs.c_ntfs import ATTRIBUTE_TYPE_CODE as A
        if rec.is_reparse_point() and (rec.is_symlink() or rec.is_mount_point()):
            k = "link"
        elif rec.is_dir():
            k = "dir"
        else:
            k = "file"
        size = 0
        if k == "file":
            try:
                size = rec.size()
            except Exception:
                size = 0
        mt = at = None
        try:
            si = rec.attributes[A.STANDARD_INFORMATION]
            if si:
                mt, at = si[0].last_modification_time_ns, si[0].last_access_time_ns
        except Exception:
            pass
        return Node(name, k, size, mt, at, None, int(seg), rec)

    def root(self):
        return self._node("", self.fs.mft.get(5), 5)

    def resolve(self, path):
        p = path.strip("/")
        if not p:
            return self.root()
        try:
            rec = self.fs.mft.get(p)
        except Exception as ex:
            if "not found" in str(ex).lower() or isinstance(ex, (KeyError, FileNotFoundError)):
                raise FileNotFoundError(f"/{p} isn't there any more.") from ex
            raise
        return self._node(p.rsplit("/", 1)[-1], rec, getattr(rec, "segment", 0))

    def iterdir(self, node):
        from dissect.ntfs.util import segment_reference
        rec = node.obj
        seen = set()
        for entry in rec.iterdir(ignore_dos=True):
            try:
                seg = segment_reference(entry.header.FileReference)
                fn = entry.attribute
                name = fn.file_name
            except Exception as ex:
                yield Node("?", "other", error=f"{type(ex).__name__}: {ex}")
                continue
            if seg < 16 or (seg, name) in seen:      # NTFS's own metadata files ($MFT, $Bitmap...)
                continue
            seen.add((seg, name))
            try:
                child = self.fs.mft.get(seg)
                yield self._node(name, child, seg)
            except Exception as ex:
                # fall back to the copy of the details kept in the folder index
                try:
                    k = "dir" if fn.is_dir() else "file"
                    yield Node(name, k, fn.file_size if k == "file" else 0, fn.last_modification_time_ns,
                               fn.last_access_time_ns, None, int(seg), None, error=f"{type(ex).__name__}: {ex}")
                except Exception:
                    yield Node(name, "other", handle=int(seg), error=f"{type(ex).__name__}: {ex}")

    def open_handle(self, handle):
        rec = self.fs.mft.get(int(handle))
        try:
            return rec.open()
        except Exception as ex:
            if "No such stream" in str(ex):
                return EmptyStream(b"")
            raise

    def scan_deleted(self, notes):
        from .undelete_ntfs import scan
        return scan(self, notes)

    def open_deleted(self, handle):
        from .undelete_ntfs import open_deleted
        return open_deleted(self, handle)

    def readlink(self, handle):
        from dissect.ntfs.c_ntfs import ATTRIBUTE_TYPE_CODE as A
        rec = self.fs.mft.get(int(handle))
        attr = rec.attributes[A.REPARSE_POINT]
        target = attr.substitute_name or attr.print_name
        if target.startswith("\\??\\"):
            target = target[4:]
        return target


# ====================================================================== FAT12/16/32
class FatView(FSView):
    def __init__(self, spec, **_):
        super().__init__(spec)
        from dissect.fat import FATFS
        from dissect.fat.c_fat import Fattype
        try:
            self.fs = FATFS(self.src)
        except Exception as ex:
            self.src.close()
            raise FsError(f"The FAT volume can't be read ({type(ex).__name__}: {ex}).") from ex
        self.fstype = {Fattype.FAT12: "fat12", Fattype.FAT16: "fat16", Fattype.FAT32: "fat32"}[self.fs.type]
        self.case_insensitive = True
        fat = self.fs.fat
        self._bits = fat.bits_per_entry
        self._eoc = {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[self._bits]
        self._bad = {12: 0xFF7, 16: 0xFFF7, 32: 0x0FFFFFF7}[self._bits]
        self._fat_off = self.fs.bpb.BPB_RsvdSecCnt * self.fs.sector_size
        self._fat_len = self.fs.fat_size * self.fs.sector_size
        self._fat_cache: dict[int, bytes] = {}
        fat.get = self._fat_get                     # cached, bounds-checked FAT reads
        fat.chain = self._chain                     # loop-safe chains for directories
        lab = (self.fs.volume_label or "").strip()
        self.label = "" if lab.upper() == "NO NAME" else lab
        try:
            for d in self.fs.root.iterdir():
                if d.is_volume_id() and not d.is_directory() and d.dirent.DIR_Attr & 0x0F != 0x0F:
                    self.label = d.short_name.replace(".", "").strip() or self.label
                    break
        except Exception:
            pass

    def _fat_get(self, cluster):
        if self._bits == 12:
            off = cluster + cluster // 2
        else:
            off = cluster * (self._bits // 8)
        if off + 2 > self._fat_len:
            raise ValueError(f"Cluster {cluster} is outside the allocation table")
        blk = off // 65536
        data = self._fat_cache.get(blk)
        if data is None:
            data = self.src.pread(self._fat_off + blk * 65536, min(65536 + 4, self._fat_len - blk * 65536))
            if len(self._fat_cache) > 512:
                self._fat_cache.clear()
            self._fat_cache[blk] = data
        i = off - blk * 65536
        if self._bits == 12:
            v = int.from_bytes(data[i:i + 2], "little")
            return v >> 4 if cluster & 1 else v & 0x0FFF
        if self._bits == 16:
            return int.from_bytes(data[i:i + 2], "little")
        return int.from_bytes(data[i:i + 4], "little") & 0x0FFFFFFF

    def _chain(self, cluster):
        limit = self._fat_len * 8 // self._bits + 2
        n = 0
        while True:
            if cluster < 2:
                raise ValueError(f"Broken cluster chain at {cluster}")
            yield cluster
            n += 1
            if n > limit:
                raise ValueError("The cluster chain loops")
            nxt = self._fat_get(cluster)
            if nxt >= self._eoc or nxt == self._bad or nxt < 2:
                return
            cluster = nxt

    def _runs(self, cluster, size):
        """-> (runs, problem). problem is None when the chain covers the whole file."""
        cs = self.fs.cluster_size
        need = -(-size // cs)
        runs: list[list[int]] = []
        cur, count = cluster, 0
        max_cl = self._fat_len * 8 // self._bits
        problem = None
        while True:
            if cur < 2 or cur >= max_cl:
                problem = f"The file's cluster chain is broken (cluster {cur})"
                break
            idx = cur - 2
            if runs and runs[-1][0] + runs[-1][1] == idx:
                runs[-1][1] += 1
            else:
                runs.append([idx, 1])
            count += 1
            if count >= need:
                break
            nxt = self._fat_get(cur)
            if nxt >= self._eoc:
                problem = "The file's cluster chain ends early"
                break
            if nxt == self._bad:
                problem = f"The file's data runs into a cluster marked bad (after cluster {cur})"
                break
            if nxt < 2:
                problem = f"The file's cluster chain is broken after cluster {cur}"
                break
            cur = nxt
        if problem:
            problem += f" - only the first {min(count * cs, size):,} of {size:,} bytes can be found"
        return [(a, b) for a, b in runs], problem

    @staticmethod
    def _display_name(d) -> str:
        """Long name if its checksum matches the short entry, else the 8.3 name with the
        Windows NT lower-case flags applied (how Windows itself shows it)."""
        raw = bytes(d.dirent.DIR_Name)
        if d.ldirents:
            s = 0
            for b in raw:
                s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
            if all(ld.LDIR_Chksum == s for ld in d.ldirents) and d.name:
                return d.name
        base, _, ext = d.short_name.partition(".")
        nt = d.dirent.DIR_NTRes
        if nt & 0x08:
            base = base.lower()
        if nt & 0x10:
            ext = ext.lower()
        return f"{base}.{ext}" if ext else base

    def _node(self, d, name=None):
        k = "dir" if d.is_directory() else "file"
        name = name or self._display_name(d)
        try:
            mt = _dt_ns(d.mtime)
        except Exception:
            mt = None
        try:
            at = _dt_ns(d.atime)
        except Exception:
            at = None
        size = d.dirent.DIR_FileSize if k == "file" else 0
        return Node(name, k, size, mt, at, None, (int(d.cluster), int(size)), d)

    def root(self):
        return Node("", "dir", 0, handle=None, obj=self.fs.root)

    def iterdir(self, node):
        obj = node.obj
        try:
            for d in obj.iterdir():
                try:
                    if d.name in (".", "..") or d.is_volume_id():
                        continue
                    yield self._node(d)
                except Exception as ex:
                    yield Node(getattr(d, "name", "?") or "?", "other", error=f"{type(ex).__name__}: {ex}")
        finally:
            # dissect keeps every folder's entries forever; on a card with a million files that is
            # gigabytes, so forget them (the OS caches the few sectors needed to list again)
            obj._entries = None

    def open_handle(self, handle):
        from dissect.util.stream import RunlistStream
        cluster, size = handle
        if not size:
            return EmptyStream(b"")
        if cluster < 2:
            raise IOError("The file's data location is missing from its folder entry")
        runs, problem = self._runs(cluster, size)
        if problem is None:
            return RunlistStream(self.fs.data_stream, runs, size, self.fs.cluster_size)
        if not runs:
            raise IOError(problem)
        avail = min(size, sum(n for _, n in runs) * self.fs.cluster_size)
        return PartialStream(RunlistStream(self.fs.data_stream, runs, avail, self.fs.cluster_size), avail, size,
                             problem)


    def scan_deleted(self, notes):
        from .undelete_fat import scan_fat
        return scan_fat(self, notes)

    def open_deleted(self, handle):
        from .undelete_fat import open_deleted_fat
        return open_deleted_fat(self, handle)


# ====================================================================== exFAT
class ExfatView(FSView):
    fstype = "exfat"

    def __init__(self, spec, **_):
        super().__init__(spec)
        from .exfat import ExFAT
        try:
            self.fs = ExFAT(self.src)
        except Exception as ex:
            self.src.close()
            raise FsError(f"The exFAT volume can't be read ({type(ex).__name__}: {ex}).") from ex
        self.label = self.fs.label
        self.case_insensitive = True

    def _node(self, e):
        k = "dir" if e.is_dir else "file"
        size = e.size if k == "file" else 0
        return Node(e.name, k, size, e.mtime_ns, e.atime_ns, None,
                    (int(e.first_cluster), int(e.size), int(e.valid_size), bool(e.nofat)), e,
                    None if e.checksum_ok else "This entry's checksum doesn't match - it may be damaged")

    def root(self):
        return Node("", "dir", obj=self.fs.root)

    def iterdir(self, node):
        for e in self.fs.iterdir(node.obj):
            yield self._node(e)

    def open_handle(self, handle):
        from .exfat import ExFatError
        first, size, valid, nofat = handle
        try:
            return self.fs.open(first, size, valid, nofat)
        except ExFatError as ex:
            if nofat or not size:
                raise IOError(str(ex)) from ex
            runs = self.fs.partial_runs(first)
            if not runs:
                raise IOError(str(ex)) from ex
            from .exfat import ClusterStream
            avail = min(size, sum(n for _, n in runs) * self.fs.cluster_size)
            return PartialStream(ClusterStream(self.fs, runs, avail, min(valid, avail)), avail, size,
                                 f"{ex} - only the first {avail:,} of {size:,} bytes can be found")


    def scan_deleted(self, notes):
        from .undelete_fat import scan_exfat
        return scan_exfat(self, notes)

    def open_deleted(self, handle):
        from .undelete_fat import open_deleted_exfat
        return open_deleted_exfat(self, handle)


# ====================================================================== ext2/3/4, XFS (inode based)
class _InodeView(FSView):
    def _node(self, name, ino):
        try:
            k = _kind_from_mode(ino.filetype)
            size = ino.size if k == "file" else 0
            try:
                mt, at = ino.mtime_ns, ino.atime_ns
            except Exception:
                mt = at = None
            mode = None
            try:
                mode = self._mode(ino) & 0o7777
            except Exception:
                pass
            return Node(name, k, size, mt, at, mode, int(ino.inum), ino)
        except Exception as ex:
            return Node(name, "other", handle=int(getattr(ino, "inum", 0)), obj=ino,
                        error=f"{type(ex).__name__}: {ex}")

    def root(self):
        return self._node("", self.fs.root)

    def iterdir(self, node):
        for ino in node.obj.iterdir():
            name = ino.filename
            if name in (".", "..") or name is None:
                continue
            yield self._node(name, ino)

    def _inode(self, inum):
        return self.fs.get_inode(int(inum))

    def open_handle(self, handle):
        return self._inode(handle).open()

    def readlink(self, handle):
        return self._inode(handle).link


class ExtView(_InodeView):
    def __init__(self, spec, **_):
        super().__init__(spec)
        from dissect.extfs import ExtFS
        from dissect.extfs.c_ext import EXT2, EXT3
        try:
            self.fs = ExtFS(self.src)
        except Exception as ex:
            self.src.close()
            raise FsError(f"The Linux (ext) volume can't be read ({type(ex).__name__}: {ex}).") from ex
        self.fstype = "ext2" if self.fs.type == EXT2 else "ext3" if self.fs.type == EXT3 else "ext4"
        self.label = self.fs.volume_name or ""

    @staticmethod
    def _mode(ino):
        return ino.inode.i_mode


class XfsView(_InodeView):
    fstype = "xfs"

    def __init__(self, spec, **_):
        super().__init__(spec)
        from dissect.xfs import XFS
        try:
            self.fs = XFS(self.src)
        except Exception as ex:
            self.src.close()
            raise FsError(f"The XFS volume can't be read ({type(ex).__name__}: {ex}).") from ex
        self.label = self.fs.name or ""

    @staticmethod
    def _mode(ino):
        return ino.inode.di_mode


# ====================================================================== Btrfs
class BtrfsView(FSView):
    fstype = "btrfs"

    def __init__(self, spec, **_):
        super().__init__(spec)
        from dissect.btrfs import Btrfs
        try:
            self.fs = Btrfs(self.src)
        except Exception as ex:
            self.src.close()
            raise FsError(f"The Btrfs volume can't be read ({type(ex).__name__}: {ex}).") from ex
        self.label = self.fs.label or ""

    def _node(self, name, ino):
        try:
            mode = ino.mode
            k = _kind_from_mode(mode)
            size = ino.size if k == "file" else 0
            return Node(name, k, size, ino.mtime_ns, ino.atime_ns, mode & 0o7777,
                        (int(ino.subvolume.objectid), int(ino.inum)), ino)
        except Exception as ex:
            return Node(name, "other", obj=ino, error=f"{type(ex).__name__}: {ex}")

    def root(self):
        return self._node("", self.fs.root)

    def resolve(self, path):
        p = path.strip("/")
        if not p:
            return self.root()
        try:
            ino = self.fs.get(p)
        except Exception as ex:
            if "not found" in str(ex).lower():
                raise FileNotFoundError(f"/{p} isn't there any more.") from ex
            raise
        return self._node(p.rsplit("/", 1)[-1], ino)

    def iterdir(self, node):
        for name, ino in node.obj.iterdir():
            if name in (".", ".."):
                continue
            yield self._node(name, ino)

    def _inode(self, handle):
        sub, inum = handle
        return self.fs.open_subvolume(int(sub)).inode(int(inum))

    def open_handle(self, handle):
        return self._inode(handle).open()

    def readlink(self, handle):
        return self._inode(handle).link


DELETED_FS = ("ntfs", "fat12", "fat16", "fat32", "exfat")      # where deleted files can be listed
VIEWS = {"apfs": ApfsView, "ntfs": NtfsView, "fat12": FatView, "fat16": FatView, "fat32": FatView,
         "exfat": ExfatView, "ext2": ExtView, "ext3": ExtView, "ext4": ExtView, "xfs": XfsView,
         "btrfs": BtrfsView}
_MODULES = {"apfs": "dissect.apfs", "ntfs": "dissect.ntfs", "fat12": "dissect.fat", "fat16": "dissect.fat",
            "fat32": "dissect.fat", "exfat": "dissect.util", "ext2": "dissect.extfs", "ext3": "dissect.extfs",
            "ext4": "dissect.extfs", "xfs": "dissect.xfs", "btrfs": "dissect.btrfs"}


def available(fs: str) -> bool:
    import importlib.util
    mod = _MODULES.get(fs)
    try:
        return bool(mod) and importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def open_view(spec: dict, cache_blocks: int = 8192, overlay: bool = True) -> FSView:
    """Open a read-only view of the filesystem described by spec
    {source, offset, size?, fs?, volume?}. fs is detected when missing. On NTFS, FAT and exFAT the
    view also serves the "/Deleted files" folder (overlay=False gives the plain view)."""
    fs = spec.get("fs")
    if not fs or fs == "auto":
        src = ReadOnlySource(spec["source"], int(spec.get("offset") or 0))
        try:
            det = detect_fs(src, 0, quick=True)
        finally:
            src.close()
        if det is None:
            raise FsError("No filesystem was recognised at that position.")
        fs = det["fs"]
        spec = dict(spec, fs=fs)
    if fs not in SUPPORTED:
        raise FsError(UNSUPPORTED_WHY.get(fs, f"The {FS_NAMES.get(fs, fs)} filesystem isn't supported."))
    if not available(fs):
        raise FsError(f"Support for {FS_NAMES.get(fs, fs)} isn't installed in this copy of Retrova.")
    cls = VIEWS[fs]
    if cls is ApfsView:
        return cls(spec, cache_blocks=cache_blocks)
    view = cls(spec)
    if overlay and fs in DELETED_FS:
        from .undelete import WithDeleted
        return WithDeleted(view)
    return view
