"""Load all pipeline config from a single yaml — the one source of truth.

`-c` is a path (relative or absolute) to a pipeline yaml; with no `-c` the
default is `config/pipeline.yml`. The first -c becomes the session config
(stored in config/.current): later commands reuse it, and switching requires
`pipeline reset`. The optional `dataset:` key names the dataset yaml relative to
the pipeline yaml's directory, so many projects coexist side by side. The dataset
block is kept verbatim (same yml the graph was always configured with) and passed
through to `setup`.
"""

import contextlib
import hashlib
import os
import re
from dataclasses import dataclass, field, fields

import yaml

CONFIG_DIR = "config"
ENV_CONFIGMAP = "pcg-env"
ATOMIC_LAYER = 2  # supervoxels; every graph is built from here up
# v3.2.0 added the worker entrypoint and the PCG_* env contract; .dev4 made the ingest
# pools honor PCG_N_PROCESSES, .dev5 the meshing stitch pool, and .dev6 bundles a
# cave-pipeline whose harness actually emits that variable (dev5 pinned one that still
# emitted n_threads, so every pod died on KeyError). Older images either ignore the
# contract or size a pool from the node's cores — both only fail once pods are burning.
MIN_PCG_IMAGE = "v3.2.0.dev6"
# builders block on Bigtable at ~6% of a core, so one worker per vCPU idles the pod
PROCESSES_PER_VCPU = 2
# Pod-billed classes. "" is the default class, requested by omitting the selector — there is
# no selectable name for it. A custom class is pod-billed only if its priorities use
# podFamily, which the name cannot show, so it is refused here.
# https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-compute-classes
POD_BILLED_CLASSES = ("", "Balanced", "Scale-Out")


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
class Override:
    """Exact requests for one layer; a dimension left out falls back to its curve."""

    cpu: float | None = None
    memory: float | None = None


@dataclass
class Resources:
    cpu: Curve = None
    memory: Curve = None
    overrides: dict = field(default_factory=dict)  # {layer: Override}
    # fill the node the request already forces (packing.fill_node); raises the billed
    # request above the curve value, so size ramp.max off the request
    pack: bool = True


@dataclass
class Job:
    perm_seed: int = 0
    batch_size: int = 1000
    start_layer: int = ATOMIC_LAYER  # first layer to submit; below it is assumed built
    parallel: bool = True  # process pool sized by the pod's cpu request, not the node's
    # workers per billed vCPU; above 1 splits the memory request that many ways
    processes_per_vcpu: int = PROCESSES_PER_VCPU
    compute_class: str = ""
    task_retries: int = 3  # per-task retry budget before the task is dead
    max_failed_tasks: int = 50  # dead tasks tolerated before the layer aborts
    ramp: Ramp = field(default_factory=Ramp)
    resources: Resources = (
        None  # per-pod requests (cpu/memory curves); None = default base
    )


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
    database: dict = field(
        default_factory=dict
    )  # {cost, state} URLs; default local SQLite
    region: str = (
        ""  # GKE region; selects the cost rate row (no default — set per cluster)
    )
    zone: str = ""  # optional: pin worker pods to one zone (topology.kubernetes.io/zone)
    config_dir: str = (
        "config"  # where pipeline.yml lives; also holds the local counts cache
    )
    source: str = "pipeline.yml"  # config file name this was loaded from
    dataset_path: str = ""  # resolved dataset yaml path ("" when graph-less)
    # digest of the dataset yaml at load; "" when built outside load(). Dataset only —
    # fingerprinting pipeline.yml would make every `apply` a hard stop.
    fingerprint: str = ""

    def image(self) -> str:
        return self.images.l2cache if self.workload == "l2cache" else self.images.pcg


# every published 3.2 image is a pre-release, so the ordering has to rank them:
# dev < alpha < beta < rc < the final release that follows them.
_STAGE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "rc": 3, "c": 3}
_FINAL = max(_STAGE_RANK.values()) + 1
_VERSION_RE = re.compile(
    r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:[._-]?(dev|alpha|beta|rc|a|b|c)\.?(\d+)?)?",
    re.IGNORECASE,
)


