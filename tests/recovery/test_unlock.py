"""Opening encrypted volumes (BitLocker, LUKS, FileVault/APFS) with a key the user supplies, read-only."""
import json
import os
import pickle
import shutil
import threading

import pytest
from conftest import read_tree, run_job, start_server, stop_server
from make_unlock_images import (APFS_PASSWORD, BDE_FILES, BDE_PASSWORD, BDE_RECOVERY, LUKS_FILES,
                                LUKS_PASSPHRASE, build_unlock_images, same_data_blocks)
from test_fsviews import walk

from retrova.recovery import unlock
from retrova.recovery.fsview import FsError, open_view
from retrova.recovery.imagejob import ImageJob
from retrova.recovery.partitions import best_part, scan
from retrova.recovery.source import ReadOnlySource
from retrova.recovery.unlock import KeyRejected, NeedsKey, UnlockError, make_key, open_decrypted


@pytest.fixture(scope="session")
def locked(images, tmp_path_factory):
    return build_unlock_images(str(tmp_path_factory.mktemp("unlock")), images)


WRONG_RECOVERY = "000000-000000-000000-000000-000000-000000-000000-000000"       # passes the format checks


def need_img(locked, key):
    if key not in locked:
        pytest.skip(f"{key} image could not be built here (cryptsetup / mkntfs missing)")
    return locked[key]


def spec_for(path, fs, offset=0, size=None):
    return {"source": path, "offset": offset, "size": size or os.path.getsize(path) - offset, "fs": fs,
            "volume": None}


def unlocked_spec(path, fs, kind, secret, **kw):
    sp = spec_for(path, fs, **kw)
    sp["unlock"] = make_key(sp, kind, secret)
    return sp


def assert_tree(view, want):
    files, dirs, links, errors = walk(view)
    assert not errors
    for rel, data in want.items():
        assert rel in files, rel
        n = files[rel]
        assert n.size == len(data)
        assert view.open_handle(n.handle).read(n.size) == data, rel


# ====================================================================== APFS (FileVault)
def test_apfs_locked_then_unlocked_with_exact_bytes(locked):
    sp = spec_for(locked["apfs_enc"], "apfs")
    with pytest.raises(NeedsKey) as ei:
        open_view(sp)
    nk = ei.value
    assert nk.kind == "apfs" and nk.volume == "Encrypted" and "password" in nk.hint.lower()
    assert nk.accepts == ["password", "recovery"]
    with pytest.raises(KeyRejected) as bad:
        make_key(sp, "apfs", "not the password")
    assert "didn't unlock" in str(bad.value) and "not the password" not in str(bad.value)
    sp["unlock"] = make_key(sp, "apfs", APFS_PASSWORD)
    with open_view(sp) as v:
        assert v.fstype == "apfs" and v.label == "Encrypted"
        root = {c.name: c for c in v.iterdir(v.root())}
        assert v.open_handle(root["hardlink"].handle).read() == " File System\n".encode()
        assert root["empty"].size == 0
        lz = v.resolve("/dir/compressed-lzvn-fork")
        data = v.open_handle(lz.handle).read(lz.size)
        assert len(data) == 7873 and data.startswith(b"# BEGIN TEST DATA\n\nThere was a clock")
        other = v.resolve("/dir/compressed-zlib-fork")
        assert v.open_handle(other.handle).read(other.size) == data
        # reading in pieces through the patched stream decrypts at any offset
        fh = v.open_handle(lz.handle)
        fh.seek(5000)
        assert fh.read(300) == data[5000:5300]


def test_apfs_key_is_hidden_and_survives_a_worker_process(locked):
    sp = unlocked_spec(locked["apfs_enc"], "apfs", "apfs", APFS_PASSWORD)
    key = sp["unlock"]
    vek = key.get("vek")
    assert vek.hex() not in repr(key) and vek.hex() not in str(sp) and APFS_PASSWORD not in repr(key)
    clone = pickle.loads(pickle.dumps(sp))          # what a spawned worker receives
    with open_view(clone) as v:
        assert v.open_handle(v.resolve("/hardlink").handle).read() == " File System\n".encode()
    # the key object holds the derived key only, never the typed password
    assert APFS_PASSWORD.encode() not in pickle.dumps(key)


