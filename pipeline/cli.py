"""`pipeline` — operator CLI for the GKE Autopilot chunk-batch pipelines.

Everything configurable lives in pipeline.yml (one graph, one workload at a time),
so commands carry only a layer. This drives the helm static infra and the per-layer
Indexed Jobs. Layers are operator-gated: submit one, watch it Complete, submit the
next (a layer's writes are non-idempotent).
"""

import argparse
import logging
import subprocess
import tempfile
import time

import urllib3
import yaml
from kubernetes.client import ApiException
from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import NOTE, config, costs, kube, log, manifest, note, util

HELM_CHART = "helm"


def deploy(cfg, args):
    """helm upgrade --install the static infra, incl. the Secret built from ./secrets."""
    if args.submit_l2 and not args.setup:
        raise SystemExit("--submit-l2 requires --setup")
    data = kube.secret_data(args.secrets, cfg.secret_files)
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
    if args.setup:
        setup(cfg, args, wait_create=True)  # util pod is still spinning up post-deploy
        if args.submit_l2:
            args.layer = 2
            submit(cfg, args)
        else:
            note("pipeline ready; run `pipeline submit <layer>`")


def undeploy(cfg, args):
    """Tear down everything deploy/submit created: all pipeline Jobs, then the helm release."""
    note("undeploy: deleting jobs + helm release")
    for job in kube.list_jobs(cfg.namespace):
        kube.delete_job(cfg.namespace, job.metadata.name)
        note(f"deleted job {job.metadata.name}")
    res = subprocess.run(
        ["helm", "uninstall", "pcg", "-n", cfg.namespace], capture_output=True, text=True
    )
    note(res.stdout.strip() or res.stderr.strip())


def setup(cfg, args, wait_create=False):
    """Prepare the graph for the workload: ingest creates the table; migrate preps it."""
    if cfg.workload in ("migrate", "migrate_cleanup"):
        argv = ["python", "-m", "pychunkedgraph.pipeline.migrate.setup", cfg.graph_id]
    else:
        argv = ["python", "-m", "pychunkedgraph.pipeline.ingest.setup", cfg.graph_id]
        if args.raw:
            argv.append("--raw")
    note(f"setup ({cfg.workload})")
    note(util.run_pcg(cfg, "setup", argv, wait_create=wait_create) or "setup done")
    util.invalidate_layer_counts(cfg)  # graph may have changed; recompute on next read


def mesh_meta(cfg, args):
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


def submit(cfg, args):
    """Create one layer's Indexed Job (completions from cg.meta) and ramp parallelism."""
    note(f"submit L{args.layer} ({cfg.workload})")
    _require_prev_complete(cfg, args.layer, force=getattr(args, "force", False))
    n = util.read_n(cfg, args.layer)
    completions = util.ceil_div(n, cfg.job.batch_size)
    pmax = min(cfg.ramp.max, completions)
    parallelism = min(cfg.ramp.start, pmax)
    spec = manifest.job_spec(cfg, args.layer, n, completions, parallelism)
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
                f"~${burn:.4f}/pod-hr spot; `pipeline costs {args.layer}` for the running total"
            )
        except Exception:  # noqa: BLE001 - cost is auxiliary, never fatal
            pass


def scale(cfg, args):
    """Resize the running layer's workers: set its Indexed Job parallelism."""
    name = manifest.job_name(cfg, args.layer)
    kube.set_parallelism(cfg.namespace, name, args.parallelism)
    note(f"{name}: parallelism -> {args.parallelism}")


def sample(cfg, args):
    """Run `count` scattered chunks of the layer (one per pod) to size CPU/memory."""
    count = args.count
    spec = manifest.job_spec(
        cfg, args.layer, count, count, min(count, cfg.ramp.max), batch_size=1
    )
    name = spec.metadata.name
    note(
        f"{name}: launching {count} sample chunks; size with `pipeline top {args.layer}`"
    )
    kube.recreate_job(cfg.namespace, spec)


