"""Built-in resource plugins. Importing this package registers them all.

``generated`` is the Tier-1 half of that: stubs emitted by
``tools/gen_plugin.py`` (issue #27) from a spec operation. They register like
any other kind, show up in ``ec-cli show coverage`` at tier 1, and refuse to
``normalize()`` until a human curates them.
"""

from pyecsdwan.resources import (
    acls,
    appliance_zones,
    bgp,
    common_settings,
    deployment,
    dhcp,
    generated,
    interface_labels,
    internal_subnets,
    loopback,
    nat,
    ospf,
    overlays,
    policy_maps,
    priorities,
    regions,
    routes,
    security_policy,
    shapers,
    templates,
    vrrp,
    zones,
)

__all__ = [
    "acls",
    "appliance_zones",
    "bgp",
    "common_settings",
    "deployment",
    "dhcp",
    "generated",
    "interface_labels",
    "internal_subnets",
    "loopback",
    "nat",
    "ospf",
    "overlays",
    "policy_maps",
    "priorities",
    "regions",
    "routes",
    "security_policy",
    "shapers",
    "templates",
    "vrrp",
    "zones",
]