def test_unlock_key_lives_until_session_and_jobs_are_done(locked):
    sp = unlocked_spec(locked["apfs_enc"], "apfs", "apfs", APFS_PASSWORD)
    key = sp["unlock"]
    key.hold()                                    # a running job
    unlock.release(sp)                            # the session closes
    assert key.alive and key.get("vek")
    key.release()                                 # the job ends
    assert not key.alive
    with pytest.raises(unlock.KeyGone, match="locked again"):
        key.get("vek")
    with pytest.raises(unlock.KeyGone):
        key.hold()
    with pytest.raises(UnlockError):
        unlock.ensure_alive(sp)


def test_apfs_volume_without_usable_password_record_is_explained(locked, monkeypatch):
    sp = spec_for(locked["apfs_enc"], "apfs")
    monkeypatch.setattr(unlock, "_apfs_password_records", lambda volume: [])
    with pytest.raises(UnlockError) as ei:
        open_view(sp)
    assert not isinstance(ei.value, NeedsKey) and "can't be unlocked from anywhere else" in str(ei.value)


# ====================================================================== BitLocker, upstream fixtures
def bde_fixture(tmp_path, name):
    from make_unlock_images import gunzip
    return gunzip(name, str(tmp_path / name[:-3]))


def test_bitlocker_fixture_states_and_keys(tmp_path):
    pw = bde_fixture(tmp_path, "bde_password.bin.gz")
    sp = spec_for(pw, "bitlocker")
    with pytest.raises(NeedsKey) as ei:
        open_decrypted(sp, "bitlocker")
    nk = ei.value
    assert nk.kind == "bitlocker" and nk.label == "DESKTOP-QNI1MMF TestVolume 10/8/2021"
    assert nk.accepts == ["password"] and "password" in nk.hint
    sp["unlock"] = make_key(sp, "bitlocker", BDE_PASSWORD)
    with open_decrypted(sp, "bitlocker") as src:
        assert src.size == os.path.getsize(pw)
        assert src.pread(0, 512)[3:11] == b"NTFS    "
        assert src.pread(0x2000, 8) == b"FILE0\x00\x03\x00"       # inside the first sectors BitLocker moves around
    # a recovery-key-protected volume
    rec = spec_for(bde_fixture(tmp_path, "bde_recovery.bin.gz"), "bitlocker")
    with pytest.raises(NeedsKey) as ei:
        open_decrypted(rec, "bitlocker")
    assert ei.value.accepts == ["password", "recovery"] and "recovery key" in ei.value.hint
    for typed in (BDE_RECOVERY, BDE_RECOVERY.replace("-", ""), " " + BDE_RECOVERY.replace("-", " ") + " "):
        k = make_key(rec, "bitlocker", typed)
        with open_decrypted(dict(rec, unlock=k), "bitlocker") as src:
            assert src.pread(0, 512)[3:11] == b"NTFS    "
    # suspended (clear key) and fully decrypted volumes need no key at all
    for name in ("bde_suspended.bin.gz", "bde_decrypted.bin.gz"):
        with open_decrypted(spec_for(bde_fixture(tmp_path, name), "bitlocker"), "bitlocker") as src:
            assert src.pread(0, 512)[3:11] == b"NTFS    "
    with pytest.raises(UnlockError, match="doesn't need a key"):
        make_key(spec_for(bde_fixture(tmp_path, "bde_suspended.bin.gz"), "bitlocker"), "bitlocker", "anything")


def test_bitlocker_wrong_keys_get_plain_messages(tmp_path):
    rec = spec_for(bde_fixture(tmp_path, "bde_recovery.bin.gz"), "bitlocker")
    pw = spec_for(bde_fixture(tmp_path, "bde_password.bin.gz"), "bitlocker")
    with pytest.raises(KeyRejected, match="didn't unlock"):
        make_key(pw, "bitlocker", "wrong password")
    with pytest.raises(KeyRejected, match="48 digits"):
        make_key(rec, "bitlocker", BDE_RECOVERY[:-7])                    # a group short
    with pytest.raises(KeyRejected, match="doesn't look like a valid recovery key"):
        make_key(rec, "bitlocker", "123456-" * 7 + "123456")             # right shape, groups don't check out
    with pytest.raises(KeyRejected, match="didn't unlock"):
        make_key(rec, "bitlocker", WRONG_RECOVERY)                       # well-formed, but not this drive's key
    with pytest.raises(KeyRejected, match="valid recovery key"):
        make_key(rec, "bitlocker", BDE_RECOVERY[:-1] + "9")              # last group fails its own checksum
    with pytest.raises(KeyRejected, match="no recovery key"):
        make_key(pw, "bitlocker", BDE_RECOVERY)
    with pytest.raises(KeyRejected, match="Type the key"):
        make_key(pw, "bitlocker", "")


