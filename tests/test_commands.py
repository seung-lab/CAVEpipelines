from types import SimpleNamespace

import pytest
from kubernetes.client import ApiException

from pipeline import cli, manifest


def test_command_for_routes_per_workload(cfg):
    cfg.workload = "ingest"
    assert manifest.command_for(cfg) == ["python", "-m", "pychunkedgraph.pipeline.ingest"]
    cfg.workload = "meshing"
    assert manifest.command_for(cfg) == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.meshing",
    ]
    cfg.workload = "migrate"
    assert manifest.command_for(cfg) == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.migrate",
    ]
    cfg.workload = "migrate_cleanup"
    assert manifest.command_for(cfg) == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.migrate",
        "--clean",
    ]
    cfg.workload = "l2cache"  # no built-in entrypoint -> from cfg.commands (empty here)
    assert manifest.command_for(cfg) is None


def _capture_run_pcg(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli.util,
        "run_pcg",
        lambda c, name, argv, **kw: seen.update(name=name, argv=argv) or "",
    )
    return seen


def test_setup_runs_pipeline_ingest_setup(monkeypatch, cfg):
    seen = _capture_run_pcg(monkeypatch)
    cli.setup(cfg, SimpleNamespace(raw=False))
    assert seen["argv"] == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.ingest.setup",
        cfg.graph_id,
    ]


def test_setup_runs_migrate_setup_for_migrate_workload(monkeypatch, cfg):
    cfg.workload = "migrate"
    seen = _capture_run_pcg(monkeypatch)
    cli.setup(cfg, SimpleNamespace(raw=False))
    assert seen["argv"] == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.migrate.setup",
        cfg.graph_id,
    ]


def test_env_injected_into_job_and_oneshot(cfg):
    cfg.env = {"TASK_SIZE": "1", "PROCESS_MULTIPLIER": "5", "BIGTABLE_PROJECT": None}
    job = manifest.job_spec(cfg, layer=2, chunks=100, completions=1, parallelism=1)
    job_env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    assert job_env["TASK_SIZE"] == "1" and job_env["PROCESS_MULTIPLIER"] == "5"
    assert job_env["PCG_GRAPH_ID"] == cfg.graph_id  # alongside the built-in PCG_* vars
    # unset keys must be skipped, not injected as "None" (would override the ConfigMap)
    assert "BIGTABLE_PROJECT" not in job_env
    pod = manifest.oneshot_pod_spec(cfg, "u", ["python", "-c", "pass"])
    pod_env = {e.name: e.value for e in pod.spec.containers[0].env}
    assert pod_env["TASK_SIZE"] == "1"


def test_api_errors_exit_cleanly(monkeypatch, cfg):
    monkeypatch.setattr(cli.config, "load", lambda path: cfg)

    def boom(ns, workload=None):
        raise ApiException(status=403, reason="Forbidden")

    monkeypatch.setattr(cli.kube, "list_jobs", boom)
    with pytest.raises(SystemExit, match="403"):
        cli.main(["status"])


def test_status_quiet_when_no_jobs(monkeypatch, cfg):
    monkeypatch.setattr(cli.util, "read_layer_counts", lambda c: None)
    monkeypatch.setattr(cli.kube, "list_jobs", lambda ns, workload=None: [])
    built = {}
    monkeypatch.setattr(
        cli.util, "status_table", lambda c, t=None: built.setdefault("yes", True)
    )
    cli.status(cfg, SimpleNamespace(once=True))
    assert not built  # no table rendered, just a note


def test_layer_counts_cache_round_trip(monkeypatch, cfg, tmp_path):
    cfg.config_dir = str(tmp_path)
    calls = []
    monkeypatch.setattr(
        cli.util,
        "run_pcg",
        lambda c, name, argv, **kw: calls.append(name) or "import noise\n100 50 1\n",
    )
    assert cli.util.read_layer_counts(cfg) == {2: 100, 3: 50, 4: 1}
    assert cli.util.read_layer_counts(cfg) == {2: 100, 3: 50, 4: 1}  # served from cache
    assert calls == ["layer-counts"]  # ChunkedGraph hit exactly once
    assert cli.util.read_n(cfg, 3) == 50
    cli.util.invalidate_layer_counts(cfg)
    cli.util.read_layer_counts(cfg)
    assert calls == ["layer-counts", "layer-counts"]  # recomputed after invalidate


