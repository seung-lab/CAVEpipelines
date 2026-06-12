import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from pipeline import config  # noqa: E402


@pytest.fixture
def cfg():
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
    )
