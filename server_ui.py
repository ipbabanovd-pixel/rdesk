"""Графический интерфейс центрального сервера rdesk."""

from __future__ import annotations

import ctypes
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Optional

from . import __version__, server_config, ui_theme
from .relay_server import RelayServer

LOG_MAX_BYTES = 2 * 1024 * 1024


class _LogFile:
    def __init__(self, directory: str) -> None:
        self.path = os.path.join(directory, "server.log")
        self._lock = threading.Lock()
        self._fh = None
        try:
            os.makedirs(directory, exist_ok=True)
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX_BYTES:
                backup = self.path + ".1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(self.path, backup)
            self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        except OSError:
            self._fh = None

    def write(self, level: str, text: str) -> None:
        if self._fh is None:
            return
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {level.upper():5} {text}"
        with self._lock:
            try:
                self._fh.write(line + "\n")
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


class ServerApp:
    def __init__(self, root: tk.Tk, app_dir: str, start_hidden: bool = False) -> None:
        self.root = root
        self.app_dir = app_dir
        self.config, first_run = server_config.load_config(app_dir)
        self.server: Optional[RelayServer] = None
        self.events: queue.Queue = queue.Queue()
        self.log_file = _LogFile(os.path.join(app_dir, "logs"))
        self.hosts: list[dict] = []
        self._tray = None
        self._hidden = False
        self._closing = False
        self._last_stats: dict = {}
        self.first_run = first_run
        self._start_hidden = start_hidden
        self._poll_job: Optional[str] = None
        self._build_ui()
        self._setup_tray()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.root.after(80, self._poll)
        self.root.after(180, self.start_server)
        if first_run:
            if not start_hidden:
                self._info("Настройки сервера созданы. Пароль сгенерирован автоматически.")
                self.root.after(600, self._show_first_run_hint)

    # ------------------------------------------------------------------
    # интерфейс
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        ui_theme.apply_theme(self.root)
        self.root.title(f"rdesk server {__version__}")
        self.root.geometry("1120x760")
        self.root.minsize(940, 640)
        try:
            self.root.iconbitmap(os.path.join(self.app_dir, "assets", "rdesk.ico"))
        except tk.TclError:
            pass

        header = tk.Frame(self.root, bg=ui_theme.DARK, height=74)
        header.pack(side=tk.TOP, fill=tk.X)
        header.pack_propagate(False)

        brand = tk.Frame(header, bg=ui_theme.DARK)
        brand.pack(side=tk.LEFT, padx=(24, 0), pady=13)
        tk.Label(brand, text="rdesk", bg=ui_theme.DARK, fg=ui_theme.WHITE,
                 font=("Segoe UI", 19, "bold")).pack(side=tk.LEFT)
        tk.Label(brand, text=" server", bg=ui_theme.DARK, fg="#93c5fd",
                 font=("Segoe UI", 19)).pack(side=tk.LEFT)
        tk.Label(header, text="центральный сервер подключений",
                 bg=ui_theme.DARK, fg=ui_theme.DARK_MUTED,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(14, 0), pady=(28, 0))

        right = tk.Frame(header, bg=ui_theme.DARK)
        right.pack(side=tk.RIGHT, padx=22, pady=15)
        self.header_status = tk.Label(right, text="● Запуск…", bg=ui_theme.DARK, fg="#fbbf24",
                                      font=("Segoe UI", 10, "bold"))
        self.header_status.pack(side=tk.RIGHT)

        body = ttk.Frame(self.root, padding=(18, 16, 18, 10))
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        body.columnconfigure(0, minsize=338)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_settings(body)
        self._build_content(body)

        self.status_var = tk.StringVar(value="Сервер ещё не запущен")
        status = tk.Frame(self.root, bg="#e9edf5", height=28)
        status.pack(side=tk.BOTTOM, fill=tk.X)
        status.pack_propagate(False)
        tk.Label(status, textvariable=self.status_var, bg="#e9edf5", fg=ui_theme.MUTED,
                 font=("Segoe UI", 9), anchor="w").pack(side=tk.LEFT, padx=14)
        tk.Label(status, text=f"rdesk {__version__}  •  порт по умолчанию 7443",
                 bg="#e9edf5", fg=ui_theme.MUTED, font=("Segoe UI", 9)).pack(
                     side=tk.RIGHT, padx=14)

    def _build_settings(self, parent: ttk.Frame) -> None:
        card = tk.Frame(parent, bg=ui_theme.CARD, highlightbackground=ui_theme.BORDER,
                        highlightthickness=1, padx=20, pady=18)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 16))

        tk.Label(card, text="Настройки сервера", bg=ui_theme.CARD, fg=ui_theme.TEXT,
                 font=("Segoe UI", 14, "bold"), anchor="w").pack(fill=tk.X)
        tk.Label(card, text="Пароль нужен и хостам, и вьюверу.",
                 bg=ui_theme.CARD, fg=ui_theme.MUTED, font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=285).pack(fill=tk.X, pady=(4, 16))

        tk.Label(card, text="Название сервера", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X)
        self.name_var = tk.StringVar(value=str(self.config.get("server_name", "")))
        ttk.Entry(card, textvariable=self.name_var).pack(fill=tk.X, pady=(4, 12))

        row = tk.Frame(card, bg=ui_theme.CARD)
        row.pack(fill=tk.X, pady=(0, 12))
        port_col = tk.Frame(row, bg=ui_theme.CARD)
        port_col.pack(side=tk.LEFT, fill=tk.X, expand=False)
        tk.Label(port_col, text="TCP-порт", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X)
        self.port_var = tk.StringVar(value=str(self.config.get("port", 7443)))
        ttk.Entry(port_col, textvariable=self.port_var, width=10).pack(anchor="w", pady=(4, 0))
        info_col = tk.Frame(row, bg=ui_theme.CARD)
        info_col.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(14, 0))
        tk.Label(info_col, text="Порт открывается", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X)
        tk.Label(info_col, text="только на этом сервере", bg=ui_theme.CARD,
                 fg=ui_theme.TEXT, font=("Segoe UI", 9), anchor="w").pack(fill=tk.X, pady=(7, 0))

        tk.Label(card, text="Пароль сервера", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X)
        pass_row = tk.Frame(card, bg=ui_theme.CARD)
        pass_row.pack(fill=tk.X, pady=(4, 4))
        self.pass_var = tk.StringVar(value=str(self.config.get("password", "")))
        self.pass_entry = ttk.Entry(pass_row, textvariable=self.pass_var, show="•")
        self.pass_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.show_var = tk.BooleanVar(value=False)
        ui_theme.CheckBox(pass_row, "Показать", self.show_var, bg=ui_theme.CARD,
                          fg=ui_theme.MUTED, command=self._toggle_password).pack(
                              side=tk.LEFT, padx=(8, 0))

        actions = tk.Frame(card, bg=ui_theme.CARD)
        actions.pack(fill=tk.X, pady=(2, 12))
        ttk.Button(actions, text="Копировать", style="Soft.TButton",
                   command=self._copy_password).pack(side=tk.LEFT)
        ttk.Button(actions, text="Новый пароль", command=self._new_password).pack(
            side=tk.LEFT, padx=(6, 0))

        tk.Label(card, text="Адреса для подключения", bg=ui_theme.CARD, fg=ui_theme.MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X, pady=(4, 2))
        bind0 = str(self.config.get("bind") or "0.0.0.0")
        addresses = (", ".join(_local_addresses()[:3]) or "127.0.0.1") if bind0 == "0.0.0.0" else bind0
        self.addr_label = tk.Label(
            card, text=f"{addresses}\nпорт {self.port_var.get()}",
            bg=ui_theme.CARD, fg=ui_theme.TEXT, font=("Consolas", 9),
            anchor="w", justify="left", wraplength=285,
        )
        self.addr_label.pack(fill=tk.X, pady=(0, 14))

        ttk.Button(card, text="Применить и перезапустить", style="Accent.TButton",
                   command=self.apply_settings).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(card, text="Открыть порт в брандмауэре", style="Soft.TButton",
                   command=self.open_firewall).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(card, text="Свернуть в трей", command=self._hide_to_tray).pack(fill=tk.X)

        tk.Label(card, text="Пароль хранится в config/server.json. Не публикуйте его.",
                 bg=ui_theme.CARD, fg=ui_theme.MUTED, font=("Segoe UI", 8),
                 wraplength=285, justify="left", anchor="w").pack(fill=tk.X, pady=(14, 0))

    def _build_content(self, parent: ttk.Frame) -> None:
        content = ttk.Frame(parent)
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)
        content.rowconfigure(3, weight=1)

        stats = ttk.Frame(content)
        stats.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        for index in range(4):
            stats.columnconfigure(index, weight=1)
        self.stat_labels: dict[str, tk.Label] = {}
        for index, (key, title) in enumerate((
            ("hosts", "Компьютеры онлайн"),
            ("viewers", "Вьюверы"),
            ("sessions", "Активные сеансы"),
            ("port", "Порт"),
        )):
            card = tk.Frame(stats, bg=ui_theme.CARD, highlightbackground=ui_theme.BORDER,
                            highlightthickness=1, padx=14, pady=10)
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 8, 0))
            value = tk.Label(card, text="—", bg=ui_theme.CARD, fg=ui_theme.TEXT,
                             font=("Segoe UI", 17, "bold"), anchor="w")
            value.pack(fill=tk.X)
            tk.Label(card, text=title, bg=ui_theme.CARD, fg=ui_theme.MUTED,
                     font=("Segoe UI", 8), anchor="w").pack(fill=tk.X)
            self.stat_labels[key] = value

        hosts_card = tk.Frame(content, bg=ui_theme.CARD, highlightbackground=ui_theme.BORDER,
                              highlightthickness=1)
        hosts_card.grid(row=1, column=0, sticky="nsew")
        head = tk.Frame(hosts_card, bg=ui_theme.CARD, padx=16, pady=12)
        head.pack(fill=tk.X)
        tk.Label(head, text="Компьютеры", bg=ui_theme.CARD, fg=ui_theme.TEXT,
                 font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        self.host_count_label = tk.Label(head, text="0", bg=ui_theme.ACCENT_LIGHT,
                                         fg=ui_theme.ACCENT_DARK, font=("Segoe UI", 9, "bold"),
                                         padx=8, pady=1)
        self.host_count_label.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(head, text="Отключить компьютер", style="Danger.TButton",
                   command=self._disconnect_host).pack(side=tk.RIGHT)

        columns = ("name", "user", "os", "ip", "since", "status")
        self.tree = ttk.Treeview(hosts_card, columns=columns, show="headings",
                                 selectmode="browse", height=8)
        headings = {
            "name": ("Компьютер", 150),
            "user": ("Пользователь", 105),
            "os": ("Система", 125),
            "ip": ("IP", 95),
            "since": ("Подключён", 85),
            "status": ("Статус", 105),
        }
        for key, (title, width) in headings.items():
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=70, anchor="w",
                             stretch=key in ("name", "os"))
        self.tree.tag_configure("busy", foreground=ui_theme.AMBER)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=(0, 10))
        scroll = ttk.Scrollbar(hosts_card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(0, 10), padx=(0, 10))
        self.empty_label = tk.Label(
            hosts_card, text="Пока никто не подключён.\nУстановите rdesk host на компьютер "
                             "и укажите адрес этого сервера.",
            bg=ui_theme.CARD, fg=ui_theme.MUTED, font=("Segoe UI", 9), justify="left",
        )
        self.empty_label.place(relx=0.04, rely=0.52)

        log_card = tk.Frame(content, bg=ui_theme.CARD, highlightbackground=ui_theme.BORDER,
                            highlightthickness=1)
        log_card.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        log_head = tk.Frame(log_card, bg=ui_theme.CARD, padx=16, pady=10)
        log_head.pack(fill=tk.X)
        tk.Label(log_head, text="Журнал сервера", bg=ui_theme.CARD, fg=ui_theme.TEXT,
                 font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        ttk.Button(log_head, text="Очистить", style="Link.TButton",
                   command=self._clear_log).pack(side=tk.RIGHT)
        self.log_text = ui_theme.make_log_text(log_card, height=7)
        self.log_text.master.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

    # ------------------------------------------------------------------
    # сервер
    # ------------------------------------------------------------------

    def start_server(self) -> None:
        if self._closing or self.server is not None:
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            self._error("Порт должен быть числом.")
            return
        if not (1 <= port <= 65535):
            self._error("Порт должен быть в диапазоне 1…65535.")
            return
        password = self.pass_var.get().strip()
        if not password:
            self._error("Пароль сервера не может быть пустым.")
            return
        name = self.name_var.get().strip() or socket.gethostname()
        server = RelayServer(str(self.config.get("bind") or "0.0.0.0"), port, password,
                             name, on_event=self._on_event)
        try:
            server.start()
        except OSError as exc:
            self._error(f"Не удалось занять порт {port}:\n{exc}\n\n"
                        "Возможно, он уже используется другой программой.")
            self._set_state(False, "Не удалось запустить")
            return
        self.server = server
        self.config.update({"port": port, "password": password, "server_name": name})
        server_config.save_config(self.app_dir, self.config)
        bind = str(self.config.get("bind") or "0.0.0.0")
        shown = (", ".join(_local_addresses()[:3]) or "127.0.0.1") if bind == "0.0.0.0" else bind
        self.addr_label.configure(text=f"{shown}\nпорт {port}")
        self._set_state(True, f"Сервер запущен на порту {port}")
        self._append_log("ok", f"Сервер запущен на {bind}:{port}")
        self._update_tray()

    def stop_server(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.shutdown()
        self._set_state(False, "Сервер остановлен")
        self.hosts = []
        self._render_hosts()

    def apply_settings(self) -> None:
        try:
            port = int(self.port_var.get())
        except ValueError:
            self._error("Порт должен быть числом.")
            return
        if not (1 <= port <= 65535):
            self._error("Порт должен быть в диапазоне 1…65535.")
            return
        password = self.pass_var.get().strip()
        if len(password) < 6:
            if not messagebox.askyesno(
                "Слишком короткий пароль",
                "Пароль короче 6 символов. Лучше использовать длинный пароль.\n"
                "Всё равно сохранить?",
                parent=self.root,
            ):
                return
        name = self.name_var.get().strip() or socket.gethostname()
        old_port = int(self.config.get("port", 0))
        old_password = str(self.config.get("password", ""))
        old_name = str(self.config.get("server_name", ""))
        changed = (port != old_port or password != old_password or name != old_name)
        if changed and self.server is not None:
            if not messagebox.askyesno(
                "Перезапустить сервер?",
                "Изменение порта или пароля отключит все компьютеры и вьюверы.\n"
                "Продолжить?",
                parent=self.root,
            ):
                self.port_var.set(str(old_port))
                self.pass_var.set(old_password)
                self.name_var.set(old_name)
                return
        self.config.update({"port": port, "password": password, "server_name": name})
        server_config.save_config(self.app_dir, self.config)
        if changed and self.server is not None:
            self.stop_server()
            self.root.after(150, self.start_server)
        elif self.server is None:
            self.start_server()
        else:
            self._append_log("ok", "Настройки сохранены")
            self._info("Настройки сохранены.")
        self._update_autostart()

    def _new_password(self) -> None:
        if not messagebox.askyesno(
            "Новый пароль",
            "Сгенерировать новый пароль?\n\n"
            "После перезапуска все хосты и вьюверы нужно будет настроить заново.",
            parent=self.root,
        ):
            return
        password = server_config.generate_password()
        self.pass_var.set(password)
        self.show_var.set(True)
        self._toggle_password()
        self._append_log("warn", "Сгенерирован новый пароль. Нажмите «Применить».")

    def _copy_password(self) -> None:
        password = self.pass_var.get()
        if not password:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(password)
        self.status_var.set("Пароль скопирован в буфер обмена")
        self.root.after(2500, lambda: self.status_var.set(
            "Сервер запущен" if self.server else "Сервер остановлен"))

    def _toggle_password(self) -> None:
        self.pass_entry.configure(show="" if self.show_var.get() else "•")

    def open_firewall(self) -> None:
        try:
            port = int(self.port_var.get())
        except ValueError:
            self._error("Сначала укажите корректный порт.")
            return
        rule = f"rdesk server TCP {port}"
        args = (f'advfirewall firewall add rule name="{rule}" dir=in action=allow '
                f'protocol=TCP localport={port} profile=any enable=yes')
        code = 0
        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "netsh.exe", args, None, 1
            )
            code = int(result)
        except Exception as exc:  # noqa: BLE001
            self._error(f"Не удалось запустить netsh: {exc}")
            return
        if code <= 32:
            self._info("Запрос прав администратора отменён или недоступен.\n\n"
                       "Откройте порт вручную в брандмауэре Windows.")
        else:
            self._info(f"Запрошено правило брандмауэра для TCP-порта {port}.\n"
                       "Подтвердите запрос UAC, если он появился.")
            self._append_log("ok", f"Создаётся правило брандмауэра для TCP {port}")

    # ------------------------------------------------------------------
    # события и данные
    # ------------------------------------------------------------------

    def _on_event(self, kind: str, payload: Any) -> None:
        self.events.put((kind, payload))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    data = payload if isinstance(payload, dict) else {}
                    self._append_log(str(data.get("level", "info")), str(data.get("text", "")))
                elif kind == "hosts":
                    self.hosts = list(payload or [])
                    self._render_hosts()
        except queue.Empty:
            pass
        self._update_stats()
        self._process_ui_requests()
        if not self._closing:
            self._poll_job = self.root.after(120, self._poll)

    def _process_ui_requests(self) -> None:
        if self._closing:
            return
        try:
            while True:
                action = self._ui_queue.get_nowait()
                if action == "show":
                    self._show_window()
                elif action == "quit":
                    self._quit()
        except AttributeError:
            self._ui_queue: queue.Queue = queue.Queue()
        except queue.Empty:
            pass

    def _render_hosts(self) -> None:
        selected = self.tree.selection()
        selected_id = selected[0] if selected else ""
        existing = set(self.tree.get_children())
        wanted = {host.get("id", "") for host in self.hosts}
        for iid in existing - wanted:
            self.tree.delete(iid)
        for host in self.hosts:
            host_id = str(host.get("id", ""))
            if not host_id:
                continue
            status = "Занят" if host.get("busy") else "Онлайн"
            if host.get("read_only"):
                status += " • только просмотр"
            values = (
                host.get("name", ""),
                host.get("user", ""),
                host.get("os", ""),
                host.get("ip", ""),
                _format_since(host.get("connected_at")),
                status,
            )
            tags = ("busy",) if host.get("busy") else ()
            if host_id in existing:
                self.tree.item(host_id, values=values, tags=tags)
            else:
                self.tree.insert("", tk.END, iid=host_id, values=values, tags=tags)
        if selected_id and selected_id in self.tree.get_children():
            self.tree.selection_set(selected_id)
        count = len(self.hosts)
        self.host_count_label.configure(text=str(count), bg=ui_theme.ACCENT_LIGHT
                                         if count else "#eef1f6",
                                         fg=ui_theme.ACCENT_DARK if count else ui_theme.MUTED)
        if count:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.04, rely=0.52)
        self._update_tray()

    def _disconnect_host(self) -> None:
        selection = self.tree.selection()
        if not selection or self.server is None:
            return
        host = next((h for h in self.hosts if str(h.get("id")) == selection[0]), None)
        if host is None:
            return
        if messagebox.askyesno(
            "Отключить компьютер",
            f"Отключить «{host.get('name', '')}» от сервера?\n"
            "Хост попробует подключиться снова через несколько секунд.",
            parent=self.root,
        ):
            self.server.disconnect_host(selection[0])

    def _update_stats(self) -> None:
        if self.server is None:
            for key in ("hosts", "viewers", "sessions"):
                self.stat_labels[key].configure(text="—")
            self.stat_labels["port"].configure(text=self.port_var.get() or "—")
            return
        stats = self.server.stats()
        self._last_stats = stats
        self.stat_labels["hosts"].configure(text=str(stats["hosts"]))
        self.stat_labels["viewers"].configure(text=str(stats["viewers"]))
        self.stat_labels["sessions"].configure(text=str(stats["sessions"]))
        self.stat_labels["port"].configure(text=str(stats["port"]))

    def _set_state(self, running: bool, text: str) -> None:
        self.status_var.set(text)
        self.header_status.configure(
            text="● Работает" if running else "● Остановлен",
            fg="#4ade80" if running else "#f87171",
        )
        if not running:
            self._update_tray()

    def _append_log(self, level: str, text: str) -> None:
        if not text:
            return
        self.log_file.write(level, text)
        ui_theme.log_append(self.log_text, text, level, time.strftime("%H:%M:%S"))

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def _error(self, message: str) -> None:
        messagebox.showerror("rdesk server", message, parent=self.root)

    def _info(self, message: str) -> None:
        messagebox.showinfo("rdesk server", message, parent=self.root)

    def _show_first_run_hint(self) -> None:
        messagebox.showinfo(
            "Сервер rdesk готов",
            "Пароль сервера создан автоматически.\n\n"
            "Нажмите «Копировать» в разделе «Пароль сервера» и вставьте этот пароль "
            "при установке rdesk host и при входе во вьювере.\n\n"
            "Для подключения из интернета откройте TCP-порт "
            f"{self.port_var.get()} на этом сервере (кнопка в окне сервера поможет "
            "добавить правило брандмауэра).",
            parent=self.root,
        )

    # ------------------------------------------------------------------
    # трей и закрытие
    # ------------------------------------------------------------------

    def _setup_tray(self) -> None:
        from .tray import TrayIcon

        self._ui_queue: queue.Queue = queue.Queue()
        icon = os.path.join(self.app_dir, "assets", "rdesk.ico")
        tray = TrayIcon(
            tooltip="rdesk server",
            icon_path=icon if os.path.exists(icon) else None,
            on_open=lambda: self._ui_queue.put("show"),
            on_exit=lambda: self._ui_queue.put("quit"),
            open_label="Открыть сервер",
            exit_label="Остановить и выйти",
        )
        if tray.start():
            self._tray = tray
        else:
            self._tray = None
            self._append_log("warn", f"Значок в трее недоступен: {tray.error}")

    def _update_tray(self) -> None:
        if self._tray is None:
            return
        count = len(self.hosts)
        state = "работает" if self.server else "остановлен"
        self._tray.set_tooltip(f"rdesk server — {state}, компьютеров: {count}")

    def _hide_to_tray(self) -> None:
        if self._tray is None:
            self._info("Значок в трее недоступен. Сверните окно обычным способом.")
            return
        self.root.withdraw()
        self._hidden = True
        self._tray.notify("rdesk server", "Сервер продолжает работать в фоне.",
                          sound=False)

    def _show_window(self) -> None:
        self._hidden = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_close(self) -> None:
        if self._tray is None:
            if messagebox.askyesno("Выход", "Остановить сервер и выйти?",
                                   parent=self.root):
                self._quit()
            return
        choice = messagebox.askyesnocancel(
            "rdesk server",
            "Да — свернуть в трей и оставить сервер работать.\n"
            "Нет — остановить сервер и выйти.\n"
            "Отмена — вернуться в окно.",
            parent=self.root,
        )
        if choice is True:
            self._hide_to_tray()
        elif choice is False:
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
        self.stop_server()
        if self._tray is not None:
            self._tray.stop()
        self.log_file.close()
        self.root.after(80, self.root.destroy)


