"""`pipeline` — operator CLI for the GKE Autopilot chunk-batch pipelines.

Everything configurable lives in pipeline.yml (one graph, one workload at a time),
so commands carry only a layer. This drives the helm static infra and the per-layer
Indexed Jobs. Layers are operator-gated: submit one, watch it Complete, submit the
next (a layer's writes are non-idempotent).
"""

import functools
import logging
import subprocess
import tempfile
import time

import click
import urllib3
import yaml
from kubernetes.client import ApiException
from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import NOTE, config, costs, kube, log, manifest, note, util

HELM_CHART = "helm"


@click.group(help=__doc__, context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-c",
    "--config",
    "config_name",
    default="config",
    help="config dir with pipeline.yml + dataset.yml (default: config)",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="debug logging, incl. every kubernetes API request",
)
@click.pass_context
def cli(ctx, config_name, verbose):
    # Root stays at NOTE so -v doesn't unleash urllib3/kubernetes HTTP body dumps
    # (unreadable multi-KB single lines); -v only deepens our own logger.
    logging.basicConfig(level=NOTE, format="%(message)s")
    if verbose:
        log.setLevel(logging.DEBUG)
    ctx.obj = config_name  # loaded lazily by pass_cfg, so --help needs no config


def pass_cfg(fn):
    """Pass the loaded Config as the handler's first argument.

    Loading is lazy (post --help) and cached on ctx.obj; tests inject a prebuilt
    Config via CliRunner(...).invoke(command, obj=cfg)."""

    @click.pass_context
    def wrap(ctx, *args, **kwargs):
        if not isinstance(ctx.obj, config.Config):
            ctx.obj = config.load(ctx.obj or "config")
        return ctx.invoke(fn, ctx.obj, *args, **kwargs)

    return functools.update_wrapper(wrap, fn)


_LAYER = click.argument("layer", type=int)


@cli.command(help="install/upgrade static infra (helm) + secret")
@click.option(
    "-s", "--secrets", default="secrets", help="dir of secret files (default: secrets)"
)
@click.option(
    "--setup",
    "run_setup",
    is_flag=True,
    help="run `setup` after deploy (first-run convenience)",
)
@click.option("-r", "--raw", is_flag=True, help="raw agglomeration input (with --setup)")
@click.option(
    "--submit-l2", is_flag=True, help="submit layer 2 after setup (requires --setup)"
)
@pass_cfg
def deploy(cfg, secrets, run_setup, raw, submit_l2):
    """helm upgrade --install the static infra, incl. the Secret built from ./secrets."""
    if submit_l2 and not run_setup:
        raise SystemExit("--submit-l2 requires --setup")
    data = kube.secret_data(secrets, cfg.secret_files)
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
    if run_setup:
        ctx = click.get_current_context()
        ctx.invoke(setup, raw=raw, wait_create=True)  # util pod still spinning up
        if submit_l2:
            ctx.invoke(submit, layer=2)
        else:
            note("pipeline ready; run `pipeline submit <layer>`")


@cli.command(help="delete all pipeline Jobs and the helm release (incl. secret)")
@pass_cfg
def undeploy(cfg):
    """Tear down everything deploy/submit created: all pipeline Jobs, then the helm release."""
    note("undeploy: deleting jobs + helm release")
    for job in kube.list_jobs(cfg.namespace):
        kube.delete_job(cfg.namespace, job.metadata.name)
        note(f"deleted job {job.metadata.name}")
    res = subprocess.run(
        ["helm", "uninstall", "pcg", "-n", cfg.namespace], capture_output=True, text=True
    )
    note(res.stdout.strip() or res.stderr.strip())


