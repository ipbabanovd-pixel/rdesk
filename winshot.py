"""Снимок конкретного окна Windows без перекрывающих системных диалогов."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from tools.pngwrite import write_png

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

GA_ROOT = 2
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def screenshot_window(widget, path: str) -> bool:
    try:
        hwnd = user32.GetAncestor(int(widget.winfo_id()), GA_ROOT)
        if not hwnd:
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return False
        screen = user32.GetDC(0)
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        old = gdi32.SelectObject(memory, bitmap)
        try:
            ok = user32.PrintWindow(hwnd, memory, PW_RENDERFULLCONTENT)
            if not ok:
                gdi32.BitBlt(memory, 0, 0, width, height, screen, rect.left, rect.top,
                             0x00CC0020)
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = BI_RGB
            buffer = ctypes.create_string_buffer(width * height * 4)
            lines = gdi32.GetDIBits(memory, bitmap, 0, height, buffer,
                                    ctypes.byref(info), DIB_RGB_COLORS)
            if lines == 0:
                return False
            source = buffer.raw
            rgb = bytearray(width * height * 3)
            for index in range(width * height):
                src = index * 4
                dst = index * 3
                rgb[dst] = source[src + 2]
                rgb[dst + 1] = source[src + 1]
                rgb[dst + 2] = source[src]
            write_png(path, width, height, bytes(rgb))
            return True
        finally:
            gdi32.SelectObject(memory, old)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory)
            user32.ReleaseDC(0, screen)
    except Exception:  # noqa: BLE001
        return False
