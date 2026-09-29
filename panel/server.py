"""The panel's routes. Knows nothing about subprocesses or how the engine works.

It talks to the process through the `engine` object (which exposes
start/stop/log) and to the engine itself through `engine_client`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web

import history
import voice as voice_mod
from panel import (
    ADDRESS,
    PANEL_PORT,
    ROOT,
    STATIC,
    engine_client,
    environment,
    state,
)
from panel.process import Process

ALLOWED_ORIGINS = {
    f"http://{ADDRESS}:{PANEL_PORT}",
    f"http://localhost:{PANEL_PORT}",
}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@web.middleware
async def check_origin(request: web.Request, handler):
    """Keeps a page open in the browser from messing with your bot.

    127.0.0.1 blocks the network, but not another tab: any website can POST
    to localhost. Reading stays open — the damage would only come from
    writing.
    """
    if request.method in WRITE_METHODS:
        origin = request.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return web.json_response({"error": "origin not allowed"}, status=403)
    return await handler(request)


@web.middleware
async def no_cache_for_ui(request: web.Request, handler):
    """Makes the browser ask before reusing the HTML, CSS and JS.

    Without `Cache-Control`, the browser applies *heuristic caching*: it
    makes up an expiry from `Last-Modified` and, within it, serves what it has
    without talking to the server. The `ETag` aiohttp sends only helps when it
    decides to ask — and it doesn't.

    The symptom is expensive: after updating the panel, the new server answers
    on the port and the browser keeps drawing the old screen.

    `no-cache` doesn't mean "don't store", it means "ask before using". The
    file stays in the cache and revalidation still costs an empty 304; what
    changes is that a new version shows up on the first F5.

    It only applies to the UI. The audio under `/api/audio/` is never
    rewritten (`free_path` creates a new name), so caching that is free.
    """
    response = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def create_app(
    engine,
    voices_dir: Path,
    hidden: Path,
    outputs: Path,
    pending: Path,
    env: Path,
) -> web.Application:
    app = web.Application(middlewares=[check_origin, no_cache_for_ui])
    app["engine"] = engine
    app["voices_dir"] = Path(voices_dir)
    app["hidden"] = Path(hidden)
    app["outputs"] = Path(outputs)
    app["pending"] = Path(pending)
    app["env"] = Path(env)
    app["subscribers"] = set()

    # aiohttp frowns on changing app[...] after the application starts, so
    # what changes at runtime lives in here - the same reason as app["state"]
    # in engine_api. An `app["x"] = y` inside a handler works today and emits
    # a DeprecationWarning, which means it breaks by itself on some aiohttp
    # update.
    app["mutable"] = {
        # A function instead of a list so the tests can swap it for a
        # two-line script: running the real preparation.py would require
        # torch and DeepFilterNet in the test environment.
        "prepare_command": lambda source, trim: [
            str(environment.venv_python()),
            str(ROOT / "preparation.py"),
            str(source),
            *(["--trim"] if trim else []),
        ],
    }

    engine.listen(lambda line: _publish(app, "log", line))

    # One automatic attempt each time someone starts the engine by hand. If
    # the weights don't download or the token is wrong, restarting fifty times
    # doesn't fix it — it only floods the log. The flag goes inside `mutable`
    # because it changes while the application is running.
    app["mutable"]["restarted"] = False
    engine.on_death(
        lambda code: asyncio.get_running_loop().create_task(_died(app, code))
    )

    app.add_routes([
        web.get("/api/state", get_state),
        web.post("/api/engine", post_engine),
        web.post("/api/discord", post_discord),
        web.post("/api/voices/{name}", post_voice),
        web.get("/api/history", get_history),
        web.post("/api/say", post_say),
        web.get("/api/audio/{file}", get_audio),
        web.get("/api/config", get_config),
        web.post("/api/config", post_config),
        web.post("/api/clone/upload", post_clone_upload),
        web.post("/api/clone/prepare", post_clone_prepare),
        web.post("/api/clone/import", post_clone_import),
        web.post("/api/clone/save", post_clone_save),
        web.post("/api/clone/discard", post_clone_discard),
        web.get("/events", events),
        web.get("/", index),
    ])
    if STATIC.is_dir():
        app.router.add_static("/static", STATIC)
    return app


# ------------------------------------------------------------------ publishing


def _publish(app: web.Application, event: str, data) -> None:
    """Pushes to every open tab. A full queue means a frozen tab."""
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    for queue in list(app["subscribers"]):
        try:
            queue.put_nowait((event, text))
        except asyncio.QueueFull:
            pass


async def _build_state(app: web.Application) -> dict:
    engine = app["engine"]
    hidden = state.read_hidden(app["hidden"])
    base = {
        "engine": "on" if engine.running else "off",
        "discord": "off",
        "guild": None,
        "voice_channel": None,
        "queue": 0,
        "exit_code": engine.exit_code,
        "voices": state.list_voices(app["voices_dir"], hidden),
        # Recomputed on every publish, not cached at startup: creating the
        # .venv with the panel open is exactly what the person will do after
        # reading the warning, and a warning that doesn't go away is worse
        # than none.
        "environment": environment.check(),
    }
    if engine.running:
        try:
            base.update(
                {k: v for k, v in (await engine_client.status()).items() if k != "voices"}
            )
        except engine_client.EngineUnavailable:
            base["engine"] = "starting"
        except engine_client.EngineRefused as e:
            # The engine is up and complaining: that isn't "starting".
            base["error"] = e.error
    return base


async def _publish_state(app: web.Application) -> dict:
    current = await _build_state(app)
    _publish(app, "status", current)
    return current


async def _died(app: web.Application, code: int) -> None:
    """The engine went down on its own. Tell the screen and try once.

    Without this, the log showed the traceback but the status card kept
    saying "on" until the next click — and clicking Start on an engine the
    panel thought was on did nothing, because `post_engine` checks `running`.
    """
    _publish(app, "log", f"[panel] the engine died (code {code})")
    await _publish_state(app)

    if app["mutable"]["restarted"]:
        _publish(app, "log", "[panel] it had already gone down before; not insisting")
        return

    app["mutable"]["restarted"] = True
    _publish(app, "log", "[panel] restarting it once, automatically")
    try:
        await app["engine"].start()
    except Exception as e:  # noqa: BLE001 - a failed restart can't take the panel down
        _publish(app, "log", f"[panel] couldn't restart it: {e}")
    await _publish_state(app)


# ---------------------------------------------------------------------- routes


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(await _build_state(request.app))


async def post_engine(request: web.Request) -> web.Response:
    action = (await request.json()).get("action")
    engine = request.app["engine"]

    if action == "start":
        # Starting by hand re-arms the automatic restart: whoever fixed the
        # .env and clicked deserves the attempt back.
        request.app["mutable"]["restarted"] = False
        if not engine.running:
            await engine.start()
    elif action == "stop":
        await engine.stop()
    elif action == "restart":
        request.app["mutable"]["restarted"] = False
        await engine.stop()
        await engine.start()
    else:
        return web.json_response({"error": f"unknown action: {action}"}, status=400)

    return web.json_response(await _publish_state(request.app))


async def post_discord(request: web.Request) -> web.Response:
    action = (await request.json()).get("action")
    engine = request.app["engine"]

    if action == "connect" and not engine.running:
        await engine.start()
        if not await _wait_for_engine():
            return web.json_response({"error": "the engine didn't start"}, status=504)

    try:
        await engine_client.discord(action)
    except engine_client.EngineRefused as e:
        # Passes on what the engine said, with its status. Without this, a
        # rejected token reached the browser as complete silence: the light
        # stayed grey and no message showed up anywhere.
        return web.json_response({"error": e.error}, status=e.status)
    except engine_client.EngineUnavailable as e:
        return web.json_response({"error": f"the engine didn't answer: {e}"}, status=503)

    return web.json_response(await _publish_state(request.app))


async def _wait_for_engine(deadline: float = 180.0) -> bool:
    """Loading Pocket TTS takes seconds; the first time it may download weights."""
    limit = asyncio.get_running_loop().time() + deadline
    while asyncio.get_running_loop().time() < limit:
        if await engine_client.is_up():
            return True
        await asyncio.sleep(0.5)
    return False


async def post_voice(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    hide = bool((await request.json()).get("hidden"))

    hidden = state.read_hidden(request.app["hidden"])
    hidden.add(name) if hide else hidden.discard(name)
    state.write_hidden(request.app["hidden"], hidden)

    return web.json_response(await _publish_state(request.app))


async def events(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
    await resp.prepare(request)

    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    request.app["subscribers"].add(queue)
    try:
        # The accumulated log first: whoever opens the tab now sees what
        # already happened.
        for line in request.app["engine"].log:
            await _send(resp, "log", line)
        await _send(
            resp, "status", json.dumps(await _build_state(request.app), ensure_ascii=False)
        )

        while True:
            try:
                event, text = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")  # keeps the connection alive
                continue
            await _send(resp, event, text)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        request.app["subscribers"].discard(queue)
    return resp


async def _send(resp: web.StreamResponse, event: str, text: str) -> None:
    body = "".join(f"data: {line}\n" for line in text.splitlines() or [""])
    await resp.write(f"event: {event}\n{body}\n".encode("utf-8"))


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "index.html")


# ---------------------------------------------------------------- generate audio

# A file name the audio route is willing to serve. No dot, no slash and no
# `..`: the only way out of `outputs/` would be one of those, so the regex
# settles path traversal before any `resolve()`. The extension goes through
# the same sieve — `.env` and `.py` aren't audio.
AUDIO_NAME = re.compile(r"[A-Za-z0-9_\-]{1,120}\.(?:wav|ogg)")


async def post_say(request: web.Request) -> web.Response:
    """Generates a test clip. Requires the engine on; doesn't require Discord."""
    body = await request.json()
    voice_name = (body.get("voice") or "").strip()
    text = (body.get("text") or "").strip()

    if not voice_name:
        return web.json_response({"error": "pick a voice"}, status=400)
    if not text:
        return web.json_response({"error": "write the text to say"}, status=400)
    if not request.app["engine"].running:
        return web.json_response(
            {"error": "start the engine before generating audio"}, status=409
        )

    try:
        wav = await engine_client.say(text=text, voice=voice_name)
    except engine_client.EngineRefused as e:
        return web.json_response({"error": e.error}, status=e.status)
    except engine_client.EngineUnavailable as e:
        return web.json_response({"error": f"the engine didn't answer: {e}"}, status=503)

    base = f"{voice_name}_{voice_mod.slug(text)}"
    return web.json_response(await _save_output(request.app, base, wav))


