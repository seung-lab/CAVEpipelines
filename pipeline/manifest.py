"""Builders: Config -> Indexed Job / one-shot Pod (kubernetes client objects), and
helm values (dicts, rendered to YAML by helm — not client objects)."""

from kubernetes import client

from . import config as cfgmod

INGEST_COMMAND = ["python", "-m", "pychunkedgraph.pipeline.ingest"]
MESHING_COMMAND = ["python", "-m", "pychunkedgraph.pipeline.meshing"]
SPOT_SELECTOR = {"cloud.google.com/gke-spot": "true"}
SPOT_TOLERATION = {
    "key": "cloud.google.com/gke-spot",
    "operator": "Equal",
    "value": "true",
    "effect": "NoSchedule",
}
UTIL_REQUESTS = {"cpu": "250m", "memory": "1Gi"}  # cheapest that still imports PCG


def job_name(cfg, layer: int) -> str:
    return f"{cfg.workload}-l{layer}"


def command_for(cfg):
    if cfg.workload == "ingest":
        return INGEST_COMMAND
    if cfg.workload == "meshing":
        return MESHING_COMMAND
    return cfg.commands.get(cfg.workload)  # l2cache entrypoint from pipeline.yml


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


def job_spec(
    cfg,
    layer: int,
    chunks: int,
    completions: int,
    parallelism: int,
    *,
    batch_size=None,
    name=None,
) -> client.V1Job:
    command = command_for(cfg)
    if not command:
        raise SystemExit(
            f"no container command for '{cfg.workload}'; set commands.{cfg.workload} in pipeline.yml"
        )
    batch_size = cfg.job.batch_size if batch_size is None else batch_size
    name = name or job_name(cfg, layer)
    node_selector = dict(SPOT_SELECTOR)
    if cfg.job.compute_class:
        node_selector["cloud.google.com/compute-class"] = cfg.job.compute_class

    container = client.V1Container(
        name=cfg.workload,
        image=cfg.image(),
        command=command,
        env=[
            client.V1EnvVar(name=k, value=v)
            for k, v in (
                ("PCG_GRAPH_ID", cfg.graph_id),
                ("PCG_LAYER", str(layer)),
                ("PCG_PERM_SEED", str(cfg.job.perm_seed)),
                ("PCG_BATCH_SIZE", str(batch_size)),
                ("PCG_N_THREADS", str(cfg.job.n_threads)),
            )
        ],
        env_from=_env_from(),
        resources=client.V1ResourceRequirements(
            requests={"cpu": cfg.job.cpu, "memory": cfg.job.memory}
        ),
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
            # total chunks (N) so `status` shows chunk progress without re-reading meta
            annotations={"chunks": str(chunks), "batch_size": str(batch_size)},
        ),
        spec=client.V1JobSpec(
            completion_mode="Indexed",
            completions=completions,
            parallelism=parallelism,
            backoff_limit_per_index=cfg.job.backoff_limit_per_index,
            max_failed_indexes=cfg.job.max_failed_indexes,
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


def oneshot_pod_spec(cfg, name: str, argv: list) -> client.V1Pod:
    """A transient PCG-image pod (setup / meta read) on a spot node; the CLI deletes it
    after, so with persistent_util off the cluster sits at zero nodes when idle."""
    container = client.V1Container(
        name="util",
        image=cfg.images.pcg,
        command=argv,
        env_from=_env_from(),
        resources=client.V1ResourceRequirements(requests=UTIL_REQUESTS),
        volume_mounts=[
            client.V1VolumeMount(name="datasets", mount_path="/app/datasets"),
            _secrets_mount(),
        ],
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
            volumes=[
                client.V1Volume(
                    name="datasets",
                    config_map=client.V1ConfigMapVolumeSource(
                        name=cfgmod.DATASET_CONFIGMAP
                    ),
                ),
                _secrets_volume(cfg),
            ],
        ),
    )


def helm_values(cfg) -> dict:
    values = {
        "namespace": cfg.namespace,
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
                },
            }
        ],
        "configyamls": [
            {
                "name": cfgmod.DATASET_CONFIGMAP,
                "namespace": cfg.namespace,
                "files": [{"name": "dataset.yml", "content": cfg.dataset}],
            }
        ],
    }
    if cfg.persistent_util:
        values["deployments"] = [_util_deployment(cfg)]
    return values


def _util_deployment(cfg) -> dict:
    repo, _, tag = cfg.images.pcg.rpartition(":")
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
                "name": "datasets-volume",
                "configMap": {"name": cfgmod.DATASET_CONFIGMAP},
            },
            {
                "name": "secrets-volume",
                "secret": {"secretName": cfg.secret_name, "optional": True},
            },
        ],
        "containers": [
            {
                "name": "util",
                "image": {"repository": repo, "tag": tag or "latest"},
                "volumeMounts": [
                    {"name": "datasets-volume", "mountPath": "/app/datasets"},
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
