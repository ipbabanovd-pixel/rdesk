"""Хост (сервер) удалённого доступа.

Запуск:
    python host.py                 # пароль и порт берутся из config/host.json
    python host.py --password ...  # задать пароль на этот запуск
    python host.py --read-only     # только просмотр, ввод игнорируется

Как это работает:
    1. Хост принимает TCP-подключение и проводит взаимную аутентификацию по паролю
       (PBKDF2 + HMAC-подписи обеих сторон), после чего весь трафик шифруется.
    2. Клиент присылает настройки (масштаб, FPS, уровень сжатия, «только просмотр»).
    3. Поток кадров: захват экрана -> тайлы 64x64 -> только изменившиеся ->
       zlib -> FRAME. Хост ждёт ACK, что даёт естественное ограничение скорости
       под пропускную способность канала.
    4. Поток ввода: INPUT_MOUSE / INPUT_KEY -> SendInput.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import string
import struct
import sys
import threading
import time
import traceback

from . import capture, crypto, inject, keys, protocol

DEFAULT_PORT = 47890
MAX_CLIENTS = 4
AUTH_FAIL_LIMIT = 6
AUTH_LOCK_SECONDS = 120.0
LAYOUT_CHECK_INTERVAL = 2.0
LOG_MAX_BYTES = 2 * 1024 * 1024

_log_handle = None
_log_lock = threading.Lock()


def setup_logging(log_dir: str) -> str:
    """Журнал в файл — нужен, когда хост работает без консольного окна."""
    global _log_handle
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "host.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            backup = path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
    except OSError:
        pass
    try:
        _log_handle = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        _log_handle = None
    return path


def log(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    with _log_lock:
        if _log_handle is not None:
            try:
                _log_handle.write(line + "\n")
            except (OSError, ValueError):
                pass
        if sys.stdout is not None:
            try:
                print(line, flush=True)
            except (OSError, ValueError):
                pass


# --------------------------------------------------------------------------
# конфигурация
# --------------------------------------------------------------------------

def config_path(app_dir: str) -> str:
    return os.path.join(app_dir, "config", "host.json")


def generate_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_config(app_dir: str) -> dict:
    path = config_path(app_dir)
    data: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
    changed = False
    if not data.get("password"):
        data["password"] = generate_password()
        changed = True
    data.setdefault("port", DEFAULT_PORT)
    data.setdefault("bind", "0.0.0.0")
    data.setdefault("host_name", socket.gethostname())
    if changed:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    return data


def save_config(app_dir: str, data: dict) -> None:
    path = config_path(app_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def local_addresses() -> list[str]:
    """Список IPv4-адресов машины (чтобы подсказать, куда подключаться)."""
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


# --------------------------------------------------------------------------
# обработчик одного клиента
# --------------------------------------------------------------------------

class ClientHandler:
    def __init__(self, server: "DesktopServer", conn: protocol.Conn,
                 addr: tuple[str, int], client_name: str) -> None:
        self.server = server
        self.conn = conn
        self.addr = addr
        self.name = client_name
        self.label = f"{client_name}@{addr[0]}"
        self.settings = {"scale": 1.0, "fps": 15, "level": 1, "view_only": False}
        self._stop = threading.Event()
        self._force_full = True
        self._acked = 0
        self._ack_cond = threading.Condition()
        self._layout: capture.ScreenLayout | None = None
        self._stats = {"frames": 0, "bytes": 0, "since": time.perf_counter()}
        self._close_lock = threading.Lock()
        self._closed = False
        self._clip_stop = threading.Event()
        self._clip_last = ""
        self._clip_lock = threading.Lock()

    # --- служебное ---

    def _send_screen(self, layout: capture.ScreenLayout) -> None:
        self._layout = layout
        info = layout.as_dict()
        info["host"] = self.server.host_name
        info["read_only"] = self.server.read_only
        self.conn.send_json(protocol.Msg.SCREEN, info)
        log(f"[{self.label}] экран {layout.width}x{layout.height}, "
            f"мониторов {len(layout.monitors)}")

    def _read_cfg(self) -> None:
        self.conn.settimeout(20.0)
        mtype, payload = self.conn.recv_json()
        self.conn.settimeout(None)
        if mtype is not protocol.Msg.CFG:
            raise protocol.ProtocolError(f"ожидался CFG, получено {mtype.name}")
        if not isinstance(payload, dict):
            raise protocol.ProtocolError("CFG должен быть объектом JSON")
        try:
            self.settings["scale"] = min(1.0, max(0.15, float(payload.get("scale", 1.0))))
            self.settings["fps"] = min(30, max(1, int(payload.get("fps", 15))))
            self.settings["level"] = min(9, max(0, int(payload.get("level", 1))))
            self.settings["view_only"] = bool(payload.get("view_only", False))
        except (TypeError, ValueError) as exc:
            raise protocol.ProtocolError(f"некорректные настройки: {exc}") from exc
        log(f"[{self.label}] настройки: {self.settings['scale'] * 100:.0f} %, "
            f"{self.settings['fps']} fps, сжатие {self.settings['level']}, "
            f"{'только просмотр' if not self._input_allowed else 'полное управление'}")

    # --- поток кадров ---

    def _frame_loop(self) -> None:
        grabber = capture.ScreenGrabber()
        encoder = capture.TileEncoder(level=self.settings["level"])
        frame_id = 0
        last_layout_check = 0.0
        try:
            if self._layout is None:
                self._send_screen(capture.get_layout())
            while not self._stop.is_set():
                started = time.perf_counter()
                if started - last_layout_check > LAYOUT_CHECK_INTERVAL:
                    last_layout_check = started
                    layout = capture.get_layout()
                    if (self._layout is None
                            or (layout.width, layout.height) != (self._layout.width, self._layout.height)
                            or layout.x != self._layout.x or layout.y != self._layout.y):
                        encoder.reset()
                        self._force_full = True
                        self._send_screen(layout)
                layout = self._layout
                assert layout is not None
                scale = self.settings["scale"]
                width, height, rgb = grabber.grab(layout, scale)
                if self._stop.is_set():
                    break
                tiles, full = encoder.encode(
                    width, height, rgb,
                    force_full=self._force_full,
                    level=self.settings["level"],
                )
                self._force_full = False
                frame_id += 1
                header = protocol.pack_frame_header(
                    int(round(scale * 1000)), width, height, frame_id,
                    protocol.FLAG_FULL if full else 0, len(tiles),
                )
                body, sent = protocol.pack_frame(header, tiles)
                self.conn.send(protocol.Msg.FRAME, body)
                self._note_sent(len(body), len(tiles))

                with self._ack_cond:
                    self._ack_cond.wait_for(
                        lambda: self._acked >= frame_id or self._stop.is_set(),
                        timeout=max(0.5, 2.0 / self.settings["fps"]),
                    )
                elapsed = time.perf_counter() - started
                target = 1.0 / self.settings["fps"]
                if elapsed < target:
                    time.sleep(min(0.25, target - elapsed))
        except (OSError, ConnectionError, protocol.ProtocolError, capture.CaptureError) as exc:
            if not self._stop.is_set():
                log(f"[{self.label}] поток кадров остановлен: {exc}")
            self._stop.set()
        except Exception:  # noqa: BLE001
            log(f"[{self.label}] непредвиденная ошибка в потоке кадров:")
            traceback.print_exc()
            self._stop.set()
        finally:
            grabber.close()
            self.close()

    def _note_sent(self, size: int, tiles: int) -> None:
        stats = self._stats
        stats["frames"] += 1
        stats["bytes"] += size
        now = time.perf_counter()
        if now - stats["since"] >= 5.0:
            fps = stats["frames"] / (now - stats["since"])
            kbps = stats["bytes"] / (now - stats["since"]) / 1024
            log(f"[{self.label}] {fps:.1f} fps, {kbps:.0f} КБ/с, тайлов {tiles}, кадр {size / 1024:.0f} КБ")
            stats.update({"frames": 0, "bytes": 0, "since": now})

    # --- поток ввода ---

    def _input_loop(self) -> None:
        while not self._stop.is_set():
            mtype, payload = self.conn.recv()
            if mtype is protocol.Msg.INPUT_MOUSE:
                self._on_mouse(payload)
            elif mtype is protocol.Msg.INPUT_KEY:
                self._on_key(payload)
            elif mtype is protocol.Msg.ACK:
                if len(payload) != 4:
                    raise protocol.ProtocolError("некорректный ACK")
                (ack_id,) = struct.unpack(">I", payload)
                with self._ack_cond:
                    self._acked = max(self._acked, ack_id)
                    self._ack_cond.notify_all()
            elif mtype is protocol.Msg.KEYFRAME:
                self._force_full = True
            elif mtype is protocol.Msg.CLIP:
                self._on_clip(payload)
            elif mtype is protocol.Msg.PING:
                self.conn.send(protocol.Msg.PONG, payload)
            elif mtype is protocol.Msg.BYE:
                log(f"[{self.label}] отключился")
                break
            else:
                raise protocol.ProtocolError(f"неожиданное сообщение {mtype.name}")

    @property
    def _input_allowed(self) -> bool:
        return not (self.server.read_only or self.settings["view_only"])

    # --- буфер обмена (автоматическая синхронизация host → viewer) ---

    def _clip_loop(self) -> None:
        try:
            with self._clip_lock:
                self._clip_last = inject.get_clipboard_text()
        except Exception:  # noqa: BLE001
            pass
        while not self._clip_stop.wait(0.35):
            if self._stop.is_set():
                return
            try:
                text = inject.get_clipboard_text()
            except Exception:  # noqa: BLE001
                continue
            if not text:
                continue
            try:
                encoded = text.encode("utf-8")
            except UnicodeEncodeError:
                continue
            if len(encoded) > 1024 * 1024:
                continue
            with self._clip_lock:
                if text == self._clip_last:
                    continue
            try:
                self.conn.send(protocol.Msg.CLIP, encoded)
            except (OSError, ConnectionError, protocol.ProtocolError):
                return
            with self._clip_lock:
                self._clip_last = text
            log(f"[{self.label}] буфер обмена отправлен вьюверу ({len(text)} символов)")

    def _on_mouse(self, payload: bytes) -> None:
        if not self._input_allowed:
            return
        kind, button, wheel, x, y = protocol.unpack_mouse(payload)
        try:
            if kind == protocol.MOUSE_MOVE:
                inject.mouse_move(x, y)
            elif kind == protocol.MOUSE_DOWN:
                inject.mouse_button(button, True, x, y)
            elif kind == protocol.MOUSE_UP:
                inject.mouse_button(button, False, x, y)
            elif kind == protocol.MOUSE_WHEEL:
                inject.mouse_wheel(wheel)
        except inject.InjectError as exc:
            log(f"[{self.label}] ввод мыши отклонён: {exc}")

    def _on_key(self, payload: bytes) -> None:
        if not self._input_allowed:
            return
        vk, scan, flags, down = protocol.unpack_key(payload)
        extended = bool(keys.EXTENDED_FLAG & flags)
        try:
            if vk == 0 and scan:
                # юникод-ввод: scan — код символа UTF-16
                char = chr(scan)
                inject.unicode_char(char, down)
            elif vk:
                inject.key_event(vk, down, extended)
        except inject.InjectError as exc:
            log(f"[{self.label}] ввод клавиатуры отклонён: {exc}")

    def _on_clip(self, payload: bytes) -> None:
        if not self._input_allowed:
            return
        if len(payload) > 1024 * 1024:
            raise protocol.ProtocolError("слишком большой текст буфера обмена")
        try:
            text = payload.decode("utf-8", "replace")
            with self._clip_lock:
                if text == self._clip_last:
                    return
                # Ставим метку заранее: наблюдатель буфера не должен успеть
                # отправить это же значение обратно вьюверу.
                self._clip_last = text
            try:
                inject.set_clipboard_text(text)
            except Exception:
                with self._clip_lock:
                    self._clip_last = ""
                raise
            log(f"[{self.label}] в буфер обмена хоста вставлено {len(text)} символов")
        except Exception as exc:  # noqa: BLE001
            log(f"[{self.label}] не удалось установить буфер обмена: {exc}")

    # --- завершение ---

    def run(self) -> None:
        log(f"[{self.label}] подключение с {self.addr[0]}")
        try:
            self._send_screen(capture.get_layout())
            self._read_cfg()
            threading.Thread(target=self._frame_loop, name=f"frames-{self.label}", daemon=True).start()
            threading.Thread(target=self._clip_loop, name=f"clip-{self.label}", daemon=True).start()
            self._input_loop()
        except (OSError, ConnectionError, protocol.ProtocolError) as exc:
            if not self._stop.is_set():
                log(f"[{self.label}] соединение прервано: {exc}")
        except Exception:  # noqa: BLE001
            log(f"[{self.label}] непредвиденная ошибка:")
            traceback.print_exc()
        finally:
            self.close()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        self._clip_stop.set()
        with self._ack_cond:
            self._ack_cond.notify_all()
        try:
            self.conn.send(protocol.Msg.BYE)
        except (OSError, protocol.ProtocolError):
            pass
        self.conn.close()
        self.server.unregister(self)


# --------------------------------------------------------------------------
# сервер
# --------------------------------------------------------------------------

class DesktopServer:
    def __init__(self, bind: str, port: int, password: str, host_name: str,
                 read_only: bool = False, notifier=None) -> None:
        self.bind = bind
        self.port = port
        self.password = password
        self.host_name = host_name
        self.read_only = read_only
        self.notifier = notifier  # callable(kind, label): "connected"/"disconnected"
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((bind, port))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]  # при port=0 узнаём реальный номер
        self._clients: list[ClientHandler] = []
        self._clients_lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._failures_lock = threading.Lock()
        self._stop = threading.Event()

    # --- защита от перебора пароля ---

    def _recent_failures(self, ip: str) -> list[float]:
        now = time.time()
        with self._failures_lock:
            attempts = [t for t in self._failures.get(ip, []) if now - t < AUTH_LOCK_SECONDS]
            self._failures[ip] = attempts
            return attempts

    def _register_failure(self, ip: str) -> None:
        with self._failures_lock:
            self._failures.setdefault(ip, []).append(time.time())

    # --- клиенты ---

    def register(self, client: ClientHandler) -> bool:
        with self._clients_lock:
            if len(self._clients) >= MAX_CLIENTS:
                return False
            self._clients.append(client)
            return True

    def unregister(self, client: ClientHandler) -> None:
        removed = False
        with self._clients_lock:
            if client in self._clients:
                self._clients.remove(client)
                removed = True
        if removed:
            self._notify("disconnected", client.label)

    def _notify(self, kind: str, label: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier(kind, label)
        except Exception:  # noqa: BLE001
            pass

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    # --- цикл ---

    def _handle(self, sock: socket.socket, addr: tuple[str, int]) -> None:
        ip = addr[0]
        if len(self._recent_failures(ip)) >= AUTH_FAIL_LIMIT:
            log(f"отказ {ip}: слишком много неудачных попыток, подождите {AUTH_LOCK_SECONDS:.0f} с")
            sock.close()
            return
        try:
            conn, name = protocol.server_handshake(sock, self.password, self.host_name)
        except protocol.AuthError as exc:
            self._register_failure(ip)
            log(f"отказ {ip}: {exc}")
            try:
                sock.close()
            except OSError:
                pass
            return
        except (protocol.ProtocolError, OSError, ConnectionError) as exc:
            log(f"ошибка подключения {ip}: {exc}")
            try:
                sock.close()
            except OSError:
                pass
            return

        client = ClientHandler(self, conn, addr, name)
        if not self.register(client):
            log(f"отказ {ip}: уже {MAX_CLIENTS} активных подключения")
            try:
                conn.send(protocol.Msg.ERROR, "слишком много подключений".encode("utf-8"))
            except OSError:
                pass
            conn.close()
            return
        self._notify("connected", client.label)
        try:
            client.run()
        finally:
            self.unregister(client)

    def serve_forever(self) -> None:
        log(f"хост «{self.host_name}» слушает {self.bind}:{self.port}")
        if self.read_only:
            log("режим «только просмотр»: ввод с клиентов игнорируется")
        for address in local_addresses():
            log(f"  адрес для подключения: {address}:{self.port}")
        while not self._stop.is_set():
            try:
                sock, addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(sock, addr), daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            client.close()


# --------------------------------------------------------------------------
# точка входа
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="host.py",
        description="rdesk host — удалённый доступ к этому компьютеру",
    )
    parser.add_argument("--port", type=int, help=f"порт (по умолчанию {DEFAULT_PORT})")
    parser.add_argument("--bind", help="адрес прослушивания (по умолчанию 0.0.0.0)")
    parser.add_argument("--password", help="пароль на этот запуск (иначе из config/host.json)")
    parser.add_argument("--read-only", action="store_true", help="разрешить только просмотр")
    parser.add_argument("--name", help="имя этого компьютера для клиентов")
    parser.add_argument("--tray", action="store_true",
                        help="значок в области уведомлений (режим без консоли)")
    parser.add_argument("--show-password", action="store_true",
                        help="напечатать пароль из настроек и выйти")
    parser.add_argument("--log-dir", help="папка для журнала (по умолчанию app/logs)")
    return parser


def main(argv: list[str] | None = None, app_dir: str | None = None) -> int:
    args = build_parser().parse_args(argv)
    app_dir = app_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = load_config(app_dir)

    port = args.port if args.port is not None else int(config.get("port", DEFAULT_PORT))
    bind = args.bind or str(config.get("bind", "0.0.0.0"))
    password = args.password or str(config["password"])
    host_name = args.name or str(config.get("host_name") or socket.gethostname())

    if args.password:
        config["password"] = args.password
        save_config(app_dir, config)

    if args.show_password:
        print(password)
        return 0

    log_dir = args.log_dir or os.path.join(app_dir, "logs")
    log_path = setup_logging(log_dir)
    pid_path = os.path.join(log_dir, "host.pid")
    try:
        with open(pid_path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pid_path = ""
    capture.enable_dpi_awareness()

    try:
        server = DesktopServer(bind, port, password, host_name, read_only=args.read_only)
    except OSError as exc:
        log(f"не удалось занять {bind}:{port}: {exc}")
        if sys.stderr is not None:
            print(f"Не удалось занять {bind}:{port}: {exc}", file=sys.stderr)
            print("Возможно, порт занят или нужны права администратора.", file=sys.stderr)
        return 1

    tray = None
    if args.tray:
        from .tray import TrayIcon

        icon_path = os.path.join(app_dir, "assets", "rdesk.ico")
        tray = TrayIcon(
            tooltip=f"rdesk — {host_name}",
            icon_path=icon_path,
            on_exit=server.shutdown,
            on_open=lambda: os.startfile(log_dir),
        )
        if not tray.start():
            log(f"значок в трее недоступен: {tray.error}")
            tray = None
        else:
            log("значок в области уведомлений включён")

    def notifier(kind: str, label: str) -> None:
        if tray is None:
            return
        if kind == "connected":
            tray.notify("rdesk — подключение", label)
        else:
            tray.notify("rdesk — отключение", label, sound=False)
        tray.set_tooltip(f"rdesk — активно: {server.client_count}")

    server.notifier = notifier

    if sys.stdout is not None:
        print()
        print("=" * 62)
        print("  rdesk host — этот компьютер доступен удалённо")
        print("=" * 62)
        print(f"  Пароль для подключения: {password}")
        print(f"  Файл настроек:          {config_path(app_dir)}")
        print(f"  Журнал:                 {log_path}")
        print("  Остановить:             Ctrl+C" + (" или меню значка в трее" if tray else ""))
        print("=" * 62)
        print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("остановка по Ctrl+C")
    finally:
        server.shutdown()
        if tray is not None:
            tray.stop()
        log("хост остановлен")
        if pid_path:
            try:
                os.remove(pid_path)
            except OSError:
                pass
    return 0
