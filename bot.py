"""Discord bot that speaks in voice channels using voices cloned with Pocket TTS."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta

import discord
from discord import app_commands
from dotenv import load_dotenv

import history
from audio_source import BufferedPCMSource
from panel import HIDDEN, state as panel_state
from tts_engine import TTSEngine

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("tts-bot")

# Without GUILD_ID the sync is global and Discord takes up to an hour to show
# a new command in the client. With the server ID it shows up right away.
GUILD_ID = os.getenv("GUILD_ID")
VOICES_DIR = os.getenv("VOICES_DIR", "./voices")
LANGUAGE = os.getenv("TTS_LANGUAGE", "english")
MAX_TEXT_LEN = int(os.getenv("MAX_TEXT_LEN", "400"))
PREBUFFER_SECONDS = float(os.getenv("PREBUFFER_SECONDS", "0.8"))
IDLE_TIMEOUT = float(os.getenv("IDLE_TIMEOUT", "300"))

# Seconds until the bot deletes its own message. 0 turns it off.
# `None` is what discord.py understands as "don't delete".
AUTO_DELETE = float(os.getenv("AUTO_DELETE_SECONDS", "0")) or None

# A single thread: the model is serialized by the engine lock anyway, and more
# threads would only fight over the 2 cores Pocket TTS uses.
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")

_CLEANUP_PATTERNS = [
    (re.compile(r"<a?:\w+:\d+>"), " "),          # custom emoji
    (re.compile(r"<[@#!&]+\d+>"), " "),          # mentions
    (re.compile(r"https?://\S+"), " link "),     # URLs
    (re.compile(r"[`*_~|]+"), ""),               # markdown
    (re.compile(r"\s+"), " "),
]


def sanitize(text: str) -> str:
    for pattern, repl in _CLEANUP_PATTERNS:
        text = pattern.sub(repl, text)
    return text.strip()[:MAX_TEXT_LEN]


@dataclass(slots=True)
class Utterance:
    voice: str
    text: str
    requester: str


class TTSBot(discord.Client):
    def __init__(self, engine: TTSEngine) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.engine = engine
        self._queues: dict[int, asyncio.Queue[Utterance]] = {}
        self._workers: dict[int, asyncio.Task] = {}

    async def setup_hook(self) -> None:
        # The flag belongs to the PROCESS, not the instance: every connection
        # creates a new client, so an instance field would never see the
        # previous sync and would push the commands on every reconnect — a
        # sure way to hit the rate limit. The commands don't change while the
        # process lives.
        global _COMMANDS_SYNCED
        if _COMMANDS_SYNCED:
            log.info("Slash commands already synced in this process; skipping.")
            return
        _COMMANDS_SYNCED = True

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            commands = await self.tree.sync(guild=guild)
            where = f"to server {GUILD_ID}"
        else:
            commands = await self.tree.sync()
            where = "globally (may take up to 1h to show up; set GUILD_ID)"

        log.info(
            "%d slash commands synced %s: %s",
            len(commands), where, ", ".join(f"/{c.name}" for c in commands),
        )

    # ----------------------------------------------------------------- queue

    def queue_for(self, guild_id: int) -> asyncio.Queue[Utterance]:
        if guild_id not in self._queues:
            self._queues[guild_id] = asyncio.Queue()
            self._workers[guild_id] = asyncio.create_task(self._worker(guild_id))
        return self._queues[guild_id]

    async def stop_workers(self) -> None:
        """Cancels the per-server queues. Required before closing the client.

        Without this, a reconnect leaves old tasks pointing at dead guilds,
        and they break silently on the first utterance.
        """
        for task in self._workers.values():
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        self._queues.clear()

    def visible_voices(self) -> list[str]:
        """What Discord may offer, minus what the panel has hidden.

        Re-reads the file on every call, the same way the engine re-globs the
        folder — it's a few bytes, and it saves having to notify the engine
        when the panel changes something.
        """
        return panel_state.visible_names(VOICES_DIR, panel_state.read_hidden(HIDDEN))

    # ---------------------------------------------------------------- events

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)
        log.info("Voices: %s", ", ".join(self.visible_voices()) or "(none)")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Leaves the channel when only the bot is left."""
        vc = member.guild.voice_client
        if vc is None or before.channel != vc.channel or after.channel == vc.channel:
            return
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            log.info("Channel empty in %s, disconnecting.", member.guild.name)
            await vc.disconnect()

    async def _worker(self, guild_id: int) -> None:
        """Drains the server's queue, one utterance at a time."""
        queue = self._queues[guild_id]
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                guild = self.get_guild(guild_id)
                if guild and guild.voice_client:
                    log.info("Idle in %s, leaving the channel.", guild.name)
                    await guild.voice_client.disconnect()
                continue

            try:
                await self._speak(guild_id, item)
            except Exception as e:
                log.exception("Failed to play the utterance from %s", item.requester)
                guild = self.get_guild(guild_id)
                history.record(
                    "failure",
                    user=item.requester,
                    voice=item.voice,
                    text=item.text,
                    error=f"{type(e).__name__}: {e}",
                    # Without these, /status on this server wouldn't see the failure.
                    guild=getattr(guild, "name", None),
                    guild_id=guild_id,
                )
            finally:
                queue.task_done()

    async def _speak(self, guild_id: int, item: Utterance) -> None:
        guild = self.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if vc is None or not vc.is_connected():
            log.warning("No voice connection in %s; dropping the utterance.", guild_id)
            return

        loop = asyncio.get_running_loop()
        source = BufferedPCMSource()

        def produce() -> None:
            try:
                for frame in self.engine.stream(item.voice, item.text):
                    if source.stopped:
                        break
                    source.feed(frame)
            except Exception:
                log.exception("Audio generation failed")
            finally:
                source.mark_eof()

        producer = loop.run_in_executor(EXECUTOR, produce)

        # Wait for a cushion before playing, otherwise the first frame goes out
        # before the model has caught up with the playback rate.
        deadline = loop.time() + 60
        while (
            source.buffered_seconds < PREBUFFER_SECONDS
            and not source.finished
            and loop.time() < deadline
        ):
            await asyncio.sleep(0.05)

        if vc.is_playing():
            vc.stop()

        done = asyncio.Event()
        vc.play(source, after=lambda _err: loop.call_soon_threadsafe(done.set))
        await done.wait()
        await producer


