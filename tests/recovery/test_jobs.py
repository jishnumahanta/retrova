import csv
import io
import os

import pytest
from conftest import expected_for, need, read_tree, run_job

from retrova.recovery.copyjob import CopyJob
from retrova.recovery.imagejob import ImageJob
from retrova.recovery.regenjob import RegenJob
from retrova.recovery.verifyjob import VerifyJob, read_manifest


def spec(path, offset=0, fs=None):
    return {"source": path, "offset": offset, "size": None, "fs": fs, "volume": None}


def copy(sp, out, paths=("/",), **kw):
    return run_job(CopyJob(sp, kw.pop("label", "Test"), list(paths), out, kw.pop("patterns", []),
                           kw.pop("reserve_gb", 0.01), kw.pop("workers", 3), kw.pop("excluded", []),
                           kw.pop("skipped_info", [])))


def verify(sp, out, paths=("/",), mode="full", **kw):
    job = VerifyJob(sp, "Test", list(paths), out, [], kw.pop("excluded", []), True, mode, 3,
                    kw.pop("resume", True))
    return job, run_job(job)


@pytest.mark.parametrize("key", ["fat32", "exfat", "ext4", "ntfs", "btrfs", "xfs", "fat16"])
def test_copy_whole_volume_is_byte_identical(images, tree, tmp_path, key):
    sp = spec(need(images, key))
    out = str(tmp_path / "out")
    st = copy(sp, out)
    assert st["state"] == "done", st
    assert st["failed"] == 0 and st["errors"] == []
    roots = [d for d in os.listdir(out) if not d.startswith("_")]
    assert len(roots) == 1                                     # named after the volume label
    got = read_tree(os.path.join(out, roots[0]))
    want = expected_for(tree, key, images.get("ntfs_mode"))
    for rel, data in want.items():
        assert got.get(rel) == data, rel
    assert st["stats"]["files_total"] == st["copied"] and st["progress"] == 1.0
    assert os.path.exists(st["manifest"]) and os.path.exists(st["error_log"])
    if key in ("ext4", "btrfs"):
        link = os.path.join(out, roots[0], "link.txt")
        assert os.path.islink(link) and os.readlink(link) == "hello.txt"
        assert int(os.stat(os.path.join(out, roots[0], "hello.txt")).st_mtime) == 1_600_000_000
    # second run: everything is already there
    st2 = copy(sp, out)
    assert st2["copied"] == 0 and st2["skipped"] == st["copied"]


def test_copy_selection_exclusions_and_patterns(images, tree, tmp_path):
    sp = spec(need(images, "ext4"))
    out = str(tmp_path / "out")
    st = copy(sp, out, paths=["/docs", "/photos", "/hello.txt"], excluded=["/docs/sub/deeper"],
              patterns=["*.png"], skipped_info=[{"path": "/docs/sub/deeper", "what": "test", "regen": "none"}])
    assert st["state"] == "done"
    got = read_tree(out)
    assert got["hello.txt"] == tree["hello.txt"]
    assert got["docs/report.pdf"] == tree["docs/report.pdf"]
    assert "docs/sub/deeper/data.bin" not in got
    assert not any(k.endswith(".png") for k in got)
    assert st["skip_report"] and "docs/sub/deeper" in open(st["skip_report"]).read()


def test_copy_never_overwrites(images, tree, tmp_path):
    sp = spec(need(images, "fat32"))
    out = str(tmp_path / "out")
    os.makedirs(out)
    with open(os.path.join(out, "hello.txt"), "wb") as fh:
        fh.write(b"my own different file")
    st = copy(sp, out, paths=["/hello.txt"])
    assert st["copied"] == 1
    assert open(os.path.join(out, "hello.txt"), "rb").read() == b"my own different file"
    assert open(os.path.join(out, "hello.txt.retrova-2"), "rb").read() == tree["hello.txt"]
    assert not [f for f in os.listdir(out) if f.endswith(".part")]


