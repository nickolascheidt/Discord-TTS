import asyncio
import io
import os
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import voice as voice_mod
from panel import engine_client, server, state

# One 16-bit sample. What matters in the tests is the size, not the content:
# at 48 kHz stereo, `SILENCE * 48000` is exactly half a second of audio.
SILENCE = bytes((0, 1))

ORIGIN = {"Origin": "http://127.0.0.1:8080"}


class FakeProcess:
    """A double of panel.process.Process, with no subprocess at all."""

    def __init__(self):
        self.running = False
        self.log = ["engine starting", "Engine ready"]
        self.exit_code = None
        self.started = 0
        self.stopped = 0
        self.death_callback = None

    async def start(self):
        self.started += 1
        self.running = True

    async def stop(self):
        self.stopped += 1
        self.running = False

    def listen(self, callback):
        pass

    def on_death(self, callback):
        self.death_callback = callback


class _SpyQueue:
    """An SSE subscriber that only keeps what was published."""

    def __init__(self, dest: list) -> None:
        self._dest = dest

    def put_nowait(self, item) -> None:
        self._dest.append(item)


@pytest.fixture(autouse=True)
def venv_present(monkeypatch):
    """The prepare route checks the .venv before running anything.

    The tests swap the command for a two-line script, so the check needs a
    Python that exists — otherwise they'd only pass on a machine that has the
    project's .venv set up.
    """
    monkeypatch.setattr(server.environment, "venv_python", lambda: Path(sys.executable))


def _create_app(tmp_path):
    (tmp_path / "voices").mkdir(exist_ok=True)
    return server.create_app(
        engine=FakeProcess(),
        voices_dir=tmp_path / "voices",
        hidden=tmp_path / "hidden.json",
        outputs=tmp_path / "outputs",
        pending=tmp_path / "pending",
        env=tmp_path / ".env",
    )


@pytest.fixture
async def client(tmp_path):
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices" / "bob.safetensors").write_bytes(b"x" * 10)
    (tmp_path / "voices" / "alice.safetensors").write_bytes(b"y" * 20)
    (tmp_path / ".env").write_text(
        "# comment\nDISCORD_TOKEN=do_not_touch\nMAX_TEXT_LEN=400\n", encoding="utf-8"
    )

    app = _create_app(tmp_path)
    # Saving the config updates os.environ; without restoring it, the value
    # leaks into the other tests in the process.
    env = dict(os.environ)
    try:
        async with TestClient(TestServer(app)) as c:
            yield c
    finally:
        os.environ.clear()
        os.environ.update(env)


async def test_state_with_the_engine_off(client):
    resp = await client.get("/api/state")
    assert resp.status == 200
    data = await resp.json()
    assert data["engine"] == "off"
    assert data["discord"] == "off"


async def test_state_lists_the_voices_on_disk(client):
    data = await (await client.get("/api/state")).json()
    assert [v["name"] for v in data["voices"]] == ["alice", "bob"]


async def test_state_carries_the_environment_check(client):
    """The screen warns ahead, instead of failing halfway through a clone."""
    data = await (await client.get("/api/state")).json()

    assert {c["what"] for c in data["environment"]} == {"ffmpeg", ".venv python"}
    assert all("ok" in c and "detail" in c for c in data["environment"])


async def test_start_the_engine(client):
    resp = await client.post("/api/engine", json={"action": "start"}, headers=ORIGIN)
    assert resp.status == 200
    assert client.app["engine"].started == 1


async def test_hide_a_voice(client, tmp_path):
    resp = await client.post("/api/voices/bob", json={"hidden": True}, headers=ORIGIN)
    assert resp.status == 200
    assert state.read_hidden(tmp_path / "hidden.json") == {"bob"}

    data = await (await client.get("/api/state")).json()
    assert [v["hidden"] for v in data["voices"]] == [False, True]


async def test_a_write_without_the_right_origin_is_refused(client):
    """Any page in the browser can't shut your bot down."""
    resp = await client.post(
        "/api/engine", json={"action": "start"}, headers={"Origin": "https://some-site.com"}
    )
    assert resp.status == 403
    assert client.app["engine"].started == 0


async def test_reading_does_not_require_origin(client):
    resp = await client.get("/api/state", headers={"Origin": "https://some-site.com"})
    assert resp.status == 200


async def test_sse_sends_the_accumulated_log_right_away(client):
    resp = await client.get("/events")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")

    chunk = (await resp.content.read(400)).decode("utf-8")
    assert "event: log" in chunk
    assert "Engine ready" in chunk
    resp.close()


