"""Plugin registry: kind string -> Resource implementation, plus the
dependency-ordered apply sequence for a changeset and the promotion checklist
(issue #29) that decides whether a kind has earned its tier.
"""

from __future__ import annotations

import dataclasses
import enum
from graphlib import CycleError, TopologicalSorter
from typing import Any

from pyecsdwan.contract import (
    CanonicalState,
    Ctx,
    NotCurated,
    RawState,
    Ref,
    Resource,
    Tier,
)


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


# -- promotion checklist (issue #29) ------------------------------------------
#
# ``docs/plugin-promotion.md`` used to be advisory prose: a generated stub that
# never got curated would sail through review and only be caught at runtime, by
# the transaction engine refusing it a confirm window (``txn.py``, low_tier).
# The checks below make the machine-decidable boxes on that checklist
# executable, so the failure lands in ``make check`` instead of on a fabric.
#
# They are deliberately *properties*, not bookkeeping: nothing here asserts
# "a test named X exists" (a rename would silently disable that), it asserts
# the behavior the checklist demands, given a sample raw state.


class CheckStatus(str, enum.Enum):
    OK = "pass"
    FAIL = "fail"
    #: Human judgment; no machine can decide it. Never a failure, always
    #: reported, so the checklist cannot quietly shrink to what is automatable.
    MANUAL = "manual"


@dataclasses.dataclass(frozen=True)
class Check:
    """One checklist box, evaluated."""

    name: str
    status: CheckStatus
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status is CheckStatus.FAIL


#: Checklist boxes that stay human judgment after this issue. Reported by
#: ``ec-cli plugin promote`` so a reviewer sees what the machine did *not*
#: decide; keep in step with ``docs/plugin-promotion.md``.
MANUAL_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "reversibility-class",
        "REVERSIBLE only when snapshot/restore is exact; COMPENSABLE when only "
        "a compensator exists; IRREVERSIBLE when neither.",
    ),
    (
        "async-jobs",
        "Every action key polled via jobs.wait_for_action; apply() returns only "
        "after a terminal state; TIMEOUT counts as failure.",
    ),
    (
        "appliance-scope-ownership",
        "managed_by() implemented and proxy writes persisted with one batched "
        "ctx.save_changes([...]) per operation.",
    ),
    (
        "dependencies",
        "`dependencies` declares the ordering this kind needs within a changeset.",
    ),
    (
        "spec-divergence",
        "Docstring notes any spec-vs-live divergence; unknown fields pass through.",
    ),
)


def manual_checks() -> list[Check]:
    """The checklist boxes a machine cannot decide, as :class:`Check` records."""
    return [Check(name, CheckStatus.MANUAL, detail) for name, detail in MANUAL_CHECKS]


def check_untransactional_normalize(resource: Resource) -> Check:
    """Tier-0/1 obligation: ``normalize()`` must refuse with :class:`NotCurated`.

    A generated stub whose ``normalize()`` quietly returns something is the
    exact failure this gate exists for: it looks curated to every caller, so
    the engine's tier guard is the only thing left between it and a fabric.
    """
    name = "generated-normalize-refuses"
    if resource.tier >= Tier.CURATED:
        return Check(name, CheckStatus.OK, "tier 2: normalize() is expected to work")
    try:
        produced = resource.normalize({})
    except NotCurated:
        return Check(
            name,
            CheckStatus.OK,
            f"tier {int(resource.tier)}: normalize() raises NotCurated",
        )
    except Exception as exc:  # noqa: BLE001 - any other failure mode is a finding
        return Check(
            name,
            CheckStatus.FAIL,
            f"normalize() raised {type(exc).__name__} ({exc}); an un-curated "
            f"resource must raise NotCurated so the tier guard and the operator "
            f"see the same reason",
        )
    return Check(
        name,
        CheckStatus.FAIL,
        f"tier {int(resource.tier)} but normalize() returned {produced!r} instead "
        f"of raising NotCurated — either finish the curation and set "
        f"tier = Tier.CURATED, or restore the raise",
    )


