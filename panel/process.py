"""Lifecycle of a child process, with its stdout turned into a live log.

Knows nothing about HTTP or TTS: it takes a command, starts, stops, and tells
whoever is listening. That's what lets the supervisor be tested without
loading the model.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections import deque
from collections.abc import Callable
from pathlib import Path


class Process:
    """A supervised subprocess, with a ring buffer of what it prints."""

    def __init__(
        self,
        command: list[str],
        cwd: Path,
        max_lines: int = 500,
    ) -> None:
        self._command = command
        self._cwd = Path(cwd)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._log: deque[str] = deque(maxlen=max_lines)
        self._listeners: list[Callable[[str], None]] = []
        self._on_death: list[Callable[[int], None]] = []
        # Tells "the engine crashed" apart from "I asked it to stop". Without
        # this, every click on Stop would fire the automatic restart.
        self._stopping = False
        # Start and stop are "check, then wait", and aiohttp runs handlers
        # concurrently on the same loop. Without this, two clicks on Start
        # launch two engines: two 1.3 GB models, two readers fighting over the
        # same stdout, and a process the panel doesn't even know exists. The
        # lock lives here, and not in the handler, because the tray calls
        # these same methods through another path.
        self._gate: asyncio.Lock | None = None
        self.exit_code: int | None = None

    def _lock(self) -> asyncio.Lock:
        """Created on demand: the Process is born outside the event loop."""
        if self._gate is None:
            self._gate = asyncio.Lock()
        return self._gate

    # ------------------------------------------------------------------ state

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def log(self) -> list[str]:
        return list(self._log)

    def listen(self, callback: Callable[[str], None]) -> None:
        """Called on every new line. Used by SSE to push to the browser."""
        self._listeners.append(callback)

    def on_death(self, callback: Callable[[int], None]) -> None:
        """Called when the child dies on its own, with its exit code.

        Doesn't fire on a deliberate stop: `stop()` raises the flag before
        sending the signal.
        """
        self._on_death.append(callback)

    # ------------------------------------------------------------------ cycle

    async def start(self) -> None:
        async with self._lock():
            await self._start()

    async def _start(self) -> None:
        # Idempotent on purpose. "Start" here means "make sure it's running",
        # and a double click on the button isn't an error condition: the first
        # version raised RuntimeError and the panel answered 500 to whoever
        # just clicked too fast.
        if self.running:
            return

        self.exit_code = None
        self._log.clear()

        # Without this, the child's Python holds lines in its buffer and the
        # log arrives in blocks instead of live.
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=str(self._cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # The panel runs under pythonw, without a console. A `python.exe`
            # child would get its own window from Windows — empty, because the
            # output comes here, and one that kills the child if closed.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = asyncio.create_task(self._read(self._proc))

    async def stop(self) -> None:
        async with self._lock():
            await self._stop()

    async def _stop(self) -> None:
        if not self.running:
            return

        assert self._proc is not None
        self._stopping = True
        try:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()

            if self._reader is not None:
                await self._reader
            self.exit_code = self._proc.returncode
        finally:
            self._stopping = False

    async def wait(self, deadline: float | None = None) -> int | None:
        """Waits for a short-lived child to finish and returns its exit code.

        The engine stays alive until someone stops it, but `preparation.py`
        runs, prints the diagnosis and exits — and the caller needs to know
        whether it worked. Waiting here is waiting for the reader: it pumps
        until the pipe closes, does `await proc.wait()` and records
        `exit_code`.

        `None` means it was never started. Blowing the deadline kills the
        child and returns the kill's code, so a stuck script can't hold the
        HTTP handler forever.

        Doesn't take the lock: `stop()` does, and waiting while holding it
        would prevent cancelling from outside exactly when that's most useful.
        """
        reader = self._reader
        if reader is None:
            return self.exit_code

        if deadline is None:
            await asyncio.shield(reader)
        else:
            try:
                await asyncio.wait_for(asyncio.shield(reader), timeout=deadline)
            except asyncio.TimeoutError:
                # The shield exists because `wait_for` cancels whatever it's
                # waiting on when the deadline passes — and cancelling the
                # reader would leave `exit_code` unrecorded and the stdout pipe
                # half read. This way the deadline only gives up on the wait,
                # and `stop()` cleans up through the path that knows how to kill.
                await self.stop()

        return self.exit_code

    # ---------------------------------------------------------------- reading

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        """Pumps the child's stdout until the pipe closes, which is when it dies.

        Takes the process as a parameter instead of re-reading `self._proc`: a
        reader from a previous run, still draining what was left in the pipe,
        must not write the exit code of the next run.
        """
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            self._log.append(line)
            for listener in self._listeners:
                listener(line)

        await proc.wait()
        if proc is self._proc:
            self.exit_code = proc.returncode
            if not self._stopping:
                for listener in self._on_death:
                    listener(proc.returncode)
