import pytest
import yaml

from cave_pipeline import config

BASE = {"graph_id": "g", "images": {"pcg": "repo/pcg:v3.2.0"}}


def _write(dirpath, name, content):
    (dirpath / name).write_text(yaml.safe_dump(content))


FINAL = config._FINAL


@pytest.mark.parametrize(
    "image, version",
    [
        ("caveconnectome/pychunkedgraph:v3.2.0", (3, 2, 0, FINAL, 0)),
        ("caveconnectome/pychunkedgraph:3.2.0", (3, 2, 0, FINAL, 0)),  # v optional
        ("caveconnectome/pychunkedgraph:v3.2", (3, 2, 0, FINAL, 0)),  # patch -> 0
        ("caveconnectome/pychunkedgraph:v3.2.0.dev4", (3, 2, 0, 0, 4)),
        ("caveconnectome/pychunkedgraph:v3.2.0rc1", (3, 2, 0, 3, 1)),  # no separator
        ("caveconnectome/pychunkedgraph:v3.10.0", (3, 10, 0, FINAL, 0)),  # not lexical
        ("localhost:5000/pcg:v4.0.0", (4, 0, 0, FINAL, 0)),  # port is not the tag
        ("caveconnectome/pychunkedgraph:vNewIngest6", ()),
        ("caveconnectome/pychunkedgraph:latest", ()),
        ("caveconnectome/pychunkedgraph", ()),  # untagged
        ("caveconnectome/pychunkedgraph@sha256:abc123", ()),  # digest-pinned
        ("localhost:5000/pcg", ()),  # port, still untagged
    ],
)
def test_image_version_reads_the_tag(image, version):
    assert config.image_version(image) == version


def test_prerelease_ordering_puts_dev_below_its_release():
    """dev3 < dev4 < rc1 < the 3.2.0 release — the floor sits between dev3 and dev4."""
    tags = ["v3.2.0.dev3", "v3.2.0.dev4", "v3.2.0rc1", "v3.2.0", "v3.2.1"]
    versions = [config.image_version(f"repo/pcg:{t}") for t in tags]
    assert versions == sorted(versions)


@pytest.mark.parametrize(
    "tag",
    [
        "vNewIngest6",  # the tag that ran 2h and built nothing
        "latest",
        "v3.1.9",
        "v2.22.0.dev8",  # newer by date, older by version
        "v3.2.0.dev3",  # clears the entrypoint bar, still pools on the node's cores
        "v3.2.0.dev0",
    ],
)
def test_load_rejects_a_pcg_image_below_the_floor(tmp_path, monkeypatch, tag):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "pipeline.yml", {**BASE, "images": {"pcg": f"repo/pcg:{tag}"}})
    with pytest.raises(SystemExit, match=config.MIN_PCG_IMAGE):
        config.load(str(tmp_path / "pipeline.yml"))


@pytest.mark.parametrize(
    "tag", ["v3.2.0.dev4", "v3.2.0.dev5", "v3.2.0rc1", "v3.2.0", "v3.3.0", "v4.0.0"]
)
def test_load_accepts_the_floor_and_above(tmp_path, monkeypatch, tag):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "pipeline.yml", {**BASE, "images": {"pcg": f"repo/pcg:{tag}"}})
    assert config.load(str(tmp_path / "pipeline.yml")).images.pcg == f"repo/pcg:{tag}"


def test_load_defaults_and_bigtable_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    (tmp_path / "pipeline.yml").write_text("""
graph_id: g
images: {pcg: repo/pcg:v3.2.0}
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
        "graph_id: g\nimages: {pcg: x:v3.2.0}\n"
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
    (tmp_path / "pipeline.yml").write_text("graph_id: g\nimages: {pcg: x:v3.2.0}\n")
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
    with pytest.raises(SystemExit, match="config not found"):
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
