"""AudioSource fed by streaming, without FFmpeg.

The discord.py player calls `read()` every 20 ms, on a separate thread, and
expects exactly 3840 bytes (20 ms * 48 kHz * 2 channels * 2 bytes). Returning
b"" ends playback.

Pocket TTS usually generates faster than real time on the CPU, so after a
small prebuffer the producer stays ahead of the consumer. `read()` blocks for
up to `_UNDERRUN_TIMEOUT` in the rare case of an underrun, which is better than
inserting silence in the middle of a word.
"""

from __future__ import annotations

import logging
import threading

import discord

log = logging.getLogger(__name__)

FRAME_BYTES = 3840
_BYTES_PER_SECOND = 48_000 * 2 * 2
_UNDERRUN_TIMEOUT = 10.0


class BufferedPCMSource(discord.AudioSource):
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._eof = False
        self._stopped = False
        self._underrun = False

    # --------------------------------------------------------------- producer

    def feed(self, data: bytes) -> None:
        with self._cond:
            self._buffer.extend(data)
            self._cond.notify_all()

    def mark_eof(self) -> None:
        """Signals that no more audio is coming. Always call it, even on error."""
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def finished(self) -> bool:
        return self._eof

    @property
    def buffered_seconds(self) -> float:
        with self._cond:
            return len(self._buffer) / _BYTES_PER_SECOND

    # --------------------------------------------------------------- consumer

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        with self._cond:
            while (
                len(self._buffer) < FRAME_BYTES
                and not self._eof
                and not self._stopped
                and not self._underrun
            ):
                if not self._cond.wait(timeout=_UNDERRUN_TIMEOUT):
                    # The producer stalled. `_underrun` is sticky: without it,
                    # every following read() would wait again and return
                    # silence, leaving the bot mute in the channel forever.
                    self._underrun = True
                    log.warning("Underrun: the generator stopped feeding the buffer.")

            if self._stopped or not self._buffer:
                return b""

            frame = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]

        # The last frame may come out short: pad it with silence.
        return frame.ljust(FRAME_BYTES, b"\x00")

    def cleanup(self) -> None:
        with self._cond:
            self._stopped = True
            self._buffer.clear()
            self._cond.notify_all()
