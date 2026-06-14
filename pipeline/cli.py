"""`pipeline` — operator CLI for the GKE Autopilot chunk-batch pipelines.

Everything configurable lives in pipeline.yml (one graph, one workload at a time),
so commands carry only a layer. This drives the helm static infra and the per-layer
Indexed Jobs. Layers are operator-gated: submit one, watch it Complete, submit the
next (a layer's writes are non-idempotent).
"""

import dataclasses
import functools
import logging
import os
import time
from datetime import datetime, timezone

import click
import urllib3
from kubernetes.client import ApiException
from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import NOTE, config, costs, kube, log, manifest, note, ops, util
from .db import cost, state


@click.group(help=__doc__, context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-c",
    "--config",
    "config_name",
    default=None,
    help="path to a pipeline yaml; the first -c selects the session config "
    "(default: the session config, else config/pipeline.yml)",
)
@click.option(
    "-g",
    "--graph-id",
    default=None,
    help="override the config file's graph_id (e.g. test-run iterations)",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="debug logging, incl. every kubernetes API request",
)
@click.pass_context
def cli(ctx, config_name, graph_id, verbose):
    # Root stays at NOTE so -v doesn't unleash urllib3/kubernetes HTTP body dumps
    # (unreadable multi-KB single lines); -v only deepens our own logger.
    logging.basicConfig(level=NOTE, format="%(message)s")
    if verbose:
        log.setLevel(logging.DEBUG)
    ctx.obj = (config_name, graph_id)  # loaded lazily by pass_cfg: --help needs no config


def pass_cfg(fn):
    """Pass the loaded Config as the handler's first argument.

    Loading is lazy (post --help) and cached on ctx.obj; tests inject a prebuilt
    Config via CliRunner(...).invoke(command, obj=cfg)."""

    @click.pass_context
    def wrap(ctx, *args, **kwargs):
        if not isinstance(ctx.obj, config.Config):
            name, graph_id = ctx.obj or (None, None)
            newly_selected = name and not config.stored()
            cfg = config.resolve(name)
            if graph_id:
                cfg.graph_id = graph_id
            ctx_id = f"graph: {cfg.graph_id}, workload: {cfg.workload}"
            if newly_selected:  # announce the session lock loudly
                note(
                    f"config: {cfg.source} ({ctx_id}) — session config; "
                    f"every command uses it until `pipeline reset`"
                )
            else:
                note(f"config: {cfg.source} ({ctx_id})")
            ctx.obj = cfg
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
@click.option(
    "--submit-l2", is_flag=True, help="submit layer 2 after setup (requires --setup)"
)
@click.option(
    "--oneshot",
    is_flag=True,
    help="build DAG; prompts for start/end depth (or --from/--to), independents in parallel",
)
@click.option(
    "--all-layers",
    "all_layers",
    is_flag=True,
    help="run all layers of the configured workload (+ its setup), nothing else",
)
@click.option("--from", "start", type=int, help="--oneshot start depth (skip the prompt)")
@click.option("--to", "end", type=int, help="--oneshot end depth (skip the prompt)")
@click.option(
    "--sequential",
    is_flag=True,
    help="run independent stages one at a time instead of in parallel",
)
@click.option(
    "--yes",
    is_flag=True,
    help="skip the --oneshot/--all-layers prompt + confirmation (unattended, full range)",
)
@pass_cfg
def deploy(
    cfg, secrets, run_setup, submit_l2, oneshot, all_layers, start, end, sequential, yes
):
    if oneshot and all_layers:
        raise SystemExit("--oneshot and --all-layers are mutually exclusive")
    if (oneshot or all_layers) and (run_setup or submit_l2):
        raise SystemExit("--oneshot/--all-layers supersede --setup/--submit-l2")
    if submit_l2 and not run_setup:
        raise SystemExit("--submit-l2 requires --setup")
    if oneshot and cfg.workload in ("migrate", "migrate_cleanup"):
        raise SystemExit(f"'{cfg.workload}' is not part of a build; use --all-layers")
    parallel = not sequential
    run_set = None
    if oneshot:
        run_set = ops.select_range(cfg, start, end, yes)  # DAG + start/end prompt
    elif all_layers:
        run_set = {cfg.workload}
    if run_set is not None:
        ops.confirm_run(
            cfg, run_set, parallel, yes
        )  # confirm before any cluster mutation
    ops.deploy_infra(cfg, secrets)
    if run_set is not None:
        state.start_run(cfg, run_set, parallel, pid=os.getpid())
        ops.drive(cfg)
    elif run_setup:
        ops.setup(cfg)
        if submit_l2:
            ops.submit(cfg, 2)
        else:
            note("pipeline ready; run `pipeline submit <layer>`")


@cli.command(
    help="delete all pipeline Jobs, the helm release (incl. secret) + layer-counts cache"
)
@pass_cfg
def undeploy(cfg):
    ops.undeploy(cfg)


@cli.command(help="suspend every pipeline Job (0 resources; deletes nothing)")
@pass_cfg
def pause(cfg):
    ops.pause(cfg)


@cli.command(help="unsuspend the run's Jobs and continue driving where it paused")
@pass_cfg
def resume(cfg):
    ops.resume(cfg)


@cli.command(help="create the graph table + meta (one-shot pod with the dataset)")
@click.option(
    "--exists", is_flag=True, help="skip (don't error) if the graph already exists"
)
@pass_cfg
def setup(cfg, exists):
    ops.setup(cfg, exist_ok=exists)