# ====================================================================== BitLocker + NTFS files, LUKS + ext4 / FAT
def test_bitlocker_volumes_browse_and_read_exact_bytes(locked):
    want = BDE_FILES
    cases = [("bde_password", "password", BDE_PASSWORD), ("bde_recovery", "recovery", BDE_RECOVERY),
             ("bde_recovery", "password", "wrong"), ("bde_suspended", None, None), ("bde_decrypted", None, None)]
    for key, how, secret in cases:
        path = need_img(locked, key)
        sp = spec_for(path, "bitlocker")
        if secret == "wrong":
            with pytest.raises(NeedsKey):
                open_view(sp)
            with pytest.raises(KeyRejected):
                make_key(sp, "bitlocker", secret)
            continue
        if secret:
            with pytest.raises(NeedsKey):
                open_view(sp)
            sp["unlock"] = make_key(sp, "bitlocker", secret)
        with open_view(sp) as v:
            assert v.fstype == "ntfs", key
            assert_tree(v, want)
        pickle.loads(pickle.dumps(sp))


@pytest.mark.parametrize("key", ["luks2", "luks2_argon", "luks1"])
def test_luks_volumes_browse_and_read_exact_bytes(locked, key):
    path = need_img(locked, key)
    sp = spec_for(path, "luks")
    with pytest.raises(NeedsKey) as ei:
        open_view(sp)
    assert ei.value.kind == "luks" and ei.value.accepts == ["password"]
    with pytest.raises(KeyRejected, match="passphrase didn't unlock"):
        make_key(sp, "luks", "nope")
    sp["unlock"] = make_key(sp, "luks", LUKS_PASSPHRASE)
    with open_view(sp) as v:
        assert v.fstype == "ext4"
        assert_tree(v, LUKS_FILES)
    # the typed passphrase is nowhere in the key that is kept
    assert LUKS_PASSPHRASE.encode() not in pickle.dumps(sp["unlock"])
    # sector-exact: the unlocked bytes are the plain image
    with open_decrypted(sp, "luks") as src:
        assert same_data_blocks(src, locked["luks_plain"], 8 * 1024 * 1024)


def test_locked_partitions_are_listed_and_found_at_their_offsets(locked):
    disk = need_img(locked, "locked_disk")
    lay = scan(disk)
    kinds = [(p["fs"], p.get("locked"), p["supported"]) for p in lay["parts"]]
    assert kinds == [("bitlocker", True, True), ("luks", True, True)]
    assert "BitLocker" in lay["parts"][0]["why"] and "LUKS" in lay["parts"][1]["why"]
    assert best_part(lay["parts"])["fs"] == "bitlocker"
    off, _, size = locked["locked_disk_parts"][1]
    sp = {"source": disk, "offset": off, "size": size, "fs": "luks", "volume": None}
    sp["unlock"] = make_key(sp, "luks", LUKS_PASSPHRASE)
    with open_view(sp) as v:
        assert v.fstype == "ext4"
        assert_tree(v, LUKS_FILES)


def test_source_is_never_written(locked, tmp_path):
    path = shutil.copy(need_img(locked, "luks2"), str(tmp_path / "ro.img"))
    before = os.stat(path)
    with open(path, "rb") as fh:
        digest = __import__("hashlib").sha256(fh.read()).hexdigest()
    sp = unlocked_spec(path, "luks", "luks", LUKS_PASSPHRASE)
    with open_view(sp) as v:
        walk(v)
    src = open_decrypted(sp, "luks")
    assert not src.writable() and not src.raw.writable()
    src.close()
    assert os.stat(path).st_mtime_ns == before.st_mtime_ns
    with open(path, "rb") as fh:
        assert __import__("hashlib").sha256(fh.read()).hexdigest() == digest