async def _save_output(app: web.Application, base: str, wav: bytes) -> dict:
    """Writes the WAV to `outputs/` without overwriting anything and describes the result."""
    outputs = app["outputs"]
    await asyncio.to_thread(outputs.mkdir, parents=True, exist_ok=True)
    dest = voice_mod.free_path(outputs / f"{base}.wav")
    await asyncio.to_thread(dest.write_bytes, wav)
    return {
        "file": dest.name,
        "bytes": len(wav),
        "seconds": round(voice_mod.wav_duration(wav), 2),
    }


async def get_audio(request: web.Request) -> web.StreamResponse:
    """Serves audio from `outputs/`, converting to `.ogg` on demand.

    The engine always returns WAV. The conversion happens here, and not
    there, for two reasons: the engine doesn't need to know about delivery
    formats, and `ffmpeg` can't take up the single thread that generates audio.
    """
    name = request.match_info["file"]
    if not AUDIO_NAME.fullmatch(name):
        return web.json_response(
            {"error": f"invalid file name: {name}"}, status=400
        )

    dest = request.app["outputs"] / name

    if dest.suffix == ".ogg" and not dest.is_file():
        wav = dest.with_suffix(".wav")
        if not wav.is_file():
            return web.json_response({"error": f"not found: {wav.name}"}, status=404)
        if shutil.which("ffmpeg") is None:
            return web.json_response(
                {"error": "ffmpeg isn't on PATH — without it there's no .ogg"},
                status=503,
            )
        try:
            await asyncio.to_thread(voice_mod.wav_to_ogg, wav, dest)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=500)

    if not dest.is_file():
        return web.json_response({"error": f"not found: {name}"}, status=404)
    return web.FileResponse(dest)


