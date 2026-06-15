"""Builders: Config -> Indexed Job / one-shot Pod (kubernetes client objects), and
helm values (dicts, rendered to YAML by helm — not client objects)."""

import hashlib
import re

from kubernetes import client

from . import cgcache, config as cfgmod, note
from .costs import normalize_requests, parse_cpu, parse_mem

INGEST_COMMAND = ["python", "-m", "pychunkedgraph.pipeline.ingest"]
MESHING_COMMAND = ["python", "-m", "pychunkedgraph.pipeline.meshing"]
MIGRATE_COMMAND = ["python", "-m", "pychunkedgraph.pipeline.migrate"]
L2CACHE_COMMAND = ["python", "-m", "pcgl2cache.pipeline.l2cache"]
SPOT_SELECTOR = {"cloud.google.com/gke-spot": "true"}
SPOT_TOLERATION = {
    "key": "cloud.google.com/gke-spot",
    "operator": "Equal",
    "value": "true",
    "effect": "NoSchedule",
}
UTIL_REQUESTS = {"cpu": "250m", "memory": "1Gi"}  # cheapest that still imports PCG


def job_name(cfg, layer: int) -> str:
    # DNS-1123: migrate_cleanup's underscore is invalid in Job/container names
    return f"{cfg.workload.replace('_', '-')}-l{layer}"


def _curve_value(curve, layer: int) -> float:
    val = curve.base * curve.factor ** (layer - 2) + curve.add
    return min(val, curve.max) if curve.max else val


def requests_for(job, layer: int) -> tuple:
    """(vCPU, GiB) for a layer: override > curve > flat cpu/memory, per dimension."""
    res = job.resources
    over = res.overrides.get(layer, {}) if res else {}
    if "cpu" in over:
        cpu = float(over["cpu"])
    elif res and res.cpu:
        cpu = _curve_value(res.cpu, layer)
    else:
        cpu = parse_cpu(job.cpu)
    if "memory" in over:
        mem = float(over["memory"])
    elif res and res.memory:
        mem = _curve_value(res.memory, layer)
    else:
        mem = parse_mem(job.memory)
    return cpu, mem


