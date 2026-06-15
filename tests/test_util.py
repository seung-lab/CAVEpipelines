from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pipeline import cgcache, util


def test_ceil_div_completions():
    assert util.ceil_div(10000, 1000) == 10
    assert util.ceil_div(10001, 1000) == 11  # a partial last batch still needs an index
    assert util.ceil_div(1, 1000) == 1


def _cond(t, s="True"):
    return SimpleNamespace(type=t, status=s)


def _job(conditions=None, **status):
    return SimpleNamespace(status=SimpleNamespace(conditions=conditions, **status))


def test_job_state():
    assert util.job_state(_job([_cond("Complete")])) == "complete"
    assert util.job_state(_job([_cond("Failed")])) == "failed"
    assert util.job_state(_job([_cond("Complete", "False")])) == "running"
    assert util.job_state(_job(None)) == "running"


def test_elapsed():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end_75 = start + timedelta(minutes=75)
    end_5 = start + timedelta(minutes=5)
    assert util.elapsed(_job(start_time=start, completion_time=end_75)) == "1h15m"
    assert util.elapsed(_job(start_time=start, completion_time=end_5)) == "5m"
    assert util.elapsed(_job(start_time=None)) == "-"


def _populate(monkeypatch, job):
    """Stub the cluster: one job + a fixed node summary, for status_table rendering."""
    monkeypatch.setattr(util.kube, "list_jobs", lambda ns, w=None: [job])
    monkeypatch.setattr(util.kube, "node_summary", lambda: (3, 2, {"e2-standard-4": 3}))


def _job_row(succeeded, chunks, batch, conditions=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            labels={"layer": "2"},
            annotations={"chunks": str(chunks), "batch_size": str(batch)},
        ),
        status=SimpleNamespace(
            conditions=conditions,
            succeeded=succeeded,
            active=0,
            failed=0,
            ready=0,
            start_time=None,
            completion_time=None,
        ),
    )


def test_status_progress_math(monkeypatch, cfg, render):
    _populate(monkeypatch, _job_row(succeeded=4, chunks=1000, batch=100))
    out = render(util.status_table(cfg))
    # 4 succeeded batches * 100 = 400 done of 1000 -> 40%
    assert "400" in out and "1000" in out and "40%" in out
    assert "3 nodes" in out and "2 spot" in out


def test_status_done_caps_at_total(monkeypatch, cfg, render):
    # last batch is partial: 10*100 = 1000 reported, but only 950 chunks exist.
    job = _job_row(succeeded=10, chunks=950, batch=100, conditions=[_cond("Complete")])
    _populate(monkeypatch, job)
    out = render(util.status_table(cfg))
    assert "950" in out and "100%" in out  # not 1000, not 105%


def test_count_indexes_parses_k8s_interval_strings():
    assert util.count_indexes(None) == 0
    assert util.count_indexes("") == 0
    assert util.count_indexes("1,3-5,7") == 5


def test_status_table_splits_retries_from_dead_tasks(monkeypatch, cfg, make_job):
    job = make_job(chunks=100, batch_size=10, succeeded=10, failed=34)
    monkeypatch.setattr(util.kube, "list_jobs", lambda ns, workload=None: [job])
    monkeypatch.setattr(util.kube, "node_summary", lambda: (0, 0, {}))
    monkeypatch.setattr(util.costs, "load_table", lambda: {})
    cells = {c.header: list(c.cells) for c in util.status_table(cfg, {2: 100}).columns}
    assert cells["retries"] == ["34"]  # transient attempts, all recovered
    assert cells["failed"] == ["0"]  # nothing permanently dead -> not alarming
    job.status.failed_indexes = "1,3-5,7"
    cells = {c.header: list(c.cells) for c in util.status_table(cfg, {2: 100}).columns}
    assert cells["failed"] == ["[red]5[/]"]


