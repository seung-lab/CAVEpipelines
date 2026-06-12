"""Estimate GKE Autopilot Spot cluster cost for a layer from pod requests x runtime.

Autopilot bills pod resource *requests* (not usage) per second; for pod-based compute classes
(general-purpose / Balanced / Scale-Out) the node machine type is irrelevant, so requests x lifetime
x the class rate is structurally exact. Node-based classes (Performance / GPU) bill per VM and are
absent from the rate table, so they aren't priced. This is an estimate, not the invoice.
"""

from datetime import datetime, timezone

from . import rates

_GENERAL = "general-purpose"  # GKE default compute class (empty compute_class maps here)
_CLASS_KEY = "cloud.google.com/compute-class"
_MEM_UNITS = {
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
}


def parse_cpu(value) -> float:
    """k8s CPU quantity -> vCPU cores ('500m' -> 0.5, '8913484669n' -> 8.9)."""
    text = str(value or 0)
    for suffix, div in (("n", 1e9), ("u", 1e6), ("m", 1e3)):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) / div
    return float(text)


def parse_mem(value) -> float:
    """k8s memory quantity -> GiB ('2Gi' -> 2.0, '512Mi' -> 0.5)."""
    text = str(value or 0)
    for unit, mult in _MEM_UNITS.items():
        if text.endswith(unit):
            return float(text[: -len(unit)]) * mult / 2**30
    return float(text) / 2**30  # bare bytes


def load_table() -> dict:
    """Per-(region, class) rate table; {} on any failure (cost is auxiliary, never fatal)."""
    try:
        return rates.load()
    except Exception:  # noqa: BLE001 - cost is auxiliary, never fatal
        return {}


def rate_for(table: dict, region: str, compute_class: str):
    """Rate dict for (region, compute_class), or None. Empty class -> general-purpose."""
    return table.get(region, {}).get(compute_class or _GENERAL)


def _container_requests(job):
    container = job.spec.template.spec.containers[0]
    req = (container.resources.requests if container.resources else None) or {}
    return parse_cpu(req.get("cpu")), parse_mem(req.get("memory"))


def _compute_class(job) -> str:
    return (job.spec.template.spec.node_selector or {}).get(_CLASS_KEY, "")


def _wall_hours(job) -> float:
    start = job.status.start_time
    if not start:
        return 0.0
    end = job.status.completion_time or datetime.now(timezone.utc)
    return max(0.0, (end - start).total_seconds()) / 3600


def _pod_hours(pods) -> float:
    """Sum of per-pod lifetimes in hours (container started->finished, else creation->now)."""
    now = datetime.now(timezone.utc)
    total = 0.0
    for pod in pods:
        start, end = pod.metadata.creation_timestamp, now
        statuses = pod.status.container_statuses or []
        term = statuses[0].state.terminated if statuses and statuses[0].state else None
        if term:
            start = term.started_at or start
            end = term.finished_at or now
        if start:
            total += max(0.0, (end - start).total_seconds()) / 3600
    return total


def estimate_job_cost(job, pods, table: dict, region: str, *, spot: bool = True) -> dict:
    """Cost estimate for one layer Job — never raises; {error} for node-based/missing rates."""
    try:
        cls = _compute_class(job)
        rate = rate_for(table, region, cls)
        if rate is None:
            return {
                "error": f"no rate for class '{cls or _GENERAL}' in region '{region}' "
                f"(node-based class or region missing)"
            }
        cpu, mem = _container_requests(job)
        wall = _wall_hours(job)
        pod_hours, basis = _pod_hours(pods), "per-pod"
        if not pods:  # pods garbage-collected -> fall back to wall-clock x pod count
            pod_hours = wall * ((job.status.succeeded or 0) + (job.status.active or 0))
            basis = "wall x pods"
        cpu_cost = cpu * rate["cpu_spot" if spot else "cpu_on_demand"] * pod_hours
        mem_cost = mem * rate["mem_spot" if spot else "mem_on_demand"] * pod_hours
        fee = rate["cluster_fee_hr"] * wall
        return {
            "total": cpu_cost + mem_cost + fee,
            "cpu": cpu_cost,
            "mem": mem_cost,
            "fee": fee,
            "pod_hours": pod_hours,
            "basis": basis,
        }
    except Exception as exc:  # noqa: BLE001 - cost is auxiliary, never fatal
        return {"error": f"cost error: {exc}"}


def fmt_dollars(value: float) -> str:
    """Dollar string with sub-cent resolution below $1, so slow accrual stays visible."""
    return f"${value:.2f}" if value >= 1 else f"${value:.3f}"


def format_cost(est: dict) -> str:
    if "error" in est:
        return est["error"]
    return (
        f"~{fmt_dollars(est['total'])} (cpu {fmt_dollars(est['cpu'])} + "
        f"mem {fmt_dollars(est['mem'])} + cluster {fmt_dollars(est['fee'])}; "
        f"{est['pod_hours']:.1f} pod-hr, {est['basis']}; estimate)"
    )
