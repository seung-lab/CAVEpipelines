"""The image's kept sources, parsed as its own Python would parse and import them."""

from __future__ import annotations

import ast
import posixpath
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from typing import NamedTuple

from ..contract import STANDARD
from .files import ImageFiles
from .utils import named, positional, string


class Origin(NamedTuple):
    """Where a module-level `from module import name` binding comes from."""

    module: str
    name: str


class Import(NamedTuple):
    """One import a module executes: the module, the names taken from it, and its line."""

    module: str
    names: tuple[str, ...]
    line: int


class EnvRead(NamedTuple):
    """One `os.environ` read: its name and default, `None` where either is not a literal;
    `required` when a missing name raises."""

    name: str | None
    default: object
    required: bool
    line: int


class Definition(NamedTuple):
    """A function and the module defining it."""

    module: Module
    function: ast.FunctionDef


@dataclass(frozen=True)
class Module:
    """One parsed source of the image, and what its names are bound to."""

    name: str
    path: str
    tree: ast.Module

    ENVIRON = "os.environ"
    GETENV = "os.getenv"
    IMPORT_ERRORS = frozenset({"ImportError", "ModuleNotFoundError"})
    CATCH_ALL = frozenset({"BaseException"})
    FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
    TRIES = (ast.Try, ast.TryStar)

    @property
    def package(self) -> str:
        """The package its relative imports resolve against."""
        if self.path.endswith("/__init__.py"):
            return self.name
        return self.name.rpartition(".")[0]

    @cached_property
    def statements(self) -> tuple[ast.stmt, ...]:
        """Module-level statements, with those inside module-level `if`, `try` and `with`
        blocks, whose bindings are module-level too; never a `TYPE_CHECKING` block, which
        does not run."""
        out, pending = [], list(self.tree.body)
        while pending:
            node = pending.pop()
            out.append(node)
            if isinstance(node, ast.If):
                checking = self._type_checking(node.test)
                pending += node.orelse if checking else [*node.body, *node.orelse]
            elif isinstance(node, ast.With):
                pending += node.body
            elif isinstance(node, self.TRIES):
                pending += [*node.body, *node.orelse, *node.finalbody]
                pending += [stmt for handler in node.handlers for stmt in handler.body]
        return tuple(out)

    @cached_property
    def origins(self) -> dict[str, Origin]:
        return {
            alias.asname or alias.name: Origin(self.absolute(node), alias.name)
            for node in self.statements
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name != "*"
        }

    @cached_property
    def aliases(self) -> dict[str, str]:
        """Names `import` binds to modules: `import a.b` binds `a`, `import a.b as x` `x`."""
        out = {}
        for node in self.statements:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.partition(".")[0]
                    out[alias.asname or top] = alias.name if alias.asname else top
        return out

    @cached_property
    def functions(self) -> dict[str, ast.FunctionDef]:
        return {n.name: n for n in self.statements if isinstance(n, self.FUNCTIONS)}

    @cached_property
    def bindings(self) -> frozenset[str]:
        names = {*self.functions, *self.origins, *self.aliases}
        for node in self.statements:
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(
                    n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)
                )
        return frozenset(names)

    @cached_property
    def open_ended(self) -> bool:
        """Whether it binds names no reading can list: a star import, `__getattr__`, or
        module-level code handed `globals()`, as generated protobuf modules do."""
        if "__getattr__" in self.functions:
            return True
        for node in self.statements:
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "*" for alias in node.names
            ):
                return True
            if not isinstance(node, (*self.FUNCTIONS, ast.ClassDef)) and any(
                isinstance(call, ast.Call) and named(call.func, "globals")
                for call in ast.walk(node)
            ):
                return True
        return False

    def binds(self, name: str) -> bool:
        return self.open_ended or name in self.bindings

    def absolute(self, node: ast.ImportFrom) -> str:
        """The module a `from ... import` names, relative level resolved; empty above the
        top-level package."""
        if not node.level:
            return node.module or ""
        parts = self.package.split(".")
        if node.level > len(parts):
            return ""
        base = parts[: len(parts) - node.level + 1]
        return ".".join([*base, node.module] if node.module else base)

    def qualified(self, expr: ast.expr) -> str:
        """The dotted name an expression refers to through this module's imports, or empty."""
        if isinstance(expr, ast.Name):
            if expr.id in self.origins:
                origin = self.origins[expr.id]
                return f"{origin.module}.{origin.name}"
            return self.aliases.get(expr.id, "")
        if isinstance(expr, ast.Attribute):
            base = self.qualified(expr.value)
            return f"{base}.{expr.attr}" if base else ""
        return ""

    def main_guard(self) -> ast.If | None:
        """The module-level `if __name__ == "__main__":` block, either operand order."""
        for node in self.tree.body:
            test = node.test if isinstance(node, ast.If) else None
            if not (
                isinstance(test, ast.Compare)
                and [type(op) for op in test.ops] == [ast.Eq]
            ):
                continue
            sides = (test.left, test.comparators[0])
            if any(
                named(name, "__name__") and string(literal) == "__main__"
                for name, literal in (sides, sides[::-1])
            ):
                return node
        return None

    def exits_through(self, function: ast.FunctionDef, dotted: str) -> bool:
        """Whether `function` always leaves through the call `dotted` names: in a top-level
        `finally`, or as a top-level call no earlier `return` or `raise` skips, with every
        call of its first parameter inside a `try` whose handler catches everything."""
        body = function.body
        if any(
            isinstance(stmt, self.TRIES) and self._calls_at_top(stmt.finalbody, dotted)
            for stmt in body
        ):
            return True
        exits = [i for i, stmt in enumerate(body) if self._calls_at_top([stmt], dotted)]
        if not exits or any(
            isinstance(stmt, (ast.Return, ast.Raise)) for stmt in body[: exits[0]]
        ):
            return False
        parameters = positional(function)
        if not parameters:
            return True
        guarded = {
            id(node)
            for stmt in body[: exits[0]]
            if isinstance(stmt, self.TRIES)
            and any(self._caught(handler) & self.CATCH_ALL for handler in stmt.handlers)
            for inner in stmt.body
            for node in ast.walk(inner)
        }
        return all(
            id(node) in guarded
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and named(node.func, parameters[0])
        )

    def environ_reads(self, scope: ast.AST) -> tuple[EnvRead, ...]:
        """Every `os.environ[...]`, `os.environ.get(...)` and `os.getenv(...)` read in
        `scope`; writes and deletes are not reads."""
        reads = []
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Load)
                and self.qualified(node.value) == self.ENVIRON
            ):
                reads.append(EnvRead(string(node.slice), None, True, node.lineno))
            elif (
                isinstance(node, ast.Call)
                and node.args
                and self._reads_environ(node.func)
            ):
                default = node.args[1] if len(node.args) > 1 else None
                value = default.value if isinstance(default, ast.Constant) else None
                reads.append(EnvRead(string(node.args[0]), value, False, node.lineno))
        return tuple(reads)

    def imports(self) -> tuple[Import, ...]:
        """Every import the module can execute; ones inside `if TYPE_CHECKING:`, or a `try`
        whose handler names an import failure, are optional and left out."""
        found, pending = [], [(self.tree, False)]
        while pending:
            node, optional = pending.pop()
            if not optional and isinstance(node, ast.Import):
                found += [Import(alias.name, (), node.lineno) for alias in node.names]
            elif (
                not optional
                and isinstance(node, ast.ImportFrom)
                and node.module != "__future__"
            ):
                names = tuple(alias.name for alias in node.names if alias.name != "*")
                found.append(Import(self.absolute(node), names, node.lineno))
            pending += self._children(node, optional)
        return tuple(sorted(found, key=lambda one: one.line))

    def _calls_at_top(self, body: list[ast.stmt], dotted: str) -> bool:
        return any(
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and self.qualified(stmt.value.func) == dotted
            for stmt in body
        )

    def _reads_environ(self, func: ast.expr) -> bool:
        if self.qualified(func) == self.GETENV:
            return True
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and self.qualified(func.value) == self.ENVIRON
        )

    def _children(self, node: ast.AST, optional: bool) -> list[tuple[ast.AST, bool]]:
        if isinstance(node, self.TRIES) and any(
            self._caught(handler) & self.IMPORT_ERRORS for handler in node.handlers
        ):
            rest = [*node.handlers, *node.orelse, *node.finalbody]
            return [(child, True) for child in node.body] + [(c, optional) for c in rest]
        if isinstance(node, ast.If) and self._type_checking(node.test):
            guarded = [(child, True) for child in node.body]
            return guarded + [(child, optional) for child in node.orelse]
        return [(child, optional) for child in ast.iter_child_nodes(node)]

    @classmethod
    def _caught(cls, handler: ast.ExceptHandler) -> frozenset[str]:
        """The exception names a handler lists; a bare `except:` catches everything."""
        if handler.type is None:
            return cls.CATCH_ALL
        listed = (
            handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        )
        return frozenset(one.id for one in listed if isinstance(one, ast.Name))

    @staticmethod
    def _type_checking(test: ast.expr) -> bool:
        """`TYPE_CHECKING` or `typing.TYPE_CHECKING`, read without resolving imports, which
        are themselves built from the statements this decides."""
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING" and named(test.value, "typing")
        return named(test, "TYPE_CHECKING")


