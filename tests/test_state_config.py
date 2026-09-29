import pytest

from panel import state

SAMPLE_ENV = (
    "# Copy this file to .env and fill in the two tokens.\n"
    "DISCORD_TOKEN=MTIzNDU2.secret.mustnotleak\n"
    "\n"
    "# Maximum characters per utterance.\n"
    "MAX_TEXT_LEN=400\n"
    "HF_TOKEN=hf_anothersecretthatmustnotleak\n"
    "PREBUFFER_SECONDS=0.8\n"
    "KEY_THE_PANEL_DOES_NOT_KNOW=value\n"
)


def write_env(tmp_path, content=SAMPLE_ENV, newline="\n"):
    path = tmp_path / ".env"
    with path.open("w", encoding="utf-8", newline=newline) as f:
        f.write(content)
    return path


def test_read_config_returns_only_allowlisted_keys(tmp_path):
    read = state.read_config(write_env(tmp_path))
    assert read == {"MAX_TEXT_LEN": "400", "PREBUFFER_SECONDS": "0.8"}


def test_read_config_never_exposes_a_token(tmp_path):
    read = state.read_config(write_env(tmp_path))
    assert "DISCORD_TOKEN" not in read
    assert "HF_TOKEN" not in read
    assert not any("secret" in v for v in read.values())


def test_write_changes_only_the_requested_line(tmp_path):
    path = write_env(tmp_path)
    state.write_config(path, {"MAX_TEXT_LEN": "250"})

    expected = SAMPLE_ENV.replace("MAX_TEXT_LEN=400", "MAX_TEXT_LEN=250")
    with path.open(encoding="utf-8", newline="") as f:
        assert f.read() == expected


def test_write_keeps_tokens_byte_for_byte(tmp_path):
    path = write_env(tmp_path)
    state.write_config(path, {"MAX_TEXT_LEN": "250", "PREBUFFER_SECONDS": "1.5"})

    with path.open(encoding="utf-8", newline="") as f:
        lines = f.read().splitlines()
    assert "DISCORD_TOKEN=MTIzNDU2.secret.mustnotleak" in lines
    assert "HF_TOKEN=hf_anothersecretthatmustnotleak" in lines


def test_write_keeps_comments_and_unknown_keys(tmp_path):
    path = write_env(tmp_path)
    state.write_config(path, {"MAX_TEXT_LEN": "250"})

    with path.open(encoding="utf-8", newline="") as f:
        text = f.read()
    assert "# Maximum characters per utterance." in text
    assert "KEY_THE_PANEL_DOES_NOT_KNOW=value" in text


def test_write_keeps_crlf(tmp_path):
    """Windows: rewriting with \\n would turn the whole file into a diff."""
    path = write_env(tmp_path, newline="\r\n")
    state.write_config(path, {"MAX_TEXT_LEN": "250"})

    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")  # no stray \n left


def test_write_appends_a_missing_key_at_the_end(tmp_path):
    path = write_env(tmp_path)
    state.write_config(path, {"IDLE_TIMEOUT": "600"})

    with path.open(encoding="utf-8", newline="") as f:
        assert f.read().rstrip().endswith("IDLE_TIMEOUT=600")


def test_write_refuses_a_key_outside_the_allowlist(tmp_path):
    path = write_env(tmp_path)
    with pytest.raises(ValueError, match="DISCORD_TOKEN"):
        state.write_config(path, {"DISCORD_TOKEN": "stolen"})


def test_write_refuses_a_value_with_a_line_break(tmp_path):
    path = write_env(tmp_path)
    with pytest.raises(ValueError, match="line break"):
        state.write_config(path, {"MAX_TEXT_LEN": "400\nHF_TOKEN=stolen"})


def test_write_refuses_a_number_that_is_not_a_number(tmp_path):
    path = write_env(tmp_path)
    with pytest.raises(ValueError, match="number"):
        state.write_config(path, {"IDLE_TIMEOUT": "three"})
    with pytest.raises(ValueError, match="at least"):
        state.write_config(path, {"MAX_TEXT_LEN": "0"})


def test_write_leaves_a_backup_with_the_old_content(tmp_path):
    path = write_env(tmp_path)
    state.write_config(path, {"MAX_TEXT_LEN": "250"})

    backup = tmp_path / ".env.bak"
    with backup.open(encoding="utf-8", newline="") as f:
        assert f.read() == SAMPLE_ENV