def batch_for(job, layer: int) -> int:
    """Chunks per task: halves every layer above 2 — a parent chunk covers ~8x
    the volume of its children, so a flat batch would balloon upper-layer tasks."""
    return max(1, job.batch_size // 2 ** (layer - 2))


def layer_requests(job, layer: int) -> dict:
    """The layer's normalized k8s requests — the cheapest valid Autopilot point."""
    cpu, mem = requests_for(job, layer)
    cpu, mem, warnings, errors = normalize_requests(cpu, mem, job.compute_class)
    if errors:
        raise SystemExit("; ".join(errors))
    for warning in warnings:
        note(warning)
    return {"cpu": f"{round(cpu * 1000)}m", "memory": f"{round(mem * 1024)}Mi"}


def dataset_configmap_name(graph_id: str) -> str:
    """DNS-safe per-graph ConfigMap name; the raw id lives in the `graph` label."""
    safe = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", graph_id.lower())).strip("-")
    budget = 63 - len("pcg-dataset-") - 7  # keep room for the "-xxxxxx" disambiguator
    changed = safe != graph_id or len(safe) > budget
    safe = safe[:budget].rstrip("-")
    if changed:  # sanitized or truncated ids could collide without the hash
        safe = f"{safe}-{hashlib.sha1(graph_id.encode()).hexdigest()[:6]}"
    return f"pcg-dataset-{safe}"


def command_for(cfg):
    if cfg.workload == "ingest":
        return INGEST_COMMAND
    if cfg.workload == "meshing":
        return MESHING_COMMAND
    if cfg.workload == "migrate":
        return MIGRATE_COMMAND
    if cfg.workload == "migrate_cleanup":
        return MIGRATE_COMMAND + ["--clean"]
    if cfg.workload == "l2cache":  # commands.l2cache overrides the built-in
        return cfg.commands.get("l2cache") or L2CACHE_COMMAND
    return cfg.commands.get(cfg.workload)  # any other custom workload from pipeline.yml


def _spot_tolerations():
    return [client.V1Toleration(**SPOT_TOLERATION)]


def _secrets_mount():
    return client.V1VolumeMount(
        name="secrets", mount_path="/root/.cloudvolume/secrets", read_only=True
    )


def _secrets_volume(cfg):
    return client.V1Volume(
        name="secrets",
        secret=client.V1SecretVolumeSource(secret_name=cfg.secret_name, optional=True),
    )


def _env_from():
    return [
        client.V1EnvFromSource(
            config_map_ref=client.V1ConfigMapEnvSource(name=cfgmod.ENV_CONFIGMAP)
        )
    ]


def _extra_env(cfg):
    """Operator env from pipeline.yml ``env:`` — injected into worker + util containers.
    Unset keys are skipped: container env overrides the ConfigMap, so injecting an
    empty value would clobber vars published there (e.g. BIGTABLE_*)."""
    return [
        client.V1EnvVar(name=k, value=str(v))
        for k, v in cfg.env.items()
        if v is not None and v != ""
    ]


def _l2cache_env(cfg):
    """The l2cache worker's config from ``l2cache_config`` (graphene cv + cache table);
    the snapshot timestamp is read worker-side off the cache table (recorded at setup,
    so the whole fleet shares it), not passed here. Empty for other workloads."""
    if cfg.workload != "l2cache":
        return []
    lc = cfg.dataset.get("l2cache_config") or {}
    pairs = {
        "L2CACHE_CV_PATH": lc.get("cv_path"),
        "L2CACHE_TABLE_ID": lc.get("table_id"),
    }
    return [client.V1EnvVar(name=k, value=str(v)) for k, v in pairs.items() if v]


def job_spec(
    cfg,
    layer: int,
    chunks: int,
    completions: int,
    parallelism: int,
    *,
    batch_size=None,
    name=None,
    run_id="",
) -> client.V1Job:
    command = command_for(cfg)
    if not command:
        raise SystemExit(
            f"no container command for '{cfg.workload}'; set commands.{cfg.workload} in pipeline.yml"
        )
    batch_size = batch_for(cfg.job, layer) if batch_size is None else batch_size
    name = name or job_name(cfg, layer)
    node_selector = dict(SPOT_SELECTOR)
    if cfg.job.compute_class:
        node_selector["cloud.google.com/compute-class"] = cfg.job.compute_class
    # optional: co-locate workers in one zone (e.g. Bigtable's) for lower latency
    if cfg.zone:
        node_selector["topology.kubernetes.io/zone"] = cfg.zone

    container = client.V1Container(
        name=cfg.workload.replace("_", "-"),
        image=cfg.image(),
        command=command,
        env=[
            client.V1EnvVar(name=k, value=v)
            for k, v in (
                ("PCG_GRAPH_ID", cfg.graph_id),
                ("PCG_LAYER", str(layer)),
                ("PCG_PERM_SEED", str(cfg.job.perm_seed)),
                ("PCG_BATCH_SIZE", str(batch_size)),
                # any value >1 opens the builder's process-pool gate; the pool then
                # sizes itself to every core — the number itself is never a count
                ("PCG_N_THREADS", "2" if cfg.job.parallel else "1"),
            )
        ]
        + _l2cache_env(cfg)
        + _extra_env(cfg),
        env_from=_env_from(),
        resources=client.V1ResourceRequirements(requests=layer_requests(cfg.job, layer)),
        volume_mounts=[_secrets_mount()],
    )
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"pipeline": cfg.workload}),
        spec=client.V1PodSpec(
            restart_policy="Never",
            service_account_name=cfg.workload_identity.service_account,
            node_selector=node_selector,
            tolerations=_spot_tolerations(),
            containers=[container],
            volumes=[_secrets_volume(cfg)],
        ),
    )
    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            name=name,
            labels={
                "pipeline": cfg.workload,
                "graph": cfg.graph_id,
                "layer": str(layer),
            },
            # total chunks (N) so `status` shows chunk progress without re-reading meta;
            # run-id tags this Job's cost rows with the deploy that created it
            annotations={
                "chunks": str(chunks),
                "batch_size": str(batch_size),
                "run-id": run_id,
            },
        ),
        spec=client.V1JobSpec(
            completion_mode="Indexed",
            completions=completions,
            parallelism=parallelism,
            backoff_limit_per_index=cfg.job.task_retries,
            # k8s rejects maxFailedIndexes > completions; small layers have few tasks
            max_failed_indexes=min(cfg.job.max_failed_tasks, completions),
            pod_failure_policy=client.V1PodFailurePolicy(
                rules=[
                    client.V1PodFailurePolicyRule(
                        action="Ignore",
                        on_pod_conditions=[
                            client.V1PodFailurePolicyOnPodConditionsPattern(
                                type="DisruptionTarget", status="True"
                            )
                        ],
                    ),
                    client.V1PodFailurePolicyRule(
                        action="FailIndex",
                        on_exit_codes=client.V1PodFailurePolicyOnExitCodesRequirement(
                            operator="In", values=[42]
                        ),
                    ),
                ]
            ),
            template=template,
        ),
    )


def max_parallelism(ramp_max: int, completions: int) -> int:
    """A Job's parallelism ceiling — never more concurrent pods than there are tasks."""
    return min(ramp_max, completions)


