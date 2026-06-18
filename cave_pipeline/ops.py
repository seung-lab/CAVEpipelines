"""Operator actions behind the CLI — everything that mutates the cluster.

cli.py stays lean (argument parsing + read-only presentation); these are plain
functions with no click context, so `deploy --oneshot` composes them directly.
"""

import base64
import concurrent.futures
import contextlib
import graphlib
import os
import shutil
import subprocess
import tempfile
import threading
import time

import click
import yaml
from kubernetes.client import ApiException

from . import config, costs, kube, manifest, note, stages, util
from .db import cost, state

HELM_CHART = "helm"
ONESHOT_POLL_SEC = 30
OOM_POLL_SEC = 30


class Paused(Exception):
    """The run was suspended (`pipeline pause`); the driver stops without failing a stage."""


class Undeployed(Exception):
    """The run's state was cleared (`undeploy`/`purge`) under a live driver; stop, don't fail."""


# meshing's graphene CloudVolume demands a cave-secret.json at construction but never calls
# the graph server, so a placeholder token satisfies it when the operator provides no real one.
_CAVE_SECRET = "cave-secret.json"
_PLACEHOLDER_CAVE_SECRET = b'{"token": "placeholder"}'


def _with_cave_placeholder(data: dict) -> dict:
    """Add a placeholder cave-secret.json when none is provided; meshing needs the file
    present, not a real token (it reads/writes only the bucket, never the graph server)."""
    if _CAVE_SECRET in data:
        return data
    note(
        f"no {_CAVE_SECRET} provided; mounting a placeholder "
        f"(meshing needs the file, not a real token)"
    )
    return {**data, _CAVE_SECRET: base64.b64encode(_PLACEHOLDER_CAVE_SECRET).decode()}


def deploy_infra(cfg, secrets_dir: str) -> None:
    """helm upgrade --install the static infra, incl. the Secret from secrets_dir."""
    if not shutil.which("helm"):
        raise SystemExit("helm not found on PATH; install helm to deploy")
    if not os.path.isdir(secrets_dir):
        raise SystemExit(f"secrets dir not found: {secrets_dir}")
    data = _with_cave_placeholder(kube.secret_data(secrets_dir, cfg.secret_files))
    mounted = (
        ", ".join(f"{src} -> {mnt}" for mnt, src in cfg.secret_files.items()) or "none"
    )
    note(f"deploy: helm release 'pcg' (secrets from {secrets_dir}: {mounted})")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
        yaml.safe_dump(manifest.helm_values(cfg, data), f)
        f.flush()
        res = subprocess.run(
            [
                "helm",
                "upgrade",
                "--install",
                "pcg",
                HELM_CHART,
                "-n",
                cfg.namespace,
                "--create-namespace",
                "-f",
                f.name,
            ],
            capture_output=True,  # swallow helm's NOTES + autopilot warnings
            text=True,
        )
        if res.returncode:
            raise SystemExit(
                f"helm upgrade failed (exit {res.returncode}):\n{res.stderr.strip()}"
            )
    note(
        f"deployed; secret '{cfg.secret_name}' <- {mounted}"
        if data
        else "deployed (no secret)"
    )


def register_cave(cfg, secrets_dir) -> None:
    """Register the graph with CAVE sticky_auth when cave_config is set — a one-shot
    POST on deploy, not a build-DAG stage; token comes from the deploy secrets dir."""
    stage = stages.CaveRegister()
    if stage.applies(cfg):
        stage.setup(cfg, secrets_dir)


def undeploy(cfg) -> None:
    """Tear down everything deploy/submit created: Jobs, dataset ConfigMaps, helm release,
    the local layer-counts cache, and the run state. The cost db and terraform-managed infra
    (cluster, identities, Bigtable) are untouched."""
    note("undeploy: deleting jobs, dataset configmaps + helm release")
    for job in kube.list_jobs(cfg.namespace):
        kube.delete_job(cfg.namespace, job.metadata.name)
        note(f"deleted job {job.metadata.name}")
    for cm in kube.list_configmaps(cfg.namespace, "pipeline=dataset"):
        kube.delete_configmap(cfg.namespace, cm.metadata.name)
        note(f"deleted dataset configmap {cm.metadata.name}")
    res = subprocess.run(
        ["helm", "uninstall", "pcg", "-n", cfg.namespace], capture_output=True, text=True
    )
    if res.returncode and "not found" not in (res.stderr or "").lower():
        raise SystemExit(
            f"helm uninstall failed (exit {res.returncode}):\n{res.stderr.strip()}"
        )
    note(res.stdout.strip() or res.stderr.strip() or "release removed")
    # derived temp state; stale counts/run rows would otherwise phantom-fill `status`
    util.invalidate_layer_counts(cfg)
    state.clear(cfg)
    note("cleared local layer-counts cache + run state")


