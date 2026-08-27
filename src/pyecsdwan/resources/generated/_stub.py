"""Runtime support shared by every generated Tier-1 plugin stub (issue #27).

Hand-written, and the only hand-written module in this package -- the same
split :mod:`pyecsdwan.generated._base` makes for the models and bindings, and
for the same reason: a policy change here costs one edit instead of a
regeneration sweep over every stub anyone has generated.

What lives here is everything a stub needs that is *not* specific to one
operation:

* :func:`param_values` -- the ref-naming convention. A generated stub has no
  curated notion of what identifies an instance, so the path and required
  query parameters of its endpoint are carried in ``Ref.name``.
* :func:`as_raw` -- reduce whatever a generated binding returns (a pydantic
  model, a mapping, a list, ``None``) to a :data:`~pyecsdwan.contract.RawState`
  suitable for the journal and for replay through the write binding.
* :func:`ne_pk_for` -- the appliance-scope resolution every appliance stub
  repeats.

Nothing here decides *policy* about a resource: reversibility, ownership and
normalization are all declared in the emitted stub itself, where a curator can
see and change them.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any, Literal

from pyecsdwan.contract import Ctx, RawState, Ref
from pyecsdwan.generated._base import dump_body

#: Separator between ``name=value`` pairs in a generated stub's ``Ref.name``.
PARAM_SEPARATOR = ","


@dataclasses.dataclass(frozen=True)
class StubParam:
    """One value a generated stub needs from its ref to address an instance.

    ``wire_name`` is the spec's own spelling (``vtiName``, ``src_vrf``), never
    the binding's Python spelling, so the convention a human types matches the
    API documentation rather than this project's snake_case rewrite of it.
    """

    wire_name: str
    where: Literal["path", "query"]
    description: str = ""


def ref_name_syntax(params: Sequence[StubParam]) -> str:
    """The ``Ref.name`` form a stub accepts, for docs and error messages."""
    if not params:
        return "any label (this endpoint addresses a single instance)"
    if len(params) == 1:
        return f"<{params[0].wire_name}> (or {params[0].wire_name}=<value>)"
    return PARAM_SEPARATOR.join(f"{p.wire_name}=<value>" for p in params)


def param_values(kind: str, ref: Ref, params: Sequence[StubParam]) -> dict[str, str]:
    """Parse ``ref.name`` into the values a generated binding needs.

    Two accepted spellings, because one parameter is by far the common case:

    * a single declared parameter -- the bare name is its value
      (``Ref("generated/...", "vti1")``), or the explicit ``vtiName=vti1``;
    * more than one -- ``name=value`` pairs separated by
      :data:`PARAM_SEPARATOR` (``vrfId=0,IP=10.0.0.1``), in any order.

    Every declared parameter is required: a generated stub cannot know which
    of an endpoint's parameters have a safe default, and guessing one would
    silently address a different instance than the operator named. Unknown
    names are refused for the same reason -- a typo must not be swallowed.
    """
    if not params:
        return {}
    declared = {p.wire_name: p for p in params}
    text = ref.name.strip()
    values: dict[str, str] = {}
    if text and "=" not in text and len(params) == 1:
        values[params[0].wire_name] = text
    else:
        for chunk in text.split(PARAM_SEPARATOR):
            piece = chunk.strip()
            if not piece:
                continue
            name, sep, value = piece.partition("=")
            name = name.strip()
            if not sep:
                raise ValueError(
                    f"{kind}: ref name {ref.name!r} is not in the expected form "
                    f"{ref_name_syntax(params)!r} ({piece!r} has no '=')"
                )
            if name not in declared:
                raise ValueError(
                    f"{kind}: ref name {ref.name!r} sets unknown parameter {name!r}; "
                    f"this endpoint takes {sorted(declared)}"
                )
            values[name] = value.strip()
    missing = [name for name in declared if not values.get(name)]
    if missing:
        raise ValueError(
            f"{kind}: ref name {ref.name!r} is missing {missing}; expected "
            f"{ref_name_syntax(params)!r}"
        )
    return values


def as_bool(text: str) -> bool:
    """Parse a ref-supplied value for a boolean-typed query parameter."""
    lowered = text.strip().lower()
    if lowered in ("true", "1", "yes", "on"):
        return True
    if lowered in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"expected a boolean (true/false), got {text!r}")


def as_raw(value: Any) -> RawState:
    """Reduce a generated binding's return value to a :data:`RawState`.

    Generated GET bindings return a pydantic model (or ``None``); untyped ones
    return whatever the server sent. ``dump_body`` is the same serializer the
    write bindings use, so a snapshot taken here is exactly what replaying it
    through ``apply()``/``rollback()`` would put back on the wire -- including
    the undocumented fields ``extra="allow"`` preserved. Scalars become
    ``None``: the journal's contract is "object or absent", and a bare string
    is not state anything can be restored from.
    """
    data = dump_body(value)
    if isinstance(data, dict):
        return {str(key): item for key, item in data.items()}
    if isinstance(data, list):
        return list(data)
    return None


def ne_pk_for(ctx: Ctx, kind: str, ref: Ref) -> str:
    """Resolve an appliance-scope ref's appliance name to its nePk."""
    if ref.appliance is None:
        raise ValueError(f"{kind} is appliance-scoped; {ref} is missing an appliance")
    return ctx.resolver.ne_pk_for(ref.appliance)
