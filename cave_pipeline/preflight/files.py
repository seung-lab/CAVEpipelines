"""An image's filesystem, streamed layer by layer into what the contract reads of it."""

from __future__ import annotations

import http.client
import posixpath
import re
import sys
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..contract import STANDARD
from .registry import ImageConfig, Registry
from .utils import Unreachable


class Overlay:
    """The image's filesystem, assembled newest layer first.

    A path is taken from the newest layer holding it, and a whiteout hides what older layers
    hold beneath it. Both reach only layers older than the one declaring them."""

    WHITEOUT = ".wh."
    OPAQUE = ".wh..wh..opq"

    def __init__(self) -> None:
        self._taken: set[str] = set()
        self._hidden: set[str] = set()
        self._opaque: set[str] = set()
        self._layer: set[str] = set()
        self._layer_hidden: set[str] = set()
        self._layer_opaque: set[str] = set()

    def admit(self, path: str) -> bool:
        """Whether `path`, met in the layer being read, is part of the image."""
        parent, base = posixpath.split(path)
        if base == self.OPAQUE:
            self._layer_opaque.add(parent)
        elif base.startswith(self.WHITEOUT):
            self._layer_hidden.add(posixpath.join(parent, base[len(self.WHITEOUT) :]))
        elif path not in self._taken and not self._covered(path):
            self._layer.add(path)
            return True
        return False

    def close_layer(self) -> None:
        self._taken |= self._layer
        self._hidden |= self._layer_hidden
        self._opaque |= self._layer_opaque
        self._layer, self._layer_hidden, self._layer_opaque = set(), set(), set()

    def _covered(self, path: str) -> bool:
        if path in self._hidden:
            return True
        while path != "/":
            path = posixpath.dirname(path)
            if path in self._hidden or path in self._opaque:
                return True
        return False


class Installation:
    """What the image installs, gathered while its layers stream: the modules of the
    virtualenv `$VIRTUAL_ENV` names, as dotted names, and each standard library it holds."""

    LIB = re.compile(r"/lib/python3\.(\d+)/(.+)$")
    METADATA = (".dist-info", ".egg-info", ".egg-link", ".pth")
    MODULES = (".py", ".so", ".pyd")
    NOT_STDLIB = frozenset({"dist-packages", "__pycache__"})

    def __init__(self, venv: str):
        self.venv = venv
        self.site = ""  # the virtualenv's site-packages, once a layer holds it
        self.python: tuple[int, int] | None = None  # the Python that site-packages is for
        self._modules: set[str] = set()  # module files, pure or extension
        self._packages: set[str] = set()  # directories holding an `__init__.py`
        self._directories: set[str] = set()  # every directory, namespace packages too
        self._stdlib: dict[int, set[str]] = {}

    def add(self, path: str, is_dir: bool) -> str:
        """Record one image path; returns it relative to the virtualenv's site-packages, or
        empty outside it."""
        found = self.LIB.search(path)
        if not found:
            return ""
        minor, parts = int(found.group(1)), found.group(2).split("/")
        if parts[0] != "site-packages":
            self._add_stdlib(minor, parts)
            return ""
        if len(parts) == 1 or not (self.venv and path.startswith(f"{self.venv}/")):
            return ""
        self.site, self.python = path[: found.start(2)] + parts[0], (3, minor)
        self._add_site(parts[1:], is_dir)
        return "/".join(parts[1:])

    def provides(self, module: str, names: tuple[str, ...]) -> bool:
        """Whether an import of `names` from `module` resolves: in the image's standard
        library, the operator's standing in only when the image's was not found, or with each
        package on its path in the virtualenv, a name from a namespace package a module."""
        top = module.partition(".")[0]
        stdlib = self._stdlib.get(self.python[1]) if self.python else None
        if top in (stdlib or sys.stdlib_module_names) or top in sys.builtin_module_names:
            return True
        parts = module.split(".")
        for depth in range(1, len(parts) + 1):
            prefix = ".".join(parts[:depth])
            if prefix in self._modules:
                return True  # anything below it is an attribute of that module
            if prefix not in self._directories:
                return False
        return module in self._packages or all(
            f"{module}.{name}" in self._modules or f"{module}.{name}" in self._directories
            for name in names
        )

    def _add_site(self, parts: list[str], is_dir: bool) -> None:
        if any(part == "__pycache__" or part.endswith(self.METADATA) for part in parts):
            return
        folders = parts if is_dir else parts[:-1]
        self._directories.update(".".join(folders[: n + 1]) for n in range(len(folders)))
        if is_dir:
            return
        if parts[-1] == "__init__.py":
            self._packages.add(".".join(folders))
        elif parts[-1].endswith(self.MODULES):
            self._modules.add(".".join([*folders, parts[-1].partition(".")[0]]))

    def _add_stdlib(self, minor: int, parts: list[str]) -> None:
        top = parts[0]
        if top in self.NOT_STDLIB or top.startswith("config-"):
            return
        if top == "lib-dynload":
            name = parts[1].partition(".")[0] if len(parts) > 1 else ""
        elif len(parts) == 1:
            name = top.removesuffix(".py") if top.endswith(".py") else ""
        else:
            name = top
        if name:
            self._stdlib.setdefault(minor, set()).add(name)


@dataclass(frozen=True)
class ImageFiles:
    """What the contract reads of an image: its config, what it installs, and the sources of
    its root package and of the harness homes in its virtualenv."""

    config: ImageConfig
    installed: Installation
    sources: Mapping[str, str]

    # the root package runs from the working directory, so a virtualenv copy never runs
    INSTALLED_HOMES = tuple(
        f"{home}/" for home in STANDARD.harness_homes if home != STANDARD.root_package
    )
    READ_ERRORS = (tarfile.TarError, EOFError, OSError, http.client.HTTPException)

    @classmethod
    def read(cls, registry: Registry) -> ImageFiles:
        config = registry.config()
        installed = Installation(config.env.get(STANDARD.venv_env, ""))
        app = posixpath.join(config.workdir, STANDARD.root_package) + "/"
        overlay, sources = Overlay(), {}
        try:
            for digest in registry.layers():
                with tarfile.open(fileobj=registry.blob(digest), mode="r|gz") as layer:
                    for member in layer:
                        path = posixpath.normpath("/" + member.name)
                        if not overlay.admit(path):
                            continue
                        in_site = installed.add(path, member.isdir())
                        kept = path.startswith(app) or in_site.startswith(
                            cls.INSTALLED_HOMES
                        )
                        if kept and member.isfile() and path.endswith(".py"):
                            text = layer.extractfile(member).read()
                            sources[path] = text.decode("utf-8", "replace")
                overlay.close_layer()
        except cls.READ_ERRORS as exc:
            raise Unreachable(f"a layer stopped streaming: {exc!r}")
        return cls(config, installed, MappingProxyType(sources))
