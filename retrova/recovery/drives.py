"""List the computer's disks, partitions and mounted volumes (no admin needed).

macOS: diskutil list -plist + diskutil info -plist (SMART status, bus, offsets)
Windows: PowerShell Get-Disk / Get-Partition / Get-Volume / Get-PhysicalDisk as JSON
Linux: lsblk -J -b (+ /sys/class/block/*/start for offsets, smartctl when available)

The OS-specific parsers are pure functions over the parsed tool output so they can
be tested on any platform.
"""
from __future__ import annotations

import copy
import json
import os
import plistlib
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from retrova.util import IS_MAC, IS_WIN, is_admin

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd, timeout=30) -> bytes:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW)
        return r.stdout or b""
    except Exception:
        return b""


def _free(mount) -> int | None:
    if not mount:
        return None
    try:
        return shutil.disk_usage(mount).free
    except OSError:
        return None


def _usage(mount):
    try:
        u = shutil.disk_usage(mount)
        return u.total, u.free
    except OSError:
        return None, None


def health(state="unknown", detail=""):
    return {"state": state, "detail": detail}


NO_HEALTH = "This drive doesn't report its health (common for USB drives and memory cards)."


_CACHE = {"t": 0.0, "v": None}
_CACHE_LOCK = threading.Lock()
CACHE_SECONDS = 3.0


def list_drives(max_age: float = CACHE_SECONDS) -> dict:
    """{disks, volumes}. Cached for a few seconds: diskutil / PowerShell take a second or two
    and the UI polls the drive strip."""
    with _CACHE_LOCK:
        if _CACHE["v"] is not None and time.time() - _CACHE["t"] < max_age:
            return copy.deepcopy(_CACHE["v"])
        try:
            if IS_MAC:
                v = mac_drives()
            elif IS_WIN:
                v = win_drives()
            else:
                v = linux_drives()
        except Exception as ex:   # never break the UI's drive strip
            return {"disks": [], "volumes": [], "error": f"Couldn't list the drives ({type(ex).__name__}: {ex})"}
        _CACHE.update(t=time.time(), v=v)
        return copy.deepcopy(v)


# ====================================================================== macOS
_MAC_FS = {"apfs": "apfs", "hfs": "hfs+", "msdos": "fat32", "exfat": "exfat", "ntfs": "ntfs"}
_MAC_CONTENT_FS = {"Apple_APFS": "apfs", "Apple_HFS": "hfs+", "Microsoft Basic Data": None, "EFI": "fat32",
                   "Windows_NTFS": "ntfs", "DOS_FAT_32": "fat32", "Linux": None, "Apple_CoreStorage": "corestorage",
                   "Windows_FAT_32": "fat32", "Apple_Boot": None, "Linux Filesystem": None}


def _plist(cmd, timeout=30):
    out = _run(cmd, timeout)
    if not out:
        return {}
    try:
        return plistlib.loads(out)
    except Exception:
        return {}


def mac_drives() -> dict:
    lst = _plist(["diskutil", "list", "-plist"])
    ids = []
    for d in lst.get("AllDisksAndPartitions", []):
        ids.append(d.get("DeviceIdentifier"))
        ids += [p.get("DeviceIdentifier") for p in d.get("Partitions", [])]
        ids += [v.get("DeviceIdentifier") for v in d.get("APFSVolumes", [])]
    ids = [i for i in ids if i]
    with ThreadPoolExecutor(max_workers=8) as ex:
        infos = dict(zip(ids, ex.map(lambda i: _plist(["diskutil", "info", "-plist", i], 20), ids)))
    return parse_mac(lst, infos)


def _mac_health(info):
    s = info.get("SMARTStatus")
    if s == "Verified":
        return health("ok", "The drive's self-check (SMART) reports no problems.")
    if s == "Failing":
        return health("bad", "The drive's self-check (SMART) says it is failing. Copy your files off it now "
                             "and don't write anything new to it.")
    return health("unknown", NO_HEALTH)


