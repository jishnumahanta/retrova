"""The folder tree that tests/hfs_images/*.gz were built from (see make_hfs_fixtures.py).

Paths use the composed (NFC) names Retrova shows; the Mac stores them decomposed."""
import struct

BIG_BLOCKS = 2304                                      # 9 MiB + a few bytes: more than one 8 MiB copy chunk


def big_bytes() -> bytes:
    """Every 4096-byte block repeats its own number, so a misplaced block can't go unnoticed."""
    return b"".join(struct.pack(">I", n) * 1024 for n in range(BIG_BLOCKS)) + b"tail-123"


MANY = 1500                                            # enough names to make the catalog three levels deep

FILES = {
    "hello.txt": b"hello world\n",
    "zero.txt": b"",
    "docs/Readme.TXT": b"case test\n",
    "docs/deep/er/big.bin": big_bytes(),
    "café/ü-naïve.txt": b"unicode body\n",
    "café/😀 smile.txt": b"emoji body\n",
    "col:on.txt": b"colon in the name\n",
    "withrsrc.txt": b"data fork\n",
    "emptydir/": b"",
}
FILES.update({f"many/f{i:04d}.txt": b"file %d\n" % i for i in range(MANY)})
RESOURCE_FORKS = {"withrsrc.txt": b"RESOURCE-FORK-DATA"}
SYMLINKS = {"link.txt": "hello.txt", "deeplink": "docs/deep"}
HARDLINKS = {"hard.txt": "hello.txt"}                  # same inode as the target
MTIME = 1_600_000_000                                  # hello.txt
VOLNAME = "T"
