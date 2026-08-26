"""Plugin registry: kind string -> Resource implementation, plus the
dependency-ordered apply sequence for a changeset."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter

from pyecsdwan.contract import Ref, Resource


class UnknownKind(KeyError):
    def __init__(self, kind: str, known: list[str]):
        self.kind = kind
        super().__init__(
            f"unknown resource kind {kind!r}; known kinds: {', '.join(sorted(known)) or '(none)'}"
        )


class Registry:
    def __init__(self) -> None:
        self._plugins: dict[str, Resource] = {}

    def register(self, resource: Resource) -> None:
        if not resource.kind:
            raise ValueError(f"{type(resource).__name__} has no kind set")
        if resource.kind in self._plugins:
            raise ValueError(f"duplicate resource kind {resource.kind!r}")
        self._plugins[resource.kind] = resource

    def get(self, kind: str) -> Resource:
        try:
            return self._plugins[kind]
        except KeyError:
            raise UnknownKind(kind, list(self._plugins)) from None

    def kinds(self) -> list[str]:
        return sorted(self._plugins)

    def __contains__(self, kind: str) -> bool:
        return kind in self._plugins

    def order_refs(self, refs: list[Ref], deletes: set[str] | None = None) -> list[Ref]:
        """Dependency-ordered apply sequence.

        Kind-level DAG from ``Resource.dependencies`` (templates before
        associations, BIOs before BIO-appliance association, ...). Within the
        creates/updates, dependencies apply first; pure deletes apply last and
        in reverse dependency order, so an association is removed before the
        object it points at.
        """
        deletes = deletes or set()
        kinds_present = {r.kind for r in refs}
        ts: TopologicalSorter[str] = TopologicalSorter()
        for kind in kinds_present:
            deps = [d for d in self.get(kind).dependencies if d in kinds_present]
            ts.add(kind, *deps)
        try:
            kind_order = list(ts.static_order())
        except CycleError as exc:
            raise ValueError(f"dependency cycle between resource kinds: {exc}") from exc

        rank = {kind: i for i, kind in enumerate(kind_order)}
        upserts = [r for r in refs if r.key() not in deletes]
        removals = [r for r in refs if r.key() in deletes]
        upserts.sort(key=lambda r: rank[r.kind])
        removals.sort(key=lambda r: rank[r.kind], reverse=True)
        return upserts + removals


#: Process-wide default registry; plugins self-register on import.
default_registry = Registry()


def register(resource: Resource) -> Resource:
    default_registry.register(resource)
    return resource
