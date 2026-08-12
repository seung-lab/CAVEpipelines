import base64
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiException

from cave_pipeline import kube


def _pod(phase, deleting=False):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="pipeline-util-abc", deletion_timestamp="now" if deleting else None
        ),
        status=SimpleNamespace(phase=phase),
    )


def _core_returning(batches):
    calls = iter(batches)
    return SimpleNamespace(
        list_namespaced_pod=lambda ns, label_selector: SimpleNamespace(items=next(calls))
    )


def test_util_pod_waits_through_pending(monkeypatch, no_sleep):
    fake = _core_returning([[_pod("Pending")], [_pod("Pending")], [_pod("Running")]])
    monkeypatch.setattr(kube, "core", lambda: fake)
    assert kube.util_pod("ns") == "pipeline-util-abc"


def test_util_pod_skips_terminating_pod(monkeypatch, no_sleep):
    # a Running pod with deletion_timestamp is dying (helm rollout) — wait for its successor
    fake = _core_returning([[_pod("Running", deleting=True)], [_pod("Running")]])
    monkeypatch.setattr(kube, "core", lambda: fake)
    assert kube.util_pod("ns") == "pipeline-util-abc"


def test_util_pod_missing_points_to_deploy(monkeypatch):
    fake = _core_returning([[]])
    monkeypatch.setattr(kube, "core", lambda: fake)
    with pytest.raises(SystemExit, match="pipeline deploy"):
        kube.util_pod("ns")


def test_exec_cmd_failure_is_clean(monkeypatch):
    monkeypatch.setattr(
        kube, "core", lambda: SimpleNamespace(connect_get_namespaced_pod_exec=None)
    )

    def boom(*a, **kw):
        raise kube.ApiException(status=500, reason="boom")

    monkeypatch.setattr(kube, "stream", boom)
    with pytest.raises(SystemExit, match="exec into pod"):
        kube.exec_cmd("ns", "pod-x", ["true"])


def test_secret_data_renames_to_container_filename(tmp_path):
    (tmp_path / "projA").mkdir()
    (tmp_path / "projA" / "g.json").write_text("GCP")
    (tmp_path / "cave.json").write_text("CAVE")
    data = kube.secret_data(
        str(tmp_path),
        {"google-secret.json": "projA/g.json", "cave-secret.json": "cave.json"},
    )
    assert set(data) == {"google-secret.json", "cave-secret.json"}
    assert base64.b64decode(data["google-secret.json"]).decode() == "GCP"


def test_secret_data_missing_file_raises(tmp_path):
    with pytest.raises(SystemExit):
        kube.secret_data(str(tmp_path), {"x": "nope.json"})


@pytest.mark.parametrize(
    "log_return",
    [
        SimpleNamespace(
            data=b"847 144 18 4 1\n"
        ),  # raw response (_preload_content=False)
        b"847 144 18 4 1\n",  # a client variant that returns raw bytes
        "847 144 18 4 1\n",  # a client variant that already decodes
    ],
)
def test_run_oneshot_returns_decoded_text(monkeypatch, log_return, no_sleep):
    # the log must come back as text regardless of client return shape — never a
    # str(bytes) "b'...'" repr, which silently breaks every consumer
    def absent_delete(name, ns, **kw):
        raise kube.ApiException(status=404, reason="Not Found")

    fake = SimpleNamespace(
        delete_namespaced_pod=absent_delete,
        create_namespaced_pod=lambda ns, spec: None,
        read_namespaced_pod_status=lambda name, ns: SimpleNamespace(
            status=SimpleNamespace(phase="Succeeded")
        ),
        read_namespaced_pod_log=lambda name, ns, **kw: log_return,
    )
    monkeypatch.setattr(kube, "core", lambda: fake)
    spec = SimpleNamespace(metadata=SimpleNamespace(name="layer-counts-xyz"))
    assert kube.run_oneshot("ns", spec) == "847 144 18 4 1\n"


def test_tolerate_swallows_only_the_named_statuses():
    """The point of the block is that every unnamed status still propagates — a wider
    catch would hide a real API failure behind sugar."""
    with kube.tolerate(404):
        raise ApiException(status=404, reason="Not Found")
    with kube.tolerate(400, 404, 422):
        raise ApiException(status=422, reason="Unprocessable")
    with pytest.raises(ApiException), kube.tolerate(404):
        raise ApiException(status=403, reason="Forbidden")
    with pytest.raises(ValueError), kube.tolerate(404):  # not an ApiException at all
        raise ValueError("unrelated")


