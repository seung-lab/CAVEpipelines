"""The PCG image contract: each clause's statement, and how an image is checked against it."""

from __future__ import annotations

import ast
import enum
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cached_property
from typing import NamedTuple

from ..contract import STANDARD, Workload
from .files import ImageFiles
from .registry import Registry
from .source import Closure, Codebase, Origin
from .trace import Guard, Shape, Trace
from .utils import ENTRY, HOMES


@dataclass
class Inspection:
    """One image as every clause reads it, with each workload's shapes traced once."""

    files: ImageFiles
    codebase: Codebase
    supplied_env: frozenset[str]
    workloads: tuple[Workload, ...]
    _traces: dict[str, Trace] = field(default_factory=dict, init=False, repr=False)

    def trace(self, workload: Workload) -> Trace:
        if workload.name not in self._traces:
            self._traces[workload.name] = Trace(self.codebase, workload)
        return self._traces[workload.name]

    @cached_property
    def closure(self) -> Closure:
        roots = [STANDARD.probe.module]
        for workload in self.workloads:
            roots += [
                workload.main_module,
                workload.processor_module,
                workload.setup_module,
            ]
        return Closure.walk(self.codebase, roots)


class Violation(NamedTuple):
    """One way an image breaks one clause."""

    clause: Clause
    workload: str
    detail: str

    def __str__(self) -> str:
        where = f" [{self.workload}]" if self.workload else ""
        return f"{self.clause.id}{where}: {self.detail}"


class Scope(enum.Enum):
    """Whether a clause holds once per image, or once per workload."""

    IMAGE = "image"
    WORKLOAD = "workload"


class Clause(ABC):
    """One clause of the contract: its statement, and how an image is checked against it."""

    id: str
    statement: str
    scope: Scope = Scope.WORKLOAD
    shape: Shape | None = None  # the traced shape whose failure this clause reports

    @abstractmethod
    def check(self, inspection: Inspection, workload: Workload | None) -> Iterator[str]:
        """Each way the image breaks this clause, as one line."""

    def violations(self, inspection: Inspection) -> Iterator[Violation]:
        """Every breach, checked once for the image or once per workload, by its scope."""
        if self.scope is Scope.IMAGE:
            for detail in self.check(inspection, None):
                yield Violation(self, "", detail)
            return
        for workload in inspection.workloads:
            for detail in self.check(inspection, workload):
                yield Violation(self, workload.name, detail)

    def untraced(self, inspection: Inspection, workload: Workload) -> Iterator[str]:
        """Why the shape this clause owns could not be traced, when it could not."""
        failure = inspection.trace(workload).failure(self.shape)
        if failure:
            yield failure


class EntrypointClause(Clause):
    """A clause on a module whose `__main__` guard hands `main` to the contract's runner."""

    @staticmethod
    def guard_problems(guard: Guard) -> Iterator[str]:
        path = guard.module.path
        if guard.block is None:
            yield f'{path} has no `if __name__ == "__main__":` guard'
        elif not guard.runner:
            yield f"{path}:{guard.block.lineno} does not hand `{ENTRY.main}` to a runner"
        elif guard.definition is None:
            yield f"{path}: runner `{guard.runner}` is not a function in {HOMES} sources"
        elif not guard.definition.module.exits_through(
            guard.definition.function, STANDARD.runner_exit
        ):
            yield (
                f"{guard.definition.module.path}: runner `{guard.runner}` does not always "
                f"leave through {STANDARD.runner_exit}"
            )


class Published(Clause):
    id = "C1"
    scope = Scope.IMAGE
    statement = (
        f"The image is public on Docker Hub, with a {'/'.join(Registry.PLATFORM)} image."
    )

    def check(self, inspection, workload):
        platform = inspection.files.config.platform
        if platform != Registry.PLATFORM:
            yield f"its platform is {'/'.join(platform)}"