@cli.command(help="create the graph table + meta (runs in the util pod)")
@click.option("-r", "--raw", is_flag=True, help="raw agglomeration input")
@pass_cfg
def setup(cfg, raw, wait_create=False):
    """Prepare the graph for the workload: ingest creates the table; migrate preps it."""
    if cfg.workload in ("migrate", "migrate_cleanup"):
        argv = ["python", "-m", "pychunkedgraph.pipeline.migrate.setup", cfg.graph_id]
    else:
        argv = ["python", "-m", "pychunkedgraph.pipeline.ingest.setup", cfg.graph_id]
        if raw:
            argv.append("--raw")
    note(f"setup ({cfg.workload})")
    note(util.run_pcg(cfg, "setup", argv, wait_create=wait_create) or "setup done")
    util.invalidate_layer_counts(cfg)  # graph may have changed; recompute on next read


@cli.command(
    "mesh-meta", help="write mesh metadata once (after ingest reaches the root layer)"
)
@pass_cfg
def mesh_meta(cfg):
    """Write mesh metadata once (after ingest reaches root); needs `mesh_config:` in the dataset."""
    argv = ["python", "-m", "pychunkedgraph.pipeline.meshing.setup", cfg.graph_id]
    note("mesh-meta: writing mesh metadata")
    note(util.run_pcg(cfg, "mesh-meta", argv) or "mesh metadata written")


def _require_prev_complete(cfg, layer, force=False):
    """Refuse to submit a layer until the one below it is 100% complete (--force overrides)."""
    if layer <= 2 or force:
        return
    prev = manifest.job_name(cfg, layer - 1)
    try:
        job = kube.batch().read_namespaced_job(prev, cfg.namespace)
    except ApiException as exc:
        if exc.status == 404:
            raise SystemExit(
                f"layer {layer - 1} has no job; submit it first "
                f"(--force only if you're sure it's already done)"
            )
        raise
    if util.job_state(job) != "complete":
        raise SystemExit(
            f"layer {layer - 1} is not complete; finish it first "
            f"(--force only if you're sure)"
        )


@cli.command(help="submit one layer's Indexed Job and ramp parallelism")
@_LAYER
@click.option(
    "-f",
    "--force",
    is_flag=True,
    help="submit even if the layer below isn't complete — only if you're sure",
)
@pass_cfg
def submit(cfg, layer, force=False):
    """Create one layer's Indexed Job (completions from cg.meta) and ramp parallelism."""
    note(f"submit L{layer} ({cfg.workload})")
    _require_prev_complete(cfg, layer, force=force)
    n = util.read_n(cfg, layer)
    completions = util.ceil_div(n, cfg.job.batch_size)
    pmax = min(cfg.ramp.max, completions)
    parallelism = min(cfg.ramp.start, pmax)
    spec = manifest.job_spec(cfg, layer, n, completions, parallelism)
    name = spec.metadata.name
    note(f"{name}: {n} chunks, {completions} tasks, workers {parallelism}->{pmax}")
    kube.recreate_job(cfg.namespace, spec)
    p = parallelism
    while p < pmax:
        time.sleep(cfg.ramp.period)
        p = min(p * cfg.ramp.factor, pmax)
        kube.set_parallelism(cfg.namespace, name, p)
        note(f"  parallelism -> {p}/{pmax}")
    note("at full parallelism; watch with `pipeline status`")
    rate = costs.rate_for(costs.load_table(), cfg.region, cfg.job.compute_class)
    if rate:
        try:
            burn = (
                costs.parse_cpu(cfg.job.cpu) * rate["cpu_spot"]
                + costs.parse_mem(cfg.job.memory) * rate["mem_spot"]
            )
            note(
                f"~${burn:.4f}/pod-hr spot; `pipeline costs {layer}` for the running total"
            )
        except Exception:  # noqa: BLE001 - cost is auxiliary, never fatal
            pass


@cli.command(help="resize the running layer's workers (set Job parallelism)")
@_LAYER
@click.argument("parallelism", type=int)
@pass_cfg
def scale(cfg, layer, parallelism):
    """Resize the running layer's workers: set its Indexed Job parallelism."""
    name = manifest.job_name(cfg, layer)
    kube.set_parallelism(cfg.namespace, name, parallelism)
    note(f"{name}: parallelism -> {parallelism}")


