"""File-format knowledge for signature carving ("deep scan").

Each detector gets a reader `rd` (rd.read(offset, n) -> bytes, short at the end
of the scanned area), the absolute start offset of a signature hit and a size cap.
It returns (length, type) when the bytes at `start` are the beginning of a
well-formed file of that format, or None. Detectors never trust a single magic
number: they walk the format's structure to find the real end and reject
anything inconsistent, so random data and fragments are not saved.
"""
from __future__ import annotations

import re
import struct
import zlib

KB, MB, GB = 1024, 1024 ** 2, 1024 ** 3
BLK = 1024 * 1024

# type -> (extension, group, friendly name, max size)
TYPES = {
    "jpg": ("jpg", "photos", "JPEG photo", 128 * MB),
    "png": ("png", "photos", "PNG image", 256 * MB),
    "gif": ("gif", "photos", "GIF image", 64 * MB),
    "bmp": ("bmp", "photos", "BMP image", 256 * MB),
    "webp": ("webp", "photos", "WebP image", 64 * MB),
    "heic": ("heic", "photos", "HEIC photo (iPhone)", 128 * MB),
    "avif": ("avif", "photos", "AVIF image", 128 * MB),
    "tif": ("tif", "photos", "TIFF image", 1 * GB),
    "cr2": ("cr2", "photos", "Canon RAW photo", 512 * MB),
    "cr3": ("cr3", "photos", "Canon RAW photo (CR3)", 512 * MB),
    "nef": ("nef", "photos", "Nikon RAW photo", 512 * MB),
    "arw": ("arw", "photos", "Sony RAW photo", 512 * MB),
    "dng": ("dng", "photos", "DNG RAW photo", 512 * MB),
    "psd": ("psd", "photos", "Photoshop document", 2 * GB),
    "mp4": ("mp4", "videos", "MP4 video", 64 * GB),
    "mov": ("mov", "videos", "QuickTime video", 64 * GB),
    "m4v": ("m4v", "videos", "M4V video", 64 * GB),
    "3gp": ("3gp", "videos", "3GP video", 8 * GB),
    "avi": ("avi", "videos", "AVI video", 4 * GB),
    "mkv": ("mkv", "videos", "Matroska video", 64 * GB),
    "webm": ("webm", "videos", "WebM video", 64 * GB),
    "mp3": ("mp3", "music", "MP3 audio", 1 * GB),
    "m4a": ("m4a", "music", "M4A audio", 4 * GB),
    "wav": ("wav", "music", "WAV audio", 4 * GB),
    "ogg": ("ogg", "music", "Ogg audio", 1 * GB),
    "opus": ("opus", "music", "Opus audio", 1 * GB),
    "flac": ("flac", "music", "FLAC audio", 2 * GB),
    "pdf": ("pdf", "documents", "PDF document", 512 * MB),
    "docx": ("docx", "documents", "Word document", 1 * GB),
    "xlsx": ("xlsx", "documents", "Excel spreadsheet", 1 * GB),
    "pptx": ("pptx", "documents", "PowerPoint presentation", 2 * GB),
    "odt": ("odt", "documents", "OpenDocument text", 1 * GB),
    "ods": ("ods", "documents", "OpenDocument spreadsheet", 1 * GB),
    "odp": ("odp", "documents", "OpenDocument presentation", 2 * GB),
    "epub": ("epub", "documents", "E-book (EPUB)", 1 * GB),
    "doc": ("doc", "documents", "Word 97-2003 document", 1 * GB),
    "xls": ("xls", "documents", "Excel 97-2003 spreadsheet", 1 * GB),
    "ppt": ("ppt", "documents", "PowerPoint 97-2003 presentation", 2 * GB),
    "msg": ("msg", "documents", "Outlook message", 512 * MB),
    "zip": ("zip", "archives", "ZIP archive", 4 * GB),
    "apk": ("apk", "archives", "Android app", 4 * GB),
    "jar": ("jar", "archives", "Java archive", 4 * GB),
    "rar": ("rar", "archives", "RAR archive", 16 * GB),
    "7z": ("7z", "archives", "7-Zip archive", 16 * GB),
    "sqlite": ("sqlite", "other", "SQLite database", 8 * GB),
}
GROUPS = {"photos", "videos", "music", "documents", "archives", "other"}


def expand_types(names) -> set:
    """Accept type names, group names ('photos'...) and 'all' -> set of types."""
    names = [str(n).lower().strip().lstrip(".") for n in (names or [])]
    if not names or "all" in names:
        return set(TYPES)
    alias = {"jpeg": "jpg", "tiff": "tif", "heif": "heic", "sqlite3": "sqlite", "db": "sqlite", "qt": "mov",
             "image": "photos", "images": "photos", "photo": "photos", "video": "videos", "audio": "music",
             "office": "documents", "docs": "documents", "archive": "archives"}
    out = set()
    for n in names:
        n = alias.get(n, n)
        if n in TYPES:
            out.add(n)
        elif n in GROUPS:
            out |= {t for t, v in TYPES.items() if v[1] == n}
    return out


def _be16(b, o=0):
    return struct.unpack_from(">H", b, o)[0]


