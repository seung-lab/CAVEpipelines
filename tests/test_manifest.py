import pytest
from kubernetes import client

from pipeline import cgcache, config, manifest


def _job(cfg, **kw):
    return client.ApiClient().sanitize_for_serialization(
        manifest.job_spec(cfg, 2, 100, 5, 3, **kw)
    )


def test_job_spec_completion_counts(cfg):
    spec = _job(cfg)["spec"]
    assert spec["completionMode"] == "Indexed"
    assert spec["completions"] == 5
    assert spec["parallelism"] == 3
    assert spec["backoffLimitPerIndex"] == cfg.job.task_retries
    # clamped: the API rejects maxFailedIndexes > completions (here 5 tasks, limit 50)
    assert spec["maxFailedIndexes"] == 5


def test_max_failed_indexes_passes_through_on_big_layers(cfg):
    spec = client.ApiClient().sanitize_for_serialization(
        manifest.job_spec(cfg, 2, 1_000_000, 1000, 3)
    )["spec"]
    assert spec["maxFailedIndexes"] == cfg.job.max_failed_tasks


def test_per_layer_resource_curves(cfg):
    cfg.job.compute_class = ""
    cfg.job.resources = config.Resources(
        cpu=config.Curve(base=1, factor=2, max=28),
        memory=config.Curve(base=1, factor=2, add=1, max=33),
        overrides={9: {"cpu": 30, "memory": 110}},
    )
    assert manifest.requests_for(cfg.job, 2) == (1, 2)
    assert manifest.requests_for(cfg.job, 5) == (8, 9)
    assert manifest.requests_for(cfg.job, 8) == (28, 33)  # capped at the declared max
    assert manifest.requests_for(cfg.job, 9) == (30, 110)  # override wins


def test_flat_fallback_without_resources_block(cfg):
    # no resources block -> job.cpu/job.memory verbatim, every layer
    assert manifest.requests_for(cfg.job, 2) == (1.0, 2.0)
    assert manifest.requests_for(cfg.job, 9) == (1.0, 2.0)


