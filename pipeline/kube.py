"""kubernetes client access for the pipeline CLI (exec, jobs, logs, secret)."""

import base64
import pathlib
import time
from collections import Counter

from kubernetes import client, config as kube_config
from kubernetes.client import ApiException
from kubernetes.stream import stream


def _load():
    try:
        kube_config.load_kube_config()
    except Exception:
        kube_config.load_incluster_config()


def batch():
    _load()
    return client.BatchV1Api()


def core():
    _load()
    return client.CoreV1Api()


def custom():
    _load()
    return client.CustomObjectsApi()


def util_pod(namespace: str, selector: str = "app=pipeline-util") -> str:
    pods = core().list_namespaced_pod(namespace, label_selector=selector).items
    running = [p for p in pods if p.status.phase == "Running"]
    if not running:
        raise SystemExit(
            f"no running pipeline-util pod in ns '{namespace}'; run `pipeline deploy` first"
        )
    return running[0].metadata.name


def list_jobs(namespace: str, workload: str = None):
    """Layer Jobs — one workload's, or every pipeline Job when workload is None."""
    selector = f"pipeline={workload}" if workload else "pipeline"
    return batch().list_namespaced_job(namespace, label_selector=selector).items


def node_summary():
    """(total, spot, {instance_type: count}) for the cluster — Autopilot capacity."""
    labels = [n.metadata.labels or {} for n in core().list_node().items]
    by_type = Counter(l.get("node.kubernetes.io/instance-type", "?") for l in labels)
    spot = sum(1 for l in labels if l.get("cloud.google.com/gke-spot") == "true")
    return len(labels), spot, dict(by_type)


def exec_cmd(namespace: str, pod: str, argv: list) -> str:
    """Run argv in the pod and return its stdout."""
    return stream(
        core().connect_get_namespaced_pod_exec,
        pod,
        namespace,
        command=argv,
        stderr=True,
        stdout=True,
        stdin=False,
        tty=False,
        _preload_content=True,
    ).strip()


def secret_data(secrets_dir: str, mapping) -> dict:
    """{container_filename: local_path} -> {container_filename: base64(contents)};
    local files (relative to secrets_dir) can be named/organized however you like."""
    base = pathlib.Path(secrets_dir)
    data = {}
    for key, rel in mapping.items():
        p = base / rel
        if not p.is_file():
            raise SystemExit(f"secret file not found: {p}")
        data[key] = base64.b64encode(p.read_bytes()).decode()
    return data


def pods_of(namespace: str, job_name: str):
    return (
        core()
        .list_namespaced_pod(
            namespace, label_selector=f"batch.kubernetes.io/job-name={job_name}"
        )
        .items
    )


def pod_metrics(namespace: str, job_name: str):
    """Per-pod usage from the metrics API; [] if metrics-server is unavailable."""
    try:
        objs = custom().list_namespaced_custom_object(
            "metrics.k8s.io",
            "v1beta1",
            namespace,
            "pods",
            label_selector=f"batch.kubernetes.io/job-name={job_name}",
        )
    except ApiException:
        return []
    return objs.get("items", [])


def job_events(namespace: str, job_name: str):
    """Events for the Job and its pods (scheduling, scale-up, failures), oldest first."""
    names = {job_name} | {p.metadata.name for p in pods_of(namespace, job_name)}
    evs = [
        e
        for e in core().list_namespaced_event(namespace).items
        if e.involved_object.name in names
    ]
    return sorted(
        evs,
        key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp,
    )


def delete_job(namespace: str, name: str):
    batch().delete_namespaced_job(name, namespace, propagation_policy="Foreground")


def recreate_job(namespace: str, spec):
    """Replace any existing Job of the same name, then create it — so a layer can be
    re-submitted (done chunks are then skipped by the per-chunk lock)."""
    name = spec.metadata.name
    b = batch()
    try:
        b.delete_namespaced_job(name, namespace, propagation_policy="Foreground")
        for _ in range(60):
            try:
                b.read_namespaced_job(name, namespace)
                time.sleep(1)
            except ApiException as exc:
                if exc.status == 404:
                    break
                raise
    except ApiException as exc:
        if exc.status != 404:
            raise
    b.create_namespaced_job(namespace, spec)


def _delete_pod_if_exists(c, namespace, name):
    try:
        c.delete_namespaced_pod(name, namespace, grace_period_seconds=0)
        for _ in range(30):
            try:
                c.read_namespaced_pod(name, namespace)
                time.sleep(1)
            except ApiException as exc:
                if exc.status == 404:
                    return
                raise
    except ApiException as exc:
        if exc.status != 404:
            raise


def run_oneshot(namespace: str, pod_spec) -> str:
    """Create a one-shot pod, wait for it to finish, return its stdout, then delete it."""
    c = core()
    name = pod_spec.metadata.name
    _delete_pod_if_exists(c, namespace, name)
    c.create_namespaced_pod(namespace, pod_spec)
    phase = "Pending"
    try:
        for _ in range(600):  # allow time for an Autopilot node to be provisioned
            phase = c.read_namespaced_pod_status(name, namespace).status.phase
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(2)
        log = c.read_namespaced_pod_log(name, namespace)
        if phase != "Succeeded":
            raise SystemExit(f"{name} {phase}:\n{log}")
        return log
    finally:
        _delete_pod_if_exists(c, namespace, name)


def set_parallelism(namespace: str, name: str, parallelism: int):
    # merge patch of one field (a full V1JobSpec would require `template`)
    batch().patch_namespaced_job(name, namespace, {"spec": {"parallelism": parallelism}})
