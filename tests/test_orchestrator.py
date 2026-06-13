import dataclasses

import pytest

from pipeline import ops, stages


def test_dag_levels_orders_by_depth():
    # ingest at depth 0; meshing + l2cache (both depend only on ingest) at depth 1
    assert ops.dag_levels({"ingest", "meshing", "l2cache"}) == [
        ["ingest"],
        ["l2cache", "meshing"],
    ]


def test_orchestrate_runs_levels_in_order(monkeypatch, cfg):
    cfg.persistent_util = False
    batches = []
    monkeypatch.setattr(
        ops, "_run_ready", lambda c, ready, parallel: batches.append(set(ready))
    )
    ops.orchestrate(cfg, {"ingest", "meshing", "l2cache"}, parallel=True)
    assert batches == [{"ingest"}, {"meshing", "l2cache"}]


def test_orchestrate_solo_stage_runs_without_a_completion_gate(monkeypatch, cfg):
    # meshing alone runs immediately; its dep (ingest) is the operator's call, never checked
    cfg.persistent_util = False
    ran = []
    monkeypatch.setattr(
        ops, "_run_ready", lambda c, ready, parallel: ran.append(set(ready))
    )
    ops.orchestrate(cfg, {"meshing"}, parallel=True)
    assert ran == [{"meshing"}]


def test_orchestrate_parallel_partial_failure_reports_and_finishes_siblings(
    monkeypatch, cfg
):
    cfg.persistent_util = False
    monkeypatch.setattr(
        ops, "_phase_cfg", lambda c, w: dataclasses.replace(c, workload=w)
    )
    monkeypatch.setattr(ops.util, "read_layer_counts", lambda c: {2: 1})
    ran = []

    def fake_run_workload(cfg_w):
        ran.append(cfg_w.workload)
        if cfg_w.workload == "meshing":
            raise SystemExit("boom in meshing")

    monkeypatch.setattr(ops, "run_workload", fake_run_workload)
    with pytest.raises(SystemExit, match="meshing"):
        ops.orchestrate(cfg, {"meshing", "l2cache"}, parallel=True)
    assert "l2cache" in ran  # a failing sibling never aborts the healthy one


def test_select_range_picks_depth_levels(cfg):
    cfg.dataset.pop("l2cache_config", None)  # build_set {ingest, meshing}; depths [0, 1]
    assert ops.select_range(cfg, 0, 1, yes=True) == {"ingest", "meshing"}  # full
    assert ops.select_range(cfg, 1, 1, yes=True) == {"meshing"}  # post-ingest only
    assert ops.select_range(cfg, 0, 0, yes=True) == {"ingest"}  # ingest only
    with pytest.raises(SystemExit, match="outside"):
        ops.select_range(cfg, 0, 5, yes=True)


def test_build_set_requires_meshing_and_optional_l2cache(cfg):
    cfg.dataset.pop("l2cache_config", None)
    assert stages.build_set(cfg) == {"ingest", "meshing"}  # meshing is mandatory
    cfg.dataset["l2cache_config"] = {}  # the dataset, not pipeline.yml, drives the DAG
    assert stages.build_set(cfg) == {"ingest", "meshing", "l2cache"}


def test_confirm_run_rejects_a_selected_stage_the_dataset_does_not_configure(cfg):
    cfg.dataset.pop("mesh_config", None)  # meshing selected, but its config is missing
    with pytest.raises(SystemExit, match="not configured"):
        ops.confirm_run(cfg, {"ingest", "meshing"}, parallel=True, yes=True)
