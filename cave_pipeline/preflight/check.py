"""One image checked against every clause, and the report the operator reads."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import NamedTuple

from .. import manifest, note
from ..contract import STANDARD, WORKLOADS
from .clauses import CLAUSES, DEPENDENT, FOUNDATION, PUBLISHED, Inspection, Violation
from .files import ImageFiles
from .registry import Registry
from .source import Codebase
from .utils import Unreachable, Unreadable


class Report(NamedTuple):
    """The outcome of one preflight, as the operator reads it."""

    image: str
    workloads: tuple[str, ...]
    violations: tuple[Violation, ...]
    seconds: float
    unreachable: str = ""  # why the image went unchecked, or empty when it was checked

    @property
    def refused(self) -> bool:
        return bool(self.violations or self.unreachable)

    def text(self) -> str:
        """What was found, then the verdict, which is always the last line."""
        lines = []
        if self.unreachable:
            lines.append(f"  {self.unreachable}")
        elif self.violations:
            broken = dict.fromkeys(violation.clause for violation in self.violations)
            lines += ["violations:", *(f"  {violation}" for violation in self.violations)]
            lines += ["broken clauses:", *(f"  {c.id}: {c.statement}" for c in broken)]
        return "\n".join([*lines, self.verdict()])

    def verdict(self) -> str:
        took, names = f"{self.seconds:.1f}s", ", ".join(self.workloads)
        if self.unreachable:
            return f"image-preflight: UNCHECKED {self.image} was not read for {names} ({took})"
        if self.violations:
            broken = len({violation.clause.id for violation in self.violations})
            return (
                f"image-preflight: FAIL {self.image} breaks {broken} of {len(CLAUSES)} "
                f"contract clauses for {names} ({took})"
            )
        return (
            f"image-preflight: PASS {self.image} satisfies all {len(CLAUSES)} contract "
            f"clauses for {names} ({took})"
        )


class Preflight:
    """One image checked against every clause, for the contract workloads a command runs."""

    def __init__(
        self,
        image: str,
        workloads: Iterable[str],
        extra_env: Iterable[str] = (),
        registry: Callable[[str], Registry] = Registry,
    ):
        requested = set(workloads)
        self.image = image
        self.workloads = tuple(
            WORKLOADS[name] for name in sorted(requested & set(WORKLOADS))
        )
        self.outside = tuple(sorted(requested - set(WORKLOADS)))
        self._extra_env = frozenset(extra_env)
        self._registry = registry

    @classmethod
    def for_config(cls, cfg, workloads: Iterable[str]) -> Preflight:
        """The config's PCG image, allowing the env its `env:` block adds to every pod."""
        extra = (var.name for var in manifest.extra_env(cfg))
        return cls(cfg.images.pcg, workloads, extra_env=extra)

    def violations(self) -> tuple[Violation, ...]:
        """Every violation, all at once; raises `Unreachable` when the image went unread."""
        try:
            files = ImageFiles.read(self._registry(self.image))
        except Unreadable as failure:
            return (Violation(PUBLISHED, "", str(failure)),)
        supplied = frozenset(
            (*STANDARD.worker_env, *STANDARD.pod_env, *files.config.env, *self._extra_env)
        )
        inspection = Inspection(files, Codebase(files), supplied, self.workloads)
        foundation = tuple(
            v for clause in FOUNDATION for v in clause.violations(inspection)
        )
        if foundation:
            return foundation
        return tuple(v for clause in DEPENDENT for v in clause.violations(inspection))

    def report(self) -> Report:
        started = time.monotonic()
        names = tuple(workload.name for workload in self.workloads)
        try:
            found = self.violations()
        except Unreachable as failure:
            return Report(self.image, names, (), time.monotonic() - started, str(failure))
        return Report(self.image, names, found, time.monotonic() - started)

    def require(self) -> None:
        """Refuse an image breaking the contract, listing every violation, or one that could
        not be checked, saying why, the verdict last. A pass prints nothing."""
        for name in self.outside:
            note(
                f"image-preflight: {name} is outside the PCG image contract; not inspected"
            )
        if not self.workloads:
            return
        report = self.report()
        if report.refused:
            raise SystemExit(report.text())
