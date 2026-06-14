import base64
from types import SimpleNamespace

import pytest

from pipeline import kube


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
