"""Template-ownership detection, fail closed (#20).

The Orchestrator exposes no per-section "managed-by" field (see
docs/research/appliance-config.md). Ownership is derived as a join:

    appliance's associated template groups (GET /template/applianceAssociation?nePk=)
        x  each group's selected template sections (GET /template/templateSelection?templateGroup=)
        x  a static map of resource kind -> template section name(s)

If any associated group selects a section that covers a resource kind, direct
appliance-level writes to that kind are template-owned: the next template push
would silently revert them. This is the single most common real-world footgun,
so appliance-scope plugins call :func:`owning_group` from ``managed_by()``.

**The join can fail, and it used to fail silently.** Every step above can come
back unreadable, empty, or the wrong shape, and the previous version answered
all of them with ``None`` — the same value that means "checked, nothing owns
it". A 403 on the selection endpoint, a kind missing from the map, a response
that was not a list: each one produced a confident "safe to write directly".

The third factor is the subtle one. Most section names in the map below have
never been seen in a live Default Template Group's selected-section list; they
are spelled after the ECOS path and assumed to match. A *match* on a guessed
name still proves ownership — the name was right. A **non**-match proves
nothing at all, because "that section is not selected" and "I am comparing
against the wrong name" are indistinguishable. So an unverified mapping can
return OWNED but never UNOWNED.

Everything unresolved returns :meth:`Ownership.unknown`, which the commit
guard refuses exactly as it refuses OWNED. See ``docs/live-validation.md`` for
how a section name gets promoted from guess to verified.
"""

from __future__ import annotations

import dataclasses

import structlog

from pyecsdwan.client import OrchApiError
from pyecsdwan.contract import Ctx, Ownership

log = structlog.get_logger("pyecsdwan.ownership")


class Unreadable(Exception):
    """A step of the join could not be read. Carries the operator-readable
    reason so the UNKNOWN that results says *why* it is unknown."""


@dataclasses.dataclass(frozen=True)
class Sections:
    """The template section names that manage one resource kind.

    ``verified`` is the load-bearing field and it means one specific thing: a
    live Orchestrator's ``GET /template/templateSelection`` was observed to
    return this name. Not "it matches the ECOS path", not "the UI seems to
    group it that way" — observed. Anything less is a guess that cannot
    support a negative answer.
    """

    names: tuple[str, ...]
    verified: bool
    #: Where the verification came from, or what the guess is based on.
    note: str

    def matches(self, selected: list[str]) -> bool:
        wanted = {n.lower() for n in self.names}
        return any(s.lower() in wanted for s in selected)


def _verified(note: str, *names: str) -> Sections:
    return Sections(names=names, verified=True, note=note)


def _guess(note: str, *names: str) -> Sections:
    return Sections(names=names, verified=False, note=note)


#: The section names live-confirmed this project has ever seen. A real Default
#: Template Group's selected-section list was probed read-only during #38 and
#: answered with exactly these eleven names. Every ``_verified`` entry below
#: draws from this list and nothing else may claim verification without adding
#: to it — which takes a fabric, not an argument.
LIVE_CONFIRMED_SECTIONS: frozenset[str] = frozenset(
    {
        "adminDistance",
        "cli",
        "dns",
        "datetime",
        "logging",
        "mgmtServices",
        "routes",
        "secureWebServicesConfig",
        "shaper",
        "snmp",
        "webconfig",
    }
)

_ECOS_PATH_GUESS = (
    "spelled after the ECOS path; never seen in a live selected-section list"
)

