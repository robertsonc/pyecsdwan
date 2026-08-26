"""Built-in resource plugins. Importing this package registers them all."""

from pyecsdwan.resources import (
    bgp,
    deployment,
    interface_labels,
    overlays,
    routes,
    security_policy,
    templates,
    zones,
)

__all__ = [
    "bgp",
    "deployment",
    "interface_labels",
    "overlays",
    "routes",
    "security_policy",
    "templates",
    "zones",
]
