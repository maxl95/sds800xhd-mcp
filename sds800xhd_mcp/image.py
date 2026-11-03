"""BMP → PNG without external dependencies (for SCDP screenshots)."""

from __future__ import annotations

import struct
import zlib


def _ctz(n: int) -> int:
    if n == 0:
        return 0
    return (n & -n).bit_length() - 1


def bmp_to_png(bmp: bytes) -> bytes:
    if bmp[:2] != b"BM":
        raise ValueError("Keine BMP-Daten")
    pixel_offset = struct.unpack_from("<I", bmp, 10)[0]
    width = struct.unpack_from("<i", bmp, 18)[0]
    raw_height = struct.unpack_from("<i", bmp, 22)[0]
    height = abs(raw_height)
    bpp = struct.unpack_from("<H", bmp, 28)[0]
    compression = struct.unpack_from("<I", bmp, 30)[0]
    top_down = raw_height < 0
    row_bytes = ((width * bpp + 31) // 32) * 4

    if bpp == 16:
        if compression == 3:
            masks = struct.unpack_from("<III", bmp, 54)
        else:
            masks = (0x7C00, 0x03E0, 0x001F)  # RGB555
        shifts = [_ctz(m) for m in masks]
        maxima = [(m >> s) or 1 for m, s in zip(masks, shifts)]
    elif bpp not in (24, 32):
        raise ValueError(f"Unsupported BMP color depth: {bpp} bpp")

    rows = bytearray()
    for y in range(height):
        src_row = y if top_down else height - 1 - y
        off = pixel_offset + src_row * row_bytes
        rows.append(0)  # PNG filter type "None"
        if bpp == 16:
            px = struct.unpack_from(f"<{width}H", bmp, off)
            for p in px:
                for m, s, mx in zip(masks, shifts, maxima):
                    rows.append(((p & m) >> s) * 255 // mx)
        else:
            step = bpp // 8
            line = bmp[off : off + width * step]
            for x in range(0, width * step, step):
                b, g, r = line[x], line[x + 1], line[x + 2]
                rows += bytes((r, g, b))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )
