from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pipeline import costdb

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _cfg(graph="g", workload="ingest"):
    return SimpleNamespace(graph_id=graph, workload=workload, namespace="ns")


def _job(uid="j1", layer="3", succeeded=0, active=1, end=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(uid=uid, name="ingest-l3", labels={"layer": layer}),
        spec=SimpleNamespace(
            completions=4,
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
            start_time=T0,
            completion_time=end,
            succeeded=succeeded,
            failed=0,
            active=active,
        ),
    )


def _pod(uid, running=True, end=None):
    term = None if running else SimpleNamespace(started_at=T0, finished_at=end)
    return SimpleNamespace(
        metadata=SimpleNamespace(uid=uid, creation_timestamp=T0),
        status=SimpleNamespace(
            phase="Running" if running else "Succeeded",
            container_statuses=[SimpleNamespace(state=SimpleNamespace(terminated=term))],
        ),
    )


def _patch_cluster(monkeypatch, jobs, pods):
    monkeypatch.setattr(
        costdb,
        "kube",
        SimpleNamespace(list_jobs=lambda ns, w: jobs, pods_of=lambda ns, n: pods),
    )


def test_sample_records_and_freezes_vanished_pods(tmp_path, monkeypatch):
    monkeypatch.setattr(costdb, "COSTS_DIR", str(tmp_path))
    cfg = _cfg()
    done = _pod("p2", running=False, end=T0 + timedelta(hours=1))
    _patch_cluster(monkeypatch, [_job()], [_pod("p1"), done])
    costdb.sample(cfg)
    conn = costdb.connect(cfg)
    pods = {r["pod_uid"]: r for r in costdb.pod_rows(conn, "j1")}
    assert pods["p2"]["finished_at"] is not None  # terminated -> real end time
    assert pods["p1"]["finished_at"] is None  # still running
    conn.close()
    # p1 GC'd before the next sample: its runtime freezes at the last sighting
    _patch_cluster(monkeypatch, [_job()], [done])
    costdb.sample(cfg)
    conn = costdb.connect(cfg)
    pods = {r["pod_uid"]: r for r in costdb.pod_rows(conn, "j1")}
    assert pods["p1"]["finished_at"] == pods["p1"]["last_seen"]
    conn.close()


def test_db_per_graph_workload_and_resubmits_accrue(tmp_path, monkeypatch):
    monkeypatch.setattr(costdb, "COSTS_DIR", str(tmp_path))
    _patch_cluster(monkeypatch, [_job(uid="a")], [])
    costdb.sample(_cfg())
    costdb.sample(_cfg(graph="other"))
    # a re-submitted layer is a NEW uid under the same name: history accrues
    _patch_cluster(monkeypatch, [_job(uid="a"), _job(uid="b")], [])
    costdb.sample(_cfg())
    assert (tmp_path / "g.ingest.db").exists()
    assert (tmp_path / "other.ingest.db").exists()
    conn = costdb.connect(_cfg())
    assert len(costdb.job_rows(conn)) == 2
    assert len(costdb.job_rows(conn, 3)) == 2
    conn.close()


def test_sample_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(costdb, "COSTS_DIR", str(tmp_path))

    def boom(ns, w):
        raise RuntimeError("api down")

    monkeypatch.setattr(costdb, "kube", SimpleNamespace(list_jobs=boom))
    costdb.sample(_cfg())  # cost is auxiliary: no exception escapes
