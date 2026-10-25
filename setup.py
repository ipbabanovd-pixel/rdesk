"""Установка, удаление и остановка rdesk (host, viewer, server).

Скрипт работает внутри комплекта: его запускает установщик сервера и MSI
(для остановки процессов перед обновлением/удалением). Внешних зависимостей
нет — только стандартная библиотека и встроенный Python.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
import winreg

ROLES = {
    "host": {
        "title": "rdesk host",
        "exe": "rdesk-host.exe",
        "script": "host.py",
        "autostart": True,
        "desktop": False,
        "pid": "host.pid",
    },
    "viewer": {
        "title": "rdesk viewer",
        "exe": "rdesk-viewer.exe",
        "script": "viewer.py",
        "autostart": False,
        "desktop": True,
        "pid": "viewer.pid",
    },
    "server": {
        "title": "rdesk server",
        "exe": "rdesk-server.exe",
        "script": "server.py",
        "autostart": True,
        "desktop": True,
        "pid": "server.pid",
    },
}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
VERSION = "2.0.0"
TEST_MODE = os.environ.get("RDESK_TEST") == "1"
QUIET = False


def say(message: str) -> None:
    print(message, flush=True)


def message_box(title: str, text: str, flags: int = 0x40) -> None:
    if TEST_MODE or QUIET:
        say(f"  [сообщение] {title}: {text.replace(chr(10), ' | ')}")
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:  # noqa: BLE001
        say(f"{title}: {text}")


def app_dir(dest: str) -> str:
    return os.path.join(dest, "app")


def launcher(dest: str, role: str) -> str:
    return os.path.join(dest, ROLES[role]["exe"])


def shortcut_paths(role: str) -> list[str]:
    programs = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                            "Start Menu", "Programs", "rdesk")
    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    paths = [os.path.join(programs, f"{ROLES[role]['title']}.lnk")]
    if ROLES[role]["desktop"]:
        paths.append(os.path.join(desktop, f"{ROLES[role]['title']}.lnk"))
    return paths


def create_shortcut(lnk: str, target: str, arguments: str, workdir: str, icon: str) -> bool:
    if TEST_MODE:
        say(f"  [тест] ярлык: {lnk} -> {target} {arguments}")
        return True
    script = (
        "$ErrorActionPreference='Stop';"
        "$w = New-Object -ComObject WScript.Shell;"
        f"$s = $w.CreateShortcut('{lnk}');"
        f"$s.TargetPath = '{target}';"
        f"$s.Arguments = '{arguments}';"
        f"$s.WorkingDirectory = '{workdir}';"
        f"$s.IconLocation = '{icon}';"
        "$s.WindowStyle = 1;"
        "$s.Save()"
    )
    try:
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            say(f"  не удалось создать ярлык: {result.stderr.strip()[:200]}")
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"  не удалось создать ярлык: {exc}")
        return False


def set_run_key(name: str, command: str) -> bool:
    if TEST_MODE:
        say(f"  [тест] автозапуск: {name} = {command}")
        return True
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
        return True
    except OSError as exc:
        say(f"  не удалось прописать автозапуск: {exc}")
        return False


def delete_run_key(name: str) -> None:
    if TEST_MODE:
        say(f"  [тест] удалить автозапуск: {name}")
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass


def write_uninstall_entry(role: str, dest: str) -> bool:
    name = f"rdesk-{role}"
    title = ROLES[role]["title"]
    uninstaller = os.path.join(dest, "uninstall.cmd")
    command = f'"{uninstaller}"'
    if TEST_MODE:
        say(f"  [тест] запись об удалении: {name} -> {command}")
        return True
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                f"{UNINSTALL_KEY}\\{name}", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, title)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "rdesk")
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ,
                              os.path.join(app_dir(dest), "assets", "rdesk.ico"))
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, dest)
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, command)
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, 140000)
        return True
    except OSError as exc:
        say(f"  не удалось добавить запись об удалении: {exc}")
        return False


def delete_uninstall_entry(role: str) -> None:
    if TEST_MODE:
        say(f"  [тест] удалить запись об удалении rdesk-{role}")
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{UNINSTALL_KEY}\\rdesk-{role}")
    except OSError:
        pass


def kill_running(role: str, dest: str) -> None:
    info = ROLES[role]
    pid_file = os.path.join(app_dir(dest), "logs", info["pid"])
    if not os.path.exists(pid_file):
        return
    try:
        with open(pid_file, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, timeout=20)
        say(f"  остановлен работающий {info['title']} (PID {pid})")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        os.remove(pid_file)
    except OSError:
        pass


def launch(role: str, dest: str, tray: bool = False) -> bool:
    info = ROLES[role]
    exe = launcher(dest, role)
    if not os.path.exists(exe):
        say(f"  не найден {exe}")
        return False
    args = [exe]
    if tray:
        args.append("--tray")
    if TEST_MODE:
        say(f"  [тест] запуск: {' '.join(args)}")
        return True
    creation = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        creation |= getattr(subprocess, name, 0)
    try:
        subprocess.Popen(
            args, cwd=dest, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creation, close_fds=True,
        )
        say(f"  {info['title']} запущен")
        return True
    except OSError as exc:
        say(f"  не удалось запустить {info['title']}: {exc}")
        return False


def install(role: str, dest: str) -> int:
    info = ROLES[role]
    say(f"Установка {info['title']} в {dest}")
    kill_running(role, dest)
    problems: list[str] = []

    exe = launcher(dest, role)
    icon = os.path.join(app_dir(dest), "assets", "rdesk.ico")

    say("Создание ярлыков…")
    for lnk in shortcut_paths(role):
        if not create_shortcut(lnk, exe, "", dest, icon):
            problems.append(f"ярлык {os.path.basename(lnk)}")

    if info["autostart"]:
        say("Автозапуск при входе в систему…")
        if not set_run_key(info["title"], f'"{exe}" --tray'):
            problems.append("автозапуск")

    if not write_uninstall_entry(role, dest):
        problems.append("запись в «Установка и удаление»")

    if not launch(role, dest):
        problems.append("запуск")

    if role == "server":
        text = (
            "Установка завершена.\n\n"
            "Открывается окно rdesk server. Скопируйте пароль сервера и используйте "
            "его при установке rdesk host и при входе во вьювере.\n\n"
            "Для подключения из интернета откройте входящий TCP-порт 7443 "
            "(кнопка «Открыть порт в брандмауэре» в окне сервера).\n"
            "На компьютерах с host и на компьютере вьювера входящие порты не нужны."
        )
    elif role == "host":
        text = (
            "Установка завершена.\n\n"
            "Открывается окно rdesk host. Укажите адрес сервера, порт и пароль, "
            "которые показывает rdesk server.\n\n"
            "Хост будет автоматически запускаться при входе в систему. Его значок — "
            "в области уведомлений Windows."
        )
    else:
        text = (
            "Установка завершена.\n\n"
            "Открывается rdesk viewer. Введите адрес сервера и пароль сервера, "
            "после чего появятся все подключённые компьютеры."
        )
    message_box(info["title"], text)

    if problems:
        say("Проблемы: " + ", ".join(problems))
        return 1
    say("Готово.")
    return 0


def stop(role: str, dest: str) -> int:
    kill_running(role, dest)
    return 0


def uninstall(role: str, dest: str) -> int:
    info = ROLES[role]
    say(f"Удаление {info['title']}")
    kill_running(role, dest)
    for lnk in shortcut_paths(role):
        try:
            if os.path.exists(lnk):
                os.remove(lnk)
                say(f"  удалён ярлык {os.path.basename(lnk)}")
        except OSError:
            pass
    delete_run_key(info["title"])
    delete_uninstall_entry(role)

    if TEST_MODE:
        say(f"  [тест] папка не удаляется: {dest}")
        return 0

    parent = os.path.dirname(dest.rstrip("\\/"))
    helper = (
        "@echo off\r\n"
        "ping -n 4 127.0.0.1 >nul\r\n"
        f'rmdir /s /q "{dest}"\r\n'
        f'rmdir "{parent}" 2>nul\r\n'
    )
    temp = os.path.join(os.environ.get("TEMP", "."), f"rdesk_cleanup_{role}.cmd")
    try:
        with open(temp, "w", encoding="utf-8") as fh:
            fh.write(helper)
        subprocess.Popen(["cmd", "/c", temp],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         close_fds=True)
    except OSError as exc:
        say(f"  не удалось запланировать удаление папки: {exc}")
    say("Готово.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup.py", description="Установка rdesk")
    parser.add_argument("action", choices=["install", "uninstall", "stop"])
    parser.add_argument("--role", required=True, choices=sorted(ROLES))
    parser.add_argument("--dest", required=True)
    parser.add_argument("--no-message", action="store_true")
    args = parser.parse_args(argv)
    global QUIET
    QUIET = args.no_message

    dest = os.path.abspath(args.dest)
    if not os.path.isdir(dest):
        say(f"Папка установки не найдена: {dest}")
        return 2
    start = time.perf_counter()
    if args.action == "install":
        code = install(args.role, dest)
    elif args.action == "stop":
        code = stop(args.role, dest)
    else:
        code = uninstall(args.role, dest)
    say(f"Затрачено {time.perf_counter() - start:.1f} с")
    return code


if __name__ == "__main__":
    sys.exit(main())