# Slash commands belong to the process, not the instance: see setup_hook.
_COMMANDS_SYNCED = False


# --------------------------------------------------------------- autocomplete


async def voice_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    return [
        app_commands.Choice(name=v, value=v)
        for v in interaction.client.visible_voices()
        if current in v.lower()
    ][:25]


# ------------------------------------------------------------------- commands


def author_channel(interaction: discord.Interaction):
    return getattr(getattr(interaction.user, "voice", None), "channel", None)


async def connect(guild: discord.Guild, channel) -> discord.VoiceClient:
    """Joins the channel, moving the bot if it's already in another one."""
    vc = guild.voice_client
    if vc is None:
        return await channel.connect()
    if vc.channel != channel:
        await vc.move_to(channel)
    return vc


async def ensure_connected(interaction: discord.Interaction) -> discord.VoiceClient | None:
    """Joins the author's channel, or replies asking them to join one."""
    channel = author_channel(interaction)
    if channel is None:
        await interaction.response.send_message(
            "Join a voice channel first.", ephemeral=True
        )
        return None
    return await connect(interaction.guild, channel)


@app_commands.command(name="say", description="Speaks a text in the voice channel with the chosen voice")
@app_commands.describe(voice="Which voice to use", text="What to say")
@app_commands.autocomplete(voice=voice_autocomplete)
async def say(interaction: discord.Interaction, voice: str, text: str) -> None:
    if voice not in interaction.client.visible_voices():
        await interaction.response.send_message(
            f"Voice `{voice}` doesn't exist. Use `/voices` to see the available ones.",
            ephemeral=True,
        )
        return

    clean = sanitize(text)
    if not clean:
        await interaction.response.send_message("Empty text.", ephemeral=True)
        return

    vc = await ensure_connected(interaction)
    if vc is None:
        return

    await interaction.client.queue_for(interaction.guild_id).put(
        Utterance(voice=voice, text=clean, requester=str(interaction.user))
    )
    history.record_interaction(
        "say",
        interaction,
        voice=voice,
        text=clean,
        chars=len(clean),
        # Only keeps the original when sanitize changed something (mention, link, emoji).
        original_text=text if text != clean else None,
    )
    await interaction.response.send_message(
        f"🗣️ **{voice}**: {clean}", delete_after=AUTO_DELETE
    )


@app_commands.command(name="voices", description="Lists the available voices")
async def voices(interaction: discord.Interaction) -> None:
    available = interaction.client.visible_voices()
    if not available:
        await interaction.response.send_message(
            "No voices loaded. Run `python voice.py clone sample.wav NAME` first.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        "Available voices: " + ", ".join(f"`{v}`" for v in available), ephemeral=True
    )


MEDALS = ["🥇", "🥈", "🥉"]
PERIODS = {"today": 1, "week": 7, "month": 30, "all": None}


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


async def _reply_temporary(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
) -> None:
    """Replies to an already deferred interaction, honoring AUTO_DELETE.

    `followup.send` doesn't take `delete_after` (only `response.send_message`
    does), so the message has to be requested back with `wait=True` and the
    delete scheduled on it.
    """
    message = await interaction.followup.send(
        content=content or discord.utils.MISSING,
        embed=embed or discord.utils.MISSING,
        wait=True,
    )
    if AUTO_DELETE:
        await message.delete(delay=AUTO_DELETE)


