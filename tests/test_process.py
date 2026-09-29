import asyncio
import sys

from panel.process import Process

# A child that prints N lines and stays alive until killed.
TALKER = (
    "import sys, time\n"
    "for i in range(int(sys.argv[1])):\n"
    "    print(f'line {i}', flush=True)\n"
    "time.sleep(60)\n"
)

# A child that dies right away with code 3.
DYING = "import sys; print('about to die', flush=True); sys.exit(3)\n"


def command(tmp_path, source, *args):
    script = tmp_path / "child.py"
    script.write_text(source, encoding="utf-8")
    return [sys.executable, str(script), *map(str, args)]


async def wait_until(condition, deadline=5.0):
    """A short busy wait: a subprocess doesn't tell you when it's done writing."""
    limit = asyncio.get_running_loop().time() + deadline
    while asyncio.get_running_loop().time() < limit:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_start_marks_it_as_running(tmp_path):
    p = Process(command(tmp_path, TALKER, 1), cwd=tmp_path)
    await p.start()
    try:
        assert p.running is True
    finally:
        await p.stop()


async def test_stop_ends_the_process(tmp_path):
    p = Process(command(tmp_path, TALKER, 1), cwd=tmp_path)
    await p.start()
    await p.stop()
    assert p.running is False


async def test_captures_the_child_stdout(tmp_path):
    p = Process(command(tmp_path, TALKER, 3), cwd=tmp_path)
    await p.start()
    try:
        assert await wait_until(lambda: len(p.log) >= 3)
        assert p.log[:3] == ["line 0", "line 1", "line 2"]
    finally:
        await p.stop()


async def test_the_ring_buffer_has_a_cap(tmp_path):
    p = Process(command(tmp_path, TALKER, 600), cwd=tmp_path, max_lines=500)
    await p.start()
    try:
        assert await wait_until(lambda: len(p.log) == 500)
        assert p.log[-1] == "line 599"
    finally:
        await p.stop()


async def test_detects_death_and_keeps_the_code(tmp_path):
    p = Process(command(tmp_path, DYING), cwd=tmp_path)
    await p.start()
    assert await wait_until(lambda: not p.running)
    assert p.exit_code == 3
    assert "about to die" in p.log


async def test_starting_twice_does_not_launch_two(tmp_path):
    """Starting what is already running is a no-op, not an error: it's a double click."""
    p = Process(command(tmp_path, TALKER, 1), cwd=tmp_path)
    await p.start()
    try:
        first = p._proc.pid
        await p.start()
        assert p._proc.pid == first
        assert p.running is True
    finally:
        await p.stop()


async def test_two_simultaneous_starts_launch_a_single_process(tmp_path):
    """Regression: without the lock, two clicks on Start launched two engines.

    `start()` checks `running` and only then awaits create_subprocess_exec,
    and aiohttp runs handlers concurrently on the same loop. Both calls passed
    the check, two 1.3 GB models started, both readers fought over the same
    stdout, and the Process only kept the second pid - the first stayed
    alive, holding the port, out of reach of Stop and Quit.
    """
    p = Process(command(tmp_path, TALKER, 1), cwd=tmp_path)
    try:
        await asyncio.gather(p.start(), p.start(), p.start())
        assert p.running is True
        # A single reader; two iterating the same stdout would raise RuntimeError.
        assert await wait_until(lambda: p.log[:1] == ["line 0"])
    finally:
        await p.stop()
    assert p.running is False


async def test_stopping_a_stopped_process_does_not_blow_up(tmp_path):
    p = Process(command(tmp_path, TALKER, 1), cwd=tmp_path)
    await p.stop()
    assert p.running is False


async def test_tells_the_listener_about_every_line(tmp_path):
    received = []
    p = Process(command(tmp_path, TALKER, 2), cwd=tmp_path)
    p.listen(received.append)
    await p.start()
    try:
        assert await wait_until(lambda: len(received) >= 2)
        assert received[:2] == ["line 0", "line 1"]
    finally:
        await p.stop()


# -------------------------------------------------------------------- wait()

SHORT_CHILD = (
    "import sys\n"
    "print('[ref] 18.3s | peak -3.2 dBFS | clip 0.00%')\n"
    "print('    written: x_ref.wav')\n"
    "sys.exit(0)\n"
)

FAILING_CHILD = "import sys\nprint('ERROR: ffmpeg not found')\nsys.exit(2)\n"


async def test_wait_returns_the_exit_code(tmp_path):
    proc = Process([sys.executable, "-c", SHORT_CHILD], cwd=tmp_path)
    await proc.start()

    assert await proc.wait() == 0
    assert not proc.running


async def test_wait_returns_the_failure_code(tmp_path):
    proc = Process([sys.executable, "-c", FAILING_CHILD], cwd=tmp_path)
    await proc.start()

    assert await proc.wait() == 2


async def test_wait_delivers_the_whole_log(tmp_path):
    """It's preparation.py's diagnosis; the screen shows these lines."""
    proc = Process([sys.executable, "-c", SHORT_CHILD], cwd=tmp_path)
    await proc.start()
    await proc.wait()

    assert any("[ref] 18.3s" in line for line in proc.log)
    assert any("written" in line for line in proc.log)


async def test_waiting_twice_returns_the_same_code(tmp_path):
    """Awaiting the same task twice is fine; the second wait can't hang."""
    proc = Process([sys.executable, "-c", FAILING_CHILD], cwd=tmp_path)
    await proc.start()

    assert await proc.wait() == 2
    assert await proc.wait() == 2


async def test_wait_without_starting_returns_None(tmp_path):
    proc = Process([sys.executable, "-c", SHORT_CHILD], cwd=tmp_path)

    assert await proc.wait() is None


async def test_wait_with_a_deadline_kills_what_does_not_finish(tmp_path):
    """A stuck preparation.py can't hold the handler forever."""
    proc = Process([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
    await proc.start()

    code = await proc.wait(deadline=0.5)

    assert code is not None
    assert code != 0
    assert not proc.running


# ------------------------------------------------------------- dying on its own


async def test_dying_on_its_own_tells_the_listener(tmp_path):
    deaths: list[int] = []
    proc = Process([sys.executable, "-c", FAILING_CHILD], cwd=tmp_path)
    proc.on_death(deaths.append)

    await proc.start()
    await proc.wait()

    assert deaths == [2]


async def test_stopping_on_purpose_does_not_count_as_death(tmp_path):
    """Otherwise every Stop would fire an automatic restart."""
    deaths: list[int] = []
    proc = Process([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path)
    proc.on_death(deaths.append)

    await proc.start()
    await proc.stop()

    assert deaths == []


async def test_a_clean_exit_also_counts(tmp_path):
    """The engine doesn't exit with 0 by itself: if it exited, something happened."""
    deaths: list[int] = []
    proc = Process([sys.executable, "-c", "pass"], cwd=tmp_path)
    proc.on_death(deaths.append)

    await proc.start()
    await proc.wait()

    assert deaths == [0]
