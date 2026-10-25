"""Снимки интерфейсов rdesk для проверки внешнего вида.

Запуск:  python tools/ui_snapshot.py [server|viewer|login|host|all]
"""

from __future__ import annotations

import os
import queue
import shutil
import socket
import sys
import time
import ctypes
import tkinter as tk
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from rdesk import capture, host as host_core, relay_proto  # noqa: E402
from rdesk.host_agent import save_config  # noqa: E402
from rdesk.relay_server import RelayServer  # noqa: E402
from tools.pngwrite import write_png  # noqa: E402
from tools.winshot import screenshot_window  # noqa: E402

ART = os.path.join(ROOT, "tests", "artifacts")
PASSWORD = "snapshot-password-2026"


def fresh(name: str) -> str:
    path = os.path.join(ART, "_ui_" + name)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def close_security_dialogs() -> None:
    """Закрывает системное окно брандмауэра, чтобы оно не попало на снимок."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        titles = {"Безопасность Windows", "Windows Security"}
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value in titles:
                    user32.PostMessageW(hwnd, 0x0010, 0, 0)
            return True

        user32.EnumWindows(callback_type(callback), 0)
    except Exception:  # noqa: BLE001
        pass


def shot(widget: tk.Misc, path: str) -> None:
    close_security_dialogs()
    widget.update_idletasks()
    widget.update()
    time.sleep(0.2)
    if screenshot_window(widget, path):
        print(f"снимок: {path} (PrintWindow)")
        return
    close_security_dialogs()
    time.sleep(0.15)
    layout = capture.get_layout()
    grabber = capture.ScreenGrabber()
    try:
        sw, sh, rgb = grabber.grab(layout, 1.0)
    finally:
        grabber.close()
    x = widget.winfo_rootx() - layout.x
    y = widget.winfo_rooty() - layout.y
    w = min(widget.winfo_width(), sw - x)
    h = min(widget.winfo_height(), sh - y)
    if w <= 0 or h <= 0:
        raise RuntimeError(f"некорректный прямоугольник окна: {x},{y},{w},{h}")
    rows = []
    for row in range(h):
        start = ((y + row) * sw + x) * 3
        rows.append(rgb[start:start + w * 3])
    write_png(path, w, h, b"".join(rows))
    print(f"снимок: {path} ({w}x{h})")


def fake_hosts(port: int, count: int = 3, password: str = PASSWORD) -> list:
    conns = []
    names = [("WORK-PC", "Алексей", "Windows 11 Pro"),
             ("OFFICE-2", "Мария", "Windows 10 Pro"),
             ("BUH-PC", "Ольга", "Windows 11 Pro")]
    for index in range(count):
        try:
            conn = relay_proto.client_login(
                socket.create_connection(("127.0.0.1", port), timeout=5),
                "host", names[index][0], password, host_id=f"snap-{index}",
            )
            conn.recv()  # welcome
            conn.send("metadata", name=names[index][0], user=names[index][1],
                      os=names[index][2], version="2.0.0", read_only=False)
            conns.append(conn)
        except Exception as exc:  # noqa: BLE001
            print(f"фейковый хост не подключился: {exc}")
    return conns


def make_server_ui() -> None:
    from rdesk import server_config
    from rdesk.server_ui import ServerApp

    app_dir = fresh("server")
    config = server_config.default_config()
    config.update({"port": 7450, "password": PASSWORD, "bind": "127.0.0.1"})
    server_config.save_config(app_dir, config)
    root = tk.Tk()
    root.withdraw()
    app = ServerApp(root, app_dir)
    root.geometry("1120x760+30+30")
    root.deiconify()
    root.attributes("-topmost", True)
    hosts = []

    def connect_fakes() -> None:
        nonlocal hosts
        if app.server is not None:
            hosts = fake_hosts(app.server.port, 3, password=app.pass_var.get())
        root.after(1200, lambda: finish())

    def finish() -> None:
        root.update()
        shot(root, os.path.join(ART, "server-ui.png"))
        for conn in hosts:
            try:
                conn.close()
            except OSError:
                pass
        app._quit()

    root.after(1400, connect_fakes)
    root.mainloop()


def make_viewer_ui() -> None:
    from rdesk.viewer import ViewerApp

    relay = RelayServer("127.0.0.1", 0, PASSWORD, "MAIN-SERVER",
                        on_event=lambda kind, payload: None)
    relay.start()
    hosts = fake_hosts(relay.port, 3)
    app_dir = fresh("viewer")
    root = tk.Tk()
    root.withdraw()
    app = ViewerApp(root, app_dir, autoconnect={
        "server_host": "127.0.0.1", "server_port": relay.port, "password": PASSWORD,
    })
    root.geometry("1260x780+20+20")
    root.attributes("-topmost", True)
    root.deiconify()

    def finish() -> None:
        root.update()
        shot(root, os.path.join(ART, "viewer-ui.png"))
        app._quit()
        for conn in hosts:
            try:
                conn.close()
            except OSError:
                pass
        relay.shutdown()

    root.after(4200, finish)
    root.mainloop()


def make_login_ui() -> None:
    from rdesk.viewer import ViewerApp

    app_dir = fresh("login")
    root = tk.Tk()
    root.withdraw()
    app = ViewerApp(root, app_dir)

    def finish() -> None:
        dialog = app.login_dialog
        if dialog is None:
            print("диалог входа не появился")
            app._quit()
            return
        dialog.host_var.set("rdesk.example.ru")
        dialog.port_var.set("7443")
        dialog.pass_var.set("Xk3pQ7mZ1aB4cD6eF8gH")
        dialog.show_var.set(True)
        dialog._toggle_password()
        dialog.window.update_idletasks()
        shot(dialog.window, os.path.join(ART, "viewer-login.png"))
        dialog._closed = True
        dialog.window.destroy()
        app._quit()

    root.after(1400, finish)
    root.mainloop()


def make_host_ui() -> None:
    from rdesk.host_ui import HostApp

    relay = RelayServer("127.0.0.1", 0, PASSWORD, "MAIN-SERVER",
                        on_event=lambda kind, payload: None)
    relay.start()
    app_dir = fresh("host")
    config = {
        "server_host": "127.0.0.1", "server_port": relay.port,
        "password": PASSWORD, "host_name": "WORK-PC",
        "host_id": "snap-host", "read_only": False, "confirm": False,
    }
    save_config(app_dir, config)
    host_core.setup_logging(os.path.join(app_dir, "logs"))
    root = tk.Tk()
    root.withdraw()
    app = HostApp(root, app_dir, start_hidden=False)
    root.geometry("900x680+40+40")
    root.attributes("-topmost", True)
    root.deiconify()

    def finish() -> None:
        root.update()
        shot(root, os.path.join(ART, "host-ui.png"))
        app._quit()
        relay.shutdown()

    root.after(3400, finish)
    root.mainloop()


def make_viewer_session_ui() -> None:
    from rdesk.host_agent import HostAgent
    from rdesk.viewer import ViewerApp

    relay = RelayServer("127.0.0.1", 0, PASSWORD, "MAIN-SERVER",
                        on_event=lambda kind, payload: None)
    relay.start()
    host_dir = fresh("session_host")
    config = {
        "server_host": "127.0.0.1", "server_port": relay.port,
        "password": PASSWORD, "host_name": "WORK-PC", "host_id": "snap-session",
        "read_only": True, "confirm": False,
    }
    save_config(host_dir, config)
    host_core.setup_logging(os.path.join(host_dir, "logs"))
    agent = HostAgent(host_dir, config)
    agent.start()

    app_dir = fresh("session_viewer")
    root = tk.Tk()
    root.withdraw()
    app = ViewerApp(root, app_dir, autoconnect={
        "server_host": "127.0.0.1", "server_port": relay.port, "password": PASSWORD,
    })
    root.geometry("1260x780+20+20")
    root.attributes("-topmost", True)
    root.deiconify()

    def connect() -> None:
        if app.host_tree.get_children():
            app.host_tree.selection_set(app.host_tree.get_children()[0])
            app._connect_selected()
        else:
            print("хост не появился в списке")
            finish()

    def finish() -> None:
        root.update()
        print("статус:", app.status_var.get(), "кадр:", app._frame_size,
              "показ:", app._display_size, "factor:", app._scale_factor,
              "canvas:", app.canvas.winfo_width(), app.canvas.winfo_height())
        shot(root, os.path.join(ART, "viewer-session.png"))
        app._quit()
        agent.stop()
        relay.shutdown()

    root.after(2200, connect)
    root.after(6500, finish)
    root.mainloop()


def main() -> int:
    capture.enable_dpi_awareness()
    os.makedirs(ART, exist_ok=True)
    role = sys.argv[1] if len(sys.argv) > 1 else "all"
    if role in ("server", "all"):
        make_server_ui()
    if role in ("viewer", "all"):
        make_viewer_ui()
    if role in ("viewer-session", "all"):
        make_viewer_session_ui()
    if role in ("login", "all"):
        make_login_ui()
    if role in ("host", "all"):
        make_host_ui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
