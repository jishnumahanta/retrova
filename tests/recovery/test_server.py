"""Exercise every recovery/platform endpoint over real HTTP against `python -m retrova --no-window`."""
import os
import time

import pytest
from conftest import APFS_DIR, need, read_tree, start_server, stop_server

import carve_samples as cs


def test_info_drives_sources(server):
    info = server.ok("/api/info")
    for k in ("version", "platform", "admin", "can_elevate", "user", "home", "app_data"):
        assert k in info
    assert info["platform"] in ("mac", "windows", "linux") and isinstance(info["filesystems"], list)
    d = server.ok("/api/drives")
    assert isinstance(d["disks"], list) and isinstance(d["volumes"], list)
    for disk in d["disks"]:
        for k in ("id", "name", "model", "device", "size", "internal", "removable", "protocol", "health",
                  "partitions"):
            assert k in disk
        assert disk["health"]["state"] in ("ok", "warn", "bad", "unknown")
    s = server.ok("/api/recover/sources")
    for e in s["sources"]:
        for k in ("source", "label", "size", "fs", "detail", "recommended", "needs_admin"):
            assert k in e
    fr = server.ok("/api/recover/free?out=/tmp/some/new/folder")
    assert fr["free"] > 0 and fr["exists"] is False
    # On a Mac or Windows box /api/elevate really asks for the admin password, so only check the refusal paths.
    if info["platform"] == "linux" or info["admin"]:
        st, d = server.post("/api/elevate")
        assert st in (400, 409) and d["error"]      # Linux / already admin: friendly refusal


def test_unauthorised(server):
    st, d = server.req("/api/recover/sources", headers={"X-Token": "wrong"})
    assert st == 401
    st, d = server.req("/api/recover/open", {"source": "/x"}, headers={"X-Token": "wrong"})
    assert st == 401


def test_open_errors(server, tmp_path):
    st, d = server.post("/api/recover/open", {"source": "/no/such/disk.img"})
    assert st == 404 and "Can't find" in d["error"]
    st, d = server.post("/api/recover/open", {"source": str(tmp_path)})
    assert st == 400 and "folder" in d["error"]
    noise = tmp_path / "noise.img"
    noise.write_bytes(os.urandom(300_000))
    st, d = server.post("/api/recover/open", {"source": str(noise)})
    assert st == 422 and "Deep scan" in d["error"]
    st, d = server.get("/api/recover/ls?sid=nope&path=/")
    assert st == 404 and "open it again" in d["error"]


def test_scan_open_ls_preview(server, images, tree):
    gpt = need(images, "gpt")
    sc = server.ok("/api/recover/scan?source=" + gpt)
    assert sc["scheme"] == "gpt" and [p["fs"] for p in sc["partitions"]] == ["fat32", "ext4"]
    o = server.ok("/api/recover/open", {"source": gpt})
    assert o["fs"] == "ext4" and o["offset"] == images["gpt_parts"][1][0] and o["sid"]
    assert any("partitions" in n for n in o["notes"])
    o2 = server.ok("/api/recover/open", {"source": gpt, "offset": images["gpt_parts"][0][0]})
    assert o2["fs"] == "fat32" and o2["label"] == "RETROVAFAT"
    sid = o["sid"]
    ls = server.ok(f"/api/recover/ls?sid={sid}&path=/")
    names = {e["name"]: e for e in ls["entries"]}
    assert names["docs"]["kind"] == "dir" and names["docs"]["size"] is None
    assert names["hello.txt"]["size"] == 12 and names["hello.txt"]["mtime"] == 1_600_000_000_000
    assert names["link.txt"]["kind"] == "link"
    assert ls["entries"][0]["kind"] == "dir"               # folders first
    sub = server.ok(f"/api/recover/ls?sid={sid}&path=/docs/sub")
    assert {e["path"] for e in sub["entries"]} == {"/docs/sub/deeper", "/docs/sub/x.txt"}
    st, d = server.get(f"/api/recover/ls?sid={sid}&path=/nope")
    assert st == 404
    st, d = server.get(f"/api/recover/ls?sid={sid}&path=/hello.txt")
    assert st == 400
    # preview: full, range, suffix range, out of range
    st, h, body = server.req(f"/api/recover/preview?sid={sid}&path=/hello.txt", raw=True)
    assert st == 200 and body == tree["hello.txt"] and h["Content-Type"].startswith("text/plain")
    st, h, body = server.req(f"/api/recover/preview?sid={sid}&path=/big.bin", headers={"Range": "bytes=100-199"},
                             raw=True)
    assert st == 206 and body == tree["big.bin"][100:200]
    assert h["Content-Range"] == f"bytes 100-199/{len(tree['big.bin'])}" and h["Accept-Ranges"] == "bytes"
    st, h, body = server.req(f"/api/recover/preview?sid={sid}&path=/big.bin", headers={"Range": "bytes=-10"}, raw=True)
    assert st == 206 and body == tree["big.bin"][-10:]
    st, h, body = server.req(f"/api/recover/preview?sid={sid}&path=/big.bin",
                             headers={"Range": f"bytes={len(tree['big.bin']) + 5}-"}, raw=True)
    assert st == 416
    st, h, body = server.req(f"/api/recover/preview?sid={sid}&path=/photos/IMG_0001.JPG", raw=True)
    assert st == 200 and h["Content-Type"] == "image/jpeg" and body == tree["photos/IMG_0001.JPG"]
    st, d = server.get(f"/api/recover/preview?sid={sid}&path=/docs")
    assert st == 400
    sess = server.ok(f"/api/recover/session?sid={sid}")
    assert sess["sid"] == sid and sess["fs"] == "ext4"
    assert server.ok("/api/recover/close", {"sid": o2["sid"]})["closed"]


