"""The PCG image contract, checked against images built in memory.

Each image is gzip tar layers served by a stand-in registry, so no test reaches Docker Hub.
The compliant image installs this repository's own runner and harness, so the harness the
operator ships is held to the contract too. Each other test breaks the compliant image one
way and names the clauses that must catch it."""

import io
import pathlib
import tarfile
from types import MappingProxyType

import cave_pipeline
from cave_pipeline import preflight
from cave_pipeline.preflight import clauses
from cave_pipeline.preflight.registry import ImageConfig

PACKAGE = pathlib.Path(cave_pipeline.__file__).parent
STDLIB = "/usr/local/lib/python3.11"
SITE = "/app/venv/lib/python3.11/site-packages"
APP = "/app/pychunkedgraph"
WHITEOUT = object()  # a layer entry deleting the path from older layers

BASE = {
    f"{STDLIB}/{name}": ""
    for name in (
        "os.py",
        "argparse.py",
        "traceback.py",
        "typing.py",
        "logging/__init__.py",
        "collections/__init__.py",
        "collections/abc.py",
    )
}

VENV = {
    f"{SITE}/cave_pipeline/{name}": (PACKAGE / name).read_text()
    for name in (
        "__init__.py",
        "distribution/__init__.py",
        "distribution/exit_codes.py",
        "distribution/grid.py",
        "distribution/harness.py",
    )
} | {
    f"{SITE}/numpy/__init__.py": "",
    f"{SITE}/yaml/__init__.py": "",
    f"{SITE}/google/protobuf/__init__.py": "",
}

MAIN = """from cave_pipeline.distribution import run_and_exit

from .worker import main

if __name__ == "__main__":
    run_and_exit(main)
"""

PIPELINE = """def cg_factory(env):
    return env["graph_id"]


def layer_bounds(cg, layer):
    return (1, 1, 1)
"""

VENDORED_WORKER = """from .. import cg_factory
from ..harness import run


def make_processor(cg, layer, env):
    return env["n_processes"]


def main():
    return run(make_processor{keywords})
"""