def setup(cfg, exist_ok=False) -> None:
    """Run the configured workload's setup (dispatched per stage)."""
    stages.STAGES[cfg.workload].setup(cfg, exist_ok=exist_ok)


def top_layer(cfg, counts) -> int:
    """Highest layer to run for the configured workload (per stage)."""
    return stages.STAGES[cfg.workload].top_layer(cfg, counts)


def mesh_meta(cfg) -> None:
    """Write mesh metadata once (after ingest reaches root); needs `mesh_config:`."""
    stages.STAGES["meshing"].setup(cfg)


def _read_job(cfg, layer):
    try:
        return kube.batch().read_namespaced_job(
            manifest.job_name(cfg, layer), cfg.namespace
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def _is_sample(job) -> bool:
    return bool((job.metadata.annotations or {}).get("sample"))


def check_graph_owner(cfg, job, force=False) -> None:
    """A job left by another graph makes layer-state checks meaningless — never mix."""
    owner = (job.metadata.labels or {}).get("graph", "")
    if owner and owner != cfg.graph_id and not force:
        raise SystemExit(
            f"job {job.metadata.name} belongs to graph '{owner}', not "
            f"'{cfg.graph_id}' — delete it first (--force only if you're sure)"
        )


def require_prev_complete(cfg, layer, force=False) -> None:
    """Refuse to submit a layer until the one below it is 100% complete."""
    if layer <= 2 or force:
        return
    job = _read_job(cfg, layer - 1)
    if job is None:
        raise SystemExit(
            f"layer {layer - 1} has no job; submit it first "
            f"(--force only if you're sure it's already done)"
        )
    check_graph_owner(cfg, job, force)
    if _is_sample(job):  # a tiny sizing run must never satisfy the gate
        raise SystemExit(f"layer {layer - 1} only has a sample run; submit it first")
    if util.job_state(job) != "complete":
        raise SystemExit(
            f"layer {layer - 1} is not complete; finish it first "
            f"(--force only if you're sure)"
        )


def submit(cfg, layer, force=False) -> None:
    """Create one layer's Indexed Job (completions from cg.meta) and ramp parallelism."""
    note(f"submit L{layer} ({cfg.workload})")
    if cfg.workload == "meshing" and layer == 2 and not util.mesh_meta_written(cfg):
        # without it workers silently default mip=0; only need to check at the leaf layer
        raise SystemExit("meshing has no mesh metadata; run `pipeline mesh-meta` first")
    require_prev_complete(cfg, layer, force=force)
    existing = _read_job(cfg, layer)
    if existing:  # re-submitting replaces a job; never silently absorb another graph's
        check_graph_owner(cfg, existing, force)
    n = util.read_n(cfg, layer)
    batch = manifest.batch_for(cfg.job, layer)
    completions = util.ceil_div(n, batch)
    pmax = manifest.max_parallelism(cfg.job.ramp.max, completions)
    parallelism = min(cfg.job.ramp.start, pmax)
    run = state.get_run(cfg)  # tag this Job's cost rows with the active deploy run
    spec = manifest.job_spec(
        cfg, layer, n, completions, parallelism, run_id=run.run_id if run else ""
    )
    name = spec.metadata.name
    req = spec.spec.template.spec.containers[0].resources.requests
    vcpu = costs.parse_cpu(req["cpu"])  # millicores/bytes -> readable cores / GiB
    gib = costs.parse_mem(req["memory"])
    spot_note = ""
    rate = costs.rate_for(costs.load_table(), cfg.region, cfg.job.compute_class)
    if rate:
        with contextlib.suppress(Exception):  # cost is auxiliary, never fatal
            burn = vcpu * rate["cpu_spot"] + gib * rate["mem_spot"]
            spot_note = f" | ~${burn:.4f}/pod-hr spot"
    note(
        f"{name}: {n} chunks, batch {batch}, {completions} tasks | "
        f"workers {parallelism}->{pmax} | {vcpu:g} cpu, {gib:g}Gi per pod{spot_note}"
    )
    kube.recreate_job(cfg.namespace, spec)
    full = "at full parallelism; watch with `pipeline status`"
    p = parallelism
    while p < pmax:
        time.sleep(cfg.job.ramp.period)
        p = min(p * cfg.job.ramp.factor, pmax)
        kube.set_parallelism(cfg.namespace, name, p)
        tail = f" ({full})" if p >= pmax else ""  # the step that maxes carries it inline
        note(f"  parallelism -> {p}/{pmax}{tail}")
        cost.sample(cfg)
    cost.sample(cfg)
    if parallelism >= pmax:  # already maxed: no ramp line to carry the note
        note(full)


def scale(cfg, layer, parallelism) -> None:
    """Resize the running layer's workers: set its Indexed Job parallelism."""
    name = manifest.job_name(cfg, layer)
    if _read_job(cfg, layer) is None:
        raise SystemExit(f"no job '{name}' in ns '{cfg.namespace}'")
    kube.set_parallelism(cfg.namespace, name, parallelism)
    note(f"{name}: parallelism -> {parallelism}")


def _requests_differ(pod, container: str, desired: dict) -> bool:
    """True if the pod's container requests don't match `desired` — numeric, so the
    canonical '2'/'8Gi' a pod stores compares equal to layer_requests' '2000m'/'8192Mi'."""
    spec = next((c for c in pod.spec.containers if c.name == container), None)
    current = (spec.resources.requests if spec and spec.resources else None) or {}
    return costs.parse_cpu(current.get("cpu")) != costs.parse_cpu(
        desired["cpu"]
    ) or costs.parse_mem(current.get("memory")) != costs.parse_mem(desired["memory"])


def reconcile(cfg) -> None:
    """Apply pipeline.yml edits to running layers: resources (in-place pod resize) and
    ramp.max (parallelism). Strict — any other immutable field that drifts from a running
    Job skips that layer untouched; revert the yml or resubmit to change it."""
    for job in kube.list_jobs(cfg.namespace):
        if util.job_state(job) == "complete" or _is_sample(job) or job.spec.suspend:
            continue
        check_graph_owner(cfg, job)
        layer = int(job.metadata.labels["layer"])
        drift = manifest.immutable_drift(cfg, layer, job)
        if drift:
            field, running, desired = drift[0]
            note(
                f"L{layer}: immutable {field} differs (running={running}, yml={desired}); "
                f"skipping — revert the yml or resubmit"
            )
            continue
        try:  # an out-of-Autopilot-range edit invalidates the whole layer, not just resize
            desired_req = manifest.layer_requests(cfg.job, layer)
        except SystemExit as exc:
            note(f"L{layer}: {exc}; skipping")
            continue
        name = job.metadata.name
        pmax = manifest.max_parallelism(cfg.job.ramp.max, job.spec.completions)
        if job.spec.parallelism != pmax:
            kube.set_parallelism(cfg.namespace, name, pmax)
            note(f"{name}: parallelism -> {pmax}")
        container = cfg.workload.replace("_", "-")
        running_pods = [
            p for p in kube.pods_of(cfg.namespace, name) if p.status.phase == "Running"
        ]
        resized = 0
        for pod in running_pods:
            if _requests_differ(pod, container, desired_req):
                kube.resize_pod(cfg.namespace, pod.metadata.name, container, desired_req)
                resized += 1
        if running_pods:
            note(f"L{layer}: resized {resized}/{len(running_pods)} Running pods")


def sample(cfg, layer, count) -> None:
    """Run `count` scattered chunks of the layer (one per pod) to size CPU/memory."""
    existing = _read_job(cfg, layer)
    if existing:
        check_graph_owner(cfg, existing)
        if not _is_sample(existing):  # never destroy a real layer run for sizing
            raise SystemExit(
                f"layer {layer} already has a real job; `pipeline delete {layer}` first"
            )
    spec = manifest.job_spec(
        cfg, layer, count, count, min(count, cfg.job.ramp.max), batch_size=1
    )
    spec.metadata.annotations["sample"] = "true"  # must never satisfy the layer gate
    name = spec.metadata.name
    note(f"{name}: launching {count} sample chunks; size with `pipeline top {layer}`")
    kube.recreate_job(cfg.namespace, spec)


def delete(cfg, layer) -> None:
    """Delete the layer's Job and its pods."""
    name = manifest.job_name(cfg, layer)
    try:
        kube.delete_job(cfg.namespace, name)
    except ApiException as exc:
        if exc.status == 404:
            note(f"no job '{name}' in ns '{cfg.namespace}'")
            return
        raise
    note(f"deleting {name}")


def _phase_cfg(cfg, workload):
    """A config loaded for `workload` (per-workload job merge) carrying the -g override."""
    c = config.load(cfg.source, workload=workload)
    c.graph_id = cfg.graph_id
    return c


def _layer_plan(cfg, cached, top) -> None:
    for layer in sorted(layer for layer in cached if layer <= top):
        req = manifest.layer_requests(cfg.job, layer)
        vcpu = costs.parse_cpu(req["cpu"])
        gib = costs.parse_mem(req["memory"])
        note(f"    L{layer}: {cached[layer]} chunks | {vcpu:g} cpu, {gib:g}Gi per pod")


def orchestrate(cfg, run_set, parallel=True) -> None:
    """Run `run_set`; the DAG only orders it (independents run in parallel unless `parallel`
    is False). Deps outside run_set are assumed satisfied — the operator owns the selection;
    a truly-missing upstream surfaces as that stage's own worker failure."""
    if cfg.persistent_util:
        kube.util_pod(cfg.namespace, wait_create=True)  # helm just created it
    ts = graphlib.TopologicalSorter({w: stages.STAGES[w].deps & run_set for w in run_set})
    ts.prepare()  # a dependency cycle fails loud here
    while ts.is_active():
        ready = list(ts.get_ready())
        _run_ready(cfg, ready, parallel)
        ts.done(*ready)  # only on success -> a failed batch halts downstream stages
    note("orchestrate: all workloads complete")


def _watch_oom(cfg, stop) -> None:
    """Background heads-up: on a fresh OOMKilling event, warn once per active Job of the
    current run that its layer is out of memory. Node-level events carry no run, so we
    scope via the Job's run-id annotation. Best-effort — never disrupts the driver."""
    warned, seen = set(), set()
    with contextlib.suppress(Exception, SystemExit):  # OOMs already on-cluster aren't ours
        seen = {e.metadata.uid for e in kube.oom_events(cfg.namespace)}
    while not stop.wait(OOM_POLL_SEC):
        with contextlib.suppress(Exception, SystemExit):  # a hiccup never kills the watcher
            fresh = [e for e in kube.oom_events(cfg.namespace) if e.metadata.uid not in seen]
            seen.update(e.metadata.uid for e in fresh)
            if not fresh:
                continue
            run = state.get_run(cfg)
            run_id = run.run_id if run else ""
            for job in kube.list_jobs(cfg.namespace):
                anns = job.metadata.annotations or {}
                if (job.status.active or 0) == 0 or anns.get("run-id") != run_id:
                    continue
                if job.metadata.uid not in warned:
                    warned.add(job.metadata.uid)
                    layer = (job.metadata.labels or {}).get("layer", "?")
                    note(f"layer {layer}: OOMKilling — raise its job.resources.memory, re-submit")


def _clear_suspend(cfg) -> None:
    """Unsuspend every suspended Job of the run and mark it running. The driver converges
    from any prior pause, so deploy and resume are one 'make progress' action."""
    cleared = 0
    for job in kube.list_jobs(cfg.namespace):
        if job.spec.suspend:
            kube.set_suspend(cfg.namespace, job.metadata.name, False)
            cleared += 1
    state.set_run_status(cfg, state.RUNNING)
    if cleared:
        note(f"resumed: {cleared} suspended job(s) unsuspended")


def _confirm_resume(exc) -> bool:
    """After a self-pause, let an attended operator fix the cause and continue in place,
    instead of exiting and re-running by hand."""
    note(f"suspended after failure: {exc}")
    return click.confirm("fix the cause, then resume now?", default=False)


def drive(cfg, interactive=False) -> None:
    """Drive the recorded run to completion, converging from any prior state: a leftover
    suspend is cleared on entry (so deploy and resume both just continue), done layers
    skip, failed layers re-submit. An external pause or undeploy exits cleanly; a failure
    self-suspends so no Jobs burn, then — when attended — offers to resume in place."""
    run = state.get_run(cfg)
    if run is None:
        raise SystemExit("no run recorded; deploy --oneshot or --all-layers first")
    state.set_run_pid(cfg, os.getpid())
    _clear_suspend(cfg)  # heal any prior pause; the operator never picks deploy vs resume
    stop = threading.Event()
    threading.Thread(target=_watch_oom, args=(cfg, stop), daemon=True).start()
    try:
        while True:
            try:
                orchestrate(cfg, run.stage_set, run.parallel)
                break
            except Paused:
                note("paused; `pipeline resume` to continue")  # external pause: respect it
                return
            except Undeployed:
                note("run undeployed; exiting")  # state + jobs already gone
                return
            except KeyboardInterrupt:
                pause(cfg)  # Ctrl-C: stop the burn, don't nag to resume
                raise
            except BaseException as exc:  # noqa: BLE001 - any failure self-pauses
                pause(cfg)  # suspend so no Jobs burn while the operator looks
                if not (interactive and _confirm_resume(exc)):
                    raise
                _clear_suspend(cfg)  # fixed in place; converge and drive on
    finally:
        stop.set()
    state.finish_run(cfg)


def pause(cfg) -> None:
    """Suspend every non-complete pipeline Job (0 resources, nothing deleted) + mark paused."""
    # record intent first so a partial suspend still leaves the run marked paused
    state.set_run_status(cfg, state.PAUSED)
    for job in kube.list_jobs(cfg.namespace):
        if util.job_state(job) != "complete":
            kube.set_suspend(cfg.namespace, job.metadata.name, True)
    note("paused: jobs suspended (0 resources); `pipeline resume` to continue")


def resume(cfg) -> None:
    """Unsuspend a paused (or stalled) run and continue driving where it paused — the same
    'make progress' action as deploy; drive clears the suspend and converges from any state."""
    run = state.get_run(cfg)
    if run is None:
        raise SystemExit("no run to resume; `pipeline deploy` first")
    if run.status == state.DONE:
        raise SystemExit("run already complete; nothing to resume")
    if run.status == state.RUNNING and util.pid_alive(run.pid):
        raise SystemExit(
            f"a driver (pid {run.pid}) is already running; `pipeline pause` first"
        )
    drive(cfg, interactive=True)


def _run_ready(cfg, ready, parallel) -> None:
    """Run a batch of ready workloads — concurrently when parallel, else serially."""
    cfgs = {w: _phase_cfg(cfg, w) for w in ready}
    if not parallel or len(ready) == 1:
        for w in ready:
            run_workload(cfgs[w])
        return
    util.read_layer_counts(cfgs[ready[0]])  # warm the shared cache before threads read it
    errors, paused, undeployed = {}, False, False
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(ready))
    try:
        futs = {ex.submit(run_workload, cfgs[w]): w for w in ready}
        for fut in concurrent.futures.as_completed(futs):
            try:
                fut.result()
            except KeyboardInterrupt:
                raise
            except Paused:
                paused = True  # pause suspends every Job; siblings stop the same way
            except Undeployed:
                undeployed = (
                    True  # undeploy tore down the run; siblings stop the same way
                )
            except SystemExit as exc:
                errors[futs[fut]] = str(exc)
            except Exception as exc:  # noqa: BLE001 - report every failure, not the first
                errors[futs[fut]] = repr(exc)
    except KeyboardInterrupt:
        ex.shutdown(
            wait=False, cancel_futures=True
        )  # Jobs keep running; re-run to resume
        raise
    finally:
        ex.shutdown(wait=False)
    if undeployed:  # a teardown supersedes pause/failure reporting — the run is gone
        raise Undeployed()
    if paused:
        if errors:  # don't let the pause hide a real failure that re-runs on resume
            note(f"paused; also failed (re-run on resume): {', '.join(sorted(errors))}")
        raise Paused()
    if errors:
        detail = "; ".join(f"{w}: {m}" for w, m in sorted(errors.items()))
        raise SystemExit(f"workload(s) failed: {detail} — fix and re-run to resume")


