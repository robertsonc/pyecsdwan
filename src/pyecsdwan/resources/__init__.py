"""Built-in resource plugins. Importing this package registers them all."""

from pyecsdwan.resources import interface_labels, overlays, security_policy, templates, zones  # noqa: I001
from pyecsdwan.resources import deployment

__all__ = ["interface_labels", "overlays", "security_policy", "templates", "zones"]
__all__ += ["deployment"]
