"""Optional: carve REAL camera/media/mail files. Skipped unless RETROVA_REAL_SAMPLES points at a folder of
sample files (see tools/fetch_real_samples.py). The extension decides which type each file must be found as."""
import os
import random

import pytest

from retrova.recovery import carve_formats as cf
from retrova.recovery.carve import CarveJob
from conftest import run_job

FOLDER = os.environ.get("RETROVA_REAL_SAMPLES", "")
KINDS = {"cr2", "cr3", "nef", "nrw", "arw", "dng", "orf", "raf", "pef", "rw2", "wmv", "wma", "pst", "ost"}
FILES = sorted(f for f in (os.listdir(FOLDER) if os.path.isdir(FOLDER) else ()) if f.rsplit(".", 1)[-1].lower() in KINDS)
pytestmark = pytest.mark.skipif(not FILES, reason="set RETROVA_REAL_SAMPLES to a folder of real sample files")


def test_real_files_are_found_whole(tmp_path):
    r = random.Random(7)
    img, placed = str(tmp_path / "disk.img"), []
    with open(img, "wb") as fh:
        fh.write(r.randbytes(300_000))
        for name in FILES:
            fh.write(r.randbytes(512 * r.randrange(3, 400)))
            data = open(os.path.join(FOLDER, name), "rb").read()
            placed.append((name, fh.tell(), data))
            fh.write(data)
        fh.write(r.randbytes(200_000))
    job = CarveJob(img, 0, os.path.getsize(img), str(tmp_path / "out"), cf.expand_types(["all"]), 0.01)
    assert run_job(job)["state"] == "done"
    found = {i["offset"]: i for i in job.items}
    for name, off, data in placed:
        it = found.get(off)
        assert it, f"{name}: not found at {off}"
        want = name.rsplit(".", 1)[-1].lower()
        assert it["type"] == want, (name, it["type"])
        assert open(it["path"], "rb").read() == data, f"{name}: bytes differ (size {it['size']} vs {len(data)})"
