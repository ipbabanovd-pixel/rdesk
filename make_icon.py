"""Генератор значка rdesk.ico (32x32 и 16x16) без сторонних библиотек.

ICO собирается из 32-битных BMP-картинок (BGRA + пустая AND-маска).
Запуск:  python assets/make_icon.py
"""

from __future__ import annotations

import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "rdesk.ico")

BG = (26, 38, 58)        # тёмно-синий фон
FRAME = (235, 241, 249)  # светлая рамка «экрана»
SCREEN = (58, 96, 140)   # заливка экрана
DOT = (56, 210, 107)     # зелёная точка «на связи»


def rounded_rect(x: int, y: int, w: int, h: int, r: int) -> set[tuple[int, int]]:
    pixels = set()
    for py in range(y, y + h):
        for px in range(x, x + w):
            dx = min(px - x, x + w - 1 - px)
            dy = min(py - y, y + h - 1 - py)
            if dx < r and dy < r:
                if (r - dx) ** 2 + (r - dy) ** 2 > r * r:
                    continue
            pixels.add((px, py))
    return pixels


def circle(cx: int, cy: int, radius: float) -> set[tuple[int, int]]:
    pixels = set()
    r = int(radius) + 1
    for py in range(cy - r, cy + r + 1):
        for px in range(cx - r, cx + r + 1):
            if (px - cx + 0.0) ** 2 + (py - cy + 0.0) ** 2 <= radius * radius:
                pixels.add((px, py))
    return pixels


def draw(size: int) -> list[tuple[int, int, int, int]]:
    """Возвращает пиксели сверху вниз: (r, g, b, a)."""
    canvas = [(0, 0, 0, 0)] * (size * size)

    def put(pixels, color, alpha=255):
        for px, py in pixels:
            if 0 <= px < size and 0 <= py < size:
                canvas[py * size + px] = (color[0], color[1], color[2], alpha)

    pad = max(1, size // 16)
    put(rounded_rect(pad, pad, size - 2 * pad, size - 2 * pad, max(2, size // 6)), BG)

    # монитор
    mx = max(2, round(size * 0.14))
    my = max(2, round(size * 0.20))
    mw = size - 2 * mx
    mh = round(size * 0.44)
    put(rounded_rect(mx, my, mw, mh, max(1, size // 16)), FRAME)
    inner = max(1, size // 16)
    put(rounded_rect(mx + inner, my + inner, mw - 2 * inner, mh - 2 * inner, 1), SCREEN)
    # подставка
    stand_w = max(2, round(size * 0.20))
    stand_h = max(1, round(size * 0.09))
    put(rounded_rect((size - stand_w) // 2, my + mh, stand_w, stand_h, 0),
        FRAME)
    base_w = max(4, round(size * 0.42))
    base_h = max(1, round(size * 0.07))
    put(rounded_rect((size - base_w) // 2, my + mh + stand_h, base_w, base_h, 0), FRAME)

    # зелёная точка «подключено»
    dot_r = max(1.5, size * 0.11)
    put(circle(round(size * 0.74), round(size * 0.74), dot_r), DOT)
    return canvas


def bmp_image(pixels: list[tuple[int, int, int, int]], size: int) -> bytes:
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):  # снизу вверх
        row = bytearray()
        for x in range(size):
            r, g, b, a = pixels[y * size + x]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    mask_row = b"\x00" * (((size + 31) // 32) * 4)
    mask = mask_row * size
    return header + b"".join(rows) + mask


def main() -> None:
    sizes = (16, 32, 48)
    images = [(s, bmp_image(draw(s), s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for size, blob in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                               len(blob), offset)
        offset += len(blob)
    with open(OUT, "wb") as fh:
        fh.write(header + entries + b"".join(blob for _, blob in images))
    print(f"готово: {OUT} ({os.path.getsize(OUT)} байт)")


if __name__ == "__main__":
    main()