async def test_history_route(client, tmp_path, monkeypatch):
    jsonl = tmp_path / "history.jsonl"
    jsonl.write_text(
        '{"time":"2026-09-16T10:00:00-03:00","event":"say",'
        '"user":"alice","voice":"bob","chars":10}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HISTORY_FILE", str(jsonl))

    data = await (await client.get("/api/history?period=all")).json()
    assert data["total"] == 1
    assert data["by_voice"] == {"bob": 1}
    assert data["latest"][0]["voice"] == "bob"


async def test_history_with_an_invalid_period_is_400(client):
    assert (await client.get("/api/history?period=forever")).status == 400


# ----------------------------------------------------------- engine errors


async def test_an_engine_error_reaches_the_browser(client, monkeypatch):
    """Regression: the panel threw away the engine's answer and replied 200.

    The real scenario: the user changes DISCORD_TOKEN and gets it wrong.
    Clicks Connect, the light stays grey, and no message shows up anywhere —
    the engine had written "Discord rejected the token", and the panel threw
    it away.
    """
    async def refuse(action):
        raise engine_client.EngineRefused(401, "Discord rejected the token: Improper token")

    monkeypatch.setattr(engine_client, "discord", refuse)
    client.app["engine"].running = True

    resp = await client.post("/api/discord", json={"action": "connect"}, headers=ORIGIN)

    assert resp.status == 401
    assert "rejected the token" in (await resp.json())["error"]


async def test_engine_down_is_503_not_401(client, monkeypatch):
    """Engine off and engine disagreeing are opposite diagnoses."""
    async def gone(action):
        raise engine_client.EngineUnavailable("Cannot connect to host")

    monkeypatch.setattr(engine_client, "discord", gone)
    client.app["engine"].running = True

    resp = await client.post("/api/discord", json={"action": "connect"}, headers=ORIGIN)

    assert resp.status == 503
    assert "didn't answer" in (await resp.json())["error"]


async def _fake_engine(monkeypatch, handler, route: str = "/discord"):
    """Starts a server in place of the engine and points the client at it."""
    app = web.Application()
    app.router.add_post(route, handler)
    test_server = TestServer(app)
    await test_server.start_server()
    monkeypatch.setattr(engine_client, "BASE", str(test_server.make_url("")).rstrip("/"))
    return test_server


async def test_request_turns_a_json_error_into_EngineRefused(monkeypatch):
    async def refuse(request):
        return web.json_response({"error": "Discord rejected the token: xyz"}, status=401)

    s = await _fake_engine(monkeypatch, refuse)
    try:
        with pytest.raises(engine_client.EngineRefused) as caught:
            await engine_client.discord("connect")
        assert caught.value.status == 401
        assert "rejected the token" in caught.value.error
    finally:
        await s.close()


async def test_request_treats_a_broken_engine_as_refusal_not_absence(monkeypatch):
    """An aiohttp 500 returns HTML; untreated, it would become "engine off"."""
    async def explode(request):
        raise web.HTTPInternalServerError(text="500: Internal Server Error")

    s = await _fake_engine(monkeypatch, explode)
    try:
        with pytest.raises(engine_client.EngineRefused) as caught:
            await engine_client.discord("connect")
        assert caught.value.status == 500
        assert "broke" in caught.value.error
    finally:
        await s.close()


async def test_say_returns_the_raw_bytes(monkeypatch):
    """WAV isn't JSON: the normal _request would choke on ContentTypeError."""
    async def generate(request):
        body = await request.json()
        assert body == {"voice": "bob", "text": "hi", "path": None}
        return web.Response(body=b"RIFF....WAVEdata", content_type="audio/wav")

    s = await _fake_engine(monkeypatch, generate, route="/say")
    try:
        data = await engine_client.say(voice="bob", text="hi")
        assert data == b"RIFF....WAVEdata"
    finally:
        await s.close()


async def test_say_turns_an_engine_error_into_EngineRefused(monkeypatch):
    async def refuse(request):
        return web.json_response({"error": "unknown voice: 'x'"}, status=404)

    s = await _fake_engine(monkeypatch, refuse, route="/say")
    try:
        with pytest.raises(engine_client.EngineRefused) as caught:
            await engine_client.say(voice="x", text="hi")
        assert caught.value.status == 404
        assert "unknown voice" in caught.value.error
    finally:
        await s.close()


async def test_say_with_a_broken_engine_is_not_unavailable(monkeypatch):
    """An HTML 500 is "the engine disagrees", not "the engine is gone"."""
    async def explode(request):
        raise web.HTTPInternalServerError(text="500: Internal Server Error")

    s = await _fake_engine(monkeypatch, explode, route="/say")
    try:
        with pytest.raises(engine_client.EngineRefused) as caught:
            await engine_client.say(voice="x", text="hi")
        assert caught.value.status == 500
        assert "broke" in caught.value.error
    finally:
        await s.close()


async def test_say_with_the_engine_down_is_EngineUnavailable(monkeypatch):
    """No server on the port at all: the short sock_connect answers fast.

    The port is obtained by starting a server and taking it down right after,
    not by pointing at the real 8081: whoever runs the tests with the panel
    open has a LIVE engine there, and the test would end up measuring its
    answer.
    """
    async def never_called(request):
        raise AssertionError("the server should be closed")

    dead = await _fake_engine(monkeypatch, never_called, route="/say")
    await dead.close()

    with pytest.raises(engine_client.EngineUnavailable):
        await engine_client.say(voice="x", text="hi")


async def test_import_sends_file_name_and_dest(monkeypatch):
    async def import_voice(request):
        body = await request.json()
        assert body == {"file": "a_ref.wav", "name": "ann", "dest": "pending"}
        return web.json_response({"safetensors": "pending/ann.safetensors", "bytes": 9})

    s = await _fake_engine(monkeypatch, import_voice, route="/import")
    try:
        data = await engine_client.import_voice("a_ref.wav", "ann", "pending")
        assert data["safetensors"] == "pending/ann.safetensors"
    finally:
        await s.close()


# ------------------------------------------------------------- generate audio


async def test_say_writes_the_wav_to_outputs(client, tmp_path, monkeypatch):
    async def generate(text, voice=None, path=None):
        return voice_mod.pcm_to_wav(SILENCE * 48000)  # 0.5s of audio

    monkeypatch.setattr(engine_client, "say", generate)
    client.app["engine"].running = True

    resp = await client.post(
        "/api/say", json={"voice": "bob", "text": "Hello world"}, headers=ORIGIN
    )

    assert resp.status == 200, await resp.text()
    data = await resp.json()
    assert data["file"] == "bob_hello_world.wav"
    assert data["seconds"] == 0.5
    assert (tmp_path / "outputs" / "bob_hello_world.wav").is_file()


async def test_say_does_not_overwrite_a_previous_test(client, tmp_path, monkeypatch):
    """Two tests with the same text become _2, like in voice.py."""
    async def generate(text, voice=None, path=None):
        return voice_mod.pcm_to_wav(SILENCE * 100)

    monkeypatch.setattr(engine_client, "say", generate)
    client.app["engine"].running = True

    await client.post("/api/say", json={"voice": "bob", "text": "hi"}, headers=ORIGIN)
    resp = await client.post("/api/say", json={"voice": "bob", "text": "hi"}, headers=ORIGIN)

    assert (await resp.json())["file"] == "bob_hi_2.wav"


async def test_say_with_the_engine_off_is_409(client):
    """Needs the engine on; doesn't need Discord. The error has to say so."""
    resp = await client.post("/api/say", json={"voice": "bob", "text": "hi"}, headers=ORIGIN)

    assert resp.status == 409
    assert "engine" in (await resp.json())["error"]


async def test_say_without_text_is_400(client):
    client.app["engine"].running = True

    resp = await client.post("/api/say", json={"voice": "bob", "text": "  "}, headers=ORIGIN)

    assert resp.status == 400


async def test_say_passes_on_the_engine_refusal(client, monkeypatch):
    async def refuse(text, voice=None, path=None):
        raise engine_client.EngineRefused(404, "unknown voice: 'x'")

    monkeypatch.setattr(engine_client, "say", refuse)
    client.app["engine"].running = True

    resp = await client.post("/api/say", json={"voice": "x", "text": "hi"}, headers=ORIGIN)

    assert resp.status == 404
    assert "unknown voice" in (await resp.json())["error"]


async def test_say_with_an_engine_that_vanished_is_503(client, monkeypatch):
    async def gone(text, voice=None, path=None):
        raise engine_client.EngineUnavailable("Cannot connect to host")

    monkeypatch.setattr(engine_client, "say", gone)
    client.app["engine"].running = True

    resp = await client.post("/api/say", json={"voice": "x", "text": "hi"}, headers=ORIGIN)

    assert resp.status == 503


# ----------------------------------------------------------------- serving files


async def test_audio_serves_the_saved_wav(client, tmp_path):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "bob_hi.wav").write_bytes(voice_mod.pcm_to_wav(SILENCE * 10))

    resp = await client.get("/api/audio/bob_hi.wav")

    assert resp.status == 200
    assert (await resp.read())[:4] == b"RIFF"


