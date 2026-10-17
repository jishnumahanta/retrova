"""Real (or faithfully hand-built) sample files for the carving tests, plus a builder
for a synthetic "disk" with the files at odd offsets between random noise and zeros."""
from __future__ import annotations

import io
import os
import random
import sqlite3
import struct
import tempfile
import wave
import zipfile
import zlib


def _box(t: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + t + payload


def _rnd(seed, n):
    return random.Random(seed).randbytes(n)


def jpeg(seed=1, size=(320, 200), progressive=False):
    from PIL import Image
    im = Image.frombytes("RGB", size, _rnd(seed, size[0] * size[1] * 3))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85, progressive=progressive)
    return b.getvalue()


def pil(fmt, seed=2, size=(120, 90), mode="RGB", **kw):
    from PIL import Image
    im = Image.frombytes(mode, size, _rnd(seed, size[0] * size[1] * len(mode)))
    b = io.BytesIO()
    im.save(b, fmt, **kw)
    return b.getvalue()


def pdf_incremental():
    """A PDF with an incremental update (two %%EOF markers)."""
    base = pil("PDF", seed=9)
    upd = (b"\n9 0 obj\n<< /Producer (Retrova test) >>\nendobj\nxref\n0 1\n0000000000 65535 f \n"
           b"9 1\n0000000000 00000 n \ntrailer\n<< /Size 10 /Info 9 0 R >>\nstartxref\n%d\n%%%%EOF\n" % len(base))
    return base + upd


def zip_plain():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("readme.txt", "hello " * 200, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("raw.bin", _rnd(3, 5000), compress_type=zipfile.ZIP_STORED)
        z.writestr("dir/notes.txt", "notes\n" * 50, compress_type=zipfile.ZIP_DEFLATED)
        z.comment = b"made by the retrova tests"
    return b.getvalue()


class _NoSeek(io.RawIOBase):
    def __init__(self):
        self.buf = bytearray()

    def writable(self):
        return True

    def write(self, b):
        self.buf += b
        return len(b)


def zip_streamed():
    """Written to a non-seekable stream: entries use data descriptors (flag bit 3)."""
    raw = _NoSeek()
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as z:
        with z.open("a.txt", "w") as fh:
            fh.write(b"streamed entry " * 300)
        with z.open("b.bin", "w") as fh:
            fh.write(_rnd(4, 20000))
    return bytes(raw.buf)


def office(kind):
    folder = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}[kind]
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="x"/>')
        z.writestr("_rels/.rels", "<Relationships/>")
        z.writestr(folder, "<doc>" + "text " * 500 + "</doc>")
        z.writestr("docProps/core.xml", "<core/>")
    return b.getvalue()


def epub():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", "<container/>", compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/ch1.xhtml", "<html>" + "chapter " * 300 + "</html>", compress_type=zipfile.ZIP_DEFLATED)
    return b.getvalue()


def mp4(seed=5, brand=b"isom", mdat_size=300_000, order="moov-last"):
    ftyp = _box(b"ftyp", brand + b"\0\0\x02\0" + brand + b"iso2avc1mp41")
    mvhd = _box(b"mvhd", b"\0" * 4 + struct.pack(">IIII", 0, 0, 1000, 5000) + b"\0" * 80)
    trak = _box(b"trak", _box(b"tkhd", b"\0" * 84))
    moov = _box(b"moov", mvhd + trak)
    mdat = _box(b"mdat", _rnd(seed, mdat_size))
    if brand == b"qt  ":
        return ftyp + _box(b"wide", b"") + mdat + moov
    return ftyp + (moov + mdat if order == "moov-first" else _box(b"free", b"\0" * 16) + mdat + moov)


def heic(seed=6):
    ftyp = _box(b"ftyp", b"heic\0\0\0\0mif1heic")
    meta = _box(b"meta", b"\0\0\0\0" + _box(b"hdlr", b"\0" * 8 + b"pict" + b"\0" * 13) + _box(b"iinf", b"\0" * 6))
    return ftyp + meta + _box(b"mdat", _rnd(seed, 80_000))


def mp3(frames=60, seed=7):
    tag_body = b"TIT2" + struct.pack(">I", 6) + b"\0\0" + b"\x00Test\0"
    pad = b"\0" * 64
    body = tag_body + pad
    n = len(body)
    ss = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    out = b"ID3\x03\x00\x00" + ss + body
    r = random.Random(seed)
    for _ in range(frames):          # MPEG-1 Layer III, 128 kbit/s, 44.1 kHz, no padding: 417 bytes
        out += b"\xff\xfb\x90\x64" + r.randbytes(413)
    return out + b"TAG" + b"Retrova test".ljust(125, b"\0")


def wav(seed=8):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(_rnd(seed, 40_001 * 2))
    return b.getvalue()


def avi(seed=9):
    hdrl = b"LIST" + struct.pack("<I", 4 + 8 + 56) + b"hdrl" + b"avih" + struct.pack("<I", 56) + b"\0" * 56
    movi_data = b"00dc" + struct.pack("<I", 3001) + _rnd(seed, 3001) + b"\0"
    movi = b"LIST" + struct.pack("<I", 4 + len(movi_data)) + b"movi" + movi_data
    body = b"AVI " + hdrl + movi
    return b"RIFF" + struct.pack("<I", len(body)) + body


def sqlite_db():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.sqlite")
    con = sqlite3.connect(p)
    con.execute("create table t (a integer, b text)")
    con.executemany("insert into t values (?, ?)", [(i, "row %d " % i * 20) for i in range(800)])
    con.commit()
    con.close()
    with open(p, "rb") as fh:
        data = fh.read()
    os.remove(p)
    os.rmdir(d)
    return data


