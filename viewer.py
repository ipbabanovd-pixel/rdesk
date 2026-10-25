"""Вьювер rdesk: вход на сервер, список компьютеров и удалённый рабочий стол."""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Optional

from . import __version__, capture, crypto, keys, protocol, relay_proto, ui_theme
from .host_agent import DEFAULT_SERVER_PORT, split_server
from .relay_proto import AuthError, RelayError

DEFAULT_SCALE_NAME = "66 %"
SCALE_CHOICES = [("100 %", 1.0), ("75 %", 0.75), ("66 %", 0.66),
                 ("50 %", 0.5), ("33 %", 0.33)]
MODIFIER_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Caps_Lock", "Num_Lock",
}
CTRL_STATE = 0x0004
ALT_STATE = 0x0008


def _human_error(exc: Exception) -> str:
    if isinstance(exc, (AuthError, protocol.AuthError)):
        return f"Неверный пароль сервера или доступ отклонён.\n{exc}"
    if isinstance(exc, socket.gaierror):
        return "Не удалось найти сервер по этому адресу. Проверьте имя или IP."
    if isinstance(exc, ConnectionRefusedError):
        return ("Сервер отклонил подключение. Проверьте адрес, порт и что rdesk server "
                "запущен.")
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return "Сервер не ответил вовремя. Проверьте адрес и порт."
    if isinstance(exc, ConnectionError):
        return f"Соединение с сервером потеряно.\n{exc}"
    if isinstance(exc, RelayError):
        return f"Ошибка протокола сервера.\n{exc}"
    return f"{type(exc).__name__}: {exc}"


# ==========================================================================
# сетевые клиенты
# ==========================================================================

class ViewerClient:
    """Сквозной сеанс с хостом через уже открытый канал данных."""

    def __init__(self, password: str, settings: dict, events: queue.Queue,
                 session_key: str = "") -> None:
        self.password = password
        self.session_key = session_key
        self.settings = dict(settings)
        self.events = events
        self.host_name = ""
        self.remote_size = (0, 0)
        self.latest_frame: Optional[protocol.Frame] = None
        self.frame_lock = threading.Lock()
        self._conn: Optional[protocol.Conn] = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False

    def connect_socket(self, sock: socket.socket, timeout: float = 15.0) -> str:
        try:
            sock.settimeout(timeout)
            secret = self.session_key or self.password
            iterations = 1 if self.session_key else crypto.PBKDF2_ITERATIONS
            conn = protocol.client_handshake(sock, secret,
                                              socket.gethostname() or "viewer",
                                              iterations=iterations)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise
        self._conn = conn
        self.host_name = getattr(conn, "host_name", "") or "компьютер"
        conn.send_json(protocol.Msg.CFG, self.settings)
        threading.Thread(target=self._reader, name="viewer-session", daemon=True).start()
        return self.host_name

    def _reader(self) -> None:
        assert self._conn is not None
        try:
            while not self._stop.is_set():
                mtype, payload = self._conn.recv()
                if mtype is protocol.Msg.FRAME:
                    frame = protocol.unpack_frame(payload)
                    with self.frame_lock:
                        self.latest_frame = frame
                elif mtype is protocol.Msg.SCREEN:
                    try:
                        info = json.loads(payload.decode("utf-8"))
                        self.remote_size = (int(info.get("w", 0)), int(info.get("h", 0)))
                        self.events.put(("screen", info))
                    except (UnicodeDecodeError, ValueError, TypeError):
                        pass
                elif mtype is protocol.Msg.ERROR:
                    self.events.put(("error", payload.decode("utf-8", "replace")))
                elif mtype is protocol.Msg.CLIP:
                    self.events.put(("clipboard", payload.decode("utf-8", "replace")))
                elif mtype is protocol.Msg.BYE:
                    self.events.put(("closed", "хост завершил сеанс"))
                    break
                elif mtype is protocol.Msg.PONG:
                    continue
        except (OSError, ConnectionError, protocol.ProtocolError, ValueError) as exc:
            if not self._stop.is_set():
                self.events.put(("closed", str(exc) or type(exc).__name__))
        finally:
            self.close()

    def _send(self, mtype: protocol.Msg, payload: bytes = b"") -> None:
        conn = self._conn
        if conn is None or self._stop.is_set():
            return
        with self._send_lock:
            try:
                conn.send(mtype, payload)
            except (OSError, ConnectionError, protocol.ProtocolError):
                self._stop.set()

    def send_mouse(self, kind: int, button: int, wheel: int, x: int, y: int) -> None:
        self._send(protocol.Msg.INPUT_MOUSE, protocol.pack_mouse(kind, button, wheel, x, y))

    def send_key(self, vk: int, scan: int, flags: int, down: bool) -> None:
        self._send(protocol.Msg.INPUT_KEY, protocol.pack_key(vk, scan, flags, down))

    def send_unicode(self, text: str) -> None:
        units = protocol.utf16_units(text)
        if not units:
            return
        for unit in units:
            self._send(protocol.Msg.INPUT_KEY, protocol.pack_key(0, unit, 0, True))
        for unit in units:
            self._send(protocol.Msg.INPUT_KEY, protocol.pack_key(0, unit, 0, False))

    def send_ack(self, frame_id: int) -> None:
        self._send(protocol.Msg.ACK, struct.pack(">I", frame_id))

    def request_keyframe(self) -> None:
        self._send(protocol.Msg.KEYFRAME)

    def send_clipboard(self, text: str) -> None:
        self._send(protocol.Msg.CLIP, text.encode("utf-8")[: 1024 * 1024])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.send(protocol.Msg.BYE)
            except (OSError, protocol.ProtocolError):
                pass
            conn.close()