def run_workload(cfg_w) -> None:
    """Run every layer of one workload, with its setup; record its lifecycle state."""
    state.set_state(cfg_w, cfg_w.workload, state.RUNNING)
    try:
        setup(cfg_w, exist_ok=True)  # forgiveness: a fresh graph creates, a resume skips
        counts = util.read_layer_counts(cfg_w)
        top = top_layer(cfg_w, counts)
        _layer_plan(cfg_w, counts, top)  # every layer's requests + clamps up front, not at L7
        for layer in range(2, top + 1):
            run_layer(cfg_w, layer)
    except (KeyboardInterrupt, Paused, Undeployed):
        raise  # don't fail the stage: a re-run resumes a pause; undeploy already cleared state
    except BaseException:
        state.set_state(cfg_w, cfg_w.workload, state.FAILED)
        raise
    state.set_state(cfg_w, cfg_w.workload, state.COMPLETE)
    note(f"all layers complete ({cfg_w.workload})")


def dag_levels(run_set) -> list:
    """Topological depth levels of run_set, e.g. [['ingest'], ['l2cache', 'meshing']]."""
    ts = graphlib.TopologicalSorter({w: stages.STAGES[w].deps & run_set for w in run_set})
    ts.prepare()
    levels = []
    while ts.is_active():
        batch = sorted(ts.get_ready())
        levels.append(batch)
        ts.done(*batch)
    return levels


