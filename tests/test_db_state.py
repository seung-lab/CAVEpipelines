import dataclasses
import os

import pytest

from pipeline.db import state


def test_run_round_trip_and_stage_progress(cfg):
    state.start_run(cfg, {"ingest", "meshing"}, parallel=False)
    run = state.get_run(cfg)
    assert run.stage_set == {"ingest", "meshing"}
    assert run.parallel is False and run.status == state.RUNNING
    assert state.states(cfg) == {"ingest": state.PENDING, "meshing": state.PENDING}
    state.set_state(cfg, "ingest", state.RUNNING)
    state.set_state(cfg, "ingest", state.COMPLETE)
    assert state.states(cfg)["ingest"] == state.COMPLETE
    state.finish_run(cfg)
    assert state.get_run(cfg).status == state.DONE


def test_start_run_mints_a_graph_linked_run_id(cfg):
    state.start_run(cfg, {"ingest"}, parallel=True)
    rid = state.get_run(cfg).run_id
    assert rid.startswith(f"{cfg.graph_id}-")  # graph-linked: the run names its graph
    assert rid[len(cfg.graph_id) + 1 :]  # non-empty timestamp suffix -> per-invocation


def test_start_run_resets_to_the_new_stage_set(cfg):
    state.start_run(cfg, {"ingest", "meshing", "l2cache"}, parallel=True)
    state.set_state(cfg, "ingest", state.COMPLETE)
    state.start_run(cfg, {"ingest"}, parallel=True)  # a fresh run replaces the old stages
    assert state.states(cfg) == {"ingest": state.PENDING}  # meshing/l2cache dropped


def test_state_is_scoped_by_graph(cfg):
    other = dataclasses.replace(cfg, graph_id="other")
    state.start_run(cfg, {"ingest"}, parallel=True)
    state.start_run(other, {"meshing"}, parallel=True)
    assert state.states(cfg) == {"ingest": state.PENDING}
    assert state.states(other) == {"meshing": state.PENDING}
    assert state.get_run(cfg).stage_set == {"ingest"}


def test_clear_drops_only_this_graphs_run_and_stages(cfg):
    other = dataclasses.replace(cfg, graph_id="other")
    state.start_run(cfg, {"ingest"}, parallel=True)
    state.start_run(other, {"meshing"}, parallel=True)
    state.clear(cfg)
    assert state.get_run(cfg) is None and state.states(cfg) == {}
    assert state.get_run(other) is not None  # another graph's run is untouched


def test_purge_drops_every_graphs_run_and_stages(cfg):
    other = dataclasses.replace(cfg, graph_id="other")
    state.start_run(cfg, {"ingest"}, parallel=True)
    state.start_run(other, {"meshing"}, parallel=True)
    state.purge(cfg)
    assert state.get_run(cfg) is None and state.get_run(other) is None
    assert state.states(cfg) == {} and state.states(other) == {}


def test_progress_writes_are_best_effort_but_run_creation_surfaces(cfg, tmp_path):
    # a directory where the state db file should be: SQLite cannot open it
    os.makedirs(f"{tmp_path}/blocked/state.db")
    bad = dataclasses.replace(
        cfg, database={"state": f"sqlite:///{tmp_path}/blocked/state.db"}
    )
    state.set_state(bad, "ingest", state.RUNNING)  # best-effort: never raises
    with pytest.raises(Exception):  # noqa: B017 - the essential write surfaces the failure
        state.start_run(bad, {"ingest"}, parallel=True)