# ---------------------------------------------------------------------- config


async def get_config(request: web.Request) -> web.Response:
    """Only the allow-listed keys. The tokens don't go through here.

    It isn't discretion, it's design: `state.read_config` walks the lines of
    the .env and ignores everything that isn't in `EDITABLE_KEYS`, so the
    DISCORD_TOKEN value never even enters the panel's memory where it could
    leak.
    """
    return web.json_response({
        "keys": list(state.EDITABLE_KEYS),
        "values": state.read_config(request.app["env"]),
        "descriptions": state.DESCRIPTIONS,
        "need_restart": sorted(state.NEED_RESTART),
    })


async def post_config(request: web.Request) -> web.Response:
    changes = (await request.json()).get("changes")
    if not isinstance(changes, dict) or not changes:
        return web.json_response({"error": "send an object with the changes"}, status=400)
    if not all(isinstance(v, str) for v in changes.values()):
        return web.json_response({"error": "the values must be text"}, status=400)

    try:
        await asyncio.to_thread(state.write_config, request.app["env"], changes)
    except ValueError as e:
        # A key outside the allow-list or a value with a line break.
        # `write_config` validates everything BEFORE writing a single byte, so
        # getting here means the file wasn't touched.
        return web.json_response({"error": str(e)}, status=400)
    except OSError as e:
        return web.json_response(
            {"error": f"couldn't write the .env: {e}"}, status=500
        )

    # The engine inherits the panel's environment, and its load_dotenv doesn't
    # override what's already there: without this, the new value would only
    # apply after restarting the panel.
    os.environ.update(changes)

    return web.json_response({
        "values": state.read_config(request.app["env"]),
        "restart": sorted(k for k in changes if k in state.NEED_RESTART),
    })


