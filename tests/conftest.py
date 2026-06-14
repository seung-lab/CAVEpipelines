import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from pipeline import config  # noqa: E402
from pipeline.db import base  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_db_caches():
    """Reset the process-global engine cache + create-once set after each test, so DB
    state (or a test that deletes its db) never leaks into the next."""
    yield
    base._engine.cache_clear()
    base._initialized.clear()


@pytest.fixture
def cfg(tmp_path):
    # config_dir + databases isolated per test: never touch the repo's real config/ or costs/
    return config.Config(
        namespace="ns",
        graph_id="g",
        images=config.Images(pcg="repo/pcg:tag", l2cache="repo/l2:tag"),
        workload_identity=config.WorkloadIdentity(
            service_account="pipeline", gsa_email="gsa@p.iam"
        ),
        bigtable=config.Bigtable(project="proj", instance="inst"),
        dataset={"data_source": {"EDGES": "gs://b/e"}},
        job=config.Job(perm_seed=7, batch_size=1000, compute_class="Balanced"),
        config_dir=str(tmp_path),
        database={
            "cost": f"sqlite:///{tmp_path}/cost.db",
            "state": f"sqlite:///{tmp_path}/state.db",
        },
    )


@pytest.fixture
def make_job():
    """Factory for the fake Job shape consumed by util.job_progress/status_table."""

    def _make(
        *,
        name="ingest-l2",
        graph="g",
        layer=2,
        chunks=10,
        batch_size=1,
        annotations=None,
        conditions=None,
        succeeded=0,
        active=0,
        ready=0,
        failed=0,
        failed_indexes=None,
        suspend=None,
    ):
        ann = {"chunks": str(chunks), "batch_size": str(batch_size)}
        ann.update(annotations or {})
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=name, labels={"graph": graph, "layer": str(layer)}, annotations=ann
            ),
            spec=SimpleNamespace(suspend=suspend),
            status=SimpleNamespace(
                conditions=conditions or [],
                succeeded=succeeded,
                active=active,
                ready=ready,
                failed=failed,
                failed_indexes=failed_indexes,
                start_time=None,
                completion_time=None,
            ),
        )

    return _make
