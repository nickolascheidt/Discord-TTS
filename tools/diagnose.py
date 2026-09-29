"""Diagnostics. Use it when something breaks — it isn't part of normal use.

To clone voices and generate audio, use voice.py at the project root.

    python tools/diagnose.py resampler
        Doesn't load the model, runs in a second. Pushes a sine wave through
        _StreamResampler and measures discontinuities at the chunk seams.
        Only matters if you touch _StreamResampler in tts_engine.py — it's
        what proved that the click in the audio came from the seams.

    python tools/diagnose.py smoke
        A built-in catalog voice for TTS_LANGUAGE, the raw pocket-tts API, none
        of our code in between. Checks the install, the weights download and
        the language. Answers "is the problem my sample or the model?" and
        validates the setup on a new machine. Writes outputs/smoke.wav.
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

# The script lives in tools/, but imports tts_engine from the root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUTPUTS = ROOT / "outputs"

import numpy as np  # noqa: E402


def _write_wav(path: Path, data: np.ndarray | bytes, rate: int, channels: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        if isinstance(data, bytes):
            w.writeframes(data)
        else:
            w.writeframes((np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())


# -------------------------------------------------------------- resampler mode


def resampler_mode() -> None:
    """Detects clicks at the chunk seams without loading the model."""
    from tts_engine import _StreamResampler

    SR, FREQ, CHUNK, AMP = 24_000, 440.0, 1920, 0.5
    n = SR * 3
    sine = (np.sin(2 * np.pi * FREQ * np.arange(n) / SR) * AMP).astype(np.float32)

    r = _StreamResampler(SR)
    output = bytearray()
    for k in range(0, n, CHUNK):
        output += r.push(sine[k : k + CHUNK])
    output += r.flush()

    pcm = np.frombuffer(bytes(output), dtype="<i2").reshape(-1, 2)
    if not np.array_equal(pcm[:, 0], pcm[:, 1]):
        print("FAIL: L/R channels differ (they should be identical)")

    mono = pcm[:, 0].astype(np.float32) / 32767.0
    steps = np.abs(np.diff(mono))
    expected = AMP * 2 * np.pi * FREQ / 48_000
    worst = (steps > 2 * expected).sum()

    print(f"samples: {len(mono)} (expected ~{n * 2})")
    print(f"expected step: {expected:.5f} | largest seen: {steps.max():.5f}")
    print(f"suspicious seams: {worst}")
    print("OK — no audible clicks." if worst == 0 else "FAIL — there's a discontinuity.")

    dest = OUTPUTS / "resampler_440hz.wav"
    _write_wav(dest, bytes(output), 48_000, channels=2)
    print(f"-> {dest} (should be a clean, continuous tone)")


# ------------------------------------------------------------------ smoke mode


def smoke_mode(language: str) -> None:
    """The smallest possible test: a catalog voice, none of our code in between."""
    from pocket_tts import TTSModel
    from pocket_tts.default_parameters import (
        DEFAULT_VOICE_FALLBACK,
        DEFAULT_VOICE_FOR_LANGUAGE,
        get_default_text_for_language,
    )

    voice = next(
        (v for k, v in DEFAULT_VOICE_FOR_LANGUAGE.items() if k in language),
        DEFAULT_VOICE_FALLBACK,
    )

    t0 = time.time()
    model = TTSModel.load_model(language=language)
    print(f"model loaded in {time.time() - t0:.1f}s | sample rate {model.sample_rate} Hz")

    t0 = time.time()
    state = model.get_state_for_audio_prompt(voice)
    audio = model.generate_audio(state, get_default_text_for_language(language))
    spent = time.time() - t0
    dur = len(audio) / model.sample_rate
    print(f"voice {voice!r}: {dur:.1f}s of audio in {spent:.1f}s ({dur / spent:.1f}x real time)")

    dest = OUTPUTS / "smoke.wav"
    _write_wav(dest, audio.numpy(), model.sample_rate)
    print(f"-> {dest}")


# ----------------------------------------------------------------------- main


def main() -> int:
    import os

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    language = os.getenv("TTS_LANGUAGE", "english")

    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode == "resampler":
        resampler_mode()
    elif mode == "smoke":
        smoke_mode(language)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
