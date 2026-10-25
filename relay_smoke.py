"""Быстрый интеграционный тест relay-протокола без GUI и захвата экрана."""

from __future__ import annotations

import os
import queue
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from rdesk import relay_proto  # noqa: E402
from rdesk.relay_server import RelayServer  # noqa: E402

PASSWORD = "test-relay-password-42"
FAILS: list[str] = []


def check(name, fn):
    started = time.perf_counter()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        FAILS.append(name)
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
    else:
        print(f"[ ok ] {name} ({1000 * (time.perf_counter() - started):.0f} ms)")


def expect(message: dict, mtype: str) -> dict:
    if message.get("type") != mtype:
        raise AssertionError(f"ожидалось {mtype}, получено {message.get('type')!r}: {message}")
    return message


def main() -> int:
    events: queue.Queue = queue.Queue()
    server = RelayServer("127.0.0.1", 0, PASSWORD, "TEST-SERVER",
                         on_event=lambda kind, payload: events.put((kind, payload)))
    server.start()

    def print_events() -> None:
        while True:
            kind, payload = events.get()
            print(f"  [{kind}] {payload}")

    threading.Thread(target=print_events, daemon=True).start()
    print(f"relay на 127.0.0.1:{server.port}")

    host_conn = None
    viewer_conn = None
    host_data = None
    viewer_data = None
    try:
        # хосту нужен host_id
        host_conn = relay_proto.client_login(
            socket.create_connection(("127.0.0.1", server.port)),
            "host", "TEST-PC", PASSWORD, host_id="host-1",
        )
        expect(host_conn.recv(), "welcome")
        host_conn.send("metadata", name="TEST-PC", user="tester", os="Windows 11",
                       version="2.0.0", read_only=False)

        viewer_conn = relay_proto.client_login(
            socket.create_connection(("127.0.0.1", server.port)),
            "viewer", "TEST-VIEWER", PASSWORD,
        )
        expect(viewer_conn.recv(), "welcome")
        listed = expect(viewer_conn.recv(), "host_list")["hosts"]
        assert len(listed) == 1, listed
        assert listed[0]["name"] == "TEST-PC", listed
        assert listed[0]["busy"] is False, listed

        viewer_conn.send("connect", host_id="host-1")
        open_message = expect(host_conn.recv(), "session_open")
        ready = expect(viewer_conn.recv(), "session_ready")
        print(f"    сеанс {ready['session_id']}: {ready['host']['name']}")

        host_token = bytes.fromhex(open_message["host_token"])
        viewer_token = bytes.fromhex(ready["viewer_token"])
        host_data = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        relay_proto.send_data_hello(host_data, host_token)
        viewer_data = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        relay_proto.send_data_hello(viewer_data, viewer_token)
        host_data.settimeout(5)
        viewer_data.settimeout(5)

        # Первым обычно открывается канал хоста, но сервер не должен зависеть
        # от порядка. Проверяем двустороннюю передачу байтов.
        host_data.sendall(b"PING-FROM-HOST")
        assert viewer_data.recv(64) == b"PING-FROM-HOST"
        viewer_data.sendall(b"PING-FROM-VIEWER")
        assert host_data.recv(64) == b"PING-FROM-VIEWER"
        print("    данные ходят в обе стороны")

        # busy виден в списке
        viewer_conn.send("list")
        # порядок: сначала может прийти host_list, потом что-то ещё
        deadline = time.time() + 4
        while time.time() < deadline:
            message = viewer_conn.recv()
            if message.get("type") == "host_list":
                assert message["hosts"][0]["busy"] is True, message
                break
        else:
            raise AssertionError("не получили host_list с busy=True")

        host_data.close()
        viewer_data.close()
        host_data = viewer_data = None
        deadline = time.time() + 4
        while time.time() < deadline:
            message = viewer_conn.recv()
            if message.get("type") == "session_closed":
                print(f"    сеанс закрыт: {message.get('reason')}")
                break
        else:
            raise AssertionError("не получили session_closed")
    finally:
        for sock in (host_data, viewer_data):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        for conn in (host_conn, viewer_conn):
            if conn is not None:
                conn.close()
        server.shutdown()

    check("неверный пароль", wrong_password)
    print(f"\nПровалено: {len(FAILS)}")
    return 1 if FAILS else 0


def wrong_password() -> None:
    events: queue.Queue = queue.Queue()
    server = RelayServer("127.0.0.1", 0, PASSWORD, "TEST-SERVER",
                         on_event=lambda kind, payload: events.put((kind, payload)))
    server.start()
    try:
        try:
            relay_proto.client_login(
                socket.create_connection(("127.0.0.1", server.port)),
                "viewer", "BAD", "wrong-password",
            )
        except relay_proto.AuthError:
            return
        raise AssertionError("неверный пароль был принят")
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
