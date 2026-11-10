---
name: pre-deploy
description: Check every prerequisite before a CAVEpipelines run touches a cluster -- a deploy, a setup, a sample or a layer submit -- for one pipeline config, as read-only commands with pass criteria. Covers the session and target, what the run could overwrite, the identity the driver and pods run as, the image, the workload's inputs and its sizing, then hands the operator the cluster-touching commands. Use before the first run of a new or copied config, after any change to its paths, graph id, project, image, key or workload, and whenever the operator asks to verify a run before it starts.
---

# Pre-deploy
[How it runs](#how-it-runs) · [1 Session](#1-is-the-session-pointed-at-this-run) · [2 Overwrites](#2-could-the-run-overwrite-anything) · [3 Identity](#3-can-the-runs-identity-do-everything-it-must) · [4 Image](#4-does-the-image-run-this-workload) · [5 Inputs](#5-are-the-workloads-inputs-right) · [6 Sizing](#6-does-one-real-chunk-fit-the-request) · [7 Handover](#7-what-does-the-operator-run)

### How it runs
- Every check here is a read; a failing row holds the run.
- Cluster-touching `pipeline` commands are the operator's: `deploy`, `setup`, `sample`, `submit`, `resize`, `apply`, `delete`, `pause`, `resume`, `undeploy`, `mesh-meta`.
- Reads and local-only commands may run freely: `status`, `top`, `failed`, `pods`, `events`, `describe`, `costs`, `image-preflight`, `reset`.
- `CFG` is the pipeline yaml, `DS` the dataset yaml its `dataset:` names, `W` its `workload:`; every value comes from those files.
- Configs are gitignored, so search them with `find config -name '*.yml' -exec grep -Hn PATTERN {} +`, never a gitignore-aware search.
- Report one table per section: check, the command's output as evidence, pass or fail.

### 1. Is the session pointed at this run?
| check | how | passes when |
|---|---|---|
| session config | `cat config/.current` | it names `CFG`, or `pipeline reset` runs first; no other run drives from this checkout |
| workload | `workload:` in `CFG` | it is the stage this run executes; `pipeline` runs one workload at a time |
| config loads | `config.load(CFG)` in the repo's venv | it prints the expected workload, image, graph and namespace |
| kube context | `kubectl config current-context`, and `kubectl config view` for its cluster | it names the cluster `gcloud container clusters list --project <project>` shows `RUNNING` |
| tools | `which gcloud kubectl helm gke-gcloud-auth-plugin` | each is on `PATH`; `deploy` needs `helm` |

### 2. Could the run overwrite anything?
| check | how | passes when |
|---|---|---|
| tables | `gcloud bigtable instances tables list --instances=<instance> --project=<project>` | the table this setup creates, `graph_id` or l2cache's `table_id`, is absent unless this resumes the same run |
| ids and paths | the config search above, for the graph id, `table_id` and every write path | only `CFG` and `DS` name them |
| write paths | `gcloud storage ls` on ingest's `EDGES` and `COMPONENTS`, meshing's mesh dirs | empty, or this graph's own earlier attempt |
| namespace Jobs | `kubectl --context <ctx> get jobs -n <namespace>` | no `<workload>-l<layer>` Job of another graph; layer Jobs never expire, task Jobs after an hour |

### 3. Can the run's identity do everything it must?
The driver and every pod act as the account in `secrets/<secret_files["google-secret.json"]>`.

| check | how | passes when |
|---|---|---|
| account | that file's `client_email` | an account of the target project |
| key | `gcloud iam service-accounts keys list --iam-account=<email> --managed-by=user` | the file's `private_key_id` is listed, not disabled or expired |
| roles | `gcloud projects get-iam-policy <project> --flatten=bindings --filter=bindings.members:serviceAccount:<email> --format='value(bindings.role)'` | `roles/container.developer`, since the driver reads and creates Jobs as it; `roles/bigtable.admin`; a write role on every write bucket |
| pod env | `kubectl get configmap pcg-env -n <namespace> -o jsonpath='{.data}'` | `BIGTABLE_PROJECT` and `BIGTABLE_INSTANCE` are `CFG`'s |
| CAVE token | `secrets/<secret_files["cave-secret.json"]>` | valid JSON with a `token`; `deploy` pushes it into Secret `cloud-volume-secrets` |

### 4. Does the image run this workload?
| check | how | passes when |
|---|---|---|
| tag | Docker Hub `v2/repositories/<repo>/tags?name=<tag>` | the exact tag is listed |
| runtime | `docker run --rm --entrypoint python <image> -c ...` importing the workload module | it imports, and `cave-pipeline`'s version and harness source match `cave_pipeline/distribution/harness.py` |
| contract | `pipeline image-preflight` | `images.pcg` passes; `images.l2cache` is outside the contract, so the runtime row is its only check |

### 5. Are the workload's inputs right?
| workload | check | passes when |
|---|---|---|
| ingest, meshing | the grid, `ceil(watershed size / CHUNK_SIZE)`, against `chunked_rg/` names and remap's `done_<level>_*` | they agree, levels `0` to layer count minus 2, and records decode at `DATA_VERSION` |
| l2cache | `cv_path` opened with the pods' token, `CLOUD_VOLUME_DIR` holding `secrets/cave-secret.json` | `graph_chunk_size` is `DS`'s `CHUNK_SIZE` and `data_dir` its `WATERSHED` |
| l2cache | the grid, `atomic_chunk_bounds(cv)`, against `.layer_counts.json` | its product is the L2 count the pipeline batches |
| l2cache | each supervoxel of one real chunk through the graph table's L2 parents | none maps to L2 id 0 |

### 6. Does one real chunk fit the request?
| check | how | passes when |
|---|---|---|
| peak | one real chunk through the worker's code, under `/usr/bin/time -v` | its resident peak and seconds are recorded |
| request | `manifest.job_spec(cfg, layer, ...)` rendered offline | memory exceeds that peak with headroom, within 6.5 GiB per vCPU |
| sample | the operator's `pipeline sample <layer> <n>`, then the Job's pods and the rows written | every pod exits 0, and the rows hold no L2 id 0 |

### 7. What does the operator run?
- Hand the operator each cluster-touching command, in order: context, reset, deploy, setup, sample, submit.
- After each, read its effect back: the table, the Job's pods, the rows written.
