import dataclasses
import io
import os
from types import SimpleNamespace

from click.testing import CliRunner
from rich.console import Console

from pipeline import cli, util
from pipeline.db import state

_NO_CLUSTER = SimpleNamespace(list_jobs=lambda ns, w: [], node_summary=lambda: (0, 0, {}))


def _render(renderable) -> str:
    # force_terminal renders markup like production (so a bad style tag that swallows
    # text — e.g. [running] — would be caught), while keeping plain words intact
    buf = io.StringIO()
    Console(file=buf, width=200, force_terminal=True).print(renderable)
    return buf.getvalue()


def test_pid_alive_distinguishes_live_and_dead_pids():
    assert util.pid_alive(os.getpid()) is True
    assert util.pid_alive(2**31 - 1) is False  # an impossibly high, unused pid
    assert util.pid_alive(None) is False


def test_run_view_renders_table_summary_and_pending(cfg, monkeypatch):
    monkeypatch.setattr(util, "kube", _NO_CLUSTER)
    cfg = dataclasses.replace(cfg, region="")  # skip cost lookups
    state.start_run(cfg, {"ingest", "meshing", "l2cache"}, parallel=True)
    state.set_state(cfg, "ingest", state.COMPLETE)
    state.set_state(cfg, "meshing", state.RUNNING)
    out = _render(
        util.run_view(
            cfg, state.get_run(cfg), ["ingest", "meshing", "l2cache"], state.states(cfg)
        )
    )
    assert "running" in out  # run status in the header
    assert "ingest: complete" in out  # done stage -> one-line summary
    assert "meshing | g" in out  # running stage -> full status_table (its title)
    assert "l2cache: pending" in out  # pending stage -> one line


def test_run_view_flags_a_stalled_run(cfg, monkeypatch):
    monkeypatch.setattr(util, "kube", _NO_CLUSTER)
    cfg = dataclasses.replace(cfg, region="")
    state.start_run(cfg, {"ingest"}, parallel=True, pid=2**31 - 1)  # recorded pid is dead
    out = _render(util.run_view(cfg, state.get_run(cfg), ["ingest"], state.states(cfg)))
    assert "driver not running" in out  # running status + dead driver pid -> stalled


def test_status_once_with_a_run_renders_the_multi_stage_view(cfg, monkeypatch):
    monkeypatch.setattr(util, "kube", _NO_CLUSTER)
    monkeypatch.setattr(cli.cost, "sample", lambda c: None)
    monkeypatch.setattr(cli.util, "read_layer_counts", lambda c: {})
    cfg = dataclasses.replace(cfg, region="")
    state.start_run(cfg, {"ingest", "meshing"}, parallel=True)
    state.set_state(cfg, "ingest", state.COMPLETE)
    state.set_state(cfg, "meshing", state.RUNNING)
    res = CliRunner().invoke(cli.status, ["--once"], obj=cfg, catch_exceptions=False)
    assert res.exit_code == 0
    assert "ingest: complete" in res.output and "meshing | g" in res.output
