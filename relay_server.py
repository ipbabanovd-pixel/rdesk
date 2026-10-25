"""Центральный relay-сервер rdesk.

Сервер не захватывает экран и не управляет компьютерами. Его задачи:
    * принять исходящие подключения хостов и вьюверов;
    * проверить общий пароль сервера;
    * показать вьюверу список компьютеров;
    * свести хост и вьювер по одноразовым токенам и пересылать байты.

Трафик удалённого сеанса остаётся сквозным: сервер видит только шифрованные
сообщения rdesk и не знает ни пароля сеанса, ни содержимого экрана.
"""

from __future__ import annotations

import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import relay_proto

MAX_HOSTS = 500
MAX_VIEWERS = 100
AUTH_FAIL_LIMIT = 8
AUTH_WINDOW = 300.0
SESSION_WAIT = 25.0
BRIDGE_BUFFER = 64 * 1024


@dataclass
class _Host:
    host_id: str
    name: str
    addr: tuple[str, int]
    conn: relay_proto.ControlChannel
    user: str = ""
    os_name: str = ""
    version: str = ""
    read_only: bool = False
    connected_at: float = field(default_factory=time.time)
    session_id: str = ""
    replaced: bool = False

    @property
    def ip(self) -> str:
        return self.addr[0]

    def public(self) -> dict:
        return {
            "id": self.host_id,
            "name": self.name,
            "user": self.user,
            "os": self.os_name,
            "version": self.version,
            "ip": self.ip,
            "connected_at": int(self.connected_at),
            "busy": bool(self.session_id),
            "read_only": self.read_only,
        }


@dataclass
class _Viewer:
    viewer_id: str
    name: str
    addr: tuple[str, int]
    conn: relay_proto.ControlChannel
    connected_at: float = field(default_factory=time.time)
    session_id: str = ""

    @property
    def ip(self) -> str:
        return self.addr[0]


@dataclass
class _Pending:
    session_id: str
    host: _Host
    viewer: _Viewer
    host_token: bytes
    viewer_token: bytes
    session_key: bytes
    created: float = field(default_factory=time.time)
    host_sock: Optional[socket.socket] = None
    viewer_sock: Optional[socket.socket] = None
    paired: bool = False
    finished: bool = False


