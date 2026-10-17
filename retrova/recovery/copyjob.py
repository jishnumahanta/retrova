"""Copy job: one walker + N workers (threads, or processes on Windows) copying files
out of the read-only source. Resumable (finished files are skipped), never
overwrites, writes '<name>.retrova.part' and renames, and pauses itself at the
free-space reserve."""
from __future__ import annotations

import os
import queue
import threading
import time

from retrova.jobs import Job, StopJob

from retrova.util import free_bytes

from .common import LOG_DIR, MAX_WORKERS, NameMap, excluded, give_to_user, human, join_src, long_path, \
    makedirs_owned_safe, norm_src, safe_name
from .fsview import open_view
from .pool import WorkerPool
from .workers import copy_worker


class CopyJob(Job):
    kind = "copy"
    title = "Recovering files"
    exclusive = True

    def __init__(self, spec, label, paths, out_root, patterns, reserve_gb, workers, excluded_paths=(),
                 skipped_info=(), params=None):
        super().__init__(params)
        self.spec = dict(spec)
        self.src_label = label
        self.title = f"Recovering files from {label}" if label else "Recovering files"
        self.paths = [norm_src(p) for p in paths]
        self.out_root = out_root
        self.patterns = [str(p) for p in patterns]
        self.excluded_paths = {norm_src(x).lower() for x in excluded_paths}
        self.reserve = int(float(reserve_gb) * 1024 ** 3)
        self.nworkers = max(1, min(int(workers or 4), MAX_WORKERS))
        self.pool = WorkerPool(maxsize=4000)
        self.stop_reason = ""
        self.found_files = self.found_bytes = 0
        self.walk_done = False
        self.copied = self.skipped = self.failed = 0
        self.bytes_copied = self.skipped_bytes = 0
        self.inflight = 0
        self.renamed = 0
        self.links_skipped = 0
        self.current: dict[int, dict] = {}
        self.errors: list = []
        self.dir_times: list = []
        self.io_root = long_path(out_root)
        makedirs_owned_safe(self.io_root)
        logdir = os.path.join(self.io_root, LOG_DIR)
        makedirs_owned_safe(logdir)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.err_path = os.path.join(logdir, f"errors-{stamp}.txt")
        self.man_path = os.path.join(logdir, f"copied-{stamp}.tsv")
        self.err_log = open(self.err_path, "a", encoding="utf-8")
        self.manifest = open(self.man_path, "a", encoding="utf-8")
        give_to_user(self.err_path)
        give_to_user(self.man_path)
        self.skip_report = ""
        if skipped_info:
            self.skip_report = os.path.join(logdir, f"SKIPPED-REGENERABLE-{stamp}.md")
            self._write_skip_report(self.skip_report, skipped_info)

    def _write_skip_report(self, path, info):
        lines = ["# Folders skipped because they can be regenerated", "",
                 "These were NOT copied. Recreate them in each project with the command shown.", "",
                 "| Folder | What it is | How to regenerate |", "|---|---|---|"]
        for it in info:
            lines.append(f"| `{it.get('path', '')}` | {it.get('what', '')} | `{it.get('regen', '')}` |")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        give_to_user(path)

    # ---------------------------------------------------------------- control
    def pause(self):
        super().pause()
        if self.state == "paused":
            self.pool.pause()

    def resume(self):
        super().resume()
        self.pool.resume()

    def stop(self, reason="Stopped by you - press Start again to continue (finished files are skipped)."):
        if not self.stopping:
            self.stop_reason = reason
        super().stop()
        self.pool.stop()

    def _error(self, path, msg):
        with self.lock:
            self.failed += 1
            if len(self.errors) < 20000:
                self.errors.append({"path": path, "msg": str(msg)})
            try:
                self.err_log.write(f"{path}\t{msg}\n")
                self.err_log.flush()
            except (OSError, ValueError):
                pass

    # ---------------------------------------------------------------- run
    def run(self):
        self.phase = "Finding files…"
        n = self.nworkers
        self.pool.spawn(copy_worker, n, before=(self.spec,))
        collector = threading.Thread(target=self._collect, daemon=True, name="copy-collector")
        collector.start()
        try:
            self._walk()
        except StopJob:
            pass
        except Exception as ex:
            self._error("(walker)", f"{type(ex).__name__}: {ex}")
        finally:
            self.walk_done = True
            self.phase = "Copying…"
            self.pool.finish()
        collector.join()
        self.pool.join()
        for path, at, mt in reversed(self.dir_times):
            try:
                os.utime(path, ns=(at or mt, mt))
            except (OSError, OverflowError, ValueError):
                pass
        if not self.stopping:
            missing = self.found_files - (self.copied + self.skipped + self.failed)
            if missing > 0:  # safety net: every queued file must be confirmed
                self._error("(check)", f"{missing} files were not confirmed by a worker - press Start "
                                       f"again to retry them (finished files are skipped)")
        self.phase = "Stopped" if self.stopping else "Finished"
        if self.stopping and self.stop_reason:
            self.message = self.stop_reason

    def finish(self):
        for fh in (self.manifest, self.err_log):
            try:
                fh.close()
            except Exception:
                pass

    def _collect(self):
        alive = len(self.pool.workers)
        res_q = self.pool.res_q
        while alive > 0:
            try:
                msg = res_q.get(timeout=1.0)
            except queue.Empty:
                if not self.pool.alive():
                    break
                continue
            except (EOFError, OSError):
                break
            kind, idx = msg[0], msg[1]
            with self.lock:
                if kind == "bytes":
                    self.bytes_copied += msg[2]
                    if idx in self.current:
                        self.current[idx]["written"] += msg[2]
                elif kind == "cur":
                    self.current[idx] = {"path": msg[2], "written": 0, "size": msg[3]}
                elif kind in ("copied", "skipped"):
                    self.current.pop(idx, None)
                    self.inflight -= msg[3]
                    if kind == "copied":
                        self.copied += 1
                        try:
                            self.manifest.write(f"{msg[2]}\t{msg[3]}\t{msg[4]}\n")
                            if self.copied % 200 == 0:
                                self.manifest.flush()
                        except (OSError, ValueError):
                            pass
                    else:
                        self.skipped += 1
                        self.skipped_bytes += msg[3]
                elif kind == "drop":
                    self.current.pop(idx, None)
                    self.inflight -= msg[2]
                elif kind == "exit":
                    alive -= 1
                    self.current.pop(idx, None)
            if kind == "err":
                with self.lock:
                    self.current.pop(idx, None)
                    self.inflight -= msg[4]
                self._error(msg[2], msg[3])
            elif kind == "fatal":
                self._error(f"(worker {idx})", msg[2])
                self.stop(f"A copy worker could not start: {msg[2]}")

    def _reserve_space(self, size) -> bool:
        with self.lock:
            free = free_bytes(self.out_root)
            if free - self.inflight - size < self.reserve:
                return False
            self.inflight += size
            return True

    def _enqueue_file(self, node, sp, dp):
        size = int(node.size)
        with self.lock:
            self.found_files += 1
            self.found_bytes += size
        if os.path.isfile(dp) and os.path.getsize(dp) == size:
            with self.lock:  # already recovered on an earlier run - no need to bother a worker
                self.skipped += 1
                self.skipped_bytes += size
            return
        if node.handle is None:
            self._error(sp, node.error or "This file's details can't be read on the source.")
            return
        if not self._reserve_space(size):
            self.stop(f"The output drive is down to the {human(self.reserve)} you asked to keep free. Free up "
                      f"space or choose another drive, then press Start again - finished files are skipped.")
            raise StopJob()
        # plain ints/strs/tuples only: worker processes receive these through a pickling queue
        task = (node.handle, str(sp), str(dp), size, int(node.mtime_ns or 0), int(node.atime_ns or 0),
                int(node.mode or 0))
        if not self.pool.put(task, lambda: self.stopping):
            with self.lock:
                self.inflight -= size
            if not self.stopping and not self.pool.alive():
                self.stop("The copy workers stopped unexpectedly - press Start again to continue.")
            raise StopJob()

    def _walk(self):
        view = open_view(self.spec, cache_blocks=16384)
        try:
            for p in self.paths:
                self.checkpoint()
                try:
                    node = view.resolve(p)
                except Exception as ex:
                    self._error(p, f"Not found on the source: {ex}")
                    continue
                name = p.strip("/").split("/")[-1] or view.label or "Recovered"
                dst = os.path.join(self.io_root, safe_name(name))
                if node.kind == "file":
                    self._enqueue_file(node, p, dst)
                    continue
                if node.kind == "link":
                    self._link(view, node, p, dst)
                    continue
                if node.kind != "dir":
                    continue
                stack = [(node, p, dst)]
                while stack:
                    self.checkpoint()
                    dnode, sp, dp = stack.pop()
                    makedirs_owned_safe(dp)
                    if dnode.mtime_ns:
                        self.dir_times.append((dp, dnode.atime_ns, dnode.mtime_ns))
                    try:
                        entries = sorted(view.iterdir(dnode), key=lambda e: e.name)
                    except StopJob:
                        raise
                    except Exception as ex:
                        self._error(sp + "/", f"Can't list this folder on the source: {ex}")
                        continue
                    subdirs = []
                    names = NameMap()
                    for e in entries:
                        if excluded(e.name, self.patterns):
                            continue
                        csp = join_src(sp, e.name)
                        if csp.lower() in self.excluded_paths:
                            continue
                        out_name = names.get(e.name)
                        if out_name != e.name and out_name != e.name.replace("/", ":"):
                            with self.lock:
                                self.renamed += 1
                                try:
                                    self.manifest.write(f"RENAMED\t{csp}\t{out_name}\n")
                                except (OSError, ValueError):
                                    pass
                        cdp = os.path.join(dp, out_name)
                        try:
                            if e.kind == "dir":
                                subdirs.append((e, csp, cdp))
                            elif e.kind == "file":
                                self._enqueue_file(e, csp, cdp)
                            elif e.kind == "link":
                                self._link(view, e, csp, cdp)
                            elif e.error:
                                self._error(csp, e.error)
                        except StopJob:
                            raise
                        except Exception as ex:
                            self._error(csp, f"{type(ex).__name__}: {ex}")
                    stack.extend(reversed(subdirs))
        finally:
            view.close()

    def _link(self, view, node, sp, dp):
        try:
            target = view.readlink(node.handle)
        except Exception as ex:
            self._error(sp, f"Can't read this link on the source: {ex}")
            return
        if os.path.lexists(dp):
            return
        try:
            os.symlink(target, dp)
            give_to_user(dp)
        except (OSError, NotImplementedError) as ex:
            # e.g. Windows without the symlink privilege: not worth failing the recovery for
            with self.lock:
                self.links_skipped += 1
                try:
                    self.err_log.write(f"{sp}\tlink to {target} not recreated: {ex}\n")
                except (OSError, ValueError):
                    pass

    # ---------------------------------------------------------------- status
    def _refresh(self):
        speed = self.rate(self.bytes_copied)
        done = self.copied + self.skipped + self.failed
        left = self.found_bytes - self.bytes_copied - self.skipped_bytes
        eta = left / speed if self.walk_done and speed > 0 and self.state == "running" else None
        self.stats = {"files_done": done, "files_total": self.found_files, "bytes_done": self.bytes_copied,
                      "bytes_total": self.found_bytes, "speed": speed, "eta": eta, "failed": self.failed,
                      "skipped": self.skipped, "found": self.found_files, "copied": self.copied}
        if self.walk_done and self.found_bytes:
            self.progress = min(1.0, (self.bytes_copied + self.skipped_bytes) / self.found_bytes)
        elif self.walk_done and self.found_files:
            self.progress = done / self.found_files
        elif self.state == "done":
            self.progress = 1.0
        else:
            self.progress = None

    def status(self):
        with self.lock:
            self._refresh()
        return super().status()

    def extra_status(self):
        now = time.time()
        done_files = self.copied + self.skipped + self.failed
        return {
            "stop_reason": self.stop_reason, "out_root": self.out_root, "found_files": self.found_files,
            "found_bytes": self.found_bytes, "walk_done": self.walk_done, "copied": self.copied,
            "skipped": self.skipped, "failed": self.failed, "bytes_copied": self.bytes_copied,
            "speed": self.stats.get("speed", 0), "current": [dict(c) for c in list(self.current.values())],
            "errors": self.errors[-60:], "error_count": len(self.errors), "free": free_bytes(self.out_root),
            "reserve": self.reserve, "error_log": self.err_path, "manifest": self.man_path,
            "skip_report": self.skip_report, "paths": self.paths, "renamed": self.renamed,
            "links_skipped": self.links_skipped, "sid_source": self.spec.get("source"),
            "files_per_sec": done_files / max(now - self.t0, 0.001),
            "engine": f"{self.nworkers} {self.pool.engine_name}",
        }

    def result(self, q):
        off = max(0, int(q.get("offset") or 0))
        lim = max(1, min(int(q.get("limit") or 300), 5000))
        with self.lock:
            items = self.errors[off:off + lim]
            total = len(self.errors)
        return {"errors": items, "total": total, "more": max(0, total - off - len(items))}

