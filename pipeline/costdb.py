"""Local, durable record of pod runtimes per layer Job — the cost source of truth.

Kubernetes garbage-collects finished pods (and their runtimes with them), so any
estimate from live cluster state decays; every sample here is an upsert keyed by
uid into costs/<graph>.<workload>.db, never lost. Physical quantities only —
dollars are computed at read time from the current rates table.
"""

import os
import sqlite3
from datetime import datetime, timezone

from . import kube
from .costs import parse_cpu, parse_mem

COSTS_DIR = "costs"
_CLASS_KEY = "cloud.google.com/compute-class"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_uid       TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  layer         INTEGER NOT NULL,
  compute_class TEXT NOT NULL DEFAULT '',
  cpu_req       REAL NOT NULL,
  mem_req       REAL NOT NULL,
  started_at    REAL,
  finished_at   REAL,
  completions   INTEGER,
  succeeded     INTEGER NOT NULL DEFAULT 0,
  failed        INTEGER NOT NULL DEFAULT 0,
  active        INTEGER NOT NULL DEFAULT 0,
  last_seen     REAL NOT NULL,
  parallelism   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pods (
  pod_uid     TEXT PRIMARY KEY,
  job_uid     TEXT NOT NULL,
  layer       INTEGER NOT NULL,
  cpu_req     REAL NOT NULL,
  mem_req     REAL NOT NULL,
  started_at  REAL,
  finished_at REAL,
  last_seen   REAL NOT NULL,
  phase       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pods_by_job ON pods (job_uid);
CREATE INDEX IF NOT EXISTS jobs_by_layer ON jobs (layer);
PRAGMA user_version = 2;
"""


def db_path(cfg) -> str:
    return os.path.join(COSTS_DIR, f"{cfg.graph_id}.{cfg.workload}.db")


def connect(cfg) -> sqlite3.Connection:
    os.makedirs(COSTS_DIR, exist_ok=True)
    conn = sqlite3.connect(db_path(cfg))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "parallelism" not in cols:  # v1 -> v2
        conn.execute("ALTER TABLE jobs ADD COLUMN parallelism INTEGER NOT NULL DEFAULT 0")
    return conn


def _ts(dt) -> float:
    return dt.timestamp() if dt else None


def _terminal_ts(status) -> float:
    """k8s sets completionTime only on success; Failed Jobs end at the condition."""
    for c in status.conditions or []:
        if c.type in ("Complete", "Failed") and c.status == "True":
            return _ts(c.last_transition_time)
    return None


def record(conn, job, pods, now: float) -> None:
    """Upsert one Job and its currently-visible pods; close out pods that vanished."""
    spec = job.spec.template.spec
    req = (spec.containers[0].resources.requests or {}) if spec.containers else {}
    s = job.status
    conn.execute(
        """INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_uid) DO UPDATE SET
             started_at=COALESCE(jobs.started_at, excluded.started_at),
             finished_at=excluded.finished_at, completions=excluded.completions,
             succeeded=excluded.succeeded, failed=excluded.failed,
             active=excluded.active, last_seen=excluded.last_seen,
             parallelism=MAX(jobs.parallelism, excluded.parallelism)""",
        (
            job.metadata.uid,
            job.metadata.name,
            int((job.metadata.labels or {}).get("layer", 0)),
            (spec.node_selector or {}).get(_CLASS_KEY, ""),
            parse_cpu(req.get("cpu")),
            parse_mem(req.get("memory")),
            _ts(s.start_time),
            _ts(s.completion_time) or _terminal_ts(s),
            job.spec.completions,
            s.succeeded or 0,
            s.failed or 0,
            s.active or 0,
            now,
            job.spec.parallelism or 0,
        ),
    )
    seen = []
    for pod in pods:
        statuses = pod.status.container_statuses or []
        term = statuses[0].state.terminated if statuses and statuses[0].state else None
        # bill from node-bound start, never creation: Pending time is not billed
        started = (_ts(term.started_at) if term else None) or _ts(pod.status.start_time)
        finished = _ts(term.finished_at) if term else None
        if finished is None and (pod.status.phase or "") in ("Succeeded", "Failed"):
            finished = now  # terminal pod without container state: stop accruing
        seen.append(pod.metadata.uid)
        conn.execute(
            """INSERT INTO pods VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(pod_uid) DO UPDATE SET
                 started_at=COALESCE(pods.started_at, excluded.started_at),
                 finished_at=CASE
                   WHEN excluded.finished_at IS NOT NULL THEN excluded.finished_at
                   WHEN excluded.phase IN ('Pending', 'Running') THEN NULL
                   ELSE pods.finished_at END,
                 last_seen=excluded.last_seen, phase=excluded.phase""",
            (
                pod.metadata.uid,
                job.metadata.uid,
                int((job.metadata.labels or {}).get("layer", 0)),
                parse_cpu(req.get("cpu")),
                parse_mem(req.get("memory")),
                started,
                finished,
                now,
                pod.status.phase or "",
            ),
        )
    # a pod no longer listed was GC'd: freeze its runtime at the last sighting.
    # 'Gone' marks it as a consumed completion so the backfill never re-bills it.
    marks = ",".join("?" * len(seen))
    clause = f"AND pod_uid NOT IN ({marks})" if seen else ""
    conn.execute(
        f"""UPDATE pods SET finished_at = last_seen, phase = 'Gone'
            WHERE job_uid = ? AND finished_at IS NULL {clause}""",
        (job.metadata.uid, *seen),
    )


def _close_out_unlisted(conn, listed_uids, now: float) -> None:
    """Jobs deleted/replaced between samples must stop accruing cost."""
    marks = ",".join("?" * len(listed_uids))
    clause = f"job_uid NOT IN ({marks})" if listed_uids else "1=1"
    conn.execute(
        f"""UPDATE pods SET finished_at = last_seen, phase = 'Gone'
            WHERE finished_at IS NULL AND {clause}""",
        listed_uids,
    )
    conn.execute(
        f"""UPDATE jobs SET finished_at = last_seen, active = 0
            WHERE finished_at IS NULL AND {clause}""",
        listed_uids,
    )


def sample(cfg) -> None:
    """Record the workload's Jobs + pods right now; best-effort, never raises."""
    try:
        now = datetime.now(timezone.utc).timestamp()
        conn = connect(cfg)
        try:
            listed = []
            for job in kube.list_jobs(cfg.namespace, cfg.workload):
                record(conn, job, kube.pods_of(cfg.namespace, job.metadata.name), now)
                listed.append(job.metadata.uid)
            _close_out_unlisted(conn, listed, now)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - cost is auxiliary, never fatal
        pass


def job_rows(conn) -> list:
    return conn.execute("SELECT * FROM jobs ORDER BY layer").fetchall()


def pod_rows(conn, job_uid: str) -> list:
    return conn.execute("SELECT * FROM pods WHERE job_uid = ?", (job_uid,)).fetchall()
