> **Heads up:** `main` is being actively reworked. The previous, stable pipeline is frozen on the [`legacy`](https://github.com/seung-lab/CAVEpipelines/tree/legacy) branch — use that for the existing setup.

# CAVEpipelines

Run the connectomics pipelines — **chunkedgraph ingest**, **meshing**, and
**l2cache** — on **GKE Autopilot** as stock Kubernetes **Indexed Jobs**. No Redis,
no RQ, no long-running workers, no AWS SQS.

All three are the same shape: a layer's chunks form an `X·Y·Z` grid; one Indexed
Job per layer hands each pod a scattered slice of that grid; each chunk is
processed under a per-chunk lock (ingest) or idempotently (meshing/l2cache). Spot
pods absorb preemption; a cold Bigtable is ramped into gradually.

## How it's organized

| Path | What |
|---|---|
| `pipeline/` | the **`pipeline` CLI** (Python, kubernetes client) — the only thing you run |
| `pipeline.yml` | the **only file you edit** (copy from `pipeline.yml.example`); single source of truth |
| `secrets/` | local secret files (gitignored); `secret_files:` in `pipeline.yml` picks which to load |
| `terraform/` | the GKE Autopilot cluster + Workload-Identity service account |
| `helm/` | static infra only (service account, ConfigMaps, an optional spot util pod) — driven by the CLI |

**One config, no duplication.** `pipeline.yml` holds everything; the CLI feeds it to
both helm and the Jobs. The Bigtable project/instance, image, and service account
each appear once. The dataset block stays the same yaml the graph was always
configured with — it's nested in `pipeline.yml` and only `setup` reads it (workers
read graph meta from Bigtable).

**Secrets & the util pod.** `secret_files` is a `{container_filename: local_path}` map —
`deploy` reads each local file under `secrets/` and bundles it into one k8s Secret mounted
read-only at `/root/.cloudvolume/secrets/` in every pod. The in-container name can differ
from the local one, so `secrets/` can hold many projects' creds side by side and each
`pipeline.yml` loads only what it needs (an empty map is fine — GCP auth is Workload
Identity). `setup` and `submit`'s meta-read run in the PCG image: by default a small
**spot** util pod kept alive between layers (`persistent_util: true`); set it `false` for
long ingests to use a one-shot pod instead, so the cluster idles at **zero nodes**.

**The CLI.**

| command | does |
|---|---|
| `pipeline deploy` | `helm upgrade --install` the static infra + create the Secret from `secrets/` |
| `pipeline setup` | create the graph table + meta (in the util pod, or a one-shot pod) |
| `pipeline mesh-meta` | write the graph's mesh metadata once (meshing only, after ingest reaches root) |
| `pipeline submit <layer>` | submit (or re-submit) the layer's Indexed Job; ramp parallelism |
| `pipeline scale <layer> <n>` | resize the running layer's workers (set Job parallelism) anytime |
| `pipeline sample <layer> <n>` | run N scattered chunks (one per pod) to size CPU/memory before a full run |
| `pipeline status` | live per-layer table: done, total, % (green=done, red=failed), active/ready, failed count, elapsed + nodes by type/spot |
| `pipeline inspect <layer> [index]` | list a layer's failed indexes; with an index, that pod's log |
| `pipeline pods <layer>` | the layer's pods: index, phase, node, scheduling reason |
| `pipeline events <layer>` | the layer's Job + pod events (scheduling, scale-up, failures) |
| `pipeline top <layer>` | per-pod CPU/memory usage (needs metrics-server) |
| `pipeline delete <layer>` | delete the layer's Job and pods |

**One graph, one workload at a time** — both `graph_id` and `workload`
(`ingest`/`l2cache`/`meshing`) live in `pipeline.yml`, so commands carry only a
layer. Layers are **operator-gated**: submit a layer, watch `pipeline status` until
it's complete, submit the next — nothing auto-advances (a layer's writes are
non-idempotent).

```shell
# optional: isolate in a venv (or skip these two lines to install system-wide)
python -m venv .venv
source .venv/bin/activate

pip install -e .
```

## Requirements

- gcloud SDK, Terraform (>= 1.6), Helm (>= 3.13), kubectl (>= 1.30), Python (>= 3.12)
- An existing Bigtable instance (co-locate it in the cluster region for low latency).

## 1. Cluster — `terraform`

Creates a **GKE Autopilot** cluster (Google manages nodes; spot VMs and machine
class are chosen per Job via nodeSelectors) and one Workload-Identity service
account. No node pools, no Redis, default network. The cluster starts at zero nodes and
scales back to zero when idle (Autopilot's autoscaler is the aggressive
`OPTIMIZE_UTILIZATION` profile); a persistent util pod, if enabled, holds one small spot
node between layers.

Roles you need: Kubernetes Engine Admin, Service Account Admin, Project IAM Admin.

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

The container images are pulled from Docker Hub — no build needed:

```
caveconnectome/pychunkedgraph:<tag>   # ingest + meshing
caveconnectome/pcgl2cache:<tag>       # l2cache
```

Pin the tags in `pipeline.yml` (`images:`).

## 3. Config + deploy

```shell
cp pipeline.yml.example pipeline.yml

# fill in pipeline.yml: graph_id, bigtable, image tags, gsa_email, dataset
# optional: map credential files in secret_files: (local files under ./secrets); GCP auth is Workload Identity

pipeline deploy   # static infra (helm) + secret
pipeline setup    # create the graph table + meta, once
```

`deploy` is idempotent — re-run it after editing `pipeline.yml`.

## 4. Ingest

```shell
pipeline submit 2     # layer 2
pipeline status       # watch; the layer reaches 100% when done
pipeline submit 3     # next layer, and so on up to the root
```

