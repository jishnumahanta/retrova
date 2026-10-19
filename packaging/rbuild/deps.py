"""Third-party dependencies (pip --target per platform), runtime pruning, bytecode, import scan."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from . import config as C
from .common import copytree, die, linux_python, log, rmtree, run, venv_python, warn

REQ_DIR = C.PKG_DIR / "requirements"
WHEELHOUSE = C.CACHE / "wheelhouse"
PIP_ENV = {"PIP_CACHE_DIR": str(C.CACHE / "pip"), "PIP_DISABLE_PIP_VERSION_CHECK": "1",
           "PYTHONNOUSERSITE": "1", "PIP_NO_INPUT": "1"}

# sdist-only pure-Python packages we turn into wheels ourselves (pip --platform needs wheels).
PURE_SDISTS = ["proxy_tools==0.1.0"]

# Importable top-level names that only exist on one platform (not an error elsewhere).
MAC_ONLY = {"objc", "AppKit", "Foundation", "Cocoa", "CoreFoundation", "Quartz", "Vision", "WebKit",
            "Security", "UniformTypeIdentifiers", "PyObjCTools", "CoreML", "CoreServices", "LaunchServices",
            "DiskArbitration", "IOKit", "PDFKit", "ScreenCaptureKit", "Metal", "CoreText"}
WIN_ONLY = {"winrt", "clr", "clr_loader", "pythonnet", "win32api", "win32con", "win32com", "pywintypes",
            "win32file", "win32process", "win32security", "wmi", "comtypes", "_winapi", "winreg", "msvcrt",
            "winsound", "_overlapped", "nt"}
LINUX_ONLY = {"gi", "dbus", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy"}
# import name -> PyPI distribution, for nicer warnings
DIST_HINTS = {"Crypto": "pycryptodome", "webview": "pywebview", "send2trash": "send2trash", "yaml": "PyYAML",
              "PIL": "Pillow", "objc": "pyobjc-core", "clr": "pythonnet", "winrt": "winrt-runtime"}


def ensure_wheelhouse() -> Path:
    WHEELHOUSE.mkdir(parents=True, exist_ok=True)
    for spec in PURE_SDISTS:
        name, ver = spec.split("==")
        if not list(WHEELHOUSE.glob(f"{name}-{ver}-*.whl")):
            run([venv_python(), "-m", "pip", "wheel", "--no-deps", "--no-binary", ":all:", "-w", WHEELHOUSE, spec],
                env=PIP_ENV)
        whl = next(WHEELHOUSE.glob(f"{name}-{ver}-*.whl"))
        if not whl.name.endswith("-py3-none-any.whl") and "-py2.py3-none-any" not in whl.name:
            die(f"{whl.name} is not a pure-Python wheel")
    return WHEELHOUSE


def _req_files(target: str) -> list[Path]:
    return [REQ_DIR / f for f in C.TARGETS[target]["requirements"]]


def site_cache_key(target: str) -> str:
    h = hashlib.sha256()
    for f in _req_files(target) + [REQ_DIR / "constraints.txt"]:
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    h.update(json.dumps([target, C.TARGETS[target], C.PY_VERSION, PURE_SDISTS]).encode())
    return h.hexdigest()[:16]


def install_site(target: str) -> Path:
    """pip install --target for the target platform; cached by requirements hash."""
    key = site_cache_key(target)
    dest = C.CACHE / "site" / f"{target}-{key}"
    if (dest / ".complete").exists():
        log(f"deps for {target}: cached ({dest.name})")
        return dest
    ensure_wheelhouse()
    rmtree(dest)
    tmp = dest.with_name(dest.name + ".tmp")
    rmtree(tmp)
    reqs = []
    for f in _req_files(target):
        reqs += ["-r", f]
    common = ["install", "--target", tmp, "--no-compile", "--only-binary=:all:", "--find-links", WHEELHOUSE,
              "-c", REQ_DIR / "constraints.txt", "--no-warn-script-location"]
    t = C.TARGETS[target]
    if t["pip_platforms"]:
        plat = []
        for p in t["pip_platforms"]:
            plat += ["--platform", p]
        cmd = [venv_python(), "-m", "pip", *common, *plat, "--python-version", C.PY_MAJMIN,
               "--implementation", "cp", *reqs]
    else:  # native (Linux simulation): use the bundled Linux interpreter itself
        cmd = [linux_python(), "-m", "pip", *common, *reqs]
    run(cmd, env=PIP_ENV)
    # console-script wrappers point at the build machine's interpreter - useless in the bundle
    rmtree(tmp / "bin")
    rmtree(tmp / "Scripts")
    for p in tmp.rglob("__pycache__"):
        rmtree(p)
    freeze = sorted(d.name.replace(".dist-info", "") for d in tmp.glob("*.dist-info"))
    (tmp / ".freeze.txt").write_text("\n".join(freeze) + "\n")
    tmp.rename(dest)
    (dest / ".complete").write_text("ok\n")
    log(f"deps for {target}: {len(freeze)} distributions")
    return dest


def copy_site(site_src: Path, site_dst: Path, target: str) -> list[str]:
    """Copy cached deps into a runtime's site-packages and drop things the app never needs."""
    site_dst.mkdir(parents=True, exist_ok=True)
    copytree(site_src, site_dst, ignore=shutil.ignore_patterns(".complete", ".freeze.txt"))
    os_ = C.TARGETS[target]["os"]
    lib = site_dst / "webview" / "lib"
    if lib.exists():
        rmtree(lib / "pywebview-android.jar")
        if os_ != "win":
            for p in list(lib.iterdir()):
                rmtree(p)           # WebView2 / WinForms .NET assemblies are Windows-only
        else:
            # Keep every lib/runtimes/win-* folder: pywebview's edgechromium.py adds all three
            # (win-arm64, win-x64, win-x86) to PATH at import and raises FileNotFoundError if one
            # is missing. Then the WebView2 window never opens and Retrova falls back to an Edge
            # window without native dialogs (1.0.0 and 1.0.1 shipped like that).
            rmtree(lib / "WebBrowserInterop.x86.dll")
    rmtree(site_dst / "PyObjCTest")   # pyobjc's own test-suite helpers
    for p in list(site_dst.rglob("*.dSYM")):
        rmtree(p)                     # debug-symbol bundles (pyobjc ships one); not code, breaks signing
    return (site_src / ".freeze.txt").read_text().split()