class InMemoryRegistry:
    """Layers given oldest first, served as a registry serves an image."""

    def __init__(self, *layers: dict[str, object]):
        self._blobs = [self._pack(files) for files in layers]

    def config(self) -> ImageConfig:
        env = MappingProxyType({"VIRTUAL_ENV": "/app/venv"})
        return ImageConfig(("linux", "amd64"), "/app", env)

    def layers(self) -> list[str]:
        return [str(index) for index in reversed(range(len(self._blobs)))]

    def blob(self, digest: str) -> io.BytesIO:
        return io.BytesIO(self._blobs[int(digest)])

    @staticmethod
    def _pack(files: dict[str, object]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for path, text in files.items():
                name = path.lstrip("/")
                if text is WHITEOUT:
                    head, _, base = name.rpartition("/")
                    name, text = f"{head}/.wh.{base}", ""
                data = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buffer.getvalue()


class StalledRegistry(InMemoryRegistry):
    """A registry whose layers stop streaming."""

    def blob(self, digest: str) -> io.BytesIO:
        raise OSError("connection reset by peer")


def _worker(read: str, imports: str = "") -> str:
    return f"""{imports}from cave_pipeline.distribution.harness import run

from .. import cg_factory, layer_bounds


def make_processor(cg, layer, env):
    processes = {read}
    return lambda coord: "ok"


def main():
    return run(make_processor, context_factory=cg_factory, bounds_fn=layer_bounds)
"""


def _setup(
    *flags: str,
    group: bool = False,
    extra: str = "",
    guard: str = "run_and_exit(main)",
    dataset: str = "/app/datasets/dataset.yml",
) -> str:
    holder = "flags" if group else "parser"
    grouping = '    flags = parser.add_argument_group("flags")\n' if group else ""
    added = "".join(
        f'    {holder}.add_argument("{flag}", action="store_true")\n' for flag in flags
    )
    return f"""import argparse
from os import environ

from cave_pipeline.distribution import run_and_exit

DATASET_PATH = environ.get("PCG_DATASET", "{dataset}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("graph_id")
{grouping}{added}{extra}    parser.parse_args()


if __name__ == "__main__":
    {guard}
"""


def _compliant() -> dict[str, object]:
    """The smallest PCG tree following every clause, shaped like pcgv3."""
    return {
        f"{APP}/__init__.py": "",
        f"{APP}/graph/__init__.py": "from .chunkedgraph import ChunkedGraph\n",
        f"{APP}/graph/chunkedgraph.py": "class ChunkedGraph:\n    pass\n",
        f"{APP}/pipeline/__init__.py": PIPELINE,
        f"{APP}/pipeline/ingest/__init__.py": "",
        f"{APP}/pipeline/ingest/__main__.py": MAIN,
        f"{APP}/pipeline/ingest/worker.py": _worker('env["n_processes"]'),
        f"{APP}/pipeline/ingest/setup.py": _setup("--raw", "--exist-ok"),
        f"{APP}/pipeline/meshing/__init__.py": "",
        f"{APP}/pipeline/meshing/__main__.py": MAIN,
        f"{APP}/pipeline/meshing/worker.py": _worker('int(env.get("n_processes", 1))'),
        f"{APP}/pipeline/meshing/setup.py": _setup(),
    }


def _check(registry: InMemoryRegistry) -> preflight.Report:
    return preflight.Preflight(
        "example/pcg:test", ["ingest", "meshing"], registry=lambda image: registry
    ).report()


def _broken(*layers: dict[str, object]) -> set[tuple[type, str]]:
    """(clause type, workload) for every violation of an image built from `layers`."""
    report = _check(InMemoryRegistry(BASE, VENV, *layers))
    assert not report.unreachable
    return {(type(v.clause), v.workload) for v in report.violations}


def _app(path: str, text: str) -> dict[str, object]:
    return _compliant() | {f"{APP}/{path}": text}


def test_a_compliant_image_breaks_no_clause():
    assert _broken(_compliant()) == set()


def test_a_processor_reading_a_key_the_harness_never_builds_breaks_env_keys():
    """dev5: the harness built one key while the processor read another, and every pod died
    on a KeyError."""
    app = _app("pipeline/ingest/worker.py", _worker('env["n_threads"]'))
    assert _broken(app) == {(clauses.EnvKeys, "ingest")}


def test_a_callable_main_passes_by_keyword_is_held_to_the_built_keys():
    app = _app(
        "pipeline/__init__.py", PIPELINE.replace('env["graph_id"]', 'env["graph"]')
    )
    assert _broken(app) == {(clauses.EnvKeys, "ingest"), (clauses.EnvKeys, "meshing")}


def test_a_harness_handing_env_on_by_keyword_is_traced():
    harness = """import os


def run(make_processor, *, context_factory):
    env = {"n_processes": int(os.environ.get("PCG_N_PROCESSES", 1))}
    ctx = context_factory(env=env)
    return make_processor(ctx, 2, env)
"""
    worker = VENDORED_WORKER.format(keywords=", context_factory=cg_factory")
    app = _app("pipeline/harness.py", harness) | {
        f"{APP}/pipeline/ingest/worker.py": worker
    }
    assert _broken(app) == {(clauses.EnvKeys, "ingest")}


def test_a_harness_reading_an_env_name_the_job_never_sets_breaks_harness_env():
    harness = """import os


def run(make_processor):
    env = {"n_processes": int(os.environ.get("PCG_N_THREADS", 1))}
    return make_processor(None, 2, env)
"""
    worker = VENDORED_WORKER.format(keywords="")
    app = _app("pipeline/harness.py", harness) | {
        f"{APP}/pipeline/ingest/worker.py": worker
    }
    assert _broken(app) == {(clauses.HarnessEnv, "ingest")}


def test_a_harness_writing_the_environment_is_not_reading_it():
    harness = """import os


def run(make_processor):
    os.environ["OMP_NUM_THREADS"] = "1"
    env = {"n_processes": int(os.environ.get("PCG_N_PROCESSES", 1))}
    return make_processor(None, 2, env)
"""
    worker = VENDORED_WORKER.format(keywords="")
    app = _app("pipeline/harness.py", harness) | {
        f"{APP}/pipeline/ingest/worker.py": worker
    }
    assert _broken(app) == set()


def test_a_processor_requiring_an_env_name_the_job_never_sets_breaks_harness_env():
    worker = _worker('os.environ["PCG_THREADS"]', imports="import os\n")
    assert _broken(_app("pipeline/ingest/worker.py", worker)) == {
        (clauses.HarnessEnv, "ingest")
    }


def test_a_setup_without_a_contract_flag_breaks_setup_arguments():
    app = _app("pipeline/ingest/setup.py", _setup("--raw"))
    assert _broken(app) == {(clauses.SetupArguments, "ingest")}


def test_a_contract_flag_taking_a_value_breaks_setup_arguments():
    setup = _setup("--raw", extra='    parser.add_argument("--exist-ok")\n')
    assert _broken(_app("pipeline/ingest/setup.py", setup)) == {
        (clauses.SetupArguments, "ingest")
    }


def test_a_setup_requiring_an_option_the_operator_never_sends_breaks_setup_arguments():
    extra = '    parser.add_argument("--dataset", required=True)\n'
    app = _app("pipeline/meshing/setup.py", _setup(extra=extra))
    assert _broken(app) == {(clauses.SetupArguments, "meshing")}


def test_contract_flags_added_through_an_argument_group_are_accepted():
    app = _app("pipeline/ingest/setup.py", _setup("--raw", "--exist-ok", group=True))
    assert _broken(app) == set()


def test_an_entrypoint_exiting_through_sys_exit_breaks_worker_entrypoint():
    main = "import sys\n\nfrom .worker import main\n\nif __name__ == '__main__':\n"
    app = _app("pipeline/ingest/__main__.py", main + "    sys.exit(main())\n")
    assert _broken(app) == {(clauses.WorkerEntrypoint, "ingest")}


def test_a_runner_skipping_os_exit_when_main_raises_breaks_worker_entrypoint():
    runner = "import os\n\n\ndef run_and_exit(main):\n    os._exit(main() or 0)\n"
    main = MAIN.replace("from cave_pipeline.distribution import", "from .. import")
    app = _app("pipeline/__init__.py", PIPELINE + "\n\n" + runner) | {
        f"{APP}/pipeline/ingest/__main__.py": main
    }
    assert _broken(app) == {(clauses.WorkerEntrypoint, "ingest")}


def test_a_setup_guard_calling_main_directly_breaks_setup_shape():
    app = _app("pipeline/meshing/setup.py", _setup(guard="main()"))
    assert _broken(app) == {(clauses.SetupShape, "meshing")}


def test_a_setup_reading_its_dataset_elsewhere_breaks_dataset_path():
    app = _app("pipeline/meshing/setup.py", _setup(dataset="/data/dataset.yml"))
    assert _broken(app) == {(clauses.DatasetPath, "meshing")}


def test_a_probe_class_bound_only_for_type_checking_breaks_graph_probe():
    graph = (
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n"
        "    from .chunkedgraph import ChunkedGraph\n"
    )
    assert _broken(_app("graph/__init__.py", graph)) == {(clauses.GraphProbe, "")}


def test_an_import_of_a_missing_first_party_module_breaks_first_party_imports():
    worker = _worker('env["n_processes"]', imports="from ...ingest import simple_tests\n")
    app = _app("pipeline/ingest/worker.py", worker)
    assert _broken(app) == {(clauses.FirstPartyImports, "")}


def test_an_import_of_a_name_its_module_never_binds_breaks_first_party_imports():
    imports = "from ...graph.chunkedgraph import MissingGraph\n"
    app = _app(
        "pipeline/ingest/worker.py", _worker('env["n_processes"]', imports=imports)
    )
    assert _broken(app) == {(clauses.FirstPartyImports, "")}


def test_an_uninstalled_third_party_import_breaks_installed_imports():
    worker = _worker('env["n_processes"]', imports="import cloudvolume\n")
    app = _app("pipeline/meshing/worker.py", worker)
    assert _broken(app) == {(clauses.InstalledImports, "")}


def test_a_namespace_package_missing_the_imported_package_breaks_installed_imports():
    worker = _worker('env["n_processes"]', imports="from google.cloud import bigtable\n")
    app = _app("pipeline/ingest/worker.py", worker)
    assert _broken(app) == {(clauses.InstalledImports, "")}


def test_an_import_guarded_by_a_broad_except_is_still_required():
    imports = "try:\n    import cloudvolume\nexcept Exception:\n    cloudvolume = None\n"
    app = _app(
        "pipeline/ingest/worker.py", _worker('env["n_processes"]', imports=imports)
    )
    assert _broken(app) == {(clauses.InstalledImports, "")}


def test_the_standard_library_is_the_images_own_not_the_operators():
    worker = _worker('env["n_processes"]', imports="import imp\n")
    app = _app("pipeline/ingest/worker.py", worker)
    assert _broken(app) == {(clauses.InstalledImports, "")}


def test_a_newer_layer_whiteout_removes_a_module():
    removed = {f"{APP}/pipeline/meshing/setup.py": WHITEOUT}
    assert _broken(_compliant(), removed) == {(clauses.SetupShape, "meshing")}


def test_an_opaque_whiteout_hides_an_older_directory():
    opaque = {f"{APP}/pipeline/meshing/.wh..wh..opq": ""}
    assert _broken(_compliant(), opaque) == {
        (clauses.WorkerEntrypoint, "meshing"),
        (clauses.ProcessorShape, "meshing"),
        (clauses.SetupShape, "meshing"),
    }


def test_every_violation_is_reported_together():
    app = _app("pipeline/ingest/worker.py", _worker('env["n_threads"]'))
    removed = {f"{APP}/pipeline/meshing/setup.py": WHITEOUT}
    assert _broken(app, removed) == {
        (clauses.EnvKeys, "ingest"),
        (clauses.SetupShape, "meshing"),
    }


def test_an_image_outside_docker_hub_breaks_published_without_a_read():
    check = preflight.Preflight("gcr.io/project/pcg:v1", ["ingest"])
    assert {type(v.clause) for v in check.violations()} == {clauses.Published}


def test_an_image_that_stops_streaming_goes_unchecked_rather_than_violated():
    report = _check(StalledRegistry(BASE, VENV, _compliant()))
    assert report.violations == () and "connection reset" in report.unreachable
    assert report.refused


def test_the_verdict_ends_every_report():
    app = _app("pipeline/ingest/worker.py", _worker('env["n_threads"]'))
    broken = _check(InMemoryRegistry(BASE, VENV, app))
    unread = _check(StalledRegistry(BASE, VENV, _compliant()))
    assert broken.text().splitlines()[-1].startswith("image-preflight: FAIL ")
    assert unread.text().splitlines()[-1].startswith("image-preflight: UNCHECKED ")
