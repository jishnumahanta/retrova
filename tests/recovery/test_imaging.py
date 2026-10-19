"""Imaging a failing drive: copy / trim / scrape / retry passes, the ddrescue-style map,
resume, and the SHA-256 fingerprint. The source is a plain file wrapped by a test-only
source that raises OSError on configured ranges (and can "heal" after some failures)."""
import hashlib
import json
import os
import random

import pytest
from conftest import run_job

from retrova.recovery.imagejob import ImageJob
from retrova.recovery.rescuemap import RescueMap
from retrova.recovery.source import ReadOnlySource

KB = 1024
SIZE = 4 * 1024 * KB
SECTOR = 512


@pytest.fixture(scope="module")
def disk(tmp_path_factory):
    """4 MiB of random data with a zero stretch (so the image has a hole)."""
    data = bytearray(random.Random(7).randbytes(SIZE))
    data[3072 * KB:3584 * KB] = bytes(512 * KB)
    path = str(tmp_path_factory.mktemp("failing") / "disk.bin")
    with open(path, "wb") as fh:
        fh.write(data)
    return path, bytes(data)


class FaultySource:
    """Wraps a ReadOnlySource; reads touching a fault range raise OSError. heal_after=N: the
    range reads fine once N reads have failed on it (None = never heals)."""

    def __init__(self, real, faults, hook=None):
        self.real, self.faults, self.hook = real, [list(f) + [0] for f in faults], hook
        self.log = []

    def pread(self, off, n):
        self.log.append((off, n))
        if self.hook:
            self.hook(off, n)
        failing = False
        for f in self.faults:
            start, end, heal, fails = f
            if off < end and off + n > start and (heal is None or fails < heal):
                f[3] += 1
                failing = True
        if failing:
            raise OSError(5, "Input/output error")
        return self.real.pread(off, n)

    def close(self):
        self.real.close()


class FaultyImageJob(ImageJob):
    CHUNK = 256 * KB
    MIN_SKIP = 64 * KB
    SAVE_EVERY = 0.0

    def __init__(self, src, out, faults=(), hook=None, **kw):
        super().__init__(src, 0, SIZE, out, 0.001, "Test", **kw)
        self.faults, self.hook, self.faulty = list(faults), hook, None

    def _open_source(self):
        self.faulty = FaultySource(super()._open_source(), self.faults, self.hook and (lambda o, n: self.hook(self, o, n)))
        return self.faulty


def expected(data, bad):
    out = bytearray(data)
    for s, e in bad:
        out[s:e] = bytes(e - s)
    return bytes(out)


def parse_map(path):
    rows = [ln.split() for ln in open(path).read().splitlines() if ln.strip() and not ln.startswith("#")]
    head = rows[0]
    return head, [(int(s, 16), int(z, 16), t) for s, z, t in rows[1:]]


def bad_blocks(path):
    return [(s, s + z) for s, z, t in parse_map(path)[1] if t != "+"]


BAD = [(1000 * KB, 1000 * KB + 2 * SECTOR), (2048 * KB + 512, 2048 * KB + 1024), (2500 * KB, 2500 * KB + 300 * KB)]


def test_failing_drive_image_matches_outside_bad_ranges(disk, tmp_path):
    src, data = disk
    before = hashlib.sha256(data).hexdigest()
    out = str(tmp_path / "a.img")
    job = FaultyImageJob(src, out, [(s, e, None) for s, e in BAD], retries=1)
    st = run_job(job)
    assert st["state"] == "done" and st["phase"] == "Finished"
    assert open(out, "rb").read() == expected(data, BAD)
    # the map lists exactly the bad ranges (nothing healthy is lost to the bad ones)
    assert bad_blocks(out + ".map") == sorted(BAD)
    head, blocks = parse_map(out + ".map")
    assert sum(z for _, z, _ in blocks) == SIZE and head[1] == "+"
    want_bad = sum(e - s for s, e in BAD)
    assert st["bad_bytes"] == want_bad and st["rescued"] == SIZE - want_bad and st["error_areas"] == 3
    # .unreadable.txt is generated from the map, and no fingerprint is written for a damaged image
    lines = [ln for ln in open(out + ".unreadable.txt").read().splitlines() if not ln.startswith("#")]
    assert lines == [f"{s}\t{e - s}" for s, e in sorted(BAD)]
    assert not os.path.exists(out + ".sha256") and not os.path.exists(out + ".retrova-progress")
    assert "could not be read" in st["message"]
    assert hashlib.sha256(open(src, "rb").read()).hexdigest() == before  # source untouched


