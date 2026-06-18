"""Helper functions for the pipeline CLI."""

import contextlib
import dataclasses
import json
import os
from datetime import datetime, timezone

import yaml
from rich.console import Group
from rich.table import Table
from rich.text import Text

from . import cgcache, costs, kube, manifest, note
from .db import cost, state

# Cold ChunkedGraph init fits in 30s with headroom; the warm cg-cache server pays it
# once at boot, so every later probe returns well within this.
CG_TIMEOUT = 30


def ceil_div(a, b):
    return -(-a // b)


def run_workload(cfg, name, argv, timeout=300):
    """Run the active workload's command in its image (cfg.image()): the warm util pod
    if one is deployed (logs streamed live), else a one-shot pod (log on completion)."""
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


def _query_meta(cfg, op, gid, quiet=False) -> str:
    """Run a cg meta probe ('counts'|'mesh') and return its stdout: the persistent util
    pod's warm cg-cache server over its socket, else a one-shot pod that imports cg
    inline. `quiet` drops the live line echo (the failure reason still rides the raised
    SystemExit) so best-effort callers present their own one-line summary instead."""
    name = {"counts": "layer-counts", "mesh": "mesh-check"}[op]
    echo = None if quiet else (lambda ln: note(f"  [{name}] {ln}"))
    if cfg.persistent_util:
        pod = kube.util_pod(cfg.namespace)
        if not quiet:
            note(f"{name}: in util pod")
        argv = ["python", "-u", "-c", cgcache.CLIENT_SRC, cgcache.CG_SOCK, op, gid]
        argv.append(str(CG_TIMEOUT))  # the client's own connect-retry deadline
        # exec cap exceeds that deadline so the client's 'unreachable' message wins
        return kube.exec_cmd(cfg.namespace, pod, argv, timeout=CG_TIMEOUT + 5, on_line=echo)
    if not quiet:
        note(f"{name}: in one-shot pod")
    argv = ["python", "-u", "-c", cgcache.ONESHOT_SRC, op, gid]
    # the probe reads the ChunkedGraph, so it runs in the PCG image whatever workload we're in
    return kube.run_oneshot(
        cfg.namespace, manifest.oneshot_pod_spec(cfg, name, argv, image=cfg.images.pcg)
    )


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
    # cache writes are best-effort: a disk hiccup must never abort a submit
    with contextlib.suppress(OSError), open(_counts_cache(cfg), "w") as f:
        json.dump(cache, f)


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


def read_layer_counts(cfg, quiet=False) -> dict:
    """{layer: chunk_count} for every layer (L2..root). Cached locally after the first
    read, so ChunkedGraph is initialized at most once per graph. Any read failure raises;
    `quiet` suppresses the in-pod line echo (the reason rides the SystemExit)."""
    cache = _read_cache(cfg)
    if cfg.graph_id in cache:
        return {int(k): v for k, v in cache[cfg.graph_id].items()}
    out = _query_meta(cfg, "counts", cfg.graph_id, quiet=quiet)
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


def try_layer_counts(cfg):
    """Best-effort warm-up for `status`: cached counts, else a quiet probe. A failure
    (graph not built yet, pipeline torn down) prints one clean line — never a remote
    traceback — and returns None so the caller renders the bare table."""
    cached = cached_layer_counts(cfg)
    if cached is not None:
        return cached
    try:
        return read_layer_counts(cfg, quiet=True)
    except (SystemExit, Exception) as exc:  # noqa: BLE001 - status must never crash
        lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
        note(f"layer-counts unavailable: {lines[-1] if lines else type(exc).__name__}")
        return None


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


def _pod_terminated(pod):
    """The container's terminal state (current or last), or None — holds the exit reason."""
    for cs in pod.status.container_statuses or []:
        term = (cs.state and cs.state.terminated) or (cs.last_state and cs.last_state.terminated)
        if term:
            return term
    return None


def pod_status(pod) -> str:
    """Phase plus the container's terminal reason/exit, e.g. 'Failed OOMKilled (137)'."""
    term = _pod_terminated(pod)
    if term and (term.reason or term.exit_code is not None):
        return f"{pod.status.phase} {term.reason or 'exit'} ({term.exit_code})"
    return pod.status.phase


def pod_reason(pod) -> str:
    """Why a pod isn't progressing: unschedulable message, container waiting reason, or exit."""
    for c in pod.status.conditions or []:
        if c.type == "PodScheduled" and c.status != "True":
            return f"{c.reason}: {c.message}".strip(": ")
    for cs in pod.status.container_statuses or []:
        waiting = cs.state and cs.state.waiting
        if waiting:
            return f"{waiting.reason}: {waiting.message or ''}".strip(": ")
    term = _pod_terminated(pod)
    return f"{term.reason or 'exit'} ({term.exit_code})" if term else ""


def relevant_log(text: str, n: int = 40) -> str:
    """Pod log narrowed for failure inspection: from the Python traceback onward (the
    actual failure) when present, else the last `n` lines. Pod-log noise is dropped."""
    lines = [
        ln
        for ln in text.splitlines()
        if ln.strip() and not any(s in ln for s in kube.LOG_NOISE)
    ]
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.lstrip().startswith("Traceback (most recent call last)")
        ),
        max(0, len(lines) - n),
    )
    return "\n".join(lines[start:])


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


def recorded_costs(cfg, rate_table, run_id) -> tuple:
    """({layer: priced totals}, cluster fee) for one run of this (graph, workload)."""
    per_layer = {}
    now = datetime.now(timezone.utc).timestamp()
    jobs = cost.jobs(cfg, run_id, cfg.workload)
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


def _job_cost(cfg, rate_table, job, now) -> tuple:
    """(usage, priced, pods) for one recorded Job; priced is zeros when no rate applies."""
    pods = cost.pods(cfg, job.job_uid)
    usage = costs.usage_from_rows(job, pods, now)
    rate = costs.rate_for(rate_table, cfg.region, job.compute_class)
    priced = (
        costs.price_usage(usage, rate) if rate else {"cpu": 0.0, "mem": 0.0, "total": 0.0}
    )
    return usage, priced, pods


def _run_start(jobs) -> float:
    """Earliest start of a run's jobs (0.0 if none started), for newest-first ordering."""
    starts = [j.started_at for j in jobs if j.started_at is not None]
    return min(starts) if starts else 0.0


def _fmt_started(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "-"
    )


def runs_table(cfg, rate_table, graph=None) -> Table:
    """One row per recorded run (newest first): run-id, graph, workloads, layer span,
    started, total cost. Spans the durable cost db, so it outlives undeploy."""
    now = datetime.now(timezone.utc).timestamp()
    groups = {}
    for j in cost.runs(cfg, graph):
        groups.setdefault((j.graph, j.run_id), []).append(j)
    has_rates = bool(cfg.region) and bool(rate_table)
    table = Table(title=f"recorded runs{f' | {graph}' if graph else ''}")
    for col in ("run", "graph", "workloads", "layers", "started", "compute_cost"):
        table.add_column(col, justify="right")
    for (g, run_id), js in sorted(
        groups.items(), key=lambda kv: _run_start(kv[1]), reverse=True
    ):
        layers = sorted({j.layer for j in js})
        table.add_row(
            run_id or "(ad-hoc)",
            g,
            ",".join(sorted({j.workload for j in js})),
            f"{layers[0]}-{layers[-1]}" if layers else "-",
            _fmt_started(_run_start(js)),
            costs.fmt_dollars(_price_jobs(cfg, rate_table, js, now))
            if has_rates
            else "-",
        )
    return table


def _price_jobs(cfg, rate_table, jobs, now) -> float:
    """Total Spot dollars over `jobs` (each priced from its pods) + the one-time cluster fee."""
    total = sum(_job_cost(cfg, rate_table, j, now)[1]["total"] for j in jobs)
    return total + costs.fee(rate_table, cfg.region, jobs, now)


