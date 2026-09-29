"""TTSEngine path resolution, without loading Pocket TTS.

Importing `tts_engine` costs ~2s and loads no model: `TTSModel.load_model` is
only called in `__init__`. The engine in these tests is built with
`object.__new__`, with the three attributes the methods use filled in by hand.
It's white-box on purpose — what's being locked in here is exactly the path
resolution, and there's no other way to exercise it without 1.3 GB of weights.
"""

from __future__ import annotations

import threading

import pytest

import tts_engine


class FakeModel:
    """Records which path it was called with. Doesn't generate real audio."""

    sample_rate = 24_000

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def get_state_for_audio_prompt(self, path: str):
        self.prompts.append(path)
        return {"state": path}

    def generate_audio_stream(self, state, text):
        return iter(())  # no chunks: the resampler emits nothing and the stream ends


@pytest.fixture
def engine(tmp_path):
    e = object.__new__(tts_engine.TTSEngine)
    e.voices_dir = tmp_path / "voices"
    e.voices_dir.mkdir()
    e.model = FakeModel()
    e._lock = threading.Lock()
    return e


def test_stream_resolves_the_name_inside_the_voices_folder(engine):
    (engine.voices_dir / "alice.safetensors").write_bytes(b"x")

    list(engine.stream("alice", "hi"))

    assert engine.model.prompts == [str(engine.voices_dir / "alice.safetensors")]


def test_stream_of_an_unknown_voice_raises_KeyError(engine):
    with pytest.raises(KeyError):
        list(engine.stream("nobody", "hi"))


def test_stream_from_path_accepts_a_file_outside_the_voices_folder(engine, tmp_path):
    """It's what lets you listen to a voice that is still in the pending folder."""
    pending = tmp_path / "pending" / "new.safetensors"
    pending.parent.mkdir()
    pending.write_bytes(b"x")

    list(engine.stream_from_path(pending, "hi"))

    assert engine.model.prompts == [str(pending)]


def test_import_wav_writes_to_the_voices_folder_by_default(engine, monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(
        tts_engine, "export_model_state", lambda state, dest: written.append(dest)
    )

    output = engine.import_wav("sample.wav", "bob")

    assert output == engine.voices_dir / "bob.safetensors"
    assert written == [str(engine.voices_dir / "bob.safetensors")]


def test_import_wav_honors_dest_dir(engine, tmp_path, monkeypatch):
    """The new voice is born in the pending folder: Discord can't see it yet."""
    written: list[str] = []
    monkeypatch.setattr(
        tts_engine, "export_model_state", lambda state, dest: written.append(dest)
    )
    pending = tmp_path / "pending"

    output = engine.import_wav("sample.wav", "bob", dest_dir=pending)

    assert output == pending / "bob.safetensors"
    assert written == [str(pending / "bob.safetensors")]
    assert pending.is_dir()  # created the folder instead of blowing up
    assert not (engine.voices_dir / "bob.safetensors").exists()
