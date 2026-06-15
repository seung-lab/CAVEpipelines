"""`pipeline apply` — the reconcile of running layers from an edited pipeline.yml:
in-place pod resize + parallelism, gated by a strict immutable-field drift check."""

from types import SimpleNamespace

import pytest
from kubernetes.client import ApiException

from cave_pipeline import kube, manifest, ops


def _job(cfg, layer=2, completions=100, parallelism=4, run_id="deploy-1"):
    """A real Job built by job_spec, so immutable_drift is tested against the actual
    spec layout it mirrors (status is set to a running Job for reconcile)."""
    job = manifest.job_spec(cfg, layer, completions, completions, parallelism, run_id=run_id)
    job.status = SimpleNamespace(conditions=[])
    return job


def _pod(name, phase, container, requests):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(phase=phase),
        spec=SimpleNamespace(
            containers=[SimpleNamespace(name=container, resources=SimpleNamespace(requests=requests))]
        ),
    )


# ---- immutable_drift: the strict guard rail ----------------------------------


def test_immutable_drift_none_for_matching_job(cfg):
    assert manifest.immutable_drift(cfg, 2, _job(cfg)) == []


def test_immutable_drift_ignores_run_id(cfg):
    # run-id is per-deploy mutable; a different one must NOT register as drift
    assert manifest.immutable_drift(cfg, 2, _job(cfg, run_id="other")) == []


@pytest.mark.parametrize(
    "mutate, field",
    [
        (lambda c: setattr(c.job, "perm_seed", 99), "perm_seed"),
        (lambda c: setattr(c.job, "batch_size", 500), "batch_size"),
        (lambda c: setattr(c.job, "parallel", False), "parallel"),
        (lambda c: setattr(c.job, "compute_class", "Scale-Out"), "compute_class"),
        (lambda c: setattr(c, "zone", "us-east1-b"), "zone"),
        (lambda c: setattr(c.job, "task_retries", 9), "task_retries"),
        (lambda c: setattr(c.job, "max_failed_tasks", 999), "max_failed_tasks"),
        (lambda c: setattr(c.images, "pcg", "repo/pcg:new"), "image"),
    ],
)
def test_immutable_drift_flags_each_changed_field(cfg, mutate, field):
    job = _job(cfg)  # built from the original cfg
    mutate(cfg)  # operator edits the yml after the Job is running
    assert field in {f for f, _, _ in manifest.immutable_drift(cfg, 2, job)}


def test_immutable_drift_flags_l2cache_env(cfg):
    cfg.workload = "l2cache"
    cfg.dataset["l2cache_config"] = {"cv_path": "gs://a", "table_id": "t"}
    job = _job(cfg)
    cfg.dataset["l2cache_config"]["cv_path"] = "gs://b"
    assert ("env:L2CACHE_CV_PATH", "gs://a", "gs://b") in manifest.immutable_drift(cfg, 2, job)


# ---- kube.resize_pod: the /resize subresource call ---------------------------


def _fake_core(monkeypatch, raise_status=None):
    calls = []

    def patch_resize(name, namespace, body):
        calls.append((name, namespace, body))
        if raise_status:
            raise ApiException(status=raise_status)

    monkeypatch.setattr(kube, "core", lambda: SimpleNamespace(patch_namespaced_pod_resize=patch_resize))
    return calls


def test_resize_pod_targets_the_resize_subresource(monkeypatch):
    calls = _fake_core(monkeypatch)
    kube.resize_pod("ns", "pod-1", "ingest", {"cpu": "2000m", "memory": "8192Mi"})
    assert calls == [
        ("pod-1", "ns", {"spec": {"containers": [{"name": "ingest", "resources": {"requests": {"cpu": "2000m", "memory": "8192Mi"}}}]}})
    ]


@pytest.mark.parametrize("status", [404, 422])
def test_resize_pod_swallows_gone_and_unsupported(monkeypatch, status):
    _fake_core(monkeypatch, raise_status=status)
    kube.resize_pod("ns", "pod-1", "ingest", {"cpu": "1", "memory": "1Gi"})  # no raise


def test_resize_pod_reraises_other_errors(monkeypatch):
    _fake_core(monkeypatch, raise_status=500)
    with pytest.raises(ApiException):
        kube.resize_pod("ns", "pod-1", "ingest", {"cpu": "1", "memory": "1Gi"})


# ---- ops.reconcile: orchestration --------------------------------------------


@pytest.fixture
def fake_kube(monkeypatch):
    """Record set_parallelism / resize_pod; serve a configurable job + pod list."""
    rec = SimpleNamespace(parallelism=[], resized=[], jobs=[], pods=[])
    monkeypatch.setattr(kube, "list_jobs", lambda ns: rec.jobs)
    monkeypatch.setattr(kube, "pods_of", lambda ns, name: rec.pods)
    monkeypatch.setattr(kube, "set_parallelism", lambda ns, name, p: rec.parallelism.append((name, p)))
    monkeypatch.setattr(kube, "resize_pod", lambda ns, pod, c, r: rec.resized.append((pod, r)))
    monkeypatch.setattr(ops.util, "job_state", lambda job: "running")
    return rec


def test_reconcile_resizes_running_drifted_pods_and_scales(cfg, fake_kube):
    job = _job(cfg, completions=10, parallelism=4)  # ramp.max 256 -> cap 10; 4 drifts
    desired = manifest.layer_requests(cfg.job, 2)
    fake_kube.jobs = [job]
    fake_kube.pods = [
        _pod("p-old", "Running", "ingest", {"cpu": "1", "memory": "1Gi"}),  # drift -> resize
        _pod("p-ok", "Running", "ingest", desired),  # matches -> skip
        _pod("p-pending", "Pending", "ingest", {"cpu": "1", "memory": "1Gi"}),  # not Running
    ]
    ops.reconcile(cfg)
    assert fake_kube.parallelism == [(job.metadata.name, 10)]
    assert fake_kube.resized == [("p-old", desired)]


def test_reconcile_skips_layer_on_immutable_drift(cfg, fake_kube):
    job = _job(cfg, parallelism=4)
    cfg.job.perm_seed = 99  # operator changed an immutable field
    fake_kube.jobs = [job]
    fake_kube.pods = [_pod("p", "Running", "ingest", {"cpu": "1", "memory": "1Gi"})]
    ops.reconcile(cfg)
    assert fake_kube.parallelism == [] and fake_kube.resized == []


@pytest.mark.parametrize("kind", ["complete", "sample", "suspended"])
def test_reconcile_skips_complete_sample_suspended(cfg, fake_kube, monkeypatch, kind):
    job = _job(cfg, parallelism=4)
    if kind == "complete":
        monkeypatch.setattr(ops.util, "job_state", lambda j: "complete")
    elif kind == "sample":
        job.metadata.annotations["sample"] = "1"
    else:
        job.spec.suspend = True
    fake_kube.jobs = [job]
    fake_kube.pods = [_pod("p", "Running", "ingest", {"cpu": "1", "memory": "1Gi"})]
    ops.reconcile(cfg)
    assert fake_kube.parallelism == [] and fake_kube.resized == []
