"""macOS: Retrova.app (native launcher + bundled CPython + deps + app), ad-hoc signature, UDIF .dmg."""
from __future__ import annotations

import os
import plistlib
import shutil
from pathlib import Path

from . import config as C
from . import deps
from .common import copytree, die, fetch_python, human, log, rmtree, run, tree_size, venv_python, warn
from .tools import dmg_tool, hfsplus_tool, rcodesign

LAUNCHER_SRC = C.PKG_DIR / "launchers" / "mac" / "retrova_launcher.c"
ARCH_LABEL = {"arm64": "Apple silicon", "x64": "Intel"}
ARCH_HINT = {"arm64": "Macs with Apple silicon (M1, M2, M3, M4 and later)",
             "x64": "Intel-based Macs (it also runs on Apple silicon through Rosetta, but the "
                    "Apple silicon download is faster there)"}


def info_plist(ver: str) -> dict:
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": C.APP_NAME,
        "CFBundleIconFile": "Retrova.icns",
        "CFBundleIdentifier": C.BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": C.APP_NAME,
        "CFBundleDisplayName": C.APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "CFBundleShortVersionString": ver,
        "CFBundleVersion": ver,
        "LSMinimumSystemVersion": C.MAC_MIN_OS,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHumanReadableCopyright": C.COPYRIGHT,
        "NSPrincipalClass": "NSApplication",
        "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True,
        "NSRequiresAquaSystemAppearance": False,
        # The UI is served by Retrova's own server on 127.0.0.1 inside the app.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSDocumentsFolderUsageDescription":
            "Retrova looks at the files in Documents that you choose to analyse, de-duplicate, verify or search.",
        "NSDownloadsFolderUsageDescription":
            "Retrova looks at the files in Downloads that you choose to analyse, clean up, verify or search.",
        "NSDesktopFolderUsageDescription":
            "Retrova looks at the files on your Desktop that you choose to analyse, de-duplicate, verify or search.",
        "NSRemovableVolumesUsageDescription":
            "Retrova reads external drives and memory cards to recover, verify and copy your files.",
        "NSNetworkVolumesUsageDescription":
            "Retrova reads network drives that you choose as a source or a destination for your files.",
        "NSAppleEventsUsageDescription":
            "Retrova asks Finder to show files and to move items to the Trash.",
    }


def build_launcher(target: str, py: Path, out: Path) -> Path:
    t = C.TARGETS[target]
    out.parent.mkdir(parents=True, exist_ok=True)
    inc = py / "include" / f"python{C.PY_MAJMIN}"
    run([venv_python(), "-m", "ziglang", "cc", "-target", t["zig_target"], "-Os", "-Wall", "-Wextra",
         "-Wno-unused-parameter", "-Werror", "-I", inc, "-L", py / "lib", f"-lpython{C.PY_MAJMIN}",
         "-Wl,-rpath,@executable_path/../Resources/python/lib", "-Wl,-headerpad_max_install_names",
         "-o", out, LAUNCHER_SRC],
        env={"ZIG_GLOBAL_CACHE_DIR": str(C.CACHE / "zig"), "ZIG_LOCAL_CACHE_DIR": str(C.CACHE / "zig")})
    return out


def assemble_app(target: str, icons: Path, ver: str) -> Path:
    t = C.TARGETS[target]
    work = C.BUILD / target
    rmtree(work)
    app = work / f"{C.APP_NAME}.app"
    contents = app / "Contents"
    res = contents / "Resources"
    (contents / "MacOS").mkdir(parents=True)
    res.mkdir(parents=True)

    pristine = fetch_python(target)
    # The launcher compiles against the pristine runtime (headers are pruned from the bundle).
    build_launcher(target, pristine, work / "launcher" / C.APP_NAME)
    shutil.copy2(work / "launcher" / C.APP_NAME, contents / "MacOS" / C.APP_NAME)
    os.chmod(contents / "MacOS" / C.APP_NAME, 0o755)

    py = res / "python"
    log(f"[{target}] copying CPython {C.PY_VERSION}")
    copytree(pristine, py)
    deps.prune_runtime(target, py)
    stdlib = py / "lib" / f"python{C.PY_MAJMIN}"
    site = stdlib / "site-packages"
    freeze = deps.copy_site(deps.install_site(target), site, target)
    deps.copy_app(res / "app")
    deps.write_app_pth(site, res / "app")
    deps.check_imports(target, site)
    deps.compile_bytecode([stdlib, res / "app"], strict_dirs=[res / "app"])

    shutil.copy2(icons / "Retrova.icns", res / "Retrova.icns")
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump(info_plist(ver), fh, fmt=plistlib.FMT_XML, sort_keys=True)
    (contents / "PkgInfo").write_bytes(b"APPL????")
    (res / "BUILD-INFO.txt").write_text(
        f"Retrova {ver} ({target})\nCPython {C.PY_VERSION} (python-build-standalone {C.PBS_RELEASE}, "
        f"{t['triple']})\n\nBundled distributions:\n" + "\n".join(freeze) + "\n")
    for legal in ("LICENSE", "THIRD_PARTY_NOTICES.md"):       # AGPL: ship the licence texts with the binary
        shutil.copy2(C.ROOT / legal, res / legal)
    normalize_modes(app)
    log(f"[{target}] Retrova.app assembled: {human(tree_size(app))}")
    return app


def normalize_modes(root: Path) -> None:
    """Predictable permissions: dirs 755, Mach-O / executables 755, everything else 644."""
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for f in filenames:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                continue
            with open(p, "rb") as fh:
                magic = fh.read(4)
            is_macho = magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
                                 b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xbe\xba\xfe\xca")
            os.chmod(p, 0o755 if (is_macho or magic[:2] == b"#!") else 0o644)