def _be32(b, o=0):
    return struct.unpack_from(">I", b, o)[0]


def _be64(b, o=0):
    return struct.unpack_from(">Q", b, o)[0]


def _le16(b, o=0):
    return struct.unpack_from("<H", b, o)[0]


def _le32(b, o=0):
    return struct.unpack_from("<I", b, o)[0]


def _le64(b, o=0):
    return struct.unpack_from("<Q", b, o)[0]


def _printable4(t: bytes) -> bool:
    return len(t) == 4 and all(32 <= c < 127 for c in t)


# ====================================================================== JPEG
_ENTROPY_MARKER = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg(rd, s, cap):
    h = rd.read(s, 4)
    if len(h) < 4 or h[:3] != b"\xff\xd8\xff" or not 0xC0 <= h[3] <= 0xFE:
        return None
    lim = s + cap
    pos = s + 2
    sof = sos = False
    while pos + 2 <= lim:
        m = rd.read(pos, 4)
        if len(m) < 2 or m[0] != 0xFF:
            return None
        marker = m[1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xD9:
            return (pos + 2 - s, "jpg") if sof and sos else None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xD8, 0x00) or len(m) < 4:
            return None
        seg = _be16(m, 2)
        if seg < 2:
            return None
        if marker in _SOF:
            f = rd.read(pos + 4, 6)
            if len(f) < 6 or f[0] not in (8, 12, 16) or _be16(f, 3) == 0 or f[5] not in (1, 2, 3, 4):
                return None
            sof = True
        elif marker == 0xDA:
            if not sof:
                return None
            sos = True
            pos += 2 + seg
            # entropy-coded data: the next marker is FF followed by anything but 00, RSTn or FF
            while True:
                if pos >= lim:
                    return None
                block = rd.read(pos, min(BLK, lim - pos) + 1)
                if len(block) < 2:
                    return None
                mm = _ENTROPY_MARKER.search(block)
                if mm is not None:
                    pos += mm.start()
                    break
                pos += len(block) - 1
            continue
        pos += 2 + seg
    return None


# ====================================================================== PNG
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def png(rd, s, cap):
    if rd.read(s, 8) != PNG_SIG:
        return None
    lim = s + cap
    pos, first = s + 8, True
    while pos + 12 <= lim:
        hdr = rd.read(pos, 8)
        if len(hdr) < 8:
            return None
        n, t = _be32(hdr), hdr[4:8]
        if not all(65 <= c <= 90 or 97 <= c <= 122 for c in t) or n > cap:
            return None
        if first:
            if t != b"IHDR" or n != 13:
                return None
            body = rd.read(pos + 4, 4 + 13 + 4)
            if len(body) < 21 or zlib.crc32(body[:17]) & 0xFFFFFFFF != _be32(body, 17):
                return None
            first = False
        pos += 12 + n
        if t == b"IEND":
            return (pos - s, "png") if n == 0 else None
    return None


# ====================================================================== GIF
def gif(rd, s, cap):
    h = rd.read(s, 13)
    if len(h) < 13 or h[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    if _le16(h, 6) == 0 or _le16(h, 8) == 0:
        return None
    pos = s + 13
    if h[10] & 0x80:
        pos += 3 * (2 << (h[10] & 7))
    lim = s + cap
    images = 0

    def sub_blocks(p):
        while p < lim:
            n = rd.read(p, 1)
            if not n:
                return None
            p += 1 + n[0]
            if n[0] == 0:
                return p
        return None

    while pos < lim:
        b = rd.read(pos, 1)
        if not b:
            return None
        if b == b"\x3b":
            return (pos + 1 - s, "gif") if images else None
        if b == b"\x21":
            pos = sub_blocks(pos + 2)
        elif b == b"\x2c":
            d = rd.read(pos, 10)
            if len(d) < 10:
                return None
            pos += 10
            if d[9] & 0x80:
                pos += 3 * (2 << (d[9] & 7))
            lzw = rd.read(pos, 1)
            if not lzw or not 1 <= lzw[0] <= 12:
                return None
            pos = sub_blocks(pos + 1)
            images += 1
        else:
            return None
        if pos is None:
            return None
    return None


# ====================================================================== BMP
def bmp(rd, s, cap):
    h = rd.read(s, 54)
    if len(h) < 30 or h[:2] != b"BM":
        return None
    size, off, dib = _le32(h, 2), _le32(h, 10), _le32(h, 14)
    if dib not in (12, 40, 52, 56, 64, 108, 124) or not 26 <= size <= cap or not 14 + dib <= off < size:
        return None
    if _le32(h, 6) != 0:
        return None
    if dib == 12:
        w, hgt, planes, bpp, comp = _le16(h, 18), _le16(h, 20), _le16(h, 22), _le16(h, 24), 0
    else:
        if len(h) < 54:
            return None
        w, hgt = struct.unpack_from("<ii", h, 18)
        planes, bpp, comp = _le16(h, 26), _le16(h, 28), _le32(h, 30)
    hgt = abs(hgt)
    if planes != 1 or bpp not in (1, 2, 4, 8, 16, 24, 32) or not 0 < w <= 65535 or not 0 < hgt <= 65535 \
            or comp > 6:
        return None
    if comp == 0:
        need = ((w * bpp + 31) // 32) * 4 * hgt
        if off + need > size or size > off + need + 4 * KB + need // 8:
            return None
    return size, "bmp"


# ====================================================================== RIFF: WAV / AVI / WebP
def riff(rd, s, cap):
    h = rd.read(s, 20)
    if len(h) < 20 or h[:4] != b"RIFF":
        return None
    size, form, first = _le32(h, 4), h[8:12], h[12:16]
    if form == b"WAVE" and first in (b"fmt ", b"LIST", b"JUNK", b"bext", b"fact", b"PEAK", b"iXML", b"ds64"):
        typ = "wav"
    elif form == b"AVI " and first in (b"LIST", b"JUNK"):
        typ = "avi"
    elif form == b"WEBP" and first in (b"VP8 ", b"VP8L", b"VP8X"):
        typ = "webp"
    else:
        return None
    total = size + 8
    if total < 20 or total > cap:
        return None
    pos, end = s + 12, s + total
    chunks = 0
    while pos + 8 <= end:
        c = rd.read(pos, 8)
        if len(c) < 8 or not _printable4(c[:4]):
            return None
        n = _le32(c, 4)
        pos += 8 + n + (n & 1)
        chunks += 1
        if chunks > 100000:
            break
    if pos > end + 1 or chunks == 0:
        return None
    return total, typ


# ====================================================================== ISO base media (MP4/MOV/HEIC/M4A/CR3)
_TOP_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"pnot", b"PICT", b"styp",
              b"sidx", b"moof", b"mfra", b"pdin", b"prft", b"emsg", b"junk", b"udta", b"Xtra", b"beam",
              b"CNCV", b"ssix", b"idat", b"iinf", b"jumb"}
_HEIF = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"mif2"}


