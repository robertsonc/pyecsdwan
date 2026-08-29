"""Fabric-wide drift: every curated kind, every instance, one report (epic #8).

`ec-cli diff` answers "does what I staged match the server?". It only looks at
what is staged, so a fabric with an empty candidate is reported as having no
changes — which is true, and useless as a fabric report. This widens the same
question to every instance the registry can enumerate, and the widening is the
point: the interesting rows are the ones `diff` never had a reason to mention.

**Nothing is collapsed into "clean".** Epic #8's definition of done asks for
exactly this, and it is the whole design:

* an instance with no staged intent is ``undeclared`` — *not* in sync. Nobody
  has said what it should be, so nothing can be compared, and reporting that as
  "no drift" would let an unmanaged fabric look like a managed one;
* an instance whose live state could not be read is ``unreadable``, and its
  absence from the drift count is stated rather than implied;
* a sub-curated kind is ``unsupported``: ``normalize()`` raises ``NotCurated``,
  so there is no canonical form to compare and no amount of fetching helps.

That distinction drives the exit code too. **Incompleteness outranks drift**:
a run that could not read part of the fabric exits ``partial`` even when it
also found drift, because "no drift" from a report that skipped half the
appliances is a claim it has not earned. Both are non-zero, so a CI job fails
either way; the code says which problem to look at first.

Where "desired" comes from is deliberately one line
(:func:`_desired_for`). Today it is the candidate store. When epic #8's
declarative apply lands, a directory of YAML becomes another source and the
enumeration, the status taxonomy and the exit codes below do not change.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Sequence
from typing import Any

import structlog

from pyecsdwan.candidate import CandidateStore
from pyecsdwan.contract import Ctx, NotCurated, Ref, Resource, Tier
from pyecsdwan.jobs import UNSAVED_FIELD
from pyecsdwan.registry import Registry
from pyecsdwan.reports.fanout import DEFAULT_CONCURRENCY, fan_out

log = structlog.get_logger(__name__)


class Status(str, enum.Enum):
    """What this report found for one instance. Five outcomes, not two."""

    #: Staged intent differs from live state.
    DRIFT = "drift"
    #: Staged intent matches live state.
    IN_SYNC = "in-sync"
    #: Nothing staged. The instance exists and was read; no one has said what
    #: it should be. Never counted as in-sync.
    UNDECLARED = "undeclared"
    #: Live state could not be read — unreachable appliance, refused call,
    #: unexpected shape. The reason is carried on the row.
    UNREADABLE = "unreadable"
    #: Tier-1 stub: ``normalize()`` raises, so there is no canonical form.
    UNSUPPORTED = "unsupported"


#: Statuses that mean "this row contributes no information about drift". Kept
#: as a set rather than spelled out at each use, because the exit code, the
#: renderer and the completeness note must agree on it — three copies of this
#: judgement would eventually disagree.
INCONCLUSIVE = frozenset({Status.UNREADABLE, Status.UNSUPPORTED})

#: Exit codes, from the grammar's outcome table (`specs/001-cli-command-
#: taxonomy/grammar.md`). ``ok`` also covers an all-``undeclared`` fabric: you
#: cannot fail a CI check for not having declared anything yet.
EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_PARTIAL = 8


@dataclasses.dataclass(frozen=True)
class Row:
    """One instance's finding."""

    #: User-facing noun, never the registry key (Principle IV / #77).
    noun: str
    kind: str
    name: str
    #: Empty for orchestrator-scope instances.
    appliance: str
    status: Status
    #: Number of differing paths, for DRIFT. Zero otherwise.
    entries: int = 0
    #: Why, for the states that need one.
    detail: str = ""

    @property
    def target(self) -> str:
        return f"{self.appliance}:{self.name}" if self.appliance else self.name

    def as_json(self) -> dict[str, Any]:
        return {
            "noun": self.noun,
            "kind": self.kind,
            "name": self.name,
            "appliance": self.appliance,
            "status": self.status.value,
            "entries": self.entries,
            "detail": self.detail,
        }


@dataclasses.dataclass(frozen=True)
class Report:
    rows: tuple[Row, ...] = ()
    #: Things that shaped the report without being a row of it — an inventory
    #: that would not enumerate, appliances with unsaved changes.
    notes: tuple[str, ...] = ()

    def of(self, status: Status) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if r.status is status)

    @property
    def drifted(self) -> tuple[Row, ...]:
        return self.of(Status.DRIFT)

    @property
    def inconclusive(self) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if r.status in INCONCLUSIVE)

    @property
    def complete(self) -> bool:
        """Every instance was actually compared. A "no drift" answer is only
        worth anything when this is true, which is why the exit code checks it
        before it checks for drift."""
        return not self.inconclusive

    @property
    def counts(self) -> dict[str, int]:
        return {s.value: len(self.of(s)) for s in Status}

    @property
    def exit_code(self) -> int:
        if not self.complete:
            return EXIT_PARTIAL
        return EXIT_DRIFT if self.drifted else EXIT_OK

    def as_json(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "exit_code": self.exit_code,
            "counts": self.counts,
            "notes": list(self.notes),
            "rows": [r.as_json() for r in self.rows],
        }


# -- enumeration -------------------------------------------------------------


def _enumerable(registry: Registry) -> list[str]:
    """Kinds this report can say anything about, in stable order.

    Sub-curated kinds are listed too — as ``unsupported`` rows, not omissions.
    A kind silently missing from a fabric-wide report reads as "there is
    nothing of that kind here", which is a different and wrong claim.
    """
    return sorted(registry.kinds())