def sign_app(app: Path, target: str) -> None:
    """Ad-hoc sign every Mach-O and seal the bundle (Apple silicon refuses to run unsigned code)."""
    logf = C.BUILD / target / "rcodesign.log"
    run([rcodesign(), "sign", app], logfile=logf)
    # `rcodesign verify` mis-reports ad-hoc signatures, so check the CodeDirectory is there instead.
    info = run([rcodesign(), "print-signature-info", app / "Contents" / "MacOS" / C.APP_NAME],
               quiet=True, capture=True).stdout
    if "CodeDirectory" not in info:
        die("the app launcher has no code signature after signing")
    if not (app / "Contents" / "_CodeSignature" / "CodeResources").exists():
        die("rcodesign did not write Contents/_CodeSignature/CodeResources")


def write_ds_store(path: Path) -> bool:
    """Finder window layout for the DMG (icon positions, 128 px icons, no toolbar)."""
    code = r'''
import sys
from ds_store import DSStore
with DSStore.open(sys.argv[1], "w+") as d:
    d["."]["bwsp"] = {"ShowStatusBar": False, "WindowBounds": "{{200, 140}, {640, 440}}",
                      "ContainerShowSidebar": False, "PreviewPaneVisibility": False, "SidebarWidth": 180,
                      "ShowTabView": False, "ShowToolbar": False, "ShowPathbar": False, "ShowSidebar": False}
    d["."]["icvp"] = {"viewOptionsVersion": 1, "backgroundType": 0, "backgroundColorRed": 1.0,
                      "backgroundColorGreen": 1.0, "backgroundColorBlue": 1.0, "gridOffsetX": 0.0,
                      "gridOffsetY": 0.0, "gridSpacing": 100.0, "arrangeBy": "none", "showIconPreview": False,
                      "showItemInfo": False, "labelOnBottom": True, "textSize": 13.0, "iconSize": 128.0,
                      "scrollPositionX": 0.0, "scrollPositionY": 0.0}
    d["."]["vSrn"] = ("long", 1)
    d["Retrova.app"]["Iloc"] = (170, 170)
    d["Applications"]["Iloc"] = (470, 170)
    d["Read me first.txt"]["Iloc"] = (320, 330)
'''
    r = run([venv_python(), "-I", "-c", code, path], quiet=True, check=False, capture=True)
    if r.returncode != 0:
        warn(f"could not write the DMG .DS_Store (Finder will use its default layout): {r.stdout[-500:]}")
        return False
    return True


def make_dmg(target: str, app: Path, icons: Path, ver: str) -> Path:
    arch = C.TARGETS[target]["arch"]
    work = C.BUILD / target
    root = work / "dmgroot"
    rmtree(root)
    root.mkdir()
    copytree(app, root / app.name)
    os.symlink("/Applications", root / "Applications")
    readme = (C.PKG_DIR / "assets" / "mac-readme.txt").read_text(encoding="utf-8").format(
        version=ver, arch_label=ARCH_LABEL[arch], arch_hint=ARCH_HINT[arch])
    (root / "Read me first.txt").write_text(readme, encoding="utf-8")
    shutil.copy2(icons / "Retrova.icns", root / ".VolumeIcon.icns")
    has_ds = write_ds_store(root / ".DS_Store")
    for p in (root / "Read me first.txt", root / ".VolumeIcon.icns", root / ".DS_Store"):
        if p.exists():
            os.chmod(p, 0o644)

    # HFS+ image: generous size (4 KiB allocation blocks per file + catalog), then compressed by `dmg`.
    nfiles = sum(len(f) + len(d) for _, d, f in os.walk(root))
    size = tree_size(root) + nfiles * 4096 + 48 * 1024 * 1024
    size = (size + (1 << 20) - 1) // (1 << 20) * (1 << 20)
    img = work / "Retrova.hfs"
    rmtree(img)
    with open(img, "wb") as fh:
        fh.truncate(size)
    logf = work / "hfsplus.log"
    rmtree(logf)
    run(["mkfs.hfsplus", "-v", C.APP_NAME, img], logfile=logf)
    run([hfsplus_tool(), "-s", "clone_link", "-m", "no", img, "addall", root], logfile=logf)
    run([hfsplus_tool(), img, "attr", "/", "C"], logfile=logf)          # volume has a custom icon
    run([hfsplus_tool(), img, "attr", "/.VolumeIcon.icns", "V"], logfile=logf)
    if has_ds:
        run([hfsplus_tool(), img, "attr", "/.DS_Store", "V"], logfile=logf)

    C.DIST.mkdir(parents=True, exist_ok=True)
    out = C.DIST / f"{C.APP_NAME}-{ver}-mac-{arch}.dmg"
    rmtree(out)
    run([dmg_tool(), "-c", "lzma", "-l", "9", "build", img, out], logfile=work / "dmg.log")
    log(f"[{target}] {out.name}: {human(out.stat().st_size)} (HFS+ {human(size)}, {nfiles} entries)")
    return out


def make_zip(target: str, app: Path, ver: str) -> Path:
    arch = C.TARGETS[target]["arch"]
    out = C.DIST / f"{C.APP_NAME}-{ver}-mac-{arch}.zip"
    rmtree(out)
    run(["zip", "-q", "-y", "-r", "-X", "-9", out, app.name], cwd=app.parent)
    return out


def build(target: str, icons: Path, ver: str) -> list[Path]:
    app = assemble_app(target, icons, ver)
    sign_app(app, target)
    outs = [make_dmg(target, app, icons, ver)]
    if os.environ.get("RETROVA_MAC_ZIP") == "1":
        outs.append(make_zip(target, app, ver))
    return outs