class RelayViewer:
    """Управляющее подключение вьювера к серверу и список компьютеров."""

    def __init__(self, host: str, port: int, password: str, events: queue.Queue,
                 name: str = "") -> None:
        self.host = host
        self.port = int(port)
        self.password = password
        self.events = events
        self.name = (name or socket.gethostname() or "viewer")[:64]
        self.server_name = ""
        self.hosts: list[dict] = []
        self._conn: Optional[relay_proto.ControlChannel] = None
        self._stop = threading.Event()
        self._closed = False
        self._ping_stop = threading.Event()
        self._send_lock = threading.Lock()

    def connect(self, timeout: float = 15.0) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            conn = relay_proto.client_login(sock, "viewer", self.name, self.password)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise
        self._conn = conn
        threading.Thread(target=self._reader, name="viewer-relay", daemon=True).start()
        threading.Thread(target=self._ping_loop, name="viewer-relay-ping",
                         daemon=True).start()

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._stop.is_set()

    def _reader(self) -> None:
        assert self._conn is not None
        try:
            while not self._stop.is_set():
                message = self._conn.recv()
                mtype = message.get("type")
                if mtype == "welcome":
                    self.server_name = str(message.get("server") or self.host)
                    self.events.put(("welcome", message))
                elif mtype == "host_list":
                    hosts = message.get("hosts")
                    self.hosts = list(hosts) if isinstance(hosts, list) else []
                    self.events.put(("hosts", self.hosts))
                elif mtype == "session_ready":
                    self.events.put(("session_ready", message))
                elif mtype == "session_error":
                    self.events.put(("session_error", str(message.get("error") or "")))
                elif mtype == "session_closed":
                    self.events.put(("session_closed", message))
                elif mtype == "pong":
                    continue
                elif mtype == "error":
                    self.events.put(("error", str(message.get("error") or "")))
        except (OSError, ConnectionError, RelayError) as exc:
            if not self._stop.is_set():
                self.events.put(("closed", str(exc) or type(exc).__name__))
        finally:
            self.close()

    def _ping_loop(self) -> None:
        while not self._ping_stop.wait(15.0):
            try:
                self.send("ping", time=int(time.time()))
            except (OSError, ConnectionError, RelayError):
                return

    def send(self, mtype: str, **fields: Any) -> None:
        conn = self._conn
        if conn is None:
            raise ConnectionError("нет связи с сервером")
        with self._send_lock:
            conn.send(mtype, **fields)

    def refresh(self) -> None:
        try:
            self.send("list")
        except (OSError, ConnectionError, RelayError):
            pass

    def request_session(self, host_id: str) -> None:
        self.send("connect", host_id=host_id)

    def cancel_session(self) -> None:
        try:
            self.send("cancel")
        except (OSError, ConnectionError, RelayError):
            pass

    def connect_data(self, token_hex: str, timeout: float = 15.0) -> socket.socket:
        try:
            token = bytes.fromhex(token_hex)
        except ValueError as exc:
            raise RelayError("некорректный токен сеанса") from exc
        if len(token) != relay_proto.DATA_TOKEN_LEN:
            raise RelayError("некорректная длина токена сеанса")
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            relay_proto.send_data_hello(sock, token)
            sock.settimeout(timeout)
        except Exception:
            sock.close()
            raise
        return sock

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._ping_stop.set()
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.send("bye")
            except (OSError, ConnectionError, RelayError):
                pass
            conn.close()


# ==========================================================================
# окно входа
# ==========================================================================

class LoginDialog:
    def __init__(self, app: "ViewerApp", first_login: bool) -> None:
        self.app = app
        self.first_login = first_login
        self.window = tk.Toplevel(app.root)
        self.window.title("rdesk viewer — вход на сервер")
        self.window.configure(bg=ui_theme.BG)
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._busy = False
        self._closed = False
        self._build()
        ui_theme.center_window(self.window, 520, 650)
        self.window.deiconify()
        self.window.lift()
        self.window.grab_set()
        self.window.after(80, lambda: self.host_entry.focus_set())
        self.window.bind("<Return>", lambda _e: self._do_connect())

    def _build(self) -> None:
        header = tk.Frame(self.window, bg=ui_theme.DARK, height=104)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="rdesk", bg=ui_theme.DARK, fg=ui_theme.WHITE,
                 font=("Segoe UI", 24, "bold")).pack(side=tk.LEFT, padx=(28, 0), pady=(18, 0))
        tk.Label(header, text=" viewer", bg=ui_theme.DARK, fg="#93c5fd",
                 font=("Segoe UI", 24)).pack(side=tk.LEFT, pady=(18, 0))
        tk.Label(header, text="подключение к серверу", bg=ui_theme.DARK,
                 fg=ui_theme.DARK_MUTED, font=("Segoe UI", 9)).pack(
                     side=tk.LEFT, padx=(12, 0), pady=(34, 0))

        body = tk.Frame(self.window, bg=ui_theme.CARD, padx=30, pady=24,
                        highlightbackground=ui_theme.BORDER, highlightthickness=1)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=18)

        tk.Label(body, text="Вход на сервер", bg=ui_theme.CARD, fg=ui_theme.TEXT,
                 font=("Segoe UI", 15, "bold"), anchor="w").pack(fill=tk.X)
        tk.Label(body, text="Укажите адрес rdesk server и пароль, который показывает "
                            "окно сервера.",
                 bg=ui_theme.CARD, fg=ui_theme.MUTED, font=("Segoe UI", 9),
                 justify="left", wraplength=400, anchor="w").pack(fill=tk.X, pady=(4, 18))

        tk.Label(body, text="Адрес сервера", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X)
        self.host_var = tk.StringVar(value=str(self.app.config.get("server_host", "")))
        self.host_entry = ttk.Entry(body, textvariable=self.host_var)
        self.host_entry.pack(fill=tk.X, pady=(4, 3))

        port_row = tk.Frame(body, bg=ui_theme.CARD)
        port_row.pack(fill=tk.X, pady=(12, 0))
        tk.Label(port_row, text="Порт", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=str(self.app.config.get(
            "server_port", DEFAULT_SERVER_PORT)))
        ttk.Entry(port_row, textvariable=self.port_var, width=9).pack(side=tk.LEFT,
                                                                     padx=(8, 10))
        tk.Label(port_row, text="по умолчанию 7443", bg=ui_theme.CARD,
                 fg=ui_theme.MUTED, font=("Segoe UI", 8)).pack(side=tk.LEFT, pady=6)

        tk.Label(body, text="Пароль сервера", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X, pady=(14, 0))
        pass_row = tk.Frame(body, bg=ui_theme.CARD)
        pass_row.pack(fill=tk.X, pady=(4, 4))
        self.pass_var = tk.StringVar(value=str(self.app.config.get("password", "")))
        self.pass_entry = ttk.Entry(pass_row, textvariable=self.pass_var, show="•")
        self.pass_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.show_var = tk.BooleanVar(value=False)
        ui_theme.CheckBox(pass_row, "Показать", self.show_var, bg=ui_theme.CARD,
                          fg=ui_theme.MUTED, command=self._toggle_password).pack(
                              side=tk.LEFT, padx=(8, 0))

        self.remember_var = tk.BooleanVar(value=bool(self.app.config.get("remember", True)))
        ui_theme.CheckBox(body, "Запомнить пароль на этом компьютере",
                          self.remember_var, bg=ui_theme.CARD).pack(fill=tk.X, pady=(8, 0))

        self.error_var = tk.StringVar(value="")
        self.error_label = tk.Label(body, textvariable=self.error_var, bg=ui_theme.CARD,
                                    fg=ui_theme.RED, font=("Segoe UI", 9),
                                    justify="left", wraplength=400, anchor="w")
        self.error_label.pack(fill=tk.X, pady=(10, 0))

        self.connect_btn = ttk.Button(body, text="Подключиться", style="Accent.TButton",
                                      command=self._do_connect)
        self.connect_btn.pack(fill=tk.X, pady=(16, 6))
        ttk.Button(body, text="Отмена", command=self._on_cancel).pack(fill=tk.X)
        tk.Label(body, text="Пароль передаётся только в виде криптографического "
                            "доказательства и не отправляется открытым текстом.",
                 bg=ui_theme.CARD, fg=ui_theme.MUTED, font=("Segoe UI", 8),
                 wraplength=400, justify="left", anchor="w").pack(fill=tk.X, pady=(14, 0))

    def _toggle_password(self) -> None:
        self.pass_entry.configure(show="" if self.show_var.get() else "•")

    def _do_connect(self) -> None:
        if self._busy:
            return
        raw = self.host_var.get().strip()
        if not raw:
            self._show_error("Укажите адрес сервера.")
            return
        try:
            default_port = int(self.port_var.get().strip() or DEFAULT_SERVER_PORT)
        except ValueError:
            self._show_error("Порт должен быть числом.")
            return
        host, port = split_server(raw, default_port)
        if not (1 <= port <= 65535):
            self._show_error("Порт должен быть в диапазоне 1…65535.")
            return
        password = self.pass_var.get().strip()
        if not password:
            self._show_error("Введите пароль сервера.")
            return
        self._busy = True
        self.connect_btn.configure(state=tk.DISABLED, text="Подключение…")
        self.error_var.set("")
        self.host_var.set(host)
        self.port_var.set(str(port))
        relay = RelayViewer(host, port, password, self.app.relay_events)
        remember = bool(self.remember_var.get())

        def worker() -> None:
            try:
                relay.connect(timeout=15.0)
            except Exception as exc:  # noqa: BLE001
                relay.close()
                self.window.after(0, lambda: self._failed(_human_error(exc)))
            else:
                self.window.after(0, lambda: self._succeeded(relay, host, port,
                                                             password, remember))

        threading.Thread(target=worker, name="viewer-login", daemon=True).start()

    def _failed(self, message: str) -> None:
        if self._closed:
            return
        self._busy = False
        self.connect_btn.configure(state=tk.NORMAL, text="Подключиться")
        self._show_error(message)

    def _show_error(self, message: str) -> None:
        self.error_var.set(message)

    def _succeeded(self, relay: RelayViewer, host: str, port: int,
                   password: str, remember: bool) -> None:
        if self._closed:
            relay.close()
            return
        self._closed = True
        self.app.on_login(relay, host, port, password, remember)
        self.window.grab_release()
        self.window.destroy()

    def _on_cancel(self) -> None:
        if self._busy:
            return
        self._closed = True
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()
        self.app.on_login_cancelled(self.first_login)

    def focus(self) -> None:
        try:
            self.window.lift()
            self.window.focus_force()
        except tk.TclError:
            pass