async def test_audio_converts_to_ogg_on_demand(client, tmp_path, monkeypatch):
    """The engine always returns WAV; the .ogg is the supervisor's job."""
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "bob_hi.wav").write_bytes(b"fake RIFF")

    calls = []

    def convert(wav, ogg):
        calls.append((Path(wav).name, Path(ogg).name))
        Path(ogg).write_bytes(b"fake OggS")

    monkeypatch.setattr(voice_mod, "wav_to_ogg", convert)
    monkeypatch.setattr(server.shutil, "which", lambda _: "ffmpeg")

    resp = await client.get("/api/audio/bob_hi.ogg")

    assert resp.status == 200
    assert calls == [("bob_hi.wav", "bob_hi.ogg")]
    assert (tmp_path / "outputs" / "bob_hi.ogg").is_file()


async def test_audio_does_not_reconvert_an_existing_ogg(client, tmp_path, monkeypatch):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "x.ogg").write_bytes(b"OggS")

    def never(wav, ogg):
        raise AssertionError("shouldn't convert again")

    monkeypatch.setattr(voice_mod, "wav_to_ogg", never)

    resp = await client.get("/api/audio/x.ogg")

    assert resp.status == 200


async def test_audio_without_ffmpeg_is_503_not_500(client, tmp_path, monkeypatch):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "x.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(server.shutil, "which", lambda _: None)

    resp = await client.get("/api/audio/x.ogg")

    assert resp.status == 503
    assert "ffmpeg" in (await resp.json())["error"]


