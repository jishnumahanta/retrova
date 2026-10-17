#!/usr/bin/env python3
"""Retrova release build. Normally run through packaging/build_all.sh.

  build.py [all]                      everything below, in order
  build.py prepare                    host checks, downloads, build tools
  build.py icons                      Retrova.icns / Retrova.ico from brand/logo.svg
  build.py mac [arm64|x64]            Retrova.app -> dist/Retrova-<ver>-mac-<arch>.dmg
  build.py win                        dist/Retrova-<ver>-win-x64-Setup.exe + -portable.zip
  build.py verify [mac|win|sim]       checks on what was built (also run by `all`)
  build.py sums                       dist/SHA256SUMS
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rbuild import config as C  # noqa: E402
from rbuild import common  # noqa: E402
from rbuild.common import die, human, log, sha256_file  # noqa: E402


def step_prepare(need_wine=False):
    from rbuild import tools
    tools.check_host(need_wine)
    for t in ("mac-arm64", "mac-x64", "win-x64", "linux-x64"):
        common.fetch_python(t)
    tools.build_libdmg()
    tools.rcodesign()
    from rbuild import deps
    deps.ensure_wheelhouse()


def step_icons() -> Path:
    from rbuild import icons
    out = C.BUILD / "icons"
    icons.main(str(C.BRAND / "logo.svg"), str(out))
    return out


def step_mac(archs, icons_dir, ver):
    from rbuild import mac
    outs = []
    for a in archs:
        outs += mac.build(f"mac-{a}", icons_dir, ver)
    return outs


def step_win(icons_dir, ver):
    from rbuild import win
    return win.build(icons_dir, ver)


def step_verify(which):
    from rbuild import verify
    return verify.run_all(which)


def step_sums():
    files = sorted(p for p in C.DIST.iterdir()
                   if p.is_file() and p.suffix in (".dmg", ".exe", ".zip") and p.name.startswith(C.APP_NAME))
    if not files:
        die("nothing in dist/ to checksum")
    lines = [f"{sha256_file(p)}  {p.name}" for p in files]
    (C.DIST / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    for p in files:
        log(f"  {p.name:45s} {human(p.stat().st_size):>10s}")
    return files


def main(argv):
    cmd = argv[0] if argv else "all"
    args = argv[1:]
    ver = C.app_version()
    log(f"Retrova {ver} | CPython {C.PY_VERSION} (pbs {C.PBS_RELEASE}) | dist -> {C.DIST}")
    if cmd == "prepare":
        step_prepare()
    elif cmd == "icons":
        step_icons()
    elif cmd == "mac":
        step_prepare()
        step_mac(args or ["arm64", "x64"], step_icons(), ver)
    elif cmd == "win":
        step_prepare()
        step_win(step_icons(), ver)
    elif cmd == "verify":
        step_verify(args or ["sim", "mac", "win"])
    elif cmd == "sums":
        step_sums()
    elif cmd == "all":
        step_prepare()
        icons_dir = step_icons()
        step_mac(["arm64", "x64"], icons_dir, ver)
        step_win(icons_dir, ver)
        step_verify(["sim", "mac", "win"])
        step_sums()
    else:
        print(__doc__)
        return 2
    if common.WARNINGS:
        print("\n==== warnings ====", file=sys.stderr)
        for w in common.WARNINGS:
            print(" - " + w, file=sys.stderr)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
