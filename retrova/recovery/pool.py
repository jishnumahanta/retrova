"""Worker plumbing shared by copy and verify: threads on macOS/Linux, separate
processes (spawn) on Windows where one Python process can't keep a fast disk busy."""
from __future__ import annotations

import queue
import threading
import time

from .common import USE_PROCESSES


class WorkerPool:
    def __init__(self, maxsize: int):
        self.processes = USE_PROCESSES
        if self.processes:
            import multiprocessing as mp
            self.ctx = mp.get_context("spawn")
            self.task_q = self.ctx.Queue(maxsize=maxsize)
            self.res_q = self.ctx.Queue()
            self.stop_ev, self.run_ev = self.ctx.Event(), self.ctx.Event()
        else:
            self.ctx = None
            self.task_q = queue.Queue(maxsize=maxsize)
            self.res_q = queue.Queue()
            self.stop_ev, self.run_ev = threading.Event(), threading.Event()
        self.run_ev.set()
        self.workers: list = []

    @property
    def engine_name(self):
        return "processes" if self.processes else "threads"

    def spawn(self, target, n: int, before: tuple = (), after: tuple = ()):
        """Start n workers: target(i, *before, task_q, res_q, run_ev, stop_ev, *after)."""
        for i in range(n):
            args = (i, *before, self.task_q, self.res_q, self.run_ev, self.stop_ev, *after)
            if self.processes:
                w = self.ctx.Process(target=target, args=args, daemon=True)
            else:
                w = threading.Thread(target=target, args=args, daemon=True, name=f"worker-{i}")
            w.start()
            self.workers.append(w)
        return self.workers

    def alive(self) -> bool:
        return any(w.is_alive() for w in self.workers)

    def put(self, item, stopping) -> bool:
        """Queue a task; False when the job is stopping or every worker died."""
        while True:
            if stopping():
                return False
            try:
                self.task_q.put(item, timeout=0.5)
                return True
            except queue.Full:
                if not self.alive():
                    return False

    def finish(self):
        """Tell every worker there is no more work (never blocks forever)."""
        for _ in self.workers:
            while True:
                try:
                    self.task_q.put(None, timeout=0.5)
                    break
                except queue.Full:
                    if not self.alive():
                        break

    def join(self, timeout=5.0):
        end = time.time() + timeout
        for w in self.workers:
            w.join(timeout=max(0.1, end - time.time()))
        if self.processes:
            for w in self.workers:
                if w.is_alive():
                    w.terminate()
            for q in (self.task_q, self.res_q):
                try:
                    q.cancel_join_thread()
                    q.close()
                except Exception:
                    pass

    def pause(self):
        self.run_ev.clear()

    def resume(self):
        self.run_ev.set()

    def stop(self):
        self.stop_ev.set()
        self.run_ev.set()