def _delete_doubles(monkeypatch, collection=None, suspend=None):
    """Fakes for batch()/core() that record how a Job and its pods are deleted.

    `order` records the teardown steps as they happen, so the suspend-then-clear-then-drop
    sequence can be asserted rather than merely surviving."""
    calls = {"jobs": [], "pods": [], "created": [], "suspend": [], "order": []}

    def _delete_collection(ns, **kw):
        calls["pods"].append((ns, kw))
        calls["order"].append("clear-pods")
        if collection is not None:
            raise collection

    def _patch_job(n, ns, body):
        calls["suspend"].append((n, body))
        calls["order"].append("suspend")
        if suspend is not None:
            raise suspend

    def _delete_job(n, ns, propagation_policy=None):
        calls["jobs"].append((n, propagation_policy))
        calls["order"].append("drop-job")

    monkeypatch.setattr(
        kube,
        "batch",
        lambda: SimpleNamespace(
            read_namespaced_job=lambda n, ns: SimpleNamespace(status=None),
            patch_namespaced_job=_patch_job,
            delete_namespaced_job=_delete_job,
            create_namespaced_job=lambda ns, s: calls["created"].append(s),
        ),
    )
    monkeypatch.setattr(
        kube,
        "core",
        lambda: SimpleNamespace(
            delete_collection_namespaced_pod=_delete_collection,
            # no pods left holding the tracking finalizer, so nothing to release
            list_namespaced_pod=lambda ns, **kw: SimpleNamespace(items=[]),
        ),
    )
    return calls


def _tracked_pod(name, phase, finalizers, deleting=True):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            finalizers=finalizers,
            deletion_timestamp="2026-08-10T22:00:00Z" if deleting else None,
        ),
        status=SimpleNamespace(phase=phase),
    )


def _finalizer_doubles(monkeypatch, pods):
    """core() double that lists `pods` and records finalizer patches."""
    patched = []

    def _patch(name, ns, body):
        patched.append((name, body))

    monkeypatch.setattr(
        kube,
        "core",
        lambda: SimpleNamespace(
            delete_collection_namespaced_pod=lambda ns, **kw: None,
            list_namespaced_pod=lambda ns, **kw: SimpleNamespace(items=pods),
            patch_namespaced_pod=_patch,
        ),
    )
    monkeypatch.setattr(
        kube,
        "batch",
        lambda: SimpleNamespace(
            delete_namespaced_job=lambda n, ns, propagation_policy=None: None
        ),
    )
    return patched


def test_delete_job_pods_releases_the_tracking_finalizer(monkeypatch):
    """Only the Job controller clears batch.kubernetes.io/job-tracking, and only while
    reconciling — a suspended Job whose nodes were scaled away strands the pod objects
    forever, still listed and still alarming the operator."""
    fin = [kube.JOB_TRACKING_FINALIZER]
    pods = [
        _tracked_pod("failed-a", "Failed", fin),
        _tracked_pod("failed-b", "Failed", fin),
    ]
    patched = _finalizer_doubles(monkeypatch, pods)
    kube.delete_job_pods("ns", "ingest-l3", keep_terminal=True)  # the pause path
    assert [n for n, _ in patched] == ["failed-a", "failed-b"]
    assert all(b["metadata"]["finalizers"] is None for _, b in patched)


def test_delete_job_pods_skips_the_finalizer_sweep_when_the_job_goes(monkeypatch):
    """Deleting the Job makes the controller sweep its own selector, so patching every pod
    first is one request each for nothing — minutes of them on a 12,000-pod layer."""
    fin = [kube.JOB_TRACKING_FINALIZER]
    pods = [
        _tracked_pod("failed-a", "Failed", fin),
        _tracked_pod("failed-b", "Failed", fin),
    ]
    patched = _finalizer_doubles(monkeypatch, pods)
    kube.delete_job_pods("ns", "ingest-l3")
    assert patched == []


def test_delete_job_pods_spares_succeeded_pods(monkeypatch):
    """The finalizer is how a completion gets counted; clearing it early loses the index,
    which then has to be rebuilt on resume."""
    fin = [kube.JOB_TRACKING_FINALIZER]
    pods = [_tracked_pod("done", "Succeeded", fin), _tracked_pod("failed", "Failed", fin)]
    patched = _finalizer_doubles(monkeypatch, pods)
    kube.delete_job_pods("ns", "ingest-l3", keep_terminal=True)
    assert [n for n, _ in patched] == ["failed"]  # the succeeded pod keeps its finalizer