def isobmff(rd, s, cap):
    h = rd.read(s, 64)
    if len(h) < 16 or h[4:8] != b"ftyp":
        return None
    fsz = _be32(h)
    if not 16 <= fsz <= 512 or fsz % 4:
        return None
    ft = rd.read(s, fsz)
    if len(ft) < fsz:
        return None
    major = ft[8:12]
    brands = [major] + [ft[i:i + 4] for i in range(16, fsz, 4)]
    if not all(_printable4(b) or b == b"\0\0\0\0" for b in brands) or not _printable4(major):
        return None
    lim = s + cap
    pos, boxes = s, {}
    while pos + 8 <= lim:
        hd = rd.read(pos, 16)
        if len(hd) < 8:
            break
        sz, t = _be32(hd), hd[4:8]
        if t not in _TOP_BOXES:
            break
        if sz == 1:
            if len(hd) < 16:
                return None
            sz = _be64(hd, 8)
            if sz < 16:
                return None
        elif sz == 0:
            return None            # "to the end of the file": no way to tell where that is
        elif sz < 8:
            return None
        if pos + sz > lim:
            return None
        boxes.setdefault(t, pos)
        pos += sz
    total = pos - s
    bset = set(brands)
    if bset & _HEIF or b"avif" in bset or b"avis" in bset:
        if b"meta" not in boxes or (b"mdat" not in boxes and b"idat" not in boxes):
            return None
        return total, ("avif" if (b"avif" in bset or b"avis" in bset) and not bset & _HEIF else "heic")
    if b"moov" not in boxes:
        return None
    mv = rd.read(boxes[b"moov"], 4096)
    if b"mvhd" not in mv and b"cmov" not in mv:
        return None
    if major == b"qt  ":
        typ = "mov"
    elif major == b"crx ":
        typ = "cr3"
    elif major in (b"M4A ", b"M4B ", b"M4P "):
        typ = "m4a"
    elif major == b"M4V ":
        typ = "m4v"
    elif major[:3] in (b"3gp", b"3g2"):
        typ = "3gp"
    else:
        typ = "mp4"
    return total, typ


# ====================================================================== PDF
_PDF_HDR = re.compile(rb"%PDF-[12]\.\d")
_PDF_MORE = re.compile(rb"(\d+\s+\d+\s+obj|xref|trailer|startxref|%(?!PDF-))")


def pdf(rd, s, cap):
    if not _PDF_HDR.match(rd.read(s, 8)):
        return None
    lim = s + cap
    pos, end = s, None
    while pos < lim:
        block = rd.read(pos, min(BLK, lim - pos) + 5)
        if len(block) < 6:
            break
        i = block.find(b"%%EOF")
        if i < 0:
            pos += len(block) - 5
            continue
        e = pos + i + 5
        tail = rd.read(e, 2)
        if tail[:2] == b"\r\n":
            e += 2
        elif tail[:1] in (b"\r", b"\n"):
            e += 1
        # the startxref pointer must sit just before %%EOF
        before = rd.read(max(s, pos + i - 1024), min(1024, pos + i - s))
        if b"startxref" in before:
            end = e
            nxt = rd.read(e, 64).lstrip(b"\r\n\t ")
            if nxt and _PDF_MORE.match(nxt):      # an incremental update follows
                pos = e
                continue
            break
        pos = e
    return (end - s, "pdf") if end else None


