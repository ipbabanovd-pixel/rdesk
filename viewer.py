"""Запуск клиента (вьювера) с графическим окном.

    python viewer.py
    python viewer.py --host 192.168.1.10 --port 47890

Вся логика — в rdesk/viewer.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _stream in (sys.stdout, sys.stderr):
    try:
        # В настоящей консоли Windows Python сам пишет Unicode (WriteConsoleW),
        # его кодировку не трогаем. Для файлов и каналов — принудительный UTF-8.
        if not _stream.isatty():
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from rdesk.viewer import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
