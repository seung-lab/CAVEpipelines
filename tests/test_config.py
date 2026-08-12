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
    """dev5 < dev6 < rc1 < the 3.2.0 release — the floor sits between dev5 and dev6."""
    tags = ["v3.2.0.dev5", "v3.2.0.dev6", "v3.2.0rc1", "v3.2.0", "v3.2.1"]
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
        "v3.2.0.dev4",  # ingest pools fixed, meshing stitch still single-process
        "v3.2.0.dev5",  # both pools fixed, but pins a harness emitting n_threads
        "v3.2.0.dev0",
    ],
)
def test_load_rejects_a_pcg_image_below_the_floor(tmp_path, tag):
    _write(tmp_path, "pipeline.yml", {**BASE, "images": {"pcg": f"repo/pcg:{tag}"}})
    with pytest.raises(SystemExit, match=config.MIN_PCG_IMAGE):
        config.load(str(tmp_path / "pipeline.yml"))


def _scoped(bad):
    return {"job": {"workloads": {"ingest": {"start_layer": bad}}}}


@pytest.mark.parametrize("bad", [1, 0, -1])
def test_start_layer_below_the_atomic_layer_is_refused(tmp_path, bad):
    """L2 is the atomic layer; anything lower would skip real work silently."""
    _write(tmp_path, "pipeline.yml", {**BASE, **_scoped(bad)})
    with pytest.raises(SystemExit, match="start_layer must be >= 2"):
        config.load(str(tmp_path / "pipeline.yml"))


@pytest.mark.parametrize("bad", [3.9, 2.5, None, "three", [3]])
def test_start_layer_must_be_a_whole_number(tmp_path, bad):
    """3.9 must not silently become 3, and a string must not reach the run as a string."""
    _write(tmp_path, "pipeline.yml", {**BASE, **_scoped(bad)})
    with pytest.raises(SystemExit, match="start_layer must be"):
        config.load(str(tmp_path / "pipeline.yml"))


def test_top_level_start_layer_is_refused(tmp_path):
    """Unscoped it reaches every workload: skipping ingest L3 would also skip meshing's
    L2 marching-cubes pass, and l2cache (top layer 2) would have nothing left to run."""
    _write(tmp_path, "pipeline.yml", {**BASE, "job": {"start_layer": 3}})
    with pytest.raises(SystemExit, match="scoped to one workload"):
        config.load(str(tmp_path / "pipeline.yml"))


def test_misspelled_workload_key_is_refused(tmp_path):
    """A typo silently drops the block, so a mid-graph restart quietly re-ingests L2."""
    bad = {"job": {"workloads": {"l2cach": {"start_layer": 5}}}}
    _write(tmp_path, "pipeline.yml", {**BASE, **bad})
    with pytest.raises(SystemExit, match="is not a workload"):
        config.load(str(tmp_path / "pipeline.yml"))


def test_every_workloads_start_layer_is_validated_up_front(tmp_path):
    """A sibling stage's typo must fail at load, not from inside a running deploy."""
    bad = {"job": {"workloads": {"meshing": {"start_layer": "three"}}}}
    _write(tmp_path, "pipeline.yml", {**BASE, **bad})
    with pytest.raises(SystemExit, match="start_layer must be"):
        config.load(str(tmp_path / "pipeline.yml"), workload="ingest")


@pytest.mark.parametrize("bad", [1.5, "two", [2], -1])
def test_processes_per_vcpu_must_be_a_whole_count(tmp_path, bad):
    """Stored raw it breaks inside the pod: 1.5 ships PCG_N_PROCESSES="21.0", and a
    non-numeric value raises TypeError mid-deploy, after the cluster is mutated."""
    _write(tmp_path, "pipeline.yml", {**BASE, "job": {"processes_per_vcpu": bad}})
    with pytest.raises(SystemExit, match="processes_per_vcpu must be"):
        config.load()


def test_processes_per_vcpu_accepts_a_whole_count(tmp_path):
    _write(tmp_path, "pipeline.yml", {**BASE, "job": {"processes_per_vcpu": 4}})
    assert config.load().job.processes_per_vcpu == 4
    _write(tmp_path, "pipeline.yml", {**BASE, "job": {"processes_per_vcpu": 0}})
    assert config.load().job.processes_per_vcpu == 0  # 0 and 1 both mean one worker
    # a quoted int is coerced, not refused: yaml quoting is not an operator's intent
    _write(tmp_path, "pipeline.yml", {**BASE, "job": {"processes_per_vcpu": "2"}})
    assert config.load().job.processes_per_vcpu == 2


def test_empty_job_scalar_takes_its_declared_default(tmp_path):
    """`job:` scalars are splatted into Job, so an empty key used to land as None and
    reach the pods instead of the default."""
    (tmp_path / "pipeline.yml").write_text(
        "graph_id: g\nimages: {pcg: x:v3.2.0}\n"
        "job:\n  processes_per_vcpu:\n  batch_size:\n  parallel: false\n"
    )
    job = config.load().job
    assert job.processes_per_vcpu == config.PROCESSES_PER_VCPU
    assert job.batch_size == 1000
    assert job.parallel is False  # an explicit false is a value, not "unset"


