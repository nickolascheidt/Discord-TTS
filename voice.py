"""Voice manager. Everything you do day to day is here.

    python voice.py list
        Shows the available voices. Doesn't load the model — answers right away.

    python voice.py clone sample.wav alice
        Creates voices/alice.safetensors from a reference WAV (15-30 seconds
        of clean speech is enough) and generates a test clip in outputs/ so
        you can check that it came out well. --no-test skips that.

    python voice.py say alice "text to say"
        Generates outputs/alice_text_to_say.wav.
        --whatsapp  delivers an .ogg ready to send as a voice message
        --play      opens the file as soon as it's done
        -o FILE     picks the output path by hand

The bot sees any new voice right away, without restarting.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VOICES = ROOT / "voices"
OUTPUTS = ROOT / "outputs"

# The sentence used to check a freshly cloned voice. It follows TTS_LANGUAGE:
# an English sentence read by a Portuguese model says little about the voice.
SAMPLE_SENTENCES = {
    "english": "Hello, this is my cloned voice. If you can understand me clearly, it worked.",
    "portuguese": "Olá, essa é a minha voz clonada. Se você me entende bem, deu certo.",
    "spanish": "Hola, esta es mi voz clonada. Si me entiendes bien, funcionó.",
    "french": "Bonjour, voici ma voix clonée. Si tu me comprends bien, ça a marché.",
    "german": "Hallo, das ist meine geklonte Stimme. Wenn du mich gut verstehst, hat es geklappt.",
    "italian": "Ciao, questa è la mia voce clonata. Se mi capisci bene, ha funzionato.",
}


def sample_sentence(language: str | None = None) -> str:
    """The test sentence for `language` (e.g. "portuguese_24l"), English by default."""
    language = (language or os.getenv("TTS_LANGUAGE", "english")).lower()
    for key, text in SAMPLE_SENTENCES.items():
        if language.startswith(key):
            return text
    return SAMPLE_SENTENCES["english"]


# The format Discord and WhatsApp expect from the PCM that TTSEngine produces.
RATE = 48_000
CHANNELS = 2
BYTES_PER_SAMPLE = 2


# ------------------------------------------------------------------ utilities


def slug(text: str, limit: int = 40) -> str:
    """'Hey, dude!' -> 'hey_dude'. A predictable file name, without accents."""
    base = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").lower()
    return base[:limit].strip("_") or "audio"


def free_path(base: Path) -> Path:
    """Avoids overwriting: name.wav -> name_2.wav -> name_3.wav."""
    if not base.exists():
        return base
    for n in range(2, 1000):
        alt = base.with_name(f"{base.stem}_{n}{base.suffix}")
        if not alt.exists():
            return alt
    raise RuntimeError(f"couldn't find a free name for {base}")


def pcm_to_wav(pcm: bytes) -> bytes:
    """Wraps the TTSEngine's PCM in a complete WAV, without touching the disk.

    The engine delivers audio over HTTP, and writing a temporary file just to
    read it back would be pure overhead. The format is the same as
    `write_wav`'s because the source is the same: `TTSEngine` only produces
    Discord's format.
    """
    buffer = io.BytesIO()
    # `wave` seeks back to fix the size in the header; a BytesIO allows it, a
    # socket wouldn't. That's why the WAV is built entirely in memory first.
    with wave.open(buffer, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(BYTES_PER_SAMPLE)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buffer.getvalue()


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pcm_to_wav(pcm))


def wav_duration(data: bytes) -> float:
    """Seconds of audio in an in-memory WAV, read from the header.

    The panel gets WAV, not PCM, so it can't divide the body size by the rate:
    the 44-byte header would give a wrong duration.
    """
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()


def wav_to_ogg(wav: Path, ogg: Path) -> None:
    """Mono Opus at 32 kbps with the voice profile — what WhatsApp uses for voice notes."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(wav),
         "-c:a", "libopus", "-b:a", "32k", "-ac", "1", "-application", "voip", str(ogg)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{r.stderr.strip()}")