async def test_audio_that_does_not_exist_is_404(client, tmp_path):
    (tmp_path / "outputs").mkdir()

    resp = await client.get("/api/audio/ghost.wav")

    assert resp.status == 404


async def test_audio_refuses_a_name_with_path_traversal(client, tmp_path):
    """Without this, /api/audio/..%2F..%2F.env would hand out the DISCORD_TOKEN."""
    for name in ("..", "..%2F.env", "a%2Fb.wav", "x.py", ".env"):
        resp = await client.get(f"/api/audio/{name}")
        assert resp.status in (400, 404), f"{name} got through with {resp.status}"


async def test_audio_refuses_an_extension_that_is_not_audio(client, tmp_path):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "secret.txt").write_text("no", encoding="utf-8")

    resp = await client.get("/api/audio/secret.txt")

    assert resp.status == 400


# ---------------------------------------------------------------------- config


async def test_config_lists_values_and_keys(client):
    data = await (await client.get("/api/config")).json()

    assert data["values"] == {"MAX_TEXT_LEN": "400"}
    assert data["keys"] == list(state.EDITABLE_KEYS)
    assert "MAX_TEXT_LEN" in data["need_restart"]


async def test_config_never_returns_the_token(client):
    """The allow-list is the defense: the code doesn't know the token exists."""
    body = await (await client.get("/api/config")).text()

    assert "DISCORD_TOKEN" not in body
    assert "do_not_touch" not in body


async def test_saving_config_changes_only_the_requested_key(client, tmp_path):
    resp = await client.post(
        "/api/config", json={"changes": {"MAX_TEXT_LEN": "250"}}, headers=ORIGIN
    )

    assert resp.status == 200, await resp.text()
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MAX_TEXT_LEN=250" in text
    assert "DISCORD_TOKEN=do_not_touch" in text  # byte for byte
    assert "# comment" in text


async def test_saving_config_says_what_needs_a_restart(client):
    resp = await client.post(
        "/api/config", json={"changes": {"MAX_TEXT_LEN": "250"}}, headers=ORIGIN
    )

    data = await resp.json()
    assert data["restart"] == ["MAX_TEXT_LEN"]
    assert data["values"]["MAX_TEXT_LEN"] == "250"


async def test_saving_config_leaves_a_bak(client, tmp_path):
    await client.post(
        "/api/config", json={"changes": {"MAX_TEXT_LEN": "250"}}, headers=ORIGIN
    )

    saved = (tmp_path / ".env.bak").read_text(encoding="utf-8")
    assert saved.count("MAX_TEXT_LEN=400") == 1


