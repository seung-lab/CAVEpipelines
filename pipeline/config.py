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
class Curve:
    """Per-layer scaling: value(L) = min(base * factor**(L-2) + add, max); max 0 = uncapped."""

    base: float
    factor: float = 1.0
    add: float = 0.0
    max: float = 0.0


@dataclass
class Resources:
    cpu: Curve = None
    memory: Curve = None
    overrides: dict = field(default_factory=dict)  # {layer: {"cpu": x, "memory": y}}


@dataclass
class Job:
    perm_seed: int = 0
    batch_size: int = 1000
    parallel: bool = True  # parent-chunk builds fan out over every core (process pool)
    cpu: str = "1"
    memory: str = "2Gi"
    compute_class: str = ""
    task_retries: int = 3  # per-task retry budget before the task is dead
    max_failed_tasks: int = 50  # dead tasks tolerated before the layer aborts
    ramp: Ramp = field(default_factory=Ramp)
    resources: Resources = None  # per-layer curves; None = flat cpu/memory everywhere


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
    source: str = "pipeline.yml"  # config file name this was loaded from

    def image(self) -> str:
        return self.images.l2cache if self.workload == "l2cache" else self.images.pcg


def load(name: str = "pipeline.yml", workload: str = None) -> Config:
    """Load CONFIG_DIR/<name>; its `dataset:` key names the dataset yaml there.

    `workload` overrides the file's — the per-workload job merge follows it."""
    with open(os.path.join(CONFIG_DIR, name)) as stream:
        raw = yaml.safe_load(stream) or {}
    bt = Bigtable(**raw.get("bigtable", {}))
    dataset = _with_bigtable(_read_dataset(raw.get("dataset", "dataset.yml")), bt)
    raw_job = dict(raw.get("job", {}))
    workload = workload or raw.get("workload", "ingest")
    raw_job = _merge(raw_job, raw_job.pop("workloads", {}).get(workload, {}))
    ramp = Ramp(**raw_job.pop("ramp", {}))
    resources = _resources(raw_job.pop("resources", None))
    return Config(
        namespace=raw.get("namespace", "default"),
        graph_id=raw["graph_id"],
        images=Images(**raw["images"]),
        workload_identity=WorkloadIdentity(**raw.get("workload_identity", {})),
        bigtable=bt,
        dataset=dataset,
        job=Job(ramp=ramp, resources=resources, **raw_job),
        workload=workload,
        secret_name=raw.get("secret_name", "cloud-volume-secrets"),
        persistent_util=raw.get("persistent_util", True),
        secret_files=raw.get("secret_files", {}),
        commands=raw.get("commands", {}),
        env=raw.get("env") or {},  # `env:` left empty in yaml parses to None
        region=raw.get("region", ""),
        zone=raw.get("zone", ""),
        config_dir=CONFIG_DIR,
        source=name,
    )


def _merge(base: dict, override: dict) -> dict:
    """Recursive dict merge — `job.workloads.<workload>` deep-overrides `job`."""
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def _resources(raw) -> Resources:
    if not raw:
        return None
    return Resources(
        cpu=Curve(**raw["cpu"]) if "cpu" in raw else None,
        memory=Curve(**raw["memory"]) if "memory" in raw else None,
        overrides={int(k): v for k, v in raw.get("overrides", {}).items()},
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
