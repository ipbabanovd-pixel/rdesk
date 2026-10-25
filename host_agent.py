"""Агент хоста: исходящее подключение к центральному серверу.

Хост больше не слушает порт и не требует проброса портов. Он сам подключается
к серверу rdesk, регистрируется в списке компьютеров и ждёт запросов на сеанс.
Когда вьювер выбирает этот компьютер, сервер выдаёт одноразовые токены, обе
стороны открывают канал данных, и внутри него работает обычный сквозной
протокол rdesk (пароль сервера + шифрование кадра).
"""

from __future__ import annotations

import json
import os
import platform
import socket
import threading
import time
import uuid
from typing import Callable, Optional

from . import __version__, crypto, protocol, relay_proto
from . import host as host_core

DEFAULT_SERVER_PORT = relay_proto.DEFAULT_PORT
RECONNECT_MIN = 3.0
RECONNECT_MAX = 30.0
PING_INTERVAL = 15.0


# --------------------------------------------------------------------------
# конфигурация
# --------------------------------------------------------------------------

def config_path(app_dir: str) -> str:
    return os.path.join(app_dir, "config", "host.json")


def default_config() -> dict:
    return {
        "server_host": "",
        "server_port": DEFAULT_SERVER_PORT,
        "password": "",
        "host_name": socket.gethostname(),
        "host_id": uuid.uuid4().hex,
        "read_only": False,
        "confirm": False,
    }


