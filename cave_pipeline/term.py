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


class DropTransientRetries(logging.Filter):
    """Drop urllib3's connection-retry warnings; a real failure still raises ApiException.

    Must be a *handler* filter, on two counts: kubernetes' Configuration resets the
    urllib3 logger's level to WARNING whenever one is constructed, and a logger filter
    is not applied to records propagated from children like urllib3.connectionpool.
    """

    def filter(self, record) -> bool:
        return not (record.name.startswith("urllib3") and record.levelno < logging.ERROR)


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
