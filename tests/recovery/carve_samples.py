"""Real (or faithfully hand-built) sample files for the carving tests, plus a builder
for a synthetic "disk" with the files at odd offsets between random noise and zeros."""
from __future__ import annotations

import base64
import io
import os
import random
import sqlite3
import struct
import tempfile
import uuid
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


# ---------------------------------------------------------------- camera RAW and other image formats
def tiff_raw(make, model, order="<", magic=None, cr2=False, dng=False, seed=30, strip=6000):
    """A TIFF-based RAW: header, one IFD (size, make, model, strip offsets) and the raw strip at the end."""
    e = order
    magic = magic or (b"II*\0" if e == "<" else b"MM\0*")
    hdr_len = 16 if cr2 else 8
    make_b, model_b = make + b"\0", model + b"\0"
    tags = [(256, 3, 1, 4000), (257, 3, 1, 3000), (271, 2, len(make_b), None), (272, 2, len(model_b), None),
            (273, 4, 1, None), (279, 4, 1, strip)]
    if dng:
        tags.append((50706, 1, 4, b"\x01\x04\x00\x00"))
    ifd_len = 2 + 12 * len(tags) + 4
    vals = hdr_len + ifd_len
    strings = make_b + model_b
    strip_off = vals + len(strings) + (len(strings) & 1)
    data = {271: vals, 272: vals + len(make_b), 273: strip_off}
    ifd = struct.pack(e + "H", len(tags))
    for tag, typ, num, val in tags:
        if isinstance(val, bytes):
            ifd += struct.pack(e + "HHI", tag, typ, num) + val
        elif typ == 3:
            ifd += struct.pack(e + "HHIHxx", tag, typ, num, val)
        else:
            ifd += struct.pack(e + "HHII", tag, typ, num, data.get(tag, val))
    ifd += struct.pack(e + "I", 0)
    head = magic + struct.pack(e + "I", hdr_len)
    if cr2:
        head += b"CR\x02\x00" + struct.pack(e + "I", 0)
    return head + ifd + strings + b"\0" * (len(strings) & 1) + _rnd(seed, strip)


def orf(seed=31):
    return tiff_raw(b"OLYMPUS CORPORATION", b"E-M5", magic=b"IIRO", seed=seed)


def raf(seed=32):
    jpg = jpeg(seed, (64, 48))
    cfa_hdr, cfa = _rnd(seed + 1, 1200), _rnd(seed + 2, 9000)
    jpg_off = 0x94
    hdr_off = jpg_off + len(jpg)
    cfa_off = hdr_off + len(cfa_hdr)
    head = b"FUJIFILMCCD-RAW 0201FF129502" + b"X-T3".ljust(32, b"\0") + b"0100" + b"\0" * 20
    head += struct.pack(">6I", jpg_off, len(jpg), hdr_off, len(cfa_hdr), cfa_off, len(cfa))
    return head.ljust(jpg_off, b"\0") + jpg + cfa_hdr + cfa


def avif(seed=33):
    ftyp = _box(b"ftyp", b"avif\0\0\0\0avifmif1miaf")
    meta = _box(b"meta", b"\0\0\0\0" + _box(b"hdlr", b"\0" * 8 + b"pict" + b"\0" * 13) + _box(b"iinf", b"\0" * 6))
    return ftyp + meta + _box(b"mdat", _rnd(seed, 30_000))


def jxl(seed=34):
    ftyp = _box(b"ftyp", b"jxl \0\0\0\0jxl ")
    return b"\0\0\0\x0cJXL \r\n\x87\n" + ftyp + _box(b"jxll", b"\x0a") + _box(b"jxlc", b"\xff\x0a" + _rnd(seed, 7000))


def ico(seed=35):
    png = pil("PNG", seed, (16, 16), mode="RGBA")
    dib = struct.pack("<IiiHHIIiiII", 40, 32, 64, 1, 32, 0, 32 * 32 * 4, 0, 0, 0, 0) + _rnd(seed + 1, 32 * 32 * 4 + 128)
    off = 6 + 32
    return (b"\0\0\1\0" + struct.pack("<H", 2) + struct.pack("<BBBBHHII", 16, 16, 0, 0, 1, 32, len(png), off)
            + struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(dib), off + len(png)) + png + dib)


