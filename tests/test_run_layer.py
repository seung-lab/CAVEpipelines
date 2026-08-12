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


def test_skipping_a_complete_layer_still_closes_out_its_cost(monkeypatch, cfg, make_job):
    """A driver that died mid-layer never ran the poll loop's final pass, so the layer's
    last pods stay frozen at an interim estimate until something re-reads them."""
    job = make_job(conditions=_CONDS["complete"], succeeded=5)
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: job)
    monkeypatch.setattr(ops, "submit", lambda c, layer: None)
    finals = []
    monkeypatch.setattr(ops.cost, "sample", lambda c, final=False: finals.append(final))
    ops.run_layer(cfg, 2)
    assert finals == [True]  # true terminated timestamps, not the interim freeze


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


def test_run_layer_replaces_a_stale_job_it_would_otherwise_attach_to(
    monkeypatch, running_run, make_job, no_cost_sample
):
    """A running Job carries the image it was created with, so attaching blind runs the
    old one — this is how a suspended dev5 Job survived a config bumped to dev6."""
    stale = make_job(conditions=_CONDS["running"], image="repo/pcg:old", succeeded=0)
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: stale)
    submitted = []
    monkeypatch.setattr(ops, "submit", lambda c, layer: submitted.append(layer))
    monkeypatch.setattr(
        ops.util, "job_progress", lambda j, t=None: 1 / 0
    )  # stop the poll
    with pytest.raises(ZeroDivisionError):
        ops.run_layer(running_run, 2)
    assert submitted == [2]  # rebuilt from the yml, not attached


def test_run_layer_replaces_a_drifted_job_that_has_progress(
    monkeypatch, running_run, make_job, no_cost_sample
):
    """Completed tasks must not pin a bad image: chunks carry their own done marker, so a
    replacement re-scans them. Halting here made `resume` useless after an image fix."""
    stale = make_job(conditions=_CONDS["running"], image="repo/pcg:old", succeeded=7)
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: stale)
    submitted = []
    monkeypatch.setattr(ops, "submit", lambda c, layer: submitted.append(layer))
    monkeypatch.setattr(
        ops.util, "job_progress", lambda j, t=None: 1 / 0
    )  # stop the poll
    with pytest.raises(ZeroDivisionError):
        ops.run_layer(running_run, 2)
    assert submitted == [2]


def test_run_layer_attaches_when_drift_only_changes_scheduling(
    monkeypatch, running_run, make_job, no_cost_sample
):
    """`processes_per_vcpu` changes how a chunk is worked, not what it produces, so the
    finished chunks stay valid — resubmitting to apply it would rebuild all of them."""
    drifted = make_job(
        conditions=_CONDS["running"],
        annotations={"processes_per_vcpu": "9"},
        succeeded=7,
    )
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: drifted)
    submitted = []
    monkeypatch.setattr(ops, "submit", lambda c, layer: submitted.append(layer))
    monkeypatch.setattr(
        ops.util, "job_progress", lambda j, t=None: 1 / 0
    )  # stop the poll
    with pytest.raises(ZeroDivisionError):  # reached the poll, so it attached
        ops.run_layer(running_run, 2)
    assert not submitted


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
