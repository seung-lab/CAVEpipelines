"""Price recorded pod runtimes against GKE Autopilot rates.

Autopilot bills pod resource *requests* (not usage) per second; for pod-based
compute classes (general-purpose / Balanced / Scale-Out) the node machine type is
irrelevant, so recorded quantity-hours x the class rate is structurally exact.
The runtimes come from the local cost db samples — never from live cluster state,
which Kubernetes garbage-collects. This is an estimate, not the invoice.
"""

from . import rates

_GENERAL = "general-purpose"  # GKE default compute class (empty compute_class maps here)

# Autopilot billing grid for the default (general-purpose) class — platform facts,
# per cloud.google.com/kubernetes-engine/docs/concepts/autopilot-resource-requests
CPU_STEP = 0.25  # non-bursting clusters round CPU requests UP to this
MEM_PER_CPU = (1.0, 6.5)  # billable memory:cpu window, GiB per vCPU
GP_MIN = (0.25, 0.5)  # smallest billable pod (vCPU, GiB)
GP_MAX = (30.0, 110.0)  # class ceiling; above needs a different compute class
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


def normalize_requests(cpu: float, mem: float, compute_class: str = "") -> tuple:
    """Snap requests to the cheapest valid Autopilot point >= the ask.

    Autopilot silently rounds invalid requests up and bills the result; doing it
    explicitly keeps cost estimates true. Returns (cpu, mem, warnings); over-ceiling
    requests are clamped (with a warning), not rejected. Only the default class's grid
    is modeled — other classes pass through untouched."""
    warnings = []
    if compute_class:
        return cpu, mem, warnings
    if cpu < GP_MIN[0]:
        warnings.append(
            f"cpu {cpu:g} below the Autopilot minimum; billed as {GP_MIN[0]:g}"
        )
        cpu = GP_MIN[0]
    if mem < GP_MIN[1]:
        warnings.append(f"memory {mem:g}Gi below the minimum; billed as {GP_MIN[1]:g}Gi")
        mem = GP_MIN[1]
    if mem < cpu * MEM_PER_CPU[0]:
        warnings.append(
            f"memory raised {mem:g}Gi -> {cpu * MEM_PER_CPU[0]:g}Gi "
            f"({MEM_PER_CPU[0]:g} GiB/vCPU billing floor)"
        )
        mem = cpu * MEM_PER_CPU[0]
    if mem > cpu * MEM_PER_CPU[1]:
        new_cpu = mem / MEM_PER_CPU[1]
        warnings.append(
            f"cpu raised {cpu:g} -> {new_cpu:g} "
            f"(memory exceeds {MEM_PER_CPU[1]:g} GiB/vCPU billing ceiling)"
        )
        cpu = new_cpu
    if (cpu / CPU_STEP) % 1:
        warnings.append(
            f"cpu {cpu:g} off the {CPU_STEP:g}-vCPU billing step; "
            f"non-bursting clusters round it up"
        )
    if cpu > GP_MAX[0]:
        warnings.append(
            f"cpu {cpu:g} clamped to the {GP_MAX[0]:g}-vCPU general-purpose ceiling; "
            f"set job.compute_class for more"
        )
        cpu = GP_MAX[0]
    if mem > GP_MAX[1]:
        warnings.append(
            f"memory {mem:g}Gi clamped to the {GP_MAX[1]:g}Gi general-purpose ceiling; "
            f"set job.compute_class for more"
        )
        mem = GP_MAX[1]
    return cpu, mem, warnings


def load_table() -> dict:
    """Per-(region, class) rate table; {} on any failure (cost is auxiliary, never fatal)."""
    try:
        return rates.load()
    except Exception:  # noqa: BLE001 - cost is auxiliary, never fatal
        return {}


def rate_for(table: dict, region: str, compute_class: str):
    """Rate dict for (region, compute_class), or None. Empty class -> general-purpose."""
    return table.get(region, {}).get(compute_class or _GENERAL)


def usage_from_rows(job, pods, now: float) -> dict:
    """Quantity-hours for one recorded Job; backfills pods that were never observed.

    Observed pods accrue started->finished (or ->now while live). A closed-out
    'Gone' pod consumes a completion like a Succeeded one, so it is never billed
    again by the backfill. Completions never sampled get the mean observed
    runtime; with no pod record at all, fall back to job wall x workers."""
    cpu_h = mem_h = pod_h = 0.0
    durations = []
    succeeded_seen = 0
    for p in pods:
        if p.started_at is None:
            continue
        hours = max(0.0, (p.finished_at or now) - p.started_at) / 3600
        pod_h += hours
        cpu_h += p.cpu_req * hours
        mem_h += p.mem_req * hours
        if p.finished_at:
            durations.append(hours)
        if p.phase in ("Succeeded", "Gone"):
            succeeded_seen += 1
    basis = "observed"
    missing = max(0, (job.succeeded or 0) - succeeded_seen)
    if missing and durations:
        mean = sum(durations) / len(durations)
        pod_h += missing * mean
        cpu_h += missing * job.cpu_req * mean
        mem_h += missing * job.mem_req * mean
        basis = "observed+backfill"
    elif pod_h == 0.0 and job.started_at is not None:
        wall = max(0.0, (job.finished_at or now) - job.started_at) / 3600
        count = (job.succeeded or 0) + (job.active or 0)
        # tasks never run all at once; true pod-hours <= wall x peak workers
        workers = min(job.parallelism or count, count)
        pod_h = wall * workers
        cpu_h = job.cpu_req * pod_h
        mem_h = job.mem_req * pod_h
        basis = "wall"
    return {
        "cpu_hours": cpu_h,
        "mem_gib_hours": mem_h,
        "pod_hours": pod_h,
        "basis": basis,
    }


def price_usage(usage: dict, rate: dict) -> dict:
    """Spot dollars for quantity-hours at one (region, class) rate — no cluster fee."""
    cpu = usage["cpu_hours"] * rate["cpu_spot"]
    mem = usage["mem_gib_hours"] * rate["mem_spot"]
    return {"cpu": cpu, "mem": mem, "total": cpu + mem}


def union_hours(intervals) -> float:
    """Total hours covered by possibly-overlapping (start, end) second spans."""
    total = 0.0
    end_max = None
    for start, end in sorted(intervals):
        if end_max is None or start > end_max:
            total += end - start
            end_max = end
        elif end > end_max:
            total += end - end_max
            end_max = end
    return total / 3600


def fee(table: dict, region: str, jobs, now: float) -> float:
    """Cluster fee over the union of job wall spans — charged once, never per layer."""
    rate = next(iter(table.get(region, {}).values()), None)
    if not rate:
        return 0.0
    spans = [
        (j.started_at, j.finished_at or now) for j in jobs if j.started_at is not None
    ]
    return rate["cluster_fee_hr"] * union_hours(spans)


def fmt_dollars(value: float) -> str:
    """Dollar string with sub-cent resolution below $1, so slow accrual stays visible."""
    return f"${value:.2f}" if value >= 1 else f"${value:.3f}"


def format_cost(est: dict) -> str:
    return (
        f"~{fmt_dollars(est['total'])} (cpu {fmt_dollars(est['cpu'])} + "
        f"mem {fmt_dollars(est['mem'])}; {est['pod_hours']:.1f} pod-hr, "
        f"{est['basis']}; estimate, excl. cluster fee)"
    )