def test_healthy_data_is_copied_first_and_skips_grow(disk, tmp_path):
    src, data = disk
    dead = (1024 * KB, 2048 * KB)
    job = FaultyImageJob(src, str(tmp_path / "b.img"), [(*dead, None)], retries=0)
    run_job(job)
    log = [r for r in job.faulty.log[1:]]       # [0] is the identity read
    big = [i for i, (o, n) in enumerate(log) if n > job.TRIM_BLOCK]
    small = [i for i, (o, n) in enumerate(log) if n <= job.TRIM_BLOCK]
    assert max(big) < min(small)                 # every big read comes before any trim/scrape read
    # after the first failure the next read jumps ahead instead of walking into the dead zone
    first_fail = next(i for i, (o, n) in enumerate(log) if dead[0] <= o < dead[1])
    assert log[first_fail + 1][0] >= log[first_fail][0] + log[first_fail][1] + job.MIN_SKIP
    # data past the dead zone is saved before the dead zone has been worked through
    after = next(i for i, (o, n) in enumerate(log) if o >= dead[1])
    assert after < max(i for i, (o, n) in enumerate(log) if n > job.TRIM_BLOCK and dead[0] <= o < dead[1])
    assert open(str(tmp_path / "b.img"), "rb").read() == expected(data, [dead])


def test_trim_and_scrape_reduce_a_big_failure_to_the_bad_sector(disk, tmp_path):
    src, data = disk
    bad = [(1500 * KB + 5 * SECTOR, 1500 * KB + 6 * SECTOR)]
    job = FaultyImageJob(src, str(tmp_path / "c.img"), [(*bad[0], None)], retries=0)
    st = run_job(job)
    assert st["bad_bytes"] == SECTOR and bad_blocks(job.out_path + ".map") == bad
    sizes = {n for o, n in job.faulty.log}
    assert job.TRIM_BLOCK in sizes and SECTOR in sizes   # trim used small blocks, scrape used sectors
    assert open(job.out_path, "rb").read() == expected(data, bad)


def test_retry_pass_recovers_a_flaky_sector_and_writes_sha256(disk, tmp_path):
    src, data = disk
    flaky = (1500 * KB, 1500 * KB + SECTOR)
    # reads of this sector fail 4 times: copy chunk, trim forward, trim backward, scrape - the retry pass gets it
    job = FaultyImageJob(src, str(tmp_path / "d.img"), [(*flaky, 4)], retries=1)
    st = run_job(job)
    assert st["state"] == "done" and st["bad_bytes"] == 0 and st["error_areas"] == 0
    assert open(job.out_path, "rb").read() == data
    sha = open(job.out_path + ".sha256").read()
    assert sha == f"{hashlib.sha256(data).hexdigest()}  d.img\n" and st["sha256"] in sha
    assert not os.path.exists(job.out_path + ".unreadable.txt")


def test_no_retry_pass_leaves_the_flaky_sector_bad_and_no_sha256(disk, tmp_path):
    src, data = disk
    flaky = (1500 * KB, 1500 * KB + SECTOR)
    job = FaultyImageJob(src, str(tmp_path / "e.img"), [(*flaky, 4)], retries=0)
    st = run_job(job)
    assert st["bad_bytes"] == SECTOR and not os.path.exists(job.out_path + ".sha256")
    assert open(job.out_path, "rb").read() == expected(data, [flaky])


