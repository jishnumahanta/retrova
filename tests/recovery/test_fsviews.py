import hashlib
import os
import pickle
import time

import pytest
from conftest import APFS_DIR, expected_for, need

from retrova.recovery.fsview import FsError, open_view

FS_KEYS = ["fat32", "fat16", "fat12", "ext4", "xfs", "btrfs", "ntfs", "exfat"]


def walk(view):
    files, dirs, links, errors = {}, [], {}, []

    def go(node, path):
        for c in view.iterdir(node):
            p = (path + "/" + c.name).lstrip("/")
            if c.error:
                errors.append((p, c.error))
            if c.kind == "dir":
                dirs.append(p)
                go(c, "/" + p)
            elif c.kind == "file":
                files[p] = c
            elif c.kind == "link":
                links[p] = c
    go(view.root(), "")
    return files, dirs, links, errors


@pytest.mark.parametrize("key", FS_KEYS)
def test_walk_and_read_every_file(images, tree, key):
    path = need(images, key)
    with open_view({"source": path, "offset": 0}) as v:
        assert v.fstype == key
        files, dirs, links, errors = walk(v)
        assert not errors
        want = expected_for(tree, key, images.get("ntfs_mode"))
        for rel, data in want.items():
            assert rel in files, f"{rel} missing on {key}"
            n = files[rel]
            assert n.size == len(data)
            fh = v.open_handle(n.handle)
            assert fh.read(n.size) == data, rel
            pickle.dumps(n.handle)              # worker processes get handles through a queue
        if key != "fat12" and not (key == "ntfs" and images.get("ntfs_mode") == "flat"):
            assert "emptydir" in dirs


@pytest.mark.parametrize("key", ["ext4", "xfs", "btrfs"])
def test_symlinks_and_times(images, key):
    with open_view({"source": need(images, key), "offset": 0}) as v:
        root_children = {c.name: c for c in v.iterdir(v.root())}
        link = root_children["link.txt"]
        assert link.kind == "link" and v.readlink(link.handle) == "hello.txt"
        if key != "xfs":         # mkfs.xfs protofiles don't carry timestamps
            assert root_children["hello.txt"].mtime_ns // 1_000_000_000 == 1_600_000_000
        assert root_children["hello.txt"].mtime_ns > 0


def test_fat_times_are_local(images):
    with open_view({"source": need(images, "fat32"), "offset": 0}) as v:
        n = v.resolve("/hello.txt")
        assert abs(n.mtime_ns / 1e9 - 1_600_000_000) <= 2      # 2-second FAT resolution


@pytest.mark.parametrize("key", ["fat32", "ntfs", "exfat"])
def test_case_insensitive_resolve(images, key):
    if key == "ntfs" and images.get("ntfs_mode") == "flat":
        pytest.skip("NTFS image without folders")
    with open_view({"source": need(images, key), "offset": 0}) as v:
        n = v.resolve("/DOCS/SUB/X.TXT")
        assert n.kind == "file" and n.size == 5000
        with pytest.raises(FileNotFoundError):
            v.resolve("/docs/nope.txt")


def test_handles_reopen_in_a_new_view(images, tree):
    """What a Windows worker process does: open the spec again and find the file by handle."""
    for key in ("fat32", "ext4", "ntfs", "exfat", "btrfs", "xfs"):
        if key not in images:
            continue
        spec = {"source": images[key], "offset": 0}
        with open_view(spec) as a:
            n = a.resolve("/hello.txt")
            h = pickle.loads(pickle.dumps(n.handle))
        with open_view(spec) as b:
            assert b.open_handle(h).read(100) == tree["hello.txt"]


def test_exfat_fragmented_and_big_directory(images):
    if images.get("exfat_mode") != "full":
        pytest.skip("exFAT image could not be populated (needs FUSE)")
    import random
    with open_view({"source": images["exfat"], "offset": 0}) as v:
        n = v.resolve("/fragmented.bin")
        assert n.handle[3] is False                         # uses the FAT chain
        assert len(v.fs.runs(n.handle[0], n.size, False)) > 1
        assert v.open_handle(n.handle).read(n.size) == random.Random(12).randbytes(600_000)
        names = [c.name for c in v.iterdir(v.resolve("/many"))]
        assert len(names) == 300 and "file number 0299 with a long name.txt" in names


