import dataclasses
import os
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from pipeline import cli, ops
from pipeline.db import state

_COMPLETE = [SimpleNamespace(type="Complete", status="True")]


def test_run_layer_stops_when_its_job_is_suspended(monkeypatch, cfg, make_job):
    job = make_job(conditions=[], suspend=True)  # pause drained it to 0 pods
    monkeypatch.setattr(ops.cost, "sample", lambda c: None)
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: job)
    monkeypatch.setattr(ops, "submit", lambda c, layer: None)
    with pytest.raises(ops.Paused):
        ops.run_layer(cfg, 2)


def test_pause_suspends_only_incomplete_jobs_and_marks_paused(
    monkeypatch, cfg, running_run, make_job
):
    running = make_job(name="ingest-l2", conditions=[])
    done = make_job(name="ingest-l3", conditions=_COMPLETE)
    monkeypatch.setattr(ops.kube, "list_jobs", lambda ns: [running, done])
    suspended = []
    monkeypatch.setattr(
        ops.kube, "set_suspend", lambda ns, name, s: suspended.append((name, s))
    )
    ops.pause(cfg)
    assert suspended == [("ingest-l2", True)]  # the finished layer is left alone
    assert state.get_run(cfg).status == state.PAUSED


def test_resume_unsuspends_suspended_jobs_then_drives(
    monkeypatch, cfg, running_run, make_job
):
    state.set_run_status(cfg, state.PAUSED)
    monkeypatch.setattr(
        ops.kube,
        "list_jobs",
        lambda ns: [make_job(name="ingest-l2", suspend=True), make_job(name="ingest-l3")],
    )
    calls = []
    monkeypatch.setattr(
        ops.kube, "set_suspend", lambda ns, name, s: calls.append((name, s))
    )
    monkeypatch.setattr(ops, "drive", lambda c: calls.append("drive"))
    ops.resume(cfg)
    unsuspends = [c for c in calls if isinstance(c, tuple)]
    assert unsuspends == [("ingest-l2", False)]  # only the suspended job is unsuspended
    assert calls[-1] == "drive" and state.get_run(cfg).status == state.RUNNING


def test_resume_without_a_run_errors(cfg):
    with pytest.raises(SystemExit, match="no run"):
        ops.resume(cfg)


def test_drive_self_pauses_on_failure(monkeypatch, cfg, running_run):
    def boom(c, run_set, parallel):
        raise SystemExit("dead tasks")

    monkeypatch.setattr(ops, "orchestrate", boom)
    paused = []
    monkeypatch.setattr(ops, "pause", lambda c: paused.append(True))
    with pytest.raises(SystemExit, match="dead tasks"):
        ops.drive(cfg)
    assert paused == [True]  # a dying driver suspends the cluster


def test_drive_marks_the_run_done_on_success(monkeypatch, cfg, running_run):
    monkeypatch.setattr(ops, "orchestrate", lambda c, run_set, parallel: None)
    monkeypatch.setattr(ops, "pause", lambda c: pytest.fail("must not pause on success"))
    ops.drive(cfg)
    assert state.get_run(cfg).status == state.DONE


def test_drive_exits_cleanly_when_paused(monkeypatch, cfg, running_run):
    def paused(c, run_set, parallel):
        raise ops.Paused("suspended")

    monkeypatch.setattr(ops, "orchestrate", paused)
    monkeypatch.setattr(
        ops, "pause", lambda c: pytest.fail("must not re-pause on a pause")
    )
    ops.drive(cfg)  # returns cleanly — no traceback, the operator's pause is not undone


def test_resume_refuses_a_live_driver(monkeypatch, cfg, running_run):
    state.set_run_pid(cfg, os.getpid())  # a healthy driver is recorded
    monkeypatch.setattr(
        ops, "drive", lambda c: pytest.fail("must not start a second driver")
    )
    with pytest.raises(SystemExit, match="already running"):
        ops.resume(cfg)


def test_resume_refuses_a_completed_run(monkeypatch, cfg, running_run):
    state.finish_run(cfg)  # status done
    monkeypatch.setattr(ops, "drive", lambda c: pytest.fail("nothing to resume"))
    with pytest.raises(SystemExit, match="complete"):
        ops.resume(cfg)


def test_resume_cli_exits_cleanly_when_paused(monkeypatch, cfg, running_run):
    state.set_run_status(cfg, state.PAUSED)
    monkeypatch.setattr(ops.kube, "list_jobs", lambda ns: [])

    def paused(c, run_set, parallel):
        raise ops.Paused("L2 (ingest) suspended")

    monkeypatch.setattr(ops, "orchestrate", paused)
    # catch_exceptions=False: a leaked Paused would re-raise here; exit 0 = clean, not a traceback
    res = CliRunner().invoke(cli.resume, obj=cfg, catch_exceptions=False)
    assert res.exit_code == 0


def test_resume_drives_a_stalled_run(monkeypatch, cfg, running_run):
    state.set_run_pid(cfg, 2**31 - 1)  # dead pid -> stalled, resumable
    monkeypatch.setattr(ops.kube, "list_jobs", lambda ns: [])
    driven = []
    monkeypatch.setattr(ops, "drive", lambda c: driven.append(True))
    ops.resume(cfg)
    assert driven == [True]


def test_run_ready_surfaces_a_pause_not_a_failure(monkeypatch, cfg):
    monkeypatch.setattr(
        ops, "_phase_cfg", lambda c, w: dataclasses.replace(c, workload=w)
    )
    monkeypatch.setattr(ops.util, "read_layer_counts", lambda c: {2: 1})

    def run_workload(cfg_w):
        if cfg_w.workload == "meshing":
            raise ops.Paused("suspended")

    monkeypatch.setattr(ops, "run_workload", run_workload)
    with pytest.raises(ops.Paused):  # a paused sibling is not aggregated as a failure
        ops._run_ready(cfg, ["meshing", "l2cache"], parallel=True)
