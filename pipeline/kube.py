"""kubernetes client access for the pipeline CLI (exec, jobs, logs, secret)."""

import base64
import pathlib
import time
from collections import Counter

from kubernetes import client, config as kube_config
from kubernetes.client import ApiException
from kubernetes.stream import stream

from . import note


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


def util_pod(
    namespace: str,
    selector: str = "app=pipeline-util",
    timeout: int = 600,
    wait_create: bool = False,
) -> str:
    """Name of the running util pod; waits while it's Pending (Autopilot node spin-up).
    With `wait_create`, also waits for the pod to first appear (e.g. right after deploy)."""
    c = core()
    waiting = False
    for _ in range(timeout // 2):
        pods = c.list_namespaced_pod(namespace, label_selector=selector).items
        running = [
            p
            for p in pods
            if p.status.phase == "Running" and not p.metadata.deletion_timestamp
        ]
        if running:
            return running[0].metadata.name
        if not pods and not wait_create:
            raise SystemExit(
                f"no pipeline-util pod in ns '{namespace}'; run `pipeline deploy` first"
            )
        # terminating pods are phase Running but dying (e.g. mid helm rollout) — wait;
        # an absent pod under wait_create is also transitional (still being created)
        transitional = (not pods and wait_create) or any(
            p.status.phase == "Pending" or p.metadata.deletion_timestamp for p in pods
        )
        if not transitional:
            raise SystemExit(
                f"pipeline-util pod is {pods[0].status.phase}; re-run `pipeline deploy`"
            )
        if not waiting:
            note("waiting for util pod to start...")
            waiting = True
        time.sleep(2)
    raise SystemExit(
        f"util pod not running after {timeout}s; "
        f"kubectl describe pod -n {namespace} -l app=pipeline-util"
    )


def list_jobs(namespace: str, workload: str = None):
    """Layer Jobs — one workload's, or every pipeline Job when workload is None."""
    selector = f"pipeline={workload}" if workload else "pipeline"
    return batch().list_namespaced_job(namespace, label_selector=selector).items


def node_summary():
    """(total, spot, {instance_type: count}) for the cluster — Autopilot capacity."""
    labels = [n.metadata.labels or {} for n in core().list_node().items]
    by_type = Counter(lbl.get("node.kubernetes.io/instance-type", "?") for lbl in labels)
    spot = sum(1 for lbl in labels if lbl.get("cloud.google.com/gke-spot") == "true")
    return len(labels), spot, dict(by_type)


# In-pod shutdown noise dropped from streamed logs (we os._exit, so it's spurious).
_NOISE = ("resource_tracker:", "leaked semaphore")


def exec_cmd(
    namespace: str, pod: str, argv: list, timeout: int = 300, on_line=None
) -> str:
    """Run argv in the pod, streaming stdout+stderr to `on_line` as it arrives, and
    return the full stdout. Aborts after `timeout`s so a wedged command fails loudly
    instead of hanging silently. PCG logs to stderr, so both channels are forwarded."""
    try:
        ws = stream(
            core().connect_get_namespaced_pod_exec,
            pod,
            namespace,
            command=argv,
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
            _preload_content=False,
        )
    except ApiException as exc:
        raise SystemExit(
            f"exec into pod '{pod}' failed ({exc.status} {exc.reason}); "
            f"the pod may have just restarted — retry"
        )
    out_buf, partial = [], {1: "", 2: ""}

    def drain():
        # emit only whole lines as they complete; keep the trailing fragment buffered
        for chan, text in ((1, ws.read_stdout()), (2, ws.read_stderr())):
            if chan == 1:
                out_buf.append(text)
            if not text:
                continue
            partial[chan] += text
            *lines, partial[chan] = partial[chan].split("\n")
            if on_line:
                for line in lines:
                    if not any(n in line for n in _NOISE):
                        on_line(line)

    deadline = time.monotonic() + timeout
    while ws.is_open():
        if time.monotonic() > deadline:
            ws.close()
            drain()
            raise SystemExit(
                f"in-pod command timed out after {timeout}s: {' '.join(argv)}"
            )
        ws.update(timeout=1)
        drain()
    if on_line:  # flush any unterminated trailing line
        for chan in (1, 2):
            if partial[chan]:
                on_line(partial[chan])
    if ws.returncode:
        raise SystemExit(f"in-pod command exited {ws.returncode}: {' '.join(argv)}")
    return "".join(out_buf).strip()


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


def apply_configmap(namespace: str, name: str, data: dict, labels: dict) -> None:
    """Create or replace a ConfigMap — re-applying keeps its content fresh."""
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=name, labels=labels), data=data
    )
    c = core()
    try:
        c.create_namespaced_config_map(namespace, body)
    except ApiException as exc:
        if exc.status != 409:
            raise
        c.replace_namespaced_config_map(name, namespace, body)


def list_configmaps(namespace: str, selector: str):
    return core().list_namespaced_config_map(namespace, label_selector=selector).items


def delete_configmap(namespace: str, name: str) -> None:
    core().delete_namespaced_config_map(name, namespace)


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
        note(f"{name}: replacing existing job")
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
    note(f"{name}: running")
    phase = "Pending"
    try:
        for _ in range(600):  # allow time for an Autopilot node to be provisioned
            phase = c.read_namespaced_pod_status(name, namespace).status.phase
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(2)
        if phase not in ("Succeeded", "Failed"):
            raise SystemExit(
                f"one-shot pod '{name}' still {phase} after 20m (deleting it); "
                f"check capacity/quota: kubectl get events -n {namespace}"
            )
        log = c.read_namespaced_pod_log(name, namespace)
        if phase != "Succeeded":
            raise SystemExit(f"{name} {phase}:\n{log}")
        return log
    finally:
        _delete_pod_if_exists(c, namespace, name)


def set_parallelism(namespace: str, name: str, parallelism: int):
    # merge patch of one field (a full V1JobSpec would require `template`)
    batch().patch_namespaced_job(name, namespace, {"spec": {"parallelism": parallelism}})