def svg(prolog=True):
    body = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><title>café &amp; logo</title>'
            '<svg x="10" y="10" width="40" height="40"><circle cx="20" cy="20" r="18" fill="#c33"/></svg>'
            '<path d="M10 80 L90 80" stroke="black"/></svg>\n')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!-- made by the retrova tests -->\n' + body if prolog else body).encode()


def apng():
    from PIL import Image
    frames = [Image.frombytes("RGB", (40, 30), _rnd(36 + i, 40 * 30 * 3)) for i in range(3)]
    b = io.BytesIO()
    frames[0].save(b, "PNG", save_all=True, append_images=frames[1:], duration=100, loop=0)
    assert b"acTL" in b.getvalue()
    return b.getvalue()


def psb(seed=37, w=40, h=30):
    hdr = b"8BPS" + struct.pack(">H", 2) + b"\0" * 6 + struct.pack(">HIIHH", 3, h, w, 8, 3)
    return hdr + struct.pack(">II", 0, 0) + struct.pack(">Q", 0) + struct.pack(">H", 0) + _rnd(seed, w * h * 3)


# ---------------------------------------------------------------- video, audio
def asf(video=True, with_size=True, seed=38):
    def g(x):
        return uuid.UUID(x).bytes_le
    payload = _rnd(seed, 12_000)
    data = g("75B22636-668E-11CF-A6D9-00AA0062CE6C") + struct.pack("<Q", 50 + len(payload)) + b"\2" * 16 + \
        struct.pack("<Q", 10) + b"\1\1" + payload
    index = g("33000890-E5B1-11CF-89F4-00A0C90349CB") + struct.pack("<Q", 56 + 24) + b"\2" * 16 + \
        struct.pack("<QII", 10_000_000, 1, 4) + b"\0" * 24
    hdr_len = 30 + 104 + 78
    total = hdr_len + len(data) + len(index)
    fprops = g("8CABDCA1-A947-11CF-8EE4-00C00C205365") + struct.pack("<Q", 104) + b"\1" * 16 + \
        struct.pack("<Q", total if with_size else 0) + b"\0" * 56
    stream = g("B7DC0791-A9B7-11CF-8EE6-00C00C205365") + struct.pack("<Q", 78) + g(
        "BC19EFC0-5B4D-11CF-A8FD-00805F5C442B" if video else "F8699E40-5B4D-11CF-A8FD-00805F5C442B") + b"\0" * 38
    header = g("75B22630-668E-11CF-A6D9-00AA0062CE6C") + struct.pack("<QI", hdr_len, 2) + b"\1\2" + fprops + stream
    return header + data + index


def aac(frames=40, seed=39):
    r = random.Random(seed)
    out = b""
    for _ in range(frames):
        n = r.randrange(120, 400)
        out += bytes([0xFF, 0xF1, 0x50, 0x80 | (n >> 11), (n >> 3) & 0xFF, ((n & 7) << 5) | 0x1F, 0xFC]) + r.randbytes(n - 7)
    return out


def aiff(seed=40):
    comm = b"COMM" + struct.pack(">I", 18) + struct.pack(">hIh", 2, 5000, 16) + b"\x40\x0e\xac\x44" + b"\0" * 6
    ssnd = b"SSND" + struct.pack(">I", 8 + 20_000) + b"\0" * 8 + _rnd(seed, 20_000)
    body = b"AIFF" + comm + ssnd
    return b"FORM" + struct.pack(">I", len(body)) + body


def midi(seed=41):
    def track(n):
        ev = b"".join(b"\x00\x90" + bytes([60 + i % 12, 100]) + b"\x10\x80" + bytes([60 + i % 12, 0]) for i in range(n))
        ev += b"\x00\xff\x2f\x00"
        return b"MTrk" + struct.pack(">I", len(ev)) + ev
    return b"MThd" + struct.pack(">IHHH", 6, 1, 2, 480) + track(30) + track(55)


# ---------------------------------------------------------------- documents
def rtf(paragraphs=40):
    head = rb"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}" + b"\n"
    para = rb"{\pard caf\'e9 \{literal\} and \\ backslash\par}" + b"\n"
    binary = b"}{}{{" + b"\\\x00\xff" + b"}}}{}{}}" + b"{{{{"          # braces and a stray backslash: skipped by \bin
    assert len(binary) == 20
    return head + para * paragraphs + rb"{\pict\wmetafile8\bin20 " + binary + b"}\n}\r\n"


