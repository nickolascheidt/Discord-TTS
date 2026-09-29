"""The Windows shortcut needs to see the `panel` package.

These tests exist because of a bug that left no trace at all: `Panel.bat`
pointed at `panel\\supervisor.py`, Python put `panel\\` in `sys.path[0]`, and
`import panel` failed. Since `pythonw.exe` has no console, the double click
simply did nothing — no window, no message, no log.

The trap is about file location, so the tests run real subprocesses from each
place instead of simulating.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, cwd=ROOT
    )


def test_a_script_inside_panel_does_not_see_its_own_package():
    """Documents the trap: `sys.path[0]` is the script's folder, not the root."""
    probe = ROOT / "panel" / "_test_probe.py"
    probe.write_text("import panel\n", encoding="utf-8")
    try:
        r = _run(probe)
        assert r.returncode != 0
        assert "No module named 'panel'" in r.stderr
    finally:
        probe.unlink()


def test_a_script_at_the_root_sees_the_package():
    """That's why the launcher lives at the root, not inside `panel/`."""
    probe = ROOT / "_test_probe.py"
    probe.write_text("import panel.supervisor\nprint('ok')\n", encoding="utf-8")
    try:
        r = _run(probe)
        assert r.returncode == 0, r.stderr
        assert "ok" in r.stdout
    finally:
        probe.unlink()


def test_the_launcher_exists_at_the_root():
    assert (ROOT / "Panel.pyw").is_file()


def test_the_bat_calls_the_launcher_and_not_the_supervisor_directly():
    bat = (ROOT / "Panel.bat").read_text(encoding="utf-8")
    assert "Panel.pyw" in bat
    assert "supervisor.py" not in bat, "calling the supervisor by path breaks the import"
    assert "pythonw" in bat, "python.exe would open a console window"


def test_the_launcher_logs_the_failure_to_a_file():
    """Without a console, a lost traceback is a double click that does nothing."""
    source = (ROOT / "Panel.pyw").read_text(encoding="utf-8")
    assert "panel-error.log" in source
    assert "traceback.print_exc" in source
    assert "MessageBoxW" in source, "the user needs some warning on screen"
