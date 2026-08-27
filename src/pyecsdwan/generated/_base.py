"""Runtime support shared by every generated model and binding (issue #26).

Hand-written, unlike the rest of :mod:`pyecsdwan.generated`. Keeping the
pydantic configuration and the request-shaping helpers here means a policy
change costs one edit instead of a regeneration sweep over every emitted file.

Two policies are worth stating explicitly, because both exist to survive live
drift from the vendored spec:

* ``extra="allow"`` -- the Orchestrator returns fields the 7.2.0 baseline does
  not document, and drops documented ones. Unknown keys are kept verbatim on
  the model and round-trip through ``model_dump``.
* ``coerce_numbers_to_str=True`` -- EdgeConnect is inconsistent about whether a
  numeric-looking value is a JSON string or a JSON number (``"topologyType":
  "1"`` in one schema, ``priority: 100`` in the next). Without this, a response
  that disagrees with the spec on that one point fails validation for no useful
  reason. The opposite direction (``"7"`` into an ``int`` field) is already
  pydantic's default lax behaviour.

Bodies are serialized with ``exclude_unset=True``: a generated request model
declares every optional field with a ``None`` default, so "the caller never
touched this" and "the caller wants a literal null" have to stay
distinguishable. Only fields the caller actually set are sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict


class GeneratedModel(BaseModel):
    """Base class for every generated record model.

    ``populate_by_name`` lets callers construct a model with either the Python
    field name or the wire name, which matters because the emitter renames any
    property that is not a safe Python identifier (``self`` -> ``self_``,
    ``1k-blocks`` -> ``field_1k_blocks``) and pins the original as the alias.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        coerce_numbers_to_str=True,
        # Frozen empty: the emitter never renames a `model_*` property, so the
        # v2 protected-namespace warning would only ever be noise here.
        protected_namespaces=(),
    )


def dump_body(body: Any) -> Any:
    """Turn a generated model (or a raw payload) into JSON-ready data.

    Accepts a model, a mapping, a sequence, or ``None`` so a caller can always
    fall back to a hand-built dict when the spec's shape is wrong -- which it
    sometimes is. Anything else is passed through untouched for httpx to
    serialize or reject.
    """
    if body is None:
        return None
    if isinstance(body, BaseModel):
        return body.model_dump(by_alias=True, exclude_unset=True)
    if isinstance(body, Mapping):
        return {str(key): dump_body(value) for key, value in body.items()}
    if isinstance(body, (list, tuple)):
        return [dump_body(item) for item in body]
    return body


def format_path(template: str, values: Mapping[str, Any]) -> str:
    """Substitute ``{name}`` placeholders in a spec path.

    Values are inserted raw rather than percent-encoded: orchestrator paths go
    on to httpx, which encodes the path itself, and appliance paths go to
    ``OrchClient.appliance_request``, whose ECOS path validator rejects the
    ``%`` a pre-encoded value would introduce. Encoding here would therefore
    either double-encode or hard-fail depending on the scope.

    A value carrying ``/`` would silently retarget the request at a different
    endpoint, so it is refused instead.
    """
    out = template
    for name, value in values.items():
        text = str(value)
        if not text:
            raise ValueError(f"path parameter {name!r} must not be empty")
        if "/" in text:
            raise ValueError(f"path parameter {name!r} must not contain '/': {text!r}")
        out = out.replace("{" + name + "}", text)
    return out


def ecos_query(path: str, params: Mapping[str, Any]) -> str:
    """Append query parameters to an ECOS path.

    ``OrchClient.appliance_request`` has no ``params`` argument -- the proxy's
    own ``url`` parameter carries the appliance path *and* its query string, so
    that is where these have to go.

    Values are not percent-encoded, deliberately. ``appliance_request``
    validates the assembled path against a conservative character class and a
    ``%`` would fail it, so encoding here would turn every non-trivial value
    into a confusing rejection. Passing the raw value instead means a value
    that genuinely needs encoding fails with the client's own message naming
    the offending path. ``None`` values are dropped, which is how a generated
    binding says "caller left this optional parameter alone".
    """
    pairs = [(name, value) for name, value in params.items() if value is not None]
    if not pairs:
        return path
    separator = "&" if "?" in path else "?"
    return path + separator + "&".join(f"{name}={value}" for name, value in pairs)
