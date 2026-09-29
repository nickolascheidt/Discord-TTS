"""Tests for voice.py's pure functions. None of them loads the model."""

from __future__ import annotations

import io
import wave

import voice


def test_pcm_to_wav_produces_a_readable_wav():
    """The engine returns this over HTTP; the browser has to be able to play it."""
    pcm = b"\x00\x01" * 4800  # 4800 2-byte samples = 2400 stereo frames

    data = voice.pcm_to_wav(pcm)

    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnchannels() == voice.CHANNELS
        assert w.getframerate() == voice.RATE
        assert w.getsampwidth() == voice.BYTES_PER_SAMPLE
        assert w.readframes(w.getnframes()) == pcm


def test_pcm_to_wav_accepts_empty_pcm():
    """A generation that produced nothing can't become an exception halfway through HTTP."""
    data = voice.pcm_to_wav(b"")

    with wave.open(io.BytesIO(data)) as w:
        assert w.getnframes() == 0


def test_wav_duration_counts_the_frames():
    """2400 frames at 48 kHz = 0.05s. It's the number the screen shows."""
    pcm = b"\x00\x01" * 4800

    assert voice.wav_duration(voice.pcm_to_wav(pcm)) == 0.05


def test_write_wav_and_pcm_to_wav_agree(tmp_path):
    """Two routes to the same format: if they diverge, the .ogg comes out wrong."""
    pcm = b"\x10\x20" * 1000
    path = tmp_path / "x.wav"

    voice.write_wav(path, pcm)

    assert path.read_bytes() == voice.pcm_to_wav(pcm)


def test_sample_sentence_follows_the_language():
    assert voice.sample_sentence("portuguese_24l").startswith("Olá")
    assert voice.sample_sentence("english").startswith("Hello")


def test_sample_sentence_falls_back_to_english():
    assert voice.sample_sentence("klingon") == voice.SAMPLE_SENTENCES["english"]
