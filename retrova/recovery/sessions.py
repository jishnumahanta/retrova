"""Open drives/images ("sessions"): one browsing view per session behind a lock,
a separate preview view (so streaming a video never blocks browsing) and a
background folder-size service with its own view."""
from __future__ import annotations

import os
import queue
import threading
import time
import uuid

from .common import excluded, is_device_path, join_src, norm_src
from .fsview import open_view

MAX_SESSIONS = 8


class SizeService:
    """Measures folder sizes in a background thread with its own read-only view."""

    def __init__(self, spec):
        self.spec = spec
        self.results: dict[str, dict] = {}
        self.q: queue.Queue = queue.Queue()
        self.gen = 0
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.closed = False

    @staticmethod
    def key(path, patterns):
        return path + "|" + ",".join(sorted(patterns))

    def request(self, path, patterns) -> str:
        k = self.key(path, patterns)
        with self.lock:
            r = self.results.get(k)
            if r and r["state"] in ("queued", "running", "done"):
                return k
            self.results[k] = {"state": "queued", "bytes": 0, "files": 0, "errors": 0}
            self.q.put((k, path, list(patterns), self.gen))
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="sizes")
                self.thread.start()
        return k

    def get(self, k) -> dict | None:
        with self.lock:
            r = self.results.get(k)
            return dict(r) if r else None

    def cancel(self):
        with self.lock:
            self.gen += 1
            for r in self.results.values():
                if r["state"] in ("queued", "running"):
                    r["state"] = "cancelled"

    def close(self):
        self.closed = True
        self.cancel()
        self.q.put(None)

    def _run(self):
        view = None
        try:
            while not self.closed:
                item = self.q.get()
                if item is None:
                    break
                k, path, patterns, gen = item
                if gen != self.gen:
                    continue
                r = self.results[k]
                r["state"] = "running"
                try:
                    if view is None:
                        view = open_view(self.spec, cache_blocks=4096)
                    self._measure(view, path, patterns, gen, r)
                except Exception as ex:
                    r["state"], r["msg"] = "error", str(ex)
        finally:
            if view is not None:
                view.close()

    def _measure(self, view, path, patterns, gen, r):
        node = view.resolve(path)
        if node.kind != "dir":
            r["bytes"], r["files"] = node.size, 1 if node.kind == "file" else 0
            r["state"] = "done"
            return
        stack = [node]
        while stack:
            if gen != self.gen or self.closed:
                r["state"] = "cancelled"
                return
            n = stack.pop()
            try:
                children = list(view.iterdir(n))
            except Exception:
                r["errors"] += 1
                continue
            for c in children:
                if excluded(c.name, patterns):
                    continue
                if c.error:
                    r["errors"] += 1
                if c.kind == "dir":
                    stack.append(c)
                elif c.kind == "file":
                    r["bytes"] += c.size
                    r["files"] += 1
        r["state"] = "done"


class Session:
    def __init__(self, spec: dict, view, info: dict):
        self.sid = uuid.uuid4().hex[:12]
        self.spec = spec
        self.view = view
        self.lock = threading.Lock()
        self._preview = None
        self.preview_lock = threading.Lock()
        self.sizes = SizeService(spec)
        self.info = info
        self.used = time.time()

    @property
    def source(self):
        return self.spec["source"]

    @property
    def is_image(self):
        return not is_device_path(self.spec["source"])

    def preview_view(self):
        """Caller must hold preview_lock."""
        if self._preview is None:
            self._preview = open_view(self.spec, cache_blocks=4096)
        return self._preview

    def ls(self, path: str) -> dict:
        path = norm_src(path)
        with self.lock:
            node = self.view.resolve(path)
            if node.kind != "dir":
                raise NotADirectoryError(f"{path} is a file, not a folder.")
            rows = []
            for c in self.view.iterdir(node):
                row = {"name": c.name, "path": join_src(path, c.name), "kind": c.kind,
                       "size": c.size if c.kind == "file" else None,
                       "mtime": c.mtime_ns // 1_000_000 if c.mtime_ns else None}
                if c.error:
                    row["error"] = c.error
                rows.append(row)
        rows.sort(key=lambda r: (r["kind"] != "dir", r["name"].lower()))
        return {"path": path, "entries": rows}

    def close(self):
        self.sizes.close()
        with self.lock:
            try:
                self.view.close()
            except Exception:
                pass
        with self.preview_lock:
            if self._preview is not None:
                try:
                    self._preview.close()
                except Exception:
                    pass
                self._preview = None


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def add_session(s: Session):
    with SESSIONS_LOCK:
        SESSIONS[s.sid] = s
        if len(SESSIONS) > MAX_SESSIONS:
            oldest = sorted(SESSIONS.values(), key=lambda x: x.used)[: len(SESSIONS) - MAX_SESSIONS]
            for o in oldest:
                SESSIONS.pop(o.sid, None)
                threading.Thread(target=o.close, daemon=True).start()


def get_session(sid: str) -> Session | None:
    s = SESSIONS.get(str(sid or ""))
    if s:
        s.used = time.time()
    return s


def close_session(sid: str) -> bool:
    with SESSIONS_LOCK:
        s = SESSIONS.pop(str(sid or ""), None)
    if s:
        s.close()
    return s is not None


def source_exists(path: str) -> bool:
    if is_device_path(path) and os.name == "nt":
        return True       # \\.\PhysicalDriveN can't be stat()ed; opening tells
    return os.path.exists(path)