# ====================================================================== ZIP family
_ZIP_METHODS = {0, 1, 6, 8, 9, 12, 14, 19, 93, 95, 96, 97, 98, 99}


def zip_family(rd, s, cap):
    lim = s + cap
    pos, entries = s, 0
    while True:
        h = rd.read(pos, 30)
        if len(h) < 4:
            return None
        sig = h[:4]
        if sig == b"PK\x03\x04":
            if len(h) < 30:
                return None
            ver, flags, method = _le16(h, 4), _le16(h, 6), _le16(h, 8)
            csize, nlen, xlen = _le32(h, 18), _le16(h, 26), _le16(h, 28)
            if ver > 100 or method not in _ZIP_METHODS or not 0 < nlen <= 4096:
                return None
            name = rd.read(pos + 30, nlen)
            if len(name) < nlen or b"\0" in name:
                return None
            data = pos + 30 + nlen + xlen
            if flags & 0x08 and csize in (0, 0xFFFFFFFF):
                nxt = _find_after_data(rd, data, lim)
                if nxt is None:
                    return None
                pos = nxt
            else:
                if csize == 0xFFFFFFFF:      # zip64: real size in the extra field
                    extra = rd.read(pos + 30 + nlen, xlen)
                    csize = _zip64_csize(extra, h)
                    if csize is None:
                        return None
                pos = data + csize
                if flags & 0x08:
                    dd = rd.read(pos, 4)
                    pos += (16 if dd == b"PK\x07\x08" else 12)
            entries += 1
            if pos > lim:
                return None
        elif sig == b"PK\x01\x02" and entries:
            return _zip_central(rd, s, pos, lim, entries)
        else:
            return None


def _zip64_csize(extra, h):
    i = 0
    while i + 4 <= len(extra):
        tag, n = _le16(extra, i), _le16(extra, i + 2)
        if tag == 1:
            body = extra[i + 4:i + 4 + n]
            # order: uncompressed, compressed (each present only if the 32-bit field is 0xFFFFFFFF)
            j = 8 if _le32(h, 22) == 0xFFFFFFFF else 0
            if len(body) >= j + 8:
                return _le64(body, j)
            return None
        i += 4 + n
    return None


def _find_after_data(rd, pos, lim):
    """Streamed entry (size unknown in its header): find its data descriptor or the next header."""
    start = pos
    while pos < lim:
        block = rd.read(pos, min(BLK, lim - pos) + 4)
        if len(block) < 5:
            return None
        cands = [i for i in (block.find(b"PK\x07\x08"), block.find(b"PK\x03\x04"), block.find(b"PK\x01\x02"))
                 if i >= 0]
        if cands:
            i = min(cands)
            at = pos + i
            if block[i:i + 4] == b"PK\x07\x08":
                dd = rd.read(at, 24)
                if len(dd) >= 16 and _le32(dd, 8) == at - start:
                    return at + 16
                if len(dd) >= 24 and _le64(dd, 8) == at - start:
                    return at + 24
                pos = at + 1
                continue
            return at
        pos += len(block) - 4
    return None


def _zip_central(rd, s, cd_start, lim, local_entries):
    pos, names, count = cd_start, [], 0
    while pos + 46 <= lim:
        h = rd.read(pos, 46)
        if h[:4] != b"PK\x01\x02":
            break
        nlen, xlen, clen = _le16(h, 28), _le16(h, 30), _le16(h, 32)
        if count < 2000:
            names.append(rd.read(pos + 46, nlen))
        pos += 46 + nlen + xlen + clen
        count += 1
    h = rd.read(pos, 4)
    if h == b"PK\x06\x06":                      # zip64 end of central directory record
        z = rd.read(pos, 12)
        pos += 12 + _le64(z, 4)
        loc = rd.read(pos, 20)
        if loc[:4] != b"PK\x06\x07":
            return None
        pos += 20
    e = rd.read(pos, 22)
    if len(e) < 22 or e[:4] != b"PK\x05\x06":
        return None
    total_entries, cd_size, cd_off = _le16(e, 10), _le32(e, 12), _le32(e, 16)
    if cd_off != 0xFFFFFFFF and (cd_off != cd_start - s or cd_size != (pos - cd_start if h != b"PK\x06\x06"
                                                                        else cd_size)):
        return None
    if total_entries not in (0xFFFF, count & 0xFFFF):
        return None
    end = pos + 22 + _le16(e, 20)
    if end > lim or count == 0:
        return None
    return end - s, _zip_kind(rd, s, names)


def _zip_kind(rd, s, names):
    ns = [n.decode("utf-8", "replace") for n in names]
    sset = set(ns)
    if "[Content_Types].xml" in sset:
        if any(n.startswith("word/") for n in ns):
            return "docx"
        if any(n.startswith("xl/") for n in ns):
            return "xlsx"
        if any(n.startswith("ppt/") for n in ns):
            return "pptx"
    if "mimetype" in sset:
        mt = rd.read(s + 30 + 8, 80)        # first entry is the stored "mimetype" file
        if b"application/epub+zip" in mt:
            return "epub"
        if b"opendocument.text" in mt:
            return "odt"
        if b"opendocument.spreadsheet" in mt:
            return "ods"
        if b"opendocument.presentation" in mt:
            return "odp"
    if "AndroidManifest.xml" in sset and any(n.endswith(".dex") for n in ns):
        return "apk"
    if "META-INF/MANIFEST.MF" in sset and any(n.endswith(".class") for n in ns):
        return "jar"
    return "zip"


