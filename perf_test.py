"""Замеры задержек: вход, открытие сеанса, рукопожатие, первый кадр."""

from __future__ import annotations

import os
import queue
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from rdesk import capture, host as host_core  # noqa: E402
from rdesk.host_agent import HostAgent, save_config  # noqa: E402
from rdesk.relay_server import RelayServer  # noqa: E402
from rdesk.viewer import RelayViewer, ViewerClient  # noqa: E402

PASSWORD = "perf-test-password-2026"


def wait_event(events, wanted, timeout=15.0):
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
    app_dir = os.path.join(ROOT, "tests", "artifacts", "_perf_app")
    os.makedirs(app_dir, exist_ok=True)
    host_core.setup_logging(os.path.join(app_dir, "logs"))
    server = RelayServer("127.0.0.1", 0, PASSWORD, "PERF", on_event=lambda k, p: None)
    server.start()
    config = {
        "server_host": "127.0.0.1", "server_port": server.port, "password": PASSWORD,
        "host_name": "PERF-PC", "host_id": "perf-host", "read_only": False,
    }
    save_config(app_dir, config)
    agent = HostAgent(app_dir, config)
    agent.start()
    while agent.state != "online":
        time.sleep(0.05)

    relay_events: queue.Queue = queue.Queue()
    events: queue.Queue = queue.Queue()
    relay = RelayViewer("127.0.0.1", server.port, PASSWORD, relay_events)
    viewer = None
    try:
        t0 = time.perf_counter()
        relay.connect()
        viewer_login = (time.perf_counter() - t0) * 1000
        hosts = wait_event(relay_events, "hosts")
        t0 = time.perf_counter()
        relay.request_session(hosts[0]["id"])
        ready = wait_event(relay_events, "session_ready")
        ready_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        sock = relay.connect_data(ready["viewer_token"])
        data_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        viewer = ViewerClient(PASSWORD, {"scale": 0.75, "fps": 15, "level": 1,
                                         "view_only": True}, events)
        name = viewer.connect_socket(sock)
        handshake_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        deadline = time.time() + 20
        while time.time() < deadline:
            with viewer.frame_lock:
                frame = viewer.latest_frame
            if frame is not None:
                break
            time.sleep(0.002)
        first_frame_ms = (time.perf_counter() - t0) * 1000
        if frame is None:
            raise TimeoutError("нет кадра")
        viewer.send_ack(frame.frame_id)
        print(f"viewer login to server: {viewer_login:.0f} ms")
        print(f"request -> session_ready: {ready_ms:.0f} ms")
        print(f"connect data socket: {data_ms:.0f} ms")
        print(f"data handshake: {handshake_ms:.0f} ms")
        print(f"first frame after handshake: {first_frame_ms:.0f} ms")
        print(f"first frame: {frame.width}x{frame.height}, {frame.raw_size/1024:.0f} KiB, "
              f"tiles {len(frame.tiles)}")

        # Чистые замеры хоста
        grabber = capture.ScreenGrabber()
        try:
            layout = capture.get_layout()
            for scale in (1.0, 0.75, 0.66, 0.5):
                grabber.grab(layout, scale)
                t0 = time.perf_counter()
                w, h, rgb = grabber.grab(layout, scale)
                grab_ms = (time.perf_counter() - t0) * 1000
                enc = capture.TileEncoder(level=1)
                t0 = time.perf_counter()
                tiles, full = enc.encode(w, h, rgb, force_full=True)
                enc_ms = (time.perf_counter() - t0) * 1000
                print(f"scale {scale:.2f}: grab {grab_ms:.0f} ms, encode {enc_ms:.0f} ms, "
                      f"{w}x{h}, {sum(len(t[1]) for t in tiles)/1024:.0f} KiB")
        finally:
            grabber.close()

        print(f"viewer host: {name}")
    finally:
        if viewer is not None:
            viewer.close()
        relay.close()
        agent.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