def test_accepted_compute_classes_are_all_selectable():
    """An accepted class GKE does not offer stamps a selector naming nothing and the pods
    stay Pending. Nothing else pins these two sets, which is how a billing SKU got in."""
    from cave_pipeline import manifest

    named = {c for c in config.POD_BILLED_CLASSES if c}
    assert named <= manifest.BUILTIN_COMPUTE_CLASSES
    assert "" in config.POD_BILLED_CLASSES  # the default class: selector omitted
    # a billing SKU is not a selector value
    assert "general-purpose" not in named | manifest.BUILTIN_COMPUTE_CLASSES


def test_known_workloads_match_the_stage_registry():
    """config cannot import stages (circular), so this pins the hand-kept copy."""
    from cave_pipeline import stages

    assert set(config._WORKLOADS) == set(stages.STAGES)


def test_start_layer_applies_only_to_its_own_workload(tmp_path):
    _write(tmp_path, "pipeline.yml", {**BASE, **_scoped(3)})
    path = str(tmp_path / "pipeline.yml")
    assert config.load(path, workload="ingest").job.start_layer == 3
    assert config.load(path, workload="meshing").job.start_layer == 2


def test_start_layer_defaults_to_the_atomic_layer(tmp_path):
    _write(tmp_path, "pipeline.yml", BASE)
    assert config.load(str(tmp_path / "pipeline.yml")).job.start_layer == 2


@pytest.mark.parametrize(
    "tag", ["v3.2.0.dev6", "v3.2.0.dev7", "v3.2.0rc1", "v3.2.0", "v3.3.0", "v4.0.0"]
)
def test_load_accepts_the_floor_and_above(tmp_path, tag):
    _write(tmp_path, "pipeline.yml", {**BASE, "images": {"pcg": f"repo/pcg:{tag}"}})
    assert config.load(str(tmp_path / "pipeline.yml")).images.pcg == f"repo/pcg:{tag}"


def test_load_defaults_and_bigtable_injection(tmp_path):
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


def test_bare_yaml_blocks_load_as_defaults(tmp_path):
    # an operator can leave any block key present-but-empty (it parses to None)
    (tmp_path / "pipeline.yml").write_text(
        "graph_id: g\nimages: {pcg: x:v3.2.0}\n"
        "job:\nbigtable:\nworkload_identity:\nsecret_files:\ncommands:\n"
    )
    cfg = config.load()
    assert cfg.job.batch_size == 1000
    assert cfg.secret_files == {} and cfg.commands == {}


def test_non_growing_ramp_is_rejected(tmp_path):
    _write(tmp_path, "pipeline.yml", {**BASE, "job": {"ramp": {"factor": 1}}})
    with pytest.raises(SystemExit, match="ramp"):  # factor 1 would loop forever
        config.load()


def test_node_billed_compute_class_is_rejected(tmp_path):
    """Naming a machine series bills the whole node plus a management premium, so the
    fleet's cost stops tracking its requests — the reason the cluster is Autopilot."""
    _write(tmp_path, "pipeline.yml", {**BASE, "job": {"compute_class": "ingest-any"}})
    with pytest.raises(SystemExit, match="bills per node"):
        config.load()


def test_pod_billed_compute_classes_are_accepted(tmp_path):
    for cls in config.POD_BILLED_CLASSES:
        _write(tmp_path, "pipeline.yml", {**BASE, "job": {"compute_class": cls}})
        assert config.load().job.compute_class == cls


def test_a_workload_override_cannot_smuggle_in_a_node_billed_class(tmp_path):
    """The check runs after the per-workload merge; before it, `job.workloads.ingest`
    could set a class the top-level gate never saw."""
    _write(
        tmp_path,
        "pipeline.yml",
        {**BASE, "job": {"workloads": {"ingest": {"compute_class": "ingest-any"}}}},
    )
    with pytest.raises(SystemExit, match="bills per node"):
        config.load(workload="ingest")


def test_bad_yaml_names_the_file_whichever_one_it_is(tmp_path):
    """The identical typo used to print a clean one-liner for pipeline.yml and a pyyaml
    traceback for dataset.yml — the guard existed at one site, not as a rule."""
    bad = "graph_id: g\n  images: {pcg: x:v3.2.0}\n"  # bad indent
    (tmp_path / "pipeline.yml").write_text(bad)
    with pytest.raises(SystemExit, match="invalid yaml in .*pipeline.yml"):
        config.load()

    _write(tmp_path, "pipeline.yml", {**BASE, "dataset": "dataset.yml"})
    (tmp_path / "dataset.yml").write_text(bad)
    with pytest.raises(SystemExit, match="invalid yaml in .*dataset.yml"):
        config.load()


def test_override_keyed_by_a_non_layer_is_named(tmp_path):
    resources = {"cpu": {"base": 1}, "overrides": {"L5": {"cpu": 30}}}
    _write(tmp_path, "pipeline.yml", {**BASE, "job": {"resources": resources}})
    with pytest.raises(SystemExit, match="'L5' is not a layer number"):
        config.load()