def test_copy_stops_at_reserve(images, tmp_path):
    st = copy(spec(need(images, "ext4")), str(tmp_path / "out"), reserve_gb=1e9)
    assert st["state"] == "stopped" and "keep free" in st["message"]


def test_copy_missing_path_is_reported(images, tmp_path):
    st = copy(spec(need(images, "ext4")), str(tmp_path / "out"), paths=["/nope"])
    assert st["state"] == "done" and st["failed"] == 1 and "Not found" in st["errors"][0]["msg"]


def test_verify_damage_and_repair(images, tree, tmp_path):
    sp = spec(need(images, "ext4"))
    out = str(tmp_path / "out")
    assert copy(sp, out)["state"] == "done"
    root = os.path.join(out, "retrovaext")
    job, st = verify(sp, out)
    assert st["state"] == "done" and st["problems"] == 0, job.result({})
    assert st["counts"]["OK"] >= 15
    man = read_manifest(os.path.join(out, "_recovery-verify", "manifest-sha256.txt"))
    assert "retrovaext/big.bin" in man
    # damage the copy: missing, truncated, zero-filled, changed bytes, extra file
    os.remove(os.path.join(root, "docs", "report.pdf"))
    with open(os.path.join(root, "photos", "IMG_0001.JPG"), "r+b") as fh:
        fh.truncate(1000)
    with open(os.path.join(root, "big.bin"), "r+b") as fh:
        fh.seek(5 * 1024 * 1024)
        fh.write(b"\0" * 300_000)
    with open(os.path.join(root, "docs", "sub", "x.txt"), "r+b") as fh:
        fh.seek(10)
        fh.write(b"Y")
    with open(os.path.join(root, "extra.txt"), "wb") as fh:
        fh.write(b"only in the copy")
    for mode in ("quick", "sample", "full"):
        job, st = verify(sp, out, mode=mode, resume=False)
        codes = {i["key"]: i["status"] for i in job.result({"limit": 1000})["items"]}
        assert codes["retrovaext/docs/report.pdf"] == "MISSING"
        assert codes["retrovaext/photos/IMG_0001.JPG"] == "SIZE"
        assert codes["retrovaext/extra.txt"] == "EXTRA"
        if mode != "quick":
            assert codes["retrovaext/big.bin"] == "CONTENT" or mode == "sample" and "retrovaext/big.bin" not in codes
        if mode == "full":
            assert codes["retrovaext/big.bin"] == "CONTENT"
            assert codes["retrovaext/docs/sub/x.txt"] == "CONTENT"
            assert st["fixable"] == 4
            zero_note = next(i for i in job.result({})["items"] if i["key"] == "retrovaext/big.bin")["detail"]
            assert "zeros" in zero_note
    # filters + csv
    assert job.result({"status": "MISSING"})["total"] == 1
    assert job.result({"q": "x.txt"})["total"] == 1
    rows = list(csv.reader(io.StringIO(job.csv())))
    assert rows[0][0] == "result" and any(r and r[1] == "CONTENT" for r in rows)
    # repair from the original, then everything matches again
    tasks, carry = job.repair_plan()
    rep = VerifyJob(sp, "Test", job.paths, out, [], [], True, "full", 2, False, repair_tasks=tasks, carry=carry)
    rep.mappings, rep.roots = list(job.mappings), list(job.roots)
    st = run_job(rep)
    assert st["state"] == "done" and st["repaired"] == 4, rep.result({})
    assert read_tree(root)["big.bin"] == tree["big.bin"]
    assert read_tree(root)["docs/report.pdf"] == tree["docs/report.pdf"]
    remaining = {i["status"] for i in rep.result({})["items"]}
    assert remaining == {"EXTRA"}                              # carried over, never deleted
    job, st = verify(sp, out)
    assert st["problems"] == 0


def test_verify_resume_uses_cache(images, tmp_path):
    sp = spec(need(images, "fat32"))
    out = str(tmp_path / "out")
    copy(sp, out)
    verify(sp, out)
    job, st = verify(sp, out)
    assert st["cached"] == st["total_files"] > 0 and st["problems"] == 0


