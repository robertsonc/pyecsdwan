"""What has actually been *observed*, per resource, at which versions (#66).

`Tier` says how carefully a resource was **written**. This says what anyone has
**seen it do**. They are independent, and conflating them is the failure this
module exists to end: the roadmap used one word — "shipped" — for a resource
implemented against a spec, a resource green against the bundled mock, and a
resource whose writes had actually been run on a fabric and rolled back. Those
are not the same claim, and an operator deciding whether to point this tool at
production is entitled to know which one they are being offered.

The ladder is deliberately *evidence*-shaped rather than *effort*-shaped. There
is no rung for "reviewed carefully" or "the author is confident", because
neither is observable by the person who has to trust it. Every rung above
:attr:`Evidence.MOCK_VERIFIED` names a thing someone did against real gear and
wrote down, with the versions it was done against.

**Why versions are mandatory above the mock.** An observation without a version
is not evidence about anything you can act on: "it worked" on an unrecorded
Orchestrator tells the next operator nothing about theirs. This repository has
a live-read history — Phase-2/3 plugins carry payload shapes captured against a
real lab Orchestrator, and six template-section names were confirmed against
a real Default Template Group (``docs/sitrep/2026-08-26-fanout.md``) — and
**none of it promotes a single resource**, because no one recorded the version.
That is not a gap in this implementation. It is the finding, and it is why
:func:`Record.validate` refuses a live claim without the versions attached.

**Mock evidence can never reach the top.** ``production-supported`` requires
behaviors — reboot persistence, a template-owned refusal, an injected job
failure, a permission-denied path — that the bundled mock cannot witness on
anyone's behalf, and the level itself requires versions the mock does not have.
The ceiling is structural, checked at load, not a policy note someone has to
remember.

The ledger ships inside the package (``_evidence/ledger.json``) so
``show coverage`` answers this question offline, from an installed wheel, with
no Orchestrator and no credentials — the same reason the OpenAPI baselines
moved into the package in #65.
"""

from __future__ import annotations

import dataclasses
import enum
import functools
import json
import os
from pathlib import Path
from typing import Any

#: Environment override for the ledger directory, mirroring ``ECSDWAN_SPECS_DIR``.
ENV_EVIDENCE_DIR = "ECSDWAN_EVIDENCE_DIR"
LEDGER_FILENAME = "ledger.json"


class Evidence(enum.IntEnum):
    """Resource maturity by observation, as #66 names the levels.

    Ordered, and compared as an ordinal: ``record.level >= Evidence.LIVE_READ_VERIFIED``
    is the question every consumer actually asks.
    """

    #: Code exists and type-checks. Nothing has been run against it.
    IMPLEMENTED = 1
    #: Green against the bundled mock Orchestrator. This is where a resource
    #: lands by passing `make check`, and it is the ceiling of what any amount
    #: of work in this repository can establish on its own.
    MOCK_VERIFIED = 2
    #: ``fetch()`` + ``normalize()`` run against a real fabric, at a recorded
    #: version. The first rung that needs gear.
    LIVE_READ_VERIFIED = 3
    #: A no-op round trip on real gear: read current state, commit it back
    #: unchanged, confirm the re-plan diffs empty. Proves the write path is
    #: shaped right without changing anything.
    LIVE_NO_OP_WRITE_VERIFIED = 4
    #: A real change, verified after apply, then rolled back, with the change
    #: persisted through save-changes. The first level at which "this resource
    #: can safely write" is a claim anyone has grounds for.
    LIVE_CHANGE_AND_ROLLBACK_VERIFIED = 5
    #: Everything above, plus the failure paths: persistence across a reboot,
    #: a template-owned refusal, an injected job failure, and a
    #: permission-denied path. Behaving correctly when things go right is the
    #: easy half.
    PRODUCTION_SUPPORTED = 6

    @property
    def label(self) -> str:
        return self.name.lower().replace("_", "-")

    @classmethod
    def from_label(cls, label: str) -> Evidence:
        for level in cls:
            if level.label == label:
                return level
        known = ", ".join(level.label for level in cls)
        raise ValueError(f"unknown evidence level {label!r}; expected one of: {known}")


#: The first level that cannot be reached without a real fabric.
LIVE_FLOOR = Evidence.LIVE_READ_VERIFIED
#: The level at or above which a *write* path may be described as supported.
#: Below this, "shipped" means the code exists, not that writing works.
WRITE_SUPPORTED_FLOOR = Evidence.LIVE_CHANGE_AND_ROLLBACK_VERIFIED

# -- the live test protocol, as checkable behavior names ----------------------
#
# #66 names eight steps. They are constants rather than free text so a ledger
# entry cannot claim "tested" in prose that no one can check, and so
# `docs/live-validation.md` and this module cannot drift: the doc's section
# anchors are these strings.