# ====================================================================== SQLite
def sqlite(rd, s, cap):
    h = rd.read(s, 100)
    if len(h) < 100 or h[:16] != b"SQLite format 3\0":
        return None
    ps = _be16(h, 16)
    ps = 65536 if ps == 1 else ps
    if ps < 512 or ps > 65536 or ps & (ps - 1):
        return None
    if h[18] not in (1, 2) or h[19] not in (1, 2) or h[21] != 64 or h[22] != 32 or h[23] != 32:
        return None
    pages = _be32(h, 28)
    if pages == 0 or _be32(h, 92) != _be32(h, 24):      # in-header size only valid if these agree
        return None
    total = ps * pages
    if total > cap:
        return None
    return total, "sqlite"


# ====================================================================== MP3 (with ID3v2 tag)
_BR = {
    (3, 3): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (3, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 3): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 1): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def mpeg_frame_len(h: bytes, want=None):
    if len(h) < 4 or h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return 0, None
    ver, layer = (h[1] >> 3) & 3, (h[1] >> 1) & 3
    bri, sri, pad = h[2] >> 4, (h[2] >> 2) & 3, (h[2] >> 1) & 1
    if ver == 1 or layer == 0 or bri in (0, 15) or sri == 3:
        return 0, None
    key = (ver, layer)
    if want is not None and key != want:
        return 0, None
    br = _BR[(3 if ver == 3 else 2, layer)][bri] * 1000
    sr = _SR[ver][sri]
    if layer == 3:          # Layer I
        n = (12 * br // sr + pad) * 4
    elif layer == 1 and ver != 3:
        n = 72 * br // sr + pad
    else:
        n = 144 * br // sr + pad
    return n, key


def mp3(rd, s, cap):
    h = rd.read(s, 10)
    if len(h) < 10 or h[:3] != b"ID3" or h[3] not in (2, 3, 4) or h[4] == 0xFF or any(b & 0x80 for b in h[6:10]):
        return None
    size = (h[6] << 21) | (h[7] << 14) | (h[8] << 7) | h[9]
    pos = s + 10 + size + (10 if h[5] & 0x10 else 0)
    lim = s + cap
    # some taggers leave zero padding past the declared size
    skip = 0
    while skip < 64 * KB:
        b = rd.read(pos, 1)
        if b != b"\0":
            break
        pos += 1
        skip += 1
    frames, want = 0, None
    while pos + 4 <= lim:
        n, key = mpeg_frame_len(rd.read(pos, 4), want)
        if not n:
            break
        want = key
        frames += 1
        pos += n
    if frames < 8 or pos > lim:
        return None
    if rd.read(pos, 3) == b"TAG":
        pos += 128
    return pos - s, "mp3"


# ====================================================================== Ogg
def _ogg_crc_table():
    t = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if r & 0x80000000 else (r << 1) & 0xFFFFFFFF
        t.append(r)
    return t


_OGG_T = _ogg_crc_table()


def _ogg_crc(data: bytes) -> int:
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFFFFFF) ^ _OGG_T[((c >> 24) & 0xFF) ^ b]
    return c


def _ogg_page_ok(rd, pos, n):
    page = bytearray(rd.read(pos, n))
    if len(page) < n:
        return False
    want = _le32(page, 22)
    page[22:26] = b"\0\0\0\0"
    return _ogg_crc(bytes(page)) == want


def ogg(rd, s, cap):
    lim = s + cap
    pos, pages, first_packet, good_end = s, 0, b"", None
    while pos + 27 <= lim:
        h = rd.read(pos, 27)
        if len(h) < 27 or h[:4] != b"OggS" or h[4] != 0:
            break
        nseg = h[26]
        segs = rd.read(pos + 27, nseg)
        if len(segs) < nseg:
            break
        n = 27 + nseg + sum(segs)
        if pos + n > lim:
            break
        if pages == 0:
            if not h[5] & 0x02 or not _ogg_page_ok(rd, pos, n):     # first page must begin a stream
                return None
            first_packet = rd.read(pos + 27 + nseg, 16)
        pages += 1
        nxt = rd.read(pos + n, 4)
        if nxt == b"OggS":
            good_end = pos + n             # another page follows: this one is complete
        elif h[5] & 0x04 and _ogg_page_ok(rd, pos, n):
            good_end = pos + n             # a complete end-of-stream page
            break
        else:
            break
        pos += n
    if pages < 2 or good_end is None:
        return None
    if first_packet.startswith(b"OpusHead"):
        return good_end - s, "opus"
    return good_end - s, "ogg"


