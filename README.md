# Discord-TTS

**English** · [Português](README.pt-BR.md)

A Discord bot that joins a voice channel and speaks text in the voice of
whoever you choose, using [Pocket TTS](https://github.com/kyutai-labs/pocket-tts)
running on the CPU. It comes with a local web panel to start and stop
everything, generate audio, clone new voices and look at usage stats.

No web framework, no database, no build step. The bot doesn't use FFmpeg; the
preparation tools do.

```
bot.py, tts_engine.py, audio_source.py   the bot
engine.py, engine_api.py                  the heavy process: model + local API
panel/, Panel.pyw, Panel.bat              the web panel and its launcher
voice.py                                  clone voices and generate audio (CLI)
preparation.py                            raw audio -> reference sample
history.py                                usage log + reports
tools/                                    batch preparation and diagnostics
```

Developed and tested on Windows 11. The panel's launcher and tray icon are
Windows-first; the bot and the engine are plain Python.

## First of all: consent

Cloning someone else's voice requires their explicit permission. The Pocket
TTS license explicitly forbids using it without consent, and in many places a
person's voice is protected personal data (the GDPR in Europe, the LGPD in
Brazil). Ask before recording, not after.

## Setup

In short — the details for each step follow below:

| Step | Needed for |
|---|---|
| 1. `pip install -r requirements.txt` | everything |
| 2. Hugging Face token (accept the Pocket TTS terms) | downloading the model on the first start |
| 3. Discord bot token, bot invited to your server | speaking on Discord (not for generating audio files) |
| 4. `.env` with the two tokens | everything |
| 5. FFmpeg on the PATH | cloning voices, `.ogg` downloads |
| 6. A Python 3.11 `.venv` with `requirements-prep.txt` | cloning voices |
| 7. At least one voice in `voices/` | speaking at all |

If step 5 or 6 is missing, the panel shows a warning at the top with the
exact commands to run; it goes away by itself once they're in place.

**1. Dependencies**

There are two separate Python environments, and the separation is required —
don't try to merge them. The second one is only needed to clone voices.

*Bot, engine, panel and `voice.py`* — your regular Python (3.10+; tested on 3.14):

```powershell
pip install -r requirements.txt
```

