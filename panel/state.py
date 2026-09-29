"""The panel's persisted state: which voices are hidden, and the config.

Reading and writing to disk, with no notion of HTTP or processes. It's the
easiest module of the panel to test, on purpose.
"""

from __future__ import annotations

import json
from pathlib import Path


# ---------------------------------------------------------------- hidden voices


def read_hidden(path: Path) -> set[str]:
    """Voice names hidden from Discord. A missing or broken file becomes empty.

    Doesn't raise on purpose: a corrupted JSON would take down the whole
    panel over a cosmetic preference.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(name) for name in data}


def write_hidden(path: Path, names: set[str]) -> None:
    """Written sorted so the file doesn't change content without changing meaning."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sorted(names), ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ----------------------------------------------------------------------- voices


def list_voices(voices_dir: Path, hidden: set[str]) -> list[dict]:
    """The disk rules: a hidden voice that was deleted simply doesn't show up."""
    voices_dir = Path(voices_dir)
    if not voices_dir.is_dir():
        return []
    return [
        {
            "name": path.stem,
            "bytes": path.stat().st_size,
            "hidden": path.stem in hidden,
        }
        for path in sorted(voices_dir.glob("*.safetensors"))
    ]


def visible_names(voices_dir: Path, hidden: set[str]) -> list[str]:
    """What Discord may offer. This is what bot.py consumes."""
    return [v["name"] for v in list_voices(voices_dir, hidden) if not v["hidden"]]


# ----------------------------------------------------------------------- config

# The panel only knows these keys. The DISCORD_TOKEN and HF_TOKEN lines aren't
# here, so they're never read or rewritten — not out of care, but because the
# code doesn't know they exist.
EDITABLE_KEYS = (
    "MAX_TEXT_LEN",
    "PREBUFFER_SECONDS",
    "IDLE_TIMEOUT",
    "AUTO_DELETE_SECONDS",
    "VOICES_DIR",
    "TTS_LANGUAGE",
)

# What each key does, so the Config tab doesn't show just the name.
DESCRIPTIONS = {
    "MAX_TEXT_LEN": "maximum characters per utterance",
    "PREBUFFER_SECONDS": "seconds of audio buffered before playing; raise it if you hear cuts",
    "IDLE_TIMEOUT": "seconds without speaking before the bot leaves the call on its own",
    "AUTO_DELETE_SECONDS": "seconds until the bot deletes its own messages; 0 turns it off",
    "VOICES_DIR": "voices folder",
    "TTS_LANGUAGE": "Pocket TTS language model; clone the voices again after changing it",
}

# A number that isn't a number would crash the bot on import.
_NUMBERS = {
    "MAX_TEXT_LEN": (int, 1), "PREBUFFER_SECONDS": (float, 0),
    "IDLE_TIMEOUT": (float, 1), "AUTO_DELETE_SECONDS": (float, 0),
}


def _validate_number(key: str, value: str) -> None:
    if key not in _NUMBERS:
        return
    kind, minimum = _NUMBERS[key]
    try:
        number = kind(value)
    except ValueError:
        raise ValueError(f"{key} must be a number, not {value!r}") from None
    if number < minimum:
        raise ValueError(f"{key} must be at least {minimum}")


# Changing these requires reloading the model or rebuilding discord.py objects.
# The panel uses this to warn that the engine needs a restart.
NEED_RESTART = frozenset(EDITABLE_KEYS)


def _lines(path: Path) -> list[str]:
    """Reads keeping the original line endings (newline='' converts nothing)."""
    with Path(path).open(encoding="utf-8", newline="") as f:
        return f.read().splitlines(keepends=True)


def _key_of(line: str) -> str | None:
    clean = line.strip()
    if not clean or clean.startswith("#") or "=" not in clean:
        return None
    return clean.split("=", 1)[0].strip()


def read_config(path: Path) -> dict[str, str]:
    """Current values of the editable keys. Tokens don't go through here."""
    try:
        lines = _lines(path)
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in lines:
        key = _key_of(line)
        if key in EDITABLE_KEYS:
            values[key] = line.strip().split("=", 1)[1].strip()
    return values


def write_config(path: Path, changes: dict[str, str]) -> None:
    """Replaces the value of each requested key, leaving everything else byte for byte.

    Rewriting the file from a dict would destroy the comments, shuffle the
    order and make the tokens pass through the panel's memory.
    """
    for key, value in changes.items():
        if key not in EDITABLE_KEYS:
            raise ValueError(f"key not editable from the panel: {key}")
        if "\n" in value or "\r" in value:
            raise ValueError(f"the value of {key} has a line break")
        _validate_number(key, value)

    path = Path(path)
    lines = _lines(path)

    # Backup before any write. The .gitignore already covers .env.* .
    with path.with_suffix(path.suffix + ".bak").open(
        "w", encoding="utf-8", newline=""
    ) as f:
        f.writelines(lines)

    # Finds the line ending in use for keys that get appended.
    eol = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"

    pending = dict(changes)
    output: list[str] = []
    for line in lines:
        key = _key_of(line)
        if key in pending:
            end = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            output.append(f"{key}={pending.pop(key)}{end}")
        else:
            output.append(line)

    if pending:
        if output and not output[-1].endswith(("\n", "\r\n")):
            output[-1] += eol
        for key, value in pending.items():
            output.append(f"{key}={value}{eol}")

    with path.open("w", encoding="utf-8", newline="") as f:
        f.writelines(output)
