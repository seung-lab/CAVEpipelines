import textwrap
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from rich.text import Text

from cave_pipeline import cgcache, util


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
    # a finished Job whose Complete write admission rejected: every completion succeeded,
    # so it must not read as running and strand every layer above it
    assert util.job_state(_job([_cond("SuccessCriteriaMet")])) == "complete"
    assert util.job_state(_job([_cond("SuccessCriteriaMet", "False")])) == "running"


def test_job_state_survives_a_job_with_no_status_yet():
    """`pipeline top` reads this inside Live, where an AttributeError kills the frame."""
    assert util.job_state(SimpleNamespace(status=None)) == "running"


def test_usage_view_survives_a_job_with_no_status_yet(monkeypatch, cfg):
    job = _fake_job()
    job.status = None
    monkeypatch.setattr(util.kube, "read_job", lambda ns, n: job)
    monkeypatch.setattr(util.kube, "pod_metrics", lambda ns, n: [])
    assert "ingest-l3" in _view_text(util.usage_view(cfg, "ingest-l3", 3))


def test_elapsed():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end_75 = start + timedelta(minutes=75)
    end_5 = start + timedelta(minutes=5)
    assert util.elapsed(_job(start_time=start, completion_time=end_75)) == "1h15m"
    assert util.elapsed(_job(start_time=start, completion_time=end_5)) == "5m"
    assert util.elapsed(_job(start_time=None)) == "-"


def _populate(monkeypatch, job):
    """Stub the cluster: one job + a fixed node summary, for status_table rendering."""
    monkeypatch.setattr(util.kube, "list_jobs", lambda ns, w=None: [job])
    monkeypatch.setattr(util.kube, "node_summary", lambda: (3, 2, 12.0, 48.0))


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


def test_status_usage_lines_only_for_the_running_layer(monkeypatch, cfg, render):
    """One metrics call per frame at most: a finished layer has no live pods to describe."""
    asked = []
    monkeypatch.setattr(
        util,
        "live_usage_table",
        lambda c, job, layer: asked.append(layer) or Text("USAGE-MARKER"),
    )
    done = _job_row(succeeded=4, chunks=100, batch=25, conditions=[_cond("Complete")])
    _populate(monkeypatch, done)
    assert "USAGE-MARKER" not in render(util.status_table(cfg))
    assert asked == []  # complete layer -> no metrics call at all

    _populate(monkeypatch, _job_row(succeeded=1, chunks=100, batch=25))  # running
    assert "USAGE-MARKER" in render(util.status_table(cfg))
    assert asked == [2]  # the running layer's number reaches the caption


@pytest.mark.parametrize(
    "metrics, marker",
    [
        (lambda ns, name: [], "no metrics yet"),
        (lambda ns, name: 1 / 0, "usage unavailable"),
    ],
)
def test_live_usage_table_renders_when_metrics_are_missing(
    monkeypatch, cfg, render, make_job, metrics, marker
):
    """A layer that just scaled up reports nothing for a scrape or two. Returning None
    would pull the block out of the frame mid-run, and Live cannot erase a taller frame."""
    monkeypatch.setattr(util.kube, "pod_metrics", metrics)
    out = render(util.live_usage_table(cfg, make_job(), 3))
    assert marker in out
    assert "cpu" in out and "mem" in out  # the rows are present, just dashed


def _event(kind, name, reason="OOMKilled"):
    return SimpleNamespace(
        reason=reason, involved_object=SimpleNamespace(kind=kind, name=name)
    )


def test_layer_ooms_counts_this_jobs_pod_kills(monkeypatch, cfg, make_job):
    """make_job is named 'ingest-l2'; only its own pods' kills are charged to the layer."""
    monkeypatch.setattr(
        util.kube,
        "oom_events",
        lambda ns: [_event("Pod", "ingest-l2-0-abc"), _event("Pod", "ingest-l2-7-def")],
    )
    assert util.layer_ooms(cfg, make_job()) == 2


def test_layer_ooms_excludes_other_jobs_and_node_scoped_events(
    monkeypatch, cfg, make_job
):
    """'ingest-l20' must not match 'ingest-l2-', and a Node event names no Job at all."""
    monkeypatch.setattr(
        util.kube,
        "oom_events",
        lambda ns: [
            _event("Pod", "ingest-l20-0-abc"),  # a different layer
            _event("Pod", "someone-else-9-xyz"),
            _event("Node", "n1", reason="OOMKilling"),
        ],
    )
    assert util.layer_ooms(cfg, make_job()) == 0


