"""Shared helpers for the recovery package: naming rules, output safety, constants.

Standard library only so that Windows worker processes (spawn) import it cheaply.
"""
from __future__ import annotations

import fnmatch
import os
import re

from retrova.util import IS_MAC, IS_WIN, REAL_HOME, give_to_user, human, long_path, makedirs_owned

# re-exported for the rest of the package
__all__ = ["IS_MAC", "IS_WIN", "REAL_HOME", "give_to_user", "human", "long_path", "makedirs_owned",
           "FAKE_WIN", "WIN_RULES", "USE_PROCESSES", "CHUNK", "ALIGN", "PART_SUFFIX", "INCOMPLETE_SUFFIX",
           "LOG_DIR", "LEGACY_LOG_DIRS", "VERIFY_DIR", "MAX_WORKERS", "unique_name", "makedirs_owned_safe",
           "excluded", "join_src", "norm_src", "safe_name", "NameMap", "expand_out", "is_device_path",
           "check_out_root", "friendly_error"]

# Test hooks (also accept the legacy T5 Rescue names):
#   RETROVA_FAKE_WIN=1  Windows I/O + Windows file-name rules on macOS/Linux
#   RETROVA_PROCS=1     copy / verify workers are separate processes (spawn), as on Windows
FAKE_WIN = os.environ.get("RETROVA_FAKE_WIN") == "1" or os.environ.get("T5R_FAKE_WIN") == "1"
WIN_RULES = IS_WIN or FAKE_WIN
USE_PROCESSES = IS_WIN or os.environ.get("RETROVA_PROCS") == "1" or os.environ.get("T5R_PROCS") == "1"

CHUNK = 8 * 1024 * 1024          # copy / compare block
ALIGN = 4096                     # raw device reads are aligned to this
PART_SUFFIX = ".retrova.part"     # unfinished file being written
INCOMPLETE_SUFFIX = ".INCOMPLETE"
LOG_DIR = "_retrova_logs"         # copy logs + skip reports inside the output folder
LEGACY_LOG_DIRS = ("_t5rescue_logs",)
VERIFY_DIR = "_recovery-verify"
MAX_WORKERS = 12 if USE_PROCESSES else 8


def unique_name(dp: str) -> str:
    """'<dp>.retrova-N' that does not exist yet (never overwrite a different file)."""
    n = 2
    while os.path.lexists(f"{dp}.retrova-{n}"):
        n += 1
    return f"{dp}.retrova-{n}"


def makedirs_owned_safe(path: str):
    """mkdir -p (each new folder handed to the real user when running elevated)."""
    if path:
        makedirs_owned(path)


def excluded(name: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def join_src(parent: str, name: str) -> str:
    return (parent.rstrip("/") + "/" + name) if parent not in ("", "/") else "/" + name


def norm_src(path: str) -> str:
    """Normalise a path inside the source filesystem to '/a/b'."""
    parts = [p for p in str(path or "/").replace("\\", "/").split("/") if p and p != "."]
    return "/" + "/".join(parts)


_WIN_BAD = set('<>:"/\\|?*') | {chr(i) for i in range(32)}
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_name(name: str) -> str:
    """Make a source file name valid on the output filesystem."""
    if not WIN_RULES:
        s = name.replace("/", ":")
        return s if s not in ("", ".", "..") else "_" + s
    s = "".join("_" if ch in _WIN_BAD else ch for ch in name)
    if s.endswith((" ", ".")):
        s += "_"
    if s.split(".")[0].upper().rstrip() in _WIN_RESERVED:
        s = "_" + s
    return s or "_"


class NameMap:
    """Per-folder mapping of source names to unique, valid output names (deterministic).

    Two names that only differ in case (fine on Linux/APFS case-sensitive volumes)
    or that become equal after Windows character replacement get '~2', '~3'..."""

    def __init__(self):
        self.used: dict[str, str] = {}

    def get(self, name: str) -> str:
        sn = safe_name(name)
        if self.used.get(sn.lower(), name) != name:
            stem, dot, ext = sn.rpartition(".")
            if not dot or not stem:
                stem, ext, dot = sn, "", ""
            n = 2
            while True:
                cand = f"{stem}~{n}{dot}{ext}"
                if cand.lower() not in self.used:
                    sn = cand
                    break
                n += 1
        self.used[sn.lower()] = name
        return sn


def expand_out(path: str) -> str:
    path = str(path or "").strip()
    if not path:
        return ""
    if path.startswith("~"):
        path = REAL_HOME + path[1:]
    return os.path.abspath(path)


def is_device_path(path: str) -> bool:
    p = str(path)
    return p.startswith("/dev/") or p.startswith("\\\\.\\") or p.upper().startswith("\\\\?\\GLOBALROOT")


def check_out_root(out: str, source: str | None = None):
    """Refuse output locations that could touch the source drive."""
    if not out:
        raise ValueError("Choose a folder to save the recovered files in.")
    if is_device_path(out):
        raise ValueError("Please choose a folder to save into, not a drive device.")
    if not source:
        return
    src = os.path.abspath(source) if not is_device_path(source) else source
    real_out = os.path.realpath(out)
    if not is_device_path(source):
        rs = os.path.realpath(src)
        if real_out == rs or real_out.startswith(rs + os.sep):
            raise ValueError("The output can't be inside the disk image you are recovering from.")
        return
    # Raw device: refuse a folder that lives on that very device (only possible if it is mounted).
    try:
        dev_rdev = os.stat(_block_twin(source)).st_rdev
    except (OSError, ValueError):
        return
    p = real_out
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        out_dev = os.stat(p).st_dev
    except OSError:
        return
    if dev_rdev and out_dev == dev_rdev:
        raise ValueError("That folder is on the drive you are recovering from. Save to a different drive - "
                         "writing to a damaged drive can destroy the files you want back.")


def _block_twin(dev: str) -> str:
    """macOS: /dev/rdiskN -> /dev/diskN (the block device mounted filesystems report)."""
    m = re.match(r"^/dev/r(disk\d+.*)$", dev)
    return "/dev/" + m.group(1) if m else dev


def friendly_error(ex: BaseException) -> str:
    """A short, plain-English description of a read error for the UI."""
    msg = str(ex) or type(ex).__name__
    if isinstance(ex, PermissionError):
        return "Permission denied - Retrova needs administrator rights to read this drive."
    if isinstance(ex, FileNotFoundError):
        return "Not found."
    if isinstance(ex, OSError) and getattr(ex, "errno", None) in (5, 6, 19, 1117, 23, 1167):
        return f"The drive reported a read error ({msg})."
    return msg
