"""The fast AES-XTS path must give exactly the bytes dissect.fve's own XTS code gives."""
import os
import random

import pytest

pytest.importorskip("dissect.fve")
from dissect.fve.crypto import _pycryptodome, create_cipher  # noqa: E402
from dissect.fve.crypto.base import DECRYPT, ENCRYPT  # noqa: E402

from retrova.recovery import fastxts  # noqa: E402

ORIGINAL = getattr(_pycryptodome.XtsMode, "_crypt")


@pytest.mark.parametrize("keylen", [32, 64])
@pytest.mark.parametrize("spec", ["aes-xts-plain64", "aes-xts-plain"])
@pytest.mark.parametrize("sector_size,iv_sector_size", [(512, 512), (4096, 4096), (4096, 512), (1024, 512)])
def test_fast_xts_matches_dissect(keylen, spec, sector_size, iv_sector_size):
    rnd = random.Random(keylen * 31 + sector_size)
    cipher = create_cipher(spec, rnd.randbytes(keylen), sector_size=sector_size, iv_sector_size=iv_sector_size)
    original = getattr(type(cipher), "_retrova_original", None) or ORIGINAL
    cases = []
    for sector in (0, 1, 7, 2 ** 31 + 5, 2 ** 40 + 3, 0xFFFFFFFF - 2):
        data = rnd.randbytes(sector_size * rnd.choice([1, 2, 3, 17]))
        cases.append((data, sector, original(cipher, DECRYPT, data, sector), original(cipher, ENCRYPT, data, sector)))
    fastxts.install()
    for data, sector, dec, enc in cases:
        assert cipher.decrypt(data, sector) == dec
        assert cipher.encrypt(data, sector) == enc
        buf = bytearray(len(data))
        assert cipher.decrypt(data, sector, output=buf) is None and bytes(buf) == dec


def test_unaligned_input_falls_back_to_dissect():
    fastxts.install()
    cipher = create_cipher("aes-xts-plain64", os.urandom(32))
    with pytest.raises(ValueError):
        cipher.decrypt(b"x" * 100, 0)
