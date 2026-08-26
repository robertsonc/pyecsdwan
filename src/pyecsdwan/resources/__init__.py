"""Built-in resource plugins. Importing this package registers them all."""

from pyecsdwan.resources import interface_labels, overlays, security_policy, templates, zones  # noqa: I001
from pyecsdwan.resources import bgp  # noqa: F401 - appended, kept on its own line/__all__ entry to merge cleanly (#16)

__all__ = ["interface_labels", "overlays", "security_policy", "templates", "zones"]
__all__.append("bgp")
