from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from kubernetes.client import ApiException

from pipeline import cli, manifest, ops


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


def test_meshing_submit_requires_mesh_meta(monkeypatch, cfg):
    cfg.workload = "meshing"  # workers silently default mip=0 without it
    monkeypatch.setattr(ops.util, "mesh_meta_written", lambda c: False)
    with pytest.raises(SystemExit, match="mesh-meta"):
        ops.submit(cfg, 2)


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
    monkeypatch.setattr(cli.config, "load", lambda name, workload=None: cfg)
    monkeypatch.setattr(cli.util, "read_layer_counts", lambda c: None)  # no cluster I/O

    def boom(ns, workload=None):
        raise ApiException(status=403, reason="Forbidden")

    monkeypatch.setattr(cli.kube, "list_jobs", boom)
    with pytest.raises(SystemExit, match="403"):
        cli.main(["status"])


def test_status_quiet_when_no_jobs(monkeypatch, cfg):
    # cached a-priori counts persist locally across runs; they are NOT evidence of a
    # live deployment. With counts present but zero jobs, status must stay quiet.
    monkeypatch.setattr(cli.util, "read_layer_counts", lambda c: {2: 847, 3: 144})
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


def test_graph_id_flag_overrides_config(monkeypatch, cfg):
    monkeypatch.setattr(cli.config, "load", lambda name, workload=None: cfg)
    seen = _capture_run_with_dataset(monkeypatch)
    CliRunner().invoke(
        cli.cli, ["-g", "other_graph", "mesh-meta"], catch_exceptions=False
    )
    assert seen["argv"][-1] == "other_graph"  # the override reaches the workload


def test_submit_refuses_jobs_owned_by_another_graph(cfg, make_job):
    job = make_job(graph="other")
    with pytest.raises(SystemExit, match="belongs to graph"):
        ops.check_graph_owner(cfg, job)
    ops.check_graph_owner(cfg, job, force=True)  # explicit override
    job.metadata.labels = {"graph": cfg.graph_id}
    ops.check_graph_owner(cfg, job)  # own job passes


def test_submit_blocks_until_prev_layer_complete(monkeypatch, cfg, make_job):
    running = make_job(conditions=[])
    monkeypatch.setattr(
        cli.kube,
        "batch",
        lambda: SimpleNamespace(read_namespaced_job=lambda n, ns: running),
    )
    with pytest.raises(SystemExit, match="not complete"):
        ops.require_prev_complete(cfg, 3, force=False)
    ops.require_prev_complete(cfg, 3, force=True)  # override -> no raise
    ops.require_prev_complete(cfg, 2, force=False)  # L2 has no predecessor


def test_sample_runs_never_satisfy_the_layer_gate(monkeypatch, cfg, make_job):
    done_sample = make_job(
        conditions=[SimpleNamespace(type="Complete", status="True")],
        annotations={"sample": "true"},
    )
    monkeypatch.setattr(
        cli.kube,
        "batch",
        lambda: SimpleNamespace(read_namespaced_job=lambda n, ns: done_sample),
    )
    with pytest.raises(SystemExit, match="sample run"):
        ops.require_prev_complete(cfg, 3, force=False)


def test_sample_refuses_to_replace_a_real_job(monkeypatch, cfg, make_job):
    real = make_job(conditions=[])
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: real)
    with pytest.raises(SystemExit, match="already has a real job"):
        ops.sample(cfg, 2, 5)
    recreated = []
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: None)
    monkeypatch.setattr(cli.kube, "recreate_job", lambda ns, s: recreated.append(s))
    ops.sample(cfg, 2, 5)
    assert recreated[0].metadata.annotations["sample"] == "true"  # gate marker


def test_submit_sizes_job_and_ramps_to_pmax(monkeypatch, cfg, make_job):
    cfg.job.ramp = cli.config.Ramp(start=4, factor=2, period=60, max=256)
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: None)
    monkeypatch.setattr(ops.util, "read_n", lambda c, layer: 10001)
    created, scaled = [], []
    monkeypatch.setattr(cli.kube, "recreate_job", lambda ns, s: created.append(s))
    monkeypatch.setattr(cli.kube, "set_parallelism", lambda ns, n, p: scaled.append(p))
    monkeypatch.setattr(ops.costdb, "sample", lambda c: None)
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)
    ops.submit(cfg, 2)
    spec = created[0].spec
    assert spec.completions == 11  # ceil(10001 / 1000)
    assert spec.parallelism == 4  # ramp.start
    assert scaled == [8, 11]  # doubles, then caps at the task count


def test_submit_uses_the_layer_adjusted_batch(monkeypatch, cfg, make_job):
    done = make_job(conditions=[SimpleNamespace(type="Complete", status="True")])
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: done if layer == 2 else None)
    monkeypatch.setattr(ops.util, "read_n", lambda c, layer: 1000)
    created = []
    monkeypatch.setattr(cli.kube, "recreate_job", lambda ns, s: created.append(s))
    monkeypatch.setattr(cli.kube, "set_parallelism", lambda ns, n, p: None)
    monkeypatch.setattr(ops.costdb, "sample", lambda c: None)
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)
    ops.submit(cfg, 3)
    # completions and the worker's batch annotation must agree, or tasks mis-slice
    assert created[0].spec.completions == 2  # ceil(1000 / (1000 // 2))
    assert created[0].metadata.annotations["batch_size"] == "500"


def test_submit_blocks_when_prev_layer_missing(monkeypatch, cfg):
    def boom(name, ns):
        raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(
        cli.kube, "batch", lambda: SimpleNamespace(read_namespaced_job=boom)
    )
    with pytest.raises(SystemExit, match="submit it first"):
        ops.require_prev_complete(cfg, 3, force=False)


def test_count_indexes_parses_k8s_interval_strings():
    assert cli.util.count_indexes(None) == 0
    assert cli.util.count_indexes("") == 0
    assert cli.util.count_indexes("1,3-5,7") == 5


def test_status_table_splits_retries_from_dead_tasks(monkeypatch, cfg, make_job):
    job = make_job(chunks=100, batch_size=10, succeeded=10, failed=34)
    monkeypatch.setattr(cli.kube, "list_jobs", lambda ns, workload=None: [job])
    monkeypatch.setattr(cli.kube, "node_summary", lambda: (0, 0, {}))
    monkeypatch.setattr(cli.costs, "load_table", lambda: {})
    cells = {
        c.header: list(c.cells) for c in cli.util.status_table(cfg, {2: 100}).columns
    }
    assert cells["retries"] == ["34"]  # transient attempts, all recovered
    assert cells["failed"] == ["0"]  # nothing permanently dead -> not alarming
    job.status.failed_indexes = "1,3-5,7"
    cells = {
        c.header: list(c.cells) for c in cli.util.status_table(cfg, {2: 100}).columns
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
        c.header: list(c.cells) for c in cli.util.usage_table(cfg, "ingest-l6").columns
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
        "reset",
    } <= set(cli.cli.commands)


def test_layer_counts_failure_is_loud(monkeypatch, cfg):
    # unparseable pod output must never be read as empty/zero counts
    monkeypatch.setattr(
        cli.util, "_query_meta", lambda c, op, gid: "WARNING: only noise\n"
    )
    with pytest.raises(SystemExit, match="could not read layer counts"):
        cli.util.read_layer_counts(cfg)
