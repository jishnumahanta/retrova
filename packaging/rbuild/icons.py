"""App icons from brand/logo.svg: Retrova.icns (macOS, 16..1024) and Retrova.ico (Windows, 16..256).

Runs inside the packaging venv (cairosvg + Pillow). The ICNS and ICO containers are
written by hand so we control exactly which representations are inside.
"""
from __future__ import annotations

import io
import re
import struct
import sys
from pathlib import Path

import cairosvg
from PIL import Image, ImageFilter

MAC_BODY = 824          # Apple icon grid: 824x824 body centred on a 1024 canvas
SVG_BODY = 896          # logo.svg draws its rounded square at 64..960


def render(svg: bytes, size: int, viewbox: str | None = None) -> Image.Image:
    if viewbox:
        svg = re.sub(rb'viewBox="[^"]*"', b'viewBox="' + viewbox.encode() + b'"', svg, count=1)
    png = cairosvg.svg2png(bytestring=svg, output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def mac_master(svg: bytes) -> Image.Image:
    """1024px macOS icon: body scaled to the 824px grid plus Apple's soft drop shadow."""
    inner = round(1024 * MAC_BODY / SVG_BODY)
    art = render(svg, inner)
    canvas = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    off = (1024 - inner) // 2
    shadow_alpha = Image.new("L", (1024, 1024), 0)
    shadow_alpha.paste(art.getchannel("A"), (off, off + 12))
    shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(14)).point(lambda a: int(a * 0.30))
    shadow = Image.new("RGBA", (1024, 1024), (0, 0, 0, 255))
    shadow.putalpha(shadow_alpha)
    canvas = Image.alpha_composite(canvas, shadow)
    layer = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    layer.paste(art, (off, off))
    return Image.alpha_composite(canvas, layer)


def png_bytes(im: Image.Image) -> bytes:
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return b.getvalue()


def _icns_rle(channel: bytes) -> bytes:
    """Apple's PackBits variant used by is32/il32 channels."""
    out, i, n = bytearray(), 0, len(channel)
    while i < n:
        run = 1
        while i + run < n and run < 130 and channel[i + run] == channel[i]:
            run += 1
        if run >= 3:
            out += bytes([0x80 + run - 3, channel[i]])
            i += run
            continue
        start = i
        while i < n and i - start < 128:
            if i + 2 < n and channel[i] == channel[i + 1] == channel[i + 2]:
                break
            i += 1
        out += bytes([i - start - 1]) + channel[start:i]
    return bytes(out)


def _legacy_rgb(im: Image.Image) -> tuple[bytes, bytes]:
    r, g, b, a = im.split()
    return b"".join(_icns_rle(c.tobytes()) for c in (r, g, b)), a.tobytes()


def write_icns(master: Image.Image, path: Path) -> list[tuple[str, int]]:
    # (OSType, pixel size). PNG payloads (supported since 10.7) plus legacy RLE 16/32 px
    # (is32/s8mk, il32/l8mk) for old parsers and Finder's tiny list-view icons.
    types = [("icp4", 16), ("ic11", 32), ("icp5", 32), ("ic12", 64), ("icp6", 64),
             ("ic07", 128), ("ic13", 256), ("ic08", 256), ("ic14", 512), ("ic09", 512), ("ic10", 1024)]
    cache: dict[int, bytes] = {}
    chunks = []
    for ostype, rgb_type, mask_type, px in (("is32", "is32", "s8mk", 16), ("il32", "il32", "l8mk", 32)):
        rgb, mask = _legacy_rgb(master.resize((px, px), Image.LANCZOS))
        chunks.append(rgb_type.encode() + struct.pack(">I", len(rgb) + 8) + rgb)
        chunks.append(mask_type.encode() + struct.pack(">I", len(mask) + 8) + mask)
    for ostype, px in types:
        if px not in cache:
            im = master if px == 1024 else master.resize((px, px), Image.LANCZOS)
            cache[px] = png_bytes(im)
        data = cache[px]
        chunks.append(ostype.encode("ascii") + struct.pack(">I", len(data) + 8) + data)
    toc = b"TOC " + struct.pack(">I", 8 + 8 * len(chunks)) + b"".join(
        c[:8] for c in chunks)
    body = toc + b"".join(chunks)
    path.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)
    return types


def _dib(im: Image.Image) -> bytes:
    """32-bit BGRA DIB with AND mask, as stored inside .ico files (bottom-up)."""
    w, h = im.size
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    px = im.tobytes("raw", "BGRA")
    rows = [px[y * w * 4:(y + 1) * w * 4] for y in range(h)]
    xor = b"".join(reversed(rows))
    mask_stride = ((w + 31) // 32) * 4
    alpha = im.getchannel("A").tobytes()
    mask_rows = []
    for y in range(h):
        bits = bytearray(mask_stride)
        for x in range(w):
            if alpha[y * w + x] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask_rows.append(bytes(bits))
    return header + xor + b"".join(reversed(mask_rows))


def write_ico(svg: bytes, path: Path, sizes=(16, 20, 24, 32, 40, 48, 64, 96, 128, 256)) -> None:
    images = []
    for s in sizes:
        # Small sizes: crop the transparent margin so the glyph uses every pixel.
        vb = "52 52 920 920" if s <= 32 else ("40 40 944 944" if s <= 64 else None)
        im = render(svg, s, vb)
        data = png_bytes(im) if s >= 256 else _dib(im)
        images.append((s, data))
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in images:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    path.write_bytes(head + entries + blobs)


def main(svg_path: str, out_dir: str) -> None:
    svg = Path(svg_path).read_bytes()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    master = mac_master(svg)
    master.save(out / "Retrova-mac-1024.png")
    write_icns(master, out / "Retrova.icns")
    write_ico(svg, out / "Retrova.ico")
    render(svg, 256).save(out / "Retrova-256.png")
    print(f"icons written to {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