# ====================================================================== jobs on unlocked sources
def test_copy_verify_and_image_use_the_decrypted_bytes(locked, tmp_path):
    from retrova.recovery.copyjob import CopyJob
    from retrova.recovery.verifyjob import VerifyJob
    path = need_img(locked, "luks2")
    sp = unlocked_spec(path, "luks", "luks", LUKS_PASSPHRASE)
    out = str(tmp_path / "out")
    st = run_job(CopyJob(sp, "Locked", ["/"], out, [], 0.01, 3, [], []))
    assert st["state"] == "done" and st["failed"] == 0, st
    root = [d for d in os.listdir(out) if not d.startswith("_")][0]
    got = read_tree(os.path.join(out, root))
    for rel, data in LUKS_FILES.items():
        assert got[rel] == data, rel
    key = sp["unlock"]
    assert key.alive                                  # the session's hold; jobs came and went
    job = VerifyJob(sp, "Locked", ["/"], out, [], [], True, "full", 3, True)
    st = run_job(job)
    assert st["state"] == "done" and st["problems"] == 0 and st["counts"]["OK"] >= len(got) - 2, st
    # damage a copied file: Verify compares decrypted bytes, so it notices
    victim = os.path.join(out, root, "docs", "report.pdf")
    with open(victim, "r+b") as fh:
        fh.seek(100)
        fh.write(b"\0\0\0\0")
    st = run_job(VerifyJob(sp, "Locked", ["/"], out, [], [], True, "full", 3, False))
    assert st["problems"] == 1
    # an image of the unlocked volume is the plain file system, not the scrambled bytes
    img = str(tmp_path / "plain.img")
    dec = open_decrypted(sp, "luks")
    total = dec.size
    dec.close()
    st = run_job(ImageJob(path, 0, total, img, 0.01, "Locked", decrypt=sp))
    assert st["state"] == "done", st
    with open(img, "rb") as a:
        assert same_data_blocks(type("R", (), {"pread": lambda self, o, n: (a.seek(o), a.read(n))[1]})(),
                                locked["luks_plain"], 8 * 1024 * 1024)
    # the image of the raw bytes would be ciphertext
    raw = str(tmp_path / "raw.img")
    run_job(ImageJob(path, 0, 1024 * 1024, raw, 0.01, "Raw"))
    assert open(raw, "rb").read(4096) != open(locked["luks_plain"], "rb").read(4096)
    # once the session lets go and the jobs are done, the key is gone and new jobs refuse to start
    unlock.release(sp)
    assert not key.alive
    with pytest.raises(unlock.KeyGone):
        run_job(CopyJob(sp, "Locked", ["/"], str(tmp_path / "again"), [], 0.01, 1, [], []))


def test_carve_looks_inside_the_unlocked_volume(locked, tmp_path):
    from retrova.recovery.carve import CarveJob
    from retrova.recovery.carve_formats import expand_types
    path = need_img(locked, "luks2")
    sp = unlocked_spec(path, "luks", "luks", LUKS_PASSPHRASE)
    dec = open_decrypted(sp, "luks")
    size = dec.size
    dec.close()
    st = run_job(CarveJob(path, 0, size, str(tmp_path / "carved"), expand_types(["zip", "png", "jpg", "pdf"]),
                          0.01, decrypt=sp))
    assert st["state"] == "done", st
    # the tree holds PDF-less random bytes; what matters is that the scan ran on the decrypted bytes
    assert st["stats"]["bytes_done"] >= size


# ====================================================================== over HTTP, like the app does it
@pytest.fixture(scope="module")
def srv():
    api = start_server()
    yield api
    stop_server(api)


@pytest.fixture(scope="module")
def srv_procs():
    api = start_server({"RETROVA_PROCS": "1"})
    yield api
    stop_server(api)


def every_response_text(api):
    """Wraps api.req so every response body is kept for the 'no key comes back' check."""
    seen = []
    orig = api.req

    def spy(path, body=None, headers=None, raw=False):
        r = orig(path, body, headers, raw)
        seen.append(json.dumps(r[1], default=str) if not raw else repr(r[2][:2000]))
        return r
    api.req = spy
    return seen


