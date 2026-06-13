# Pipeline config

**Index:** [pipeline.yml](#pipelineyml) — [env](#env) · [cost](#cost) · [tuning](#tuning-per-dataset) ·
[resource curves](#how-per-layer-resources-scale) | [dataset.yml](#datasetyml) —
[data_source](#data_source) · [graph_config](#graph_config) · [ingest_config](#ingest_config-optional) ·
[backend_client](#backend_client) · [mesh_config](#mesh_config-meshing-only)

`config/` is the conventional home for run configs — two files per project:

| File | What | Read by |
|---|---|---|
| `pipeline.yml` | run-wide settings: cluster, images, identity, Bigtable, job sizing (incl. ramp), env | the `pipeline` CLI (feeds helm + every Job) |
| `dataset.yml` | the graph definition (sources, chunk layout, mesh params) | `setup` / `mesh-meta` only |

Copy the templates (the copies are gitignored):

```shell
cp pipeline-example.yml pipeline.yml   # the default; or any name, e.g. my_project.yml
cp dataset-example.yml  dataset.yml    # non-default names link via `dataset:` in the pipeline yaml
```

Everything here except the examples and this README is gitignored, so any number of projects
live side by side (`my_project.yml` + `my_project/dataset.yml`, …). `pipeline -c <path>` points at
one (relative or absolute; `config/pipeline.yml` is the default when omitted). The first `-c`
selects the session config, stored in `.current` here: later commands use it without `-c` and log
it on every invocation, and a different `-c` is refused until `pipeline reset`. `-g` overrides its
`graph_id` per run (test iterations like `…_test1` without editing files). The layer-counts
cache `.layer_counts.json` lives beside the pipeline yaml, keyed by graph id.

Workers read graph meta from Bigtable at run time, so only `setup` (and `mesh-meta`) read
`dataset.yml`. `PROJECT`/`INSTANCE` under its `backend_client.CONFIG` are filled automatically
from `pipeline.yml`'s `bigtable:`.

## `pipeline.yml`

| Key | What |
|---|---|
| `namespace` | k8s namespace for all pods |
| `graph_id` | the ChunkedGraph id (table name); `-g` overrides it per invocation |
| `dataset` | dataset yaml, relative to the pipeline yaml's directory (default `dataset.yml`; subdirs ok) |
| `workload` | `ingest` \| `l2cache` \| `meshing` \| `migrate` \| `migrate_cleanup` — one at a time (ignored by `deploy --oneshot`, which runs ingest then meshing) |
| `persistent_util` | keep the spot util pod alive between layers; `false` = one-shot pod (idle 0 nodes) |
| `secret_files` | `{container_filename: local_path under ./secrets}`; must include `google-secret.json` — all Google clients authenticate with it |
| `secret_name` | name of the k8s Secret built from `secret_files` (default `cloud-volume-secrets`) |
| `images.pcg` / `images.l2cache` | container image per workload |
| `workload_identity.service_account` | KSA bound to the worker GSA |
| `workload_identity.gsa_email` | the worker GSA (terraform output `worker_service_account`) |
| `bigtable.project` / `bigtable.instance` | Bigtable target; also injected into `dataset.yml`'s `backend_client` |
| `region` | GKE region — selects the cost rate row in [rates.csv](../pipeline/rates.csv) (required for cost estimates) |
| `zone` | optional: pin worker pods to one zone (e.g. Bigtable's) for lower latency — trades Spot capacity |
| `job.*` | sizing: `perm_seed`, `batch_size`, `parallel` (parent-chunk builds fan out over every core; `false` = sequential, for debugging), `cpu`, `memory`, `compute_class`, `task_retries` (per-task retry budget), `max_failed_tasks` (dead tasks tolerated before the layer aborts — bounds retry spend; auto-clamped to the layer's task count) |
| `job.resources.*` | optional per-layer cpu/memory curves + per-layer overrides — see ["How per-layer resources scale"](#how-per-layer-resources-scale) |
| `job.workloads.<name>` | per-workload deep-overrides of `job` (own `batch_size`, curves, ramp) |
| `job.ramp.*` | parallelism ramp: `start`, `factor`, `period` (s), `max` |
| `env` | extra env on every worker + setup pod (below) |
| `commands` | container command for non-built-in workloads (only `l2cache` today) |

### `env`

Arbitrary env injected into every worker and setup pod, on top of the built-in `PCG_*`
plumbing — whatever the workload's code reads from the environment:

- migration tuning (`migrate` / `migrate_cleanup`): `TASK_SIZE`, `PROCESS_MULTIPLIER`, `PARENT_CACHE_LIMIT`, `MAX_CHEBYSHEV_DISTANCE`.

`BIGTABLE_PROJECT` / `BIGTABLE_INSTANCE` are set automatically on every pod from `bigtable:` —
do not list them here. Unset keys are skipped (a per-pod env entry overrides the ConfigMap).

### Cost

The CLI records every pod's runtime into a local SQLite file (`costs/<graph_id>.<workload>.db`,
gitignored) whenever it watches the cluster — each `pipeline status` tick, `submit`'s ramp, and
`pipeline costs`. Dollars are computed at read time as recorded requests x runtime x the
(`region`, compute class) rate from [rates.csv](../pipeline/rates.csv) (refreshed by the
[update-rates](../.github/workflows/update-rates.yml) workflow), so a
rates refresh re-prices history. Records survive pod garbage collection; completions that finished
unwatched are backfilled from the mean observed runtime (the printed `basis` says so), and the
cluster fee is charged once over the union of job wall-time — never per layer. A Job or pod that
vanishes between samples (deleted, replaced, GC'd) is closed out at its last sighting — cost stops
accruing the moment it stops running, and a closed-out pod's completion is never billed a second
time by the backfill. It is an estimate,
never the invoice, and never fatal. Needs `region:` set; node-based compute classes
(`Performance` / GPU) bill per VM and are not priced. Leave `pipeline status` running while a
layer runs for exact accounting.

### Tuning per dataset

`batch_size`, `job.ramp.*`, and `job.cpu`/`memory`/`compute_class` are throughput knobs — size
them to the graph and the target layer throughput, and revise per layer:

- **`batch_size`** (chunks per pod) sets the task count `ceil(chunks / batch_size)`, which is also
  the worker ceiling. Smaller = more tasks = finer parallelism + retry/inspection granularity, but
  more pods; larger = fewer, larger pods.
- **`job.ramp.*`** grows the worker count up to `max` — capped at the task count, so a small layer uses
  fewer workers no matter how high `max` is.
- **`job.memory` / `compute_class`** — raise for heavy upper layers (stitching, meshing).

`pipeline submit` prints `chunks / batch = tasks; workers …`; track progress with `pipeline status`.

### How per-layer resources scale

Upper layers do heavier per-chunk work; `job.resources` declares requests as a curve instead of
one flat size — per dimension, `value(L) = min(base × factor^(L−2) + add, max)` (layer 2 is the
base; `max: 0` = uncapped; a dimension without a curve falls back to the flat `job.cpu` /
`job.memory`). `overrides` pins exact values for layers that break the curve. Every value is
operator-declared — nothing is assumed.

With `cpu: base 1, factor 2, max 28` and `memory: base 1, factor 2, add 1, max 33`:

| layer | 2 | 3 | 4 | 5 | 6 | 7+ |
|---|---|---|---|---|---|---|
| vCPU | 1 | 2 | 4 | 8 | 16 | 28 (capped) |
| GiB | 2 | 3 | 5 | 9 | 17 | 33 (capped) |

Each layer is then snapped to the **cheapest valid Autopilot request**: memory is raised to the
1 GiB/vCPU billing floor and cpu to the 6.5 GiB/vCPU ceiling's implied minimum (Autopilot would
round both up silently and bill the result — explicit keeps cost records true), off-step cpu
(0.25-vCPU grid) warns, and anything past the general-purpose ceiling (30 vCPU / 110 GiB) refuses
with a pointer to `compute_class` or an override. A curve can therefore never silently land on a
pricier bill than declared.

## `dataset.yml`

The graph definition — the same yaml the graph was always configured with.

### `data_source`
- `EDGES` — path to edge files.
- `COMPONENTS` — path to component files.
- `WATERSHED` — path to the flat segmentation the graph is built on; must have an `info` file. Also read by meshing.

Paths use any protocol supported by [cloud-files](https://github.com/seung-lab/cloud-files/) / [cloud-volume](https://github.com/seung-lab/cloud-volume/).

### `graph_config`
- `CHUNK_SIZE` — atomic chunk size `[x, y, z]`.
- `FANOUT` — chunks per axis forming a parent chunk.
- `SPATIAL_BITS` — bits per axis reserved for chunk coordinates in segment IDs.
- `LAYER_ID_BITS` — bits reserved for the layer in segment IDs.

See the [graphene](https://github.com/seung-lab/cloud-volume/wiki/Graphene) wiki for details.

### `ingest_config` (optional)
- `AGGLOMERATION` — path to raw agglomeration data. Its presence automatically enables the
  raw ingest input path (`setup --raw` is implied; there is no manual flag).

### `backend_client`
Bigtable is the only supported backend; leave unchanged.

### `mesh_config` (meshing only)
- `dir` — mesh directory inside the watershed CloudVolume (e.g. `graphene_meshes`).
- `mip` — mip level to mesh at.
- `max_layer` — highest layer to mesh and stitch to.
- `max_error` — marching-cubes simplification error.
- `chunk_size` — mesh chunk size `[x, y, z]`: `graph_config.CHUNK_SIZE` divided per axis by the mip's downsample factor (usually X/Y for anisotropic EM, Z unchanged).
- `minishard_bits` — sharded-mesh minishard bits per layer, `{layer: bits}`.
- `dynamic_mesh_dir` — directory for post-edit dynamic meshes. Use `dynamic` on `main`; on `pcgv3` the graph id is appended automatically (default `dynamic_<graph_id>`).
