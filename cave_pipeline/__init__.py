"""Operator CLI for the GKE Autopilot chunk-batch pipelines (ingest/l2cache/meshing)."""

import logging

# One level above INFO so the CLI's own messages show but libraries' INFO logs do not.
NOTE = logging.INFO + 5
logging.addLevelName(NOTE, "NOTE")
log = logging.getLogger("pipeline")


def note(msg):
    log.log(NOTE, msg)
