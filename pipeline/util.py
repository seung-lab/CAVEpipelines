"""Helper functions for the pipeline CLI."""

import dataclasses
import json
import os
from datetime import datetime, timezone

import yaml
from rich.console import Group
from rich.table import Table

from . import cgcache, costs, kube, manifest, note
from .db import cost, state

# Cold ChunkedGraph init fits in 30s with headroom; the warm cg-cache server pays it
# once at boot, so every later probe returns well within this.
CG_TIMEOUT = 30


def ceil_div(a, b):
    return -(-a // b)


def run_pcg(cfg, name, argv, timeout=300):
    """Run a command in the PCG image: util pod (logs streamed live) or one-shot
    pod (log returned on completion)."""
    if cfg.persistent_util:
        pod = kube.util_pod(cfg.namespace)
        note(f"{name}: in util pod")
        return kube.exec_cmd(
            cfg.namespace,
            pod,
            argv,
            timeout=timeout,
            on_line=lambda ln: note(f"  [{name}] {ln}"),
        )
    note(f"{name}: in one-shot pod")
    return kube.run_oneshot(cfg.namespace, manifest.oneshot_pod_spec(cfg, name, argv))


def _query_meta(cfg, op, gid) -> str:
    """Run a cg meta probe ('counts'|'mesh') and return its stdout: the persistent util
    pod's warm cg-cache server over its socket, else a one-shot pod that imports cg
    inline. A PCG error surfaces via the non-zero exit, traceback already streamed."""
    name = {"counts": "layer-counts", "mesh": "mesh-check"}[op]
    if cfg.persistent_util:
        pod = kube.util_pod(cfg.namespace)
        note(f"{name}: in util pod")
        argv = ["python", "-u", "-c", cgcache.CLIENT_SRC, cgcache.CG_SOCK, op, gid]
        argv.append(str(CG_TIMEOUT))  # the client's own connect-retry deadline
        # exec cap exceeds that deadline so the client's 'unreachable' message wins
        return kube.exec_cmd(
            cfg.namespace,
            pod,
            argv,
            timeout=CG_TIMEOUT + 5,
            on_line=lambda ln: note(f"  [{name}] {ln}"),
        )
    note(f"{name}: in one-shot pod")
    argv = ["python", "-u", "-c", cgcache.ONESHOT_SRC, op, gid]
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
    """Drop this graph's cached counts; stale counts would mis-size every submit."""
    cache = _read_cache(cfg)
    if cache.pop(cfg.graph_id, None) is None:
        return
    try:  # unlike cache writes, failed invalidation must be loud
        with open(_counts_cache(cfg), "w") as f:
            json.dump(cache, f)
    except OSError as exc:
        raise SystemExit(f"could not invalidate the layer-counts cache: {exc}")


def cached_layer_counts(cfg) -> dict:
    """This graph's cached {layer: chunks}, or None — never touches the cluster."""
    cached = _read_cache(cfg).get(cfg.graph_id)
    return {int(k): v for k, v in cached.items()} if cached else None


def read_layer_counts(cfg) -> dict:
    """{layer: chunk_count} for every layer (L2..root). Cached locally after the first
    read, so ChunkedGraph is initialized at most once per graph. Any read failure
    raises with the PCG traceback already streamed above."""
    cache = _read_cache(cfg)
    if cfg.graph_id in cache:
        return {int(k): v for k, v in cache[cfg.graph_id].items()}
    out = _query_meta(cfg, "counts", cfg.graph_id)
    for line in reversed(out.splitlines()):
        parts = line.split()
        if parts and all(p.isdigit() for p in parts):
            counts = {2 + i: int(c) for i, c in enumerate(parts)}
            cache[cfg.graph_id] = {str(k): v for k, v in counts.items()}
            _write_cache(cfg, cache)
            return counts
    raise SystemExit(
        f"could not read layer counts for graph '{cfg.graph_id}'; pod output:\n{out}"
    )


def read_n(cfg, layer):
    """Chunk count for a layer, from the cached per-layer counts."""
    counts = read_layer_counts(cfg)
    if layer not in counts:
        raise SystemExit(f"layer {layer} not in {sorted(counts)} for '{cfg.graph_id}'")
    return counts[layer]


def mesh_meta_written(cfg) -> bool:
    """Whether mesh-meta has run (graph meta has a mesh block). Read errors surface."""
    out = _query_meta(cfg, "mesh", cfg.graph_id)
    return out.strip().split("\n")[-1].strip() == "yes"


def count_indexes(intervals) -> int:
    """Count indexes in a k8s interval string like '1,3-5,7' (failed_indexes format)."""
    total = 0
    for part in (intervals or "").split(","):
        if part:
            lo, _, hi = part.partition("-")
            total += int(hi or lo) - int(lo) + 1
    return total


def job_progress(job, total=None) -> dict:
    """One Job's progress numbers — shared by the status table and --oneshot."""
    s = job.status
    ann = job.metadata.annotations or {}
    if total is None:
        total = int(ann.get("chunks", 0))
    batch = int(ann.get("batch_size", 0)) or 1
    done = min((s.succeeded or 0) * batch, total) if total else 0
    return {
        "total": total,
        "done": done,
        "pct": 100 * done // total if total else 0,
        "active": s.active or 0,
        "ready": getattr(s, "ready", None) or 0,
        "retries": s.failed or 0,  # attempts that burned a retry; dead is separate
        "dead": count_indexes(getattr(s, "failed_indexes", None)),
        "state": job_state(job),
    }


def job_state(job):
    for c in job.status.conditions or []:
        if c.type == "Complete" and c.status == "True":
            return "complete"
        if c.type == "Failed" and c.status == "True":
            return "failed"
    return "running"


def _terminal_time(job):
    """k8s sets completionTime only on success; Failed Jobs end at the condition."""
    for c in job.status.conditions or []:
        if c.type in ("Complete", "Failed") and c.status == "True":
            return c.last_transition_time
    return None


def elapsed(job):
    start = job.status.start_time
    if not start:
        return "-"
    end = job.status.completion_time or _terminal_time(job) or datetime.now(timezone.utc)
    minutes = int((end - start).total_seconds()) // 60
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def recorded_costs(cfg, rate_table) -> tuple:
    """({layer: priced totals}, cluster fee) from the local cost record."""
    per_layer = {}
    now = datetime.now(timezone.utc).timestamp()
    jobs = cost.jobs(cfg)
    for j in jobs:
        rate = costs.rate_for(rate_table, cfg.region, j.compute_class)
        if not rate:
            continue
        usage = costs.usage_from_rows(j, cost.pods(cfg, j.job_uid), now)
        priced = costs.price_usage(usage, rate)
        agg = per_layer.setdefault(
            j.layer,
            {"total": 0.0, "cpu": 0.0, "mem": 0.0, "pod_hours": 0.0, "basis": set()},
        )
        for key in ("total", "cpu", "mem"):
            agg[key] += priced[key]
        agg["pod_hours"] += usage["pod_hours"]
        agg["basis"].add(usage["basis"])
    cluster_fee = costs.fee(rate_table, cfg.region, jobs, now)
    for agg in per_layer.values():
        agg["basis"] = "+".join(sorted(agg["basis"]))
    return per_layer, cluster_fee


def usage_table(cfg, job_name, layer=None) -> Table:
    """Per-pod usage for one layer Job, in cores/GiB, ordered by task index."""
    if layer is None:
        caption = f"requests: {cfg.job.cpu} cpu | {cfg.job.memory} per pod"
    else:  # the layer's actual request (curves/overrides), not the flat default
        cpu, mem = manifest.requests_for(cfg.job, layer)
        caption = f"requests: {cpu:g} cpu | {mem:g}Gi per pod"
    table = Table(title=f"{job_name} usage", caption=caption)
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
        nodes = f"{n_nodes} nodes | {spot} spot" + (f" | {types}" if types else "")
    except Exception:  # noqa: BLE001 - node list may be RBAC-denied; not essential
        nodes = "nodes ?"
    table = Table(
        title=f"{cfg.workload} | {cfg.graph_id} | {nodes}",
        caption="retries = failed attempts (transient) | failed = dead tasks (`inspect`) | "
        "active−ready ≈ pods waiting on capacity | cost = recorded Spot estimate",
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
        p = job_progress(job, total)
        color = {"complete": "green", "failed": "red"}.get(p["state"])
        cost_cell = (
            costs.fmt_dollars(recorded[layer]["total"]) if layer in recorded else "-"
        )
        table.add_row(
            str(layer),
            str(p["done"]) if p["total"] else "-",
            str(p["total"]) if p["total"] else "-",
            f"[{color}]{p['pct']}%[/]" if color else f"{p['pct']}%",
            str(p["active"]),
            str(p["ready"]),
            str(p["retries"]),
            f"[red]{p['dead']}[/]" if p["dead"] else "0",
            elapsed(job),
            cost_cell,
        )
    return table


_GLYPH = {
    state.PENDING: "·",
    state.RUNNING: "⟳",
    state.COMPLETE: "[green]✓[/]",
    state.FAILED: "[red]✗[/]",
}


def pid_alive(pid) -> bool:
    """True if a process with `pid` is running (driver liveness for a stalled run)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def _fmt_span(start_ts: float, end_ts: float) -> str:
    minutes = max(0, int(end_ts - start_ts)) // 60
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def stage_summary(cfg_w, rate_table) -> str:
    """One line for a completed stage, from the durable cost record (survives undeploy)."""
    jobs = cost.jobs(cfg_w)
    if not jobs:
        return f"  {cfg_w.workload}: complete"
    layers = sorted({j.layer for j in jobs})
    starts = [j.started_at for j in jobs if j.started_at is not None]
    ends = [j.finished_at for j in jobs if j.finished_at is not None]
    span = _fmt_span(min(starts), max(ends)) if starts and ends else "-"
    spend = ""
    if cfg_w.region and rate_table:
        per_layer, fee = recorded_costs(cfg_w, rate_table)
        total = sum(a["total"] for a in per_layer.values()) + fee
        spend = f"  ~{costs.fmt_dollars(total)}" if per_layer else ""
    return f"  {cfg_w.workload}: complete  layers {layers[0]}-{layers[-1]}  {span}{spend}"


def run_view(cfg, run, order, stage_states, layer_totals=None) -> Group:
    """A run as a DAG header plus per-stage detail: a full table while a stage runs, a
    one-line summary once it's done, a single line while pending. `order` is DAG-ordered."""
    glyphs = "  ".join(
        f"{w} {_GLYPH.get(stage_states.get(w, state.PENDING), '·')}" for w in order
    )
    head = f"{cfg.graph_id}  {glyphs}  ({run.status})"
    if run.status == state.RUNNING and not pid_alive(run.pid):
        head += "  [red](driver not running — re-run deploy to resume)[/]"
    rate_table = costs.load_table()
    parts = [head]
    for w in order:
        st = stage_states.get(w, state.PENDING)
        cfg_w = dataclasses.replace(cfg, workload=w)
        if st == state.RUNNING:
            parts.append(status_table(cfg_w, layer_totals))
        elif st == state.COMPLETE:
            parts.append(stage_summary(cfg_w, rate_table))
        else:
            parts.append(f"  {w}: {st}")
    return Group(*parts)
