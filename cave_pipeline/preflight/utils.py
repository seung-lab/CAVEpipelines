"""What the preflight's modules share: how a read of the image ends early, the contract
names every message uses, and the few ways a piece of syntax is read."""

from __future__ import annotations

import ast

from ..contract import STANDARD

HOMES = " or ".join(STANDARD.harness_homes)
ENTRY = STANDARD.entrypoints


class Unreadable(Exception):
    """The image is not one the contract can read: absent, private, outside Docker Hub, or
    without a linux/amd64 gzip image. It breaks the Published clause."""


class Unreachable(Exception):
    """Docker Hub could not serve the image right now, so it went unchecked."""


def string(node: ast.AST | None) -> str | None:
    """A string literal's value, or `None` for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def named(node: ast.AST | None, name: str) -> bool:
    """Whether `node` is the bare name `name`."""
    return isinstance(node, ast.Name) and node.id == name


def positional(function: ast.FunctionDef) -> list[str]:
    """Its positional parameter names, in order."""
    return [arg.arg for arg in (*function.args.posonlyargs, *function.args.args)]


def parameters(function: ast.FunctionDef) -> frozenset[str]:
    """Every parameter name a caller can pass or a body can call."""
    return frozenset((*positional(function), *(a.arg for a in function.args.kwonlyargs)))
