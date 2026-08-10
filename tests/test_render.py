"""A note() emitted under a live display must redraw the frame, not stack another one.

rich's Live installs a render hook on *its own* Console, so text printed through that console
erases and redraws the frame. A record written to a bare stream bypasses the hook, Live's
cursor-up count stops matching the screen, and the previous frame is left behind as orphaned
table borders. Needs a real pty: the hook only repositions the cursor when the console is
interactive, so capture()/pipe-based tests cannot see this.
"""

import os
import pathlib
import pty
import select
import subprocess
import sys
import time

import pytest

pyte = pytest.importorskip("pyte")

ROWS, COLS = 40, 100
REPO_ROOT = (
    pathlib.Path(__file__).resolve().parent.parent
)  # the child imports cave_pipeline

PROBE = """
import logging
import cave_pipeline
from cave_pipeline import term
from rich.live import Live
from rich.table import Table

logging.basicConfig(
    level=cave_pipeline.NOTE,
    format="%(message)s",
    handlers=[term.ConsoleHandler()],
)


def frame(n):
    t = Table()
    t.add_column("cpu")
    t.add_column("memory")
    t.add_row(str(n), f"{n}Gi")
    return t


with Live(console=term.console, refresh_per_second=1000) as live:
    for i in range(3):
        live.update(frame(i), refresh=True)
        cave_pipeline.note(f"note {i}")
"""


def _screen_lines(code, timeout=60):
    """Run `code` under a pty; return the non-blank lines a terminal would be left showing.

    Bounded and exit-checked: pre-commit runs pytest with always_run, so a probe that
    blocks would wedge every commit, and a probe that dies on import would otherwise
    return an empty screen that reads as a rendering failure."""
    env = {
        **os.environ,
        "COLUMNS": str(COLS),
        "LINES": str(ROWS),
        "TERM": "xterm-256color",
    }
    main, worker = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=worker,
        stderr=worker,
        env=env,
        cwd=REPO_ROOT,
    )
    os.close(worker)
    out = b""
    try:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            if not select.select([main], [], [], left)[0]:
                break
            try:
                chunk = os.read(main, 65536)
            except OSError:  # pty closed when the child exited
                break
            if not chunk:
                break
            out += chunk
        try:
            status = proc.wait(timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError(f"probe still running after {timeout}s")
    finally:
        os.close(main)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    text = out.decode("utf-8", "replace")
    assert status == 0, f"probe exited {status}:\n{text}"
    screen = pyte.Screen(COLS, ROWS)
    pyte.Stream(screen).feed(text)
    return [line.rstrip() for line in screen.display if line.strip()]


def test_note_under_live_redraws_instead_of_stacking():
    lines = _screen_lines(PROBE)
    # one table top-border => the frame was erased and redrawn; the bare-stream wiring
    # leaves one border per update (the bug: duplicate cpu/memory rows on screen)
    assert sum(1 for line in lines if line.startswith("┏")) == 1
    assert [line for line in lines if line.startswith("note ")] == [
        "note 0",
        "note 1",
        "note 2",
    ]
