"""Протокол обмена: кадрирование сообщений, рукопожатие, кодирование данных.

Формат кадра в сокете:
    [длина: 4 байта BE][тело: длина байт]

Во время рукопожатия тело = [тип: 1 байт][данные].
После рукопожатия тело = зашифровано(ciphertext + тег), а тип находится
внутри зашифрованных данных, т.е. тип тоже защищён от подмены.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
from enum import IntEnum
from typing import Any, Iterable, Optional

from . import crypto

PROTOCOL_VERSION = crypto.PROTOCOL_VERSION
MAX_MESSAGE = 32 * 1024 * 1024  # 32 МБ — защита от «бомб» в потоке
HANDSHAKE_TIMEOUT = 20.0


class ProtocolError(Exception):
    """Нарушение формата или порядка сообщений."""


class AuthError(Exception):
    """Неверный пароль или отказ в доступе."""


class Msg(IntEnum):
    # рукопожатие (открытым текстом)
    HELLO = 1
    CHALLENGE = 2
    AUTH = 3
    AUTH_OK = 4
    AUTH_FAIL = 5
    # видеопоток
    CFG = 10
    SCREEN = 11
    FRAME = 12
    ACK = 13
    KEYFRAME = 14
    # ввод
    INPUT_MOUSE = 20
    INPUT_KEY = 21
    CLIP = 22
    # служебные
    PING = 30
    PONG = 31
    BYE = 40
    ERROR = 41


# --------------------------------------------------------------------------
# примитивы сокета
# --------------------------------------------------------------------------

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


def _send_all(sock: socket.socket, data: bytes) -> None:
    sock.sendall(data)


def _pack_str(text: str, max_len: int = 4096) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) > max_len:
        raise ProtocolError("слишком длинная строка")
    return struct.pack(">H", len(raw)) + raw


def _unpack_str(payload: bytes, offset: int = 0) -> tuple[str, int]:
    if len(payload) < offset + 2:
        raise ProtocolError("обрезанная строка")
    (length,) = struct.unpack_from(">H", payload, offset)
    offset += 2
    if len(payload) < offset + length:
        raise ProtocolError("обрезанная строка")
    return payload[offset:offset + length].decode("utf-8", "replace"), offset + length


class Conn:
    """Соединение: отправка/приём сообщений, при необходимости — шифрованных.

    Отправка защищена блокировкой: хост пишет в один сокет из двух потоков
    (кадры и ответы на ping), иначе части кадров могут перемешаться.
    """

    def __init__(self, sock: socket.socket, session: crypto.Session | None = None,
                 role: str = "client") -> None:
        self.sock = sock
        self.session = session
        self.role = role
        self._send_lock = threading.Lock()
        if session is None:
            self._send_cipher = None
            self._recv_cipher = None
        else:
            if role == "client":
                self._send_cipher = session.client_to_server
                self._recv_cipher = session.server_to_client
            else:
                self._send_cipher = session.server_to_client
                self._recv_cipher = session.client_to_server

    # --- низкий уровень ---

    def _send_raw(self, body: bytes) -> None:
        _send_all(self.sock, struct.pack(">I", len(body)) + body)

    def _recv_raw(self) -> bytes:
        (length,) = struct.unpack(">I", recv_exact(self.sock, 4))
        if length > MAX_MESSAGE:
            raise ProtocolError(f"сообщение размером {length} байт превышает лимит")
        if length == 0:
            raise ProtocolError("пустой кадр")
        return recv_exact(self.sock, length)

    # --- сообщения ---

    def send(self, mtype: Msg, payload: bytes = b"") -> None:
        body = bytes((int(mtype),)) + payload
        if self._send_cipher is not None:
            body = self._send_cipher.seal(body)
        with self._send_lock:
            self._send_raw(body)

    def send_json(self, mtype: Msg, obj: Any) -> None:
        self.send(mtype, json.dumps(obj, separators=(",", ":")).encode("utf-8"))

    def recv(self) -> tuple[Msg, bytes]:
        body = self._recv_raw()
        if self._recv_cipher is not None:
            try:
                body = self._recv_cipher.open(body)
            except crypto.CryptoError as exc:
                raise ProtocolError(str(exc)) from exc
            if not body:
                raise ProtocolError("пустое сообщение после расшифровки")
        mtype = body[0]
        try:
            msg = Msg(mtype)
        except ValueError as exc:
            raise ProtocolError(f"неизвестный тип сообщения {mtype}") from exc
        return msg, body[1:]

    def recv_json(self) -> tuple[Msg, Any]:
        mtype, payload = self.recv()
        try:
            return mtype, json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"некорректный JSON в сообщении {mtype.name}") from exc

    def settimeout(self, value: Optional[float]) -> None:
        self.sock.settimeout(value)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# рукопожатие
# --------------------------------------------------------------------------

_CLIENT_LABEL = b"client-proof"
_SERVER_LABEL = b"server-proof"


def client_handshake(sock: socket.socket, password: str, client_name: str,
                     iterations: int = crypto.PBKDF2_ITERATIONS) -> Conn:
    sock.settimeout(HANDSHAKE_TIMEOUT)
    conn = Conn(sock, role="client")
    name = client_name[:64] or "viewer"
    conn.send(Msg.HELLO, struct.pack(">H", PROTOCOL_VERSION) + _pack_str(name, 128))

    mtype, payload = conn.recv()
    if mtype is Msg.AUTH_FAIL:
        reason, _ = _unpack_str(payload)
        raise AuthError(reason or "хост отклонил подключение")
    if mtype is not Msg.CHALLENGE:
        raise ProtocolError(f"ожидался CHALLENGE, получено {mtype.name}")

    if len(payload) < 2 + crypto.SALT_LEN + crypto.NONCE_RANDOM_LEN:
        raise ProtocolError("короткий CHALLENGE")
    (version,) = struct.unpack_from(">H", payload, 0)
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"версия протокола {version} не поддерживается")
    salt = payload[2:2 + crypto.SALT_LEN]
    server_nonce = payload[2 + crypto.SALT_LEN:2 + crypto.SALT_LEN + crypto.NONCE_RANDOM_LEN]
    host_name, _ = _unpack_str(payload, 2 + crypto.SALT_LEN + crypto.NONCE_RANDOM_LEN)

    session = crypto.Session(password, salt, iterations=iterations)
    client_nonce = crypto.new_nonce()
    proof = session.sign(_CLIENT_LABEL, crypto.transcript(name, salt, server_nonce, client_nonce))
    conn.send(Msg.AUTH, client_nonce + proof)

    mtype, payload = conn.recv()
    if mtype is Msg.AUTH_FAIL:
        reason, _ = _unpack_str(payload)
        raise AuthError(reason or "неверный пароль")
    if mtype is not Msg.AUTH_OK:
        raise ProtocolError(f"ожидался AUTH_OK, получено {mtype.name}")
    if len(payload) < 32:
        raise ProtocolError("короткий AUTH_OK")
    expected = session.sign(
        _SERVER_LABEL, crypto.transcript(name, salt, server_nonce, client_nonce)
    )
    if not hmac_compare(expected, payload[:32]):
        raise AuthError("хост не подтвердил знание пароля")

    conn = Conn(sock, session=session, role="client")
    conn.host_name = host_name  # type: ignore[attr-defined]
    conn.settimeout(None)
    return conn


def server_handshake(sock: socket.socket, password: str, host_name: str,
                     iterations: int = crypto.PBKDF2_ITERATIONS) -> tuple[Conn, str]:
    """Возвращает соединение и имя клиента. Бросает AuthError при неверном пароле."""
    sock.settimeout(HANDSHAKE_TIMEOUT)
    conn = Conn(sock, role="server")

    mtype, payload = conn.recv()
    if mtype is not Msg.HELLO:
        raise ProtocolError(f"ожидался HELLO, получено {mtype.name}")
    if len(payload) < 2:
        raise ProtocolError("короткий HELLO")
    (version,) = struct.unpack_from(">H", payload, 0)
    if version != PROTOCOL_VERSION:
        conn.send(Msg.AUTH_FAIL, _pack_str(f"версия протокола {version} не поддерживается"))
        raise AuthError("версия протокола не совпадает")
    client_name, _ = _unpack_str(payload, 2)
    client_name = client_name[:64] or "viewer"

    salt = crypto.new_salt()
    server_nonce = crypto.new_nonce()
    conn.send(
        Msg.CHALLENGE,
        struct.pack(">H", PROTOCOL_VERSION)
        + salt + server_nonce + _pack_str(host_name, 128),
    )

    mtype, payload = conn.recv()
    if mtype is not Msg.AUTH:
        raise ProtocolError("ожидался AUTH")
    if len(payload) < crypto.NONCE_RANDOM_LEN + 32:
        raise ProtocolError("короткий AUTH")
    client_nonce = payload[:crypto.NONCE_RANDOM_LEN]
    client_proof = payload[crypto.NONCE_RANDOM_LEN:crypto.NONCE_RANDOM_LEN + 32]

    session = crypto.Session(password, salt, iterations=iterations)
    transcript = crypto.transcript(client_name, salt, server_nonce, client_nonce)
    if not session.verify(_CLIENT_LABEL, transcript, client_proof):
        conn.send(Msg.AUTH_FAIL, _pack_str("неверный пароль"))
        raise AuthError("неверный пароль")
    conn.send(Msg.AUTH_OK, session.sign(_SERVER_LABEL, transcript))

    conn = Conn(sock, session=session, role="server")
    conn.settimeout(None)
    return conn, client_name


def hmac_compare(a: bytes, b: bytes) -> bool:
    import hmac

    return hmac.compare_digest(a, b)


# --------------------------------------------------------------------------
# кодирование полезной нагрузки
# --------------------------------------------------------------------------

# мышь: вид (0=move,1=down,2=up,3=wheel), кнопка, колесо, x, y (0..65535)
MOUSE_MOVE, MOUSE_DOWN, MOUSE_UP, MOUSE_WHEEL = 0, 1, 2, 3


def pack_mouse(kind: int, button: int, wheel: int, x: int, y: int) -> bytes:
    return struct.pack(">BBhHH", kind, button & 0xFF, wheel, x & 0xFFFF, y & 0xFFFF)


def unpack_mouse(payload: bytes) -> tuple[int, int, int, int, int]:
    if len(payload) != 8:
        raise ProtocolError("некорректное сообщение мыши")
    return struct.unpack(">BBhHH", payload)


def pack_key(vk: int, scan: int, flags: int, down: bool) -> bytes:
    return struct.pack(">HHHB", vk & 0xFFFF, scan & 0xFFFF, flags & 0xFFFF, 1 if down else 0)


def utf16_units(text: str) -> list[int]:
    """Коды UTF-16 без codecs: одиночные суррогаты тоже допустимы (они нужны SendInput)."""
    units: list[int] = []
    for ch in text:
        code = ord(ch)
        if code <= 0xFFFF:
            units.append(code)
        else:
            code -= 0x10000
            units.append(0xD800 + (code >> 10))
            units.append(0xDC00 + (code & 0x3FF))
    return units


def unpack_key(payload: bytes) -> tuple[int, int, int, bool]:
    if len(payload) != 7:
        raise ProtocolError("некорректное сообщение клавиатуры")
    vk, scan, flags, down = struct.unpack(">HHHB", payload)
    return vk, scan, flags, bool(down)


def pack_frame_header(scale_pct: int, width: int, height: int, frame_id: int,
                      flags: int, tile_count: int) -> bytes:
    return struct.pack(">HHHIBH", scale_pct, width, height, frame_id, flags, tile_count)


FRAME_HEADER = struct.Struct(">HHHIBH")
FRAME_HEADER_SIZE = FRAME_HEADER.size
FLAG_FULL = 0x01


class Frame:
    """Разобранный кадр: геометрия + список тайлов (индекс, сжатые данные)."""

    __slots__ = ("scale_pct", "width", "height", "frame_id", "flags", "tiles", "raw_size")

    def __init__(self, scale_pct: int, width: int, height: int, frame_id: int,
                 flags: int, tiles: list[tuple[int, bytes]], raw_size: int) -> None:
        self.scale_pct = scale_pct
        self.width = width
        self.height = height
        self.frame_id = frame_id
        self.flags = flags
        self.tiles = tiles
        self.raw_size = raw_size

    @property
    def is_full(self) -> bool:
        return bool(self.flags & FLAG_FULL)


def pack_frame(header: bytes, tiles: Iterable[tuple[int, bytes]]) -> bytes:
    parts: list[bytes] = []
    total = 0
    for idx, payload in tiles:
        chunk = struct.pack(">HI", idx, len(payload)) + payload
        parts.append(chunk)
        total += len(payload)
    body = b"".join(parts)
    return header + body, total


def unpack_frame(payload: bytes) -> Frame:
    if len(payload) < FRAME_HEADER_SIZE:
        raise ProtocolError("обрезанный заголовок кадра")
    scale_pct, width, height, frame_id, flags, count = FRAME_HEADER.unpack_from(payload, 0)
    offset = FRAME_HEADER_SIZE
    tiles: list[tuple[int, bytes]] = []
    total = 0
    for _ in range(count):
        if len(payload) < offset + 6:
            raise ProtocolError("обрезанный список тайлов")
        idx, length = struct.unpack_from(">HI", payload, offset)
        offset += 6
        if len(payload) < offset + length:
            raise ProtocolError("обрезанные данные тайла")
        tiles.append((idx, payload[offset:offset + length]))
        total += length
        offset += length
    if width <= 0 or height <= 0:
        raise ProtocolError("некорректная геометрия кадра")
    return Frame(scale_pct, width, height, frame_id, flags, tiles, total)
