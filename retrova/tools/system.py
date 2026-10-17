"""System check: CPU, memory (with pressure), top apps, disks, free-up-memory, quit app."""
from __future__ import annotations

import os
import platform
import re
import subprocess
import time

from .. import util
from ..server import route, ApiError
from ..util import IS_MAC, IS_WIN


def _psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:
        return None


def _mac_pressure():
    """'normal' | 'warn' | 'critical' from memory_pressure / sysctl."""
    out = util.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], 5).strip()
    return {"1": "normal", "2": "warn", "4": "critical"}.get(out)


def _model():
    if IS_MAC:
        return util.run(["sysctl", "-n", "hw.model"], 5).strip() or "Mac"
    if IS_WIN:
        return platform.node()
    try:
        with open("/sys/devices/virtual/dmi/id/product_name") as fh:
            return fh.read().strip()
    except OSError:
        return platform.node()


def _os_name():
    if IS_MAC:
        v = platform.mac_ver()[0]
        return f"macOS {v}" if v else "macOS"
    if IS_WIN:
        return f"Windows {platform.release()} ({platform.version()})"
    return f"{platform.system()} {platform.release()}"


SYSTEM_NAMES = {"kernel_task", "launchd", "WindowServer", "loginwindow", "System", "Registry", "smss.exe",
                "csrss.exe", "wininit.exe", "services.exe", "lsass.exe", "winlogon.exe", "svchost.exe",
                "dwm.exe", "explorer.exe", "systemd", "init", "Memory Compression", "Secure System", "Idle"}


def _top(ps, n=12):
    procs = []
    me = os.getpid()
    for p in ps.process_iter(["pid", "name", "memory_info", "username"]):
        try:
            mi = p.info.get("memory_info")
            if not mi:
                continue
            procs.append({"pid": p.info["pid"], "name": p.info.get("name") or "?", "rss": int(mi.rss),
                          "user": p.info.get("username") or "", "self": p.info["pid"] == me,
                          "system": (p.info.get("name") or "") in SYSTEM_NAMES or p.info["pid"] <= 1})
        except Exception:
            continue
    procs.sort(key=lambda d: d["rss"], reverse=True)
    top = procs[:n]
    # CPU% needs two samples; take a short one for the top entries only
    for d in top:
        try:
            d["proc"] = ps.Process(d["pid"])
            d["proc"].cpu_percent(None)
        except Exception:
            d["proc"] = None
    time.sleep(0.25)
    for d in top:
        pr = d.pop("proc", None)
        try:
            d["cpu"] = round(pr.cpu_percent(None), 1) if pr else 0.0
        except Exception:
            d["cpu"] = 0.0
    return top


@route("GET", "/api/system")
def system(q, body):
    ps = _psutil()
    out = {"os": _os_name(), "model": _model(), "platform": util.PLATFORM, "admin": util.is_admin(),
           "psutil": ps is not None}
    if ps is None:
        out.update({"cpu": None, "memory": None, "top": [], "disks": [], "uptime": None,
                    "message": "Detailed system information isn't available on this system."})
        return out
    out["cpu"] = out["memory"] = None
    try:
        vm = ps.virtual_memory()
        try:
            sw = ps.swap_memory()
            swap_used, swap_total = sw.used, sw.total
        except Exception:            # Windows with performance counters disabled
            swap_used = swap_total = 0
        pressure = _mac_pressure() if IS_MAC else ("critical" if vm.percent > 92 else "warn" if vm.percent > 80 else "normal")
        out["memory"] = {"total": vm.total, "used": vm.total - vm.available, "available": vm.available,
                         "percent": vm.percent, "pressure": pressure or "normal", "swap_used": swap_used,
                         "swap_total": swap_total}
    except Exception:
        pass
    try:
        out["cpu"] = {"percent": ps.cpu_percent(interval=0.2), "cores": ps.cpu_count() or 1}
    except Exception:
        out["cpu"] = {"percent": None, "cores": ps.cpu_count() or 1}
    try:
        out["top"] = _top(ps, int(q.get("n", 12) or 12))
    except Exception:
        out["top"] = []
    disks, seen = [], set()
    for part in ps.disk_partitions(all=False):
        mp = part.mountpoint
        if mp in seen or (not IS_WIN and (mp.startswith("/System/Volumes/") and mp != "/System/Volumes/Data")):
            continue
        if part.fstype in ("squashfs", "overlay", "tmpfs", "devtmpfs", "autofs", "nullfs"):
            continue
        try:
            du = ps.disk_usage(mp)
        except Exception:
            continue
        seen.add(mp)
        disks.append({"mount": mp, "fs": part.fstype, "device": part.device, "total": du.total,
                      "used": du.used, "free": du.free, "percent": du.percent})
    out["disks"] = disks
    try:
        out["uptime"] = time.time() - ps.boot_time()
    except Exception:
        out["uptime"] = None
    return out


