"""Tests for the engine API, with no network and without loading the model.

The doubles inherit from the REAL `discord.Client`, with only login and
connect replaced. An entirely fake object wouldn't do: the bugs these tests
lock in come from the real lifecycle of `_ready`, `_closing_task` and the
`connector` inside discord.py.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

import engine_api

GOOD_TOKEN = "any_token"
BAD_TOKEN = "rejected_token"


class FakeEngine:
    """A fake engine. `stream` returns silent PCM, no model at all."""

    def __init__(self, voices_dir: Path) -> None:
        self.voices_dir = voices_dir
        self.requests: list[tuple[str, str]] = []
        self.imported: list[tuple[str, str, str | None]] = []
        self.threads: list[str] = []

    def available_voices(self) -> list[str]:
        return ["alice", "bob"]

    def _record_thread(self) -> None:
        self.threads.append(threading.current_thread().name)

    def stream(self, voice: str, text: str):
        self._record_thread()
        if voice not in self.available_voices():
            raise KeyError(f"Unknown voice: {voice!r}")
        self.requests.append((voice, text))
        yield b"\x00\x01" * 2400

    def stream_from_path(self, state_path, text: str):
        self._record_thread()
        self.requests.append((str(state_path), text))
        yield b"\x00\x01" * 2400

    def import_wav(self, audio_path, voice_name: str, dest_dir=None):
        self._record_thread()
        self.imported.append(
            (str(audio_path), voice_name, str(dest_dir) if dest_dir else None)
        )
        dest = Path(dest_dir or self.voices_dir) / f"{voice_name}.safetensors"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake safetensors")
        return dest


class OfflineBot(discord.Client):
    """A real discord.py client with login and connect swapped for doubles.

    `login` does what the real one does that matters — it calls
    `_async_setup_hook()`, which is what creates `_ready` — and rejects a bad
    token the same way. `connect` sets `_ready`, imitating the gateway's READY.
    """

    created: list["OfflineBot"] = []

    def __init__(self, engine) -> None:
        super().__init__(intents=discord.Intents.default())
        self.engine = engine
        self._queues: dict[int, asyncio.Queue] = {}
        self.workers_stopped = 0
        self.really_closed = False
        OfflineBot.created.append(self)

    async def login(self, token: str) -> None:
        if token == BAD_TOKEN:
            raise discord.LoginFailure("Improper token has been passed.")
        await self._async_setup_hook()

    async def close(self) -> None:
        self.really_closed = True
        await super().close()

    async def connect(self, *, reconnect: bool = True) -> None:
        # The real connect() is a `while not self.is_closed()`, and that's why
        # it returns by itself when close() happens. The double needs the same
        # stop condition, otherwise disconnect waits forever on a coroutine
        # that never ends.
        self._ready.set()
        while not self.is_closed():
            await asyncio.sleep(0.01)

    async def stop_workers(self) -> None:
        self.workers_stopped += 1


@asynccontextmanager
async def open_api(tmp_path: Path, token: str = GOOD_TOKEN):
    """Starts the API with the offline bot factory; nothing is left running."""
    OfflineBot.created.clear()
    engine = FakeEngine(tmp_path / "voices")
    # A single thread, with the same prefix as bot.py: it's what the queue
    # tests check, and it's what guarantees that generating audio doesn't run
    # on the event loop.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
    app = engine_api.create_app(engine, token, OfflineBot, executor)
    async with TestClient(TestServer(app)) as c:
        try:
            yield c
        finally:
            task = app["state"]["task"]
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            executor.shutdown(wait=True)


@pytest.fixture
async def client(tmp_path):
    async with open_api(tmp_path) as c:
        yield c


async def test_status_with_discord_off(client):
    data = await (await client.get("/status")).json()
    assert data["engine"] == "on"
    assert data["discord"] == "off"
    assert data["voices"] == ["alice", "bob"]


async def test_connect_does_not_return_500(client):
    """Regression: `create_task(start)` + `wait_until_ready()` gave HTTP 500.

    `Client.__init__` leaves `_ready = MISSING` and only `_async_setup_hook()`,
    inside `login()`, creates it. Since awaiting a coroutine runs its body
    right away, without yielding to the event loop, the `start()` task never
    got to run and `wait_until_ready()` raised RuntimeError. It always failed;
    it wasn't a race.
    """
    resp = await client.post("/discord", json={"action": "connect"})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["discord"] == "on"


async def test_connect_with_a_bad_token_gives_a_readable_error(tmp_path):
    """A rejected token has to answer right away, not after 60s of waiting."""
    async with open_api(tmp_path, BAD_TOKEN) as c:
        resp = await c.post("/discord", json={"action": "connect"})

        assert resp.status == 401
        assert "rejected the token" in (await resp.json())["error"]


async def test_connecting_twice_is_idempotent(client):
    await client.post("/discord", json={"action": "connect"})
    first = client.app["state"]["task"]

    resp = await client.post("/discord", json={"action": "connect"})

    assert resp.status == 200
    assert client.app["state"]["task"] is first  # didn't open another connection
    assert len(OfflineBot.created) == 1          # nor another client


async def test_disconnect_stops_the_workers_before_closing(client):
    """Without it, the previous cycle's queues are left pointing at a dead guild."""
    await client.post("/discord", json={"action": "connect"})

    resp = await client.post("/discord", json={"action": "disconnect"})

    assert resp.status == 200
    assert (await resp.json())["discord"] == "off"
    assert OfflineBot.created[-1].workers_stopped == 1
    assert client.app["state"]["bot"] is None
    assert client.app["state"]["task"] is None