*`preparation.py`* — a venv with **Python 3.11**, in `.venv` at the project root
(the panel looks for it there):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-prep.txt --extra-index-url https://download.pytorch.org/whl/cpu
```

It has to be 3.11: `deepfilterlib` only ships `cp311` wheels. And the versions
in `requirements-prep.txt` are pinned for a reason:

| Package | Version | Why |
|---|---|---|
| torch / torchaudio | 2.1.2+cpu | 2.2+ removed `torchaudio.backend.common`, which DeepFilterNet's `df/io.py` imports |
| setuptools | <81 | 81+ removed `pkg_resources`, which resemblyzer imports |
| numpy | <2 | required by torch 2.1.2 |

Upgrading without testing will break it.

You also need [FFmpeg](https://ffmpeg.org) on the PATH for preparing samples
and for `.ogg` downloads (`winget install Gyan.FFmpeg` on Windows).

**2. Model access**

The Pocket TTS weights are distributed under a usage agreement. Log in to
Hugging Face, accept the terms at https://huggingface.co/kyutai/pocket-tts and
create a token at https://huggingface.co/settings/tokens.

**3. Discord bot**

At https://discord.com/developers/applications: create the application, go to
**Bot**, generate the token. In **OAuth2 → URL Generator** tick the `bot` and
`applications.commands` scopes, and the *Connect* and *Speak* permissions. Use
the generated URL to add the bot to your server.

No *privileged intent* is needed — the bot only uses slash commands.

**4. Configuration**

```powershell
copy .env.example .env
```

Fill in `DISCORD_TOKEN` and `HF_TOKEN`; the rest is commented in the file.
`TTS_LANGUAGE` picks the language model (see [Languages](#languages)).

**5. Voices**

A voice is cloned from 15–30 seconds of clean speech (see
[Sample quality](#sample-quality)). The easiest way is the panel's **Clone**
tab, which prepares the audio, lets you listen to the result and only then
saves it. From the command line, prepare the sample first
([Preparing a reference sample](#preparing-a-reference-sample)) and then:

```bash
python voice.py clone path/to/sample_ref.wav alice
```

It creates `voices/alice.safetensors` and a check clip in `outputs/`. The bot sees
the new voice right away, without restarting — it globs `voices/` on every
command.

**6. Run**

Double-click `Panel.bat`. It opens the panel in the browser and puts an icon in
the tray, next to the clock. No terminal window.

The panel has two separate switches:

- **Engine** — loads the model and lets you generate audio. The first start
  downloads the weights and takes a while; later ones use the cache.
- **Discord** — connects the bot to your server. Turning this on starts the
  engine too.

The split exists because generating audio for WhatsApp shouldn't require
showing up online to your friends. Both always start off.

### Generate

Pick the voice, write the text, listen in the browser. Needs the engine on;
doesn't need Discord. The files go to `outputs/`, the same folder as
`python voice.py say` — and the `.ogg` button uses WhatsApp's parameters
(mono Opus 32 kbps, VoIP profile).

Generating from the panel goes into the **same queue** as speaking on Discord:
if the bot is in the middle of a sentence, the test waits its turn instead of
fighting it for the model.

### Clone

Four steps: the audio goes to `work/pending/`, `preparation.py` runs on the
`.venv`'s Python 3.11 with the diagnosis live on screen, you name the voice and
listen to the test sentence, and only then **Save** moves the voice to
`voices/`.

A voice with a name that already exists asks for confirmation. That's the
difference from `python voice.py clone`, which writes straight away and says
"overwriting" after the fact — here, a bad attempt doesn't destroy a voice
that already worked.

The uploaded audio and the `_ref.wav` stay in `work/pending/` after saving, on
purpose: they're the reference material, and they let you try again under
another name. Cleaning the folder is safe at any time.

### Config

The editable `.env` keys, one per field. The `DISCORD_TOKEN` and `HF_TOKEN`
lines don't show up because the panel doesn't know they exist: it reads and
rewrites only the keys on its allow-list, line by line, and leaves everything
else byte for byte. A `.env.bak` is written before every change.

The bot reads them once, when the engine starts, so changing one on Discord's
side needs the **Restart the engine** button — which takes the same seconds as
a normal start.

### When the engine crashes by itself

The panel notices, logs the exit code and restarts it **once**. If it crashes
again, it stops and says so: if the token is wrong or the weights won't
download, restarting fifty times won't fix it. Clicking **Start** by hand
re-arms the automatic attempt.

If you'd rather skip the panel, the engine runs on its own:

```bash
python engine.py
```

It starts an API on `127.0.0.1:8081`, but doesn't connect to Discord by itself
— the panel is what tells it to connect.

## Day-to-day use

```bash
python voice.py list                          # available voices (instant)
python voice.py clone sample.wav alice        # reference WAV -> new voice
python voice.py say alice "text"              # -> outputs/alice_text.wav
python voice.py say alice "text" --whatsapp   # -> .ogg voice message
```

`--whatsapp` delivers mono Opus at 32 kbps with the voice profile, the same
format as a WhatsApp voice note. `--play` opens the file when done; `-o`
picks the path by hand.

`say` goes through the same `TTSEngine.stream()` the bot uses and prints the
real-time factor. If it sounds good here, it sounds good on Discord.

## Preparing a reference sample

Runs in the 3.11 venv. **Activate it first** — outside it, the DeepFilterNet
import fails:

```powershell
.\.venv\Scripts\Activate.ps1
python .\preparation.py '.\samples\PERSON\audio.ogg'
deactivate
```

It writes `audio_ref.wav` next to the input file. That `_ref.wav` is what you
pass to `voice.py clone`.

It takes `.opus .ogg .oga .m4a .mp3 .wav .flac .aac .webm .mp4` — no need to
convert anything first, ffmpeg reads them all. It also takes a **folder**, and
then processes every audio file inside, skipping the `_ref` ones that already
exist.

| Flag | What it does |
|---|---|
| `--sr N` | output sample rate (default 24000, Pocket TTS's native rate) |
| `--lufs N` | target loudness (default −23) |
| `--trim` | cuts long silences |
| `--check` | speaker similarity via resemblyzer |
| `--enhance` | resemble-enhance variant — needs `pip install resemble-enhance` (`deepspeed` doesn't build on Windows) |

Pipeline: ffmpeg (mono 48k, unprocessed) → DeepFilterNet3 → loudnorm −23 LUFS
→ resample.

**Deliberately no gate, compressor or EQ.** The speaker encoder encodes the
processing along with the voice — if you compress the reference, the clone
comes out with the compression baked in.

`--check` measures embedding distance, **not** clone quality. It's for batch
triage. Running it on an already processed output gives 0.999 and means
nothing.

### Recording with Craig

In [Craig](https://craig.chat), download **Multi-track → FLAC**: one track per
person, lossless. Avoid AAC and Ogg Vorbis, which re-encode already lossy
material.

The tracks come with the full length of the call. Cut a good stretch before
processing — `-c copy` slices without re-encoding:

```powershell
ffmpeg -i track.flac -ss 00:04:30 -t 25 -c copy clip.flac
```

### Tools

```bash
python tools/whatsapp_samples.py exports --me "Your Name"
```

Takes a WhatsApp chat export `.zip` (or a folder with several), splits the
voice messages by sender by reading `_chat.txt`, converts them to WAV and ranks
them by quality, leaving the best ones in `work/NAME/candidates/`. You listen
and clone the best one with `voice.py`.

| Tool | What it does |
|---|---|
| `tools/rank_samples.py FOLDER` | scores a folder of WAVs and ranks the best sample candidates |
| `tools/diagnose.py resampler` | checks for clicks at chunk seams without loading the model |
| `tools/diagnose.py smoke` | validates the install and the download with a built-in catalog voice |
| `tools/spectrogram.py A B` | side-by-side spectrogram of two files (run it in the `.venv`) |

## Sample quality

The model reproduces the quality of the reference audio, not just the timbre.
A hissy sample makes a hissy voice. 20 clean seconds are worth more than 3
minutes cut from a call recording.

- 15–30 seconds of continuous speech, without long pauses
- A room without echo, no music, nobody talking over
- In the register you want to reproduce (record it whispering, the clone whispers)
- Run it through an enhancer (e.g. Adobe Podcast Enhance) before importing

If all you have is a call recording with everyone mixed together, the cheapest
path isn't diarization — it's asking each person to record 20 seconds on their
phone. The alternative is a multi-track recording bot, which already delivers
one track per person.

### The source is the ceiling (measured)

WhatsApp voice notes are narrow-band Opus. Measuring a real clip:

| | 99% of the energy | 4–8k band | 8–12k band |
|---|---|---|---|
| raw WhatsApp `.ogg` | 4.2 kHz | −19.5 dB | −35.6 dB |
| after `preparation.py` | 4.5 kHz | −18.5 dB | −34.2 dB |
| clone generated from the `_ref.wav` | 4.0 kHz | −20.1 dB | −39.7 dB |
| clone generated from the raw `.ogg` | 3.2 kHz | −22.3 dB | −40.3 dB |

Three conclusions:

1. **A muffled clone from WhatsApp is the source's limit.** There's no
   brightness above ~5 kHz to clone. Only a better source (Craig) or a
   generative enhancer fixes it.
2. **`preparation.py` helps.** The clone from the raw audio is worse than the
   one from the processed audio — narrower and with less energy at 4–8k.
3. **The sample rate isn't the cause.** Pocket TTS is natively 24 kHz and
   `get_state_for_audio_prompt` resamples by itself. `--sr 24000` is already
   right, and going higher gains nothing because the source has no content up
   there.

Prefer clips peaking between **−6 and −1 dBFS**. A peak at 0.0 dBFS means
WhatsApp's AGC saturated.

## Commands

| Command   | What it does                                                |
|-----------|-------------------------------------------------------------|
| `/say`    | Speaks a text in the chosen voice (with autocomplete)       |
| `/voices` | Lists the loaded voices                                     |
| `/status` | Ranking of who uses the bot most and the favorite voices    |
| `/skip`   | Skips only the current utterance                            |
| `/stop`   | Stops and clears the server's queue                         |
| `/leave`  | Disconnects from the channel                                |

The bot joins the channel of whoever called `/say` by itself, and leaves by
itself when the channel empties or after `IDLE_TIMEOUT` seconds with nothing in
the queue.

## Usage history

Every command becomes a JSON line in `logs/history.jsonl`: when, who asked
(name, id and nickname), the text, the voice, the server and the voice
channel. It's append-only, so you can query it while the bot runs. The path
comes from `HISTORY_FILE`.

```
python history.py summary                # totals by person, voice and day
python history.py show -n 20             # the latest 20 utterances
python history.py csv -o history.csv     # opens in Excel with accents intact
```

All three take `--since 2026-08-01 --until 2026-08-31` to narrow the period.

Inside Discord, `/status` shows the same report in an embed: the podium of who
uses it most, favorite voices, total characters and the busiest day. It takes
`Today`, `Last 7 days`, `Last 30 days` or `All time` (default), and counts
**only the server where it was called** — nothing leaks from one Discord to
another.

`logs/` is in the `.gitignore` — it's the server members' conversations and
shouldn't be committed. Let the people who use the bot know that their lines
are logged.

## Architecture

```
tts_engine.py       model + resampling 24kHz mono -> 48kHz stereo
audio_source.py     buffer that streams into discord.py's player
bot.py              slash commands, per-server queue, client factory
voice.py            CLI: clone / say / list
history.py          usage log (JSONL) + report CLI

