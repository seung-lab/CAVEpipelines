"""Helper functions for the pipeline CLI."""

import json
import os
from datetime import datetime, timezone

import yaml
from rich.table import Table

from . import costdb, costs, kube, manifest, note

# All layers' chunk counts (L2..root). ChunkedGraph init is costly, so this runs at most
# once (os._exit dodges the bigtable channel-thread exit hang); the result is cached
# locally and setup invalidates the cache so a re-setup recomputes.
_COUNTS_CODE = """
import os, sys
from pychunkedgraph.graph import ChunkedGraph
cg = ChunkedGraph(graph_id={gid!r})
print(*[int(c) for c in cg.meta.layer_chunk_counts])
sys.stdout.flush()
os._exit(0)
"""


def ceil_div(a, b):
    return -(-a // b)


def run_pcg(cfg, name, argv):
    """Run a command in the PCG image (util pod or one-shot pod), streaming its logs live."""
    if cfg.persistent_util:
        pod = kube.util_pod(cfg.namespace)
        note(f"{name}: in util pod")
        return kube.exec_cmd(
            cfg.namespace, pod, argv, on_line=lambda ln: note(f"  [{name}] {ln}")
        )
    note(f"{name}: in one-shot pod")
    return kube.run_oneshot(cfg.namespace, manifest.oneshot_pod_spec(cfg, name, argv))


def run_with_dataset(cfg, name, argv):
    """Apply the graph's dataset ConfigMap, then run argv in a fresh one-shot pod.

    Always one-shot: a new pod mounts the just-applied ConfigMap immediately, while
    a running pod's mount would lag the kubelet sync by 60-90s. The key stays
    `dataset.yml`, matching the in-pod PCG_DATASET default."""
    cm = manifest.dataset_configmap_name(cfg.graph_id)
    kube.apply_configmap(
        cfg.namespace,
        cm,
        {"dataset.yml": yaml.safe_dump(cfg.dataset)},
        {"pipeline": "dataset", "graph": cfg.graph_id},
    )
    note(f"{name}: dataset configmap '{cm}' applied")
    return kube.run_oneshot(
        cfg.namespace, manifest.oneshot_pod_spec(cfg, name, argv, dataset_configmap=cm)
    )


def _counts_cache(cfg) -> str:
    return os.path.join(cfg.config_dir, ".layer_counts.json")


def _read_cache(cfg) -> dict:
    try:
        with open(_counts_cache(cfg)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_cache(cfg, cache) -> None:
    try:
        with open(_counts_cache(cfg), "w") as f:
            json.dump(cache, f)
    except OSError:
        pass  # cache is best-effort


def invalidate_layer_counts(cfg) -> None:
    """Drop this graph's cached counts (call after setup changes the graph)."""
    cache = _read_cache(cfg)
    if cache.pop(cfg.graph_id, None) is not None:
        _write_cache(cfg, cache)


def read_layer_counts(cfg) -> dict:
    """{layer: chunk_count} for every layer (L2..root). Cached locally after the first
    read, so ChunkedGraph is initialized at most once per graph."""
    cache = _read_cache(cfg)
    if cfg.graph_id in cache:
        return {int(k): v for k, v in cache[cfg.graph_id].items()}
    out = run_pcg(
        cfg, "layer-counts", ["python", "-u", "-c", _COUNTS_CODE.format(gid=cfg.graph_id)]
    )
    for line in reversed(out.splitlines()):
        parts = line.split()
        if parts and all(p.isdigit() for p in parts):
            counts = {2 + i: int(c) for i, c in enumerate(parts)}
            cache[cfg.graph_id] = {str(k): v for k, v in counts.items()}
            _write_cache(cfg, cache)
            return counts
    raise SystemExit(
        f"could not read layer counts for '{cfg.graph_id}'; pod output:\n{out}"
    )


def read_n(cfg, layer):
    """Chunk count for a layer, from the cached per-layer counts."""
    counts = read_layer_counts(cfg)
    if layer not in counts:
        raise SystemExit(f"layer {layer} not in {sorted(counts)} for '{cfg.graph_id}'")
    return counts[layer]


def count_indexes(intervals) -> int:
    """Count indexes in a k8s interval string like '1,3-5,7' (failed_indexes format)."""
    total = 0
    for part in (intervals or "").split(","):
        if part:
            lo, _, hi = part.partition("-")
            total += int(hi or lo) - int(lo) + 1
    return total


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


def recorded_costs(cfg, rate_table) -> tuple:
    """({layer: priced totals}, cluster fee) from the local cost record."""
    per_layer = {}
    now = datetime.now(timezone.utc).timestamp()
    conn = costdb.connect(cfg)
    try:
        jobs = costdb.job_rows(conn)
        for j in jobs:
            rate = costs.rate_for(rate_table, cfg.region, j["compute_class"])
            if not rate:
                continue
            usage = costs.usage_from_rows(j, costdb.pod_rows(conn, j["job_uid"]), now)
            priced = costs.price_usage(usage, rate)
            agg = per_layer.setdefault(
                j["layer"],
                {"total": 0.0, "cpu": 0.0, "mem": 0.0, "pod_hours": 0.0, "basis": set()},
            )
            for key in ("total", "cpu", "mem"):
                agg[key] += priced[key]
            agg["pod_hours"] += usage["pod_hours"]
            agg["basis"].add(usage["basis"])
        cluster_fee = costs.fee(rate_table, cfg.region, jobs, now)
    finally:
        conn.close()
    for agg in per_layer.values():
        agg["basis"] = "+".join(sorted(agg["basis"]))
    return per_layer, cluster_fee


def usage_table(cfg, job_name) -> Table:
    """Per-pod usage for one layer Job, in cores/GiB, ordered by task index."""
    table = Table(
        title=f"{job_name} usage",
        caption=f"requests: {cfg.job.cpu} cpu · {cfg.job.memory} per pod",
    )
    for col, justify in (("pod", "left"), ("cpu", "right"), ("memory", "right")):
        table.add_column(col, justify=justify)
    items = kube.pod_metrics(cfg.namespace, job_name)
    if not items:
        table.caption = "no metrics (metrics-server unavailable, or no running pods)"
        return table

    def index_of(item):  # pods are named {job}-{completion index}-{suffix}
        try:
            return int(item["metadata"]["name"][len(job_name) + 1 :].split("-")[0])
        except ValueError:
            return -1

    for item in sorted(items, key=index_of):
        usage = item["containers"][0]["usage"]
        table.add_row(
            item["metadata"]["name"],
            f"{costs.parse_cpu(usage['cpu']):.1f}",
            f"{costs.parse_mem(usage['memory']):.1f}Gi",
        )
    return table


def status_table(cfg, layer_totals=None) -> Table:
    """Per-layer progress. With `layer_totals` ({layer: chunks}), every layer is shown —
    submitted ones with live progress, the rest with their a-priori total (pending)."""
    jobs_by_layer = {
        int((j.metadata.labels or {}).get("layer", "0")): j
        for j in kube.list_jobs(cfg.namespace, cfg.workload)
    }
    try:
        n_nodes, spot, by_type = kube.node_summary()
        types = ", ".join(f"{c}×{t}" for t, c in sorted(by_type.items()))
        nodes = f"{n_nodes} nodes · {spot} spot" + (f" · {types}" if types else "")
    except Exception:  # noqa: BLE001 - node list may be RBAC-denied; not essential
        nodes = "nodes ?"
    table = Table(
        title=f"{cfg.workload} · {cfg.graph_id} · {nodes}",
        caption="retries = failed attempts (transient) · failed = dead tasks (`inspect`) · "
        "active−ready ≈ pods waiting on capacity · cost = recorded Spot estimate",
    )
    cols = (
        "layer",
        "done",
        "total",
        "%",
        "active",
        "ready",
        "retries",
        "failed",
        "elapsed",
        "cost",
    )
    for col in cols:
        table.add_column(col, justify="right")
    rate_table = costs.load_table()
    recorded = {}
    if cfg.region and rate_table:
        try:
            recorded, _ = recorded_costs(cfg, rate_table)
        except Exception:  # noqa: BLE001 - cost is auxiliary, never fatal
            recorded = {}
    layers = sorted(layer_totals) if layer_totals else sorted(jobs_by_layer)
    for layer in layers:
        job = jobs_by_layer.get(layer)
        total = (layer_totals or {}).get(layer)
        if job is None:  # known size, not yet submitted
            row = [str(layer), "-", str(total) if total else "-"] + ["-"] * 7
            table.add_row(*row)
            continue
        s = job.status
        ann = job.metadata.annotations or {}
        if total is None:
            total = int(ann.get("chunks", 0))
        batch = int(ann.get("batch_size", 0)) or 1
        done = min((s.succeeded or 0) * batch, total) if total else 0
        pct = 100 * done // total if total else 0
        color = {"complete": "green", "failed": "red"}.get(job_state(job))
        retries = s.failed or 0  # attempts that burned a retry; dead tasks are separate
        dead = count_indexes(getattr(s, "failed_indexes", None))
        cost_cell = (
            costs.fmt_dollars(recorded[layer]["total"]) if layer in recorded else "-"
        )
        table.add_row(
            str(layer),
            str(done) if total else "-",
            str(total) if total else "-",
            f"[{color}]{pct}%[/]" if color else f"{pct}%",
            str(s.active or 0),
            str(getattr(s, "ready", None) or 0),
            str(retries),
            f"[red]{dead}[/]" if dead else "0",
            elapsed(job),
            cost_cell,
        )
    return table