def test_layer_ooms_never_lists_pods(monkeypatch, cfg, make_job):
    """This runs every frame; resolving names via a pod list is the fleet-sized read."""
    listed = []
    monkeypatch.setattr(util.kube, "oom_events", lambda ns: [_event("Pod", "x-0-a")])
    monkeypatch.setattr(util.kube, "pods_of", lambda ns, job: listed.append(job) or [])
    monkeypatch.setattr(util.kube, "pods_of_uid", lambda *a, **k: listed.append(a) or [])
    util.layer_ooms(cfg, make_job())
    assert not listed


def test_layer_ooms_survives_unreadable_events(monkeypatch, cfg, make_job):
    """RBAC can deny Events; a missing count must not take down the status frame."""
    monkeypatch.setattr(util.kube, "oom_events", lambda ns: 1 / 0)
    assert util.layer_ooms(cfg, make_job()) == 0


def test_usage_table_flags_ooms_without_literal_markup(render):
    """Text(), not markup: a '[red]' tag would print verbatim through Rich."""
    out = render(util.usage_table([], 2.0, 4.0, "cap", ooms=3))
    assert "OOM x3" in out and "[red]" not in out
    assert "OOM" not in render(util.usage_table([], 2.0, 4.0, "cap"))


def test_at_risk_is_not_the_failure_colour():
    """A saturated pod may still finish; red is reserved for the kill that happened, so
    the operator can tell a near miss from a dead task at a glance."""
    assert util.AT_RISK != "red"


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
    monkeypatch.setattr(util.kube, "node_summary", lambda: (0, 0, 0.0, 0.0))
    monkeypatch.setattr(util.costs, "load_table", dict)
    cells = {
        c.header: list(c.cells)
        for c in util.status_table(cfg, {2: 100}).renderables[1].columns
    }
    assert cells["retries"] == ["34"]  # transient attempts, all recovered
    assert cells["failed"] == ["0"]  # nothing permanently dead -> not alarming
    job.status.failed_indexes = "1,3-5,7"
    cells = {
        c.header: list(c.cells)
        for c in util.status_table(cfg, {2: 100}).renderables[1].columns
    }
    assert cells["failed"] == ["[red]5[/]"]


_IDX = "batch.kubernetes.io/job-completion-index"


def _metric(name, cpu, mem, idx=None, container="ingest"):
    meta = {"name": name}
    if idx is not None:
        meta["labels"] = {_IDX: str(idx)}
    return {
        "metadata": meta,
        "containers": [{"name": container, "usage": {"cpu": cpu, "memory": mem}}],
    }


def test_pod_index_prefers_label_then_annotation_then_name():
    """metrics-server copies the label but not the annotation, so a PodMetrics item is
    only indexable by label or by parsing {job}-{index}-{suffix} out of the name."""
    assert util.pod_index({"metadata": {"labels": {_IDX: "7"}, "name": "j-9-abc"}}) == 7
    assert util.pod_index({"metadata": {"annotations": {_IDX: "4"}, "name": "x"}}) == 4
    assert util.pod_index({"metadata": {"name": "ingest-l3-11-abc"}}) == 11
    assert util.pod_index({"metadata": {"name": "no-index"}}) is None
    assert util.pod_index({"metadata": {"labels": {_IDX: "junk"}, "name": "z"}}) is None


def test_usage_records_drops_unreported_pods_rather_than_counting_them_zero():
    """Folding a not-yet-scraped pod in as 0.0 drags every percentile down and flips the
    widen/don't-widen verdict this view exists to answer."""
    items = [
        _metric("a", "2000m", "1Gi", idx=1),
        {"metadata": {"name": "starting"}, "containers": []},  # no usage yet
    ]
    recs = util.usage_records(items, "ingest")
    assert [r["pod"] for r in recs] == ["a"]