Panel.bat           shortcut: opens the panel without a terminal window
Panel.pyw           launcher (lives at the root so `import panel` works)
engine.py           heavy process: loads the model, serves the API on :8081
engine_api.py       engine routes: /status, /discord, /say, /import

panel/
  supervisor.py       panel entry point: tray, single instance, serves :8080
  server.py           panel routes, SSE, origin check on writes
  process.py          the engine's lifecycle as a subprocess + live log
  engine_client.py    HTTP conversation with the engine
  state.py            hidden voices and line-by-line .env editing
  environment.py      checks ffmpeg and the .venv python, warns on screen
  static/             index.html, style.css, app.js (no framework, no build)

work/pending/       cloning's pending folder (not committed): the uploaded
                    audio, the _ref.wav and the not-yet-approved .safetensors
```

The `.safetensors` is the voice (tens of MB); the model itself is 670 MB in
`~/.cache/huggingface` and serves them all. After the first download, it works
offline.

**Two processes, on purpose.** The supervisor is light — it doesn't import
`torch`, `pocket_tts` or `discord` — so it can stay on all the time. The engine
is the heavy one, and only starts when you tell it to. The engine is the sole
owner of the model: the `TTSEngine` lock only protects within a process, so a
second copy would corrupt the audio of two simultaneous generations.

### Decisions worth understanding

**One queue per server, one utterance at a time.** The model is *batch size 1*.
Two simultaneous generations corrupt both. A `ThreadPoolExecutor(max_workers=1)`
plus a lock in the engine guarantee serialization; the `asyncio` queue makes
requests wait instead of failing.

**Inference never runs on the event loop.** PyTorch is synchronous. Calling it
straight from the handler would freeze the bot and drop the voice connection.
Every generation goes to the executor via `run_in_executor`.

**No FFmpeg.** Discord wants 48 kHz stereo 16-bit PCM; the model delivers mono
24 kHz float. The conversion is done with `scipy.signal.resample_poly`, which
removes the subprocess and gives exact control over the buffer.

**Resampling with lookahead.** The polyphase filter needs neighboring samples
on both sides. Resampling each chunk on its own produces an audible click at
every seam — with a 440 Hz sine, the step between samples jumps from 0.034 to
0.246 exactly at the edges. That's why `_StreamResampler` holds back the last
samples of each chunk until the next one arrives. Cost: microseconds of extra
latency.

**An underrun ends playback.** If the generator stalls, `read()` stops waiting
after 10 s and returns `b""`. Without that sticky behavior, the bot would keep
emitting silence in the channel forever.

**The voice state is re-read from disk on every utterance.** Reading the KV
cache from the safetensors is cheap and removes any risk of the in-memory state
being mutated by the previous generation. If you measure it as a bottleneck,
you can cache it — but first confirm that `generate_audio_stream` doesn't
consume the state.

## Tuning

| Symptom                              | What to change                                  |
|--------------------------------------|-------------------------------------------------|
| A new command doesn't show up on Discord | Fill in `GUILD_ID` in the `.env` and restart the engine |
| The text channel fills up with announcements | `AUTO_DELETE_SECONDS=60` in the `.env`   |
| Cuts in the middle of speech         | Raise `PREBUFFER_SECONDS` to 1.5–2.0            |
| Takes too long to start speaking     | Lower `PREBUFFER_SECONDS`; try a model without `_24l` |
| Robotic or unstable voice            | See `--lsd-decode-steps` and `--temperature` in the Pocket TTS CLI |
| CPU maxed out                        | A model without `_24l`; consider int8 quantization |

### Languages

Valid values for `TTS_LANGUAGE`. The real list is the contents of
`pocket_tts/config/`: `load_model()` builds the path straight from the name.

| Language   | Options                                           |
|------------|---------------------------------------------------|
| English    | `english`, `english_2026-01`, `english_2026-04`    |
| Portuguese | `portuguese`, `portuguese_24l`                     |
| Spanish    | `spanish`, `spanish_24l`                           |
| Italian    | `italian`, `italian_24l`                           |
| German     | `german`, `german_24l`                             |
| French     | `french_24l`                                       |

The `_24l` suffix means 24 layers in the `flow_lm` transformer instead of 6 —
it's the **only** difference between `portuguese.yaml` and
`portuguese_24l.yaml`; the rest (Mimi codec, flow depth, tokenizer) is
identical. More quality, more CPU per utterance.

Voices are tied to the model they were cloned with: after changing
`TTS_LANGUAGE`, clone them again.

## Known limitations

- One generation at a time across the whole process, not per server. With
  several busy servers, the queues compete. Scaling means multiple processes.
- No persistence: the queue lives in memory and is gone on restart.
- No permission control. Anyone on the server can use any voice. If that
  matters, filter by role in `say()`.
- **No emotion or speed control.** See below.

### Emotion and speed: what the model allows

Checked against the installed `pocket_tts` sources:

- There's no emotion, speed, pitch or prosody parameter. A search for
  `emotion|speed|prosod|pitch` across the whole package returns nothing.
- The only conditioners are the text (`conditioners/text.py`) and the audio
  prompt. `generate_audio_stream()` only takes `max_tokens`,
  `frames_after_eos` and `copy_state`.
- The generation parameters live in `load_model()`: `temp` (0.7),
  `lsd_decode_steps`, `noise_clamp`, `eos_threshold`. They become instance
  attributes and are read on every generation, so they can change between
  utterances. But `temp` controls **variability**, not emotional direction:
  raising it makes it more expressive and more unstable, not happier.

**Emotion comes from the reference sample.** The `.safetensors` carries the
prosody of whoever spoke in the WAV. To get emotions, clone the same person
once per emotion (`alice_happy`, `alice_sad`) from samples where they really
speak that way.

Speed only with post-processing: changing the resampler's ratio changes the
pitch too (chipmunk effect); keeping the pitch requires `ffmpeg atempo` in the
middle of the stream, which goes against the "no FFmpeg" decision and adds
latency.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite doesn't load the model and doesn't connect to Discord: everything
external is replaced by doubles.

## See also

[alkmei/discord-tts](https://github.com/alkmei/discord-tts) is a more complete
bot (Django, Redis, Docker, an admin panel to manage voices), licensed under
**AGPL-3.0**.

## License

[MIT](LICENSE). Pocket TTS and its weights have their own terms — read them at
https://huggingface.co/kyutai/pocket-tts before using a cloned voice.