@cli.command(help="run N scattered chunks of a layer to size CPU/memory")
@_LAYER
@click.argument("count", type=int)
@pass_cfg
def sample(cfg, layer, count):
    """Run `count` scattered chunks of the layer (one per pod) to size CPU/memory."""
    spec = manifest.job_spec(
        cfg, layer, count, count, min(count, cfg.ramp.max), batch_size=1
    )
    name = spec.metadata.name
    note(f"{name}: launching {count} sample chunks; size with `pipeline top {layer}`")
    kube.recreate_job(cfg.namespace, spec)


@cli.command(help="list a layer's failed indexes; add an index for its pod log")
@_LAYER
@click.argument("index", type=int, required=False)
@pass_cfg
def inspect(cfg, layer, index):
    """List a layer's failed indexes; with an index, show that index's pod log."""
    name = manifest.job_name(cfg, layer)
    if index is None:
        s = kube.batch().read_namespaced_job(name, cfg.namespace).status
        note(
            f"{name}: {s.succeeded or 0} ok, {s.active or 0} active, "
            f"{s.failed or 0} failed pod attempts"
        )
        failed_idx = getattr(s, "failed_indexes", None)
        if failed_idx:
            note(f"permanently-failed indexes: {failed_idx}")
            note(f"`pipeline inspect {layer} <index>` for a failed index's log")
        else:
            note(
                "no permanently-failed indexes (failed attempts retried + recovered); "
                f"`pipeline events {layer}` shows preemptions/retries"
            )
        return
    pods_ = (
        kube.core()
        .list_namespaced_pod(
            cfg.namespace,
            label_selector=f"batch.kubernetes.io/job-name={name},"
            f"batch.kubernetes.io/job-completion-index={index}",
        )
        .items
    )
    if not pods_:
        note(f"no pod for index {index} of {name}")
        return
    for pod in pods_:
        note(f"== {pod.metadata.name} ({pod.status.phase}) ==")
        try:
            note(
                kube.core().read_namespaced_pod_log(
                    pod.metadata.name, cfg.namespace, tail_lines=40
                )
            )
        except Exception as exc:  # noqa: BLE001 - best-effort log fetch
            note(exc)


@cli.command(help="list the layer's pods (index, phase, node)")
@_LAYER
@pass_cfg
def pods(cfg, layer):
    """List the layer's pods: index, phase, node, scheduling reason."""
    name = manifest.job_name(cfg, layer)
    table = Table(title=name)
    for col in ("index", "phase", "node", "reason"):
        table.add_column(col)
    for pod in sorted(
        kube.pods_of(cfg.namespace, name),
        key=lambda p: int(
            (p.metadata.annotations or {}).get(
                "batch.kubernetes.io/job-completion-index", -1
            )
        ),
    ):
        ann = pod.metadata.annotations or {}
        reason = next(
            (
                c.reason
                for c in pod.status.conditions or []
                if c.type == "PodScheduled" and c.status != "True"
            ),
            "",
        )
        table.add_row(
            ann.get("batch.kubernetes.io/job-completion-index", "?"),
            pod.status.phase or "?",
            pod.spec.node_name or "-",
            reason or "",
        )
    Console().print(table)


@cli.command(help="show the layer's Job + pod events")
@_LAYER
@pass_cfg
def events(cfg, layer):
    """Recent events for the layer's Job and pods (scheduling, scale-up, failures)."""
    name = manifest.job_name(cfg, layer)
    for e in kube.job_events(cfg.namespace, name):
        when = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
        note(
            f"{when:%H:%M:%S} {e.type:7} {e.reason:22} "
            f"{e.involved_object.kind}/{e.involved_object.name}: {e.message}"
        )