async def test_saving_a_key_outside_the_allowlist_is_400(client, tmp_path):
    """Regression: a POST with DISCORD_TOKEN can't become a 500 or write anything."""
    before = (tmp_path / ".env").read_bytes()

    resp = await client.post(
        "/api/config", json={"changes": {"DISCORD_TOKEN": "stolen"}}, headers=ORIGIN
    )

    assert resp.status == 400
    assert "not editable" in (await resp.json())["error"]
    assert (tmp_path / ".env").read_bytes() == before


async def test_saving_config_applies_to_the_next_engine(client, monkeypatch):
    # It happened: the engine inherits the panel's environment, and its
    # load_dotenv doesn't override what's already there. Saving
    # IDLE_TIMEOUT=99 and restarting the engine left the bot with the value
    # from when the panel opened.
    monkeypatch.setenv("IDLE_TIMEOUT", "45")
    await client.post("/api/config", json={"changes": {"IDLE_TIMEOUT": "99"}},
                      headers=ORIGIN)
    assert os.environ["IDLE_TIMEOUT"] == "99"


async def test_saving_an_invalid_number_is_400(client, tmp_path):
    before = (tmp_path / ".env").read_bytes()
    resp = await client.post("/api/config", json={"changes": {"IDLE_TIMEOUT": "three"}},
                             headers=ORIGIN)
    assert resp.status == 400
    assert (tmp_path / ".env").read_bytes() == before


async def test_saving_config_with_a_wrong_body_is_400(client):
    resp = await client.post("/api/config", json={"changes": "MAX=1"}, headers=ORIGIN)

    assert resp.status == 400


async def test_saving_config_with_no_change_is_400(client):
    resp = await client.post("/api/config", json={"changes": {}}, headers=ORIGIN)

    assert resp.status == 400


# ------------------------------------------------------------------------ clone

FAKE_PREP = (
    "import sys\n"
    "from pathlib import Path\n"
    "source = Path(sys.argv[1])\n"
    "print('[ref] 18.3s | peak -3.2 dBFS | clip 0.00%')\n"
    "source.with_name(source.stem + '_ref.wav').write_bytes(b'RIFF')\n"
)

FAILING_PREP = "import sys\nprint('ERROR: unreadable sample')\nsys.exit(2)\n"


def use_prep(app, script: str) -> None:
    """Swaps preparation.py for a short script, with no torch or DeepFilterNet."""
    app["mutable"]["prepare_command"] = (
        lambda source, trim: [sys.executable, "-c", script, str(source)]
    )


async def upload_audio(client, name: str = "sample.wav", data: bytes = b"RIFFxxxx"):
    form = aiohttp.FormData()
    form.add_field("audio", io.BytesIO(data), filename=name, content_type="audio/wav")
    return await client.post("/api/clone/upload", data=form, headers=ORIGIN)


async def test_upload_keeps_the_audio_in_the_pending_folder(client, tmp_path):
    resp = await upload_audio(client, "My Sample.WAV", b"RIFF" * 10)

    assert resp.status == 200, await resp.text()
    data = await resp.json()
    assert data["file"] == "my_sample.wav"
    assert data["bytes"] == 40
    assert (tmp_path / "pending" / "my_sample.wav").read_bytes() == b"RIFF" * 10


async def test_upload_does_not_overwrite_a_previous_upload(client, tmp_path):
    await upload_audio(client, "a.wav")
    resp = await upload_audio(client, "a.wav")

    assert (await resp.json())["file"] == "a_2.wav"


async def test_upload_refuses_an_extension_that_is_not_audio(client, tmp_path):
    resp = await upload_audio(client, "virus.exe")

    assert resp.status == 400
    assert "extension" in (await resp.json())["error"]
    pending = tmp_path / "pending"
    assert not pending.exists() or not list(pending.iterdir())


async def test_upload_with_a_body_that_is_not_multipart_is_400(client):
    """`request.multipart()` asserts on the content type, and an assert becomes 500."""
    form = aiohttp.FormData()
    form.add_field("other", "x")  # with no file, aiohttp sends urlencoded

    resp = await client.post("/api/clone/upload", data=form, headers=ORIGIN)

    assert resp.status == 400
    assert "multipart" in (await resp.json())["error"]


async def test_upload_in_the_wrong_field_is_400(client):
    form = aiohttp.FormData()
    form.add_field("attachment", io.BytesIO(b"RIFF"), filename="a.wav")

    resp = await client.post("/api/clone/upload", data=form, headers=ORIGIN)

    assert resp.status == 400
    assert "audio" in (await resp.json())["error"]


