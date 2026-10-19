import os
import random

import pytest
from conftest import APFS_DIR, need

from retrova.recovery.partitions import best_part, container_format, detect_fs, scan
from retrova.recovery.source import ReadOnlySource, device_size


def test_readonly_source_reads_any_offset(tmp_path):
    data = random.Random(1).randbytes(100_000)
    p = tmp_path / "img.bin"
    p.write_bytes(data)
    s = ReadOnlySource(str(p))
    try:
        assert not s.writable()
        for off, n in ((0, 10), (1, 4095), (4095, 2), (12345, 54321), (99_990, 100)):
            assert s.pread(off, n) == data[off:off + n]
        s.seek(777)
        assert s.read(10) == data[777:787]
        assert s.size == len(data)
        # the handle really is read-only
        if isinstance(s.h, int):
            with pytest.raises(OSError):
                os.write(s.h, b"x")
    finally:
        s.close()
    assert p.read_bytes() == data


def test_readonly_source_window(tmp_path):
    data = random.Random(2).randbytes(50_000)
    p = tmp_path / "img.bin"
    p.write_bytes(data)
    with ReadOnlySource(str(p), base=1000, length=5000) as s:
        assert s.pread(0, 10) == data[1000:1010]
        assert s.pread(4990, 100) == data[5990:6000]       # short at the end of the window
        assert s.pread(5000, 10) == b""
        assert s.size == 5000
    assert device_size(str(p)) == 50_000


@pytest.mark.parametrize("key,fs,label", [("fat32", "fat32", "RETROVAFAT"), ("fat16", "fat16", "F16"),
                                          ("fat12", "fat12", "F12"), ("ext4", "ext4", "retrovaext"),
                                          ("xfs", "xfs", "retrovaxfs"), ("btrfs", "btrfs", "retrovabtr"),
                                          ("ntfs", "ntfs", "RetrovaNTFS"), ("exfat", "exfat", "RETROVAEX")])
def test_bare_filesystems_detected(images, key, fs, label):
    lay = scan(need(images, key))
    assert lay["scheme"] == "none"
    (p,) = lay["parts"]
    assert (p["offset"], p["fs"], p["label"], p["supported"]) == (0, fs, label, True)
    assert p["size"] > 0


def test_gpt_two_partitions(images):
    path = need(images, "gpt")
    lay = scan(path)
    assert lay["scheme"] == "gpt"
    got = [(p["offset"], p["fs"]) for p in lay["parts"]]
    assert got == [tuple(x) for x in images["gpt_parts"]]
    assert lay["parts"][0]["name"] == "Photos"
    assert best_part(lay["parts"])["fs"] == "ext4"        # the biggest readable one


def test_gpt_backup_header_used_when_primary_damaged(images, tmp_path):
    src = need(images, "gpt")
    p = tmp_path / "damaged.img"
    data = bytearray(open(src, "rb").read())
    data[512:520] = b"XXXXXXXX"                        # wipe the primary header signature
    p.write_bytes(bytes(data))
    lay = scan(str(p))
    assert lay["scheme"] == "gpt"
    assert [(x["offset"], x["fs"]) for x in lay["parts"]] == [tuple(x) for x in images["gpt_parts"]]
    assert any("backup" in n for n in lay["notes"])


def test_mbr_with_extended_and_logical(images):
    lay = scan(need(images, "mbr"))
    assert lay["scheme"] == "mbr"
    got = [(p["offset"], p["fs"]) for p in lay["parts"]]
    assert got == [tuple(x) for x in images["mbr_parts"]]
    assert [bool(p.get("logical")) for p in lay["parts"]] == [False, True, True]


def test_apfs_images():
    lay = scan(os.path.join(APFS_DIR, "gptdisk.img"))
    assert lay["scheme"] == "gpt"
    apfs = [p for p in lay["parts"] if p["fs"] == "apfs"]
    assert len(apfs) == 1 and apfs[0]["offset"] == 4194304 and apfs[0]["volumes"] == ["Snapshots"]
    assert best_part(lay["parts"]) is apfs[0]
    lay = scan(os.path.join(APFS_DIR, "jhfs_converted.bin"))
    assert lay["scheme"] == "none" and lay["parts"][0]["fs"] == "apfs"


def test_unknown_and_unsupported(tmp_path):
    p = tmp_path / "noise.bin"
    p.write_bytes(random.Random(3).randbytes(200_000))
    lay = scan(str(p))
    assert lay["parts"] == [] and lay["scheme"] == "unknown"
    h = bytearray(8192)
    h[3:11] = b"ReFS\0\0\0\0"
    q = tmp_path / "refs.img"
    q.write_bytes(bytes(h))
    lay = scan(str(q))
    assert lay["parts"][0]["fs"] == "refs" and not lay["parts"][0]["supported"]
    assert "ReFS" in lay["parts"][0]["why"]


def test_container_formats(tmp_path):
    p = tmp_path / "x.dmg"
    p.write_bytes(b"\0" * 4096 + b"koly" + b"\0" * 508)
    assert "dmg" in container_format(str(p))
    p.write_bytes(b"QFI\xfb" + b"\0" * 1000)
    assert "qcow2" in container_format(str(p))
    p.write_bytes(b"\0" * 2000)
    assert container_format(str(p)) is None


def test_detect_fs_quick(images):
    with ReadOnlySource(need(images, "gpt")) as s:
        r = detect_fs(s, images["gpt_parts"][1][0], quick=True)
    assert r["fs"] == "ext4"
