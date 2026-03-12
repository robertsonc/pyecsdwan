"""Introspect pyedgeconnect classes to extract method metadata for MCP tools."""

from __future__ import annotations

import inspect
import re
from typing import Any


def _parse_param_description(docstring: str, param_name: str) -> str:
    """Extract a parameter's description from an RST-style docstring."""
    if not docstring:
        return ""
    pattern = rf":param {re.escape(param_name)}:\s*(.*?)(?=\n\s*:|\n\s*\n|\Z)"
    match = re.search(pattern, docstring, re.DOTALL)
    if match:
        desc = match.group(1).strip()
        # Collapse multi-line descriptions into one line
        desc = re.sub(r"\s+", " ", desc)
        return desc
    return ""


def _parse_summary(docstring: str) -> str:
    """Extract the first sentence/paragraph from a docstring as summary."""
    if not docstring:
        return ""
    # Take everything up to the first RST directive or blank line
    lines = []
    for line in docstring.split("\n"):
        stripped = line.strip()
        if stripped.startswith("..") or stripped.startswith(":param"):
            break
        if not stripped and lines:
            break
        if stripped:
            lines.append(stripped)
    return " ".join(lines)


def _python_type_to_json_schema(annotation) -> dict:
    """Convert a Python type annotation to a JSON schema type dict."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return {"type": "string"}

    type_str = str(annotation)

    # Handle common types
    if annotation is str:
        return {"type": "string"}
    elif annotation is int:
        return {"type": "integer"}
    elif annotation is float:
        return {"type": "number"}
    elif annotation is bool:
        return {"type": "boolean"}
    elif annotation is list or "list" in type_str.lower():
        return {"type": "array", "items": {"type": "string"}}
    elif annotation is dict or "dict" in type_str.lower():
        return {"type": "object"}
    else:
        return {"type": "string"}


def get_public_methods(cls) -> list[dict[str, Any]]:
    """Extract all public methods from a class with their metadata.

    Returns a list of dicts with keys:
      - name: method name
      - summary: short description
      - doc: full docstring
      - parameters: list of param dicts with name, type, required, default, description
    """
    methods = []

    for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
        # Skip private/magic methods
        if name.startswith("_"):
            continue

        sig = inspect.signature(method)
        docstring = inspect.getdoc(method) or ""
        summary = _parse_summary(docstring)

        params = []
        for pname, param in sig.parameters.items():
            if pname == "self":
                continue

            desc = _parse_param_description(docstring, pname)
            schema = _python_type_to_json_schema(param.annotation)
            required = param.default is inspect.Parameter.empty

            param_info = {
                "name": pname,
                "schema": schema,
                "required": required,
                "description": desc,
            }
            if not required:
                param_info["default"] = param.default

            params.append(param_info)

        methods.append(
            {
                "name": name,
                "summary": summary,
                "doc": docstring,
                "parameters": params,
            }
        )

    return methods
