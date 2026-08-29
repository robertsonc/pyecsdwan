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
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any


def _quote(part: str) -> str:
    # Escape ':' and '%' so component boundaries in a Ref key are unambiguous.
    return urllib.parse.quote(part, safe="")


def _unquote(part: str) -> str:
    return urllib.parse.unquote(part)

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


class Owned(str, enum.Enum):
    """Whether a template owns a config section, as a **tri-state** (#20).

    The two-state version of this — an owner string or ``None`` — was fail
    open, because ``None`` answered two different questions with one word:
    "nothing owns this" and "I could not find out". An unreadable template
    selection, a kind missing from the section map, a section name nobody has
    confirmed: all of them produced the same confident "unowned" that lets a
    direct write through, and a direct write to a template-owned section is
    silently reverted by the next template push.
    """

    #: A template group associated with this appliance selects a section that
    #: covers this kind, or the object itself carries an ownership marker.
    OWNED = "owned"
    #: Positively established: nothing owns it. The *only* state that permits
    #: a direct write without a break-glass flag.
    UNOWNED = "unowned"
    #: Could not be established. Never silently treated as UNOWNED.
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class Ownership:
    """The answer to "would a template push revert this write?", with its
    reason. Build one through the three constructors, never by hand, so the
    reason is never left empty on the states that need it."""

    state: Owned
    #: Who owns it, e.g. ``"template-group Branch-Std"``. OWNED only.
    owner: str = ""
    #: Operator-readable justification. Required for OWNED and UNKNOWN;
    #: carried for UNOWNED too, because "nothing owns it *because* no group is
    #: associated" and "... *because* no associated group selects it" are
    #: different facts and the second one is the one that goes stale.
    reason: str = ""

    @classmethod
    def owned(cls, owner: str, reason: str = "") -> Ownership:
        return cls(Owned.OWNED, owner=owner, reason=reason or owner)

    @classmethod
    def unowned(cls, reason: str) -> Ownership:
        return cls(Owned.UNOWNED, reason=reason)

    @classmethod
    def unknown(cls, reason: str) -> Ownership:
        return cls(Owned.UNKNOWN, reason=reason)

    @property
    def blocks_write(self) -> bool:
        """True unless we positively established that nothing owns this.

        The whole point of the tri-state: UNKNOWN blocks exactly as OWNED
        does. Anything that reads ``state is Owned.OWNED`` to decide whether
        to refuse has reintroduced the bug.
        """
        return self.state is not Owned.UNOWNED

    @property
    def label(self) -> str:
        """What ``show``/``compare``/``--json`` print for this instance."""
        if self.state is Owned.OWNED:
            return f"managed-by: {self.owner}"
        if self.state is Owned.UNKNOWN:
            return f"ownership-unknown: {self.reason}"
        return ""

    def __str__(self) -> str:
        return self.label or "unowned"


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
        # Percent-escape each component so a ':' inside a name (e.g. a BGP
        # neighbor "peer:10.1.1.1") can't corrupt round-tripping through the
        # journal, candidate store, or resolver.
        kind = _quote(self.kind)
        name = _quote(self.name)
        if self.appliance is not None:
            return f"{kind}:{_quote(self.appliance)}:{name}"
        return f"{kind}:{name}"

    def __str__(self) -> str:
        if self.appliance is not None:
            return f"{self.kind}:{self.appliance}:{self.name}"
        return f"{self.kind}:{self.name}"

    @staticmethod
    def from_key(key: str) -> Ref:
        parts = key.split(":")
        if len(parts) == 2:
            return Ref(kind=_unquote(parts[0]), name=_unquote(parts[1]))
        if len(parts) == 3:
            return Ref(
                kind=_unquote(parts[0]),
                appliance=_unquote(parts[1]),
                name=_unquote(parts[2]),
            )
        raise ValueError(f"malformed ref key: {key!r}")


@dataclasses.dataclass
class Ctx:
    """Runtime context handed to every resource method."""

    client: OrchClient
    resolver: Resolver
    dry_run: bool = False

    def save_changes(self, ne_pks: str | Sequence[str], description: str = "") -> JobOutcome:
        """Persist appliance running config after proxy writes (issue #11).

        Appliance-scope resources call this at the end of ``apply()`` /
        ``rollback()``, passing every nePk the operation wrote to through the
        appliance proxy — one batched save per operation, never one per
        write. Returns the terminal :class:`JobOutcome`; anything but SUCCESS
        means the running-config change is unpersisted (lost on reboot) and
        the operation must report failure. ``dry_run`` skips the API call.
        """
        from pyecsdwan import jobs

        if self.dry_run:
            return JobOutcome(key="", state="SUCCESS", detail="dry-run: save-changes skipped")
        pks = [ne_pks] if isinstance(ne_pks, str) else list(ne_pks)
        return jobs.save_changes(self.client, pks, self.client.settings, description)


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
    #: SUCCESS | FAILED | TIMEOUT | UNKNOWN.
    #:
    #: ``UNKNOWN`` (#64) means the job finished but the poller cannot tell
    #: whether it worked — an unrecognised terminal shape, or a keyless save
    #: whose persistence could not be checked. Every consumer branches on
    #: ``state != "SUCCESS"``, so an unknown job fails its transaction; it is a
    #: separate state from FAILED because "it did not work" is a claim this
    #: code cannot support, and because the operator's next move differs
    #: (report the shape, rather than read the Orchestrator's error).
    state: str
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


