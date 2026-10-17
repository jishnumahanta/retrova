"""Windows x64: Retrova\ folder (GUI launcher + bundled CPython + deps + app) -> NSIS Setup.exe + portable zip.

Everything is cross-built on Linux: the launcher with mingw-w64, the installer with makensis.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import config as C
from . import deps
from .common import copytree, die, fetch_python, human, log, rmtree, run, tree_size

TARGET = "win-x64"
LAUNCHER_DIR = C.PKG_DIR / "launchers" / "win"
NSI = C.PKG_DIR / "installer" / "retrova.nsi"
MINGW = "x86_64-w64-mingw32"


def build_launcher(icons: Path, ver: str, out: Path) -> Path:
    work = out.parent.parent / "launcher-src"
    rmtree(work)
    work.mkdir(parents=True)
    for f in ("retrova_launcher.c", "retrova.rc", "retrova.manifest"):
        shutil.copy2(LAUNCHER_DIR / f, work / f)
    shutil.copy2(icons / "Retrova.ico", work / "Retrova.ico")
    comma = ",".join(str(n) for n in C.version_tuple4(ver))
    run([f"{MINGW}-windres", f"-DRETROVA_VER_COMMA={comma}", f'-DRETROVA_VER_STR=\\"{ver}\\"',
         '-DRETROVA_ICON=\\"Retrova.ico\\"', "-O", "coff", "-i", "retrova.rc", "-o", "retrova_res.o"], cwd=work)
    run([f"{MINGW}-gcc", "-municode", "-mwindows", "-O2", "-s", "-Wall", "-Wextra", "-Werror",
         "-Wno-unused-parameter", "-o", out, "retrova_launcher.c", "retrova_res.o", "-lshell32",
         "-static", "-static-libgcc"], cwd=work)
    return out


def assemble(icons: Path, ver: str) -> Path:
    work = C.BUILD / TARGET
    rmtree(work)
    root = work / C.APP_NAME
    root.mkdir(parents=True)
    pristine = fetch_python(TARGET)
    log(f"[{TARGET}] copying CPython {C.PY_VERSION}")
    copytree(pristine, root)
    for exe in ("python.exe", "pythonw.exe"):
        rmtree(root / exe)        # Retrova.exe is the interpreter (see launcher source)
    deps.prune_runtime(TARGET, root)
    site = root / "Lib" / "site-packages"
    freeze = deps.copy_site(deps.install_site(TARGET), site, TARGET)
    deps.copy_app(root / "app")
    deps.write_app_pth(site, root / "app")
    deps.check_imports(TARGET, site)
    deps.compile_bytecode([root / "Lib", root / "app"], strict_dirs=[root / "app"])
    build_launcher(icons, ver, root / f"{C.APP_NAME}.exe")
    (root / "BUILD-INFO.txt").write_text(
        f"Retrova {ver} ({TARGET})\r\nCPython {C.PY_VERSION} (python-build-standalone {C.PBS_RELEASE}, "
        f"{C.TARGETS[TARGET]['triple']})\r\n\r\nBundled distributions:\r\n" + "\r\n".join(freeze) + "\r\n")
    for legal in ("LICENSE", "THIRD_PARTY_NOTICES.md"):           # AGPL: ship the licence texts with the binary
        shutil.copy2(C.ROOT / legal, root / legal)
    for need in (f"{C.APP_NAME}.exe", f"python{C.PY_NODOT}.dll", "vcruntime140.dll", "Lib/os.py",
                 "app/retrova/__main__.py", "app/retrova/ui/index.html"):
        if not (root / need).exists():
            die(f"[{TARGET}] {need} is missing from the assembled app")
    log(f"[{TARGET}] Retrova folder assembled: {human(tree_size(root))}")
    return root


def make_zip(root: Path, ver: str) -> Path:
    C.DIST.mkdir(parents=True, exist_ok=True)
    out = C.DIST / f"{C.APP_NAME}-{ver}-win-x64-portable.zip"
    rmtree(out)
    run(["zip", "-q", "-r", "-X", "-9", out, root.name], cwd=root.parent)
    log(f"[{TARGET}] {out.name}: {human(out.stat().st_size)}")
    return out


def make_installer(root: Path, icons: Path, ver: str) -> Path:
    C.DIST.mkdir(parents=True, exist_ok=True)
    out = C.DIST / f"{C.APP_NAME}-{ver}-win-x64-Setup.exe"
    rmtree(out)
    # The uninstaller removes only what the installer put there, so list the top level here.
    top_dirs = sorted(p.name for p in root.iterdir() if p.is_dir())
    top_files = sorted(p.name for p in root.iterdir() if p.is_file())
    lst = root.parent / "uninstall-list.nsh"
    with open(lst, "w", encoding="utf-8") as fh:
        for d in top_dirs:
            fh.write(f'  RMDir /r "$INSTDIR\\{d}"\n')
        for f in top_files:
            fh.write(f'  Delete "$INSTDIR\\{f}"\n')
    size_kb = tree_size(root) // 1024
    run(["makensis", "-V2", "-INPUTCHARSET", "UTF8",
         f"-DAPP_NAME={C.APP_NAME}", f"-DAPP_VERSION={ver}",
         f"-DAPP_VERSION4={'.'.join(str(n) for n in C.version_tuple4(ver))}",
         f"-DSRC_DIR={root}", f"-DICON={icons / 'Retrova.ico'}", f"-DOUT_FILE={out}",
         f"-DUNINSTALL_LIST={lst}", f"-DSIZE_KB={size_kb}", NSI])
    log(f"[{TARGET}] {out.name}: {human(out.stat().st_size)}")
    return out


def build(icons: Path, ver: str) -> list[Path]:
    root = assemble(icons, ver)
    return [make_installer(root, icons, ver), make_zip(root, ver)]
