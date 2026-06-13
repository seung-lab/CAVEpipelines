"""Each pipeline workload as a Stage: its DAG deps + setup + layer range + output dir.

The orchestrator (in `ops`) depends on the `Stage` Protocol; our workloads subclass
`BaseStage` for defaults, and a future/external pipeline can satisfy the Protocol without
inheriting it. Rendering (container command, job labels) stays in `manifest`; this module
imports only `util`/`note`, so `manifest` stays free of a `manifest -> stages` import cycle.
"""

from typing import Protocol, runtime_checkable

from . import note, util

INGEST_SETUP = ["python", "-m", "pychunkedgraph.pipeline.ingest.setup"]
MESHING_SETUP = ["python", "-m", "pychunkedgraph.pipeline.meshing.setup"]
MIGRATE_SETUP = ["python", "-m", "pychunkedgraph.pipeline.migrate.setup"]


@runtime_checkable
class Stage(Protocol):
    """The contract the orchestrator depends on."""

    name: str
    deps: frozenset[str]
    build: bool

    def applies(self, cfg) -> bool: ...
    def setup(self, cfg, exist_ok: bool = False) -> None: ...
    def top_layer(self, cfg, counts) -> int: ...
    def output_dir(self, cfg) -> str | None: ...


class BaseStage:
    """Defaults our stages reuse: no deps, part of a build, always applies, root top
    layer, no setup, no output dir."""

    deps: frozenset[str] = frozenset()
    build: bool = True

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
        return min(int(cfg.dataset["mesh_config"]["max_layer"]), max(counts))

    def output_dir(self, cfg) -> str | None:
        ws = (cfg.dataset.get("data_source") or {}).get("WATERSHED")
        mdir = (cfg.dataset.get("mesh_config") or {}).get("dir")
        # shards land in initial/; <dir> itself holds an info file from mesh-meta
        return f"{ws.rstrip('/')}/{mdir}/initial" if ws and mdir else None


class L2Cache(BaseStage):
    name = "l2cache"
    deps = frozenset({"ingest"})

    def applies(self, cfg) -> bool:
        return bool(cfg.commands.get("l2cache"))

    def setup(self, cfg, exist_ok: bool = False) -> None:
        note("l2cache: no graph setup needed (graph already ingested)")

    def top_layer(self, cfg, counts) -> int:
        return 2


class Migrate(BaseStage):
    name = "migrate"
    build = False

    def setup(self, cfg, exist_ok: bool = False) -> None:
        note(f"setup ({self.name})")  # migrate reads everything from Bigtable
        argv = MIGRATE_SETUP + [cfg.graph_id]
        note(util.run_pcg(cfg, "setup", argv) or "setup done")
        util.invalidate_layer_counts(cfg)


class MigrateCleanup(Migrate):
    name = "migrate_cleanup"
    deps = frozenset({"migrate"})


STAGES: dict[str, Stage] = {
    s.name: s for s in (Ingest(), Meshing(), L2Cache(), Migrate(), MigrateCleanup())
}


def build_set(cfg) -> set:
    """Workloads a full build runs for this config: ingest + any applicable optional stage."""
    return {s.name for s in STAGES.values() if s.build and s.applies(cfg)}
