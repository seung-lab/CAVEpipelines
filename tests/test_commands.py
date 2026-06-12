from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from kubernetes.client import ApiException

from pipeline import cli, manifest


def run_cmd(command, argv, cfg):
    """Invoke one click command with a prebuilt Config (no group, no config file)."""
    return CliRunner().invoke(command, argv, obj=cfg, catch_exceptions=False)


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
    run_cmd(cli.status, ["--once"], cfg)
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


def test_graph_id_flag_overrides_config(monkeypatch, cfg):
    monkeypatch.setattr(cli.config, "load", lambda name: cfg)
    seen = _capture_run_with_dataset(monkeypatch)
    CliRunner().invoke(
        cli.cli, ["-g", "other_graph", "mesh-meta"], catch_exceptions=False
    )
    assert seen["argv"][-1] == "other_graph"  # the override reaches the workload


def test_submit_refuses_jobs_owned_by_another_graph(cfg):
    job = SimpleNamespace(
        metadata=SimpleNamespace(name="ingest-l2", labels={"graph": "other"})
    )
    with pytest.raises(SystemExit, match="belongs to graph"):
        cli._check_graph_owner(job, cfg)
    cli._check_graph_owner(job, cfg, force=True)  # explicit override
    job.metadata.labels = {"graph": cfg.graph_id}
    cli._check_graph_owner(job, cfg)  # own job passes


def test_submit_blocks_until_prev_layer_complete(monkeypatch, cfg):
    running = SimpleNamespace(
        metadata=SimpleNamespace(name="ingest-l2", labels={"graph": cfg.graph_id}),
        status=SimpleNamespace(conditions=[]),
    )
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


def test_count_indexes_parses_k8s_interval_strings():
    assert cli.util.count_indexes(None) == 0
    assert cli.util.count_indexes("") == 0
    assert cli.util.count_indexes("1,3-5,7") == 5


def test_status_table_splits_retries_from_dead_tasks(monkeypatch, cfg):
    job = SimpleNamespace(
        metadata=SimpleNamespace(
            name="ingest-l2",
            labels={"layer": "2", "graph": "g"},
            annotations={"chunks": "100", "batch_size": "10"},
        ),
        status=SimpleNamespace(
            succeeded=10,
            active=0,
            ready=0,
            failed=34,  # transient attempts, all recovered
            failed_indexes=None,
            conditions=[],
            start_time=None,
            completion_time=None,
        ),
    )
    monkeypatch.setattr(cli.kube, "list_jobs", lambda ns, workload=None: [job])
    monkeypatch.setattr(cli.kube, "node_summary", lambda: (0, 0, {}))
    monkeypatch.setattr(cli.costs, "load_table", lambda: {})
    cells = {
        c.header: list(c._cells) for c in cli.util.status_table(cfg, {2: 100}).columns
    }
    assert cells["retries"] == ["34"]
    assert cells["failed"] == ["0"]  # nothing permanently dead -> not alarming
    job.status.failed_indexes = "1,3-5,7"
    cells = {
        c.header: list(c._cells) for c in cli.util.status_table(cfg, {2: 100}).columns
    }
    assert cells["failed"] == ["[red]5[/]"]


def test_usage_table_renders_cores_and_gib_by_task_index(monkeypatch, cfg):
    items = [
        {
            "metadata": {"name": "ingest-l6-11-abc"},
            "containers": [{"usage": {"cpu": "8913484669n", "memory": "6341544Ki"}}],
        },
        {
            "metadata": {"name": "ingest-l6-2-xyz"},
            "containers": [{"usage": {"cpu": "250m", "memory": "445480Ki"}}],
        },
    ]
    monkeypatch.setattr(cli.kube, "pod_metrics", lambda ns, name: items)
    cells = {
        c.header: list(c._cells) for c in cli.util.usage_table(cfg, "ingest-l6").columns
    }
    assert cells["pod"] == ["ingest-l6-2-xyz", "ingest-l6-11-abc"]  # task order
    assert cells["cpu"] == ["0.2", "8.9"]
    assert cells["memory"] == ["0.4Gi", "6.0Gi"]


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
    cm = SimpleNamespace(metadata=SimpleNamespace(name="pcg-dataset-g"))
    monkeypatch.setattr(cli.kube, "list_jobs", lambda ns, workload=None: [job])
    monkeypatch.setattr(cli.kube, "delete_job", lambda ns, name: deleted.append(name))
    monkeypatch.setattr(cli.kube, "list_configmaps", lambda ns, sel: [cm])
    monkeypatch.setattr(
        cli.kube, "delete_configmap", lambda ns, name: deleted.append(name)
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv, **kw: (
            ran.setdefault("argv", argv),
            SimpleNamespace(stdout="released", stderr=""),
        )[1],
    )
    run_cmd(cli.undeploy, [], cfg)
    assert deleted == ["ingest-l2", "pcg-dataset-g"]  # jobs, then dataset configmaps
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
    seen = _capture_run_with_dataset(monkeypatch)  # mesh meta reads mesh_config
    run_cmd(cli.mesh_meta, [], cfg)
    assert seen["name"] == "mesh-meta"
    assert seen["argv"] == [
        "python",
        "-m",
        "pychunkedgraph.pipeline.meshing.setup",
        cfg.graph_id,
    ]


def test_all_commands_registered():
    assert {
        "deploy",
        "undeploy",
        "setup",
        "mesh-meta",
        "submit",
        "scale",
        "sample",
        "status",
        "inspect",
        "pods",
        "events",
        "top",
        "delete",
        "costs",
    } <= set(cli.cli.commands)


def test_mesh_meta_dispatches_through_main(monkeypatch, cfg):
    monkeypatch.setattr(cli.config, "load", lambda path: cfg)
    ran = {}
    monkeypatch.setattr(
        cli.mesh_meta, "callback", lambda *a, **k: ran.setdefault("ok", True)
    )
    try:
        cli.main(["mesh-meta"])
    except SystemExit as exc:  # click standalone mode exits 0 on success
        assert not exc.code
    assert ran.get("ok")
