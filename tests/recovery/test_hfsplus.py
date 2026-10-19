"""HFS+ / HFSX: real volumes made by macOS (hdiutil) plus hand-built ones for what hdiutil can't make."""
import gzip
import os
import pickle
import shutil
import struct
import subprocess
import unicodedata
import uuid
import zlib

import pytest
from hfs_builder import Volume
from hfs_tree import FILES, HARDLINKS, MANY, MTIME, RESOURCE_FORKS, SYMLINKS, VOLNAME

from retrova.recovery.fsview import FsError, open_view
from retrova.recovery.hfsplus import HfsPlus, hfs_ns
from retrova.recovery.partitions import detect_fs, scan
from retrova.recovery.source import ReadOnlySource

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR = os.path.join(ROOT, "hfs_images")
KINDS = ["hfsplus", "hfsx", "jhfs"]


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    out = tmp_path_factory.mktemp("hfs")
    paths = {}
    for k in KINDS:
        paths[k] = str(out / f"{k}.img")
        with gzip.open(os.path.join(IMG_DIR, k + ".img.gz"), "rb") as src, open(paths[k], "wb") as dst:
            shutil.copyfileobj(src, dst)
    return paths


def walk(view):
    files, dirs, links = {}, set(), {}

    def go(node, path):
        for c in view.iterdir(node):
            assert not c.error, (c.name, c.error)
            p = (path + "/" + c.name).lstrip("/")
            if c.kind == "dir":
                dirs.add(p)
                go(c, "/" + p)
            elif c.kind == "file":
                files[p] = c
            elif c.kind == "link":
                links[p] = c
    go(view.root(), "")
    return files, dirs, links


def synth(tmp_path, vol, name="v.img"):
    p = str(tmp_path / name)
    with open(p, "wb") as fh:
        fh.write(vol.image())
    return p


# ------------------------------------------------------------------ volumes made by macOS
@pytest.mark.parametrize("key", KINDS)
def test_real_volume_tree_names_and_bytes(real, key):
    with open_view({"source": real[key], "offset": 0}) as v:
        assert v.fstype == "hfs+" and v.label == VOLNAME and not v.notes
        assert v.case_insensitive == (key != "hfsx")
        files, dirs, links = walk(v)
        want_files = {p: d for p, d in FILES.items() if not p.endswith("/")}
        want_dirs = {os.path.dirname(p) for p in want_files}
        want_dirs |= {os.path.dirname(d) for d in want_dirs if os.path.dirname(d)} | {"emptydir"}
        want_dirs.discard("")
        # exact names (composed form), nothing extra: no private folders, no journal files
        assert set(files) == set(want_files) | set(HARDLINKS)
        assert dirs == want_dirs
        assert set(links) == set(SYMLINKS)
        for rel, data in want_files.items():
            n = files[rel]
            assert n.size == len(data), rel
            assert v.open_handle(n.handle).read(n.size) == data, rel
        for name, target in SYMLINKS.items():
            assert v.readlink(links[name].handle) == target
        assert {c.name for c in v.iterdir(v.root())} == \
            {p.split("/")[0] for p in list(want_files) + list(HARDLINKS) + list(SYMLINKS)} | {"emptydir"}


@pytest.mark.parametrize("key", KINDS)
def test_real_volume_hard_link_times_and_modes(real, key):
    with open_view({"source": real[key], "offset": 0}) as v:
        top = {c.name: c for c in v.iterdir(v.root())}
        hello, hard = top["hello.txt"], top["hard.txt"]
        assert hello.handle[0] == hard.handle[0]                   # one inode behind both names
        assert v.open_handle(hard.handle).read() == b"hello world\n"
        assert hello.mtime_ns == hard.mtime_ns == MTIME * 1_000_000_000
        assert hello.mode == 0o644 and top["docs"].mode == 0o755 and top["docs"].kind == "dir"
        assert top["zero.txt"].size == 0 and v.open_handle(top["zero.txt"].handle).read() == b""