def eml(crlf=True, mozilla=False):
    nl = "\r\n" if crlf else "\n"
    b64 = base64.encodebytes(_rnd(42, 3000)).decode().replace("\n", nl)
    first = "X-Mozilla-Status: 0001" if mozilla else "Received: from mail.example.org (mail.example.org [192.0.2.7])"
    lines = [first, "From: Ann <ann@example.org>", "To: Bob <bob@example.net>", "Subject: Holiday photos",
             "Date: Sat, 04 Jul 2026 10:11:12 +0000", "Message-ID: <abc123@example.org>", "MIME-Version: 1.0",
             'Content-Type: multipart/mixed;', '\tboundary="----=_Part_77_1"', "", "This is a MIME message.", "",
             "------=_Part_77_1", "Content-Type: text/plain; charset=utf-8", "", "Here they are!", "",
             "------=_Part_77_1", 'Content-Type: image/jpeg; name="a.jpg"', "Content-Transfer-Encoding: base64", "",
             b64.rstrip(), "------=_Part_77_1--", ""]
    return nl.join(lines).encode()


def pst(ver=23, client=b"SM"):
    unicode, page = ver >= 23, 4096 if ver == 36 else 512
    amap = 0x4400 if ver != 36 else 0x4000
    eof = amap + page + (page if ver == 36 else 0)
    img = bytearray(_rnd(43, eof))
    img[:512] = bytes(512)
    img[0:4], img[8:10] = b"!BDN", client
    struct.pack_into("<H", img, 10, ver)
    img[14] = img[15] = 1
    if unicode:
        struct.pack_into("<QQ", img, 184, eof, amap)
        img[248] = 2
    else:
        struct.pack_into("<II", img, 168, eof, amap)
        img[200] = 2
    trailer = 16 if unicode else 12
    img[amap + page - trailer:amap + page - trailer + 2] = b"\x84\x84"
    return bytes(img)


def _zip_of(names, mimetype=None):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", compression=zipfile.ZIP_DEFLATED) as z:
        if mimetype:
            z.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        for n in names:
            z.writestr(n, ("<x>" + n + "</x>") * 100 + base64.b64encode(_rnd(len(n), 600)).decode())
    return b.getvalue()


def iwork(kind):
    extra = {"pages": ["Index/Document.iwa"],
             "numbers": ["Index/Document.iwa", "Index/CalculationEngine.iwa", "Index/Tables/DataList.iwa"],
             "key": ["Index/Document.iwa", "Index/Slide.iwa", "Index/MasterSlide.iwa"]}[kind]
    return _zip_of(extra + ["Metadata/Properties.plist", "Metadata/BuildVersionHistory.plist", "preview.jpg"])


# ---------------------------------------------------------------- fonts, logs
def sfnt(otf=False):
    tags = [b"CFF ", b"cmap", b"head", b"maxp"] if otf else [b"cmap", b"glyf", b"head", b"loca", b"maxp"]
    head = struct.pack(">IIIIHH", 0x10000, 0x10000, 0, 0x5F0F3CF5, 0, 2048) + b"\0" * 34
    sizes = {b"head": 54, b"maxp": 6, b"cmap": 100, b"loca": 40, b"glyf": 20_000, b"CFF ": 20_000}
    n = len(tags)
    log = n.bit_length() - 1
    out = (b"OTTO" if otf else b"\0\1\0\0") + struct.pack(">HHHH", n, 16 << log, log, 16 * n - (16 << log))
    off, body = 12 + 16 * n, b""
    for tg in tags:
        data = head if tg == b"head" else _rnd(tg[0], sizes[tg])
        out += tg + struct.pack(">III", 0, off, len(data))
        pad = data + b"\0" * (-len(data) % 4)
        body += pad
        off += len(pad)
    return out + body


def woff(v2=False, seed=44):
    comp = _rnd(seed, 7000)
    meta = _rnd(seed + 1, 300)
    tables = 2
    if v2:
        directory = _rnd(seed + 2, 12)
        meta_off = 48 + len(directory) + len(comp)
        length = meta_off + len(meta)
        return b"wOF2\0\1\0\0" + struct.pack(">IHHIIHHIIIII", length, tables, 0, 30_000, len(comp), 1, 0, meta_off,
                                              len(meta), 600, 0, 0) + directory + comp + meta
    directory = b"".join(tg + struct.pack(">IIII", 44 + 40 + i * 3500, 3500, 6000, 0) for i, tg in
                         enumerate([b"cmap", b"glyf"]))
    meta_off = 44 + len(directory) + len(comp)
    length = meta_off + len(meta)
    return b"wOFF\0\1\0\0" + struct.pack(">IHHIHHIIIII", length, tables, 0, 12 + 32 + 12000, 1, 0, meta_off, len(meta),
                                          600, 0, 0) + directory + comp + meta


