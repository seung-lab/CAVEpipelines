"""Ephemeral per-graph record of the active run and each stage's progress.

The driver writes it and `status` reads it; `undeploy` clears it. Backend-agnostic: a
SQLAlchemy URL (`database.state`), default local SQLite, separate from the durable cost db.
Progress writes are best-effort (a state hiccup never aborts a running workload); `start_run`
surfaces a real failure, so an untrackable run never starts; reads propagate for the caller
to degrade.
"""

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select

from .base import session
from .models import Run, Stage, StateBase

DEFAULT_URL = "sqlite:///costs/state.db"

PENDING, RUNNING, COMPLETE, FAILED = "pending", "running", "complete", "failed"
DONE, PAUSED = "done", "paused"  # run-level statuses (RUNNING also marks an active run)


def _session(cfg):
    return session(cfg.database.get("state") or DEFAULT_URL, StateBase)


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def start_run(cfg, run_set, parallel, overwrite=False, pid=None) -> None:
    """Open the run: one Run row (status running) + every stage seeded pending."""
    now = _now()
    with _session(cfg) as s:
        s.merge(
            Run(
                graph=cfg.graph_id,
                workloads=json.dumps(sorted(run_set)),
                parallel=bool(parallel),
                overwrite=bool(overwrite),
                status=RUNNING,
                pid=pid,
                updated_at=now,
            )
        )
        s.execute(delete(Stage).where(Stage.graph == cfg.graph_id))
        for w in run_set:
            s.add(Stage(graph=cfg.graph_id, workload=w, state=PENDING, updated_at=now))


def set_state(cfg, workload, state) -> None:
    """Record a stage's progress; best-effort, never aborts a running workload."""
    try:
        with _session(cfg) as s:
            s.merge(
                Stage(
                    graph=cfg.graph_id,
                    workload=workload,
                    state=state,
                    updated_at=_now(),
                )
            )
    except Exception:  # noqa: BLE001 - progress tracking is auxiliary
        pass


def _set_run(cfg, **fields) -> None:
    """Update fields on this graph's Run row; best-effort."""
    try:
        with _session(cfg) as s:
            run = s.get(Run, cfg.graph_id)
            if run:
                for key, value in fields.items():
                    setattr(run, key, value)
                run.updated_at = _now()
    except Exception:  # noqa: BLE001 - progress tracking is auxiliary
        pass


def set_run_status(cfg, status) -> None:
    _set_run(cfg, status=status)


def set_run_pid(cfg, pid) -> None:
    """Record the driver process so a stalled run (running + dead pid) is detectable."""
    _set_run(cfg, pid=pid)


def finish_run(cfg) -> None:
    set_run_status(cfg, DONE)


def get_run(cfg) -> Run | None:
    """The active Run for this graph, or None."""
    with _session(cfg) as s:
        return s.get(Run, cfg.graph_id)


def states(cfg) -> dict:
    """{workload: state} for this graph's stages."""
    with _session(cfg) as s:
        rows = s.scalars(select(Stage).where(Stage.graph == cfg.graph_id))
        return {st.workload: st.state for st in rows}


def clear(cfg) -> None:
    """Drop this graph's run + stage rows (undeploy); best-effort. Cost db is untouched."""
    try:
        with _session(cfg) as s:
            s.execute(delete(Stage).where(Stage.graph == cfg.graph_id))
            s.execute(delete(Run).where(Run.graph == cfg.graph_id))
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass


def purge(cfg) -> None:
    """Delete every run + stage row (all graphs); best-effort. Cost db is untouched."""
    try:
        with _session(cfg) as s:
            s.execute(delete(Stage))
            s.execute(delete(Run))
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass
