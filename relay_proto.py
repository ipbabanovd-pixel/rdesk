"""Протокол центрального сервера rdesk.

Схема
-----
Один TCP-порт (по умолчанию 7443) принимает три вида подключений:

* ``RDS1`` — управляющее подключение хоста или вьювера. Пароль сервера
  проверяется взаимным HMAC-рукопожатием, дальше сообщения шифруются тем же
  кодом, что и сеанс удалённого доступа (PBKDF2 + SHAKE-256 + HMAC).
* ``RDD1`` — канал данных. Хост и вьювер открывают его по одноразовым
  криптостойким токенам, которые сервер раздаёт каждой стороне отдельно.
  Сервер только пересылает байты, не расшифровывая их: внутри канала
  работает обычное сквозное шифрование rdesk.

Пароль сервера нигде не передаётся открытым текстом: обе стороны
доказывают знание пароля HMAC-подписью.
"""

from __future__ import annotations

import hmac
import json
import os
import socket
import struct
import threading
from typing import Any, Optional

from . import crypto

PROTOCOL_VERSION = 2
DEFAULT_PORT = 7443
MAGIC_CONTROL = b"RDS1"
MAGIC_DATA = b"RDD1"
DATA_TOKEN_LEN = 32
MAX_CONTROL_MESSAGE = 1024 * 1024
HANDSHAKE_TIMEOUT = 20.0
CONTROL_TIMEOUT = 60.0
CLIENT_LABEL = b"rdesk-relay-client"
SERVER_LABEL = b"rdesk-relay-server"


class RelayError(Exception):
    """Общая ошибка протокола сервера."""


class ProtocolError(RelayError):
    """Нарушен формат сообщений."""


class AuthError(RelayError):
    """Неверный пароль сервера или отказ в доступе."""


# --------------------------------------------------------------------------
# кадрирование
# --------------------------------------------------------------------------

def send_frame(sock: socket.socket, body: bytes) -> None:
    if len(body) > MAX_CONTROL_MESSAGE:
        raise ProtocolError("слишком большое управляющее сообщение")
    sock.sendall(struct.pack(">I", len(body)) + body)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray(size)
    view = memoryview(buf)
    got = 0
    while got < size:
        n = sock.recv_into(view[got:], size - got)
        if n == 0:
            raise ConnectionError("соединение закрыто другой стороной")
        got += n
    return bytes(buf)


def recv_frame(sock: socket.socket, limit: int = MAX_CONTROL_MESSAGE) -> bytes:
    (length,) = struct.unpack(">I", recv_exact(sock, 4))
    if length == 0 or length > limit:
        raise ProtocolError(f"недопустимая длина сообщения: {length}")
    return recv_exact(sock, length)


def _encode_json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode_json(payload: bytes) -> dict:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("некорректный JSON от сервера") from exc
    if not isinstance(value, dict):
        raise ProtocolError("ожидался JSON-объект")
    return value


def _send_plain(sock: socket.socket, obj: Any) -> None:
    send_frame(sock, _encode_json(obj))


def _recv_plain(sock: socket.socket) -> dict:
    return _decode_json(recv_frame(sock))


def _hex(value: bytes) -> str:
    return value.hex()


def _unhex(value: Any, length: int, what: str) -> bytes:
    if not isinstance(value, str):
        raise ProtocolError(f"некорректное поле {what}")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise ProtocolError(f"некорректное поле {what}") from exc
    if len(raw) != length:
        raise ProtocolError(f"некорректная длина поля {what}")
    return raw


# --------------------------------------------------------------------------
# шифрованный управляющий канал
# --------------------------------------------------------------------------

class ControlChannel:
    """Шифрованный канал управляющих JSON-сообщений после рукопожатия."""

    def __init__(self, sock: socket.socket, session: crypto.Session, role: str) -> None:
        self.sock = sock
        self.session = session
        self.role = role
        if role == "client":
            self._send_cipher = session.client_to_server
            self._recv_cipher = session.server_to_client
        elif role == "server":
            self._send_cipher = session.server_to_client
            self._recv_cipher = session.client_to_server
        else:
            raise ValueError("role must be 'client' or 'server'")
        self._send_lock = threading.Lock()
        self._closed = False

    def send(self, mtype: str, **fields: Any) -> None:
        if self._closed:
            raise ConnectionError("канал уже закрыт")
        obj = {"type": mtype}
        obj.update(fields)
        sealed = self._send_cipher.seal(_encode_json(obj))
        with self._send_lock:
            send_frame(self.sock, sealed)

    def recv(self) -> dict:
        body = recv_frame(self.sock)
        try:
            plain = self._recv_cipher.open(body)
        except crypto.CryptoError as exc:
            raise ProtocolError(str(exc)) from exc
        message = _decode_json(plain)
        if not isinstance(message.get("type"), str):
            raise ProtocolError("в сообщении нет поля type")
        return message

    def settimeout(self, value: Optional[float]) -> None:
        self.sock.settimeout(value)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed


# --------------------------------------------------------------------------
# рукопожатие
# --------------------------------------------------------------------------