def immutable_drift(cfg, layer: int, job) -> list:
    """``(field, running, desired)`` for every immutable Job field whose yml value differs
    from the running Job — mirrors ``job_spec``. The live ``apply`` fields (resources,
    parallelism) and the per-deploy ``run-id`` are excluded; graph ownership is guarded
    separately by ``check_graph_owner``."""
    spec = job.spec
    container = spec.template.spec.containers[0]
    env = {e.name: e.value for e in (container.env or [])}
    selector = spec.template.spec.node_selector or {}
    annotations = job.metadata.annotations or {}
    checks = [
        ("perm_seed", env.get("PCG_PERM_SEED"), str(cfg.job.perm_seed)),
        ("batch_size", annotations.get("batch_size"), str(batch_for(cfg.job, layer))),
        ("parallel", env.get("PCG_N_THREADS"), "2" if cfg.job.parallel else "1"),
        ("compute_class", selector.get("cloud.google.com/compute-class", ""), cfg.job.compute_class),
        ("zone", selector.get("topology.kubernetes.io/zone", ""), cfg.zone),
        ("image", container.image, cfg.image()),
        ("task_retries", spec.backoff_limit_per_index, cfg.job.task_retries),
        ("max_failed_tasks", spec.max_failed_indexes, min(cfg.job.max_failed_tasks, spec.completions)),
    ]
    for var in _l2cache_env(cfg) + _extra_env(cfg):  # workload + operator env vars
        checks.append((f"env:{var.name}", env.get(var.name), var.value))
    return [(field, run, want) for field, run, want in checks if str(run) != str(want)]


def oneshot_pod_spec(
    cfg, name: str, argv: list, dataset_configmap=None, image=None
) -> client.V1Pod:
    """A transient pod (setup / meta read) on a spot node; the CLI deletes it after.
    Runs the active workload's image by default; `image` overrides it for a cross-workload
    op (the cg-meta probe always reads the graph in the PCG image). The dataset mount is
    opt-in — only setup/mesh-meta read the file."""
    mounts = [_secrets_mount()]
    volumes = [_secrets_volume(cfg)]
    if dataset_configmap:
        mounts.insert(
            0, client.V1VolumeMount(name="datasets", mount_path="/app/datasets")
        )
        volumes.insert(
            0,
            client.V1Volume(
                name="datasets",
                config_map=client.V1ConfigMapVolumeSource(name=dataset_configmap),
            ),
        )
    container = client.V1Container(
        name="util",
        image=image or cfg.image(),
        command=argv,
        env=_extra_env(cfg),
        env_from=_env_from(),
        resources=client.V1ResourceRequirements(requests=UTIL_REQUESTS),
        volume_mounts=mounts,
    )
    return client.V1Pod(
        api_version="v1",
        kind="Pod",
        metadata=client.V1ObjectMeta(name=name, labels={"pipeline": "util"}),
        spec=client.V1PodSpec(
            restart_policy="Never",
            service_account_name=cfg.workload_identity.service_account,
            node_selector=SPOT_SELECTOR,
            tolerations=_spot_tolerations(),
            containers=[container],
            volumes=volumes,
        ),
    )


def helm_values(cfg, secret_data=None) -> dict:
    values = {
        "serviceAccounts": [
            {
                "name": cfg.workload_identity.service_account,
                "namespace": cfg.namespace,
                "annotations": {
                    "iam.gke.io/gcp-service-account": cfg.workload_identity.gsa_email
                },
            }
        ],
        "env": [
            {
                "name": cfgmod.ENV_CONFIGMAP,
                "namespace": cfg.namespace,
                "vars": {
                    "BIGTABLE_PROJECT": cfg.bigtable.project,
                    "BIGTABLE_INSTANCE": cfg.bigtable.instance,
                    # ADC: all Google clients (Bigtable + buckets) use the mounted key
                    "GOOGLE_APPLICATION_CREDENTIALS": "/root/.cloudvolume/secrets/google-secret.json",
                },
            }
        ],
    }
    # helm owns the Secret's lifecycle (created/updated/removed with the release)
    values["secrets"] = (
        [{"name": cfg.secret_name, "namespace": cfg.namespace, "data": secret_data}]
        if secret_data
        else []
    )
    if cfg.persistent_util:
        values["deployments"] = [_util_deployment(cfg)]
    return values


def _util_deployment(cfg) -> dict:
    repo, sep, tag = cfg.images.pcg.rpartition(":")
    if not sep or "/" in tag:  # untagged image, or the colon was a registry port
        repo, tag = cfg.images.pcg, ""
    return {
        "enabled": True,
        "name": "pipeline-util",
        "namespace": cfg.namespace,
        "replicaCount": 1,
        "serviceAccountName": cfg.workload_identity.service_account,
        "hpa": {"enabled": False},
        "nodeSelector": SPOT_SELECTOR,
        "tolerations": [SPOT_TOLERATION],
        "affinity": {},
        "imagePullSecrets": [],
        "volumes": [
            {
                "name": "secrets-volume",
                "secret": {"secretName": cfg.secret_name, "optional": True},
            },
        ],
        "containers": [
            {
                "name": "util",
                "image": {"repository": repo, "tag": tag or "latest"},
                "command": ["python", "-u", "-c", cgcache.SERVER_SRC, cgcache.CG_SOCK],
                "volumeMounts": [
                    {
                        "name": "secrets-volume",
                        "mountPath": "/root/.cloudvolume/secrets",
                        "readOnly": True,
                    },
                ],
                "envFromConfigMap": [cfgmod.ENV_CONFIGMAP],
                "resources": {"requests": UTIL_REQUESTS},
            }
        ],
    }
