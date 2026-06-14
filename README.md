> **Note:** `main` is under active rework; the previous stable pipeline is frozen on the [`legacy`](https://github.com/seung-lab/CAVEpipelines/tree/legacy) branch.

# CAVEpipelines

[![codecov](https://codecov.io/gh/seung-lab/CAVEpipelines/branch/main/graph/badge.svg)](https://codecov.io/gh/seung-lab/CAVEpipelines)

**Index:** [end-to-end flow](#the-end-to-end-flow) · [layout](#layout) · [the CLI](#the-cli) ·
[requirements](#requirements) · [1 cluster](#1-cluster--terraform) · [2 image](#2-image) ·
[3 config + deploy](#3-config--deploy) · [4 ingest](#4-ingest) · [5 meshing](#5-meshing) ·
[6 l2cache](#6-l2cache) · [7 migration](#7-migration) · [how a layer behaves](#how-a-layer-behaves) ·
[costs](#cost-effective-compute) · [debugging](#debugging-failures) ·
[chunk distribution](#how-chunks-are-distributed-toy-example) · [teardown](#teardown) ·
[reference](#reference) · [architecture](ARCHITECTURE.md)

Runs the connectomics pipelines — **chunkedgraph ingest**, **meshing**,
**l2cache** — on **GKE Autopilot** as stock Kubernetes **Indexed Jobs**: no
Redis, RQ, SQS, or long-running workers.

All three are the same shape: a layer's chunks form an `X·Y·Z` grid; one Indexed
Job per layer hands each pod a scattered slice of that grid; each chunk is
processed under a per-chunk lock (ingest) or idempotently (meshing/l2cache). Spot
pods absorb preemption; a cold Bigtable is ramped into gradually.

This README is the operator's guide (how to run it). For the design and the reasoning
behind it, see [ARCHITECTURE.md](ARCHITECTURE.md).

## The end-to-end flow

1. **Cluster** (once) — `terraform apply` creates the Autopilot cluster and the
   worker service account ([§1](#1-cluster--terraform)); the worker images come
   from Docker Hub ([§2](#2-image)).
2. **Config + deploy** — copy the two yamls in `config/`, fill in the graph,
   Bigtable, and identity, then `pipeline deploy` ([§3](#3-config--deploy)).
3. **Run** — submit each layer and watch it to completion: `pipeline submit 2`,
   `pipeline status`, next layer ([§4 ingest](#4-ingest), [§5 meshing](#5-meshing),
   [§6 l2cache](#6-l2cache), [§7 migration](#7-migration)) — or let
   `pipeline deploy --oneshot` run the whole build DAG (ingest, then meshing/l2cache).
4. **Spend** — every watch tick records pod runtimes into the cost db; `pipeline costs <layer>`
   prices them ([Cost-effective compute](#cost-effective-compute)).
5. **Teardown** — `pipeline undeploy`, then `terraform destroy` ([Teardown](#teardown)).

## Layout

| Path | What |
|---|---|
| [pipeline/](pipeline/) | the **`pipeline` CLI** (Python, kubernetes client) — the operator entry point |
| [config/](config/) | all run configs — `-c` is the path to a pipeline yaml, its `dataset:` key names the dataset yaml relative to it; any number of projects side by side — see [config/README.md](config/README.md) |
| [secrets/](secrets/) | local secret files (gitignored); `secret_files:` in `pipeline.yml` picks which to load |
| [terraform/](terraform/) | the GKE Autopilot cluster + Workload-Identity service account |
| [helm/](helm/) | the helm chart for static infra (service account, ConfigMaps, an optional spot util pod); the `pipeline` CLI renders its values and runs helm |

**Single-source config.** `pipeline.yml` holds everything except the graph definition,
which lives in `dataset.yml` (read only by `setup`; workers read graph meta from Bigtable).

## The CLI

| command | does |
|---|---|
| `pipeline deploy` | install the static infra + credentials Secret. `--setup`/`--submit-l2` chain setup + layer 2; `--oneshot` runs the build DAG (`--from`/`--to` depth, `--sequential`, `--yes`); `--all-layers` runs the configured workload — see [§3](#3-config--deploy) |
| `pipeline setup` | create the graph table + meta (one-shot pod; raw agglomeration auto-enabled from `ingest_config.AGGLOMERATION`; `--exists` skips if the graph already exists) |
| `pipeline mesh-meta` | write the graph's mesh metadata once (meshing only, after ingest reaches root) |
| `pipeline submit <layer>` | submit (or re-submit) the layer's Indexed Job; ramp parallelism (refuses if the layer below is not 100% — `--force` to override) |
| `pipeline scale <layer> <n>` | resize the running layer's workers (set Job parallelism) anytime |
| `pipeline sample <layer> <n>` | run N scattered chunks (one per pod) to size CPU/memory before a full run |
| `pipeline status` | live progress until Ctrl-C (`-o` one snapshot, `-i` interval). A recorded `--oneshot`/`--all-layers` run shows a per-stage DAG view (running stage → full table, done → one-line summary, dead driver → red warning); otherwise the configured workload's per-layer table: done, %, active, retries, failed, elapsed, cost, nodes |
| `pipeline inspect <layer> [index]` | list a layer's failed indexes; with an index, that pod's log |
| `pipeline pods <layer>` | the layer's pods: index, phase, node, scheduling reason |
| `pipeline events <layer>` | the layer's Job + pod events (scheduling, scale-up, failures) |
| `pipeline top <layer>` | live per-pod usage in cores/GiB vs the request, by task index (needs metrics-server; `-o`/`--once` for one snapshot, `-i`/`--interval` refresh seconds) |
| `pipeline costs <layer>` | the layer's recorded Spot spend for the current run (from the local cost db; estimate) |
| `pipeline delete <layer>` | delete the layer's Job and pods |
| `pipeline reset` | forget the session config (the next `-c` selects a new one) |
| `pipeline pause` | suspend every pipeline Job — pods get SIGTERM, Autopilot scales to 0, **nothing is deleted** (finished indexes are kept); the driver stops on its next poll |
| `pipeline resume` | unsuspend the run's Jobs and continue driving from where it paused (finished layers skip; the suspended layer resumes its incomplete indexes) |
| `pipeline purge` | purge all run/stage tracking, every graph (e.g. a stuck run after a crash); prompts for confirmation (`--yes` to skip); the durable cost db and the running Jobs are untouched |
| `pipeline undeploy` | delete all pipeline Jobs + the helm release (KSA, ConfigMaps, util pod, secret) + the local layer-counts cache + run state |

**One graph, one workload at a time** — both `graph_id` and `workload`
(`ingest`/`l2cache`/`meshing`) live in `pipeline.yml`, so commands carry only a
layer. Layers are **operator-gated**: submit a layer, watch `pipeline status` until
it completes, submit the next — nothing auto-advances (a layer's writes are
non-idempotent).

## Requirements

- gcloud SDK, Terraform (>= 1.6), Helm (>= 3.13), kubectl (>= 1.30), Python (>= 3.12)
- An existing Bigtable instance (co-locate it in the cluster region for low latency).

```shell
# optional: isolate in a venv (or skip these two lines to install system-wide)
python -m venv .venv
source .venv/bin/activate

pip install -e .
```

## 1. Cluster — `terraform`

Creates a **GKE Autopilot** cluster (Google manages nodes) and one Workload-Identity
service account. It scales to zero nodes when idle, so it costs nothing between runs.

Required roles: Kubernetes Engine Admin, Service Account Admin, Project IAM Admin.

**Authentication** — a temporary OAuth token (~1 h, nothing persisted to disk),
minted for the account gcloud is logged in as; use the human account holding the
roles above, not a worker service account. Re-export on expiry, or use persistent
[ADC](https://docs.cloud.google.com/docs/terraform/authentication)
(`gcloud auth application-default login`).

```shell
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)
```

```shell
# set common_name, project_id, region in terraform.tfvars
cd terraform/
terraform init
terraform apply
```

Useful outputs:

```
kubernetes_cluster_context = "gcloud container clusters get-credentials cave-pipeline --region us-east1 --project <proj>"
worker_service_account     = "cave-pipeline-worker@<proj>.iam.gserviceaccount.com"
```

Run the `kubernetes_cluster_context` command to point `kubectl` at the cluster, and
put `worker_service_account` into `pipeline.yml` (`workload_identity.gsa_email`).

## 2. Image

Images are pulled from Docker Hub; no build required:

```
caveconnectome/pychunkedgraph:<tag>   # ingest + meshing
caveconnectome/pcgl2cache:<tag>       # l2cache
```

Pin the tags in `pipeline.yml` (`images:`).

## 3. Config + deploy

```shell
cp config/pipeline-example.yml config/pipeline.yml
cp config/dataset-example.yml config/dataset.yml

# fill in pipeline.yml (graph_id, bigtable, images, gsa_email) and dataset.yml (data_source, graph_config)
# secret_files must include google-secret.json — every Google client (Bigtable, CloudVolume) authenticates with it

pipeline deploy --setup --submit-l2   # deploy infra + setup + submit layer 2, in one step
```

`deploy` installs the static infra (helm) and the credentials Secret built from
`secrets/`, mounted read-only in every pod. It is idempotent — re-run it after
editing `pipeline.yml`.

Point `-c` at a pipeline yaml (default `config/pipeline.yml`); the first `-c` becomes
the session config so later commands omit it (`pipeline reset` to switch). `-g`
overrides `graph_id` for one command. See [config/README.md](config/README.md) for
every field.

**Run a whole build** with one of two mutually-exclusive flags:

- **`pipeline deploy --oneshot`** — the build DAG (ingest → meshing/l2cache; l2cache
  only if the dataset has `l2cache_config`). Prints the DAG, prompts for a start/end
  depth (`--from N`/`--to N` to skip the prompt), and runs same-depth stages in
  parallel (`--sequential` to serialize). Deps only **order** the run — a stage
  outside the chosen range is assumed already built. Not for `migrate`.
- **`pipeline deploy --all-layers`** — the configured `workload:` only (its setup +
  every layer, L2→root).

Either drives the run **in the foreground** — keep the terminal open (or use
`tmux`/`screen`). `pipeline pause` from a second terminal suspends the Jobs and the
driver exits cleanly; `pipeline resume` continues where it left off. Re-running also
resumes: finished layers skip, a layer with dead tasks stops the run.

## 4. Ingest

```shell
pipeline submit 2     # layer 2
pipeline status       # watch; the layer reaches 100% when done
pipeline submit 3     # next layer, and so on up to the root
```

Each `submit` sizes the layer's Indexed Job from its chunk count and **ramps parallelism**
up gradually (`job.ramp.*`) so a cold Bigtable can split before full load. **Tune per layer**
in `pipeline.yml` — `job.memory`, `compute_class`, `batch_size`, the ramp; size CPU/memory
first with `pipeline sample <layer> <n>` then `pipeline top <layer>`.

## 5. Meshing

Set `workload: meshing` and add a `mesh_config:` block to the dataset (fields in
[config/README.md](config/README.md)). Meshes are written in the [sharded
format](https://github.com/seung-lab/cloud-volume/wiki/Sharding:-Reducing-Load-on-the-Filesystem)
into the segmentation GCS bucket (the worker service account needs `storage.objectAdmin`). Run
it after ingest reaches the root layer:

```shell
pipeline mesh-meta    # one-shot: write the graph's mesh.* metadata (run once, before the first layer)
pipeline submit 2     # L2: marching cubes on each chunk
pipeline submit 3     # L3..max_layer: stitch child meshes into bigger ones, bottom-up
```

Or run the whole meshing pass in one command: `pipeline deploy --all-layers` (with
`workload: meshing`) does `mesh-meta` then meshing L2→`max_layer`. Submitting a meshing
layer without mesh metadata is refused — run `mesh-meta` (or `--all-layers`) first.

`mesh-meta` writes the graph's mesh metadata once. Mesh **before editing the graph** — it
pins a pre-edit timestamp automatically. Re-meshing is idempotent (overwrites shards).

Set `mesh_config` per [config/README.md](config/README.md#mesh_config-meshing-only). One
operational caveat: stitching memory grows ~8× per layer (a single L7 chunk can need 30–50 GB),
so cap `max_layer` around L6–L7 and give the upper layers a large `job.memory` / `compute_class`
(tune with `sample` + `top`).

## 6. L2cache

The L2 cache stores per-L2-ID parameters (e.g. a neuron's volume = the sum over its L2 IDs), so
neuron-level queries and post-edit recomputation stay fast — only edited chunks recompute.

> **Pending:** the PCGL2Cache batch entrypoint does not exist yet;
> `commands.l2cache` is commented out in the example.

Set `workload: l2cache` and point `commands.l2cache` at the PCGL2Cache batch entrypoint in
`pipeline.yml`, then run the single L2 pass:

```shell
pipeline submit 2
```

L2cache is a single-layer, idempotent overwrite (no per-chunk lock) into its own Bigtable cache
table. The online L2Cache query frontend stays a normal Deployment, separate from this batch pass.

## 7. Migration

> **Safety**: migration rewrites the graph in place. Run it against a *copy* of the table first,
> verify the result, and only then migrate the production table.

Upgrade a pcgv2 graph to pcgv3 in place: recompute each chunk's cross-chunk edges, bottom-up.
Idempotent (overwrites), no per-chunk lock. Migration is **two full passes** over every layer,
in order: `migrate_cleanup` (fixes corrupt nodes) on every layer first, then `migrate` (the
upgrade). Each pass is a separate `workload` in `pipeline.yml`; within a pass, submit each layer
lowest-first and wait for completion before the next (the same operator-gated flow as ingest —
layers do not auto-advance), or run the whole pass in one command with `pipeline deploy
--all-layers`. Run the full `migrate_cleanup` pass before any `migrate` (ordering is operator-gated).

Prepare the table once:

```shell
pipeline setup   # version, column family, and cache earliest_ts into graph meta
```

**Pass 1 (required, first) — corrupt-node cleanup.** Set `workload: migrate_cleanup` in `pipeline.yml`, then:

```shell
pipeline submit 2
pipeline submit 3
# ...one per layer, lowest to root...
pipeline submit <root>
```

**Pass 2 — the upgrade.** Set `workload: migrate` in `pipeline.yml`, then repeat the same per-layer submits:

```shell
pipeline submit 2
pipeline submit 3
# ...one per layer, lowest to root...
pipeline submit <root>
```

Upgrade tuning comes from the `env:` block in `pipeline.yml` (`TASK_SIZE`, `PROCESS_MULTIPLIER`,
`PARENT_CACHE_LIMIT`, `MAX_CHEBYSHEV_DISTANCE`).

## How a layer behaves

- **Spot preemption** is absorbed by the Job's pod failure policy (it does not spend
  the per-index retry budget); the index is retried automatically.
- **Transient failure** retries per index up to `job.task_retries`; a retried
  pod re-claims only the not-done chunks in its batch (done chunks are skipped via
  the per-chunk lock, for ingest).
- **Fatal chunk** (worker exit 42) fails only that index (`FailIndex`) without
  burning retries; `pipeline inspect <layer>` lists the failed indexes, and
  `pipeline inspect <layer> <index>` prints that pod's log (chunk coords + traceback).
- Re-running a layer (`pipeline submit` again) skips already-done chunks.
- **Resizing mid-layer**: worker *count* is live — `pipeline scale <layer> <n>` patches the Job's
  `parallelism` (the ramp does this automatically). Per-pod **resources** (`job.cpu`/`job.memory`)
  are baked into the Job's pod template and immutable once it runs; to change them, edit
  `pipeline.yml` and re-`submit` the layer (recreates the Job) — done chunks are skipped (ingest
  markers; migrate/meshing/l2cache idempotent), so it resumes rather than restarts.

## Cost-effective compute

Autopilot bills pod **requests** (not usage) per second; Spot Pods are 60–91% off. The defaults
capture the main levers — operators mainly right-size requests and keep the default compute class.

- **Spot** (default) — 60–91% off; every worker Job runs on Spot.
- **Default (general-purpose) compute class** — the cheapest pod-based class; `Balanced` costs about
  45% more and `Scale-Out` about 26% more per vCPU/GiB. Leave `compute_class: ""` unless a layer
  needs the extra capacity or higher per-pod limits.
- **Right-size requests per layer** — billing follows requests. Measure with
  `pipeline sample <layer> <n>` then `pipeline top <layer>`, and either set flat
  `job.cpu`/`job.memory` or declare a per-layer curve (`job.resources`) so upper layers scale
  automatically. The CLI snaps every layer to the cheapest valid Autopilot request (≥ 250m/512Mi,
  1:1–1:6.5 cpu:mem) and refuses past the general-purpose ceiling instead of silently billing a
  pricier class — see [config/README.md](config/README.md).
- **Scale to zero between layers** — `persistent_util: false` runs setup/meta in a one-shot pod (no
  warm server), so the cluster idles at zero nodes when no Job is running (no pods = no compute cost).
- **System logs only** — the cluster ships only system logs to Cloud Logging (terraform
  `logging_config`); pod stdout stays on the kubelet, so chunk pods do not bill ~$0.50/GiB of
  log ingestion; `pipeline inspect` / `kubectl logs` still work.
- **Region** — us-central1/us-east1/us-west1 are the cheapest tier; other regions run ~10–30% more.
- **Cluster fee** — flat $0.10/hr/cluster (~$74/mo). A $74.40/mo free-tier credit covers exactly
  one Autopilot/zonal cluster **per billing account** (not per project) — if another cluster under
  the same billing account already consumes it, this cluster's fee applies in full.

Costs are **recorded** as the CLI watches the cluster (each `pipeline status` tick, `submit`'s
ramp, `pipeline costs`): it samples pod runtimes into the cost database (`database.cost`, default a
local SQLite under `costs/`; point it at a server to share), priced at read time from
[rates.csv](pipeline/rates.csv). It is an estimate — keep `pipeline status` running during a layer
for exact accounting. Each deploy is tagged with a run-id, so `pipeline costs <layer>` and the
`status` cost column report **this run's** spend — re-running the same graph starts a fresh tally
rather than summing past runs (the cost db still keeps every run).

## Debugging failures

Any command accepts `-v` — debug logging, including every kubernetes API request.

When a layer shows `failed > 0` (or a red `%` — the Job aborted), trace it from the
batch index down to the offending chunk and its traceback; the `retries` column
counts transient attempts that were retried and recovered — no action required:

```shell
pipeline status            # which layer failed? (red %, failed count)
pipeline inspect 3         # -> failed indexes: 3,40-71,90-103   (the dead batches)
pipeline inspect 3 40      # -> that batch pod's log: the failing chunk + traceback
```

A failed *index* is one batch; its pod log names the batch and the chunk that threw.
Example tail of `inspect 3 40` (the worker's own log lines):

```
layer 3 batch 40: 1000 chunks
fatal chunk 3_(46, 5, 29)
Traceback (most recent call last):
  ...
  ValueError: <the actual error>
```

The exit code classifies it: **42** = `FatalChunkError` (bad input / bug — fails the
index immediately, will not self-heal); **1** = transient (retried up to
`job.task_retries`). Spot preemptions are ignored and do not count.

Further inspection, all through the CLI (no kubectl required):

```shell
pipeline pods 3      # the layer's pods: index, phase, node, scheduling reason
pipeline events 3    # Job + pod events (scheduling, scale-up, podFailurePolicy)
pipeline top 3       # per-pod CPU/memory (metrics-server)
pipeline delete 3    # remove the Job (submit also replaces it automatically)
```

After fixing the cause, re-submit the layer — already-done chunks are skipped:

```shell
pipeline submit 3
```

## Teardown

`pipeline undeploy` removes what the CLI created in-cluster — all pipeline Jobs, the
per-graph dataset ConfigMaps, and the helm release (service account, env ConfigMap,
util pod, and the credentials Secret with it), and clears the local layer-counts
cache and the run state; the durable cost db and the cluster remain.

`terraform destroy` removes everything terraform created — the Autopilot cluster
(which takes the Jobs, pods, and secret with it) and the Workload-Identity service
account. Bigtable and the segmentation/mesh bucket are not terraform-managed, so
they are left intact.

## Reference

- [ARCHITECTURE.md](ARCHITECTURE.md) — design and rationale: how the system works and why.
- [config/README.md](config/README.md) — the dataset / `mesh_config` field reference.

## How chunks are distributed (toy example)

A layer's chunks form an `X·Y·Z` grid of `N` chunks. The pipeline builds them as one
**indexed job** of `ceil(N / batch_size)` **batches**: Kubernetes starts one worker (a
short-lived pod) per batch and stamps each with a unique number `0 … batches−1` — its
`JOB_COMPLETION_INDEX`. So 144 chunks at `batch_size 15` is 10 batches → **10 workers**,
numbered 0–9.

- **how many run at once** ramps up (`job.ramp.*`, toward `ramp.max`) but never exceeds
  the batch count — there are only that many workers.
- **each worker gets its own cpu/memory**, never shared (the layer's
  [request](config/README.md#how-per-layer-resources-scale)), so the cluster's peak draw
  is about `workers-running-at-once × per-worker request`. Upper-layer chunks are heavier
  — a parent spans ~8× the volume of its children — so `batch_size` **halves each layer
  above 2** (`batch_size // 2^(layer−2)`): fewer chunks per worker where each is heavier.

**No work-list — each worker computes its own chunks.** The job ships no coordinates. The
`seed` fixes **one shuffled ordering of all `N` chunks** — identical on every worker,
computed on demand, never materialized or queued — and worker `i` runs the `i`-th
contiguous slice of it. Reading the grid shape `(X, Y, Z)` from the graph metadata:

```
# the one global shuffle of all N = X·Y·Z chunks — position p -> the p-th chunk,
# defined for every p in [0, N), identical on every worker:
nth_chunk(p) = unravel( permute(p, N, seed), (X, Y, Z) )

# worker i runs only its own window of that one global order:
for p in [i·batch_size, (i+1)·batch_size):
    process( nth_chunk(p) )
```

Because the shuffle spans all `N`, consecutive positions land grid-wide — so worker `i`'s
window, and the windows of every worker running at once, scatter across the whole volume,
**not just within a batch**. The order depends only on the `seed`, so a retried worker
rebuilds the *same* window and no two workers overlap — no queue, no shared cursor, no
coordination.

**How the shuffle works without an array.** A 10M-entry shuffle is normally a *materialized*
permutation array (e.g. `numpy.random.permutation(N)`) that one process builds and feeds to
the workers — the job a Redis/SQS queue did. We want no array, just `permute(p)` for a single
`p`. The tool is a **format-preserving permutation**: a tiny keyed cipher that bijectively
scrambles a number *within a fixed bit-width*, so a position maps to a pseudo-random position
in the same range — by arithmetic alone, nothing stored.

It is a **balanced Feistel network** (the structure inside block ciphers): split the value
into two halves and, for a few rounds, fold one half into the other with a seed-keyed hash —

```
permute_pow2(v):                       # a bijection on [0, 2^b), b even, 2^b >= N
    L, R = high_half(v), low_half(v)
    for key in round_keys(seed):       # 4 rounds, keys derived from the seed
        L, R = R, (L XOR hash(R, key))
    return join(L, R)
```

Every round is reversible, so the whole map is a bijection — and running the rounds backwards
inverts it, mapping a chunk coordinate back to the batch that owns it (for inspecting a
specific failed chunk).

That cipher permutes a **power-of-two** range `[0, 2^b)`, but a layer has exactly `N` chunks.
So it **cycle-walks**: if a result lands `≥ N`, re-apply until it falls inside `[0, N)`.
Because `2^b` is the *smallest* power of two `≥ N`, the range is under `4·N`, so each lookup
retries only a few times on average (under 4). The net `permute(p, N, seed)` is a handful of
hashes with no state — `O(1)` time and memory, scaling to billions of chunks, and a pure
function of `(p, N, seed)`.

So this yields the **same global scatter** as materializing `numpy.random.permutation(N)` and
dealing out windows of it — every chunk in exactly one batch, spread grid-wide — but computed
index-by-index, so each worker does `O(batch_size)` work and the cluster runs no broker at all.

**Why scatter at all.** Neighbouring chunks have neighbouring Bigtable row keys, so walking
the grid in plain order points the whole active fleet at one key range at a time — a write
hotspot. Spreading every window across the volume keeps concurrent writes on distinct row
ranges.

Concretely, an **8×6×3 grid = 144 chunks**, `batch_size 15`, `seed 42` → **10 batches**
(the last holds 9). Worker 0's 15 chunks span the grid:

```
(0,5,1) (0,3,1) (5,2,0) (1,0,2) (2,0,0) (1,1,1) (2,3,1) (7,3,0)
(7,1,0) (5,0,0) (5,4,1) (6,3,2) (2,2,1) (3,2,1) (3,0,1)
```

Which worker (`w0`–`w9`) each chunk goes to, z=0 plane — neighbours land on different
workers:

```
        x=0 x=1 x=2 x=3 x=4 x=5 x=6 x=7
 y=0     w9  w4  w0  w4  w6  w0  w3  w6
 y=1     w7  w1  w2  w7  w4  w5  w7  w0
 y=2     w2  w8  w6  w5  w2  w0  w3  w4
 y=3     w9  w6  w1  w3  w1  w7  w2  w0
 y=4     w9  w4  w1  w5  w1  w3  w3  w4
 y=5     w6  w4  w6  w3  w5  w2  w9  w1
```

Plain row-major order would instead hand worker 0 a solid corner
(`(0,0,0),(0,0,1),(0,0,2),(0,1,0)…`), marching the whole fleet through neighbouring keys in
lockstep — exactly what the shuffle exists to prevent.