def test_usage_records_sorts_by_cpu_and_matches_container_by_name():
    """containers[0] on a pod with a sidecar measures the wrong process."""
    items = [
        _metric("low", "250m", "445480Ki", idx=2),
        _metric("high", "8913484669n", "6341544Ki", idx=11),
    ]
    recs = util.usage_records(items, "ingest")
    assert [r["pod"] for r in recs] == ["high", "low"]  # highest cpu first
    assert round(recs[0]["cpu"], 1) == 8.9
    assert round(recs[1]["mem"], 1) == 0.4
    sidecar = [
        {
            "metadata": {"name": "p"},
            "containers": [
                {"name": "istio", "usage": {"cpu": "50m", "memory": "1Gi"}},
                {"name": "ingest", "usage": {"cpu": "2", "memory": "3Gi"}},
            ],
        }
    ]
    assert util.usage_records(sidecar, "ingest")[0]["cpu"] == 2.0


def test_usage_bands_are_disjoint_and_ascending():
    """Three independent slices overlap for every n between k and 3k, which would print
    the same pod twice in a `pipeline sample 20` view."""
    assert util.usage_bands(20, 10) == [range(20)]  # <= 3k: list everything
    assert util.usage_bands(30, 10) == [range(30)]  # exactly 3k is still one range
    bands = util.usage_bands(1000, 10)
    assert [(b.start, b.stop) for b in bands] == [(0, 10), (495, 505), (990, 1000)]
    starts = [b.start for b in bands]
    assert starts == sorted(starts)
    assert bands[0].stop <= bands[1].start and bands[1].stop <= bands[2].start
    assert util.usage_bands(1000, 0) == [range(1000)]  # k<=0 means every pod


def test_quantile_is_nearest_rank_and_survives_one_pod():
    """statistics.quantiles interpolates (reporting a p90 no pod exhibits) and raises
    below two points, which a one-pod layer would hit.

    Nearest rank is ceil(q*n)-1. int(q*n) is one rank high at every n, which makes p90 the
    maximum on a 10-pod layer — reading a backend-bound layer as cpu-saturated, the one
    call `pipeline top` exists to inform."""
    assert util._quantile([5.0], 0.9) == 5.0
    vals = [float(i) for i in range(10)]
    assert util._quantile(vals, 0.0) == 0.0
    assert util._quantile(vals, 0.1) == 0.0
    assert util._quantile(vals, 0.5) == 4.0
    assert util._quantile(vals, 0.9) == 8.0  # not 9.0: p90 must not be the max at n=10
    assert util._quantile(vals, 0.99) == 9.0
    assert util._quantile(vals, 1.0) == 9.0  # clamped to the last index, never IndexError


def _cells(table) -> dict:
    return {c.header: list(c.cells) for c in table.columns}


def test_usage_table_renders_cores_and_gib_by_task_index():
    """Two decimals: at .1f an idle pod (0.00) and one blocked on Bigtable (0.07) both
    print "0.0", which is the distinction this view is read for."""
    recs = util.usage_records(
        [
            _metric("ingest-l6-2-xyz", "250m", "445480Ki", idx=2),
            _metric("ingest-l6-11-abc", "8913484669n", "6341544Ki", idx=11),
        ],
        "ingest",
    )
    cells = _cells(util._pod_usage_table(recs, billed=8.0, req_mem=8.0, rows=10))
    assert cells["pod"] == ["ingest-l6-11-abc", "ingest-l6-2-xyz"]  # highest cpu first
    assert cells["task"] == ["11", "2"]
    assert cells["cpu"] == ["8.91", "0.25"]
    assert cells["mem"] == ["6.0Gi", "0.4Gi"]


def test_usage_table_flags_memory_at_the_saturation_mark():
    """The marked mem cell is the OOM warning; without it a pod at 95% of its request
    reads the same as one at 10%. Not red: this pod is at risk, not dead."""
    recs = util.usage_records([_metric("hot", "1", "7.6Gi", idx=0)], "ingest")
    hot = _cells(util._pod_usage_table(recs, 8.0, 8.0, 10))["mem%"][0]
    assert f"[{util.AT_RISK}]" in hot and "[red]" not in hot
    recs = util.usage_records([_metric("cool", "1", "1Gi", idx=0)], "ingest")
    assert (
        f"[{util.AT_RISK}]"
        not in _cells(util._pod_usage_table(recs, 8.0, 8.0, 10))["mem%"][0]
    )


