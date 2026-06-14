import dataclasses
import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pipeline.db import base, cost, models

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _job(
    uid="j1",
    layer="3",
    succeeded=0,
    active=1,
    end=None,
    start=T0,
    parallelism=4,
    conditions=None,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(uid=uid, name="ingest-l3", labels={"layer": layer}),
        spec=SimpleNamespace(
            completions=4,
            parallelism=parallelism,
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[
                        SimpleNamespace(
                            resources=SimpleNamespace(
                                requests={"cpu": "2", "memory": "4Gi"}
                            )
                        )
                    ],
                    node_selector={},
                )
            ),
        ),
        status=SimpleNamespace(
            start_time=start,
            completion_time=end,
            conditions=conditions or [],
            succeeded=succeeded,
            failed=0,
            active=active,
        ),
    )


def _pod(uid, running=True, end=None, start=T0, container=True):
    term = None if running else SimpleNamespace(started_at=start, finished_at=end)
    statuses = (
        [SimpleNamespace(state=SimpleNamespace(terminated=term))] if container else []
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(uid=uid, creation_timestamp=T0),
        status=SimpleNamespace(
            phase="Running" if running else "Succeeded",
            start_time=start,
            container_statuses=statuses,
        ),
    )


def _patch_cluster(monkeypatch, jobs, pods):
    """Fake only the cluster (no live k8s); the cost db itself is a real SQLite file."""
    monkeypatch.setattr(
        cost,
        "kube",
        SimpleNamespace(list_jobs=lambda ns, w: jobs, pods_of=lambda ns, n: pods),
    )


def _db_file(cfg):
    return cfg.database["cost"].removeprefix("sqlite:///")


def test_sample_records_and_freezes_vanished_pods(cfg, monkeypatch):
    done = _pod("p2", running=False, end=T0 + timedelta(hours=1))
    _patch_cluster(monkeypatch, [_job()], [_pod("p1"), done])
    cost.sample(cfg)
    pods = {p.pod_uid: p for p in cost.pods(cfg, "j1")}
    assert pods["p2"].finished_at is not None  # terminated -> real end time
    assert pods["p1"].finished_at is None  # still running
    # p1 GC'd before the next sample: its runtime freezes at the last sighting
    _patch_cluster(monkeypatch, [_job()], [done])
    cost.sample(cfg)
    pods = {p.pod_uid: p for p in cost.pods(cfg, "j1")}
    assert pods["p1"].finished_at == pods["p1"].last_seen


def test_one_db_scopes_by_graph_and_workload(cfg, monkeypatch):
    _patch_cluster(monkeypatch, [_job(uid="a")], [])
    cost.sample(cfg)  # graph g, workload ingest
    _patch_cluster(monkeypatch, [_job(uid="c")], [])
    cost.sample(dataclasses.replace(cfg, graph_id="other"))
    _patch_cluster(monkeypatch, [_job(uid="d")], [])
    cost.sample(dataclasses.replace(cfg, workload="meshing"))
    # a re-submitted layer is a NEW uid under the same name: history accrues
    _patch_cluster(monkeypatch, [_job(uid="a"), _job(uid="b")], [])
    cost.sample(cfg)
    assert {j.job_uid for j in cost.jobs(cfg)} == {"a", "b"}  # this graph+workload only
    assert [j.job_uid for j in cost.jobs(dataclasses.replace(cfg, graph_id="other"))] == [
        "c"
    ]
    assert [
        j.job_uid for j in cost.jobs(dataclasses.replace(cfg, workload="meshing"))
    ] == ["d"]


def test_started_at_keeps_first_seen(cfg, monkeypatch):
    _patch_cluster(monkeypatch, [_job(start=T0)], [_pod("p1", start=T0)])
    cost.sample(cfg)
    later = T0 + timedelta(hours=1)
    _patch_cluster(monkeypatch, [_job(start=later)], [_pod("p1", start=later)])
    cost.sample(cfg)
    assert cost.jobs(cfg)[0].started_at == T0.timestamp()  # COALESCE keeps first start
    assert cost.pods(cfg, "j1")[0].started_at == T0.timestamp()