def test_apfs_over_http(server):
    o = server.ok("/api/recover/open", {"source": os.path.join(APFS_DIR, "gptdisk.img")})
    assert o["fs"] == "apfs" and o["volumes"] == ["Snapshots"] and o["offset"] == 4194304
    ls = server.ok(f"/api/recover/ls?sid={o['sid']}&path=/.fseventsd")
    assert len(ls["entries"]) > 10
    o = server.ok("/api/recover/open", {"source": os.path.join(APFS_DIR, "jhfs_converted.bin")})
    st, h, body = server.req(f"/api/recover/preview?sid={o['sid']}&path=/dir/compressed-lzfse-fork", raw=True)
    assert st == 200 and len(body) == 7873


def test_sizes_poll(server, images, tree):
    o = server.ok("/api/recover/open", {"source": need(images, "fat32")})
    want_docs = sum(len(v) for k, v in tree.items() if k.startswith("docs/"))
    for _ in range(100):
        r = server.ok("/api/recover/size", {"sid": o["sid"], "paths": ["/docs", "/hello.txt"], "patterns": []})
        if all(v["state"] == "done" for v in r["sizes"].values()):
            break
        time.sleep(0.05)
    assert r["sizes"]["/docs"] == {"state": "done", "bytes": want_docs, "files": 3, "errors": 0}
    assert r["sizes"]["/hello.txt"]["bytes"] == 12
    r = server.ok("/api/recover/size", {"sid": o["sid"], "paths": ["/docs"], "patterns": ["*.pdf"]})
    for _ in range(100):
        r = server.ok("/api/recover/size", {"sid": o["sid"], "paths": ["/docs"], "patterns": ["*.pdf"]})
        if r["sizes"]["/docs"]["state"] == "done":
            break
        time.sleep(0.05)
    assert r["sizes"]["/docs"]["bytes"] == want_docs - len(tree["docs/report.pdf"])