def test_usage_table_counts_the_pods_it_elides_and_survives_a_missing_index():
    """Without the gap row a banded table reads as the whole fleet, and an unindexed pod
    must render rather than raise."""
    items = [_metric(f"p{i}", "1", "1Gi", idx=i) for i in range(100)]
    items[0]["metadata"].pop("labels")  # scraped before the label landed
    cells = _cells(
        util._pod_usage_table(util.usage_records(items, "ingest"), 8.0, 8.0, 2)
    )
    assert any("pods not shown" in c for c in cells["pod"])
    assert "?" in cells["task"]  # unknown index, not a crash


def _fake_job(cpu="1", mem="3Gi", procs="2", limits=None):
    container = SimpleNamespace(
        name="ingest",
        resources=SimpleNamespace(requests={"cpu": cpu, "memory": mem}, limits=limits),
        env=[SimpleNamespace(name="PCG_N_PROCESSES", value=procs)],
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(annotations={}),
        spec=SimpleNamespace(
            template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))
        ),
        status=SimpleNamespace(active=2, conditions=[]),
    )


def _view_text(group):
    return "\n".join(r.plain for r in group.renderables if hasattr(r, "plain"))


def test_usage_view_flags_saturated_memory_without_literal_markup(
    monkeypatch, cfg, render
):
    """The OOM flag is a cell style, not a markup tag: Text() renders markup verbatim, so
    a "[red]" built into a string would print as literal characters."""
    monkeypatch.setattr(util.kube, "read_job", lambda ns, n: _fake_job())
    monkeypatch.setattr(
        util.kube, "pod_metrics", lambda ns, n: [_metric("a", "1", "2900Mi", idx=0)]
    )
    out = render(util.usage_view(cfg, "ingest-l3", 3))
    assert "[red]" not in out and "[/]" not in out
    assert "pods >=90%" in out  # the signal itself is still on the view


def test_usage_view_keeps_the_job_state_brackets_literal(monkeypatch, cfg):
    """The head line's "[running]" must not be eaten as a markup tag."""
    monkeypatch.setattr(util.kube, "read_job", lambda ns, n: _fake_job())
    monkeypatch.setattr(
        util.kube, "pod_metrics", lambda ns, n: [_metric("a", "1", "1Gi", idx=0)]
    )
    assert "[running]" in _view_text(util.usage_view(cfg, "ingest-l3", 3))


def test_usage_view_explains_over_request_cpu_by_the_jobs_own_limits(monkeypatch, cfg):
    """Without a cpu limit a pod bursts into the node's spare cores as a matter of course,
    so blaming a stale Job generation would send the operator hunting a non-existent bug.
    The compute class does not imply the limit — Balanced and Scale-Out are pod-billed too
    — so the signal is read off this Job's own template."""
    monkeypatch.setattr(
        util.kube, "pod_metrics", lambda ns, n: [_metric("hot", "6340m", "0.5Gi", idx=0)]
    )
    monkeypatch.setattr(util.kube, "read_job", lambda ns, n: _fake_job(limits=None))
    text = _view_text(util.usage_view(cfg, "ingest-l3", 3))
    assert "over 105% of the cpu request - no cpu limit on this Job's pods" in text

    limits = {"cpu": "1", "memory": "3Gi"}  # limits==requests: this really is stale
    monkeypatch.setattr(util.kube, "read_job", lambda ns, n: _fake_job(limits=limits))
    text = _view_text(util.usage_view(cfg, "ingest-l3", 3))
    assert "a previous Job generation" in text


def test_usage_view_degrades_when_the_job_is_unreadable(monkeypatch, cfg):
    """A diagnostic view must not die on an RBAC denial or a deleted Job."""

    def _boom(ns, n):
        raise RuntimeError("forbidden")

    monkeypatch.setattr(util.kube, "read_job", _boom)
    monkeypatch.setattr(util.kube, "pod_metrics", lambda ns, n: [])
    text = _view_text(util.usage_view(cfg, "ingest-l3", 3))
    assert "not readable" in text


def test_usage_view_says_when_no_pod_reports_at_all(monkeypatch, cfg):
    """Active pods but an empty payload is metrics-server being down or every pod being
    younger than its scrape window — distinct from a partial read."""
    monkeypatch.setattr(util.kube, "read_job", lambda ns, n: _fake_job())
    monkeypatch.setattr(util.kube, "pod_metrics", lambda ns, n: [])
    text = _view_text(util.usage_view(cfg, "ingest-l3", 3))  # _fake_job has active=2
    assert "no metrics for 2 active pods" in text


