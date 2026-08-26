"""Runtime bootstrap: build a connected Ctx + populated registry.

Shared by the CLI entrypoints and the detached watchdog (which reconstructs
its world from environment variables after the CLI is gone).
"""

from __future__ import annotations

import httpx

from pyecsdwan import config
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.registry import Registry, default_registry
from pyecsdwan.resolver import Resolver


def bootstrap(
    orch_url: str | None = None,
    insecure: bool | None = None,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
    mock: bool = False,
) -> tuple[Ctx, Registry, config.Settings]:
    settings = config.settings_from_env(orch_url=orch_url, insecure=insecure)
    if mock and not settings.api_key:
        # The bundled mock accepts any non-empty token; supply one so the
        # demo path (and commit-confirm, which requires key auth) works with
        # no real credential configured.
        settings.api_key = "mock"
    config.ensure_dirs()
    client = OrchClient(settings, transport=transport)
    resolver = Resolver(client)
    ctx = Ctx(client=client, resolver=resolver, dry_run=dry_run)
    # Importing the package registers every built-in plugin.
    import pyecsdwan.resources  # noqa: F401

    return ctx, default_registry, settings
