from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from kubernetes.client import ApiException

from pipeline import cli, ops


def run_cmd(command, argv, cfg):
    """Invoke one click command with a prebuilt Config (no group, no config file)."""
    return CliRunner().invoke(command, argv, obj=cfg, catch_exceptions=False)


def _capture_run_pcg(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli.util,
        "run_pcg",
        lambda c, name, argv, **kw: seen.update(name=name, argv=argv) or "",
    )
    return seen


def _capture_run_with_dataset(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli.util,
        "run_with_dataset",
        lambda c, name, argv: seen.update(name=name, argv=argv) or "",
    )
    return seen


def test_setup_runs_pipeline_ingest_setup(monkeypatch, cfg):
    seen = _capture_run_with_dataset(monkeypatch)  # ingest setup reads the dataset
    run_cmd(cli.setup, [], cfg)
    assert seen["argv"] == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.ingest.setup",
        cfg.graph_id,
    ]


def test_setup_enables_raw_when_agglomeration_present(monkeypatch, cfg):
    cfg.dataset["ingest_config"] = {"AGGLOMERATION": "gs://b/agg"}
    seen = _capture_run_with_dataset(monkeypatch)
    run_cmd(cli.setup, [], cfg)
    assert seen["argv"][-1] == "--raw"  # presence of the source enables the raw path


def test_setup_exists_flag_passes_exist_ok(monkeypatch, cfg):
    seen = _capture_run_with_dataset(monkeypatch)
    run_cmd(cli.setup, ["--exists"], cfg)
    assert "--exist-ok" in seen["argv"]  # resume-safe create reaches the worker
    seen.clear()
    run_cmd(cli.setup, [], cfg)
    assert "--exist-ok" not in seen["argv"]  # default errors on an existing table


def test_setup_runs_migrate_setup_for_migrate_workload(monkeypatch, cfg):
    cfg.workload = "migrate"
    seen = _capture_run_pcg(monkeypatch)
    run_cmd(cli.setup, [], cfg)
    assert seen["argv"] == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.migrate.setup",
        cfg.graph_id,
    ]


def test_setup_dispatches_meshing_to_mesh_meta(monkeypatch, cfg):
    cfg.workload = "meshing"  # meshing's setup is mesh-meta, never ingest.setup
    seen = _capture_run_with_dataset(monkeypatch)
    run_cmd(cli.setup, [], cfg)
    assert seen["argv"] == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.meshing.setup",
        cfg.graph_id,
    ]


def test_mesh_meta_runs_pipeline_meshing_setup(monkeypatch, cfg):
    seen = _capture_run_with_dataset(monkeypatch)  # mesh meta reads mesh_config
    run_cmd(cli.mesh_meta, [], cfg)
    assert seen["name"] == "mesh-meta"
    assert seen["argv"] == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.meshing.setup",
        cfg.graph_id,
    ]


def test_graph_id_flag_overrides_config(monkeypatch, cfg):
    monkeypatch.setattr(cli.config, "load", lambda name, workload=None: cfg)
    seen = _capture_run_with_dataset(monkeypatch)
    CliRunner().invoke(
        cli.cli, ["-g", "other_graph", "mesh-meta"], catch_exceptions=False
    )
    assert seen["argv"][-1] == "other_graph"  # the override reaches the workload


def test_api_errors_exit_cleanly(monkeypatch, cfg, stub_layer_counts):
    monkeypatch.setattr(cli.config, "load", lambda name, workload=None: cfg)
    stub_layer_counts(None)  # no cluster I/O

    def boom(ns, workload=None):
        raise ApiException(status=403, reason="Forbidden")

    monkeypatch.setattr(cli.kube, "list_jobs", boom)
    with pytest.raises(SystemExit, match="403"):
        cli.main(["status"])


