"""kubernetes client access for the pipeline CLI (exec, jobs, logs, secret)."""

import base64
import contextlib
import functools
import pathlib
import time

from kubernetes import client
from kubernetes import config as kube_config
from kubernetes.client import ApiException
from kubernetes.stream import stream

from . import note

# A sanity bound, not a fleet-sized wait: delete_job clears pods as one collection and
# drops the Job in Background, so the name frees at once. (Foreground would hold it for
# 20+ minutes on a 12,000-pod layer while the GC walks every pod.)
DELETE_TIMEOUT = 60


def _load():
    try:
        kube_config.load_kube_config()
    except Exception:  # noqa: BLE001 - any unusable kubeconfig falls through to in-cluster
        try:
            kube_config.load_incluster_config()
        except Exception:  # noqa: BLE001 - neither source worked; report both as one
            raise SystemExit(
                "cannot load kube config; set KUBECONFIG or run "
                "`gcloud container clusters get-credentials <cluster>`"
            )


# cached: a fresh ApiClient per call would re-read kubeconfig and re-handshake
# TLS on every tick of the live status/top loops
@functools.cache
def batch():
    _load()
    return client.BatchV1Api()


@functools.cache
def core():
    _load()
    return client.CoreV1Api()


@functools.cache
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


def list_jobs(namespace: str, workload: str | None = None):
    """Layer Jobs — one workload's, or every pipeline Job when workload is None."""
    selector = f"pipeline={workload}" if workload else "pipeline"
    return batch().list_namespaced_job(namespace, label_selector=selector).items


def oom_events(namespace: str):
    """Cluster OOMKilling events (kubelet emits them node-level) in the namespace."""
    return (
        core().list_namespaced_event(namespace, field_selector="reason=OOMKilling").items
    )


def unfinished_pods(namespace: str, job_name: str):
    """A job's not-yet-Succeeded pods (active/pending/failed) — cheap, field-selected."""
    return (
        core()
        .list_namespaced_pod(
            namespace,
            label_selector=f"batch.kubernetes.io/job-name={job_name}",
            field_selector="status.phase!=Succeeded",
        )
        .items
    )


def node_summary():
    """(total, spot, vCPU, GiB) for the cluster — Autopilot capacity.

    cpu/memory sum each node's *allocatable* (capacity minus kubelet/system reserve),
    so the totals are what pods can actually be scheduled against."""
    from .costs import parse_cpu, parse_mem

    nodes = core().list_node().items
    labels = [n.metadata.labels or {} for n in nodes]
    spot = sum(1 for lbl in labels if lbl.get("cloud.google.com/gke-spot") == "true")
    alloc = [(n.status.allocatable or {}) if n.status else {} for n in nodes]
    cpu = sum(parse_cpu(a.get("cpu")) for a in alloc)
    gib = sum(parse_mem(a.get("memory")) for a in alloc)
    return len(nodes), spot, cpu, gib


# Pod-log noise the operator never needs, dropped from both streamed and saved logs:
# Python interpreter-shutdown chatter (we os._exit, so it's spurious) and the C++ auth
# provider's startup banner.
LOG_NOISE = (
    "resource_tracker:",
    "leaked semaphore",
    "google_auth_provider.cc",
    "Using ServiceAccount AuthProvider",
)


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
    out_buf, err_buf, partial = [], [], {1: "", 2: ""}

    def emit(line):  # echo a whole line unless it's pod-log noise
        if line and on_line and not any(n in line for n in LOG_NOISE):
            on_line(line)

    def drain():
        # emit only whole lines as they complete; keep the trailing fragment buffered
        for chan, text in ((1, ws.read_stdout()), (2, ws.read_stderr())):
            (out_buf if chan == 1 else err_buf).append(text)
            if not text:
                continue
            partial[chan] += text
            *lines, partial[chan] = partial[chan].split("\n")
            for line in lines:
                emit(line)

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
    for chan in (1, 2):  # flush any unterminated trailing line
        emit(partial[chan])
    if ws.returncode:
        shown = " ".join(a if len(a) <= 60 else a[:57] + "..." for a in argv)
        reason = ([ln for ln in "".join(err_buf).splitlines() if ln.strip()] or [""])[-1]
        raise SystemExit(
            f"in-pod command exited {ws.returncode}: {shown}"
            + (f"\n  {reason.strip()}" if reason else "")
        )
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


