"""Quarantined legacy MCP server (issue #62). Not part of the product.

Importing :mod:`.server` refuses unless ``ECSDWAN_MCP_LEGACY_ENABLE=1``. The
trust-boundary rules live in :mod:`.policy`, which has no ``mcp`` or
``pyedgeconnect`` dependency so it can be tested in the normal test run.
"""
