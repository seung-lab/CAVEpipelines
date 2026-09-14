"""The PCG image contract: what a PyChunkedGraph image follows for this operator to drive
its ingest and meshing workloads.

`preflight` checks an image against these records by reading it, never running it, and the
operator builds the commands, setup arguments, env names and dataset mount it sends from
the same records, so what is checked is what is sent. Stdlib only.
"""

import posixpath
from collections.abc import Mapping
from types import MappingProxyType
from typing import NamedTuple

RAW_FLAG = "--raw"  # the dataset names an agglomeration source
EXIST_OK_FLAG = "--exist-ok"  # the graph may already exist, as on a resumed run


class SetupInterface(NamedTuple):
    """What a workload's setup accepts: positionals in order, then `store_true` flags."""

    positionals: tuple[str, ...]
    flags: tuple[str, ...] = ()


class Workload(NamedTuple):
    """A contract workload, named by the package holding its entrypoints."""

    name: str
    package: str
    setup: SetupInterface

    @property
    def main_module(self) -> str:
        return f"{self.package}.__main__"

    @property
    def processor_module(self) -> str:
        return f"{self.package}.worker"

    @property
    def setup_module(self) -> str:
        return f"{self.package}.setup"

    def worker_argv(self) -> list[str]:
        return ["python", "-m", self.package]

    def setup_argv(self, *positionals: str, flags: tuple[str, ...] = ()) -> list[str]:
        """The setup command, refusing any argument outside the interface: no image is
        checked for it."""
        if len(positionals) != len(self.setup.positionals):
            wanted = ", ".join(self.setup.positionals)
            raise ValueError(f"{self.name} setup takes exactly: {wanted}")
        undeclared = [flag for flag in flags if flag not in self.setup.flags]
        if undeclared:
            raise ValueError(f"{self.name} setup declares no {', '.join(undeclared)}")
        return ["python", "-m", self.setup_module, *positionals, *flags]


class Entrypoints(NamedTuple):
    """The functions a workload's modules define for the operator to run."""

    main: str  # what every entrypoint's `__main__` guard hands to its runner
    processor: str  # the worker's per-batch processor factory
    processor_params: tuple[str, ...]  # its parameters; the env dict arrives in the last

    @property
    def env_position(self) -> int:
        return len(self.processor_params) - 1

    @property
    def processor_signature(self) -> str:
        return f"{self.processor}({', '.join(self.processor_params)})"


class WorkerEnv(NamedTuple):
    """Env names on every worker pod."""

    graph_id: str
    layer: str
    perm_seed: str
    batch_size: str
    n_processes: str  # the pod's process budget; pools never exceed it
    index: str  # set by Kubernetes on an Indexed Job, never by the operator


class PodEnv(NamedTuple):
    """Env names the pcg-env ConfigMap sets on every pod the operator starts."""

    bigtable_project: str
    bigtable_instance: str
    credentials: str


class Probe(NamedTuple):
    """The class the operator's graph-meta probes import from the image."""

    module: str
    name: str


class Standard(NamedTuple):
    """The image-wide clauses every workload relies on."""

    workdir: str
    root_package: str
    venv_env: str  # names the virtualenv whose site-packages holds third-party imports
    runner_exit: str  # the call an entrypoint's runner must always leave through
    harness_homes: tuple[str, ...]  # top-level packages a harness or runner may live in
    entrypoints: Entrypoints
    dataset_env: str
    dataset_path: str
    worker_env: WorkerEnv
    pod_env: PodEnv
    probe: Probe

    @property
    def dataset_dir(self) -> str:
        return posixpath.dirname(self.dataset_path)

    @property
    def dataset_file(self) -> str:
        return posixpath.basename(self.dataset_path)


STANDARD = Standard(
    workdir="/app",
    root_package="pychunkedgraph",
    venv_env="VIRTUAL_ENV",
    # a normal exit can join a client's non-daemon thread and never return
    runner_exit="os._exit",
    harness_homes=("pychunkedgraph", "cave_pipeline"),
    entrypoints=Entrypoints(
        main="main",
        processor="make_processor",
        processor_params=("ctx", "layer", "env"),
    ),
    dataset_env="PCG_DATASET",
    dataset_path="/app/datasets/dataset.yml",
    worker_env=WorkerEnv(
        graph_id="PCG_GRAPH_ID",
        layer="PCG_LAYER",
        perm_seed="PCG_PERM_SEED",
        batch_size="PCG_BATCH_SIZE",
        n_processes="PCG_N_PROCESSES",
        index="JOB_COMPLETION_INDEX",
    ),
    pod_env=PodEnv(
        bigtable_project="BIGTABLE_PROJECT",
        bigtable_instance="BIGTABLE_INSTANCE",
        credentials="GOOGLE_APPLICATION_CREDENTIALS",
    ),
    probe=Probe(module="pychunkedgraph.graph", name="ChunkedGraph"),
)

WORKLOADS: Mapping[str, Workload] = MappingProxyType(
    {
        workload.name: workload
        for workload in (
            Workload(
                name="ingest",
                package="pychunkedgraph.pipeline.ingest",
                setup=SetupInterface(("graph_id",), (RAW_FLAG, EXIST_OK_FLAG)),
            ),
            Workload(
                name="meshing",
                package="pychunkedgraph.pipeline.meshing",
                setup=SetupInterface(("graph_id",)),
            ),
        )
    }
)
