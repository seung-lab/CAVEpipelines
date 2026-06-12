"""Helper functions for the pipeline CLI."""

from datetime import datetime, timezone

from rich.table import Table

from . import costs, kube, manifest, note

# os._exit after printing: the bigtable.data client leaves a non-daemon channel-refresh
# thread that atexit join()s forever, so a normal exit would hang the exec long after the
# count is computed. Hard-exit once we have the value.
_N_CODE = """
import os, sys
import numpy as np
from pychunkedgraph.graph import ChunkedGraph
cg = ChunkedGraph(graph_id={gid!r})
L = {layer}
print(1 if L == cg.meta.layer_count else int(np.prod(cg.meta.layer_chunk_bounds[L])))
sys.stdout.flush()
os._exit(0)
"""


def ceil_div(a, b):
    return -(-a // b)


def run_pcg(cfg, name, argv):
    """Run a command in the PCG image (util pod or one-shot pod), streaming its logs live."""
    if cfg.persistent_util:
        pod = kube.util_pod(cfg.namespace)
        note(f"{name}: running in util pod '{pod}'...")
        return kube.exec_cmd(
            cfg.namespace, pod, argv, on_line=lambda ln: note(f"  [{name}] {ln}")
        )
    note(f"{name}: running in a one-shot pod...")
    return kube.run_oneshot(cfg.namespace, manifest.oneshot_pod_spec(cfg, name, argv))


def read_n(cfg, layer):
    """Number of chunks in a layer, from cg.meta (via a PCG-image pod)."""
    out = run_pcg(
        cfg,
        f"nread-l{layer}",
        ["python", "-u", "-c", _N_CODE.format(gid=cfg.graph_id, layer=layer)],
    )
    # last all-digit line = the count; anything else is import noise or a traceback
    for line in reversed(out.splitlines()):
        if line.strip().isdigit():
            return int(line.strip())
    raise SystemExit(
        f"could not read the layer {layer} chunk count for '{cfg.graph_id}'; "
        f"pod output:\n{out or '(empty — pod may be restarting; retry)'}"
    )


def job_state(job):
    for c in job.status.conditions or []:
        if c.type == "Complete" and c.status == "True":
            return "complete"
        if c.type == "Failed" and c.status == "True":
            return "failed"
    return "running"


def elapsed(job):
    start = job.status.start_time
    if not start:
        return "-"
    end = job.status.completion_time or datetime.now(timezone.utc)
    minutes = int((end - start).total_seconds()) // 60
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def status_table(cfg) -> Table:
    jobs = sorted(
        kube.list_jobs(cfg.namespace, cfg.workload),
        key=lambda j: int((j.metadata.labels or {}).get("layer", "0")),
    )
    try:
        n_nodes, spot, by_type = kube.node_summary()
        types = ", ".join(f"{c}×{t}" for t, c in sorted(by_type.items()))
        nodes = f"{n_nodes} nodes · {spot} spot" + (f" · {types}" if types else "")
    except Exception:  # noqa: BLE001 - node list may be RBAC-denied; not essential
        nodes = "nodes ?"
    table = Table(
        title=f"{cfg.workload} · {cfg.graph_id} · {nodes}",
        caption="active−ready ≈ pods waiting on Autopilot nodes / spot capacity",
    )
    cols = ("layer", "done", "total", "%", "active", "ready", "failed", "elapsed", "cost")
    for col in cols:
        table.add_column(col, justify="right")
    rate_table = costs.load_table()
    for job in jobs:
        s = job.status
        ann = job.metadata.annotations or {}
        total = int(ann.get("chunks", 0))
        batch = int(ann.get("batch_size", 0)) or 1
        done = min((s.succeeded or 0) * batch, total) if total else 0
        pct = 100 * done // total if total else 0
        color = {"complete": "green", "failed": "red"}.get(job_state(job))
        failed = s.failed or 0
        cost_cell = "-"
        if cfg.region and rate_table:
            est = costs.estimate_job_cost(job, [], rate_table, cfg.region)
            cost_cell = f"${est['total']:.2f}" if "total" in est else "err"
        table.add_row(
            (job.metadata.labels or {}).get("layer", "?"),
            str(done) if total else "-",
            str(total) if total else "-",
            f"[{color}]{pct}%[/]" if color else f"{pct}%",
            str(s.active or 0),
            str(getattr(s, "ready", None) or 0),
            f"[red]{failed}[/]" if failed else "0",
            elapsed(job),
            cost_cell,
        )
    return table