def test_verify_source_unreadable_marks_src_bad(images, tree, tmp_path):
    """A file whose data can't be read from the source is SRC_BAD, not a copy problem."""
    src = need(images, "fat32")
    sp = spec(src)
    out = str(tmp_path / "out")
    copy(sp, out, paths=["/big.bin"])
    from retrova.recovery.fsview import open_view
    with open_view(sp) as v:
        n = v.resolve("/big.bin")
        fat_off = v.fs.bpb.BPB_RsvdSecCnt * v.fs.sector_size
        fat_len = v.fs.fat_size * v.fs.sector_size
        c2 = v._fat_get(v._fat_get(n.handle[0]))
    data = bytearray(open(src, "rb").read())
    for k in range(2):
        o = fat_off + k * fat_len + c2 * 4
        data[o:o + 4] = (0x0FFFFFFF).to_bytes(4, "little")
    broken = tmp_path / "broken.img"
    broken.write_bytes(bytes(data))
    job, st = verify(spec(str(broken)), out, paths=["/big.bin"])
    (issue,) = job.result({})["items"]
    assert issue["status"] == "SRC_BAD" and "Everything readable matches" in issue["detail"]
    # the copy job keeps what can be read as .INCOMPLETE
    out2 = str(tmp_path / "out2")
    st = copy(spec(str(broken)), out2, paths=["/big.bin"])
    assert st["failed"] == 1 and "chain ends early" in st["errors"][0]["msg"]
    part = open(os.path.join(out2, "big.bin.INCOMPLETE"), "rb").read()
    assert len(part) > 0 and tree["big.bin"].startswith(part)
    assert not os.path.exists(os.path.join(out2, "big.bin"))
    # Verify then sees the partial copy (MISSING with a note) and Repair can't do better than the source
    job, st = verify(spec(str(broken)), out2, paths=["/big.bin"])
    (issue,) = job.result({})["items"]
    assert issue["status"] in ("MISSING", "SRC_BAD") and ".INCOMPLETE" in issue["detail"]


def test_image_job_and_resume(images, tmp_path):
    src = need(images, "gpt")
    off, fs = images["gpt_parts"][0]
    size = os.path.getsize(images["fat32"])
    out = str(tmp_path / "part.img")
    job = ImageJob(src, off, size, out, 0.01, "Photos")
    job.CHUNK = 1024 * 1024
    from retrova.jobs import JOBS
    JOBS.start(job)
    import time
    while job.pos < 4 * 1024 * 1024 and job.live():
        time.sleep(0.005)
    job.stop()
    while job.live():
        time.sleep(0.02)
    st = job.status()
    assert st["state"] in ("stopped", "done")
    if st["state"] == "stopped":
        assert os.path.exists(out + ".retrova-progress")
    job2 = ImageJob(src, off, size, out, 0.01, "Photos")
    st2 = run_job(job2)
    assert st2["state"] == "done" and st2["stats"]["bytes_done"] == size
    assert open(out, "rb").read() == open(images["fat32"], "rb").read()
    assert not os.path.exists(out + ".retrova-progress")
    # a finished image is never overwritten
    job3 = ImageJob(src, off, size, out, 0.01, "Photos")
    st3 = run_job(job3)
    assert st3["state"] == "error" and "already exists" in st3["error"]


def test_image_whole_disk(images, tmp_path):
    src = need(images, "mbr")
    out = str(tmp_path / "disk.img")
    st = run_job(ImageJob(src, 0, os.path.getsize(src), out, 0.01))
    assert st["state"] == "done" and st["zero"] > 0
    assert open(out, "rb").read() == open(src, "rb").read()


def test_regen_job_finds_regenerable_folders(images):
    st = run_job(RegenJob(spec(need(images, "ext4")), ["/"]))
    assert st["state"] == "done"
    from retrova.jobs import JOBS
    found = {f["path"]: f for f in JOBS.get(st["id"]).result({})["found"]}
    assert found["/project/node_modules"]["confidence"] == "safe"
    assert found["/project/__pycache__"]["confidence"] == "safe"
    assert "/docs" not in found
