"""Запуск хоста rdesk.

    python host.py                     — окно настроек
    python host.py --tray              — свёрнуто в трей (для автозапуска)
    python host.py --server 1.2.3.4 --password "..." --tray
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _stream in (sys.stdout, sys.stderr):
    try:
        if not _stream.isatty():
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from rdesk.host_ui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
