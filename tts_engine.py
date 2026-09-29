"""Thin layer around Pocket TTS, adapted to feed Discord.

Two responsibilities:
  1. Serialize access to the model (batch size 1, not thread-safe).
  2. Convert the model's audio (mono, 24 kHz, float) into the format Discord
     requires (stereo, 48 kHz, little-endian int16).
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Iterator
from math import gcd
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from pocket_tts import TTSModel, export_model_state

log = logging.getLogger(__name__)

DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS = 2


class _StreamResampler:
    """Resamples successive chunks without introducing discontinuities at the seams.

    The polyphase filter in `resample_poly` needs neighboring samples on BOTH
    sides of each point. Resampling each chunk on its own makes the filter see
    zeros at the edges, which produces an audible click at every seam
    (verifiable: with a 440 Hz sine, the step between samples jumps from
    ~0.06 to ~0.25 exactly at multiples of the chunk size).

    The fix is to hold back the last `_pad` samples of each chunk until the
    next one arrives, and to keep `_pad` already-emitted samples as left
    context. The cost is an extra latency of `_pad` samples — microseconds.
    """

    def __init__(self, source_rate: int) -> None:
        divisor = gcd(DISCORD_SAMPLE_RATE, source_rate)
        self._up = DISCORD_SAMPLE_RATE // divisor
        self._down = source_rate // divisor
        # Half-width of the filter in input samples, with some slack.
        self._pad = max(64, 16 * math.ceil(self._down / self._up))
        self._buf = np.zeros(0, dtype=np.float32)
        self._emitted = 0  # samples at the start of _buf that already became output

    def _to_output_index(self, i: int) -> int:
        return (i * self._up) // self._down

    def _emit_until(self, end: int) -> bytes:
        if end <= self._emitted:
            return b""

        upsampled = resample_poly(self._buf, self._up, self._down)
        lo = self._to_output_index(self._emitted)
        hi = min(self._to_output_index(end), len(upsampled))
        out = upsampled[lo:hi]

        # Keeps `_pad` samples before `end` as context for the next round.
        keep_from = max(0, end - self._pad)
        self._buf = self._buf[keep_from:]
        self._emitted = end - keep_from

        return self._encode(out)

    @staticmethod
    def _encode(mono: np.ndarray) -> bytes:
        pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2")
        stereo = np.repeat(pcm[:, None], DISCORD_CHANNELS, axis=1)
        return stereo.tobytes()

    def push(self, mono: np.ndarray) -> bytes:
        """Consumes a chunk and returns the PCM that can already be played."""
        self._buf = np.concatenate([self._buf, mono.astype(np.float32, copy=False)])
        return self._emit_until(len(self._buf) - self._pad)

    def flush(self) -> bytes:
        """Emits what was held back. Call once, at the end of generation."""
        return self._emit_until(len(self._buf))


class TTSEngine:
    """Keeps the model in memory and produces PCM ready for Discord."""

    def __init__(self, voices_dir: str | Path, language: str = "english") -> None:
        self.voices_dir = Path(voices_dir)
        self.voices_dir.mkdir(parents=True, exist_ok=True)

        # The model uses batch size 1 and keeps internal state while generating.
        # Two simultaneous generations corrupt the audio of both.
        self._lock = threading.Lock()

        log.info("Loading the Pocket TTS model (%s). This takes a few seconds...", language)
        self.model = TTSModel.load_model(language=language)
        log.info("Model ready. Native sample rate: %d Hz", self.model.sample_rate)

    # ----------------------------------------------------------------- voices

    def available_voices(self) -> list[str]:
        return sorted(p.stem for p in self.voices_dir.glob("*.safetensors"))

    def _voice_path(self, name: str) -> Path:
        path = self.voices_dir / f"{name}.safetensors"
        if not path.is_file():
            raise KeyError(f"Unknown voice: {name!r}")
        return path

    def import_wav(
        self,
        audio_path: str | Path,
        voice_name: str,
        dest_dir: str | Path | None = None,
    ) -> Path:
        """Turns an audio file into a voice state (.safetensors).

        `dest_dir` exists for cloning through the panel, which writes to a
        pending folder: a voice only shows up in `voices/` — and therefore in
        Discord's autocomplete — after the user listens to the test and
        approves it.
        """
        folder = Path(dest_dir) if dest_dir is not None else self.voices_dir
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{voice_name}.safetensors"
        with self._lock:
            state = self.model.get_state_for_audio_prompt(str(audio_path))
            export_model_state(state, str(out))
        return out

    # ------------------------------------------------------------------ audio

    def stream(self, voice: str, text: str) -> Iterator[bytes]:
        """Generates PCM incrementally for (voice, text). Blocks — run it in a thread."""
        yield from self.stream_from_path(self._voice_path(voice), text)

    def stream_from_path(self, state_path: str | Path, text: str) -> Iterator[bytes]:
        """Same as `stream`, but for a `.safetensors` anywhere.

        It's what lets you listen to a voice that is still in the pending
        folder, without putting it in `voices/` before it's approved.

        The voice state is re-read from disk on every generation.
        `get_state_for_audio_prompt` detects the .safetensors extension and just
        reads the KV cache, without recomputing anything.
        (`generate_audio_stream` already uses copy_state=True, so the state
        wouldn't be mutated anyway — but re-reading is cheap and keeps the code
        simple.)
        """
        with self._lock:
            state = self.model.get_state_for_audio_prompt(str(state_path))
            resampler = _StreamResampler(self.model.sample_rate)

            for chunk in self.model.generate_audio_stream(state, text):
                mono = np.asarray(chunk.detach().cpu().numpy()).reshape(-1)
                pcm = resampler.push(mono)
                if pcm:
                    yield pcm

            tail = resampler.flush()
            if tail:
                yield tail
