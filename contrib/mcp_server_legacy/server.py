"""Quarantined legacy MCP server over the vendored pyedgeconnect SDK (#62).

**This is not part of the pyecsdwan product.** It lives under ``contrib/``,
is not packaged, is disabled unless explicitly enabled, and wraps the vendored
reference SDK rather than ``pyecsdwan.Resource``/``txn``. Nothing it does is
transactional. See ``README.md`` in this directory, and ``policy.py`` for the
trust-boundary rules this module enforces.

What changed when it was quarantined:

* Refuses to start without ``ECSDWAN_MCP_LEGACY_ENABLE=1``.
* **All direct-to-appliance (``ec_*``) tools removed** — not gated, removed.
  #10 defers direct appliance access until an RBAC broker exists.
* TLS verification defaults **on**.
* Credentials are read from the environment/keyring only; no tool takes an
  api key, user, or password as an argument.
* Tools are classified read / write / destructive from the verbs each SDK
  method actually issues. Reads are exposed when enabled; writes and
  destructive operations need ``ECSDWAN_MCP_LEGACY_ALLOW_WRITES=1`` as well,
  and carry an explicit Tier-0 warning in their description.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import os
import sys
from typing import Any

from . import policy
from .policy import Disabled, OpClass

logger = logging.getLogger("pyecsdwan-mcp-legacy")

# Refuse before importing anything that can open a socket. An operator who has
# not opted in should get an explanation, not a running server.
policy.check_enabled()

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from mcp.server.fastmcp import FastMCP  # noqa: E402
from pyedgeconnect import Orchestrator  # noqa: E402

from .introspect import get_public_methods  # noqa: E402

mcp = FastMCP(
    "pyecsdwan-legacy",
    instructions=(
        "QUARANTINED legacy MCP server over the vendored pyedgeconnect SDK. "
        "Not transactional: no plan, no journal, no rollback, no "
        "template-ownership check. Read-only unless writes were explicitly "
        "enabled, and direct-to-appliance access is unavailable. "
        "For anything that needs to be undoable, use the ec-cli CLI instead."
    ),
)

#: One Orchestrator session, built from configured credentials. The old server
#: keyed arbitrary named sessions off credentials supplied per call; there is
#: nothing to key on now, because credentials come from the environment.
_session: Orchestrator | None = None


def _serialize(obj: Any) -> str:
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return json.dumps(obj)
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, indent=2, default=str)
    return str(obj)


def http_verbs_for(func: Any) -> frozenset[str] | None:
    """The HTTP verbs a vendored SDK method actually issues.

    Returns ``None`` when the source cannot be read or parsed — which
    ``policy.classify`` treats as a write, not as a read.
    """
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(inspect.cleandoc(source))
    except SyntaxError:
        return None
    verbs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in ("_get", "_post", "_put", "_delete", "_patch"):
                verbs.add(attr.lstrip("_"))
    return frozenset(verbs)


def _connect() -> Orchestrator:
    """Build the single Orchestrator session from configured credentials."""
    global _session
    if _session is not None:
        return _session
    url, api_key = policy.orchestrator_credentials()
    if not api_key:
        raise Disabled(
            f"No API key configured. Set {policy.API_KEY_ENV} or store one in "
            f"the keyring (service 'pyecsdwan', username = the Orchestrator "
            f"host). Interactive user/password login is not available here: "
            f"it would mean accepting a password as a tool argument (#62)."
        )
    _session = Orchestrator(
        url=url,
        api_key=api_key,
        auth_mode="local",
        verify_ssl=policy.verify_tls(),
    )
    return _session


@mcp.tool()
def orch_status() -> str:
    """Report the configured Orchestrator and this server's exposure policy.

    Names no credential: the URL and whether a key was found, nothing more.
    """
    try:
        url, api_key = policy.orchestrator_credentials()
    except Disabled as exc:
        return f"Not configured: {exc}"
    return json.dumps(
        {
            "orchestrator": url,
            "api_key_configured": bool(api_key),
            "tls_verification": policy.verify_tls(),
            "writes_exposed": policy.writes_allowed(),
            "appliance_tools": policy.appliance_tools_enabled(),
            "transactional": False,
            "note": (
                "Tier 0 surface. Use ec-cli for anything requiring a plan, "
                "journal, ownership check, or rollback."
            ),
        },
        indent=2,
    )


def _register_orchestrator_tools() -> dict[str, int]:
    """Register Orchestrator methods the policy allows. Returns a class tally."""
    tally = {op.value: 0 for op in OpClass}
    skipped = 0
    for meta in get_public_methods(Orchestrator):
        name = meta["name"]
        if name in {"login", "logout", "send_mfa"}:
            # Session lifecycle is ours, not the model's — and login would
            # mean a password as a tool argument.
            continue
        func = getattr(Orchestrator, name)
        op = policy.classify(name, http_verbs_for(func))
        if not policy.is_exposed(op):
            skipped += 1
            continue
        tally[op.value] += 1
        _register_one(name, func, meta, op)
    logger.warning(
        "legacy MCP: exposed %s read, %s write, %s destructive tool(s); %s withheld",
        tally[OpClass.READ.value],
        tally[OpClass.WRITE.value],
        tally[OpClass.DESTRUCTIVE.value],
        skipped,
    )
    return tally


def _register_one(name: str, func: Any, meta: dict[str, Any], op: OpClass) -> None:
    sig = inspect.signature(func)
    description = meta["summary"] or name.replace("_", " ").title()
    if meta["parameters"]:
        description += "\n\nParameters:\n" + "\n".join(
            f'{pm["name"]} ({"required" if pm["required"] else "optional"}): {pm["description"]}'
            for pm in meta["parameters"]
        )
    description += policy.tier0_notice(op)

    def handler(**kwargs: Any) -> str:
        instance = _connect()
        call_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        result = getattr(instance, name)(**call_kwargs)
        _url, api_key = policy.orchestrator_credentials()
        return policy.redact(_serialize(result), api_key)

    handler.__name__ = f"orch_{name}"
    handler.__doc__ = description
    mcp.tool()(handler)


_register_orchestrator_tools()


def main() -> None:
    policy.check_enabled()
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