def run_breakdown(cfg, rate_table, run_id) -> Table:
    """One run's recorded cost by (workload, layer): requests, pods, succeeded/failed,
    runtime, priced cost, and the backfill basis. Spans the durable cost db."""
    now = datetime.now(timezone.utc).timestamp()
    jobs = cost.run_jobs(cfg, run_id)
    rows = {}
    for j in jobs:
        usage, priced, pods = _job_cost(cfg, rate_table, j, now)
        agg = rows.setdefault(
            (j.workload, j.layer),
            {
                "cpu": j.cpu_req,
                "mem": j.mem_req,
                "pods": 0,
                "ok": 0,
                "fail": 0,
                "pod_hours": 0.0,
                "total": 0.0,
                "basis": set(),
            },
        )
        agg["pods"] += len(pods)
        agg["ok"] += j.succeeded or 0
        agg["fail"] += j.failed or 0
        agg["pod_hours"] += usage["pod_hours"]
        agg["total"] += priced["total"]
        agg["basis"].add(usage["basis"])
    has_rates = bool(cfg.region) and bool(rate_table)
    fee = costs.fee(rate_table, cfg.region, jobs, now) if has_rates else 0.0
    grand = sum(a["total"] for a in rows.values()) + fee
    table = Table(
        title=f"run {run_id}",
        caption=f"total ~{costs.fmt_dollars(grand)} (incl. cluster fee {costs.fmt_dollars(fee)})"
        if has_rates
        else "no cost rates (set `region`)",
    )
    for col in (
        "workload",
        "layer",
        "cpu",
        "mem",
        "pods",
        "ok",
        "fail",
        "pod-hr",
        "compute_cost",
        "basis",
    ):
        table.add_column(col, justify="right")
    for w, layer in sorted(rows):
        a = rows[(w, layer)]
        table.add_row(
            w,
            str(layer),
            f"{a['cpu']:g}",
            f"{a['mem']:g}Gi",
            str(a["pods"]),
            str(a["ok"]),
            str(a["fail"]),
            f"{a['pod_hours']:.1f}",
            costs.fmt_dollars(a["total"]) if has_rates else "-",
            "+".join(sorted(a["basis"])),
        )
    return table


def usage_table(cfg, job_name, layer) -> Table:
    """Per-pod usage for one layer Job, in cores/GiB, ordered by task index."""
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


def status_table(cfg, layer_totals=None, run_id="") -> Group:
    """Per-layer progress. With `layer_totals` ({layer: chunks}), every layer is shown —
    submitted ones with live progress, the rest with their a-priori total (pending).
    The cost column is scoped to `run_id` (the current run; "" for ad-hoc/no run)."""
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
    # node_summary changes every refresh; keep it one non-wrapping line so the table
    # height stays constant — a title that wraps differently per frame leaves rich's
    # Live unable to erase the taller previous frame (the stacked-header bug).
    header = Text(
        f"{cfg.workload} | {cfg.graph_id} | {nodes}", no_wrap=True, overflow="ellipsis"
    )
    table = Table(
        caption="retries = failed attempts (transient) | failed = dead tasks (`inspect`) | "
        "active−ready ≈ pods waiting on capacity | compute_cost = recorded Spot estimate",
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
        "compute_cost",
    )
    for col in cols:
        table.add_column(col, justify="right")
    rate_table = costs.load_table()
    recorded = {}
    if cfg.region and rate_table:
        with contextlib.suppress(Exception):  # cost is auxiliary, never fatal
            recorded, _ = recorded_costs(cfg, rate_table, run_id)
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
    return Group(header, table)


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


def stage_summary(cfg_w, rate_table, run_id="") -> str:
    """One line for a completed stage of `run_id`, from the durable cost record."""
    jobs = cost.jobs(cfg_w, run_id, cfg_w.workload)
    if not jobs:
        return f"  {cfg_w.workload}: complete"
    layers = sorted({j.layer for j in jobs})
    starts = [j.started_at for j in jobs if j.started_at is not None]
    ends = [j.finished_at for j in jobs if j.finished_at is not None]
    span = _fmt_span(min(starts), max(ends)) if starts and ends else "-"
    spend = ""
    if cfg_w.region and rate_table:
        per_layer, fee = recorded_costs(cfg_w, rate_table, run_id)
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
            parts.append(status_table(cfg_w, layer_totals, run.run_id))
        elif st == state.COMPLETE:
            parts.append(stage_summary(cfg_w, rate_table, run.run_id))
        else:
            parts.append(f"  {w}: {st}")
    return Group(*parts)