def test_usage_table_renders_cores_and_gib_by_task_index(monkeypatch, cfg):
    items = [
        {
            "metadata": {"name": "ingest-l6-11-abc"},
            "containers": [{"usage": {"cpu": "8913484669n", "memory": "6341544Ki"}}],
        },
        {
            "metadata": {"name": "ingest-l6-2-xyz"},
            "containers": [{"usage": {"cpu": "250m", "memory": "445480Ki"}}],
        },
    ]
    monkeypatch.setattr(util.kube, "pod_metrics", lambda ns, name: items)
    cells = {c.header: list(c.cells) for c in util.usage_table(cfg, "ingest-l6").columns}
    assert cells["pod"] == ["ingest-l6-2-xyz", "ingest-l6-11-abc"]  # task order
    assert cells["cpu"] == ["0.2", "8.9"]
    assert cells["memory"] == ["0.4Gi", "6.0Gi"]


def test_status_table_shows_pending_layers(monkeypatch, cfg):
    def _raise(*a, **k):
        raise Exception("no nodes")

    monkeypatch.setattr(util.kube, "list_jobs", lambda ns, workload=None: [])
    monkeypatch.setattr(util.kube, "node_summary", _raise)
    monkeypatch.setattr(util.costs, "load_table", lambda: {})
    table = util.status_table(cfg, {2: 100, 3: 200})
    assert table.row_count == 2  # both layers shown though none submitted


def test_query_meta_routes_persistent_to_cache_client(monkeypatch, cfg):
    cfg.persistent_util = True
    seen = {}
    monkeypatch.setattr(util.kube, "util_pod", lambda ns: "util-pod")

    def _exec(ns, pod, argv, **kw):
        seen["argv"] = argv
        return "100 50 1\n"

    monkeypatch.setattr(util.kube, "exec_cmd", _exec)
    monkeypatch.setattr(
        util.kube,
        "run_oneshot",
        lambda *a, **k: pytest.fail("persistent path must not use a one-shot pod"),
    )
    assert util._query_meta(cfg, "counts", "g") == "100 50 1\n"
    assert cgcache.CLIENT_SRC in seen["argv"]  # the warm-server client, not the server


def test_query_meta_routes_oneshot_when_not_persistent(monkeypatch, cfg):
    cfg.persistent_util = False
    cfg.workload = "l2cache"  # the cg-meta probe still reads the graph in the PCG image
    seen = {}
    monkeypatch.setattr(
        util.manifest,
        "oneshot_pod_spec",
        lambda c, name, argv, image=None: ("spec", argv, image),
    )

    def _oneshot(ns, spec):
        seen["argv"], seen["image"] = spec[1], spec[2]
        return "yes\n"

    monkeypatch.setattr(util.kube, "run_oneshot", _oneshot)
    monkeypatch.setattr(
        util.kube,
        "exec_cmd",
        lambda *a, **k: pytest.fail("one-shot path must not exec into the util pod"),
    )
    assert util._query_meta(cfg, "mesh", "g") == "yes\n"
    assert cgcache.ONESHOT_SRC in seen["argv"]  # the inline import snippet
    assert seen["image"] == cfg.images.pcg  # graph read pins PCG, not the l2cache image


def test_runs_table_lists_newest_first_and_filters_by_graph(cfg, seed_cost, render):
    seed_cost("g-260101-000000", started_at=100.0)
    seed_cost("g-260201-000000", started_at=200.0)  # the more recent deploy
    seed_cost("other-260101-000000", graph="other", started_at=50.0)
    out = render(util.runs_table(cfg, {}))
    assert out.index("g-260201-000000") < out.index("g-260101-000000")  # newest first
    assert "other-260101-000000" in out  # spans every graph by default
    filtered = render(util.runs_table(cfg, {}, graph="g"))
    assert "g-260201-000000" in filtered and "other-260101-000000" not in filtered


def test_runs_table_buckets_ad_hoc_submits(cfg, seed_cost, render):
    seed_cost("", uid="adhoc-1")  # a standalone submit/sample probe has no deploy run-id
    assert "(ad-hoc)" in render(util.runs_table(cfg, {}))


def test_run_breakdown_rows_by_workload_layer_scoped_to_the_run(cfg, seed_cost, render):
    seed_cost("g-1", workload="ingest", layer=2, uid="j1")
    seed_cost("g-1", workload="meshing", layer=3, uid="j2")
    seed_cost("g-2", workload="l2cache", layer=4, uid="j3")  # a different run
    out = render(util.run_breakdown(cfg, {}, "g-1"))
    assert "ingest" in out and "meshing" in out  # both workloads of this run
    assert "l2cache" not in out  # scoped to run g-1, not g-2
