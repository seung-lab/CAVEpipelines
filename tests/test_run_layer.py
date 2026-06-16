from types import SimpleNamespace

import pytest

from cave_pipeline import ops
from cave_pipeline.db import state

_CONDS = {
    "complete": [SimpleNamespace(type="Complete", status="True")],
    "running": [],
}


def test_run_layer_skips_complete_layers(monkeypatch, cfg, make_job):
    job = make_job(conditions=_CONDS["complete"], succeeded=5)
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: job)
    submitted = []
    monkeypatch.setattr(ops, "submit", lambda c, layer: submitted.append(True))
    ops.run_layer(cfg, 2)
    assert not submitted


def test_run_layer_attaches_and_stops_on_dead_tasks(
    monkeypatch, running_run, make_job, no_cost_sample
):
    job = make_job(conditions=_CONDS["running"], succeeded=5, failed_indexes="0-3")
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: job)
    submitted = []
    monkeypatch.setattr(ops, "submit", lambda c, layer: submitted.append(True))
    with pytest.raises(SystemExit, match="inspect 2"):
        ops.run_layer(running_run, 2)
    assert not submitted  # a running layer is attached, never recreated


def test_run_layer_stops_cleanly_when_job_vanishes(
    monkeypatch, running_run, make_job, no_cost_sample
):
    job = make_job(conditions=_CONDS["running"])
    reads = iter([job, None])  # present at attach, deleted before the first poll
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: next(reads))
    with pytest.raises(SystemExit, match="disappeared"):
        ops.run_layer(running_run, 2)


def test_run_layer_stops_when_its_job_is_suspended(
    monkeypatch, running_run, make_job, no_cost_sample
):
    job = make_job(conditions=[], suspend=True)  # pause drained it to 0 pods
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: job)
    monkeypatch.setattr(ops, "submit", lambda c, layer: None)
    with pytest.raises(ops.Paused):
        ops.run_layer(running_run, 2)


def test_run_layer_stops_when_run_undeployed(
    monkeypatch, running_run, make_job, no_cost_sample
):
    # undeploy clears the run row while its Job lingers in Terminating (foreground delete);
    # the driver must detect the cleared run and stop, not poll the corpse forever.
    job = make_job(conditions=_CONDS["running"])
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: job)
    state.clear(running_run)  # operator ran `pipeline undeploy` mid-poll
    with pytest.raises(ops.Undeployed):
        ops.run_layer(running_run, 2)