def open_file(path: Path) -> None:
    """Plays the file in the system's default player."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as e:
        print(f"  (couldn't open the file: {e})")


def available_voices() -> list[str]:
    if not VOICES.is_dir():
        return []
    return sorted(p.stem for p in VOICES.glob("*.safetensors"))


def generate(engine, voice: str, text: str) -> bytes:
    """Goes through the same path the bot uses and reports the real-time factor."""
    t0 = time.time()
    chunks = list(engine.stream(voice, text))
    total = time.time() - t0

    pcm = b"".join(chunks)
    dur = len(pcm) / (RATE * CHANNELS * BYTES_PER_SAMPLE)
    print(f"  {dur:.1f}s of audio in {total:.1f}s ({dur / total:.1f}x real time)")
    return pcm


def deliver(pcm: bytes, dest: Path, whatsapp: bool) -> Path:
    """Writes the PCM to disk, converting to .ogg if it's for WhatsApp."""
    if not whatsapp:
        write_wav(dest, pcm)
        return dest

    temporary = dest.with_suffix(".wav.tmp")
    write_wav(temporary, pcm)
    try:
        wav_to_ogg(temporary, dest)
    finally:
        temporary.unlink(missing_ok=True)
    return dest


# ------------------------------------------------------------------- commands


def cmd_list() -> int:
    voices = available_voices()
    if not voices:
        print(f"no voices in {VOICES}")
        print("create one with: python voice.py clone sample.wav NAME")
        return 1
    print(f"{len(voices)} voice(s) in {VOICES}:")
    for v in voices:
        size = (VOICES / f"{v}.safetensors").stat().st_size / 1e6
        print(f"  {v:<16} {size:6.1f} MB")
    return 0


def cmd_clone(file: str, name: str, no_test: bool, language: str) -> int:
    source = Path(file)
    if not source.is_file():
        print(f"not found: {source}")
        return 1
    if source.suffix.lower() != ".wav":
        print(f"warning: {source.suffix} needs `pip install soundfile`. WAV is the safe path.")

    name = slug(name, limit=32)
    dest = VOICES / f"{name}.safetensors"
    if dest.exists():
        print(f"warning: overwriting {dest.name}")

    from tts_engine import TTSEngine

    engine = TTSEngine(VOICES, language=language)

    t0 = time.time()
    engine.import_wav(source, name)
    print(f"-> {dest}  ({dest.stat().st_size / 1e6:.1f} MB, {time.time() - t0:.1f}s)")

    if no_test:
        print("\nThe bot already sees this voice, no restart needed.")
        return 0

    print("\ngenerating a test clip...")
    pcm = generate(engine, name, sample_sentence(language))
    output = free_path(OUTPUTS / f"{name}_test.wav")
    write_wav(output, pcm)
    print(f"-> {output}")
    print("\nListen to the test. If it came out bad, pick another sample and run it again.")
    print("The bot already sees this voice, no restart needed.")
    return 0


def cmd_say(voice: str, text: str, whatsapp: bool, play: bool, output: str | None, language: str) -> int:
    available = available_voices()
    if voice not in available:
        print(f"voice {voice!r} doesn't exist. Available: {', '.join(available) or '(none)'}")
        return 1

    # Fail before spending seconds loading the model.
    if whatsapp and shutil.which("ffmpeg") is None:
        print("ffmpeg not found on PATH — it's needed for --whatsapp.")
        return 1

    extension = ".ogg" if whatsapp else ".wav"
    if output:
        dest = Path(output)
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        dest = free_path(OUTPUTS / f"{voice}_{slug(text)}{extension}")

    from tts_engine import TTSEngine

    engine = TTSEngine(VOICES, language=language)
    pcm = generate(engine, voice, text)

    try:
        dest = deliver(pcm, dest, whatsapp)
    except RuntimeError as e:
        print(e)
        return 1

    print(f"-> {dest}  ({dest.stat().st_size / 1024:.0f} KB)")
    if play:
        open_file(dest)
    return 0


# ----------------------------------------------------------------------- main


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    language = os.getenv("TTS_LANGUAGE", "english")

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="shows the available voices")

    c = sub.add_parser("clone", help="reference WAV -> new voice")
    c.add_argument("file", help="WAV with 15-30s of clean speech")
    c.add_argument("name", help="voice name (becomes the file name)")
    c.add_argument("--no-test", action="store_true", help="don't generate a check clip")

    s = sub.add_parser("say", help="generates audio with an existing voice")
    s.add_argument("voice")
    s.add_argument("text")
    s.add_argument("--whatsapp", action="store_true", help="delivers an .ogg ready for a voice message")
    s.add_argument("--play", action="store_true", help="opens the file when done")
    s.add_argument("-o", "--output", default=None, help="output path, by hand")

    args = ap.parse_args()

    OUTPUTS.mkdir(exist_ok=True)

    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "clone":
        return cmd_clone(args.file, args.name, args.no_test, language)
    return cmd_say(args.voice, args.text, args.whatsapp, args.play, args.output, language)


if __name__ == "__main__":
    sys.exit(main())
