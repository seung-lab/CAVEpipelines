from kubernetes import client

from pipeline import manifest


def _job(cfg, **kw):
    return client.ApiClient().sanitize_for_serialization(
        manifest.job_spec(cfg, 2, 100, 5, 3, **kw)
    )


def test_job_spec_completion_counts(cfg):
    spec = _job(cfg)["spec"]
    assert spec["completionMode"] == "Indexed"
    assert spec["completions"] == 5
    assert spec["parallelism"] == 3
    assert spec["backoffLimitPerIndex"] == cfg.job.backoff_limit_per_index
    # clamped: the API rejects maxFailedIndexes > completions (here 5 tasks, limit 50)
    assert spec["maxFailedIndexes"] == 5


def test_max_failed_indexes_passes_through_on_big_layers(cfg):
    spec = client.ApiClient().sanitize_for_serialization(
        manifest.job_spec(cfg, 2, 1_000_000, 1000, 3)
    )["spec"]
    assert spec["maxFailedIndexes"] == cfg.job.max_failed_indexes


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


def test_spot_scheduling(cfg):
    pod = _job(cfg)["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["cloud.google.com/gke-spot"] == "true"
    assert pod["nodeSelector"]["cloud.google.com/compute-class"] == "Balanced"
    assert pod["tolerations"][0]["key"] == "cloud.google.com/gke-spot"


def test_status_annotations_and_optional_secret(cfg):
    job = _job(cfg)
    assert job["metadata"]["annotations"] == {"chunks": "100", "batch_size": "1000"}
    vol = job["spec"]["template"]["spec"]["volumes"][0]
    assert vol["secret"]["optional"] is True  # pods start even with no Secret (WI-only)


def test_sample_uses_batch_size_one(cfg):
    assert _job(cfg, batch_size=1)["metadata"]["annotations"]["batch_size"] == "1"


def test_oneshot_pod_is_spot_oneshot(cfg):
    pod = client.ApiClient().sanitize_for_serialization(
        manifest.oneshot_pod_spec(cfg, "setup", ["python", "-c", "x"])
    )
    assert pod["kind"] == "Pod"
    assert pod["spec"]["restartPolicy"] == "Never"
    assert pod["spec"]["nodeSelector"]["cloud.google.com/gke-spot"] == "true"
    mounts = {m["mountPath"] for m in pod["spec"]["containers"][0]["volumeMounts"]}
    assert {"/app/datasets", "/root/.cloudvolume/secrets"} <= mounts


def test_helm_values_persistent_util_toggle(cfg):
    cfg.persistent_util = True
    dep = manifest.helm_values(cfg)["deployments"][0]
    assert dep["nodeSelector"]["cloud.google.com/gke-spot"] == "true"
    assert dep["tolerations"][0]["effect"] == "NoSchedule"
    cfg.persistent_util = False
    assert "deployments" not in manifest.helm_values(cfg)  # idle -> 0 nodes


def test_command_for_missing_workload_is_none(cfg):
    cfg.workload = (
        "l2cache"  # no commands configured -> submit must refuse, not run ingest
    )
    assert manifest.command_for(cfg) is None
