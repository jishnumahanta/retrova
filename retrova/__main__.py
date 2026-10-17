"""Start Retrova: local API server + a native app window (pywebview).

  python -m retrova                 normal start (native window)
  python -m retrova --no-window     server only, prints the URL (for testing)
  python -m retrova --serve-only --port P --token T --parent PID
                                   elevated helper started by /api/elevate
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback

from . import APP_NAME, __version__
from .server import Server, route, ApiError
from .util import IS_MAC, IS_WIN, REAL_HOME

NATIVE = {"window": None}   # set when a pywebview window exists (used for native file dialogs)


def app_data_dir() -> str:
    if IS_MAC:
        d = os.path.join(REAL_HOME, "Library", "Application Support", APP_NAME)
    elif IS_WIN:
        d = os.path.join(os.environ.get("LOCALAPPDATA", REAL_HOME), APP_NAME)
    else:
        d = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(REAL_HOME, ".local", "share")), APP_NAME.lower())
    os.makedirs(d, exist_ok=True)
    return d


def register_all():
    for name in ("recovery", "tools"):
        try:
            mod = __import__(f"retrova.{name}", fromlist=["register"])
            if hasattr(mod, "register"):
                mod.register()
        except Exception:
            print(f"[retrova] could not load {name}:", file=sys.stderr)
            traceback.print_exc()


@route("POST", "/api/native/pick")
def _native_pick(q, body):
    """Native open-folder / open-file dialog when running in the app window."""
    w = NATIVE["window"]
    if w is None:
        raise ApiError("Native dialogs aren't available - use the in-app browser.", 409)
    import webview  # type: ignore
    kind = body.get("kind", "folder")
    dialog = {"folder": webview.FOLDER_DIALOG, "file": webview.OPEN_DIALOG,
              "save": webview.SAVE_DIALOG}.get(kind, webview.FOLDER_DIALOG)
    kw = {"allow_multiple": bool(body.get("multiple"))} if kind == "file" else {}
    if kind == "save":
        kw["save_filename"] = body.get("filename", "")
    res = w.create_file_dialog(dialog, directory=body.get("directory") or REAL_HOME, **kw)
    if not res:
        return {"paths": []}
    return {"paths": [res] if isinstance(res, str) else list(res)}


def watch_parent(pid: int):
    """Elevated helper: exit when the app that started us is gone."""
    def loop():
        while True:
            time.sleep(2)
            try:
                if IS_WIN:
                    import ctypes
                    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
                    code = ctypes.c_ulong()
                    alive = bool(h) and ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value == 259
                    if h:
                        ctypes.windll.kernel32.CloseHandle(h)
                else:
                    os.kill(pid, 0)
                    alive = True
            except OSError:
                alive = False
            if not alive:
                os._exit(0)
    threading.Thread(target=loop, daemon=True).start()


def open_window(url: str) -> bool:
    """Show the UI in an embedded native web view. Returns False if not possible."""
    try:
        import webview  # type: ignore
    except Exception:
        return False
    try:
        w = webview.create_window(APP_NAME, url, width=1280, height=840, min_size=(920, 620),
                                  background_color="#0E1512", text_select=True)
        NATIVE["window"] = w
        if IS_WIN:
            _win_app_id()
        webview.start(debug=bool(os.environ.get("RETROVA_DEBUG")), private_mode=False,
                      storage_path=os.path.join(app_data_dir(), "webview"))
        return True
    except Exception:
        traceback.print_exc()
        return False


def _win_app_id():
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Retrova.App")
    except Exception:
        pass


def open_app_mode_browser(url: str) -> bool:
    """Fallback: a chromeless app window from Edge/Chrome (no tabs, no address bar)."""
    import shutil
    import subprocess
    cands = []
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    elif IS_MAC:
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]
    else:
        cands = [shutil.which(n) or "" for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")]
    profile = os.path.join(app_data_dir(), "browser-profile")
    for c in cands:
        if c and os.path.exists(c):
            subprocess.Popen([c, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1280,840",
                              "--no-first-run", "--no-default-browser-check"])
            return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="retrova", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--token")
    ap.add_argument("--no-window", action="store_true", help="don't open a window; print the URL")
    ap.add_argument("--serve-only", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--parent", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--src", help="open this disk or image in Recover straight away")
    args, _unknown = ap.parse_known_args(argv)   # ignore e.g. macOS -psn_ arguments

    os.environ.setdefault("RETROVA_APP_DATA", app_data_dir())
    if args.src:
        os.environ["RETROVA_START_SOURCE"] = args.src
    register_all()
    server = Server(args.port, args.token)
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    url = server.url
    print(f"{APP_NAME} {__version__} running at {url}", flush=True)

    if args.serve_only:
        if args.parent:
            watch_parent(args.parent)
        while True:
            time.sleep(3600)
    if args.no_window:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    if open_window(url):
        return  # window closed -> quit
    if not open_app_mode_browser(url):
        import webbrowser
        webbrowser.open(url)
    print("Keep this running while you use Retrova. Press Ctrl+C to quit.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
