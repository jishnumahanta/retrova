"""Checks on what was built.

  sim  - builds the macOS-style bundle with the *Linux* runtime and the same launcher source,
         starts it, and drives a real recovery through the HTTP API (proves launcher, .pth,
         bytecode, module entry point and engine wiring).
  mac  - static checks on each Retrova.app: Mach-O architectures, signatures, dylib/rpath
         resolution, Info.plist, bundled app; then opens the .dmg and lists it.
  win  - PE checks on Retrova.exe (x64, GUI), zip contents, installer present; under Wine
         (if installed) imports every bundled dependency and runs the engine.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from . import config as C
from . import deps
from .common import copytree, die, fetch_python, linux_python, log, rmtree, run, venv_python, warn

TEST_IMAGE = C.ROOT / "tests" / "apfs_images" / "gptdisk.img"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _api(port, token, path, body=None, timeout=60):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Token": token},
                                 data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _wait_up(port, token, proc, secs=60):
    t0 = time.time()
    while time.time() - t0 < secs:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            _api(port, token, "/api/info", timeout=3)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _recovery_roundtrip(port, token, src_path: str, out_path: str) -> dict:
    s = _api(port, token, "/api/recover/open", {"source": src_path})
    job = _api(port, token, "/api/recover/copy", {"sid": s["sid"], "paths": ["/"], "out": out_path})
    for _ in range(240):
        st = _api(port, token, f"/api/job?id={job['id']}")
        if st["state"] not in ("queued", "running", "paused"):
            return st
        time.sleep(0.5)
    die("recovery job did not finish in time")


# ------------------------------------------------------------------ sim

def verify_sim() -> None:
    target = "linux-x64"
    work = C.BUILD / "sim"
    rmtree(work)
    contents = work / f"{C.APP_NAME}.app" / "Contents"
    res = contents / "Resources"
    (contents / "MacOS").mkdir(parents=True)
    pristine = fetch_python(target)
    copytree(pristine, res / "python")
    deps.prune_runtime(target, res / "python")
    site = res / "python" / "lib" / f"python{C.PY_MAJMIN}" / "site-packages"
    deps.copy_site(deps.install_site(target), site, target)
    deps.copy_app(res / "app")
    deps.write_app_pth(site, res / "app")
    deps.compile_bytecode([res / "app"], strict_dirs=[res / "app"])
    exe = contents / "MacOS" / C.APP_NAME
    run(["gcc", "-O2", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Werror",
         "-I", pristine / "include" / f"python{C.PY_MAJMIN}", "-o", exe,
         C.PKG_DIR / "launchers" / "mac" / "retrova_launcher.c",
         "-L", res / "python" / "lib", f"-lpython{C.PY_MAJMIN}", "-Wl,-rpath,$ORIGIN/../Resources/python/lib"])
    # 1. interpreter pass-through, isolated from the build host's Python
    r = run([exe, "-c", "import sys, retrova; print(sys.executable); print(retrova.__version__)"],
            capture=True, env={"PYTHONPATH": "/nonexistent", "PYTHONHOME": "/nonexistent"})
    if str(exe) not in r.stdout or C.app_version() not in r.stdout:
        die(f"sim: launcher pass-through gave unexpected output:\n{r.stdout}")
    # 2. the engine itself, plus a real recovery from the APFS test image
    port, token = _free_port(), "simtoken"
    tmp = Path(tempfile.mkdtemp(prefix="retrova-sim-"))
    env = dict(os.environ, RETROVA_APP_DATA=str(tmp / "appdata"), RETROVA_NO_LOG="1")
    proc = subprocess.Popen([exe, "--serve-only", "--port", str(port), "--token", token],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if not _wait_up(port, token, proc):
            out = proc.stdout.read().decode(errors="replace") if proc.poll() is not None else ""
            die(f"sim: engine did not start\n{out[-3000:]}")
        info = _api(port, token, "/api/info")
        gz = TEST_IMAGE.with_name(TEST_IMAGE.name + ".gz")      # stored gzipped in git
        if not TEST_IMAGE.exists() and gz.exists():
            import gzip
            with gzip.open(gz, "rb") as src, open(TEST_IMAGE, "wb") as dst:
                shutil.copyfileobj(src, dst)
        if TEST_IMAGE.exists():
            st = _recovery_roundtrip(port, token, str(TEST_IMAGE), str(tmp / "out"))
            if st["state"] != "done" or st["stats"]["failed"] or not st["stats"]["copied"]:
                die(f"sim: recovery did not complete cleanly: {st}")
            log(f"sim: engine {info['version']} up; recovered {st['stats']['copied']} files from the test image")
        else:
            warn("sim: tests/apfs_images/gptdisk.img not found - recovery round trip skipped")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ mac

def _macho_report(path: Path):
    from macholib.MachO import MachO  # build venv
    from macholib.mach_o import LC_CODE_SIGNATURE, CPU_TYPE_NAMES
    m = MachO(str(path))
    cpus, signed = [], True
    for h in m.headers:
        cpus.append(CPU_TYPE_NAMES.get(h.header.cputype, str(h.header.cputype)))
        if not any(lc.cmd == LC_CODE_SIGNATURE for lc, _c, _d in h.commands):
            signed = False
    return cpus, signed


def verify_mac() -> None:
    for target in ("mac-arm64", "mac-x64"):
        app = C.BUILD / target / f"{C.APP_NAME}.app"
        want = {"arm64": "ARM64", "x64": "x86_64"}[C.TARGETS[target]["arch"]]
        if not app.exists():
            warn(f"mac: {app} not built - skipped")
            continue
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        assert info["CFBundleExecutable"] == C.APP_NAME and info["CFBundleIdentifier"] == C.BUNDLE_ID
        for need in ("MacOS/" + C.APP_NAME, "Resources/Retrova.icns", "_CodeSignature/CodeResources",
                     f"Resources/python/lib/libpython{C.PY_MAJMIN}.dylib",
                     f"Resources/app/{C.MODULE}/__main__.py", f"Resources/app/{C.MODULE}/ui/index.html"):
            if not (app / "Contents" / need).exists():
                die(f"mac[{target}]: missing Contents/{need}")
        bad, n = [], 0
        for root, _d, files in os.walk(app):
            for f in files:
                p = Path(root) / f
                if p.is_symlink():
                    continue
                with open(p, "rb") as fh:
                    magic = fh.read(4)
                if magic not in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
                    continue
                n += 1
                cpus, signed = _macho_report(p)
                if want not in cpus or not signed:
                    bad.append(f"{p.relative_to(app)} cpus={cpus} signed={signed}")
        if bad:
            die(f"mac[{target}]: {len(bad)} Mach-O files with the wrong architecture or no signature:\n"
                + "\n".join(bad[:20]))
        pyc = list((app / "Contents/Resources/app" / C.MODULE).rglob("*.pyc"))
        if not pyc:
            die(f"mac[{target}]: the app was not precompiled")
        dmg = C.DIST / f"{C.APP_NAME}-{C.app_version()}-mac-{C.TARGETS[target]['arch']}.dmg"
        if not dmg.exists():
            die(f"mac[{target}]: {dmg.name} missing")
        from .tools import dmg_tool, hfsplus_tool
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "img.hfs"
            run([dmg_tool(), "extract", dmg, raw], quiet=True)
            ls = run([hfsplus_tool(), raw, "ls", "/"], quiet=True, capture=True).stdout
            for want_name in (f"{C.APP_NAME}.app", "Applications", "Read me first.txt"):
                if want_name not in ls:
                    die(f"mac[{target}]: {dmg.name} does not contain {want_name}:\n{ls}")
        log(f"mac[{target}]: {n} Mach-O files, all {want} and signed; {dmg.name} opens and lists correctly")


# ------------------------------------------------------------------ win

def _pe_info(path: Path):
    b = path.read_bytes()
    off = struct.unpack_from("<I", b, 0x3C)[0]
    if b[off:off + 4] != b"PE\0\0":
        die(f"{path} is not a PE file")
    machine = struct.unpack_from("<H", b, off + 4)[0]
    opt = off + 24
    subsystem = struct.unpack_from("<H", b, opt + 68)[0]
    return machine, subsystem


def verify_win() -> None:
    ver = C.app_version()
    root = C.BUILD / "win-x64" / C.APP_NAME
    exe = root / f"{C.APP_NAME}.exe"
    if not exe.exists():
        warn("win: not built - skipped")
        return
    machine, subsystem = _pe_info(exe)
    if machine != 0x8664 or subsystem != 2:
        die(f"win: {exe.name} machine={machine:#x} subsystem={subsystem} (want x64 GUI)")
    z = C.DIST / f"{C.APP_NAME}-{ver}-win-x64-portable.zip"
    setup = C.DIST / f"{C.APP_NAME}-{ver}-win-x64-Setup.exe"
    for p in (z, setup):
        if not p.exists():
            die(f"win: {p.name} missing")
    names = set(zipfile.ZipFile(z).namelist())
    for need in (f"{C.APP_NAME}/{C.APP_NAME}.exe", f"{C.APP_NAME}/python{C.PY_NODOT}.dll",
                 f"{C.APP_NAME}/app/{C.MODULE}/__main__.py"):
        if need not in names:
            die(f"win: {z.name} lacks {need}")
    if _pe_info(setup)[0] not in (0x14C, 0x8664):
        die("win: Setup.exe is not a Windows executable")
    wine = shutil.which("wine")
    if not wine:
        warn("win: Wine not installed - runtime check skipped (apt-get install wine64)")
        log("win: PE, zip and installer checks passed")
        return
    # GUI-subsystem programs get no usable std handles under Wine, so run through cmd redirection.
    prefix = C.CACHE / "wineprefix"
    env = {"WINEPREFIX": str(prefix), "WINEDEBUG": "-all", "WINEDLLOVERRIDES": "mscoree,mshtml="}
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "t.py").write_text(
            "import retrova, webview, psutil, xxhash, send2trash, clr_loader, dissect.apfs, dissect.ntfs, "
            "dissect.fat, dissect.extfs, dissect.xfs, dissect.btrfs\n"
            "import retrova.recovery, retrova.tools\nprint('IMPORTS-OK', retrova.__version__)\n")
        run(["wine", "cmd", "/c", f"{exe} t.py > o.txt 2>&1 < NUL"], cwd=t, env=env, quiet=True,
            check=False, timeout=300)
        out = (t / "o.txt").read_text(errors="replace") if (t / "o.txt").exists() else ""
        if "IMPORTS-OK" not in out:
            die(f"win: bundled imports failed under Wine:\n{out[-3000:]}")
        port, token = _free_port(), "wintoken"
        proc = subprocess.Popen(["wine", "cmd", "/c",
                                 f"{exe} --serve-only --port {port} --token {token} > s.txt 2>&1 < NUL"],
                                cwd=t, env=dict(os.environ, **env), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            if not _wait_up(port, token, None, 120):
                die("win: engine did not start under Wine")
            if TEST_IMAGE.exists():
                st = _recovery_roundtrip(port, token, "Z:" + str(TEST_IMAGE).replace("/", "\\"),
                                         "Z:" + str(t / "out").replace("/", "\\"))
                if st["state"] != "done" or st["stats"]["failed"] or not st["stats"]["copied"]:
                    die(f"win: recovery under Wine did not complete cleanly: {st}")
                log(f"win: engine ran under Wine and recovered {st['stats']['copied']} files")
        finally:
            subprocess.run(["wineserver", "-k"], env=dict(os.environ, **env), check=False)
            proc.kill()
    log("win: PE, zip, installer and Wine runtime checks passed")


def run_all(which) -> None:
    for w in which:
        {"sim": verify_sim, "mac": verify_mac, "win": verify_win}[w]()
