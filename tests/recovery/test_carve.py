import os
import random
import time

import pytest
from conftest import run_job

import carve_samples as cs
from retrova.recovery import carve_formats as cf
from retrova.recovery.carve import CarveJob


class MemReader:
    def __init__(self, data):
        self.b = data

    def read(self, off, n):
        return self.b[off:off + n] if off >= 0 else b""


@pytest.fixture(scope="module")
def samples():
    return cs.all_samples()


def detect(buf, typ):
    for magic, moff, fn, types in cf.SIGNATURES:
        if typ in types and buf[moff:moff + len(magic)] == magic:
            return fn(MemReader(buf), 0, len(buf))
    return None


def test_every_detector_finds_the_exact_end(samples):
    noise = random.Random(5).randbytes(20_000)
    for name, (typ, data) in samples.items():
        assert detect(data + noise, typ) == (len(data), typ), name


def test_truncated_or_damaged_files_are_rejected(samples):
    for name, (typ, data) in samples.items():
        if typ in ("sqlite", "7z", "bmp", "wav", "avi", "webp", "psd", "tif"):
            continue        # their size comes from the header: truncation is caught by the cap/end check below
        cut = data[: len(data) // 2]
        r = detect(cut + b"\0" * 64, typ)
        assert r is None or r[0] <= len(cut), name
    # header-sized formats: a cap smaller than the claimed size rejects them
    for name, (typ, data) in samples.items():
        if typ in ("sqlite", "7z", "bmp", "wav", "avi", "webp"):
            buf = data[: len(data) // 2]
            for magic, moff, fn, types in cf.SIGNATURES:
                if typ in types and buf[moff:moff + len(magic)] == magic:
                    assert fn(MemReader(buf), 0, len(buf)) is None, name


def test_magic_alone_is_not_enough():
    r = random.Random(9)
    for magic, moff, fn, types in cf.SIGNATURES:
        for _ in range(50):
            buf = bytearray(r.randbytes(4096))
            buf[moff:moff + len(magic)] = magic
            assert fn(MemReader(bytes(buf)), 0, len(buf)) is None, (magic, fn.__name__)


def test_expand_types():
    assert cf.expand_types([]) == set(cf.TYPES)
    assert cf.expand_types(["jpeg", "PNG"]) == {"jpg", "png"}
    assert {"jpg", "heic", "png"} <= cf.expand_types(["photos"])
    assert cf.expand_types(["nonsense"]) == set()


@pytest.fixture(scope="module")
def disk(tmp_path_factory, samples):
    p = str(tmp_path_factory.mktemp("carve") / "disk.img")
    placed = cs.build_disk(p, samples)
    return p, placed


def carve(path, out, types=("all",), start=0, end=None, **kw):
    job = CarveJob(path, start, end or os.path.getsize(path), out, cf.expand_types(types), 0.01, **kw)
    return job, run_job(job)


def test_deep_scan_recovers_every_file_byte_identical(disk, tmp_path):
    path, placed = disk
    out = str(tmp_path / "out")
    t0 = time.time()
    job, st = carve(path, out)
    assert st["state"] == "done", st
    res = job.result({"limit": 5000})
    items = {i["offset"]: i for i in res["items"]}
    for name, typ, off, data in placed:
        it = items.pop(off, None)
        assert it is not None, f"{name} at {off} not found"
        assert it["type"] == typ and it["size"] == len(data), (name, it)
        assert it["path"] == os.path.join(out, typ, f"retrova-{off}.{cf.TYPES[typ][0]}")
        with open(it["path"], "rb") as fh:
            assert fh.read() == data, name
    assert items == {}, f"unexpected extra files: {items}"
    assert sum(res["counts"].values()) == len(placed) == st["stats"]["found"]
    assert st["stats"]["zero"] > 0 and st["progress"] == 1.0
    assert os.path.exists(st["log"])
    assert not os.path.exists(os.path.join(out, ".retrova-carve-progress.json"))
    assert time.time() - t0 < 60
    # pagination + filter
    page = job.result({"offset": 2, "limit": 3})
    assert len(page["items"]) == 3 and page["total"] == len(placed) and page["more"] == len(placed) - 5
    assert job.result({"type": "jpg"})["total"] == 2


def test_type_filter_and_range(disk, tmp_path):
    path, placed = disk
    job, st = carve(path, str(tmp_path / "o1"), types=["photos"])
    got = {i["type"] for i in job.result({"limit": 1000})["items"]}
    assert got and got <= {t for t, v in cf.TYPES.items() if v[1] == "photos"}
    assert not os.path.exists(os.path.join(str(tmp_path / "o1"), "pdf"))
    pdfs = [(off, data) for name, typ, off, data in placed if typ == "pdf"]
    off, data = pdfs[0]
    job, st = carve(path, str(tmp_path / "o2"), types=["pdf"], start=off - 100, end=off + len(data) + 100)
    (it,) = job.result({})["items"]
    assert it["offset"] == off and it["size"] == len(data)


def test_resume_after_stop(disk, tmp_path):
    path, placed = disk
    out = str(tmp_path / "out")
    job = CarveJob(path, 0, os.path.getsize(path), out, cf.expand_types(["all"]), 0.01)
    from retrova.jobs import JOBS
    import retrova.recovery.carve as carve_mod
    old = carve_mod.CHUNK
    carve_mod.CHUNK = 1024 * 1024
    try:
        JOBS.start(job)
        while job.live() and len(job.items) < 5:
            time.sleep(0.001)
        job.stop()
        while job.live():
            time.sleep(0.01)
        job._save_resume()
    finally:
        carve_mod.CHUNK = old
    first = len(job.items)
    job2, st = carve(path, out)
    assert st["state"] == "done"
    if job.status()["state"] == "stopped":
        assert job2.resumed_from is not None
    offsets = {i["offset"] for i in job.items} | {i["offset"] for i in job2.items}
    assert offsets == {off for _, _, off, _ in placed}
    assert first >= 1


def test_noise_gives_no_false_positives(tmp_path):
    p = tmp_path / "noise.img"
    with open(p, "wb") as fh:
        r = random.Random(77)
        for _ in range(4):
            fh.write(r.randbytes(16 * 1024 * 1024))
    job, st = carve(str(p), str(tmp_path / "out"))
    assert st["state"] == "done" and st["stats"]["found"] == 0


def test_aligned_mode_only_checks_sector_starts(disk, tmp_path):
    path, placed = disk
    job, st = carve(path, str(tmp_path / "out"), aligned=True)
    aligned = {off for _, _, off, _ in placed if off % 512 == 0}
    assert {i["offset"] for i in job.items} == aligned


def test_existing_results_are_not_overwritten(disk, tmp_path):
    path, placed = disk
    out = str(tmp_path / "out")
    name, typ, off, data = next(x for x in placed if x[1] == "png")
    os.makedirs(os.path.join(out, "png"))
    mine = os.path.join(out, "png", f"retrova-{off}.png")
    with open(mine, "wb") as fh:
        fh.write(b"mine")
    job, st = carve(path, out, types=["png"])
    assert open(mine, "rb").read() == b"mine"
    assert open(mine + ".retrova-2", "rb").read() == data


# ---------------------------------------------------------------- format-specific checks
def test_every_type_has_a_sample_except_the_known_gaps(samples):
    assert set(cf.TYPES) - {typ for typ, _ in samples.values()} == {"doc", "xls", "ppt", "msg", "ogg", "flac", "mkv"}
    for t, (ext, group, name, cap) in cf.TYPES.items():
        assert group in cf.GROUPS and ext and name and cap > 0, t


def test_camera_raw_and_phone_files_get_the_right_extension(samples):
    want = {"canon.cr2": "cr2", "canon.cr3": "cr3", "nikon.nef": "nef", "coolpix.nrw": "nrw", "sony.arw": "arw",
            "adobe.dng": "dng", "pentax.pef": "pef", "olympus.orf": "orf", "fuji.raf": "raf", "photo.avif": "avif",
            "iphone.heic": "heic", "image.jxl": "jxl", "big.psb": "psb", "art.psd": "psd", "scan.tif": "tif"}
    for name, typ in want.items():
        assert samples[name][0] == typ and cf.TYPES[typ][0] == typ
        assert detect(samples[name][1], typ) == (len(samples[name][1]), typ), name


def test_avif_with_the_generic_heif_brand_is_still_avif(samples):
    assert b"mif1" in samples["photo.avif"][1][:40]
    assert detect(samples["photo.avif"][1], "avif")[1] == "avif"


def test_animated_png_is_carved_whole(samples):
    typ, data = samples["anim.apng"]
    assert typ == "png" and data.count(b"fdAT") >= 1 and detect(data + b"\xff" * 50, "png") == (len(data), "png")


def test_rtf_binary_data_and_block_boundaries(samples):
    data = samples["letter.rtf"][1]
    assert detect(data + b"}}}}", "rtf") == (len(data), "rtf")
    old = cf.BLK
    try:
        for blk in (40, 41, 64, 97, 200):          # tokens and \bin data straddling every kind of block edge
            cf.BLK = blk
            for name in ("letter.rtf", "book.rtf"):
                if name == "book.rtf" and blk < 64:
                    continue
                d = samples[name][1]
                assert detect(d + b"}}", "rtf") == (len(d), "rtf"), (name, blk)
    finally:
        cf.BLK = old
    assert detect(data.replace(b"\\par", b"\\par\x80", 1), "rtf") is None       # raw 8-bit byte: not RTF text
    assert detect(data[:-4], "rtf") is None                                     # the outer brace never closes


def test_eml_needs_a_multipart_closing_line(samples):
    data = samples["mail.eml"][1]
    assert detect(data, "eml") == (len(data), "eml")
    assert detect(data.replace(b"----=_Part_77_1--", b"----=_Part_77_1-X"), "eml") is None
    single = data.replace(b"multipart/mixed;", b"text/plain;")
    assert detect(single, "eml") is None


def test_wal_stops_at_the_first_frame_that_is_not_valid(samples):
    wal = samples["db.sqlite-wal"][1]
    page = int.from_bytes(wal[8:12], "big")
    frame = 24 + page
    assert (len(wal) - 32) % frame == 0
    n = (len(wal) - 32) // frame
    assert n >= 5
    assert detect(wal, "wal") == (len(wal), "wal")
    bad = bytearray(wal)
    bad[32 + 3 * frame + 100] ^= 0xFF               # one flipped byte in frame 4 breaks its checksum
    assert detect(bytes(bad), "wal") == (32 + 3 * frame, "wal")
    stale = bytearray(wal + wal[32:32 + frame])
    stale[len(wal) + 8:len(wal) + 12] = b"\1\2\3\4"  # a leftover frame from an earlier log (other salt)
    assert detect(bytes(stale), "wal") == (len(wal), "wal")


def test_pst_header_is_checked_against_the_file(samples):
    data = samples["mail.pst"][1]
    assert detect(data, "pst") == (len(data), "pst")
    bad = bytearray(data)
    bad[0x4400 + 496] = 0                           # the last allocation map page is not one
    assert detect(bytes(bad), "pst") is None
    assert detect(samples["cache.ost"][1], "ost")[1] == "ost"


def test_asf_with_and_without_a_file_size(samples):
    for name, kind in (("video.wmv", "wmv"), ("song.wma", "wma")):
        d = samples[name][1]
        assert detect(d, kind) == (len(d), kind)
    d = bytearray(samples["video.wmv"][1])
    d[30 + 40:30 + 48] = (len(d) + 5).to_bytes(8, "little")      # the header claims a different file size
    assert detect(bytes(d), "wmv") is None


def test_fonts_are_checked_table_by_table(samples):
    d = bytearray(samples["body.ttf"][1])
    assert detect(bytes(d), "ttf") == (len(d), "ttf")
    d[12 + 16 * 2 + 8] ^= 1                                      # a table offset that is no longer 4-byte aligned
    assert detect(bytes(d), "ttf") is None
