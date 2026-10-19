"""App / platform endpoints: /api/info, /api/elevate, /api/drives."""
from __future__ import annotations

import gc
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request

from retrova import __version__
from retrova.server import ApiError, route
from retrova.util import IS_MAC, IS_WIN, PLATFORM, REAL_GID, REAL_HOME, REAL_UID, REAL_USER, is_admin

from .common import FAKE_WIN, USE_PROCESSES
from .drives import list_drives
from .fsview import available
from .partitions import FS_NAMES, SUPPORTED
from .regen import DEV_JUNK

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _app_data():
    d = os.environ.get("RETROVA_APP_DATA")
    if d:
        return d
    try:
        from retrova.__main__ import app_data_dir
        return app_data_dir()
    except Exception:
        return ""


@route("GET", "/api/info")
def api_info(q, body):
    admin = is_admin()
    return {
        "version": __version__, "platform": PLATFORM, "admin": admin,
        "can_elevate": (IS_MAC or IS_WIN) and not admin,
        "user": REAL_USER, "home": REAL_HOME, "app_data": _app_data(),
        # extras
        "elevated": os.environ.get("RETROVA_ELEVATED") == "1" or admin,
        "default_out": os.path.join(REAL_HOME, "Recovered"),
        "dev_junk": DEV_JUNK,
        "workers": "processes" if USE_PROCESSES else "threads",
        "windows_names": IS_WIN or FAKE_WIN,
        "start_source": os.environ.get("RETROVA_START_SOURCE") or None,
        "filesystems": [{"fs": f, "name": FS_NAMES.get(f, f), "available": available(f)} for f in SUPPORTED],
    }


@route("GET", "/api/drives")
def api_drives(q, body):
    return list_drives()


# ---------------------------------------------------------------- elevation
def _current_token(body) -> str:
    """The running server's session token (the elevated helper reuses it)."""
    from retrova import server as srv
    for o in gc.get_objects():
        try:
            if isinstance(o, srv.Server):
                return o.token
        except ReferenceError:
            continue
    tok = str((body or {}).get("token") or "")
    if not tok:
        raise ApiError("Couldn't find this session's token to hand to the administrator helper.", 500)
    return tok


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _answers(port, token) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/info", headers={"X-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:     # local helper on 127.0.0.1 only
            return bool(json.loads(r.read() or b"{}").get("admin"))
    except Exception:
        return False


def _wait_for(port, token, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if _answers(port, token):
            return True
        time.sleep(0.3)
    return False


def _applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _launch_mac(port, token):
    env = (f"SUDO_UID={int(REAL_UID)} SUDO_GID={int(REAL_GID)} SUDO_USER={shlex.quote(REAL_USER)} "
           f"RETROVA_ELEVATED=1 RETROVA_APP_DATA={shlex.quote(_app_data())}")
    cmd = (f"cd {shlex.quote(PKG_ROOT)} && {env} nohup {shlex.quote(sys.executable)} -m retrova --serve-only "
           f"--port {port} --token {shlex.quote(token)} --parent {os.getpid()} >/dev/null 2>&1 &")
    script = (f"do shell script {_applescript_quote(cmd)} with prompt "
              f"{_applescript_quote('Retrova needs administrator rights to read the drive directly. It only ever reads it.')}"
              f" with administrator privileges")
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise ApiError("The administrator password prompt timed out. Please try again.", 408)
    if r.returncode != 0:
        if "-128" in (r.stderr or "") or "canceled" in (r.stderr or "").lower():
            raise ApiError("Administrator access was cancelled - Retrova can still open disk images and "
                           "drives your account can read.", 403)
        raise ApiError(f"macOS didn't start the administrator helper: {(r.stderr or '').strip()}", 500)


def _launch_windows(port, token):
    import ctypes
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    params = subprocess.list2cmdline(["-m", "retrova", "--serve-only", "--port", str(port), "--token", token,
                                      "--parent", str(os.getpid())])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, PKG_ROOT, 0)
    if rc <= 32:
        if rc == 5:
            raise ApiError("Administrator access was cancelled - Retrova can still open disk images and "
                           "drives your account can read.", 403)
        raise ApiError(f"Windows didn't start the administrator helper (code {rc}).", 500)


@route("POST", "/api/elevate")
def api_elevate(q, body):
    if is_admin():
        raise ApiError("Retrova already has administrator rights.", 409)
    if not (IS_MAC or IS_WIN):
        raise ApiError("On Linux, start Retrova with sudo to read drives directly (for example: "
                       "sudo python3 -m retrova).", 400)
    token = _current_token(body)
    port = _free_port()
    if IS_MAC:
        _launch_mac(port, token)
    else:
        _launch_windows(port, token)
    if not _wait_for(port, token, 90):
        raise ApiError("The administrator helper didn't start in time. Please try again.", 504)
    return {"url": f"http://127.0.0.1:{port}/?t={token}", "port": port}