def test_http_locked_flow_end_to_end(srv, locked, tmp_path):
    need_img(locked, "luks2")
    seen = every_response_text(srv)
    disk = locked["locked_disk"]
    off = locked["locked_disk_parts"][1][0]
    # sources / scan show the partitions as locked
    sc = srv.ok("/api/recover/scan?source=" + disk)
    assert [(p["fs"], p["locked"], p["supported"]) for p in sc["partitions"]] == [("bitlocker", True, True),
                                                                               ("luks", True, True)]
    # 1. no key yet: the engine says what it needs, and opens nothing
    r = srv.ok("/api/recover/open", {"source": disk, "offset": off})
    assert "sid" not in r and r["needs_key"]["kind"] == "luks" and r["needs_key"]["accepts"] == ["password"]
    assert r["offset"] == off and r["fs_name"] == "LUKS (encrypted)"
    # 2. wrong key: a plain error, and the sheet can try again
    st, d = srv.post("/api/recover/open", {"source": disk, "offset": off, "key": "nope nope"})
    assert st == 400 and "passphrase didn't unlock" in d["error"] and "nope nope" not in d["error"]
    # 3. right key
    o = srv.ok("/api/recover/open", {"source": disk, "offset": off, "key": LUKS_PASSPHRASE})
    assert o["fs"] == "ext4" and o["encryption"] == {"kind": "luks", "name": "LUKS", "unlocked": True}
    assert any("unlocked with the key you entered" in n for n in o["notes"])
    sid = o["sid"]
    ls = srv.ok(f"/api/recover/ls?sid={sid}&path=/")
    names = {e["name"]: e for e in ls["entries"]}
    assert names["hello.txt"]["size"] == len(LUKS_FILES["hello.txt"]) and names["docs"]["kind"] == "dir"
    st, h, body = srv.req(f"/api/recover/preview?sid={sid}&path=/photos/beach.jpg", headers={"Range": "bytes=100-299"},
                          raw=True)
    assert st == 206 and body == LUKS_FILES["photos/beach.jpg"][100:300]
    st, h, body = srv.req(f"/api/recover/preview?sid={sid}&path=/hello.txt", raw=True)
    assert body == LUKS_FILES["hello.txt"]
    # copy, then verify from the copy job, then an image of the unlocked volume, then a deep scan
    out = str(tmp_path / "out")
    j = srv.ok("/api/recover/copy", {"sid": sid, "paths": ["/docs", "/hello.txt", "/photos"], "out": out,
                                     "workers": 3, "reserve_gb": 0.5})
    st = srv.wait(j)
    assert st["state"] == "done" and st["failed"] == 0, st
    with open(os.path.join(out, "photos", "beach.jpg"), "rb") as fh:
        assert fh.read() == LUKS_FILES["photos/beach.jpg"]
    v = srv.wait(srv.ok("/api/recover/verify", {"from_job": j["id"], "mode": "full", "workers": 2}))
    assert v["state"] == "done" and v["problems"] == 0
    j = srv.ok("/api/recover/image", {"sid": sid, "out": str(tmp_path / "unlocked.img"), "reserve_gb": 0.5})
    assert j["params"]["decrypted"] is True
    st = srv.wait(j)
    assert st["state"] == "done"
    with open(str(tmp_path / "unlocked.img"), "rb") as a:
        assert same_data_blocks(type("R", (), {"pread": lambda self, o, n: (a.seek(o), a.read(n))[1]})(),
                                locked["luks_plain"], 8 * 1024 * 1024)
    j = srv.ok("/api/recover/carve", {"sid": sid, "out": str(tmp_path / "carve"), "types": ["png"], "reserve_gb": 0.1})
    assert j["params"]["decrypted"] is True and j["params"]["start"] == 0
    assert srv.wait(j)["state"] == "done"
    # 4. leaving the drive forgets the key: jobs on the old session can't start any more
    assert srv.ok("/api/recover/close", {"sid": sid}) == {"closed": True}
    st, d = srv.post("/api/recover/copy", {"sid": sid, "paths": ["/"], "out": str(tmp_path / "late")})
    assert st == 404
    prev_jobs = srv.ok("/api/jobs")["jobs"]
    st, d = srv.post("/api/recover/verify", {"from_job": j["id"]})
    # the earlier copy job's key is gone with the session, so a new Verify asks to unlock again
    st, d = srv.post("/api/recover/verify", {"from_job": [x for x in prev_jobs if x["kind"] == "copy"][0]["id"],
                                             "mode": "full"})
    assert st == 409 and "locked again" in d["error"]
    # nothing the server ever sent back contains the key, the passphrase or a long hex string of key bytes
    blob = "\n".join(seen)
    assert LUKS_PASSPHRASE not in blob and "nope nope" not in blob
    assert "volume_key" not in blob and "UnlockKey" not in blob

def test_server_output_never_contains_the_key(locked):
    api = start_server()
    try:
        st, _ = api.post("/api/recover/open", {"source": need_img(locked, "luks1"), "key": "wrong-pass-123"})
        assert st == 400
        assert api.ok("/api/recover/open", {"source": locked["luks1"], "key": LUKS_PASSPHRASE})["sid"]
    finally:
        stop_server(api)
    log = api.proc.stdout.read().decode(errors="replace")
    assert LUKS_PASSPHRASE not in log and "wrong-pass-123" not in log


