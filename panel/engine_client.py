"""HTTP conversation with the engine. Knows nothing about subprocesses."""

from __future__ import annotations

import aiohttp

from panel import ADDRESS, ENGINE_PORT

BASE = f"http://{ADDRESS}:{ENGINE_PORT}"

# `total` is generous because connecting to Discord can take tens of seconds.
# `sock_connect` is short because the only thing it measures is opening a
# socket on loopback, which takes under a millisecond when the engine is up.
#
# The value exists for a measured reason: on some Windows machines a refused
# connection on 127.0.0.1 takes ~2s to return ECONNREFUSED (for any port —
# likely a firewall or antivirus filtering loopback). Without this limit, every
# screen refresh with the engine off would cost those 2s, and the screen would
# look frozen precisely while the model is loading.
TIMEOUT = aiohttp.ClientTimeout(total=90, sock_connect=0.5)

# Generating audio and importing a voice take from seconds to minutes: the
# model may run at around real time, and a generation requested by the panel
# still waits its turn behind whatever is playing on Discord. The normal
# TIMEOUT, sized for connecting to Discord, would cut the generation halfway
# and the screen would say the engine disappeared — while it is working.
LONG_TIMEOUT = aiohttp.ClientTimeout(total=600, sock_connect=0.5)


class EngineUnavailable(Exception):
    """The engine didn't answer. Almost always means it's off."""


class EngineRefused(Exception):
    """The engine answered, and what it said is that it didn't work.

    Kept apart from EngineUnavailable on purpose: they're opposite
    diagnoses. One says "the engine is down", the other carries the
    explanation the engine itself wrote — "Discord rejected the token", for
    example. Mixing the two would make the panel say the engine is off when
    it's alive and well and simply disagreeing.
    """

    def __init__(self, status: int, error: str) -> None:
        self.status = status
        self.error = error
        super().__init__(f"HTTP {status}: {error}")


async def _request(method: str, route: str, **kwargs) -> dict:
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.request(method, BASE + route, **kwargs) as resp:
                try:
                    body = await resp.json()
                except aiohttp.ContentTypeError:
                    # A non-JSON response means the engine broke before
                    # reaching our code — aiohttp's error page. Without this
                    # branch, a 500 from the engine would become a
                    # "ClientError" and the screen would say it's off.
                    text = (await resp.text())[:300].strip()
                    raise EngineRefused(resp.status, f"the engine broke: {text}")
                if resp.status >= 400:
                    raise EngineRefused(resp.status, body.get("error", f"HTTP {resp.status}"))
                return body
    except (aiohttp.ClientError, TimeoutError) as e:
        raise EngineUnavailable(str(e)) from e


async def _error_from_response(resp: aiohttp.ClientResponse) -> EngineRefused:
    """Builds the EngineRefused by reading the body, which may not be JSON."""
    try:
        body = await resp.json()
        return EngineRefused(resp.status, body.get("error", f"HTTP {resp.status}"))
    except aiohttp.ContentTypeError:
        text = (await resp.text())[:300].strip()
        return EngineRefused(resp.status, f"the engine broke: {text}")


async def say(text: str, voice: str | None = None, path: str | None = None) -> bytes:
    """Returns the raw WAV. `path` is for a voice still in the pending folder.

    It doesn't go through `_request` because the success response is binary:
    it would call `resp.json()` and turn a perfect WAV into EngineRefused.
    """
    body = {"voice": voice, "text": text, "path": path}
    try:
        async with aiohttp.ClientSession(timeout=LONG_TIMEOUT) as session:
            async with session.post(BASE + "/say", json=body) as resp:
                if resp.status >= 400:
                    raise await _error_from_response(resp)
                return await resp.read()
    except (aiohttp.ClientError, TimeoutError) as e:
        raise EngineUnavailable(str(e)) from e


async def import_voice(file: str, name: str, dest: str | None = None) -> dict:
    body = {"file": file, "name": name, "dest": dest}
    try:
        async with aiohttp.ClientSession(timeout=LONG_TIMEOUT) as session:
            async with session.post(BASE + "/import", json=body) as resp:
                if resp.status >= 400:
                    raise await _error_from_response(resp)
                return await resp.json()
    except (aiohttp.ClientError, TimeoutError) as e:
        raise EngineUnavailable(str(e)) from e


async def status() -> dict:
    return await _request("GET", "/status")


async def discord(action: str) -> dict:
    return await _request("POST", "/discord", json={"action": action})


async def is_up() -> bool:
    try:
        await status()
        return True
    except EngineUnavailable:
        return False