@pytest.mark.parametrize("key", KINDS)
def test_real_volume_names_are_composed(real, key):
    with open_view({"source": real[key], "offset": 0}) as v:
        names = {c.name for c in v.iterdir(v.resolve("/café"))}
        assert names == {"ü-naïve.txt", "😀 smile.txt"}
        assert all(unicodedata.is_normalized("NFC", n) for n in names)
        assert v.resolve("café/ü-naïve.txt").size == 13
        assert "col:on.txt" in {c.name for c in v.iterdir(v.root())}      # a "/" on disk, shown as ":"


def test_real_volume_case_rules(real):
    with open_view({"source": real["hfsplus"], "offset": 0}) as v:
        assert v.resolve("docs/readme.txt").size == 10 and v.resolve("DOCS/README.TXT").size == 10
    with open_view({"source": real["hfsx"], "offset": 0}) as v:
        assert v.resolve("docs/Readme.TXT").size == 10
        with pytest.raises(FileNotFoundError):
            v.resolve("docs/readme.txt")


def test_real_volume_resource_fork_and_handles_survive_pickle(real):
    with open_view({"source": real["hfsplus"], "offset": 0}) as v:
        node = v.resolve("withrsrc.txt")
        handle = pickle.loads(pickle.dumps(node.handle))       # our own handle tuple, as the worker queue does
    with open_view({"source": real["hfsplus"], "offset": 0}) as w:          # what a worker process does
        assert w.open_handle(handle).read() == b"data fork\n"
    fs = HfsPlus(ReadOnlySource(real["hfsplus"]))
    e = next(e for e in fs.iterdir(2) if e.name == "withrsrc.txt")
    assert fs.open_fork(e.rsrc, e.cid, rsrc=True).read() == RESOURCE_FORKS["withrsrc.txt"]
    assert fs.path_of(e.cid) == "/withrsrc.txt"
    deep = next(x for x in fs.iterdir(next(x for x in fs.iterdir(next(x for x in fs.iterdir(2)
                if x.name == "docs").cid) if x.name == "deep").cid))
    assert fs.path_of(deep.cid) == "/docs/deep/er"


def test_real_volume_catalog_is_deep_and_lists_every_name(real):
    fs = HfsPlus(ReadOnlySource(real["hfsplus"]))
    assert fs.catalog.node_size == 4096
    many = next(e for e in fs.iterdir(2) if e.name == "many")
    names = [e.name for e in fs.iterdir(many.cid)]
    assert len(names) == MANY and sorted(names) == [f"f{i:04d}.txt" for i in range(MANY)]
    # looking up one name by key (no scan) finds the right record
    assert fs.lookup(many.cid, "f1234.txt".encode("utf-16-be"))
    assert fs.lookup(many.cid, "F1234.TXT".encode("utf-16-be"))              # case-insensitive volume
    assert not fs.lookup(many.cid, "f9999.txt".encode("utf-16-be"))


@pytest.mark.parametrize("key,detail", [("hfsplus", ""), ("hfsx", "case-sensitive (HFSX)"), ("jhfs", "")])
def test_partition_scan_marks_hfs_supported(real, key, detail):
    lay = scan(real[key])
    (p,) = lay["parts"]
    assert p["fs"] == "hfs+" and p["supported"] and p["label"] == VOLNAME and p["detail"] == detail
    assert p["size"] == os.path.getsize(real[key]) and "why" not in p
    assert detect_fs(ReadOnlySource(real[key]), 0, quick=True)["fs"] == "hfs+"
    with open_view({"source": real[key], "offset": 0, "fs": "auto"}) as v:
        assert v.fs_name.startswith("Mac OS Extended")


def test_not_an_hfs_volume(tmp_path):
    p = tmp_path / "x.img"
    h = bytearray(8192)
    h[1024:1026] = b"H+"                    # the magic alone isn't enough: version and block size must make sense
    p.write_bytes(bytes(h))
    assert scan(str(p))["parts"] == []
    with pytest.raises(FsError):
        open_view({"source": str(p), "offset": 0, "fs": "hfs+"})