def check_idempotent(
    resource: Resource,
    ref: Ref,
    raw: RawState,
    ctx: Ctx | None = None,
) -> list[Check]:
    """Prove the Tier-2 idempotency obligations against one sample raw state.

    Four boxes, in the order a failure is most useful to read:

    ``normalize-runs``
        A curated resource's ``normalize()`` must not raise on real state.
    ``sample-non-trivial``
        The sample must canonicalize to *something*. Without this the three
        checks below pass vacuously on ``None`` — the single largest way a
        gate like this rots.
    ``normalize-idempotent``
        ``normalize(normalize(x)) == normalize(x)``, the checklist's own words.
    ``replan-empty``
        Feeding canonical state back through ``canonicalize_desired()`` diffs
        empty: "apply desired state, re-plan, diff is empty". Needs a ``ctx``
        (name<->ID resolution); skipped without one.
    """
    checks: list[Check] = []
    try:
        canonical = resource.normalize(raw)
    except Exception as exc:  # noqa: BLE001 - the plugin under test may raise anything
        return [
            Check(
                "normalize-runs",
                CheckStatus.FAIL,
                f"{ref}: normalize() raised {type(exc).__name__}: {exc}",
            )
        ]
    checks.append(Check("normalize-runs", CheckStatus.OK, str(ref)))

    if not has_content(canonical):
        checks.append(
            Check(
                "sample-non-trivial",
                CheckStatus.FAIL,
                f"{ref}: normalize() of the sample is {canonical!r}; a canonical "
                f"state with no leaf values makes every check below vacuous — "
                f"supply a sample that actually carries configuration",
            )
        )
        return checks
    checks.append(
        Check("sample-non-trivial", CheckStatus.OK, f"{ref}: {_shape(canonical)}")
    )

    try:
        again = resource.normalize(canonical)
    except Exception as exc:  # noqa: BLE001 - see above
        checks.append(
            Check(
                "normalize-idempotent",
                CheckStatus.FAIL,
                f"{ref}: normalize() raised {type(exc).__name__} on its own "
                f"output: {exc}",
            )
        )
        return checks
    if again != canonical:
        checks.append(
            Check(
                "normalize-idempotent",
                CheckStatus.FAIL,
                f"{ref}: normalize(normalize(x)) != normalize(x); the second "
                f"pass changed the state, so every re-plan will show phantom "
                f"drift. Diff: {_first_difference(canonical, again)}",
            )
        )
        return checks
    checks.append(Check("normalize-idempotent", CheckStatus.OK, str(ref)))

    if ctx is None:
        return checks
    try:
        desired = (
            resource.canonicalize_desired(ctx, ref, canonical)
            if isinstance(canonical, dict)
            else canonical
        )
        replan = resource.diff(ref, canonical, desired)
    except Exception as exc:  # noqa: BLE001 - see above
        checks.append(
            Check(
                "replan-empty",
                CheckStatus.FAIL,
                f"{ref}: re-planning canonical state raised "
                f"{type(exc).__name__}: {exc}",
            )
        )
        return checks
    if not replan.empty:
        checks.append(
            Check(
                "replan-empty",
                CheckStatus.FAIL,
                f"{ref}: re-planning the state the server already reports "
                f"produced {len(replan)} change(s) — applying this kind twice "
                f"would write twice. First: {replan.entries[0]}",
            )
        )
        return checks
    checks.append(Check("replan-empty", CheckStatus.OK, str(ref)))
    return checks


def has_content(state: CanonicalState) -> bool:
    """True when a canonical state carries at least one leaf value.

    Truthiness is not enough: ``{"templates": {}}`` is a non-empty dict that
    says nothing, and ``normalize(normalize(x)) == normalize(x)`` holds for it
    for free. Only leaves count, so an empty-shell sample cannot be mistaken
    for proof.
    """
    if isinstance(state, dict):
        return any(has_content(v) for v in state.values())
    if isinstance(state, list):
        return any(has_content(v) for v in state)
    return state is not None


def _shape(state: object) -> str:
    """Compact description of a canonical state, for check details."""
    if isinstance(state, dict):
        return f"dict with keys {sorted(str(k) for k in state)}"
    if isinstance(state, list):
        return f"list of {len(state)}"
    return type(state).__name__


def _first_difference(left: Any, right: Any, path: str = "") -> str:
    """Locate the first divergence between two canonical states."""
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right), key=str):
            if key not in left:
                return f"{path}/{key} added by the second normalize()"
            if key not in right:
                return f"{path}/{key} dropped by the second normalize()"
            if left[key] != right[key]:
                return _first_difference(left[key], right[key], f"{path}/{key}")
        return path or "(no difference found)"
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return f"{path} length {len(left)} -> {len(right)}"
        for index, (lhs, rhs) in enumerate(zip(left, right, strict=False)):
            if lhs != rhs:
                return _first_difference(lhs, rhs, f"{path}/{index}")
        return path or "(no difference found)"
    return f"{path or '/'}: {left!r} -> {right!r}"
