"""Partition discovery (GPT, MBR incl. extended/logical, bare filesystems) and
filesystem detection by magic numbers. Read-only."""
from __future__ import annotations

import os
import struct
import uuid
import zlib

from .common import human
from .source import ReadOnlySource, device_size
from .unlock import LOCKED

SUPPORTED = ("apfs", "ntfs", "fat12", "fat16", "fat32", "exfat", "ext2", "ext3", "ext4", "xfs", "btrfs", "hfs+")
FS_NAMES = {
    "apfs": "APFS", "ntfs": "NTFS", "fat12": "FAT12", "fat16": "FAT16", "fat32": "FAT32", "exfat": "exFAT",
    "ext2": "ext2", "ext3": "ext3", "ext4": "ext4", "xfs": "XFS", "btrfs": "Btrfs", "hfs+": "Mac OS Extended (HFS+)",
    "bitlocker": "BitLocker (encrypted)", "luks": "LUKS (encrypted)", "refs": "ReFS", "swap": "Linux swap",
    "lvm": "Linux LVM", "iso9660": "CD/DVD image (ISO 9660)", "corestorage": "Core Storage",
}
UNSUPPORTED_WHY = {
    "refs": "ReFS isn't supported yet - use Deep scan instead.",
    "swap": "This is Linux swap space - it holds no files.",
    "lvm": "This is a Linux LVM volume. Retrova can't read LVM yet - use Deep scan instead.",
    "iso9660": "This is a CD/DVD image. Open it with your operating system instead.",
    "corestorage": "This is a Core Storage (old FileVault/Fusion) volume, which isn't supported yet.",
}

LOCKED_WHY = {
    "bitlocker": "Locked with BitLocker. Retrova can open it if you have its password or 48-digit recovery key.",
    "luks": "Locked with LUKS (Linux disk encryption). Retrova can open it if you have its passphrase.",
}

GPT_TYPES = {
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Apple APFS",
    "48465300-0000-11aa-aa11-00306543ecac": "Apple HFS+",
    "53746f72-6167-11aa-aa11-00306543ecac": "Apple Core Storage",
    "426f6f74-0000-11aa-aa11-00306543ecac": "Apple Boot",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Microsoft basic data",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft reserved",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows recovery",
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3": "Windows LDM metadata",
    "af9b60a0-1431-4f62-bc68-3311714a69ad": "Windows LDM data",
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI system",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "Linux root (x86-64)",
    "b921b045-1df0-41c3-af44-4c6f280d3fae": "Linux root (ARM64)",
    "933ac7e1-2eb4-4f13-b844-0e14e2aef915": "Linux home",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "e6d6d379-f507-44c2-a23c-238f2a3dd928": "Linux LVM",
    "a19d880f-05fc-4d3b-a006-743f0f84911e": "Linux RAID",
    "ca7d7ccb-63ed-4c53-861c-1742536059cc": "Linux LUKS",
    "bc13c2ff-59e6-4262-a352-b275fd6f7172": "Linux /boot",
}
MBR_TYPES = {
    0x01: "FAT12", 0x04: "FAT16", 0x06: "FAT16", 0x07: "NTFS / exFAT", 0x0B: "FAT32", 0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)", 0x05: "Extended", 0x0F: "Extended (LBA)", 0x85: "Linux extended", 0x82: "Linux swap",
    0x83: "Linux", 0x8E: "Linux LVM", 0xA8: "Apple UFS", 0xAB: "Apple boot", 0xAF: "Apple HFS+/APFS",
    0xEE: "GPT protective", 0xEF: "EFI system", 0x27: "Windows recovery", 0x17: "Hidden NTFS",
    0x1B: "Hidden FAT32", 0x1C: "Hidden FAT32 (LBA)", 0x42: "Windows dynamic disk",
}
APM_TYPES = {"Apple_HFS": "Apple HFS+", "Apple_HFSX": "Apple HFS+", "Apple_APFS": "Apple APFS",
             "Apple_Boot": "Apple Boot", "Apple_UNIX_SVR2": "Apple UFS", "Apple_Scratch": "Apple scratch"}
EXTENDED = (0x05, 0x0F, 0x85)


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


