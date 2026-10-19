"""A tiny HFS+/HFSX *writer* for tests. hdiutil can't be told to make Time Machine folder links,
compressed files, fragmented files with extents-overflow records or tiny B-tree nodes, so this builds
volumes with exactly those things. Names must be ASCII (or already decomposed): sorting uses a plain
lower-case fold, which is all the reader needs for them."""
import struct

HFS_EPOCH_DELTA = 2082844800
S_IFREG, S_IFDIR, S_IFLNK = 0o100000, 0o040000, 0o120000


def _pad(b):
    return b + b"\0" * (len(b) & 1)


def fork_struct(size=0, blocks=0, extents=()):
    ext = list(extents)[:8]
    ext += [(0, 0)] * (8 - len(ext))
    return struct.pack(">QII", size, 0, blocks) + b"".join(struct.pack(">II", a, b) for a, b in ext)


def btree(records, node_size=512, hfsx=False):
    """records: sorted [(key bytes, data bytes)] -> a B-tree file: header node, leaves, then index levels."""
    def pack(recs, kind, height):
        nodes, cur, used = [], [], 16

        def flush():
            node, off = bytearray(node_size), 14
            offs = []
            for k, d in cur:
                offs.append(off)
                rec = _pad(_pad(struct.pack(">H", len(k)) + k) + d)
                node[off:off + len(rec)] = rec
                off += len(rec)
            offs.append(off)
            node[8], node[9] = kind & 0xFF, height
            struct.pack_into(">H", node, 10, len(cur))
            for i, o in enumerate(offs):
                struct.pack_into(">H", node, node_size - 2 * (i + 1), o)
            nodes.append(node)
        for k, d in recs:
            size = len(_pad(struct.pack(">H", len(k)) + k)) + len(_pad(d)) + 2
            if cur and used + size > node_size:
                flush()
                cur, used = [], 16
            cur.append((k, d))
            used += size
        if cur:
            flush()
        return nodes

    leaves = pack(records, -1, 1) if records else []
    nodes = [bytearray(node_size)] + leaves
    for i, n in enumerate(leaves):                       # leaf n is node i+1: link to its neighbours
        struct.pack_into(">II", n, 0, i + 2 if i + 1 < len(leaves) else 0, i)
    level, depth = list(range(1, len(leaves) + 1)), 1
    while len(level) > 1:
        recs = []
        for n in level:                                  # an index record: the node's first key -> the node
            klen = struct.unpack_from(">H", nodes[n], 14)[0]
            recs.append((bytes(nodes[n][16:16 + klen]), struct.pack(">I", n)))
        depth += 1
        idx = pack(recs, 0, depth)
        level = list(range(len(nodes), len(nodes) + len(idx)))
        nodes += idx
    head = nodes[0]
    head[8] = 1
    struct.pack_into(">H", head, 10, 3)
    struct.pack_into(">HIIIIHHII", head, 14, depth if leaves else 0, level[0] if leaves else 0, len(records),
                     1 if leaves else 0, len(leaves), node_size, 516, len(nodes), 0)
    head[14 + 37] = 0xBC if hfsx else 0xCF
    for i, o in enumerate((14, 120, 248, node_size - 8)):
        struct.pack_into(">H", head, node_size - 2 * (i + 1), o)
    return b"".join(bytes(n) for n in nodes)


