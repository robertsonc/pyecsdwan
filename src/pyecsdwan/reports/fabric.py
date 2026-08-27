"""``show run``: the fabric's Orchestrator-managed configuration (#55).

Where ``show run appliance <name>`` (#56) asks one appliance what *it* thinks
its config is, this asks the Orchestrator what it is *managing*: which
overlays exist and who is in them, which template groups exist and where they
are applied, what security policy is orchestrated, what the inventory looks
like, and how each appliance is deployed. Five sections, one command.

Read-only by construction. Every call below is a GET; nothing here stages a
candidate, writes the journal, opens a transaction, or calls ``saveChanges``
— the same standing obligation as the rest of :mod:`pyecsdwan.reports`, and
``tests/test_show_run.py`` asserts it rather than trusting the docstring.

Read paths are the resources' own
---------------------------------
Nothing here re-derives a shape a resource plugin already knows:

* **overlays** — ``ctx.resolver.overlays()`` is the exact
  ``GET /gms/overlays/config`` list read that ``resources/overlays.py`` names
  through, and ``GET /gms/overlays/association`` is read as
  ``BioAssociation.fetch`` reads it (a map of ``overlayId -> [nePk]``, with
  the ids as *strings*).
* **templates** — ``GET /template/templateGroups`` in its list form, then
  ``TemplateGroup.normalize`` for the section names, which is where the
  knowledge that ``templates`` arrives as either a list of
  ``{name, valObject}`` or an already-canonical map lives. The association is
  read in its **unfiltered** form (``GET /template/applianceAssociation`` with
  no ``nePk``), which answers ``{nePk: [group, ...]}`` for the whole fabric in
  one call — ``TemplateAssociation.fetch`` reads the per-appliance form
  because it diffs one appliance; a fabric report wants the map and would be
  wrong to fan out for it.
* **security policy** — ``SecurityPolicy.normalize``, which already knows the
  response may or may not wrap the maps in ``data``/``maps`` and that ``self``
  and ``gms_marked`` are echoes rather than content.
* **deployment** — ``resources/deployment.py``'s read path, the appliance
  proxy (``GET /appliance/rest?nePk=&url=deployment``). See the note below.

Two endpoint findings, both deliberate departures from the issue brief
----------------------------------------------------------------------
* The brief lists per-appliance deployment as ``GET /deployment?nePk=``. That
  Orchestrator-scope operation does exist in
  ``specs/orchestrator-openapi-7.2.0.json``, but it requires **both** ``nePk``
  and ``cached`` (an omitted ``cached`` is a 422, exactly as with
  ``/appliancesSoftwareVersions`` — see :mod:`pyecsdwan.reports.versions`),
  and its summary says it "does a direct call to the appliance". The
  *appliance-scope* ``deployment`` object read through the proxy is the one
  ``resources/deployment.py`` already reads, normalizes and writes back, so
  that is the path taken here: same object, same shape knowledge, and the
  brief's own instruction to reuse the resource read paths points at it.
* There is no listing endpoint for orchestrated security policy that this
  codebase already reads: ``GET /vrf/config/securityPolicies`` requires a
  ``map`` (a ``<srcSegment>_<dstSegment>`` pair). Segment ids therefore come
  from ``resources/zones.py``'s ``segment_zone_map()`` read-only view
  (``GET /zones/vrfZonesMap``), and the pairs to read are derived from them.
  The cross product is bounded (:data:`MAX_POLICY_READS`): a fabric with many
  segments reads the intra-segment pairs only and says so in-band, rather than
  turning a report into an N-squared GET storm against a control plane.

Partial data is data
--------------------
Every section collects behind its own ``try``. A section that cannot be read
renders as itself, empty, carrying the reason as a note — one dead endpoint
costs the operator that section and nothing else. The appliance inventory is
read once and shared; when *it* fails, the sections that need names fall back
to raw nePks and say so, because a report whose whole point is orientation is
most wanted exactly when part of the fabric is not answering.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final

import structlog

from pyecsdwan.contract import Ctx
from pyecsdwan.reports.fanout import DEFAULT_CONCURRENCY, fan_out

# `pyecsdwan.resources` is imported inside the collectors below, never at
# module scope: importing the package registers every built-in plugin, and
# `cli/main.py` imports `pyecsdwan.reports` at module load. A module-level
# import here would drag the whole plugin set into `ec-cli --help`, which
# `runtime.bootstrap()` deliberately defers.

log = structlog.get_logger(__name__)

# -- endpoints (all GET; see the module docstring for whose read path each is)

OVERLAY_CONFIG_PATH: Final[str] = "/gms/overlays/config"
OVERLAY_ASSOCIATION_PATH: Final[str] = "/gms/overlays/association"
TEMPLATE_GROUPS_PATH: Final[str] = "/template/templateGroups"
TEMPLATE_ASSOCIATION_PATH: Final[str] = "/template/applianceAssociation"
SECURITY_POLICY_PATH: Final[str] = "/vrf/config/securityPolicies"
#: ECOS path handed to the appliance proxy — relative, no leading slash.
DEPLOYMENT_ECOS_PATH: Final[str] = "deployment"

# -- section names ------------------------------------------------------------

OVERLAYS: Final[str] = "overlays"
TEMPLATES: Final[str] = "templates"
SECURITY: Final[str] = "security"
INVENTORY: Final[str] = "appliances"
DEPLOYMENT: Final[str] = "deployment"

#: Canonical order: what the fabric overlays and templates first (the
#: Orchestrator-managed intent this command is named for), then the policy over
#: it, then what it is made of, and last the per-appliance deployment — the one
#: section that costs a fan-out.
SECTIONS: Final[tuple[str, ...]] = (OVERLAYS, TEMPLATES, SECURITY, INVENTORY, DEPLOYMENT)

#: Rendered wherever the Orchestrator reports no value for a grouping field.
UNSPECIFIED: Final[str] = "unspecified"

#: Ceiling on segment-pair policy reads (see the module docstring). Sixteen is
#: four segments' full cross product — past that, intra-segment pairs only.
MAX_POLICY_READS: Final[int] = 16

#: ``ApplianceItem.networkRole``: "spoke = 0, hub = 1, nrhub = 3". Anything
#: else passes through as the Orchestrator spelled it — a role code this
#: version does not know is still worth counting, just not worth renaming.
_ROLE_NAMES: Final[dict[str, str]] = {"0": "spoke", "1": "hub", "3": "nrhub"}

#: ``ApplianceItem.state``, per the same schema.
_STATE_NAMES: Final[dict[str, str]] = {
    "0": "unknown",
    "1": "normal",
    "2": "unreachable",
    "3": "unsupported version",
    "4": "out of sync",
    "5": "sync in progress",
}


class UnknownSection(ValueError):
    """``--section`` named something this report does not have.

    A ``ValueError`` carrying the valid names, so both the CLI's error path
    and the shell's render it as a clean line that tells the operator what to
    type instead.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"unknown section {name!r}; valid sections: {', '.join(SECTIONS)}"
        )