def test_status_quiet_when_no_jobs(monkeypatch, cfg, stub_layer_counts):
    # cached a-priori counts persist locally across runs; they are NOT evidence of a
    # live deployment. With counts present but zero jobs, status must stay quiet.
    stub_layer_counts({2: 847, 3: 144})
    monkeypatch.setattr(cli.kube, "list_jobs", lambda ns, workload=None: [])
    built = {}
    monkeypatch.setattr(
        cli.util, "status_table", lambda c, t=None: built.setdefault("yes", True)
    )
    run_cmd(cli.status, ["--once"], cfg)
    assert not built  # no table rendered, just a note


def test_undeploy_deletes_jobs_then_uninstalls_release(monkeypatch, cfg):
    deleted, ran = [], {}
    job = SimpleNamespace(metadata=SimpleNamespace(name="ingest-l2"))
    cm = SimpleNamespace(metadata=SimpleNamespace(name="pcg-dataset-g"))
    monkeypatch.setattr(cli.kube, "list_jobs", lambda ns, workload=None: [job])
    monkeypatch.setattr(cli.kube, "delete_job", lambda ns, name: deleted.append(name))
    monkeypatch.setattr(cli.kube, "list_configmaps", lambda ns, sel: [cm])
    monkeypatch.setattr(
        cli.kube, "delete_configmap", lambda ns, name: deleted.append(name)
    )
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        lambda argv, **kw: (
            ran.setdefault("argv", argv),
            SimpleNamespace(returncode=0, stdout="released", stderr=""),
        )[1],
    )
    run_cmd(cli.undeploy, [], cfg)
    assert deleted == ["ingest-l2", "pcg-dataset-g"]  # jobs, then dataset configmaps
    assert ran["argv"][:2] == ["helm", "uninstall"]


def test_undeploy_clears_layer_counts_cache(monkeypatch, cfg, tmp_path):
    # stale cached counts would phantom-fill `status` after teardown; undeploy drops them.
    cfg.config_dir = str(tmp_path)
    cli.util._write_cache(cfg, {cfg.graph_id: {"2": 847, "3": 144}})
    monkeypatch.setattr(cli.kube, "list_jobs", lambda ns, workload=None: [])
    monkeypatch.setattr(cli.kube, "list_configmaps", lambda ns, sel: [])
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="released", stderr=""),
    )
    run_cmd(cli.undeploy, [], cfg)
    assert cli.util.cached_layer_counts(cfg) is None


def test_layer_counts_cache_round_trip(monkeypatch, cfg, tmp_path):
    cfg.config_dir = str(tmp_path)
    calls = []
    monkeypatch.setattr(
        cli.util,
        "_query_meta",
        lambda c, op, gid: calls.append(op) or "import noise\n100 50 1\n",
    )
    assert cli.util.read_layer_counts(cfg) == {2: 100, 3: 50, 4: 1}
    assert cli.util.read_layer_counts(cfg) == {2: 100, 3: 50, 4: 1}  # served from cache
    assert calls == ["counts"]  # ChunkedGraph hit exactly once
    assert cli.util.read_n(cfg, 3) == 50
    cli.util.invalidate_layer_counts(cfg)
    cli.util.read_layer_counts(cfg)
    assert calls == ["counts", "counts"]  # recomputed after invalidate


def test_layer_counts_failure_is_loud(monkeypatch, cfg):
    # unparseable pod output must never be read as empty/zero counts
    monkeypatch.setattr(
        cli.util, "_query_meta", lambda c, op, gid: "WARNING: only noise\n"
    )
    with pytest.raises(SystemExit, match="could not read layer counts"):
        cli.util.read_layer_counts(cfg)


def test_runs_command_reads_the_cost_db(cfg, seed_cost):
    assert run_cmd(cli.runs, [], cfg).output == ""  # empty db -> note (stderr), no table
    seed_cost("r1")
    assert "r1" in run_cmd(cli.runs, [], cfg).output  # rows -> table printed to stdout


def test_run_command_reads_the_cost_db(cfg, seed_cost):
    assert run_cmd(cli.run, ["r1"], cfg).output == ""  # unknown run -> note, no table
    seed_cost("r1", workload="meshing")
    assert (
        "r1" in run_cmd(cli.run, ["r1"], cfg).output
    )  # found -> breakdown (title "run r1")
