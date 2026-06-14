"""Durable per-(graph, workload) record of pod runtimes — the cost source of truth.

Kubernetes garbage-collects finished pods (and their runtimes with them), so any estimate
from live cluster state decays; every sample upserts by uid into the cost database, never
lost. Physical quantities only — dollars are priced at read time from the current rates.
Backend-agnostic: the database is a SQLAlchemy URL (`database.cost`), default local SQLite.
A sample writes absolute cluster state (not deltas), so concurrent samplers of one workload
converge — sampling is best-effort and never blocks the actual run.
"""

from datetime import datetime, timezone

from sqlalchemy import select, update

from .. import kube
from ..costs import parse_cpu, parse_mem
from .base import session
from .models import CostBase, Job, Pod

DEFAULT_URL = "sqlite:///costs/cost.db"
_CLASS_KEY = "cloud.google.com/compute-class"


def _session(cfg):
    return session(cfg.database.get("cost") or DEFAULT_URL, CostBase)


def _ts(dt) -> float | None:
    return dt.timestamp() if dt else None


def _terminal_ts(status) -> float | None:
    """k8s sets completionTime only on success; Failed Jobs end at the condition."""
    for c in status.conditions or []:
        if c.type in ("Complete", "Failed") and c.status == "True":
            return _ts(c.last_transition_time)
    return None


def record(s, cfg, job, pods, now: float) -> None:
    """Upsert one Job and its visible pods; freeze pods that vanished."""
    spec = job.spec.template.spec
    req = (spec.containers[0].resources.requests or {}) if spec.containers else {}
    st = job.status
    layer = int((job.metadata.labels or {}).get("layer", 0))
    run_id = (job.metadata.annotations or {}).get("run-id", "")
    cpu, mem = parse_cpu(req.get("cpu")), parse_mem(req.get("memory"))
    row = s.get(Job, job.metadata.uid)
    if row is None:
        row = Job(
            job_uid=job.metadata.uid,
            graph=cfg.graph_id,
            workload=cfg.workload,
            run_id=run_id,
        )
        s.add(row)
    if row.started_at is None:  # keep the first start ever seen
        row.started_at = _ts(st.start_time)
    row.name, row.layer = job.metadata.name, layer
    row.compute_class = (spec.node_selector or {}).get(_CLASS_KEY, "")
    row.cpu_req, row.mem_req = cpu, mem
    row.finished_at = _ts(st.completion_time) or _terminal_ts(st)
    row.completions = job.spec.completions
    row.succeeded, row.failed, row.active = (
        st.succeeded or 0,
        st.failed or 0,
        st.active or 0,
    )
    row.last_seen = now
    row.parallelism = max(row.parallelism or 0, job.spec.parallelism or 0)
    seen = []
    for pod in pods:
        statuses = pod.status.container_statuses or []
        term = statuses[0].state.terminated if statuses and statuses[0].state else None
        # bill from node-bound start, never creation: Pending time is not billed
        started = (_ts(term.started_at) if term else None) or _ts(pod.status.start_time)
        finished = _ts(term.finished_at) if term else None
        phase = pod.status.phase or ""
        if finished is None and phase in ("Succeeded", "Failed"):
            finished = now  # terminal pod without container state: stop accruing
        seen.append(pod.metadata.uid)
        prow = s.get(Pod, pod.metadata.uid)
        if prow is None:
            prow = Pod(
                pod_uid=pod.metadata.uid,
                graph=cfg.graph_id,
                workload=cfg.workload,
                run_id=run_id,
                job_uid=job.metadata.uid,
            )
            s.add(prow)
        if prow.started_at is None:
            prow.started_at = started
        prow.layer, prow.cpu_req, prow.mem_req = layer, cpu, mem
        if finished is not None:
            prow.finished_at = finished
        elif phase in ("Pending", "Running"):
            prow.finished_at = None  # a re-listed live pod un-freezes
        prow.last_seen, prow.phase = now, phase
    _freeze_gone(s, cfg, job.metadata.uid, seen)


def _freeze_gone(s, cfg, job_uid, seen) -> None:
    """Pods of this job no longer listed were GC'd: freeze runtime, mark 'Gone' so the
    backfill never re-bills the consumed completion."""
    where = [
        Pod.graph == cfg.graph_id,
        Pod.workload == cfg.workload,
        Pod.job_uid == job_uid,
        Pod.finished_at.is_(None),
    ]
    # with no pods listed the NOT IN is dropped, freezing every unfinished pod of this job
    if seen:
        where.append(Pod.pod_uid.notin_(seen))
    s.execute(update(Pod).where(*where).values(finished_at=Pod.last_seen, phase="Gone"))


def _close_out_unlisted(s, cfg, listed) -> None:
    """Jobs/pods deleted/replaced between samples stop accruing (this graph+workload)."""
    pod_where = [
        Pod.graph == cfg.graph_id,
        Pod.workload == cfg.workload,
        Pod.finished_at.is_(None),
    ]
    job_where = [
        Job.graph == cfg.graph_id,
        Job.workload == cfg.workload,
        Job.finished_at.is_(None),
    ]
    # with nothing listed the NOT IN is dropped, closing out every unfinished row in scope
    if listed:
        pod_where.append(Pod.job_uid.notin_(listed))
        job_where.append(Job.job_uid.notin_(listed))
    s.execute(
        update(Pod).where(*pod_where).values(finished_at=Pod.last_seen, phase="Gone")
    )
    s.execute(update(Job).where(*job_where).values(finished_at=Job.last_seen, active=0))


def sample(cfg) -> None:
    """Record the workload's Jobs + pods right now; best-effort, never raises."""
    try:
        now = datetime.now(timezone.utc).timestamp()
        with _session(cfg) as s:
            listed = []
            for job in kube.list_jobs(cfg.namespace, cfg.workload):
                record(s, cfg, job, kube.pods_of(cfg.namespace, job.metadata.name), now)
                listed.append(job.metadata.uid)
            _close_out_unlisted(s, cfg, listed)
    except Exception:  # noqa: BLE001 - cost is auxiliary, never fatal
        pass


def jobs(cfg) -> list[Job]:
    """Recorded Jobs for this (graph, workload), ordered by layer."""
    with _session(cfg) as s:
        stmt = select(Job).where(Job.graph == cfg.graph_id, Job.workload == cfg.workload)
        return list(s.scalars(stmt.order_by(Job.layer)))


def pods(cfg, job_uid: str) -> list[Pod]:
    with _session(cfg) as s:
        stmt = select(Pod).where(
            Pod.graph == cfg.graph_id,
            Pod.workload == cfg.workload,
            Pod.job_uid == job_uid,
        )
        return list(s.scalars(stmt))
