"""Запуск центрального сервера rdesk.

    python server.py
    python server.py --tray           — запустить свёрнутым в трей
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _stream in (sys.stdout, sys.stderr):
    try:
        if not _stream.isatty():
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from rdesk.server_ui import main  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rdesk-server")
    parser.add_argument("--tray", action="store_true", help="свернуть окно в трей")
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    sys.exit(main(start_hidden=args.tray))