`submit` reads N (chunks in the layer) from `cg.meta`, sets
`completions = ceil(N / batch_size)`, applies the Indexed Job, and ramps parallelism
(geometric: `ramp.start` → ×`ramp.factor` every `ramp.period`s → up to `ramp.max`) so a cold
Bigtable autoscales/splits before full load — the same flow every workload uses. Each chunk is
built under a per-chunk lock (one writer per chunk). Tune memory, `compute_class`, batch size,
and the ramp per layer in `pipeline.yml`; to size memory/CPU first, `pipeline sample <layer>
<n>` runs n chunks one-per-pod, then `pipeline top <layer>` shows per-pod usage.

## 5. Meshing

Set `workload: meshing` and add a `mesh_config:` block to the dataset (fields in
[dataset_config.md](dataset_config.md)). Meshes are written in the [sharded
format](https://github.com/seung-lab/cloud-volume/wiki/Sharding:-Reducing-Load-on-the-Filesystem)
into the segmentation GCS bucket (the worker service account needs `storage.objectAdmin`). Run
it after ingest reaches the root layer:

```shell
pipeline mesh-meta    # one-shot: write the graph's mesh.* metadata (run once, before the first layer)
pipeline submit 2     # L2: marching cubes on each chunk
pipeline submit 3     # L3..max_layer: stitch child meshes into bigger ones, bottom-up
```

`mesh-meta` writes the graph's mesh metadata (mesh dir, sharded spec, draco grid, and the
bigtable mesh block). It derives `initial_ts` from a root sampled before any edit, so mesh
**before editing the graph** (it picks a pre-edit timestamp automatically). Meshing is
idempotent (re-meshing overwrites shards) and needs no per-chunk lock.

Choosing `mesh_config` values:
- **`mip` / `chunk_size`** — mesh at the `mip` the segmentation is downsampled to; `chunk_size`
  must match the ChunkedGraph chunk size adjusted for that mip.
- **`max_error`** — marching-cubes simplification error, typically the largest dimension of the
  resolution at the chosen mip.
- **`max_layer`** — highest layer to stitch to. Stitching memory grows ~8× per layer (a single
  L7 chunk can need 30–50 GB), so stop around L6–L7 and give the high layers a large
  `job.memory` / `compute_class` (tune per layer with `sample` + `top`).

## 6. L2cache

The L2 cache stores per-L2-ID parameters (e.g. a neuron's volume = the sum over its L2 IDs), so
neuron-level queries and post-edit recomputation stay fast — only edited chunks recompute.

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
Idempotent (overwrites), no per-chunk lock. Two full passes over all layers — an optional cleanup
that fixes corrupt nodes, then the upgrade itself — selected by `workload`:

```shell
pipeline setup                          # prep the existing table (version, column family, cache earliest_ts)

# workload: migrate_cleanup  (optional) — fix corrupt nodes, every layer (L2 -> root)
pipeline submit 2 ; pipeline submit 3 ; ... ; pipeline submit <root>

# workload: migrate — the upgrade, every layer (L2 -> root)
pipeline submit 2 ; pipeline submit 3 ; ... ; pipeline submit <root>
```

`migrate_cleanup` is the same worker run with `--clean`. Upgrade tuning comes from the `env:` block
in `pipeline.yml` (`TASK_SIZE`, `PROCESS_MULTIPLIER`, `PARENT_CACHE_LIMIT`, `MAX_CHEBYSHEV_DISTANCE` —
any env the upgrade code reads). `setup` caches `earliest_ts` into graph meta so workers read it once
instead of hammering a single Bigtable row.

## How a layer behaves

- **Spot preemption** is absorbed by the Job's pod failure policy (it doesn't spend
  the per-index retry budget); the index is retried automatically.
- **Transient failure** retries per index up to `backoff_limit_per_index`; a retried
  pod re-claims only the not-done chunks in its batch (done chunks are skipped via
  the per-chunk lock, for ingest).
- **Fatal chunk** (worker exit 42) fails just that index (`FailIndex`) without
  burning retries; `pipeline inspect <layer>` lists the failed indexes, and
  `pipeline inspect <layer> <index>` prints that pod's log (chunk coords + traceback).
- Re-running a layer (`pipeline submit` again) skips already-done chunks.
- **Resizing mid-layer**: worker *count* is live — `pipeline scale <layer> <n>` patches the Job's
  `parallelism` (the ramp does this automatically). Per-pod **resources** (`job.cpu`/`job.memory`)
  are baked into the Job's pod template and immutable once it runs; to change them, edit
  `pipeline.yml` and re-`submit` the layer (recreates the Job) — done chunks are skipped (ingest
  markers; migrate/meshing/l2cache idempotent), so it resumes rather than restarts.

## Debugging failures

When a layer shows `failed > 0` (or a red `%` — the Job gave up), trace it from the
batch index down to the offending chunk and its traceback:

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
index immediately, won't self-heal); **1** = transient (retried up to
`backoff_limit_per_index`). Spot preemptions are ignored and don't count.

Dig further — all through the one CLI (no kubectl needed):

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

A tiny layer — an **8×6×3 grid = 144 chunks**, `batch_size 15` → **10 batches**, run
at **parallelism 10** (one worker per batch). Each worker's batch is a contiguous
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

`terraform destroy` removes everything terraform created — the Autopilot cluster
(which takes the Jobs, pods, and secret with it) and the Workload-Identity service
account. Bigtable and the segmentation/mesh bucket are not terraform-managed, so
they are left intact.

## Reference

- [dataset_config.md](dataset_config.md) — the dataset / `mesh_config` field reference.