# ====================================================================== FLAC
def flac(rd, s, cap):
    """FLAC: walk metadata blocks, then frames (sync 0xFFF8/0xFFF9) until the sync is lost."""
    h = rd.read(s, 8)
    if h[:4] != b"fLaC" or h[4] & 0x7F != 0:          # first block must be STREAMINFO
        return None
    pos, lim = s + 4, s + cap
    for _ in range(128):
        b = rd.read(pos, 4)
        if len(b) < 4:
            return None
        last, n = b[0] & 0x80, int.from_bytes(b[1:4], "big")
        pos += 4 + n
        if last:
            break
    else:
        return None
    if rd.read(pos, 2) not in (b"\xff\xf8", b"\xff\xf9"):
        return None
    # frames have no length field: the file ends where frame syncs stop appearing
    last_sync = pos
    while pos < lim:
        block = rd.read(pos, min(BLK, lim - pos) + 1)
        if len(block) < 2:
            break
        found = False
        i = block.find(b"\xff", 1)
        while 0 <= i < len(block) - 1:
            if block[i + 1] in (0xF8, 0xF9):
                last_sync, found = pos + i, True
            i = block.find(b"\xff", i + 1)
        if not found and pos + len(block) - last_sync > 64 * KB:
            break
        pos += len(block) - 1
    end = last_sync + 16
    while end < lim:
        c = rd.read(end, 4096)
        nz = len(c.rstrip(b"\0"))
        if nz == 0:
            break
        if nz < len(c):
            end += nz
            break
        end += len(c)
        if end - last_sync > 64 * KB:
            break
    return end - s, "flac"


# ====================================================================== RAR / 7z
def _vint(b, i):
    v, shift = 0, 0
    while i < len(b) and shift < 64:
        c = b[i]
        v |= (c & 0x7F) << shift
        i += 1
        if not c & 0x80:
            return v, i
        shift += 7
    return None, i


def rar(rd, s, cap):
    sig = rd.read(s, 8)
    lim = s + cap
    if sig[:7] == b"Rar!\x1a\x07\x00":
        pos = s + 7
        blocks = 0
        while pos + 7 <= lim:
            h = rd.read(pos, 11)
            if len(h) < 7:
                return None
            crc, typ, flags, size = _le16(h), h[2], _le16(h, 3), _le16(h, 5)
            if size < 7 or not 0x72 <= typ <= 0x7B:
                return None
            if blocks == 0:
                if typ != 0x73:
                    return None
                body = rd.read(pos + 2, size - 2)
                if zlib.crc32(body) & 0xFFFF != crc:
                    return None
            add = _le32(h, 7) if flags & 0x8000 and len(h) >= 11 else 0
            pos += size + add
            blocks += 1
            if typ == 0x7B:
                return pos - s, "rar"
        return None
    if sig == b"Rar!\x1a\x07\x01\x00":
        pos = s + 8
        blocks = 0
        while pos + 7 <= lim:
            h = rd.read(pos, 32)
            crc = _le32(h)
            hsize, j = _vint(h, 4)
            if hsize is None or hsize == 0 or hsize > 2 * MB:
                return None
            hdr = rd.read(j + pos, hsize) if j + hsize > len(h) else h[j:j + hsize]
            if blocks == 0 and zlib.crc32(rd.read(pos + 4, j - 4 + hsize)) & 0xFFFFFFFF != crc:
                return None
            htype, k = _vint(hdr, 0)
            hflags, k = _vint(hdr, k)
            if htype is None or hflags is None or not 1 <= htype <= 5:
                return None
            if blocks == 0 and htype != 1:
                return None
            data = 0
            if hflags & 1:
                _, k = _vint(hdr, k)
            if hflags & 2:
                data, k = _vint(hdr, k)
                if data is None:
                    return None
            pos += j + hsize + (data or 0)
            blocks += 1
            if htype == 5:
                return pos - s, "rar"
        return None
    return None


def sevenzip(rd, s, cap):
    h = rd.read(s, 32)
    if len(h) < 32 or h[:6] != b"7z\xbc\xaf\x27\x1c":
        return None
    if zlib.crc32(h[12:32]) & 0xFFFFFFFF != _le32(h, 8):
        return None
    total = 32 + _le64(h, 12) + _le64(h, 20)
    if _le64(h, 20) == 0 or total > cap:
        return None
    nh = rd.read(s + 32 + _le64(h, 12), min(_le64(h, 20), 16 * MB))
    if zlib.crc32(nh) & 0xFFFFFFFF != _le32(h, 28):
        return None
    return total, "7z"


# ====================================================================== Matroska / WebM
def _ebml_vint(rd, pos, is_id=False):
    b = rd.read(pos, 1)
    if not b:
        return None, 0
    first = b[0]
    n = 1
    mask = 0x80
    while n <= 8 and not first & mask:
        mask >>= 1
        n += 1
    if n > 8:
        return None, 0
    raw = rd.read(pos, n)
    if len(raw) < n:
        return None, 0
    if is_id:
        return int.from_bytes(raw, "big"), n
    v = first & (mask - 1)
    for c in raw[1:]:
        v = (v << 8) | c
    if v == (1 << (7 * n)) - 1:
        return -1, n             # unknown size
    return v, n


