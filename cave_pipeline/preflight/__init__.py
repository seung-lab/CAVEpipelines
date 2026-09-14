"""Check a PCG image against the image contract by reading it from Docker Hub.

Nothing runs and nothing is created: the image's manifest, config and layers stream into
memory, and every clause is checked against the sources they hold, with all violations
reported at once. No pass is remembered, because a tag can be re-pushed; every spending
command reads the image again. An image Docker Hub cannot serve right now goes unchecked,
and the command stops with that reason rather than a violation.

| module | owns |
|---|---|
| `registry` | Docker Hub: the manifest, config and layer blobs |
| `files` | the layer walk: whiteouts, what is installed, the kept sources |
| `source` | those sources parsed as modules, and the imports they reach |
| `trace` | the processor, harness and parser shapes each workload is built from |
| `clauses` | the contract's clauses, each checked against one inspection |
| `check` | the preflight a command runs, and its report |
| `utils` | what the rest share |
"""

from .check import Preflight, Report

__all__ = ["Preflight", "Report"]
