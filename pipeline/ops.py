"""Operator actions behind the CLI — everything that mutates the cluster.

cli.py stays lean (argument parsing + read-only presentation); these are plain
functions with no click context, so `deploy --oneshot` composes them directly.
"""

import subprocess
import tempfile
import time

import click
import yaml
from kubernetes.client import ApiException

from . import config, costdb, costs, kube, manifest, note, util

HELM_CHART = "helm"
ONESHOT_POLL_SEC = 30


def deploy_infra(cfg, secrets_dir: str) -> None:
    """helm upgrade --install the static infra, incl. the Secret from secrets_dir."""
    data = kube.secret_data(secrets_dir, cfg.secret_files)
    note(f"deploy: helm release 'pcg' (secrets: {list(data) or 'none'})")
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
        f"deployed; secret '{cfg.secret_name}' <- {list(data)}"
        if data
        else "deployed (no secret)"
    )


def undeploy(cfg) -> None:
    """Tear down everything deploy/submit created: Jobs, dataset ConfigMaps, helm release."""
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


def setup(cfg) -> None:
    """Prepare the graph for the workload: ingest creates the table; migrate preps it."""
    note(f"setup ({cfg.workload})")
    if cfg.workload in ("migrate", "migrate_cleanup"):
        # migrate reads everything from Bigtable; no dataset file involved
        argv = ["python", "-m", "pychunkedgraph.pipeline.migrate.setup", cfg.graph_id]
        note(util.run_pcg(cfg, "setup", argv) or "setup done")
    else:
        argv = ["python", "-m", "pychunkedgraph.pipeline.ingest.setup", cfg.graph_id]
        if cfg.dataset.get("ingest_config", {}).get("AGGLOMERATION"):
            argv.append("--raw")  # an agglomeration source implies the raw input path
        note(util.run_with_dataset(cfg, "setup", argv) or "setup done")
    util.invalidate_layer_counts(cfg)  # graph may have changed; recompute on next read


def mesh_meta(cfg) -> None:
    """Write mesh metadata once (after ingest reaches root); needs `mesh_config:`."""
    argv = ["python", "-m", "pychunkedgraph.pipeline.meshing.setup", cfg.graph_id]
    note("mesh-meta: writing mesh metadata")
    note(util.run_with_dataset(cfg, "mesh-meta", argv) or "mesh metadata written")


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
    require_prev_complete(cfg, layer, force=force)
    existing = _read_job(cfg, layer)
    if existing:  # re-submitting replaces a job; never silently absorb another graph's
        check_graph_owner(cfg, existing, force)
    n = util.read_n(cfg, layer)
    completions = util.ceil_div(n, cfg.job.batch_size)
    pmax = min(cfg.job.ramp.max, completions)
    parallelism = min(cfg.job.ramp.start, pmax)
    spec = manifest.job_spec(cfg, layer, n, completions, parallelism)
    name = spec.metadata.name
    req = spec.spec.template.spec.containers[0].resources.requests
    note(
        f"{name}: {n} chunks, {completions} tasks, workers {parallelism}->{pmax}, "
        f"{req['cpu']} cpu / {req['memory']} per pod"
    )
    kube.recreate_job(cfg.namespace, spec)
    p = parallelism
    while p < pmax:
        time.sleep(cfg.job.ramp.period)
        p = min(p * cfg.job.ramp.factor, pmax)
        kube.set_parallelism(cfg.namespace, name, p)
        note(f"  parallelism -> {p}/{pmax}")
        costdb.sample(cfg)
    costdb.sample(cfg)
    note("at full parallelism; watch with `pipeline status`")
    rate = costs.rate_for(costs.load_table(), cfg.region, cfg.job.compute_class)
    if rate:
        try:
            burn = (
                costs.parse_cpu(req["cpu"]) * rate["cpu_spot"]
                + costs.parse_mem(req["memory"]) * rate["mem_spot"]
            )
            note(
                f"~${burn:.4f}/pod-hr spot; `pipeline costs {layer}` for the running total"
            )
        except Exception:  # noqa: BLE001 - cost is auxiliary, never fatal
            pass


