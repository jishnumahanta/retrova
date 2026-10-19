"""Deleted files with their names and folders: FAT12/16/32, NTFS and exFAT images built in undelete_images.py."""
import os
import pickle
import shutil
from urllib.parse import quote

import pytest
import undelete_images as U
from conftest import read_tree, run_job

from retrova.recovery.copyjob import CopyJob
from retrova.recovery.fsview import open_view
from retrova.recovery.undelete import DELETED_ROOT, LOST
from retrova.recovery.verifyjob import VerifyJob

ROOT = "/" + DELETED_ROOT


def spec(path):
    return {"source": path, "offset": 0, "size": None, "fs": None, "volume": None}


# what each image should give back: path under "Deleted files" -> bytes (None = only the size is checked)
FAT_EXPECT = {
    "docs/Quarterly Report 2024.txt": U.FAT_DELETED["docs/Quarterly Report 2024.txt"],
    "photos/Holiday Photo.jpg": U.FAT_DELETED["photos/Holiday Photo.jpg"],
    "_HORT.TXT": U.FAT_DELETED["SHORT.TXT"],
    "_iny.txt": U.FAT_DELETED["tiny.txt"],
    "_lddir/_nner/deep file.bin": U.FAT_DELETED["olddir/inner/deep file.bin"],
    "_mpty.txt": b"",
    "_ictim.bin": None,                              # another file now sits on its clusters
}
FAT_LEVELS = {"_iny.txt": "good", "_mpty.txt": "good", "_ictim.bin": "overwritten", "_HORT.TXT": "incomplete",
              "docs/Quarterly Report 2024.txt": "incomplete", "photos/Holiday Photo.jpg": "incomplete"}

NTFS_EXPECT = {
    "readme.txt": U.NTFS_FILES["readme.txt"],
    "movie.bin": U.NTFS_FILES["movie.bin"],
    "report final.docx": U.NTFS_FILES["report final.docx"],
    "dirA/inner.txt": U.NTFS_FILES["inner.txt"],
    f"{LOST}/Folder {{orphan}}/orphan.txt": U.NTFS_FILES["orphan.txt"],
    f"{LOST}/Folder {{reused}}/reused.txt": U.NTFS_FILES["reused.txt"],
    "dirB/kid.txt": U.NTFS_FILES["kid.txt"],
    "taken.bin": None,
}

EXFAT_EXPECT = {
    "Holiday Video.bin": U.EXFAT_DELETED["Holiday Video.bin"],
    "tiny.txt": U.EXFAT_DELETED["tiny.txt"],
    "docs/Meeting notes ✓.txt": U.EXFAT_DELETED["docs/Meeting notes ✓.txt"],
    "olddir/inner/deep file.bin": U.EXFAT_DELETED["olddir/inner/deep file.bin"],
    "frag-chain-kept.bin": U.EXFAT_FRAG_KEPT[1],
    "frag-chain-lost.bin": None,                     # fragmented and the chain is gone: not recoverable exactly
    "taken.bin": None,
    "empty.txt": b"",
}
EXFAT_LEVELS = {"Holiday Video.bin": "good", "tiny.txt": "good", "docs/Meeting notes ✓.txt": "good",
                "olddir/inner/deep file.bin": "good", "frag-chain-kept.bin": "good", "empty.txt": "good",
                "frag-chain-lost.bin": "incomplete", "taken.bin": "overwritten"}


@pytest.fixture(scope="module")
def imgs(tmp_path_factory):
    if not all(shutil.which(t) for t in ("mkfs.vfat", "mcopy", "mdel", "mkntfs", "ntfscp", "mkfs.exfat")):
        pytest.skip("mkfs tools (dosfstools, mtools, ntfs-3g, exfatprogs) are not installed")
    d = str(tmp_path_factory.mktemp("undelete"))
    return {"fat32": U.build_fat(os.path.join(d, "fat32.img"), 32, 48000),
            "fat16": U.build_fat(os.path.join(d, "fat16.img"), 16, 24000),
            "fat12": U.build_fat(os.path.join(d, "fat12.img"), 12, 1440),
            "ntfs": U.build_ntfs(os.path.join(d, "ntfs.img")),
            "exfat": U.build_exfat(os.path.join(d, "exfat.img"))}