#: Tokens the command grammar needs for itself, so they cannot also name a
#: resource (issue #71 R12). This constraint arrived with the owner's Q1
#: answer: once the datastore token is optional, ``show configuration <token>``
#: must decide whether ``<token>`` is a datastore, a scope noun, or a kind.
#: While the token was mandatory the position was unambiguous and none of this
#: was needed.
RESERVED_CLI_WORDS: frozenset[str] = frozenset(
    {"running", "candidate", "appliance", "fabric", "configuration"}
)


def default_cli_name(kind: str) -> str:
    """The user-facing noun for a kind, when the plugin does not name one.

    ``appliance/`` is a *scope* prefix, and the command has already established
    scope by the time a kind is read — so repeating it is duplication and it is
    dropped.

    ``generated/`` is deliberately **not** stripped. It marks tier, not scope,
    and what sits beneath it is a raw operation id
    (``generated/appliance_post_virtualif_vti_by_vti_name``). Stripping the
    prefix would leave that id as the "friendly" name, which is not an
    improvement — a Tier-1 stub has no curated noun to offer yet, and saying so
    is more honest than inventing one.
    """
    prefix = "appliance/"
    return kind[len(prefix) :] if kind.startswith(prefix) else kind


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
    #: Whether whole-resource ``delete`` is meaningful. False for singleton
    #: tables (interface-labels) that have no "absent" state — deleting an
    #: entry there means removing a sub-key, not the resource.
    deletable: bool = True
    #: Optional JSON-schema-ish hint used by the CLI for YAML input validation.
    desired_state_doc: str = ""
    #: Spec endpoints this resource covers, as ``"<scope> <METHOD> <path>"``
    #: keys (see ``pyecsdwan.specs.endpoint_key``). Drives ``show coverage``.
    endpoints: tuple[str, ...] = ()
    #: User-facing CLI noun (issue #77). Empty means "derive it" — see
    #: :func:`default_cli_name`. Set this only when the derived name is wrong.
    #:
    #: The operator should never have to type a registry key. ``kind`` stays
    #: the stable internal identifier used by the candidate store, the journal
    #: and every API contract; this is what the command line calls it. The two
    #: are deliberately separate so a nicer noun never becomes a state
    #: migration.
    cli_name: str = ""
    #: Extra accepted spellings for the same resource, within the same scope.
    cli_aliases: tuple[str, ...] = ()

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

    def write_target(self, ctx: Ctx, ref: Ref) -> str | None:
        """Identify the *server object* this instance replaces, or ``None``.

        Different resources can write the same underlying object. The known
        case is ``appliance/deployment`` and ``appliance/dhcp``: DHCP has no
        endpoint of its own and lives in a subtree of the deployment object, so
        both POST ``/deployment`` on the same appliance (#69).

        Two individually-correct writes to one object are order-dependent, and
        ordering is not a fix: ``dhcp`` re-reads and splices, so it survives
        being second, while ``deployment`` posts the whole body it computed at
        plan time, so whatever it follows is overwritten. Planning refuses when
        two changed items return the same string.

        The string is opaque and compared for equality only, so it must
        identify the object *and its instance*: ``"appliance 3.NE deployment"``,
        never ``"deployment"``, or two appliances would look like a conflict.

        ``None`` means "shares nothing" and is right for most resources. It is
        not a fail-open the way ownership's was: a resource writing an object
        nobody else writes has no collision to miss, and
        ``tests/test_write_collisions.py`` derives the resources that *do*
        overlap from their declared endpoints, so a future pair cannot stay
        silently undeclared.
        """
        return None

    def managed_by(self, ctx: Ctx, ref: Ref) -> Ownership:
        """Would a template push revert a direct write to this instance?

        Appliance-scope plugins must override, normally by delegating to
        :func:`pyecsdwan.ownership.owning_group` (after any per-object
        ``gms_marked`` check). The default is the orchestrator-scope answer,
        and it is a *positive* UNOWNED rather than a shrug: template groups
        push appliance configuration, so nothing template-owned can exist on
        an Orchestrator-scope object.

        Returning UNOWNED means "I checked, and nothing owns it". An override
        that cannot check must return :meth:`Ownership.unknown`, which the
        commit guard refuses just as it refuses OWNED (#20).
        """
        return Ownership.unowned("orchestrator-scope config; no template group pushes it")

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
