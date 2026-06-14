import dataclasses
import os

import pytest
from click.testing import CliRunner

from pipeline import cli, util
from pipeline.db import state


def test_pid_alive_distinguishes_live_and_dead_pids():
    assert util.pid_alive(os.getpid()) is True
    assert util.pid_alive(2**31 - 1) is False  # an impossibly high, unused pid
    assert util.pid_alive(None) is False


def test_run_view_renders_table_summary_and_pending(cfg, monkeypatch, no_cluster, render):
    monkeypatch.setattr(util, "kube", no_cluster)
    cfg = dataclasses.replace(cfg, region="")  # skip cost lookups
    state.start_run(cfg, {"ingest", "meshing", "l2cache"}, parallel=True)
    state.set_state(cfg, "ingest", state.COMPLETE)
    state.set_state(cfg, "meshing", state.RUNNING)
    out = render(
        util.run_view(
            cfg, state.get_run(cfg), ["ingest", "meshing", "l2cache"], state.states(cfg)
        )
    )
    assert "running" in out  # run status in the header
    assert "ingest: complete" in out  # done stage -> one-line summary
    assert "meshing | g" in out  # running stage -> full status_table (its title)
    assert "l2cache: pending" in out  # pending stage -> one line


def test_run_view_flags_a_stalled_run(cfg, monkeypatch, no_cluster, render):
    monkeypatch.setattr(util, "kube", no_cluster)
    cfg = dataclasses.replace(cfg, region="")
    state.start_run(cfg, {"ingest"}, parallel=True, pid=2**31 - 1)  # recorded pid is dead
    out = render(util.run_view(cfg, state.get_run(cfg), ["ingest"], state.states(cfg)))
    assert "driver not running" in out  # running status + dead driver pid -> stalled


def test_status_once_with_a_run_renders_the_multi_stage_view(
    cfg, monkeypatch, no_cluster, no_cost_sample
):
    monkeypatch.setattr(util, "kube", no_cluster)
    cfg = dataclasses.replace(cfg, region="")
    state.start_run(cfg, {"ingest", "meshing"}, parallel=True)
    state.set_state(cfg, "ingest", state.COMPLETE)
    state.set_state(cfg, "meshing", state.RUNNING)
    res = CliRunner().invoke(cli.status, ["--once"], obj=cfg, catch_exceptions=False)
    assert res.exit_code == 0
    assert "ingest: complete" in res.output and "meshing | g" in res.output


def test_status_recorded_run_reads_cache_and_shows_unsubmitted_layers(
    cfg, monkeypatch, no_cluster, no_cost_sample
):
    # the driver cached the layer counts; status must read them (not probe a cold util pod)
    # so the table shows every layer, including those not yet submitted as a Job
    monkeypatch.setattr(util, "kube", no_cluster)
    cfg = dataclasses.replace(cfg, region="")
    util._write_cache(cfg, {cfg.graph_id: {"2": 847, "3": 144, "4": 18}})
    state.start_run(cfg, {"ingest"}, parallel=True)
    state.set_state(cfg, "ingest", state.RUNNING)
    monkeypatch.setattr(
        cli.util,
        "read_layer_counts",
        lambda c: pytest.fail("status must not probe the cluster for a recorded run"),
    )
    res = CliRunner().invoke(cli.status, ["--once"], obj=cfg, catch_exceptions=False)
    assert res.exit_code == 0
    assert all(
        t in res.output for t in ("847", "144", "18")
    )  # every cached layer, no Job needed


def test_status_exits_cleanly_when_run_cleared_mid_watch(
    cfg, monkeypatch, no_cluster, no_cost_sample
):
    # the run exists when status starts but is cleared (undeploy/purge) before the first
    # render: status must exit cleanly, not crash in run_view on a None run
    monkeypatch.setattr(util, "kube", no_cluster)
    cfg = dataclasses.replace(cfg, region="")
    state.start_run(cfg, {"ingest"}, parallel=True)
    seq = iter(
        [state.get_run(cfg), None]
    )  # start sees the run; the render sees it cleared
    monkeypatch.setattr(state, "get_run", lambda c: next(seq, None))
    res = CliRunner().invoke(cli.status, ["--once"], obj=cfg, catch_exceptions=False)
    assert res.exit_code == 0
