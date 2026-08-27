"""Template-ownership detection.

The Orchestrator exposes no per-section "managed-by" field (see
docs/research/appliance-config.md). Ownership is derived as a join:

    appliance's associated template groups (GET /template/applianceAssociation?nePk=)
        x  each group's selected template sections (GET /template/templateSelection?templateGroup=)
        x  a static map of resource kind -> template section name(s)

If any associated group selects a section that covers a resource kind, direct
appliance-level writes to that kind are template-owned: the next template push
would silently revert them. This is the single most common real-world footgun,
so appliance-scope plugins call :func:`owning_group` from ``managed_by()``.
"""

from __future__ import annotations

import structlog

from pyecsdwan.client import OrchApiError
from pyecsdwan.contract import Ctx

log = structlog.get_logger("pyecsdwan.ownership")

#: Resource kind -> template section names that manage it. Section names are
#: the template names the Orchestrator UI shows (Default Template Group has
#: the full list). Extend as appliance-scope plugins are added (Phase 2).
KIND_TO_TEMPLATE_SECTIONS: dict[str, tuple[str, ...]] = {
    "appliance/security-policy": ("securityMaps",),
    "appliance/bgp": ("bgp",),
    "appliance/ospf": ("ospf",),
    "appliance/dns": ("dns",),
    "appliance/vrrp": ("vrrp",),
    "appliance/routes": ("subnets", "routes"),
    "appliance/dhcp": ("dhcpd", "dhcpFailover"),
    "appliance/shaper": ("shaper",),
    "appliance/nat": ("natMaps",),
    # UNVERIFIED — not confirmed against a live Default Template Group's
    # section list (that group didn't select an obvious interfaces/deployment
    # section this session). "deployment"/"interfaces" are the section-name
    # candidates the Orchestrator UI groups this config under; revisit once a
    # live group with an interfaces template selected is available (#12).
    "appliance/deployment": ("deployment", "interfaces"),
    # Appliance-scope zones + security maps (#19). The resource kind here is
    # deliberately "appliance/security-maps", not the pre-seeded
    # "appliance/security-policy" above (left untouched — some other
    # resource may still claim it) nor the orchestrator-scope
    # "security-policy" kind, to keep all three names unambiguous. Section
    # name CONFIRMED real (matches the ECOS path itself, "securityMaps").
    "appliance/security-maps": ("securityMaps",),
    # UNVERIFIED — no live Default Template Group with a zones-only section
    # selected was available this session; "zones" is the natural section-
    # name candidate (matches the ECOS path), same UNVERIFIED convention as
    # appliance/deployment above.
    "appliance/zones": ("zones",),
    # Policy maps + shapers (#33). "shaper" (pre-seeded above, used as-is by
    # resources/shapers.py's outbound resource) is CONFIRMED real against a
    # live Default Template Group's selected-section list. The four names
    # below are UNVERIFIED: they follow the ECOS-path-matching convention
    # that was confirmed for "securityMaps", but no live group selecting them
    # was available this session. The inbound shaper claims the confirmed
    # "shaper" section too — the Orchestrator's Shaper template covers both
    # directions — so its ownership detection does not rest on the
    # unverified half alone.
    "appliance/qos-map": ("qosMaps",),
    "appliance/optimization-map": ("optimizationMaps",),
    "appliance/route-map": ("routeMaps",),
    "appliance/inbound-shaper": ("shaper", "inboundShapers"),
}


def associated_groups(ctx: Ctx, ne_pk: str) -> list[str]:
    """Template groups currently associated to an appliance."""
    try:
        raw = ctx.client.get("/template/applianceAssociation", params={"nePk": ne_pk})
    except OrchApiError as exc:
        # A missing association record is "no groups", not an error.
        if exc.status_code in (204, 404):
            return []
        raise
    if isinstance(raw, dict):
        ids = raw.get("templateIds", [])
        return [str(g) for g in ids] if isinstance(ids, list) else []
    return []


def selected_sections(ctx: Ctx, template_group: str) -> list[str]:
    """Template section names a group actually has selected."""
    raw = ctx.client.get("/template/templateSelection", params={"templateGroup": template_group})
    if isinstance(raw, list):
        return [str(s) for s in raw]
    return []


def owning_group(ctx: Ctx, kind: str, ne_pk: str) -> str | None:
    """Return ``"template-group <name>"`` when a template section covering
    ``kind`` is selected in a group associated to the appliance, else None."""
    sections = KIND_TO_TEMPLATE_SECTIONS.get(kind)
    if not sections:
        return None
    wanted = {s.lower() for s in sections}
    for group in associated_groups(ctx, ne_pk):
        try:
            selected = selected_sections(ctx, group)
        except OrchApiError as exc:
            log.debug("ownership_selection_unreadable", group=group, error=str(exc))
            continue
        if any(s.lower() in wanted for s in selected):
            return f"template-group {group}"
    return None
