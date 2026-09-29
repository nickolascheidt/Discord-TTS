"""Entry point of the heavy process: loads the model and serves the API.

Connecting to Discord is an action on this process, not the reason it exists —
that's what lets you generate audio (for WhatsApp, say) without showing up
online to anyone.
"""

from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web
from dotenv import load_dotenv

from panel import ADDRESS, ENGINE_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("engine")


async def main() -> None:
    load_dotenv()
    token = os.environ["DISCORD_TOKEN"]

    # Imported after load_dotenv because bot.py reads the variables on import.
    import bot as bot_mod
    import engine_api
    from tts_engine import TTSEngine

    engine = TTSEngine(
        os.getenv("VOICES_DIR", "./voices"),
        language=os.getenv("TTS_LANGUAGE", "english"),
    )

    # The factory, not an instance: every Discord connection gets a new
    # client, and the engine (the expensive part) outlives all of them.
    #
    # The EXECUTOR is the bot's, on purpose: generating audio from the panel
    # and speaking on Discord go through the SAME thread, so a test waits its
    # turn instead of fighting the utterance being played for the model lock.
    app = engine_api.create_app(engine, token, bot_mod.create_bot, bot_mod.EXECUTOR)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, ADDRESS, ENGINE_PORT).start()

    log.info("Engine ready at http://%s:%d", ADDRESS, ENGINE_PORT)
    try:
        await asyncio.Event().wait()
    finally:
        bot = app["state"]["bot"]
        if bot is not None and not bot.is_closed():
            await bot.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