def parse_mac(lst: dict, infos: dict) -> dict:
    disks, volumes = [], []
    by_id = {d.get("DeviceIdentifier"): d for d in lst.get("AllDisksAndPartitions", [])}
    # APFS containers: synthesized disks whose physical store is a partition elsewhere
    container_of_store, container_vols = {}, {}
    for did, d in by_id.items():
        stores = [s.get("DeviceIdentifier") for s in d.get("APFSPhysicalStores", []) if s.get("DeviceIdentifier")]
        if d.get("APFSVolumes") is not None and stores:
            for s in stores:
                container_of_store[s] = did
            container_vols[did] = d.get("APFSVolumes", [])
    for did, d in by_id.items():
        info = infos.get(did, {})
        if did in container_vols and info.get("VirtualOrPhysical", "Virtual") != "Physical":
            for v in container_vols[did]:
                vi = infos.get(v.get("DeviceIdentifier"), {})
                mount = v.get("MountPoint") or vi.get("MountPoint") or ""
                if mount:
                    total, free = _usage(mount)
                    volumes.append({"mount": mount, "label": v.get("VolumeName") or vi.get("VolumeName") or "",
                                    "fs": "apfs", "size": total or v.get("Size"), "free": free,
                                    "device": "/dev/" + v.get("DeviceIdentifier", "")})
            continue          # the container itself is listed under its physical partition
        parts = []
        for p in d.get("Partitions", []):
            pid = p.get("DeviceIdentifier", "")
            pi = infos.get(pid, {})
            content = p.get("Content") or pi.get("Content") or ""
            fs = _MAC_FS.get((pi.get("FilesystemType") or "").lower()) or _MAC_CONTENT_FS.get(content)
            label = p.get("VolumeName") or pi.get("VolumeName") or ""
            mount = p.get("MountPoint") or pi.get("MountPoint") or ""
            if pid in container_of_store:
                fs = "apfs"
                cvols = container_vols.get(container_of_store[pid], [])
                names = [v.get("VolumeName", "") for v in cvols if v.get("VolumeName")]
                label = ", ".join(n for n in names if n not in ("Preboot", "Recovery", "VM", "Update")) or label
                mounts = [v.get("MountPoint") or infos.get(v.get("DeviceIdentifier"), {}).get("MountPoint")
                          for v in cvols]
                mounts = [m for m in mounts if m]
                mount = mount or next((m for m in mounts if m == "/System/Volumes/Data"), "") or \
                    (mounts[0] if mounts else "")
            else:
                mounts = [mount] if mount else []
            if (pi.get("FilesystemType") or "").lower() == "msdos":
                fs = {"FAT12": "fat12", "FAT16": "fat16"}.get(pi.get("FilesystemUserVisibleName", "")
                                                               .replace("MS-DOS ", "").strip("()"), "fat32")
            parts.append({"id": pid, "device": "/dev/r" + pid, "offset": pi.get("PartitionMapPartitionOffset"),
                          "size": p.get("Size") or pi.get("Size"), "fs": fs, "label": label, "mount": mount,
                          "mounts": mounts, "free": _free(mount), "content": content})
            if mount and pid not in container_of_store:
                total, free = _usage(mount)
                volumes.append({"mount": mount, "label": label, "fs": fs, "size": total or p.get("Size"),
                                "free": free, "device": "/dev/" + pid})
        if not parts and d.get("MountPoint"):        # a disk without a partition table
            mount = d.get("MountPoint")
            total, free = _usage(mount)
            volumes.append({"mount": mount, "label": d.get("VolumeName", ""), "fs": None, "size": total,
                            "free": free, "device": "/dev/" + did})
        proto = info.get("BusProtocol") or ""
        internal = bool(info.get("Internal", False))
        disks.append({
            "id": did, "name": info.get("MediaName") or info.get("IORegistryEntryName") or did,
            "model": info.get("MediaName") or "", "device": "/dev/r" + did,
            "size": info.get("TotalSize") or info.get("Size") or d.get("Size"),
            "internal": internal, "removable": bool(info.get("RemovableMedia") or info.get("Removable")
                                                    or info.get("Ejectable") or not internal),
            "protocol": proto, "solid_state": info.get("SolidState"),
            "virtual": info.get("VirtualOrPhysical") == "Virtual", "health": _mac_health(info),
            "partitions": parts, "scheme": d.get("Content") or "",
        })
    return {"disks": disks, "volumes": volumes}