async def test_can_connect_again_after_disconnecting(client):
    await client.post("/discord", json={"action": "connect"})
    await client.post("/discord", json={"action": "disconnect"})

    resp = await client.post("/discord", json={"action": "connect"})

    assert resp.status == 200, await resp.text()
    assert (await resp.json())["discord"] == "on"


async def test_every_connection_uses_a_new_client(client):
    """The heart of the design: no discord.py state crosses the cycle.

    Reusing the client required fixing the `connector` by hand (close()
    closes it and clear() doesn't restore it) and living with `loop` going
    back to MISSING from inside connect(). Throwing the object away, none of
    that exists.
    """
    await client.post("/discord", json={"action": "connect"})
    first = OfflineBot.created[-1]

    await client.post("/discord", json={"action": "disconnect"})
    await client.post("/discord", json={"action": "connect"})
    second = OfflineBot.created[-1]

    assert len(OfflineBot.created) == 2
    assert second is not first
    assert first.really_closed
    assert not second.is_closed()


async def test_disconnect_without_connecting_does_not_blow_up(client):
    resp = await client.post("/discord", json={"action": "disconnect"})

    assert resp.status == 200
    assert (await resp.json())["discord"] == "off"
    assert OfflineBot.created == []


async def test_unknown_action_is_400(client):
    resp = await client.post("/discord", json={"action": "dance"})
    assert resp.status == 400
    assert "dance" in (await resp.json())["error"]


# ------------------------------------------------------------------- POST /say


async def test_say_returns_a_wav(client):
    resp = await client.post("/say", json={"voice": "bob", "text": "hello"})

    assert resp.status == 200
    assert resp.headers["Content-Type"] == "audio/wav"
    body = await resp.read()
    assert body[:4] == b"RIFF"
    assert body[8:12] == b"WAVE"


async def test_say_passes_voice_and_text_to_the_engine(client):
    await client.post("/say", json={"voice": "bob", "text": "hello world"})

    assert client.app["engine"].requests == [("bob", "hello world")]


async def test_say_runs_off_the_event_loop(client):
    """The model blocks for tens of seconds; on the loop, the panel would freeze.

    A single thread, and the bot's: that's how a test from the panel queues up
    behind the Discord utterance instead of fighting it for the engine lock.
    """
    await client.post("/say", json={"voice": "bob", "text": "hello"})

    assert client.app["engine"].threads == ["tts_0"]


async def test_say_takes_a_path_instead_of_a_voice(client, tmp_path):
    """It's what lets you listen to a voice that is still in the pending folder."""
    pending = tmp_path / "new.safetensors"
    pending.write_bytes(b"x")

    resp = await client.post("/say", json={"path": str(pending), "text": "hi"})

    assert resp.status == 200
    assert client.app["engine"].requests == [(str(pending), "hi")]


async def test_say_without_text_is_400(client):
    resp = await client.post("/say", json={"voice": "bob", "text": "   "})

    assert resp.status == 400
    assert "text" in (await resp.json())["error"]


async def test_say_with_voice_and_path_together_is_400(client):
    """Ambiguous: without this, one of the two would be silently ignored."""
    resp = await client.post(
        "/say", json={"voice": "bob", "path": "x.safetensors", "text": "hi"}
    )

    assert resp.status == 400


async def test_say_without_voice_or_path_is_400(client):
    resp = await client.post("/say", json={"text": "hi"})

    assert resp.status == 400


async def test_say_with_an_unknown_voice_is_404(client):
    """The engine's KeyError has to become a diagnosis, not an anonymous 500."""
    resp = await client.post("/say", json={"voice": "nobody", "text": "hi"})

    assert resp.status == 404
    assert "nobody" in (await resp.json())["error"]


async def test_say_with_a_path_that_does_not_exist_is_404(client):
    resp = await client.post(
        "/say", json={"path": "does/not/exist.safetensors", "text": "hi"}
    )

    assert resp.status == 404


# ---------------------------------------------------------------- POST /import


async def test_import_calls_the_engine_with_the_pending_folder(client, tmp_path):
    sample = tmp_path / "carol_ref.wav"
    sample.write_bytes(b"RIFF....")
    pending = tmp_path / "pending"

    resp = await client.post(
        "/import",
        json={"file": str(sample), "name": "carol", "dest": str(pending)},
    )

    assert resp.status == 200, await resp.text()
    data = await resp.json()
    assert data["safetensors"] == str(pending / "carol.safetensors")
    assert data["bytes"] > 0
    assert client.app["engine"].imported == [
        (str(sample), "carol", str(pending))
    ]


async def test_import_runs_off_the_event_loop(client, tmp_path):
    """Taking the model lock on the loop would freeze even /status."""
    sample = tmp_path / "a.wav"
    sample.write_bytes(b"RIFF")

    await client.post(
        "/import",
        json={"file": str(sample), "name": "x", "dest": str(tmp_path / "p")},
    )

    assert client.app["engine"].threads == ["tts_0"]


async def test_import_without_dest_uses_the_voices_folder(client, tmp_path):
    sample = tmp_path / "a.wav"
    sample.write_bytes(b"RIFF")

    resp = await client.post("/import", json={"file": str(sample), "name": "x"})

    assert resp.status == 200
    assert client.app["engine"].imported == [(str(sample), "x", None)]
    assert (tmp_path / "voices" / "x.safetensors").is_file()


async def test_import_a_file_that_does_not_exist_is_404(client):
    resp = await client.post(
        "/import", json={"file": "does/not/exist.wav", "name": "x"}
    )

    assert resp.status == 404
    assert "not found" in (await resp.json())["error"]


async def test_import_without_a_name_is_400(client, tmp_path):
    sample = tmp_path / "a.wav"
    sample.write_bytes(b"RIFF")

    resp = await client.post("/import", json={"file": str(sample), "name": ""})

    assert resp.status == 400