#: Resource kind -> the template sections that manage it, and whether anyone
#: has confirmed those names against a real Orchestrator.
SECTION_MAP: dict[str, Sections] = {
    # -- verified: names drawn verbatim from the #38 live probe ---------------
    "appliance/dns": _verified("live probe (#38) returned 'dns'", "dns"),
    "appliance/routes": _verified(
        "live probe (#38) returned 'routes'; 'subnets' is an extra candidate "
        "that only ever widens a match",
        "subnets",
        "routes",
    ),
    "appliance/shaper": _verified("live probe (#38) returned 'shaper'", "shaper"),
    "appliance/snmp": _verified("live probe (#38) returned 'snmp'", "snmp"),
    "appliance/logging": _verified("live probe (#38) returned 'logging'", "logging"),
    "appliance/mgmt-services": _verified(
        "live probe (#38) returned 'mgmtServices'", "mgmtServices"
    ),
    "appliance/inbound-shaper": _verified(
        "live probe (#38) returned 'shaper', and the Orchestrator's Shaper "
        "template covers both directions, so the confirmed name alone decides "
        "this kind; 'inboundShapers' is an unconfirmed extra",
        "shaper",
        "inboundShapers",
    ),
    # -- guesses: a match still proves ownership, a non-match proves nothing --
    #
    # Several of these were previously commented "CONFIRMED real (matches the
    # ECOS path itself)". That is not confirmation, it is the guess restated —
    # the ECOS path and the template section name are different namespaces that
    # happen to agree often. They are recorded here as what they are.
    "appliance/security-policy": _guess(_ECOS_PATH_GUESS, "securityMaps"),
    "appliance/bgp": _guess(_ECOS_PATH_GUESS, "bgp"),
    "appliance/ospf": _guess(_ECOS_PATH_GUESS, "ospf"),
    "appliance/vrrp": _guess(_ECOS_PATH_GUESS, "vrrp"),
    "appliance/dhcp": _guess(_ECOS_PATH_GUESS, "dhcpd", "dhcpFailover"),
    "appliance/nat": _guess(_ECOS_PATH_GUESS, "natMaps"),
    "appliance/deployment": _guess(
        "UI-grouping candidates; the live probe's group selected neither (#12)",
        "deployment",
        "interfaces",
    ),
    # The kind here is deliberately "appliance/security-maps", not the
    # pre-seeded "appliance/security-policy" above nor the orchestrator-scope
    # "security-policy" kind, to keep all three names unambiguous (#19).
    "appliance/security-maps": _guess(_ECOS_PATH_GUESS, "securityMaps"),
    "appliance/zones": _guess(_ECOS_PATH_GUESS, "zones"),
    # The bare "appliance/nat" above is left in place for Branch NAT (ECOS
    # "nat/maps"): one kind cannot name the two distinct appliance NAT
    # resources, which have different endpoints (#32).
    "appliance/nat-maps": _guess(_ECOS_PATH_GUESS, "natMaps"),
    "appliance/nat-pools": _guess(_ECOS_PATH_GUESS, "natPools"),
    "appliance/qos-map": _guess(_ECOS_PATH_GUESS, "qosMaps"),
    "appliance/optimization-map": _guess(_ECOS_PATH_GUESS, "optimizationMaps"),
    "appliance/route-map": _guess(_ECOS_PATH_GUESS, "routeMaps"),
    "appliance/banners": _guess(
        "'banners' is NOT among the live-probed names, so the UI may fold "
        "login banners into another section entirely",
        "banners",
    ),
    # acls.py prefers the per-rule gms_marked flag; this join is its fallback.
    "appliance/acl": _guess(_ECOS_PATH_GUESS, "acls"),
    # No entry for "schedule-timezone": it is Orchestrator-scope config
    # (/gms/scheduleTimezone), not per-appliance, so no template owns it.
    # "datetime" is live-confirmed and unclaimed — it is the section a
    # per-appliance NTP/time resource should take once one exists.
}

#: Back-compatible view for callers that only want the names. Derived, so it
#: cannot drift from the table above — and deliberately lossy: it drops the
#: verification status, which is why nothing in the ownership decision reads it.
KIND_TO_TEMPLATE_SECTIONS: dict[str, tuple[str, ...]] = {
    kind: entry.names for kind, entry in SECTION_MAP.items()
}


