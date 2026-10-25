"""Настройки центрального сервера rdesk."""

from __future__ import annotations

import json
import os
import secrets
import socket
import string

from . import relay_proto

DEFAULT_PORT = relay_proto.DEFAULT_PORT


def config_path(app_dir: str) -> str:
    return os.path.join(app_dir, "config", "server.json")


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def default_config() -> dict:
    return {
        "port": DEFAULT_PORT,
        "bind": "0.0.0.0",
        "password": generate_password(),
        "server_name": socket.gethostname(),
        "autostart": True,
    }


def load_config(app_dir: str) -> tuple[dict, bool]:
    """Возвращает настройки и признак того, что файл был создан/исправлен."""
    data = default_config()
    path = config_path(app_dir)
    changed = False
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, json.JSONDecodeError):
            changed = True
    if not data.get("password"):
        data["password"] = generate_password()
        changed = True
    if not data.get("server_name"):
        data["server_name"] = socket.gethostname()
        changed = True
    try:
        data["port"] = int(data.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        data["port"] = DEFAULT_PORT
        changed = True
    data["autostart"] = bool(data.get("autostart", True))
    if changed or not os.path.exists(path):
        save_config(app_dir, data)
    return data, changed


def save_config(app_dir: str, data: dict) -> None:
    path = config_path(app_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