def select_range(cfg, start, end, yes) -> set:
    """The operator's start..end depth of the build DAG -> the run set. Displays the DAG and
    prompts for any unset depth (unless `yes`, which defaults to the full top->bottom range)."""
    levels = dag_levels(stages.build_set(cfg))
    last = len(levels) - 1
    if not yes and (start is None or end is None):
        note(f"build DAG for graph '{cfg.graph_id}':")
        for depth, batch in enumerate(levels):
            note(f"  {depth}  {', '.join(batch)}")
        if start is None:
            start = click.prompt("start depth", default=0, type=int)
        if end is None:
            end = click.prompt("end depth", default=last, type=int)
    start, end = (0 if start is None else start), (last if end is None else end)
    if not 0 <= start <= end <= last:
        raise SystemExit(f"depth range {start}..{end} is outside 0..{last}")
    return {w for batch in levels[start : end + 1] for w in batch}


def confirm_run(cfg, run_set, parallel, yes) -> None:
    """Validate the selected stages are configured, preview the run, and confirm — all
    before any mutation."""
    for w in sorted(run_set):
        if not stages.STAGES[w].applies(cfg):
            raise SystemExit(
                f"stage '{w}' is selected but not configured in the dataset "
                f"(meshing needs `mesh_config`, l2cache needs `l2cache_config`)"
            )
    note(f"run: graph '{cfg.graph_id}' ({cfg.source}) -> {sorted(run_set)}")
    cached = util.cached_layer_counts(cfg)
    for batch in dag_levels(run_set):
        tag = " (parallel)" if parallel and len(batch) > 1 else ""
        note(f"  {' + '.join(batch)}{tag}")
        for w in batch:
            if cached:
                c = _phase_cfg(cfg, w)
                _layer_plan(c, cached, top_layer(c, cached))
    if not cached:
        note("  layer counts are computed during setup")
    if not yes:
        click.confirm("proceed?", abort=True)


