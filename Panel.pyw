"""Panel entry point for the Windows shortcut.

This file lives at the project root on purpose. Python puts the script's
folder in `sys.path[0]`, so `pythonw.exe Panel.pyw` makes the root visible and
`import panel` works. Pointing the shortcut straight at `panel\\supervisor.py`
would put `panel\\` on the path and fail with `ModuleNotFoundError` — which is
exactly what happened in the first version.

The try/except exists for the same reason: `pythonw.exe` has no console, so a
traceback goes nowhere. A panel that doesn't open needs to say why, otherwise
the symptom is a double click that simply does nothing.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ERROR_LOG = ROOT / "logs" / "panel-error.log"


def alert(message: str) -> None:
    """Windows message box. Without a console, it's the only channel left."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "TTS Panel", 0x10)
    except Exception:  # noqa: BLE001 - the alert is secondary, it can never hide the real error
        pass


def main() -> int:
    # The engine resolves `.env` and `voices/` by relative path; without this,
    # a shortcut launched from another folder would start with no voices.
    os.chdir(ROOT)

    try:
        from panel.supervisor import main as start

        start()
    except Exception:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n")
            traceback.print_exc(file=f)
        alert(f"The panel couldn't start.\n\nDetails in:\n{ERROR_LOG}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
