"""Operator CLI for the GKE Autopilot chunk-batch pipelines (ingest/l2cache/meshing)."""

import logging

# stdlib only: workers install `cave-pipeline[distribution]` (no rich, no click) and
# importing `cave_pipeline.distribution` runs this module. Terminal output lives in `term`.

# One level above INFO so the CLI's own messages show but libraries' INFO logs do not.
NOTE = logging.INFO + 5
logging.addLevelName(NOTE, "NOTE")
log = logging.getLogger("pipeline")


def note(msg):
    log.log(NOTE, msg)
