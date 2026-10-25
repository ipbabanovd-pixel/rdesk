"""Захват экрана через GDI (без сторонних библиотек) и нарезка на тайлы.

Почему GDI: BitBlt/GetDIBits — нативный код, кадр 1920x1080 снимается за ~20 мс.
Масштабирование делает StretchBlt (тоже нативный, режим HALFTONE), поэтому
клиент может попросить картинку 50 % или 75 % и хост не тратит CPU на ресайз.

Сжатие: кадр режется на тайлы 64x64, сравнивается с предыдущим кадром,
и передаются только изменившиеся тайлы, каждый — отдельный zlib-поток.
Так статичный рабочий стол почти не занимает канал.
"""

from __future__ import annotations

import ctypes
import zlib
from ctypes import wintypes as wt

TILE = 64
SRCCOPY = 0x00CC0020
HALFTONE = 4
DIB_RGB_COLORS = 0
BI_RGB = 0

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------

def enable_dpi_awareness() -> None:
    """Без этого Windows отдаёт «виртуальные» координаты при масштабе 125–150 %."""
    try:
        # PER_MONITOR_AWARE_V2 = -4
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# --------------------------------------------------------------------------
# структуры GDI
# --------------------------------------------------------------------------

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


MONITORINFOF_PRIMARY = 1
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM
)

user32.GetSystemMetrics.restype = ctypes.c_int
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.MonitorFromPoint.argtypes = (wt.POINT, wt.DWORD)
user32.GetMonitorInfoW.argtypes = (wt.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
user32.GetDC.argtypes = (wt.HWND,)
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = (wt.HWND, wt.HDC)
user32.ReleaseDC.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = (wt.HDC,)
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = (wt.HDC, ctypes.c_int, ctypes.c_int)
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = (wt.HDC, wt.HGDIOBJ)
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = (wt.HGDIOBJ,)
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = (wt.HDC,)
gdi32.DeleteDC.restype = wt.BOOL
gdi32.BitBlt.argtypes = (wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD)
gdi32.BitBlt.restype = wt.BOOL
gdi32.GetDIBits.argtypes = (wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT,
                            ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wt.UINT)
gdi32.StretchBlt.argtypes = (wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wt.DWORD)
gdi32.SetStretchBltMode.argtypes = (wt.HDC, ctypes.c_int)
gdi32.SetBrushOrgEx.argtypes = (wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)


def _monitors() -> list[dict]:
    result: list[dict] = []

    def _callback(hmon, hdc, lprect, lparam):  # noqa: ANN001
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            result.append({
                "x": r.left, "y": r.top,
                "w": r.right - r.left, "h": r.bottom - r.top,
                "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                "name": info.szDevice.replace("\x00", "").strip(),
            })
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0)
    result.sort(key=lambda m: (not m["primary"], m["x"], m["y"]))
    return result


class ScreenLayout:
    """Виртуальный рабочий стол (объединение всех мониторов)."""

    __slots__ = ("x", "y", "width", "height", "monitors")

    def __init__(self, x: int, y: int, width: int, height: int, monitors: list[dict]) -> None:
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.monitors = monitors

    def as_dict(self) -> dict:
        return {
            "x": self.x, "y": self.y, "w": self.width, "h": self.height,
            "monitors": self.monitors, "tile": TILE,
        }

    def __repr__(self) -> str:
        return f"ScreenLayout({self.width}x{self.height}+{self.x}+{self.y}, мониторов: {len(self.monitors)})"


def get_layout() -> ScreenLayout:
    x = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    y = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    w = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    h = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
    if w <= 0 or h <= 0:  # запасной вариант — основной экран
        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        x, y = 0, 0
    mons = _monitors()
    if not mons:
        mons = [{"x": x, "y": y, "w": w, "h": h, "primary": True, "name": "DISPLAY"}]
    return ScreenLayout(x, y, w, h, mons)


class CaptureError(RuntimeError):
    pass