def wal_file(rows=40):
    """A real SQLite write-ahead log: copied while the database is still open, before any checkpoint."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.sqlite")
    con = sqlite3.connect(p)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma wal_autocheckpoint=0")
    con.execute("create table t (a integer, b text)")
    for i in range(rows):
        con.execute("insert into t values (?, ?)", (i, "row %d " % i * 30))
        con.commit()
    with open(p + "-wal", "rb") as fh:
        data = fh.read()
    con.close()
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
    os.rmdir(d)
    return data


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
        "film.3g2": ("3g2", mp4(51, brand=b"3g2a", mdat_size=20_000)),
        "clip.3gp": ("3gp", mp4(52, brand=b"3gp4", mdat_size=20_000)),
        "video.m4v": ("m4v", mp4(53, brand=b"M4V ", mdat_size=20_000)),
        "canon.cr3": ("cr3", mp4(54, brand=b"crx ", mdat_size=40_000)),
        "photo.avif": ("avif", avif()),
        "image.jxl": ("jxl", jxl()),
        "icon.ico": ("ico", ico()),
        "logo.svg": ("svg", svg()),
        "plain.svg": ("svg", svg(prolog=False)),
        "anim.apng": ("png", apng()),
        "big.psb": ("psb", psb()),
        "canon.cr2": ("cr2", tiff_raw(b"Canon", b"Canon EOS 5D", cr2=True, seed=60)),
        "nikon.nef": ("nef", tiff_raw(b"NIKON CORPORATION", b"NIKON D850", order=">", seed=61)),
        "coolpix.nrw": ("nrw", tiff_raw(b"NIKON", b"COOLPIX A", seed=62)),
        "sony.arw": ("arw", tiff_raw(b"SONY", b"ILCE-7M3", seed=63)),
        "adobe.dng": ("dng", tiff_raw(b"Leica Camera AG", b"M10", dng=True, seed=64)),
        "pentax.pef": ("pef", tiff_raw(b"RICOH IMAGING COMPANY, LTD.", b"PENTAX K-1", order=">", seed=65)),
        "olympus.orf": ("orf", orf()),
        "fuji.raf": ("raf", raf()),
        "video.wmv": ("wmv", asf(video=True)),
        "song.wma": ("wma", asf(video=False, with_size=False)),
        "radio.aac": ("aac", aac()),
        "sound.aiff": ("aiff", aiff()),
        "tune.mid": ("midi", midi()),
        "letter.rtf": ("rtf", rtf()),
        "book.rtf": ("rtf", rtf(30_000)),
        "mail.eml": ("eml", eml()),
        "thunderbird.eml": ("eml", eml(crlf=False, mozilla=True)),
        "mail.pst": ("pst", pst()),
        "old.pst": ("pst", pst(ver=14)),
        "big.pst": ("pst", pst(ver=36)),
        "cache.ost": ("ost", pst(client=b"SO")),
        "report.pages": ("pages", iwork("pages")),
        "budget.numbers": ("numbers", iwork("numbers")),
        "talk.key": ("key", iwork("key")),
        "notes.odt": ("odt", _zip_of(["content.xml", "styles.xml"], b"application/vnd.oasis.opendocument.text")),
        "calc.ods": ("ods", _zip_of(["content.xml"], b"application/vnd.oasis.opendocument.spreadsheet")),
        "deck.odp": ("odp", _zip_of(["content.xml"], b"application/vnd.oasis.opendocument.presentation")),
        "app.apk": ("apk", _zip_of(["AndroidManifest.xml", "classes.dex"])),
        "lib.jar": ("jar", _zip_of(["META-INF/MANIFEST.MF", "a/B.class"])),
        "trip.kmz": ("kmz", _zip_of(["doc.kml", "files/icon.png"])),
        "body.ttf": ("ttf", sfnt()),
        "body.otf": ("otf", sfnt(otf=True)),
        "web.woff": ("woff", woff()),
        "web.woff2": ("woff2", woff(v2=True)),
        "db.sqlite-wal": ("wal", wal_file()),
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
