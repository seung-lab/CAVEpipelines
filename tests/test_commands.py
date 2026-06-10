from types import SimpleNamespace

from pipeline import cli, manifest


def test_command_for_routes_per_workload(cfg):
    cfg.workload = "ingest"
    assert manifest.command_for(cfg) == ["python", "-m", "pychunkedgraph.pipeline.ingest"]
    cfg.workload = "meshing"
    assert manifest.command_for(cfg) == ["python", "-m", "pychunkedgraph.pipeline.meshing"]
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
    assert seen["argv"] == ["python", "-m", "pychunkedgraph.pipeline.ingest.setup", cfg.graph_id]


def test_mesh_meta_runs_pipeline_meshing_setup(monkeypatch, cfg):
    seen = _capture_run_pcg(monkeypatch)
    cli.mesh_meta(cfg, SimpleNamespace())
    assert seen["name"] == "mesh-meta"
    assert seen["argv"] == ["python", "-m", "pychunkedgraph.pipeline.meshing.setup", cfg.graph_id]


def test_mesh_meta_subcommand_is_registered(monkeypatch, cfg):
    monkeypatch.setattr(cli.config, "load", lambda path: cfg)
    ran = {}
    monkeypatch.setattr(cli, "mesh_meta", lambda c, a: ran.setdefault("ok", True))
    cli.main(["mesh-meta"])
    assert ran.get("ok")