def _ranking(counts: Counter, total: int, top: int = 5) -> str:
    """Counter -> the first `top` lines, with a medal on the podium."""
    if not counts:
        return "—"
    lines = []
    for pos, (key, n) in enumerate(counts.most_common(top)):
        mark = MEDALS[pos] if pos < len(MEDALS) else f"`{pos + 1}.`"
        pct = 100 * n / total if total else 0
        lines.append(f"{mark} **{key}** — {n} ({pct:.0f}%)")
    if len(counts) > top:
        lines.append(f"*... and {len(counts) - top} more*")
    return "\n".join(lines)


@app_commands.command(name="status", description="Shows who uses the bot the most and the favorite voices")
@app_commands.describe(period="Time range (default: all time)")
@app_commands.choices(period=[
    app_commands.Choice(name="Today", value="today"),
    app_commands.Choice(name="Last 7 days", value="week"),
    app_commands.Choice(name="Last 30 days", value="month"),
    app_commands.Choice(name="All time", value="all"),
])
async def status(
    interaction: discord.Interaction,
    period: app_commands.Choice[str] | None = None,
) -> None:
    key = period.value if period else "all"
    days = PERIODS[key]
    since = (date.today() - timedelta(days=days - 1)).isoformat() if days else None

    # Reading the file is blocking I/O: keep it off the event loop, or a large
    # history would stutter whatever is playing.
    await interaction.response.defer()
    records = await asyncio.to_thread(history.read, since, None, interaction.guild_id)
    s = history.stats(records)

    label = {"today": "today", "week": "in the last 7 days",
             "month": "in the last 30 days", "all": "all time"}[key]

    if not s["total"]:
        await _reply_temporary(interaction, f"No utterances recorded {label}.")
        return

    embed = discord.Embed(
        title="📊 TTS stats",
        description=(
            f"**{s['total']}** {_plural(s['total'], 'utterance', 'utterances')} {label}\n"
            f"from {s['first_day']} to {s['last_day']}"
        ),
        colour=discord.Colour.blurple(),
    )
    embed.add_field(
        name="Top users", value=_ranking(s["by_user"], s["total"]), inline=True
    )
    embed.add_field(
        name="Top voices", value=_ranking(s["by_voice"], s["total"]), inline=True
    )
    embed.add_field(
        name="Text",
        value=(
            f"{s['chars']} characters in total\n"
            f"{s['avg_chars']:.0f} per utterance on average"
        ),
        inline=False,
    )

    busiest_day, busiest_count = s["by_day"].most_common(1)[0]
    embed.set_footer(
        text=f"Busiest day: {busiest_day} "
             f"({busiest_count} {_plural(busiest_count, 'utterance', 'utterances')})"
    )

    await _reply_temporary(interaction, embed=embed)


@app_commands.command(name="stop", description="Stops the current utterance and clears the queue")
async def stop(interaction: discord.Interaction) -> None:
    queue = interaction.client.queue_for(interaction.guild_id)
    dropped = 0
    while not queue.empty():
        queue.get_nowait()
        queue.task_done()
        dropped += 1

    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()

    history.record_interaction("stop", interaction, dropped=dropped)
    await interaction.response.send_message(
        f"Stopped. {dropped} utterance(s) dropped.", ephemeral=True
    )


@app_commands.command(name="skip", description="Skips only the current utterance")
async def skip(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        history.record_interaction("skip", interaction)
        await interaction.response.send_message("Skipped.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)


@app_commands.command(name="leave", description="Disconnects from the voice channel")
async def leave(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        history.record_interaction("leave", interaction)
        await interaction.response.send_message("Left.", ephemeral=True)
    else:
        await interaction.response.send_message("I'm not in any channel.", ephemeral=True)


# -------------------------------------------------------------------- factory


def create_bot(engine: TTSEngine) -> TTSBot:
    """A new client per connection.

    Reusing the same `discord.Client` across connections looked possible —
    `clear()` advertises itself as "re-opened" — but it only restores part of
    the state: it leaves the HTTP `connector` pointing at one that `close()`
    already closed, and `loop` goes back to MISSING through paths inside
    `connect()` itself, which calls `close()` in its exception handlers.
    Throwing the object away removes the whole class of problem; the cost is
    registering the commands again, and they are immutable objects that can be
    shared between trees.
    """
    bot = TTSBot(engine)
    for command in COMMANDS:
        bot.tree.add_command(command)
    return bot


# Immutable objects: the same Command can be registered on several clients'
# trees, which is what makes the factory above cheap.
COMMANDS = (say, voices, status, stop, skip, leave)