def sevenzip(seed=10):
    nh = _rnd(seed, 300)
    packed = _rnd(seed + 1, 5000)
    start = struct.pack("<QQI", len(packed), len(nh), zlib.crc32(nh) & 0xFFFFFFFF)
    return b"7z\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", zlib.crc32(start) & 0xFFFFFFFF) + start + packed + nh


def rar4(seed=11):
    def block(typ, flags, extra=b"", add=b""):
        body = bytes([typ]) + struct.pack("<HH", flags | (0x8000 if add else 0), 7 + len(extra) +
                                          (4 if add else 0)) + (struct.pack("<I", len(add)) if add else b"") + extra
        return struct.pack("<H", zlib.crc32(body) & 0xFFFF) + body + add
    main = block(0x73, 0, b"\0" * 6)
    name = b"file.txt"
    fhdr = struct.pack("<IIBIIBBHI", 3000, 3000, 2, 0, 0, 29, 0x30, len(name), 0x20) + name
    f = block(0x74, 0, fhdr, add=_rnd(seed, 3000))
    end = block(0x7B, 0x4000)
    return b"Rar!\x1a\x07\x00" + main + f + end


def ogg_opus(seed=12):
    from retrova.recovery.carve_formats import _ogg_crc

    def page(htype, seq, packet):
        segs = []
        n = len(packet)
        while n >= 255:
            segs.append(255)
            n -= 255
        segs.append(n)
        hdr = b"OggS\0" + bytes([htype]) + struct.pack("<qIII", seq * 960, 0x1234, seq, 0) + bytes([len(segs)]) + \
            bytes(segs)
        p = bytearray(hdr + packet)
        p[22:26] = struct.pack("<I", _ogg_crc(bytes(p)))
        return bytes(p)
    r = random.Random(seed)
    out = page(2, 0, b"OpusHead\x01\x02\x38\x01\x80\xbb\0\0\0\0\0")
    out += page(0, 1, b"OpusTags" + b"\0" * 12)
    for i in range(2, 12):
        out += page(4 if i == 11 else 0, i, r.randbytes(400))
    return out


def webm(seed=13):
    def el(eid: bytes, payload: bytes):
        n = len(payload)
        return eid + bytes([0x01]) + n.to_bytes(7, "big") + payload
    hdr = el(b"\x1a\x45\xdf\xa3", b"\x42\x82\x84webm" + b"\x42\x87\x81\x04")
    seg = el(b"\x18\x53\x80\x67", el(b"\x15\x49\xa9\x66", b"\0" * 20) + el(b"\x1f\x43\xb6\x75", _rnd(seed, 9000)))
    return hdr + seg


def psd(seed=14, w=40, h=30):
    hdr = b"8BPS" + struct.pack(">H", 1) + b"\0" * 6 + struct.pack(">HIIHH", 3, h, w, 8, 3)
    return hdr + struct.pack(">I", 0) * 3 + struct.pack(">H", 0) + _rnd(seed, w * h * 3)


def all_samples() -> dict:
    """name -> (expected type, bytes)."""
    s = {
        "photo.jpg": ("jpg", jpeg(1)),
        "progressive.jpg": ("jpg", jpeg(21, (200, 150), progressive=True)),
        "image.png": ("png", pil("PNG", 2)),
        "anim.gif": ("gif", pil("GIF", 3, mode="L")),
        "pic.bmp": ("bmp", pil("BMP", 4)),
        "scan.tif": ("tif", pil("TIFF", 5)),
        "doc.pdf": ("pdf", pil("PDF", 6)),
        "updated.pdf": ("pdf", pdf_incremental()),
        "archive.zip": ("zip", zip_plain()),
        "streamed.zip": ("zip", zip_streamed()),
        "letter.docx": ("docx", office("docx")),
        "sheet.xlsx": ("xlsx", office("xlsx")),
        "slides.pptx": ("pptx", office("pptx")),
        "book.epub": ("epub", epub()),
        "clip.mp4": ("mp4", mp4(5)),
        "movie.mov": ("mov", mp4(15, brand=b"qt  ")),
        "song.m4a": ("m4a", mp4(16, brand=b"M4A ", mdat_size=50_000, order="moov-first")),
        "iphone.heic": ("heic", heic()),
        "song.mp3": ("mp3", mp3()),
        "voice.wav": ("wav", wav()),
        "film.avi": ("avi", avi()),
        "data.sqlite": ("sqlite", sqlite_db()),
        "pack.7z": ("7z", sevenzip()),
        "old.rar": ("rar", rar4()),
        "voice.opus": ("opus", ogg_opus()),
        "video.webm": ("webm", webm()),
        "art.psd": ("psd", psd()),
    }
    try:
        s["photo.webp"] = ("webp", pil("WEBP", 17))
    except Exception:
        pass
    return s


def build_disk(path, samples: dict, seed=42):
    """Write noise / zeros / files at odd offsets. Returns [(name, type, offset, bytes)]."""
    r = random.Random(seed)
    placed = []
    with open(path, "wb") as fh:
        fh.write(r.randbytes(777_777))                       # noise first
        for i, (name, (typ, data)) in enumerate(sorted(samples.items())):
            if i % 3 == 0:
                fh.write(b"\0" * (r.randrange(1, 6) * 1024 * 1024 + r.randrange(1, 4096)))   # zero area
            else:
                fh.write(r.randbytes(r.randrange(10_000, 400_000) + 1))                     # noise
            off = fh.tell()
            fh.write(data)
            placed.append((name, typ, off, data))
        fh.write(r.randbytes(123_457))
        fh.write(b"\0" * (17 * 1024 * 1024 + 3))               # a long zero tail spanning chunks
    return placed