async def test_prepare_runs_the_script_and_finds_the_ref(client, tmp_path):
    await upload_audio(client, "ann.wav")
    use_prep(client.app, FAKE_PREP)

    resp = await client.post("/api/clone/prepare", json={"file": "ann.wav"}, headers=ORIGIN)

    assert resp.status == 200, await resp.text()
    data = await resp.json()
    assert data["ok"] is True
    assert data["code"] == 0
    assert data["ref"] == "ann_ref.wav"
    assert any("[ref] 18.3s" in line for line in data["output"])
    assert (tmp_path / "pending" / "ann_ref.wav").is_file()


async def test_prepare_publishes_the_output_live(client, tmp_path):
    """The diagnosis is only useful if it shows up while running, not at the end."""
    await upload_audio(client, "ann.wav")
    use_prep(client.app, FAKE_PREP)
    published: list[tuple[str, str]] = []
    client.app["subscribers"].add(_SpyQueue(published))

    await client.post("/api/clone/prepare", json={"file": "ann.wav"}, headers=ORIGIN)

    assert any(event == "prep" and "[ref] 18.3s" in text for event, text in published)


async def test_a_failing_prepare_returns_ok_false_with_the_output(client):
    await upload_audio(client, "ann.wav")
    use_prep(client.app, FAILING_PREP)

    resp = await client.post("/api/clone/prepare", json={"file": "ann.wav"}, headers=ORIGIN)

    assert resp.status == 200  # the process failed, the route didn't
    data = await resp.json()
    assert data["ok"] is False
    assert data["code"] == 2
    assert data["ref"] is None
    assert any("unreadable sample" in line for line in data["output"])


async def test_prepare_a_file_that_does_not_exist_is_404(client):
    use_prep(client.app, FAKE_PREP)

    resp = await client.post("/api/clone/prepare", json={"file": "ghost.wav"}, headers=ORIGIN)

    assert resp.status == 404


async def test_prepare_refuses_a_name_with_traversal(client):
    use_prep(client.app, FAKE_PREP)

    resp = await client.post("/api/clone/prepare", json={"file": "../../.env"}, headers=ORIGIN)

    assert resp.status == 400


async def test_prepare_without_the_venv_is_503(client, tmp_path, monkeypatch):
    """Warns ahead, instead of blowing up with FileNotFoundError in create_subprocess."""
    await upload_audio(client, "ann.wav")
    monkeypatch.setattr(server.environment, "venv_python", lambda: tmp_path / "missing")

    resp = await client.post("/api/clone/prepare", json={"file": "ann.wav"}, headers=ORIGIN)

    assert resp.status == 503
    assert ".venv" in (await resp.json())["error"]


async def test_prepare_passes_trim_when_asked(client):
    await upload_audio(client, "ann.wav")
    seen: list[list[str]] = []

    def command(source, trim):
        cmd = [sys.executable, "-c", FAKE_PREP, str(source)] + (["--trim"] if trim else [])
        seen.append(cmd)
        return cmd

    client.app["mutable"]["prepare_command"] = command

    await client.post(
        "/api/clone/prepare", json={"file": "ann.wav", "trim": True}, headers=ORIGIN
    )

    assert seen[0][-1] == "--trim"


async def _prepared(client, name: str = "ann.wav"):
    """Leaves the pending folder with an audio file and its _ref.wav."""
    await upload_audio(client, name)
    use_prep(client.app, FAKE_PREP)
    resp = await client.post("/api/clone/prepare", json={"file": name}, headers=ORIGIN)
    return (await resp.json())["ref"]


def engine_that_imports(monkeypatch):
    """A double of both engine calls, recording what was asked.

    `import_voice` really writes to the `dest` the route sends: that's how the
    test confirms the voice is born in the pending folder and not in `voices/`.
    """
    requests: dict = {}

    async def import_voice(file, name, dest=None):
        requests["import"] = (Path(file).name, name, dest)
        path = Path(dest) / f"{name}.safetensors"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake safetensors")
        return {"safetensors": str(path), "bytes": path.stat().st_size}

    async def say(text, voice=None, path=None):
        requests["say"] = (text, voice, path)
        return voice_mod.pcm_to_wav(SILENCE * 4800)

    monkeypatch.setattr(engine_client, "import_voice", import_voice)
    monkeypatch.setattr(engine_client, "say", say)
    return requests


