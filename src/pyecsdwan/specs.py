"""Shared read-only view over the vendored API specs (epic #6).

One place that knows how to find ``specs/``, parse the two OpenAPI baselines,
and enumerate the endpoint universe. Everything in the Tier-1 pipeline reads
through here so the CLI, the codegen tools, and the coverage report can never
disagree about what an endpoint *is*:

* ``ec-cli show coverage`` (#28) — every known endpoint x tier, offline.
* ``tools/gen_models.py`` (#26) — pydantic models + typed bindings.
* ``tools/gen_plugin.py`` (#27) — Tier-1 plugin stubs.

Two deliberate design points:

* **Offline by construction.** Nothing here touches the network. A missing
  ``specs/`` directory degrades to an empty universe rather than raising, so
  ``show coverage`` still works from an installed wheel that ships no specs.
* **Path normalization is the join key.** The specs, the Postman collections,
  and a live URL all spell path parameters differently (``{nePk}``, ``:nePk``,
  ``{{nePk}}``). :func:`normalize_path` reduces all three to ``{}`` so the same
  endpoint from two sources compares equal. Keep raw paths for display, joins
  on the normalized form.

``$ref`` resolution is bounded and cycle-safe: EdgeConnect's schemas are deeply
self-referential (a template contains templates), so :func:`resolve_schema`
stops at ``MAX_REF_DEPTH`` and leaves a ``{"$ref": ...}`` marker behind rather
than recursing forever.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

JsonDict = dict[str, Any]

Scope = Literal["orchestrator", "appliance"]
SCOPES: tuple[Scope, ...] = ("orchestrator", "appliance")

#: Environment override for the spec directory (mirrors tools/spec_sync.py).
ENV_SPECS_DIR = "ECSDWAN_SPECS_DIR"
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
#: Guard against EdgeConnect's mutually recursive component schemas.
MAX_REF_DEPTH = 6
#: Distilled Postman payload examples (issue #51); optional.
PAYLOAD_EXAMPLES_GLOB = "payload-examples-*.json"

_PATH_PARAM = re.compile(r"\{\{[^}]+\}\}|\{[^}]*\}|:[A-Za-z_]\w*")


def normalize_path(path: str) -> str:
    """Reduce a path to its parameter-agnostic join key.

    ``/appliance/{nePk}``, ``/appliance/:nePk`` and ``/appliance/{{nePk}}`` all
    become ``/appliance/{}``. Query strings and trailing slashes are dropped,
    and a leading slash is added when missing: appliance-scope resource modules
    write ECOS paths relative (``"bgp/config/system"``, because the proxy's
    ``url`` param takes them that way) while the spec writes them absolute, and
    those two must join.
    """
    collapsed = _PATH_PARAM.sub("{}", path.split("?")[0].rstrip("/"))
    if not collapsed:
        return "/"
    return collapsed if collapsed.startswith("/") else f"/{collapsed}"


def endpoint_key(scope: str, method: str, path: str) -> str:
    """Stable identity for one operation, e.g. ``orchestrator GET /gms/grNodes``."""
    return f"{scope} {method.upper()} {normalize_path(path)}"


def specs_dir() -> Path | None:
    """Locate the vendored spec directory, or ``None`` when unavailable.

    Order: ``$ECSDWAN_SPECS_DIR`` -> ``<package>/_specs`` (present only if a
    build ships them as package data) -> the repo-root ``specs/`` directory.
    """
    override = os.environ.get(ENV_SPECS_DIR)
    if override:
        candidate = Path(override)
        return candidate if candidate.is_dir() else None
    here = Path(__file__).resolve()
    for candidate in (here.parent / "_specs", here.parents[2] / "specs"):
        if candidate.is_dir():
            return candidate
    return None


def baseline_path(scope: Scope) -> Path | None:
    """Path to a scope's vendored baseline, e.g. ``orchestrator-openapi-7.2.0.json``."""
    directory = specs_dir()
    if directory is None:
        return None
    matches = sorted(directory.glob(f"{scope}-openapi-*.json"))
    return matches[0] if matches else None