# ---------------------------------------------------------------------- cloning

# The extensions `preparation.py` can decode. It has the same list in
# `AUDIO_EXTS`, but importing it from here is impossible: it lives in the
# .venv's Python 3.11, with numpy and soundfile, and the supervisor runs on
# another Python without either. The duplication is the price of a light
# supervisor.
AUDIO_EXTENSIONS = frozenset({
    ".opus", ".ogg", ".oga", ".m4a", ".mp3", ".wav", ".flac", ".aac", ".webm", ".mp4",
})

# 500 MB. A two-hour call in Opus is ~60 MB; the cap exists so a wrong
# drag-and-drop doesn't fill the disk silently.
UPLOAD_LIMIT = 500 * 1024 * 1024

# A name inside the pending folder, with the same sieve as the audio route: no
# dot, no slash, no `..`.
PENDING_NAME = re.compile(r"[A-Za-z0-9_\-]{1,120}\.[A-Za-z0-9]{1,8}")


async def post_clone_upload(request: web.Request) -> web.Response:
    """Receives the audio and keeps it in the pending folder.

    Reads in chunks instead of `request.post()`: a half-gigabyte file held
    entirely in memory would take the panel down, and drag-and-dropping a call
    recording is exactly the big case.
    """
    # `request.multipart()` asserts on the content type, and an assert that
    # blows up becomes a 500. A body that isn't multipart is the caller's
    # mistake, not a panel defect.
    if not (request.content_type or "").startswith("multipart/"):
        return web.json_response(
            {"error": "send the file as multipart/form-data"}, status=400
        )

    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "audio":
        return web.json_response({"error": "send the file in the 'audio' field"}, status=400)

    # The name may come percent-encoded in the Content-Disposition. Without
    # undoing that, "My Sample.wav" became "my_20sample.wav" — the `%20` went
    # through the slug as if it were part of the name.
    source = Path(unquote(field.filename or "sample"))
    extension = source.suffix.lower()
    if extension not in AUDIO_EXTENSIONS:
        return web.json_response(
            {"error": f"unsupported extension: {extension or '(none)'}"}, status=400
        )

    pending = request.app["pending"]
    pending.mkdir(parents=True, exist_ok=True)
    dest = voice_mod.free_path(pending / f"{voice_mod.slug(source.stem)}{extension}")

    size = 0
    try:
        with dest.open("wb") as file:
            while chunk := await field.read_chunk():
                size += len(chunk)
                if size > UPLOAD_LIMIT:
                    raise ValueError("file too large")
                file.write(chunk)
    except ValueError as e:
        dest.unlink(missing_ok=True)
        return web.json_response({"error": str(e)}, status=413)

    return web.json_response({"file": dest.name, "bytes": size})


