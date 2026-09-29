"""Environment check: ffmpeg on the PATH and the .venv's Python 3.11."""

from __future__ import annotations

from panel import environment


def test_venv_python_points_inside_the_project():
    path = environment.venv_python()

    assert path.parent.parent.name == ".venv"
    assert path.name in ("python.exe", "python")


def test_check_lists_both_dependencies():
    items = {c["what"] for c in environment.check()}

    assert items == {"ffmpeg", ".venv python"}


def test_check_reports_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: None)

    ffmpeg = next(c for c in environment.check() if c["what"] == "ffmpeg")

    assert ffmpeg["ok"] is False
    assert "PATH" in ffmpeg["detail"]


def test_check_finds_ffmpeg(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    ffmpeg = next(c for c in environment.check() if c["what"] == "ffmpeg")

    assert ffmpeg["ok"] is True
    assert "ffmpeg" in ffmpeg["detail"]


def test_check_reports_missing_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, "venv_python", lambda: tmp_path / "does" / "not_exist")

    venv = next(c for c in environment.check() if c["what"] == ".venv python")

    assert venv["ok"] is False
    assert "not_exist" in venv["detail"]
    assert "cloning" in venv["why"]


def test_every_item_says_what_it_is_for_and_how_to_fix_it():
    """A bare "not found" leaves someone who just installed the project stuck."""
    for item in environment.check():
        assert item["why"]
        assert item["fix"] and all(isinstance(line, str) for line in item["fix"])
        assert item["docs"].startswith("https://")


def test_the_venv_fix_creates_it_with_python_3_11_and_installs_the_prep_packages():
    venv = next(c for c in environment.check() if c["what"] == ".venv python")
    commands = "\n".join(venv["fix"])

    assert "3.11" in commands
    assert "requirements-prep.txt" in commands
    assert "download.pytorch.org" in commands


def test_missing_says_what_is_missing(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: None)

    assert "ffmpeg" in environment.missing()


def test_missing_is_empty_when_everything_is_in_place(monkeypatch):
    monkeypatch.setattr(environment, "check", lambda: [{"what": "x", "ok": True, "detail": ""}])

    assert environment.missing() == []
