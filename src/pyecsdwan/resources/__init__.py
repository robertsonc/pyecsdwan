"""Built-in resource plugins. Importing this package registers them all."""

from pyecsdwan.resources import interface_labels, overlays, security_policy, templates, zones  # noqa: I001

# Appended (not merged into the import above) so parallel per-kind branches
# each add one line here without reordering/reformatting anyone else's line.
from pyecsdwan.resources import routes  # noqa: F401 - re-exported via __all__ below

__all__ = ["interface_labels", "overlays", "security_policy", "templates", "zones"]
__all__.append("routes")