class ScreenGrabber:
    """Снимает кадр виртуального рабочего стола в формате RGB (3 байта/пиксель)."""

    def __init__(self) -> None:
        self._hdc_screen = user32.GetDC(None)
        if not self._hdc_screen:
            raise CaptureError("не удалось получить контекст экрана (GetDC)")
        self._surface_cache: dict[tuple[int, int], tuple[ctypes.c_void_p, ctypes.c_void_p, ctypes.Array]] = {}
        self._dpi = 96

    # --- внутреннее ---

    def _surface(self, width: int, height: int):
        key = (width, height)
        cached = self._surface_cache.get(key)
        if cached is not None:
            return cached
        hdc_mem = gdi32.CreateCompatibleDC(self._hdc_screen)
        if not hdc_mem:
            raise CaptureError("CreateCompatibleDC не удался")
        hbmp = gdi32.CreateCompatibleBitmap(self._hdc_screen, width, height)
        if not hbmp:
            gdi32.DeleteDC(hdc_mem)
            raise CaptureError("CreateCompatibleBitmap не удался")
        gdi32.SelectObject(hdc_mem, hbmp)
        buf = ctypes.create_string_buffer(width * height * 4)
        entry = (hdc_mem, hbmp, buf)
        self._surface_cache[key] = entry
        return entry

    def _dib(self, width: int, height: int) -> tuple[bytes, int]:
        hdc_mem, hbmp, buf = self._surface(width, height)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # сверху вниз
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        lines = gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(info), DIB_RGB_COLORS)
        if lines != height:
            raise CaptureError(f"GetDIBits вернул {lines} строк вместо {height}")
        return bytes(buf.raw), width * 4

    # --- публичный интерфейс ---

    def grab(self, layout: ScreenLayout, scale: float = 1.0) -> tuple[int, int, bytes]:
        """Снимок экрана, приведённый к масштабу scale. Возвращает (w, h, rgb)."""
        scale = min(1.0, max(0.1, float(scale)))
        dst_w = max(16, int(round(layout.width * scale)))
        dst_h = max(16, int(round(layout.height * scale)))
        hdc_mem, _hbmp, buf = self._surface(dst_w, dst_h)
        if (dst_w, dst_h) == (layout.width, layout.height):
            ok = gdi32.BitBlt(
                hdc_mem, 0, 0, dst_w, dst_h, self._hdc_screen,
                layout.x, layout.y, SRCCOPY,
            )
        else:
            gdi32.SetStretchBltMode(hdc_mem, HALFTONE)
            gdi32.SetBrushOrgEx(hdc_mem, 0, 0, None)
            ok = gdi32.StretchBlt(
                hdc_mem, 0, 0, dst_w, dst_h, self._hdc_screen,
                layout.x, layout.y, layout.width, layout.height, SRCCOPY,
            )
        if not ok:
            raise CaptureError("BitBlt/StretchBlt не удался (нет доступа к рабочему столу?)")
        bgra, stride = self._dib(dst_w, dst_h)
        rgb = bytearray(dst_w * dst_h * 3)
        # BGRA(4 байта) -> RGB(3 байта) срезами: это C-скорость, ~3 мс на кадр 720p
        rgb[0::3] = bgra[2::4]
        rgb[1::3] = bgra[1::4]
        rgb[2::3] = bgra[0::4]
        return dst_w, dst_h, bytes(rgb)

    def close(self) -> None:
        for hdc_mem, hbmp, _buf in self._surface_cache.values():
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
        self._surface_cache.clear()
        if self._hdc_screen:
            user32.ReleaseDC(None, self._hdc_screen)
            self._hdc_screen = None


# --------------------------------------------------------------------------
# тайлы
# --------------------------------------------------------------------------

class TileEncoder:
    """Сравнивает кадры и сжимает только изменившиеся тайлы."""

    def __init__(self, tile: int = TILE, level: int = 1) -> None:
        self.tile = tile
        self.level = level
        self._prev: bytes | None = None
        self._geom: tuple[int, int] | None = None
        self._cols = 0
        self._rows = 0

    def reset(self) -> None:
        self._prev = None
        self._geom = None

    @property
    def geometry(self) -> tuple[int, int]:
        return self._geom or (0, 0)

    def _tile_bytes(self, img: bytes, width: int, height: int, tx: int, ty: int) -> bytes:
        tile = self.tile
        x0 = tx * tile
        y0 = ty * tile
        tw = min(tile, width - x0)
        th = min(tile, height - y0)
        stride = width * 3
        row_len = tw * 3
        out = bytearray(row_len * th)
        pos = 0
        for y in range(y0, y0 + th):
            offset = y * stride + x0 * 3
            out[pos:pos + row_len] = img[offset:offset + row_len]
            pos += row_len
        return bytes(out)

    def encode(self, width: int, height: int, rgb: bytes, force_full: bool = False,
               level: int | None = None) -> tuple[list[tuple[int, bytes]], bool]:
        """Возвращает (список (индекс, сжатые байты), полный_кадр)."""
        lvl = self.level if level is None else level
        tile = self.tile
        stride = width * 3
        full = force_full or self._prev is None or self._geom != (width, height)
        prev = None if full else self._prev
        cols = (width + tile - 1) // tile
        rows = (height + tile - 1) // tile
        self._cols, self._rows = cols, rows

        if full:
            dirty_all = True
        else:
            dirty_rows = bytearray(height)
            dirty_count = 0
            for y in range(height):
                offset = y * stride
                if rgb[offset:offset + stride] != prev[offset:offset + stride]:  # type: ignore[index]
                    dirty_rows[y] = 1
                    dirty_count += 1
            dirty_all = dirty_count * 4 >= height * 3

        tiles: list[tuple[int, bytes]] = []
        for ty in range(rows):
            y0 = ty * tile
            if not dirty_all and 1 not in dirty_rows[y0:y0 + tile]:
                continue
            for tx in range(cols):
                idx = ty * cols + tx
                raw = self._tile_bytes(rgb, width, height, tx, ty)
                if not full:
                    old = self._tile_bytes(prev, width, height, tx, ty)  # type: ignore[arg-type]
                    if old == raw:
                        continue
                tiles.append((idx, zlib.compress(raw, lvl)))

        self._prev = rgb
        self._geom = (width, height)
        return tiles, full


def decode_tiles(width: int, height: int, tiles: list[tuple[int, bytes]],
                 tile: int = TILE, canvas: bytearray | None = None) -> bytearray:
    """Собирает кадр из тайлов (используется клиентом и тестами)."""
    size = width * height * 3
    if canvas is None or len(canvas) != size:
        canvas = bytearray(size)
    stride = width * 3
    cols = (width + tile - 1) // tile
    for idx, payload in tiles:
        raw = zlib.decompress(payload)
        tx, ty = idx % cols, idx // cols
        x0 = tx * tile
        y0 = ty * tile
        tw = min(tile, width - x0)
        th = min(tile, height - y0)
        expected = tw * th * 3
        if len(raw) != expected:
            raise ValueError(f"тайл {idx}: {len(raw)} байт вместо {expected}")
        row_len = tw * 3
        src = 0
        for y in range(y0, y0 + th):
            dst = y * stride + x0 * 3
            canvas[dst:dst + row_len] = raw[src:src + row_len]
            src += row_len
    return canvas
