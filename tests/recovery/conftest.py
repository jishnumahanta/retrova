import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
APFS_DIR = os.path.join(ROOT, "tests", "apfs_images")
# The APFS test images are stored gzipped in git (they are mostly zeros); unpack any that are missing.
for _gz in sorted(os.listdir(APFS_DIR)) if os.path.isdir(APFS_DIR) else ():
    _raw = os.path.join(APFS_DIR, _gz[:-3])
    if _gz.endswith(".gz") and not os.path.exists(_raw):
        import gzip
        with gzip.open(os.path.join(APFS_DIR, _gz), "rb") as _src, open(_raw + ".part", "wb") as _dst:
            shutil.copyfileobj(_src, _dst)
        os.replace(_raw + ".part", _raw)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from make_images import build_all, tree_files  # noqa: E402

PY = sys.executable


@pytest.fixture(scope="session")
def images(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("fsimages"))
    res = build_all(out)
    if "fat32" not in res:
        pytest.skip("mkfs tools (dosfstools, mtools, e2fsprogs...) are not installed")
    return res


@pytest.fixture(scope="session")
def tree():
    return tree_files()


def need(images, key):
    if key not in images:
        pytest.skip(f"{key} image could not be built here (tool missing)")
    return images[key]


@pytest.fixture(scope="session", autouse=True)
def _register():
    from retrova.recovery import register
    register()


def run_job(job, timeout=120):
    from retrova.jobs import JOBS
    JOBS.start(job)
    end = time.time() + timeout
    while job.live():
        if time.time() > end:
            job.stop()
            raise AssertionError(f"job {job.kind} did not finish in {timeout}s: {job.status()}")
        time.sleep(0.05)
    return job.status()


def wait_status(status, timeout=120):
    from retrova.jobs import JOBS
    return run_wait(JOBS.get(status["id"]), timeout)


def run_wait(job, timeout=120):
    end = time.time() + timeout
    while job.live():
        if time.time() > end:
            raise AssertionError(f"job {job.kind} did not finish: {job.status()}")
        time.sleep(0.05)
    return job.status()


def read_tree(root):
    """relative posix path -> bytes, for every file under root (logs excluded)."""
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ("_retrova_logs", "_recovery-verify")]
        for f in fns:
            p = os.path.join(dp, f)
            if os.path.islink(p):
                continue
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace(os.sep, "/")] = fh.read()
    return out


def expected_for(tree, fs, mode=None):
    """The part of the shared test tree each filesystem image actually holds."""
    files = {k: v for k, v in tree.items() if not k.endswith("/")}
    if fs == "fat12":
        files = {k: v for k, v in files.items() if k in ("hello.txt", "zero.txt") or k.startswith("docs/")}
    if fs == "xfs":
        files = {k: v for k, v in files.items() if " " not in k}
    if fs == "ntfs" and mode == "flat":
        files = {k: v for k, v in files.items() if k in ("hello.txt", "big.bin", "zero.txt")}
    return files


# ---------------------------------------------------------------- real HTTP server
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Api:
    def __init__(self, port, token, proc):
        self.base, self.token, self.proc = f"http://127.0.0.1:{port}", token, proc

    def req(self, path, body=None, headers=None, raw=False):
        h = {"X-Token": self.token}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=data, headers=h, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                content = resp.read()
                if raw:
                    return resp.status, dict(resp.headers), content
                return resp.status, json.loads(content or b"{}")
        except urllib.error.HTTPError as e:
            content = e.read()
            if raw:
                return e.code, dict(e.headers), content
            try:
                return e.code, json.loads(content or b"{}")
            except ValueError:
                return e.code, {"raw": content}

    def get(self, path, **kw):
        return self.req(path, **kw)

    def post(self, path, body=None, **kw):
        return self.req(path, body or {}, **kw)

    def ok(self, path, body=None):
        st, d = self.req(path, body)
        assert st == 200, (path, st, d)
        return d

    def wait(self, job, timeout=180):
        end = time.time() + timeout
        while True:
            st = self.ok(f"/api/job?id={job['id']}")
            if st["state"] not in ("queued", "running", "paused", "stopping"):
                return st
            if time.time() > end:
                raise AssertionError(f"job did not finish: {st}")
            time.sleep(0.1)


def start_server(env_extra=None):
    port, token = free_port(), "test-token-123"
    env = dict(os.environ)
    env.update(env_extra or {})
    env["RETROVA_APP_DATA"] = env.get("RETROVA_APP_DATA") or os.path.join(ROOT, "tests", ".appdata")
    proc = subprocess.Popen([PY, "-m", "retrova", "--no-window", "--port", str(port), "--token", token], cwd=ROOT,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    api = Api(port, token, proc)
    end = time.time() + 30
    while time.time() < end:
        try:
            if api.get("/api/info")[0] == 200:
                return api
        except Exception:
            pass
        if proc.poll() is not None:
            raise AssertionError("server exited: " + proc.stdout.read().decode(errors="replace"))
        time.sleep(0.2)
    proc.kill()
    raise AssertionError("server did not start")


def stop_server(api):
    api.proc.terminate()
    try:
        api.proc.wait(10)
    except subprocess.TimeoutExpired:
        api.proc.kill()


@pytest.fixture(scope="module")
def server():
    api = start_server()
    yield api
    stop_server(api)


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / "out"
    yield str(d)
    shutil.rmtree(str(d), ignore_errors=True)
