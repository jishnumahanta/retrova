"""Regen job: walk the selected folders on the source (folder names only, no file
contents) looking for regenerable folders (node_modules, build caches, venvs...)."""
from __future__ import annotations

from retrova.jobs import Job, StopJob

from .common import join_src, norm_src
from .fsview import open_view
from .regen import any_lockfile, classify_regen, venv_verdict


class RegenJob(Job):
    kind = "regen"
    title = "Looking for folders that can be regenerated"
    exclusive = False          # light, metadata only - fine next to a running copy

    def __init__(self, spec, paths, params=None):
        super().__init__(params)
        self.spec = dict(spec)
        self.paths = [norm_src(p) for p in paths]
        self.found: list = []
        self.dirs = 0

    def run(self):
        self.phase = "Looking at folder names…"
        view = open_view(self.spec, cache_blocks=4096)
        try:
            stack = []
            for p in self.paths:
                try:
                    node = view.resolve(p)
                except Exception:
                    continue
                if node.kind == "dir":
                    stack.append((node, p, False, None))
            while stack:
                self.checkpoint()
                node, path, lock_above, parent_names = stack.pop()
                try:
                    entries = list(view.iterdir(node))
                except StopJob:
                    raise
                except Exception:
                    continue
                self.dirs += 1
                names = [e.name for e in entries]
                if parent_names is not None and "pyvenv.cfg" in names:
                    # a Python environment with an unusual name (e.g. .release-venv)
                    conf, what, regen = venv_verdict(path.rsplit("/", 1)[-1], parent_names)
                    self._add(path, conf, what, regen)
                    continue
                lock_here = lock_above or any_lockfile(names)  # monorepos: lockfile at the root
                for e in entries:
                    if e.kind != "dir":
                        continue
                    cpath = join_src(path, e.name)

                    def list_inside(e=e):
                        try:
                            return [x.name for x in view.iterdir(e)]
                        except Exception:
                            return []
                    hit = classify_regen(e.name, names, list_inside, lock_here)
                    if hit:
                        self._add(cpath, *hit)
                    elif e.name != ".git":
                        stack.append((e, cpath, lock_here, names))
            self.phase = "Finished"
        finally:
            view.close()

    def _add(self, path, conf, what, regen):
        with self.lock:
            self.found.append({"path": path, "confidence": conf, "what": what, "regen": regen})

    def status(self):
        with self.lock:
            self.stats = {"found": len(self.found), "dirs": self.dirs,
                          "safe": sum(1 for f in self.found if f["confidence"] == "safe")}
            self.progress = 1.0 if self.state == "done" else None
        return super().status()

    def extra_status(self):
        return {"dirs": self.dirs, "found_count": len(self.found), "paths": self.paths}

    def result(self, q):
        with self.lock:
            return {"found": list(self.found), "dirs": self.dirs, "state": self.state}
