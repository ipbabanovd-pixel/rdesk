"""Проверка ввода через relay: мышь и клавиатура доходят до хоста."""

from __future__ import annotations

import os
import queue
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from rdesk import capture, host as host_core, inject, protocol  # noqa: E402
from rdesk.host_agent import HostAgent, save_config  # noqa: E402
from rdesk.relay_server import RelayServer  # noqa: E402
from rdesk.viewer import RelayViewer, ViewerClient  # noqa: E402

PASSWORD = "input-relay-password-2026"


def wait_event(events: queue.Queue, wanted: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            kind, payload = events.get(timeout=0.2)
        except queue.Empty:
            continue
        if kind == wanted:
            return payload
    raise TimeoutError(wanted)


def main() -> int:
    capture.enable_dpi_awareness()
    app_dir = os.path.join(ROOT, "tests", "artifacts", "_input_app")
    os.makedirs(app_dir, exist_ok=True)
    host_core.setup_logging(os.path.join(app_dir, "logs"))
    mouse: list[tuple] = []
    keys: list[tuple] = []
    original = {name: getattr(inject, name) for name in
                ("mouse_move", "mouse_button", "mouse_wheel", "key_event", "unicode_char")}
    inject.mouse_move = lambda x, y: mouse.append(("move", x, y))
    inject.mouse_button = lambda b, d, x, y: mouse.append(("button", b, d, x, y))
    inject.mouse_wheel = lambda d, x=None, y=None: mouse.append(("wheel", d))
    inject.key_event = lambda vk, down, extended=False: keys.append(("vk", vk, down))
    inject.unicode_char = lambda ch, down=True: keys.append(("uni", ch, down))

    relay = RelayServer("127.0.0.1", 0, PASSWORD, "INPUT-SERVER",
                        on_event=lambda kind, payload: None)
    relay.start()
    config = {
        "server_host": "127.0.0.1", "server_port": relay.port, "password": PASSWORD,
        "host_name": "INPUT-PC", "host_id": "input-host", "read_only": False,
    }
    save_config(app_dir, config)
    agent = HostAgent(app_dir, config)
    agent.start()
    events: queue.Queue = queue.Queue()
    relay_events: queue.Queue = queue.Queue()
    viewer_relay = RelayViewer("127.0.0.1", relay.port, PASSWORD, relay_events)
    viewer: ViewerClient | None = None
    try:
        viewer_relay.connect()
        hosts = wait_event(relay_events, "hosts", 10)
        viewer_relay.request_session(hosts[0]["id"])
        ready = wait_event(relay_events, "session_ready", 10)
        sock = viewer_relay.connect_data(ready["viewer_token"], 10)
        viewer = ViewerClient(PASSWORD, {"scale": 0.5, "fps": 8, "level": 1,
                                         "view_only": False}, events)
        viewer.connect_socket(sock, 15)
        deadline = time.time() + 15
        while time.time() < deadline and viewer.latest_frame is None:
            time.sleep(0.05)
        assert viewer.latest_frame is not None, "нет кадра"

        viewer.send_mouse(protocol.MOUSE_MOVE, 0, 0, 32768, 16384)
        viewer.send_mouse(protocol.MOUSE_DOWN, 0, 0, 32768, 16384)
        viewer.send_mouse(protocol.MOUSE_UP, 0, 0, 32768, 16384)
        viewer.send_unicode("A")
        deadline = time.time() + 5
        while time.time() < deadline and not (len(mouse) >= 3 and len(keys) >= 2):
            time.sleep(0.05)
        print("мышь:", mouse[:5])
        print("клавиши:", keys[:5])
        assert len(mouse) >= 3, "события мыши не дошли"
        assert any(k[0] == "uni" and k[1] == "A" for k in keys), "текст не дошёл"
        print("Ввод через relay работает.")
        return 0
    finally:
        if viewer is not None:
            viewer.close()
        viewer_relay.close()
        agent.stop()
        relay.shutdown()
        for name, fn in original.items():
            setattr(inject, name, fn)


if __name__ == "__main__":
    sys.exit(main())