def _transcript(role: str, name: str, host_id: str, salt: bytes,
                server_nonce: bytes, client_nonce: bytes) -> bytes:
    # crypto.transcript уже связывает соль и оба nonce; роль и id добавляем
    # в имя, чтобы клиент не мог переиспользовать подпись в другой роли.
    label = f"{role}|{host_id}|{name}"[:255]
    return crypto.transcript(label, salt, server_nonce, client_nonce)


def client_login(sock: socket.socket, role: str, name: str, password: str,
                 host_id: str = "") -> ControlChannel:
    """Вход хоста/вьювера на сервер. Возвращает шифрованный канал."""
    if role not in ("host", "viewer"):
        raise ValueError("unknown role")
    sock.settimeout(HANDSHAKE_TIMEOUT)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.sendall(MAGIC_CONTROL)
    name = name[:64] or role
    _send_plain(sock, {
        "type": "hello",
        "version": PROTOCOL_VERSION,
        "role": role,
        "name": name,
        "host_id": host_id[:64],
        "app": "rdesk",
    })

    response = _recv_plain(sock)
    if response.get("type") == "error":
        raise AuthError(str(response.get("error") or "сервер отклонил подключение"))
    if response.get("type") != "challenge":
        raise ProtocolError("сервер не прислал challenge")

    salt = _unhex(response.get("salt"), crypto.SALT_LEN, "salt")
    server_nonce = _unhex(response.get("nonce"), crypto.NONCE_RANDOM_LEN, "nonce")
    if int(response.get("version", 0)) != PROTOCOL_VERSION:
        raise ProtocolError("версия протокола сервера не поддерживается")

    session = crypto.Session(password, salt)
    client_nonce = os.urandom(crypto.NONCE_RANDOM_LEN)
    transcript = _transcript(role, name, host_id[:64], salt, server_nonce, client_nonce)
    proof = session.sign(CLIENT_LABEL, transcript)
    _send_plain(sock, {
        "type": "auth",
        "nonce": _hex(client_nonce),
        "proof": _hex(proof),
    })

    response = _recv_plain(sock)
    if response.get("type") == "error":
        raise AuthError(str(response.get("error") or "неверный пароль сервера"))
    if response.get("type") != "auth_ok":
        raise ProtocolError("сервер не подтвердил вход")
    expected = session.sign(SERVER_LABEL, transcript)
    received = _unhex(response.get("proof"), 32, "proof")
    if not hmac.compare_digest(expected, received):
        raise AuthError("сервер не подтвердил знание пароля (возможна подмена сервера)")

    channel = ControlChannel(sock, session, role="client")
    channel.settimeout(None)
    return channel


def server_login(sock: socket.socket, password: str,
                 server_name: str) -> tuple[ControlChannel, dict]:
    """Принять управляющее подключение и проверить пароль сервера."""
    sock.settimeout(HANDSHAKE_TIMEOUT)
    hello = _recv_plain(sock)
    if hello.get("type") != "hello":
        raise ProtocolError("ожидался hello")
    if hello.get("app") != "rdesk":
        raise AuthError("неизвестный клиент")
    if int(hello.get("version", 0)) != PROTOCOL_VERSION:
        _send_plain(sock, {"type": "error", "error": "версия программы не совпадает с сервером"})
        raise AuthError("версия протокола не совпадает")
    role = str(hello.get("role", ""))
    if role not in ("host", "viewer"):
        raise AuthError("неизвестная роль клиента")
    name = str(hello.get("name", ""))[:64] or role
    host_id = str(hello.get("host_id", ""))[:64]
    if role == "host" and not host_id:
        raise AuthError("хост не передал идентификатор")

    salt = crypto.new_salt()
    server_nonce = crypto.new_nonce()
    _send_plain(sock, {
        "type": "challenge",
        "version": PROTOCOL_VERSION,
        "salt": _hex(salt),
        "nonce": _hex(server_nonce),
        "server": server_name[:64],
    })

    response = _recv_plain(sock)
    if response.get("type") != "auth":
        raise ProtocolError("ожидался auth")
    client_nonce = _unhex(response.get("nonce"), crypto.NONCE_RANDOM_LEN, "nonce")
    proof = _unhex(response.get("proof"), 32, "proof")

    session = crypto.Session(password, salt)
    transcript = _transcript(role, name, host_id, salt, server_nonce, client_nonce)
    if not session.verify(CLIENT_LABEL, transcript, proof):
        _send_plain(sock, {"type": "error", "error": "неверный пароль сервера"})
        raise AuthError("неверный пароль сервера")

    _send_plain(sock, {
        "type": "auth_ok",
        "proof": _hex(session.sign(SERVER_LABEL, transcript)),
    })
    channel = ControlChannel(sock, session, role="server")
    channel.settimeout(None)
    return channel, hello


# --------------------------------------------------------------------------
# канал данных
# --------------------------------------------------------------------------

def send_data_hello(sock: socket.socket, token: bytes) -> None:
    if len(token) != DATA_TOKEN_LEN:
        raise ValueError("token must be 32 bytes")
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.sendall(MAGIC_DATA + token)


def recv_data_hello(sock: socket.socket) -> bytes:
    token = recv_exact(sock, DATA_TOKEN_LEN)
    return token