def write_app_pth(site_dst: Path, app_dir: Path) -> None:
    rel = os.path.relpath(app_dir, site_dst)
    (site_dst / "retrova-app.pth").write_text(rel.replace(os.sep, "/") + "\n")


APP_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store", "*.orig", "*.rej",
                                    "*.swp", "*~", ".pytest_cache", ".mypy_cache", "*.egg-info")


def copy_app(app_dst: Path) -> None:
    if not (C.APP_SRC / "__main__.py").exists():
        die(f"{C.APP_SRC}/__main__.py not found")
    rmtree(app_dst / C.MODULE)
    app_dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(C.APP_SRC, app_dst / C.MODULE, ignore=APP_IGNORE, symlinks=False)


# ---------------------------------------------------------------- runtime pruning

def app_uses(module: str) -> bool:
    return module in scan_app_imports()


def prune_runtime(target: str, py: Path) -> None:
    """Remove parts of python-build-standalone the app can't use (dev tools, tests, tkinter...)."""
    os_ = C.TARGETS[target]["os"]
    keep_tk = app_uses("tkinter") or app_uses("_tkinter") or app_uses("turtle")
    if os_ == "win":
        stdlib = py / "Lib"
        for d in ("include", "libs", "Scripts"):
            rmtree(py / d)
        for p in (py / "DLLs").glob("_test*.pyd"):
            p.unlink()
        rmtree(py / "DLLs" / "_ctypes_test.pyd")
        if not keep_tk:
            rmtree(py / "tcl")
            for n in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll"):
                rmtree(py / "DLLs" / n)
    else:
        stdlib = py / "lib" / f"python{C.PY_MAJMIN}"
        rmtree(py / "include")
        rmtree(py / "share")
        rmtree(py / "lib" / "pkgconfig")
        if os_ == "mac":
            rmtree(py / "bin")       # the launcher is the interpreter; bin/python3.12 is a static 18 MB copy
        else:
            for p in (py / "bin").iterdir():
                if p.name not in (f"python{C.PY_MAJMIN}", "python3", "python"):
                    rmtree(p)
        for p in stdlib.glob("config-*"):
            rmtree(p)            # static libpython + Makefile for building extensions
        if not keep_tk:
            for pat in ("libtcl*", "libtk*", "tcl*", "tk*", "itcl*", "thread*"):
                for p in (py / "lib").glob(pat):
                    rmtree(p)
            for p in (stdlib / "lib-dynload").glob("_tkinter*"):
                rmtree(p)
    for d in ("idlelib", "turtledemo", "ensurepip", "lib2to3", "test", "unittest/test", "tkinter/test",
              "__phello__", "venv/scripts/nt" if os_ != "win" else "venv/scripts/posix"):
        rmtree(stdlib / d)
    if not keep_tk:
        rmtree(stdlib / "tkinter")
        rmtree(stdlib / "turtle.py")
    for p in (stdlib / "site-packages").iterdir():
        if p.name.startswith("pip") or p.name == "README.txt":
            rmtree(p)
    for p in stdlib.rglob("__pycache__"):
        rmtree(p)            # recompiled below with one consistent invalidation mode


