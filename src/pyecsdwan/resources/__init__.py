"""Built-in resource plugins. Importing this package registers them all."""

from pyecsdwan.resources import (
    appliance_zones,
    bgp,
    deployment,
    dhcp,
    interface_labels,
    loopback,
    ospf,
    overlays,
    regions,
    routes,
    security_policy,
    templates,
    vrrp,
    zones,
)

__all__ = [
    "appliance_zones",
    "bgp",
    "deployment",
    "dhcp",
    "interface_labels",
    "loopback",
    "ospf",
    "overlays",
    "regions",
    "routes",
    "security_policy",
    "templates",
    "vrrp",
    "zones",
]
