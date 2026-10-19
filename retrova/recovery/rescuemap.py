"""Rescue map: which parts of a failing drive are copied, and what is left to try.

The file format is GNU ddrescue's mapfile, so a user can carry on with `ddrescue`
using the `.map` Retrova leaves next to the image. Positions are relative to the
start of the imaged region, which is also where the image file starts.

  ?  not tried yet          *  tried in a big block, failed (not trimmed)
  /  trimmed, not scraped   -  bad: unreadable even sector by sector
  +  finished (copied)
"""
from __future__ import annotations

import time
from bisect import bisect_left, bisect_right

NON_TRIED, NON_TRIMMED, NON_SCRAPED, BAD, FINISHED = "?", "*", "/", "-", "+"
STATUSES = (NON_TRIED, NON_TRIMMED, NON_SCRAPED, BAD, FINISHED)


class RescueMap:
    def __init__(self, total: int):
        self.total = int(total)
        self.starts = [0]
        self.blocks = [[0, self.total, NON_TRIED]]
        self.sums = {s: 0 for s in STATUSES}
        self.sums[NON_TRIED] = self.total

    # ---------------- editing
    def set(self, start: int, size: int, status: str):
        """Give [start, start+size) a status, merging with equal neighbours."""
        if size <= 0:
            return
        end = start + size
        i = bisect_right(self.starts, start) - 1
        j = bisect_left(self.starts, end)
        for s, z, t in self.blocks[i:j]:
            self.sums[t] -= min(end, s + z) - max(start, s)
        self.sums[status] += size
        head, tail = self.blocks[i], self.blocks[j - 1]
        new = []
        if head[0] < start:
            new.append([head[0], start - head[0], head[2]])
        new.append([start, size, status])
        if tail[0] + tail[1] > end:
            new.append([end, tail[0] + tail[1] - end, tail[2]])
        lo = max(i - 1, 0)
        hi = min(j + 1, len(self.blocks))
        merged: list = []
        for b in self.blocks[lo:i] + new + self.blocks[j:hi]:
            if merged and merged[-1][2] == b[2]:
                merged[-1][1] += b[1]
            else:
                merged.append(list(b))
        self.blocks[lo:hi] = merged
        self.starts[lo:hi] = [b[0] for b in merged]

    def find(self, status: str, pos: int = 0, reverse: bool = False):
        """First (start, size) with this status at or after pos; reverse: the last one ending at or before pos."""
        if not reverse:
            for i in range(max(bisect_right(self.starts, pos) - 1, 0), len(self.blocks)):
                s, z, t = self.blocks[i]
                if t == status and s + z > pos:
                    return max(s, pos), s + z - max(s, pos)
            return None
        for i in range(min(bisect_right(self.starts, pos), len(self.blocks)) - 1, -1, -1):
            s, z, t = self.blocks[i]
            if t == status and s < pos:
                return s, min(s + z, pos) - s
        return None

    # ---------------- questions
    def bytes_of(self, *statuses: str) -> int:
        return sum(self.sums[s] for s in statuses)

    def areas(self, *statuses: str):
        return [(s, z, t) for s, z, t in self.blocks if t in statuses]

    # ---------------- ddrescue mapfile
    def to_text(self, pos: int, status: str, pass_no: int, started: float, comment: str) -> str:
        fmt = "%a %b %d %H:%M:%S %Y"
        out = [f"# Mapfile. Created by {comment}",
               f"# Start time:   {time.strftime(fmt, time.localtime(started))}",
               f"# Current time: {time.strftime(fmt)}",
               "# current_pos  current_status  current_pass",
               f"0x{pos:08X}     {status}               {pass_no}",
               "#      pos        size  status"]
        out += [f"0x{s:08X}  0x{z:08X}  {t}" for s, z, t in self.blocks]
        return "\n".join(out) + "\n"

    @classmethod
    def from_text(cls, text: str, total: int):
        """Parse a mapfile -> (map, current_pos, current_status, current_pass). ValueError if it isn't ours to resume."""
        rows = [ln.split() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        if len(rows) < 2 or len(rows[0]) < 2:
            raise ValueError("not a mapfile")
        pos, status = int(rows[0][0], 0), rows[0][1]
        pass_no = int(rows[0][2]) if len(rows[0]) > 2 else 1
        m = cls(total)
        want = 0
        for r in rows[1:]:
            s, z, t = int(r[0], 0), int(r[1], 0), r[2]
            if s != want or z <= 0 or t not in STATUSES or s + z > total:
                raise ValueError("mapfile has gaps, unknown statuses or is for a bigger drive")
            m.set(s, z, t)
            want = s + z
        if want != total:
            raise ValueError("mapfile is for a different size")
        return m, pos, status, pass_no