# ====================================================================== Windows
_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$d  = @(Get-Disk | Select-Object Number,FriendlyName,Model,SerialNumber,Size,BusType,IsBoot,IsSystem,IsOffline,IsReadOnly,PartitionStyle,HealthStatus,OperationalStatus,Location,UniqueId)
$p  = @(Get-Partition | Select-Object DiskNumber,PartitionNumber,Offset,Size,DriveLetter,Type,GptType,MbrType,AccessPaths,IsHidden,IsActive,Guid)
$v  = @(Get-Volume | Select-Object DriveLetter,FileSystemLabel,FileSystem,Size,SizeRemaining,HealthStatus,Path,DriveType)
$pd = @(Get-PhysicalDisk | Select-Object DeviceId,FriendlyName,MediaType,BusType,HealthStatus,OperationalStatus,Size)
@{disks=$d; parts=$p; vols=$v; phys=$pd} | ConvertTo-Json -Depth 4 -Compress
"""
_WIN_BUS = {"1": "SCSI", "2": "ATAPI", "3": "ATA", "4": "IEEE 1394", "5": "SSA", "6": "Fibre Channel", "7": "USB",
            "8": "RAID", "9": "iSCSI", "10": "SAS", "11": "SATA", "12": "SD", "13": "MMC", "14": "Virtual",
            "15": "File Backed Virtual", "16": "Storage Spaces", "17": "NVMe"}
_WIN_HEALTH = {"0": "ok", "healthy": "ok", "1": "warn", "warning": "warn", "2": "bad", "unhealthy": "bad"}
_WIN_FS = {"ntfs": "ntfs", "fat32": "fat32", "fat": "fat16", "exfat": "exfat", "refs": "refs"}


def win_drives() -> dict:
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                _PS_SCRIPT], timeout=60)
    try:
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    return parse_windows(data)


def _list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _s(x):
    return "" if x is None else str(x)


def parse_windows(data: dict) -> dict:
    vols = _list(data.get("vols"))
    phys = {_s(p.get("DeviceId")): p for p in _list(data.get("phys"))}
    vol_by_letter = {_s(v.get("DriveLetter")).upper(): v for v in vols if _s(v.get("DriveLetter")).strip()}
    vol_by_path = {_s(v.get("Path")).lower(): v for v in vols if v.get("Path")}
    parts_by_disk: dict = {}
    for p in _list(data.get("parts")):
        parts_by_disk.setdefault(_s(p.get("DiskNumber")), []).append(p)
    disks, volumes = [], []
    for v in vols:
        letter = _s(v.get("DriveLetter")).strip()
        if not letter:
            continue
        volumes.append({"mount": f"{letter}:\\", "label": v.get("FileSystemLabel") or "",
                        "fs": _WIN_FS.get(_s(v.get("FileSystem")).lower(), _s(v.get("FileSystem")).lower() or None),
                        "size": v.get("Size"), "free": v.get("SizeRemaining"), "device": v.get("Path") or ""})
    for d in _list(data.get("disks")):
        num = _s(d.get("Number"))
        pd = phys.get(num, {})
        bus = _WIN_BUS.get(_s(d.get("BusType")), _s(d.get("BusType")))
        hs = _WIN_HEALTH.get(_s(pd.get("HealthStatus", d.get("HealthStatus"))).lower(), "unknown")
        ops = " ".join(_s(x) for x in _list(pd.get("OperationalStatus") or d.get("OperationalStatus")))
        if "predictive" in ops.lower() or "fail" in ops.lower():
            hs = "bad"
        detail = {"ok": "Windows reports this drive as healthy.",
                  "warn": "Windows reports a warning for this drive. Back up your files soon.",
                  "bad": "Windows reports this drive as unhealthy. Copy your files off it now and don't write "
                         "anything new to it.", "unknown": NO_HEALTH}[hs]
        parts = []
        for p in sorted(parts_by_disk.get(num, []), key=lambda p: p.get("Offset") or 0):
            letter = _s(p.get("DriveLetter")).strip().strip("\x00")
            v = vol_by_letter.get(letter.upper()) if letter else None
            if v is None:
                for ap in _list(p.get("AccessPaths")):
                    v = vol_by_path.get(_s(ap).lower())
                    if v:
                        break
            fs = _WIN_FS.get(_s(v.get("FileSystem")).lower()) if v else None
            mount = f"{letter}:\\" if letter else ""
            parts.append({"id": f"{num}:{p.get('PartitionNumber')}", "device": f"\\\\.\\PhysicalDrive{num}",
                          "offset": p.get("Offset"), "size": p.get("Size"), "fs": fs,
                          "label": (v or {}).get("FileSystemLabel") or "", "mount": mount,
                          "free": (v or {}).get("SizeRemaining") if v else None,
                          "content": _s(p.get("Type")) or _s(p.get("GptType"))})
        removable = bus in ("USB", "SD", "MMC", "IEEE 1394")
        disks.append({
            "id": num, "name": d.get("FriendlyName") or d.get("Model") or f"Disk {num}",
            "model": d.get("Model") or "", "device": f"\\\\.\\PhysicalDrive{num}", "size": d.get("Size"),
            "internal": not removable, "removable": removable, "protocol": bus,
            "health": health(hs, detail), "partitions": parts,
            "scheme": {"1": "mbr", "2": "gpt", "0": "raw"}.get(_s(d.get("PartitionStyle")),
                                                               _s(d.get("PartitionStyle")).lower()),
            "offline": bool(d.get("IsOffline")), "system": bool(d.get("IsSystem") or d.get("IsBoot")),
        })
    return {"disks": disks, "volumes": volumes}


# ====================================================================== Linux
_LSBLK_COLS = "NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,MODEL,VENDOR,RM,HOTPLUG,TRAN,ROTA,RO,PARTLABEL"
_LINUX_FS = {"vfat": None, "exfat": "exfat", "ntfs": "ntfs", "ntfs3": "ntfs", "ext2": "ext2", "ext3": "ext3",
             "ext4": "ext4", "xfs": "xfs", "btrfs": "btrfs", "apfs": "apfs", "hfsplus": "hfs+", "swap": "swap",
             "crypto_luks": "luks", "bitlocker": "bitlocker", "LVM2_member": "lvm"}


def linux_drives() -> dict:
    out = _run(["lsblk", "-J", "-b", "-o", _LSBLK_COLS], timeout=20)
    try:
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    smart = {}
    if shutil.which("smartctl") and is_admin():
        for d in data.get("blockdevices", []):
            if d.get("type") == "disk":
                smart[d.get("name")] = _smart(d.get("path") or "/dev/" + d.get("name", ""))
    res = parse_linux(data, smart, _sys_start)
    _add_root_volume(res["volumes"])
    return res


def _add_root_volume(volumes):
    """Containers / overlay roots aren't block devices: still show '/' and home in the drive strip."""
    have = {v["mount"] for v in volumes}
    mounts = {}
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                bits = line.split()
                if len(bits) >= 3:
                    mounts[bits[1]] = (bits[0], bits[2])
    except OSError:
        pass
    for m in ("/", os.path.expanduser("~")):
        while m not in mounts and m not in ("/", ""):
            m = os.path.dirname(m)
        if m in have or m not in mounts:
            continue
        total, free = _usage(m)
        dev, fstype = mounts[m]
        volumes.append({"mount": m, "label": "System" if m == "/" else os.path.basename(m), "fs": _linux_fs(fstype, total),
                        "size": total, "free": free, "device": dev if dev.startswith("/dev/") else ""})
        have.add(m)