@cli.command(help="delete the layer's Job and pods")
@_LAYER
@pass_cfg
def delete(cfg, layer):
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


@cli.command(help="live per-pod CPU/memory usage in cores/GiB (needs metrics-server)")
@_LAYER
@click.option("-o", "--once", is_flag=True, help="print one snapshot and exit")
@click.option(
    "-i", "--interval", type=float, default=5.0, help="refresh seconds (default 5)"
)
@pass_cfg
def top(cfg, layer, once, interval):
    """Live per-pod usage for the layer, ordered by task index; Ctrl-C to stop."""
    name = manifest.job_name(cfg, layer)
    if once:
        Console().print(util.usage_table(cfg, name))
        return
    try:
        with Live(refresh_per_second=4) as live:
            while True:
                live.update(util.usage_table(cfg, name))
                time.sleep(interval)
    except KeyboardInterrupt:
        pass


@cli.command(help="live per-layer progress table")
@click.option("-o", "--once", is_flag=True, help="print one snapshot and exit")
@click.option(
    "-i", "--interval", type=float, default=5.0, help="refresh seconds (default 5)"
)
@pass_cfg
def status(cfg, once, interval):
    """Live progress over all layers (a-priori chunk counts); runs until Ctrl-C."""
    try:
        layer_totals = util.read_layer_counts(cfg)
    except (SystemExit, Exception):  # noqa: BLE001 - totals are enrichment; degrade gracefully
        layer_totals = None
    if not layer_totals and not kube.list_jobs(cfg.namespace, cfg.workload):
        note(f"no {cfg.workload} jobs in ns '{cfg.namespace}'")
        return
    if once:
        Console().print(util.status_table(cfg, layer_totals))
        return
    try:
        with Live(refresh_per_second=4) as live:
            while True:  # stays up across layers; Ctrl-C to stop
                live.update(util.status_table(cfg, layer_totals))
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    table = costs.load_table()
    if cfg.region and table:
        try:
            total = sum(
                costs.estimate_job_cost(
                    job, kube.pods_of(cfg.namespace, job.metadata.name), table, cfg.region
                ).get("total", 0.0)
                for job in kube.list_jobs(cfg.namespace, cfg.workload)
            )
            note(f"estimated cost so far ~{costs.fmt_dollars(total)}")
        except Exception as exc:  # noqa: BLE001 - cost is auxiliary, never fatal
            note(f"cost unavailable: {exc}")


@cli.command("costs", help="estimate the layer's spot cost (pod requests x runtime)")
@_LAYER
@pass_cfg
def show_costs(cfg, layer):
    """Estimate a layer's Autopilot spot cost from pod requests x runtime."""
    table = costs.load_table()
    if not cfg.region or not table:
        note(
            f"no cost rates (region '{cfg.region}'); set `region:` in config/pipeline.yml "
            f"or run `python -m pipeline.rates`"
        )
        return
    name = manifest.job_name(cfg, layer)
    try:
        job = kube.batch().read_namespaced_job(name, cfg.namespace)
        est = costs.estimate_job_cost(
            job, kube.pods_of(cfg.namespace, name), table, cfg.region
        )
        note(f"{name}: {costs.format_cost(est)}")
    except Exception as exc:  # noqa: BLE001 - cost is auxiliary, never fatal
        note(f"cost unavailable: {exc}")


def main(argv=None):
    try:
        cli.main(args=argv, prog_name="pipeline")
    except ApiException as exc:
        body = (exc.body or "").strip()
        raise SystemExit(
            f"kubernetes API error {exc.status} {exc.reason}"
            + (f": {body[:300]}" if body else "")
            + " — rerun with -v for the full exchange"
        )
    except urllib3.exceptions.HTTPError as exc:
        raise SystemExit(
            f"cannot reach the cluster: {exc} — check kubeconfig "
            f"(terraform output kubernetes_cluster_context), rerun with -v"
        )
