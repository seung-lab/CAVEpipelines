import base64

import pytest

from pipeline import kube


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
