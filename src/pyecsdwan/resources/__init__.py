"""Built-in resource plugins. Importing this package registers them all."""

from pyecsdwan.resources import (
    bgp,
    deployment,
    dhcp,
    interface_labels,
    loopback,
    ospf,
    overlays,
    routes,
    security_policy,
    templates,
    vrrp,
    zones,
)

__all__ = [
    "bgp",
    "deployment",
    "dhcp",
    "interface_labels",
    "loopback",
    "ospf",
    "overlays",
    "routes",
    "security_policy",
    "templates",
    "vrrp",
    "zones",
]
