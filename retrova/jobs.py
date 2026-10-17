"""Background jobs: every long-running operation is a Job the UI polls.

Subclass Job, set `kind`, implement run(); call self.checkpoint() often (it
handles Pause and Stop) and keep self.phase / self.progress / self.stats
current. Put large result lists behind result(query) so status() stays small.
"""
from __future__ import annotations

import collections
import threading
import time
import traceback
import uuid


class StopJob(Exception):
    """Raised by checkpoint() when the user pressed Stop."""


class Job:
    kind = "job"
    title = "Working"
    exclusive = False      # True: only one exclusive job (e.g. anything reading a raw disk) at a time

    def __init__(self, params: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.params = dict(params or {})
        self.state = "queued"            # queued running paused stopping stopped done error
        self.phase = ""
        self.message = ""
        self.error = ""
        self.progress: float | None = None   # 0..1 when known
        self.stats: dict = {}
        self.t0 = time.time()
        self.t_end: float | None = None
        self.lock = threading.RLock()
        self._run_ev = threading.Event()
        self._run_ev.set()
        self._stop_ev = threading.Event()
        self._samples: collections.deque = collections.deque(maxlen=30)
        self._thread: threading.Thread | None = None

    # ------------------------------------------------ lifecycle
    def start(self):
        self._thread = threading.Thread(target=self._wrap, daemon=True, name=f"job-{self.kind}")
        self._thread.start()
        return self

    def _wrap(self):
        self.state = "running"
        try:
            self.run()
            self.state = "stopped" if self._stop_ev.is_set() else "done"
        except StopJob:
            self.state = "stopped"
        except Exception as ex:
            self.error = f"{type(ex).__name__}: {ex}"
            self.state = "error"
            traceback.print_exc()
        finally:
            self.t_end = time.time()
            try:
                self.finish()
            except Exception:
                traceback.print_exc()

    def run(self):
        raise NotImplementedError

    def finish(self):
        """Called once after run() ends, whatever the outcome (close files, write reports)."""

    def checkpoint(self):
        """Call often from run(): blocks while paused, raises StopJob when stopped."""
        if not self._run_ev.is_set():
            while not self._run_ev.wait(0.3):
                pass
        if self._stop_ev.is_set():
            raise StopJob()

    @property
    def stopping(self) -> bool:
        return self._stop_ev.is_set()

    def pause(self):
        if self.state == "running":
            self._run_ev.clear()
            self.state = "paused"

    def resume(self):
        if self.state == "paused":
            self.state = "running"
            self._run_ev.set()

    def stop(self):
        self._stop_ev.set()
        self._run_ev.set()
        if self.state in ("queued", "running", "paused"):
            self.state = "stopping"

    def live(self) -> bool:
        return self.state in ("queued", "running", "paused", "stopping")

    # ------------------------------------------------ reporting
    def rate(self, value: float) -> float:
        """Smoothed per-second rate of a growing counter (call from status())."""
        now = time.time()
        self._samples.append((now, value))
        t, v = self._samples[0]
        return (value - v) / (now - t) if now - t > 0.5 else 0.0

    def extra_status(self) -> dict:
        return {}

    def status(self) -> dict:
        with self.lock:
            d = {"id": self.id, "kind": self.kind, "title": self.title, "state": self.state,
                 "phase": self.phase, "message": self.message, "error": self.error,
                 "progress": self.progress, "stats": dict(self.stats),
                 "elapsed": (self.t_end or time.time()) - self.t0, "params": self.params}
            d.update(self.extra_status())
            return d

    def result(self, query: dict) -> dict:
        return {}


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def start(self, job: Job) -> Job:
        with self.lock:
            if job.exclusive:
                for j in self.jobs.values():
                    if j.exclusive and j.live():
                        raise RuntimeError(f"Wait for '{j.title}' to finish (or stop it) first - "
                                           f"only one job reads a disk at a time.")
            self.jobs[job.id] = job
        return job.start()

    def get(self, jid: str) -> Job:
        j = self.jobs.get(jid)
        if not j:
            raise KeyError(f"No such job: {jid}")
        return j

    def list(self, kind: str | None = None):
        return [j.status() for j in sorted(self.jobs.values(), key=lambda j: j.t0, reverse=True)
                if kind is None or j.kind == kind]

    def latest(self, kind: str) -> Job | None:
        js = [j for j in self.jobs.values() if j.kind == kind]
        return max(js, key=lambda j: j.t0) if js else None


JOBS = JobManager()
