"""Generic one-shot batch-worker harness — the container entrypoint skeleton.

A workload calls ``run(make_processor, context_factory=..., bounds_fn=...)``: this
reads the Indexed-Job env contract, maps JOB_COMPLETION_INDEX to a batch of
scattered chunk coords, and runs each through the workload's per-chunk processor,
returning an exit code for the Job's podFailurePolicy. No Redis, no yaml.

The graph/context is workload-supplied so the harness stays graph-agnostic:
``context_factory(env) -> ctx`` (built once per pod), ``bounds_fn(ctx, layer) ->
(X, Y, Z)``, ``make_processor(ctx, layer, env) -> process_one``; ``process_one(coord)``
returns ``"ok" | "done" | "transient" | "fatal"``.
"""

import logging
import os

from . import grid
from .exit_codes import FATAL, SUCCESS, TRANSIENT

logger = logging.getLogger(__name__)
NOTE = logging.INFO + 5  # progress level above other libs' INFO so they stay quiet
logging.addLevelName(NOTE, "NOTE")


def run(make_processor, *, context_factory, bounds_fn, finalize=None) -> int:
    """Run one batch index for the configured layer; returns a process exit code.

    ``context_factory(env)`` builds the per-pod context once; ``bounds_fn(ctx, layer)``
    gives the layer's (X,Y,Z) chunk grid. ``finalize(ctx, layer)`` runs only after a
    fully successful batch; its failure fails the pod (FATAL) without re-opening any
    chunk — safe to re-run."""
    logging.basicConfig(level=NOTE)
    env = {
        "graph_id": os.environ["PCG_GRAPH_ID"],
        "layer": int(os.environ["PCG_LAYER"]),
        "seed": int(os.environ["PCG_PERM_SEED"]),
        "batch_size": int(os.environ["PCG_BATCH_SIZE"]),
        "index": int(os.environ["JOB_COMPLETION_INDEX"]),
        "n_threads": int(os.environ.get("PCG_N_THREADS", 1)),
    }
    layer, index = env["layer"], env["index"]

    # One context per pod, reused for the whole batch: the graph's meta is read once
    # here (not per chunk) so it never hot-rows.
    ctx = context_factory(env)
    process_one = make_processor(ctx, layer, env)

    coords = grid.batch_coords(
        index, bounds_fn(ctx, layer), env["seed"], env["batch_size"]
    )
    logger.log(NOTE, f"layer {layer} batch {index}: {len(coords)} chunks")

    fatal = transient = 0
    for coord in coords:
        outcome = process_one(coord)
        if outcome == "fatal":
            fatal += 1
        elif outcome == "transient":
            transient += 1

    logger.log(
        NOTE,
        f"layer {layer} batch {index} done: {len(coords) - fatal - transient} ok, "
        f"{transient} transient, {fatal} fatal",
    )
    # Retry the batch while any chunk is transiently unfinished (done ones skip);
    # only FailIndex once nothing but fatal chunks remain.
    if transient:
        return TRANSIENT
    if fatal:
        return FATAL
    if finalize:
        try:
            finalize(ctx, layer)
        except Exception:
            logger.exception("finalize failed")
            return FATAL
    return SUCCESS
