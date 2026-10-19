"""Deleted files, found by their file-system records, with their original names and folders.

Deleting a file normally only clears a flag (NTFS), a first letter (FAT) or a bit (exFAT); the name,
size, dates and where the data was all stay behind until something reuses that space. The scanners
(undelete_ntfs / undelete_fat) read those leftovers; this module holds what they find as a folder
tree and lets the normal views browse it:

    /Deleted files/<original folders>/<name>        (folders that can't be traced: /Deleted files/Lost folders/...)

Everything goes through the same resolve()/iterdir()/open_handle() calls as live files, so previews,
copy and Verify need nothing special. Nothing is ever written to the source.
"""
from __future__ import annotations

import collections
import os
import threading

DELETED_ROOT = "Deleted files"
LOST = "Lost folders"
GOOD, OVERWRITTEN, INCOMPLETE = "good", "overwritten", "incomplete"
LEVEL_TEXT = {GOOD: "Good", OVERWRITTEN: "Possibly overwritten", INCOMPLETE: "May be incomplete"}
HANDLE_TAG = "del"


def is_deleted_handle(handle) -> bool:
    return isinstance(handle, tuple) and bool(handle) and handle[0] == HANDLE_TAG


def clean_name(name: str) -> str:
    """A name from the disk that is safe as one path part (no separators, never empty)."""
    s = "".join("_" if ch in "/\\" or ord(ch) < 32 else ch for ch in name).strip()
    return s if s not in ("", ".", "..") else "_" + s


def any_bit_set(data: bytes, first: int, count: int) -> bool:
    """Is any of the `count` bits starting at bit `first` set? (bit i = bit i&7 of byte i>>3)"""
    last = first + count - 1
    chunk = bytearray(data[first >> 3:(last >> 3) + 1])
    if not chunk:
        return False
    chunk[0] &= (0xFF << (first & 7)) & 0xFF
    chunk[-1] &= 0xFF >> (7 - (last & 7))
    return any(chunk)


class DeletedFile:
    __slots__ = ("parts", "name", "size", "mtime_ns", "atime_ns", "handle", "level", "note")

    def __init__(self, parts, name, size, mtime_ns, atime_ns, handle, level, note=""):
        self.parts, self.name, self.size = tuple(parts), name, int(size)
        self.mtime_ns, self.atime_ns, self.handle = mtime_ns, atime_ns, handle
        self.level, self.note = level, note


class VDir:
    """The virtual folder behind a Node.obj."""
    __slots__ = ("parts",)

    def __init__(self, parts):
        self.parts = tuple(parts)


def _cf(s: str) -> str:
    return s.casefold()


class DeletedIndex:
    """Every deleted file found, as a case-insensitive folder tree."""

    def __init__(self):
        self.tree: dict[tuple, dict[str, tuple]] = {(): {}}    # folder key -> {folded name: (name, DeletedFile|None)}
        self.count = 0
        self.notes: list[str] = []

    def _dir(self, key, name):
        m = self.tree[key]
        n, tries = name, 1
        while True:
            e = m.get(_cf(n))
            if e is None:
                m[_cf(n)] = (n, None)
                self.tree[key + (_cf(n),)] = {}
                return n
            if e[1] is None:
                return e[0]
            tries += 1
            n = f"{name} (folder)" if tries == 2 else f"{name} (folder {tries - 1})"

    def add(self, f: DeletedFile):
        key = ()
        for part in f.parts:
            key += (_cf(self._dir(key, part)),)
        m = self.tree[key]
        name, tries = f.name, 1
        stem, dot, ext = f.name.rpartition(".")
        if not dot or not stem:
            stem, ext = f.name, ""
        while _cf(name) in m:
            tries += 1
            name = f"{stem} ({tries}){'.' + ext if ext else ''}"
        f.name = name
        m[_cf(name)] = (name, f)
        self.count += 1

    def lookup(self, parts):
        """-> ('dir', key) | ('file', DeletedFile) | None"""
        key = ()
        for i, part in enumerate(parts):
            e = self.tree.get(key, {}).get(_cf(part))
            if e is None:
                return None
            if e[1] is not None:
                return ("file", e[1]) if i == len(parts) - 1 else None
            key += (_cf(part),)
        return ("dir", key)

    def children(self, key):
        return list(self.tree.get(key, {}).values())