def test_delete_job_pods_ignores_pods_not_condemned_or_untracked(monkeypatch):
    """Patching a live pod's finalizers would strip tracking from work still running."""
    pods = [
        _tracked_pod("running", "Running", [kube.JOB_TRACKING_FINALIZER], deleting=False),
        _tracked_pod("no-finalizer", "Failed", []),
    ]
    patched = _finalizer_doubles(monkeypatch, pods)
    kube.delete_job_pods("ns", "ingest-l3")
    assert patched == []


def test_delete_job_suspends_before_clearing_pods(monkeypatch):
    """Clearing pods frees their indexes; an unsuspended controller refills parallelism in
    the window before the Job delete lands, and those replacements schedule and bill."""
    calls = _delete_doubles(monkeypatch)
    kube.delete_job("ns", "ingest-l3")
    assert calls["order"] == ["suspend", "clear-pods", "drop-job"]
    assert calls["suspend"] == [("ingest-l3", {"spec": {"suspend": True}})]


def test_delete_job_tolerates_a_finished_or_absent_job(monkeypatch):
    """`undeploy` iterates complete Jobs too, and a re-issued delete finds none — neither
    may abort the teardown."""
    for status in (404, 422):
        calls = _delete_doubles(monkeypatch, suspend=ApiException(status=status))
        kube.delete_job("ns", "ingest-l3")
        assert calls["jobs"] == [("ingest-l3", "Background")], status
    calls = _delete_doubles(monkeypatch, suspend=ApiException(status=403))
    with pytest.raises(ApiException):  # an RBAC denial must still surface
        kube.delete_job("ns", "ingest-l3")


def test_delete_job_bulk_deletes_pods_and_drops_the_job_in_background(monkeypatch):
    """Foreground makes the GC walk every pod (a finalizer write plus a delete each), which
    held a 12,000-pod layer's name for 20+ minutes and blocked the next submit."""
    calls = _delete_doubles(monkeypatch)
    kube.delete_job("ns", "ingest-l3")

    ns, kw = calls["pods"][0]
    assert (ns, kw["label_selector"]) == ("ns", "batch.kubernetes.io/job-name=ingest-l3")
    # Spot pods must not sit out their grace period on a teardown
    assert kw["grace_period_seconds"] == 0
    assert kw["propagation_policy"] == "Background"
    assert calls["jobs"] == [("ingest-l3", "Background")]


def test_delete_job_pods_can_spare_terminal_pods(monkeypatch):
    """`pause` leaves the Job alive, so its Failed pods stay readable by `inspect` and its
    Succeeded tally stays with the controller. Teardown paths still take everything."""
    calls = _delete_doubles(monkeypatch)
    kube.delete_job_pods("ns", "ingest-l3", keep_terminal=True)
    assert calls["pods"][0][1]["field_selector"] == (
        "status.phase!=Succeeded,status.phase!=Failed"
    )
    calls = _delete_doubles(monkeypatch)
    kube.delete_job_pods("ns", "ingest-l3")
    assert "field_selector" not in calls["pods"][0][1]


def test_delete_job_tolerates_pods_already_gone(monkeypatch):
    """A re-issued delete finds no pods; that 404 must not abort the Job delete."""
    calls = _delete_doubles(monkeypatch, collection=ApiException(status=404))
    kube.delete_job("ns", "ingest-l3")
    assert calls["jobs"] == [("ingest-l3", "Background")]


def test_delete_job_propagates_non_404_pod_errors(monkeypatch):
    """An RBAC denial must surface, not be mistaken for an empty layer."""
    calls = _delete_doubles(monkeypatch, collection=ApiException(status=403))
    with pytest.raises(ApiException):
        kube.delete_job("ns", "ingest-l3")
    assert calls["jobs"] == []  # the Job is left alone when its pods could not be cleared


def test_recreate_job_waits_only_the_flat_timeout(monkeypatch, no_sleep):
    """Background deletion frees the name at once, so the wait is a sanity bound and no
    longer scales with the fleet."""
    calls = _delete_doubles(monkeypatch)
    waited = []
    monkeypatch.setattr(
        kube, "_wait_deleted", lambda read, name, timeout: waited.append(timeout)
    )
    spec = SimpleNamespace(metadata=SimpleNamespace(name="ingest-l3"))
    kube.recreate_job("ns", spec)
    assert waited == [kube.DELETE_TIMEOUT]
    assert calls["pods"] and calls["jobs"] == [("ingest-l3", "Background")]
    assert calls["created"] == [spec]