@functools.lru_cache(maxsize=len(SCOPES))
def load_spec(scope: Scope) -> JsonDict:
    """Parse and cache one baseline. Returns ``{}`` when it isn't vendored."""
    path = baseline_path(scope)
    if path is None:
        return {}
    with path.open(encoding="utf-8") as handle:
        spec: JsonDict = json.load(handle)
    return spec


def spec_version(scope: Scope) -> str:
    """``info.version`` of a baseline, or ``""`` when unavailable."""
    info = load_spec(scope).get("info")
    return str(info.get("version", "")) if isinstance(info, dict) else ""


# ---------------------------------------------------------------------------
# $ref resolution


def resolve_ref(spec: JsonDict, ref: str) -> JsonDict | None:
    """Look up a local JSON pointer.

    Walks the document rather than assuming a layout, so OpenAPI 3
    (``#/components/schemas/Name``) and Swagger 2 (``#/definitions/Name``)
    both resolve — the two vendored baselines are not consistent about it.
    """
    if not ref.startswith("#/"):
        return None
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def resolve_schema(spec: JsonDict, schema: Any, *, depth: int = MAX_REF_DEPTH) -> Any:
    """Inline ``$ref``s up to *depth*, leaving deeper ones as ``$ref`` markers.

    Composition keywords (``allOf``/``anyOf``/``oneOf``) are resolved in place
    but not merged — callers decide how to interpret a union, and silently
    flattening one would invent a shape the API never promised.
    """
    if depth <= 0 or not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = resolve_ref(spec, ref)
        if target is None:
            return schema
        resolved = resolve_schema(spec, target, depth=depth - 1)
        extra = {k: v for k, v in schema.items() if k != "$ref"}
        if extra and isinstance(resolved, dict):
            return {**resolved, **extra}
        return resolved
    out: JsonDict = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            out[key] = {
                name: resolve_schema(spec, sub, depth=depth - 1) for name, sub in value.items()
            }
        elif key in ("items", "additionalProperties"):
            out[key] = resolve_schema(spec, value, depth=depth - 1)
        elif key in ("allOf", "anyOf", "oneOf") and isinstance(value, list):
            out[key] = [resolve_schema(spec, sub, depth=depth - 1) for sub in value]
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Endpoint universe


@dataclasses.dataclass(frozen=True)
class Endpoint:
    """One spec operation, flattened into the fields the pipeline needs."""

    scope: Scope
    method: str
    #: Raw path as written in the spec, e.g. ``/appliance/{nePk}``.
    path: str
    operation_id: str = ""
    summary: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    deprecated: bool = False
    #: The operation object, verbatim; ``$ref``s unresolved.
    operation: JsonDict = dataclasses.field(default_factory=dict, repr=False)
    #: Path-level ``parameters``, shared by every method on the path.
    path_parameters: tuple[JsonDict, ...] = dataclasses.field(default=(), repr=False)

    @property
    def key(self) -> str:
        return endpoint_key(self.scope, self.method, self.path)

    @property
    def normalized_path(self) -> str:
        return normalize_path(self.path)

    @property
    def is_write(self) -> bool:
        return self.method in ("POST", "PUT", "PATCH", "DELETE")

    @property
    def path_param_names(self) -> tuple[str, ...]:
        """Placeholder names in declaration order, e.g. ``("nePk", "id")``."""
        return tuple(re.findall(r"\{([^}]+)\}", self.path))

    def parameters(self, location: str | None = None) -> list[JsonDict]:
        """Merged path-level + operation-level parameters, optionally by ``in``."""
        merged: list[JsonDict] = [*self.path_parameters]
        own = self.operation.get("parameters")
        if isinstance(own, list):
            merged.extend(p for p in own if isinstance(p, dict))
        if location is None:
            return merged
        return [p for p in merged if p.get("in") == location]

    def request_schema(self, spec: JsonDict | None = None) -> Any:
        """Resolved request-body schema, or ``None`` when the spec is silent.

        Covers both the OpenAPI 3 ``requestBody`` and the Swagger-2 ``in: body``
        parameter, because the vendored baselines mix the two conventions.
        """
        spec = load_spec(self.scope) if spec is None else spec
        body = self.operation.get("requestBody")
        if isinstance(body, dict):
            for media in (body.get("content") or {}).values():
                if isinstance(media, dict) and media.get("schema"):
                    return resolve_schema(spec, media["schema"])
        for param in self.parameters("body"):
            if param.get("schema"):
                return resolve_schema(spec, param["schema"])
        return None

    def response_schema(self, spec: JsonDict | None = None, status: str = "200") -> Any:
        """Resolved success-response schema, or ``None`` when untyped."""
        spec = load_spec(self.scope) if spec is None else spec
        responses = self.operation.get("responses")
        if not isinstance(responses, dict):
            return None
        response = responses.get(status) or responses.get("default")
        if not isinstance(response, dict):
            return None
        for media in (response.get("content") or {}).values():
            if isinstance(media, dict) and media.get("schema"):
                return resolve_schema(spec, media["schema"])
        if response.get("schema"):  # Swagger 2
            return resolve_schema(spec, response["schema"])
        return None


