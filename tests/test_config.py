import yaml

from pipeline import config

BASE = {"graph_id": "g", "images": {"pcg": "repo/pcg:t"}}


def _write(dirpath, name, content):
    (dirpath / name).write_text(yaml.safe_dump(content))


def test_load_defaults_and_bigtable_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    (tmp_path / "pipeline.yml").write_text("""
graph_id: g
images: {pcg: repo/pcg:tag}
bigtable: {project: proj, instance: inst}
secret_files: {google-secret.json: projA/g.json}
env:
""")
    (tmp_path / "dataset.yml").write_text("backend_client:\n  CONFIG: {ADMIN: true}\n")
    cfg = config.load()
    assert cfg.env == {}  # bare `env:` parses to None; load normalizes it
    assert cfg.graph_id == "g"
    assert cfg.namespace == "default"  # default
    assert cfg.workload == "ingest"  # default
    assert cfg.persistent_util is True  # default
    assert cfg.secret_files == {"google-secret.json": "projA/g.json"}

    conf = cfg.dataset["backend_client"]["CONFIG"]
    assert conf["PROJECT"] == "proj" and conf["INSTANCE"] == "inst"
    assert conf["ADMIN"] is True  # operator value preserved


def test_bigtable_not_injected_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    (tmp_path / "pipeline.yml").write_text("graph_id: g\nimages: {pcg: x:1}\n")
    cfg = config.load()
    assert "backend_client" not in cfg.dataset


def test_load_resolves_named_files_under_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "projA.yml", {**BASE, "dataset": "projA-dataset.yml"})
    _write(tmp_path, "projA-dataset.yml", {"data_source": {"EDGES": "gs://a/e"}})
    cfg = config.load("projA.yml")
    assert cfg.dataset["data_source"]["EDGES"] == "gs://a/e"
    assert cfg.graph_id == "g"


def test_dataset_key_defaults_to_sibling_and_allows_subdirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "pipeline.yml", BASE)
    _write(tmp_path, "dataset.yml", {"data_source": {"EDGES": "gs://default/e"}})
    assert config.load().dataset["data_source"]["EDGES"] == "gs://default/e"
    (tmp_path / "pinky").mkdir()
    _write(tmp_path, "nested.yml", {**BASE, "dataset": "pinky/dataset.yml"})
    _write(tmp_path / "pinky", "dataset.yml", {"data_source": {"EDGES": "gs://n/e"}})
    assert config.load("nested.yml").dataset["data_source"]["EDGES"] == "gs://n/e"


def test_image_selects_by_workload(cfg):
    cfg.workload = "ingest"
    assert cfg.image() == "repo/pcg:tag"
    cfg.workload = "l2cache"
    assert cfg.image() == "repo/l2:tag"
    cfg.workload = "meshing"
    assert cfg.image() == "repo/pcg:tag"