@cli.command(
    "mesh-meta", help="write mesh metadata once (after ingest reaches the root layer)"
)
@pass_cfg
def mesh_meta(cfg):
    ops.mesh_meta(cfg)


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
    ops.submit(cfg, layer, force=force)


@cli.command(help="resize the running layer's workers (set Job parallelism)")
@_LAYER
@click.argument("parallelism", type=int)
@pass_cfg
def scale(cfg, layer, parallelism):
    ops.scale(cfg, layer, parallelism)


@cli.command(help="run N scattered chunks of a layer to size CPU/memory")
@_LAYER
@click.argument("count", type=int)
@pass_cfg
def sample(cfg, layer, count):
    ops.sample(cfg, layer, count)


@cli.command(help="delete the layer's Job and pods")
@_LAYER
@pass_cfg
def delete(cfg, layer):
    ops.delete(cfg, layer)


@cli.command(help="forget the session config; the next -c selects a new one")
def reset():
    config.forget()
    note("session config cleared")


@cli.command(help="list a layer's failed indexes; add an index for its pod log")
@_LAYER
@click.argument("index", type=int, required=False)
@pass_cfg
def inspect(cfg, layer, index):
    """List a layer's failed indexes; with an index, show that index's pod log."""
    name = manifest.job_name(cfg, layer)
    if index is None:
        try:
            s = kube.batch().read_namespaced_job(name, cfg.namespace).status
        except ApiException as exc:
            if exc.status == 404:
                note(f"no job '{name}' in ns '{cfg.namespace}'")
                return
            raise
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
        Console().print(util.usage_table(cfg, name, layer))
        return
    try:
        with Live(refresh_per_second=4) as live:
            while True:
                live.update(util.usage_table(cfg, name, layer))
                time.sleep(interval)
    except KeyboardInterrupt:
        pass


def _cost_note(cfg, workloads) -> None:
    """Estimated spend so far across `workloads`: compute summed, cluster fee charged once."""
    rate_table = costs.load_table()
    if not (cfg.region and rate_table):
        return
    try:
        now = datetime.now(timezone.utc).timestamp()
        compute, jobs = 0.0, []
        for w in workloads:
            cfg_w = dataclasses.replace(cfg, workload=w)
            per_layer, _ = util.recorded_costs(cfg_w, rate_table)
            compute += sum(agg["total"] for agg in per_layer.values())
            jobs += cost.jobs(cfg_w)
        total = compute + costs.fee(rate_table, cfg.region, jobs, now)
        note(f"estimated cost so far ~{costs.fmt_dollars(total)} (incl. cluster fee)")
    except Exception as exc:  # noqa: BLE001 - cost is auxiliary, never fatal
        note(f"cost unavailable: {exc}")


@cli.command(help="live progress: the recorded run's stages, or the configured workload")
@click.option("-o", "--once", is_flag=True, help="print one snapshot and exit")
@click.option(
    "-i", "--interval", type=float, default=5.0, help="refresh seconds (default 5)"
)
@pass_cfg
def status(cfg, once, interval):
    """Live progress. With a recorded run: each stage as a table while running, a one-line
    summary once done. With no run: the configured workload's per-layer table. Until Ctrl-C."""
    run = state.get_run(cfg)
    if run is None and not kube.list_jobs(cfg.namespace, cfg.workload):
        note(f"no recorded run and no {cfg.workload} jobs in ns '{cfg.namespace}'")
        return
    try:  # totals only enrich the table (fill pending-layer counts), so degrade gracefully
        layer_totals = util.read_layer_counts(cfg)
    except (SystemExit, Exception):  # noqa: BLE001
        layer_totals = None
    if run is not None:
        order = [w for level in ops.dag_levels(run.stage_set) for w in level]

        def render():
            current = state.states(cfg)
            for w in order:
                if current.get(w) == state.RUNNING:
                    cost.sample(dataclasses.replace(cfg, workload=w))
            return util.run_view(cfg, state.get_run(cfg), order, current, layer_totals)
    else:
        order = [cfg.workload]

        def render():
            cost.sample(cfg)
            return util.status_table(cfg, layer_totals)

    if once:
        Console().print(render())
        return
    try:
        with Live(refresh_per_second=4) as live:
            while True:  # stays up across layers; Ctrl-C to stop
                live.update(render())
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    _cost_note(cfg, order)


@cli.command("costs", help="the layer's recorded spot cost (sampled runtimes x rates)")
@_LAYER
@pass_cfg
def show_costs(cfg, layer):
    """A layer's Autopilot spot cost from recorded pod runtimes x current rates."""
    rate_table = costs.load_table()
    if not cfg.region or not rate_table:
        note(
            f"no cost rates (region '{cfg.region}'); set `region:` in config/pipeline.yml "
            f"or run `python -m pipeline.rates`"
        )
        return
    cost.sample(cfg)
    try:
        per_layer, _ = util.recorded_costs(cfg, rate_table)
    except Exception as exc:  # noqa: BLE001 - cost is auxiliary, never fatal
        note(f"cost unavailable: {exc}")
        return
    if layer not in per_layer:
        note(f"no recorded runs for layer {layer}")
        return
    note(f"{manifest.job_name(cfg, layer)}: {costs.format_cost(per_layer[layer])}")


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