def associated_groups(ctx: Ctx, ne_pk: str) -> list[str]:
    """Template groups currently associated to an appliance.

    Raises :class:`Unreadable` rather than guessing. The empty case is not an
    error and does not come through here: the baseline documents it as a 200
    carrying ``{"templateIds": []}``, so a 404 is something else — a wrong
    path, an unknown appliance, a permission boundary — and answering it with
    "no groups" would be inventing the safest possible fact out of an error.
    """
    try:
        raw = ctx.client.get("/template/applianceAssociation", params={"nePk": ne_pk})
    except OrchApiError as exc:
        raise Unreadable(
            f"template associations for {ne_pk} unreadable: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise Unreadable(
            f"template associations for {ne_pk} came back as "
            f"{type(raw).__name__}, expected an object with templateIds"
        )
    ids = raw.get("templateIds", [])
    if not isinstance(ids, list):
        raise Unreadable(
            f"templateIds for {ne_pk} came back as {type(ids).__name__}, expected a list"
        )
    return [str(g) for g in ids]


def selected_sections(ctx: Ctx, template_group: str) -> list[str]:
    """Template section names a group actually has selected.

    A non-list response is :class:`Unreadable`, not an empty selection: the
    baseline types this as an array of strings, so anything else means the
    read did not do what we think it did.
    """
    try:
        raw = ctx.client.get(
            "/template/templateSelection", params={"templateGroup": template_group}
        )
    except OrchApiError as exc:
        raise Unreadable(
            f"template selection for group {template_group!r} unreadable: {exc}"
        ) from exc
    if not isinstance(raw, list):
        raise Unreadable(
            f"template selection for group {template_group!r} came back as "
            f"{type(raw).__name__}, expected a list of section names"
        )
    return [str(s) for s in raw]


def owning_group(ctx: Ctx, kind: str, ne_pk: str) -> Ownership:
    """Resolve template ownership of ``kind`` on one appliance.

    OWNED wins over every uncertainty: a matched section name is a matched
    section name regardless of how confident the rest of the join is. The
    order below reflects that — a match short-circuits before any unreadable
    group or unverified name gets to force an UNKNOWN.
    """
    entry = SECTION_MAP.get(kind)
    if entry is None:
        return Ownership.unknown(
            f"no template-section mapping for {kind}; ownership cannot be checked. "
            f"Add one to pyecsdwan.ownership.SECTION_MAP"
        )
    try:
        groups = associated_groups(ctx, ne_pk)
    except Unreadable as exc:
        log.debug("ownership_association_unreadable", ne_pk=ne_pk, error=str(exc))
        return Ownership.unknown(str(exc))

    if not groups:
        # The one clean negative that holds whatever the section names are:
        # ownership needs an associated group, and there is none. This is why
        # unverified mappings do not make every appliance UNKNOWN.
        return Ownership.unowned(f"no template group is associated with {ne_pk}")

    unreadable: list[str] = []
    for group in groups:
        try:
            selected = selected_sections(ctx, group)
        except Unreadable as exc:
            log.debug("ownership_selection_unreadable", group=group, error=str(exc))
            unreadable.append(group)
            continue
        if entry.matches(selected):
            return Ownership.owned(
                f"template-group {group}",
                reason=f"group {group!r} selects a section covering {kind}",
            )

    names = ", ".join(entry.names)
    if unreadable:
        return Ownership.unknown(
            f"template selection unreadable for group(s) {', '.join(unreadable)} "
            f"associated with {ne_pk}; one of them may select {names}"
        )
    if not entry.verified:
        return Ownership.unknown(
            f"no group associated with {ne_pk} selects {names}, but those section "
            f"names are unverified for {kind} ({entry.note}) — a non-match cannot "
            f"tell 'not selected' from 'wrong name'. Confirm the name against a "
            f"live template group and mark it verified in "
            f"pyecsdwan.ownership.SECTION_MAP"
        )
    return Ownership.unowned(
        f"no template group associated with {ne_pk} selects {names}"
    )