def iter_endpoints(scope: Scope | None = None) -> Iterator[Endpoint]:
    """Every operation in the baselines, ordered by scope, path, then method."""
    for one in SCOPES if scope is None else (scope,):
        spec = load_spec(one)
        paths = spec.get("paths")
        if not isinstance(paths, dict):
            continue
        for path in sorted(paths):
            item = paths[path]
            if not isinstance(item, dict):
                continue
            shared = item.get("parameters")
            shared_tuple = (
                tuple(p for p in shared if isinstance(p, dict)) if isinstance(shared, list) else ()
            )
            for method in HTTP_METHODS:
                operation = item.get(method)
                if not isinstance(operation, dict):
                    continue
                tags = operation.get("tags")
                yield Endpoint(
                    scope=one,
                    method=method.upper(),
                    path=path,
                    operation_id=str(operation.get("operationId", "")),
                    summary=str(operation.get("summary", "")),
                    description=str(operation.get("description", "")),
                    tags=tuple(str(t) for t in tags) if isinstance(tags, list) else (),
                    deprecated=bool(operation.get("deprecated", False)),
                    operation=operation,
                    path_parameters=shared_tuple,
                )


@functools.lru_cache(maxsize=1)
def endpoint_index() -> dict[str, Endpoint]:
    """``endpoint_key()`` -> :class:`Endpoint` for the whole universe.

    Two spec paths that differ only in parameter *name* collapse to one key;
    the first in sort order wins, which is stable across runs.
    """
    index: dict[str, Endpoint] = {}
    for endpoint in iter_endpoints():
        index.setdefault(endpoint.key, endpoint)
    return index


def find_endpoint(scope: str, method: str, path: str) -> Endpoint | None:
    """Look one operation up by its normalized identity."""
    return endpoint_index().get(endpoint_key(scope, method, path))


# ---------------------------------------------------------------------------
# Postman-derived payload examples (issue #51)


@functools.lru_cache(maxsize=1)
def payload_examples() -> dict[str, JsonDict]:
    """Distilled request/response examples keyed by :func:`endpoint_key`.

    Produced by ``tools/postman_sync.py`` from the vendor's published Postman
    collections; absent by default. Values are *shape* examples — the vendor
    fills scalars with ``"string"`` / ``0`` placeholders — so they document
    field names and nesting, never real values.
    """
    directory = specs_dir()
    if directory is None:
        return {}
    matches = sorted(directory.glob(PAYLOAD_EXAMPLES_GLOB))
    if not matches:
        return {}
    with matches[-1].open(encoding="utf-8") as handle:
        loaded = json.load(handle)
    entries = loaded.get("endpoints") if isinstance(loaded, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {str(k): v for k, v in entries.items() if isinstance(v, dict)}


def payload_example(scope: str, method: str, path: str) -> JsonDict | None:
    """Example payloads for one endpoint, or ``None`` when none was captured."""
    return payload_examples().get(endpoint_key(scope, method, path))


def clear_caches() -> None:
    """Drop every cached parse — for tests that point ``$ECSDWAN_SPECS_DIR`` elsewhere."""
    load_spec.cache_clear()
    endpoint_index.cache_clear()
    payload_examples.cache_clear()
