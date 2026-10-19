"""Encrypted test images for the unlock tests.

Nothing here is mounted and nothing needs privileges.

  LUKS 1 / LUKS 2   `cryptsetup luksFormat` writes the real header (keyslots, KDF, digest) on a plain file; the
                    file system image is then encrypted into the data area with the volume key that the
                    passphrase unlocks (dissect.fve's cipher, run backwards).
  BitLocker         Microsoft-made metadata from dissect.fve's published test images
                    (tests/unlock_images/bde_*.bin.gz, passphrase / recovery key / suspended / decrypted); the
                    file system image is encrypted into the volume with the volume's own key, sector by sector,
                    following the same virtualised-region and reserved-region rules the reader uses.
  APFS              dissect.apfs's published encrypted container (apfs_encrypted.bin.gz), used as it is.
"""
from __future__ import annotations

import gzip
import os
import random
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(os.path.dirname(os.path.dirname(HERE)), "tests", "unlock_images")
MB = 1024 * 1024

BDE_PASSWORD = "password12!@"
BDE_RECOVERY = "284867-596541-514998-422114-660297-261613-215424-199408"
APFS_PASSWORD = "password"
LUKS_PASSPHRASE = "correct horse battery staple"

# Root-level files of the NTFS volume inside the BitLocker images. BitLocker keeps a few areas of the volume for
# itself (about 35 MB in), so the file system inside is small and has to leave those areas empty.
BDE_FILES = {
    "hello.txt": b"hello from a BitLocker drive\n",
    "zero.txt": b"",
    "holiday.jpg": random.Random(11).randbytes(1_500_000),
    "notes.txt": b"Notes on the drive that was locked.\n" * 400,
    "data.bin": random.Random(12).randbytes(700_001),
}


def gunzip(name, dst):
    with gzip.open(os.path.join(FIXTURES, name), "rb") as a, open(dst, "wb") as b:
        shutil.copyfileobj(a, b, 4 * MB)
    return dst


def have_cryptsetup():
    return shutil.which("cryptsetup") is not None


# ---------------------------------------------------------------- LUKS
# Files inside the LUKS volumes. Small on purpose: the cipher is pure Python and decrypts a few MB a second at best.
LUKS_FILES = {
    "hello.txt": b"hello from a LUKS drive\n",
    "zero.txt": b"",
    "docs/report.pdf": random.Random(21).randbytes(210_000),
    "docs/sub/x.txt": b"x" * 5000,
    "photos/beach.jpg": random.Random(22).randbytes(900_001),
    "unicode/café.txt": "Unicode ✓\n".encode(),
}


def make_small_ext4(path, files, size="8M"):
    """ext4 image holding `files` (mkfs.ext4 -d: no mounting). None when mkfs.ext4 is missing."""
    if not shutil.which("mkfs.ext4"):
        return None
    root = path + ".src"
    for rel, data in files.items():
        dst = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(data)
    subprocess.run(["mkfs.ext4", "-q", "-L", "locked-ext4", "-d", root, path, size], check=True, capture_output=True)
    shutil.rmtree(root)
    return path