# one index per source, shared by every view of it in this process (building walks the whole disk)
_CACHE: collections.OrderedDict[tuple, DeletedIndex] = collections.OrderedDict()
_LOCK = threading.Lock()
_BUILDING: dict[tuple, threading.Lock] = {}


def _cache_key(spec):
    try:
        st = os.stat(spec["source"])
        stamp = (st.st_size, st.st_mtime_ns)
    except OSError:
        stamp = None
    return (spec["source"], int(spec.get("offset") or 0), spec.get("fs"), spec.get("volume"), stamp)


def build_index(spec) -> DeletedIndex:
    from .fsview import open_view
    key = _cache_key(spec)
    with _LOCK:
        idx = _CACHE.get(key)
        if idx is not None:
            _CACHE.move_to_end(key)
            return idx
        lock = _BUILDING.setdefault(key, threading.Lock())
    with lock:
        with _LOCK:
            idx = _CACHE.get(key)
        if idx is None:
            idx = DeletedIndex()
            with open_view(spec, overlay=False) as view:
                for f in view.scan_deleted(idx.notes):
                    idx.add(f)
            with _LOCK:
                _CACHE[key] = idx
                while len(_CACHE) > 4:
                    _CACHE.popitem(last=False)
    with _LOCK:
        _BUILDING.pop(key, None)
    return idx


class WithDeleted:
    """Wraps a view whose filesystem can be scanned for deleted files. Normal paths behave exactly as
    before; "/Deleted files/..." paths browse the deleted-file tree."""

    supports_deleted = True

    def __init__(self, inner):
        self._inner = inner
        self._index = None
        self._root_name = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        self._inner.close()

    # ---- the index
    def deleted_index(self) -> DeletedIndex:
        if self._index is None:
            self._index = build_index(self._inner.spec)
        return self._index

    @property
    def deleted_root(self) -> str:
        """The folder name for deleted files (adjusted in the unlikely case the disk has such a folder)."""
        if self._root_name is None:
            taken = {_cf(c.name) for c in self._inner.iterdir(self._inner.root())}
            name = DELETED_ROOT
            while _cf(name) in taken:
                name += " (Retrova)"
            self._root_name = name
        return self._root_name

    def in_deleted_tree(self, path) -> bool:
        parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
        return self._under_root(parts) is not None

    def _under_root(self, parts):
        if parts and _cf(parts[0]).startswith(_cf(DELETED_ROOT)) and _cf(parts[0]) == _cf(self.deleted_root):
            return parts[1:]
        return None

    # ---- nodes
    def _file_node(self, f: DeletedFile):
        from .fsview import Node
        return Node(f.name, "file", f.size, f.mtime_ns, f.atime_ns, None, f.handle, None, rec=(f.level, f.note))

    def _dir_node(self, name, key_or_parts):
        from .fsview import Node
        return Node(name, "dir", obj=VDir(key_or_parts))

    def root_node(self):
        return self._dir_node(self.deleted_root, ())

    def resolve(self, path):
        parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
        rest = self._under_root(parts)
        if rest is None:
            return self._inner.resolve(path)
        if not rest:
            return self.root_node()
        hit = self.deleted_index().lookup(rest)
        if hit is None:
            raise FileNotFoundError(f"“{rest[-1]}” isn't there any more.")
        if hit[0] == "file":
            return self._file_node(hit[1])
        return self._dir_node(rest[-1], hit[1])

    def iterdir(self, node):
        if not isinstance(node.obj, VDir):
            return self._inner.iterdir(node)
        return self._iter_virtual(node.obj.parts)

    def _iter_virtual(self, key):
        for name, f in self.deleted_index().children(key):
            yield self._file_node(f) if f is not None else self._dir_node(name, key + (_cf(name),))

    def deleted_entries_in(self, path):
        """The deleted files and folders that used to be in the live folder `path` (for showing them
        next to the live ones). -> [Node] named as in the virtual tree."""
        parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
        hit = self.deleted_index().lookup(parts)
        if hit is None or hit[0] != "dir":
            return []
        return list(self._iter_virtual(hit[1]))

    def open_handle(self, handle):
        if is_deleted_handle(handle):
            return self._inner.open_deleted(handle)
        return self._inner.open_handle(handle)
