"""The offline command reference (#77, taxonomy T4).

Answers "what can I type, and what will it do?" without an Orchestrator. That
is the point: an operator deciding whether this tool can do a thing, or
writing a script against it, should not have to authenticate against a fabric
to find out.

Distinct from ``show coverage``, which answers a different question — *what
API surface does each plugin cover, and at which tier* — and is organised by
endpoint. This is organised by command.

**Every row is generated from the same constants the parser uses**, never
hand-listed. A reference that is maintained separately from the parser is a
reference that is eventually wrong, and wrong in the direction that matters:
it documents commands that do not exist. `tests/test_command_reference.py`
closes the loop from the other side, asserting that every offerable noun in
the registry appears in a row.

What this deliberately does *not* claim: how many instances of a kind exist
on your fabric. That is a live question, and an offline view that answered it
would be guessing. The ``address`` column says how a kind is *addressed* —
derived from ``Resource.deletable``, whose own contract ties it to singleton
tables with no absent state — not how many objects are out there.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from pyecsdwan.contract import Resource, Reversibility, Scope, Tier
from pyecsdwan.registry import Registry

#: Intents, as `grammar.md` §1 names them. `cli-state` is the fifth category
#: the corpus has no precedent for — the subject is the tool, not the fabric.
OPERATIONAL = "operational"
CONFIGURATION = "configuration"
CLI_STATE = "cli-state"


@dataclasses.dataclass(frozen=True)
class CommandRow:
    """One command an operator can type."""

    command: str
    intent: str
    scope: str
    #: How the object is addressed, not how many exist — see the module
    #: docstring. "singleton" | "named" | "-".
    address: str
    #: "read-only", or the resource's reversibility class for a mutable kind.
    mutability: str
    #: "supported", or "unsupported: <why>". A view with no source stays
    #: listed and says so; hiding it would make the CLI look like it never
    #: considered the question (#72 finding 2).
    support: str

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _address(resource: Resource) -> str:
    """How a kind is addressed, from its declared contract.

    `Resource.deletable` is documented as "False for singleton tables that
    have no absent state" — so it is the existing, per-kind declaration of
    exactly this, rather than something inferred from a name or guessed from
    a live fetch.
    """
    return "named" if resource.deletable else "singleton"


def _mutability(resource: Resource) -> str:
    if resource.tier < Tier.CURATED:
        # A stub's normalize() raises, so nothing can be staged against it.
        return "read-only (not curated)"
    if resource.reversibility is Reversibility.REVERSIBLE:
        return "reversible"
    return str(resource.reversibility.value)


def _support(resource: Resource) -> str:
    if resource.tier is Tier.RAW:
        return "unsupported: raw passthrough, journaled for audit only"
    if resource.tier is Tier.GENERATED:
        return "unsupported: generated stub, normalize() refuses"
    return "supported"


def build(registry: Registry) -> list[CommandRow]:
    """Every command, generated from the parser's own tables.

    Imported lazily so this module stays importable without prompt_toolkit —
    the reference must run in the thinnest environment an operator has.
    """
    from pyecsdwan.cli import shell
    from pyecsdwan.reports import fabric

    rows: list[CommandRow] = []

    def add(command: str, intent: str, scope: str, address: str, mut: str, sup: str) -> None:
        rows.append(CommandRow(command, intent, scope, address, mut, sup))

    # -- the tool's own state -------------------------------------------------
    for noun in shell._SHOW_CLI_STATE:
        suffix = " pending" if noun == "transactions" else ""
        add(f"show {noun}{suffix}", CLI_STATE, "-", "-", "read-only", "supported")
    # `commands` is itself in _SHOW_CLI_STATE, so it is generated above. Adding
    # it by hand here produced a duplicate row — which is the whole reason
    # nothing in this function is hand-listed.

    # -- operational: fabric --------------------------------------------------
    add("show fabric version", OPERATIONAL, "fabric", "-", "read-only", "supported")
    add("show fabric flows summary", OPERATIONAL, "fabric", "-", "read-only", "supported")
    add("show fabric flow <ip>", OPERATIONAL, "fabric", "named", "read-only", "supported")

    # -- operational: one appliance -------------------------------------------
    for view in shell._BGP_VIEWS:
        arg = " [<ip>]" if view == "neighbors" else ""
        support = (
            "unsupported: no BGP route-table endpoint in the supported API"
            if view == "routes"
            else "supported"
        )
        add(
            f"show appliance <name> bgp {view}{arg}",
            OPERATIONAL,
            "appliance",
            "named" if view == "neighbors" else "-",
            "read-only",
            support,
        )

    # -- configuration --------------------------------------------------------
    add(
        f"show configuration fabric [{'|'.join(fabric.SECTIONS)}]",
        CONFIGURATION,
        "fabric",
        "-",
        "read-only",
        "supported",
    )
    add(
        "show configuration appliance <name> --format native",
        CONFIGURATION,
        "appliance",
        "-",
        "read-only",
        "supported",
    )
    add("show configuration candidate", CONFIGURATION, "-", "-", "read-only", "supported")

    prefixes = {
        Scope.ORCHESTRATOR: "show configuration",
        Scope.APPLIANCE: "show configuration appliance <name>",
    }
    # Every registered kind, not just the offerable nouns. `cli_names()`
    # excludes Tier-1 stubs on purpose — completing a name whose normalize()
    # raises is offering a dead end — but a *reference* that omits them
    # answers "can this tool do X?" with silence, which reads as "no such
    # thing" rather than "there, and not curated yet". They are listed with
    # the only spelling they have (`generated/<operation-id>`), which is
    # exactly why that prefix is kept rather than stripped (#77).
    for kind in registry.kinds():
        resource = registry.get(kind)
        prefix = prefixes.get(resource.scope)
        if prefix is None:
            continue
        add(
            f"{prefix} {registry.cli_name(kind)} [<instance>]",
            CONFIGURATION,
            resource.scope.value,
            _address(resource),
            _mutability(resource),
            _support(resource),
        )
    return rows
