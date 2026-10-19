"""Opening encrypted volumes read-only with a key the user supplies.

    BitLocker (Windows)   password or 48-digit recovery key, or no key at all when the volume is
                          suspended ("clear key") or already decrypted
    LUKS 1/2 (Linux)      passphrase
    APFS (FileVault)      password or personal recovery key; volumes locked to a Mac's own
                          hardware (T2 / Apple silicon startup disks) cannot be opened elsewhere

What is typed is turned into key material once (that can take a few seconds - it is the
price of the drive's own protection) and then thrown away. Only the derived key, an
`UnlockKey`, is kept: in memory, never written, logged or sent to the UI, and dropped when
the session that unlocked it closes and no job is still using it. A key is handed to
worker processes through their start-up pipe (Windows); nothing touches disk.
"""
from __future__ import annotations

import re
import threading

from .fastxts import install as fast_xts
from .source import ReadOnlySource

LOCKED = ("bitlocker", "luks")          # partition types that wrap another file system
KIND_NAMES = {"bitlocker": "BitLocker", "luks": "LUKS", "apfs": "FileVault"}

HARDWARE_BOUND = ("This volume is encrypted with a key that never leaves its Mac's built-in security chip, so it "
                  "can't be unlocked from anywhere else, not even with the password. Open it on that Mac instead "
                  "(start it normally, or in Recovery, and sign in).")


class UnlockError(Exception):
    """A plain-English reason the volume could not be unlocked (shown to the user as is)."""


class KeyRejected(UnlockError):
    """The password / recovery key / passphrase was wrong."""


class KeyGone(UnlockError):
    """The key was dropped because its session closed."""

    def __init__(self):
        super().__init__("This drive was locked again when you left it. Open it again and enter its key.")


class NeedsKey(UnlockError):
    """The volume is locked and no key was given. Carries what the UI needs to ask for it."""

    def __init__(self, kind: str, label: str = "", hint: str = "", volume: str | None = None,
                 accepts: tuple = ("password",)):
        self.kind, self.label, self.hint, self.volume, self.accepts = kind, label, hint, volume, list(accepts)
        super().__init__(f"This {KIND_NAMES.get(kind, kind)} volume is locked.")

    def public(self) -> dict:
        return {"kind": self.kind, "label": self.label, "hint": self.hint, "volume": self.volume,
                "accepts": self.accepts}


class UnlockKey:
    """Derived key material for one volume. In memory only; see the module notes."""

    def __init__(self, kind: str, **data):
        self.kind = kind
        self._data: dict | None = dict(data)
        self._holds = 1                       # the session that unlocked it
        self._lock = threading.Lock()

    def __repr__(self):
        return f"<UnlockKey {self.kind} (hidden)>"

    __str__ = __repr__

    def __getstate__(self):                   # to a worker process
        return {"kind": self.kind, "data": self._data}

    def __setstate__(self, st):
        self.kind, self._data, self._holds, self._lock = st["kind"], st["data"], 1, threading.Lock()

    @property
    def alive(self) -> bool:
        return self._data is not None

    def get(self, name):
        data = self._data
        if data is None:
            raise KeyGone()
        return data.get(name)

    def hold(self):
        with self._lock:
            if self._data is None:
                raise KeyGone()
            self._holds += 1

    def release(self):
        with self._lock:
            self._holds -= 1
            if self._holds <= 0:
                self._data = None


def hold(spec: dict) -> None:
    key = spec.get("unlock")
    if key is not None:
        key.hold()


def release(spec: dict) -> None:
    key = spec.get("unlock")
    if key is not None:
        key.release()


def ensure_alive(spec: dict) -> None:
    key = spec.get("unlock")
    if key is not None and not key.alive:
        raise KeyGone()


