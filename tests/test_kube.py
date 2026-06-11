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


def test_util_pod_waits_through_pending(monkeypatch):
    fake = _core_returning([[_pod("Pending")], [_pod("Pending")], [_pod("Running")]])
    monkeypatch.setattr(kube, "core", lambda: fake)
    monkeypatch.setattr(kube.time, "sleep", lambda s: None)
    assert kube.util_pod("ns") == "pipeline-util-abc"


def test_util_pod_skips_terminating_pod(monkeypatch):
    # a Running pod with deletion_timestamp is dying (helm rollout) — wait for its successor
    fake = _core_returning([[_pod("Running", deleting=True)], [_pod("Running")]])
    monkeypatch.setattr(kube, "core", lambda: fake)
    monkeypatch.setattr(kube.time, "sleep", lambda s: None)
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