def _available():
    ps = _psutil()
    return ps.virtual_memory().available if ps else None


@route("POST", "/api/system/free_memory")
def free_memory(q, body):
    before = _available()
    if IS_MAC:
        if not util.is_admin():
            return {"ok": False, "needs_admin": True, "before": before, "after": before, "method": "purge",
                    "message": "Freeing memory on a Mac needs your administrator password. "
                               "Unlock admin access, then try again. (macOS also frees memory by itself when it's needed.)"}
        r = subprocess.run(["purge"], capture_output=True, text=True, timeout=120)
        method = "purge"
        if r.returncode:
            raise ApiError("macOS refused to free memory: " + (r.stderr.strip() or "unknown reason"))
    elif IS_WIN:
        method = "trim working sets"
        _win_empty_working_sets()
    else:
        method = "drop caches"
        if not util.is_admin():
            return {"ok": False, "needs_admin": True, "before": before, "after": before, "method": method,
                    "message": "Freeing memory needs administrator rights on this system."}
        try:
            os.sync()
            with open("/proc/sys/vm/drop_caches", "w") as fh:
                fh.write("3")
        except OSError as ex:
            raise ApiError(f"Couldn't free memory: {ex}")
    time.sleep(0.5)
    after = _available()
    gained = (after - before) if (after is not None and before is not None) else None
    msg = ("Done." if gained is None else
           f"Freed {util.human(max(gained, 0))} - {util.human(after)} is available now." if gained > 0 else
           "Done - there wasn't much to free; your memory was already in good shape.")
    return {"ok": True, "before": before, "after": after, "freed": gained, "method": method, "message": msg}


def _win_empty_working_sets():
    import ctypes
    from ctypes import wintypes
    ps = _psutil()
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    PROCESS_SET_QUOTA, PROCESS_QUERY_INFORMATION = 0x0100, 0x0400
    pids = [p.pid for p in ps.process_iter()] if ps else []
    for pid in pids:
        h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, wintypes.DWORD(pid))
        if h:
            try:
                psapi.EmptyWorkingSet(h)
            finally:
                k32.CloseHandle(h)


@route("POST", "/api/system/quit")
def quit_app(q, body):
    ps = _psutil()
    if ps is None:
        raise ApiError("Quitting apps from Retrova isn't available on this system.")
    try:
        pid = int(body.get("pid"))
    except (TypeError, ValueError):
        raise ApiError("Pick an app to quit.")
    if pid <= 1 or pid == os.getpid() or pid == os.getppid():
        raise ApiError("That one can't be quit from here.")
    try:
        p = ps.Process(pid)
        name = p.name()
        user = p.username() or ""
    except ps.NoSuchProcess:
        raise ApiError("That app has already closed.", 404)
    except Exception as ex:
        raise ApiError(f"Couldn't look at that app: {ex}")
    if name in SYSTEM_NAMES or user in ("root", "SYSTEM", "NT AUTHORITY\\SYSTEM", "_windowserver") or \
            re.match(r"^(com\.apple\.|_)", name):
        raise ApiError(f"{name} is part of the system - quitting it could make your computer unstable.")
    try:
        p.terminate()
        p.wait(timeout=5)
        return {"ok": True, "message": f"{name} has quit."}
    except ps.TimeoutExpired:
        return {"ok": True, "message": f"Asked {name} to quit - it may show a 'save changes?' prompt first."}
    except ps.AccessDenied:
        raise ApiError(f"{name} belongs to another user or the system, so it can't be quit from here.")