def walk(view, node, path=""):
    """-> {relative path: Node} of every file below node."""
    out = {}
    for c in view.iterdir(node):
        p = f"{path}/{c.name}".lstrip("/")
        if c.kind == "dir":
            out.update(walk(view, c, p))
        else:
            out[p] = c
    return out


def deleted_files(view):
    return walk(view, view.resolve(ROOT))


def live_files(view):
    return walk(view, view.root())


def ntfs_expect(path):
    """NTFS_EXPECT with the numbers of the folder records the two unplaceable files pointed at."""
    ed = U.NtfsEditor(path)
    try:
        names = {k: ed.info(f"{k}.txt").parent & 0xFFFFFFFFFFFF for k in ("orphan", "reused")}
    finally:
        ed.close()
    return {k.format(**names): v for k, v in NTFS_EXPECT.items()}


def check(view, want, levels=None):
    got = deleted_files(view)
    assert sorted(got) == sorted(want)
    for rel, data in want.items():
        n = got[rel]
        if data is not None:
            assert n.size == len(data), rel
            assert view.open_handle(n.handle).read(n.size) == data, rel
        pickle.dumps(n.handle)                      # worker processes get handles through a queue
        assert n.rec and n.rec[0] in ("good", "overwritten", "incomplete")
        if levels and rel in levels:
            assert n.rec[0] == levels[rel], (rel, n.rec)
    return got


@pytest.mark.parametrize("key", ["fat32", "fat16", "fat12"])
def test_fat_names_folders_and_exact_bytes(imgs, key):
    with open_view(spec(imgs[key])) as v:
        got = check(v, FAT_EXPECT, FAT_LEVELS)
        assert "lost when it was deleted" in got["_HORT.TXT"].rec[1]          # 8.3 entry: first letter is a guess
        assert "lost when it was deleted" not in got["docs/Quarterly Report 2024.txt"].rec[1]
        assert "split into pieces" in got["photos/Holiday Photo.jpg"].rec[1]
        # live files are not "deleted", and the plain listing is unchanged
        live = live_files(v)
        assert sorted(live) == sorted(["squatter.bin", *U.FAT_LIVE])
        assert not set(live) & set(got)
        assert v.open_handle(live["docs/notes.txt"].handle).read(3000) == U.FAT_LIVE["docs/notes.txt"]
        assert DELETED_ROOT not in [c.name for c in v.iterdir(v.root())]


def test_fat_long_name_gives_back_the_deleted_first_letter(imgs):
    with open_view(spec(imgs["fat32"])) as v:
        assert v.resolve(f"{ROOT}/DOCS/quarterly report 2024.TXT").kind == "file"      # case-insensitive
        with pytest.raises(FileNotFoundError):
            v.resolve(f"{ROOT}/docs/nope.txt")


def test_ntfs_names_paths_and_exact_bytes(imgs):
    want = ntfs_expect(imgs["ntfs"])
    with open_view(spec(imgs["ntfs"])) as v:
        got = check(v, want, {"readme.txt": "good", "movie.bin": "good", "taken.bin": "overwritten",
                              "dirA/inner.txt": "good", "dirB/kid.txt": "good"})
        assert got["readme.txt"].size == 300                             # small file stored inside its record
        index = v.deleted_index()
        assert not index.notes
        assert index.lookup([LOST])[0] == "dir"
        # live records are never listed as deleted
        assert "live.txt" not in got and not any(p.endswith("live.txt") for p in got)


def test_ntfs_deleted_folder_with_raised_sequence_number_is_still_followed(imgs):
    with open_view(spec(imgs["ntfs"])) as v:
        assert v.resolve(f"{ROOT}/dirB/kid.txt").kind == "file"          # dirB is deleted and its sequence number is +1