def _voice_name(raw: str | None) -> str | None:
    """A normalized voice name, or None if nothing usable is left.

    `voice.slug` falls back to the literal "audio" when the text has no usable
    character. That's a good default for a generated file name and a terrible
    default for a voice name: typing "!!!" would create a voice called "audio"
    without anyone being told. The check happens here first, the same way the
    slug does it inside, so it can answer 400 instead of inventing a name.
    """
    raw = (raw or "").strip()
    ascii_only = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    if not re.search(r"[A-Za-z0-9]", ascii_only):
        return None
    return voice_mod.slug(raw, limit=32)


def _in_pending(request: web.Request, name: str | None) -> Path | None:
    """Resolves a name inside the pending folder, or None if it's unacceptable."""
    if not name or not PENDING_NAME.fullmatch(name):
        return None
    return request.app["pending"] / name


async def post_clone_prepare(request: web.Request) -> web.Response:
    """Runs `preparation.py` on Python 3.11, with its output live on screen.

    Returns 200 even when the script fails: "the preparation didn't work" is a
    result, not an HTTP error, and the screen needs the diagnostic lines for
    the user to understand why.
    """
    body = await request.json()
    source = _in_pending(request, body.get("file"))
    if source is None:
        return web.json_response({"error": "invalid file name"}, status=400)
    if not source.is_file():
        return web.json_response({"error": f"not found: {source.name}"}, status=404)

    python = environment.venv_python()
    if not python.is_file():
        return web.json_response(
            {
                "error": f"cloning needs the Python 3.11 environment in .venv, and "
                f"{python} doesn't exist — see the warning at the top of the panel"
            },
            status=503,
        )

    command = request.app["mutable"]["prepare_command"](source, bool(body.get("trim")))
    prep = Process(command, cwd=ROOT, max_lines=300)
    prep.listen(lambda line: _publish(request.app, "prep", line))
    await prep.start()
    # 30 min: resemble-enhance without a GPU takes minutes per clip, and
    # DeepFilterNet still has to load. Waiting forever would leave the handler
    # stuck if the script hung.
    code = await prep.wait(deadline=1800)

    reference = source.with_name(f"{source.stem}_ref.wav")
    found = reference.is_file()
    return web.json_response({
        "code": code,
        "ok": code == 0 and found,
        "ref": reference.name if found else None,
        "output": prep.log,
    })


