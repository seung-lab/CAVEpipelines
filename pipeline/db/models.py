"""ORM models for the cost and state databases (separate dbs, one base each).

Cost (`Job`/`Pod`) is the durable per-(graph, workload) record of pod runtimes; state
(`Run`/`Stage`) is the ephemeral record of the active run. Every row is scoped by graph (and
workload), so a single database holds every graph — a server stores them this way; local
SQLite is just one such backend.
"""

import json

from sqlalchemy import String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# bound so the DDL compiles on every backend (k8s names <= 253, uids 36)
_ID = String(255)


class CostBase(DeclarativeBase):
    pass


class Job(CostBase):
    """One layer Job; one row per uid (a re-submit is a new uid, so history accrues)."""

    __tablename__ = "jobs"

    job_uid: Mapped[str] = mapped_column(_ID, primary_key=True)
    graph: Mapped[str] = mapped_column(_ID, index=True)
    workload: Mapped[str] = mapped_column(_ID)
    run_id: Mapped[str] = mapped_column(_ID, index=True, default="")
    name: Mapped[str] = mapped_column(_ID)
    layer: Mapped[int]
    compute_class: Mapped[str] = mapped_column(_ID, default="")
    cpu_req: Mapped[float]
    mem_req: Mapped[float]
    started_at: Mapped[float | None]
    finished_at: Mapped[float | None]
    completions: Mapped[int | None]
    succeeded: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    active: Mapped[int] = mapped_column(default=0)
    last_seen: Mapped[float]
    parallelism: Mapped[int] = mapped_column(default=0)


class Pod(CostBase):
    """One worker pod; `phase` 'Gone' means GC'd, runtime frozen at its last sighting."""

    __tablename__ = "pods"

    pod_uid: Mapped[str] = mapped_column(_ID, primary_key=True)
    graph: Mapped[str] = mapped_column(_ID, index=True)
    workload: Mapped[str] = mapped_column(_ID)
    run_id: Mapped[str] = mapped_column(_ID, index=True, default="")
    job_uid: Mapped[str] = mapped_column(_ID, index=True)
    layer: Mapped[int]
    cpu_req: Mapped[float]
    mem_req: Mapped[float]
    started_at: Mapped[float | None]
    finished_at: Mapped[float | None]
    last_seen: Mapped[float]
    phase: Mapped[str] = mapped_column(_ID)


class StateBase(DeclarativeBase):
    pass


class Run(StateBase):
    """The active run for one graph: its stage set, how it's driven, and its status."""

    __tablename__ = "run"

    graph: Mapped[str] = mapped_column(_ID, primary_key=True)
    run_id: Mapped[str] = mapped_column(_ID, default="")  # unique per deploy invocation
    workloads: Mapped[str] = mapped_column(Text)  # JSON array of stage names
    parallel: Mapped[bool]
    overwrite: Mapped[bool]
    status: Mapped[str] = mapped_column(_ID)  # running | paused | done
    pid: Mapped[int | None]  # the driver process; a dead pid + running = stalled
    updated_at: Mapped[float]

    @property
    def stage_set(self) -> set[str]:
        return set(json.loads(self.workloads))


class Stage(StateBase):
    """One stage's progress within the run: pending | running | complete | failed."""

    __tablename__ = "stages"

    graph: Mapped[str] = mapped_column(_ID, primary_key=True)
    workload: Mapped[str] = mapped_column(_ID, primary_key=True)
    state: Mapped[str] = mapped_column(_ID)
    updated_at: Mapped[float]
