"""Build-host tools: libdmg-hfsplus (`hfsplus`, `dmg`), rcodesign, host package checks."""
from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

from . import config as C
from .common import die, download, log, rmtree, run

APT_PACKAGES = {
    # command -> apt package providing it
    "makensis": "nsis",
    "x86_64-w64-mingw32-gcc": "gcc-mingw-w64-x86-64",
    "x86_64-w64-mingw32-windres": "binutils-mingw-w64-x86-64",
    "mkfs.hfsplus": "hfsprogs",
    "cmake": "cmake",
    "gcc": "build-essential",
    "git": "git",
    "zip": "zip",
    "unzip": "unzip",
}
APT_LIBS = ["zlib1g-dev", "libbz2-dev", "liblzma-dev", "libssl-dev"]


def check_host(need_wine: bool = False) -> None:
    missing = sorted({pkg for cmd, pkg in APT_PACKAGES.items() if not shutil.which(cmd)})
    if missing:
        die("missing build tools. Install them with:\n  apt-get update && apt-get install -y "
            + " ".join(missing + APT_LIBS) + (" wine wine64 xvfb" if need_wine else ""))


def libdmg_dir() -> Path:
    return C.TOOLS / f"libdmg-hfsplus-{C.LIBDMG_COMMIT[:12]}"


def build_libdmg() -> Path:
    out = libdmg_dir()
    if (out / "dmg").exists() and (out / "hfsplus").exists():
        return out
    src = C.CACHE / "src" / "libdmg-hfsplus"
    if not (src / ".git").exists():
        rmtree(src)
        src.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", C.LIBDMG_GIT, src])
    have = run(["git", "-C", src, "rev-parse", "HEAD"], quiet=True, capture=True).stdout.strip()
    if have != C.LIBDMG_COMMIT:
        run(["git", "-C", src, "fetch", "--quiet", "origin"], check=False)
        run(["git", "-C", src, "checkout", "--quiet", C.LIBDMG_COMMIT])
    bld = C.CACHE / "src" / "libdmg-build"
    rmtree(bld)
    bld.mkdir(parents=True)
    log("building libdmg-hfsplus (hfsplus + dmg tools)")
    run(["cmake", "-S", src, "-B", bld, "-DCMAKE_BUILD_TYPE=Release"], quiet=True)
    run(["cmake", "--build", bld, "--target", "dmg-bin", "hfsplus", "-j", "4"], quiet=True)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bld / "dmg" / "dmg", out / "dmg")
    shutil.copy2(bld / "hfs" / "hfsplus", out / "hfsplus")
    r = run([out / "dmg"], quiet=True, check=False, capture=True)
    if "lzma" not in r.stdout:
        die("libdmg-hfsplus was built without LZMA support (install liblzma-dev and rebuild)")
    return out


def hfsplus_tool() -> Path:
    return build_libdmg() / "hfsplus"


def dmg_tool() -> Path:
    return build_libdmg() / "dmg"


def rcodesign() -> Path:
    name = f"apple-codesign-{C.RCODESIGN_VERSION}-x86_64-unknown-linux-musl"
    exe = C.TOOLS / name / "rcodesign"
    if not exe.exists():
        tgz = download(C.RCODESIGN_URL, C.DOWNLOADS / f"{name}.tar.gz", C.RCODESIGN_SHA256)
        with tarfile.open(tgz) as tf:
            tf.extractall(C.TOOLS, filter="data")
        exe.chmod(0o755)
    return exe