# ---------------------------------------------------------------- filesystem detection
def detect_fs(src: ReadOnlySource, offset: int = 0, quick: bool = False) -> dict | None:
    """Recognise the filesystem starting at `offset`.

    -> {fs, label, detail, size?} or None. With quick=True labels that need the
    filesystem to be parsed (NTFS, FAT root dir, APFS volume names) are skipped."""
    head = src.pread(offset, 4096)
    if len(head) < 512:
        return None
    r = None
    if head[32:36] == b"NXSB":
        r = {"fs": "apfs", "label": "", "detail": "APFS container"}
        bs = _u32(head, 36)
        if 4096 <= bs <= 65536:
            r["size"] = _u64(head, 40) * bs
    elif head[3:11] == b"NTFS    ":
        r = {"fs": "ntfs", "label": "", "detail": ""}
        bps, spc = _u16(head, 11), head[13]
        if spc > 0x80:
            spc = 1 << (256 - spc)
        r["size"] = _u64(head, 40) * bps if bps else None
    elif head[3:11] == b"EXFAT   ":
        r = {"fs": "exfat", "label": "", "detail": ""}
        r["size"] = _u64(head, 72) * (1 << head[108]) if 9 <= head[108] <= 12 else None
    elif head[3:11] == b"-FVE-FS-":
        r = {"fs": "bitlocker", "label": "", "detail": ""}
    elif head[3:11] == b"ReFS\0\0\0\0":
        r = {"fs": "refs", "label": "", "detail": ""}
    elif head[:6] == b"LUKS\xba\xbe":
        r = {"fs": "luks", "label": "", "detail": ""}
    elif head[:4] == b"XFSB":
        r = {"fs": "xfs", "label": head[108:120].split(b"\0")[0].decode("utf-8", "replace"), "detail": ""}
        r["size"] = struct.unpack_from(">Q", head, 8)[0] * struct.unpack_from(">I", head, 4)[0]
    elif len(head) >= 1024 + 0x80 and head[1024 + 56:1024 + 58] == b"\x53\xef":
        sb = head[1024:]
        compat, incompat = _u32(sb, 0x5C), _u32(sb, 0x60)
        fs = "ext4" if incompat & 0x40 or incompat & 0x80 else "ext3" if compat & 0x4 else "ext2"
        bsize = 1024 << min(_u32(sb, 24), 16)
        blocks = _u32(sb, 4) | ((_u32(sb, 0x150) << 32) if incompat & 0x80 else 0)
        r = {"fs": fs, "label": sb[0x78:0x88].split(b"\0")[0].decode("utf-8", "replace"), "detail": "",
             "size": blocks * bsize}
    elif _hfs_plus(src, offset, head):
        r = _hfs_plus(src, offset, head)
    elif head[512:520] == b"LABELONE":
        r = {"fs": "lvm", "label": "", "detail": ""}
    elif head[4086:4096] in (b"SWAPSPACE2", b"SWAP-SPACE"):
        r = {"fs": "swap", "label": "", "detail": ""}
    else:
        bt = src.pread(offset + 0x10040, 8)
        if bt == b"_BHRfS_M":
            sb = src.pread(offset + 0x10000, 0x400)
            r = {"fs": "btrfs", "label": sb[0x12B:0x12B + 256].split(b"\0")[0].decode("utf-8", "replace"),
                 "detail": "", "size": _u64(sb, 0x70)}
        else:
            fat = _detect_fat(head)
            if fat:
                r = fat
            elif src.pread(offset + 0x8001, 5) == b"CD001":
                r = {"fs": "iso9660", "label": "", "detail": ""}
    if r is None:
        return None
    if not quick:
        _fill_label(src, offset, r)
    return r


def _hfs_plus(src: ReadOnlySource, offset: int, head: bytes) -> dict | None:
    """An HFS+ / HFSX volume header at 1024, or an old HFS wrapper that holds one (as on 1998-2003 Macs).
    HFS stores its numbers big-endian."""
    base = 0
    if head[1024:1026] == b"BD" and head[1024 + 124:1024 + 126] == b"H+":
        first_block, block_size, embed_start = struct.unpack_from(">H", head, 1024 + 28)[0], \
            struct.unpack_from(">I", head, 1024 + 20)[0], struct.unpack_from(">H", head, 1024 + 126)[0]
        base = first_block * 512 + embed_start * block_size
    vh = src.pread(offset + base + 1024, 48) if base else head[1024:1024 + 48]
    if len(vh) < 48 or vh[:2] not in (b"H+", b"HX"):
        return None
    version, bsize, blocks = struct.unpack_from(">H", vh, 2)[0], *struct.unpack_from(">II", vh, 40)
    if version not in (4, 5) or bsize < 512 or bsize & (bsize - 1):
        return None
    return {"fs": "hfs+", "label": "", "size": bsize * blocks,
            "detail": "case-sensitive (HFSX)" if vh[:2] == b"HX" else ""}


