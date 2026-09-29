"""What has to exist on the machine for the Generate and Clone tabs to work.

Two external dependencies: `ffmpeg` converts the WAV to `.ogg` and decodes the
samples, and the Python 3.11 in `.venv` runs `preparation.py`. Neither is
importable — one is an executable on the PATH, the other is a whole
interpreter with `torch` inside — so the only possible check is looking at the
disk.

The supervisor warns on screen instead of letting cloning die halfway: it's
the difference between "install ffmpeg" and a `FileNotFoundError` traceback
ten minutes after uploading a 40 MB file. So each missing item says what it's
for and carries the exact commands that fix it — a bare "not found" leaves the
person to figure out the rest.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from panel import ROOT

README_SETUP = "https://github.com/nickolascheidt/Discord-TTS#setup"

_WINDOWS = os.name == "nt"


def venv_python() -> Path:
    """The 3.11 interpreter that runs `preparation.py`.

    `.venv` has `torch`, `DeepFilterNet` and `librosa`; the supervisor's
    Python doesn't and won't. The bridge between the two is a subprocess, so
    what matters here is the path to the executable, not an import.
    """
    sub = Path("Scripts/python.exe") if _WINDOWS else Path("bin/python")
    return ROOT / ".venv" / sub


def _ffmpeg_fix() -> list[str]:
    install = ("winget install Gyan.FFmpeg" if _WINDOWS
               else "sudo apt install ffmpeg   # or: brew install ffmpeg")
    # The panel reads the PATH once, when it starts: a freshly installed
    # ffmpeg only shows up after a restart.
    return [install, "# then quit the panel from the tray icon and open it again"]


def _venv_fix() -> list[str]:
    python = r".venv\Scripts\python" if _WINDOWS else ".venv/bin/python"
    create = "py -3.11 -m venv .venv" if _WINDOWS else "python3.11 -m venv .venv"
    return [
        f"cd {ROOT}",
        create,
        f"{python} -m pip install -r requirements-prep.txt "
        "--extra-index-url https://download.pytorch.org/whl/cpu",
    ]


def check() -> list[dict]:
    """One entry per dependency, ready for the screen to draw.

    `why` says which feature needs it, `fix` has the commands that solve it,
    and `detail` is where it was found (or where it was looked for).
    """
    ffmpeg = shutil.which("ffmpeg")
    python = venv_python()
    return [
        {
            "what": "ffmpeg",
            "ok": ffmpeg is not None,
            "why": "needed to prepare voice samples (Clone tab) and to download .ogg files",
            "detail": ffmpeg or "not found on the PATH",
            "fix": _ffmpeg_fix(),
            "docs": README_SETUP,
        },
        {
            "what": ".venv python",
            "ok": python.is_file(),
            "why": "cloning voices runs preparation.py in a separate Python 3.11 "
                   "environment, with its own packages (DeepFilterNet, torch)",
            "detail": str(python) if python.is_file() else f"not found at {python}",
            "fix": _venv_fix(),
            "docs": README_SETUP,
        },
    ]


def missing() -> list[str]:
    """Just the names of what is missing. Empty means everything is in place."""
    return [c["what"] for c in check() if not c["ok"]]