async def test_import_creates_the_voice_in_pending_and_generates_the_test(client, tmp_path, monkeypatch):
    ref = await _prepared(client)
    requests = engine_that_imports(monkeypatch)
    client.app["engine"].running = True

    resp = await client.post(
        "/api/clone/import", json={"ref": ref, "name": "Ann Marie"}, headers=ORIGIN
    )

    assert resp.status == 200, await resp.text()
    data = await resp.json()
    assert data["name"] == "ann_marie"
    assert data["file"] == "ann_marie_test.wav"
    assert data["exists"] is False
    # The voice is born in pending, NOT in voices/: Discord can't see it yet.
    assert (tmp_path / "pending" / "ann_marie.safetensors").is_file()
    assert not (tmp_path / "voices" / "ann_marie.safetensors").exists()
    assert requests["import"] == ("ann_ref.wav", "ann_marie", str(tmp_path / "pending"))
    assert requests["say"] == (
        voice_mod.sample_sentence(),
        None,
        str(tmp_path / "pending" / "ann_marie.safetensors"),
    )


async def test_import_warns_that_the_name_already_exists(client, tmp_path, monkeypatch):
    """The screen needs to know BEFORE the user clicks Save."""
    ref = await _prepared(client)
    engine_that_imports(monkeypatch)
    client.app["engine"].running = True

    resp = await client.post("/api/clone/import", json={"ref": ref, "name": "bob"}, headers=ORIGIN)

    assert (await resp.json())["exists"] is True


async def test_import_with_the_engine_off_is_409(client):
    ref = await _prepared(client)

    resp = await client.post("/api/clone/import", json={"ref": ref, "name": "ann"}, headers=ORIGIN)

    assert resp.status == 409


async def test_import_without_a_name_is_400(client, tmp_path, monkeypatch):
    ref = await _prepared(client)
    engine_that_imports(monkeypatch)
    client.app["engine"].running = True

    resp = await client.post("/api/clone/import", json={"ref": ref, "name": "!!!"}, headers=ORIGIN)

    assert resp.status == 400


async def test_import_a_ref_that_does_not_exist_is_404(client, monkeypatch, tmp_path):
    engine_that_imports(monkeypatch)
    client.app["engine"].running = True

    resp = await client.post(
        "/api/clone/import", json={"ref": "ghost_ref.wav", "name": "ann"}, headers=ORIGIN,
    )

    assert resp.status == 404


async def test_import_passes_on_the_engine_refusal(client, tmp_path, monkeypatch):
    ref = await _prepared(client)

    async def refuse(file, name, dest=None):
        raise engine_client.EngineRefused(500, "import failed: sample too short")

    monkeypatch.setattr(engine_client, "import_voice", refuse)
    client.app["engine"].running = True

    resp = await client.post("/api/clone/import", json={"ref": ref, "name": "ann"}, headers=ORIGIN)

    assert resp.status == 500
    assert "too short" in (await resp.json())["error"]


# --------------------------------------------------------------- save/discard


async def test_save_moves_the_voice_to_the_voices_folder(client, tmp_path):
    (tmp_path / "pending").mkdir(exist_ok=True)
    (tmp_path / "pending" / "ann.safetensors").write_bytes(b"content")

    resp = await client.post("/api/clone/save", json={"name": "ann"}, headers=ORIGIN)

    assert resp.status == 200, await resp.text()
    assert (tmp_path / "voices" / "ann.safetensors").read_bytes() == b"content"
    assert not (tmp_path / "pending" / "ann.safetensors").exists()
    # The answer is the new state: the Voices tab already shows the new voice.
    assert "ann" in [v["name"] for v in (await resp.json())["voices"]]


async def test_save_does_not_overwrite_without_permission(client, tmp_path):
    """The point of the pending folder: a bad attempt doesn't destroy what works."""
    (tmp_path / "pending").mkdir(exist_ok=True)
    (tmp_path / "pending" / "bob.safetensors").write_bytes(b"new")

    resp = await client.post("/api/clone/save", json={"name": "bob"}, headers=ORIGIN)

    assert resp.status == 409
    assert (await resp.json())["exists"] is True
    assert (tmp_path / "voices" / "bob.safetensors").read_bytes() == b"x" * 10


async def test_save_overwrites_when_the_user_confirms(client, tmp_path):
    (tmp_path / "pending").mkdir(exist_ok=True)
    (tmp_path / "pending" / "bob.safetensors").write_bytes(b"new")

    resp = await client.post(
        "/api/clone/save", json={"name": "bob", "overwrite": True}, headers=ORIGIN
    )

    assert resp.status == 200
    assert (tmp_path / "voices" / "bob.safetensors").read_bytes() == b"new"