def compile_bytecode(paths: list[Path], strict_dirs: list[Path] = ()) -> None:
    """Precompile with the bundled CPython version (unchecked-hash pycs: valid wherever the files land,
    and the read-only, signed macOS bundle never needs to write a .pyc)."""
    py = linux_python()
    for p in paths:
        strict = any(p == s for s in strict_dirs)
        r = run([py, "-I", "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash", p],
                quiet=True, check=False, capture=True)
        if r.returncode != 0:
            msg = f"compileall reported errors in {p}:\n{r.stdout[-3000:]}"
            if strict:
                die(msg)
            warn(msg)


# ---------------------------------------------------------------- import scan

_SCAN: set[str] | None = None


def scan_app_imports() -> set[str]:
    """Top-level absolute imports anywhere in the app package (incl. lazy imports inside functions)."""
    global _SCAN
    if _SCAN is not None:
        return _SCAN
    found: set[str] = set()
    for f in C.APP_SRC.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"), str(f))
        except SyntaxError as e:
            warn(f"syntax error in app source {f}: {e}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    found.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in (
                    "import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) and not node.args[0].value.startswith("."):
                found.add(node.args[0].value.split(".")[0])
    _SCAN = found
    return found


def stdlib_names() -> set[str]:
    r = run([linux_python(), "-I", "-c", "import sys; print('\\n'.join(sorted(sys.stdlib_module_names)))"],
            quiet=True, capture=True)
    return set(r.stdout.split())


def check_imports(target: str, site: Path) -> list[str]:
    """Return third-party modules the app imports that the target bundle doesn't contain."""
    std = stdlib_names()
    os_ = C.TARGETS[target]["os"]
    other = {"mac": WIN_ONLY | LINUX_ONLY, "win": MAC_ONLY | LINUX_ONLY, "linux": MAC_ONLY | WIN_ONLY}[os_]
    present = set()
    for p in site.iterdir():
        n = p.name
        if p.is_dir() and not n.endswith((".dist-info", ".data")):
            present.add(n)
        elif n.endswith((".py", ".pyd", ".so")):
            present.add(n.split(".")[0])
    missing = []
    for mod in sorted(scan_app_imports()):
        if mod in std or mod == C.MODULE or mod in present or mod in other or mod.startswith("_"):
            continue
        missing.append(mod)
    for mod in missing:
        hint = DIST_HINTS.get(mod, mod)
        warn(f"[{target}] the app imports '{mod}' but it is not bundled - add '{hint}' to "
             f"packaging/requirements/*.txt (and pin it in constraints.txt) if it is a real dependency")
    return missing