def image_version(image: str) -> tuple:
    """Sortable (major, minor, patch, stage, stage_no) from an image tag, or ().

    A digest-pinned, untagged, or non-numeric reference yields (), which sorts below
    every real version — so an unreadable tag fails the floor check like an old one."""
    last = image.rpartition("/")[2]  # a registry host may carry a :port
    tag = image.rpartition(":")[2] if ":" in last else ""
    match = _VERSION_RE.match(tag)
    if not match:
        return ()
    major, minor, patch, stage, stage_no = match.groups()
    return (
        int(major),
        int(minor),
        int(patch or 0),
        _STAGE_RANK.get((stage or "").lower(), _FINAL),
        int(stage_no or 0),
    )


_WORKLOADS = ("ingest", "l2cache", "meshing", "migrate", "migrate_cleanup")


def _start_layer(path: str, value) -> int:
    """Validate a workload's start_layer: a whole number at or above the atomic layer.

    Truncating (3.9 -> 3) would start somewhere the operator never asked for, and a raw
    string would survive to `max()` and fail mid-run, after the cluster was mutated."""
    try:
        layer = int(value)
        if layer != float(value):
            raise ValueError(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: `.inf`
        raise SystemExit(
            f"{path}: start_layer must be a whole number >= {ATOMIC_LAYER}, got {value!r}"
        )
    if layer < ATOMIC_LAYER:
        raise SystemExit(
            f"{path}: start_layer must be >= {ATOMIC_LAYER} (the atomic layer)"
        )
    return layer


def _processes_per_vcpu(path: str, value) -> int:
    """A whole number >= 0 (0 and 1 both mean one worker).

    Stored raw it breaks inside the pod: 1.5 ships PCG_N_PROCESSES="21.0"."""
    try:
        procs = int(value)
        if procs != float(value):
            raise ValueError(value)
    except (TypeError, ValueError, OverflowError):
        raise SystemExit(
            f"{path}: job.processes_per_vcpu must be a whole number, got {value!r}"
        )
    if procs < 0:
        raise SystemExit(f"{path}: job.processes_per_vcpu must be >= 0, got {procs}")
    return procs


def _check_start_layers(path: str, raw: dict) -> None:
    """Validate every workload's start_layer up front, whichever one is being loaded.

    Deferring it to the workload's own load means a typo in a sibling stage surfaces from
    inside a running deploy, hours and real spend after the cluster was mutated."""
    job = raw.get("job") or {}
    if "start_layer" in job:
        # unscoped it reaches every workload, and layer 2 is different work in each:
        # skipping ingest's L3 would also skip meshing's marching-cubes pass, and
        # l2cache (top layer 2) would be left with nothing to run at all
        raise SystemExit(
            f"{path}: job.start_layer must be scoped to one workload — set it under "
            f"job.workloads.<{'|'.join(_WORKLOADS)}>.start_layer"
        )
    for name, block in (job.get("workloads") or {}).items():
        if name not in _WORKLOADS:  # a typo silently drops the whole block
            raise SystemExit(
                f"{path}: job.workloads.{name} is not a workload — "
                f"expected one of {', '.join(_WORKLOADS)}"
            )
        if "start_layer" in (block or {}):
            _start_layer(path, block["start_layer"])


def stored() -> str:
    """The session config path (selected by the first -c), or None."""
    try:
        with open(os.path.join(CONFIG_DIR, ".current")) as stream:
            return stream.read().strip() or None
    except OSError:
        return None


def forget() -> None:
    """Clear the session config (`pipeline reset`)."""
    with contextlib.suppress(OSError):
        os.remove(os.path.join(CONFIG_DIR, ".current"))


def resolve(name: str | None = None, workload: str | None = None) -> Config:
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


def load(name: str | None = None, workload: str | None = None) -> Config:
    """Load the pipeline yaml at `name` (any path); defaults to config/pipeline.yml.

    The `dataset:` key resolves relative to the pipeline yaml's directory.
    `workload` overrides the file's — the per-workload job merge follows it."""
    path = name or os.path.join(CONFIG_DIR, "pipeline.yml")
    config_dir = os.path.dirname(path) or "."
    try:
        raw = _read_yaml(path)
    except FileNotFoundError:
        raise SystemExit(f"config not found: {path}; `pipeline reset` to pick a new one")
    for key in ("graph_id", "images"):
        if key not in raw:
            raise SystemExit(f"{path}: missing required key '{key}'")
    image = (raw.get("images") or {}).get("pcg")
    if not image:
        raise SystemExit(f"{path}: images.pcg is required")
    version = image_version(image)
    if not version:
        raise SystemExit(
            f"{path}: images.pcg '{image}' carries no version tag; pipeline requires "
            f"pychunkedgraph >= {MIN_PCG_IMAGE}. A floating (`latest`) or digest-pinned "
            f"reference cannot be checked, and the wrong image fails inside the pod."
        )
    if version < image_version(f":{MIN_PCG_IMAGE}"):
        raise SystemExit(
            f"{path}: images.pcg '{image}' is older than {MIN_PCG_IMAGE}, which "
            f"pipeline requires — earlier images lack the worker entrypoint, or size "
            f"their process pools from the node's cores and CFS-throttle every pod."
        )
    # a present-but-empty yaml key parses to None; _block/_value resolve that to the
    # field's declared default, so a default is never written twice
    bt = _block(Bigtable, raw.get("bigtable"))
    dataset, dataset_path = _read_dataset(config_dir, raw.get("dataset"))
    dataset = _with_bigtable(dataset, bt)
    raw_job = dict(raw.get("job") or {})
    workload = workload or raw.get("workload", "ingest")
    raw_job = _merge(raw_job, (raw_job.pop("workloads", None) or {}).get(workload) or {})
    ramp = _block(Ramp, raw_job.pop("ramp", None))
    if ramp.start < 1 or ramp.factor <= 1:  # else submit's ramp loop never terminates
        raise SystemExit(f"{path}: job.ramp start must be >= 1 and factor > 1")
    # checked after the workload merge, so an override cannot smuggle a class in unseen.
    # A present-but-empty `compute_class:` key parses to None and means the default class.
    raw_job["compute_class"] = compute_class = raw_job.get("compute_class") or ""
    if compute_class not in POD_BILLED_CLASSES:
        raise SystemExit(
            f"{path}: job.compute_class '{compute_class}' bills per node plus a management "
            f"premium, not per pod request, unless its spec.priorities use podFamily — which "
            f"the name alone cannot show. Use one of "
            f"{', '.join(repr(c) for c in POD_BILLED_CLASSES)} ('' is the default class)."
        )
    _check_start_layers(path, raw)
    if "start_layer" in raw_job:  # coerce here; stored raw it breaks deep inside a run
        raw_job["start_layer"] = _start_layer(path, raw_job["start_layer"])
    if raw_job.get("processes_per_vcpu") is not None:  # same: raw, it breaks in the pod
        raw_job["processes_per_vcpu"] = _processes_per_vcpu(
            path, raw_job["processes_per_vcpu"]
        )
    resources = _resources(raw_job.pop("resources", None))
    for dead in ("cpu", "memory"):
        if dead in raw_job:
            # name the file and graph: with no session config this is the *default* yaml,
            # which may belong to an entirely different project than the one intended.
            switch = (
                "`pipeline reset`, then -c <its pipeline.yml>"
                if stored()
                else "pass -c <its pipeline.yml>"
            )
            raise SystemExit(
                f"{path} (graph '{raw['graph_id']}'): job.{dead} was removed; declare "
                f"job.resources.{dead} (base/factor) instead. If you meant a different "
                f"project, {switch}."
            )
    return Config(
        namespace=_value(raw, "namespace", "default"),
        graph_id=raw["graph_id"],
        images=_block(Images, raw["images"]),
        workload_identity=_block(WorkloadIdentity, raw.get("workload_identity")),
        bigtable=bt,
        dataset=dataset,
        # _block's present-but-empty rule, applied to the splatted job scalars
        job=Job(
            ramp=ramp,
            resources=resources,
            **{k: v for k, v in raw_job.items() if v is not None},
        ),
        workload=workload,
        secret_name=_value(raw, "secret_name", "cloud-volume-secrets"),
        persistent_util=_value(raw, "persistent_util", True),
        secret_files=raw.get("secret_files") or {},
        commands=raw.get("commands") or {},
        env=raw.get("env") or {},
        database=raw.get("database") or {},
        region=_value(raw, "region", ""),
        zone=_value(raw, "zone", ""),
        config_dir=config_dir,
        source=path,
        dataset_path=dataset_path,
        fingerprint=fingerprint_of(dataset_path),
    )


def _block(cls, raw):
    """One yaml block as its config dataclass, dropping keys left present-but-empty so the
    field's declared default is the only place that default is written."""
    return cls(**{k: v for k, v in (raw or {}).items() if v is not None})


def _value(raw: dict, key: str, default):
    """A scalar from yaml, where a present-but-empty key means unset. Not `or`: `false` and
    `0` are values an operator chose, and must survive."""
    value = raw.get(key)
    return default if value is None else value


def _merge(base: dict, override: dict) -> dict:
    """Recursive dict merge — `job.workloads.<workload>` deep-overrides `job`."""
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def fingerprint_of(*paths) -> str:
    """A stable digest of the given yaml files' bytes; unreadable paths contribute nothing."""
    digest = hashlib.sha1()
    for path in paths:
        digest.update(b"\0")
        if path:
            with contextlib.suppress(OSError), open(path, "rb") as handle:
                digest.update(handle.read())
    return digest.hexdigest()[:12]


def _override_layer(key) -> int:
    """The layer an override is keyed by; yaml keys are strings, so this coerces."""
    try:
        return int(key)
    except (TypeError, ValueError):
        raise SystemExit(
            f"job.resources.overrides: '{key}' is not a layer number — key each override "
            f"by the layer it sizes, e.g. `5: {{cpu: 15, memory: 30}}`"
        )


def _override(layer, raw) -> Override:
    """One layer's exact requests. An unknown dimension is a config error: left to fall
    through it would resolve to the curve, shipping a layer nobody asked for."""
    known = {f.name for f in fields(Override)}
    if raw is not None and not isinstance(raw, dict):
        raise SystemExit(
            f"job.resources.overrides[{layer}]: expected a mapping of "
            f"{' and/or '.join(sorted(known))}, got {raw!r}"
        )
    unknown = sorted(set(raw or {}) - known)
    if unknown:
        raise SystemExit(
            f"job.resources.overrides[{layer}]: unknown {', '.join(unknown)}; "
            f"expected {' and/or '.join(sorted(known))}"
        )
    return Override(**(raw or {}))


def _resources(raw) -> Resources:
    if not raw:
        return None
    return Resources(
        cpu=Curve(**raw["cpu"]) if "cpu" in raw else None,
        memory=Curve(**raw["memory"]) if "memory" in raw else None,
        overrides={
            _override_layer(k): _override(k, v)
            for k, v in (raw.get("overrides") or {}).items()
        },
        pack=bool(_value(raw, "pack", True)),
    )


def _read_yaml(path: str) -> dict:
    """Parse a config yaml. A syntax error names the file rather than surfacing a pyyaml
    traceback, so every file this loader reads reports the same way."""
    with open(path) as stream:
        try:
            return yaml.safe_load(stream) or {}
        except yaml.YAMLError as exc:
            raise SystemExit(f"invalid yaml in {path}: {exc}")


def _read_dataset(config_dir: str, rel_path) -> tuple[dict, str]:
    """The dataset yaml and its resolved path, relative to the pipeline yaml's
    directory. A named-but-absent file fails loudly; an unconfigured dataset
    (graph-less workload) yields ({}, "")."""
    path = os.path.join(config_dir, rel_path or "dataset.yml")
    if os.path.exists(path):
        return _read_yaml(path), path
    if rel_path is not None:  # named in the pipeline yaml but not on disk
        raise SystemExit(f"dataset file not found: {path}")
    return {}, ""  # no dataset configured (graph-less workload)


def _with_bigtable(dataset: dict, bt: Bigtable) -> dict:
    """Inject the single bigtable project/instance into the dataset backend_client
    so the operator never repeats them."""
    if bt.project and bt.instance:
        cfg = dataset.setdefault("backend_client", {}).setdefault("CONFIG", {})
        cfg.setdefault("PROJECT", bt.project)
        cfg.setdefault("INSTANCE", bt.instance)
    return dataset
