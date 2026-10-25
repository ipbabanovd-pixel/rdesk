"""Таблица клавиш: keysym Tkinter -> виртуальный код Windows (VK).

Используется вьювером: он получает keysym от Tk и отправляет хосту
уже готовые VK/скан-коды, чтобы хост не зависел от раскладки вьювера.
"""

from __future__ import annotations

import ctypes
from functools import lru_cache

_user32 = ctypes.WinDLL("user32")
# keysym -> (VK, extended)
KEYSYM_TO_VK: dict[str, tuple[int, bool]] = {
    "Return": (0x0D, False),
    "KP_Enter": (0x0D, True),
    "BackSpace": (0x08, False),
    "Tab": (0x09, False),
    "ISO_Left_Tab": (0x09, False),
    "Escape": (0x1B, False),
    "space": (0x20, False),
    "Shift_L": (0xA0, False),
    "Shift_R": (0xA1, False),
    "Control_L": (0xA2, False),
    "Control_R": (0xA3, True),
    "Alt_L": (0xA4, False),
    "Alt_R": (0xA5, True),
    "Super_L": (0x5B, True),
    "Super_R": (0x5C, True),
    "Caps_Lock": (0x14, False),
    "Num_Lock": (0x90, True),
    "Scroll_Lock": (0x91, False),
    "Pause": (0x13, False),
    "Print": (0x2C, True),
    "Menu": (0x5D, True),
    "Left": (0x25, True),
    "Up": (0x26, True),
    "Right": (0x27, True),
    "Down": (0x28, True),
    "Home": (0x24, True),
    "End": (0x23, True),
    "Prior": (0x21, True),
    "Next": (0x22, True),
    "Insert": (0x2D, True),
    "Delete": (0x2E, True),
    "F1": (0x70, False), "F2": (0x71, False), "F3": (0x72, False),
    "F4": (0x73, False), "F5": (0x74, False), "F6": (0x75, False),
    "F7": (0x76, False), "F8": (0x77, False), "F9": (0x78, False),
    "F10": (0x79, False), "F11": (0x7A, False), "F12": (0x7B, False),
    "minus": (0xBD, False), "equal": (0xBB, False),
    "bracketleft": (0xDB, False), "bracketright": (0xDD, False),
    "backslash": (0xDC, False), "semicolon": (0xBA, False),
    "apostrophe": (0xDE, False), "grave": (0xC0, False),
    "comma": (0xBC, False), "period": (0xBE, False), "slash": (0xBF, False),
    "KP_Multiply": (0x6A, False), "KP_Add": (0x6B, False),
    "KP_Subtract": (0x6D, False), "KP_Decimal": (0x6E, False),
    "KP_Divide": (0x6F, True), "KP_0": (0x60, False), "KP_1": (0x61, False),
    "KP_2": (0x62, False), "KP_3": (0x63, False), "KP_4": (0x64, False),
    "KP_5": (0x65, False), "KP_6": (0x66, False), "KP_7": (0x67, False),
    "KP_8": (0x68, False), "KP_9": (0x69, False),
    "Cyrillic_shorti": (0x54, False),  # на всякий случай, «й»
}

# флаг «расширенная клавиша» в сообщениях протокола (совпадает с KEYEVENTF_EXTENDEDKEY)
EXTENDED_FLAG = 0x0001

# VK, которые Windows считает «расширенными» (без флага ломаются стрелки и т.п.)
EXTENDED_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
               0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0x90,
               0xA3, 0xA5}

MODIFIER_MASK = 0x0004  # Control
SHIFT_MASK = 0x0001


@lru_cache(maxsize=256)
def _vk_for_ascii(ch: str) -> tuple[int, bool] | None:
    """VK для ASCII-символа через нативную таблицу Windows (учитывает раскладку)."""
    if not ch or ord(ch) > 0x7E:
        return None
    try:
        res = _user32.VkKeyScanW(ord(ch))
    except Exception:
        return None
    if res == -1:
        return None
    vk = res & 0xFF
    return vk, vk in EXTENDED_VK


def keysym_to_vk(keysym: str) -> tuple[int, bool] | None:
    """Возвращает (VK, extended) для именованной клавиши или одиночного символа."""
    hit = KEYSYM_TO_VK.get(keysym)
    if hit is not None:
        return hit
    if len(keysym) == 1:
        return _vk_for_ascii(keysym)
    return None
