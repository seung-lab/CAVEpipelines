"""Each pipeline workload as a Stage: its DAG deps + setup + layer range + output dir.

The orchestrator (in `ops`) depends on the `Stage` Protocol; our workloads subclass
`BaseStage` for defaults, and a future/external pipeline can satisfy the Protocol without
inheriting it. Rendering (container command, job labels) stays in `manifest`; this module
imports only `util`/`note`, so `manifest` stays free of a `manifest -> stages` import cycle.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import note, util

INGEST_SETUP = ["python", "-m", "pychunkedgraph.pipeline.ingest.setup"]
MESHING_SETUP = ["python", "-m", "pychunkedgraph.pipeline.meshing.setup"]
MIGRATE_SETUP = ["python", "-m", "pychunkedgraph.pipeline.migrate.setup"]
L2CACHE_SETUP = ["python", "-m", "pcgl2cache.pipeline.l2cache.setup"]


@runtime_checkable
class Stage(Protocol):
    """The contract the orchestrator depends on."""

    name: str
    deps: frozenset[str]
    build: bool
    optional: bool

    def applies(self, cfg) -> bool: ...
    def setup(self, cfg, exist_ok: bool = False) -> None: ...
    def top_layer(self, cfg, counts) -> int: ...
    def output_dir(self, cfg) -> str | None: ...


class BaseStage:
    """Defaults our stages reuse: no deps, a required part of a build, always applies, root
    top layer, no setup, no output dir."""

    deps: frozenset[str] = frozenset()
    build: bool = True
    optional: bool = False  # an optional stage joins a build only when configured

    def applies(self, cfg) -> bool:
        return True

    def setup(self, cfg, exist_ok: bool = False) -> None:
        return None

    def top_layer(self, cfg, counts) -> int:
        return max(counts)  # root

    def output_dir(self, cfg) -> str | None:
        return None


class Ingest(BaseStage):
    name = "ingest"

    def setup(self, cfg, exist_ok: bool = False) -> None:
        note(f"setup ({self.name})")
        argv = INGEST_SETUP + [cfg.graph_id]
        if cfg.dataset.get("ingest_config", {}).get("AGGLOMERATION"):
            argv.append("--raw")  # an agglomeration source implies the raw input path
        if exist_ok:
            argv.append("--exist-ok")
        note(util.run_with_dataset(cfg, "setup", argv) or "setup done")
        util.invalidate_layer_counts(
            cfg
        )  # graph may have changed; recompute on next read


class Meshing(BaseStage):
    name = "meshing"
    deps = frozenset({"ingest"})

    def applies(self, cfg) -> bool:
        return "mesh_config" in cfg.dataset

    def setup(self, cfg, exist_ok: bool = False) -> None:
        note("mesh-meta: writing mesh metadata")
        argv = MESHING_SETUP + [cfg.graph_id]
        note(util.run_with_dataset(cfg, "mesh-meta", argv) or "mesh metadata written")

    def top_layer(self, cfg, counts) -> int:
        max_layer = (cfg.dataset.get("mesh_config") or {}).get("max_layer")
        if max_layer is None:
            raise SystemExit("mesh_config: max_layer is required for meshing")
        try:
            return min(int(max_layer), max(counts))
        except (TypeError, ValueError):
            raise SystemExit(f"mesh_config.max_layer must be an int, got {max_layer!r}")

    def output_dir(self, cfg) -> str | None:
        ws = (cfg.dataset.get("data_source") or {}).get("WATERSHED")
        mdir = (cfg.dataset.get("mesh_config") or {}).get("dir")
        # shards land in initial/; <dir> itself holds an info file from mesh-meta
        return f"{ws.rstrip('/')}/{mdir}/initial" if ws and mdir else None


class L2Cache(BaseStage):
    name = "l2cache"
    deps = frozenset({"ingest"})
    optional = True  # only part of a build when the dataset declares l2cache_config

    def applies(self, cfg) -> bool:
        return "l2cache_config" in cfg.dataset

    def setup(self, cfg, exist_ok: bool = False) -> None:
        lc = cfg.dataset.get("l2cache_config") or {}
        table, cv = lc.get("table_id"), lc.get("cv_path")
        if not (table and cv):
            raise SystemExit("l2cache_config needs `table_id` and `cv_path`")
        argv = L2CACHE_SETUP + [table, cfg.graph_id, cv]
        cave = (lc.get("cave_host"), lc.get("cave_dataset"), lc.get("cave_service"))
        if any(cave) and not all(cave):
            raise SystemExit(
                "l2cache_config cave_host/cave_dataset/cave_service go together"
            )
        if all(cave):  # register the graph with CAVE auth so its graphene CV is readable
            argv += [
                "--cave-host",
                cave[0],
                "--cave-dataset",
                cave[1],
                "--cave-service",
                cave[2],
            ]
        note(util.run_workload(cfg, "setup", argv) or "l2cache setup done")

    def top_layer(self, cfg, counts) -> int:
        return 2


class CaveRegister(BaseStage):
    name = "cave_register"
    deps = frozenset({"ingest", "meshing"})
    optional = True  # joins a build only when the dataset declares cave_config

    def applies(self, cfg) -> bool:
        return "cave_config" in cfg.dataset

    def setup(self, cfg, secrets_dir, exist_ok: bool = False) -> None:
        cc = cfg.dataset.get("cave_config") or {}
        host, dataset = cc.get("host"), cc.get("dataset")
        service = cc.get("service", "pychunkedgraph")  # the default service; rarely differs
        if not (host and dataset):
            note("cave-register: cave_config needs host, dataset; skipping")
            return
        url = (
            f"{host.rstrip('/')}/sticky_auth/api/v1/service/{service}"
            f"/table/{cfg.graph_id}/dataset/{dataset}"
        )
        # best-effort: post and log the response; never block the deploy (on failure,
        # register manually). token from the deploy secrets dir (the cave-secret file).
        try:
            secret = Path(secrets_dir) / cfg.secret_files["cave-secret.json"]
            token = json.loads(secret.read_text())["token"]
            req = urllib.request.Request(
                url, method="POST", headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                note(f"cave-register: {resp.status} {resp.read().decode(errors='replace')}")
        except urllib.error.HTTPError as exc:
            note(f"cave-register: {exc.code} {exc.read().decode(errors='replace')}")
        except Exception as exc:  # noqa: BLE001 - best-effort, never block the deploy
            note(f"cave-register: {exc}")

    def top_layer(self, cfg, counts) -> int:
        return 1  # not a build-DAG stage; register_cave() invokes setup() directly


class Migrate(BaseStage):
    name = "migrate"
    build = False

    def setup(self, cfg, exist_ok: bool = False) -> None:
        note(f"setup ({self.name})")  # migrate reads everything from Bigtable
        argv = MIGRATE_SETUP + [cfg.graph_id]
        note(util.run_workload(cfg, "setup", argv) or "setup done")
        util.invalidate_layer_counts(cfg)


class MigrateCleanup(Migrate):
    name = "migrate_cleanup"
    deps = frozenset({"migrate"})


STAGES: dict[str, Stage] = {
    s.name: s for s in (Ingest(), Meshing(), L2Cache(), Migrate(), MigrateCleanup())
}


def build_set(cfg) -> set:
    """Stages a full build runs: required build stages always, optional ones only when the
    dataset configures them. Meshing is required; l2cache is optional (needs l2cache_config)."""
    return {
        s.name for s in STAGES.values() if s.build and (not s.optional or s.applies(cfg))
    }