LIVE_READ = "live-read"
NO_OP_ROUND_TRIP = "no-op-round-trip"
REAL_CHANGE = "real-change"
POST_APPLY_VERIFICATION = "post-apply-verification"
ROLLBACK = "rollback"
SAVE_PERSISTENCE = "save-persistence"
REBOOT_PERSISTENCE = "reboot-persistence"
TEMPLATE_OWNED_REFUSAL = "template-owned-refusal"
INJECTED_JOB_FAILURE = "injected-job-failure"
PERMISSION_DENIED = "permission-denied"

BEHAVIORS: tuple[str, ...] = (
    LIVE_READ,
    NO_OP_ROUND_TRIP,
    REAL_CHANGE,
    POST_APPLY_VERIFICATION,
    ROLLBACK,
    SAVE_PERSISTENCE,
    REBOOT_PERSISTENCE,
    TEMPLATE_OWNED_REFUSAL,
    INJECTED_JOB_FAILURE,
    PERMISSION_DENIED,
)

#: What each rung adds. Cumulative — see :func:`required_behaviors`.
_ADDS: dict[Evidence, tuple[str, ...]] = {
    Evidence.IMPLEMENTED: (),
    Evidence.MOCK_VERIFIED: (),
    Evidence.LIVE_READ_VERIFIED: (LIVE_READ,),
    Evidence.LIVE_NO_OP_WRITE_VERIFIED: (NO_OP_ROUND_TRIP,),
    Evidence.LIVE_CHANGE_AND_ROLLBACK_VERIFIED: (
        REAL_CHANGE,
        POST_APPLY_VERIFICATION,
        ROLLBACK,
        SAVE_PERSISTENCE,
    ),
    Evidence.PRODUCTION_SUPPORTED: (
        REBOOT_PERSISTENCE,
        TEMPLATE_OWNED_REFUSAL,
        INJECTED_JOB_FAILURE,
        PERMISSION_DENIED,
    ),
}


def required_behaviors(level: Evidence) -> tuple[str, ...]:
    """Every behavior a record at ``level`` must have witnessed.

    Cumulative, so a claim at level 5 carries level 3's and 4's obligations
    too — a rollback nobody watched persist is not a rollback anyone can rely
    on.
    """
    out: list[str] = []
    for rung in Evidence:
        if rung <= level:
            out.extend(_ADDS[rung])
    return tuple(out)


class LedgerError(Exception):
    """A ledger entry claims more than it carries evidence for."""


@dataclasses.dataclass(frozen=True)
class Record:
    """One resource's observed maturity, and what backs it."""

    kind: str
    level: Evidence
    #: Orchestrator version the observation was made against, e.g. "9.4.2".
    #: Required at :data:`LIVE_FLOOR` and above.
    orchestrator: str = ""
    #: ECOS version, same rule. "n/a" is acceptable for an orchestrator-scope
    #: resource that never reaches an appliance — but it must be written down.
    ecos: str = ""
    #: "api-key" (sessionless) or "session". Which one was exercised matters:
    #: they are different code paths in `client.py`, and cloud and on-prem
    #: Orchestrators do not agree about which they accept.
    auth_mode: str = ""
    #: ISO date of the observation.
    observed: str = ""
    #: Which of :data:`BEHAVIORS` were witnessed.
    behaviors: tuple[str, ...] = ()
    #: Where the observation is written down, so it can be re-read rather than
    #: taken on trust.
    source: str = ""
    notes: str = ""

    def validate(self) -> None:
        """Raise :class:`LedgerError` if the claim outruns its evidence.

        Called at load, so an over-claiming ledger fails the moment anything
        reads it — including `show coverage` on an operator's machine. A gate
        that only runs in CI would let a locally edited ledger lie.
        """
        for behavior in self.behaviors:
            if behavior not in BEHAVIORS:
                raise LedgerError(
                    f"{self.kind}: unknown behavior {behavior!r}; "
                    f"expected one of: {', '.join(BEHAVIORS)}"
                )
        missing = [b for b in required_behaviors(self.level) if b not in self.behaviors]
        if missing:
            raise LedgerError(
                f"{self.kind}: claims {self.level.label} but has not witnessed "
                f"{', '.join(missing)}"
            )
        if self.level < LIVE_FLOOR:
            return
        blank = [
            name
            for name in ("orchestrator", "ecos", "auth_mode", "observed", "source")
            if not getattr(self, name)
        ]
        if blank:
            raise LedgerError(
                f"{self.kind}: claims {self.level.label}, which is a claim about real "
                f"gear, but records no {', '.join(blank)}. An observation without a "
                f"version tells the next operator nothing about their fabric."
            )

    def as_json(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["level"] = self.level.label
        payload["behaviors"] = list(self.behaviors)
        return payload

    @staticmethod
    def from_json(payload: dict[str, Any]) -> Record:
        return Record(
            kind=str(payload["kind"]),
            level=Evidence.from_label(str(payload["level"])),
            orchestrator=str(payload.get("orchestrator", "")),
            ecos=str(payload.get("ecos", "")),
            auth_mode=str(payload.get("auth_mode", "")),
            observed=str(payload.get("observed", "")),
            behaviors=tuple(str(b) for b in payload.get("behaviors", ())),
            source=str(payload.get("source", "")),
            notes=str(payload.get("notes", "")),
        )


@dataclasses.dataclass(frozen=True)
class SupportMatrix:
    """Which fabrics anything has been verified against.

    Empty lists are the honest answer today and are rendered as such. An empty
    matrix is *not* "every version works"; :func:`version_warning` says so.
    """

    orchestrator: tuple[str, ...] = ()
    ecos: tuple[str, ...] = ()
    auth_modes: tuple[str, ...] = ()
    #: The OpenAPI baseline the code was written against — a different and
    #: weaker claim than a verified version, kept separate for that reason.
    spec_baseline: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "orchestrator": list(self.orchestrator),
            "ecos": list(self.ecos),
            "auth_modes": list(self.auth_modes),
            "spec_baseline": self.spec_baseline,
        }


