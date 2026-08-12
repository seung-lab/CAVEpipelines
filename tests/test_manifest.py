import pytest
from kubernetes import client

from cave_pipeline import cgcache, config, manifest


def _job(cfg, layer=2, chunks=100, completions=5, parallelism=3, **kw):
    """A Job as the API would see it, defaulting to a small layer-2 one."""
    return client.ApiClient().sanitize_for_serialization(
        manifest.job_spec(cfg, layer, chunks, completions, parallelism, **kw)
    )


def _pod_spec(job) -> dict:
    """The pod template's spec of a sanitized Job."""
    return job["spec"]["template"]["spec"]


def _container(job) -> dict:
    """The worker container of a sanitized Job."""
    return _pod_spec(job)["containers"][0]


def test_job_spec_completion_counts(cfg):
    spec = _job(cfg)["spec"]
    assert spec["completionMode"] == "Indexed"
    assert spec["completions"] == 5
    assert spec["parallelism"] == 3
    assert spec["backoffLimitPerIndex"] == cfg.job.task_retries
    # clamped: the API rejects maxFailedIndexes > completions (here 5 tasks, limit 50)
    assert spec["maxFailedIndexes"] == 5


def test_max_failed_indexes_passes_through_on_big_layers(cfg):
    spec = _job(cfg, chunks=1_000_000, completions=1000)["spec"]
    assert spec["maxFailedIndexes"] == cfg.job.max_failed_tasks


def test_per_layer_resource_curves(cfg):
    _curves(cfg, overrides={9: config.Override(cpu=30, memory=110)})
    assert manifest.requests_for(cfg.job, 2) == (1, 2)
    assert manifest.requests_for(cfg.job, 5) == (8, 9)
    assert manifest.requests_for(cfg.job, 8) == (28, 33)  # capped at the declared max
    assert manifest.requests_for(cfg.job, 9) == (30, 110)  # override wins


def test_requests_require_a_curve(cfg):
    # no resources block -> fail fast with a clear message, never a silent default
    cfg.job.resources = None
    with pytest.raises(SystemExit, match="resources.cpu is required"):
        manifest.requests_for(cfg.job, 2)


def _curves(cfg, compute_class="", overrides=None):
    """The doubling cpu/memory curves the layer tests share.

    `overrides` values may be dicts or ready-made Override objects."""
    cfg.job.compute_class = compute_class
    cfg.job.parallel = True
    cfg.job.resources = config.Resources(
        cpu=config.Curve(base=1, factor=2, max=28),
        memory=config.Curve(base=1, factor=2, add=1, max=33),
        overrides={
            k: v if isinstance(v, config.Override) else config.Override(**v)
            for k, v in (overrides or {}).items()
        },
    )
    return cfg


def test_packing_never_overrules_an_explicit_override(cfg):
    """An override names that layer's numbers exactly; growing them to fill a node would
    silently overrule the one instruction more specific than the curve."""
    _curves(cfg, overrides={5: {"cpu": 8, "memory": 9}})  # pack defaults on
    assert manifest.requests_for(cfg.job, 5) == (8, 9)
    assert manifest.normalized_requests(cfg.job, 5) == (8, 9)  # untouched by fill_node
    assert manifest.normalized_requests(cfg.job, 4)[0] > 4  # the curve still packs


def test_packing_never_provokes_a_clamp_warning(cfg):
    """A warning naming 30.75 for a curve that says 16 blames the operator for the
    packer's arithmetic, and its remedy (job.compute_class) is refused by config.load."""
    _curves(cfg)  # cpu max 28 -> layers 6+ would fill to 30.75, over the 30 GP ceiling
    for layer in range(2, 9):
        cpu, _, warnings = manifest._normalized(cfg.job, layer)
        assert warnings == [], (layer, cpu)
        assert cpu <= 30.0


