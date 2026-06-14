from types import SimpleNamespace

import pytest
from kubernetes.client import ApiException

from pipeline import cli, ops


def test_meshing_submit_requires_mesh_meta(monkeypatch, cfg):
    cfg.workload = "meshing"  # workers silently default mip=0 without it
    monkeypatch.setattr(ops.util, "mesh_meta_written", lambda c: False)
    with pytest.raises(SystemExit, match="mesh-meta"):
        ops.submit(cfg, 2)


def test_submit_refuses_jobs_owned_by_another_graph(cfg, make_job):
    job = make_job(graph="other")
    with pytest.raises(SystemExit, match="belongs to graph"):
        ops.check_graph_owner(cfg, job)
    ops.check_graph_owner(cfg, job, force=True)  # explicit override
    job.metadata.labels = {"graph": cfg.graph_id}
    ops.check_graph_owner(cfg, job)  # own job passes


def test_submit_blocks_until_prev_layer_complete(monkeypatch, cfg, make_job):
    running = make_job(conditions=[])
    monkeypatch.setattr(
        cli.kube,
        "batch",
        lambda: SimpleNamespace(read_namespaced_job=lambda n, ns: running),
    )
    with pytest.raises(SystemExit, match="not complete"):
        ops.require_prev_complete(cfg, 3, force=False)
    ops.require_prev_complete(cfg, 3, force=True)  # override -> no raise
    ops.require_prev_complete(cfg, 2, force=False)  # L2 has no predecessor


def test_sample_runs_never_satisfy_the_layer_gate(monkeypatch, cfg, make_job):
    done_sample = make_job(
        conditions=[SimpleNamespace(type="Complete", status="True")],
        annotations={"sample": "true"},
    )
    monkeypatch.setattr(
        cli.kube,
        "batch",
        lambda: SimpleNamespace(read_namespaced_job=lambda n, ns: done_sample),
    )
    with pytest.raises(SystemExit, match="sample run"):
        ops.require_prev_complete(cfg, 3, force=False)


def test_sample_refuses_to_replace_a_real_job(monkeypatch, cfg, make_job):
    real = make_job(conditions=[])
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: real)
    with pytest.raises(SystemExit, match="already has a real job"):
        ops.sample(cfg, 2, 5)
    recreated = []
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: None)
    monkeypatch.setattr(cli.kube, "recreate_job", lambda ns, s: recreated.append(s))
    ops.sample(cfg, 2, 5)
    assert recreated[0].metadata.annotations["sample"] == "true"  # gate marker


def test_submit_sizes_job_and_ramps_to_pmax(monkeypatch, cfg, no_cost_sample, no_sleep):
    cfg.job.ramp = cli.config.Ramp(start=4, factor=2, period=60, max=256)
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: None)
    monkeypatch.setattr(ops.util, "read_n", lambda c, layer: 10001)
    created, scaled = [], []
    monkeypatch.setattr(cli.kube, "recreate_job", lambda ns, s: created.append(s))
    monkeypatch.setattr(cli.kube, "set_parallelism", lambda ns, n, p: scaled.append(p))
    ops.submit(cfg, 2)
    spec = created[0].spec
    assert spec.completions == 11  # ceil(10001 / 1000)
    assert spec.parallelism == 4  # ramp.start
    assert scaled == [8, 11]  # doubles, then caps at the task count


def test_submit_uses_the_layer_adjusted_batch(
    monkeypatch, cfg, make_job, no_cost_sample, no_sleep
):
    done = make_job(conditions=[SimpleNamespace(type="Complete", status="True")])
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: done if layer == 2 else None)
    monkeypatch.setattr(ops.util, "read_n", lambda c, layer: 1000)
    created = []
    monkeypatch.setattr(cli.kube, "recreate_job", lambda ns, s: created.append(s))
    monkeypatch.setattr(cli.kube, "set_parallelism", lambda ns, n, p: None)
    ops.submit(cfg, 3)
    # completions and the worker's batch annotation must agree, or tasks mis-slice
    assert created[0].spec.completions == 2  # ceil(1000 / (1000 // 2))
    assert created[0].metadata.annotations["batch_size"] == "500"


def test_submit_blocks_when_prev_layer_missing(monkeypatch, cfg):
    def boom(name, ns):
        raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(
        cli.kube, "batch", lambda: SimpleNamespace(read_namespaced_job=boom)
    )
    with pytest.raises(SystemExit, match="submit it first"):
        ops.require_prev_complete(cfg, 3, force=False)