def test_usage_view_reports_pods_that_are_not_reporting(monkeypatch, cfg):
    """metrics-server needs two scrapes for a cpu rate, so a new pod is absent rather than
    zero. Asserted exactly: an `or` across both branches left this one uncovered."""
    job = _fake_job()
    job.status.active = 3
    monkeypatch.setattr(util.kube, "read_job", lambda ns, n: job)
    monkeypatch.setattr(
        util.kube, "pod_metrics", lambda ns, n: [_metric("a", "1", "1Gi", idx=0)]
    )
    text = _view_text(util.usage_view(cfg, "ingest-l3", 3))
    assert "2 active pods not reporting yet" in text


def test_status_table_shows_pending_layers(monkeypatch, cfg):
    def _raise(*a, **k):
        raise RuntimeError("no nodes")

    monkeypatch.setattr(util.kube, "list_jobs", lambda ns, workload=None: [])
    monkeypatch.setattr(util.kube, "node_summary", _raise)
    monkeypatch.setattr(util.costs, "load_table", dict)
    table = util.status_table(cfg, {2: 100, 3: 200}).renderables[1]
    assert table.row_count == 2  # both layers shown though none submitted


def test_status_table_marks_layers_below_start_layer_skipped(monkeypatch, cfg):
    """A skipped layer has no Job, exactly like a pending one — without the label a
    finished mid-graph restart shows an L2 row that never progresses."""
    monkeypatch.setattr(util.kube, "list_jobs", lambda ns, workload=None: [])
    monkeypatch.setattr(util.kube, "node_summary", lambda: (0, 0, 0.0, 0.0))
    monkeypatch.setattr(util.costs, "load_table", dict)
    table = util.status_table(cfg, {2: 100, 3: 200}, start_layer=3).renderables[1]
    done = list(table.columns[1].cells)
    assert done == ["skipped", "-"]  # L2 declared built; L3 pending, not yet submitted
    # the default never labels: a caller rendering several stages holds one merged cfg
    plain = util.status_table(cfg, {2: 100, 3: 200}).renderables[1]
    assert list(plain.columns[1].cells) == ["-", "-"]


def test_skipped_label_is_resolved_per_workload(monkeypatch, tmp_path, cfg):
    """run_view replaces only `workload`, so cfg.job stays merged for the loaded one —
    ingest's start_layer must not mark meshing's L2 skipped."""
    (tmp_path / "p.yml").write_text(
        "graph_id: g\nimages: {pcg: repo/pcg:v3.2.0}\n"
        "job:\n  workloads:\n    ingest:\n      start_layer: 3\n"
    )
    cfg.source = str(tmp_path / "p.yml")
    assert util._start_layer(cfg, "ingest") == 3
    assert util._start_layer(cfg, "meshing") == 2  # never inherits ingest's
    # an unreadable yml must not break a live display. Absolute: a bare name resolves
    # against pytest's cwd, so the fallback would depend on what sits there.
    cfg.source = str(tmp_path / "gone.yml")
    assert util._start_layer(cfg, "ingest") == 2


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


def test_relevant_log_retains_the_traceback_and_drops_noise():
    log = textwrap.dedent("""\
        I0618 00:00 google_auth_provider.cc:149] Using credentials at /x
        Using ServiceAccount AuthProvider
        layer 2 batch 100: 4 chunks
        Traceback (most recent call last):
          File "worker.py", line 32, in process_one
            do()
        RuntimeError: boom
        resource_tracker: leaked semaphore at shutdown""")
    out = util.relevant_log(log)
    assert out.startswith("Traceback (most recent call last):")  # anchored at the failure
    assert "RuntimeError: boom" in out
    assert "google_auth_provider" not in out  # auth banner dropped
    assert "leaked semaphore" not in out  # shutdown chatter dropped
    assert "batch 100" not in out  # pre-failure progress dropped


def test_relevant_log_tails_when_no_traceback():
    log = "\n".join(f"line {i}" for i in range(50))
    assert util.relevant_log(log, n=5).splitlines() == [
        f"line {i}" for i in range(45, 50)
    ]