# Links the UI may open in the user's browser. Only these fixed addresses are allowed, so the
# engine can't be used to open arbitrary URLs. The only request Retrova itself makes is the update
# check below, and only when the user clicks "Check for updates".
LINKS = {
    "sponsor": "https://github.com/sponsors/jishnumahanta",
    "wise": "https://wise.com/pay/business/jishnumahanta",
    "source": "https://github.com/jishnumahanta/retrova",
    "website": "https://retrova.jishnumahanta.in/",
    "support": "https://retrova.jishnumahanta.in/#support",
    "issues": "https://github.com/jishnumahanta/retrova/issues",
}


def _open_in_browser(url: str):
    try:
        if IS_MAC:
            args = ["open", url]
            if is_admin() and REAL_UID:
                args = ["sudo", "-u", REAL_USER] + args
            subprocess.Popen(args)
        elif IS_WIN:
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", url])
    except OSError:
        raise ApiError(f"Couldn't open your browser. The address is {url}", 501)


@route("POST", "/api/open_link")
def api_open_link(q, body):
    url = LINKS.get(str(body.get("id", "")))
    if not url:
        raise ApiError("Unknown link.", 400)
    _open_in_browser(url)
    return {"ok": True, "url": url}


# ---------------------------------------------------------------- update check (only when the user asks)
# The one network request Retrova can make: GET the latest release from GitHub, and only when
# the user clicks "Check for updates". Nothing is sent but a plain HTTPS request; no ids, no stats.
RELEASES_API = "https://api.github.com/repos/jishnumahanta/retrova/releases/latest"
RELEASES_PAGE = "https://github.com/jishnumahanta/retrova/releases"
DOWNLOAD_PREFIX = "https://github.com/jishnumahanta/retrova/releases/"


def _version_tuple(v: str) -> tuple:
    out = []
    for part in str(v).strip().lstrip("vV").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def _fetch_json(url: str, timeout: int = 10) -> dict:
    """GET a JSON document. System curl first (it uses the OS certificate store on macOS and
    Windows, which the bundled Python's OpenSSL can't see), then urllib as a fallback."""
    headers = {"User-Agent": f"Retrova/{__version__}", "Accept": "application/vnd.github+json"}
    curl = "curl.exe" if IS_WIN else "/usr/bin/curl" if IS_MAC else "curl"
    try:
        cmd = [curl, "-fsSL", "--max-time", str(timeout)]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        r = subprocess.run(cmd + [url], capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout + 5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and r.stdout:
            return json.loads(r.stdout.decode("utf-8", "replace"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 (fixed https URL)
        return json.loads(res.read().decode("utf-8", "replace"))


def _pick_asset(assets: list) -> dict | None:
    """The download that fits this computer: Apple silicon or Intel .dmg, Windows Setup or portable."""
    import platform as _platform
    names = {a.get("name", ""): a for a in assets}
    if IS_MAC:
        arch = "arm64" if _platform.machine() in ("arm64", "aarch64") else "x64"
        want = [f"-mac-{arch}.dmg"]
    elif IS_WIN:
        installed = os.path.exists(os.path.join(os.path.dirname(sys.executable), "Uninstall Retrova.exe"))
        want = ["-win-x64-Setup.exe", "-win-x64-portable.zip"] if installed else ["-win-x64-portable.zip",
                                                                                 "-win-x64-Setup.exe"]
    else:
        return None
    for suffix in want:
        for n, a in names.items():
            if n.endswith(suffix):
                return {"name": n, "url": a.get("browser_download_url"), "size": a.get("size")}
    return None


@route("GET", "/api/update/check")
def api_update_check(q, body):
    try:
        rel = _fetch_json(RELEASES_API)
    except Exception as ex:
        raise ApiError("Couldn't reach GitHub to check for updates. Are you online? "
                       f"({type(ex).__name__})", 503)
    latest = str(rel.get("tag_name") or "").lstrip("vV")
    if not latest:
        raise ApiError("GitHub didn't say which version is the latest. Try again later.", 502)
    notes = str(rel.get("body") or "").strip()
    return {
        "current": __version__,
        "latest": latest,
        "newer": _version_tuple(latest) > _version_tuple(__version__),
        "page": rel.get("html_url") or RELEASES_PAGE,
        "published": rel.get("published_at"),
        "notes": notes[:4000],
        "asset": _pick_asset(rel.get("assets") or []),
    }


@route("POST", "/api/update/open")
def api_update_open(q, body):
    """Open a release page or download in the browser. Only Retrova's own GitHub releases."""
    url = str(body.get("url") or RELEASES_PAGE)
    if not (url == RELEASES_PAGE or url.startswith(DOWNLOAD_PREFIX)) or any(c in url for c in '\r\n "<>'):
        raise ApiError("That isn't a Retrova release address.", 400)
    _open_in_browser(url)
    return {"ok": True, "url": url}