def test_worker_processes_track_the_pods_cpu_request(cfg):
    """The pool must be bounded by the pod's own cpu, not mp.cpu_count() (= the node's cores)."""
    _curves(cfg)
    per = cfg.job.processes_per_vcpu
    assert manifest.layer_processes(cfg.job, 2) == per  # 1 vCPU
    assert manifest.layer_processes(cfg.job, 5) == 14 * per  # 8 vCPU fills a 16 -> 14.75
    cfg.job.parallel = False
    assert manifest.layer_processes(cfg.job, 5) == 1  # sequential: never fans out


def test_worker_processes_count_the_billed_cpu_not_the_raw_ask(cfg):
    """Autopilot bills a request rounded up to the 0.25-vCPU step, so 1.9 vCPU owns two
    cores; flooring the ask would run one process against a two-core bill."""
    _curves(cfg, overrides={3: {"cpu": 1.9, "memory": 4}})
    per = cfg.job.processes_per_vcpu
    assert manifest.layer_requests(cfg.job, 3)["cpu"] == "1900m"
    assert manifest.layer_processes(cfg.job, 3) == 2 * per
    # 2.6 bills as 2.75 — still two whole cores, never three
    _curves(cfg, overrides={3: {"cpu": 2.6, "memory": 6}})
    assert manifest.layer_processes(cfg.job, 3) == 2 * per


def test_worker_processes_follow_the_memory_ceiling_bump(cfg):
    """Memory past 6.5 GiB/vCPU raises cpu; the process count must follow that raise,
    since the pod is billed (and scheduled) for the larger request."""
    _curves(cfg, overrides={7: {"cpu": 2, "memory": 96}})
    # 96/6.5 = 14.77 -> billed 15 vCPU
    assert manifest.layer_processes(cfg.job, 7) == 15 * cfg.job.processes_per_vcpu


def test_processes_per_vcpu_is_tunable(cfg):
    """The oversubscription splits the pod's memory request that many ways, so a layer
    that OOMs needs a dial here; `parallel: false` is a cliff to 1, not a dial."""
    _curves(cfg)
    cfg.job.processes_per_vcpu = 1
    assert manifest.layer_processes(cfg.job, 5) == 14  # 14.75 billed vCPU, 1 worker each
    cfg.job.processes_per_vcpu = 4
    assert manifest.layer_processes(cfg.job, 5) == 56
    cfg.job.processes_per_vcpu = 0  # never zero workers
    assert manifest.layer_processes(cfg.job, 5) == 14


def test_worker_processes_on_a_custom_compute_class_skip_the_autopilot_grid(cfg):
    """normalize_requests passes non-default classes through untouched — there is no
    Autopilot grid to snap to. Node filling still applies: quota is charged per node vCPU
    whatever the class, and Scale-Out sits on the same T2D ladder."""
    _curves(cfg, compute_class="Scale-Out")
    assert manifest.layer_processes(cfg.job, 5) == 14 * cfg.job.processes_per_vcpu


def test_job_spec_ships_the_process_count_matching_the_request(cfg, container_env):
    _curves(cfg)
    job = _job(cfg, layer=5)
    # 14750m request x the oversubscription factor
    procs = str(14 * cfg.job.processes_per_vcpu)
    assert container_env(job)["PCG_N_PROCESSES"] == procs
    assert _container(job)["resources"]["requests"]["cpu"] == "14750m"


def test_job_spec_renders_layer_requests(cfg):
    _curves(cfg)
    req = _container(_job(cfg))["resources"]["requests"]
    assert req == {"cpu": "1000m", "memory": "2048Mi"}  # layer 2
    req = _container(_job(cfg, layer=5))["resources"]["requests"]
    # layer 5's 8 vCPU / 9 GiB grows to fill the 16-vCPU rung it already forced
    assert req == {"cpu": "14750m", "memory": "16992Mi"}


def test_job_spec_requests_without_limits(cfg):
    """A cpu limit would cap the pod at its request, forfeiting the burst into a node's
    spare cores; Autopilot bills the request either way, so the limit buys nothing."""
    res = _container(_job(cfg))["resources"]
    assert res["requests"]["cpu"] and "limits" not in res


