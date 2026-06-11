from pipeline import config


def test_load_defaults_and_bigtable_injection(tmp_path):
    (tmp_path / "pipeline.yml").write_text("""
graph_id: g
images: {pcg: repo/pcg:tag}
bigtable: {project: proj, instance: inst}
secret_files: {google-secret.json: projA/g.json}
""")
    (tmp_path / "dataset.yml").write_text("backend_client:\n  CONFIG: {ADMIN: true}\n")
    cfg = config.load(str(tmp_path))
    assert cfg.graph_id == "g"
    assert cfg.namespace == "default"  # default
    assert cfg.workload == "ingest"  # default
    assert cfg.persistent_util is True  # default
    assert cfg.secret_files == {"google-secret.json": "projA/g.json"}

    conf = cfg.dataset["backend_client"]["CONFIG"]
    assert conf["PROJECT"] == "proj" and conf["INSTANCE"] == "inst"
    assert conf["ADMIN"] is True  # operator value preserved


def test_bigtable_not_injected_when_absent(tmp_path):
    (tmp_path / "pipeline.yml").write_text("graph_id: g\nimages: {pcg: x:1}\n")
    cfg = config.load(str(tmp_path))
    assert "backend_client" not in cfg.dataset


def test_image_selects_by_workload(cfg):
    cfg.workload = "ingest"
    assert cfg.image() == "repo/pcg:tag"
    cfg.workload = "l2cache"
    assert cfg.image() == "repo/l2:tag"
    cfg.workload = "meshing"
    assert cfg.image() == "repo/pcg:tag"
