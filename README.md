> **Note:** `main` is under active rework; the previous stable pipeline is frozen on the [`legacy`](https://github.com/seung-lab/CAVEpipelines/tree/legacy) branch.

# CAVEpipelines

[![codecov](https://codecov.io/gh/seung-lab/CAVEpipelines/branch/main/graph/badge.svg)](https://codecov.io/gh/seung-lab/CAVEpipelines)

**Index:** [end-to-end flow](#the-end-to-end-flow) · [layout](#layout) · [the CLI](#the-cli) ·
[requirements](#requirements) · [1 cluster](#1-cluster--terraform) · [2 image](#2-image) ·
[3 config + deploy](#3-config--deploy) · [4 ingest](#4-ingest) · [5 meshing](#5-meshing) ·
[6 l2cache](#6-l2cache) · [7 migration](#7-migration) · [how a layer behaves](#how-a-layer-behaves) ·
[costs](#cost-effective-compute) · [debugging](#debugging-failures) ·
[chunk distribution](#how-chunks-are-distributed-toy-example) · [teardown](#teardown) ·
[reference](#reference)

Runs the connectomics pipelines — **chunkedgraph ingest**, **meshing**,
**l2cache** — on **GKE Autopilot** as stock Kubernetes **Indexed Jobs**: no
Redis, RQ, SQS, or long-running workers.

All three are the same shape: a layer's chunks form an `X·Y·Z` grid; one Indexed
Job per layer hands each pod a scattered slice of that grid; each chunk is
processed under a per-chunk lock (ingest) or idempotently (meshing/l2cache). Spot
pods absorb preemption; a cold Bigtable is ramped into gradually.

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
4. **Spend** — every watch tick records pod runtimes locally;
   `pipeline costs <layer>` prices them ([Cost-effective compute](#cost-effective-compute)).
5. **Teardown** — `pipeline undeploy`, then `terraform destroy` ([Teardown](#teardown)).

## Layout

| Path | What |
|---|---|
| [pipeline/](pipeline/) | the **`pipeline` CLI** (Python, kubernetes client) — the operator entry point |
| [config/](config/) | all run configs — `-c` is the path to a pipeline yaml, its `dataset:` key names the dataset yaml relative to it; any number of projects side by side — see [config/README.md](config/README.md) |
| [secrets/](secrets/) | local secret files (gitignored); `secret_files:` in `pipeline.yml` picks which to load |
| [terraform/](terraform/) | the GKE Autopilot cluster + Workload-Identity service account |
| [helm/](helm/) | the helm chart for static infra (service account, ConfigMaps, an optional spot util pod); the `pipeline` CLI renders its values and runs helm |

**Single-source config.** `pipeline.yml` holds everything except the graph
definition (`dataset.yml` — the same yaml the graph was always configured with,
read only by `setup`; workers read graph meta from Bigtable). The `pipeline` CLI
feeds both to helm and the Jobs; the Bigtable project/instance, image, and service
account each appear once.

## The CLI

| command | does |
|---|---|
| `pipeline deploy` | `helm upgrade --install` the static infra + create the Secret from `secrets/` (`--setup` also runs `setup`; `--submit-l2` also submits layer 2; `--oneshot` = build DAG, prompts for a start/end depth (or `--from`/`--to`), same-depth stages in parallel; `--all-layers` = the configured workload's layers; `--sequential` serializes parallel stages; `--yes` skips the prompt + confirmation) |
| `pipeline setup` | create the graph table + meta — a one-shot pod mounting the graph's own dataset ConfigMap; raw agglomeration input enabled automatically when the dataset has `ingest_config.AGGLOMERATION` |
| `pipeline mesh-meta` | write the graph's mesh metadata once (meshing only, after ingest reaches root) |
| `pipeline submit <layer>` | submit (or re-submit) the layer's Indexed Job; ramp parallelism (refuses if the layer below is not 100% — `--force` to override) |
| `pipeline scale <layer> <n>` | resize the running layer's workers (set Job parallelism) anytime |
| `pipeline sample <layer> <n>` | run N scattered chunks (one per pod) to size CPU/memory before a full run |
| `pipeline status` | live table of **all** layers (a-priori chunk counts; unsubmitted shown pending): done, total, %, active/ready, retries (transient attempts), failed (dead tasks), elapsed, cost (estimate) + nodes; stays up across layers until Ctrl-C |
| `pipeline inspect <layer> [index]` | list a layer's failed indexes; with an index, that pod's log |
| `pipeline pods <layer>` | the layer's pods: index, phase, node, scheduling reason |
| `pipeline events <layer>` | the layer's Job + pod events (scheduling, scale-up, failures) |
| `pipeline top <layer>` | live per-pod usage in cores/GiB vs the request, by task index (needs metrics-server) |
| `pipeline costs <layer>` | the layer's recorded Spot spend so far (from the local cost db; estimate) |
| `pipeline delete <layer>` | delete the layer's Job and pods |
| `pipeline reset` | forget the session config (the next `-c` selects a new one) |
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

Creates a **GKE Autopilot** cluster (Google manages nodes; spot VMs and machine
class are chosen per Job via nodeSelectors) and one Workload-Identity service
account. No node pools, no Redis, default network. The cluster starts at zero nodes and
scales back to zero when idle (Autopilot's autoscaler is the aggressive
`OPTIMIZE_UTILIZATION` profile); a persistent util pod, if enabled, holds one small spot
node between layers, running the warm cg-cache server.

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
# secret_files: provides google-secret.json — all Google clients authenticate with it

pipeline deploy --setup --submit-l2   # deploy + setup + submit layer 2, in one step
```

**Authentication.** All Google clients — Bigtable and bucket access (CloudVolume) —
authenticate with the service-account key from `secret_files`:
`GOOGLE_APPLICATION_CREDENTIALS` points at the mounted `google-secret.json`.

**Secrets.** `secret_files` maps `{container_filename: local_path}` under `secrets/`;
`deploy` bundles the files into one helm-managed Secret mounted read-only at
`/root/.cloudvolume/secrets/` in every pod. Container names may differ from local
ones, so one `secrets/` directory serves multiple projects.

**Setup pods.** `setup`/`mesh-meta` run as one-shot pods mounting the graph's
dataset ConfigMap (`pcg-dataset-<graph>`); per-graph maps coexist, and a fresh
mount avoids kubelet ConfigMap sync lag. `submit` reads graph meta through a small
spot util pod that holds a warm cg-cache server, kept alive between layers
(`persistent_util: true`), or a one-shot pod per probe (`false`), letting the cluster
idle at zero nodes.

Keep any number of projects side by side (e.g. `config/my_project.yml` paired to
its dataset via the `dataset:` key); `-c` is the path to one (relative or absolute,
tab-completion friendly), defaulting to `config/pipeline.yml`. The first `-c`
selects the **session config**: every later command uses it without `-c` and logs
it, a different `-c` is refused, and `pipeline reset` clears the selection. `-g`
overrides `graph_id` per invocation
(test iterations without editing files).

Two whole-pipeline flags, mutually exclusive:

- **`pipeline deploy --oneshot`** — runs the build DAG (ingest → meshing/l2cache). The build
  always includes **ingest** and **meshing** (every build meshes); **l2cache** joins only when
  the dataset declares `l2cache_config`. It
  **displays the DAG and prompts for a start/end depth** (default full, top→bottom);
  `--from N`/`--to N` set the depths non-interactively. Stages at the same depth run **in
  parallel** (`--sequential` to serialize). The DAG only **orders** the selected stages —
  upstream deps outside the selection are assumed already satisfied (by any means), so e.g.
  `--from 1` runs meshing ∥ l2cache when ingest is already built. Refused only for
  `workload: migrate`/`migrate_cleanup`. Per-stage sizing comes from `job.workloads`.
- **`pipeline deploy --all-layers`** — runs **the configured `workload:`** (its setup +
  every layer) and nothing else. E.g. `workload: meshing` → `mesh-meta` + meshing
  L2→max_layer; its upstream (ingest) is the operator's responsibility, not verified.

Both print the plan (the DAG batches, per-stage layer requests) and ask for confirmation
(`--yes` to skip); re-running resumes — an existing graph skips setup, finished layers skip; a layer
with dead tasks stops the run. Every command also logs the active workload at start.

(`deploy`/`setup`/`submit` remain separate commands; the flags chain them for a
first run. `--submit-l2` requires `--setup`.)

`deploy` is idempotent — re-run it after editing `pipeline.yml`.

## 4. Ingest

```shell
pipeline submit 2     # layer 2
pipeline status       # watch; the layer reaches 100% when done
pipeline submit 3     # next layer, and so on up to the root
```

Each `submit` (identical flow for every workload):

- **Sizes the Job** — reads N (chunks in the layer) from `cg.meta`, sets
  `completions = ceil(N / batch_size)`, applies the Indexed Job. Each chunk is built under a
  per-chunk lock (one writer per chunk).
- **Ramps parallelism** — geometric: `job.ramp.start` → ×`factor` every `period`s → up to
  `job.ramp.max`, so a cold Bigtable autoscales/splits before full load.
- **Tune per layer** in `pipeline.yml` — `job.memory`, `compute_class`, `batch_size`, the ramp.
  To size CPU/memory first: `pipeline sample <layer> <n>` runs n chunks one-per-pod, then
  `pipeline top <layer>` shows per-pod usage.

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

`mesh-meta` writes the graph's mesh metadata (mesh dir, sharded spec, draco grid, and the
bigtable mesh block). It derives `initial_ts` from a root sampled before any edit, so mesh
**before editing the graph** (it picks a pre-edit timestamp automatically). Meshing is
idempotent (re-meshing overwrites shards) and needs no per-chunk lock.

Choosing `mesh_config` values:
- **`mip` / `chunk_size`** — mesh at the `mip` the segmentation is downsampled to. `chunk_size` is
  the ChunkedGraph `CHUNK_SIZE` divided **per axis** by that mip's downsample factor
  (`resolution(mip) / resolution(0)` from the watershed scales) — for anisotropic EM that is
  usually X and Y, with Z unchanged; it is not one fixed axis.
- **`max_error`** — marching-cubes simplification error, typically the largest dimension of the
  resolution at the chosen mip.
- **`max_layer`** — highest layer to stitch to. Stitching memory grows ~8× per layer (a single
  L7 chunk can need 30–50 GB), so stop around L6–L7 and give the high layers a large
  `job.memory` / `compute_class` (tune per layer with `sample` + `top`).

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

`migrate_cleanup` is the same worker as `migrate`, run with `--clean`. Upgrade tuning comes from the
`env:` block in `pipeline.yml` (`TASK_SIZE`, `PROCESS_MULTIPLIER`, `PARENT_CACHE_LIMIT`,
`MAX_CHEBYSHEV_DISTANCE` — any env the upgrade code reads). `setup` caches `earliest_ts` into graph
meta so workers read it once instead of hammering a single Bigtable row.

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

Costs are **recorded, not derived**: whenever the CLI watches the cluster (each `pipeline status`
tick, `submit`'s ramp, `pipeline costs`), it samples every pod's runtime into the cost database —
a SQLAlchemy URL (`database.cost`, default a local SQLite file under `costs/`), so the same record
lives on a shared server by changing one config line. Rows are scoped by graph and workload, and
priced at read time from [rates.csv](pipeline/rates.csv). Kubernetes
deletes finished pods (their runtimes with them), so the recorded history is the only number that
survives a run; completions that finished unwatched are backfilled from the mean observed runtime
(flagged in the printed `basis`), and the $0.10/hr cluster fee is charged once over the union of
job wall-time — never per layer. Jobs and pods that vanish between samples (deleted, replaced,
GC'd) are closed out at their last sighting — nothing accrues after termination, nothing is
billed twice. Keep `pipeline status` running during a layer for exact per-pod accounting;
`pipeline costs <layer>` reports the layer's recorded Spot spend.

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

## How chunks are distributed (toy example)

An **8×6×3 grid = 144 chunks**, `batch_size 15` → **10 batches**, run at
**parallelism 10** (one worker per batch). Each worker's batch is a contiguous
slice of a fixed-seed permutation, so its chunks scatter across the whole volume
rather than a solid block — concurrent workers hit different Bigtable row-key ranges,
not one hot tablet.

Worker 0's 15 chunks span the grid (not a corner):

```
(0,5,1) (0,3,1) (5,2,0) (1,0,2) (2,0,0) (1,1,1) (2,3,1) (7,3,0)
(7,1,0) (5,0,0) (5,4,1) (6,3,2) (2,2,1) (3,2,1) (3,0,1)
```

Which worker (`w0`–`w9`) owns each chunk, z=0 plane — neighbours go to different
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

Row-major order would instead hand worker 0 a solid corner
(`(0,0,0),(0,0,1),(0,0,2),(0,1,0)…`), clustering all 10 workers at the low end of the
key space at once. The permutation is a bijection (every chunk in exactly one batch),
deterministic per seed (a retried index re-runs the same chunks), and invertible (a
failed index maps back to its coords).

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

- [config/README.md](config/README.md) — the dataset / `mesh_config` field reference.