def inspect(cfg, args):
    """List a layer's failed indexes; with an index, show that index's pod log."""
    name = manifest.job_name(cfg, args.layer)
    if args.index is None:
        s = kube.batch().read_namespaced_job(name, cfg.namespace).status
        note(
            f"{name}: {s.succeeded or 0} ok, {s.active or 0} active, "
            f"{s.failed or 0} failed pod attempts"
        )
        failed_idx = getattr(s, "failed_indexes", None)
        if failed_idx:
            note(f"permanently-failed indexes: {failed_idx}")
            note(f"`pipeline inspect {args.layer} <index>` for a failed index's log")
        else:
            note(
                "no permanently-failed indexes (failed attempts retried + recovered); "
                f"`pipeline events {args.layer}` shows preemptions/retries"
            )
        return
    pods_ = (
        kube.core()
        .list_namespaced_pod(
            cfg.namespace,
            label_selector=f"batch.kubernetes.io/job-name={name},"
            f"batch.kubernetes.io/job-completion-index={args.index}",
        )
        .items
    )
    if not pods_:
        note(f"no pod for index {args.index} of {name}")
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


def pods(cfg, args):
    """List the layer's pods: index, phase, node, scheduling reason."""
    name = manifest.job_name(cfg, args.layer)
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


def events(cfg, args):
    """Recent events for the layer's Job and pods (scheduling, scale-up, failures)."""
    name = manifest.job_name(cfg, args.layer)
    for e in kube.job_events(cfg.namespace, name):
        when = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
        note(
            f"{when:%H:%M:%S} {e.type:7} {e.reason:22} "
            f"{e.involved_object.kind}/{e.involved_object.name}: {e.message}"
        )


def delete(cfg, args):
    """Delete the layer's Job and its pods."""
    name = manifest.job_name(cfg, args.layer)
    try:
        kube.delete_job(cfg.namespace, name)
    except ApiException as exc:
        if exc.status == 404:
            note(f"no job '{name}' in ns '{cfg.namespace}'")
            return
        raise
    note(f"deleting {name}")


def top(cfg, args):
    """Per-pod CPU/memory usage for the layer (needs metrics-server)."""
    name = manifest.job_name(cfg, args.layer)
    items = kube.pod_metrics(cfg.namespace, name)
    if not items:
        note("no metrics (metrics-server unavailable, or no running pods)")
        return
    table = Table(title=f"{name} usage")
    for col in ("pod", "cpu", "memory"):
        table.add_column(col)
    for item in sorted(items, key=lambda i: i["metadata"]["name"]):
        usage = item["containers"][0]["usage"]
        table.add_row(item["metadata"]["name"], usage["cpu"], usage["memory"])
    Console().print(table)


def status(cfg, args):
    """Live progress over all layers (a-priori chunk counts); runs until Ctrl-C."""
    try:
        layer_totals = util.read_layer_counts(cfg)
    except (SystemExit, Exception):  # noqa: BLE001 - totals are enrichment; degrade gracefully
        layer_totals = None
    if not layer_totals and not kube.list_jobs(cfg.namespace, cfg.workload):
        note(f"no {cfg.workload} jobs in ns '{cfg.namespace}'")
        return
    if args.once:
        Console().print(util.status_table(cfg, layer_totals))
        return
    try:
        with Live(refresh_per_second=4) as live:
            while True:  # stays up across layers; Ctrl-C to stop
                live.update(util.status_table(cfg, layer_totals))
                time.sleep(args.interval)
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
            note(f"estimated cost so far ~${total:.2f}")
        except Exception as exc:  # noqa: BLE001 - cost is auxiliary, never fatal
            note(f"cost unavailable: {exc}")