def _detect_fat(head: bytes) -> dict | None:
    if head[510:512] != b"\x55\xaa" or head[0] not in (0xEB, 0xE9):
        return None
    bps, spc, rsvd, nfats = _u16(head, 11), head[13], _u16(head, 14), head[16]
    root_ent, tot16, media, fatsz16 = _u16(head, 17), _u16(head, 19), head[21], _u16(head, 22)
    tot32, fatsz32 = _u32(head, 32), _u32(head, 36)
    if bps not in (512, 1024, 2048, 4096) or spc == 0 or spc & (spc - 1) or rsvd == 0 or nfats == 0:
        return None
    if media not in (0xF0, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF):
        return None
    total = tot16 or tot32
    fatsz = fatsz16 or fatsz32
    if not total or not fatsz:
        return None
    root_secs = (root_ent * 32 + bps - 1) // bps
    data = total - (rsvd + nfats * fatsz + root_secs)
    if data <= 0:
        return None
    clusters = data // spc
    if clusters < 4085:
        fs, lab = "fat12", head[43:54]
    elif clusters < 65525:
        fs, lab = "fat16", head[43:54]
    else:
        fs, lab = "fat32", head[71:82]
    label = lab.decode("ascii", "replace").strip()
    if label.upper() == "NO NAME":
        label = ""
    return {"fs": fs, "label": label, "detail": "", "size": total * bps}


def _fill_label(src, offset, r):
    """Best-effort volume label (needs a little parsing). Never raises."""
    fs = r["fs"]
    try:
        if fs == "ntfs":
            from dissect.ntfs import NTFS
            sub = ReadOnlySource(src.path, src.base + offset)
            try:
                r["label"] = NTFS(sub).volume_name or ""
            finally:
                sub.close()
        elif fs == "exfat":
            from .exfat import ExFAT
            sub = ReadOnlySource(src.path, src.base + offset)
            try:
                r["label"] = ExFAT(sub).label
            finally:
                sub.close()
        elif fs == "apfs":
            from .apfs_engine import ApfsContainer
            sub = ReadOnlySource(src.path, src.base + offset)
            try:
                vols = ApfsContainer(sub, cache_blocks=256).volumes
                names = [v.name for v in vols]
                r["volumes"] = names
                r["label"] = ", ".join(names)
                enc = [v.name for v in vols if _safe_bool(lambda: v.is_encrypted)]
                if enc:
                    r["encrypted"] = enc
            finally:
                sub.close()
        elif fs == "hfs+":
            from .hfsplus import HfsPlus
            sub = ReadOnlySource(src.path, src.base + offset)
            try:
                r["label"] = HfsPlus(sub).label
            finally:
                sub.close()
        elif fs.startswith("fat"):
            from .fsview import FatView
            v = FatView({"source": src.path, "offset": src.base + offset, "fs": fs})
            try:
                r["label"] = v.label
            finally:
                v.close()
    except Exception as ex:
        r["detail"] = (r.get("detail") or "") + f" (could not read its details: {type(ex).__name__})"
        r["damaged"] = True


def _safe_bool(fn):
    try:
        return bool(fn())
    except Exception:
        return False


# ---------------------------------------------------------------- partition tables
def _gpt(src: ReadOnlySource, disk_size: int):
    for ss in (512, 4096):
        for lba in (1, None):
            if lba is None:                     # backup header at the last LBA
                if not disk_size:
                    continue
                lba = disk_size // ss - 1
            hdr = src.pread(lba * ss, 92)
            if hdr[:8] != b"EFI PART":
                continue
            hsize = _u32(hdr, 12)
            crc_ok = True
            if 92 <= hsize <= ss:
                full = bytearray(src.pread(lba * ss, hsize))
                want = _u32(full, 16)
                full[16:20] = b"\0\0\0\0"
                crc_ok = (zlib.crc32(bytes(full)) & 0xFFFFFFFF) == want
            table_lba = _u64(hdr, 72)
            n = min(_u32(hdr, 80), 1024)
            esz = _u32(hdr, 84) or 128
            if esz < 128 or esz > 4096:
                continue
            table = src.pread(table_lba * ss, n * esz)
            parts = []
            for i in range(n):
                e = table[i * esz:(i + 1) * esz]
                if len(e) < 128:
                    break
                tguid = uuid.UUID(bytes_le=bytes(e[:16]))
                if tguid.int == 0:
                    continue
                first, last = _u64(e, 32), _u64(e, 40)
                if last < first:
                    continue
                name = e[56:128].decode("utf-16-le", "replace").split("\0")[0]
                t = str(tguid)
                parts.append({"index": i + 1, "offset": first * ss, "size": (last - first + 1) * ss,
                              "type": t, "type_name": GPT_TYPES.get(t, "Unknown type"), "name": name})
            notes = []
            if not crc_ok:
                notes.append("The partition table's checksum doesn't match - it may be damaged.")
            if lba != 1:
                notes.append("The main partition table is damaged; using the backup copy at the end of the disk.")
            return {"scheme": "gpt", "sector_size": ss, "parts": parts, "notes": notes}
    return None