def matroska(rd, s, cap):
    if rd.read(s, 4) != b"\x1a\x45\xdf\xa3":
        return None
    size, n = _ebml_vint(rd, s + 4)
    if size is None or size < 0 or size > 4096:
        return None
    hdr = rd.read(s + 4 + n, size)
    doc = b"webm" if b"\x42\x82\x84webm" in hdr else b"matroska" if b"\x42\x82\x88matroska" in hdr else None
    if doc is None:
        return None
    pos = s + 4 + n + size
    sid, n1 = _ebml_vint(rd, pos, True)
    if sid != 0x18538067:
        return None
    ssize, n2 = _ebml_vint(rd, pos + n1)
    if ssize is None or ssize < 0:
        return None
    total = pos + n1 + n2 + ssize - s
    if total > cap:
        return None
    return total, "webm" if doc == b"webm" else "mkv"


# ====================================================================== TIFF and TIFF-based camera RAW
_TIFF_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}


def tiff(rd, s, cap):
    h = rd.read(s, 16)
    if h[:4] == b"II*\0":
        e = "<"
    elif h[:4] == b"MM\0*":
        e = ">"
    else:
        return None
    u16 = (lambda b, o: struct.unpack_from(e + "H", b, o)[0])
    u32 = (lambda b, o: struct.unpack_from(e + "I", b, o)[0])
    first = u32(h, 4)
    if not 8 <= first < cap:
        return None
    end = 8
    todo, seen, ifds = [first], set(), 0
    make, dng, has_dims = b"", False, False
    pairs = {(273, 279), (324, 325), (513, 514)}
    while todo and ifds < 64:
        off = todo.pop()
        if off in seen or not 8 <= off < cap:
            continue
        seen.add(off)
        cnt_b = rd.read(s + off, 2)
        if len(cnt_b) < 2:
            return None
        cnt = u16(cnt_b, 0)
        if not 1 <= cnt <= 1000:
            return None
        tbl = rd.read(s + off + 2, cnt * 12 + 4)
        if len(tbl) < cnt * 12 + 4:
            return None
        ifds += 1
        end = max(end, off + 2 + cnt * 12 + 4)
        vals = {}
        last_tag = -1
        for i in range(cnt):
            tag, typ, num = u16(tbl, i * 12), u16(tbl, i * 12 + 2), u32(tbl, i * 12 + 4)
            if typ not in _TIFF_SIZES or tag < last_tag:
                return None
            last_tag = tag
            n = _TIFF_SIZES[typ] * num
            if n > 4:
                voff = u32(tbl, i * 12 + 8)
                if voff + n > cap:
                    return None
                end = max(end, voff + n)
                raw = rd.read(s + voff, n) if n <= 64 * KB else b""
            else:
                raw = tbl[i * 12 + 8:i * 12 + 8 + n]
            vals[tag] = (typ, num, raw)
            if tag in (256, 257):
                has_dims = True
            if tag == 271:
                make = raw[:16]
            if tag == 50706:
                dng = True

        def ints(tag):
            if tag not in vals:
                return []
            typ, num, raw = vals[tag]
            if typ == 3:
                return [u16(raw, 2 * i) for i in range(min(num, len(raw) // 2))]
            if typ in (4, 13):
                return [u32(raw, 4 * i) for i in range(min(num, len(raw) // 4))]
            return []
        for a, b in pairs:
            offs, cnts = ints(a), ints(b)
            for o, c in zip(offs, cnts):
                if o + c > cap:
                    return None
                end = max(end, o + c)
        for sub in ints(330) + ints(34665):
            todo.append(sub)
        nxt = u32(tbl, cnt * 12)
        if nxt:
            todo.append(nxt)
    if not has_dims or end < 64:
        return None
    if h[8:10] == b"CR" and e == "<":
        typ = "cr2"
    elif dng:
        typ = "dng"
    elif make.upper().startswith(b"NIKON"):
        typ = "nef"
    elif make.upper().startswith(b"SONY"):
        typ = "arw"
    else:
        typ = "tif"
    return end, typ


# ====================================================================== Photoshop
def psd(rd, s, cap):
    h = rd.read(s, 26)
    if len(h) < 26 or h[:4] != b"8BPS" or _be16(h, 4) not in (1, 2) or h[6:12] != b"\0" * 6:
        return None
    ver, chans, hgt, wid, depth, mode = _be16(h, 4), _be16(h, 12), _be32(h, 14), _be32(h, 18), _be16(h, 22), \
        _be16(h, 24)
    if not 1 <= chans <= 56 or not 0 < hgt <= 300000 or not 0 < wid <= 300000 or depth not in (1, 8, 16, 32) \
            or mode > 9:
        return None
    pos = s + 26
    lim = s + cap
    for _ in range(3):           # colour mode data, image resources, layer & mask info
        n = rd.read(pos, 8 if ver == 2 and _ == 2 else 4)
        ln = _be64(n) if len(n) == 8 else _be32(n) if len(n) == 4 else None
        if ln is None:
            return None
        pos += len(n) + ln
        if pos > lim:
            return None
    comp_b = rd.read(pos, 2)
    if len(comp_b) < 2:
        return None
    comp = _be16(comp_b)
    pos += 2
    rows = hgt * chans
    if comp == 0:
        pos += rows * ((wid * depth + 7) // 8)
    elif comp == 1:
        cw = 4 if ver == 2 else 2
        counts = rd.read(pos, rows * cw)
        if len(counts) < rows * cw:
            return None
        fmt = ">%d%s" % (rows, "I" if cw == 4 else "H")
        pos += rows * cw + sum(struct.unpack(fmt, counts))
    else:
        return None
    if pos > lim:
        return None
    return pos - s, "psd"


# ====================================================================== OLE2 compound files (doc/xls/ppt/msg)
_OLE_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREESECT, ENDOFCHAIN = 0xFFFFFFFF, 0xFFFFFFFE


def ole(rd, s, cap):
    h = rd.read(s, 512)
    if len(h) < 512 or h[:8] != _OLE_SIG or _le16(h, 28) != 0xFFFE:
        return None
    shift = _le16(h, 30)
    if shift not in (9, 12):
        return None
    ss = 1 << shift
    nfat, dir_start, ndifat, difat_start = _le32(h, 44), _le32(h, 48), _le32(h, 72), _le32(h, 68)
    if nfat == 0 or nfat > 1_000_000:
        return None
    fat_secs = [_le32(h, 76 + 4 * i) for i in range(109)]
    fat_secs = [x for x in fat_secs if x < 0xFFFFFFFA][:nfat]
    cur, hops = difat_start, 0
    while len(fat_secs) < nfat and cur < 0xFFFFFFFA and hops < ndifat + 1:
        blk = rd.read(s + (cur + 1) * ss, ss)
        if len(blk) < ss:
            return None
        n = ss // 4 - 1
        fat_secs += [x for x in struct.unpack_from("<%dI" % n, blk) if x < 0xFFFFFFFA]
        cur = _le32(blk, ss - 4)
        hops += 1
    fat_secs = fat_secs[:nfat]
    if len(fat_secs) < nfat:
        return None
    fat, last = [], 0
    for fs in fat_secs:
        if (fs + 2) * ss > cap:
            return None
        blk = rd.read(s + (fs + 1) * ss, ss)
        if len(blk) < ss:
            return None
        fat.extend(struct.unpack_from("<%dI" % (ss // 4), blk))
    for i in range(len(fat) - 1, -1, -1):
        if fat[i] != FREESECT:
            last = i
            break
    total = (last + 2) * ss
    if total > cap or total < 2 * ss:
        return None
    # the directory tells what kind of document it is
    names, cur, hops = [], dir_start, 0
    while cur < len(fat) and hops < 64:
        blk = rd.read(s + (cur + 1) * ss, ss)
        for i in range(0, len(blk) - 127, 128):
            ln = _le16(blk, i + 64)
            if 2 <= ln <= 64:
                names.append(blk[i:i + ln - 2].decode("utf-16-le", "replace"))
        cur = fat[cur]
        hops += 1
    if not names or names[0] != "Root Entry":
        return None
    sset = set(names)
    if "WordDocument" in sset:
        typ = "doc"
    elif "Workbook" in sset or "Book" in sset:
        typ = "xls"
    elif "PowerPoint Document" in sset:
        typ = "ppt"
    elif any(n.startswith("__substg1.0_") for n in names):
        typ = "msg"
    else:
        return None           # other compound files (thumbnail caches, installers...) aren't worth saving
    return total, typ


# ====================================================================== registry
# (magic, offset of the magic inside the file, detector, types it can produce)
SIGNATURES = [
    (b"\xff\xd8\xff", 0, jpeg, {"jpg"}),
    (PNG_SIG, 0, png, {"png"}),
    (b"GIF8", 0, gif, {"gif"}),
    (b"BM", 0, bmp, {"bmp"}),
    (b"RIFF", 0, riff, {"wav", "avi", "webp"}),
    (b"ftyp", 4, isobmff, {"mp4", "mov", "m4v", "m4a", "3gp", "heic", "avif", "cr3"}),
    (b"%PDF-", 0, pdf, {"pdf"}),
    (b"PK\x03\x04", 0, zip_family, {"zip", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub", "apk", "jar"}),
    (b"SQLite format 3\0", 0, sqlite, {"sqlite"}),
    (b"ID3", 0, mp3, {"mp3"}),
    (b"OggS", 0, ogg, {"ogg", "opus"}),
    (b"fLaC", 0, flac, {"flac"}),
    (b"Rar!\x1a\x07", 0, rar, {"rar"}),
    (b"7z\xbc\xaf\x27\x1c", 0, sevenzip, {"7z"}),
    (b"\x1a\x45\xdf\xa3", 0, matroska, {"mkv", "webm"}),
    (b"II*\0", 0, tiff, {"tif", "cr2", "nef", "arw", "dng"}),
    (b"MM\0*", 0, tiff, {"tif", "nef", "dng"}),
    (b"8BPS", 0, psd, {"psd"}),
    (_OLE_SIG, 0, ole, {"doc", "xls", "ppt", "msg"}),
]
MAX_MAGIC_SPAN = max(len(m) + o for m, o, _, _ in SIGNATURES)