def test_present_but_empty_keys_take_the_declared_default(tmp_path):
    """`namespace:` used to yield None and every kube call then targeted namespace None;
    `region:` yielded None so no rate row matched and cost read zero. A key left empty is
    unset — but `false` is a value the operator chose."""
    (tmp_path / "pipeline.yml").write_text(
        "graph_id: g\nimages: {pcg: x:v3.2.0}\n"
        "namespace:\nregion:\nzone:\nsecret_name:\n"
        "persistent_util: false\n"
        "bigtable: {project: , instance: inst}\n"
        "job: {ramp: {start: , factor: 3}}\n"
    )
    cfg = config.load()
    assert cfg.namespace == "default"
    assert cfg.region == "" and cfg.zone == ""
    assert cfg.secret_name == "cloud-volume-secrets"
    assert cfg.persistent_util is False  # an explicit false is not "unset"
    assert cfg.bigtable.project == "" and cfg.bigtable.instance == "inst"
    assert (
        cfg.job.ramp.start == 4 and cfg.job.ramp.factor == 3
    )  # empty key inside a block


def test_mistyped_override_dimension_is_an_error_not_a_curve_fallback(tmp_path):
    """`cpu_` used to load clean and resolve to the cpu curve, shipping a 1-vCPU layer where
    30 was asked for — the one config block that was never validated."""
    resources = {"cpu": {"base": 1}, "memory": {"base": 2}}
    _write(
        tmp_path,
        "pipeline.yml",
        {**BASE, "job": {"resources": {**resources, "overrides": {"5": {"cpu_": 30}}}}},
    )
    with pytest.raises(SystemExit, match=r"overrides\[5\]: unknown cpu_"):
        config.load()


def test_fingerprint_survives_missing_paths_and_tracks_content(tmp_path):
    """A graph-less workload has no dataset path and a deleted file must not crash the
    comparison — but any content change has to move the digest."""
    a = tmp_path / "a.yml"
    a.write_text("x: 1")
    stable = config.fingerprint_of(str(a), "")
    assert stable == config.fingerprint_of(str(a), "")
    assert stable == config.fingerprint_of(str(a), str(tmp_path / "gone.yml"))
    a.write_text("x: 2")
    assert stable != config.fingerprint_of(str(a), "")


def test_bigtable_not_injected_when_absent(tmp_path):
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


def test_default_config_is_under_config_dir(tmp_path):
    _write(tmp_path, "pipeline.yml", BASE)
    assert config.load().source == str(tmp_path / "pipeline.yml")  # no -c -> default


def test_first_config_selects_the_session(tmp_path):
    _write(tmp_path, "pipeline.yml", BASE)
    other = str(tmp_path / "other.yml")
    _write(tmp_path, "other.yml", {**BASE, "namespace": "ns2"})
    assert config.resolve(other).namespace == "ns2"  # first -c selects
    assert config.resolve().namespace == "ns2"  # no -c: session config reused
    assert config.resolve(other).namespace == "ns2"  # same -c: fine


def test_switching_configs_requires_reset(tmp_path):
    _write(tmp_path, "a.yml", BASE)
    _write(tmp_path, "b.yml", {**BASE, "namespace": "ns2"})
    config.resolve(str(tmp_path / "a.yml"))
    with pytest.raises(SystemExit, match="reset"):  # silent switch = wrong target
        config.resolve(str(tmp_path / "b.yml"))
    config.forget()
    assert config.resolve(str(tmp_path / "b.yml")).namespace == "ns2"


def test_unreadable_config_never_becomes_the_session(tmp_path):
    _write(tmp_path, "pipeline.yml", BASE)
    with pytest.raises(SystemExit, match="config not found"):
        config.resolve(str(tmp_path / "missing.yml"))
    assert config.resolve().source.endswith("pipeline.yml")  # typo did not stick


def test_dataset_key_defaults_to_sibling_and_allows_subdirs(tmp_path):
    _write(tmp_path, "pipeline.yml", BASE)
    _write(tmp_path, "dataset.yml", {"data_source": {"EDGES": "gs://default/e"}})
    assert config.load().dataset["data_source"]["EDGES"] == "gs://default/e"
    (tmp_path / "my_project").mkdir()
    _write(tmp_path, "nested.yml", {**BASE, "dataset": "my_project/dataset.yml"})
    _write(tmp_path / "my_project", "dataset.yml", {"data_source": {"EDGES": "gs://n/e"}})
    nested = config.load(str(tmp_path / "nested.yml"))
    assert nested.dataset["data_source"]["EDGES"] == "gs://n/e"


def test_resource_curves_and_workload_merge(tmp_path):
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
    # int-coerced layer keys; memory absent means "fall back to the curve"
    assert cfg.job.resources.overrides[9] == config.Override(cpu=30, memory=None)


def test_image_selects_by_workload(cfg):
    cfg.workload = "ingest"
    assert cfg.image() == cfg.images.pcg  # any non-l2cache workload -> the pcg image
    cfg.workload = "l2cache"
    assert cfg.image() == cfg.images.l2cache  # l2cache is the one on its own image
