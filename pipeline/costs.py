"""Price recorded pod runtimes against GKE Autopilot rates.

Autopilot bills pod resource *requests* (not usage) per second; for pod-based
compute classes (general-purpose / Balanced / Scale-Out) the node machine type is
irrelevant, so recorded quantity-hours x the class rate is structurally exact.
The runtimes come from the local costdb samples — never from live cluster state,
which Kubernetes garbage-collects. This is an estimate, not the invoice.
"""

from . import rates

_GENERAL = "general-purpose"  # GKE default compute class (empty compute_class maps here)
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


def usage_from_rows(job_row, pod_rows, now: float) -> dict:
    """Quantity-hours for one recorded Job; backfills pods that were never observed.

    Observed pods accrue started->finished (or ->now while live). Completions never
    sampled get the mean observed runtime; with no pod record at all, fall back to
    job wall x pod count. The basis flag says which."""
    cpu_h = mem_h = pod_h = 0.0
    durations = []
    succeeded_seen = 0
    for p in pod_rows:
        if p["started_at"] is None:
            continue
        hours = max(0.0, (p["finished_at"] or now) - p["started_at"]) / 3600
        pod_h += hours
        cpu_h += p["cpu_req"] * hours
        mem_h += p["mem_req"] * hours
        if p["finished_at"]:
            durations.append(hours)
        if p["phase"] == "Succeeded":
            succeeded_seen += 1
    basis = "observed"
    missing = max(0, (job_row["succeeded"] or 0) - succeeded_seen)
    if missing and durations:
        mean = sum(durations) / len(durations)
        pod_h += missing * mean
        cpu_h += missing * job_row["cpu_req"] * mean
        mem_h += missing * job_row["mem_req"] * mean
        basis = "observed+backfill"
    elif pod_h == 0.0 and job_row["started_at"] is not None:
        wall = max(0.0, (job_row["finished_at"] or now) - job_row["started_at"]) / 3600
        pod_h = wall * ((job_row["succeeded"] or 0) + (job_row["active"] or 0))
        cpu_h = job_row["cpu_req"] * pod_h
        mem_h = job_row["mem_req"] * pod_h
        basis = "wall"
    return {
        "cpu_hours": cpu_h,
        "mem_gib_hours": mem_h,
        "pod_hours": pod_h,
        "basis": basis,
    }


def price_usage(usage: dict, rate: dict, *, spot: bool = True) -> dict:
    """Dollars for quantity-hours at one (region, class) rate — no cluster fee."""
    cpu = usage["cpu_hours"] * rate["cpu_spot" if spot else "cpu_on_demand"]
    mem = usage["mem_gib_hours"] * rate["mem_spot" if spot else "mem_on_demand"]
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


def fee(table: dict, region: str, job_rows, now: float) -> float:
    """Cluster fee over the union of job wall spans — charged once, never per layer."""
    rate = next(iter(table.get(region, {}).values()), None)
    if not rate:
        return 0.0
    spans = [
        (j["started_at"], j["finished_at"] or now)
        for j in job_rows
        if j["started_at"] is not None
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
