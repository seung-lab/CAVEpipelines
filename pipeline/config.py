"""Load all pipeline config from a single yaml — the one source of truth.

Every config file lives in `config/`; `-c` picks a pipeline yaml by name, and its
optional `dataset:` key names the dataset yaml (path relative to `config/`), so
many projects coexist in one directory. The dataset block is kept verbatim (same
yml the graph was always configured with) and passed through to `setup`.
"""

import os
from dataclasses import dataclass, field

import yaml

CONFIG_DIR = "config"
ENV_CONFIGMAP = "pcg-env"


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
class Ramp:
    start: int = 4
    factor: int = 2
    period: int = 60
    max: int = 256


@dataclass
class Job:
    perm_seed: int = 0
    batch_size: int = 1000
    parallel: bool = True  # parent-chunk builds fan out over every core (process pool)
    cpu: str = "1"
    memory: str = "2Gi"
    compute_class: str = ""
    backoff_limit_per_index: int = 3
    max_failed_indexes: int = 50
    ramp: Ramp = field(default_factory=Ramp)


@dataclass
class Config:
    namespace: str
    graph_id: str
    images: Images
    workload_identity: WorkloadIdentity
    bigtable: Bigtable
    dataset: dict  # passthrough; written as dataset.yml by `setup`
    job: Job
    workload: str = "ingest"
    secret_name: str = "cloud-volume-secrets"
    persistent_util: bool = True
    secret_files: dict = field(default_factory=dict)
    commands: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    region: str = (
        ""  # GKE region; selects the cost rate row (no default — set per cluster)
    )
    zone: str = ""  # optional: pin worker pods to one zone (topology.kubernetes.io/zone)
    config_dir: str = (
        "config"  # where pipeline.yml lives; also holds the local counts cache
    )

    def image(self) -> str:
        return self.images.l2cache if self.workload == "l2cache" else self.images.pcg


def load(name: str = "pipeline.yml") -> Config:
    """Load CONFIG_DIR/<name>; its `dataset:` key names the dataset yaml there."""
    with open(os.path.join(CONFIG_DIR, name)) as stream:
        raw = yaml.safe_load(stream) or {}
    bt = Bigtable(**raw.get("bigtable", {}))
    dataset = _with_bigtable(_read_dataset(raw.get("dataset", "dataset.yml")), bt)
    raw_job = dict(raw.get("job", {}))
    ramp = Ramp(**raw_job.pop("ramp", {}))
    return Config(
        namespace=raw.get("namespace", "default"),
        graph_id=raw["graph_id"],
        images=Images(**raw["images"]),
        workload_identity=WorkloadIdentity(**raw.get("workload_identity", {})),
        bigtable=bt,
        dataset=dataset,
        job=Job(ramp=ramp, **raw_job),
        workload=raw.get("workload", "ingest"),
        secret_name=raw.get("secret_name", "cloud-volume-secrets"),
        persistent_util=raw.get("persistent_util", True),
        secret_files=raw.get("secret_files", {}),
        commands=raw.get("commands", {}),
        env=raw.get("env") or {},  # `env:` left empty in yaml parses to None
        region=raw.get("region", ""),
        zone=raw.get("zone", ""),
        config_dir=CONFIG_DIR,
    )


def _read_dataset(rel_path: str) -> dict:
    """The graph definition yaml, relative to CONFIG_DIR (empty for graph-less workloads)."""
    path = os.path.join(CONFIG_DIR, rel_path)
    if not os.path.exists(path):
        return {}
    with open(path) as stream:
        return yaml.safe_load(stream) or {}


def _with_bigtable(dataset: dict, bt: Bigtable) -> dict:
    """Inject the single bigtable project/instance into the dataset backend_client
    so the operator never repeats them."""
    if bt.project and bt.instance:
        cfg = dataset.setdefault("backend_client", {}).setdefault("CONFIG", {})
        cfg.setdefault("PROJECT", bt.project)
        cfg.setdefault("INSTANCE", bt.instance)
    return dataset