def test_regen_copy_verify_repair_csv(server, images, tree, tmp_path):
    o = server.ok("/api/recover/open", {"source": need(images, "ext4")})
    sid = o["sid"]
    j = server.ok("/api/recover/regen", {"sid": sid, "paths": ["/project"]})
    assert j["kind"] == "regen"
    server.wait(j)
    found = server.ok(f"/api/job/result?id={j['id']}")["found"]
    assert {f["path"] for f in found} == {"/project/node_modules", "/project/__pycache__"}
    out = str(tmp_path / "out")
    skipped = [f for f in found if f["confidence"] == "safe"]
    j = server.ok("/api/recover/copy", {"sid": sid, "paths": ["/docs", "/project", "/big.bin"], "out": out,
                                        "excluded": [f["path"] for f in skipped], "patterns": [],
                                        "reserve_gb": 0.5, "workers": 3, "skipped_info": skipped})
    assert j["kind"] == "copy" and j["state"] in ("queued", "running", "done")
    st = server.wait(j)
    assert st["state"] == "done" and st["failed"] == 0, st
    for k in ("copied", "skipped", "failed", "renamed", "current", "errors", "free", "out_root"):
        assert k in st
    assert st["stats"]["files_done"] == st["stats"]["files_total"] == st["copied"]
    got = read_tree(out)
    assert got["big.bin"] == tree["big.bin"] and got["docs/sub/deeper/data.bin"] == tree["docs/sub/deeper/data.bin"]
    assert "project/node_modules/left-pad/index.js" not in got and got["project/package.json"]
    assert server.ok(f"/api/job/result?id={j['id']}")["total"] == 0
    # verify straight from the copy job (uses its paths, exclusions and output)
    v = server.ok("/api/recover/verify", {"from_job": j["id"], "mode": "full", "workers": 2})
    st = server.wait(v)
    assert st["state"] == "done" and st["problems"] == 0 and st["counts"]["OK"] == st["total_files"], st
    # damage the copy
    with open(os.path.join(out, "big.bin"), "r+b") as fh:
        fh.truncate(4096)
    os.remove(os.path.join(out, "docs", "report.pdf"))
    v = server.ok("/api/recover/verify", {"sid": sid, "paths": ["/docs", "/project", "/big.bin"], "out": out,
                                          "excluded": [f["path"] for f in skipped], "mode": "sample",
                                          "resume": True})
    st = server.wait(v)
    assert st["problems"] == 2 and st["fixable"] == 2
    res = server.ok(f"/api/job/result?id={v['id']}&status=SIZE")
    assert res["total"] == 1 and res["items"][0]["key"] == "big.bin" and "task" not in res["items"][0]
    stc, h, body = server.req(f"/api/recover/verify_csv?id={v['id']}", raw=True)
    assert stc == 200 and body.startswith(b"\xef\xbb\xbf") and b"MISSING" in body
    assert "attachment" in h["Content-Disposition"]
    r = server.ok("/api/recover/repair", {"job": v["id"], "workers": 2})
    st = server.wait(r)
    assert st["state"] == "done" and st["repaired"] == 2 and st["repair"]
    assert read_tree(out)["big.bin"] == tree["big.bin"]
    st, d = server.post("/api/recover/repair", {"job": r["id"]})
    assert st == 400 and "Nothing to repair" in d["error"]
    v = server.ok("/api/recover/verify", {"sid": sid, "paths": ["/docs", "/project", "/big.bin"], "out": out,
                                          "excluded": [f["path"] for f in skipped], "mode": "full"})
    assert server.wait(v)["problems"] == 0


def test_image_endpoint(server, images, tmp_path):
    o = server.ok("/api/recover/open", {"source": need(images, "mbr"), "offset": images["mbr_parts"][0][0]})
    j = server.ok("/api/recover/image", {"sid": o["sid"], "out": str(tmp_path) + os.sep, "reserve_gb": 0.5})
    st = server.wait(j)
    assert st["state"] == "done" and st["out"].endswith(".img")
    assert open(st["out"], "rb").read() == open(images["fat16"], "rb").read()
    st2, d = server.post("/api/recover/image", {"sid": o["sid"], "out": st["out"]})
    assert st2 == 409 and "already exists" in d["error"]
    j = server.ok("/api/recover/image", {"source": images["fat12"], "out": str(tmp_path / "whole.img")})
    assert server.wait(j)["state"] == "done"
    assert open(str(tmp_path / "whole.img"), "rb").read() == open(images["fat12"], "rb").read()


