import dataclasses

import pytest
import yaml

from cave_pipeline import config, ops, stages
from cave_pipeline.db import state

BASE = {"graph_id": "g", "images": {"pcg": "repo/pcg:v3.2.0"}}


def _write(dirpath, name, content):
    (dirpath / name).write_text(yaml.safe_dump(content))


def test_editing_the_dataset_yaml_makes_a_submit_refuse(tmp_path):
    """A driver holds one cfg for the whole run and writes the dataset into the graph from
    that snapshot, so a mid-run edit (mesh_config.mip is the case this exists for) reaches
    neither — it must fail loudly instead of building against the values it replaced."""
    _write(tmp_path, "dataset.yml", {"mesh_config": {"mip": 2, "max_layer": 6}})
    _write(tmp_path, "pipeline.yml", {**BASE, "dataset": "dataset.yml"})
    cfg = config.load()
    ops.require_config_unchanged(cfg)  # unedited: passes

    # dataset-only, so an apply field must not trip it. ramp.max, not zone — test_apply
    # asserts zone registers as immutable drift.
    edited = {**BASE, "dataset": "dataset.yml", "job": {"ramp": {"max": 512}}}
    _write(tmp_path, "pipeline.yml", edited)
    ops.require_config_unchanged(cfg)

    _write(tmp_path, "dataset.yml", {"mesh_config": {"mip": 1, "max_layer": 6}})
    with pytest.raises(SystemExit, match="dataset.yml changed since this run loaded it"):
        ops.require_config_unchanged(cfg)


def test_dag_batches_orders_by_depth():
    # ingest at depth 0; meshing + l2cache (both depend only on ingest) at depth 1
    assert list(ops.dag_batches({"ingest", "meshing", "l2cache"})) == [
        ["ingest"],
        ["l2cache", "meshing"],
    ]


def test_dag_batches_halts_downstream_when_the_consumer_raises():
    """The halt is the control flow, not a rule each caller restates: `ts.done` sits after
    the yield, so a body that raises never resumes the generator."""
    seen = []
    with pytest.raises(RuntimeError):
        for batch in ops.dag_batches({"ingest", "meshing", "l2cache"}):
            seen.append(batch)
            raise RuntimeError("stage failed")
    assert seen == [["ingest"]]  # depth 1 was never marked ready


def test_unconfigured_stage_names_the_key_it_wants(cfg):
    """The key is data on the stage, so the message reads it — a hardcoded mapping goes
    stale the moment another gated stage is added."""
    cfg.dataset = {}
    with pytest.raises(SystemExit, match=r"stage 'meshing'.*no `mesh_config`"):
        ops.confirm_run(cfg, {"meshing"}, parallel=False, yes=True)
    cfg.dataset = {"mesh_config": {"max_layer": 6}}
    assert stages.STAGES["meshing"].applies(cfg)
    assert stages.STAGES["ingest"].applies(cfg)  # no key = always applies


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
    monkeypatch, cfg, stub_layer_counts
):
    cfg.persistent_util = False
    monkeypatch.setattr(
        ops, "_phase_cfg", lambda c, w: dataclasses.replace(c, workload=w)
    )
    stub_layer_counts({2: 1})
    ran = []

    def fake_run_workload(cfg_w):
        ran.append(cfg_w.workload)
        if cfg_w.workload == "meshing":
            raise SystemExit("boom in meshing")

    monkeypatch.setattr(ops, "run_workload", fake_run_workload)
    with pytest.raises(SystemExit, match="meshing"):
        ops.orchestrate(cfg, {"meshing", "l2cache"}, parallel=True)
    assert "l2cache" in ran  # a failing sibling never aborts the healthy one


def test_run_workload_records_complete_then_failed(monkeypatch, cfg, stub_layer_counts):
    monkeypatch.setattr(ops, "setup", lambda c, exist_ok=False: None)
    stub_layer_counts({2: 1})
    monkeypatch.setattr(ops, "top_layer", lambda c, counts: 2)
    monkeypatch.setattr(ops, "run_layer", lambda c, layer: None)
    ops.run_workload(dataclasses.replace(cfg, workload="ingest"))
    assert state.states(cfg)["ingest"] == state.COMPLETE

    def boom(c, layer):
        raise SystemExit("dead tasks")

    monkeypatch.setattr(ops, "run_layer", boom)
    with pytest.raises(SystemExit):
        ops.run_workload(dataclasses.replace(cfg, workload="meshing"))
    assert state.states(cfg)["meshing"] == state.FAILED


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
