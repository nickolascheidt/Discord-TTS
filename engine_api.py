"""The engine's HTTP API, on 127.0.0.1. Only the panel supervisor talks to it.

It runs on the same event loop as the bot, so it sees the bot's state for free —
no IPC, no state file, no risk of drifting from what Discord sees.

The Discord client is created on every connection and thrown away on
disconnect. The first version reused a single one, relying on
`Client.clear()`, which advertises itself as "re-opened" — but it only
restores part of the state. Left behind were the HTTP `connector`, which
`close()` had already closed, and `loop`, which goes back to MISSING from
inside `connect()` itself (it calls `close()` in its exception handlers). It
took three patches to library internals before it became clear that reusing
was the problem. Throwing the object away costs registering the commands again
and solves the whole class.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from concurrent.futures import Executor
from pathlib import Path

import discord
from aiohttp import web

import voice

log = logging.getLogger("engine")


def create_app(
    engine,
    token: str,
    factory: Callable[[object], object],
    executor: Executor,
) -> web.Application:
    """`factory` takes the engine and returns a new client, ready to log in.

    It comes in as a parameter so this API can be tested without importing
    bot.py, which loads the .env and discord.py just by being imported. The
    `executor` comes in the same way, and it is on purpose the SAME as the
    bot's: a single thread, so a test requested by the panel queues up behind
    the utterance playing on Discord instead of fighting it for the model lock.
    """
    app = web.Application()
    app["engine"] = engine
    app["token"] = token
    app["factory"] = factory
    app["executor"] = executor
    # aiohttp frowns on changing app[...] after the application starts, and
    # these two change on every connection. So what is mutable lives here.
    app["state"] = {"bot": None, "task": None}
    # Connect and disconnect are "check, then wait", and aiohttp runs handlers
    # concurrently. Without the lock, two clicks on Connect open two sessions:
    # both receive every interaction, so /say answers and queues twice, and
    # disconnect only reaches the one that got registered.
    app["gate"] = asyncio.Lock()
    app.add_routes([
        web.get("/status", status),
        web.post("/discord", discord_toggle),
        web.post("/say", say),
        web.post("/import", import_voice),
    ])
    return app


def _summary(bot) -> dict:
    """Discord's state the way the panel needs to see it. `bot` None = disconnected."""
    connected = bot is not None and bot.is_ready() and not bot.is_closed()
    guild = bot.guilds[0] if (connected and bot.guilds) else None
    vc = guild.voice_client if guild else None
    return {
        "discord": "on" if connected else "off",
        "user": str(bot.user) if connected and bot.user else None,
        "guild": guild.name if guild else None,
        "voice_channel": vc.channel.name if vc and vc.channel else None,
        "queue": sum(q.qsize() for q in bot._queues.values()) if connected else 0,
    }


async def status(request: web.Request) -> web.Response:
    return web.json_response({
        "engine": "on",
        "language": os.getenv("TTS_LANGUAGE", "english"),
        # Everything, hidden ones included: the panel needs to show both.
        # It comes from the engine, not the bot, because the engine lists
        # voices even with Discord off.
        "voices": request.app["engine"].available_voices(),
        **_summary(request.app["state"]["bot"]),
    })


async def _connect(request: web.Request) -> web.Response | None:
    """Returns an error response, or None if it connected."""
    state = request.app["state"]
    if state["bot"] is not None:
        return None  # already connected; the summary at the end covers it

    bot = request.app["factory"](request.app["engine"])
    # Claims the slot BEFORE the first await. The lock already serializes, but
    # this keeps the invariant visible without tracking who holds what.
    state["bot"] = bot

    # login() before creating the connect() task, and not `create_task(start())`.
    # `Client.__init__` leaves `_ready = MISSING` and it's `_async_setup_hook()`,
    # inside login(), that creates it. Since awaiting a coroutine runs its body
    # right away, without yielding to the event loop, a `create_task(start())`
    # followed by `wait_until_ready()` would never give the task a chance to
    # run — and wait_until_ready() would raise RuntimeError, every time.
    # As a bonus, login() validates the token here: a wrong token is an
    # immediate error.
    try:
        await bot.login(request.app["token"])
    except discord.LoginFailure as e:
        # login opens the HTTP session before it can reject the token, so
        # dropping the object here would leave a session and a connector
        # dangling on every attempt with a wrong token.
        await _discard(state, bot)
        return web.json_response({"error": f"Discord rejected the token: {e}"}, status=401)
    except Exception as e:  # noqa: BLE001 - any failure has to clean up before raising
        await _discard(state, bot)
        return web.json_response({"error": f"failed to log in to Discord: {e}"}, status=502)

    state["task"] = asyncio.create_task(bot.connect())
    try:
        await asyncio.wait_for(bot.wait_until_ready(), timeout=60)
    except asyncio.TimeoutError:
        # Without tearing down, what's left is a state the screen shows as
        # "offline" but that refuses to reconnect: the button would say
        # Connect and do nothing.
        await _disconnect(request)
        return web.json_response({"error": "Discord didn't answer in 60s"}, status=504)
    return None


