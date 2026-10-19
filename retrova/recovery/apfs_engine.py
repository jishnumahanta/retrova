"""APFS engine: dissect.apfs plus the T5 Rescue fixes, battle-tested on a failing 500 GB SSD.

Patches (applied once, process-wide):
  * FileStream._read reads only the part of an extent that is asked for (stock code
    re-reads the whole extent per call - unusable for multi-GB videos) and zero-fills
    sparse extents.
  * INode.open uses the inode's private_id (data-stream id) for extents, falling back
    to the inode id - files whose data stream was swapped in (safe-save, some package
    managers) are otherwise unreadable.
  * dissect.cstruct Expression.evaluate keeps its working stack on the shared object,
    so parallel threads corrupt each other ("Invalid expression"): serialised.
"""
from __future__ import annotations

import collections
import stat
import threading

_PATCHED = False
_PATCH_LOCK = threading.Lock()


def patch_dissect():
    global _PATCHED
    with _PATCH_LOCK:
        if _PATCHED:
            return
        _patch_cstruct()
        try:
            _patch_apfs()
        except ImportError:
            pass
        _PATCHED = True


def _patch_cstruct():
    try:
        from dissect.cstruct import expression as _cexpr
    except ImportError:
        return
    if getattr(_cexpr.Expression.evaluate, "_retrova", False):
        return
    orig_eval = _cexpr.Expression.evaluate
    eval_lock = threading.Lock()

    def locked_eval(self, cs, context=None):
        with eval_lock:
            return orig_eval(self, cs, context)

    locked_eval._retrova = True
    _cexpr.Expression.evaluate = locked_eval


def _patch_apfs():
    from io import BytesIO

    from dissect.apfs import stream as _apfs_stream
    from dissect.apfs.c_apfs import c_apfs as _c
    from dissect.apfs.objects import fs as _fs
    from dissect.util.stream import BufferedStream

    if getattr(_apfs_stream.FileStream._read, "_retrova", False):
        return

    def file_stream_read(self, offset, length):
        vol = self.volume
        bs = vol.container.block_size
        cipher = _volume_cipher(vol)
        out = []
        while length > 0:
            try:
                logical, phys, ext_len, crypto_id = self._lookup(offset)
            except _apfs_stream.Error:
                alt = getattr(self, "_t5r_alt_oid", None)
                if alt is None:
                    raise
                # try the other identifier once, then stick with whichever works
                self.oid, self._t5r_alt_oid = alt, None
                logical, phys, ext_len, crypto_id = self._lookup(offset)
            off_in = offset - logical
            avail = ext_len - off_in
            if avail <= 0:
                raise _apfs_stream.Error(f"bad extent for oid {self.oid} at {offset}")
            n = min(length, avail)
            if phys == 0:
                data = bytes(n)
            else:
                skip = off_in % bs
                nblocks = (skip + n + bs - 1) // bs
                data = vol.container._read_block(phys + off_in // bs, nblocks)
                if cipher is not None:
                    if len(data) != nblocks * bs:
                        raise _apfs_stream.Error(f"short read for oid {self.oid} at {offset}")
                    # AES-XTS: the tweak counts 512-byte sectors from the extent's crypto id
                    data = cipher.decrypt(data, crypto_id * vol.container.sectors_per_block
                                          + (off_in // bs) * vol.container.sectors_per_block)
                data = data[skip:skip + n]
                if len(data) < n:
                    raise _apfs_stream.Error(f"short read for oid {self.oid} at {offset}")
            out.append(data)
            offset += n
            length -= n
        return b"".join(out)

    file_stream_read._retrova = True
    _apfs_stream.FileStream._read = file_stream_read

    def inode_open(self):
        if self.is_compressed():
            return _apfs_stream.DecmpfsStream(self)
        if dstream := self.xfields.get(_c.INO_EXT_TYPE.DSTREAM):
            _, dstream = dstream
            pid = self.private_id or self.oid
            fs_ = _apfs_stream.FileStream(self.volume, pid, dstream.size)
            if pid != self.oid:
                fs_._t5r_alt_oid = self.oid
            return fs_
        return BufferedStream(BytesIO(b""), size=0)

    _fs.INode.open = inode_open


def _volume_cipher(vol):
    """The unlocked volume's AES-XTS cipher; None for plain volumes. Locked or per-file-key volumes raise."""
    from dissect.apfs.exception import Error
    if not vol.is_encrypted:
        return None
    if vol._cipher is None:
        raise Error("Volume is encrypted, unlock it first")
    if not vol.is_onekey:
        raise Error("Multi-key encryption is not supported")
    return vol._cipher


def kind_of(ino) -> str:
    m = ino.mode
    if stat.S_ISDIR(m):
        return "dir"
    if stat.S_ISREG(m):
        return "file"
    if stat.S_ISLNK(m):
        return "link"
    return "other"


def dirent_kind(e) -> str:
    if e.is_dir():
        return "dir"
    if e.is_file():
        return "file"
    if e.is_symlink():
        return "link"
    return "other"


def pick_volume(vols, volname=None):
    """The volume to show: the one asked for (name or '#index'), else the user-data volume."""
    if volname:
        if volname.startswith("#") and volname[1:].isdigit():
            i = int(volname[1:])
            if 0 <= i < len(vols):
                return vols[i]
        for v in vols:
            if v.name == volname:
                return v
        raise LookupError(f"There is no volume called '{volname}' here. Volumes: "
                          + ", ".join(repr(v.name) for v in vols))

    def score(v):
        try:
            role = int(v.role)
        except Exception:
            role = 0
        # 0x40 = Data (user files of a macOS install); 0 = none (plain volumes on external drives)
        r = {0x40: 0, 0: 1}.get(role, 3)
        enc = 1 if _safe(lambda: v.is_encrypted, False) else 0
        return (enc, r)

    return sorted(vols, key=score)[0]


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


class ApfsContainer:
    """An APFS container on a read-only source with a shared LRU block cache."""

    def __init__(self, src, cache_blocks: int = 8192):
        patch_dissect()
        from dissect.apfs import APFS
        self.src = src
        self.c = APFS(src)
        bs = self.c.block_size
        cache: collections.OrderedDict[int, bytes] = collections.OrderedDict()
        clock = threading.Lock()

        def read_block(address, count=1):
            if count == 1:
                with clock:
                    hit = cache.get(address)
                    if hit is not None:
                        cache.move_to_end(address)
                        return hit
            data = src.pread(address * bs, count * bs)
            if count == 1:
                with clock:
                    cache[address] = data
                    if len(cache) > cache_blocks:
                        cache.popitem(last=False)
            return data

        self.c._read_block = read_block

    @property
    def volumes(self):
        return self.c.volumes