def test_reverse_scrape_gives_the_same_image(disk, tmp_path):
    src, data = disk
    job = FaultyImageJob(src, str(tmp_path / "f.img"), [(s, e, None) for s, e in BAD], retries=2, reverse=True)
    st = run_job(job)
    assert st["state"] == "done" and bad_blocks(job.out_path + ".map") == sorted(BAD)
    assert open(job.out_path, "rb").read() == expected(data, BAD)
    scraped = [o for o, n in job.faulty.log if n == SECTOR]
    assert scraped[0] > scraped[len(scraped) // 4]          # walks backwards


@pytest.mark.parametrize("target", ["?", "*", "/", "-"])
def test_resume_after_interruption_in_each_pass(disk, tmp_path, target):
    src, data = disk
    out = str(tmp_path / "g.img")
    faults = [(s, e, None) for s, e in BAD]
    seen = {"n": 0}

    def hook(job, off, n):
        if job.mode == target:
            seen["n"] += 1
            if seen["n"] == 4:
                job.stop()          # as if the app was closed in the middle of this pass

    job1 = FaultyImageJob(src, out, faults, hook=hook, retries=2)
    st1 = run_job(job1)
    assert st1["state"] == "stopped" and st1["phase"] == "Stopped"
    assert os.path.exists(out + ".retrova-progress") and not os.path.exists(out + ".sha256")
    head, blocks = parse_map(out + ".map")
    assert head[1] == target
    done = [(s, s + z) for s, z, t in blocks if t == "+"]

    job2 = FaultyImageJob(src, out, faults, retries=2)          # a fresh start of the app
    st2 = run_job(job2)
    assert st2["state"] == "done"
    assert open(out, "rb").read() == expected(data, BAD)
    # nothing that was already saved is read again
    reread = [(o, o + n) for o, n in job2.faulty.log[1:] if any(o < e and o + n > s for s, e in done)]
    assert reread == []
    assert bad_blocks(out + ".map") == sorted(BAD)


def test_stopped_image_has_no_fingerprint_and_completes_later(disk, tmp_path):
    src, data = disk
    out = str(tmp_path / "h.img")
    job1 = FaultyImageJob(src, out, hook=lambda job, o, n: job.stop() if o >= SIZE // 2 else None)
    assert run_job(job1)["state"] == "stopped" and not os.path.exists(out + ".sha256")
    st = run_job(FaultyImageJob(src, out))
    assert st["state"] == "done" and open(out, "rb").read() == data
    assert open(out + ".sha256").read().split()[0] == hashlib.sha256(data).hexdigest()


def test_progress_file_from_an_older_version_still_resumes(disk, tmp_path):
    src, data = disk
    out = str(tmp_path / "i.img")
    half = SIZE // 2
    with open(out, "wb") as fh:
        fh.write(data[:half])
    src_id = f"{SIZE}:{hashlib.sha1(data[:65536]).hexdigest()}"
    with open(out + ".retrova-progress", "w") as fh:
        json.dump({"pos": half, "total": SIZE, "src_id": src_id, "source": src, "offset": 0}, fh)
    job = FaultyImageJob(src, out)
    assert run_job(job)["state"] == "done" and open(out, "rb").read() == data
    assert all(o >= half for o, n in job.faulty.log[1:])


def test_never_overwrites_a_finished_image_or_a_leftover_map(disk, tmp_path):
    src, data = disk
    out = str(tmp_path / "j.img")
    assert run_job(FaultyImageJob(src, out))["state"] == "done"
    st = run_job(FaultyImageJob(src, out))
    assert st["state"] == "error" and "already exists" in st["error"]
    assert not os.path.exists(out + ".unreadable.txt")           # a refused job writes nothing next to the old image
    other = str(tmp_path / "k.img")
    open(other + ".map", "w").write("# stray\n")
    st = run_job(FaultyImageJob(src, other))
    assert st["state"] == "error" and not os.path.exists(other)


def test_real_source_without_faults_is_byte_identical(disk, tmp_path):
    src, data = disk
    out = str(tmp_path / "l.img")
    job = ImageJob(src, 0, SIZE, out, 0.001)
    job.CHUNK = 256 * KB
    st = run_job(job)
    assert st["state"] == "done" and st["zero"] == 512 * KB and open(out, "rb").read() == data
    assert os.path.getsize(out) == SIZE
    assert ReadOnlySource(src).writable() is False


def test_rescue_map_matches_a_plain_array_model():
    rnd = random.Random(3)
    total, model = 5000, ["?"] * 5000
    m = RescueMap(total)
    for _ in range(400):
        s = rnd.randrange(total)
        z = rnd.randrange(1, min(900, total - s) + 1)
        t = rnd.choice("?*/-+")
        m.set(s, z, t)
        model[s:s + z] = t * z
    flat = "".join(t * z for _, z, t in m.blocks)
    assert flat == "".join(model)
    assert all(a[2] != b[2] for a, b in zip(m.blocks, m.blocks[1:]))     # neighbours are merged
    assert m.starts == [b[0] for b in m.blocks]
    for t in "?*/-+":
        assert m.bytes_of(t) == model.count(t)
        pos = rnd.randrange(total)
        got = m.find(t, pos)
        want = next((i for i in range(pos, total) if model[i] == t), None)
        assert (got[0] if got else None) == want
        if got:
            assert all(model[i] == t for i in range(got[0], got[0] + got[1]))
        got = m.find(t, pos, reverse=True)
        want = next((i for i in range(pos - 1, -1, -1) if model[i] == t), None)
        assert (got[0] + got[1] - 1 if got else None) == want
    again = RescueMap.from_text(m.to_text(1234, "/", 2, 0, "test"), total)
    assert again[0].blocks == m.blocks and again[1:] == (1234, "/", 2)


def test_map_text_is_ddrescue_format_and_rejects_foreign_sizes():
    m = RescueMap(8192)
    m.set(0, 4096, "+")
    text = m.to_text(4096, "?", 1, 0, "test")
    assert "# current_pos  current_status  current_pass" in text
    assert "0x00000000  0x00001000  +" in text and "0x00001000  0x00001000  ?" in text
    with pytest.raises(ValueError):
        RescueMap.from_text(text, 4096)
    with pytest.raises(ValueError):
        RescueMap.from_text(text, 16384)


class FailingDevice(ReadOnlySource):
    """Fails like a real device: the sector-aligned read it is handed fails if it covers the bad sector."""

    bad = (1500 * KB + 3 * SECTOR, 1500 * KB + 4 * SECTOR)

    def _raw(self, start, want):
        if start < self.bad[1] and start + want > self.bad[0]:
            raise OSError(5, "Input/output error")
        return super()._raw(start, want)


def test_scrape_isolates_one_sector_inside_a_4k_block(disk, tmp_path):
    src, data = disk

    class J(FaultyImageJob):
        def _open_source(self):
            return FailingDevice(self.source, self.offset, self.total, align=self.sector)

    job = J(src, str(tmp_path / "m.img"), retries=1)
    st = run_job(job)
    assert job.sector == 512                       # image file: detected as 512
    bad = [FailingDevice.bad]
    assert st["state"] == "done" and st["bad_bytes"] == SECTOR
    assert open(job.out_path, "rb").read() == expected(data, bad)    # the other 7 sectors of that 4 KB are in the image
    assert bad_blocks(job.out_path + ".map") == bad


def test_read_only_source_align_option(disk):
    src, data = disk
    calls = []

    class Spy(ReadOnlySource):
        def _raw(self, start, want):
            calls.append((start, want))
            return super()._raw(start, want)

    s = Spy(src, align=512)
    assert s.pread(1024 + 5, 100) == data[1029:1129] and calls == [(1024, 512)]
    calls.clear()
    s = Spy(src)                                   # default stays 4096-aligned
    assert s.pread(1024 + 5, 100) == data[1029:1129] and calls == [(0, 4096)]
