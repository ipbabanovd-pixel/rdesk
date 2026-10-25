"""Проверка синхронизации текстового буфера обмена через сеанс rdesk."""

from __future__ import annotations

import os
import queue
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from rdesk import capture, host as host_core, inject  # noqa: E402
from rdesk.host_agent import HostAgent, save_config  # noqa: E402
from rdesk.relay_server import RelayServer  # noqa: E402
from rdesk.viewer import RelayViewer, ViewerClient  # noqa: E402

PASSWORD = "clipboard-test-password"


def wait_event(events: queue.Queue, wanted: str, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            kind, payload = events.get(timeout=0.2)
        except queue.Empty:
            continue
        if kind == wanted:
            return payload
    raise TimeoutError(wanted)


def wait_clipboard(expected: str, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = inject.get_clipboard_text()
        if value == expected:
            return value
        time.sleep(0.05)
    raise AssertionError(f"буфер обмена: {inject.get_clipboard_text()!r} != {expected!r}")


def main() -> int:
    capture.enable_dpi_awareness()
    original = inject.get_clipboard_text()
    app_dir = os.path.join(ROOT, "tests", "artifacts", "_clip_app")
    os.makedirs(app_dir, exist_ok=True)
    host_core.setup_logging(os.path.join(app_dir, "logs"))
    inject.set_clipboard_text("baseline")
    server = RelayServer("127.0.0.1", 0, PASSWORD, "CLIP", on_event=lambda k, p: None)
    server.start()
    config = {
        "server_host": "127.0.0.1", "server_port": server.port, "password": PASSWORD,
        "host_name": "CLIP-PC", "host_id": "clip-host", "read_only": False,
    }
    save_config(app_dir, config)
    agent = HostAgent(app_dir, config)
    agent.start()
    while agent.state != "online":
        time.sleep(0.05)

    relay_events: queue.Queue = queue.Queue()
    session_events: queue.Queue = queue.Queue()
    relay = RelayViewer("127.0.0.1", server.port, PASSWORD, relay_events)
    viewer = None
    try:
        relay.connect()
        hosts = wait_event(relay_events, "hosts")
        relay.request_session(hosts[0]["id"])
        ready = wait_event(relay_events, "session_ready")
        sock = relay.connect_data(ready["viewer_token"])
        viewer = ViewerClient(PASSWORD, {"scale": 0.5, "fps": 8, "level": 1,
                                         "view_only": False}, session_events,
                              session_key=ready.get("session_key", ""))
        viewer.connect_socket(sock)
        time.sleep(0.8)  # хост запоминает исходный буфер и не перетирает его

        inject.set_clipboard_text("from-host")
        received = wait_event(session_events, "clipboard", 5)
        assert received == "from-host", received
        print(f"host -> viewer: {received!r}")

        viewer.send_clipboard("from-viewer")
        wait_clipboard("from-viewer")
        print("viewer -> host: 'from-viewer'")
        time.sleep(1.0)
        try:
            echoed = session_events.get_nowait()
        except queue.Empty:
            echoed = None
        assert echoed is None, f"эхо буфера: {echoed}"
        print("эха нет")
        return 0
    finally:
        if viewer is not None:
            viewer.close()
        relay.close()
        agent.stop()
        server.shutdown()
        try:
            inject.set_clipboard_text(original)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
