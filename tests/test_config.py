import pytest
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


def test_bare_yaml_blocks_load_as_defaults(tmp_path, monkeypatch):
    # an operator can leave any block key present-but-empty (it parses to None)
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    (tmp_path / "pipeline.yml").write_text(
        "graph_id: g\nimages: {pcg: x:1}\n"
        "job:\nbigtable:\nworkload_identity:\nsecret_files:\ncommands:\n"
    )
    cfg = config.load()
    assert cfg.job.batch_size == 1000
    assert cfg.secret_files == {} and cfg.commands == {}


def test_non_growing_ramp_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "pipeline.yml", {**BASE, "job": {"ramp": {"factor": 1}}})
    with pytest.raises(SystemExit, match="ramp"):  # factor 1 would loop forever
        config.load()


def test_bigtable_not_injected_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    (tmp_path / "pipeline.yml").write_text("graph_id: g\nimages: {pcg: x:1}\n")
    cfg = config.load()
    assert "backend_client" not in cfg.dataset


def test_load_takes_any_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", "nonexistent")  # never consulted for -c
    _write(tmp_path, "projA.yml", {**BASE, "dataset": "projA-dataset.yml"})
    _write(tmp_path, "projA-dataset.yml", {"data_source": {"EDGES": "gs://a/e"}})
    cfg = config.load(str(tmp_path / "projA.yml"))
    assert cfg.dataset["data_source"]["EDGES"] == "gs://a/e"  # sibling dataset
    assert cfg.config_dir == str(tmp_path)  # counts cache colocates with the yaml


def test_default_config_is_under_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "pipeline.yml", BASE)
    assert config.load().source == str(tmp_path / "pipeline.yml")  # no -c -> default


def test_first_config_selects_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "pipeline.yml", BASE)
    other = str(tmp_path / "other.yml")
    _write(tmp_path, "other.yml", {**BASE, "namespace": "ns2"})
    assert config.resolve(other).namespace == "ns2"  # first -c selects
    assert config.resolve().namespace == "ns2"  # no -c: session config reused
    assert config.resolve(other).namespace == "ns2"  # same -c: fine


def test_switching_configs_requires_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "a.yml", BASE)
    _write(tmp_path, "b.yml", {**BASE, "namespace": "ns2"})
    config.resolve(str(tmp_path / "a.yml"))
    with pytest.raises(SystemExit, match="reset"):  # silent switch = wrong target
        config.resolve(str(tmp_path / "b.yml"))
    config.forget()
    assert config.resolve(str(tmp_path / "b.yml")).namespace == "ns2"


def test_unreadable_config_never_becomes_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "pipeline.yml", BASE)
    with pytest.raises(OSError):
        config.resolve(str(tmp_path / "missing.yml"))
    assert config.resolve().source.endswith("pipeline.yml")  # typo did not stick


def test_dataset_key_defaults_to_sibling_and_allows_subdirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "pipeline.yml", BASE)
    _write(tmp_path, "dataset.yml", {"data_source": {"EDGES": "gs://default/e"}})
    assert config.load().dataset["data_source"]["EDGES"] == "gs://default/e"
    (tmp_path / "my_project").mkdir()
    _write(tmp_path, "nested.yml", {**BASE, "dataset": "my_project/dataset.yml"})
    _write(tmp_path / "my_project", "dataset.yml", {"data_source": {"EDGES": "gs://n/e"}})
    nested = config.load(str(tmp_path / "nested.yml"))
    assert nested.dataset["data_source"]["EDGES"] == "gs://n/e"


def test_resource_curves_and_workload_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path,
        "pipeline.yml",
        {
            **BASE,
            "workload": "meshing",
            "job": {
                "batch_size": 1000,
                "resources": {
                    "cpu": {"base": 1, "factor": 2, "max": 28},
                    "overrides": {"9": {"cpu": 30}},
                },
                "workloads": {
                    "meshing": {
                        "batch_size": 250,
                        "resources": {"memory": {"base": 2, "max": 110}},
                    }
                },
            },
        },
    )
    cfg = config.load()
    assert cfg.job.batch_size == 250  # the workload's override wins
    assert cfg.job.resources.cpu.factor == 2  # shared curve survives the merge
    assert cfg.job.resources.memory.base == 2  # workload-added curve
    assert cfg.job.resources.overrides[9] == {"cpu": 30}  # int-coerced layer keys


def test_image_selects_by_workload(cfg):
    cfg.workload = "ingest"
    assert cfg.image() == cfg.images.pcg  # any non-l2cache workload -> the pcg image
    cfg.workload = "l2cache"
    assert cfg.image() == cfg.images.l2cache  # l2cache is the one on its own image
