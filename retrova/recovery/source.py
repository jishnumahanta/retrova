"""Read-only access to raw devices and disk-image files.

ReadOnlySource is the only way recovery code reads a source. It opens the path
O_RDONLY (Windows: 'rb', unbuffered), never writes, and turns every read into a
sector-aligned positional read so raw devices on macOS/Windows accept it.
"""
from __future__ import annotations

import io
import os
import stat
import sys
import threading

from .common import ALIGN, FAKE_WIN, IS_WIN


def split_src(src: str) -> tuple[str, int]:
    """Legacy 'path@offset' -> (path, offset)."""
    if "@" in src:
        path, off = src.rsplit("@", 1)
        if off.isdigit():
            return path, int(off)
    return src, 0


def open_raw(path: str):
    """Open a device or image strictly read-only."""
    if IS_WIN or FAKE_WIN:
        return io.open(path, "rb", buffering=0)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    return os.open(path, flags)


class ReadOnlySource:
    """Read-only, alignment-safe, thread-safe reader for a raw device or image.

    `base` shifts every offset (a partition inside a whole disk); `length` (optional)
    makes reads past the end of that region come back short, like EOF. `align` is the unit
    reads are rounded out to (default ALIGN); imaging a failing drive passes the device's
    logical sector size so one bad sector doesn't take its neighbours with it."""

    def __init__(self, path: str, base: int = 0, length: int | None = None, align: int = ALIGN):
        p, off = split_src(path)
        self.path = p
        self.base = int(base) + off
        self.length = None if length is None else int(length)
        self.align = max(1, int(align))
        self.h = open_raw(self.path)
        self.hlock = threading.Lock()
        self.pos = 0
        self.closed = False

    # file-like protocol (dissect libraries seek + read)
    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return True

    @property
    def size(self):
        return self.length if self.length is not None else max(0, device_size(self.path) - self.base)

    def seek(self, pos, whence=0):
        if whence == 0:
            self.pos = pos
        elif whence == 1:
            self.pos += pos
        elif whence == 2:
            self.pos = self.size + pos
        else:
            raise OSError("invalid whence")
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            if self.length is None:
                raise OSError("unbounded read not supported")
            n = max(0, self.length - self.pos)
        data = self.pread(self.pos, n)
        self.pos += len(data)
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def _raw(self, start: int, want: int) -> bytes:
        parts, got = [], 0
        if isinstance(self.h, int):  # macOS / Linux: positional reads, no shared offset
            while got < want:
                chunk = os.pread(self.h, want - got, start + got)
                if not chunk:
                    break
                parts.append(chunk)
                got += len(chunk)
        else:  # Windows: seek + read under a lock, in sector-aligned pieces
            with self.hlock:
                self.h.seek(start)
                while got < want:
                    chunk = self.h.read(min(want - got, 4 << 20))
                    if not chunk:
                        break
                    parts.append(chunk)
                    got += len(chunk)
        return b"".join(parts)

    def pread(self, offset: int, length: int) -> bytes:
        """Read `length` bytes at `offset` (relative to base). Short at the end."""
        if length <= 0 or offset < 0:
            return b""
        if self.length is not None:
            length = min(length, self.length - offset)
            if length <= 0:
                return b""
        absoff = self.base + offset
        start = absoff - (absoff % self.align)
        end = absoff + length
        end_al = end + (-end % self.align)
        try:
            buf = self._raw(start, end_al - start)
        except OSError:
            # Windows raw devices reject reads that run past the end; retry exactly to the end
            if not isinstance(self.h, int) and end_al != end:
                unit = 512 if self.align == ALIGN else self.align
                buf = self._raw(start, -(-(end - start) // unit) * unit)
            else:
                raise
        skip = absoff - start
        return buf[skip:skip + length]

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            os.close(self.h) if isinstance(self.h, int) else self.h.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def device_size(path: str) -> int:
    """Size in bytes of an image file or a raw device (0 if unknown)."""
    path = split_src(path)[0]
    try:
        st = os.stat(path)
    except OSError:
        st = None
    if st is not None and stat.S_ISREG(st.st_mode):
        return st.st_size
    if IS_WIN:
        return _win_device_size(path)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return 0
    try:
        if sys.platform == "darwin":
            import fcntl
            import struct
            try:
                bs = struct.unpack("I", fcntl.ioctl(fd, 0x40046418, b"\0" * 4))[0]      # DKIOCGETBLOCKSIZE
                cnt = struct.unpack("Q", fcntl.ioctl(fd, 0x40086419, b"\0" * 8))[0]     # DKIOCGETBLOCKCOUNT
                return bs * cnt
            except OSError:
                pass
        try:
            return os.lseek(fd, 0, os.SEEK_END)
        except OSError:
            return 0
    finally:
        os.close(fd)


def logical_sector_size(path: str) -> int:
    """Logical sector size of a device (what a raw read must be a multiple of); 512 for image files or if unknown."""
    path = split_src(path)[0]
    try:
        if stat.S_ISREG(os.stat(path).st_mode):
            return 512
    except OSError:
        pass
    size = _win_sector_size(path) if IS_WIN else _unix_sector_size(path)
    return size if size in (512, 1024, 2048, 4096) else 512


def _unix_sector_size(path: str) -> int:
    try:
        import fcntl
        import struct
        fd = os.open(path, os.O_RDONLY)
    except (OSError, ImportError):
        return 512
    try:
        if sys.platform == "darwin":
            return struct.unpack("I", fcntl.ioctl(fd, 0x40046418, b"\0" * 4))[0]     # DKIOCGETBLOCKSIZE
        return struct.unpack("I", fcntl.ioctl(fd, 0x1268, b"\0" * 4))[0]             # BLKSSZGET
    except OSError:
        return 512
    finally:
        os.close(fd)


def _win_sector_size(path: str) -> int:
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        h = k32.CreateFileW(path, 0x80000000, 3, None, 3, 0, None)
        if h in (None, wintypes.HANDLE(-1).value):
            return 512
        try:
            buf = ctypes.create_string_buffer(64)
            ret = wintypes.DWORD(0)
            ok = k32.DeviceIoControl(h, 0x70000, None, 0, buf, 64, ctypes.byref(ret), None)  # IOCTL_DISK_GET_DRIVE_GEOMETRY
            return int.from_bytes(buf.raw[20:24], "little") if ok else 512                   # DISK_GEOMETRY.BytesPerSector
        finally:
            k32.CloseHandle(h)
    except Exception:
        return 512


def _win_device_size(path: str) -> int:
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        h = k32.CreateFileW(path, 0x80000000, 3, None, 3, 0, None)  # GENERIC_READ, share r/w, OPEN_EXISTING
        if h in (None, wintypes.HANDLE(-1).value):
            return 0
        try:
            out = ctypes.c_longlong(0)
            ret = wintypes.DWORD(0)
            ok = k32.DeviceIoControl(h, 0x7405C, None, 0, ctypes.byref(out), 8, ctypes.byref(ret), None)
            return int(out.value) if ok else 0                    # IOCTL_DISK_GET_LENGTH_INFO
        finally:
            k32.CloseHandle(h)
    except Exception:
        return 0


def can_open(path: str) -> tuple[bool, str]:
    """(True, '') if we may read the path; otherwise (False, reason)."""
    try:
        s = ReadOnlySource(path)
    except PermissionError:
        return False, "admin"
    except FileNotFoundError:
        return False, "missing"
    except OSError as ex:
        return False, str(ex)
    s.close()
    return True, ""