def _instances(ctx: Ctx, registry: Registry, kind: str) -> tuple[list[Ref], str]:
    """Every instance of one kind, or the reason none could be listed."""
    resource = registry.get(kind)
    try:
        return list(resource.list_refs(ctx)), ""
    except Exception as exc:  # noqa: BLE001 - one kind must not take the report down
        reason = f"{type(exc).__name__}: {exc}"
        log.debug("drift_list_refs_failed", kind=kind, error=reason)
        return [], reason


def _desired_for(candidate: CandidateStore, ref: Ref) -> Any:
    """The staged intent for one ref, or a sentinel meaning "nothing staged".

    The single seam between this report and where intent comes from. A
    desired-state directory (epic #8) plugs in here without touching anything
    else in this module.
    """
    return candidate.items.get(ref.key())


# -- one row -----------------------------------------------------------------


def _row(
    ctx: Ctx,
    registry: Registry,
    candidate: CandidateStore,
    ref: Ref,
) -> Row:
    resource: Resource = registry.get(ref.kind)
    noun = registry.cli_name(ref.kind)
    appliance = ref.appliance or ""

    if resource.tier < Tier.CURATED:
        return Row(
            noun=noun, kind=ref.kind, name=ref.name, appliance=appliance,
            status=Status.UNSUPPORTED,
            detail=f"tier-{int(resource.tier)}: normalize() raises NotCurated",
        )

    try:
        current = resource.normalize(resource.fetch(ctx, ref))
    except NotCurated as exc:
        # Belt and braces: a curated resource whose normalize() still raises is
        # unsupported for this report's purposes, whatever its declared tier.
        return Row(
            noun=noun, kind=ref.kind, name=ref.name, appliance=appliance,
            status=Status.UNSUPPORTED, detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - a dead appliance is a row, not a crash
        return Row(
            noun=noun, kind=ref.kind, name=ref.name, appliance=appliance,
            status=Status.UNREADABLE, detail=f"{type(exc).__name__}: {exc}",
        )

    item = _desired_for(candidate, ref)
    if item is None:
        return Row(
            noun=noun, kind=ref.kind, name=ref.name, appliance=appliance,
            status=Status.UNDECLARED,
            detail="nothing staged for this instance",
        )

    try:
        desired_input = candidate.desired_for(item, current)
        desired = (
            None
            if desired_input is None
            else resource.canonicalize_desired(ctx, ref, desired_input)
        )
        diff = resource.diff(ref, current, desired)
    except Exception as exc:  # noqa: BLE001 - same reasoning as the fetch above
        return Row(
            noun=noun, kind=ref.kind, name=ref.name, appliance=appliance,
            status=Status.UNREADABLE, detail=f"{type(exc).__name__}: {exc}",
        )

    if diff.empty:
        return Row(
            noun=noun, kind=ref.kind, name=ref.name, appliance=appliance,
            status=Status.IN_SYNC,
        )
    return Row(
        noun=noun, kind=ref.kind, name=ref.name, appliance=appliance,
        status=Status.DRIFT, entries=len(diff.entries),
        detail=", ".join(sorted({".".join(str(p) for p in e.path) for e in diff.entries})[:4]),
    )


# -- the report --------------------------------------------------------------


def _unsaved_note(ctx: Ctx) -> str:
    """Appliances holding running-config changes that were never saved.

    A *note*, not a row, and deliberately outside the exit code: "differs from
    what was declared" and "differs from what is on flash" are two different
    axes, and folding the second into the first is the collapsing this whole
    module exists to avoid. It is still worth saying — a reboot discards it.
    """
    try:
        raw = ctx.client.get("/appliance")
    except Exception as exc:  # noqa: BLE001 - a note is never worth the report
        log.debug("drift_unsaved_probe_failed", error=str(exc))
        return ""
    if not isinstance(raw, list):
        return ""
    unsaved = sorted(
        str(a.get("hostName") or a.get("nePk") or a.get("id") or "?")
        for a in raw
        if isinstance(a, dict) and a.get(UNSAVED_FIELD) is True
    )
    if not unsaved:
        return ""
    return (
        f"{len(unsaved)} appliance(s) hold unsaved running-config changes "
        f"({', '.join(unsaved)}) — a reboot discards them. Not counted as "
        f"drift: that is a different axis from declared intent"
    )


def collect(
    ctx: Ctx,
    registry: Registry,
    candidate: CandidateStore,
    *,
    kinds: Sequence[str] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
) -> Report:
    """Compare every enumerable instance against staged intent."""
    wanted = list(kinds) if kinds is not None else _enumerable(registry)
    refs: list[Ref] = []
    notes: list[str] = []

    for kind in wanted:
        found, reason = _instances(ctx, registry, kind)
        if reason:
            notes.append(f"{registry.cli_name(kind)}: instances could not be listed ({reason})")
            continue
        refs.extend(found)

    outcomes = fan_out(
        refs,
        lambda ref: _row(ctx, registry, candidate, ref),
        concurrency=concurrency,
        timeout=timeout,
    )
    rows: list[Row] = []
    for outcome in outcomes:
        if outcome.value is not None:
            rows.append(outcome.value)
            continue
        # `_row` catches its own failures, so reaching here means the fan-out
        # itself gave up — the overall deadline. Still a row: a deadline that
        # made instances vanish would report a smaller, cleaner fabric.
        ref = outcome.item
        rows.append(
            Row(
                noun=registry.cli_name(ref.kind), kind=ref.kind, name=ref.name,
                appliance=ref.appliance or "", status=Status.UNREADABLE,
                detail=outcome.error or "fan-out deadline elapsed",
            )
        )

    unsaved = _unsaved_note(ctx)
    if unsaved:
        notes.append(unsaved)
    rows.sort(key=lambda r: (r.noun, r.appliance, r.name))
    return Report(rows=tuple(rows), notes=tuple(notes))
