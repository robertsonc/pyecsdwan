"""Built-in resource plugins. Importing this package registers them all."""

from pyecsdwan.resources import interface_labels, overlays, security_policy, templates

__all__ = ["interface_labels", "overlays", "security_policy", "templates"]
