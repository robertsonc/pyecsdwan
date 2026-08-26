"""Resource plugin contract: the single protocol every configurable object implements.

Design notes (load-bearing, read before writing a plugin):

* ``normalize()`` is where idempotency lives. It must strip server-generated IDs
  and server-injected defaults, sort lists by a stable key, and resolve
  name<->ID indirections through ``ctx.resolver`` so that user intent and
  server state meet in one canonical shape. Diffing raw server JSON against
  user intent is forbidden — it produces phantom drift.
* An empty diff means no API call is made. Running the same command twice must
  be a no-op the second time.
* ``apply()`` returns only after any Orchestrator async job (action key)
  resolves or times out. A timeout counts as failure.
* Reversibility is a *declared property of the operation class*, enforced by
  the transaction engine — IRREVERSIBLE resources refuse ``commit confirm``.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyecsdwan.client import OrchClient
    from pyecsdwan.resolver import Resolver

# Raw server JSON for one resource instance. ``None`` means "does not exist".
RawState = dict[str, Any] | list[Any] | None
# Output of normalize(): canonical, comparable state. ``None`` = absent.
CanonicalState = dict[str, Any] | list[Any] | None


class Scope(str, enum.Enum):
    ORCHESTRATOR = "orchestrator"
    APPLIANCE = "appliance"


class Reversibility(str, enum.Enum):
    #: Clean snapshot/restore. Full commit-confirm support.
    REVERSIBLE = "reversible"
    #: No restore, but a compensating action exists (create -> delete).
    COMPENSABLE = "compensable"
    #: No undo of any kind. Refuses commit-confirm, requires --force.
    IRREVERSIBLE = "irreversible"


class Tier(enum.IntEnum):
    #: Raw passthrough (``ec-cli api ...``): journaled for audit only,
    #: never part of a transaction.
    RAW = 0
    #: Generated from a spec: best-effort GET-before-write snapshot,
    #: normalize() raises until curated.
    GENERATED = 1
    #: Curated: real normalize(), true reversibility class, ownership
    #: detection where applicable. Earns full commit-confirm.
    CURATED = 2


@dataclasses.dataclass(frozen=True)
class Ref:
    """Reference to one resource instance.

    ``appliance`` is the user-facing appliance name (resolved to nePk via the
    resolver) and is required for APPLIANCE-scope resources.
    """

    kind: str
    name: str
    appliance: str | None = None

    def key(self) -> str:
        if self.appliance is not None:
            return f"{self.kind}:{self.appliance}:{self.name}"
        return f"{self.kind}:{self.name}"

    def __str__(self) -> str:
        return self.key()

    @staticmethod
    def from_key(key: str) -> Ref:
        parts = key.split(":")
        if len(parts) == 2:
            return Ref(kind=parts[0], name=parts[1])
        if len(parts) == 3:
            return Ref(kind=parts[0], appliance=parts[1], name=parts[2])
        raise ValueError(f"malformed ref key: {key!r}")


@dataclasses.dataclass
class Ctx:
    """Runtime context handed to every resource method."""

    client: OrchClient
    resolver: Resolver
    dry_run: bool = False


class DiffOp(str, enum.Enum):
    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"


@dataclasses.dataclass(frozen=True)
class DiffEntry:
    op: DiffOp
    #: Path into the canonical state, JSON-pointer style segments.
    path: tuple[str, ...]
    old: Any = None
    new: Any = None


@dataclasses.dataclass
class Diff:
    """Structural difference between two canonical states."""

    ref: Ref
    entries: list[DiffEntry] = dataclasses.field(default_factory=list)
    #: Snapshot of the desired canonical state the diff was computed against.
    #: apply() implementations use this as the write payload source.
    desired: CanonicalState = None
    current: CanonicalState = None

    @property
    def empty(self) -> bool:
        return not self.entries

    def __iter__(self) -> Iterator[DiffEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclasses.dataclass
class JobOutcome:
    """Terminal result of one Orchestrator async job (action key)."""

    key: str
    state: str  # SUCCESS | FAILED | TIMEOUT
    detail: str = ""
    per_appliance: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ApplyResult:
    ok: bool
    changed: bool = True
    message: str = ""
    jobs: list[JobOutcome] = dataclasses.field(default_factory=list)

    @staticmethod
    def noop(message: str = "no changes") -> ApplyResult:
        return ApplyResult(ok=True, changed=False, message=message)


class ResourceError(Exception):
    """Base error for resource operations."""


class NotCurated(ResourceError):
    """Raised by Tier-0/1 resources where curated behavior is required."""


class OwnershipConflict(ResourceError):
    """Direct appliance-level change on a template-managed section."""

    def __init__(self, ref: Ref, owner: str):
        self.ref = ref
        self.owner = owner
        super().__init__(
            f"{ref} is managed by {owner}; the next template push would "
            f"silently revert a direct change. Re-run with --override-template "
            f"to proceed anyway."
        )


class Resource:
    """Base class for resource plugins.

    Subclasses set the class attributes and implement the abstract methods.
    ``diff()`` has a default structural implementation that is correct for any
    plugin whose ``normalize()`` produces comparable dicts/lists; override only
    when a resource needs semantic diffing beyond that.
    """

    #: Unique kind string, e.g. "bio", "template-group", "appliance/bgp".
    kind: str = ""
    scope: Scope = Scope.ORCHESTRATOR
    reversibility: Reversibility = Reversibility.REVERSIBLE
    tier: Tier = Tier.CURATED
    #: Kinds that must be applied before this kind within one changeset
    #: (e.g. template-group before template-group-association).
    dependencies: tuple[str, ...] = ()
    #: Optional JSON-schema-ish hint used by the CLI for YAML input validation.
    desired_state_doc: str = ""

    # -- mandatory plugin surface -------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raise NotImplementedError

    def normalize(self, raw: RawState) -> CanonicalState:
        raise NotImplementedError

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        raise NotImplementedError

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        raise NotImplementedError

    # -- default implementations --------------------------------------------

    def diff(self, ref: Ref, current: CanonicalState, desired: CanonicalState) -> Diff:
        from pyecsdwan.diffing import structural_diff

        return Diff(
            ref=ref,
            entries=structural_diff(current, desired),
            desired=desired,
            current=current,
        )

    def verify(self, ctx: Ctx, ref: Ref, desired: CanonicalState) -> bool:
        """Post-apply check: server state now matches desired intent."""
        current = self.normalize(self.fetch(ctx, ref))
        return self.diff(ref, current, desired).empty

    def managed_by(self, ctx: Ctx, ref: Ref) -> str | None:
        """Return e.g. ``"template-group X"`` when a template owns this
        config section on this appliance, else ``None``. Appliance-scope
        plugins must override; orchestrator-scope config has no owner."""
        return None

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """Normalize *user intent* (YAML / set-command output) into canonical
        form. Default: run it through ``normalize()`` so both sides of the
        diff pass through identical shaping."""
        return self.normalize(dict(desired))

    def list_refs(self, ctx: Ctx) -> Sequence[Ref]:
        """Enumerate existing instances (for ``show`` and drift reports)."""
        return []