# ------------------------------------------------------------------ hand-built volumes
def test_time_machine_folder_links_and_file_hard_links(tmp_path):
    v = Volume(node_size=512)
    v.folder(2, "\0\0\0\0HFS+ Private Data", cnid=20, mode=0)
    files_dir = 20
    dirs_dir = v.folder(2, ".HFS+ Private Directory Data\r", cnid=21, mode=0)
    v.file(files_dir, "iNode400", b"shared bytes\n", cnid=400)
    for name, cnid in (("a.txt", 401), ("b.txt", 402)):
        v.file(2, name, ftype=b"hlnk", creator=b"hfs+", special=400, cnid=cnid)
    snapshot = v.folder(dirs_dir, "dir_300", cnid=300)               # the one real copy of the backed-up folder
    v.file(snapshot, "notes.txt", b"inside the backup\n")
    users = v.folder(snapshot, "Users")
    v.file(users, "me.txt", b"me\n")
    machine = v.folder(v.folder(2, "Backups.backupdb"), "Mac")
    for stamp in ("2024-01-01-000000", "2024-01-02-000000"):
        v.file(v.folder(machine, stamp), "Macintosh HD", ftype=b"fdrp", creator=b"MACS", special=300)
    path = synth(tmp_path, v)
    with open_view({"source": path, "offset": 0}) as view:
        assert {c.name for c in view.iterdir(view.root())} == {"a.txt", "b.txt", "Backups.backupdb"}
        a, b = view.resolve("a.txt"), view.resolve("b.txt")
        assert a.size == b.size == 13 and view.open_handle(a.handle).read() == b"shared bytes\n"
        assert a.handle[0] == b.handle[0] == 400
        files, dirs, _ = walk(view)
        got = {p: view.open_handle(n.handle).read() for p, n in files.items() if p.startswith("Backups")}
        assert got == {f"Backups.backupdb/Mac/{stamp}/Macintosh HD/{rel}": data
                       for stamp in ("2024-01-01-000000", "2024-01-02-000000")
                       for rel, data in (("notes.txt", b"inside the backup\n"), ("Users/me.txt", b"me\n"))}
        assert {p for p in dirs if p.endswith("Macintosh HD")} == \
            {f"Backups.backupdb/Mac/{s}/Macintosh HD" for s in ("2024-01-01-000000", "2024-01-02-000000")}
        node = view.resolve("/Backups.backupdb/Mac/2024-01-02-000000/Macintosh HD/Users/me.txt")
        assert view.open_handle(node.handle).read() == b"me\n"
        assert view.resolve("Backups.backupdb/Mac/2024-01-01-000000/Macintosh HD").kind == "dir"


def test_missing_hard_link_target_is_reported_not_fatal(tmp_path):
    v = Volume()
    v.file(2, "orphan.txt", ftype=b"hlnk", creator=b"hfs+", special=999)
    v.file(2, "fine.txt", b"ok")
    with open_view({"source": synth(tmp_path, v), "offset": 0}) as view:
        kids = {c.name: c for c in view.iterdir(view.root())}
        assert "original file is missing" in kids["orphan.txt"].error
        assert view.open_handle(kids["fine.txt"].handle).read() == b"ok"


@pytest.mark.parametrize("pieces", [3, 12, 21])
def test_fragmented_files_use_extents_overflow(tmp_path, pieces):
    data = b"".join(struct.pack(">I", n) * 1024 for n in range(130)) + b"tail"        # 130 distinct blocks
    v = Volume()
    v.file(2, "frag.bin", data, pieces=pieces, cnid=77)
    v.file(2, "after.txt", b"after")
    path = synth(tmp_path, v)
    assert (len(v.ext) > 0) == (pieces > 8)
    with open_view({"source": path, "offset": 0}) as view:
        n = view.resolve("frag.bin")
        assert n.size == len(data) and view.open_handle(n.handle).read() == data
        fh = view.open_handle(n.handle)
        fh.seek(100 * 4096 + 2)
        assert fh.read(10) == data[100 * 4096 + 2:100 * 4096 + 12]


def test_lost_overflow_record_keeps_the_readable_start(tmp_path):
    data = b"".join(struct.pack(">I", n) * 1024 for n in range(130))
    v = Volume()
    v.file(2, "frag.bin", data, pieces=21, cnid=77)
    v.ext.clear()                                                     # the extents-overflow records are gone
    with open_view({"source": synth(tmp_path, v), "offset": 0}) as view:
        n = view.resolve("frag.bin")
        fh = view.open_handle(n.handle)
        first = fh.read(8 * 7 * 4096)
        assert first == data[:len(first)]
        with pytest.raises(IOError, match="only the first"):
            fh.seek(len(data) - 10)
            fh.read(10)


