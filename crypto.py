"""Криптография канала — только стандартная библиотека Python.

Схема
-----
* Ключи выводятся из пароля: PBKDF2-HMAC-SHA256 (200 000 итераций) по соли,
  которую присылает хост в начале рукопожатия.
* Из мастер-ключа через SHAKE-256 выводятся четыре отдельных ключа
  (шифрование и MAC для каждого направления) и ключ аутентификации.
* Шифрование — гамма SHAKE-256(key || nonce) с уникальным 16-байтным nonce
  на каждое сообщение (8 байт счётчика + 8 нулевых байт).
* Аутентификация — encrypt-then-MAC: HMAC-SHA256 по (версия || nonce || шифртекст),
  обрезанный до 16 байт. Проверка сравнением за постоянное время.

Это не «настоящий» AEAD из учебника, но конструкция стандартная
(PRF в режиме счётчика + EtM) и не позволяет ни подделать, ни прочитать,
ни переставить сообщения без знания пароля.
"""

from __future__ import annotations

import hashlib
import hmac
import struct

PROTOCOL_VERSION = 1
KEY_LEN = 32
TAG_LEN = 16
NONCE_LEN = 16
SALT_LEN = 16
NONCE_RANDOM_LEN = 16
PBKDF2_ITERATIONS = 200_000
MAX_COUNTER = (1 << 64) - 1

_VERSION_TAG = b"rdesk1"


class CryptoError(Exception):
    """Сообщение повреждено, подделано или пришло не по порядку."""


def _kdf(master: bytes, label: str, length: int = KEY_LEN) -> bytes:
    """Вывод подключей из мастер-ключа (доменное разделение по метке)."""
    return hashlib.shake_256(master + b"|" + label.encode("ascii")).digest(length)


def _xor(data: bytes, keystream: bytes) -> bytes:
    """Быстрый XOR через big-int (C-скорость вместо питоновского цикла)."""
    n = len(data)
    if n == 0:
        return b""
    a = int.from_bytes(data, "big")
    b = int.from_bytes(keystream, "big")
    return (a ^ b).to_bytes(n, "big")


def derive_master_key(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    if not isinstance(salt, (bytes, bytearray)) or len(salt) != SALT_LEN:
        raise ValueError("соль должна быть 16 байт")
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes(salt), iterations, dklen=KEY_LEN
    )


class ChannelCipher:
    """Шифр одного направления: seal() для отправки, open() для приёма."""

    def __init__(self, enc_key: bytes, mac_key: bytes) -> None:
        self._enc_key = enc_key
        self._mac_key = mac_key
        self._counter = 0

    def _nonce(self, counter: int) -> bytes:
        return struct.pack(">Q", counter) + b"\x00" * (NONCE_LEN - 8)

    def _tag(self, nonce: bytes, ciphertext: bytes) -> bytes:
        mac = hmac.new(self._mac_key, _VERSION_TAG + nonce + ciphertext, hashlib.sha256)
        return mac.digest()[:TAG_LEN]

    def seal(self, plaintext: bytes) -> bytes:
        if self._counter > MAX_COUNTER:
            raise CryptoError("счётчик nonce исчерпан, нужен новый сеанс")
        nonce = self._nonce(self._counter)
        keystream = hashlib.shake_256(self._enc_key + nonce).digest(len(plaintext))
        ciphertext = _xor(plaintext, keystream)
        self._counter += 1
        return ciphertext + self._tag(nonce, ciphertext)

    def open(self, sealed: bytes) -> bytes:
        if len(sealed) < TAG_LEN:
            raise CryptoError("слишком короткое сообщение")
        ciphertext, tag = sealed[:-TAG_LEN], sealed[-TAG_LEN:]
        if self._counter > MAX_COUNTER:
            raise CryptoError("счётчик nonce исчерпан, нужен новый сеанс")
        nonce = self._nonce(self._counter)
        if not hmac.compare_digest(tag, self._tag(nonce, ciphertext)):
            raise CryptoError("неверный MAC — сообщение подделано или повреждено")
        keystream = hashlib.shake_256(self._enc_key + nonce).digest(len(ciphertext))
        self._counter += 1
        return _xor(ciphertext, keystream)


class Session:
    """Набор ключей одного сеанса: два направления + ключ аутентификации."""

    def __init__(self, password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> None:
        master = derive_master_key(password, salt, iterations)
        self.salt = bytes(salt)
        self.auth_key = _kdf(master, "auth")
        self.client_to_server = ChannelCipher(
            _kdf(master, "c2s|enc"), _kdf(master, "c2s|mac")
        )
        self.server_to_client = ChannelCipher(
            _kdf(master, "s2c|enc"), _kdf(master, "s2c|mac")
        )
        # Сами мастер-ключи не нужны после вывода подключей.
        del master

    # --- аутентификация сторон (взаимная, на общем пароле) ---

    def sign(self, label: bytes, transcript: bytes) -> bytes:
        return hmac.new(self.auth_key, label + transcript, hashlib.sha256).digest()

    def verify(self, label: bytes, transcript: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(self.sign(label, transcript), signature)


def transcript(client_name: str, salt: bytes, server_nonce: bytes, client_nonce: bytes) -> bytes:
    """Единый для обеих сторон набор данных рукопожатия."""
    name = client_name.encode("utf-8")[:255]
    return b"|".join((b"rdesk-handshake", salt, server_nonce, client_nonce, name))


def new_nonce() -> bytes:
    import os

    return os.urandom(NONCE_RANDOM_LEN)


def new_salt() -> bytes:
    import os

    return os.urandom(SALT_LEN)
