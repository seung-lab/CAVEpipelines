from types import SimpleNamespace

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
        cli.util, "run_pcg", lambda c, name, argv: seen.update(name=name, argv=argv) or ""
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