class Codebase:
    """The image's kept sources as modules, found the way the image's `python -m` finds
    them: its working directory first, then its virtualenv. Syntax and the standard library
    are the image's Python, not the operator's."""

    def __init__(self, files: ImageFiles):
        self._files = files
        roots = (files.config.workdir, files.installed.site)
        self._roots = tuple(root for root in roots if root)
        self._modules: dict[str, Module | None] = {}
        self._unparseable: dict[str, str] = {}

    @property
    def python(self) -> tuple[int, int] | None:
        return self._files.installed.python

    def module(self, name: str) -> Module | None:
        if name not in self._modules:
            self._modules[name] = self._load(name)
        return self._modules[name]

    def missing(self, name: str) -> str:
        """Why `module(name)` found nothing."""
        return self._unparseable.get(name, f"no module {name}")

    def kept(self, name: str) -> bool:
        """Whether `name` belongs to a package whose sources the contract reads."""
        return name.partition(".")[0] in STANDARD.harness_homes

    def provides(self, one: Import) -> bool:
        """Whether a third-party import resolves in the image."""
        return self._files.installed.provides(one.module, one.names)

    def definition(self, module: Module, name: str) -> Definition | None:
        """The function `name` refers to in `module`, following `from ... import`s."""
        seen = set()
        while (module.name, name) not in seen:
            seen.add((module.name, name))
            if name in module.functions:
                return Definition(module, module.functions[name])
            origin = module.origins.get(name)
            target = self.module(origin.module) if origin else None
            if target is None:
                return None
            module, name = target, origin.name
        return None

    def _load(self, name: str) -> Module | None:
        relative = name.replace(".", "/")
        for root in self._roots:
            for path in (
                posixpath.join(root, f"{relative}.py"),
                posixpath.join(root, relative, "__init__.py"),
            ):
                text = self._files.sources.get(path)
                if text is None:
                    continue
                try:
                    return Module(name, path, self._parse(text, path))
                except SyntaxError as exc:
                    self._unparseable[name] = self._unparsed(path, exc)
                    return None
        return None

    def _parse(self, text: str, path: str) -> ast.Module:
        image = self.python
        if image and image <= sys.version_info[:2]:
            return ast.parse(text, filename=path, feature_version=image)
        return ast.parse(text, filename=path)

    def _unparsed(self, path: str, exc: SyntaxError) -> str:
        image, host = self.python, sys.version_info[:2]
        where = f"{path}:{exc.lineno}"
        if image and image > host:
            return (
                f"{where} does not parse under the operator's Python {host[0]}.{host[1]}, "
                f"older than the image's {image[0]}.{image[1]}: {exc.msg}"
            )
        version = image or host
        return f"{where} is not valid Python {version[0]}.{version[1]}: {exc.msg}"