def test_job_spec_renders_layer_requests(cfg):
    cfg.job.compute_class = ""
    cfg.job.resources = config.Resources(
        cpu=config.Curve(base=1, factor=2, max=28),
        memory=config.Curve(base=1, factor=2, add=1, max=33),
    )
    req = _job(cfg)["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
    assert req == {"cpu": "1000m", "memory": "2048Mi"}  # layer 2
    spec = client.ApiClient().sanitize_for_serialization(
        manifest.job_spec(cfg, 5, 100, 5, 3)
    )
    req = spec["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
    assert req == {"cpu": "8000m", "memory": "9216Mi"}  # layer 5 scales up


def test_gp_ceiling_refuses_job(cfg):
    cfg.job.compute_class = ""
    cfg.job.resources = config.Resources(cpu=config.Curve(base=40))
    with pytest.raises(SystemExit, match="compute_class"):
        manifest.job_spec(cfg, 2, 100, 5, 3)


def test_pod_failure_policy_spot_vs_fatal(cfg):
    # spot preemption must NOT burn a retry; a fatal chunk (exit 42) must fail the index.
    rules = _job(cfg)["spec"]["podFailurePolicy"]["rules"]
    ignore = next(r for r in rules if r["action"] == "Ignore")
    assert ignore["onPodConditions"][0]["type"] == "DisruptionTarget"
    fail = next(r for r in rules if r["action"] == "FailIndex")
    assert fail["onExitCodes"]["operator"] == "In"
    assert fail["onExitCodes"]["values"] == [42]


def test_worker_env_targets_the_right_chunk(cfg):
    pod = _job(cfg)["spec"]["template"]["spec"]
    env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
    assert env["PCG_GRAPH_ID"] == "g"
    assert env["PCG_LAYER"] == "2"
    assert env["PCG_PERM_SEED"] == "7"
    assert env["PCG_BATCH_SIZE"] == "1000"


def test_parallel_flag_drives_builder_gate(cfg):
    env = {
        e["name"]: e["value"]
        for e in _job(cfg)["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert int(env["PCG_N_THREADS"]) > 1  # parallel builds on by default
    cfg.job.parallel = False
    env = {
        e["name"]: e["value"]
        for e in _job(cfg)["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["PCG_N_THREADS"] == "1"  # sequential escape hatch


def test_spot_scheduling(cfg):
    pod = _job(cfg)["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["cloud.google.com/gke-spot"] == "true"
    assert pod["nodeSelector"]["cloud.google.com/compute-class"] == "Balanced"
    assert pod["tolerations"][0]["key"] == "cloud.google.com/gke-spot"


def test_status_annotations_and_optional_secret(cfg):
    job = _job(cfg)
    # run-id ("" with no active run) tags the Job for per-deploy cost attribution
    assert job["metadata"]["annotations"] == {
        "chunks": "100",
        "batch_size": "1000",
        "run-id": "",
    }
    assert _job(cfg, run_id="r1")["metadata"]["annotations"]["run-id"] == "r1"
    vol = job["spec"]["template"]["spec"]["volumes"][0]
    assert vol["secret"]["optional"] is True  # pods start even with no Secret (WI-only)


def test_sample_uses_batch_size_one(cfg):
    assert _job(cfg, batch_size=1)["metadata"]["annotations"]["batch_size"] == "1"


def test_batch_size_halves_per_layer(cfg):
    spec = client.ApiClient().sanitize_for_serialization(
        manifest.job_spec(cfg, 5, 100, 5, 3)
    )
    assert spec["metadata"]["annotations"]["batch_size"] == "125"  # 1000 // 2^3
    env = {
        e["name"]: e["value"]
        for e in spec["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["PCG_BATCH_SIZE"] == "125"  # the worker slices by the same value
    spec = client.ApiClient().sanitize_for_serialization(
        manifest.job_spec(cfg, 13, 8, 8, 1)
    )
    assert spec["metadata"]["annotations"]["batch_size"] == "1"  # floored, never 0


def test_oneshot_pod_is_spot_oneshot(cfg):
    pod = client.ApiClient().sanitize_for_serialization(
        manifest.oneshot_pod_spec(cfg, "setup", ["python", "-c", "x"])
    )
    assert pod["kind"] == "Pod"
    assert pod["spec"]["restartPolicy"] == "Never"
    assert pod["spec"]["nodeSelector"]["cloud.google.com/gke-spot"] == "true"
    mounts = {m["mountPath"] for m in pod["spec"]["containers"][0]["volumeMounts"]}
    assert "/root/.cloudvolume/secrets" in mounts
    assert "/app/datasets" not in mounts  # dataset mount is opt-in (setup/mesh-meta)
    pod = client.ApiClient().sanitize_for_serialization(
        manifest.oneshot_pod_spec(cfg, "setup", ["x"], dataset_configmap="pcg-dataset-g")
    )
    vols = {v["name"]: v for v in pod["spec"]["volumes"]}
    assert vols["datasets"]["configMap"]["name"] == "pcg-dataset-g"


def test_dataset_configmap_name_is_dns_safe_and_distinct():
    name = manifest.dataset_configmap_name("My_Graph_v2")
    assert name.startswith("pcg-dataset-") and "_" not in name and name == name.lower()
    # ids that collide after sanitizing must yield distinct names
    assert manifest.dataset_configmap_name("a_b") != manifest.dataset_configmap_name(
        "a-b"
    )
    assert len(manifest.dataset_configmap_name("x" * 100)) <= 63


def test_helm_values_persistent_util_toggle(cfg):
    cfg.persistent_util = True
    dep = manifest.helm_values(cfg)["deployments"][0]
    assert dep["nodeSelector"]["cloud.google.com/gke-spot"] == "true"
    assert dep["tolerations"][0]["effect"] == "NoSchedule"
    # the persistent pod runs the warm cg-cache server, not the sleep-infinity default
    assert cgcache.SERVER_SRC in dep["containers"][0]["command"]
    cfg.persistent_util = False
    assert "deployments" not in manifest.helm_values(cfg)  # idle -> 0 nodes


def test_job_name_is_dns_safe_for_underscore_workloads(cfg):
    cfg.workload = "migrate_cleanup"  # raw "_" violates DNS-1123, API would 422
    assert manifest.job_name(cfg, 2) == "migrate-cleanup-l2"
    job = _job(cfg)
    assert job["spec"]["template"]["spec"]["containers"][0]["name"] == "migrate-cleanup"


def test_command_for_routes_per_workload(cfg):
    cfg.workload = "ingest"
    assert manifest.command_for(cfg) == manifest.INGEST_COMMAND  # built-in -> its command
    cfg.workload = "migrate_cleanup"  # the cleanup pass is the migrate worker + --clean
    assert manifest.command_for(cfg) == manifest.MIGRATE_COMMAND + ["--clean"]
    cfg.workload = "l2cache"  # built-in default ...
    assert manifest.command_for(cfg) == manifest.L2CACHE_COMMAND
    cfg.commands = {"l2cache": ["custom", "cmd"]}  # ... overridable from pipeline.yml
    assert manifest.command_for(cfg) == ["custom", "cmd"]


def test_l2cache_job_injects_cache_config_env(cfg):
    cfg.dataset["l2cache_config"] = {
        "cv_path": "graphene://h/segmentation/table/g",
        "table_id": "l2cache_g",
    }
    cfg.workload = "l2cache"
    env = {
        e["name"]: e["value"]
        for e in _job(cfg)["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["PCG_LAYER"] == "2"  # l2cache runs on the L2 grid
    assert env["L2CACHE_CV_PATH"] == "graphene://h/segmentation/table/g"
    assert env["L2CACHE_TABLE_ID"] == "l2cache_g"
    cfg.workload = "ingest"  # the cache config is scoped to the l2cache workload only
    other = {
        e["name"]: e["value"]
        for e in _job(cfg)["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "L2CACHE_CV_PATH" not in other


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