def test_gp_ceiling_clamps_not_aborts(cfg):
    # a curve over the ceiling clamps to the GP max (with a warning), never aborts the run
    cfg.job.compute_class = ""
    cfg.job.resources = config.Resources(  # deliberately not _curves: drives the clamp
        cpu=config.Curve(base=40), memory=config.Curve(base=2)
    )
    req = _container(_job(cfg))["resources"]["requests"]
    assert req["cpu"] == "30000m"  # 40 vCPU clamped to the 30-vCPU ceiling, not refused


def test_pod_failure_policy_spot_vs_fatal(cfg):
    # spot preemption must NOT burn a retry; a fatal chunk (exit 42) must fail the index.
    rules = _job(cfg)["spec"]["podFailurePolicy"]["rules"]
    ignore = next(r for r in rules if r["action"] == "Ignore")
    assert ignore["onPodConditions"][0]["type"] == "DisruptionTarget"
    fail = next(r for r in rules if r["action"] == "FailIndex")
    assert fail["onExitCodes"]["operator"] == "In"
    assert fail["onExitCodes"]["values"] == [42]


def test_worker_env_targets_the_right_chunk(cfg, container_env):
    env = container_env(_job(cfg))
    assert env["PCG_GRAPH_ID"] == "g"
    assert env["PCG_LAYER"] == "2"
    assert env["PCG_PERM_SEED"] == "7"
    assert env["PCG_BATCH_SIZE"] == "1000"


def test_parallel_false_forces_a_single_process(cfg, container_env):
    _curves(cfg)

    def procs(layer):
        return container_env(_job(cfg, layer=layer))["PCG_N_PROCESSES"]

    # parallel: fan out over the pod's own 14.75 vCPU, never the node's cores
    assert procs(5) == str(14 * cfg.job.processes_per_vcpu)
    cfg.job.parallel = False
    assert procs(5) == "1"  # sequential escape hatch


def test_pods_terminate_promptly(cfg):
    """Autopilot defaults Spot pods to its 25s cap; a worker cannot finish a chunk in any
    grace period, so the wait only slows teardown and every preemption."""
    pod = _pod_spec(_job(cfg))
    assert pod["terminationGracePeriodSeconds"] == manifest.GRACE_SECONDS <= 25


def test_spot_scheduling(cfg):
    """A built-in class takes gke-spot alongside it; only a custom ComputeClass refuses the
    pair. Dropping the selector here would bill on-demand while costs still quote spot."""
    pod = _pod_spec(_job(cfg))  # cfg pins the built-in "Balanced"
    assert pod["nodeSelector"] == {
        "cloud.google.com/gke-spot": "true",
        "cloud.google.com/compute-class": "Balanced",
    }
    assert pod["tolerations"][0]["key"] == "cloud.google.com/gke-spot"


def test_status_annotations_and_optional_secret(cfg):
    job = _job(cfg)
    # run-id ("" with no active run) tags the Job for per-deploy cost attribution
    assert job["metadata"]["annotations"] == {
        "chunks": "100",
        "batch_size": "1000",
        "parallel": "True",
        "processes_per_vcpu": "2",
        "run-id": "",
    }
    assert _job(cfg, run_id="r1")["metadata"]["annotations"]["run-id"] == "r1"
    vol = _pod_spec(job)["volumes"][0]
    assert vol["secret"]["optional"] is True  # pods start even with no Secret (WI-only)


def test_sample_uses_batch_size_one(cfg):
    assert _job(cfg, batch_size=1)["metadata"]["annotations"]["batch_size"] == "1"


def test_batch_size_halves_per_layer(cfg, container_env):
    job = _job(cfg, layer=5)
    assert job["metadata"]["annotations"]["batch_size"] == "125"  # 1000 // 2^3
    # the worker slices by the same value the annotation reports
    assert container_env(job)["PCG_BATCH_SIZE"] == "125"
    deep = _job(cfg, layer=13, chunks=8, completions=8, parallelism=1)
    assert deep["metadata"]["annotations"]["batch_size"] == "1"  # floored, never 0


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