@dataclass(frozen=True)
class Closure:
    """The imports reachable from the entrypoints and probe that fail to resolve."""

    unresolved: tuple[str, ...]
    uninstalled: tuple[str, ...]

    @classmethod
    def walk(cls, codebase: Codebase, roots: Iterable[str]) -> Closure:
        pending = [name for name in dict.fromkeys(roots) if codebase.module(name)]
        seen, unresolved, uninstalled = set(pending), [], []
        while pending:
            module = codebase.module(pending.pop())
            for one in module.imports():
                where = f"{module.path}:{one.line}"
                if not one.module:
                    unresolved.append(f"{where} imports above its top-level package")
                elif not codebase.kept(one.module):
                    if not codebase.provides(one):
                        uninstalled.append(
                            f"{where} imports {one.module}, which the image does not install"
                        )
                else:
                    reached, problem = cls._resolve(codebase, one)
                    if problem:
                        unresolved.append(f"{where} {problem}")
                    pending += sorted(reached - seen)
                    seen |= reached
        return cls(tuple(unresolved), tuple(uninstalled))

    @staticmethod
    def _resolve(codebase: Codebase, one: Import) -> tuple[set[str], str]:
        """The modules one import of kept code runs, and why it fails, or empty."""
        parts = one.module.split(".")
        packages = [".".join(parts[: depth + 1]) for depth in range(len(parts))]
        for name in packages:
            if codebase.module(name) is None:
                return set(), f"imports {one.module}: {codebase.missing(name)}"
        reached, target = set(packages), codebase.module(one.module)
        for name in one.names:
            submodule = f"{one.module}.{name}"
            if codebase.module(submodule) is not None:
                reached.add(submodule)
            elif not target.binds(name):
                return (
                    reached,
                    f"imports {name} from {one.module}, which binds no such name",
                )
        return reached, ""