class RelayServer:
    """Слушающий сервер. Потокобезопасен, все события отдаёт в callback."""

    def __init__(self, bind: str, port: int, password: str, server_name: str,
                 on_event: Optional[Callable[[str, Any], None]] = None) -> None:
        self.bind = bind
        self.port = int(port)
        self.password = password
        self.server_name = server_name
        self.on_event = on_event
        self._sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._janitor_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._hosts: dict[str, _Host] = {}
        self._viewers: dict[str, _Viewer] = {}
        self._sessions: dict[str, _Pending] = {}
        self._host_tokens: dict[bytes, _Pending] = {}
        self._viewer_tokens: dict[bytes, _Pending] = {}
        self._failures: dict[str, list[float]] = {}
        self._started_at = time.time()
        self._total_sessions = 0

    # ------------------------------------------------------------------
    # события и журнал
    # ------------------------------------------------------------------

    def _emit(self, kind: str, payload: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:  # noqa: BLE001
            pass

    def log(self, message: str, level: str = "info") -> None:
        self._emit("log", {"level": level, "text": message})

    # ------------------------------------------------------------------
    # запуск и остановка
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.bind, self.port))
            sock.listen(128)
            sock.settimeout(1.0)
        except OSError:
            sock.close()
            raise
        self.port = sock.getsockname()[1]
        self._sock = sock
        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="relay-accept", daemon=True
        )
        self._accept_thread.start()
        self._janitor_thread = threading.Thread(
            target=self._janitor_loop, name="relay-janitor", daemon=True
        )
        self._janitor_thread.start()
        self.log(f"Сервер «{self.server_name}» запущен на {self.bind}:{self.port}", "ok")

    def shutdown(self) -> None:
        if self._stop.is_set() and self._sock is None:
            return
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            hosts = list(self._hosts.values())
            viewers = list(self._viewers.values())
            sessions = list(self._sessions.values())
            self._hosts.clear()
            self._viewers.clear()
            self._sessions.clear()
            self._host_tokens.clear()
            self._viewer_tokens.clear()
        for pending in sessions:
            self._close_pending_sockets(pending)
        for host in hosts:
            host.conn.close()
        for viewer in viewers:
            viewer.conn.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
        self._accept_thread = None
        self.log("Сервер остановлен", "warn")

    @property
    def running(self) -> bool:
        return self._sock is not None

    # ------------------------------------------------------------------
    # приём подключений
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                client, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_connection, args=(client, addr),
                name=f"relay-conn-{addr[0]}", daemon=True,
            ).start()

    def _handle_connection(self, sock: socket.socket, addr: tuple[str, int]) -> None:
        try:
            sock.settimeout(relay_proto.HANDSHAKE_TIMEOUT)
            magic = relay_proto.recv_exact(sock, 4)
            if magic == relay_proto.MAGIC_CONTROL:
                self._control_connection(sock, addr)
                return
            if magic == relay_proto.MAGIC_DATA:
                token = relay_proto.recv_data_hello(sock)
                self._data_connection(sock, addr, token)
                return
            self.log(f"{addr[0]}: неизвестный тип подключения", "warn")
        except (OSError, ConnectionError, relay_proto.RelayError):
            pass
        except Exception as exc:  # noqa: BLE001
            self.log(f"{addr[0]}: ошибка подключения: {type(exc).__name__}: {exc}", "error")
        finally:
            # Если сокет был передан в мост, тот сам закроет его. Здесь
            # закрываем только те, что остались непристроенными.
            try:
                if sock.fileno() != -1 and not self._socket_in_use(sock):
                    sock.close()
            except OSError:
                pass

    def _socket_in_use(self, sock: socket.socket) -> bool:
        with self._lock:
            for pending in self._sessions.values():
                if pending.host_sock is sock or pending.viewer_sock is sock:
                    return True
        return False

    # ------------------------------------------------------------------
    # защита от перебора
    # ------------------------------------------------------------------

    def _blocked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            attempts = [t for t in self._failures.get(ip, []) if now - t < AUTH_WINDOW]
            if attempts:
                self._failures[ip] = attempts
            else:
                self._failures.pop(ip, None)
            return len(attempts) >= AUTH_FAIL_LIMIT

    def _failed(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, []).append(time.time())

    def _success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)

    # ------------------------------------------------------------------
    # управляющие подключения
    # ------------------------------------------------------------------

    def _control_connection(self, sock: socket.socket, addr: tuple[str, int]) -> None:
        ip = addr[0]
        if self._blocked(ip):
            self.log(f"{ip}: слишком много неудачных попыток входа, подключение отклонено", "warn")
            sock.close()
            return
        try:
            conn, hello = relay_proto.server_login(sock, self.password, self.server_name)
        except relay_proto.AuthError as exc:
            self._failed(ip)
            self.log(f"{ip}: отказ во входе: {exc}", "warn")
            sock.close()
            return
        except (relay_proto.ProtocolError, OSError, ConnectionError) as exc:
            self.log(f"{ip}: ошибка рукопожатия: {exc}", "warn")
            sock.close()
            return
        self._success(ip)
        role = str(hello.get("role"))
        name = str(hello.get("name") or role)
        try:
            conn.send("welcome", server=self.server_name, version=relay_proto.PROTOCOL_VERSION,
                      role=role, time=int(time.time()))
        except (OSError, ConnectionError, relay_proto.ProtocolError):
            conn.close()
            return

        if role == "viewer":
            viewer = _Viewer(secrets.token_hex(8), name, addr, conn)
            with self._lock:
                if len(self._viewers) >= MAX_VIEWERS:
                    conn.close()
                    self.log(f"{ip}: отклонён вьювер — достигнут лимит {MAX_VIEWERS}", "warn")
                    return
                self._viewers[viewer.viewer_id] = viewer
            self.log(f"Вьювер «{name}» ({ip}) подключился")
            try:
                self._send_host_list(viewer)
                self._viewer_loop(viewer)
            finally:
                self._remove_viewer(viewer)
            return

        host_id = str(hello.get("host_id") or "")[:64]
        host = _Host(host_id, name, addr, conn)
        old: Optional[_Host] = None
        with self._lock:
            old = self._hosts.get(host_id)
            if old is None and len(self._hosts) >= MAX_HOSTS:
                conn.close()
                self.log(f"{ip}: отклонён хост — достигнут лимит {MAX_HOSTS}", "warn")
                return
            self._hosts[host_id] = host
        if old is not None:
            old.replaced = True
            if old.session_id:
                self._end_session(old.session_id, "хост переподключился")
            old.conn.close()
            self.log(f"Хост «{name}» переподключился, старое соединение закрыто")
        self.log(f"Хост «{name}» ({ip}) подключился")
        self._broadcast_hosts()
        try:
            self._host_loop(host)
        finally:
            self._remove_host(host)

    def _host_loop(self, host: _Host) -> None:
        conn = host.conn
        conn.settimeout(relay_proto.CONTROL_TIMEOUT)
        while not self._stop.is_set() and not host.replaced:
            try:
                message = conn.recv()
            except socket.timeout:
                self.log(f"Хост «{host.name}» не отвечает, соединение закрыто", "warn")
                break
            except (OSError, ConnectionError, relay_proto.ProtocolError):
                break
            mtype = message.get("type")
            if mtype == "metadata":
                with self._lock:
                    host.name = str(message.get("name") or host.name)[:64]
                    host.user = str(message.get("user") or "")[:128]
                    host.os_name = str(message.get("os") or "")[:128]
                    host.version = str(message.get("version") or "")[:32]
                    host.read_only = bool(message.get("read_only"))
                self._broadcast_hosts()
            elif mtype == "ping":
                try:
                    conn.send("pong", time=int(time.time()))
                except (OSError, ConnectionError, relay_proto.ProtocolError):
                    break
            elif mtype == "bye":
                break
            elif mtype in ("session_data_ok", "session_data_error"):
                if mtype.endswith("error"):
                    self.log(f"Хост «{host.name}» не смог открыть канал данных: "
                             f"{message.get('error', '')}", "warn")
            else:
                self.log(f"Хост «{host.name}»: неизвестное сообщение {mtype!r}", "warn")
        conn.close()

    def _viewer_loop(self, viewer: _Viewer) -> None:
        conn = viewer.conn
        conn.settimeout(relay_proto.CONTROL_TIMEOUT)
        while not self._stop.is_set():
            try:
                message = conn.recv()
            except socket.timeout:
                self.log(f"Вьювер «{viewer.name}» не отвечает, соединение закрыто", "warn")
                break
            except (OSError, ConnectionError, relay_proto.ProtocolError):
                break
            mtype = message.get("type")
            if mtype == "connect":
                self._begin_session(viewer, str(message.get("host_id") or ""))
            elif mtype == "list":
                self._send_host_list(viewer)
            elif mtype == "cancel":
                if viewer.session_id:
                    self._end_session(viewer.session_id, "вьювер отменил подключение")
            elif mtype == "ping":
                try:
                    conn.send("pong", time=int(time.time()))
                except (OSError, ConnectionError, relay_proto.ProtocolError):
                    break
            elif mtype == "bye":
                break
        conn.close()

    # ------------------------------------------------------------------
    # список компьютеров
    # ------------------------------------------------------------------

    def _hosts_snapshot(self) -> list[dict]:
        with self._lock:
            hosts = [host.public() for host in self._hosts.values()]
        hosts.sort(key=lambda item: (item["name"].lower(), item["user"].lower()))
        return hosts

    def _send_host_list(self, viewer: _Viewer) -> None:
        try:
            viewer.conn.send("host_list", hosts=self._hosts_snapshot(),
                             server=self.server_name, time=int(time.time()))
        except (OSError, ConnectionError, relay_proto.ProtocolError):
            viewer.conn.close()

    def _broadcast_hosts(self) -> None:
        snapshot = self._hosts_snapshot()
        with self._lock:
            viewers = list(self._viewers.values())
        now = int(time.time())
        for viewer in viewers:
            try:
                viewer.conn.send("host_list", hosts=snapshot, server=self.server_name, time=now)
            except (OSError, ConnectionError, relay_proto.ProtocolError):
                viewer.conn.close()
        self._emit("hosts", snapshot)

    # ------------------------------------------------------------------
    # сеансы
    # ------------------------------------------------------------------

    def _begin_session(self, viewer: _Viewer, host_id: str) -> None:
        if viewer.session_id:
            self._send_error(viewer, "У вас уже есть активный сеанс")
            return
        with self._lock:
            host = self._hosts.get(host_id)
            if host is None:
                self._send_error(viewer, "Компьютер не найден или уже отключился")
                return
            if host.session_id:
                self._send_error(viewer, "На этом компьютере уже идёт сеанс")
                return
            session_id = secrets.token_hex(8)
            pending = _Pending(
                session_id=session_id,
                host=host,
                viewer=viewer,
                host_token=secrets.token_bytes(relay_proto.DATA_TOKEN_LEN),
                viewer_token=secrets.token_bytes(relay_proto.DATA_TOKEN_LEN),
                session_key=secrets.token_bytes(32),
            )
            self._sessions[session_id] = pending
            self._host_tokens[pending.host_token] = pending
            self._viewer_tokens[pending.viewer_token] = pending
            host.session_id = session_id
            viewer.session_id = session_id
            self._total_sessions += 1
        ok_host = True
        ok_viewer = True
        try:
            host.conn.send(
                "session_open",
                session_id=session_id,
                host_token=pending.host_token.hex(),
                session_key=pending.session_key.hex(),
                viewer=viewer.name,
                viewer_ip=viewer.ip,
            )
        except (OSError, ConnectionError, relay_proto.ProtocolError):
            ok_host = False
        if ok_host:
            try:
                viewer.conn.send(
                    "session_ready",
                    session_id=session_id,
                    viewer_token=pending.viewer_token.hex(),
                    session_key=pending.session_key.hex(),
                    host=host.public(),
                )
            except (OSError, ConnectionError, relay_proto.ProtocolError):
                ok_viewer = False
        if not (ok_host and ok_viewer):
            self._end_session(session_id, "не удалось начать сеанс")
            return
        self.log(f"Сеанс {session_id}: «{viewer.name}» → «{host.name}»")
        self._broadcast_hosts()

    def _send_error(self, viewer: _Viewer, text: str) -> None:
        try:
            viewer.conn.send("session_error", error=text)
        except (OSError, ConnectionError, relay_proto.ProtocolError):
            viewer.conn.close()

    def _data_connection(self, sock: socket.socket, addr: tuple[str, int],
                         token: bytes) -> None:
        pending: Optional[_Pending] = None
        side = ""
        with self._lock:
            pending = self._host_tokens.get(token)
            if pending is not None:
                side = "host"
                if pending.host_sock is not None:
                    pending = None
                else:
                    pending.host_sock = sock
            else:
                pending = self._viewer_tokens.get(token)
                if pending is not None:
                    side = "viewer"
                    if pending.viewer_sock is not None:
                        pending = None
                    else:
                        pending.viewer_sock = sock
        if pending is None:
            self.log(f"{addr[0]}: канал данных с неизвестным или повторно использованным токеном", "warn")
            sock.close()
            return
        # IP хоста и вьювера могут совпадать (например, за одним NAT), поэтому
        # проверяем только факт наличия обеих сторон. Токены сторонам выданы
        # раздельно и не взаимозаменяемы.
        with self._lock:
            both = pending.host_sock is not None and pending.viewer_sock is not None
            if both:
                pending.paired = True
                self._host_tokens.pop(pending.host_token, None)
                self._viewer_tokens.pop(pending.viewer_token, None)
        if not both:
            return
        self.log(f"Сеанс {pending.session_id}: канал данных открыт ({side} со стороны {addr[0]})")
        self._bridge(pending)

    def _bridge(self, pending: _Pending) -> None:
        host_sock = pending.host_sock
        viewer_sock = pending.viewer_sock
        if host_sock is None or viewer_sock is None:
            self._end_session(pending.session_id, "канал данных не открыт")
            return
        for sock in (host_sock, viewer_sock):
            try:
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        stop = threading.Event()

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while not stop.is_set():
                    data = src.recv(BRIDGE_BUFFER)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                stop.set()

        t1 = threading.Thread(target=pump, args=(host_sock, viewer_sock),
                              name=f"bridge-{pending.session_id}-hv", daemon=True)
        t2 = threading.Thread(target=pump, args=(viewer_sock, host_sock),
                              name=f"bridge-{pending.session_id}-vh", daemon=True)
        t1.start()
        t2.start()
        t1.join()
        # Даём второй стороне короткое время на штатное закрытие, затем
        # принудительно будим её, чтобы не держать сокеты и потоки.
        t2.join(timeout=2.0)
        self._close_pending_sockets(pending)
        t2.join(timeout=1.0)
        self._end_session(pending.session_id, "сеанс завершён")

    def _close_pending_sockets(self, pending: _Pending) -> None:
        for name in ("host_sock", "viewer_sock"):
            sock = getattr(pending, name)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(pending, name, None)

    def _end_session(self, session_id: str, reason: str) -> None:
        pending: Optional[_Pending]
        with self._lock:
            pending = self._sessions.pop(session_id, None)
            if pending is None:
                return
            self._host_tokens.pop(pending.host_token, None)
            self._viewer_tokens.pop(pending.viewer_token, None)
            if pending.host.session_id == session_id:
                pending.host.session_id = ""
            if pending.viewer.session_id == session_id:
                pending.viewer.session_id = ""
            pending.finished = True
        self._close_pending_sockets(pending)
        try:
            pending.host.conn.send("session_closed", session_id=session_id, reason=reason)
        except (OSError, ConnectionError, relay_proto.ProtocolError):
            pass
        try:
            pending.viewer.conn.send("session_closed", session_id=session_id, reason=reason)
        except (OSError, ConnectionError, relay_proto.ProtocolError):
            pass
        self.log(f"Сеанс {session_id} завершён: {reason}")
        self._broadcast_hosts()

    # ------------------------------------------------------------------
    # удаление клиентов
    # ------------------------------------------------------------------

    def _remove_host(self, host: _Host) -> None:
        with self._lock:
            if self._hosts.get(host.host_id) is not host:
                return
            self._hosts.pop(host.host_id, None)
        if host.session_id:
            self._end_session(host.session_id, "хост отключился")
        self.log(f"Хост «{host.name}» ({host.ip}) отключился", "warn")
        self._broadcast_hosts()

    def _remove_viewer(self, viewer: _Viewer) -> None:
        with self._lock:
            self._viewers.pop(viewer.viewer_id, None)
        if viewer.session_id:
            self._end_session(viewer.session_id, "вьювер отключился")
        self.log(f"Вьювер «{viewer.name}» ({viewer.ip}) отключился", "warn")

    # ------------------------------------------------------------------
    # фоновая уборка
    # ------------------------------------------------------------------

    def _janitor_loop(self) -> None:
        while not self._stop.wait(3.0):
            now = time.time()
            expired: list[str] = []
            with self._lock:
                for session_id, pending in self._sessions.items():
                    if not pending.paired and now - pending.created > SESSION_WAIT:
                        expired.append(session_id)
            for session_id in expired:
                self._end_session(session_id, "истекло время ожидания канала данных")

    def disconnect_host(self, host_id: str) -> bool:
        """Принудительно отключить хост (кнопка в интерфейсе сервера)."""
        with self._lock:
            host = self._hosts.get(host_id)
        if host is None:
            return False
        self.log(f"Хост «{host.name}» отключён администратором", "warn")
        host.conn.close()
        return True

    # ------------------------------------------------------------------
    # статистика для интерфейса
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "hosts": len(self._hosts),
                "viewers": len(self._viewers),
                "sessions": len(self._sessions),
                "total_sessions": self._total_sessions,
                "uptime": int(time.time() - self._started_at) if self.running else 0,
                "port": self.port,
            }
