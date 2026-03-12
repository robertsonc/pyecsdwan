"""
MCP Server for pyedgeconnect — Aruba Orchestrator & EdgeConnect SD-WAN APIs.

Exposes every public method from the Orchestrator and EdgeConnect Python
wrapper classes as MCP tools so they can be invoked by Claude or other
LLM agents.

Connection lifecycle:
  1. Call `orch_connect` or `ec_connect` to create a session.
  2. Call any tool — the server routes the call to the active session.
  3. Call `orch_disconnect` or `ec_disconnect` when done.

Environment variables (optional — credentials can also be passed via tools):
  ORCH_URL          — Orchestrator hostname / IP
  ORCH_API_KEY      — Orchestrator API key (skip login if set)
  ORCH_USER         — Orchestrator username
  ORCH_PASSWORD     — Orchestrator password
  ORCH_AUTH_MODE    — local | radius | tacacs  (default: local)
  ORCH_VERIFY_SSL   — true | false  (default: false)
  EC_URL            — EdgeConnect hostname / IP
  EC_USER           — EdgeConnect username
  EC_PASSWORD       — EdgeConnect password
  EC_VERIFY_SSL     — true | false  (default: false)
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Make the pyedgeconnect package importable regardless of install status.
# ---------------------------------------------------------------------------
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from pyedgeconnect import EdgeConnect, Orchestrator  # noqa: E402

from .introspect import get_public_methods  # noqa: E402

logger = logging.getLogger("pyedgeconnect-mcp")

# ---------------------------------------------------------------------------
# FastMCP application
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "pyedgeconnect",
    instructions=(
        "MCP server that wraps the pyedgeconnect Python library, "
        "providing tools for managing Aruba Orchestrator and "
        "EdgeConnect SD-WAN appliances. "
        "Call orch_connect or ec_connect first to establish a session, "
        "then use any orch_* or ec_* tool. "
        "Use list_orchestrator_tools or list_edgeconnect_tools to discover "
        "available operations. Use tool_help for detailed parameter docs."
    ),
)

# ---------------------------------------------------------------------------
# Session store — holds live Orchestrator / EdgeConnect instances.
# Keyed by a user-chosen session name so multiple connections are possible.
# ---------------------------------------------------------------------------
_orch_sessions: dict[str, Orchestrator] = {}
_ec_sessions: dict[str, EdgeConnect] = {}

_DEFAULT_ORCH = "default"
_DEFAULT_EC = "default"

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _str_to_bool(val: str | bool) -> bool:
    if isinstance(val, bool):
        return val
    return val.lower() in ("true", "1", "yes")


def _get_orch(session: str = _DEFAULT_ORCH) -> Orchestrator:
    if session not in _orch_sessions:
        raise RuntimeError(
            f"No Orchestrator session '{session}'. "
            "Call 'orch_connect' first."
        )
    return _orch_sessions[session]


def _get_ec(session: str = _DEFAULT_EC) -> EdgeConnect:
    if session not in _ec_sessions:
        raise RuntimeError(
            f"No EdgeConnect session '{session}'. "
            "Call 'ec_connect' first."
        )
    return _ec_sessions[session]


def _serialize(obj: Any) -> str:
    """Best-effort JSON serialisation of an API response."""
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return json.dumps(obj)
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, indent=2, default=str)
    return str(obj)


def _coerce_arg(value: Any, param: inspect.Parameter) -> Any:
    """Coerce a JSON-decoded value to the type expected by the method."""
    ann = param.annotation
    if ann is inspect.Parameter.empty:
        return value
    if value is None:
        return None
    try:
        if ann is bool:
            return _str_to_bool(value)
        if ann is int:
            return int(value)
        if ann is float:
            return float(value)
    except (ValueError, TypeError):
        pass
    return value


# ---------------------------------------------------------------------------
# Connection management tools
# ---------------------------------------------------------------------------

@mcp.tool()
def orch_connect(
    url: str = "",
    api_key: str = "",
    user: str = "",
    password: str = "",
    auth_mode: str = "local",
    verify_ssl: bool = False,
    mfacode: str = "",
    session: str = "default",
) -> str:
    """Connect to an Aruba Orchestrator instance.

    Provide either an api_key (no login required) or user+password.
    Falls back to ORCH_* environment variables when parameters are empty.
    Returns a confirmation message on success.
    """
    url = url or os.environ.get("ORCH_URL", "")
    api_key = api_key or os.environ.get("ORCH_API_KEY", "")
    user = user or os.environ.get("ORCH_USER", "")
    password = password or os.environ.get("ORCH_PASSWORD", "")
    auth_mode = auth_mode or os.environ.get("ORCH_AUTH_MODE", "local")
    if not url:
        return "Error: url is required (or set ORCH_URL env var)."

    env_ssl = os.environ.get("ORCH_VERIFY_SSL", "")
    if env_ssl:
        verify_ssl = _str_to_bool(env_ssl)

    orch = Orchestrator(
        url=url,
        api_key=api_key,
        auth_mode=auth_mode,
        verify_ssl=verify_ssl,
    )

    if api_key:
        _orch_sessions[session] = orch
        return (
            f"Connected to Orchestrator at {url} via API key "
            f"(session='{session}')."
        )

    if not user or not password:
        return (
            "Error: user and password are required for login "
            "(or set ORCH_USER / ORCH_PASSWORD env vars)."
        )

    ok = orch.login(user, password, mfacode=mfacode)
    if not ok:
        return f"Login to Orchestrator at {url} failed."

    _orch_sessions[session] = orch
    return (
        f"Logged in to Orchestrator at {url} as '{user}' "
        f"(session='{session}')."
    )


@mcp.tool()
def orch_disconnect(session: str = "default") -> str:
    """Disconnect from an Orchestrator session (logout and remove)."""
    if session not in _orch_sessions:
        return f"No Orchestrator session '{session}' to disconnect."
    try:
        _orch_sessions[session].logout()
    except Exception:
        pass
    del _orch_sessions[session]
    return f"Orchestrator session '{session}' disconnected."


@mcp.tool()
def ec_connect(
    url: str = "",
    user: str = "",
    password: str = "",
    verify_ssl: bool = False,
    session: str = "default",
) -> str:
    """Connect to an Aruba EdgeConnect appliance.

    Falls back to EC_* environment variables when parameters are empty.
    Returns a confirmation message on success.
    """
    url = url or os.environ.get("EC_URL", "")
    user = user or os.environ.get("EC_USER", "")
    password = password or os.environ.get("EC_PASSWORD", "")
    if not url:
        return "Error: url is required (or set EC_URL env var)."

    env_ssl = os.environ.get("EC_VERIFY_SSL", "")
    if env_ssl:
        verify_ssl = _str_to_bool(env_ssl)

    ec = EdgeConnect(url=url, verify_ssl=verify_ssl)

    if not user or not password:
        return (
            "Error: user and password are required "
            "(or set EC_USER / EC_PASSWORD env vars)."
        )

    ok = ec.login(user, password)
    if not ok:
        return f"Login to EdgeConnect at {url} failed."

    _ec_sessions[session] = ec
    return (
        f"Logged in to EdgeConnect at {url} as '{user}' "
        f"(session='{session}')."
    )


@mcp.tool()
def ec_disconnect(session: str = "default") -> str:
    """Disconnect from an EdgeConnect session (logout and remove)."""
    if session not in _ec_sessions:
        return f"No EdgeConnect session '{session}' to disconnect."
    try:
        _ec_sessions[session].logout()
    except Exception:
        pass
    del _ec_sessions[session]
    return f"EdgeConnect session '{session}' disconnected."


@mcp.tool()
def list_sessions() -> str:
    """List all active Orchestrator and EdgeConnect sessions."""
    result = {
        "orchestrator_sessions": list(_orch_sessions.keys()),
        "edgeconnect_sessions": list(_ec_sessions.keys()),
    }
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Discovery / help tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_orchestrator_tools() -> str:
    """List all available Orchestrator API tools with brief descriptions.

    Use this to discover what operations are available before calling them.
    """
    methods = get_public_methods(Orchestrator)
    lines = []
    for m in methods:
        # Skip login/logout since they are managed by connect/disconnect
        if m["name"] in ("login", "logout", "send_mfa"):
            continue
        params = ", ".join(p["name"] for p in m["parameters"])
        lines.append(f"orch_{m['name']}({params}) — {m['summary']}")
    return "\n".join(sorted(lines))


@mcp.tool()
def list_edgeconnect_tools() -> str:
    """List all available EdgeConnect API tools with brief descriptions.

    Use this to discover what operations are available before calling them.
    """
    methods = get_public_methods(EdgeConnect)
    lines = []
    for m in methods:
        if m["name"] in ("login", "logout"):
            continue
        params = ", ".join(p["name"] for p in m["parameters"])
        lines.append(f"ec_{m['name']}({params}) — {m['summary']}")
    return "\n".join(sorted(lines))


@mcp.tool()
def tool_help(tool_name: str) -> str:
    """Get detailed help for a specific tool including parameter docs.

    Provide the tool name with or without the orch_/ec_ prefix.
    """
    name = tool_name
    source_cls = None

    if name.startswith("orch_"):
        name = name[5:]
        source_cls = Orchestrator
    elif name.startswith("ec_"):
        name = name[3:]
        source_cls = EdgeConnect

    classes = [source_cls] if source_cls else [Orchestrator, EdgeConnect]

    for cls in classes:
        for m in get_public_methods(cls):
            if m["name"] == name:
                lines = [f"Tool: {tool_name}", f"Summary: {m['summary']}", ""]
                lines.append("Parameters:")
                for p in m["parameters"]:
                    req = "required" if p["required"] else "optional"
                    default = ""
                    if "default" in p and p["default"] is not None:
                        default = f", default={p['default']!r}"
                    lines.append(
                        f"  {p['name']} ({p['schema']['type']}, {req}{default})"
                        f" — {p['description']}"
                    )
                lines.append("")
                lines.append("Full documentation:")
                lines.append(m["doc"])
                return "\n".join(lines)

    return f"Tool '{tool_name}' not found. Use list_orchestrator_tools or list_edgeconnect_tools."


# ---------------------------------------------------------------------------
# Dynamic tool registration
# ---------------------------------------------------------------------------
# We pre-build wrapper functions for every public method in Orchestrator
# and EdgeConnect so they appear as first-class MCP tools.
# ---------------------------------------------------------------------------

# Methods already registered manually above
_SKIP_ORCH = {"login", "logout", "send_mfa"}
_SKIP_EC = {"login", "logout"}


def _register_class_tools(
    cls,
    prefix: str,
    session_getter,
    skip: set[str],
) -> None:
    """Register all public methods of *cls* as MCP tools with *prefix*."""
    methods_meta = get_public_methods(cls)

    for meta in methods_meta:
        method_name = meta["name"]
        if method_name in skip:
            continue

        # Resolve the actual function object for signature inspection
        method_func = getattr(cls, method_name)
        sig = inspect.signature(method_func)

        # Build the list of parameter names (excluding 'self')
        param_names = [
            p for p in sig.parameters if p != "self"
        ]

        # Build a concise description
        tool_name = f"{prefix}_{method_name}"
        summary = meta["summary"] or method_name.replace("_", " ").title()
        description = summary
        if meta["parameters"]:
            param_docs = []
            for pm in meta["parameters"]:
                req = "required" if pm["required"] else "optional"
                param_docs.append(f'{pm["name"]} ({req}): {pm["description"]}')
            description += "\n\nParameters:\n" + "\n".join(param_docs)

        # Add session parameter note
        description += (
            "\n\nsession (optional): Session name to use "
            "(default: 'default'). Must call "
            f"'{prefix}_connect' first."
        )

        # Capture variables for closure
        _method_name = method_name
        _param_names = param_names
        _sig = sig
        _prefix = prefix

        def make_handler(mn, pn, sg, getter):
            def handler(**kwargs) -> str:
                session_name = kwargs.pop("session", "default")
                instance = getter(session_name)
                method = getattr(instance, mn)

                # Coerce argument types based on signature
                call_kwargs = {}
                for k, v in kwargs.items():
                    if k in sg.parameters:
                        v = _coerce_arg(v, sg.parameters[k])
                    # Skip None values for optional parameters
                    if v is None and k in sg.parameters:
                        param = sg.parameters[k]
                        if param.default is not inspect.Parameter.empty:
                            continue
                    call_kwargs[k] = v

                result = method(**call_kwargs)
                return _serialize(result)

            return handler

        handler = make_handler(_method_name, _param_names, _sig, session_getter)

        # Build the JSON schema for the tool's input parameters
        properties: dict[str, Any] = {}
        required_list: list[str] = []

        for pm in meta["parameters"]:
            prop = dict(pm["schema"])
            if pm["description"]:
                prop["description"] = pm["description"]
            properties[pm["name"]] = prop
            if pm["required"]:
                required_list.append(pm["name"])

        # Add session parameter
        properties["session"] = {
            "type": "string",
            "description": (
                "Session name to use (default: 'default'). "
                f"Must call '{prefix}_connect' first."
            ),
        }

        # Register with FastMCP
        # We need to set the function name and docstring so FastMCP
        # can use them for the tool metadata.
        handler.__name__ = tool_name
        handler.__doc__ = description
        mcp.tool()(handler)


# Register all Orchestrator tools
_register_class_tools(
    cls=Orchestrator,
    prefix="orch",
    session_getter=_get_orch,
    skip=_SKIP_ORCH,
)

# Register all EdgeConnect tools
_register_class_tools(
    cls=EdgeConnect,
    prefix="ec",
    session_getter=_get_ec,
    skip=_SKIP_EC,
)
