import dataclasses
import os
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cave_pipeline import cli, ops
from cave_pipeline.db import state

_COMPLETE = [SimpleNamespace(type="Complete", status="True")]


@pytest.fixture
def drive_env(monkeypatch):
    """Empty cluster, recorded `pause`, and an `orchestrate` whose per-call outcome the
    test queues. Recording `pause` keeps "must not pause" an assertion in the test body."""
    rec = SimpleNamespace(paused=[], calls=[])
    # matches the real signature: a one-arg stub would TypeError if this starts filtering
    monkeypatch.setattr(ops.kube, "list_jobs", lambda ns, workload=None: [])
    monkeypatch.setattr(ops, "pause", lambda c: rec.paused.append(True))

    def orchestrate(*outcomes):
        """Each call consumes one outcome: an exception instance to raise, else success."""
        queue = iter(outcomes)

        def _run(c, run_set, parallel):
            rec.calls.append(c.workload)
            if (outcome := next(queue, None)) is not None:
                raise outcome

        monkeypatch.setattr(ops, "orchestrate", _run)

    rec.orchestrate = orchestrate
    return rec


def _pause_calls(monkeypatch, jobs):
    """Record the ordered (action, job) calls pause makes against the cluster."""
    calls = []
    monkeypatch.setattr(ops.kube, "list_jobs", lambda ns: jobs)
    monkeypatch.setattr(
        ops.kube,
        "set_suspend",
        lambda ns, name, s: calls.append(("suspend", name, s)),
    )
    monkeypatch.setattr(
        ops.kube,
        "delete_job_pods",
        lambda ns, name, **kw: calls.append(("delete", name, kw)),
    )
    return calls


def test_pause_suspends_only_incomplete_jobs_and_marks_paused(
    monkeypatch, cfg, running_run, make_job
):
    running = make_job(name="ingest-l2", conditions=[])
    done = make_job(name="ingest-l3", conditions=_COMPLETE)
    calls = _pause_calls(monkeypatch, [running, done])
    ops.pause(cfg)
    # the finished layer is left alone — neither suspended nor stripped of its pods
    assert [c[1] for c in calls] == ["ingest-l2", "ingest-l2"]
    assert state.get_run(cfg).status == state.PAUSED


def test_pause_clears_pods_and_only_after_suspending(
    monkeypatch, cfg, running_run, make_job
):
    """A suspend alone leaves the drain to the Job controller, one delete per pod, which
    takes minutes of paid-for pods on a wide layer. Deleting before the suspend would be
    worse still: the controller recreates every pod just cleared."""
    calls = _pause_calls(monkeypatch, [make_job(name="ingest-l2", conditions=[])])
    ops.pause(cfg)
    assert calls == [
        ("suspend", "ingest-l2", True),
        ("delete", "ingest-l2", {"keep_terminal": True}),
    ]


def test_pause_keeps_terminal_pods_for_diagnosis(monkeypatch, cfg, running_run, make_job):
    """`drive` pauses on failure and then points at `pipeline inspect <layer> <index>`;
    deleting the Failed pods would destroy the evidence for the failure it just reported.
    They hold no node resources, so keeping them costs nothing."""
    calls = _pause_calls(monkeypatch, [make_job(name="ingest-l2", conditions=[])])
    ops.pause(cfg)
    assert [c for c in calls if c[0] == "delete"] == [
        ("delete", "ingest-l2", {"keep_terminal": True})
    ]


def test_drive_clears_leftover_suspend_then_runs(monkeypatch, cfg, running_run, make_job):
    state.set_run_status(cfg, state.PAUSED)  # a prior self-pause left the jobs suspended
    monkeypatch.setattr(
        ops.kube,
        "list_jobs",
        lambda ns: [make_job(name="ingest-l2", suspend=True), make_job(name="ingest-l3")],
    )
    cleared = []
    monkeypatch.setattr(
        ops.kube, "set_suspend", lambda ns, name, s: cleared.append((name, s))
    )
    monkeypatch.setattr(ops, "orchestrate", lambda c, run_set, parallel: None)
    ops.drive(cfg)
    assert cleared == [("ingest-l2", False)]  # only the suspended leftover is unsuspended
    assert state.get_run(cfg).status == state.DONE  # converged: unsuspend -> run -> done


