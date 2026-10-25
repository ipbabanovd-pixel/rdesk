"""Эмуляция мыши и клавиатуры через SendInput (ctypes, без зависимостей).

Важно: SendInput уважает UIPI — если хост запущен без прав администратора,
он не сможет управлять окнами программ, запущенных от администратора.
Это поведение Windows, а не ограничение программы.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --- флаги ---
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

BUTTON_FLAGS = {
    0: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    1: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    2: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT
user32.MapVirtualKeyW.argtypes = (wt.UINT, wt.UINT)
user32.MapVirtualKeyW.restype = wt.UINT


class InjectError(RuntimeError):
    pass


def _send(inputs: list[INPUT]) -> None:
    if not inputs:
        return
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise InjectError(
            f"SendInput применил {sent} из {len(inputs)} событий "
            f"(код ошибки {ctypes.get_last_error()})"
        )


def _mouse(dx: int = 0, dy: int = 0, data: int = 0, flags: int = 0) -> INPUT:
    item = INPUT()
    item.type = INPUT_MOUSE
    item.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, None)
    return item


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki = KEYBDINPUT(vk, scan, flags, 0, None)
    return item


# --------------------------------------------------------------------------
# мышь
# --------------------------------------------------------------------------

def mouse_move(nx: int, ny: int) -> None:
    """Абсолютное позиционирование: nx, ny в диапазоне 0..65535 по виртуальному столу."""
    _send([_mouse(nx, ny, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)])


def mouse_button(button: int, down: bool, nx: int, ny: int) -> None:
    flags = BUTTON_FLAGS.get(button, BUTTON_FLAGS[0])[0 if down else 1]
    base = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    _send([_mouse(nx, ny, 0, flags | base)])


def mouse_wheel(delta: int, nx: int | None = None, ny: int | None = None) -> None:
    _send([_mouse(0, 0, delta, MOUSEEVENTF_WHEEL)])


# --------------------------------------------------------------------------
# клавиатура
# --------------------------------------------------------------------------

def key_event(vk: int, down: bool, extended: bool = False) -> None:
    """Нажатие/отпускание по виртуальному коду клавиши (VK)."""
    flags = 0 if down else KEYEVENTF_KEYUP
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
    _send([_key(vk, scan, flags)])


def unicode_char(ch: str, down: bool = True) -> None:
    """Ввод символа напрямую в буфер клавиатуры (не зависит от раскладки)."""
    from .protocol import utf16_units

    units = utf16_units(ch)
    if not units:
        return
    flags = KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP)
    _send([_key(0, unit, flags) for unit in units])


def type_text(text: str) -> None:
    """Печатает строку целиком (используется для передачи буфера обмена)."""
    for ch in text:
        if ch == "\n":
            unicode_char("\r", True)
            unicode_char("\r", False)
        else:
            unicode_char(ch, True)
            unicode_char(ch, False)


# --------------------------------------------------------------------------
# буфер обмена (чтобы вставить текст с клиента на хост)
# --------------------------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = (wt.HWND,)
user32.OpenClipboard.restype = wt.BOOL
user32.EmptyClipboard.restype = wt.BOOL
user32.IsClipboardFormatAvailable.argtypes = (wt.UINT,)
user32.IsClipboardFormatAvailable.restype = wt.BOOL
user32.GetClipboardData.argtypes = (wt.UINT,)
user32.GetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = (wt.UINT, wt.HANDLE)
user32.SetClipboardData.restype = wt.HANDLE
user32.CloseClipboard.restype = wt.BOOL
kernel32.GlobalAlloc.argtypes = (wt.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = (wt.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wt.HGLOBAL,)
kernel32.GlobalUnlock.restype = wt.BOOL


def get_clipboard_text(limit: int = 1024 * 1024) -> str:
    """Читает текст из буфера обмена. Возвращает "" для не-текста/занятого буфера."""
    opened = False
    for _ in range(8):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.01)
    if not opened:
        return ""
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            text = ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
        return text[:limit]
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    """Кладёт текст в буфер обмена Windows (без зависимостей)."""
    data = (text + "\x00").encode("utf-16-le")
    opened = False
    for _ in range(20):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.02)
    if not opened:
        raise InjectError("буфер обмена занят другой программой")
    try:
        if not user32.EmptyClipboard():
            raise InjectError("не удалось очистить буфер обмена")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise InjectError("не удалось выделить память под буфер обмена")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise InjectError("не удалось заблокировать память буфера обмена")
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise InjectError("SetClipboardData не удался")
        # после успешного SetClipboardData память принадлежит системе — не освобождаем
    finally:
        user32.CloseClipboard()