def scale(cfg, layer, parallelism) -> None:
    """Resize the running layer's workers: set its Indexed Job parallelism."""
    name = manifest.job_name(cfg, layer)
    if _read_job(cfg, layer) is None:
        raise SystemExit(f"no job '{name}' in ns '{cfg.namespace}'")
    kube.set_parallelism(cfg.namespace, name, parallelism)
    note(f"{name}: parallelism -> {parallelism}")


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


def oneshot_plan(cfg, yes) -> None:
    """Preview the full run and confirm before any cluster mutation."""
    mesh = "mesh_config" in cfg.dataset
    phase_cfgs = []
    for workload in ["ingest"] + (["meshing"] if mesh else []):
        c = config.load(cfg.source, workload=workload)
        c.graph_id = cfg.graph_id
        phase_cfgs.append(c)
    note(
        f"oneshot: graph '{cfg.graph_id}' ({cfg.source}): "
        f"setup -> ingest L2..root{' -> mesh-meta -> meshing' if mesh else ''}"
    )
    if not mesh:
        note("  (no mesh_config in the dataset; meshing will be skipped)")
    cached = util.cached_layer_counts(cfg)
    if cached:
        for c in phase_cfgs:  # validate each phase's curve with ITS merged job config
            top = max(cached)
            if c.workload == "meshing":
                top = min(int(cfg.dataset["mesh_config"]["max_layer"]), top)
            note(f"  {c.workload}:")
            for layer in sorted(layer for layer in cached if layer <= top):
                req = manifest.layer_requests(c.job, layer)
                note(
                    f"    L{layer}: {cached[layer]} chunks · "
                    f"{req['cpu']} cpu / {req['memory']} per pod"
                )
    else:
        note("  layer counts are computed during setup")
    if not yes:
        click.confirm("proceed?", abort=True)


def oneshot_run(cfg) -> None:
    """setup -> ingest L2..root -> mesh-meta -> meshing; resumable at every step."""
    ingest_cfg = config.load(cfg.source, workload="ingest")
    ingest_cfg.graph_id = cfg.graph_id  # carry a -g override
    if ingest_cfg.persistent_util:
        kube.util_pod(ingest_cfg.namespace, wait_create=True)  # helm just created it
    try:
        counts = util.read_layer_counts(ingest_cfg)
        note("graph exists; skipping setup")
    except SystemExit as exc:
        if "not readable" not in str(exc):
            raise  # infra failure — never guess; setup is not re-runnable
        setup(ingest_cfg)
        counts = util.read_layer_counts(ingest_cfg)
    for layer in sorted(counts):
        run_layer(ingest_cfg, layer)
    if "mesh_config" not in cfg.dataset:
        note("oneshot complete (no meshing)")
        return
    mesh_cfg = config.load(cfg.source, workload="meshing")
    mesh_cfg.graph_id = cfg.graph_id
    mesh_meta(mesh_cfg)
    top = min(int(cfg.dataset["mesh_config"]["max_layer"]), max(counts))
    for layer in range(2, top + 1):
        run_layer(mesh_cfg, layer)
    note("oneshot complete")


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
        costdb.sample(cfg)  # pod runtimes are durable once recorded
        job = _read_job(cfg, layer)
        if job is None:
            raise SystemExit(
                f"L{layer} ({cfg.workload}) job disappeared mid-run; "
                f"re-run `pipeline deploy --oneshot` to resume"
            )
        p = util.job_progress(job)
        note(
            f"  L{layer} {cfg.workload}: {p['done']}/{p['total']} {p['pct']}% · "
            f"active {p['active']} · retries {p['retries']} · dead {p['dead']}"
        )
        if p["dead"] or p["state"] == "failed":
            raise SystemExit(
                f"L{layer} ({cfg.workload}) has dead tasks — `pipeline inspect {layer}`; "
                f"re-run `pipeline deploy --oneshot` to resume (finished layers skip)"
            )
        if p["state"] == "complete":
            note(f"L{layer} ({cfg.workload}) complete")
            return
        time.sleep(ONESHOT_POLL_SEC)
