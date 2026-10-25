"""Минимальный PNG-писатель — только для тестов и скриншотов (stdlib)."""

from __future__ import annotations

import struct
import zlib


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def write_png(path: str, width: int, height: int, rgb: bytes) -> None:
    if len(rgb) < width * height * 3:
        raise ValueError("недостаточно данных для PNG")
    stride = width * 3
    rows = [b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height)]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + _chunk(b"IEND", b"")
    )
    with open(path, "wb") as fh:
        fh.write(payload)