def test_parallelism_never_shrinks(cfg, monkeypatch):
    _patch_cluster(monkeypatch, [_job(parallelism=8)], [])
    cost.sample(cfg)
    _patch_cluster(monkeypatch, [_job(parallelism=2)], [])  # scaled back down
    cost.sample(cfg)
    assert cost.jobs(cfg)[0].parallelism == 8  # MAX records the peak


def test_terminal_pod_without_container_state_freezes_at_now(cfg, monkeypatch):
    # Succeeded pod whose container status is gone: freeze at the sample time, not None
    _patch_cluster(monkeypatch, [_job()], [_pod("p1", running=False, container=False)])
    cost.sample(cfg)
    assert cost.pods(cfg, "j1")[0].finished_at is not None


def test_failed_job_ends_at_its_condition(cfg, monkeypatch):
    end = T0 + timedelta(hours=2)
    failed = [
        SimpleNamespace(type="Failed", status="True", last_transition_time=end)
    ]  # no completion_time on a Failed Job
    _patch_cluster(monkeypatch, [_job(end=None, conditions=failed)], [])
    cost.sample(cfg)
    assert cost.jobs(cfg)[0].finished_at == end.timestamp()


def test_relisted_live_pod_unfreezes(cfg, monkeypatch):
    _patch_cluster(monkeypatch, [_job()], [_pod("p1")])
    cost.sample(cfg)
    _patch_cluster(monkeypatch, [_job()], [])  # p1 vanishes -> frozen 'Gone'
    cost.sample(cfg)
    assert cost.pods(cfg, "j1")[0].finished_at is not None
    _patch_cluster(monkeypatch, [_job()], [_pod("p1")])  # p1 listed again, Running
    cost.sample(cfg)
    p = cost.pods(cfg, "j1")[0]
    assert p.finished_at is None and p.phase == "Running"


def test_deleted_jobs_stop_accruing(cfg, monkeypatch):
    _patch_cluster(monkeypatch, [_job()], [_pod("p1")])
    cost.sample(cfg)
    # the Job is deleted/replaced between samples: nothing listed any more
    _patch_cluster(monkeypatch, [], [])
    cost.sample(cfg)
    job = cost.jobs(cfg)[0]
    pod = cost.pods(cfg, "j1")[0]
    assert job.finished_at == job.last_seen and job.active == 0
    assert pod.phase == "Gone" and pod.finished_at == pod.last_seen


def test_sample_never_raises(cfg, monkeypatch):
    def boom(ns, w):
        raise RuntimeError("api down")

    monkeypatch.setattr(cost, "kube", SimpleNamespace(list_jobs=boom))
    cost.sample(cfg)  # cost is auxiliary: no exception escapes


def test_sample_does_not_raise_when_db_deleted_mid_run(cfg, monkeypatch):
    _patch_cluster(monkeypatch, [_job()], [_pod("p1")])
    cost.sample(cfg)
    os.remove(_db_file(cfg))  # deleted with the engine still cached (a live run)
    # NullPool reopens by path: no silent write to a dead inode, and no raise
    cost.sample(cfg)


def test_deleted_db_recreated_on_next_run(cfg, monkeypatch):
    _patch_cluster(monkeypatch, [_job()], [_pod("p1")])
    cost.sample(cfg)
    assert cost.jobs(cfg)
    # operator removes the cost db; a fresh process (no cached engine/schema) reopens it
    for suffix in ("", "-wal", "-shm"):
        path = _db_file(cfg) + suffix
        if os.path.exists(path):
            os.remove(path)
    base._engine.cache_clear()
    base._initialized.clear()
    assert cost.jobs(cfg) == []  # schema recreated empty, no crash
    cost.sample(cfg)
    assert cost.jobs(cfg)  # records again


def test_concurrent_first_session_is_race_safe(cfg):
    url = cfg.database["cost"]
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        barrier.wait()  # all hit the un-created schema at once
        try:
            with base.session(url, models.CostBase):
                pass
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors  # lock-guarded create_all: no 'table already exists' race
