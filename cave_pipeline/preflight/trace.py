"""The shapes one workload's entrypoints are built from, traced once each from the image's
sources."""

from __future__ import annotations

import ast
import enum
from dataclasses import dataclass
from typing import NamedTuple

from ..contract import Workload
from .source import Codebase, Definition, EnvRead, Module
from .utils import ENTRY, HOMES, named, parameters, positional, string


class Shape(enum.Enum):
    """A structure several clauses build on, traced once per workload."""

    PROCESSOR = "processor"
    HARNESS = "harness"
    PARSER = "parser"


class Untraceable(Exception):
    """A shape was not found. Only the clause owning that shape reports it, and the clauses
    built on it stay silent."""

    def __init__(self, shape: Shape, detail: str):
        super().__init__(detail)
        self.shape = shape
        self.detail = detail


class KeyRead(NamedTuple):
    """One use of an env parameter: the literal key read, or `None` for any other use."""

    key: str | None
    line: int


class Handoff(NamedTuple):
    """A harness parameter the harness calls with its env dict, by position or keyword."""

    parameter: str
    position: int | None
    keyword: str | None


@dataclass(frozen=True)
class Guard:
    """How a module's `__main__` guard runs `main`."""

    module: Module
    block: ast.If | None
    runner: str  # the name `main` is handed to, or empty
    definition: Definition | None  # that runner, when found in a harness home


@dataclass(frozen=True)
class Processor:
    """A worker module, and the `run(make_processor, ...)` its `main` returns."""

    module: Module
    run: ast.Call

    def passes(self, harness: ast.FunctionDef, parameter: str) -> ast.expr | None:
        """What `main` passes for one of the harness's parameters, or `None`."""
        for keyword in self.run.keywords:
            if keyword.arg == parameter:
                return keyword.value
        names = positional(harness)
        index = names.index(parameter) if parameter in names else len(self.run.args)
        return self.run.args[index] if index < len(self.run.args) else None


@dataclass(frozen=True)
class Recipient:
    """A callable the harness hands its env dict to, as `main` passes it."""

    handoff: Handoff
    passed: ast.expr
    definition: Definition | None
    env: str  # its parameter receiving env, or empty when it has none there

    @classmethod
    def of(
        cls, handoff: Handoff, passed: ast.expr, definition: Definition | None
    ) -> Recipient:
        env = ""
        if definition is not None and handoff.keyword is not None:
            env = (
                handoff.keyword
                if handoff.keyword in parameters(definition.function)
                else ""
            )
        elif definition is not None:
            names = positional(definition.function)
            env = names[handoff.position] if handoff.position < len(names) else ""
        return cls(handoff, passed, definition, env)

    def key_reads(self) -> tuple[KeyRead, ...]:
        """Every use of its env parameter; empty when it has no definition or no such
        parameter."""
        if self.definition is None or not self.env:
            return ()
        function = self.definition.function
        reads, consumed = [], set()
        for node in ast.walk(function):
            target, key = None, None
            if isinstance(node, ast.Subscript):
                target, key = node.value, string(node.slice)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
            ):
                target, key = node.func.value, string(node.args[0])
            if named(target, self.env) and key is not None:
                reads.append(KeyRead(key, node.lineno))
                consumed.add(id(target))
        reads += [
            KeyRead(None, node.lineno)
            for node in ast.walk(function)
            if named(node, self.env)
            and isinstance(node.ctx, ast.Load)
            and id(node) not in consumed
        ]
        return tuple(sorted(reads, key=lambda one: one.line))


@dataclass(frozen=True)
class Harness:
    """The function a worker's `main` runs, the env dict it builds, and whom it hands it to."""

    processor: Processor
    definition: Definition
    keys: frozenset[str]
    reads: tuple[EnvRead, ...]
    recipients: tuple[Recipient, ...]


class Positional(NamedTuple):
    name: str
    optional: bool


class Option(NamedTuple):
    strings: tuple[str, ...]
    action: str
    required: bool


@dataclass(frozen=True)
class Parser:
    """A setup's argument parser, read from its literal `add_argument` calls."""

    positionals: tuple[Positional, ...]
    options: tuple[Option, ...]

    def option(self, string: str) -> Option | None:
        return next((one for one in self.options if string in one.strings), None)