def test_l2cache_job_injects_cache_config_env(cfg, container_env):
    cfg.dataset["l2cache_config"] = {
        "cv_path": "graphene://h/segmentation/table/g",
        "table_id": "l2cache_g",
    }
    cfg.workload = "l2cache"
    env = container_env(_job(cfg))
    assert env["PCG_LAYER"] == "2"  # l2cache runs on the L2 grid
    assert env["L2CACHE_CV_PATH"] == "graphene://h/segmentation/table/g"
    assert env["L2CACHE_TABLE_ID"] == "l2cache_g"
    cfg.workload = "ingest"  # the cache config is scoped to the l2cache workload only
    assert "L2CACHE_CV_PATH" not in container_env(_job(cfg))


def test_env_injected_into_job_and_oneshot(cfg, container_env):
    cfg.env = {"TASK_SIZE": "1", "PROCESS_MULTIPLIER": "5", "BIGTABLE_PROJECT": None}
    job = manifest.job_spec(cfg, layer=2, chunks=100, completions=1, parallelism=1)
    job_env = container_env(job)
    assert job_env["TASK_SIZE"] == "1" and job_env["PROCESS_MULTIPLIER"] == "5"
    assert job_env["PCG_GRAPH_ID"] == cfg.graph_id  # alongside the built-in PCG_* vars
    # unset keys must be skipped, not injected as "None" (would override the ConfigMap)
    assert "BIGTABLE_PROJECT" not in job_env
    pod = manifest.oneshot_pod_spec(cfg, "u", ["python", "-c", "pass"])
    pod_env = container_env(pod)  # a Pod nests one level shallower than a Job
    assert pod_env["TASK_SIZE"] == "1"
    assert "PCG_LAYER" not in pod_env  # a probe pod is not a layer worker


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


def test_only_a_custom_compute_class_replaces_the_spot_selector(cfg):
    """GKE rejects a pod that pins gke-spot alongside a *custom* ComputeClass, which
    carries spot in its own priorities. A built-in class takes both, so dropping the
    selector for one bills on-demand while costs still quote spot. The toleration
    survives either way — it is a permit, never a request."""
    cfg.job.compute_class = "ingest-any"  # custom: not in BUILTIN_COMPUTE_CLASSES
    pod = manifest.job_spec(cfg, 2, 100, 1, 1).spec.template.spec
    assert pod.node_selector == {"cloud.google.com/compute-class": "ingest-any"}
    assert pod.tolerations[0].key == "cloud.google.com/gke-spot"

    for builtin in sorted(manifest.BUILTIN_COMPUTE_CLASSES):
        cfg.job.compute_class = builtin
        pod = manifest.job_spec(cfg, 2, 100, 1, 1).spec.template.spec
        assert pod.node_selector == {
            "cloud.google.com/gke-spot": "true",
            "cloud.google.com/compute-class": builtin,
        }, builtin

    cfg.job.compute_class = ""  # no class: spot has to be pinned by selector instead
    pod = manifest.job_spec(cfg, 2, 100, 1, 1).spec.template.spec
    assert pod.node_selector["cloud.google.com/gke-spot"] == "true"
    assert "cloud.google.com/compute-class" not in pod.node_selector


def test_compute_class_and_zone_coexist(cfg):
    """`zone` is not on Warden's forbidden-key list, so pinning both stays legal."""
    cfg.job.compute_class = "ingest-any"
    cfg.zone = "us-east1-b"
    ns = manifest.job_spec(cfg, 2, 100, 1, 1).spec.template.spec.node_selector
    assert ns == {
        "cloud.google.com/compute-class": "ingest-any",
        "topology.kubernetes.io/zone": "us-east1-b",
    }


def test_zone_pins_worker_pods(cfg):
    cfg.zone = "us-east1-b"
    ns = manifest.job_spec(cfg, 2, 100, 1, 1).spec.template.spec.node_selector
    assert ns["topology.kubernetes.io/zone"] == "us-east1-b"
    cfg.zone = ""
    ns = manifest.job_spec(cfg, 2, 100, 1, 1).spec.template.spec.node_selector
    assert "topology.kubernetes.io/zone" not in ns
