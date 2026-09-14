"""One public Docker Hub image, read anonymously through the registry API."""

from __future__ import annotations

import http.client
import json
import posixpath
import urllib.error
import urllib.request
from collections.abc import Mapping
from functools import cached_property
from types import MappingProxyType
from typing import IO, NamedTuple

from .utils import Unreachable, Unreadable


class ImageRef(NamedTuple):
    """A Docker Hub image: its repository and a tag or digest."""

    repository: str
    reference: str

    HOSTS = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})

    @classmethod
    def parse(cls, image: str) -> ImageRef:
        """`[docker.io/]name[:tag][@digest]`; any other registry host is refused."""
        named, _, digest = image.strip().partition("@")
        host, _, path = named.partition("/")
        if path and ("." in host or ":" in host or host == "localhost"):
            if host not in cls.HOSTS:
                raise Unreadable(
                    f"{host} is not Docker Hub, the only registry the contract reads"
                )
            named = path
        name, colon, tag = named.rpartition(":")
        if not colon or "/" in tag:
            name, tag = named, "latest"
        if not name:
            raise Unreadable(f"{image!r} names no image")
        return cls(name if "/" in name else f"library/{name}", digest or tag)


class Layer(NamedTuple):
    digest: str
    media_type: str


class Manifest(NamedTuple):
    """One platform's image manifest: its config blob, and its layers oldest first."""

    config: str
    layers: tuple[Layer, ...]


class ImageConfig(NamedTuple):
    """What the contract reads of an image config."""

    platform: tuple[str, str]
    workdir: str
    env: Mapping[str, str]


class Registry:
    """Serves one image's config and layers, sorting every failure into the image's fault
    (`Unreadable`) or Docker Hub's (`Unreachable`)."""

    TOKEN = (
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:{}:pull"
    )
    URL = "https://registry-1.docker.io/v2/{}/{}/{}"
    MANIFESTS = (
        "application/vnd.oci.image.index.v1+json, "
        "application/vnd.docker.distribution.manifest.list.v2+json, "
        "application/vnd.oci.image.manifest.v1+json, "
        "application/vnd.docker.distribution.manifest.v2+json"
    )
    GZIP_LAYERS = frozenset(
        {
            "application/vnd.oci.image.layer.v1.tar+gzip",
            "application/vnd.docker.image.rootfs.diff.tar.gzip",
        }
    )
    PLATFORM = ("linux", "amd64")
    ABSENT = frozenset({401, 403, 404})  # the answers for an image missing or private
    TIMEOUT_S = 60

    def __init__(self, image: str):
        self.ref = ImageRef.parse(image)

    def config(self) -> ImageConfig:
        raw = self._json("blobs", self._manifest.config)
        settings = raw.get("config") or {}
        env = dict(entry.partition("=")[::2] for entry in settings.get("Env") or [])
        return ImageConfig(
            platform=(raw.get("os", ""), raw.get("architecture", "")),
            workdir=posixpath.normpath(settings.get("WorkingDir") or "/"),
            env=MappingProxyType(env),
        )

    def layers(self) -> list[str]:
        """Layer digests, newest first."""
        for layer in self._manifest.layers:
            if layer.media_type not in self.GZIP_LAYERS:
                raise Unreadable(f"layer {layer.digest} is {layer.media_type}, not gzip")
        return [layer.digest for layer in reversed(self._manifest.layers)]

    def blob(self, digest: str) -> IO[bytes]:
        return self._get("blobs", digest)

    @cached_property
    def _token(self) -> str:
        url = self.TOKEN.format(self.ref.repository)
        with self._open(url, "a pull token", absent=frozenset()) as reply:
            try:
                return json.load(reply)["token"]
            except (ValueError, KeyError, OSError, http.client.HTTPException) as exc:
                raise Unreachable(f"no Docker Hub pull token: {exc!r}")

    @cached_property
    def _manifest(self) -> Manifest:
        found = self._json("manifests", self.ref.reference, self.MANIFESTS)
        if "manifests" in found:  # an index: take the contract's platform
            digests = [
                entry.get("digest")
                for entry in found["manifests"]
                for platform in [entry.get("platform") or {}]
                if (platform.get("os"), platform.get("architecture")) == self.PLATFORM
            ]
            if not digests:
                raise Unreadable(
                    f"{self.ref.repository} has no {'/'.join(self.PLATFORM)} image"
                )
            found = self._json("manifests", digests[0], self.MANIFESTS)
        try:
            layers = tuple(
                Layer(layer["digest"], layer.get("mediaType", ""))
                for layer in found.get("layers") or []
            )
            return Manifest(found["config"]["digest"], layers)
        except (KeyError, TypeError, AttributeError) as exc:
            raise Unreachable(f"Docker Hub returned a malformed manifest: {exc!r}")

    def _json(self, kind: str, reference: str, accept: str = "") -> Mapping[str, object]:
        with self._get(kind, reference, accept) as reply:
            try:
                found = json.load(reply)
            except (ValueError, OSError, http.client.HTTPException) as exc:
                raise Unreachable(f"Docker Hub returned unreadable {kind}: {exc!r}")
        if not isinstance(found, dict):
            raise Unreachable(f"Docker Hub returned malformed {kind}")
        return found

    def _get(self, kind: str, reference: str, accept: str = "") -> IO[bytes]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if accept:
            headers["Accept"] = accept
        url = self.URL.format(self.ref.repository, kind, reference)
        return self._open(
            urllib.request.Request(url, headers=headers), f"{kind} {reference}"
        )

    def _open(
        self,
        request: urllib.request.Request | str,
        asked: str,
        absent: frozenset[int] = ABSENT,
    ) -> IO[bytes]:
        """One request; an `absent` status blames the image, any other failure Docker Hub."""
        try:
            return urllib.request.urlopen(request, timeout=self.TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            if exc.code in absent:
                raise Unreadable(
                    f"Docker Hub answered {exc.code} for {self.ref.repository} {asked}: "
                    f"the image must exist and be public"
                )
            raise Unreachable(self._refusal(exc, asked))
        except (OSError, http.client.HTTPException) as exc:
            raise Unreachable(f"cannot reach Docker Hub: {exc!r}")

    @staticmethod
    def _refusal(exc: urllib.error.HTTPError, asked: str) -> str:
        """Why Docker Hub refused a request the image itself is not to blame for."""
        if exc.code != 429:
            return f"Docker Hub answered {exc.code} for {asked}"
        headers = exc.headers
        limit = headers.get("ratelimit-limit", "unknown") if headers else "unknown"
        wait = headers.get("retry-after") if headers else None
        after = f"retry after {wait} s" if wait else "retry later"
        return f"Docker Hub's anonymous pull limit ({limit}) is spent for this address; {after}"
