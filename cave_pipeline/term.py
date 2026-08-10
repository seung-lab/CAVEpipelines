"""Operator-only terminal output: the one Console every CLI display shares.

Imported by the CLI, never by a worker — the package root must stay stdlib-only so
``cave-pipeline[distribution]`` (numpy + kvdbclient, no rich) can import it.
"""

import logging

from rich.console import Console

# Messages and live displays must share one Console: rich's Live installs a render hook on
# *its own* console, so text printed through it erases and redraws the live frame. A write
# that bypasses the hook leaves the frame un-erased and the next one stacks under it.
# Progress and messages are stderr; stdout carries only the data tables (`runs`, `run`,
# `pods`, and every `--once` view), which stay pipeable.
console = Console(stderr=True)


class ConsoleHandler(logging.Handler):
    """Log through the shared Console so messages interleave with a live display."""

    def emit(self, record):
        try:
            # soft_wrap: pod logs and tracebacks must survive verbatim — console width is
            # 80 when stderr is not a tty, which would break file paths mid-paste.
            console.print(
                self.format(record), markup=False, highlight=False, soft_wrap=True
            )
        except Exception:  # noqa: BLE001 - logging must never break a command
            self.handleError(record)
