"""The supervisor's shutdown, without a tray and without a browser.

It exists because of a bug that turned every normal exit into a failure
warning: the tray's quit() called `loop.stop()` while `main_async()` was
waiting, `run_until_complete` raised `RuntimeError: Event loop stopped before
Future completed`, and that went up to `Panel.pyw`, which showed "The panel
couldn't start" every time the user closed the panel.

An app that reports an error when it closes teaches the user to ignore its
warnings.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

import panel.supervisor as sup


@pytest.fixture
def quiet_panel(monkeypatch, tmp_path):
    """No tab opened, no tray icon, no fight over port 8080."""
    monkeypatch.setattr(sup.webbrowser, "open", lambda *a, **k: None)
    monkeypatch.setattr(sup, "PANEL_PORT", 0)  # ephemeral port
    monkeypatch.setattr(sup, "HIDDEN", tmp_path / "hidden.json")

    captured: dict = {}

    class FakeTray:
        def run(self) -> None:
            pass

        def stop(self) -> None:
            pass

    def build(engine, loop, stop):
        captured["engine"] = engine
        captured["stop"] = stop
        return FakeTray()

    monkeypatch.setattr(sup, "build_tray", build)
    return captured


async def test_quit_ends_without_raising(quiet_panel):
    """`stop` is signaled; `main_async()` has to return cleanly."""
    task = asyncio.create_task(sup.main_async())
    for _ in range(200):                       # waits for the tray to be built
        if "stop" in quiet_panel:
            break
        await asyncio.sleep(0.01)
    assert "stop" in quiet_panel, "main_async() never built the tray"

    quiet_panel["stop"].set()

    # Without a timeout this would hang; with the old bug, it would raise RuntimeError.
    await asyncio.wait_for(task, timeout=15)


async def test_quit_stops_the_engine_too(quiet_panel):
    """main_async()'s `finally` can't leave 1.3 GB orphaned."""
    task = asyncio.create_task(sup.main_async())
    for _ in range(200):
        if "engine" in quiet_panel:
            break
        await asyncio.sleep(0.01)

    engine = quiet_panel["engine"]
    # Swaps the command for a harmless child: no loading the model.
    engine._command = [sys.executable, "-c", "import time; time.sleep(60)"]
    await engine.start()
    assert engine.running is True

    quiet_panel["stop"].set()
    await asyncio.wait_for(task, timeout=20)

    assert engine.running is False