class Volume:
    def __init__(self, hfsx=False, block_size=4096, node_size=512, name="Synth", total_blocks=1024):
        self.hfsx, self.bs, self.node_size, self.total_blocks = hfsx, block_size, node_size, total_blocks
        self.cat, self.ext, self.attrs = [], [], []      # (sort key, key bytes, data bytes)
        self.blocks = {}                                 # block number -> bytes
        self.next_block, self.next_cnid = 1, 1000          # tests pick ids below 1000 themselves
        self.files = self.folders = 0
        self.folder(1, name, cnid=2)

    # ---- space
    def _alloc(self, data):
        n = -(-len(data) // self.bs)
        for i in range(n):
            self.blocks[self.next_block + i] = data[i * self.bs:(i + 1) * self.bs].ljust(self.bs, b"\0")
        start, self.next_block = self.next_block, self.next_block + n + 3        # a gap after every run
        return start, n

    def _fork(self, cnid, ftype, data, pieces=1):
        """Store `data` in `pieces` separate runs, placed on disk in reverse order of the file; runs
        past the eighth go into the extents-overflow tree. -> the 80-byte fork struct."""
        if not data:
            return fork_struct()
        nblocks = -(-len(data) // self.bs)
        per = -(-nblocks // pieces)
        spans = [(i, min(i + per, nblocks)) for i in range(0, nblocks, per)]
        runs = {}
        for idx in reversed(range(len(spans))):
            a, b = spans[idx]
            runs[idx] = self._alloc(data[a * self.bs:b * self.bs])
        runs = [runs[i] for i in range(len(spans))]
        have = sum(n for _, n in runs[:8])
        rest = runs[8:]
        while rest:
            chunk, rest = rest[:8], rest[8:]
            key = struct.pack(">BBII", ftype, 0, cnid, have)
            body = b"".join(struct.pack(">II", a, b) for a, b in chunk + [(0, 0)] * (8 - len(chunk)))
            self.ext.append(((cnid, ftype, have), key, body))
            have += sum(n for _, n in chunk)
        return fork_struct(len(data), nblocks, runs[:8])

    # ---- catalog
    def _fold(self, name):
        return name.encode("utf-16-be") if self.hfsx else name.replace("\0", "").lower().encode("utf-16-be")

    def _add(self, parent, name, data):
        key = struct.pack(">IH", parent, len(name)) + name.encode("utf-16-be")
        self.cat.append(((parent, self._fold(name)), key, data))

    def _thread(self, cnid, parent, name, kind):
        self.cat.append(((cnid, b""), struct.pack(">IH", cnid, 0),
                         struct.pack(">HHIH", kind, 0, parent, len(name)) + name.encode("utf-16-be")))

    def _new_id(self, cnid):
        if cnid is None:
            cnid, self.next_cnid = self.next_cnid, self.next_cnid + 1
        return cnid

    def folder(self, parent, name, cnid=None, mode=0o755, mtime=1_600_000_000):
        cnid, t = self._new_id(cnid), mtime + HFS_EPOCH_DELTA
        perms = struct.pack(">IIBBHI", 501, 20, 0, 0, S_IFDIR | mode, 0)
        rec = struct.pack(">HHII5I", 1, 0, 0, cnid, t, t, t, t, 0) + perms + bytes(32) + bytes(8)
        assert len(rec) == 88
        self._add(parent, name, rec)
        self._thread(cnid, parent, name, 3)
        self.folders += 1
        return cnid

    def file(self, parent, name, data=b"", rsrc=b"", mode=0o644, kind=S_IFREG, ftype=b"\0\0\0\0",
             creator=b"\0\0\0\0", special=0, owner_flags=0, mtime=1_600_000_000, cnid=None, pieces=1):
        cnid, t = self._new_id(cnid), mtime + HFS_EPOCH_DELTA
        perms = struct.pack(">IIBBHI", 501, 20, 0, owner_flags, kind | mode, special)
        rec = (struct.pack(">HHII5I", 2, 0, 0, cnid, t, t, t, t, 0) + perms + ftype + creator + bytes(8) +
               bytes(16) + bytes(8) + self._fork(cnid, 0x00, data, pieces) + self._fork(cnid, 0xFF, rsrc))
        assert len(rec) == 248
        self._add(parent, name, rec)
        self._thread(cnid, parent, name, 4)
        self.files += 1
        return cnid

    def symlink(self, parent, name, target):
        return self.file(parent, name, target.encode(), kind=S_IFLNK, mode=0o755, ftype=b"slnk", creator=b"rhap")

    def xattr(self, cnid, name, value):
        raw = name.encode("utf-16-be")
        key = struct.pack(">HIIH", 0, cnid, 0, len(name)) + raw
        self.attrs.append(((cnid, raw, 0), key, struct.pack(">IQI", 0x10, 0, len(value)) + value))

    # ---- image
    def _tree(self, recs):
        recs = [(k, d) for _, k, d in sorted(recs, key=lambda r: r[0])]
        data = btree(recs, self.node_size, self.hfsx)
        start, n = self._alloc(data)
        return fork_struct(len(data), n, [(start, n)])

    def image(self) -> bytes:
        forks = [fork_struct(), self._tree(self.ext), self._tree(self.cat),
                 self._tree(self.attrs) if self.attrs else fork_struct(), fork_struct()]
        total = max(self.total_blocks, self.next_block + 1)
        vh = bytearray(512)
        vh[0:2] = b"HX" if self.hfsx else b"H+"
        struct.pack_into(">HIII", vh, 2, 5 if self.hfsx else 4, 0x100, 0x31302E30, 0)
        struct.pack_into(">IIII", vh, 32, self.files, self.folders, self.bs, total)
        struct.pack_into(">I", vh, 64, self.next_cnid)
        for i, f in enumerate(forks):
            vh[112 + 80 * i:112 + 80 * (i + 1)] = f
        img = bytearray(total * self.bs)
        img[1024:1536] = vh
        for n, b in self.blocks.items():
            img[n * self.bs:(n + 1) * self.bs] = b
        return bytes(img)