def test_carve_endpoint_and_job_control(server, tmp_path):
    samples = cs.all_samples()
    disk = str(tmp_path / "disk.img")
    placed = cs.build_disk(disk, samples)
    types = server.ok("/api/recover/carve_types")
    assert {"jpg", "png", "pdf", "zip", "mp4"} <= {t["type"] for t in types["types"]}
    out = str(tmp_path / "found")
    # raf, ico and wal are selected too: they carry a JPEG, a PNG and database pages, which would be found on their own
    kinds = ("jpg", "png", "pdf", "zip", "mp4", "docx", "xlsx", "pptx", "mp3", "wav", "sqlite", "mov", "heic",
             "raf", "ico", "wal")
    j = server.ok("/api/recover/carve", {"source": disk, "out": out, "types": list(kinds)})
    assert j["kind"] == "carve"
    st = server.wait(j)
    assert st["state"] == "done", st
    res = server.ok(f"/api/job/result?id={j['id']}&limit=1000")
    want = {off: (typ, data) for _, typ, off, data in placed
            if typ in kinds}
    got = {i["offset"]: i for i in res["items"]}
    assert set(got) == set(want)
    for off, (typ, data) in want.items():
        assert got[off]["type"] == typ and open(got[off]["path"], "rb").read() == data
    assert res["counts"]["jpg"] == 2 and res["counts"]["zip"] == 2
    page = server.ok(f"/api/job/result?id={j['id']}&offset=0&limit=2")
    assert len(page["items"]) == 2 and page["more"] == len(want) - 2
    # pause / resume / stop on a slower scan (big noise image)
    big = str(tmp_path / "big.img")
    with open(big, "wb") as fh:
        for i in range(12):
            fh.write(os.urandom(16 * 1024 * 1024))
    j = server.ok("/api/recover/carve", {"source": big, "out": str(tmp_path / "f2"), "types": ["all"]})
    st = server.ok("/api/job/control", {"id": j["id"], "action": "pause"})
    assert st["state"] in ("paused", "done")
    # only one disk-reading job at a time
    st2, d = server.post("/api/recover/carve", {"source": disk, "out": str(tmp_path / "f3")})
    if st["state"] == "paused":
        assert st2 == 400 and "one job reads a disk at a time" in d["error"]
        time.sleep(0.5)
        assert server.ok(f"/api/job?id={j['id']}")["state"] == "paused"
        assert server.ok("/api/job/control", {"id": j["id"], "action": "resume"})["state"] == "running"
    server.ok("/api/job/control", {"id": j["id"], "action": "stop"})
    st = server.wait(j)
    assert st["state"] in ("stopped", "done")
    jobs = server.ok("/api/jobs?kind=carve")["jobs"]
    assert any(x["id"] == j["id"] for x in jobs)


@pytest.fixture(scope="module")
def win_server():
    api = start_server({"RETROVA_PROCS": "1", "RETROVA_FAKE_WIN": "1"})
    yield api
    stop_server(api)


def test_windows_processes_and_names(win_server, images, tmp_path):
    """Worker processes (spawn, as on Windows) + Windows file-name rules + Windows I/O path."""
    api = win_server
    assert api.ok("/api/info")["workers"] == "processes"
    o = api.ok("/api/recover/open", {"source": need(images, "ext4w")})
    out = str(tmp_path / "out")
    j = api.ok("/api/recover/copy", {"sid": o["sid"], "paths": ["/winnames", "/big.bin", "/docs"], "out": out,
                                     "workers": 3, "reserve_gb": 0.5})
    st = api.wait(j)
    assert st["state"] == "done" and st["failed"] == 0, st
    assert "processes" in st["engine"]
    names = set(os.listdir(os.path.join(out, "winnames")))
    assert names == {"a_b.txt", "_CON.txt", "trailing._", "README.txt", "Readme~2.txt", "q_.txt"} or \
        names == {"a_b.txt", "_CON.txt", "trailing._", "Readme.txt", "README~2.txt", "q_.txt"}, names
    assert st["renamed"] == 5
    with open(os.path.join(out, "big.bin"), "rb") as fh:
        from make_images import tree_files
        assert fh.read() == tree_files(True)["big.bin"]
    v = api.ok("/api/recover/verify", {"from_job": j["id"], "mode": "full", "workers": 3})
    st = api.wait(v)
    assert st["state"] == "done" and st["problems"] == 0 and "processes" in st["engine"], \
        api.ok(f"/api/job/result?id={v['id']}")
    os.remove(os.path.join(out, "winnames", "_CON.txt"))
    v = api.ok("/api/recover/verify", {"from_job": j["id"], "mode": "quick"})
    st = api.wait(v)
    assert st["problems"] == 1
    r = api.ok("/api/recover/repair", {"job": v["id"]})
    st = api.wait(r)
    assert st["state"] == "done" and st["repaired"] == 1
    assert open(os.path.join(out, "winnames", "_CON.txt"), "rb").read() == b"reserved\n"


def test_windows_processes_apfs(win_server, tmp_path):
    api = win_server
    o = api.ok("/api/recover/open", {"source": os.path.join(APFS_DIR, "jhfs_converted.bin")})
    out = str(tmp_path / "out")
    j = api.ok("/api/recover/copy", {"sid": o["sid"], "paths": ["/dir", "/hardlink"], "out": out, "workers": 4})
    st = api.wait(j)
    assert st["state"] == "done" and st["failed"] == 0 and st["copied"] >= 10, st
    assert os.path.getsize(os.path.join(out, "dir", "compressed-lzfse-fork")) == 7873
    v = api.ok("/api/recover/verify", {"from_job": j["id"], "mode": "full", "workers": 4})
    st = api.wait(v)
    assert st["problems"] == 0 and st["counts"]["OK"] >= 10