# --------------------------------------------------------------------------
# вспомогательное
# --------------------------------------------------------------------------

def _local_addresses() -> list[str]:
    result: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in result and not addr.startswith("127."):
                result.append(addr)
    except OSError:
        pass
    if not result:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            result.append(probe.getsockname()[0])
            probe.close()
        except OSError:
            result.append("127.0.0.1")
    return result


def _format_since(timestamp: Any) -> str:
    try:
        value = time.localtime(int(timestamp))
        return time.strftime("%d.%m %H:%M", value)
    except (TypeError, ValueError, OSError):
        return "—"


def _single_instance() -> bool:
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\rdesk-server-single-instance")
        if not handle:
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _SINGLE_HANDLE = handle  # noqa: F841
        return True
    except Exception:  # noqa: BLE001
        return True


def _update_autostart(enabled: bool = True) -> None:
    # При установке автозапуск настраивает установщик. Здесь оставлено
    # место для будущей настройки из интерфейса, чтобы не трогать реестр
    # без явного действия пользователя.
    return


def main(app_dir: Optional[str] = None, start_hidden: bool = False) -> int:
    if app_dir is None:
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not _single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "rdesk server",
            "rdesk server уже запущен. Его окно можно открыть из значка в области "
            "уведомлений Windows.",
        )
        root.destroy()
        return 0
    log_dir = os.path.join(app_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    pid_path = os.path.join(log_dir, "server.pid")
    try:
        with open(pid_path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pid_path = ""
    root = tk.Tk()
    root.withdraw()
    app = ServerApp(root, app_dir, start_hidden=start_hidden)
    if not start_hidden:
        root.deiconify()
    else:
        app._hidden = True
    root.mainloop()
    if pid_path:
        try:
            os.remove(pid_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