def _smart(dev):
    try:
        j = json.loads(_run(["smartctl", "-H", "-j", dev], timeout=20).decode() or "{}")
        passed = j.get("smart_status", {}).get("passed")
    except Exception:
        return None
    if passed is True:
        return health("ok", "The drive's self-check (SMART) reports no problems.")
    if passed is False:
        return health("bad", "The drive's self-check (SMART) says it is failing. Copy your files off it now.")
    return None


def _sys_start(kname):
    try:
        with open(f"/sys/class/block/{kname}/start") as fh:
            return int(fh.read().strip()) * 512
    except (OSError, ValueError):
        return None


def _linux_fs(fstype, size):
    t = (fstype or "").lower()
    if t == "vfat":
        return "fat32" if (size or 0) > 2 * 1024 ** 3 else "fat16"
    return _LINUX_FS.get(fstype or "", _LINUX_FS.get(t, t or None))


def _b(x) -> bool:
    return x in (True, 1, "1", "true", "True")


def parse_linux(data: dict, smart: dict | None = None, start_of=lambda k: None) -> dict:
    disks, volumes = [], []
    smart = smart or {}
    for d in data.get("blockdevices", []):
        if d.get("type") not in ("disk",) or not d.get("size"):
            continue
        name = d.get("name", "")
        if name.startswith(("zram", "ram", "loop")):
            continue
        parts = []
        for c in d.get("children") or []:
            if c.get("type") not in ("part", "disk", "md", "crypt", "lvm"):
                continue
            mount = c.get("mountpoint") or ""
            fs = _linux_fs(c.get("fstype"), c.get("size"))
            parts.append({"id": c.get("name"), "device": c.get("path") or "/dev/" + c.get("name", ""),
                          "offset": start_of(c.get("kname") or c.get("name")), "size": c.get("size"), "fs": fs,
                          "label": c.get("label") or c.get("partlabel") or "", "mount": mount,
                          "free": _free(mount)})
            if mount and mount != "[SWAP]":
                total, free = _usage(mount)
                volumes.append({"mount": mount, "label": c.get("label") or "", "fs": fs, "size": total or c.get("size"),
                                "free": free, "device": c.get("path") or ""})
        if not parts and d.get("fstype"):
            mount = d.get("mountpoint") or ""
            fs = _linux_fs(d.get("fstype"), d.get("size"))
            parts.append({"id": name, "device": d.get("path") or "/dev/" + name, "offset": 0, "size": d.get("size"),
                          "fs": fs, "label": d.get("label") or "", "mount": mount, "free": _free(mount)})
            if mount:
                total, free = _usage(mount)
                volumes.append({"mount": mount, "label": d.get("label") or "", "fs": fs, "size": total,
                                "free": free, "device": d.get("path") or ""})
        tran = (d.get("tran") or "").upper()
        removable = _b(d.get("rm")) or _b(d.get("hotplug")) or tran in ("USB", "MMC")
        model = " ".join(x for x in ((d.get("vendor") or "").strip(), (d.get("model") or "").strip()) if x)
        disks.append({
            "id": name, "name": model or name, "model": model, "device": d.get("path") or "/dev/" + name,
            "size": d.get("size"), "internal": not removable, "removable": removable, "protocol": tran,
            "rotational": _b(d.get("rota")), "health": smart.get(name) or health("unknown", NO_HEALTH),
            "partitions": parts,
        })
    return {"disks": disks, "volumes": volumes}