async def _discard(state: dict, bot) -> None:
    """Closes and forgets a client that never got to connect."""
    state["bot"] = None
    try:
        await bot.close()
    except Exception:  # noqa: BLE001 - we're already on the error path
        pass


async def _disconnect(request: web.Request) -> None:
    state = request.app["state"]
    bot, task = state["bot"], state["task"]
    if bot is None:
        return

    # Before close(): the per-server queues hold references to guilds that
    # don't survive the close.
    await bot.stop_workers()
    await bot.close()
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)

    # No clear(): the client is thrown away. That's what spares us patching
    # its connector and loop.
    state["bot"] = None
    state["task"] = None


async def discord_toggle(request: web.Request) -> web.Response:
    action = (await request.json()).get("action")
    if action not in ("connect", "disconnect"):
        return web.json_response({"error": f"unknown action: {action}"}, status=400)

    async with request.app["gate"]:
        if action == "connect":
            if (error := await _connect(request)) is not None:
                return error
        else:
            await _disconnect(request)

        return web.json_response(_summary(request.app["state"]["bot"]))


# ------------------------------------------------------------------------ audio


async def say(request: web.Request) -> web.Response:
    """Generates (voice, text) and returns a WAV. The body has `voice` OR `path`.

    `path` points at a `.safetensors` outside `voices/` — it's how the panel
    plays a voice that is still in the pending folder, before approving it.
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    voice_name, path = body.get("voice"), body.get("path")

    if not text:
        return web.json_response({"error": "empty text"}, status=400)
    if bool(voice_name) == bool(path):
        return web.json_response(
            {"error": "send voice or path, never both"}, status=400
        )
    if path and not Path(path).is_file():
        return web.json_response({"error": f"not found: {path}"}, status=404)

    engine = request.app["engine"]

    def work() -> bytes:
        chunks = (
            engine.stream_from_path(path, text)
            if path
            else engine.stream(voice_name, text)
        )
        return voice.pcm_to_wav(b"".join(chunks))

    loop = asyncio.get_running_loop()
    try:
        wav = await loop.run_in_executor(request.app["executor"], work)
    except KeyError as e:
        # The engine raises KeyError for a voice that doesn't exist. Without
        # this branch, a typo would be a 500 and the screen would say "the
        # engine broke".
        return web.json_response({"error": f"unknown voice: {e}"}, status=404)
    except Exception as e:  # noqa: BLE001 - any model failure has to be readable
        log.exception("Failed to generate audio")
        return web.json_response({"error": f"generation failed: {e}"}, status=500)

    return web.Response(body=wav, content_type="audio/wav")


async def import_voice(request: web.Request) -> web.Response:
    """`engine.import_wav()` on an already prepared file.

    `dest` is the folder where the `.safetensors` is created. The panel sends
    the pending folder: the voice only goes into `voices/` after the user
    listens to the test and approves it, which is what keeps a bad attempt
    from overwriting a voice that already worked.
    """
    body = await request.json()
    file, name = body.get("file"), (body.get("name") or "").strip()
    dest = body.get("dest")

    if not name:
        return web.json_response({"error": "empty name"}, status=400)
    if not file or not Path(file).is_file():
        return web.json_response({"error": f"not found: {file}"}, status=404)

    engine = request.app["engine"]
    loop = asyncio.get_running_loop()
    try:
        path = await loop.run_in_executor(
            request.app["executor"],
            lambda: engine.import_wav(file, name, dest_dir=dest),
        )
    except Exception as e:  # noqa: BLE001 - the screen needs the cause, not a silent 500
        log.exception("Failed to import the voice")
        return web.json_response({"error": f"import failed: {e}"}, status=500)

    path = Path(path)
    return web.json_response({
        "safetensors": str(path),
        "bytes": path.stat().st_size,
    })