# ==========================================================================
# главное окно
# ==========================================================================

class ViewerApp:
    def __init__(self, root: tk.Tk, app_dir: str,
                 autoconnect: Optional[dict] = None) -> None:
        self.root = root
        self.app_dir = app_dir
        self.config = self._load_config()
        self.relay: Optional[RelayViewer] = None
        self.relay_events: queue.Queue = queue.Queue()
        self.session_events: queue.Queue = queue.Queue()
        self.client: Optional[ViewerClient] = None
        self.hosts: list[dict] = []
        self.server_host = str(self.config.get("server_host", ""))
        self.server_port = int(self.config.get("server_port", DEFAULT_SERVER_PORT))
        self.password = str(self.config.get("password", ""))
        self.login_dialog: Optional[LoginDialog] = None
        self._closing = False
        self._started_once = False
        self._pending_host: Optional[dict] = None
        self._opening = False
        self._epoch = 0
        self._timeout_job: Optional[str] = None
        self._buffer: Optional[bytearray] = None
        self._frame_size = (0, 0)
        self._display_size = (0, 0)
        self._offset = (0, 0)
        self._base_photo: Optional[tk.PhotoImage] = None
        self._photo: Optional[tk.PhotoImage] = None
        self._image_id: Optional[int] = None
        self._pressed_vks: set[int] = set()
        self._last_motion = 0.0
        self._first_frame = True
        self._frames = 0
        self._bytes = 0
        self._fps = 0.0
        self._fps_clock = time.perf_counter()
        self._view_only = bool(self.config.get("view_only", False))
        self._fullscreen = False
        self._connected = False
        self._remote_name = ""
        self._scale_factor = 1
        self._hidden = False
        self._poll_job: Optional[str] = None
        self._clip_job: Optional[str] = None
        self._clip_last = ""
        self._clip_remote = ""

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.root.after(10, self._poll)
        self._clip_job = self.root.after(600, self._clip_tick)
        if autoconnect:
            self.config.update(autoconnect)
            self.root.after(200, lambda: self._auto_login(autoconnect))
        else:
            self.root.withdraw()
            self.root.after(120, lambda: self.open_login(first_login=True))

    # ------------------------------------------------------------------
    # конфигурация
    # ------------------------------------------------------------------

    def _config_path(self) -> str:
        return os.path.join(self.app_dir, "config", "viewer.json")

    def _load_config(self) -> dict:
        data = {
            "server_host": "",
            "server_port": DEFAULT_SERVER_PORT,
            "password": "",
            "remember": True,
            "scale_name": DEFAULT_SCALE_NAME,
            "fps": 15,
            "view_only": False,
        }
        try:
            with open(self._config_path(), "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
        if data.get("scale_name") not in [name for name, _ in SCALE_CHOICES]:
            data["scale_name"] = DEFAULT_SCALE_NAME
        try:
            data["fps"] = max(1, min(30, int(data.get("fps", 15))))
        except (TypeError, ValueError):
            data["fps"] = 15
        try:
            data["server_port"] = int(data.get("server_port", DEFAULT_SERVER_PORT))
        except (TypeError, ValueError):
            data["server_port"] = DEFAULT_SERVER_PORT
        return data

    def _save_config(self) -> None:
        data = {
            "server_host": self.server_host or self.config.get("server_host", ""),
            "server_port": self.server_port,
            "password": self.password if self.config.get("remember", True) else "",
            "remember": bool(self.config.get("remember", True)),
            "scale_name": self.scale_var.get(),
            "fps": self.fps_var.get(),
            "view_only": bool(self.view_only_var.get()),
        }
        self.config.update(data)
        try:
            os.makedirs(os.path.dirname(self._config_path()), exist_ok=True)
            with open(self._config_path(), "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _log(self, text: str) -> None:
        path = os.path.join(self.app_dir, "logs", "viewer.log")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # интерфейс
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        ui_theme.apply_theme(self.root)
        self.root.title(f"rdesk viewer {__version__}")
        self.root.geometry("1260x780")
        self.root.minsize(980, 620)
        try:
            self.root.iconbitmap(os.path.join(self.app_dir, "assets", "rdesk.ico"))
        except tk.TclError:
            pass
        style = ttk.Style(self.root)
        style.configure("Sidebar.Treeview", background=ui_theme.SIDEBAR,
                        fieldbackground=ui_theme.SIDEBAR, foreground="#e2e8f0",
                        borderwidth=0, rowheight=40, font=("Segoe UI", 10))
        style.map("Sidebar.Treeview",
                  background=[("selected", "#1d4ed8")],
                  foreground=[("selected", "#ffffff")])
        style.configure("Sidebar.Treeview.Heading", background=ui_theme.SIDEBAR,
                        foreground="#94a3b8", relief="flat", borderwidth=0,
                        font=("Segoe UI", 8, "bold"))
        style.map("Sidebar.Treeview.Heading", background=[("active", ui_theme.SIDEBAR)])

        self.header = tk.Frame(self.root, bg=ui_theme.DARK, height=62)
        self.header.pack(side=tk.TOP, fill=tk.X)
        self.header.pack_propagate(False)
        brand = tk.Frame(self.header, bg=ui_theme.DARK)
        brand.pack(side=tk.LEFT, padx=(20, 0), pady=10)
        tk.Label(brand, text="rdesk", bg=ui_theme.DARK, fg=ui_theme.WHITE,
                 font=("Segoe UI", 17, "bold")).pack(side=tk.LEFT)
        tk.Label(brand, text=" viewer", bg=ui_theme.DARK, fg="#93c5fd",
                 font=("Segoe UI", 17)).pack(side=tk.LEFT)
        self.server_label = tk.Label(self.header, text="нет подключения",
                                     bg=ui_theme.DARK, fg=ui_theme.DARK_MUTED,
                                     font=("Segoe UI", 9))
        self.server_label.pack(side=tk.LEFT, padx=(16, 0), pady=(22, 0))
        self.link_status = tk.Label(self.header, text="● Нет связи", bg=ui_theme.DARK,
                                    fg="#f87171", font=("Segoe UI", 9, "bold"))
        self.link_status.pack(side=tk.RIGHT, padx=(0, 16))
        ttk.Button(self.header, text="Сменить сервер", style="Dark.TButton",
                   command=self._change_server).pack(side=tk.RIGHT, padx=(0, 12))

        self.body = tk.Frame(self.root, bg=ui_theme.BG)
        self.body.pack(fill=tk.BOTH, expand=True)

        self.sidebar = tk.Frame(self.body, bg=ui_theme.SIDEBAR, width=310)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)
        self._build_sidebar()

        self.main = tk.Frame(self.body, bg=ui_theme.BG)
        self.main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._build_main()

        self.status_bar = tk.Frame(self.root, bg="#e9edf5", height=28)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_bar.pack_propagate(False)
        self.status_var = tk.StringVar(value="Подключитесь к серверу")
        tk.Label(self.status_bar, textvariable=self.status_var, bg="#e9edf5",
                 fg=ui_theme.MUTED, font=("Segoe UI", 9), anchor="w").pack(
                     side=tk.LEFT, padx=14)
        self.stats_var = tk.StringVar(value="")
        tk.Label(self.status_bar, textvariable=self.stats_var, bg="#e9edf5",
                 fg=ui_theme.MUTED, font=("Segoe UI", 9)).pack(side=tk.RIGHT, padx=14)

        self.root.bind("<F11>", lambda _e: self._toggle_fullscreen())
        self.root.bind("<Escape>", lambda _e: self._escape())

    def _build_sidebar(self) -> None:
        top = tk.Frame(self.sidebar, bg=ui_theme.SIDEBAR)
        top.pack(fill=tk.X, padx=18, pady=(18, 8))
        tk.Label(top, text="КОМПЬЮТЕРЫ", bg=ui_theme.SIDEBAR, fg="#cbd5e1",
                 font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        self.count_label = tk.Label(top, text="0", bg="#1e293b", fg="#93c5fd",
                                    font=("Segoe UI", 9, "bold"), padx=7, pady=1)
        self.count_label.pack(side=tk.RIGHT)

        search_row = tk.Frame(self.sidebar, bg=ui_theme.SIDEBAR)
        search_row.pack(fill=tk.X, padx=14, pady=(4, 8))
        tk.Label(search_row, text="Поиск", bg="#1e293b", fg="#94a3b8",
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(8, 6), pady=7)
        self.filter_var = tk.StringVar()
        entry = tk.Entry(search_row, textvariable=self.filter_var, bg="#1e293b",
                         fg="#e2e8f0", insertbackground="#e2e8f0", relief="flat",
                         font=("Segoe UI", 9))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), pady=5)
        entry.bind("<KeyRelease>", lambda _e: self._render_hosts())
        self.search_entry = entry

        tree_frame = tk.Frame(self.sidebar, bg=ui_theme.SIDEBAR)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=(8, 4), pady=(0, 8))
        self.host_tree = ttk.Treeview(tree_frame, columns=("name", "user"),
                                      show="headings", selectmode="browse",
                                      style="Sidebar.Treeview")
        self.host_tree.heading("name", text="КОМПЬЮТЕР")
        self.host_tree.heading("user", text="ПОЛЬЗОВАТЕЛЬ")
        self.host_tree.column("name", width=170, minwidth=100, anchor="w")
        self.host_tree.column("user", width=105, minwidth=70, anchor="w")
        self.host_tree.tag_configure("busy", foreground="#94a3b8")
        self.host_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.host_tree.yview)
        self.host_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.host_tree.bind("<Double-1>", lambda _e: self._connect_selected())
        self.host_tree.bind("<<TreeviewSelect>>", lambda _e: self._selection_changed())

        self.sidebar_empty = tk.Label(
            self.sidebar, text="Пока нет компьютеров.\n\nУстановите rdesk host\n"
                               "и укажите адрес сервера.",
            bg=ui_theme.SIDEBAR, fg="#94a3b8", font=("Segoe UI", 9),
            justify="left",
        )
        self.sidebar_empty.place(x=30, y=220)

        bottom = tk.Frame(self.sidebar, bg=ui_theme.SIDEBAR)
        bottom.pack(fill=tk.X, padx=14, pady=(0, 16), side=tk.BOTTOM)
        self.connect_btn = ttk.Button(bottom, text="Подключиться", style="Accent.TButton",
                                      command=self._connect_selected, state=tk.DISABLED)
        self.connect_btn.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(bottom, text="Обновить список", style="Dark.TButton",
                   command=self._refresh_hosts).pack(fill=tk.X)
        tk.Label(bottom, text="Двойной щелчок по компьютеру\nсразу открывает сеанс.",
                 bg=ui_theme.SIDEBAR, fg="#64748b", font=("Segoe UI", 8),
                 justify="left").pack(fill=tk.X, pady=(10, 0))

    def _build_main(self) -> None:
        self.toolbar = tk.Frame(self.main, bg=ui_theme.CARD, height=54,
                                highlightbackground=ui_theme.BORDER, highlightthickness=1)
        self.toolbar.pack(side=tk.TOP, fill=tk.X)
        self.toolbar.pack_propagate(False)
        button_row = tk.Frame(self.toolbar, bg=ui_theme.CARD)
        button_row.pack(side=tk.LEFT, padx=10, pady=8)
        self.disconnect_btn = ttk.Button(button_row, text="Отключиться",
                                         style="Danger.TButton",
                                         command=self._disconnect_session,
                                         state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.refresh_btn = ttk.Button(button_row, text="Обновить экран",
                                      command=self._refresh_screen, state=tk.DISABLED)
        self.refresh_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.clip_btn = ttk.Button(button_row, text="Буфер обмена",
                                   command=self._send_clipboard, state=tk.DISABLED)
        self.clip_btn.pack(side=tk.LEFT)

        options = tk.Frame(self.toolbar, bg=ui_theme.CARD)
        options.pack(side=tk.RIGHT, padx=12, pady=8)
        tk.Label(options, text="Качество", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 4))
        self.scale_var = tk.StringVar(value=str(self.config.get("scale_name",
                                                                DEFAULT_SCALE_NAME)))
        ttk.Combobox(options, textvariable=self.scale_var, width=6, state="readonly",
                     values=[name for name, _ in SCALE_CHOICES]).pack(side=tk.LEFT,
                                                                      padx=(0, 10))
        tk.Label(options, text="FPS", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 4))
        self.fps_var = tk.StringVar(value=str(self.config.get("fps", 15)))
        ttk.Spinbox(options, from_=1, to=30, textvariable=self.fps_var,
                    width=4).pack(side=tk.LEFT, padx=(0, 10))
        self.view_only_var = tk.BooleanVar(value=bool(self.config.get("view_only", False)))
        ui_theme.CheckBox(options, "только смотреть", self.view_only_var,
                          bg=ui_theme.CARD).pack(side=tk.LEFT)

        self.canvas = tk.Canvas(self.main, bg="#0b1220", highlightthickness=0,
                                takefocus=True)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._on_canvas_resize())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", lambda e: self._on_button(e, 0, True))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._on_button(e, 0, False))
        self.canvas.bind("<ButtonPress-2>", lambda e: self._on_button(e, 2, True))
        self.canvas.bind("<ButtonRelease-2>", lambda e: self._on_button(e, 2, False))
        self.canvas.bind("<ButtonPress-3>", lambda e: self._on_button(e, 1, True))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self._on_button(e, 1, False))
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<KeyPress>", self._on_key_press)
        self.canvas.bind("<KeyRelease>", self._on_key_release)
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        self.canvas.delete("all")
        self._base_photo = None
        self._photo = None
        self._image_id = None
        width = max(self.canvas.winfo_width(), 600)
        height = max(self.canvas.winfo_height(), 400)
        self.canvas.create_text(
            width // 2, height // 2 - 24, text="Выберите компьютер слева",
            fill="#e2e8f0", font=("Segoe UI", 18, "bold"), tags="placeholder",
        )
        self.canvas.create_text(
            width // 2, height // 2 + 12,
            text="Сеанс откроется здесь. Список компьютеров обновляется автоматически.",
            fill="#64748b", font=("Segoe UI", 10), tags="placeholder",
        )
        self._image_id = None

    # ------------------------------------------------------------------
    # вход
    # ------------------------------------------------------------------

    def open_login(self, first_login: bool = False) -> None:
        if self.login_dialog is not None:
            self.login_dialog.focus()
            return
        self.root.withdraw()
        self.login_dialog = LoginDialog(self, first_login)

    def on_login(self, relay: RelayViewer, host: str, port: int,
                 password: str, remember: bool) -> None:
        self.login_dialog = None
        self.relay = relay
        self.server_host = host
        self.server_port = port
        # Пароль нужен для текущего сеанса, даже если его не сохраняют на диск.
        self.password = password
        self.config.update({"server_host": host, "server_port": port,
                            "password": password if remember else "",
                            "remember": remember})
        self._started_once = True
        self._save_config()
        self._log(f"Вход на сервер {host}:{port}")
        self.server_label.configure(text=f"{host}:{port} • {relay.server_name or 'сервер'}")
        self.link_status.configure(text="● Подключено", fg="#4ade80")
        self.status_var.set("Подключено к серверу, получаем список компьютеров…")
        self.root.deiconify()
        self.root.lift()
        self._refresh_hosts()

    def on_login_cancelled(self, first_login: bool) -> None:
        self.login_dialog = None
        if first_login and not self._started_once:
            self._quit()
            return
        self.root.deiconify()
        self.root.lift()
        if self.relay is None:
            self.status_var.set("Нет подключения к серверу")
            self._show_placeholder()
        else:
            self.root.deiconify()

    def _auto_login(self, data: dict) -> None:
        host = str(data.get("server_host") or "")
        if not host:
            self.root.withdraw()
            self.open_login(first_login=True)
            return
        password = str(data.get("password") or "")
        if not password:
            self.root.withdraw()
            self.open_login(first_login=True)
            return
        self.root.withdraw()
        # Используем тот же диалог, но сразу инициируем подключение.
        self.login_dialog = LoginDialog(self, True)
        self.login_dialog.host_var.set(host)
        self.login_dialog.port_var.set(str(data.get("server_port", DEFAULT_SERVER_PORT)))
        self.login_dialog.pass_var.set(password)
        self.login_dialog._do_connect()

    def _change_server(self) -> None:
        if self.client is not None:
            if not messagebox.askyesno(
                "Сменить сервер", "Текущий сеанс будет завершён. Продолжить?",
                parent=self.root,
            ):
                return
            self._disconnect_session()
        if self.relay is not None:
            self.relay.close()
            self.relay = None
        self.hosts = []
        self._render_hosts()
        self.link_status.configure(text="● Нет связи", fg="#f87171")
        self.server_label.configure(text="нет подключения")
        self.open_login(first_login=False)

    # ------------------------------------------------------------------
    # серверные события и список компьютеров
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.relay_events.get_nowait()
                self._handle_relay_event(kind, payload)
        except queue.Empty:
            pass
        try:
            while True:
                kind, payload = self.session_events.get_nowait()
                self._handle_session_event(kind, payload)
        except queue.Empty:
            pass
        self._render_latest()
        self._update_stats()
        if not self._closing:
            self._poll_job = self.root.after(10, self._poll)

    def _handle_relay_event(self, kind: str, payload: Any) -> None:
        if kind == "welcome":
            message = payload if isinstance(payload, dict) else {}
            name = str(message.get("server") or self.server_host)
            self.server_label.configure(text=f"{self.server_host}:{self.server_port} • {name}")
            self.link_status.configure(text="● Подключено", fg="#4ade80")
        elif kind == "hosts":
            self.hosts = list(payload or [])
            self._render_hosts()
            if not self._connected and not self._opening:
                self.status_var.set(f"Компьютеров онлайн: {len(self.hosts)}")
        elif kind == "session_ready":
            self._on_session_ready(payload if isinstance(payload, dict) else {})
        elif kind == "session_error":
            self._cancel_timeout()
            self._opening = False
            self._pending_host = None
            self.connect_btn.configure(state=tk.NORMAL)
            text = str(payload or "сервер не разрешил подключение")
            self.status_var.set(text)
            messagebox.showwarning("rdesk viewer", text, parent=self.root)
        elif kind == "session_closed":
            message = payload if isinstance(payload, dict) else {}
            reason = str(message.get("reason") or "сеанс завершён")
            if self.client is not None or self._opening:
                self._end_session(reason)
            elif self._pending_host:
                self._cancel_timeout()
                self._opening = False
                self._pending_host = None
                self.connect_btn.configure(state=tk.NORMAL)
                self.status_var.set(reason)
        elif kind == "error":
            self.status_var.set(str(payload))
        elif kind == "closed":
            # После «Сменить сервер» и выхода relay уже равен None — это
            # не потеря связи, а намеренное закрытие.
            if self.relay is None or self._closing:
                return
            self._on_relay_closed(str(payload))

    def _on_session_ready(self, message: dict) -> None:
        if not self._opening:
            return
        token = str(message.get("viewer_token") or "")
        session_key = str(message.get("session_key") or "")
        host = message.get("host") if isinstance(message.get("host"), dict) else {}
        self._cancel_timeout()
        self._pending_host = host
        host_name = str(host.get("name") or "компьютер")
        self.status_var.set(f"Открываем сеанс с «{host_name}»…")
        password = self.password
        settings = {
            "scale": dict(SCALE_CHOICES).get(self.scale_var.get(), 1.0),
            "fps": self._fps_setting(),
            "level": 1,
            "view_only": bool(self.view_only_var.get()),
        }
        epoch = self._epoch
        relay = self.relay
        if relay is None:
            self._end_session("нет связи с сервером")
            return

        def worker() -> None:
            client: Optional[ViewerClient] = None
            try:
                sock = relay.connect_data(token, timeout=20.0)
                client = ViewerClient(password, settings, self.session_events,
                                      session_key=session_key)
                name = client.connect_socket(sock, timeout=20.0)
            except Exception as exc:  # noqa: BLE001
                if client is not None:
                    client.close()
                self.session_events.put(("open_failed", _human_error(exc)))
                return
            if epoch != self._epoch:
                client.close()
                return
            self.session_events.put(("client_ready", (client, name)))

        threading.Thread(target=worker, name="viewer-open-session", daemon=True).start()

    def _handle_session_event(self, kind: str, payload: Any) -> None:
        if kind == "client_ready":
            client, name = payload
            if self._epoch != getattr(self, "_open_epoch", self._epoch):
                client.close()
                return
            self.client = client
            self._remote_name = name
            self._on_connected(name)
        elif kind == "screen":
            info = payload if isinstance(payload, dict) else {}
            if info.get("read_only"):
                self.status_var.set("Хост включил режим «только просмотр»")
        elif kind == "open_failed":
            self._log(f"Не удалось открыть сеанс: {payload}")
            self._end_session(str(payload))
            messagebox.showerror("Не удалось открыть сеанс", str(payload),
                                 parent=self.root)
        elif kind == "error":
            self.status_var.set(str(payload))
        elif kind == "clipboard":
            self._apply_remote_clipboard(str(payload))
        elif kind == "closed":
            self._end_session(str(payload))

    def _on_relay_closed(self, reason: str) -> None:
        if self._closing:
            return
        self.relay = None
        self.link_status.configure(text="● Нет связи", fg="#f87171")
        self.server_label.configure(text="соединение потеряно")
        self._log(f"Связь с сервером потеряна: {reason}")
        self.hosts = []
        self._render_hosts()
        if self.client is not None or self._opening:
            self._end_session("связь с сервером потеряна")
        self.status_var.set("Связь с сервером потеряна")
        messagebox.showerror(
            "Сервер недоступен",
            f"Соединение с сервером потеряно.\n\n{reason}\n\n"
            "Проверьте сервер и подключитесь снова.",
            parent=self.root,
        )
        self.open_login(first_login=False)

    def _render_hosts(self) -> None:
        selected = self.host_tree.selection()
        selected_id = selected[0] if selected else ""
        query = self.filter_var.get().strip().lower() if hasattr(self, "filter_var") else ""
        visible = []
        for host in self.hosts:
            haystack = " ".join(str(host.get(key, "")) for key in
                                ("name", "user", "os", "ip")).lower()
            if query and query not in haystack:
                continue
            visible.append(host)
        wanted = {str(host.get("id", "")) for host in visible}
        existing = set(self.host_tree.get_children())
        for iid in existing - wanted:
            self.host_tree.delete(iid)
        for host in visible:
            host_id = str(host.get("id", ""))
            if not host_id:
                continue
            name = str(host.get("name", "компьютер"))
            status = "  •  занят" if host.get("busy") else ""
            values = (f"● {name}{status}", str(host.get("user", "")))
            tags = ("busy",) if host.get("busy") else ()
            if host_id in existing:
                self.host_tree.item(host_id, values=values, tags=tags)
            else:
                self.host_tree.insert("", tk.END, iid=host_id, values=values, tags=tags)
        if selected_id and selected_id in self.host_tree.get_children():
            self.host_tree.selection_set(selected_id)
        count = len(self.hosts)
        self.count_label.configure(text=str(count))
        if visible:
            self.sidebar_empty.place_forget()
        else:
            text = ("Ничего не найдено.\nИзмените запрос." if query
                    else "Пока нет компьютеров.\n\nУстановите rdesk host\n"
                         "и укажите адрес сервера.")
            self.sidebar_empty.configure(text=text)
            self.sidebar_empty.place(x=30, y=220)
        self._selection_changed()

    def _selection_changed(self) -> None:
        selected = self.host_tree.selection()
        if not selected or self._connected or self._opening:
            self.connect_btn.configure(state=tk.DISABLED)
            return
        host = next((h for h in self.hosts if str(h.get("id")) == selected[0]), None)
        if host is None or host.get("busy"):
            self.connect_btn.configure(state=tk.DISABLED)
        else:
            self.connect_btn.configure(state=tk.NORMAL)

    def _refresh_hosts(self) -> None:
        if self.relay is not None:
            self.relay.refresh()
            self.status_var.set("Запрошен список компьютеров…")

    # ------------------------------------------------------------------
    # подключение и завершение сеанса
    # ------------------------------------------------------------------

    def _connect_selected(self) -> None:
        if self._connected or self._opening:
            return
        selected = self.host_tree.selection()
        if not selected:
            messagebox.showinfo("rdesk viewer", "Выберите компьютер в списке слева.",
                                parent=self.root)
            return
        host = next((h for h in self.hosts if str(h.get("id")) == selected[0]), None)
        if host is None:
            return
        if host.get("busy"):
            messagebox.showinfo("rdesk viewer", "На этом компьютере уже идёт сеанс.",
                                parent=self.root)
            return
        if self.relay is None:
            messagebox.showwarning("rdesk viewer", "Нет связи с сервером.",
                                   parent=self.root)
            return
        self._opening = True
        self._pending_host = host
        self._epoch += 1
        self._open_epoch = self._epoch
        self.connect_btn.configure(state=tk.DISABLED)
        self.disconnect_btn.configure(state=tk.DISABLED)
        self._show_placeholder_text(f"Подключение к «{host.get('name', '')}»…",
                                    "Ожидаем подтверждения сервера.")
        self.status_var.set(f"Запрос сеанса с «{host.get('name', '')}»…")
        try:
            self.relay.request_session(str(host.get("id")))
        except (OSError, ConnectionError, RelayError) as exc:
            self._end_session(_human_error(exc))
            return
        self._timeout_job = self.root.after(30000, self._session_timeout)

    def _session_timeout(self) -> None:
        if not self._opening:
            return
        self._timeout_job = None
        self._opening = False
        self._pending_host = None
        if self.relay is not None:
            self.relay.cancel_session()
        self.connect_btn.configure(state=tk.NORMAL)
        self._show_placeholder()
        self.status_var.set("Сервер не открыл сеанс за 30 секунд")
        messagebox.showwarning("rdesk viewer",
                               "Не удалось открыть сеанс за 30 секунд.\n"
                               "Проверьте, что хост на связи, и попробуйте снова.",
                               parent=self.root)

    def _cancel_timeout(self) -> None:
        if self._timeout_job is not None:
            try:
                self.root.after_cancel(self._timeout_job)
            except tk.TclError:
                pass
            self._timeout_job = None

    def _on_connected(self, host_name: str) -> None:
        self._opening = False
        self._connected = True
        self._pending_host = None
        self._first_frame = True
        self._buffer = None
        self._frame_size = (0, 0)
        self._display_size = (0, 0)
        self._pressed_vks.clear()
        try:
            self._clip_last = self.root.clipboard_get()
        except tk.TclError:
            self._clip_last = ""
        self._clip_remote = ""
        self.disconnect_btn.configure(state=tk.NORMAL)
        self.refresh_btn.configure(state=tk.NORMAL)
        self.clip_btn.configure(state=tk.NORMAL)
        self.connect_btn.configure(state=tk.DISABLED)
        self.canvas.focus_set()
        mode = "только просмотр" if self.view_only_var.get() else "полное управление"
        self.status_var.set(f"Подключено к «{host_name}» — {mode}")
        self.stats_var.set("")
        self.root.title(f"rdesk — {host_name}")
        self._log(f"Сеанс с «{host_name}» начат")
        if self._hidden:
            self.root.deiconify()
            self._hidden = False

    def _disconnect_session(self) -> None:
        if self.client is not None:
            self._release_keys()
            self.client.close()
        self._end_session("отключено пользователем")

    def _end_session(self, reason: str) -> None:
        self._cancel_timeout()
        self._epoch += 1
        client = self.client
        if client is not None:
            self._release_keys()
        self.client = None
        if client is not None:
            client.close()
            self._log(f"Сеанс завершён: {reason}")
        self._connected = False
        self._opening = False
        self._pending_host = None
        self._base_photo = None
        self._photo = None
        self._buffer = None
        self._frame_size = (0, 0)
        self._display_size = (0, 0)
        self.disconnect_btn.configure(state=tk.DISABLED)
        self.refresh_btn.configure(state=tk.DISABLED)
        self.clip_btn.configure(state=tk.DISABLED)
        self._selection_changed()
        self.status_var.set(f"Не подключено — {reason}" if reason else "Не подключено")
        self.stats_var.set("")
        self.root.title(f"rdesk viewer {__version__}")
        self._show_placeholder()

    def _on_canvas_resize(self) -> None:
        if self._base_photo is None:
            if not self._connected:
                self._show_placeholder()
            return
        self._fit_photo()

    # ------------------------------------------------------------------
    # кадры
    # ------------------------------------------------------------------

    def _render_latest(self) -> None:
        client = self.client
        if client is None:
            return
        with client.frame_lock:
            frame = client.latest_frame
            client.latest_frame = None
        if frame is None:
            return
        width, height = frame.width, frame.height
        if self._buffer is None or self._frame_size != (width, height):
            self._buffer = bytearray(width * height * 3)
            self._frame_size = (width, height)
        try:
            capture.decode_tiles(width, height, frame.tiles, canvas=self._buffer)
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"Ошибка кадра: {exc}")
            client.request_keyframe()
            return

        blob = b"P6\n%d %d\n255\n" % (width, height) + self._buffer
        try:
            base = tk.PhotoImage(data=blob, master=self.root)
        except tk.TclError:
            base = self._photo_via_file(blob)
        if base is None:
            return
        self._base_photo = base
        self._fit_photo()

        if self._first_frame:
            self._first_frame = False
            self._fit_window(width, height)
            self.canvas.delete("placeholder")

        self._frames += 1
        self._bytes += frame.raw_size
        self._update_fps()
        client.send_ack(frame.frame_id)

    def _fit_photo(self) -> None:
        base = self._base_photo
        if base is None:
            return
        width, height = self._frame_size
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        factor = 1
        if width > canvas_w or height > canvas_h:
            factor = max(
                1,
                (width + canvas_w - 1) // canvas_w,
                (height + canvas_h - 1) // canvas_h,
            )
        self._scale_factor = factor
        photo = base.subsample(factor) if factor > 1 else base
        self._photo = photo
        display_w = max(1, width // factor)
        display_h = max(1, height // factor)
        self._display_size = (display_w, display_h)
        offset_x = max(0, (canvas_w - display_w) // 2)
        offset_y = max(0, (canvas_h - display_h) // 2)
        self._offset = (offset_x, offset_y)
        if self._image_id is None:
            self.canvas.delete("placeholder")
            self._image_id = self.canvas.create_image(offset_x, offset_y, anchor=tk.NW,
                                                       image=photo)
        else:
            self.canvas.itemconfig(self._image_id, image=photo)
            self.canvas.coords(self._image_id, offset_x, offset_y)

    def _photo_via_file(self, blob: bytes) -> Optional[tk.PhotoImage]:
        path = os.path.join(self.app_dir, "config", "_frame.ppm")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(blob)
            return tk.PhotoImage(file=path, master=self.root)
        except (OSError, tk.TclError):
            self.status_var.set("Не удалось отобразить кадр")
            return None

    def _fit_window(self, width: int, height: int) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        target_w = min(max(1050, width + 340), screen_w - 40)
        target_h = min(max(720, height + 170), screen_h - 50)
        if self.root.state() == "normal":
            self.root.geometry(f"{int(target_w)}x{int(target_h)}")

    def _show_placeholder_text(self, title: str, subtitle: str) -> None:
        self.canvas.delete("all")
        self._image_id = None
        width = max(self.canvas.winfo_width(), 600)
        height = max(self.canvas.winfo_height(), 400)
        self.canvas.create_text(width // 2, height // 2 - 12, text=title,
                                fill="#e2e8f0", font=("Segoe UI", 16, "bold"),
                                tags="placeholder")
        self.canvas.create_text(width // 2, height // 2 + 20, text=subtitle,
                                fill="#64748b", font=("Segoe UI", 10),
                                tags="placeholder")

    def _update_fps(self) -> None:
        now = time.perf_counter()
        elapsed = now - self._fps_clock
        if elapsed >= 1.0:
            self._fps = self._frames / elapsed
            kbps = self._bytes / elapsed / 1024
            self._frames = 0
            self._bytes = 0
            self._fps_clock = now
            if self._connected and self.client is not None:
                width, height = self._frame_size
                mode = " • только просмотр" if self.view_only_var.get() else ""
                self.stats_var.set(f"{width}×{height} • {self._fps:.1f} fps • "
                                   f"{kbps:.0f} КБ/с{mode}")

    def _update_stats(self) -> None:
        # FPS считается только при поступлении кадров; отдельного таймера не нужно.
        pass

    # ------------------------------------------------------------------
    # ввод
    # ------------------------------------------------------------------

    @property
    def _can_input(self) -> bool:
        return (self._connected and self.client is not None
                and not self.view_only_var.get())

    def _to_remote(self, x: int, y: int) -> Optional[tuple[int, int]]:
        offset_x, offset_y = self._offset
        display_w, display_h = self._display_size
        local_x = x - offset_x
        local_y = y - offset_y
        if local_x < 0 or local_y < 0 or local_x >= display_w or local_y >= display_h:
            return None
        width, height = self._frame_size
        remote_x = int(local_x * width / max(1, display_w))
        remote_y = int(local_y * height / max(1, display_h))
        nx = int(remote_x * 65535 / max(1, width - 1))
        ny = int(remote_y * 65535 / max(1, height - 1))
        return min(65535, max(0, nx)), min(65535, max(0, ny))

    def _on_motion(self, event: tk.Event) -> None:
        if not self._can_input:
            return
        position = self._to_remote(event.x, event.y)
        if position is None:
            return
        now = time.perf_counter()
        if now - self._last_motion < 0.008:
            return
        self._last_motion = now
        self.client.send_mouse(protocol.MOUSE_MOVE, 0, 0, *position)  # type: ignore[union-attr]

    def _on_button(self, event: tk.Event, button: int, down: bool) -> None:
        if not self._can_input:
            return
        position = self._to_remote(event.x, event.y)
        if position is None:
            return
        kind = protocol.MOUSE_DOWN if down else protocol.MOUSE_UP
        self.client.send_mouse(kind, button, 0, *position)  # type: ignore[union-attr]
        self.canvas.focus_set()

    def _on_wheel(self, event: tk.Event) -> None:
        if not self._can_input:
            return
        delta = int(getattr(event, "delta", 0))
        if delta == 0:
            return
        position = self._to_remote(event.x, event.y)
        if position is not None:
            self.client.send_mouse(protocol.MOUSE_MOVE, 0, 0, *position)  # type: ignore[union-attr]
        self.client.send_mouse(protocol.MOUSE_WHEEL, 0, delta, 0, 0)  # type: ignore[union-attr]

    def _on_key_press(self, event: tk.Event) -> str:
        if event.keysym == "F11":
            self._toggle_fullscreen()
            return "break"
        if event.keysym == "Escape" and self._fullscreen:
            self._toggle_fullscreen()
            return "break"
        if not self._can_input:
            return "break"
        self._send_key_event(event, down=True)
        return "break"

    def _on_key_release(self, event: tk.Event) -> str:
        if self._can_input:
            self._send_key_event(event, down=False)
        return "break"

    def _send_key_event(self, event: tk.Event, down: bool) -> None:
        client = self.client
        if client is None:
            return
        keysym = event.keysym
        hit = keys.keysym_to_vk(keysym)
        char = event.char
        ctrl_or_alt = bool(event.state & (CTRL_STATE | ALT_STATE))
        if keysym in MODIFIER_KEYSYMS:
            if hit is not None:
                vk, extended = hit
                client.send_key(vk, 0, keys.EXTENDED_FLAG if extended else 0, down)
            return
        if down and char and len(char) == 1 and ord(char) >= 32 and not ctrl_or_alt:
            client.send_unicode(char)
            return
        if hit is not None:
            vk, extended = hit
            client.send_key(vk, 0, keys.EXTENDED_FLAG if extended else 0, down)
            if down:
                self._pressed_vks.add(vk)
            else:
                self._pressed_vks.discard(vk)
            return
        if down and char:
            client.send_unicode(char)

    def _release_keys(self) -> None:
        client = self.client
        if client is None:
            self._pressed_vks.clear()
            return
        for vk in list(self._pressed_vks):
            client.send_key(vk, 0, 0, False)
        self._pressed_vks.clear()

    # ------------------------------------------------------------------
    # действия
    # ------------------------------------------------------------------

    def _clip_tick(self) -> None:
        """Автоматически отправляет изменения локального буфера в сеанс."""
        if self.client is not None and self._connected and not self._opening:
            try:
                text = self.root.clipboard_get()
            except tk.TclError:
                text = ""
            if text and text != self._clip_last and text != self._clip_remote:
                if not self.view_only_var.get() and len(text.encode("utf-8")) <= 1024 * 1024:
                    self.client.send_clipboard(text)
                    self._clip_last = text
        if not self._closing:
            self._clip_job = self.root.after(450, self._clip_tick)

    def _apply_remote_clipboard(self, text: str) -> None:
        if not text or len(text.encode("utf-8")) > 1024 * 1024:
            return
        self._clip_remote = text
        self._clip_last = text
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError:
            return
        self.status_var.set("Буфер обмена синхронизирован")

    def _refresh_screen(self) -> None:
        if self.client is not None:
            self.client.request_keyframe()

    def _send_clipboard(self) -> None:
        if self.client is None:
            return
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("Буфер пуст", "В буфере обмена нет текста.",
                                parent=self.root)
            return
        if not text:
            return
        self.client.send_clipboard(text)
        self._clip_last = text
        self.status_var.set(f"В буфер обмена хоста отправлено {len(text)} символов")

    def _fps_setting(self) -> int:
        try:
            return max(1, min(30, int(self.fps_var.get())))
        except (ValueError, tk.TclError):
            return 15

    def _toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        if self._fullscreen:
            self.header.pack_forget()
            self.sidebar.pack_forget()
            self.toolbar.pack_forget()
            self.status_bar.pack_forget()
            self.root.attributes("-fullscreen", True)
            self.canvas.focus_set()
        else:
            self.root.attributes("-fullscreen", False)
            self.header.pack(side=tk.TOP, fill=tk.X, before=self.body)
            self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
            self.sidebar.pack(side=tk.LEFT, fill=tk.Y, before=self.main)
            self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self.canvas)

    def _escape(self) -> None:
        if self._fullscreen:
            self._toggle_fullscreen()

    # ------------------------------------------------------------------
    # закрытие
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        if self._connected or self._opening:
            if not messagebox.askyesno(
                "Выход", "Сеанс будет завершён. Выйти из rdesk viewer?",
                parent=self.root,
            ):
                return
        self._quit()

    def _quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        if self._clip_job is not None:
            try:
                self.root.after_cancel(self._clip_job)
            except tk.TclError:
                pass
            self._clip_job = None
        self._cancel_timeout()
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass
            self.client = None
        if self.relay is not None:
            try:
                self.relay.close()
            except Exception:  # noqa: BLE001
                pass
            self.relay = None
        self._save_config()
        self.root.destroy()


# ==========================================================================
# точка входа
# ==========================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rdesk-viewer",
                                     description="rdesk viewer — удалённый доступ")
    parser.add_argument("--server", help="адрес сервера rdesk (host или host:port)")
    parser.add_argument("--port", type=int, help="порт сервера")
    parser.add_argument("--password", help="пароль сервера")
    parser.add_argument("--name", help="имя вьювера")
    return parser


def main(argv: Optional[list[str]] = None, app_dir: Optional[str] = None) -> int:
    args = build_parser().parse_args(argv)
    if app_dir is None:
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    autoconnect = None
    if args.server:
        host, port = split_server(args.server, args.port or DEFAULT_SERVER_PORT)
        if args.port:
            port = args.port
        autoconnect = {"server_host": host, "server_port": port,
                       "password": args.password or ""}
    capture.enable_dpi_awareness()
    log_dir = os.path.join(app_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    pid_path = os.path.join(log_dir, "viewer.pid")
    try:
        with open(pid_path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pid_path = ""
    root = tk.Tk()
    root.withdraw()
    app = ViewerApp(root, app_dir, autoconnect=autoconnect)
    root.mainloop()
    if pid_path:
        try:
            os.remove(pid_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
