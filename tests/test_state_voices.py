import json
from pathlib import Path

import panel
from panel import state


def test_read_hidden_missing_file(tmp_path):
    assert state.read_hidden(tmp_path / "missing.json") == set()


def test_read_hidden_corrupted_json(tmp_path):
    """A broken file can't take the panel down — same stance as history.py."""
    path = tmp_path / "hidden.json"
    path.write_text("{this is not json", encoding="utf-8")
    assert state.read_hidden(path) == set()


def test_write_and_read_back(tmp_path):
    path = tmp_path / "hidden.json"
    state.write_hidden(path, {"alice", "bob"})
    assert state.read_hidden(path) == {"alice", "bob"}


def test_write_creates_the_folder(tmp_path):
    path = tmp_path / "sub" / "hidden.json"
    state.write_hidden(path, {"alice"})
    assert path.is_file()


def test_write_writes_a_sorted_list(tmp_path):
    """Stable order so the file doesn't produce a fake diff on every write."""
    path = tmp_path / "hidden.json"
    state.write_hidden(path, {"carol", "bob", "alice"})
    assert json.loads(path.read_text(encoding="utf-8")) == ["alice", "bob", "carol"]


def test_list_voices_sorts_and_marks_hidden(tmp_path):
    (tmp_path / "bob.safetensors").write_bytes(b"x" * 100)
    (tmp_path / "alice.safetensors").write_bytes(b"y" * 250)

    voices = state.list_voices(tmp_path, {"bob"})

    assert voices == [
        {"name": "alice", "bytes": 250, "hidden": False},
        {"name": "bob", "bytes": 100, "hidden": True},
    ]


def test_list_voices_ignores_a_hidden_voice_that_left_the_disk(tmp_path):
    """Deleting the .safetensors can't make the voice come back as a ghost."""
    (tmp_path / "alice.safetensors").write_bytes(b"y")
    voices = state.list_voices(tmp_path, {"bob_deleted"})
    assert [v["name"] for v in voices] == ["alice"]


def test_list_voices_missing_folder(tmp_path):
    assert state.list_voices(tmp_path / "missing", set()) == []


def test_visible_names_leaves_out_the_hidden_ones(tmp_path):
    (tmp_path / "bob.safetensors").write_bytes(b"x")
    (tmp_path / "alice.safetensors").write_bytes(b"y")
    assert state.visible_names(tmp_path, {"bob"}) == ["alice"]


def test_voices_dir_follows_VOICES_DIR(monkeypatch):
    """The panel and the engine have to see the SAME folder.

    The supervisor used to hard-code `ROOT/voices` while the bot read
    `VOICES_DIR` from the .env. They only agreed on the default — and
    `VOICES_DIR` is one of the keys the panel lets you edit. Pointing the
    voices at another disk would make the Voices tab list the old folder and
    the toggles write names that no longer exist.
    """
    monkeypatch.delenv("VOICES_DIR", raising=False)
    assert panel.voices_dir() == panel.ROOT / "voices"

    monkeypatch.setenv("VOICES_DIR", "./others")
    assert panel.voices_dir() == panel.ROOT / "others"

    absolute = panel.ROOT.parent / "elsewhere"
    monkeypatch.setenv("VOICES_DIR", str(absolute))
    assert panel.voices_dir() == Path(absolute)