def make_luks(path, plain, version, passphrase=LUKS_PASSPHRASE, total=24 * MB, extra=()):
    """A LUKS container (`version` 1 or 2) around the file system image `plain`."""
    from dissect.fve.luks import LUKS
    with open(path, "wb") as fh:
        fh.truncate(total)
    cmd = ["cryptsetup", "luksFormat", "--batch-mode", "--type", f"luks{version}", "--cipher", "aes-xts-plain64",
           "--key-size", "512", "--use-urandom", "--key-file", "-"]
    if not extra:
        cmd += ["--pbkdf-force-iterations", "1000"] + (["--pbkdf", "pbkdf2"] if version == 2 else ["--hash", "sha256"])
    subprocess.run(cmd + list(extra) + [path], input=passphrase.encode(), check=True, capture_output=True)
    with open(path, "r+b") as fh:
        luks = LUKS(fh)
        luks.unlock_with_passphrase(passphrase)
        stream = luks.open()
        step = stream.sector_size * 2048
        with open(plain, "rb") as src:
            pos = 0
            while chunk := src.read(step):
                chunk += bytes(-len(chunk) % stream.sector_size)
                for o in range(0, len(chunk), 4096):
                    block = chunk[o:o + 4096]
                    if any(block):                 # empty blocks stay as they are: only data needs to be readable
                        fh.seek(stream.offset + pos + o)
                        fh.write(stream.cipher.encrypt(block, (pos + o) // 512 + stream.iv_tweak))
                pos += len(chunk)
    return path


# ---------------------------------------------------------------- BitLocker
def make_small_ntfs(path, files, size=30 * MB):
    """NTFS image holding `files` in its root (mkntfs + ntfscp: no mounting). None when the tools are missing."""
    if not (shutil.which("mkntfs") and shutil.which("ntfscp")):
        return None
    with open(path, "wb") as fh:
        fh.truncate(size)
    subprocess.run(["mkntfs", "-q", "-F", "-L", "BitLockerNTFS", path], check=True, capture_output=True)
    scratch = path + ".src"
    os.makedirs(scratch, exist_ok=True)
    for name, data in files.items():
        with open(os.path.join(scratch, name), "wb") as fh:
            fh.write(data)
        subprocess.run(["ntfscp", "-f", path, os.path.join(scratch, name), name], check=True, capture_output=True)
    shutil.rmtree(scratch)
    return path


def make_bitlocker(path, template_gz, plain, how):
    """Copy a BitLocker template volume to `path` and put the file system image `plain` inside it."""
    from dissect.fve.bde import BDE
    from dissect.fve.bde.bde import BitlockerStream, _consolidate_runs
    gunzip(template_gz, path)
    with open(path, "r+b") as fh:
        bde = BDE(fh)
        how(bde)
        stream = bde.open()
        sector = stream.sector_size
        step = sector * 2048
        with open(plain, "rb") as src:
            logical = 0
            while chunk := src.read(step):
                chunk += bytes(-len(chunk) % sector)
                pos = 0
                for kind, phys, count in _consolidate_runs(stream._iter_runs(logical, len(chunk))):
                    data = chunk[pos:pos + count * sector]
                    pos += count * sector
                    if kind == BitlockerStream.RUN_SPARSE:
                        assert not any(data), "the file system uses a sector BitLocker reserves for itself"
                        continue
                    if not any(data):              # empty runs stay as they are: only data needs to be readable
                        continue
                    if kind == BitlockerStream.RUN_ENCRYPTED:
                        data = stream.cipher.encrypt(data, phys)
                    fh.seek(phys * sector)
                    fh.write(data)
                logical += len(chunk)
    return path


def build_unlock_images(out, images) -> dict:
    """-> {key: path}. Keys: apfs_enc, bde_password, bde_recovery, bde_suspended, bde_decrypted, luks2, luks1,
    luks2_argon, locked_disk (+ locked_disk_parts). Entries whose tools or inputs are missing are left out."""
    os.makedirs(out, exist_ok=True)
    res = {"apfs_enc": gunzip("apfs_encrypted.bin.gz", os.path.join(out, "apfs_enc.img"))}
    ntfs = make_small_ntfs(os.path.join(out, "bitlocker_inner.img"), BDE_FILES)
    if ntfs:
        for key, tpl, how in (
                ("bde_password", "bde_password.bin.gz", lambda b: b.unlock_with_passphrase(BDE_PASSWORD)),
                ("bde_recovery", "bde_recovery.bin.gz", lambda b: b.unlock_with_recovery_password(BDE_RECOVERY)),
                ("bde_suspended", "bde_suspended.bin.gz", lambda b: b.unlock_with_clear_key()),
                ("bde_decrypted", "bde_decrypted.bin.gz", lambda b: None)):
            res[key] = make_bitlocker(os.path.join(out, key + ".img"), tpl, ntfs, how)
    plain = make_small_ext4(os.path.join(out, "luks_plain.img"), LUKS_FILES) if have_cryptsetup() else None
    if plain:
        res["luks_plain"] = plain
        res["luks2"] = make_luks(os.path.join(out, "luks2.img"), plain, 2)
        res["luks2_argon"] = make_luks(os.path.join(out, "luks2_argon.img"), plain, 2,
                                       extra=("--pbkdf", "argon2id", "--pbkdf-memory", "8192",
                                              "--pbkdf-parallel", "1", "--pbkdf-force-iterations", "4"))
        res["luks1"] = make_luks(os.path.join(out, "luks1.img"), plain, 1)
    if "bde_password" in res and "luks2" in res and shutil.which("sgdisk"):
        res.update(make_locked_disk(out, res["bde_password"], res["luks2"]))
    return res


def make_locked_disk(out, bde, luks):
    """A GPT disk: a BitLocker partition, then a LUKS partition (so unlocking happens at a partition offset)."""
    disk = os.path.join(out, "locked_disk.img")
    sb, sl = os.path.getsize(bde), os.path.getsize(luks)
    s1 = 2048
    e1 = s1 + sb // 512 - 1
    s2 = e1 + 1 + 2048 - ((e1 + 1) % 2048)
    e2 = s2 + sl // 512 - 1
    with open(disk, "wb") as fh:
        fh.truncate((e2 + 2048) * 512)
    subprocess.run(["sgdisk", "-n", f"1:{s1}:{e1}", "-t", "1:0700", "-c", "1:Windows data", "-n",
                    f"2:{s2}:{e2}", "-t", "2:8300", "-c", "2:Linux data", disk], check=True, capture_output=True)
    with open(disk, "r+b") as d:
        for src, start in ((bde, s1), (luks, s2)):
            d.seek(start * 512)
            with open(src, "rb") as s:
                shutil.copyfileobj(s, d, 4 * MB)
    return {"locked_disk": disk, "locked_disk_parts": [(s1 * 512, "bitlocker", sb), (s2 * 512, "luks", sl)]}


def same_data_blocks(src, plain_path, limit, block=4096):
    """True when every non-empty 4 KiB block of the first `limit` bytes of the plain image reads back identical
    from `src` (the images above only encrypt blocks that hold data)."""
    with open(plain_path, "rb") as fh:
        for off in range(0, limit, block):
            want = fh.read(block)
            if any(want) and src.pread(off, len(want)) != want:
                return False
    return True