async def post_clone_import(request: web.Request) -> web.Response:
    """Creates the `.safetensors` in the pending folder and generates the test sentence.

    Both together on purpose: both need the engine, and splitting them would
    leave the user with an imported voice and no audio to judge it by.
    """
    body = await request.json()
    reference = _in_pending(request, body.get("ref"))
    name = _voice_name(body.get("name"))

    if reference is None:
        return web.json_response({"error": "invalid file name"}, status=400)
    if not name:
        return web.json_response({"error": "give the voice a name"}, status=400)
    if not reference.is_file():
        return web.json_response({"error": f"not found: {reference.name}"}, status=404)
    if not request.app["engine"].running:
        return web.json_response({"error": "start the engine before importing"}, status=409)

    pending = request.app["pending"]
    try:
        imported = await engine_client.import_voice(str(reference), name, str(pending))
        wav = await engine_client.say(
            text=voice_mod.sample_sentence(), path=imported["safetensors"]
        )
    except engine_client.EngineRefused as e:
        return web.json_response({"error": e.error}, status=e.status)
    except engine_client.EngineUnavailable as e:
        return web.json_response({"error": f"the engine didn't answer: {e}"}, status=503)

    output = await _save_output(request.app, f"{name}_test", wav)
    return web.json_response({
        "name": name,
        "safetensors": Path(imported["safetensors"]).name,
        "exists": (request.app["voices_dir"] / f"{name}.safetensors").is_file(),
        **output,
    })


async def post_clone_save(request: web.Request) -> web.Response:
    """Moves the approved voice from the pending folder into `voices/`.

    Overwriting requires `overwrite: true` — a second, conscious click. That's
    the difference from `voice.py clone`, which writes straight away and only
    says "overwriting" after the fact.
    """
    body = await request.json()
    name = _voice_name(body.get("name"))
    if not name:
        return web.json_response({"error": "invalid name"}, status=400)

    waiting = request.app["pending"] / f"{name}.safetensors"
    if not waiting.is_file():
        return web.json_response(
            {"error": f"{name}.safetensors isn't in the pending folder"}, status=404
        )

    dest = request.app["voices_dir"] / f"{name}.safetensors"
    if dest.exists() and not body.get("overwrite"):
        return web.json_response(
            {"error": f"a voice called {name} already exists", "exists": True}, status=409
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    # `shutil.move` and not `os.replace`: VOICES_DIR may point at another
    # disk, and then the atomic rename would fail with EXDEV.
    await asyncio.to_thread(shutil.move, str(waiting), str(dest))

    return web.json_response(await _publish_state(request.app))


async def post_clone_discard(request: web.Request) -> web.Response:
    """Throws the pending voice away. The audio and the `_ref.wav` stay.

    Idempotent: discarding twice, or discarding what never existed, isn't an
    error condition — it's the state the person asked for.
    """
    name = _voice_name((await request.json()).get("name"))
    if not name:
        return web.json_response({"error": "invalid name"}, status=400)

    (request.app["pending"] / f"{name}.safetensors").unlink(missing_ok=True)
    return web.json_response({"ok": True})


PERIODS = {"today": 1, "week": 7, "month": 30, "all": None}


async def get_history(request: web.Request) -> web.Response:
    """Unlike Discord's /status: no mandatory filter by server.

    There the filter exists so what was said on one Discord doesn't leak to
    another. On the owner's machine that doesn't apply, so it's optional.
    """
    period = request.query.get("period", "all")
    if period not in PERIODS:
        return web.json_response({"error": f"invalid period: {period}"}, status=400)

    days = PERIODS[period]
    since = (date.today() - timedelta(days=days - 1)).isoformat() if days else None
    guild_id = request.query.get("guild_id")

    records = await asyncio.to_thread(
        history.read, since, None, int(guild_id) if guild_id else None
    )
    s = history.stats(records)

    latest = [
        {
            "time": r.get("time"),
            "user": r.get("user"),
            "voice": r.get("voice"),
            "text": r.get("text"),
            "event": r.get("event"),
            "error": r.get("error"),
        }
        for r in reversed(records[-100:])
    ]

    return web.json_response({
        "total": s["total"],
        "events": s["events"],
        "chars": s.get("chars", 0),
        "avg_chars": round(s.get("avg_chars", 0), 1),
        "first_day": s.get("first_day"),
        "last_day": s.get("last_day"),
        "by_user": dict(s["by_user"]),
        "by_voice": dict(s["by_voice"]),
        "by_day": dict(s["by_day"]),
        "others": dict(s["others"]),
        "latest": latest,
    })