class WorkingDirectory(Clause):
    id = "C2"
    scope = Scope.IMAGE
    statement = (
        f"Its working directory is {STANDARD.workdir}, holding the "
        f"{STANDARD.root_package} package `python -m` resolves."
    )

    def check(self, inspection, workload):
        workdir = inspection.files.config.workdir
        if workdir != STANDARD.workdir:
            yield f"its working directory is {workdir}"
        elif inspection.codebase.module(STANDARD.root_package) is None:
            yield inspection.codebase.missing(STANDARD.root_package)


class InstalledImports(Clause):
    id = "C3"
    scope = Scope.IMAGE
    statement = (
        f"Every third-party module the entrypoints import is installed in the virtualenv "
        f"${STANDARD.venv_env} names, or is in the image's own standard library."
    )

    def check(self, inspection, workload):
        if not inspection.files.installed.site:
            yield f"no site-packages below ${STANDARD.venv_env}"
            return
        yield from inspection.closure.uninstalled


class WorkerEntrypoint(EntrypointClause):
    id = "C4"
    statement = (
        f"`<package>/__main__.py` hands `{ENTRY.main}`, imported from `.worker`, to a runner "
        f"that always leaves through `{STANDARD.runner_exit}`, inside its `__main__` guard."
    )

    def check(self, inspection, workload):
        module = inspection.codebase.module(workload.main_module)
        if module is None:
            yield inspection.codebase.missing(workload.main_module)
            return
        yield from self.guard_problems(inspection.trace(workload).guard(module))
        wanted = Origin(workload.processor_module, ENTRY.main)
        if module.origins.get(ENTRY.main) != wanted:
            yield (
                f"{module.path}: `{ENTRY.main}` is not imported from "
                f"{workload.processor_module}"
            )


class ProcessorShape(Clause):
    id = "C5"
    shape = Shape.PROCESSOR
    statement = (
        f"`<package>/worker.py` defines module-level `{ENTRY.processor_signature}` and "
        f"`{ENTRY.main}()`, and `{ENTRY.main}` returns `run({ENTRY.processor}, ...)`."
    )

    def check(self, inspection, workload):
        return self.untraced(inspection, workload)


class HarnessShape(Clause):
    id = "C6"
    shape = Shape.HARNESS
    statement = (
        f"`run` is a function in {HOMES} sources that builds one dict of literal "
        f"`os.environ` reads and hands it on by name, as `{ENTRY.processor}`'s last argument."
    )

    def check(self, inspection, workload):
        return self.untraced(inspection, workload)


class HarnessEnv(Clause):
    id = "C7"
    statement = (
        "Every name the harness reads from `os.environ`, and every name a callable it hands "
        "env to reads without a default, is one the operator's Job sets."
    )

    def check(self, inspection, workload):
        harness = inspection.trace(workload).harness()
        if harness is None:
            return
        reads = [(harness.definition.module, read) for read in harness.reads]
        for recipient in harness.recipients:
            if recipient.definition is not None:
                module, function = recipient.definition
                reads += [
                    (module, r) for r in module.environ_reads(function) if r.required
                ]
        for module, read in reads:
            if read.name is None:
                yield f"{module.path}:{read.line} reads os.environ with a non-literal name"
            elif read.name not in inspection.supplied_env:
                yield f"{module.path}:{read.line} reads {read.name}, which the Job never sets"


class EnvKeys(Clause):
    id = "C8"
    statement = (
        "Every callable the harness hands its env dict to reads it only as a literal "
        "`env[...]` or `env.get(...)`, of a key the harness builds."
    )

    def check(self, inspection, workload):
        harness = inspection.trace(workload).harness()
        if harness is None:
            return
        path = harness.processor.module.path
        for recipient in harness.recipients:
            parameter, passed = recipient.handoff.parameter, recipient.passed
            if not isinstance(passed, ast.Name):
                yield f"{path}:{passed.lineno} passes a non-name as {parameter}"
            elif recipient.definition is None:
                yield (
                    f"{path}: `{passed.id}`, passed as {parameter}, is not a function in "
                    f"{HOMES} sources"
                )
            elif not recipient.env:
                module, function = recipient.definition
                yield (
                    f"{module.path}:{function.lineno} {function.name} has no parameter where "
                    f"the harness passes env"
                )
            else:
                yield from self._key_problems(recipient, harness.keys)

    @staticmethod
    def _key_problems(recipient, keys: frozenset[str]) -> Iterator[str]:
        path, env = recipient.definition.module.path, recipient.env
        for read in recipient.key_reads():
            if read.key is None:
                yield f"{path}:{read.line} uses `{env}` other than as a literal key read"
            elif read.key not in keys:
                yield (
                    f"{path}:{read.line} reads {env}[{read.key!r}], which the harness never "
                    f"builds"
                )