# ---------------------------------------------------------------- the decrypted view
class DecryptedSource:
    """Same reading interface as ReadOnlySource (pread / seek / read / size), over the decrypted bytes
    of a BitLocker or LUKS volume. Offsets start at the first byte of the inner file system."""

    def __init__(self, raw: ReadOnlySource, stream):
        self.raw, self.stream = raw, stream
        self.path, self.base, self.length = raw.path, 0, None
        self.size = int(stream.size)
        self.pos = 0
        self.lock = threading.Lock()
        self.closed = False

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return True

    def seek(self, pos, whence=0):
        self.pos = pos if whence == 0 else self.pos + pos if whence == 1 else self.size + pos
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = max(0, self.size - self.pos)
        data = self.pread(self.pos, n)
        self.pos += len(data)
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def pread(self, offset: int, length: int) -> bytes:
        length = min(length, self.size - offset)
        if length <= 0 or offset < 0:
            return b""
        with self.lock:
            self.stream.seek(offset)
            return self.stream.read(length)

    def close(self):
        if not self.closed:
            self.closed = True
            self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _raw(spec: dict) -> ReadOnlySource:
    return ReadOnlySource(spec["source"], int(spec.get("offset") or 0), spec.get("size") or None)


def open_decrypted(spec: dict, kind: str) -> DecryptedSource:
    """The decrypted contents of the BitLocker / LUKS volume at spec["source"] + spec["offset"], opened with
    spec["unlock"]. Volumes that need no key (decrypted or suspended BitLocker) open without one."""
    key = spec.get("unlock")
    raw = _raw(spec)
    try:
        stream = (_bitlocker_stream if kind == "bitlocker" else _luks_stream)(raw, key)
        return DecryptedSource(raw, stream)
    except BaseException:
        raw.close()
        raise


# ---------------------------------------------------------------- BitLocker
def _bde(raw):
    from dissect.fve.bde import BDE
    try:
        return BDE(raw)
    except Exception as ex:
        raise UnlockError(f"This BitLocker volume's headers can't be read ({type(ex).__name__}: {ex}).") from ex


def _bitlocker_stream(raw, key):
    fast_xts()
    b = _bde(raw)
    if key is not None:
        fvek = key.get("fvek")
        if fvek:
            b.unlock_with_fvek(fvek)
    elif not b.unlocked:
        if not b.has_clear_key():
            raise NeedsKey("bitlocker", b.description() or "", _bitlocker_hint(b), accepts=_bitlocker_accepts(b))
        b.unlock_with_clear_key()
    return b.open()


def _bitlocker_accepts(b) -> tuple:
    out = []
    if b.has_passphrase():
        out.append("password")
    if b.has_recovery_password():
        out.append("recovery")
    return tuple(out)


def _bitlocker_hint(b) -> str:
    acc = _bitlocker_accepts(b)
    if acc == ("password", "recovery"):
        return "Enter the drive's password, or its 48-digit recovery key."
    if acc == ("password",):
        return "Enter the drive's password."
    if acc == ("recovery",):
        return "Enter the drive's 48-digit recovery key."
    return ("This drive is protected only by a TPM chip or a startup key file, which Retrova can't use, and it has "
            "no recovery key stored on it.")


def _unlock_bitlocker(raw, secret: str) -> UnlockKey:
    b = _bde(raw)
    if b.unlocked or b.has_clear_key():
        raise UnlockError("This drive doesn't need a key.")
    digits = re.sub(r"[\s-]", "", secret)
    try:
        if digits.isdigit() and len(digits) >= 40:             # looks like a recovery key, not a password
            if len(digits) != 48:
                raise KeyRejected("A BitLocker recovery key has 48 digits, in 8 groups of 6. Check that you copied "
                                  "all of it.")
            if not b.has_recovery_password():
                raise KeyRejected("This drive has no recovery key stored on it. Try its password instead.")
            b.unlock_with_recovery_password("-".join(digits[i:i + 6] for i in range(0, 48, 6)))
        elif not b.has_passphrase():
            raise KeyRejected("This drive has no password, only a recovery key (48 digits). Enter that instead.")
        else:
            b.unlock_with_passphrase(secret)
    except ValueError as ex:
        if str(ex).startswith("Invalid recovery password"):
            raise KeyRejected("That doesn't look like a valid recovery key - one of its groups of six digits is "
                              "wrong. Check each group against the original.") from None
        raise KeyRejected("That password or recovery key didn't unlock the drive. Check it and try again.") from None
    return UnlockKey("bitlocker", fvek=bytes(b._fvek))


