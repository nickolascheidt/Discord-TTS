"""Tests for bot.py that need neither Discord nor the loaded model.

Importing `bot` is cheap: the `TTSEngine` isn't built on import (it comes in
through the factory), and the token isn't read by the module. What's tested
here is the seam that "one client per connection" made critical.
"""

from __future__ import annotations

import asyncio

import pytest

import bot as bot_mod


class FakeEngine:
    def available_voices(self) -> list[str]:
        return ["alice", "bob"]


@pytest.fixture(autouse=True)
def clean_flag():
    """The flag is module-global; without resetting it, one test pollutes the next."""
    bot_mod._COMMANDS_SYNCED = False
    yield
    bot_mod._COMMANDS_SYNCED = False


async def test_factory_returns_distinct_clients():
    engine = FakeEngine()
    first = bot_mod.create_bot(engine)
    second = bot_mod.create_bot(engine)

    assert first is not second
    assert first.engine is engine is second.engine


async def test_every_new_client_gets_the_six_commands():
    for _ in range(2):
        client = bot_mod.create_bot(FakeEngine())
        names = sorted(c.name for c in client.tree.get_commands())
        assert names == ["leave", "say", "skip", "status", "stop", "voices"]


async def test_syncs_the_commands_once_per_process(monkeypatch):
    """The flag has to belong to the process, not the instance.

    Every connection creates a new client, so an instance field would never
    see the previous sync: every reconnect would push the commands to Discord
    again and hit the rate limit. This test is what prevents that regression.
    """
    syncs = []

    async def fake_sync(self, *, guild=None):
        syncs.append(guild)
        return []

    monkeypatch.setattr(type(bot_mod.create_bot(FakeEngine()).tree), "sync", fake_sync)
    monkeypatch.setattr(bot_mod, "GUILD_ID", None)

    await bot_mod.create_bot(FakeEngine()).setup_hook()
    await bot_mod.create_bot(FakeEngine()).setup_hook()
    await bot_mod.create_bot(FakeEngine()).setup_hook()

    assert len(syncs) == 1


async def test_visible_voices_leaves_out_the_hidden_ones(tmp_path, monkeypatch):
    """The bridge between the panel and Discord's autocomplete."""
    (tmp_path / "bob.safetensors").write_bytes(b"x")
    (tmp_path / "alice.safetensors").write_bytes(b"y")
    hidden = tmp_path / "hidden.json"

    monkeypatch.setattr(bot_mod, "VOICES_DIR", str(tmp_path))
    monkeypatch.setattr(bot_mod, "HIDDEN", hidden)
    client = bot_mod.create_bot(FakeEngine())

    assert client.visible_voices() == ["alice", "bob"]

    bot_mod.panel_state.write_hidden(hidden, {"bob"})

    # Re-reads the file on every call — that's what spares notifying the engine.
    assert client.visible_voices() == ["alice"]


async def test_stop_workers_clears_queues_and_tasks():
    client = bot_mod.create_bot(FakeEngine())
    client.queue_for(123)
    client.queue_for(456)
    assert len(client._workers) == 2

    await client.stop_workers()

    assert client._workers == {}
    assert client._queues == {}
    await asyncio.sleep(0)


def test_sanitize_cleans_mentions_links_and_markdown():
    assert bot_mod.sanitize("look **at** <@123> https://x.y/z") == "look at link"


# ------------------------------------------------------------------ idle leave


class _FakeVoiceClient:
    def __init__(self):
        self.left = False

    async def disconnect(self):
        self.left = True


class _FakeGuild:
    name = "server"

    def __init__(self):
        self.voice_client = _FakeVoiceClient()


async def test_idle_bot_leaves_the_channel(monkeypatch):
    monkeypatch.setattr(bot_mod, "IDLE_TIMEOUT", 0.01)
    client = bot_mod.create_bot(FakeEngine())
    guild = _FakeGuild()
    monkeypatch.setattr(client, "get_guild", lambda _gid: guild)

    client.queue_for(1)
    await asyncio.sleep(0.05)
    await client.stop_workers()

    assert guild.voice_client.left