def test_hfsx_keeps_names_that_differ_only_by_case(tmp_path):
    v = Volume(hfsx=True)
    v.file(2, "Readme.txt", b"one")
    v.file(2, "README.TXT", b"two")
    v.file(2, "café.txt", b"decomposed on disk")
    with open_view({"source": synth(tmp_path, v), "offset": 0}) as view:
        assert not view.case_insensitive
        assert view.open_handle(view.resolve("Readme.txt").handle).read() == b"one"
        assert view.open_handle(view.resolve("README.TXT").handle).read() == b"two"
        assert "café.txt" in {c.name for c in view.iterdir(view.root())}
        with pytest.raises(FileNotFoundError):
            view.resolve("readme.txt")


def test_symlink_and_dates(tmp_path):
    v = Volume()
    v.file(2, "t.txt", b"x", mtime=1_700_000_000)
    v.symlink(2, "to-t", "t.txt")
    with open_view({"source": synth(tmp_path, v), "offset": 0}) as view:
        kids = {c.name: c for c in view.iterdir(view.root())}
        assert kids["t.txt"].mtime_ns == 1_700_000_000 * 1_000_000_000
        assert kids["to-t"].kind == "link" and view.readlink(kids["to-t"].handle) == "t.txt"
    assert hfs_ns(0) is None and hfs_ns(2082844800) == 0


# ------------------------------------------------------------------ transparent compression
def decmpfs(algo, size, payload=b""):
    return b"fpmc" + struct.pack("<IQ", algo, size) + payload


def lzvn(parts):
    """A valid LZVN stream: literals, plus one 'repeat the last byte' match to exercise the decoder."""
    out = b""
    for kind, arg in parts:
        if kind == "lit":
            for i in range(0, len(arg), 15):
                piece = arg[i:i + 15]
                out += bytes([0xE0 | len(piece)]) + piece
        else:                                    # ("rle", count): small-distance match, distance 1
            out += bytes([(arg - 3) << 3, 1])
    return out + b"\x06" + bytes(7)


def rsrc_zlib(blocks):
    """The resource fork of a zlib-compressed file: a 256-byte header area, then the block table."""
    table = struct.pack("<I", len(blocks))
    pos = 4 + 8 * len(blocks)
    for b in blocks:
        table += struct.pack("<II", pos, len(b))
        pos += len(b)
    body = struct.pack(">I", pos) + table + b"".join(blocks)
    head = struct.pack(">IIII", 256, 256 + len(body), len(body), 0).ljust(256, b"\0")
    return head + body


def rsrc_offsets(blocks):
    """The resource fork of an LZVN / LZFSE file: a table of block offsets, then the blocks."""
    offs, pos = [], 4 * (len(blocks) + 1)
    for b in blocks:
        offs.append(pos)
        pos += len(b)
    return struct.pack(f"<{len(blocks) + 1}I", *offs, pos) + b"".join(blocks)


def lzfse_raw(data):
    return b"bvx-" + struct.pack("<I", len(data)) + data + b"bvx$"


UF_COMPRESSED = 0x20


