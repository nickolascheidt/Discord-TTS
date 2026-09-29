"""The panel's entry point. Runs detached, without a terminal, with a tray icon.

Deliberately light: it doesn't import torch, pocket_tts or discord. That's
what makes it acceptable to leave it always on while the engine only starts on
demand.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import webbrowser

from aiohttp import web
from dotenv import load_dotenv

from panel import (
    ADDRESS,
    ENV,
    HIDDEN,
    OUTPUTS,
    PANEL_PORT,
    PENDING,
    ROOT,
    server,
    voices_dir,
)
from panel.process import Process

URL = f"http://{ADDRESS}:{PANEL_PORT}/"


def port_in_use() -> bool:
    with socket.socket() as s:
        return s.connect_ex((ADDRESS, PANEL_PORT)) == 0


def icon_png() -> "Image.Image":
    """A plain circle. Avoids versioning a binary just to have a tray icon."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(88, 101, 242, 255))  # Discord blurple
    d.ellipse((24, 20, 40, 44), fill=(255, 255, 255, 255))
    return img


def build_tray(engine: Process, loop: asyncio.AbstractEventLoop,
               stop: asyncio.Event):
    import pystray

    def open_panel(_=None, __=None):
        webbrowser.open(URL)

    def toggle(_=None, __=None):
        action = engine.stop() if engine.running else engine.start()
        asyncio.run_coroutine_threadsafe(action, loop)

    def quit_panel(icon, _=None):
        # Signals an Event instead of stopping the loop by force. `loop.stop()`
        # with main() waiting makes run_until_complete raise "Event loop
        # stopped before Future completed" - which went up to Panel.pyw and
        # showed "The panel couldn't start" on EVERY normal exit. An app that
        # reports a failure when it closes teaches the user to ignore its
        # warnings.
        asyncio.run_coroutine_threadsafe(engine.stop(), loop).result(15)
        icon.stop()
        loop.call_soon_threadsafe(stop.set)

    return pystray.Icon(
        "tts",
        icon_png(),
        "TTS Panel",
        menu=pystray.Menu(
            pystray.MenuItem("Open panel", open_panel, default=True),
            pystray.MenuItem(
                lambda _: "Stop engine" if engine.running else "Start engine", toggle
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_panel),
        ),
    )


async def main_async() -> None:
    # The panel needs to see the same voices folder as the engine.
    load_dotenv(ROOT / ".env")

    engine = Process(
        [sys.executable, str(ROOT / "engine.py")],
        cwd=ROOT,
        max_lines=500,
    )

    app = server.create_app(
        engine=engine,
        voices_dir=voices_dir(),
        hidden=HIDDEN,
        outputs=OUTPUTS,
        pending=PENDING,
        env=ENV,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, ADDRESS, PANEL_PORT).start()

    stop = asyncio.Event()
    tray = build_tray(engine, asyncio.get_running_loop(), stop)
    threading.Thread(target=tray.run, daemon=True).start()

    webbrowser.open(URL)
    try:
        await stop.wait()
    finally:
        await engine.stop()
        await runner.cleanup()


def main() -> None:
    # Second click on the shortcut: opens the browser instead of crashing.
    if port_in_use():
        webbrowser.open(URL)
        return
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