def test_ntfs_record_that_came_back_to_life_is_refused(imgs, tmp_path):
    path = str(tmp_path / "n.img")
    shutil.copy(imgs["ntfs"], path)
    with open_view(spec(path)) as v:
        node = v.resolve(f"{ROOT}/movie.bin")
        ed = U.NtfsEditor(path)
        try:
            ed.edit("movie.bin", lambda rec, _: rec.__setitem__(0x16, rec[0x16] | 1))      # in use again
        finally:
            ed.close()
        with pytest.raises(IOError, match="in use again"):
            v.open_handle(node.handle)


def test_exfat_entry_sets_names_and_exact_bytes(imgs):
    with open_view(spec(imgs["exfat"])) as v:
        got = check(v, EXFAT_EXPECT, EXFAT_LEVELS)
        assert got["frag-chain-lost.bin"].size == len(U.EXFAT_FRAG_LOST[1])
        live = live_files(v)
        assert sorted(live) == sorted(U.EXFAT_LIVE)
        assert not set(live) & set(got)
        for rel, data in U.EXFAT_LIVE.items():
            assert v.open_handle(live[rel].handle).read(len(data)) == data


@pytest.mark.parametrize("key", ["fat32", "ntfs", "exfat"])
def test_handles_reopen_in_a_new_view(imgs, key):
    """What a worker process does: open the spec again and find the file by handle (no index needed)."""
    sp = spec(imgs[key])
    with open_view(sp) as a:
        rel, node = next((r, n) for r, n in sorted(deleted_files(a).items()) if n.size > 100 and n.rec[0] != "overwritten")
        h = pickle.loads(pickle.dumps(node.handle))
        want = a.open_handle(h).read(node.size)
    with open_view(sp, overlay=False) as b:
        assert not hasattr(b, "deleted_index")
    with open_view(sp) as c:
        assert c.open_handle(h).read(node.size) == want


# ---------------------------------------------------------------- copy and Verify go through the same views
def copy(sp, out, paths):
    return run_job(CopyJob(sp, "Test", list(paths), out, [], 0.01, 3, [], []))


def expected_tree(key, imgs):
    if key == "ntfs":
        return {k: v for k, v in ntfs_expect(imgs["ntfs"]).items() if v is not None}
    return {k: v for k, v in {"fat32": FAT_EXPECT, "exfat": EXFAT_EXPECT}[key].items() if v is not None}


@pytest.mark.parametrize("key", ["fat32", "ntfs", "exfat"])
def test_copy_and_verify_deleted_files(imgs, tmp_path, key):
    sp = spec(imgs[key])
    out = str(tmp_path / "out")
    st = copy(sp, out, [ROOT])
    assert st["state"] == "done" and st["failed"] == 0, st
    want = expected_tree(key, imgs)
    got = read_tree(os.path.join(out, DELETED_ROOT))
    for rel, data in want.items():
        assert got.get(rel) == data, rel
    assert st["copied"] == len(got)
    job = VerifyJob(sp, "Test", [ROOT], out, [], [], True, "full", 3, False)
    vst = run_job(job)
    assert vst["state"] == "done" and vst["problems"] == 0, job.result({})
    # damage one copied file: Verify compares against what the source reads
    victim = next(iter(want))
    with open(os.path.join(out, DELETED_ROOT, *victim.split("/")), "r+b") as fh:
        fh.write(b"\xff")
    job = VerifyJob(sp, "Test", [ROOT], out, [], [], True, "full", 3, False)
    run_job(job)
    assert [i["status"] for i in job.result({})["items"]] == ["CONTENT"]