def test_compressed_files(tmp_path):
    big = b"".join(struct.pack(">I", n) * 4 for n in range(14000))              # 224000 bytes: 4 blocks
    blocks = [big[i:i + 0x10000] for i in range(0, len(big), 0x10000)]
    lz_text = b"a" + b"a" * 10 + b"hello world, this is lzvn text"
    inline = {"zlib-inline.txt": (3, b"inline zlib content", zlib.compress(b"inline zlib content")),
              "zlib-raw-marker.txt": (3, b"plain!", b"\xffplain!"),
              "lzvn-inline.txt": (7, lz_text, lzvn([("lit", b"a"), ("rle", 10),
                                                    ("lit", b"hello world, this is lzvn text")])),
              "lzfse-inline.txt": (11, b"lzfse bytes!", lzfse_raw(b"lzfse bytes!")),
              "plain-inline.txt": (9, b"hello", b"\x00hello")}
    cases = {n: (decmpfs(a, len(c), payload), c, b"") for n, (a, c, payload) in inline.items()}
    cases.update({
        "zlib-rsrc.bin": (decmpfs(4, len(big)), big, rsrc_zlib([zlib.compress(b) for b in blocks])),
        "lzvn-rsrc.bin": (decmpfs(8, len(big)), big,
                          rsrc_offsets([b"\x06" + blocks[0]] + [lzvn([("lit", b)]) for b in blocks[1:]])),
        "plain-rsrc.bin": (decmpfs(10, len(big)), big, rsrc_zlib([b"\xff" + b for b in blocks])),
    })
    v = Volume()
    for name, (attr, content, rsrc) in cases.items():
        cid = v.file(2, name, b"", rsrc=rsrc, owner_flags=UF_COMPRESSED)
        v.xattr(cid, "com.apple.decmpfs", attr)
    v.file(2, "flag-but-no-xattr.txt", b"stored normally", owner_flags=UF_COMPRESSED)
    with open_view({"source": synth(tmp_path, v), "offset": 0}) as view:
        for name, (_, content, _) in cases.items():
            n = view.resolve(name)
            assert n.size == len(content), name
            fh = view.open_handle(n.handle)
            assert fh.read(len(content)) == content, name
            fh.seek(max(0, len(content) - 7))                                     # random access
            assert fh.read() == content[-7:], name
        plain = view.resolve("flag-but-no-xattr.txt")
        assert plain.size == 15 and view.open_handle(plain.handle).read() == b"stored normally"


def test_damaged_compressed_block_is_an_io_error(tmp_path):
    v = Volume()
    cid = v.file(2, "bad.txt", b"", owner_flags=UF_COMPRESSED)
    v.xattr(cid, "com.apple.decmpfs", decmpfs(3, 50, b"\x78\x9c" + b"garbage"))
    with open_view({"source": synth(tmp_path, v), "offset": 0}) as view:
        with pytest.raises(IOError):
            view.open_handle(view.resolve("bad.txt").handle).read()


