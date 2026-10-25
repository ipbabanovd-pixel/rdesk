"""Окно хоста rdesk и запуск агента."""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import socket
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from . import __version__, capture, host as host_core, ui_theme
from .host_agent import (DEFAULT_SERVER_PORT, HostAgent, is_configured,
                         load_config, save_config, server_display, split_server)

STATE_STYLE = {
    "online": ("● Подключено", ui_theme.GREEN, ui_theme.GREEN_LIGHT),
    "connecting": ("● Подключение…", ui_theme.AMBER, ui_theme.AMBER_LIGHT),
    "offline": ("● Нет связи", ui_theme.RED, ui_theme.RED_LIGHT),
    "auth_error": ("● Ошибка пароля", ui_theme.RED, ui_theme.RED_LIGHT),
    "disabled": ("● Не настроено", ui_theme.MUTED, "#eef1f6"),
}


class HostApp:
    def __init__(self, root: tk.Tk, app_dir: str, start_hidden: bool = False) -> None:
        self.root = root
        self.app_dir = app_dir
        self.config = load_config(app_dir)
        self.events: queue.Queue = queue.Queue()
        self.agent: Optional[HostAgent] = None
        self._tray = None
        self._closing = False
        self._tray_queue: queue.Queue = queue.Queue()
        self._state = "disabled"
        self._session_count = 0
        self._start_hidden = start_hidden
        self._poll_job: Optional[str] = None

        host_core.setup_logging(os.path.join(app_dir, "logs"))
        self._build_ui()
        self._setup_tray()
        self._start_agent()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.root.after(100, self._poll)
        if start_hidden and is_configured(self.config):
            self.root.withdraw()
            self._hidden = True
        else:
            self._hidden = False
            self._center()

    # ------------------------------------------------------------------
    # интерфейс
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.root.title(f"rdesk host {__version__} — этот компьютер")
        self.root.geometry("900x680")
        self.root.minsize(820, 600)
        try:
            self.root.iconbitmap(os.path.join(self.app_dir, "assets", "rdesk.ico"))
        except tk.TclError:
            pass

        header = tk.Frame(self.root, bg=ui_theme.DARK, height=72)
        header.pack(side=tk.TOP, fill=tk.X)
        header.pack_propagate(False)
        brand = tk.Frame(header, bg=ui_theme.DARK)
        brand.pack(side=tk.LEFT, padx=(22, 0), pady=12)
        tk.Label(brand, text="rdesk", bg=ui_theme.DARK, fg=ui_theme.WHITE,
                 font=("Segoe UI", 19, "bold")).pack(side=tk.LEFT)
        tk.Label(brand, text=" host", bg=ui_theme.DARK, fg="#93c5fd",
                 font=("Segoe UI", 19)).pack(side=tk.LEFT)
        tk.Label(header, text="удалённый доступ к этому компьютеру",
                 bg=ui_theme.DARK, fg=ui_theme.DARK_MUTED,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(12, 0), pady=(24, 0))
        self.header_status = tk.Label(header, text="● Не настроено", bg=ui_theme.DARK,
                                      fg="#fbbf24", font=("Segoe UI", 10, "bold"))
        self.header_status.pack(side=tk.RIGHT, padx=22)

        body = ttk.Frame(self.root, padding=(18, 16, 18, 10))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, minsize=360, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_settings(body)
        self._build_status(body)

        self.status_var = tk.StringVar(value="Готово")
        bar = tk.Frame(self.root, bg="#e9edf5", height=28)
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        bar.pack_propagate(False)
        tk.Label(bar, textvariable=self.status_var, bg="#e9edf5", fg=ui_theme.MUTED,
                 font=("Segoe UI", 9), anchor="w").pack(side=tk.LEFT, padx=14)
        tk.Label(bar, text=f"rdesk {__version__}", bg="#e9edf5", fg=ui_theme.MUTED,
                 font=("Segoe UI", 9)).pack(side=tk.RIGHT, padx=14)

    def _build_settings(self, parent: ttk.Frame) -> None:
        card = tk.Frame(parent, bg=ui_theme.CARD, highlightbackground=ui_theme.BORDER,
                        highlightthickness=1, padx=22, pady=20)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 16))

        tk.Label(card, text="Подключение к серверу", bg=ui_theme.CARD, fg=ui_theme.TEXT,
                 font=("Segoe UI", 15, "bold"), anchor="w").pack(fill=tk.X)
        tk.Label(card, text="Хост сам подключается к серверу. Входящие порты на этом "
                            "компьютере открывать не нужно.",
                 bg=ui_theme.CARD, fg=ui_theme.MUTED, font=("Segoe UI", 9),
                 justify="left", wraplength=312, anchor="w").pack(fill=tk.X, pady=(4, 16))

        tk.Label(card, text="Адрес сервера", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X)
        self.server_var = tk.StringVar(value=str(self.config.get("server_host", "")))
        server_entry = ttk.Entry(card, textvariable=self.server_var)
        server_entry.pack(fill=tk.X, pady=(4, 3))
        tk.Label(card, text="IP-адрес или имя, например 203.0.113.10 или rdesk.example.ru. "
                            "Можно указать сразу с портом.",
                 bg=ui_theme.CARD, fg=ui_theme.MUTED, font=("Segoe UI", 8),
                 justify="left", wraplength=312, anchor="w").pack(fill=tk.X)

        row = ttk.Frame(card)
        row.pack(fill=tk.X, pady=(13, 0))
        port_col = ttk.Frame(row)
        port_col.pack(side=tk.LEFT)
        tk.Label(port_col, text="Порт", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X)
        self.port_var = tk.StringVar(value=str(self.config.get("server_port", DEFAULT_SERVER_PORT)))
        ttk.Entry(port_col, textvariable=self.port_var, width=10).pack(anchor="w", pady=(4, 0))
        name_col = ttk.Frame(row)
        name_col.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(16, 0))
        tk.Label(name_col, text="Имя компьютера", bg=ui_theme.CARD,
                 fg=ui_theme.MUTED, font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X)
        self.name_var = tk.StringVar(value=str(self.config.get("host_name", "")))
        ttk.Entry(name_col, textvariable=self.name_var).pack(fill=tk.X, pady=(4, 0))

        tk.Label(card, text="Пароль сервера", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X, pady=(14, 0))
        pass_row = ttk.Frame(card)
        pass_row.pack(fill=tk.X, pady=(4, 0))
        self.pass_var = tk.StringVar(value=str(self.config.get("password", "")))
        self.pass_entry = ttk.Entry(pass_row, textvariable=self.pass_var, show="•")
        self.pass_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.show_var = tk.BooleanVar(value=False)
        ui_theme.CheckBox(pass_row, "Показать", self.show_var, bg=ui_theme.CARD,
                          fg=ui_theme.MUTED, command=self._toggle_password).pack(
                              side=tk.LEFT, padx=(8, 0))

        self.read_only_var = tk.BooleanVar(value=bool(self.config.get("read_only")))
        ui_theme.CheckBox(card, "Только просмотр (без управления)",
                          self.read_only_var,
                          bg=ui_theme.CARD).pack(fill=tk.X, pady=(14, 0))

        ttk.Button(card, text="Сохранить и подключиться", style="Accent.TButton",
                   command=self._save_and_connect).pack(fill=tk.X, pady=(20, 6))
        ttk.Button(card, text="Свернуть в трей", command=self._hide_to_tray).pack(fill=tk.X)
        ttk.Button(card, text="Выход", style="Danger.TButton",
                   command=self._ask_exit).pack(fill=tk.X, pady=(6, 0))

        tk.Label(card, text="Настройки: config\\host.json в папке установки",
                 bg=ui_theme.CARD, fg=ui_theme.MUTED, font=("Segoe UI", 8),
                 wraplength=312, justify="left", anchor="w").pack(fill=tk.X, pady=(14, 0))

    def _build_status(self, parent: ttk.Frame) -> None:
        right = ttk.Frame(parent)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        card = tk.Frame(right, bg=ui_theme.CARD, highlightbackground=ui_theme.BORDER,
                        highlightthickness=1, padx=18, pady=16)
        card.grid(row=0, column=0, sticky="ew")
        tk.Label(card, text="Состояние", bg=ui_theme.CARD, fg=ui_theme.TEXT,
                 font=("Segoe UI", 12, "bold"), anchor="w").pack(fill=tk.X)
        self.state_badge = tk.Label(card, text="● Не настроено", bg="#eef1f6",
                                    fg=ui_theme.MUTED, font=("Segoe UI", 10, "bold"),
                                    padx=10, pady=4, anchor="w")
        self.state_badge.pack(fill=tk.X, pady=(8, 8))
        self.detail_var = tk.StringVar(value="Укажите адрес сервера и пароль.")
        tk.Label(card, textvariable=self.detail_var, bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 wraplength=250, justify="left", anchor="w",
                 font=("Segoe UI", 9)).pack(fill=tk.X)
        self.session_var = tk.StringVar(value="Активных сеансов: 0")
        tk.Label(card, textvariable=self.session_var, bg=ui_theme.CARD, fg=ui_theme.TEXT,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill=tk.X, pady=(12, 0))
        tk.Label(card, text="При подключении вьювера появится уведомление в трее. "
                            "Значок rdesk виден всегда, пока хост работает.",
                 bg=ui_theme.CARD, fg=ui_theme.MUTED, font=("Segoe UI", 8),
                 wraplength=250, justify="left", anchor="w").pack(fill=tk.X, pady=(8, 0))

        log_card = tk.Frame(right, bg=ui_theme.CARD, highlightbackground=ui_theme.BORDER,
                            highlightthickness=1)
        log_card.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        head = tk.Frame(log_card, bg=ui_theme.CARD, padx=14, pady=10)
        head.pack(fill=tk.X)
        tk.Label(head, text="Журнал", bg=ui_theme.CARD, fg=ui_theme.TEXT,
                 font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        ttk.Button(head, text="Очистить", style="Link.TButton",
                   command=self._clear_log).pack(side=tk.RIGHT)
        self.log_text = ui_theme.make_log_text(log_card, height=9)
        self.log_text.master.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    def _center(self) -> None:
        self.root.update_idletasks()
        width, height = 900, 680
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    # ------------------------------------------------------------------
    # агент и события
    # ------------------------------------------------------------------

    def _start_agent(self) -> None:
        if self.agent is not None:
            return
        self.agent = HostAgent(
            self.app_dir, self.config,
            on_status=lambda data: self.events.put(("status", data)),
            on_log=lambda level, text: self.events.put(("log", (level, text))),
            on_session=lambda kind, data: self.events.put(("session", (kind, data))),
        )
        self.agent.start()
        host_core.log("Хост запущен")
        host_core.log(f"Сервер: {server_display(self.config)}")
        self._append_log("info", "Хост запущен")
        self._append_log("info", f"Сервер: {server_display(self.config)}")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self._apply_status(payload)
                elif kind == "log":
                    level, text = payload
                    self._append_log(level, text)
                elif kind == "session":
                    self._handle_session(*payload)
        except queue.Empty:
            pass
        try:
            while True:
                action = self._tray_queue.get_nowait()
                if action == "show":
                    self._show_window()
                elif action == "quit":
                    self._quit()
        except queue.Empty:
            pass
        if not self._closing:
            self._poll_job = self.root.after(120, self._poll)

    def _apply_status(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        state = str(data.get("state", "offline"))
        self._state = state
        text = str(data.get("text", ""))
        label, color, bg = STATE_STYLE.get(state, STATE_STYLE["offline"])
        self.header_status.configure(text=label, fg=color)
        self.state_badge.configure(text=label, fg=color, bg=bg)
        self.detail_var.set(text or "—")
        self.session_var.set(f"Активных сеансов: {int(data.get('sessions', 0))}")
        self._session_count = int(data.get("sessions", 0))
        self._update_tray()
        if state == "online":
            self.status_var.set(f"Подключено к {data.get('server', '')}")
        elif state == "auth_error":
            self.status_var.set("Сервер отклонил пароль — проверьте настройки")
        elif state == "disabled":
            self.status_var.set("Укажите сервер и пароль")
        else:
            self.status_var.set(text)

    def _handle_session(self, kind: str, data: dict) -> None:
        viewer = str(data.get("viewer") or "вьювер")
        if kind == "connected":
            self._append_log("ok", f"Подключился вьювер «{viewer}»")
            self.session_var.set(f"Активных сеансов: {max(1, self._session_count)}")
            if self._tray is not None:
                self._tray.notify("rdesk host — подключение",
                                  f"К этому компьютеру подключился «{viewer}»")
        elif kind == "closed":
            self._append_log("info", f"Вьювер «{viewer}» отключился")
        elif kind == "failed":
            self._append_log("error", f"Не удалось открыть сеанс: {data.get('error', '')}")
        elif kind == "opening":
            self._append_log("info", f"Запрос сеанса от «{viewer}»")
        self._update_tray()

    def _save_and_connect(self) -> None:
        server_raw = self.server_var.get().strip()
        if not server_raw:
            self._error("Укажите адрес сервера rdesk.")
            return
        port_raw = self.port_var.get().strip()
        try:
            default_port = int(port_raw) if port_raw else DEFAULT_SERVER_PORT
        except ValueError:
            self._error("Порт должен быть числом.")
            return
        host, port = split_server(server_raw, default_port)
        if not (1 <= port <= 65535):
            self._error("Порт должен быть в диапазоне 1…65535.")
            return
        password = self.pass_var.get().strip()
        if not password:
            self._error("Введите пароль сервера. Его показывает окно rdesk server.")
            return
        name = self.name_var.get().strip() or socket.gethostname()
        self.config.update({
            "server_host": host,
            "server_port": port,
            "password": password,
            "host_name": name,
            "read_only": bool(self.read_only_var.get()),
        })
        save_config(self.app_dir, self.config)
        self.server_var.set(host)
        self.port_var.set(str(port))
        self._append_log("ok", f"Настройки сохранены: {host}:{port}")
        self.status_var.set("Настройки сохранены, подключаемся…")
        if self.agent is None:
            self._start_agent()
        else:
            self.agent.apply_config(self.config)
        if self._hidden:
            self._show_window()

    def _toggle_password(self) -> None:
        self.pass_entry.configure(show="" if self.show_var.get() else "•")

    def _append_log(self, level: str, text: str) -> None:
        if not text:
            return
        ui_theme.log_append(self.log_text, text, level, time.strftime("%H:%M:%S"))

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def _error(self, message: str) -> None:
        messagebox.showerror("rdesk host", message, parent=self.root)

    # ------------------------------------------------------------------
    # трей, скрытие, выход
    # ------------------------------------------------------------------

    def _setup_tray(self) -> None:
        from .tray import TrayIcon

        icon = os.path.join(self.app_dir, "assets", "rdesk.ico")
        tray = TrayIcon(
            tooltip="rdesk host",
            icon_path=icon if os.path.exists(icon) else None,
            on_open=lambda: self._tray_queue.put("show"),
            on_exit=lambda: self._tray_queue.put("quit"),
            open_label="Открыть настройки",
            exit_label="Выход",
        )
        if tray.start():
            self._tray = tray
        else:
            self._tray = None
            self._append_log("warn", f"Значок в трее недоступен: {tray.error}")

    def _update_tray(self) -> None:
        if self._tray is None:
            return
        label = {"online": "подключено", "connecting": "подключение",
                 "auth_error": "ошибка пароля", "disabled": "не настроено"}.get(
                     self._state, "нет связи")
        self._tray.set_tooltip(f"rdesk host — {label}, сеансов: {self._session_count}")

    def _hide_to_tray(self) -> None:
        if self._tray is None:
            self._info("Значок в трее недоступен. Сверните окно обычным способом.")
            return
        self.root.withdraw()
        self._hidden = True
        self._tray.notify("rdesk host", "Хост продолжает работать. Значок rdesk — в трее.",
                          sound=False)

    def _show_window(self) -> None:
        self._hidden = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_close(self) -> None:
        if self._tray is None:
            if messagebox.askyesno("Выход", "Остановить хост и выйти?", parent=self.root):
                self._quit()
            return
        self._hide_to_tray()

    def _ask_exit(self) -> None:
        if self._session_count > 0:
            if not messagebox.askyesno(
                "Выход", "Сейчас идёт сеанс. Завершить его и остановить хост?",
                parent=self.root,
            ):
                return
        self._quit()

    def _info(self, message: str) -> None:
        messagebox.showinfo("rdesk host", message, parent=self.root)

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
        if self.agent is not None:
            self.agent.stop()
            self.agent = None
        if self._tray is not None:
            self._tray.stop()
        host_core.log("Хост остановлен пользователем")
        self.root.after(80, self.root.destroy)


# --------------------------------------------------------------------------
# точка входа
# --------------------------------------------------------------------------

def _single_instance() -> bool:
    """False, если хост уже запущен в этой сессии пользователя."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\rdesk-host-single-instance")
        if not handle:
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        # Хэндл намеренно не закрываем: он держит мьютекс до конца процесса.
        _SINGLE_HANDLE = handle  # noqa: F841
        return True
    except Exception:  # noqa: BLE001
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rdesk-host", description="rdesk host")
    parser.add_argument("--tray", action="store_true", help="запустить свёрнутым в трей")
    parser.add_argument("--server", help="адрес сервера, можно host:port")
    parser.add_argument("--password", help="пароль сервера")
    parser.add_argument("--name", help="имя компьютера в списке")
    parser.add_argument("--read-only", action="store_true", help="только просмотр")
    return parser


def main(argv: Optional[list[str]] = None, app_dir: Optional[str] = None) -> int:
    args = build_parser().parse_args(argv)
    if app_dir is None:
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not _single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "rdesk host",
            "rdesk host уже запущен. Его значок находится в области уведомлений "
            "Windows (возле часов).",
        )
        root.destroy()
        return 0

    config = load_config(app_dir)
    if args.server:
        host, port = split_server(args.server, DEFAULT_SERVER_PORT)
        config["server_host"] = host
        config["server_port"] = port
    if args.password:
        config["password"] = args.password
    if args.name:
        config["host_name"] = args.name
    if args.read_only:
        config["read_only"] = True
    if args.server or args.password or args.name or args.read_only:
        save_config(app_dir, config)

    log_dir = os.path.join(app_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    pid_path = os.path.join(log_dir, "host.pid")
    try:
        with open(pid_path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pid_path = ""

    capture.enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    app = HostApp(root, app_dir, start_hidden=args.tray)
    if not (args.tray and is_configured(app.config)):
        root.deiconify()
    root.mainloop()
    if pid_path:
        try:
            os.remove(pid_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