def pods_of_uid(namespace: str, job_uid: str, *, unfinished: bool = False):
    """Pods of one Job *generation*.

    The name is reused across generations and Background deletion frees it before the old
    pods are reaped, so a name lookup can return a previous generation's pods — which cost
    accounting would then bill against this Job's uid. The controller-uid label is stamped
    per generation and cannot alias.

    `unfinished` drops Succeeded pods server-side. A Job retains them until GC, so the full
    list grows with every completed task (100+ MB on a 1.5M-chunk layer) while those rows
    are already final in the cost DB — see db.cost.sample."""
    kwargs = {"label_selector": f"batch.kubernetes.io/controller-uid={job_uid}"}
    if unfinished:
        kwargs["field_selector"] = "status.phase!=Succeeded"
    return core().list_namespaced_pod(namespace, **kwargs).items


def read_job(namespace: str, name: str):
    """The Job, or None when it does not exist — any other ApiException propagates."""
    try:
        return batch().read_namespaced_job(name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


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


@contextlib.contextmanager
def tolerate(*statuses: int):
    """Swallow an ApiException whose status is one of `statuses`; anything else propagates.

    Only for handlers that do nothing but re-raise — a handler that falls back, returns a
    value, or notes something keeps its `except`, because that behaviour is the point."""
    try:
        yield
    except ApiException as exc:
        if exc.status not in statuses:
            raise


JOB_TRACKING_FINALIZER = "batch.kubernetes.io/job-tracking"


def _release_tracked_pods(namespace: str, name: str) -> int:
    """Force-clear finalizers on condemned, non-succeeded pods; count freed.

    This is the *suspended-Job* case: the Job controller removes the tracking finalizer
    only while reconciling, so a suspended Job whose nodes were scaled away leaves the
    pod objects behind indefinitely. (Deleting a Job instead makes the controller sweep
    its selector, which clears the rest — nothing to do there.) Succeeded pods are left
    alone: the finalizer is how their completion is counted, and dropping it early would
    lose it. Releasing a Failed pod costs its attempt against backoffLimitPerIndex, so
    this touches only pods already condemned; `pause` spares terminal pods.

    The patch nulls the whole list rather than removing one entry — under the
    strategic-merge content type the client sends, a filtered list would be *unioned*
    back and remove nothing. Core Kubernetes puts only the tracking finalizer on Job
    pods, so there is nothing else here to lose."""
    freed = 0
    c = core()
    for pod in unfinished_pods(namespace, name):
        meta = pod.metadata
        if pod.status and pod.status.phase == "Succeeded":
            continue
        if not meta.deletion_timestamp:
            continue
        if JOB_TRACKING_FINALIZER not in (meta.finalizers or []):
            continue
        with tolerate(404):  # already gone between list and patch
            c.patch_namespaced_pod(
                meta.name, namespace, {"metadata": {"finalizers": None}}
            )
            freed += 1
    return freed


def delete_job_pods(namespace: str, name: str, *, keep_terminal: bool = False):
    """Drop every pod of a Job in one call, without waiting out any grace period.

    One collection delete, not one request per pod: letting a controller reap them
    individually is bounded by the garbage collector's rate and takes tens of minutes on
    a fleet this size. The delete only marks them — see _release_tracked_pods for why the
    objects then need the tracking finalizer cleared to actually disappear.

    `keep_terminal` spares Succeeded and Failed pods, for the one caller whose Job
    survives the call (`pause`). They hold no node resources, so keeping them costs
    nothing, and a Failed pod is the only record of why its task died — which is what
    `pipeline inspect <layer> <index>` reads. Leaving them also leaves their tally to the
    Job controller, which still needs it to enforce the per-index retry budget."""
    kwargs = {
        "label_selector": f"batch.kubernetes.io/job-name={name}",
        "grace_period_seconds": 0,
        "propagation_policy": "Background",
    }
    if keep_terminal:
        kwargs["field_selector"] = "status.phase!=Succeeded,status.phase!=Failed"
    with tolerate(404):  # no pods to drop
        core().delete_collection_namespaced_pod(namespace, **kwargs)
    # only when the Job survives the call: a deleted Job makes the controller sweep its
    # own selector, so the per-pod patches below would be one request each for nothing
    if keep_terminal:
        freed = _release_tracked_pods(namespace, name)
        if freed:
            note(f"{name}: released {freed} pods held by the job-tracking finalizer")


def delete_job(namespace: str, name: str):
    """Delete a Job and its pods without the Foreground GC walk (see DELETE_TIMEOUT).

    Suspend first: clearing the pods frees their indexes, and an unsuspended controller
    refills parallelism in the window before the Job delete lands — those replacements
    schedule and bill before the GC reaps them. Dropping the Job before its pods are
    reaped is safe: a Job's pod selector carries its own controller-uid, so a later Job
    of the same name never adopts these.

    Suspending is an optimization, never a precondition: admission re-validates the
    embedded pod template on *any* Job update, so a Job pinned to a ComputeClass that has
    since been deleted rejects its own suspend (400) and could never be torn down. Losing
    the suspend only risks a few replacement pods billing until the delete lands."""
    with tolerate(400, 404, 422):  # rejected by admission, gone, or finished
        set_suspend(namespace, name, True)
    delete_job_pods(namespace, name)
    batch().delete_namespaced_job(name, namespace, propagation_policy="Background")


def _wait_deleted(read, name: str, timeout: int) -> None:
    """Poll until read() 404s; falling through to a create would 409 confusingly."""
    for _ in range(timeout):
        try:
            read()
            time.sleep(1)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise
    raise SystemExit(f"'{name}' is still terminating after {timeout}s; retry shortly")


def recreate_job(namespace: str, spec):
    """Replace any existing Job of the same name, then create it — so a layer can be
    re-submitted (done chunks are then skipped by the per-chunk lock)."""
    name = spec.metadata.name
    b = batch()
    if read_job(namespace, name) is not None:
        note(f"{name}: replacing existing job")
        with tolerate(404):  # deleted under us; nothing left to wait for
            delete_job(namespace, name)
            # the raw read, not read_job: _wait_deleted polls until it *raises* 404, and a
            # None-returning read would spin until DELETE_TIMEOUT and then SystemExit
            _wait_deleted(
                lambda: b.read_namespaced_job(name, namespace), name, DELETE_TIMEOUT
            )
    b.create_namespaced_job(namespace, spec)


def _delete_pod_if_exists(c, namespace, name):
    with tolerate(404):  # never created, or already reaped
        c.delete_namespaced_pod(name, namespace, grace_period_seconds=0)
        _wait_deleted(lambda: c.read_namespaced_pod(name, namespace), name, 30)


def _pod_log_text(c, name, namespace) -> str:
    """Pod log as decoded text. _preload_content=False yields the raw response so the
    client can't deserialize the body with str(bytes) (a "b'...'" repr); normalize
    whatever shape it returns — a response with .data, raw bytes, or already-str."""
    resp = c.read_namespaced_pod_log(name, namespace, _preload_content=False)
    data = getattr(resp, "data", resp)
    return data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)


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
        log = _pod_log_text(c, name, namespace)
        if phase != "Succeeded":
            raise SystemExit(f"{name} {phase}:\n{log}")
        return log
    finally:
        _delete_pod_if_exists(c, namespace, name)


def set_parallelism(namespace: str, name: str, parallelism: int):
    # merge patch of one field (a full V1JobSpec would require `template`)
    batch().patch_namespaced_job(name, namespace, {"spec": {"parallelism": parallelism}})


def set_suspend(namespace: str, name: str, suspend: bool):
    # suspend=True drains a Job to 0 pods (SIGTERM) without deleting it; False resumes
    batch().patch_namespaced_job(name, namespace, {"spec": {"suspend": suspend}})


def resize_pod(namespace: str, name: str, container: str, requests: dict):
    """In-place bump a Running pod's container requests via the /resize subresource — no
    restart. 404 = pod already gone; 422 = resize unsupported (cluster < 1.34.0-gke.2201000)."""
    body = {
        "spec": {"containers": [{"name": container, "resources": {"requests": requests}}]}
    }
    try:
        core().patch_namespaced_pod_resize(name, namespace, body)
    except ApiException as exc:
        if exc.status == 404:
            return
        if exc.status == 422:
            note(
                f"resize unsupported here (needs GKE >= 1.34.0-gke.2201000); {name} kept at old size"
            )
            return
        raise