# ------------------------------------------------------------------ containers and empty volumes
def test_old_hfs_wrapper_around_hfs_plus(tmp_path):
    inner = Volume(name="Inner")
    inner.file(2, "wrapped.txt", b"inside a wrapper")
    img = inner.image()
    alloc_start_sectors, start_block, bs = 4, 8, 2048
    base = alloc_start_sectors * 512 + start_block * bs
    mdb = bytearray(512)
    mdb[0:2] = b"BD"
    struct.pack_into(">I", mdb, 20, bs)
    struct.pack_into(">H", mdb, 28, alloc_start_sectors)
    mdb[124:126] = b"H+"
    struct.pack_into(">HH", mdb, 126, start_block, len(img) // bs)
    disk = bytearray(base + len(img))
    disk[1024:1536] = mdb
    disk[base:] = img
    p = tmp_path / "wrapped.img"
    p.write_bytes(bytes(disk))
    lay = scan(str(p))
    assert lay["parts"][0]["fs"] == "hfs+" and lay["parts"][0]["supported"]
    with open_view({"source": str(p), "offset": 0}) as view:
        n = view.resolve("wrapped.txt")
        assert view.open_handle(n.handle).read() == b"inside a wrapper"


def put_partition_tables(tmp_path, hfs_image):
    """The same HFS+ volume behind an Apple Partition Map and behind a GPT; -> the two disk image paths."""
    with open(hfs_image, "rb") as fh:
        vol = fh.read()
    start = 64                                                           # sectors
    apm = bytearray((start + len(vol) // 512 + 8) * 512)
    apm[0:4] = b"ER" + struct.pack(">H", 512)
    entries = [("Apple_partition_map", "Apple", 1, 63), ("Apple_Driver43", "Macintosh", 64 - 8, 4),
               ("Apple_HFS", "Photos", start, len(vol) // 512)]
    for i, (typ, name, first, count) in enumerate(entries):
        e = bytearray(512)
        e[0:2] = b"PM"
        struct.pack_into(">III", e, 4, len(entries), first, count)
        e[16:16 + len(name)], e[48:48 + len(typ)] = name.encode(), typ.encode()
        apm[(1 + i) * 512:(2 + i) * 512] = e
    apm[start * 512:start * 512 + len(vol)] = vol
    gpt = bytearray((start + len(vol) // 512 + 40) * 512)
    ent = bytearray(128)
    ent[0:16] = uuid.UUID("48465300-0000-11aa-aa11-00306543ecac").bytes_le
    ent[16:32] = bytes(range(1, 17))
    struct.pack_into("<QQ", ent, 32, start, start + len(vol) // 512 - 1)
    ent[56:56 + 12] = "Time Machine".encode("utf-16-le")
    table = bytes(ent) + bytes(128 * 127)
    hdr = bytearray(92)
    hdr[0:8] = b"EFI PART"
    struct.pack_into("<IIIIQQQQ", hdr, 8, 0x10000, 92, 0, 0, 1, len(gpt) // 512 - 1, 34, len(gpt) // 512 - 34)
    hdr[56:72] = bytes(range(16))
    struct.pack_into("<QIII", hdr, 72, 2, 128, 128, zlib.crc32(table) & 0xFFFFFFFF)
    struct.pack_into("<I", hdr, 16, zlib.crc32(bytes(hdr)) & 0xFFFFFFFF)
    gpt[512:512 + 92] = hdr
    gpt[1024:1024 + len(table)] = table
    gpt[start * 512:start * 512 + len(vol)] = vol
    out = []
    for name, data in (("apm.img", apm), ("gpt.img", gpt)):
        p = tmp_path / name
        p.write_bytes(bytes(data))
        out.append(str(p))
    return out


def test_apple_partition_map_and_gpt_apple_hfs(real, tmp_path):
    apm, gpt = put_partition_tables(tmp_path, real["hfsplus"])
    lay = scan(apm)
    assert lay["scheme"] == "apm"
    hfs = [p for p in lay["parts"] if p["fs"] == "hfs+"]
    assert len(hfs) == 1 and hfs[0]["supported"] and hfs[0]["name"] == "Photos" and hfs[0]["offset"] == 64 * 512
    drivers = [p for p in lay["parts"] if p["type"] == "Apple_Driver43"]
    assert drivers and not drivers[0]["supported"] and "no files" in drivers[0]["why"]
    lay = scan(gpt)
    assert lay["scheme"] == "gpt" and not lay["notes"]
    (p,) = lay["parts"]
    assert p["type_name"] == "Apple HFS+" and p["fs"] == "hfs+" and p["supported"] and p["name"] == "Time Machine"
    for part, disk in ((hfs[0], apm), (p, gpt)):
        with open_view({"source": disk, "offset": part["offset"]}) as v:
            assert v.open_handle(v.resolve("hello.txt").handle).read() == b"hello world\n"


def test_empty_volume_from_mkfs(tmp_path):
    if not shutil.which("mkfs.hfsplus"):
        pytest.skip("mkfs.hfsplus (hfsprogs) is not installed")
    for flag, case in (([], True), (["-s"], False)):
        p = str(tmp_path / f"empty{len(flag)}.img")
        with open(p, "wb") as fh:
            fh.truncate(16 * 1024 * 1024)
        subprocess.run(["mkfs.hfsplus", *flag, "-v", "Fresh", p], check=True, capture_output=True)
        with open_view({"source": p, "offset": 0}) as v:
            assert v.label == "Fresh" and v.case_insensitive == case
            assert list(v.iterdir(v.root())) == []
        assert scan(p)["parts"][0]["supported"]


def test_journal_note_when_not_ejected(tmp_path):
    v = Volume()
    v.file(2, "a.txt", b"a")
    p = synth(tmp_path, v)
    with open(p, "r+b") as fh:                       # journaled, "unmounted cleanly" bit cleared
        fh.seek(1024 + 4)
        fh.write(struct.pack(">I", 0x2000))
    with open_view({"source": p, "offset": 0}) as view:
        assert any("ejected cleanly" in n for n in view.notes)
        assert [c.name for c in view.iterdir(view.root())] == ["a.txt"]