def _apm(src: ReadOnlySource, s0: bytes):
    """Apple Partition Map, the table on pre-Intel Macs and old external drives (numbers are big-endian)."""
    if s0[:2] != b"ER":
        return None
    bs = struct.unpack_from(">H", s0, 2)[0]
    if bs not in (512, 1024, 2048, 4096):
        bs = 512
    first = src.pread(bs, bs)
    if first[:2] != b"PM":
        return None
    parts = []
    for i in range(min(struct.unpack_from(">I", first, 4)[0], 128)):
        e = first if i == 0 else src.pread(bs * (1 + i), bs)
        if e[:2] != b"PM":
            break
        start, count = struct.unpack_from(">II", e, 8)
        ptype = e[48:80].split(b"\0")[0].decode("ascii", "replace")
        if not count or ptype in ("Apple_partition_map", "Apple_Free"):
            continue
        parts.append({"index": i + 1, "offset": start * bs, "size": count * bs, "type": ptype,
                      "type_name": "Apple driver" if ptype.startswith("Apple_Driver") else APM_TYPES.get(ptype, ptype),
                      "name": e[16:48].split(b"\0")[0].decode("ascii", "replace")})
    return {"scheme": "apm", "sector_size": bs, "parts": parts, "notes": []}


def _mbr(src: ReadOnlySource, s0: bytes, disk_size: int):
    if s0[510:512] != b"\x55\xaa":
        return None
    entries = []
    for i in range(4):
        e = s0[446 + 16 * i:446 + 16 * (i + 1)]
        status, ptype = e[0], e[4]
        if status not in (0x00, 0x80) or ptype == 0:
            continue
        entries.append((i + 1, ptype, _u32(e, 8), _u32(e, 12)))
    if not entries:
        return None
    if disk_size:
        # a partition table must point inside the disk
        if any(start * 512 >= disk_size for _, _, start, _ in entries if start):
            if not any(start * 4096 < disk_size for _, _, start, _ in entries):
                return None
    parts = []
    for idx, ptype, start, count in entries:
        if ptype == 0xEE:
            continue
        if ptype in EXTENDED:
            parts.extend(_ebr_chain(src, start, disk_size))
            continue
        parts.append({"index": idx, "offset": start * 512, "size": count * 512, "type": f"0x{ptype:02X}",
                      "type_name": MBR_TYPES.get(ptype, "Unknown type"), "name": ""})
    return {"scheme": "mbr", "sector_size": 512, "parts": parts, "notes": []}


def _ebr_chain(src, ext_start, disk_size):
    out, cur, seen, idx = [], ext_start, set(), 5
    while cur and cur not in seen and len(out) < 128:
        seen.add(cur)
        ebr = src.pread(cur * 512, 512)
        if len(ebr) < 512 or ebr[510:512] != b"\x55\xaa":
            break
        e1, e2 = ebr[446:462], ebr[462:478]
        if e1[4] and _u32(e1, 12):
            start = cur + _u32(e1, 8)
            out.append({"index": idx, "offset": start * 512, "size": _u32(e1, 12) * 512,
                        "type": f"0x{e1[4]:02X}", "type_name": MBR_TYPES.get(e1[4], "Unknown type"),
                        "name": "", "logical": True})
            idx += 1
        nxt = _u32(e2, 8)
        cur = ext_start + nxt if e2[4] in EXTENDED and nxt else 0
    return out


