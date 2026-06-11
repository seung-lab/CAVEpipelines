"""`pipeline` — operator CLI for the GKE Autopilot chunk-batch pipelines.

Everything configurable lives in pipeline.yml (one graph, one workload at a time),
so commands carry only a layer. This drives the helm static infra and the per-layer
Indexed Jobs. Layers are operator-gated: submit one, watch it Complete, submit the
next (a layer's writes are non-idempotent).
"""

import argparse
import subprocess
import tempfile
import time

import yaml
from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import config, kube, manifest, util

HELM_CHART = "helm"


def deploy(cfg, args):
    """helm upgrade --install the static infra, then load secrets from ./secrets."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
        yaml.safe_dump(manifest.helm_values(cfg), f)
        f.flush()
        subprocess.run(
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
            check=True,
        )
    applied = kube.apply_secret(
        cfg.namespace, cfg.secret_name, args.secrets, cfg.secret_files
    )
    print(
        f"deployed static infra in ns '{cfg.namespace}'; "
        + (
            f"secret '{cfg.secret_name}' <- {applied}"
            if applied
            else "no secret_files listed (secret skipped)"
        )
    )


def setup(cfg, args):
    """Prepare the graph for the workload: ingest creates the table; migrate preps it."""
    if cfg.workload in ("migrate", "migrate_cleanup"):
        argv = ["python", "-m", "pychunkedgraph.pipeline.migrate.setup", cfg.graph_id]
    else:
        argv = ["python", "-m", "pychunkedgraph.pipeline.ingest.setup", cfg.graph_id]
        if args.raw:
            argv.append("--raw")
    print(util.run_pcg(cfg, "setup", argv))


def mesh_meta(cfg, args):
    """Write mesh metadata once (after ingest reaches root); needs `mesh_config:` in the dataset."""
    argv = ["python", "-m", "pychunkedgraph.pipeline.meshing.setup", cfg.graph_id]
    print(util.run_pcg(cfg, "mesh-meta", argv))


def submit(cfg, args):
    """Create one layer's Indexed Job (completions from cg.meta) and ramp parallelism."""
    n = util.read_n(cfg, args.layer)
    completions = util.ceil_div(n, cfg.job.batch_size)
    pmax = min(cfg.ramp.max, completions)
    parallelism = min(cfg.ramp.start, pmax)
    spec = manifest.job_spec(cfg, args.layer, n, completions, parallelism)
    name = spec.metadata.name
    kube.recreate_job(cfg.namespace, spec)
    print(f"{name}: N={n} completions={completions} parallelism={parallelism}->{pmax}")
    p = parallelism
    while p < pmax:
        time.sleep(cfg.ramp.period)
        p = min(p * cfg.ramp.factor, pmax)
        kube.set_parallelism(cfg.namespace, name, p)
        print(f"  parallelism -> {p}/{pmax}")
    print("at full parallelism; watch with `pipeline status`")


def scale(cfg, args):
    """Resize the running layer's workers: set its Indexed Job parallelism."""
    name = manifest.job_name(cfg, args.layer)
    kube.set_parallelism(cfg.namespace, name, args.parallelism)
    print(f"{name}: parallelism -> {args.parallelism}")


def sample(cfg, args):
    """Run `count` scattered chunks of the layer (one per pod) to size CPU/memory."""
    count = args.count
    spec = manifest.job_spec(
        cfg, args.layer, count, count, min(count, cfg.ramp.max), batch_size=1
    )
    name = spec.metadata.name
    kube.recreate_job(cfg.namespace, spec)
    print(f"{name}: running {count} sample chunks; size with `pipeline top {args.layer}`")


def inspect(cfg, args):
    """List a layer's failed indexes; with an index, show that index's pod log."""
    name = manifest.job_name(cfg, args.layer)
    if args.index is None:
        s = kube.batch().read_namespaced_job(name, cfg.namespace).status
        print(f"{name}: succeeded={s.succeeded} active={s.active} failed={s.failed}")
        print(f"failed indexes: {getattr(s, 'failed_indexes', None) or 'none'}")
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
        print(f"no pod for index {args.index} of {name}")
        return
    for pod in pods_:
        print(f"== {pod.metadata.name} ({pod.status.phase}) ==")
        try:
            print(
                kube.core().read_namespaced_pod_log(
                    pod.metadata.name, cfg.namespace, tail_lines=40
                )
            )
        except Exception as exc:  # noqa: BLE001 - best-effort log fetch
            print(exc)


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
        print(
            f"{when:%H:%M:%S} {e.type:7} {e.reason:22} "
            f"{e.involved_object.kind}/{e.involved_object.name}: {e.message}"
        )


def delete(cfg, args):
    """Delete the layer's Job and its pods."""
    name = manifest.job_name(cfg, args.layer)
    kube.delete_job(cfg.namespace, name)
    print(f"deleting {name}")


def top(cfg, args):
    """Per-pod CPU/memory usage for the layer (needs metrics-server)."""
    name = manifest.job_name(cfg, args.layer)
    items = kube.pod_metrics(cfg.namespace, name)
    if not items:
        print("no metrics (metrics-server unavailable, or no running pods)")
        return
    table = Table(title=f"{name} usage")
    for col in ("pod", "cpu", "memory"):
        table.add_column(col)
    for item in sorted(items, key=lambda i: i["metadata"]["name"]):
        usage = item["containers"][0]["usage"]
        table.add_row(item["metadata"]["name"], usage["cpu"], usage["memory"])
    Console().print(table)


def status(cfg, args):
    """Live per-layer progress table (the configured workload); Ctrl-C to stop."""
    if args.once:
        Console().print(util.status_table(cfg))
        return
    try:
        with Live(refresh_per_second=4) as live:
            while True:
                live.update(util.status_table(cfg))
                jobs = kube.list_jobs(cfg.namespace, cfg.workload)
                if jobs and all(util.job_state(j) != "running" for j in jobs):
                    break
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    p.add_argument(
        "-c",
        "--config",
        default="pipeline.yml",
        help="config file (default: pipeline.yml)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="install/upgrade static infra (helm) + secret")
    d.add_argument(
        "-s",
        "--secrets",
        default="secrets",
        help="dir of secret files (default: secrets)",
    )
    d.set_defaults(fn=deploy)

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
    ):
        sp = sub.add_parser(cmd, help=helptext)
        sp.add_argument("layer", type=int)
        sp.set_defaults(fn=handler)

    args = p.parse_args(argv)
    args.fn(config.load(args.config), args)


if __name__ == "__main__":
    main()