def load_config(app_dir: str) -> dict:
    data = default_config()
    path = config_path(app_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass
    if not data.get("host_id"):
        data["host_id"] = uuid.uuid4().hex
    if not data.get("host_name"):
        data["host_name"] = socket.gethostname()
    try:
        data["server_port"] = int(data.get("server_port") or DEFAULT_SERVER_PORT)
    except (TypeError, ValueError):
        data["server_port"] = DEFAULT_SERVER_PORT
    data["read_only"] = bool(data.get("read_only"))
    data["confirm"] = bool(data.get("confirm"))
    return data


def save_config(app_dir: str, data: dict) -> None:
    path = config_path(app_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def is_configured(config: dict) -> bool:
    return bool(str(config.get("server_host") or "").strip()
                and str(config.get("password") or ""))


def split_server(value: str, default_port: int = DEFAULT_SERVER_PORT) -> tuple[str, int]:
    """Принимает host, host:port или host/port и возвращает (host, port)."""
    text = (value or "").strip()
    if not text:
        return "", int(default_port)
    for prefix in ("rdesk://", "tcp://", "http://", "https://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.rstrip("/")
    host, port = text, int(default_port)
    if text.startswith("["):  # [IPv6]:port
        end = text.find("]")
        if end != -1:
            host = text[1:end]
            rest = text[end + 1:]
            if rest.startswith(":") and rest[1:].isdigit():
                port = int(rest[1:])
    elif text.count(":") == 1:
        maybe_host, maybe_port = text.rsplit(":", 1)
        if maybe_port.isdigit():
            host, port = maybe_host, int(maybe_port)
    return host.strip(), max(1, min(65535, port))


def server_display(config: dict) -> str:
    host = str(config.get("server_host") or "").strip()
    port = int(config.get("server_port") or DEFAULT_SERVER_PORT)
    return f"{host}:{port}" if host else "не указан"


# --------------------------------------------------------------------------
# агент
# --------------------------------------------------------------------------

class _Hub:
    """Контекст для ClientHandler из rdesk.host (сеанс одного вьювера)."""

    def __init__(self, agent: "HostAgent") -> None:
        self.agent = agent
        self.read_only = agent.read_only

    @property
    def host_name(self) -> str:
        return self.agent.host_name

    def unregister(self, handler: host_core.ClientHandler) -> None:
        self.agent._session_finished(handler)


class HostAgent:
    """Держит подключение к серверу и обслуживает входящие сеансы."""

    def __init__(self, app_dir: str, config: dict,
                 on_status: Optional[Callable[[dict], None]] = None,
                 on_log: Optional[Callable[[str, str], None]] = None,
                 on_session: Optional[Callable[[str, dict], None]] = None) -> None:
        self.app_dir = app_dir
        self.config = dict(config)
        self.on_status = on_status
        self.on_log = on_log
        self.on_session = on_session
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[relay_proto.ControlChannel] = None
        self._ping_stop: Optional[threading.Event] = None
        self._sessions: dict[int, host_core.ClientHandler] = {}
        self._state = "offline"
        self._hub = _Hub(self)
        self._last_error = ""

    # ------------------------------------------------------------------
    # свойства
    # ------------------------------------------------------------------

    @property
    def host_name(self) -> str:
        with self._lock:
            return str(self.config.get("host_name") or socket.gethostname())

    @property
    def read_only(self) -> bool:
        with self._lock:
            return bool(self.config.get("read_only"))

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    # ------------------------------------------------------------------
    # управление
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="host-agent", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 4.0) -> None:
        self._stop.set()
        self._wake.set()
        self._close_control()
        with self._lock:
            handlers = list(self._sessions.values())
        for handler in handlers:
            handler.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None
        self._set_state("offline", "Остановлено")

    def apply_config(self, config: dict) -> None:
        """Принять новые настройки. При изменении сети переподключиться."""
        with self._lock:
            old = self.config
            self.config = dict(config)
            network_changed = (
                str(old.get("server_host")) != str(config.get("server_host"))
                or int(old.get("server_port") or 0) != int(config.get("server_port") or 0)
                or str(old.get("password")) != str(config.get("password"))
            )
            name_changed = str(old.get("host_name")) != str(config.get("host_name"))
            self._hub.read_only = bool(config.get("read_only"))
        if network_changed:
            self._wake.set()
            self._close_control()
        elif name_changed:
            self._send_metadata()

    # ------------------------------------------------------------------
    # основной цикл
    # ------------------------------------------------------------------

    def _run(self) -> None:
        backoff = RECONNECT_MIN
        while not self._stop.is_set():
            with self._lock:
                config = dict(self.config)
            if not is_configured(config):
                self._set_state("disabled", "Укажите сервер и пароль")
                self._wait_wake(10.0)
                continue
            host, port = split_server(str(config["server_host"]),
                                      int(config.get("server_port") or DEFAULT_SERVER_PORT))
            if not host:
                self._set_state("disabled", "Не указан адрес сервера")
                self._wait_wake(10.0)
                continue
            self._set_state("connecting", f"Подключение к {host}:{port}…")
            sock = None
            try:
                sock = socket.create_connection((host, port), timeout=12.0)
                conn = relay_proto.client_login(
                    sock, "host", self.host_name, str(config.get("password") or ""),
                    host_id=str(config.get("host_id") or ""),
                )
                with self._lock:
                    self._conn = conn
                self._last_error = ""
                backoff = RECONNECT_MIN
                self._set_state("online", f"Подключено к {host}:{port}")
                self._log(f"Подключение к серверу {host}:{port} установлено")
                self._send_metadata()
                ping_stop = threading.Event()
                self._ping_stop = ping_stop
                threading.Thread(
                    target=self._ping_loop, args=(conn, ping_stop),
                    name="host-ping", daemon=True,
                ).start()
                self._read_control(conn)
            except relay_proto.AuthError as exc:
                self._last_error = str(exc)
                self._set_state("auth_error", f"Сервер отклонил пароль: {exc}")
                self._log(f"Ошибка входа на сервер: {exc}", "error")
                backoff = 20.0
            except (socket.timeout, TimeoutError):
                self._set_state("offline", "Сервер не ответил")
                self._log("Сервер не ответил вовремя", "warn")
            except (OSError, ConnectionError, relay_proto.RelayError) as exc:
                if not self._stop.is_set():
                    self._set_state("offline", "Нет связи с сервером")
                    self._log(f"Связь с сервером потеряна: {exc}", "warn")
            except Exception as exc:  # noqa: BLE001
                self._set_state("offline", "Внутренняя ошибка")
                self._log(f"Ошибка агента: {type(exc).__name__}: {exc}", "error")
            finally:
                self._close_control()
                self._close_sessions("связь с сервером потеряна")
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if self._stop.is_set():
                break
            self._set_state("offline", f"Переподключение через {int(backoff)} с…")
            self._wait_wake(backoff)
            backoff = min(RECONNECT_MAX, backoff * 1.7)

    def _read_control(self, conn: relay_proto.ControlChannel) -> None:
        while not self._stop.is_set():
            message = conn.recv()
            mtype = message.get("type")
            if mtype == "session_open":
                self._start_session(message)
            elif mtype == "session_closed":
                self._log(f"Сеанс завершён сервером: {message.get('reason', '')}")
            elif mtype == "pong":
                continue
            elif mtype == "welcome":
                continue
            elif mtype == "error":
                self._log(f"Сервер: {message.get('error', '')}", "error")
            else:
                self._log(f"Неизвестное сообщение сервера: {mtype!r}", "warn")

    def _ping_loop(self, conn: relay_proto.ControlChannel, stop: threading.Event) -> None:
        while not stop.wait(PING_INTERVAL):
            try:
                conn.send("ping", time=int(time.time()))
            except (OSError, ConnectionError, relay_proto.ProtocolError):
                return

    def _close_control(self) -> None:
        with self._lock:
            conn = self._conn
            self._conn = None
            ping_stop = self._ping_stop
            self._ping_stop = None
        if ping_stop is not None:
            ping_stop.set()
        if conn is not None:
            conn.close()

    def _wait_wake(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    # ------------------------------------------------------------------
    # сеансы
    # ------------------------------------------------------------------

    def _start_session(self, message: dict) -> None:
        try:
            token = bytes.fromhex(str(message.get("host_token") or ""))
        except ValueError:
            self._log("Сервер прислал некорректный токен сеанса", "error")
            return
        if len(token) != relay_proto.DATA_TOKEN_LEN:
            self._log("Сервер прислал токен неверной длины", "error")
            return
        session_key = ""
        raw_key = str(message.get("session_key") or "")
        if raw_key:
            try:
                if len(bytes.fromhex(raw_key)) == 32:
                    session_key = raw_key
            except ValueError:
                session_key = ""
        viewer = str(message.get("viewer") or "вьювер")[:64]
        session_id = str(message.get("session_id") or "")
        with self._lock:
            config = dict(self.config)
            if len(self._sessions) >= 2:
                self._log("Слишком много одновременных сеансов", "warn")
                return
        self._log(f"Запрос сеанса от «{viewer}»")
        self._notify_session("opening", {"viewer": viewer, "session_id": session_id})
        thread = threading.Thread(
            target=self._session_worker,
            args=(session_id, viewer, token, session_key, config),
            name=f"host-session-{session_id[:6]}", daemon=True,
        )
        thread.start()

    def _session_worker(self, session_id: str, viewer: str, token: bytes,
                        session_key: str, config: dict) -> None:
        host, port = split_server(str(config.get("server_host") or ""),
                                  int(config.get("server_port") or DEFAULT_SERVER_PORT))
        sock = None
        handler: Optional[host_core.ClientHandler] = None
        try:
            sock = socket.create_connection((host, port), timeout=12.0)
            relay_proto.send_data_hello(sock, token)
            secret = session_key or str(config.get("password") or "")
            iterations = 1 if session_key else crypto.PBKDF2_ITERATIONS
            conn, name = protocol.server_handshake(
                sock, secret, self.host_name, iterations=iterations
            )
            sock = None  # теперь владелец — Conn
            self._hub.read_only = bool(config.get("read_only"))
            handler = host_core.ClientHandler(
                self._hub, conn, ("relay", 0), name or viewer
            )
            handler.session_id = session_id
            with self._lock:
                self._sessions[id(handler)] = handler
            self._log(f"Сеанс с «{handler.name}» начат")
            self._notify_session("connected", {"viewer": handler.name, "session_id": session_id})
            handler.run()
        except (OSError, ConnectionError, protocol.ProtocolError,
                protocol.AuthError, relay_proto.RelayError) as exc:
            self._log(f"Не удалось открыть сеанс: {exc}", "warn")
            self._notify_session("failed", {"viewer": viewer, "session_id": session_id,
                                            "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._log(f"Ошибка сеанса: {type(exc).__name__}: {exc}", "error")
        finally:
            if handler is None and sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            if handler is not None:
                self._session_finished(handler)

    def _session_finished(self, handler: host_core.ClientHandler) -> None:
        with self._lock:
            existed = self._sessions.pop(id(handler), None)
        if existed is not None:
            self._log(f"Сеанс с «{handler.name}» завершён")
            self._notify_session("closed", {
                "viewer": handler.name,
                "session_id": getattr(handler, "session_id", ""),
            })

    def _close_sessions(self, reason: str) -> None:
        with self._lock:
            handlers = list(self._sessions.values())
        for handler in handlers:
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
        if handlers:
            self._log(f"Сеансы закрыты: {reason}", "warn")

    # ------------------------------------------------------------------
    # метаданные и события
    # ------------------------------------------------------------------

    def _send_metadata(self) -> None:
        with self._lock:
            conn = self._conn
            config = dict(self.config)
        if conn is None:
            return
        metadata = {
            "name": str(config.get("host_name") or socket.gethostname())[:64],
            "user": _current_user()[:128],
            "os": _os_name()[:128],
            "version": __version__,
            "read_only": bool(config.get("read_only")),
        }
        try:
            conn.send("metadata", **metadata)
        except (OSError, ConnectionError, relay_proto.ProtocolError):
            conn.close()

    def _set_state(self, state: str, text: str) -> None:
        with self._lock:
            self._state = state
        payload = {
            "state": state,
            "text": text,
            "host_name": self.host_name,
            "server": server_display(self.config),
            "sessions": self.session_count,
            "read_only": self.read_only,
        }
        if self.on_status is not None:
            try:
                self.on_status(payload)
            except Exception:  # noqa: BLE001
                pass

    def _log(self, text: str, level: str = "info") -> None:
        try:
            host_core.log(text)
        except Exception:  # noqa: BLE001
            pass
        if self.on_log is not None:
            try:
                self.on_log(level, text)
            except Exception:  # noqa: BLE001
                pass

    def _notify_session(self, kind: str, data: dict) -> None:
        if self.on_session is not None:
            try:
                self.on_session(kind, data)
            except Exception:  # noqa: BLE001
                pass


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USERNAME", "")


def _os_name() -> str:
    try:
        return platform.platform(terse=True)
    except Exception:  # noqa: BLE001
        return platform.system() + " " + platform.release()
