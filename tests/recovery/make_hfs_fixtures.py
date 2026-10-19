"""Rebuild tests/hfs_images/*.gz. Needs a Mac (hdiutil); run it from the repository root:

    python3 tests/recovery/make_hfs_fixtures.py

hdiutil only creates and fills the new image file; nothing is mounted. The images are plain raw
HFS+ volumes (no partition table), gzipped because they are mostly empty space."""
import gzip
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from hfs_tree import FILES, HARDLINKS, MTIME, RESOURCE_FORKS, SYMLINKS, VOLNAME  # noqa: E402

OUT = os.path.join(HERE, "..", "hfs_images")
KINDS = {"hfsplus": "HFS+", "hfsx": "HFSX", "jhfs": "Journaled HFS+"}


def write_source(root):
    for rel, data in FILES.items():
        p = os.path.join(root, *rel.rstrip("/").split("/"))
        if rel.endswith("/"):
            os.makedirs(p, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
    for rel, data in RESOURCE_FORKS.items():
        with open(os.path.join(root, rel, "..namedfork", "rsrc"), "wb") as fh:
            fh.write(data)
    for name, target in SYMLINKS.items():
        os.symlink(target, os.path.join(root, name))
    for name, target in HARDLINKS.items():
        os.link(os.path.join(root, target), os.path.join(root, name))
    os.utime(os.path.join(root, "hello.txt"), (MTIME, MTIME))


def main():
    os.makedirs(OUT, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src")
        os.mkdir(src)
        write_source(src)
        for key, fs in KINDS.items():
            img = os.path.join(tmp, key + ".dmg")
            subprocess.run(["hdiutil", "create", "-size", "40m", "-fs", fs, "-volname", VOLNAME, "-srcfolder", src,
                            "-format", "UDRW", "-layout", "NONE", img[:-4]], check=True)
            with open(img, "rb") as fi, gzip.open(os.path.join(OUT, key + ".img.gz"), "wb", 9) as fo:
                shutil.copyfileobj(fi, fo)


if __name__ == "__main__":
    main()