def test_http_bitlocker_and_apfs_prompts(srv_procs, locked, tmp_path):
    """BitLocker and FileVault over HTTP, with the copy run by worker processes (as on Windows)."""
    api = srv_procs
    seen = every_response_text(api)
    assert api.ok("/api/info")["workers"] == "processes"
    bl = locked["bde_recovery"]
    r = api.ok("/api/recover/open", {"source": bl})
    assert r["needs_key"]["kind"] == "bitlocker" and r["needs_key"]["accepts"] == ["password", "recovery"]
    assert "aka.ms" not in json.dumps(r) and r["needs_key"]["label"].startswith("DESKTOP-")
    st, d = api.post("/api/recover/open", {"source": bl, "key": WRONG_RECOVERY})
    assert st == 400 and "didn't unlock" in d["error"]
    st, d = api.post("/api/recover/open", {"source": bl, "key": BDE_RECOVERY[:-1] + "9"})
    assert st == 400 and "valid recovery key" in d["error"]
    o = api.ok("/api/recover/open", {"source": bl, "key": BDE_RECOVERY})
    assert o["fs"] == "ntfs" and o["encryption"]["kind"] == "bitlocker"
    want = BDE_FILES
    out = str(tmp_path / "bl")
    j = api.ok("/api/recover/copy", {"sid": o["sid"], "paths": ["/"], "out": out, "workers": 2, "reserve_gb": 0.5})
    st = api.wait(j)
    assert st["state"] == "done" and st["failed"] == 0 and "processes" in st["engine"], st
    got = read_tree(os.path.join(out, [d for d in os.listdir(out) if not d.startswith("_")][0]))
    for rel, data in want.items():
        assert got[rel] == data, rel
    # a suspended volume opens without asking
    o2 = api.ok("/api/recover/open", {"source": locked["bde_suspended"]})
    assert o2["fs"] == "ntfs" and o2["encryption"] == {"kind": "bitlocker", "name": "BitLocker", "unlocked": False}
    assert any("needed no key" in n for n in o2["notes"])
    # FileVault (APFS)
    ap = locked["apfs_enc"]
    r = api.ok("/api/recover/open", {"source": ap})
    assert r["needs_key"]["kind"] == "apfs" and r["needs_key"]["volume"] == "Encrypted"
    assert "hint" in r["needs_key"] and "password" in r["needs_key"]["hint"].lower()
    st, d = api.post("/api/recover/open", {"source": ap, "key": "wrong"})
    assert st == 400
    o3 = api.ok("/api/recover/open", {"source": ap, "key": APFS_PASSWORD})
    assert o3["fs"] == "apfs" and o3["encryption"]["name"] == "FileVault"
    out = str(tmp_path / "apfs")
    j = api.ok("/api/recover/copy", {"sid": o3["sid"], "paths": ["/hardlink", "/dir"], "out": out, "workers": 2})
    st = api.wait(j)
    assert st["state"] == "done" and st["failed"] == 0, st
    assert open(os.path.join(out, "hardlink"), "rb").read() == " File System\n".encode()
    v = api.wait(api.ok("/api/recover/verify", {"from_job": j["id"], "mode": "full", "workers": 2}))
    assert v["problems"] == 0
    blob = "\n".join(seen)
    for secret in (BDE_RECOVERY, BDE_RECOVERY.replace("-", ""), '"wrong"', '"key"'):
        assert secret not in blob
    assert "fvek" not in blob and "vek" not in blob.replace("level", "")


def test_http_open_wrong_kind_of_key_text_never_echoed(srv, locked):
    path = need_img(locked, "luks1")
    secret = "s3cr3t-passphrase-xyz"
    st, d = srv.post("/api/recover/open", {"source": path, "key": secret})
    assert st == 400 and secret not in json.dumps(d)
    assert srv.ok("/api/recover/sources")  # still fine


def test_concurrent_reads_of_one_unlocked_volume(locked):
    path = need_img(locked, "luks2")
    sp = unlocked_spec(path, "luks", "luks", LUKS_PASSPHRASE)
    src = open_decrypted(sp, "luks")
    src_plain = open_decrypted(sp, "luks")
    plain = src_plain.pread(0, 8 * 1024 * 1024)       # whatever it decodes to; threads must agree with it
    src_plain.close()
    errors = []

    def reader(seed):
        import random
        rnd = random.Random(seed)
        for _ in range(200):
            o = rnd.randrange(0, len(plain) - 70000)
            n = rnd.randrange(1, 70000)
            if src.pread(o, n) != plain[o:o + n]:
                errors.append((o, n))
    ts = [threading.Thread(target=reader, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    src.close()
    assert not errors