def run_layer(cfg, layer) -> None:
    """Submit (or attach to) one layer and poll it to completion; stop on dead tasks."""
    job = _read_job(cfg, layer)
    if job:
        check_graph_owner(cfg, job)
        if util.job_state(job) == "complete" and not _is_sample(job):
            note(f"L{layer} ({cfg.workload}) already complete; skipping")
            return
    if job and util.job_state(job) == "running" and not _is_sample(job):
        note(f"L{layer} ({cfg.workload}) already running; attaching")
    else:  # absent, failed, or a leftover sample run -> (re)submit replaces it
        submit(cfg, layer)
    while True:
        if state.get_run(cfg) is None:  # undeploy/purge cleared the run out from under us
            raise Undeployed(f"L{layer} ({cfg.workload}) run undeployed; stopping")
        cost.sample(cfg)  # pod runtimes are durable once recorded
        job = _read_job(cfg, layer)
        if job is None:
            raise SystemExit(
                f"L{layer} ({cfg.workload}) job disappeared mid-run; "
                f"re-run `pipeline deploy --oneshot` to resume"
            )
        # `pipeline pause` drained it to 0 pods; stop, don't poll a suspended job forever
        if job.spec.suspend:
            raise Paused(f"L{layer} ({cfg.workload}) suspended")
        p = util.job_progress(
            job
        )  # `pipeline status` covers live progress; stay quiet here
        if p["dead"] or p["state"] == "failed":
            raise SystemExit(
                f"L{layer} ({cfg.workload}) has dead tasks — `pipeline inspect {layer}`; "
                f"re-run `pipeline deploy --oneshot` to resume (finished layers skip)"
            )
        if p["state"] == "complete":
            note(f"L{layer} ({cfg.workload}) complete")
            return
        time.sleep(ONESHOT_POLL_SEC)
