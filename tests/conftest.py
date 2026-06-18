import io
import pathlib
import sys
import time
from types import SimpleNamespace

import pytest
from rich.console import Console

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from cave_pipeline import config, util  # noqa: E402
from cave_pipeline.db import base, cost, models, state  # noqa: E402


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
        job=config.Job(
            perm_seed=7,
            batch_size=1000,
            compute_class="Balanced",
            resources=config.Resources(
                cpu=config.Curve(base=1, factor=2, max=28),
                memory=config.Curve(base=2, factor=2, max=33),
            ),
        ),
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


@pytest.fixture
def render():
    """Render a Rich renderable to a string; force_terminal keeps markup, so a style
    tag that swallows its own text is caught (not hidden by no-color)."""

    def _render(renderable, *, width=200) -> str:
        buf = io.StringIO()
        Console(file=buf, width=width, force_terminal=True).print(renderable)
        return buf.getvalue()

    return _render


@pytest.fixture
def no_cluster():
    """Stand-in for the kube module with an empty cluster (no jobs, no nodes)."""
    return SimpleNamespace(
        list_jobs=lambda ns, w=None: [], node_summary=lambda: (0, 0, {})
    )


@pytest.fixture
def running_run(cfg):
    """Open a run for the default graph (status running, one ingest stage)."""
    state.start_run(cfg, {"ingest"}, parallel=True)
    return cfg


@pytest.fixture
def no_cost_sample(monkeypatch):
    """Silence cost sampling (it would watch the live cluster)."""
    monkeypatch.setattr(cost, "sample", lambda c: None)


@pytest.fixture
def no_sleep(monkeypatch):
    """No real sleeps in ramp/poll loops."""
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def seed_cost(cfg):
    """Insert one recorded cost Job into the isolated cost db (for the run explorer)."""

    def _seed(run_id, *, graph="g", workload="ingest", layer=2, uid=None, **fields):
        row = {
            "job_uid": uid or f"{run_id}-{workload}-{layer}",
            "graph": graph,
            "workload": workload,
            "run_id": run_id,
            "name": f"{workload}-l{layer}",
            "layer": layer,
            "cpu_req": 2.0,
            "mem_req": 4.0,
            "started_at": 0.0,
            "finished_at": 3600.0,
            "last_seen": 3600.0,
            "succeeded": 1,
            **fields,
        }
        with base.session(cfg.database["cost"], models.CostBase) as s:
            s.add(models.Job(**row))

    return _seed


@pytest.fixture
def stub_layer_counts(monkeypatch):
    """Stub util.read_layer_counts to fixed counts, bypassing the cluster probe."""

    def _stub(counts):
        monkeypatch.setattr(util, "read_layer_counts", lambda c: counts)

    return _stub
