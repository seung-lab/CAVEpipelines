# Pipeline config

The `pipeline` CLI reads its config from this directory — two files:

| File | What | Read by |
|---|---|---|
| `pipeline.yml` | run-wide settings: cluster, images, identity, Bigtable, job sizing (incl. ramp), env | the CLI (helm + every Job) |
| `dataset.yml` | the graph definition (sources, chunk layout, mesh params) | `setup` / `mesh-meta` only |

Copy the templates (the copies are gitignored):

```shell
cp pipeline-example.yml pipeline.yml   # the default; or any name, e.g. pinky.yml
cp dataset-example.yml  dataset.yml    # non-default names link via `dataset:` in the pipeline yaml
```

Everything here except the examples and this README is gitignored, so any number of projects
live side by side (`pinky.yml` + `pinky/dataset.yml`, …). Pick one with `pipeline -c <name>`;
`-g` overrides its `graph_id` per run (test iterations like `…_test1` without editing files).
The layer-counts cache `.layer_counts.json` lives here too, keyed by graph id.

Workers read graph meta from Bigtable at run time, so only `setup` (and `mesh-meta`) read
`dataset.yml`. `PROJECT`/`INSTANCE` under its `backend_client.CONFIG` are filled automatically
from `pipeline.yml`'s `bigtable:`.

## `pipeline.yml`

| Key | What |
|---|---|
| `namespace` | k8s namespace for all pods |
| `graph_id` | the ChunkedGraph id (table name); `-g` overrides it per invocation |
| `dataset` | dataset yaml file in this directory (default `dataset.yml`; subdirs ok) |
| `workload` | `ingest` \| `l2cache` \| `meshing` \| `migrate` \| `migrate_cleanup` — one at a time |
| `persistent_util` | keep the spot util pod alive between layers; `false` = one-shot pod (idle 0 nodes) |
| `secret_files` | `{container_filename: local_path under ./secrets}`; `{}` = Workload Identity only |
| `images.pcg` / `images.l2cache` | container image per workload |
| `workload_identity.service_account` | KSA bound to the worker GSA |
| `workload_identity.gsa_email` | the worker GSA (terraform output `worker_service_account`) |
| `bigtable.project` / `bigtable.instance` | Bigtable target; also injected into `dataset.yml`'s `backend_client` |
| `region` | GKE region — selects the cost rate row in `rates.csv` (required for cost estimates) |
| `zone` | optional: pin worker pods to one zone (e.g. Bigtable's) for lower latency — trades Spot capacity |
| `job.*` | per-layer sizing: `perm_seed`, `batch_size`, `parallel` (parent-chunk builds fan out over every core; `false` = sequential, for debugging), `cpu`, `memory`, `compute_class`, `backoff_limit_per_index`, `max_failed_indexes` |
| `job.ramp.*` | parallelism ramp: `start`, `factor`, `period` (s), `max` |
| `env` | extra env on every worker + setup pod (below) |
| `commands` | container command for non-built-in workloads (only `l2cache` today) |

### `env`

Arbitrary env injected into every worker and setup pod, on top of the built-in `PCG_*`
plumbing — whatever the workload's code reads from the environment:

- migration tuning (`migrate` / `migrate_cleanup`): `TASK_SIZE`, `PROCESS_MULTIPLIER`, `PARENT_CACHE_LIMIT`, `MAX_CHEBYSHEV_DISTANCE`.

`BIGTABLE_PROJECT` / `BIGTABLE_INSTANCE` are set automatically on every pod from `bigtable:` —
don't list them here. Unset keys are skipped (a per-pod env entry overrides the ConfigMap).

### Cost

The CLI records every pod's runtime into a local SQLite file (`costs/<graph_id>.<workload>.db`,
gitignored) whenever it watches the cluster — each `pipeline status` tick, `submit`'s ramp, and
`pipeline costs`. Dollars are computed at read time as recorded requests x runtime x the
(`region`, compute class) rate from `rates.csv` (refreshed by the `update-rates` workflow), so a
rates refresh re-prices history. Records survive pod garbage collection; completions that finished
unwatched are backfilled from the mean observed runtime (the printed `basis` says so), and the
cluster fee is charged once over the union of job wall-time — never per layer. It is an estimate,
never the invoice, and never fatal. Needs `region:` set; node-based compute classes
(`Performance` / GPU) bill per VM and are not priced. Leave `pipeline status` running while a
layer runs for exact accounting.

### Tuning per dataset

`batch_size`, `job.ramp.*`, and `job.cpu`/`memory`/`compute_class` are throughput knobs — set them to
your graph size and how fast you want each layer to run, and revise them per layer:

- **`batch_size`** (chunks per pod) sets the task count `ceil(chunks / batch_size)`, which is also
  the worker ceiling. Smaller = more tasks = finer parallelism + retry/inspection granularity, but
  more pods; larger = fewer, fatter pods.
- **`job.ramp.*`** grows the worker count up to `max` — capped at the task count, so a small layer uses
  fewer workers no matter how high `max` is.
- **`job.memory` / `compute_class`** — raise for heavy upper layers (stitching, meshing).

`pipeline submit` prints `chunks / batch = tasks; workers …`; track progress with `pipeline status`.

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
Leave as is — bigtable is the only supported backend.

### `mesh_config` (meshing only)
- `dir` — mesh directory inside the watershed CloudVolume (e.g. `graphene_meshes`).
- `mip` — mip level to mesh at.
- `max_layer` — highest layer to mesh and stitch to.
- `max_error` — marching-cubes simplification error.
- `chunk_size` — mesh chunk size `[x, y, z]` (mip-adjusted).
- `minishard_bits` — sharded-mesh minishard bits per layer, `{layer: bits}`.
- `dynamic_mesh_dir` — directory for post-edit dynamic meshes. Use `dynamic` on `main`; on `pcgv3` the graph id is appended automatically (default `dynamic_<graph_id>`).