def show_costs(cfg, args):
    """Estimate a layer's Autopilot spot cost from pod requests x runtime."""
    table = costs.load_table()
    if not cfg.region or not table:
        note(
            f"no cost rates (region '{cfg.region}'); set `region:` in config/pipeline.yml "
            f"or run `python -m pipeline.rates`"
        )
        return
    name = manifest.job_name(cfg, args.layer)
    try:
        job = kube.batch().read_namespaced_job(name, cfg.namespace)
        est = costs.estimate_job_cost(
            job, kube.pods_of(cfg.namespace, name), table, cfg.region
        )
        note(f"{name}: {costs.format_cost(est)}")
    except Exception as exc:  # noqa: BLE001 - cost is auxiliary, never fatal
        note(f"cost unavailable: {exc}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    p.add_argument(
        "-c",
        "--config",
        default="config",
        help="config dir with pipeline.yml + dataset.yml (default: config)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="debug logging, incl. every kubernetes API request",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="install/upgrade static infra (helm) + secret")
    d.add_argument(
        "-s",
        "--secrets",
        default="secrets",
        help="dir of secret files (default: secrets)",
    )
    d.add_argument(
        "--setup",
        action="store_true",
        help="run `setup` after deploy (first-run convenience)",
    )
    d.add_argument(
        "-r", "--raw", action="store_true", help="raw agglomeration input (with --setup)"
    )
    d.add_argument(
        "--submit-l2",
        action="store_true",
        help="submit layer 2 after setup (requires --setup)",
    )
    d.set_defaults(fn=deploy)

    ud = sub.add_parser(
        "undeploy", help="delete all pipeline Jobs and the helm release (incl. secret)"
    )
    ud.set_defaults(fn=undeploy)

    s = sub.add_parser(
        "setup", help="create the graph table + meta (runs in the util pod)"
    )
    s.add_argument("-r", "--raw", action="store_true", help="raw agglomeration input")
    s.set_defaults(fn=setup)

    mm = sub.add_parser(
        "mesh-meta", help="write mesh metadata once (after ingest reaches the root layer)"
    )
    mm.set_defaults(fn=mesh_meta)

    su = sub.add_parser(
        "submit", help="submit one layer's Indexed Job and ramp parallelism"
    )
    su.add_argument("layer", type=int)
    su.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="submit even if the layer below isn't complete — only if you're sure",
    )
    su.set_defaults(fn=submit)

    sc = sub.add_parser(
        "scale", help="resize the running layer's workers (set Job parallelism)"
    )
    sc.add_argument("layer", type=int)
    sc.add_argument("parallelism", type=int)
    sc.set_defaults(fn=scale)

    sm = sub.add_parser(
        "sample", help="run N scattered chunks of a layer to size CPU/memory"
    )
    sm.add_argument("layer", type=int)
    sm.add_argument("count", type=int)
    sm.set_defaults(fn=sample)

    st = sub.add_parser("status", help="live per-layer progress table")
    st.add_argument(
        "-o", "--once", action="store_true", help="print one snapshot and exit"
    )
    st.add_argument(
        "-i", "--interval", type=float, default=5.0, help="refresh seconds (default 5)"
    )
    st.set_defaults(fn=status)

    i = sub.add_parser(
        "inspect", help="list a layer's failed indexes; add an index for its pod log"
    )
    i.add_argument("layer", type=int)
    i.add_argument("index", type=int, nargs="?")
    i.set_defaults(fn=inspect)

    for cmd, handler, helptext in (
        ("pods", pods, "list the layer's pods (index, phase, node)"),
        ("events", events, "show the layer's Job + pod events"),
        ("top", top, "per-pod CPU/memory usage (needs metrics-server)"),
        ("delete", delete, "delete the layer's Job and pods"),
        ("costs", show_costs, "estimate the layer's spot cost (pod requests x runtime)"),
    ):
        sp = sub.add_parser(cmd, help=helptext)
        sp.add_argument("layer", type=int)
        sp.set_defaults(fn=handler)

    args = p.parse_args(argv)
    # Root stays at NOTE so -v doesn't unleash urllib3/kubernetes HTTP body dumps
    # (unreadable multi-KB single lines); -v only deepens our own logger.
    logging.basicConfig(level=NOTE, format="%(message)s")
    if args.verbose:
        log.setLevel(logging.DEBUG)
    try:
        args.fn(config.load(args.config), args)
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