class Trace:
    """The shapes one workload's entrypoints are built from. Each is traced once and kept, a
    shape that cannot be traced as its `Untraceable`."""

    ARGUMENT_PARSER = "argparse.ArgumentParser"
    GROUPS = frozenset({"add_argument_group", "add_mutually_exclusive_group"})
    PARSES = frozenset({"parse_args", "parse_known_args"})
    OPTIONAL_NARGS = frozenset({"?", "*"})

    def __init__(self, codebase: Codebase, workload: Workload):
        self.codebase = codebase
        self.workload = workload
        self._tracers = {
            Shape.PROCESSOR: self._trace_processor,
            Shape.HARNESS: self._trace_harness,
            Shape.PARSER: self._trace_parser,
        }
        self._traced: dict[Shape, object] = {}

    def guard(self, module: Module) -> Guard:
        block = module.main_guard()
        handed = [
            node
            for node in (ast.walk(block) if block else ())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and any(named(arg, ENTRY.main) for arg in node.args)
        ]
        runner = handed[0].func.id if handed else ""
        definition = self.codebase.definition(module, runner) if runner else None
        return Guard(module, block, runner, definition)

    def harness(self) -> Harness | None:
        return self._found(Shape.HARNESS)

    def parser(self) -> Parser | None:
        return self._found(Shape.PARSER)

    def failure(self, shape: Shape) -> str:
        """Why `shape` itself could not be traced, or empty. A failure of a shape it builds
        on is empty here too, since that shape's owner reports it."""
        try:
            self._require(shape)
        except Untraceable as failure:
            return failure.detail if failure.shape is shape else ""
        return ""

    def _found(self, shape: Shape):
        try:
            return self._require(shape)
        except Untraceable:
            return None

    def _require(self, shape: Shape):
        if shape not in self._traced:
            try:
                self._traced[shape] = self._tracers[shape]()
            except Untraceable as failure:
                self._traced[shape] = failure
        found = self._traced[shape]
        if isinstance(found, Untraceable):
            raise found
        return found

    def _module(self, shape: Shape, name: str) -> Module:
        module = self.codebase.module(name)
        if module is None:
            raise Untraceable(shape, self.codebase.missing(name))
        return module

    @staticmethod
    def _main(shape: Shape, module: Module) -> ast.FunctionDef:
        main = module.functions.get(ENTRY.main)
        if main is None:
            raise Untraceable(
                shape, f"{module.path} defines no module-level {ENTRY.main}()"
            )
        return main

    def _trace_processor(self) -> Processor:
        module = self._module(Shape.PROCESSOR, self.workload.processor_module)
        make = module.functions.get(ENTRY.processor)
        if make is None or len(positional(make)) < len(ENTRY.processor_params):
            raise Untraceable(
                Shape.PROCESSOR,
                f"{module.path} defines no module-level {ENTRY.processor_signature}",
            )
        for node in ast.walk(self._main(Shape.PROCESSOR, module)):
            call = node.value if isinstance(node, ast.Return) else None
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.args
                and named(call.args[0], ENTRY.processor)
            ):
                return Processor(module, call)
        raise Untraceable(
            Shape.PROCESSOR,
            f"{module.path}: {ENTRY.main} does not return run({ENTRY.processor}, ...)",
        )

    def _trace_harness(self) -> Harness:
        processor = self._require(Shape.PROCESSOR)
        found = self.codebase.definition(processor.module, processor.run.func.id)
        if found is None:
            raise Untraceable(
                Shape.HARNESS,
                f"{processor.module.path}: `{processor.run.func.id}` is not a function in "
                f"{HOMES} sources",
            )
        module, function = found
        built = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Dict)
            and module.environ_reads(node.value)
        ]
        if len(built) != 1:
            raise Untraceable(
                Shape.HARNESS,
                f"{module.path}: {function.name} does not build exactly one dict of "
                f"os.environ reads",
            )
        env = built[0].targets[0].id
        keys = [string(key) for key in built[0].value.keys]
        if None in keys:
            raise Untraceable(
                Shape.HARNESS,
                f"{module.path}:{built[0].lineno} env dict has a non-literal key",
            )
        for node in ast.walk(function):
            aliased = isinstance(node, ast.Assign) and named(node.value, env)
            spread = isinstance(node, ast.keyword) and node.arg is None
            if aliased or (spread and named(node.value, env)):
                raise Untraceable(
                    Shape.HARNESS,
                    f"{module.path}:{node.lineno} hands its env dict on through an alias "
                    f"or `**`",
                )
        handoffs = self._handoffs(function, env)
        return Harness(
            processor,
            found,
            frozenset(keys),
            module.environ_reads(function),
            self._recipients(processor, function, handoffs),
        )

    def _handoffs(self, function: ast.FunctionDef, env: str) -> tuple[Handoff, ...]:
        """Every call of a harness parameter with the env dict, requiring the processor's."""
        callable_names = parameters(function)
        handoffs = []
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in callable_names:
                continue
            handoffs += [
                Handoff(node.func.id, position, None)
                for position, arg in enumerate(node.args)
                if named(arg, env)
            ]
            handoffs += [
                Handoff(node.func.id, None, keyword.arg)
                for keyword in node.keywords
                if keyword.arg and named(keyword.value, env)
            ]
        first = positional(function)[:1]
        if not first or Handoff(first[0], ENTRY.env_position, None) not in handoffs:
            raise Untraceable(
                Shape.HARNESS,
                f"{function.name} does not pass its env dict as the last argument of "
                f"{ENTRY.processor_signature}",
            )
        return tuple(handoffs)

    def _recipients(
        self,
        processor: Processor,
        function: ast.FunctionDef,
        handoffs: tuple[Handoff, ...],
    ) -> tuple[Recipient, ...]:
        """Each handoff resolved to what `main` passes for it; a parameter left unpassed, or
        passed `None`, receives nothing."""
        out = []
        for handoff in handoffs:
            passed = processor.passes(function, handoff.parameter)
            if passed is None or (
                isinstance(passed, ast.Constant) and passed.value is None
            ):
                continue
            definition = (
                self.codebase.definition(processor.module, passed.id)
                if isinstance(passed, ast.Name)
                else None
            )
            out.append(Recipient.of(handoff, passed, definition))
        return tuple(out)

    def _trace_parser(self) -> Parser:
        module = self._module(Shape.PARSER, self.workload.setup_module)
        main = self._main(Shape.PARSER, module)
        assigns = [
            node
            for node in ast.walk(main)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        ]
        parsers = {
            target.id
            for node in assigns
            if module.qualified(node.value.func) == self.ARGUMENT_PARSER
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        if len(parsers) != 1:
            raise Untraceable(
                Shape.PARSER,
                f"{module.path}: {ENTRY.main} does not build exactly one ArgumentParser",
            )
        holders = self._holders(assigns, parsers)
        calls = sorted(
            (
                node
                for node in ast.walk(main)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in holders
            ),
            key=lambda node: (node.lineno, node.col_offset),
        )
        positionals, options = [], []
        for call in (call for call in calls if call.func.attr == "add_argument"):
            strings = [string(arg) for arg in call.args]
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            action = string(keywords["action"]) if "action" in keywords else "store"
            required = self._flag(keywords.get("required"))
            if not strings or None in strings or action is None or required is None:
                raise Untraceable(
                    Shape.PARSER,
                    f"{module.path}:{call.lineno} add_argument is not literal",
                )
            if strings[0].startswith("-"):
                options.append(Option(tuple(strings), action, required))
            else:
                nargs = keywords.get("nargs")
                optional = (
                    isinstance(nargs, ast.Constant) and nargs.value in self.OPTIONAL_NARGS
                )
                positionals.append(Positional(strings[0], optional))
        if not any(
            call.func.attr in self.PARSES and call.func.value.id in parsers
            for call in calls
        ):
            raise Untraceable(
                Shape.PARSER, f"{module.path}: {ENTRY.main} never parses its arguments"
            )
        return Parser(tuple(positionals), tuple(options))

    def _holders(self, assigns: list[ast.Assign], parsers: set[str]) -> frozenset[str]:
        """The parser's name, and every argument group taken from it, however nested."""
        holders = set(parsers)
        while True:
            grown = {
                target.id
                for node in assigns
                if isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in self.GROUPS
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id in holders
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            if grown <= holders:
                return frozenset(holders)
            holders |= grown

    @staticmethod
    def _flag(node: ast.expr | None) -> bool | None:
        """A literal boolean keyword's value, `False` when absent, `None` when not literal."""
        if node is None:
            return False
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return node.value
        return None
