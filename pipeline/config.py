"""Load all pipeline config from a single yaml — the one source of truth.

`-c` is a path (relative or absolute) to a pipeline yaml; with no `-c` the
default is `config/pipeline.yml`. The first -c becomes the session config
(stored in config/.current): later commands reuse it, and switching requires
`pipeline reset`. The optional `dataset:` key names the dataset yaml relative to
the pipeline yaml's directory, so many projects coexist side by side. The dataset
block is kept verbatim (same yml the graph was always configured with) and passed
through to `setup`.
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


def stored() -> str:
    """The session config path (selected by the first -c), or None."""
    try:
        with open(os.path.join(CONFIG_DIR, ".current")) as stream:
            return stream.read().strip() or None
    except OSError:
        return None


def forget() -> None:
    """Clear the session config (`pipeline reset`)."""
    try:
        os.remove(os.path.join(CONFIG_DIR, ".current"))
    except OSError:
        pass


def resolve(name: str = None, workload: str = None) -> Config:
    """Load the session config. The first explicit -c selects it for the session;
    a different -c is refused until `pipeline reset`."""
    current = stored()
    cfg = load(name or current, workload)
    if not name:
        return cfg
    if current and os.path.abspath(cfg.source) != os.path.abspath(current):
        raise SystemExit(f"session config is '{current}'; `pipeline reset` to switch")
    if not current:  # selected only after a successful load: a typo never sticks
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(os.path.join(CONFIG_DIR, ".current"), "w") as stream:
            stream.write(cfg.source + "\n")
    return cfg


def load(name: str = None, workload: str = None) -> Config:
    """Load the pipeline yaml at `name` (any path); defaults to config/pipeline.yml.

    The `dataset:` key resolves relative to the pipeline yaml's directory.
    `workload` overrides the file's — the per-workload job merge follows it."""
    path = name or os.path.join(CONFIG_DIR, "pipeline.yml")
    config_dir = os.path.dirname(path) or "."
    with open(path) as stream:
        raw = yaml.safe_load(stream) or {}
    # a present-but-empty yaml key parses to None; treat every block like {}
    bt = Bigtable(**(raw.get("bigtable") or {}))
    dataset = _with_bigtable(
        _read_dataset(config_dir, raw.get("dataset") or "dataset.yml"), bt
    )
    raw_job = dict(raw.get("job") or {})
    workload = workload or raw.get("workload", "ingest")
    raw_job = _merge(raw_job, (raw_job.pop("workloads", None) or {}).get(workload) or {})
    ramp = Ramp(**(raw_job.pop("ramp", None) or {}))
    if ramp.start < 1 or ramp.factor <= 1:  # else submit's ramp loop never terminates
        raise SystemExit("job.ramp: start must be >= 1 and factor > 1")
    resources = _resources(raw_job.pop("resources", None))
    return Config(
        namespace=raw.get("namespace", "default"),
        graph_id=raw["graph_id"],
        images=Images(**(raw["images"] or {})),
        workload_identity=WorkloadIdentity(**(raw.get("workload_identity") or {})),
        bigtable=bt,
        dataset=dataset,
        job=Job(ramp=ramp, resources=resources, **raw_job),
        workload=workload,
        secret_name=raw.get("secret_name", "cloud-volume-secrets"),
        persistent_util=raw.get("persistent_util", True),
        secret_files=raw.get("secret_files") or {},
        commands=raw.get("commands") or {},
        env=raw.get("env") or {},
        region=raw.get("region", ""),
        zone=raw.get("zone", ""),
        config_dir=config_dir,
        source=path,
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


def _read_dataset(config_dir: str, rel_path: str) -> dict:
    """The graph definition yaml, relative to the pipeline yaml's directory
    (empty for graph-less workloads)."""
    path = os.path.join(config_dir, rel_path)
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