@dataclasses.dataclass(frozen=True)
class Ledger:
    """The whole evidence file: per-kind records plus the support matrix."""

    records: dict[str, Record] = dataclasses.field(default_factory=dict)
    support: SupportMatrix = dataclasses.field(default_factory=SupportMatrix)
    note: str = ""
    #: False when no ledger file could be found. Distinguished from "the
    #: ledger says nothing has been verified", which is a real answer.
    available: bool = False

    def get(self, kind: str) -> Record | None:
        return self.records.get(kind)

    def level(self, kind: str) -> Evidence | None:
        record = self.records.get(kind)
        return record.level if record else None


def evidence_dir() -> Path:
    """Where the ledger lives: ``$ECSDWAN_EVIDENCE_DIR`` -> ``<package>/_evidence``."""
    override = os.environ.get(ENV_EVIDENCE_DIR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "_evidence"


@functools.lru_cache(maxsize=1)
def ledger() -> Ledger:
    """Parse and cache the evidence ledger.

    A missing file yields an empty ledger with ``available=False`` rather than
    raising — the same degradation as a wheel with no vendored specs, and for
    the same reason: a read-only report should not be the thing that crashes.
    Callers render "unavailable", never blanks that read as "no evidence".

    A *malformed or over-claiming* file is different and does raise: silently
    ignoring a ledger that claims production support without versions would be
    the exact failure this module exists to prevent.
    """
    path = evidence_dir() / LEDGER_FILENAME
    if not path.is_file():
        return Ledger()
    raw = json.loads(path.read_text(encoding="utf-8"))
    records: dict[str, Record] = {}
    for entry in raw.get("records", []):
        record = Record.from_json(entry)
        record.validate()
        if record.kind in records:
            raise LedgerError(f"{record.kind}: listed twice in the ledger")
        records[record.kind] = record
    support = raw.get("support", {})
    return Ledger(
        records=records,
        support=SupportMatrix(
            orchestrator=tuple(str(v) for v in support.get("orchestrator", ())),
            ecos=tuple(str(v) for v in support.get("ecos", ())),
            auth_modes=tuple(str(v) for v in support.get("auth_modes", ())),
            spec_baseline=str(support.get("spec_baseline", "")),
        ),
        note=str(raw.get("note", "")),
        available=True,
    )


def clear_cache() -> None:
    """Drop the cached parse — for tests that point ``$ECSDWAN_EVIDENCE_DIR`` elsewhere."""
    ledger.cache_clear()


def version_warning(orchestrator: str, ecos: str = "") -> str:
    """Warn when a connected fabric is outside the verified matrix (#66).

    Returns "" when the versions are verified, else a sentence naming what is
    unverified. **An empty support matrix warns about everything**, which is
    correct and is the state today: nothing has been verified against a
    recorded version, so every fabric is outside the matrix. A matrix that
    stayed silent while empty would be indistinguishable from one that had
    verified the world.
    """
    matrix = ledger().support
    unverified: list[str] = []
    if orchestrator and orchestrator not in matrix.orchestrator:
        unverified.append(f"Orchestrator {orchestrator}")
    if ecos and ecos not in matrix.ecos:
        unverified.append(f"ECOS {ecos}")
    if not unverified:
        return ""
    if not matrix.orchestrator and not matrix.ecos:
        return (
            f"{' and '.join(unverified)}: no version has been recorded as verified for "
            f"this tool, so every fabric is outside the support matrix. See "
            f"`show coverage --evidence`."
        )
    return (
        f"{' and '.join(unverified)} is outside the verified support matrix "
        f"(verified: {', '.join(matrix.orchestrator + matrix.ecos) or 'none'}). "
        f"See `show coverage --evidence`."
    )