def test_resume_without_a_run_errors(cfg):
    with pytest.raises(SystemExit, match="no run"):
        ops.resume(cfg)


def test_drive_self_pauses_on_failure(cfg, running_run, drive_env):
    drive_env.orchestrate(SystemExit("dead tasks"))
    with pytest.raises(SystemExit, match="dead tasks"):
        ops.drive(cfg)  # unattended: self-pauses and re-raises, no prompt
    assert drive_env.paused == [True]  # a dying driver suspends the cluster


def test_drive_resumes_in_place_when_attended(monkeypatch, cfg, running_run, drive_env):
    drive_env.orchestrate(SystemExit("dead tasks"), None)  # fails, then succeeds
    monkeypatch.setattr(
        ops.click, "confirm", lambda *a, **k: True
    )  # operator fixes + resumes
    ops.drive(cfg, interactive=True)
    assert len(drive_env.calls) == 2 and state.get_run(cfg).status == state.DONE


def test_drive_marks_the_run_done_on_success(cfg, running_run, drive_env):
    drive_env.orchestrate()  # every call succeeds
    ops.drive(cfg)
    assert state.get_run(cfg).status == state.DONE
    assert drive_env.paused == []  # must not pause on success


def test_drive_exits_cleanly_when_paused(cfg, running_run, drive_env):
    drive_env.orchestrate(ops.Paused("suspended"))
    ops.drive(cfg)  # returns cleanly — no traceback, the operator's pause is not undone
    assert drive_env.paused == []  # must not re-pause on a pause


def test_drive_exits_cleanly_when_undeployed(cfg, running_run, drive_env):
    drive_env.orchestrate(ops.Undeployed("run undeployed"))
    ops.drive(cfg)  # returns cleanly — state + jobs already gone, no traceback
    assert drive_env.paused == []  # must not suspend a torn-down run


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


def test_resume_cli_exits_cleanly_when_paused(cfg, running_run, drive_env):
    state.set_run_status(cfg, state.PAUSED)
    drive_env.orchestrate(ops.Paused("L2 (ingest) suspended"))
    # catch_exceptions=False: a leaked Paused would re-raise here; exit 0 = clean, not a traceback
    res = CliRunner().invoke(cli.resume, obj=cfg, catch_exceptions=False)
    assert res.exit_code == 0


def test_resume_drives_a_stalled_run(monkeypatch, cfg, running_run, drive_env):
    state.set_run_pid(cfg, 2**31 - 1)  # dead pid -> stalled, resumable
    driven = []
    monkeypatch.setattr(ops, "drive", lambda c, interactive=False: driven.append(True))
    ops.resume(cfg)
    assert driven == [True]


def test_run_ready_surfaces_a_pause_not_a_failure(monkeypatch, cfg, stub_layer_counts):
    monkeypatch.setattr(
        ops, "_phase_cfg", lambda c, w: dataclasses.replace(c, workload=w)
    )
    stub_layer_counts({2: 1})

    def run_workload(cfg_w):
        if cfg_w.workload == "meshing":
            raise ops.Paused("suspended")

    monkeypatch.setattr(ops, "run_workload", run_workload)
    with pytest.raises(ops.Paused):  # a paused sibling is not aggregated as a failure
        ops._run_ready(cfg, ["meshing", "l2cache"], parallel=True)


def test_run_ready_surfaces_undeploy_over_a_sibling_failure(
    monkeypatch, cfg, stub_layer_counts
):
    monkeypatch.setattr(
        ops, "_phase_cfg", lambda c, w: dataclasses.replace(c, workload=w)
    )
    stub_layer_counts({2: 1})

    def run_workload(cfg_w):
        if cfg_w.workload == "meshing":
            raise ops.Undeployed("run undeployed")
        raise SystemExit("boom in l2cache")

    monkeypatch.setattr(ops, "run_workload", run_workload)
    with pytest.raises(ops.Undeployed):  # a teardown supersedes a sibling failure
        ops._run_ready(cfg, ["meshing", "l2cache"], parallel=True)
