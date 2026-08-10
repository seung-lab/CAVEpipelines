# Agent notes

What the other docs don't say. [README.md](README.md) = how to run, [ARCHITECTURE.md](ARCHITECTURE.md)
= why, [config/README.md](config/README.md) = fields.

## Diagnose with the CLI, not kubectl

```shell
pipeline inspect <layer>          # "N ok, N active, N failed attempts" + whether any index is dead
pipeline inspect <layer> <index>  # that pod's log via relevant_log(): anchored at the Traceback
pipeline events <layer>           # scheduling, scale-up, podFailurePolicy
pipeline top <layer>              # per-pod cpu/mem vs request
```

`kubectl logs | grep | tail` shows the traceback's *innermost* frame, so a wrapper exception that
replaced the real one never appears. `relevant_log()` shows the whole failure.

**Capture logs before `pause`/`delete`** — suspending drains the pods and `inspect <index>` then
returns `no pod for index N`.

## `ramp.max` is one pod count, but it spends vCPU

`max_parallelism(ramp_max, completions)` applies the same pod cap to every layer, while
`job.resources.cpu` climbs per layer. Peak draw is `ramp_max × cpu(layer)`, so the same number
costs several times more at the top of the curve than at L2.

L2 holds most of the chunks and most of the wall clock; upper layers self-limit on task count
(`completions` caps parallelism below `ramp_max` once tasks run out). Size `max ≈ headroom /
cpu(L2)` — sizing it so the *widest upper layer* fits leaves L2 running at a fraction of quota
for the longest stretch of the run. Cost is per pod-second, so N× pods = N× sooner at the same
spend.

```shell
gcloud compute regions describe <region> --project <proj> --format=json \
  | jq '.quotas[] | select(.metric=="PREEMPTIBLE_CPUS")'
```

**Only widen a cpu-saturated layer** — check `pipeline top <layer>` first. A layer sitting well
under its request is backend-bound, and widening it only adds retries.

`pipeline apply` re-scales a running Job (`ramp.max` + resources are excluded from immutable-drift).

## Processes come from `PCG_N_PROCESSES`, never `mp.cpu_count()`

`mp.cpu_count()` is `os.cpu_count()` — "CPUs in the **system**", no cgroup awareness, so in a
container it returns the *node's* cores and a small pod forks itself into CFS throttling.
`layer_processes(job, layer)` derives the count from the pod's billed cpu; `harness.py` reads it
as `n_processes`; `parallel: false` → 1.

Explicit passing is correct: `sched_getaffinity`/`os.process_cpu_count()` give the affinity mask,
not the CFS quota Autopilot uses; `/sys/fs/cgroup/cpu.max` is cgroup-version-specific.

- **L2 proves nothing about a parent layer** — atomic ingest uses no pool.
- **A `multiprocessing.pool` traceback is a wrapper.** A worker exception is pickled with its
  traceback; when that holds an unpicklable handle the real exception is *replaced* by
  `MaybeEncodingError`, and the innermost line names the wrong culprit.
- Throttled workers miss RPC deadlines, so the backend looks guilty. Check cpu usage vs request
  before believing a `DeadlineExceeded`.
- **Sample before submitting a parent layer**: `pipeline sample <layer> 20` → `pipeline top <layer>`.

## "failed" means retried, not dead

- `status.failed` = pod attempts that exited non-zero; retried per `backoffLimitPerIndex`.
- `status.failedIndexes` = permanent death, bounded by `maxFailedIndexes`.
- `status.completedIndexes` empty = *nothing ever succeeded*, not "lagging".
- Judge by **depth** (is an index on attempt 4 of 5?), not by the retry *rate*.

An index only completes if one pod clears *all* its chunks in a single run, so a high per-chunk
failure rate can yield zero completions while every layer metric still looks busy.

Ingest lock-deferrals (`held by another worker` → `N ok, 1 transient`) are normal and rise as the
grid fills.

## Driver is foreground; Jobs are not

If the driver dies the Jobs keep running — only the *next* layer's submit is lost. Check driver
liveness separately; a Job-only monitor reads a dead driver as healthy, and can't tell a crash
from `pause`.

**Restart with `resume`, never `deploy --oneshot`** — `deploy` calls `start_run()`, minting a new
run_id and deleting Stage rows, detaching cost accounting from the running Job.

Segfaults leave no core (`ulimit -c` 0, apport keeps nothing); `journalctl | grep traps:` has the
kernel record.

## Config gotchas

- **`pychunkedgraph` >= v3.2.0.dev6 only**, enforced in `config.load()`. v3.2.0 added the worker
  entrypoint / env contract, `.dev4` made the ingest pools honor `PCG_N_PROCESSES`, `.dev5` the
  meshing stitch pool, `.dev6` a cave-pipeline wheel that actually emits the variable. Each bar
  is passed independently, so dev4 ingests correctly and still meshes single-process.
- **The image tag is a proxy; the real contract is the `cave-pipeline` wheel inside it.** dev5
  pinned 0.0.3, whose harness still emitted `n_threads`, so every pod died on
  `KeyError: 'n_processes'` — the tag gate cannot see that. Rename an env key here and the
  wheel must be released and re-pinned in PCG's `requirements.txt` before any image is built.
- `latest` and digest pins are refused: an unreadable tag can't be checked, and the wrong image
  fails inside the pod.
- No `-c` and no `config/.current` → falls back to `config/pipeline.yml`, which may be **another
  project's graph**. Always pass `-c` for destructive commands (`undeploy`, `delete`, `purge`).
- The running driver holds the `cfg` loaded at `deploy`; editing the yml mid-run does not reach
  later layers. Only `apply` (or a restart) re-reads it.

## Cost: cpu dominates, memory is nearly free

Per-vCPU rates are orders of magnitude above per-GiB ones, so a memory over-request looks alarming
on a graph and costs little, while cpu carries most of the bill. The
[ratio floor is 1 GiB/vCPU](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-resource-requests),
so requesting under that bills as 1 GiB/vCPU anyway — trimming memory below the floor saves nothing.

Rates live in [rates.csv](cave_pipeline/rates.csv) per (region, compute class) and are refreshed by
a workflow; read them there, never from a number written into a doc. Measure usage, don't estimate:
`kubectl get --raw "/apis/metrics.k8s.io/v1beta1/namespaces/default/pods"`, and take real spend from
`pipeline costs <layer>` rather than a burn-rate guess.

## Tasks ≠ chunks

`completions = ceil(chunks / batch_size)`, and `batch_size` halves each layer above 2. Label the
unit — 250 tasks/min at `batch_size 16` is 4,000 chunks/min.
