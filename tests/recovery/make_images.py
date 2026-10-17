"""Build the filesystem test images used by tests/recovery (Linux, needs root for the
FUSE-populated ones). Run directly to (re)build:  python tests/recovery/make_images.py [outdir]

Images (each holds the same TREE; see tree_files()):
  fat32.img fat16.img fat12.img   mkfs.vfat + mtools (no mounting)
  ext4.img                        mkfs.ext4 -d (no mounting)
  xfs.img                         mkfs.xfs -p protofile (no mounting)
  btrfs.img                       mkfs.btrfs --rootdir (no mounting)
  ntfs.img                        mkntfs + ntfs-3g (FUSE) for folders; ntfscp fallback (root files only)
  exfat.img                       mkfs.exfat + exfat-fuse on a loop device, incl. a fragmented file
  gpt_two.img                     GPT: FAT32 partition + ext4 partition
  mbr_ext.img                     MBR: primary FAT16 + extended partition with logical ext4 and logical FAT32
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile

MB = 1024 * 1024


def _rand(seed, n):
    return random.Random(seed).randbytes(n)


def tree_files(win_names=False) -> dict[str, bytes]:
    """path -> content. Directories are implied; 'emptydir/' marks an empty folder."""
    files = {
        "hello.txt": b"hello world\n",
        "zero.txt": b"",
        "big.bin": _rand(1, 9 * MB + 12345),            # > one 8 MB copy chunk
        "photos/IMG_0001.JPG": _rand(2, 150_000),
        "photos/Holiday Pictures/beach day.png": _rand(3, 70_000),
        "photos/Holiday Pictures/notes - long file name.txt": b"A long file name with spaces\n" * 50,
        "docs/report.pdf": _rand(4, 33_333),
        "docs/sub/deeper/data.bin": _rand(5, 70_000),
        "docs/sub/x.txt": b"x" * 5000,
        "unicode/café über.txt": "Unicode ✓\n".encode(),
        "project/package.json": b'{"name": "demo"}\n',
        "project/package-lock.json": b"{}\n",
        "project/node_modules/left-pad/index.js": b"module.exports = 1\n",
        "project/__pycache__/m.cpython-312.pyc": _rand(6, 300),
        "emptydir/": b"",
    }
    if win_names:
        files.update({
            "winnames/a:b.txt": b"colon\n",
            "winnames/CON.txt": b"reserved\n",
            "winnames/trailing.": b"dot\n",
            "winnames/Readme.txt": b"one\n",
            "winnames/README.txt": b"two\n",
            "winnames/q?.txt": b"question\n",
        })
    return files


def write_tree(root, files):
    for rel, data in files.items():
        p = os.path.join(root, *rel.rstrip("/").split("/"))
        if rel.endswith("/"):
            os.makedirs(p, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
    link = os.path.join(root, "link.txt")
    if not os.path.lexists(link):
        os.symlink("hello.txt", link)
    os.utime(os.path.join(root, "hello.txt"), (1_600_000_000, 1_600_000_000))


def have(*tools):
    return all(shutil.which(t) for t in tools)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def make_fat(path, fat, size_kb, label, tree):
    if os.path.exists(path):
        os.remove(path)
    run(["mkfs.vfat", "-C", "-F", str(fat), "-n", label, path, str(size_kb)])
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
    tops = sorted({os.path.join(tree, n) for n in os.listdir(tree) if n != "link.txt"})
    if fat == 12:  # tiny floppy-sized volume: just a few files
        tops = [os.path.join(tree, n) for n in ("hello.txt", "zero.txt", "docs")]
    run(["mcopy", "-s", "-m", "-i", path] + tops + ["::/"], env=env)


def make_ext4(path, tree, size="48M"):
    if os.path.exists(path):
        os.remove(path)
    run(["mkfs.ext4", "-q", "-L", "retrovaext", "-d", tree, path, size])


def make_xfs(path, tree):
    lines = ["dummy", "0 0", "d--755 0 0"]

    def walk(d, depth):
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if " " in n or ":" in n:
                continue      # protofile names can't contain spaces
            if os.path.islink(p):
                lines.append(f"{n} l--777 0 0 {os.readlink(p)}")
            elif os.path.isdir(p):
                lines.append(f"{n} d--755 0 0")
                walk(p, depth + 1)
                lines.append("$")
            else:
                lines.append(f"{n} ---644 0 0 {p}")
    walk(tree, 0)
    lines.append("$")
    with tempfile.NamedTemporaryFile("w", suffix=".proto", delete=False) as fh:
        fh.write("\n".join(lines) + "\n")
        proto = fh.name
    if os.path.exists(path):
        os.remove(path)
    with open(path, "wb") as fh:
        fh.truncate(320 * MB)
    try:
        run(["mkfs.xfs", "-q", "-L", "retrovaxfs", "-p", proto, path])
    finally:
        os.remove(proto)


def make_btrfs(path, tree):
    if os.path.exists(path):
        os.remove(path)
    with open(path, "wb") as fh:
        fh.truncate(128 * MB)
    run(["mkfs.btrfs", "-q", "-L", "retrovabtr", "--rootdir", tree, path])


def _fuse_ok():
    return os.path.exists("/dev/fuse") and hasattr(os, "geteuid") and os.geteuid() == 0


def make_ntfs(path, tree, size=48 * MB):
    if os.path.exists(path):
        os.remove(path)
    with open(path, "wb") as fh:
        fh.truncate(size)
    run(["mkntfs", "-q", "-F", "-L", "RetrovaNTFS", path])
    mnt = tempfile.mkdtemp()
    mounted = False
    if _fuse_ok() and have("ntfs-3g"):
        try:
            run(["ntfs-3g", path, mnt])
            mounted = True
        except subprocess.CalledProcessError:
            mounted = False
    try:
        if mounted:
            for n in os.listdir(tree):
                s = os.path.join(tree, n)
                if os.path.islink(s):
                    continue
                if os.path.isdir(s):
                    shutil.copytree(s, os.path.join(mnt, n), symlinks=False)
                else:
                    shutil.copy2(s, os.path.join(mnt, n))
            return "full"
        for n in ("hello.txt", "big.bin", "zero.txt"):
            run(["ntfscp", path, os.path.join(tree, n), n])
        return "flat"
    finally:
        if mounted:
            subprocess.run(["umount", mnt], capture_output=True)
        os.rmdir(mnt)


def make_exfat(path, tree, size=40 * MB):
    if os.path.exists(path):
        os.remove(path)
    with open(path, "wb") as fh:
        fh.truncate(size)
    run(["mkfs.exfat", "-L", "RETROVAEX", path])
    if not (_fuse_ok() and have("losetup", "mount.exfat-fuse")):
        return "empty"
    loop = run(["losetup", "-f", "--show", path], text=True).stdout.strip()
    mnt = tempfile.mkdtemp()
    try:
        run(["mount.exfat-fuse", loop, mnt])
        try:
            for n in os.listdir(tree):
                s = os.path.join(tree, n)
                if os.path.islink(s):
                    continue
                if os.path.isdir(s):
                    shutil.copytree(s, os.path.join(mnt, n))
                else:
                    shutil.copy2(s, os.path.join(mnt, n))
            # a fragmented file (needs the FAT chain): fill, punch a hole, then write a bigger file
            with open(os.path.join(mnt, "gap1.bin"), "wb") as fh:
                fh.write(_rand(10, 200_000))
            with open(os.path.join(mnt, "keep.bin"), "wb") as fh:
                fh.write(_rand(11, 50_000))
            os.remove(os.path.join(mnt, "gap1.bin"))
            with open(os.path.join(mnt, "fragmented.bin"), "wb") as fh:
                fh.write(_rand(12, 600_000))
            many = os.path.join(mnt, "many")
            os.mkdir(many)
            for i in range(300):        # a directory spanning several clusters
                with open(os.path.join(many, f"file number {i:04d} with a long name.txt"), "wb") as fh:
                    fh.write(b"%d\n" % i)
        finally:
            subprocess.run(["umount", mnt], capture_output=True)
    finally:
        subprocess.run(["losetup", "-d", loop], capture_output=True)
        os.rmdir(mnt)
    return "full"


def make_partitioned(out, imgs):
    """gpt_two.img (FAT32 + ext4) and mbr_ext.img (FAT16 primary + logical ext4 + logical FAT32)."""
    fat32, ext4, fat16 = imgs["fat32"], imgs["ext4"], imgs["fat16"]

    def place(disk, src, offset):
        with open(src, "rb") as s, open(disk, "r+b") as d:
            d.seek(offset)
            shutil.copyfileobj(s, d, 4 * MB)

    sz32, szext, sz16 = os.path.getsize(fat32), os.path.getsize(ext4), os.path.getsize(fat16)
    gpt = os.path.join(out, "gpt_two.img")
    with open(gpt, "wb") as fh:
        fh.truncate(1 * MB + sz32 + szext + 4 * MB)
    s1 = 2048
    e1 = s1 + sz32 // 512 - 1
    s2 = e1 + 1 + 2048 - ((e1 + 1) % 2048)
    e2 = s2 + szext // 512 - 1
    run(["sgdisk", "-n", f"1:{s1}:{e1}", "-t", "1:0700", "-c", "1:Photos", "-n", f"2:{s2}:{e2}", "-t", "2:8300",
         "-c", "2:Linux", gpt])
    place(gpt, fat32, s1 * 512)
    place(gpt, ext4, s2 * 512)
    mbr = os.path.join(out, "mbr_ext.img")
    total = 1 * MB + sz16 + szext + sz32 + 8 * MB
    with open(mbr, "wb") as fh:
        fh.truncate(total)
    p1 = 2048
    p1n = sz16 // 512
    ext_start = p1 + p1n + 2048 - ((p1 + p1n) % 2048)
    l1 = ext_start + 2048
    l1n = szext // 512
    l2 = l1 + l1n + 2048 - ((l1 + l1n) % 2048) + 2048
    l2n = sz32 // 512
    layout = (f"label: dos\n{p1},{p1n},6\n{ext_start},{l2 + l2n - ext_start + 2048},5\n"
              f"{l1},{l1n},83\n{l2},{l2n},c\n")
    run(["sfdisk", "-q", mbr], input=layout.encode())
    place(mbr, fat16, p1 * 512)
    place(mbr, ext4, l1 * 512)
    place(mbr, fat32, l2 * 512)
    return {"gpt": gpt, "gpt_parts": [(s1 * 512, "fat32"), (s2 * 512, "ext4")],
            "mbr": mbr, "mbr_parts": [(p1 * 512, "fat16"), (l1 * 512, "ext4"), (l2 * 512, "fat32")]}


def build_all(out) -> dict:
    os.makedirs(out, exist_ok=True)
    tree = os.path.join(out, "tree")
    if os.path.exists(tree):
        shutil.rmtree(tree)
    write_tree(tree, tree_files())
    wtree = os.path.join(out, "wtree")
    if os.path.exists(wtree):
        shutil.rmtree(wtree)
    write_tree(wtree, tree_files(win_names=True))
    res = {"tree": tree, "wtree": wtree}
    if have("mkfs.vfat", "mcopy"):
        for fat, kb, lab in ((32, 48000, "RETROVAFAT"), (16, 24000, "F16"), (12, 1440, "F12")):
            p = os.path.join(out, f"fat{fat}.img")
            make_fat(p, fat, kb, lab, tree)
            res[f"fat{fat}"] = p
    if have("mkfs.ext4"):
        res["ext4"] = os.path.join(out, "ext4.img")
        make_ext4(res["ext4"], tree)
        res["ext4w"] = os.path.join(out, "ext4w.img")
        make_ext4(res["ext4w"], wtree)
    if have("mkfs.xfs"):
        res["xfs"] = os.path.join(out, "xfs.img")
        make_xfs(res["xfs"], tree)
    if have("mkfs.btrfs"):
        res["btrfs"] = os.path.join(out, "btrfs.img")
        make_btrfs(res["btrfs"], tree)
    if have("mkntfs"):
        res["ntfs"] = os.path.join(out, "ntfs.img")
        res["ntfs_mode"] = make_ntfs(res["ntfs"], tree)
    if have("mkfs.exfat"):
        res["exfat"] = os.path.join(out, "exfat.img")
        res["exfat_mode"] = make_exfat(res["exfat"], tree)
    if have("sgdisk", "sfdisk") and all(k in res for k in ("fat32", "ext4", "fat16")):
        res.update(make_partitioned(out, res))
    return res


if __name__ == "__main__":
    import json
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "images")
    print(json.dumps(build_all(target), indent=1, default=str))
