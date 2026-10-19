"""Small shared helpers. Standard library only (send2trash is optional)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
PLATFORM = "windows" if IS_WIN else "mac" if IS_MAC else "linux"

try:
    import pwd  # type: ignore
except ImportError:  # Windows
    pwd = None


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def long_path(path: str) -> str:
    """Windows: lift the 260-character path limit."""
    if not IS_WIN or path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def display_path(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


# ---------------------------------------------------------------- privileges / ownership
def is_admin() -> bool:
    if IS_WIN:
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


if pwd is not None:
    REAL_UID = int(os.environ.get("SUDO_UID", os.getuid()))
    REAL_GID = int(os.environ.get("SUDO_GID", os.getgid()))
    try:
        REAL_USER = pwd.getpwuid(REAL_UID).pw_name
        REAL_HOME = pwd.getpwuid(REAL_UID).pw_dir
    except KeyError:
        REAL_USER, REAL_HOME = os.environ.get("USER", "user"), os.path.expanduser("~")
else:
    import getpass
    REAL_UID = REAL_GID = 0
    REAL_USER, REAL_HOME = getpass.getuser(), os.path.expanduser("~")


def give_to_user(path: str):
    """When running elevated on macOS/Linux, hand files we create back to the real user."""
    if not IS_WIN and is_admin() and REAL_UID != 0:
        try:
            os.lchown(path, REAL_UID, REAL_GID)
        except OSError:
            pass


def makedirs_owned(path: str):
    missing, p = [], path
    while p and not os.path.exists(p):
        missing.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    for d in reversed(missing):
        try:
            os.mkdir(d)
            give_to_user(d)
        except FileExistsError:
            pass


def free_bytes(path: str) -> int:
    p = path
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return shutil.disk_usage(p or os.sep).free
    except OSError:
        return 0


# ---------------------------------------------------------------- safe deletion: Trash only
def to_trash(paths) -> dict:
    """Move files/folders to the Trash / Recycle Bin. Never deletes permanently.
    Returns {"moved": [...], "failed": [{"path", "error"}]}."""
    moved, failed = [], []
    try:
        from send2trash import send2trash  # type: ignore
    except ImportError:
        send2trash = None
    for p in paths:
        try:
            if not os.path.lexists(p):
                raise FileNotFoundError(p)
            if send2trash is not None:
                send2trash(p)
            elif IS_MAC:
                _mac_trash(p)
            else:
                raise RuntimeError("Trash support is not available on this system")
            moved.append(p)
        except Exception as ex:
            failed.append({"path": p, "error": f"{type(ex).__name__}: {ex}"})
    return {"moved": moved, "failed": failed}


def _mac_trash(p):
    script = f'tell application "Finder" to delete POSIX file "{os.path.abspath(p)}"'
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode:
        raise OSError(r.stderr.strip() or "Finder refused")


def reveal(path: str):
    """Show a file or folder in Finder / Explorer."""
    path = display_path(path)
    if not os.path.exists(path):
        path = os.path.dirname(path)
    if IS_MAC:
        args = ["open", "-R", path] if os.path.isfile(path) else ["open", path]
        if is_admin() and REAL_UID:
            args = ["sudo", "-u", REAL_USER] + args
        subprocess.Popen(args)
    elif IS_WIN:
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", path])
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])


def run(cmd, timeout=30) -> str:
    """Run a helper command and return stdout ('' on any failure)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout
    except subprocess.TimeoutExpired:
        print(f"[retrova] {cmd[0]} timed out after {timeout}s", file=sys.stderr, flush=True)
        return ""
    except Exception:
        return ""


class Once:
    """Thread-safe lazy value."""

    def __init__(self, fn):
        self.fn, self.lock, self.done, self.val = fn, threading.Lock(), False, None

    def get(self):
        with self.lock:
            if not self.done:
                self.val, self.done = self.fn(), True
            return self.val