class SetupShape(EntrypointClause):
    id = "C9"
    shape = Shape.PARSER
    statement = (
        f"`<package>/setup.py` defines `{ENTRY.main}()` building one `argparse.ArgumentParser` "
        "from literal `add_argument` calls, and hands it to such a runner in its guard."
    )

    def check(self, inspection, workload):
        module = inspection.codebase.module(workload.setup_module)
        if module is not None:
            yield from self.guard_problems(inspection.trace(workload).guard(module))
        # the parser's own failure names a missing module
        yield from self.untraced(inspection, workload)


class SetupArguments(Clause):
    id = "C10"
    statement = (
        "The setup requires exactly the contract's positionals, accepts each contract flag "
        "as `store_true`, and requires no other option."
    )

    def check(self, inspection, workload):
        parser = inspection.trace(workload).parser()
        if parser is None:
            return
        wanted = workload.setup
        required = [one for one in parser.positionals if not one.optional]
        if len(required) != len(wanted.positionals):
            yield (
                f"it requires {len(required)} positionals, not the {len(wanted.positionals)} "
                f"the operator sends ({', '.join(wanted.positionals)})"
            )
        for flag in wanted.flags:
            option = parser.option(flag)
            if option is None:
                yield f"it does not accept {flag}"
            elif option.action != "store_true":
                yield f"its {flag} is {option.action}, not store_true"
        for option in parser.options:
            if option.required and not set(option.strings) & set(wanted.flags):
                yield f"it requires {option.strings[0]}, which the operator never sends"


class DatasetPath(Clause):
    id = "C11"
    statement = (
        f"The setup reads its dataset from ${STANDARD.dataset_env}, defaulting to "
        f"{STANDARD.dataset_path}, where the operator mounts it."
    )

    def check(self, inspection, workload):
        module = inspection.codebase.module(workload.setup_module)
        if module is None:  # SetupShape reports it
            return
        if not any(
            read.name == STANDARD.dataset_env and read.default == STANDARD.dataset_path
            for read in module.environ_reads(module.tree)
        ):
            yield (
                f"{module.path} never reads {STANDARD.dataset_env} with the default "
                f"{STANDARD.dataset_path}"
            )


class GraphProbe(Clause):
    id = "C12"
    scope = Scope.IMAGE
    statement = (
        f"`{STANDARD.probe.module}` binds `{STANDARD.probe.name}`, which the operator's "
        "graph-meta probes import."
    )

    def check(self, inspection, workload):
        module = inspection.codebase.module(STANDARD.probe.module)
        if module is None:
            yield inspection.codebase.missing(STANDARD.probe.module)
        elif not module.binds(STANDARD.probe.name):
            yield f"{module.path} binds no {STANDARD.probe.name}"


class FirstPartyImports(Clause):
    id = "C13"
    scope = Scope.IMAGE
    statement = (
        f"Every import of {HOMES} code reachable from the entrypoints and the probe resolves "
        "to a module, or to a name its module binds."
    )

    def check(self, inspection, workload):
        yield from inspection.closure.unresolved


PUBLISHED = Published()
# nothing else can be read from an image breaking these
FOUNDATION: tuple[Clause, ...] = (PUBLISHED, WorkingDirectory())
DEPENDENT: tuple[Clause, ...] = (
    InstalledImports(),
    WorkerEntrypoint(),
    ProcessorShape(),
    HarnessShape(),
    HarnessEnv(),
    EnvKeys(),
    SetupShape(),
    SetupArguments(),
    DatasetPath(),
    GraphProbe(),
    FirstPartyImports(),
)
CLAUSES: tuple[Clause, ...] = (*FOUNDATION, *DEPENDENT)
