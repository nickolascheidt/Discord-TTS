import history


def entry(**fields):
    base = {
        "time": "2026-09-16T10:00:00-03:00",
        "event": "say",
        "user": "alice",
        "voice": "bob",
        "chars": 10,
        "guild_id": 1,
    }
    return {**base, **fields}


def test_no_records():
    s = history.stats([])
    assert s["total"] == 0
    assert s["by_voice"] == {}


def test_counts_only_utterances():
    s = history.stats([
        entry(),
        entry(voice="carol"),
        entry(event="stop"),
    ])
    assert s["total"] == 2
    assert s["events"] == 3
    assert s["others"]["stop"] == 1


def test_ranking_by_voice_and_by_user():
    s = history.stats([
        entry(voice="bob"),
        entry(voice="bob"),
        entry(voice="carol", user="someone"),
    ])
    assert s["by_voice"].most_common(1) == [("bob", 2)]
    assert s["by_user"].most_common(1) == [("alice", 2)]


def test_average_chars():
    s = history.stats([entry(chars=10), entry(chars=20)])
    assert s["chars"] == 30
    assert s["avg_chars"] == 15


def test_first_and_last_day():
    s = history.stats([
        entry(time="2026-09-16T10:00:00-03:00"),
        entry(time="2026-08-21T10:00:00-03:00"),
    ])
    assert s["first_day"] == "2026-08-21"
    assert s["last_day"] == "2026-09-16"


def test_failures_show_up_in_others():
    """Discord's /status never showed this; the panel does."""
    s = history.stats([entry(), entry(event="failure", error="RuntimeError: x")])
    assert s["others"]["failure"] == 1


def test_record_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setenv("HISTORY_FILE", str(tmp_path / "logs" / "history.jsonl"))

    history.record("say", voice="bob", text="hi", guild_id=1)
    history.record("say", voice="bob", text="other server", guild_id=2)

    assert [r["text"] for r in history.read()] == ["hi", "other server"]
    assert [r["text"] for r in history.read(guild_id=1)] == ["hi"]


def test_read_skips_corrupted_lines(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    path.write_text('{"event": "say"}\n{broken\n', encoding="utf-8")
    monkeypatch.setenv("HISTORY_FILE", str(path))

    assert history.read() == [{"event": "say"}]
