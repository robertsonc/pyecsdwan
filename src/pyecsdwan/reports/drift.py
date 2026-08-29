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

Where "desired" comes from is one narrow interface,
:class:`pyecsdwan.candidate.IntentSource`.
Two things implement it: the candidate store (what the operator typed since
the last commit) and :class:`pyecsdwan.desired.Declared` (a directory of YAML
in git, which is what a CI drift check actually wants). Both materialize
desired state through the *same*
:func:`~pyecsdwan.candidate.materialize_desired`, so this report can never say
something ``commit`` would not do — and the enumeration, the status taxonomy
and the exit codes below are the same for either.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Sequence
from typing import Any

import structlog

from pyecsdwan.candidate import IntentSource
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
class Gap:
    """Something this run did not compare, at a coarser grain than one row.

    A :class:`Row` says "I looked at this instance and here is what I found".
    A gap says "I never got to look", and the two need different shapes
    because a gap has no instance to name: when ``list_refs`` fails, the whole
    point is that we do not know which instances exist.

    Gaps were originally free-text notes, which is how #102's bug happened:
    ``exit_code`` reads rows, notes were prose, and an entire unreadable kind
    exited 0 while the module's own docstring promised the opposite.
    """

    #: ``kind`` — a kind whose instances could not be listed.
    #: ``declared`` — intent naming an instance the enumeration never produced,
    #: so nothing compared it.
    scope: str
    #: CLI noun for ``kind`` gaps, ref key for ``declared`` ones.
    name: str
    reason: str

    def as_json(self) -> dict[str, str]:
        return {"scope": self.scope, "name": self.name, "reason": self.reason}


@dataclasses.dataclass(frozen=True)
class Report:
    rows: tuple[Row, ...] = ()
    #: Things that shaped the report without being a row of it — today only
    #: appliances holding unsaved changes. Prose, and deliberately outside the
    #: exit code; anything that makes the run *incomplete* is a :class:`Gap`.
    notes: tuple[str, ...] = ()
    #: What this run could not compare. Part of completeness, so part of the
    #: exit code.
    gaps: tuple[Gap, ...] = ()

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
        before it checks for drift.

        Gaps count, and that they did not is #102: a kind whose ``list_refs``
        raised produced a note and no rows, so ``inconclusive`` was empty, the
        report called itself complete, and a CI gate went green over a kind
        nobody could read.
        """
        return not self.inconclusive and not self.gaps

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
            "gaps": [g.as_json() for g in self.gaps],
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


def _undeclarable(
    intent: IntentSource, refs: Sequence[Ref], wanted: Sequence[str]
) -> list[Gap]:
    """Declared instances the live enumeration never produced.

    This report compares live instances against intent, so its universe is
    whatever ``list_refs`` returns. Intent naming something outside that
    universe was simply dropped — and #102 is what that costs: declare a new
    instance in a desired-state directory, run the CI drift check, and it
    reports clean, because the thing you declared does not exist yet and so
    was never enumerated to be compared. ``apply --from`` plans it as a change.
    Drift said clean; apply would write. Same intent, two answers.

    Reported as gaps rather than as rows, deliberately. A row would have to
    claim a *status*, and whether a declared-but-absent instance is an "add",
    a drift, or an error is the declarative-semantics question #101 is open to
    settle — it depends on whether files are authoritative or additive, which
    nobody has ratified. What needs no ratification is that the run is not
    complete: something was declared and nothing compared it, so this must not
    exit 0. Fail closed now, classify once the contract exists.
    """
    covered = {ref.key() for ref in refs}
    scope = set(wanted)
    out: list[Gap] = []
    for item in intent.ordered_items():
        if item.ref_key in covered:
            continue
        try:
            ref = Ref.from_key(item.ref_key)
        except ValueError:
            # A key this module cannot parse is still intent it did not
            # compare, so it is still a gap — just an unnamed one.
            out.append(Gap(scope="declared", name=item.ref_key, reason="unparseable ref key"))
            continue
        if ref.kind not in scope:
            # Filtered out by --kind. Not a gap: the operator narrowed the
            # question, and answering a narrower question completely is not
            # incompleteness.
            continue
        out.append(
            Gap(
                scope="declared",
                name=item.ref_key,
                reason=(
                    "declared, but no such instance was enumerated on the fabric, "
                    "so nothing compared it"
                ),
            )
        )
    return out


# -- one row -----------------------------------------------------------------


def _row(
    ctx: Ctx,
    registry: Registry,
    intent: IntentSource,
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

    item = intent.item_for(ref)
    if item is None:
        return Row(
            noun=noun, kind=ref.kind, name=ref.name, appliance=appliance,
            status=Status.UNDECLARED,
            detail="nothing staged for this instance",
        )

    try:
        desired_input = intent.desired_for(item, current)
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
    intent: IntentSource,
    *,
    kinds: Sequence[str] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
) -> Report:
    """Compare every enumerable instance against declared intent.

    ``intent`` is the candidate store or a desired-state directory
    (:class:`pyecsdwan.desired.Declared`) — anything implementing
    :class:`~pyecsdwan.candidate.IntentSource`. The report is identical either
    way; only where "should be" comes from differs.
    """
    wanted = list(kinds) if kinds is not None else _enumerable(registry)
    refs: list[Ref] = []
    notes: list[str] = []
    gaps: list[Gap] = []

    for kind in wanted:
        found, reason = _instances(ctx, registry, kind)
        if reason:
            # A gap, not a note: this run cannot say anything about the kind,
            # and a report that exits 0 having skipped one is worse than no
            # report (#102).
            gaps.append(
                Gap(
                    scope="kind",
                    name=registry.cli_name(kind),
                    reason=f"instances could not be listed ({reason})",
                )
            )
            continue
        refs.extend(found)

    gaps.extend(_undeclarable(intent, refs, wanted))

    outcomes = fan_out(
        refs,
        lambda ref: _row(ctx, registry, intent, ref),
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
    return Report(rows=tuple(rows), notes=tuple(notes), gaps=tuple(sorted(
        gaps, key=lambda g: (g.scope, g.name)
    )))
