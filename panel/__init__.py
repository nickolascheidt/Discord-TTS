"""Local panel: supervises the TTS engine and serves the web interface."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Loopback only. The panel is never exposed outside the machine.
ADDRESS = "127.0.0.1"
PANEL_PORT = 8080
ENGINE_PORT = 8081

HIDDEN = Path(__file__).resolve().parent / "hidden.json"
STATIC = Path(__file__).resolve().parent / "static"

# The audio the Generate tab produces. It's the same folder as `voice.py say`:
# two destinations for the same thing would only raise the question of where
# to look.
OUTPUTS = ROOT / "outputs"

# Cloning's pending folder: the uploaded audio, preparation.py's `_ref.wav`
# and the new `.safetensors`. It only moves to `voices/` after the user
# listens to the test and approves it — that's what keeps a bad attempt from
# destroying a voice that already worked.
PENDING = ROOT / "work" / "pending"

ENV = ROOT / ".env"


def voices_dir() -> Path:
    """Where the voices live, resolved the same way as `bot.py`.

    The supervisor and the engine have to agree: the panel lists what Discord
    will offer. Hard-coding `ROOT/voices` in the supervisor only worked until
    someone changed `VOICES_DIR` — and that key is precisely one of the ones
    the panel lets you edit. The day someone pointed the voices at another
    disk, the Voices tab would list the old folder and the toggles would write
    names that match nothing.

    Call it after `load_dotenv()`. A relative path is resolved against the
    project root, not against the caller's working directory.
    """
    path = Path(os.getenv("VOICES_DIR", "voices"))
    return path if path.is_absolute() else ROOT / path
