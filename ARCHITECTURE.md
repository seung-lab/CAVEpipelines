# Architecture

Design and rationale for the CAVEpipelines operator CLI. The [README](README.md) covers
**how to run** the pipeline; this document covers **why it is built the way it is**, for
anyone (human or AI) who needs to understand or change it. The worked example of chunk
distribution lives in the README's [How chunks are distributed](README.md#how-chunks-are-distributed-toy-example)
section; everything else is here.

## What it is

The pipeline runs the connectomics build steps — **chunkedgraph ingest**, **meshing**,
**l2cache**, and **graph migration** — on **GKE Autopilot** as stock Kubernetes **Indexed
Jobs**. There is no Redis, RQ, SQS, broker, or long-running worker: one Job per graph layer,
each pod processes a batch of chunks and exits.

Two halves, in two repositories, bridged by Kubernetes:

- **The operator side** (this repo) — a Python CLI using the Kubernetes client. It renders
  helm, builds Job specs, drives multi-stage runs, watches progress, and records cost. It
  never touches a chunk.
- **The worker side** ([PyChunkedGraph](https://github.com/seung-lab/PyChunkedGraph)) — the
  container entrypoint. Each pod reads its assigned index, computes which chunks it owns, and
  processes them.

```
  operator (pipeline CLI)             Kubernetes                worker pods (PyChunkedGraph)
  ───────────────────────             ──────────                ───────────────────────────
  deploy           ──────────────────▶ helm release + Secret
  submit <layer>   ──────────────────▶ Indexed Job  ──────────▶ one worker pod per batch
  status / costs   ◀── poll, sample ── Job + pods
    │
    ├──▶ cost db    (durable: pod runtimes, priced at read time)
    └──▶ state db   (ephemeral: run + stage lifecycle, driver-written)

  each worker pod reads (graph, layer, seed, batch_size) + its Kubernetes index,
                  computes its own chunk slice, then processes each chunk ──▶ Bigtable / GCS
```

## Design principles

1. **No broker, no standing infrastructure.** Stock Kubernetes primitives do the work a
   queue + workers would: parallelism, retries, completion tracking. The cluster scales to
   zero between runs, so an idle pipeline costs nothing but the cluster fee.
2. **Stateless, indexable work distribution.** A worker derives its batch from a few values
   plus its job index — no work-list is shipped, stored, or queued (see *Work distribution*).
3. **Operator-driven and resumable.** A human picks what runs; every step is observable
   through the CLI and survives a pause, a crash, or a re-run.
4. **Cost-honest on Autopilot.** Autopilot bills *requests*, so requests are declared
   explicitly and recorded, never left for Autopilot to round silently.
5. **Backend-agnostic persistence.** Cost and run-state default to local SQLite but point at
   a shared server with one config line.

## Work distribution: the indexable global shuffle

The central decision. A layer's chunks form an `X·Y·Z` grid of `N` chunks, split into batches
of `batch_size`. Instead of materializing a shuffled work-list and dealing it out through a
queue, **each worker computes its own batch** from five inputs — graph, layer, `seed`,
`batch_size`, and its Kubernetes-assigned index — against *one global shuffle of all `N`
chunks*.

That shuffle is a **format-preserving (Feistel) permutation**: a keyed bijection over `[0, N)`
computed point-by-point, with no materialized array, scaling to billions of chunks. It is
deterministic (a retried index rebuilds the identical batch) and invertible (a chunk coord
maps back to its batch for inspection). `batch_size` halves each layer above 2, because a
parent chunk spans ~8× the volume of its children.

**Why not a queue?** A materialized `permutation(N)` array or a Redis/SQS queue is exactly
what this replaces — the indexable shuffle yields the same grid-wide scatter (which keeps the
worker fleet off a single hot Bigtable key range) with no broker to run, scale, or recover.
The full mechanism, with a worked toy example, is in the
[README](README.md#how-chunks-are-distributed-toy-example).

**Why Indexed Jobs?** Kubernetes assigns each pod a unique index, runs them on Spot capacity,
retries each index independently (per-index backoff), and reports completion as a sticky
terminal condition — so the operator side needs no broker and no custom completion tracking.

## The stage DAG orchestrator

Each workload is a **Stage** with `deps`, a `setup` step, a layer range, and an output
location. The stages form a DAG:

```
ingest ──┬──▶ meshing                 migrate ──▶ migrate_cleanup
         └──▶ l2cache (optional)       (a separate pass, never part of a build)
```

`meshing` is always part of a build; `l2cache` joins only when the dataset declares
`l2cache_config`. The operator selects a **depth range** of this DAG to run (`--oneshot`,
optionally `--from`/`--to`); stages at the same depth run in parallel (thread per stage), and
a failure in one halts everything downstream of it. `--all-layers` runs a single configured
workload instead.

**No completion gate.** Dependencies only *order* the run — the orchestrator topologically
sorts `deps ∩ run_set` and never checks whether an upstream is "done." An upstream outside the
chosen range is assumed satisfied **by any means** (a prior run, a table copy, another tool);
a genuinely missing upstream surfaces as that stage's own worker error, not a guess by the
orchestrator. This keeps the orchestrator simple and lets an operator re-run any slice.

**Operator-gated layers.** Within a workload, layers run lowest-first and one at a time:
submit a layer, watch it to 100%, submit the next. Ingest's writes are **non-idempotent**, so
a layer must not start until the one below is complete — the single guard the orchestrator
*does* enforce (overridable with `--force`). Meshing, l2cache, and migrate are idempotent
overwrites, but follow the same gated flow for uniformity.

## Driver lifecycle: deploy, pause, resume

`deploy --oneshot`/`--all-layers` runs the **driver in the foreground**: it walks the DAG,
submits each layer, polls to completion, and advances. It blocks the terminal (run it under
`tmux`/`screen` for long builds). A detached background driver was considered and dropped —
the foreground driver is the simplest thing that satisfies the actual need, pause/resume.

- **Pause** marks the run `paused` in the state db, then sets `spec.suspend` on every
  non-complete Job. Kubernetes SIGTERMs the active pods and Autopilot scales to zero;
  **nothing is deleted** and finished indexes are kept. The driver's poll loop sees the
  suspend flag and exits cleanly.
- **Resume** unsuspends those Jobs, marks the run `running`, and re-drives. Finished layers
  no-op, the suspended layer resumes its incomplete indexes, the DAG continues.
- **Self-pause on failure.** If the driver hits any error or crash, it suspends the cluster
  before propagating — Jobs never keep burning behind a dead driver.
- **Stall detection.** The state db records the driver's pid; a run still marked `running`
  whose pid is dead is reported as stalled, and `resume` can adopt it.

**How long a paused run lives.** Indefinitely — a paused run is lost only if the operator deletes
it. The Job spec sets no `ttlSecondsAfterFinished` or `activeDeadlineSeconds`, and a *suspended*
Job is not a *finished* one, so the finish-TTL controller never collects it; Kubernetes keeps the
completed indexes for resume, and Autopilot's scale-to-zero removes only nodes, not the Job object.
Even a deleted Job is recoverable — `resume` re-submits it and done chunks skip, since the work
product lives in Bigtable, not the Job. The single durable dependency is the state-db `Run` row,
which `pause` only flips to `paused`; only `undeploy`/`purge` remove it. **Caveat:** with the
default local-SQLite state db that durability is tied to the operator's machine — point
`database.state` at a server to survive losing that machine (and to resume from another).

## The worker harness (container side)

All workloads share one generic harness. A workload supplies a `make_processor(cg, layer, env)
→ process_one(coord)` and an optional `finalize(cg, layer)`; the harness does the rest:

1. Reads the env contract (`PCG_GRAPH_ID`, `PCG_LAYER`, `PCG_PERM_SEED`, `PCG_BATCH_SIZE`,
   `PCG_N_THREADS`) plus the Kubernetes-injected `JOB_COMPLETION_INDEX`.
2. Builds **one `ChunkedGraph` per pod** (the meta row is read once, never per chunk, so it
   never hot-rows).
3. Computes its batch's coords from the index via the global shuffle.
4. Runs each chunk through `process_one`, which returns `ok | done | transient | fatal`.
5. Maps the batch outcome to an **exit code** the Job's `podFailurePolicy` understands.

| Outcome | Exit | Job behaviour |
|---|---|---|
| all `ok`/`done` (and `finalize` ok) | `0` SUCCESS | index complete |
| any `transient` | `1` TRANSIENT | whole batch retried (done chunks skip); counts against the per-index budget |
| any `fatal` | `42` FATAL | `FailIndex` — that index fails immediately, no retry |

`done` chunks are skipped on retry, so a re-run resumes rather than restarts. `finalize` runs
only after a fully successful batch (e.g. ingest verifies the hierarchy once the root chunk is
built; it is safe to re-run because no chunk re-opens).

**The per-chunk lock (ingest only).** Ingest's writes are non-idempotent, so each chunk is
claimed under a per-chunk Bigtable lock: `acquire` (skip if already `done`, defer to
`transient` if held live by another worker), a renew-heartbeat thread that keeps the claim
fresh while the chunk runs, then `mark_done` only if the claim is still held. A *dead* worker's
heartbeat stops and its claim expires, after which retry is safe; the lock TTL scales per layer
(cheap layers expire fast). Meshing, l2cache, and migrate are idempotent overwrites and need
**no lock**.

## The Kubernetes Job model

`job_spec` turns one layer into a `completionMode: Indexed` Job:

- `completions = ceil(N / batch_size)` (one index per batch), `parallelism` set by the ramp.
- `backoffLimitPerIndex = job.task_retries` — each index retries on its own budget, so a stuck
  index never starves the rest.
- `maxFailedIndexes = min(job.max_failed_tasks, completions)` — the dead-task tolerance that
  aborts the layer, clamped because Kubernetes rejects a value above `completions`.
- `podFailurePolicy`: **Ignore** Spot preemption (the `DisruptionTarget` condition / SIGTERM —
  it is not a task failure and must not burn the retry budget), **FailIndex** on exit 42;
  everything else counts against the per-index budget. `restartPolicy: Never`.
- Per-pod **resources are requests only**, computed per layer (below), so peak cluster draw is
  `parallelism × per-pod request`.

**Resource curves.** Upper layers do heavier per-chunk work, so requests are declared as a
curve: per dimension, `value(L) = min(base · factor^(L−2) + add, max)`, with per-layer
`overrides` and a flat `job.cpu`/`job.memory` fallback. Each layer is then **snapped to the
cheapest valid Autopilot point** (≥ the 1 GiB/vCPU floor, within the 6.5 GiB/vCPU ratio
ceiling, on the 0.25-vCPU grid) and **refused past the general-purpose ceiling** (30 vCPU /
110 GiB) rather than silently billed on a pricier class. Declaring requests explicitly keeps
the recorded cost honest, since Autopilot would otherwise round up and bill the result.

**Spot + ramp.** Every Job runs on Spot (60–91% off) with the matching toleration; an optional
`compute_class` or `zone` adds node selectors. Parallelism **ramps** geometrically
(`job.ramp.*`) up to `ramp.max` (capped at the task count), so a cold Bigtable can split its
tablets before full load.

## Configuration model

**Single source.** `pipeline.yml` holds everything the operator controls — cluster, images,
identity, Bigtable, job sizing (`batch_size`, ramp, resource curves, per-workload overrides),
env, and the database URLs. `dataset.yml` is the graph definition (`data_source`,
`graph_config`, `mesh_config`, `l2cache_config`), read **only** by `setup`; workers read graph
meta from Bigtable at run time. Field-by-field docs are in [config/README.md](config/README.md).

**Setup vs the util pod.** `setup` and `mesh-meta` run as **fresh one-shot pods** that mount the
graph's `dataset.yml` (delivered as a per-graph ConfigMap). They must be fresh pods, not the
long-lived util pod: a running pod's ConfigMap mount lags the kubelet sync by up to ~90s, so a
just-applied dataset would be read stale. The optional **util pod** is the opposite — a
persistent, graph-agnostic, dataset-free server holding a warm graph handle for sub-second meta
reads (layer counts) between layers. They can't merge: `setup` is a per-graph one-time write that
must see the current dataset; the util pod is a shared warm reader of an already-built graph.

**Session config.** The first `-c <pipeline.yml>` becomes the session config (persisted in
`config/.current`); later commands omit `-c` and a different one is refused until `pipeline
reset` — so a long terminal session can't silently target the wrong graph. `-g` overrides
`graph_id` for one command (test iterations). A local layer-counts cache keyed by graph id
avoids re-initializing a ChunkedGraph on every submit.

## Persistence: two databases

Both are backend-agnostic SQLAlchemy, defaulting to a local SQLite file under `costs/`:

- **Cost db** (`database.cost`) — **durable**. Rows scoped by graph, workload, and a per-deploy
  run-id record each pod's *physical quantities* (runtime, requested vCPU/GiB), sampled whenever
  the CLI watches the cluster (`status` ticks, `submit`'s ramp, `costs`). Dollars are computed
  **at read time** = requests × runtime × the (region, compute-class) rate from a maintained rate
  table, so a rate refresh re-prices history. `costs`/`status` scope to the active deploy's
  run-id, so re-running a graph never sums past runs into the figure; the durable db keeps every
  run. Unwatched completions
  are backfilled from the mean observed runtime; the cluster fee is charged once over the union
  of job wall-time. Records survive pod garbage collection — they are the only number that
  outlives a run.
- **State db** (`database.state`) — **ephemeral**. One Run row per graph (its stage set,
  parallel flag, status `running`/`paused`/`done`, driver pid) and one Stage row per
  `(graph, workload)`. It backs the orchestrator's lifecycle and the `status` DAG view, and is
  cleared by `undeploy` (this graph) or `purge` (all graphs). Writes are best-effort — a
  progress hiccup never aborts a running workload.

**The engine seam** is what makes both portable: one cached engine per URL; `NullPool` for
SQLite (so a deleted db file is never pinned by an open handle) vs
`pool_pre_ping` for servers; WAL + `busy_timeout` pragmas applied only to SQLite; lock-guarded
create-once; and **read-modify-write upserts** instead of any dialect's `ON CONFLICT`. Point a
URL at Postgres and the same code shares state across machines.

## Failure and recovery model

| Event | Handling |
|---|---|
| Spot preemption | `podFailurePolicy` ignores it; the index reschedules, no retry spent |
| Transient error (exit 1) | index retried up to `task_retries`; the retried pod re-claims only its not-done chunks |
| Fatal chunk (exit 42) | only that index fails (`FailIndex`); `pipeline inspect` shows the chunk + traceback |
| Too many dead indexes | the Job aborts once `maxFailedIndexes` is exceeded |
| Re-submit a layer | already-done chunks skip (ingest lock markers; others idempotent) — resumes, not restarts |
| Resize mid-layer | worker *count* is live (`scale` / the ramp patch `parallelism`); per-pod *resources* are immutable — edit `pipeline.yml` and re-submit to change them |
| Driver crash / pause | cluster suspended (self-pause or `pause`); `resume` continues; a dead-pid `running` run is flagged stalled |

## Design decisions, in brief

| Decision | Rationale | Alternative rejected |
|---|---|---|
| Stock Indexed Jobs | k8s gives parallelism, per-index retry, sticky completion for free | Redis/RQ or SQS + standing workers (a broker to run, scale, recover) |
| Indexable Feistel shuffle | same grid-wide scatter as a materialized shuffle, computed per-index | a `permutation(N)` array shipped via a queue |
| No completion gate | an upstream may be satisfied by any means; a missing one fails loudly | the orchestrator deciding "done" (brittle, blocks re-runs) |
| Foreground driver | simplest thing that delivers pause/resume | a detached background driver (added complexity for no gain) |
| Per-chunk lock for ingest only | ingest is non-idempotent; the rest are overwrites | locking everything (needless Bigtable cost) |
| Poll, don't watch | completion is a sticky terminal condition; lag is nil against minute/hour layers; the driver already needs a timer for ramp + cost | a k8s watch stream (lossy events, extra machinery) |
| Recorded, not derived, cost | pod runtimes vanish with GC; record quantities, price at read time | trusting Autopilot's rounding / deriving from job wall-time |
| Backend-agnostic SQLAlchemy | local SQLite for dev, one config line to a shared server | a database hard-wired to one backend |
| Explicit Autopilot requests | Autopilot bills requests; declare them so cost records are true | letting Autopilot round requests up silently |