def resolve_sections(section: str | None) -> tuple[str, ...]:
    """Which sections to collect: all of them, or the one named.

    Raises :class:`UnknownSection` for a name that is not one, rather than
    silently rendering an empty report — an operator who mistypes ``--section``
    must not be told the fabric has no overlays.
    """
    if section is None:
        return SECTIONS
    if section not in SECTIONS:
        raise UnknownSection(section)
    return (section,)


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _counts(values: Sequence[str]) -> tuple[tuple[str, int], ...]:
    """``(value, count)`` ordered by count descending, then name — a stable
    ordering, so two runs against an unchanged fabric render identically."""
    counter = Counter(values)
    return tuple(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


# -- sections -----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Section:
    """One section of the report, degraded or not.

    ``notes`` is how partial data reaches the operator: a section that could
    not be read fully still renders, carrying the reason. Empty notes means
    everything this section claims was actually read.
    """

    notes: tuple[str, ...] = ()

    #: Section name as ``--section`` spells it.
    name: ClassVar[str] = ""

    @property
    def degraded(self) -> bool:
        return bool(self.notes)


@dataclasses.dataclass(frozen=True)
class Overlay:
    """One Business Intent Overlay and the appliances associated to it."""

    overlay_id: str
    name: str
    topology: str = ""
    #: Appliance hostnames, or the raw nePk where inventory could not name it.
    members: tuple[str, ...] = ()
    member_ne_pks: tuple[str, ...] = ()

    @property
    def member_count(self) -> int:
        return len(self.member_ne_pks)


@dataclasses.dataclass(frozen=True)
class OverlaySection(Section):
    overlays: tuple[Overlay, ...] = ()
    #: Appliances associated to no overlay at all — the condition worth
    #: spotting in this section.
    unassociated: tuple[str, ...] = ()

    name: ClassVar[str] = OVERLAYS

    @property
    def empty_overlays(self) -> tuple[Overlay, ...]:
        return tuple(o for o in self.overlays if not o.member_count)


@dataclasses.dataclass(frozen=True)
class TemplateGroupInfo:
    """One template group: what it carries and where it is applied."""

    name: str
    #: Template section names inside the group (Firewall Zones, SNMP, ...).
    sections: tuple[str, ...] = ()
    #: Appliance hostnames the group is associated to.
    applied_to: tuple[str, ...] = ()

    @property
    def applied_count(self) -> int:
        return len(self.applied_to)


@dataclasses.dataclass(frozen=True)
class TemplateSection(Section):
    groups: tuple[TemplateGroupInfo, ...] = ()
    #: Appliances with no template group applied.
    unassigned: tuple[str, ...] = ()

    name: ClassVar[str] = TEMPLATES

    @property
    def unapplied_groups(self) -> tuple[TemplateGroupInfo, ...]:
        return tuple(g for g in self.groups if not g.applied_count)


@dataclasses.dataclass(frozen=True)
class PolicyMap:
    """Orchestrated security policy for one ``<srcSegment>_<dstSegment>`` pair."""

    pair: str
    #: False when the Orchestrator has no policy for this pair (a 204/empty
    #: body, which is a real answer — not a failure).
    present: bool = False
    rule_count: int = 0
    #: Zone pairs (``<fromZoneId>_<toZoneId>``) carrying at least one rule.
    zone_pairs: int = 0
    maps: tuple[str, ...] = ()
    #: ``(action, count)`` over every rule's ``set.action`` — allow vs deny is
    #: the one thing a policy summary must never round off.
    actions: tuple[tuple[str, int], ...] = ()
    error: str = ""

    @property
    def unreachable(self) -> bool:
        return bool(self.error)


@dataclasses.dataclass(frozen=True)
class SecuritySection(Section):
    #: Segment ids discovered from the segment/zone map.
    segments: tuple[str, ...] = ()
    policies: tuple[PolicyMap, ...] = ()

    name: ClassVar[str] = SECURITY

    @property
    def configured(self) -> tuple[PolicyMap, ...]:
        return tuple(p for p in self.policies if p.present)

    @property
    def total_rules(self) -> int:
        return sum(p.rule_count for p in self.policies)


@dataclasses.dataclass(frozen=True)
class ApplianceRow:
    """One appliance as the Orchestrator inventory describes it."""

    ne_pk: str
    hostname: str
    site: str = UNSPECIFIED
    model: str = UNSPECIFIED
    role: str = UNSPECIFIED
    state: str = UNSPECIFIED


@dataclasses.dataclass(frozen=True)
class InventorySection(Section):
    appliances: tuple[ApplianceRow, ...] = ()

    name: ClassVar[str] = INVENTORY

    @property
    def total(self) -> int:
        return len(self.appliances)

    @property
    def by_role(self) -> tuple[tuple[str, int], ...]:
        return _counts([a.role for a in self.appliances])

    @property
    def by_site(self) -> tuple[tuple[str, int], ...]:
        return _counts([a.site for a in self.appliances])

    @property
    def by_model(self) -> tuple[tuple[str, int], ...]:
        return _counts([a.model for a in self.appliances])

    @property
    def by_state(self) -> tuple[tuple[str, int], ...]:
        return _counts([a.state for a in self.appliances])


@dataclasses.dataclass(frozen=True)
class ApplianceDeployment:
    """One appliance's deployment object, reduced to what a breakdown shows."""

    ne_pk: str
    hostname: str
    mode: str = UNSPECIFIED
    license: str = ""
    wan_labels: tuple[str, ...] = ()
    lan_labels: tuple[str, ...] = ()
    interfaces: int = 0
    addresses: int = 0
    #: Non-empty when this appliance could not be read; the row still renders.
    error: str = ""

    @property
    def unreachable(self) -> bool:
        return bool(self.error)


@dataclasses.dataclass(frozen=True)
class DeploymentSection(Section):
    appliances: tuple[ApplianceDeployment, ...] = ()

    name: ClassVar[str] = DEPLOYMENT

    @property
    def reachable(self) -> tuple[ApplianceDeployment, ...]:
        return tuple(a for a in self.appliances if not a.unreachable)

    @property
    def unreachable(self) -> tuple[ApplianceDeployment, ...]:
        return tuple(a for a in self.appliances if a.unreachable)

    @property
    def by_mode(self) -> tuple[tuple[str, int], ...]:
        return _counts([a.mode for a in self.reachable])


#: Every section shape, for the renderer's dispatch and for typing.
FabricSection = (
    OverlaySection | TemplateSection | SecuritySection | InventorySection | DeploymentSection
)


@dataclasses.dataclass(frozen=True)
class FabricConfig:
    """The whole report: the sections that were asked for, in canonical order."""

    sections: tuple[FabricSection, ...] = ()
    #: What ``--section`` asked for (all of them when it was not given).
    requested: tuple[str, ...] = SECTIONS

    def section(self, name: str) -> FabricSection | None:
        return next((s for s in self.sections if s.name == name), None)

    @property
    def degraded(self) -> tuple[FabricSection, ...]:
        return tuple(s for s in self.sections if s.degraded)


# -- collection ---------------------------------------------------------------


def _inventory(ctx: Ctx) -> tuple[list[dict[str, Any]], str]:
    """``(inventory, error)`` — never raises.

    ``versions.collect`` lets an inventory failure propagate because without a
    list of appliances it has no report at all. This one does have a report:
    overlays, template groups and security policy are all fabric-level and
    render fine against raw nePks, so the failure degrades the sections that
    wanted names instead of taking the command down.
    """
    try:
        return list(ctx.resolver.appliances()), ""
    except Exception as exc:  # noqa: BLE001 - a nameless report still beats no report
        reason = _reason(exc)
        log.debug("fabric_inventory_failed", error=reason)
        return [], reason


def _names_by_ne_pk(inventory: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for entry in inventory:
        ne_pk = entry.get("nePk") or entry.get("id")
        if not ne_pk:
            continue
        names[str(ne_pk)] = str(entry.get("hostName") or ne_pk)
    return names


def _resolve_members(ne_pks: Sequence[str], names: Mapping[str, str]) -> tuple[str, ...]:
    """nePks -> hostnames, sorted, keeping the nePk where inventory has no name.

    A member the inventory does not know is still a member: dropping it would
    under-report an overlay, which is worse than printing a raw key.
    """
    return tuple(sorted(names.get(pk, pk) for pk in ne_pks))


def collect_overlays(
    ctx: Ctx, inventory: Sequence[Mapping[str, Any]], *, inventory_error: str = ""
) -> OverlaySection:
    """Overlays and their members: config list joined onto the association map."""
    notes: list[str] = []
    if inventory_error:
        notes.append(f"appliance names unavailable ({inventory_error}); showing nePks")
    names = _names_by_ne_pk(inventory)

    try:
        # Typed loosely on purpose: the resolver declares list[dict], but it
        # hands back whatever the endpoint answered, and a report must survive
        # an entry that is not an object rather than raise on it.
        configs: list[Any] = list(ctx.resolver.overlays())
    except Exception as exc:  # noqa: BLE001 - the section renders empty, flagged
        log.debug("fabric_overlays_failed", error=_reason(exc))
        return OverlaySection(notes=(*notes, f"overlay config unreadable: {_reason(exc)}"))

    association: dict[str, list[str]] = {}
    try:
        raw = ctx.client.get(OVERLAY_ASSOCIATION_PATH)
        if isinstance(raw, dict):
            # {overlayId: [nePk]}, ids as strings — read exactly as
            # BioAssociation.fetch reads it.
            association = {
                str(overlay_id): [str(pk) for pk in pks]
                for overlay_id, pks in raw.items()
                if isinstance(pks, list)
            }
        else:
            notes.append("overlay association response was not a map; members unknown")
    except Exception as exc:  # noqa: BLE001 - overlays still list without members
        notes.append(f"overlay membership unreadable: {_reason(exc)}")
        log.debug("fabric_overlay_association_failed", error=_reason(exc))

    overlays: list[Overlay] = []
    associated: set[str] = set()
    for entry in configs:
        if not isinstance(entry, dict):
            continue
        overlay_id = str(entry.get("id", ""))
        member_pks = tuple(association.get(overlay_id, ()))
        associated.update(member_pks)
        overlays.append(
            Overlay(
                overlay_id=overlay_id,
                name=str(entry.get("name") or overlay_id or UNSPECIFIED),
                topology=str(entry.get("topology") or ""),
                members=_resolve_members(member_pks, names),
                member_ne_pks=member_pks,
            )
        )
    overlays.sort(key=lambda o: o.name)

    unassociated = tuple(sorted(name for pk, name in names.items() if pk not in associated))
    return OverlaySection(
        notes=tuple(notes), overlays=tuple(overlays), unassociated=unassociated
    )


def _template_group_objects(raw: Any) -> list[dict[str, Any]]:
    """Group objects from ``GET /template/templateGroups``, list or map form.

    Mirrors ``Resolver._fetch_template_groups``: the endpoint answers a list on
    the fabrics this codebase has seen, but the resolver already tolerates a
    map keyed by group name, and a report must not be the one place that
    disagrees.
    """
    if isinstance(raw, list):
        return [g for g in raw if isinstance(g, dict)]
    if isinstance(raw, dict):
        groups: list[dict[str, Any]] = []
        for key, value in raw.items():
            if isinstance(value, dict) and ("name" in value or "templates" in value):
                groups.append({"name": value.get("name") or key, **value})
        return groups
    return []


def collect_templates(
    ctx: Ctx, inventory: Sequence[Mapping[str, Any]], *, inventory_error: str = ""
) -> TemplateSection:
    """Template groups, what each carries, and which appliances it is applied to."""
    notes: list[str] = []
    if inventory_error:
        notes.append(f"appliance names unavailable ({inventory_error}); showing nePks")
    names = _names_by_ne_pk(inventory)

    try:
        raw_groups = _template_group_objects(ctx.client.get(TEMPLATE_GROUPS_PATH))
    except Exception as exc:  # noqa: BLE001 - the section renders empty, flagged
        log.debug("fabric_template_groups_failed", error=_reason(exc))
        return TemplateSection(notes=(*notes, f"template groups unreadable: {_reason(exc)}"))

    # Unfiltered form: {nePk: [group, ...]} for the whole fabric in one call.
    applied: dict[str, list[str]] = {}
    applied_known = True
    try:
        raw_assoc = ctx.client.get(TEMPLATE_ASSOCIATION_PATH)
        if isinstance(raw_assoc, dict):
            applied = {
                str(ne_pk): [str(g) for g in groups]
                for ne_pk, groups in raw_assoc.items()
                if isinstance(groups, list)
            }
        else:
            applied_known = False
            notes.append("template association response was not a map; application unknown")
    except Exception as exc:  # noqa: BLE001 - groups still list without their targets
        applied_known = False
        notes.append(f"template application unreadable: {_reason(exc)}")
        log.debug("fabric_template_association_failed", error=_reason(exc))

    from pyecsdwan.resources import templates as templates_res

    normalizer = templates_res.TemplateGroup()
    groups: list[TemplateGroupInfo] = []
    for entry in raw_groups:
        name = str(entry.get("name") or UNSPECIFIED)
        # TemplateGroup.normalize owns the list-vs-map shape of `templates`.
        canonical = normalizer.normalize(entry)
        sections = (
            tuple(sorted(str(s) for s in canonical.get("templates", {})))
            if isinstance(canonical, dict)
            else ()
        )
        targets = tuple(
            sorted(
                names.get(ne_pk, ne_pk)
                for ne_pk, group_names in applied.items()
                if name in group_names
            )
        )
        groups.append(TemplateGroupInfo(name=name, sections=sections, applied_to=targets))
    groups.sort(key=lambda g: g.name)

    # Only claim "no group applied" when the association map was actually
    # read: an unreadable association would otherwise report every appliance
    # as untemplated, which is the most alarming possible way to be wrong.
    unassigned = (
        tuple(sorted(name for pk, name in names.items() if not applied.get(pk)))
        if applied_known
        else ()
    )
    return TemplateSection(notes=tuple(notes), groups=tuple(groups), unassigned=unassigned)


def _segment_pairs(segments: Sequence[str]) -> tuple[tuple[str, ...], str]:
    """``(pairs, note)`` — which ``<src>_<dst>`` maps to read, and why.

    The full cross product while it stays under :data:`MAX_POLICY_READS`;
    intra-segment pairs only past that, with the omission stated in-band. A
    report is allowed to be incomplete against a control plane — it is not
    allowed to be quietly incomplete.
    """
    if not segments:
        return (), ""
    if len(segments) ** 2 <= MAX_POLICY_READS:
        return tuple(f"{src}_{dst}" for src in segments for dst in segments), ""
    return (
        tuple(f"{seg}_{seg}" for seg in segments),
        f"{len(segments)} segments: inter-segment policy pairs omitted "
        f"(a full read would be {len(segments) ** 2} calls, over the "
        f"{MAX_POLICY_READS}-call bound); read one with "
        f"'ec-cli show security-policy <src>_<dst>'",
    )


def _summarize_policy(pair: str, canonical: Any) -> PolicyMap:
    """Rule and action counts for one segment pair's normalized policy."""
    maps = canonical.get("maps", {}) if isinstance(canonical, dict) else {}
    if not isinstance(maps, dict) or not maps:
        return PolicyMap(pair=pair, present=False)
    rules = 0
    zone_pairs = 0
    actions: list[str] = []
    for zone_map in maps.values():
        if not isinstance(zone_map, dict):
            continue
        for zone_pair in zone_map.values():
            if not isinstance(zone_pair, dict):
                continue
            prio = zone_pair.get("prio")
            if not isinstance(prio, dict) or not prio:
                continue
            zone_pairs += 1
            rules += len(prio)
            for rule in prio.values():
                setting = rule.get("set") if isinstance(rule, dict) else None
                action = setting.get("action") if isinstance(setting, dict) else None
                actions.append(str(action) if action else UNSPECIFIED)
    return PolicyMap(
        pair=pair,
        present=True,
        rule_count=rules,
        zone_pairs=zone_pairs,
        maps=tuple(sorted(str(m) for m in maps)),
        actions=_counts(actions),
    )


def collect_security(ctx: Ctx) -> SecuritySection:
    """Orchestrated security policy, one summary per segment pair read."""
    notes: list[str] = []
    segments: tuple[str, ...] = ()
    from pyecsdwan.resources import zones as zones_res

    try:
        segment_map = zones_res.segment_zone_map(ctx)
        segments = tuple(sorted(str(s) for s in segment_map))
    except Exception as exc:  # noqa: BLE001 - fall back to the default segment
        notes.append(f"segment map unreadable ({_reason(exc)}); assuming the default segment")
        log.debug("fabric_segment_map_failed", error=_reason(exc))
    if not segments:
        # Segment 0 is the default segment and exists on every fabric, so this
        # is a floor rather than a guess — it just may not be the whole story.
        segments = ("0",)

    pairs, bound_note = _segment_pairs(segments)
    if bound_note:
        notes.append(bound_note)

    from pyecsdwan.resources import security_policy as security_policy_res

    normalizer = security_policy_res.SecurityPolicy()
    policies: list[PolicyMap] = []
    for pair in pairs:
        try:
            raw = ctx.client.get(SECURITY_POLICY_PATH, params={"map": pair})
        except Exception as exc:  # noqa: BLE001 - one pair failing is one row
            log.debug("fabric_security_policy_failed", pair=pair, error=_reason(exc))
            policies.append(PolicyMap(pair=pair, error=_reason(exc)))
            continue
        # A 204 (no policy for this pair) reaches here as None, which is a real
        # answer — "not configured", not "could not be read".
        policies.append(_summarize_policy(pair, normalizer.normalize(raw)))
    return SecuritySection(notes=tuple(notes), segments=segments, policies=tuple(policies))


def _named(value: Any, table: Mapping[str, str]) -> str:
    """A coded enum field as a name, an unknown code as itself, absence as
    ``unspecified``.

    Passing an unrecognized code through unchanged is deliberate: a role or
    state this Orchestrator version added is still worth counting, and
    flattening it into "unspecified" would hide a real distinction.
    """
    if value is None or value == "":
        return UNSPECIFIED
    return table.get(str(value), str(value))


def collect_inventory(
    inventory: Sequence[Mapping[str, Any]], *, inventory_error: str = ""
) -> InventorySection:
    """Appliance counts by role, site, model and state."""
    notes = (f"appliance inventory unreadable: {inventory_error}",) if inventory_error else ()
    rows: list[ApplianceRow] = []
    for entry in inventory:
        ne_pk = str(entry.get("nePk") or entry.get("id") or "")
        rows.append(
            ApplianceRow(
                ne_pk=ne_pk,
                hostname=str(entry.get("hostName") or ne_pk or UNSPECIFIED),
                site=str(entry.get("site") or UNSPECIFIED),
                model=str(entry.get("model") or UNSPECIFIED),
                role=_named(entry.get("networkRole"), _ROLE_NAMES),
                state=_named(entry.get("state"), _STATE_NAMES),
            )
        )
    rows.sort(key=lambda r: r.hostname)
    return InventorySection(notes=notes, appliances=tuple(rows))


def appliance_deployment(ctx: Ctx, ne_pk: str) -> dict[str, Any]:
    """One appliance's deployment object, over the proxy path the resource uses."""
    raw = ctx.client.appliance_request("GET", ne_pk, DEPLOYMENT_ECOS_PATH)
    return raw if isinstance(raw, dict) else {}


def _deployment_row(ne_pk: str, hostname: str, raw: Mapping[str, Any]) -> ApplianceDeployment:
    sys_config = raw.get("sysConfig")
    sys_config = sys_config if isinstance(sys_config, dict) else {}
    labels = sys_config.get("ifLabels")
    labels = labels if isinstance(labels, dict) else {}

    def _labels(side: str) -> tuple[str, ...]:
        entries = labels.get(side)
        return tuple(str(e) for e in entries) if isinstance(entries, list) else ()

    mode_ifs = raw.get("modeIfs")
    mode_ifs = mode_ifs if isinstance(mode_ifs, list) else []
    addresses = sum(
        len(i["applianceIPs"])
        for i in mode_ifs
        if isinstance(i, dict) and isinstance(i.get("applianceIPs"), list)
    )
    mode = sys_config.get("mode")
    return ApplianceDeployment(
        ne_pk=ne_pk,
        hostname=hostname,
        mode=str(mode) if mode else UNSPECIFIED,
        license=str(sys_config.get("license") or ""),
        wan_labels=_labels("wan"),
        lan_labels=_labels("lan"),
        interfaces=len(mode_ifs),
        addresses=addresses,
    )


def collect_deployment(
    ctx: Ctx,
    inventory: Sequence[Mapping[str, Any]],
    *,
    inventory_error: str = "",
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
) -> DeploymentSection:
    """Per-appliance deployment mode and interface labels — the one fan-out.

    Bounded, failure-isolating and input-ordered: an appliance that does not
    answer is a marked row carrying the reason, never an exception and never a
    silently missing appliance.
    """
    notes = (f"appliance inventory unreadable: {inventory_error}",) if inventory_error else ()
    targets = [
        (str(e.get("nePk") or e.get("id")), str(e.get("hostName") or e.get("nePk") or e.get("id")))
        for e in inventory
        if e.get("nePk") or e.get("id")
    ]
    outcomes = fan_out(
        targets,
        lambda target: appliance_deployment(ctx, target[0]),
        concurrency=concurrency,
        timeout=timeout,
    )
    rows = tuple(
        _deployment_row(outcome.item[0], outcome.item[1], outcome.value or {})
        if outcome.done
        else ApplianceDeployment(
            ne_pk=outcome.item[0], hostname=outcome.item[1], error=outcome.error
        )
        for outcome in outcomes
    )
    return DeploymentSection(notes=notes, appliances=rows)


def collect(
    ctx: Ctx,
    *,
    section: str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
) -> FabricConfig:
    """Build the fabric configuration breakdown.

    *section* scopes the report to one section (see :data:`SECTIONS`); an
    unknown name raises :class:`UnknownSection` naming the valid ones. Only
    the sections asked for are collected, so ``--section overlays`` costs two
    GETs and no fan-out.

    Never raises for a fabric problem: every section degrades to itself,
    flagged, carrying whatever it could read.
    """
    requested = resolve_sections(section)

    # The inventory is read once and shared: four of the five sections want it,
    # and re-reading it per section would multiply the cost of the cheapest
    # part of the report.
    inventory: list[dict[str, Any]] = []
    inventory_error = ""
    if requested != (SECURITY,):
        inventory, inventory_error = _inventory(ctx)

    sections: list[FabricSection] = []
    for name in SECTIONS:
        if name not in requested:
            continue
        if name == OVERLAYS:
            sections.append(collect_overlays(ctx, inventory, inventory_error=inventory_error))
        elif name == TEMPLATES:
            sections.append(collect_templates(ctx, inventory, inventory_error=inventory_error))
        elif name == SECURITY:
            sections.append(collect_security(ctx))
        elif name == INVENTORY:
            sections.append(collect_inventory(inventory, inventory_error=inventory_error))
        elif name == DEPLOYMENT:
            sections.append(
                collect_deployment(
                    ctx,
                    inventory,
                    inventory_error=inventory_error,
                    concurrency=concurrency,
                    timeout=timeout,
                )
            )

    report = FabricConfig(sections=tuple(sections), requested=requested)
    log.debug(
        "fabric_config_report",
        sections=[s.name for s in report.sections],
        appliances=len(inventory),
        degraded=[s.name for s in report.degraded],
    )
    return report


# -- machine-readable output ---------------------------------------------------


def _section_payload(section: FabricSection) -> dict[str, Any]:
    """One section as JSON. Counts are emitted as maps, members as lists."""
    body: dict[str, Any] = {"notes": list(section.notes), "degraded": section.degraded}
    if isinstance(section, OverlaySection):
        body["count"] = len(section.overlays)
        body["overlays"] = [
            {
                "id": overlay.overlay_id,
                "name": overlay.name,
                "topology": overlay.topology,
                "member_count": overlay.member_count,
                "members": list(overlay.members),
                "nePks": list(overlay.member_ne_pks),
            }
            for overlay in section.overlays
        ]
        body["unassociated_appliances"] = list(section.unassociated)
    elif isinstance(section, TemplateSection):
        body["count"] = len(section.groups)
        body["groups"] = [
            {
                "name": group.name,
                "sections": list(group.sections),
                "section_count": len(group.sections),
                "applied_to": list(group.applied_to),
                "applied_count": group.applied_count,
            }
            for group in section.groups
        ]
        body["unassigned_appliances"] = list(section.unassigned)
    elif isinstance(section, SecuritySection):
        body["segments"] = list(section.segments)
        body["total_rules"] = section.total_rules
        body["policies"] = [
            {
                "map": policy.pair,
                "present": policy.present,
                "rule_count": policy.rule_count,
                "zone_pairs": policy.zone_pairs,
                "maps": list(policy.maps),
                "actions": dict(policy.actions),
                "error": policy.error,
            }
            for policy in section.policies
        ]
    elif isinstance(section, InventorySection):
        body["count"] = section.total
        body["by_role"] = dict(section.by_role)
        body["by_site"] = dict(section.by_site)
        body["by_model"] = dict(section.by_model)
        body["by_state"] = dict(section.by_state)
        body["appliances"] = [dataclasses.asdict(a) for a in section.appliances]
    elif isinstance(section, DeploymentSection):
        body["count"] = len(section.appliances)
        body["by_mode"] = dict(section.by_mode)
        body["unreachable"] = [a.ne_pk for a in section.unreachable]
        body["appliances"] = [
            {
                "nePk": appliance.ne_pk,
                "hostname": appliance.hostname,
                "mode": appliance.mode,
                "license": appliance.license,
                "wan_labels": list(appliance.wan_labels),
                "lan_labels": list(appliance.lan_labels),
                "interfaces": appliance.interfaces,
                "addresses": appliance.addresses,
                "unreachable": appliance.unreachable,
                "error": appliance.error,
            }
            for appliance in section.appliances
        ]
    return body


def to_payload(report: FabricConfig) -> dict[str, Any]:
    """``--json`` body: the structured tree, keyed by section name.

    Sections the run did not collect are absent rather than empty, so a
    consumer can tell "scoped out" from "nothing there" — ``requested`` names
    what was asked for, and ``degraded`` names the sections whose data is
    partial.
    """
    return {
        "requested": list(report.requested),
        "degraded": [s.name for s in report.degraded],
        "sections": {s.name: _section_payload(s) for s in report.sections},
    }


__all__ = [
    "DEPLOYMENT",
    "INVENTORY",
    "MAX_POLICY_READS",
    "OVERLAYS",
    "SECTIONS",
    "SECURITY",
    "TEMPLATES",
    "UNSPECIFIED",
    "ApplianceDeployment",
    "ApplianceRow",
    "DeploymentSection",
    "FabricConfig",
    "FabricSection",
    "InventorySection",
    "Overlay",
    "OverlaySection",
    "PolicyMap",
    "Section",
    "SecuritySection",
    "TemplateGroupInfo",
    "TemplateSection",
    "UnknownSection",
    "collect",
    "collect_deployment",
    "collect_inventory",
    "collect_overlays",
    "collect_security",
    "collect_templates",
    "resolve_sections",
    "to_payload",
]
