#!/usr/bin/env bash
# Build every Retrova release artifact on a Debian/Ubuntu x86_64 host:
#   dist/Retrova-<ver>-mac-arm64.dmg, -mac-x64.dmg, -win-x64-Setup.exe, -win-x64-portable.zip, SHA256SUMS
# Usage: packaging/build_all.sh            (all)
#        packaging/build_all.sh mac arm64  (any build.py sub-command)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
venv="$here/.cache/venv"

need=(makensis x86_64-w64-mingw32-gcc mkfs.hfsplus cmake gcc git zip unzip)
missing=()
for c in "${need[@]}"; do command -v "$c" >/dev/null || missing+=("$c"); done
if ((${#missing[@]})); then
  echo "Installing build tools (needs sudo): ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y nsis gcc-mingw-w64-x86-64 binutils-mingw-w64-x86-64 hfsprogs cmake \
       build-essential git zip unzip zlib1g-dev libbz2-dev liblzma-dev libssl-dev
fi

if [[ ! -x "$venv/bin/python" ]]; then
  python3 -m venv "$venv"
  "$venv/bin/pip" install -q --upgrade pip
  "$venv/bin/pip" install -q "ziglang==0.17.0" cairosvg pillow ds_store macholib
fi

exec "$venv/bin/python" "$here/build.py" "${@:-all}"