async def test_save_a_voice_that_is_not_pending_is_404(client):
    resp = await client.post("/api/clone/save", json={"name": "ghost"}, headers=ORIGIN)

    assert resp.status == 404


async def test_discard_deletes_only_the_pending_safetensors(client, tmp_path):
    """The audio and the _ref.wav stay: you can try again with another name."""
    (tmp_path / "pending").mkdir(exist_ok=True)
    (tmp_path / "pending" / "ann.safetensors").write_bytes(b"x")
    (tmp_path / "pending" / "ann_ref.wav").write_bytes(b"RIFF")

    resp = await client.post("/api/clone/discard", json={"name": "ann"}, headers=ORIGIN)

    assert resp.status == 200
    assert not (tmp_path / "pending" / "ann.safetensors").exists()
    assert (tmp_path / "pending" / "ann_ref.wav").is_file()


async def test_discarding_what_does_not_exist_does_not_blow_up(client):
    resp = await client.post("/api/clone/discard", json={"name": "ghost"}, headers=ORIGIN)

    assert resp.status == 200


# ---------------------------------------------------- the engine dies by itself


async def until(condition, deadline: float = 5.0) -> bool:
    """Waits for a condition to become true, yielding to the loop between checks.

    An `await asyncio.sleep(0)` wouldn't do: the death notice publishes the
    state, and building the state with the engine marked as running tries a
    GET on the real engine — which takes up to the client's `sock_connect` to
    be refused. Counting loop iterations would be counting what you don't
    control.
    """
    loop = asyncio.get_running_loop()
    limit = loop.time() + deadline
    while loop.time() < limit:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_engine_death_publishes_the_new_state(client):
    """The card has to change by itself, not wait for the next click."""
    published: list[tuple[str, str]] = []
    client.app["subscribers"].add(_SpyQueue(published))
    client.app["engine"].running = False

    client.app["engine"].death_callback(3)

    assert await until(lambda: any(e == "status" for e, _ in published))
    assert any("code 3" in text for e, text in published if e == "log")


async def test_engine_death_restarts_it_once(client):
    client.app["engine"].running = False

    client.app["engine"].death_callback(3)

    assert await until(lambda: client.app["engine"].started == 1)


async def test_a_second_death_does_not_restart(client):
    """A wrong token isn't fixed by restarting; insisting only floods the log."""
    client.app["engine"].running = False
    client.app["engine"].death_callback(3)
    assert await until(lambda: client.app["engine"].started == 1)

    published: list[tuple[str, str]] = []
    client.app["subscribers"].add(_SpyQueue(published))
    client.app["engine"].running = False
    client.app["engine"].death_callback(3)

    # Waits for proof that it GAVE UP, instead of waiting an arbitrary time and
    # concluding from the silence that nothing happened.
    assert await until(
        lambda: any("not insisting" in text for e, text in published if e == "log")
    )
    assert client.app["engine"].started == 1


async def test_starting_by_hand_rearms_the_automatic_restart(client):
    """Whoever fixed the .env and clicked Start deserves the attempt back."""
    client.app["engine"].running = False
    client.app["engine"].death_callback(3)
    assert await until(lambda: client.app["engine"].started == 1)

    await client.post("/api/engine", json={"action": "stop"}, headers=ORIGIN)
    await client.post("/api/engine", json={"action": "start"}, headers=ORIGIN)
    assert client.app["engine"].started == 2

    client.app["engine"].running = False
    client.app["engine"].death_callback(3)

    assert await until(lambda: client.app["engine"].started == 3)


async def test_unknown_engine_action_is_400(client):
    resp = await client.post("/api/engine", json={"action": "explode"}, headers=ORIGIN)
    assert resp.status == 400


# --------------------------------------------------------------------- UI cache


async def test_html_and_js_ask_for_revalidation(client):
    """Regression: the new panel running and the browser drawing the old screen.

    Without `Cache-Control`, the browser makes up an expiry from
    `Last-Modified` and serves what it has without asking.
    """
    for route in ("/", "/static/app.js", "/static/style.css"):
        resp = await client.get(route)
        assert resp.status == 200, route
        assert resp.headers.get("Cache-Control") == "no-cache", route


async def test_audio_stays_cacheable(client, tmp_path):
    """Audio is never rewritten: `free_path` creates a new name."""
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "x.wav").write_bytes(voice_mod.pcm_to_wav(SILENCE * 10))

    resp = await client.get("/api/audio/x.wav")

    assert resp.status == 200
    assert resp.headers.get("Cache-Control") != "no-cache"