def scan(path: str, size: int | None = None) -> dict:
    """Describe a disk or image: {scheme, size, sector_size, parts: [...], notes}.

    Every part gets {index, offset, size, type, type_name, name, fs, label, detail,
    supported, why?}. A disk without a partition table that holds one filesystem
    becomes scheme "none" with one part at offset 0."""
    src = ReadOnlySource(path)
    try:
        disk_size = size or device_size(path) or 0
        s0 = src.pread(0, 512)
        res = _gpt(src, disk_size) or _apm(src, s0)
        if res is None:
            fs0 = detect_fs(src, 0)
            if fs0 is not None:
                part = {"index": 0, "offset": 0, "size": fs0.get("size") or disk_size, "type": "",
                        "type_name": "Whole disk", "name": ""}
                part.update(fs0)
                res = {"scheme": "none", "sector_size": 512, "parts": [part], "notes": []}
            else:
                res = _mbr(src, s0, disk_size) or {"scheme": "unknown", "sector_size": 512, "parts": [],
                                                    "notes": []}
        for p in res["parts"]:
            if "fs" not in p:
                det = detect_fs(src, p["offset"])
                if det is None and res["scheme"] == "mbr":
                    # some USB enclosures expose 4K sectors: MBR LBAs are then in 4096-byte units
                    det4 = detect_fs(src, p["offset"] * 8)
                    if det4 is not None:
                        p["offset"] *= 8
                        p["size"] *= 8
                        det = det4
                if det is not None:
                    p.update({k: v for k, v in det.items() if k != "size"})
                else:
                    p.update({"fs": None, "label": "", "detail": ""})
            p["supported"] = p.get("fs") in SUPPORTED or p.get("fs") in LOCKED
            if p.get("fs") in LOCKED:
                p["locked"] = True
                p["why"] = LOCKED_WHY[p["fs"]]
            elif p.get("fs") and not p["supported"]:
                p["why"] = UNSUPPORTED_WHY.get(p["fs"], "This filesystem isn't supported.")
            elif not p.get("fs"):
                p["why"] = _unknown_why(p)
            p["fs_name"] = FS_NAMES.get(p.get("fs") or "", "Unknown")
        res["size"] = disk_size
        return res
    finally:
        src.close()


def _unknown_why(p) -> str:
    tn = p.get("type_name", "")
    if tn in ("Microsoft reserved", "BIOS boot", "Apple Boot", "Apple driver", "Windows LDM metadata"):
        return "A small system area with no files in it."
    if tn == "Apple APFS":
        return ("This should be an APFS partition but no APFS container was found at its start - "
                "it may be badly damaged. Try Deep scan.")
    return "No filesystem was recognised here. It may be empty, encrypted or damaged - Deep scan can still find files."


def describe_part(p: dict) -> str:
    bits = [FS_NAMES.get(p.get("fs") or "", "Unknown")]
    if p.get("label"):
        bits.append(f"“{p['label']}”")
    if p.get("size"):
        bits.append(human(p["size"]))
    return " · ".join(bits)


def best_part(parts: list) -> dict | None:
    """The partition a person most likely wants: supported, biggest, APFS first."""
    cands = [p for p in parts if p.get("supported")]
    if not cands:
        return None
    small = {"Microsoft reserved", "EFI system", "Windows recovery", "Apple Boot"}
    return sorted(cands, key=lambda p: (p.get("type_name") in small, bool(p.get("locked")), p.get("fs") != "apfs",
                                        -(p.get("size") or 0)))[0]


def container_format(path: str) -> str | None:
    """A friendly explanation when an image file is a format Retrova can't read directly."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(4096)
            tail = b""
            if size >= 512:
                fh.seek(size - 512)
                tail = fh.read(512)
    except OSError:
        return None
    if tail[:4] == b"koly":
        return ("This is a compressed or encrypted Mac disk image (.dmg). Double-click it in Finder to open it, "
                "or convert it with: hdiutil convert image.dmg -format UDRW -o image-raw.dmg")
    if head[:4] == b"sprs":
        return "This is a Mac sparse disk image. Double-click it in Finder to open it instead."
    if head[:4] == b"QFI\xfb":
        return ("This is a QEMU (qcow2) virtual disk. Convert it to a raw image first, for example: "
                "qemu-img convert -O raw disk.qcow2 disk.img")
    if head[:4] == b"KDMV" or head[:21] == b"# Disk DescriptorFile":
        return "This is a VMware (VMDK) virtual disk. Convert it to a raw image first (qemu-img convert -O raw)."
    if head[:8] == b"vhdxfile":
        return "This is a Hyper-V (VHDX) virtual disk. Mount it in Windows, or convert it to a raw image first."
    if head[:8] == b"conectix" or tail[:8] == b"conectix" and tail[60:64] != b"\0\0\0\x02":
        return "This is a dynamic VHD virtual disk. Mount it in Windows, or convert it to a raw image first."
    if head[:8] == b"EVF\x09\x0d\x0a\xff\x00":
        return "This is an EnCase (E01) forensic image. Convert it to a raw image first (for example with ewfexport)."
    if head[:4] in (b"PK\x03\x04", b"7z\xbc\xaf", b"Rar!") or head[:3] == b"\x1f\x8b\x08":
        return "This looks like a compressed archive, not a disk image. Extract it first."
    return None
