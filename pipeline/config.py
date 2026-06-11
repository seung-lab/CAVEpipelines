"""Load all pipeline config from a single pipeline.yml — the one source of truth.

Grouped by concern; the dataset block is kept verbatim (same yml the graph was
always configured with) and passed through to `setup`. One graph and one workload
run at a time, so both live in the config — no command repeats them.
"""

from dataclasses import dataclass, field

import yaml

ENV_CONFIGMAP = "pychunkedgraph-env"
DATASET_CONFIGMAP = "pychunkedgraph-datasets"


@dataclass
class Images:
    pcg: str
    l2cache: str = ""


@dataclass
class WorkloadIdentity:
    service_account: str = "pipeline"
    gsa_email: str = ""


@dataclass
class Bigtable:
    project: str = ""
    instance: str = ""


@dataclass
class Job:
    perm_seed: int = 0
    batch_size: int = 1000
    n_threads: int = 1
    cpu: str = "1"
    memory: str = "2Gi"
    compute_class: str = ""
    backoff_limit_per_index: int = 3
    max_failed_indexes: int = 50


@dataclass
class Ramp:
    start: int = 4
    factor: int = 2
    period: int = 60
    max: int = 256


@dataclass
class Config:
    namespace: str
    graph_id: str
    images: Images
    workload_identity: WorkloadIdentity
    bigtable: Bigtable
    dataset: dict  # passthrough; written as dataset.yml by `setup`
    job: Job
    ramp: Ramp
    workload: str = "ingest"
    secret_name: str = "cloud-volume-secrets"
    persistent_util: bool = True
    secret_files: dict = field(default_factory=dict)
    commands: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)

    def image(self) -> str:
        return self.images.l2cache if self.workload == "l2cache" else self.images.pcg


def load(path: str = "pipeline.yml") -> Config:
    with open(path) as stream:
        raw = yaml.safe_load(stream) or {}
    bt = Bigtable(**raw.get("bigtable", {}))
    dataset = _with_bigtable(raw.get("dataset", {}), bt)
    return Config(
        namespace=raw.get("namespace", "default"),
        graph_id=raw["graph_id"],
        images=Images(**raw["images"]),
        workload_identity=WorkloadIdentity(**raw.get("workload_identity", {})),
        bigtable=bt,
        dataset=dataset,
        job=Job(**raw.get("job", {})),
        ramp=Ramp(**raw.get("ramp", {})),
        workload=raw.get("workload", "ingest"),
        secret_name=raw.get("secret_name", "cloud-volume-secrets"),
        persistent_util=raw.get("persistent_util", True),
        secret_files=raw.get("secret_files", {}),
        commands=raw.get("commands", {}),
        env=raw.get("env", {}),
    )


def _with_bigtable(dataset: dict, bt: Bigtable) -> dict:
    """Inject the single bigtable project/instance into the dataset backend_client
    so the operator never repeats them."""
    if bt.project and bt.instance:
        cfg = dataset.setdefault("backend_client", {}).setdefault("CONFIG", {})
        cfg.setdefault("PROJECT", bt.project)
        cfg.setdefault("INSTANCE", bt.instance)
    return dataset
