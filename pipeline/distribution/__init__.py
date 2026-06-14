"""Workload-agnostic chunk-distribution core for the k8s Indexed-Job pipeline.

Shared by every worker (PyChunkedGraph, PCGL2Cache, ...) and the operator CLI, so
the grid scatter (a bijection the operator and workers must compute identically)
and the Job exit-code contract have a single source. Light by design: importing
``pipeline.distribution`` pulls only stdlib + the exit codes — ``grid`` (numpy),
``harness`` (numpy), and ``lock`` (kvdbclient) are imported explicitly when needed.
"""

import os
import sys
import traceback

from .exit_codes import FATAL, SUCCESS, TRANSIENT, FatalChunkError

__all__ = ["FATAL", "SUCCESS", "TRANSIENT", "FatalChunkError", "run_and_exit"]


def run_and_exit(main) -> None:
    """Run a pipeline entrypoint, then ``os._exit`` — every container/one-shot entry
    goes through this. A normal return would hang: graph I/O leaves the bigtable.data
    client's non-daemon channel-refresh thread, whose atexit join() never returns and
    the pod stalls until SIGKILL. Exit code is main()'s return (0 if None), the
    SystemExit code, or 1 with a printed traceback on any unhandled error."""
    try:
        code = main() or 0
    except SystemExit as exc:  # argparse / explicit exit, before any Bigtable I/O
        code = exc.code
        if isinstance(code, str):
            print(code, file=sys.stderr)
            code = 1
        elif not isinstance(code, int):
            code = 0 if code is None else 1
    except BaseException:  # noqa: BLE001 - report and exit non-zero, never hang
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
