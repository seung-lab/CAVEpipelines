"""ORM models for the cost database — durable per-(graph, workload) pod runtimes.

Every row is scoped by `graph`+`workload`, so a single database holds every graph and
workload (a server stores them this way; local SQLite is just one such backend).
"""

from sqlalchemy import String
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
    job_uid: Mapped[str] = mapped_column(_ID, index=True)
    layer: Mapped[int]
    cpu_req: Mapped[float]
    mem_req: Mapped[float]
    started_at: Mapped[float | None]
    finished_at: Mapped[float | None]
    last_seen: Mapped[float]
    phase: Mapped[str] = mapped_column(_ID)