def test_partition_views(images):
    for off, fs in images.get("mbr_parts", []):
        with open_view({"source": images["mbr"], "offset": off}) as v:
            assert v.fstype == fs
            assert v.resolve("/hello.txt").size == 12


def test_broken_fat_chain_reports_error(images, tmp_path):
    src = need(images, "fat32")
    data = bytearray(open(src, "rb").read())
    with open_view({"source": src, "offset": 0}) as v:
        n = v.resolve("/big.bin")
        cluster = n.handle[0]
        fat_off = v.fs.bpb.BPB_RsvdSecCnt * v.fs.sector_size
    # end the chain after its 3rd cluster (both FAT copies)
    with open_view({"source": src, "offset": 0}) as v:
        c = cluster
        for _ in range(2):
            c = v._fat_get(c)
        fat_len = v.fs.fat_size * v.fs.sector_size
    for copy in range(2):
        o = fat_off + copy * fat_len + c * 4
        data[o:o + 4] = (0x0FFFFFFF).to_bytes(4, "little")
    p = tmp_path / "broken.img"
    p.write_bytes(bytes(data))
    with open_view({"source": str(p), "offset": 0}) as v:
        n = v.resolve("/big.bin")
        fh = v.open_handle(n.handle)               # the part that can be located is still readable
        cs = v.fs.cluster_size
        assert fh.read(3 * cs) == _file_bytes(images, "big.bin")[:3 * cs]
        with pytest.raises(OSError, match="chain ends early"):
            fh.read(10)


def _file_bytes(images, rel):
    with open(os.path.join(images["tree"], rel), "rb") as fh:
        return fh.read()


# ---------------------------------------------------------------- APFS (legacy engine + patches)
def test_apfs_compressed_files_and_links():
    with open_view({"source": os.path.join(APFS_DIR, "jhfs_converted.bin"), "offset": 0}) as v:
        assert v.fstype == "apfs" and v.label == "JHFS+ Converted" and v.case_insensitive
        files, dirs, links, errors = walk(v)
        small = {files[f"dir/compressed-{a}-xattr"] for a in ("zlib", "lzvn", "lzfse")}
        big = {files[f"dir/compressed-{a}-fork"] for a in ("zlib", "lzvn", "lzfse")}
        assert {v.open_handle(n.handle).read(n.size) for n in small}.__len__() == 1
        bigdata = {hashlib.sha1(v.open_handle(n.handle).read(n.size)).hexdigest() for n in big}
        assert len(bigdata) == 1 and all(n.size == 7873 for n in big)
        assert v.open_handle(files["hardlink"].handle).read(16) == v.open_handle(files["dir/file"].handle).read(16)
        assert files["hardlink"].handle[1] is not None          # (oid, sibling_id)
        assert v.readlink(links["symlink-file"].handle) == "dir/file"
        assert v.resolve("/DIR/FILE").size == 16


def test_apfs_gpt_volume_selection():
    path = os.path.join(APFS_DIR, "gptdisk.img")
    with open_view({"source": path, "offset": 4194304, "volume": "Snapshots"}) as v:
        assert v.volumes == ["Snapshots"] and v.volume == "Snapshots"
        files, _, _, _ = walk(v)
        assert files and all(k.startswith(".fseventsd/") for k in files)
    with open_view({"source": path, "offset": 4194304, "volume": "#0"}) as v:
        assert v.volume == "Snapshots"
    with pytest.raises(FsError, match="no volume called"):
        open_view({"source": path, "offset": 4194304, "volume": "Nope"})


def test_apfs_parallel_views_are_independent():
    import threading
    path = os.path.join(APFS_DIR, "jhfs_converted.bin")
    errors = []

    def worker():
        try:
            with open_view({"source": path, "offset": 0}) as v:
                for _ in range(20):
                    n = v.resolve("/dir/compressed-zlib-fork")
                    assert len(v.open_handle(n.handle).read(n.size)) == 7873
        except Exception as ex:          # pragma: no cover - reported below
            errors.append(ex)
    ts = [threading.Thread(target=worker) for _ in range(6)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and time.time() - t0 < 60


def test_unsupported_fs_message(tmp_path):
    h = bytearray(8192)
    h[3:11] = b"-FVE-FS-"
    p = tmp_path / "bitlocker.img"
    p.write_bytes(bytes(h))
    with pytest.raises(FsError, match="BitLocker"):
        open_view({"source": str(p), "offset": 0})
