"""Small helpers shared by the build steps: logging, running tools, cached downloads."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from . import config as C

_T0 = time.time()
WARNINGS: list[str] = []


def log(msg: str) -> None:
    print(f"[{time.time() - _T0:7.1f}s] {msg}", flush=True)


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def run(cmd, *, cwd=None, env=None, quiet=False, check=True, capture=False, timeout=None, logfile=None):
    """Run a command; print it; raise with the tail of its output on failure."""
    cmd = [str(c) for c in cmd]
    if not quiet:
        log("$ " + " ".join(cmd))
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update({k: str(v) for k, v in env.items() if v is not None})
        for k, v in env.items():
            if v is None:
                full_env.pop(k, None)
    if logfile:
        with open(logfile, "ab") as fh:
            p = subprocess.run(cmd, cwd=cwd, env=full_env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
        if check and p.returncode != 0:
            tail = Path(logfile).read_bytes()[-4000:].decode("utf-8", "replace")
            die(f"command failed ({p.returncode}): {' '.join(cmd)}\n--- tail of {logfile} ---\n{tail}")
        return p
    p = subprocess.run(cmd, cwd=cwd, env=full_env, timeout=timeout,
                       stdout=subprocess.PIPE if (capture or quiet) else None,
                       stderr=subprocess.STDOUT if (capture or quiet) else None)
    if check and p.returncode != 0:
        out = (p.stdout or b"")[-4000:].decode("utf-8", "replace") if p.stdout else ""
        die(f"command failed ({p.returncode}): {' '.join(cmd)}\n{out}")
    if capture:
        p.stdout = (p.stdout or b"").decode("utf-8", "replace")
    return p


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, sha256: str | None = None) -> Path:
    """Download into the cache once; verify the checksum every time."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and sha256 and sha256_file(dest) != sha256:
        warn(f"cached {dest.name} has the wrong checksum - downloading again")
        dest.unlink()
    if not dest.exists():
        log(f"downloading {url}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as fh:
                    shutil.copyfileobj(r, fh, 1 << 20)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    die(f"download failed: {url}: {e}")
                time.sleep(3)
        tmp.rename(dest)
    if sha256:
        got = sha256_file(dest)
        if got != sha256:
            die(f"checksum mismatch for {dest.name}: expected {sha256}, got {got}")
    return dest


def pbs_checksums() -> dict[str, str]:
    sums = download(f"{C.PBS_BASE}/SHA256SUMS", C.DOWNLOADS / f"SHA256SUMS-{C.PBS_RELEASE}")
    out = {}
    for line in sums.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    return out


def fetch_python(target: str) -> Path:
    """Download (cached) + extract (cached) python-build-standalone for a target; returns .../python."""
    name = C.pbs_filename(target)
    sums = pbs_checksums()
    if name not in sums:
        die(f"{name} is not listed in the {C.PBS_RELEASE} SHA256SUMS")
    tarball = download(f"{C.PBS_BASE}/{name}", C.DOWNLOADS / name, sums[name])
    dest = C.CACHE / "runtimes" / name.replace(".tar.gz", "")
    marker = dest / ".extracted"
    if not marker.exists():
        log(f"extracting {name}")
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        with tarfile.open(tarball) as tf:
            tf.extractall(dest, filter="tar")
        marker.write_text(sums[name])
    return dest / "python"


def copytree(src: Path, dst: Path, ignore=None) -> None:
    shutil.copytree(src, dst, symlinks=True, ignore=ignore, dirs_exist_ok=True)


def rmtree(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.exists():
        shutil.rmtree(p)


def tree_size(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
    return total


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def venv_python() -> Path:
    return C.CACHE / "venv" / "bin" / "python"


def linux_python() -> Path:
    """The Linux python-build-standalone 3.12 (same bytecode magic as the bundled runtimes)."""
    return fetch_python("linux-x64") / "bin" / f"python{C.PY_MAJMIN}"