def test_submit_blocks_until_prev_layer_complete(monkeypatch, cfg):
    running = SimpleNamespace(status=SimpleNamespace(conditions=[]))
    monkeypatch.setattr(
        cli.kube,
        "batch",
        lambda: SimpleNamespace(read_namespaced_job=lambda n, ns: running),
    )
    with pytest.raises(SystemExit, match="not complete"):
        cli._require_prev_complete(cfg, 3, force=False)
    cli._require_prev_complete(cfg, 3, force=True)  # override -> no raise
    cli._require_prev_complete(cfg, 2, force=False)  # L2 has no predecessor


def test_submit_blocks_when_prev_layer_missing(monkeypatch, cfg):
    def boom(name, ns):
        raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(
        cli.kube, "batch", lambda: SimpleNamespace(read_namespaced_job=boom)
    )
    with pytest.raises(SystemExit, match="submit it first"):
        cli._require_prev_complete(cfg, 3, force=False)


def test_status_table_shows_pending_layers(monkeypatch, cfg):
    def _raise(*a, **k):
        raise Exception("no nodes")

    monkeypatch.setattr(cli.kube, "list_jobs", lambda ns, workload=None: [])
    monkeypatch.setattr(cli.kube, "node_summary", _raise)
    monkeypatch.setattr(cli.costs, "load_table", lambda: {})
    table = cli.util.status_table(cfg, {2: 100, 3: 200})
    assert table.row_count == 2  # both layers shown though none submitted


def test_undeploy_deletes_jobs_then_uninstalls_release(monkeypatch, cfg):
    deleted, ran = [], {}
    job = SimpleNamespace(metadata=SimpleNamespace(name="ingest-l2"))
    monkeypatch.setattr(cli.kube, "list_jobs", lambda ns, workload=None: [job])
    monkeypatch.setattr(cli.kube, "delete_job", lambda ns, name: deleted.append(name))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv, **kw: (
            ran.setdefault("argv", argv),
            SimpleNamespace(stdout="released", stderr=""),
        )[1],
    )
    cli.undeploy(cfg, SimpleNamespace())
    assert deleted == ["ingest-l2"]
    assert ran["argv"][:2] == ["helm", "uninstall"]


def test_helm_values_carry_secret(cfg):
    vals = manifest.helm_values(cfg, {"google-secret.json": "YjY0"})
    assert vals["secrets"] == [
        {
            "name": cfg.secret_name,
            "namespace": cfg.namespace,
            "data": {"google-secret.json": "YjY0"},
        }
    ]
    assert manifest.helm_values(cfg)["secrets"] == []  # no files -> no Secret rendered


def test_zone_pins_worker_pods(cfg):
    cfg.zone = "us-east1-b"
    ns = manifest.job_spec(cfg, 2, 100, 1, 1).spec.template.spec.node_selector
    assert ns["topology.kubernetes.io/zone"] == "us-east1-b"
    cfg.zone = ""
    ns = manifest.job_spec(cfg, 2, 100, 1, 1).spec.template.spec.node_selector
    assert "topology.kubernetes.io/zone" not in ns


def test_mesh_meta_runs_pipeline_meshing_setup(monkeypatch, cfg):
    seen = _capture_run_pcg(monkeypatch)
    cli.mesh_meta(cfg, SimpleNamespace())
    assert seen["name"] == "mesh-meta"
    assert seen["argv"] == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.meshing.setup",
        cfg.graph_id,
    ]


def test_mesh_meta_subcommand_is_registered(monkeypatch, cfg):
    monkeypatch.setattr(cli.config, "load", lambda path: cfg)
    ran = {}
    monkeypatch.setattr(cli, "mesh_meta", lambda c, a: ran.setdefault("ok", True))
    cli.main(["mesh-meta"])
    assert ran.get("ok")
