"""Fast AES-XTS for dissect.fve (BitLocker, LUKS, FileVault/APFS).

dissect.fve's XtsMode walks every 16-byte block in Python (3 C calls and a tweak update per
block), about 3-4 MB/s - days for a whole drive. XTS is the same work for every block, so this
does a whole run of sectors at once: one AES-ECB call makes every sector's starting tweak, the
tweak for block j of every sector is doubled in GF(2^128) for all sectors together using one big
integer with a 128-bit lane per sector, and the data goes through AES-ECB in a single call. The
result is byte-identical to dissect.fve (tests check that); anything unusual (other IV modes,
odd lengths) goes to the original code.
"""
from __future__ import annotations

_CHUNK = 4 * 1024 * 1024        # bytes handled per pass - bounds memory for the big integers


def _tweak_stream(first_tweaks: bytes, n: int, sector_size: int) -> bytes:
    """XTS tweaks for n whole sectors, sector-major, from each sector's encrypted IV."""
    packed = int.from_bytes(first_tweaks, "little")          # lane i = tweak of sector i, block 0
    ones = int.from_bytes((b"\x01" + bytes(15)) * n, "little")
    low = int.from_bytes((b"\xff" * 15 + b"\x7f") * n, "little")
    width = n * 16
    stream = bytearray(n * sector_size)
    for j in range(sector_size // 16):
        lanes = packed.to_bytes(width, "little")
        base = j * 16
        for k in range(16):                                  # block j of every sector, byte k at a time
            stream[base + k::sector_size] = lanes[k::16]
        top = (packed >> 127) & ones                         # multiply every lane by x in GF(2^128)
        packed = ((packed & low) << 1) ^ (top * 0x87)
    return bytes(stream)


def _sector_ivs(iv_mode, first: int, n: int, step: int) -> bytes | None:
    from dissect.fve.crypto.base import Plain, Plain64
    if iv_mode is Plain64:
        return b"".join(((first + i * step) & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little") + bytes(8) for i in range(n))
    if iv_mode is Plain:
        return b"".join(((first + i * step) & 0xFFFFFFFF).to_bytes(4, "little") + bytes(12) for i in range(n))
    return None


def install():
    """Speed up dissect.fve's XtsMode in place (idempotent)."""
    from dissect.fve.crypto import _pycryptodome
    from dissect.fve.crypto.base import ENCRYPT
    cls = _pycryptodome.XtsMode
    if getattr(cls, "_retrova_fast", False):
        return
    original = cls._crypt

    def _crypt(self, mode, ciphertext, sector=0, output=None):
        size = self.sector_size
        length = len(ciphertext)
        if (type(self.iv_mode) not in _IV_TYPES() or size % 16 or self.iv_sector_size <= 0
                or size % self.iv_sector_size or not length or length % size):
            return original(self, mode, ciphertext, sector, output)
        step = size // self.iv_sector_size
        crypt = self._aes_cipher.encrypt if mode == ENCRYPT else self._aes_cipher.decrypt
        out = output if output is not None else bytearray(length)
        per = max(1, _CHUNK // size)
        view = memoryview(ciphertext)
        done = 0
        while done < length:
            n = min(per, (length - done) // size)
            ivs = _sector_ivs(type(self.iv_mode), sector + (done // size) * step, n, step)
            tweaks = _tweak_stream(self._tweak_cipher.encrypt(ivs), n, size)
            part = view[done:done + n * size]
            t = int.from_bytes(tweaks, "little")
            mixed = (int.from_bytes(part, "little") ^ t).to_bytes(n * size, "little")
            out[done:done + n * size] = (int.from_bytes(crypt(mixed), "little") ^ t).to_bytes(n * size, "little")
            done += n * size
        return None if output is not None else bytes(out)

    cls._retrova_original = original
    cls._crypt = _crypt
    cls._retrova_fast = True


def _IV_TYPES():
    from dissect.fve.crypto.base import Plain, Plain64
    return (Plain, Plain64)
