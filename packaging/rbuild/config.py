"""Pinned inputs for the Retrova release build. Bump versions here (and in requirements/constraints.txt)."""
from __future__ import annotations

import os
import re
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent          # packaging/
ROOT = PKG_DIR.parent                                      # project root
APP_SRC = ROOT / "retrova"                                  # the app package, packaged as-is at build time
BRAND = ROOT / "brand"
DIST = Path(os.environ.get("RETROVA_DIST", ROOT / "dist"))
CACHE = Path(os.environ.get("RETROVA_CACHE", PKG_DIR / ".cache"))
BUILD = Path(os.environ.get("RETROVA_BUILD_DIR", PKG_DIR / "build"))
DOWNLOADS = CACHE / "downloads"
TOOLS = CACHE / "tools"

APP_NAME = "Retrova"
BUNDLE_ID = "com.retrova.app"
MODULE = "retrova"
MAC_MIN_OS = "11.0"
COPYRIGHT = "Copyright © 2026 Jishnu Mahanta. Free software under the GNU AGPL-3.0."

# --- python-build-standalone (https://github.com/astral-sh/python-build-standalone/releases)
PBS_RELEASE = os.environ.get("RETROVA_PBS_RELEASE", "20261001")
PY_VERSION = os.environ.get("RETROVA_PY_VERSION", "3.12.15")
PY_MAJMIN = ".".join(PY_VERSION.split(".")[:2])            # 3.12
PY_NODOT = PY_MAJMIN.replace(".", "")                      # 312
PBS_FLAVOR = "install_only_stripped"
PBS_BASE = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS_RELEASE}"

# --- rcodesign (ad-hoc Mach-O / bundle signing on Linux)
RCODESIGN_VERSION = "0.29.0"
RCODESIGN_URL = ("https://github.com/indygreg/apple-platform-rs/releases/download/"
                 f"apple-codesign%2F{RCODESIGN_VERSION}/apple-codesign-{RCODESIGN_VERSION}-x86_64-unknown-linux-musl.tar.gz")
RCODESIGN_SHA256 = "dbe85cedd8ee4217b64e9a0e4c2aef92ab8bcaaa41f20bde99781ff02e600002"   # = upstream .sha256 file

# --- libdmg-hfsplus (Mozilla's maintained fork): builds the `hfsplus` and `dmg` tools
LIBDMG_GIT = "https://github.com/mozilla/libdmg-hfsplus.git"
LIBDMG_COMMIT = "e18579b391950589b9ed16ee630c54ce4319bb29"

# --- zig (C cross compiler for the macOS launcher), installed from PyPI into the build venv
ZIGLANG_VERSION = "0.17.0"


def app_version() -> str:
    text = (APP_SRC / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", text)
    if not m:
        raise SystemExit("could not read __version__ from retrova/__init__.py")
    return m.group(1)


def version_tuple4(ver: str) -> tuple[int, int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", ver)[:4]]
    return tuple((nums + [0, 0, 0, 0])[:4])  # type: ignore[return-value]


TARGETS = {
    "mac-arm64": {
        "os": "mac", "arch": "arm64", "triple": "aarch64-apple-darwin",
        "pip_platforms": ["macosx_11_0_arm64"],
        "zig_target": f"aarch64-macos.{MAC_MIN_OS}", "macho_cpu": "ARM64",
        "requirements": ["common.txt", "mac.txt"],
    },
    "mac-x64": {
        "os": "mac", "arch": "x64", "triple": "x86_64-apple-darwin",
        "pip_platforms": ["macosx_11_0_x86_64"],
        "zig_target": f"x86_64-macos.{MAC_MIN_OS}", "macho_cpu": "X86_64",
        "requirements": ["common.txt", "mac.txt"],
    },
    "win-x64": {
        "os": "win", "arch": "x64", "triple": "x86_64-pc-windows-msvc",
        "pip_platforms": ["win_amd64"],
        "requirements": ["common.txt", "win.txt"],
    },
    # Only used to simulate the macOS bundle layout on this Linux build host.
    "linux-x64": {
        "os": "linux", "arch": "x64", "triple": "x86_64-unknown-linux-gnu",
        "pip_platforms": None,   # installed natively with the bundled Linux python
        "requirements": ["common.txt"],
    },
}


def pbs_filename(target: str) -> str:
    return f"cpython-{PY_VERSION}+{PBS_RELEASE}-{TARGETS[target]['triple']}-{PBS_FLAVOR}.tar.gz"