def test_copy_one_deleted_file_never_overwrites(imgs, tmp_path):
    out = str(tmp_path / "out")
    os.makedirs(out)
    with open(os.path.join(out, "Quarterly Report 2024.txt"), "wb") as fh:
        fh.write(b"my own different file")
    st = copy(spec(imgs["fat16"]), out, [f"{ROOT}/docs/Quarterly Report 2024.txt"])
    assert st["copied"] == 1
    assert open(os.path.join(out, "Quarterly Report 2024.txt"), "rb").read() == b"my own different file"
    assert open(os.path.join(out, "Quarterly Report 2024.txt.retrova-2"), "rb").read() == FAT_EXPECT[
        "docs/Quarterly Report 2024.txt"]


def test_copying_a_live_folder_does_not_pull_in_deleted_files(imgs, tmp_path):
    out = str(tmp_path / "out")
    st = copy(spec(imgs["fat32"]), out, ["/"])
    assert st["state"] == "done"
    assert sorted(read_tree(os.path.join(out, "UNDEL"))) == sorted(["squatter.bin", *U.FAT_LIVE])


# ---------------------------------------------------------------- HTTP: browse toggle, badges, preview
def test_browse_api_shows_deleted_only_when_asked(server, imgs):
    o = server.ok("/api/recover/open", {"source": imgs["fat32"]})
    sid = o["sid"]
    assert o["can_list_deleted"] is True
    plain = server.ok(f"/api/recover/ls?sid={sid}&path=/")
    assert DELETED_ROOT not in [e["name"] for e in plain["entries"]]
    assert not any(e.get("deleted") for e in plain["entries"])
    root = server.ok(f"/api/recover/ls?sid={sid}&path=/&deleted=1")
    by = {e["name"]: e for e in root["entries"]}
    assert by[DELETED_ROOT]["kind"] == "dir" and by[DELETED_ROOT]["deleted"] and by[DELETED_ROOT]["path"] == ROOT
    assert by["_HORT.TXT"]["deleted"] and by["_HORT.TXT"]["path"] == f"{ROOT}/_HORT.TXT"
    assert by["_HORT.TXT"]["recover"] == "incomplete" and by["_HORT.TXT"]["recover_note"]
    assert not by["squatter.bin"].get("deleted")
    docs = server.ok(f"/api/recover/ls?sid={sid}&path=/docs&deleted=1")
    by = {e["name"]: e for e in docs["entries"]}
    assert by["Quarterly Report 2024.txt"]["path"] == f"{ROOT}/docs/Quarterly Report 2024.txt"
    assert not by["notes.txt"].get("deleted")
    # the virtual folder itself, with and without the toggle
    inside = server.ok(f"/api/recover/ls?sid={sid}&path={quote(ROOT)}/docs")
    assert [e["name"] for e in inside["entries"]] == ["Quarterly Report 2024.txt"]
    assert inside["entries"][0]["deleted"] and inside["entries"][0]["size"] == 20000
    # preview streams the recovered bytes (with a range)
    path = f"{ROOT}/docs/Quarterly Report 2024.txt"
    st, hdr, body = server.req(f"/api/recover/preview?sid={sid}&path={quote(path)}", raw=True)
    assert st == 200 and body == FAT_EXPECT["docs/Quarterly Report 2024.txt"]
    st, hdr, body = server.req(f"/api/recover/preview?sid={sid}&path={quote(path)}", raw=True,
                               headers={"Range": "bytes=100-199"})
    assert st == 206 and body == FAT_EXPECT["docs/Quarterly Report 2024.txt"][100:200]
    server.ok("/api/recover/close", {"sid": sid})


def test_browse_api_other_filesystems_have_no_deleted_listing(server, images):
    if "ext4" not in images:
        pytest.skip("no ext4 image")
    o = server.ok("/api/recover/open", {"source": images["ext4"]})
    assert o["can_list_deleted"] is False
    ls = server.ok(f"/api/recover/ls?sid={o['sid']}&path=/&deleted=1")
    assert not any(e.get("deleted") for e in ls["entries"])
    server.ok("/api/recover/close", {"sid": o["sid"]})