# ---------------------------------------------------------------- LUKS
def _luks(raw):
    from dissect.fve.luks import LUKS
    try:
        return LUKS(raw)
    except Exception as ex:
        raise UnlockError(f"This encrypted Linux volume's headers can't be read ({type(ex).__name__}: {ex}).") from ex


def _luks_stream(raw, key):
    fast_xts()
    luks = _luks(raw)
    if key is None:
        raise NeedsKey("luks", str(luks.label or ""), "Enter the passphrase for this encrypted Linux drive.")
    try:
        luks.unlock(key.get("volume_key"), key.get("keyslot"))
    except ValueError as ex:
        raise UnlockError("The saved key doesn't match this drive any more.") from ex
    return luks.open()


def _unlock_luks(raw, secret: str) -> UnlockKey:
    luks = _luks(raw)
    try:
        luks.unlock_with_passphrase(secret)
    except ValueError as ex:
        if "out of memory" in str(ex).lower():
            raise UnlockError("This drive's passphrase check needs more free memory than is available right now. "
                              "Close other apps and try again.") from None
        raise KeyRejected("That passphrase didn't unlock the drive. Check it and try again.") from None
    except NotImplementedError as ex:
        raise UnlockError(f"This drive uses a LUKS setting Retrova doesn't support yet ({ex}).") from None
    return UnlockKey("luks", volume_key=bytes(luks._active_volume_key), keyslot=luks._active_keyslot_id)


# ---------------------------------------------------------------- APFS (FileVault)
def apfs_check(volume, key: UnlockKey | None) -> None:
    """Unlock an encrypted dissect.apfs volume with a derived key, or say why it can't be."""
    uuid = bytes(volume.object.apfs_vol_uuid)
    if key is not None and key.get("uuid") == uuid:
        _apfs_set_cipher(volume, key.get("vek"))
        return
    if not volume.is_onekey:
        raise UnlockError(f"The volume “{volume.name}” uses a kind of encryption Retrova can't read (a separate key "
                          "for every file).")
    if not _apfs_password_records(volume):
        raise UnlockError(HARDWARE_BOUND)
    hint = ""
    try:
        hint = volume.password_hint or ""
    except Exception:
        pass
    raise NeedsKey("apfs", volume.name, ("Password hint: " + hint) if hint else "", volume.name,
                   accepts=("password", "recovery"))


def _apfs_password_records(volume) -> list:
    try:
        return [k for k in volume.keybag.keks() if k.verify()]
    except Exception:
        return []


def _apfs_set_cipher(volume, vek: bytes) -> None:
    from dissect.fve.crypto import create_cipher
    fast_xts()
    volume._cipher = create_cipher("aes-xts-128", vek)


def _unlock_apfs(volume, secret: str) -> UnlockKey:
    uuid = bytes(volume.object.apfs_vol_uuid)
    vek_blob = volume.container.keybag.vek(uuid) if volume.container.keybag else None
    kek_records = _apfs_password_records(volume)
    if vek_blob is None or not kek_records:
        raise UnlockError(HARDWARE_BOUND)
    for kek in kek_records:
        try:
            vek = bytes(vek_blob.unwrap(kek.unwrap(secret)))
        except Exception:
            continue
        return UnlockKey("apfs", uuid=uuid, vek=vek)
    raise KeyRejected("That password didn't unlock the volume. Check it and try again. (A FileVault recovery key "
                      "works too.)")


# ---------------------------------------------------------------- entry point for the API
def make_key(spec: dict, kind: str, secret: str) -> UnlockKey:
    """Check `secret` against the volume described by spec and return the derived key. Never stores the secret."""
    if not secret:
        raise KeyRejected("Type the key first.")
    if kind in LOCKED:
        raw = _raw(spec)
        try:
            return (_unlock_bitlocker if kind == "bitlocker" else _unlock_luks)(raw, secret)
        finally:
            raw.close()
    from .apfs_engine import ApfsContainer, pick_volume
    raw = _raw(spec)
    try:
        cont = ApfsContainer(raw, 256)
        try:
            vol = pick_volume(cont.volumes, spec.get("volume"))
        except LookupError as ex:
            raise UnlockError(str(ex)) from ex
        return _unlock_apfs(vol, secret)
    finally:
        raw.close()
