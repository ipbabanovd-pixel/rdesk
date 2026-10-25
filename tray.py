"""Значок в области уведомлений Windows (tray) — без сторонних библиотек.

Зачем: хост в установленном виде работает без консольного окна, и человек за
компьютером должен видеть, что программа запущена и что к нему подключились.
Поэтому значок висит постоянно, а при подключении/отключении клиента
показывает всплывающее уведомление.

Реализация: Shell_NotifyIconW + скрытое окно с собственным циклом сообщений
в отдельном потоке.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIF_MESSAGE = 0x0001
NIF_ICON = 0x0002
NIF_TIP = 0x0004
NIF_INFO = 0x0010
NIF_SHOWTIP = 0x0080

NIM_ADD = 0x0000
NIM_MODIFY = 0x0001
NIM_DELETE = 0x0002
NIM_SETVERSION = 0x0004
NOTIFYICON_VERSION_4 = 4

NIIF_INFO = 0x0001
NIIF_NOSOUND = 0x0010

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020
TPM_LEFTALIGN = 0x0000

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512

WM_QUIT = 0x0012

WNDPROC = ctypes.WINFUNCTYPE(wt.LPARAM, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wt.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND), ("message", wt.UINT),
        ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
        ("time", wt.DWORD), ("pt", wt.POINT),
    ]


user32.RegisterClassExW.argtypes = (ctypes.POINTER(WNDCLASSEXW),)
user32.RegisterClassExW.restype = wt.WORD
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = (
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
)
user32.DefWindowProcW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.restype = wt.LPARAM
user32.DestroyWindow.argtypes = (wt.HWND,)
user32.PostQuitMessage.argtypes = (ctypes.c_int,)
user32.GetMessageW.argtypes = (ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT)
user32.TranslateMessage.argtypes = (ctypes.POINTER(MSG),)
user32.DispatchMessageW.argtypes = (ctypes.POINTER(MSG),)
user32.PostMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.LoadImageW.restype = wt.HANDLE
user32.LoadImageW.argtypes = (wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                              ctypes.c_int, ctypes.c_int, wt.UINT)
user32.LoadIconW.restype = wt.HICON
user32.LoadIconW.argtypes = (wt.HINSTANCE, wt.LPCWSTR)
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = (wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)
user32.TrackPopupMenu.argtypes = (wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.HWND, wt.LPVOID)
user32.GetCursorPos.argtypes = (ctypes.POINTER(wt.POINT),)
user32.SetForegroundWindow.argtypes = (wt.HWND,)
user32.DestroyMenu.argtypes = (wt.HMENU,)
shell32.Shell_NotifyIconW.argtypes = (wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
shell32.Shell_NotifyIconW.restype = wt.BOOL


class TrayIcon:
    """Значок в трее с меню и всплывающими уведомлениями.

    on_exit   — вызывается при выборе «Выход» в меню (в потоке трея!)
    on_open   — двойной щелчок по значку
    """

    MENU_OPEN = 1001
    MENU_EXIT = 1002

    def __init__(self, tooltip: str, icon_path: str | None = None,
                 on_exit=None, on_open=None,
                 open_label: str = "Открыть настройки",
                 exit_label: str = "Выход") -> None:
        self.tooltip = tooltip[:127]
        self.icon_path = icon_path
        self.on_exit = on_exit
        self.on_open = on_open
        self.open_label = open_label
        self.exit_label = exit_label
        self._hwnd: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._class_name = f"rdeskTray{os.getpid()}_{id(self)}"
        self._wndproc_ref = WNDPROC(self._wndproc)  # держим ссылку от сборки мусора
        self._hicon = None
        self._nid: NOTIFYICONDATAW | None = None
        self._lock = threading.Lock()

    # --- публичное ---

    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._hwnd is not None

    @property
    def error(self) -> str | None:
        return self._error

    def set_tooltip(self, text: str) -> None:
        self.tooltip = text[:127]
        self._modify(NIF_TIP)

    def notify(self, title: str, message: str, *, sound: bool = True) -> None:
        nid = self._nid
        if nid is None:
            return
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        nid.dwInfoFlags = NIIF_INFO | (0 if sound else NIIF_NOSOUND)
        nid.uVersion = 10000  # время показа, мс
        self._modify(NIF_INFO)

    def stop(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    # --- внутреннее ---

    def _modify(self, flags: int) -> None:
        nid = self._nid
        if nid is None:
            return
        with self._lock:
            nid.uFlags = flags
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _load_icon(self) -> int | None:
        if self.icon_path and os.path.exists(self.icon_path):
            handle = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if handle:
                return handle
        # IDI_APPLICATION передаётся как MAKEINTRESOURCE (число в указателе)
        resource = ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wt.LPCWSTR)
        return user32.LoadIconW(None, resource)

    def _run(self) -> None:
        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinstance
        wc.lpszClassName = self._class_name
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            self._error = f"RegisterClassExW: {ctypes.get_last_error()}"
            self._ready.set()
            return
        hwnd = user32.CreateWindowExW(0, self._class_name, "rdesk", 0, 0, 0, 0, 0,
                                      None, None, hinstance, None)
        if not hwnd:
            self._error = f"CreateWindowExW: {ctypes.get_last_error()}"
            self._ready.set()
            return
        self._hwnd = hwnd

        self._hicon = self._load_icon()
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._error = f"Shell_NotifyIcon(NIM_ADD): {ctypes.get_last_error()}"
            user32.DestroyWindow(hwnd)
            self._ready.set()
            return
        nid.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
        self._nid = nid
        self._ready.set()

        msg = MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0 or result == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _show_menu(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, self.MENU_OPEN, self.open_label)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, self.MENU_EXIT, self.exit_label)
            point = wt.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            command = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_LEFTALIGN,
                point.x, point.y, 0, hwnd, None,
            )
            if command:
                user32.PostMessageW(hwnd, WM_COMMAND, command, 0)
        finally:
            user32.DestroyMenu(menu)

    def _wndproc(self, hwnd, message, wparam, lparam):  # noqa: ANN001
        if message == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                if self.on_open:
                    try:
                        self.on_open()
                    except Exception:  # noqa: BLE001
                        pass
            elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
            return 0
        if message == WM_COMMAND:
            command = wparam & 0xFFFF
            if command == self.MENU_EXIT:
                if self.on_exit:
                    try:
                        self.on_exit()
                    except Exception:  # noqa: BLE001
                        pass
            elif command == self.MENU_OPEN:
                if self.on_open:
                    try:
                        self.on_open()
                    except Exception:  # noqa: BLE001
                        pass
            return 0
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            if self._nid is not None:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                self._nid = None
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)
