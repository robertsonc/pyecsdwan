"""Emit pydantic v2 models + a typed client binding for one spec operation (#26).

Tier-1 codegen, first half (docs/plugin-promotion.md). Given a scope, method
and path, this reads the vendored OpenAPI baseline through
:mod:`pyecsdwan.specs` and writes two modules::

    src/pyecsdwan/generated/models/<slug>.py     request/response models
    src/pyecsdwan/generated/bindings/<slug>.py   one function calling OrchClient

Usage::

    python tools/gen_models.py --scope appliance --method POST --path bgp/config/system
    python tools/gen_models.py --scope orchestrator --method GET --path /gms/grNodes --dry-run

``tools/gen_plugin.py`` (#27) imports :func:`generate` rather than shelling
out, so the two tools can never disagree about a field name or a type.

Generation is deliberately per-operation: the baselines hold 1833 operations
and a bulk dump would be 1833 files nobody reads. Output is deterministic —
same spec in, byte-identical file out — so a regeneration diff is a real
spec change, never emitter noise.

Three properties of these specs break a naive generator; all three are handled
here and pinned by tests in ``tests/test_gen_models.py``.

**1. ``self`` is a property name, 158 times over.** It is EdgeConnect's
server-echoed identity field (a key repeating its own parent's key), and it
collides with the ``self`` parameter of every model method. The curated
resources in ``pyecsdwan.resources`` *strip* it, because diffing it produces
phantom drift — but stripping is a resource-layer decision about intent, and
this layer's job is fidelity. So it is **emitted**, as ``self_`` with
``Field(alias="self")``: a model built from a live response and dumped back
out reproduces the wire payload byte-for-byte, which ``security_policy.py``
needs when it re-injects the echoes on POST.

**2. Some ``properties`` blocks are maps, not records.** The vendor documents
a dict keyed by data using the *keys* as example property names: numeric keys
(``{"1": 2, "2": 3}`` for the overlay-priority map, where the key is the
priority) and angle-bracket placeholders (``<nePk>``, ``<segment_id_1>``,
``<a number between 1-65535>``, ~125 distinct spellings). Emitting a field per
key produces nonsense — ``class Foo: field_1: int`` for a priority map. The
rule, :func:`classify_properties`: **a block whose property names are *all*
map keys is a mapping**, emitted as ``dict[str, V]`` (or a
``RootModel[dict[str, V]]`` at the root) with ``V`` unified across the example
values. A map key is a name that is all digits, or starts with ``<``, or ends
with ``>`` — the one-sided forms are needed because the baselines contain
typos (``VrfId1>``, ``<pppoeName>>``). A *mixed* block stays a record and its
map-shaped names are dropped, absorbed by ``extra="allow"``: 32 blocks look
like ``{"<nePk>": ..., "header": ...}``, where only ``header`` is a field.

**3. Pydantic's reserved surface.** Names shadowing a ``BaseModel`` attribute
(``copy``, ``json``, ``schema``, ``dict``, ``validate``, ``construct``, the
``model_*`` family) and Python keywords (``import``, ``from``, ``match``,
``if``) are renamed with a trailing underscore and aliased back. The wire name
is never mangled — it round-trips exactly through the alias.

Exit codes: 0 generated, 1 no such operation, 2 error.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import functools
import hashlib
import json
import keyword
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # runnable without an editable install
    sys.path.insert(0, str(REPO_ROOT / "src"))

from pyecsdwan import specs  # noqa: E402 - after the sys.path bootstrap above

DEFAULT_OUT_DIR = REPO_ROOT / "src" / "pyecsdwan" / "generated"
#: Package the emitted modules import from; must match ``DEFAULT_OUT_DIR``.
GENERATED_PACKAGE = "pyecsdwan.generated"
LINE_LENGTH = 100

#: ``dir(BaseModel)`` public surface, frozen rather than computed so the same
#: spec keeps emitting the same bytes across a pydantic upgrade. Revisit when
#: pydantic adds a public ``BaseModel`` attribute that collides with a real
#: EdgeConnect field name; the alias makes the fix invisible to callers.
PYDANTIC_ATTRIBUTES = frozenset(
    {
        "construct",
        "copy",
        "dict",
        "from_orm",
        "json",
        "model_computed_fields",
        "model_config",
        "model_construct",
        "model_copy",
        "model_dump",
        "model_dump_json",
        "model_extra",
        "model_fields",
        "model_fields_set",
        "model_json_schema",
        "model_parametrized_name",
        "model_post_init",
        "model_rebuild",
        "model_validate",
        "model_validate_json",
        "model_validate_strings",
        "parse_file",
        "parse_obj",
        "parse_raw",
        "schema",
        "schema_json",
        "update_forward_refs",
        "validate",
    }
)
#: Names the binding signature already occupies; a query parameter that lands
#: on one of these is suffixed instead of silently shadowing it.
BINDING_RESERVED = frozenset({"client", "body", "params", "expected", "ne_pk", "self"})

_SCALARS: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
}
_SCALAR_TYPES = frozenset(_SCALARS.values())

_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")
_NON_WORD = re.compile(r"[^0-9A-Za-z]+")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

JsonDict = dict[str, Any]
#: Where a generated model's shape came from; see :func:`body_schema`.
BodySource = Literal["spec", "spec-example", "postman-example", "none"]


class GenError(Exception):
    """Fatal condition reported to the operator; exits with status 2."""


# ---------------------------------------------------------------------------
# Naming


def snake_case(name: str) -> str:
    """``showAppliances`` -> ``show_appliances``, ``1k-blocks`` -> ``1k_blocks``."""
    text = _NON_WORD.sub("_", name)
    text = _CAMEL_BOUNDARY_1.sub(r"\1_\2", text)
    text = _CAMEL_BOUNDARY_2.sub(r"\1_\2", text)
    return re.sub(r"_+", "_", text).strip("_").lower()


def camel_case(name: str) -> str:
    """``appliance_post_bgp_config_system`` -> ``AppliancePostBgpConfigSystem``."""
    return "".join(part[:1].upper() + part[1:] for part in snake_case(name).split("_") if part)


def python_field_name(wire_name: str) -> str:
    """Safe Python identifier for a wire property name.

    Trailing underscore for anything reserved (trap 3); ``field_`` prefix when
    the wire name starts with a digit or sanitizes to nothing. The wire name is
    never altered — it comes back verbatim as the pydantic alias.
    """
    base = snake_case(wire_name)
    if not base or base[0].isdigit():
        base = f"field_{base}" if base else "field"
    if keyword.iskeyword(base) or keyword.issoftkeyword(base) or base in PYDANTIC_ATTRIBUTES:
        base = f"{base}_"
    if base == "self":  # trap 1: collides with every model method's first parameter
        base = "self_"
    return base


#: Module-level symbols a generated model module may import; a nested class may
#: never take one of these names or it would shadow the import it is annotated
#: with (``class Field(GeneratedModel)`` really did happen, from a property
#: literally named ``"/"``).
MODULE_SYMBOLS = frozenset({"Any", "Field", "RootModel", "GeneratedModel", "annotations"})


def python_class_name(hint: str) -> str:
    """Safe Python class name for a schema node.

    ``camel_case`` alone is not enough: property names like ``"/"`` collapse to
    nothing and ``"1.NE"`` starts with a digit, and both then emit code that
    does not parse.
    """
    name = camel_case(hint)
    if not name or name[0].isdigit():
        name = f"Node{name}"
    if keyword.iskeyword(name) or keyword.issoftkeyword(name):
        name = f"{name}Node"
    return name


def _dedupe(name: str, taken: set[str]) -> str:
    """First writer keeps the name; later collisions get ``_2``, ``_3``, ..."""
    if name not in taken:
        taken.add(name)
        return name
    for suffix in range(2, 1000):
        candidate = f"{name}_{suffix}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise GenError(f"could not disambiguate name {name!r}")


#: Longest a generated root model name may be before the emitter falls back to
#: a shorter spelling. 60 keeps ``class <name>(GeneratedModel):`` and
#: ``return <name>.model_validate(raw)`` inside the 100-column line budget.
MODEL_NAME_BUDGET = 60


def _path_segments(endpoint: specs.Endpoint) -> list[str]:
    """Literal path segments, parameters dropped (``{nePk}`` names nothing useful)."""
    return [
        segment
        for segment in endpoint.path.strip("/").split("/")
        if segment and not re.fullmatch(r"\{.+\}", segment)
    ]


def root_model_name(endpoint: specs.Endpoint, suffix: str) -> str:
    """Name for a request/response root model: the most specific spelling that fits.

    Preference runs from fully qualified (scope + method + whole path, unique
    across the entire universe) down to the bare suffix, taking the first
    candidate inside :data:`MODEL_NAME_BUDGET`. Dropping the scope/method
    prefix before dropping path segments is deliberate -- the tail of a path is
    what tells a reader which operation they are looking at, and the module the
    class lives in already carries the scope and method.
    """
    segments = _path_segments(endpoint) or ["operation"]
    candidates = [
        [endpoint.scope, endpoint.method, *segments],
        [endpoint.method, *segments],
        segments,
        segments[-2:],
        segments[-1:],
        [],
    ]
    for parts in candidates:
        name = camel_case("_".join(parts)) + suffix
        if len(name) <= MODEL_NAME_BUDGET:
            return name
    return camel_case("_".join(segments[-1:])) + suffix


def child_hint(endpoint: specs.Endpoint, suffix: str) -> str:
    """Short base name for the classes hanging directly off a root container.

    A ``RootModel[list[X]]`` needs a name for ``X``, and deriving it from the
    root's own (up to 60-character) name pushed ``class`` headers past 100
    columns. The last literal path segment plus the role is enough to read.
    """
    segments = _path_segments(endpoint) or ["operation"]
    return camel_case(segments[-1]) + suffix


def slug_for(endpoint: specs.Endpoint) -> str:
    """Module/function name for one operation, e.g. ``appliance_get_acls_by_id``.

    Path parameters become ``by_<name>`` segments so ``/acls/{id}`` and a
    hypothetical literal ``/acls/id`` cannot collide.
    """
    parts = [endpoint.scope, endpoint.method.lower()]
    for segment in endpoint.path.strip("/").split("/"):
        if not segment:
            continue
        match = re.fullmatch(r"\{(.+)\}", segment)
        parts.append(f"by_{snake_case(match.group(1))}" if match else snake_case(segment))
    return "_".join(part for part in parts if part)


@functools.lru_cache(maxsize=1)
def _ambiguous_slugs() -> frozenset[str]:
    """Natural slugs claimed by more than one operation in the current baselines.

    Seven pairs collide today, all because two spec paths differ only in a
    boundary ``snake_case`` erases: ``/gms/statsCollection`` vs
    ``/gms/stats/collection``, ``/natMaps`` vs ``/nat/maps``, and a vendor typo
    that ships ``/linkIntegrityTest/run`` twice, once with a trailing space.
    """
    counts = collections.Counter(slug_for(e) for e in specs.endpoint_index().values())
    return frozenset(slug for slug, count in counts.items() if count > 1)


#: Longest module slug before a digest replaces the tail. 55 keeps
#: ``from pyecsdwan.generated.models.<slug> import (`` inside 100 columns.
SLUG_BUDGET = 55


def _shorten(name: str, budget: int, seed: str) -> str:
    """Trim *name* to *budget* at a ``_`` boundary, tagged with a digest of *seed*.

    Only reached by the handful of orchestrator operations whose paths run to a
    98-character slug. The digest keeps the result injective and depends only
    on the endpoint key, so shortening never costs determinism.
    """
    if len(name) <= budget:
        return name
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6]
    head = name[: budget - len(digest) - 1].rstrip("_")
    if "_" in head:
        head = head.rsplit("_", 1)[0]
    return f"{head}_{digest}"


def unique_slug_for(endpoint: specs.Endpoint) -> str:
    """:func:`slug_for`, with a stable discriminator when two operations collide.

    The suffix is a digest of the endpoint key, so it depends only on the
    vendored baseline: the same spec always produces the same module name, and
    every member of a colliding group is suffixed rather than one arbitrarily
    keeping the clean name.
    """
    slug = slug_for(endpoint)
    if slug in _ambiguous_slugs():
        digest = hashlib.sha256(endpoint.key.encode("utf-8")).hexdigest()[:6]
        slug = f"{slug}_{digest}"
    return _shorten(slug, SLUG_BUDGET, endpoint.key)


def clear_caches() -> None:
    """Drop cached spec-derived state -- for tests that repoint ``$ECSDWAN_SPECS_DIR``."""
    specs.clear_caches()
    _ambiguous_slugs.cache_clear()


# ---------------------------------------------------------------------------
# Trap 2: map-shaped `properties` blocks


#: Appliance primary key, e.g. ``3.NE`` -- the exact shape
#: ``pyecsdwan.client.validate_ne_pk`` enforces.
_NE_PK_KEY = re.compile(r"^\d{1,10}\.\w{1,10}$")
#: Dotted-quad IPv4 literal, e.g. ``10.0.1.1``.
_IPV4_KEY = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def is_map_key(name: str) -> bool:
    """Is this ``properties`` key documenting *data* rather than naming a field?

    Four families, all from the vendor writing an example *key* where a
    property name belongs:

    * all digits -- ``"1"``, ``"65536"`` (the overlay-priority map is
      ``{priority: overlayId}``, so the key is the priority);
    * angle-bracket placeholders -- ``"<nePk>"``, ``"<segment_id_1>"``,
      ``"<a number between 1-65535>"``. The bracket test is one-sided because
      the baselines are not consistently balanced: ``VrfId1>`` and
      ``<pppoeName>>`` both occur and both mean the same thing;
    * appliance primary keys -- ``"1.NE"``, ``"2.NE"``. ``GET /tunnels2/bonded``
      documents its response as ``{"1.NE": {...}, "2.NE": {...}}``;
    * IPv4 literals -- ``"10.0.1.1"``, from ``GET /gms``.

    The last two are an evidence-based extension of the digits/brackets rule
    the issue proposed: without them ``GET /gms`` emits ``class 10011`` and
    ``GET /tunnels2/bonded`` emits ``class 1Ne``, which are not even valid
    Python. No EdgeConnect field is *named* after an appliance or an address.
    """
    if not name:
        return False
    return bool(
        name.isdigit()
        or name.startswith("<")
        or name.endswith(">")
        or _NE_PK_KEY.match(name)
        or _IPV4_KEY.match(name)
    )


def classify_properties(properties: dict[str, Any]) -> Literal["record", "map"]:
    """``"map"`` when *every* property name is a map key, else ``"record"``.

    All-or-nothing on purpose. A block mixing the two (``{"<nePk>": ...,
    "header": ...}``) really does have one real field, and calling the whole
    block a map would throw that field's type away.
    """
    if properties and all(is_map_key(name) for name in properties):
        return "map"
    return "record"


def unify_map_values(schemas: Sequence[Any]) -> Any:
    """Collapse a map's example value schemas into the single schema for ``V``.

    Identical values (the common case — the vendor lists ``<VrfId0>`` and
    ``VrfId1>`` with the same shape) collapse to one. Differing *object* values
    are merged property-wise, first spelling winning, so a map documented with
    two partially-filled examples still types. Anything else gives up and
    yields ``{}``, which the emitter renders as ``Any``.
    """
    distinct: list[Any] = []
    seen: set[str] = set()
    for schema in schemas:
        marker = json.dumps(schema, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            distinct.append(schema)
    if not distinct:
        return {}
    if len(distinct) == 1:
        return distinct[0]
    if not all(isinstance(s, dict) and isinstance(s.get("properties"), dict) for s in distinct):
        return {}
    merged: JsonDict = {}
    for schema in distinct:
        for name, sub in schema["properties"].items():
            merged.setdefault(name, sub)
    return {"type": "object", "properties": dict(sorted(merged.items()))}


# ---------------------------------------------------------------------------
# Model emission


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    """One emitted pydantic field."""

    python_name: str
    wire_name: str
    type_expr: str
    required: bool
    description: str = ""

    @property
    def needs_alias(self) -> bool:
        return self.python_name != self.wire_name


@dataclasses.dataclass(frozen=True)
class ClassSpec:
    """One emitted class: a record model, or a ``RootModel`` wrapper."""

    name: str
    kind: Literal["record", "root"]
    docstring: str = ""
    fields: tuple[FieldSpec, ...] = ()
    root_type: str = ""
    notes: tuple[str, ...] = ()


class ModelEmitter:
    """Schema -> Python type expression, accumulating the classes it needs.

    Reusable on its own: ``tools/gen_plugin.py`` (#27) drives this directly to
    learn field names and types without re-deriving them from the spec.
    Classes land in :attr:`classes` in definition order (children before the
    parent that references them), so rendering is a straight iteration.
    """

    def __init__(self) -> None:
        self.classes: list[ClassSpec] = []
        self.uses_any = False
        self.uses_field = False
        self.uses_root_model = False
        self.uses_generated_model = False
        self._names: set[str] = set(MODULE_SYMBOLS)
        self._by_shape: dict[str, str] = {}

    # -- public ------------------------------------------------------------

    def emit_root(
        self, schema: Any, base_name: str, *, child_hint: str, all_optional: bool
    ) -> str | None:
        """Emit a top-level model, returning its class name (``None`` if useless).

        A schema that reduces to a bare scalar gets no model: wrapping ``str``
        in a ``RootModel`` buys a caller nothing the raw value does not already
        give them. Containers (``list[...]``, ``dict[str, ...]``) *are* wrapped,
        because a root model is what makes the element type reachable.
        """
        expr = self.emit(schema, child_hint, all_optional=all_optional, class_name=base_name)
        if expr == "Any" or expr in _SCALAR_TYPES:
            return None
        if _IDENTIFIER.match(expr):
            return expr
        name = _dedupe(python_class_name(base_name), self._names)
        self.uses_root_model = True
        self.classes.append(
            ClassSpec(
                name=name,
                kind="root",
                root_type=expr,
                docstring=_schema_doc(schema),
            )
        )
        return name

    def emit(
        self, schema: Any, name_hint: str, *, all_optional: bool, class_name: str | None = None
    ) -> str:
        """Type expression for *schema*; may append classes as a side effect.

        *class_name* pins the name of a record emitted at this node (the root
        model's own name); children are always named from *name_hint*, which
        stays short so a three-level nesting under a long orchestrator path
        cannot produce a class name nothing can format.
        """
        if not isinstance(schema, dict) or not schema:
            return self._any()
        if "$ref" in schema:
            # specs.resolve_schema stopped at MAX_REF_DEPTH; the shape below
            # this point is genuinely unknown here, so do not invent one.
            return self._any()

        declared = schema.get("type")
        declared = declared if isinstance(declared, str) else None

        if declared == "array" or (declared is None and "items" in schema):
            item = self.emit(schema.get("items"), f"{name_hint}_item", all_optional=all_optional)
            return f"list[{item}]"

        scalar = _SCALARS.get(declared or "")
        if scalar is not None:
            return scalar

        properties = schema.get("properties")
        if isinstance(properties, dict) and properties:
            if classify_properties(properties) == "map":
                value = unify_map_values(list(properties.values()))
                value_type = self.emit(value, f"{name_hint}_value", all_optional=all_optional)
                return f"dict[str, {value_type}]"
            return self._emit_record(
                schema, properties, name_hint, all_optional=all_optional, class_name=class_name
            )

        additional = schema.get("additionalProperties")
        if isinstance(additional, dict) and additional:
            value = self.emit(additional, f"{name_hint}_value", all_optional=all_optional)
            return f"dict[str, {value}]"

        if declared == "object" or additional is not None or properties == {}:
            return f"dict[str, {self._any()}]"
        return self._any()

    # -- internals ---------------------------------------------------------

    def _any(self) -> str:
        self.uses_any = True
        return "Any"

    def _emit_record(
        self,
        schema: JsonDict,
        properties: dict[str, Any],
        name_hint: str,
        *,
        all_optional: bool,
        class_name: str | None = None,
    ) -> str:
        shape = json.dumps(
            {"s": schema, "o": all_optional, "n": class_name}, sort_keys=True, default=str
        )
        cached = self._by_shape.get(shape)
        if cached is not None:
            return cached

        required = schema.get("required")
        required_names = {str(r) for r in required} if isinstance(required, list) else set()
        dropped = sorted(n for n in properties if is_map_key(n))

        fields: list[FieldSpec] = []
        taken: set[str] = set()
        for wire_name in properties:
            if is_map_key(wire_name):
                continue  # mixed block: absorbed by extra="allow" (see module docstring)
            sub = properties[wire_name]
            # Local hint, not a path accumulation: nesting three levels into a
            # long orchestrator path produced 147-character class names, which
            # nothing could format inside a 100-column budget.
            type_expr = self.emit(sub, wire_name, all_optional=all_optional)
            python_name = _dedupe(python_field_name(wire_name), taken)
            is_required = not all_optional and wire_name in required_names
            description = sub.get("description", "") if isinstance(sub, dict) else ""
            enum = sub.get("enum") if isinstance(sub, dict) else None
            if isinstance(enum, list) and enum:
                # Deliberately not a Literal: EdgeConnect ships values the 7.2.0
                # baseline never listed, and a closed enum would reject them.
                listed = ", ".join(json.dumps(v, default=str) for v in enum)
                description = f"{description} (spec values: {listed})".strip()
            fields.append(
                FieldSpec(
                    python_name=python_name,
                    wire_name=wire_name,
                    type_expr=type_expr,
                    required=is_required,
                    description=str(description),
                )
            )
            if python_name != wire_name or not is_required or description:
                self.uses_field = True

        if not fields:
            # Every name was a map key but classify_properties said "record"
            # -- impossible today, kept so a future rule change degrades to a
            # permissive dict instead of an empty model.
            return f"dict[str, {self._any()}]"

        name = _dedupe(python_class_name(class_name or name_hint), self._names)
        self.uses_generated_model = True
        notes = (
            (f'Map-shaped properties dropped (absorbed by extra="allow"): {", ".join(dropped)}',)
            if dropped
            else ()
        )
        self.classes.append(
            ClassSpec(
                name=name,
                kind="record",
                docstring=_schema_doc(schema),
                fields=tuple(fields),
                notes=notes,
            )
        )
        self._by_shape[shape] = name
        return name


def _schema_doc(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    return str(schema.get("description") or schema.get("title") or "").strip()


# ---------------------------------------------------------------------------
# Rendering


#: Typographic characters the vendor's prose uses that ruff flags as ambiguous
#: (RUF001/RUF002). Folded to ASCII on the way out: these are documentation, not
#: payload data, so nothing that has to round-trip is touched.
_ASCII_FOLD = str.maketrans(
    {
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
        "\u00a0": " ",
    }
)
_TRIPLE_QUOTED = re.compile(r'"""(?:.|\n)*?"""')
_DOUBLE_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')


def ascii_fold(text: str) -> str:
    """Normalize typographic punctuation and collapse whitespace to one line."""
    return " ".join(text.translate(_ASCII_FOLD).split())


def _strip_literals(source: str) -> str:
    """Remove docstrings and string literals so import detection sees code only.

    A field description containing the word "Any" must not conjure a
    ``from typing import Any`` that nothing uses -- ruff rejects the result.
    """
    return _DOUBLE_QUOTED.sub('""', _TRIPLE_QUOTED.sub('""', source))


def _raw_literal(text: str) -> str:
    """A Python string literal for *text* exactly as given, quoted the way ruff would.

    ``ruff format`` rewrites ``"say \\"hi\\""`` to ``'say "hi"'`` -- it picks the
    quote character that avoids escapes. Matching that here is what lets the
    emitted file be a fixed point of the formatter, and the spec is full of
    descriptions quoting enum values.
    """
    escaped = json.dumps(text, ensure_ascii=False)
    if '"' in text and "'" not in text:
        return "'" + escaped[1:-1].replace('\\"', '"') + "'"
    return escaped


def _string_literal(text: str) -> str:
    """A Python string literal for *text*, ASCII-folded first."""
    return _raw_literal(ascii_fold(text))


def _wrapped_literal(text: str, indent: int, prefix: int = 0) -> list[str]:
    """Render *text* as one literal, or as parenthesized implicit concatenation.

    ``ruff format`` will not split a long string, and ``E501`` would then fail
    the very code this tool emits, so the wrapping happens here. The wrap width
    is searched rather than computed because ``json.dumps`` escaping inflates a
    chunk unpredictably -- a description full of quoted enum values (``"spec
    values: \"local0\", \"local1\""``) nearly doubles in length once escaped.
    """
    pad = " " * indent
    # Folded once here: ascii_fold collapses runs of whitespace, so folding a
    # chunk after appending its joining space would silently eat that space and
    # concatenate two words together.
    folded = ascii_fold(text)
    single = f"{pad}{_raw_literal(folded)}"
    if len(single) + prefix <= LINE_LENGTH:
        return [single]
    inner = " " * (indent + 4)

    def render(chunks: list[str]) -> list[str]:
        return [
            f"{inner}{_raw_literal(chunk if last else chunk + ' ')}"
            for chunk, last in ((c, i == len(chunks) - 1) for i, c in enumerate(chunks))
        ]

    limit = LINE_LENGTH - len(inner) - 2
    for width in range(limit, 19, -4):
        chunks = textwrap.wrap(folded, width=width, break_long_words=False, break_on_hyphens=False)
        rendered = render(chunks)
        if chunks and all(len(line) <= LINE_LENGTH for line in rendered):
            return [f"{pad}(", *rendered, f"{pad})"]
    # A single unbreakable token longer than the budget: split inside it rather
    # than emit a line ruff will reject.
    rendered = render(textwrap.wrap(folded, width=20, break_long_words=True, break_on_hyphens=True))
    return [f"{pad}(", *rendered, f"{pad})"]


def _docstring(text: str, indent: int, extra: Iterable[str] = ()) -> list[str]:
    pad = " " * indent
    body = [line for line in textwrap.wrap(ascii_fold(text), width=LINE_LENGTH - indent - 6)]
    notes = [
        line
        for note in extra
        for line in textwrap.wrap(ascii_fold(note), width=LINE_LENGTH - indent - 6)
    ]
    if not body and not notes:
        return []
    if len(body) == 1 and not notes:
        return [f'{pad}"""{body[0]}"""']
    lines = [f'{pad}"""{body[0]}' if body else f'{pad}"""']
    lines.extend(f"{pad}{line}" for line in body[1:])
    if notes:
        if body:
            lines.append("")
        lines.extend(f"{pad}{line}" for line in notes)
    lines.append(f'{pad}"""')
    return lines


def _class_header(name: str, base: str) -> list[str]:
    """``class Name(Base):``, split across lines when it does not fit.

    ``ruff format`` collapses the split form whenever it fits and preserves it
    when it does not, so emitting the split only past the budget keeps the
    output a fixed point of the formatter.
    """
    single = f"class {name}({base}):"
    if len(single) <= LINE_LENGTH:
        return [single]
    return [f"class {name}(", f"    {base}", "):"]


def render_class(spec: ClassSpec) -> list[str]:
    """Render one :class:`ClassSpec` as source lines."""
    if spec.kind == "root":
        lines = _class_header(spec.name, f"RootModel[{spec.root_type}]")
        doc = _docstring(spec.docstring or f"Root wrapper for ``{spec.root_type}``.", 4)
        lines.extend(doc or ['    """Root wrapper."""'])
        return lines

    lines = _class_header(spec.name, "GeneratedModel")
    doc = _docstring(spec.docstring, 4, spec.notes)
    lines.extend(doc or ['    """Generated from the spec; see the module docstring."""'])
    lines.append("")
    for field in spec.fields:
        lines.extend(_annotate_noqa(_render_field(field)))
    return lines


def _render_field(field: FieldSpec) -> list[str]:
    """Render one field, wrapping the way ``ruff format`` would.

    Three shapes, chosen by what fits: a bare annotation, a one-line
    ``Field(...)``, and -- when the name and type alone eat the budget -- the
    parenthesized ``= (Field(...))`` form ruff normalizes long assignments to.
    """
    annotation = field.type_expr if field.required else f"{field.type_expr} | None"
    head = f"    {field.python_name}: {annotation}"
    args: list[tuple[str, list[str]]] = []
    if not field.required:
        args.append(("default", ["        None"]))
    if field.needs_alias:
        args.append(("alias", [f"        {_string_literal(field.wire_name)}"]))
    if field.description:
        args.append(("description", _wrapped_literal(field.description, 8, len("description=,"))))

    if not args:
        return [head]
    if len(args) == 1 and args[0][0] == "default":
        return [f"{head} = None"]
    if all(len(value) == 1 for _, value in args):
        flat = ", ".join(f"{key}={value[0].strip()}" for key, value in args)
        one_line = f"{head} = Field({flat})"
        if len(one_line) <= LINE_LENGTH:
            return [one_line]

    body = _render_field_args(args, indent=8)
    if len(f"{head} = Field(") <= LINE_LENGTH:
        return [f"{head} = Field(", *body, "    )"]
    # ruff format's own shape for an assignment whose target already fills the
    # line: the call moves inside parentheses on the following lines.
    return [
        f"{head} = (",
        "        Field(",
        *(f"    {line}" for line in _render_field_args(args, indent=8)),
        "        )",
        "    )",
    ]


def _render_field_args(args: list[tuple[str, list[str]]], *, indent: int) -> list[str]:
    lines: list[str] = []
    pad = " " * indent
    for key, value in args:
        if len(value) == 1:
            lines.append(f"{pad}{key}={value[0].strip()},")
        else:
            lines.append(f"{pad}{key}={value[0].strip()}")
            lines.extend(value[1:-1])
            lines.append(f"{value[-1]},")
    return lines


# ---------------------------------------------------------------------------
# Schema inference (fallback for the 204 writes the specs leave untyped)


def infer_schema(value: Any) -> JsonDict:
    """Derive a minimal JSON-Schema-ish shape from an example payload.

    Used only when an operation has no spec request schema. Objects recurse,
    lists take their first element (the vendor's examples are homogeneous), and
    scalars map to their JSON type. Nothing is marked required — an example is
    evidence of shape, never of obligation.
    """
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {str(k): infer_schema(v) for k, v in sorted(value.items())},
        }
    if isinstance(value, list):
        return {"type": "array", "items": infer_schema(value[0]) if value else {}}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    return {}


def postman_request_schema(endpoint: specs.Endpoint) -> JsonDict | None:
    """Request shape distilled from the Postman collections (issue #51), if any.

    ``specs.payload_example`` returns ``None`` until that file is vendored, so
    this is inert today and lights up on merge without a change here. The
    lookup is defensive about the entry's layout for the same reason: it is
    keyed by :func:`specs.endpoint_key` but its inner keys are #51's to define.
    """
    entry = specs.payload_example(endpoint.scope, endpoint.method, endpoint.path)
    if not isinstance(entry, dict):
        return None
    for key in ("request", "requestBody", "body"):
        candidate = entry.get(key)
        if isinstance(candidate, (dict, list)) and candidate:
            return infer_schema(candidate)
    return None


def body_schema(endpoint: specs.Endpoint) -> tuple[Any, BodySource]:
    """Best available request shape, and where it came from.

    A documented four-rung ladder, because 204 of the 710 write operations in
    the baselines declare no request schema at all:

    1. the spec's own ``requestBody`` schema (541 operations);
    2. an ``example`` the spec ships *without* a schema -- ``POST /poe/config``
       documents its body only by example, and inferring from that is strictly
       better than an untyped passthrough;
    3. a Postman-derived payload example (issue #51, inert until vendored);
    4. nothing, and the binding takes an untyped mapping.
    """
    schema = endpoint.request_schema()
    if isinstance(schema, dict) and schema:
        return schema, "spec"
    examples = spec_examples(endpoint)["request"]
    if examples:
        return infer_schema(examples[0]), "spec-example"
    inferred = postman_request_schema(endpoint)
    if inferred:
        return inferred, "postman-example"
    return None, "none"


def response_schema(endpoint: specs.Endpoint) -> tuple[Any, BodySource]:
    """Best available response shape, and where it came from. Same ladder, no Postman rung."""
    schema = endpoint.response_schema()
    if isinstance(schema, dict) and schema:
        return schema, "spec"
    examples = spec_examples(endpoint)["response"]
    if examples:
        return infer_schema(examples[0]), "spec-example"
    return None, "none"


# ---------------------------------------------------------------------------
# Spec examples (the acceptance evidence: a model must accept them)


def _media_examples(container: Any) -> list[Any]:
    """Every ``example`` hanging off one ``requestBody``/``response`` object.

    Examples live in two places in these baselines and the vendor uses both:
    on the media-type object (``content/application/json/example``) and on the
    schema itself, including schemas reached through a ``$ref``.
    """
    found: list[Any] = []
    if not isinstance(container, dict):
        return found
    for media in (container.get("content") or {}).values():
        if not isinstance(media, dict):
            continue
        if "example" in media:
            found.append(media["example"])
        schema = media.get("schema")
        if isinstance(schema, dict) and "example" in schema:
            found.append(schema["example"])
    schema = container.get("schema")  # Swagger 2 shape
    if isinstance(schema, dict) and "example" in schema:
        found.append(schema["example"])
    return found


def spec_examples(endpoint: specs.Endpoint) -> dict[str, list[Any]]:
    """``{"request": [...], "response": [...]}`` -- the examples the spec ships.

    Deduped and order-stable. Response examples are gathered from the 2xx
    responses only; an error-response example describes a different shape than
    the one the generated response model was built from.
    """
    resolved_request = endpoint.request_schema()
    out: dict[str, list[Any]] = {
        "request": _media_examples(endpoint.operation.get("requestBody")),
        "response": [],
    }
    for param in endpoint.parameters("body"):
        schema = param.get("schema")
        if isinstance(schema, dict) and "example" in schema:
            out["request"].append(schema["example"])
    if isinstance(resolved_request, dict) and "example" in resolved_request:
        out["request"].append(resolved_request["example"])
    responses = endpoint.operation.get("responses")
    if isinstance(responses, dict):
        for code in sorted(responses):
            if str(code).isdigit() and 200 <= int(code) < 300:
                out["response"].extend(_media_examples(responses[code]))
    resolved_response = endpoint.response_schema()
    if isinstance(resolved_response, dict) and "example" in resolved_response:
        out["response"].append(resolved_response["example"])
    for role, values in out.items():
        seen: set[str] = set()
        unique: list[Any] = []
        for value in values:
            marker = json.dumps(value, sort_keys=True, default=str)
            if marker not in seen:
                seen.add(marker)
                unique.append(value)
        out[role] = unique
    return out


# ---------------------------------------------------------------------------
# Operation-level facts


def expected_status(endpoint: specs.Endpoint) -> tuple[int, ...]:
    """Declared 2xx responses, or the client's default for the method."""
    responses = endpoint.operation.get("responses")
    codes: list[int] = []
    if isinstance(responses, dict):
        codes = sorted(
            int(code) for code in responses if str(code).isdigit() and 200 <= int(code) < 300
        )
    if codes:
        return tuple(codes)
    return (200, 204) if endpoint.method in ("GET", "DELETE") else (200, 201, 204)


def body_is_required(endpoint: specs.Endpoint) -> bool:
    body = endpoint.operation.get("requestBody")
    if isinstance(body, dict):
        return bool(body.get("required"))
    return any(bool(p.get("required")) for p in endpoint.parameters("body"))


@dataclasses.dataclass(frozen=True)
class QueryParam:
    """One declared query parameter, ready to render into a signature."""

    python_name: str
    wire_name: str
    type_expr: str
    required: bool
    description: str = ""


def query_params(endpoint: specs.Endpoint) -> tuple[QueryParam, ...]:
    """Declared query parameters, deduped by wire name, in declaration order."""
    out: list[QueryParam] = []
    taken = set(BINDING_RESERVED)
    seen: set[str] = set()
    for param in endpoint.parameters("query"):
        wire = str(param.get("name") or "")
        if not wire or wire in seen:
            continue
        seen.add(wire)
        schema = param.get("schema")
        declared = schema.get("type") if isinstance(schema, dict) else param.get("type")
        type_expr = _SCALARS.get(str(declared), "Any")
        out.append(
            QueryParam(
                python_name=_dedupe(python_field_name(wire), taken),
                wire_name=wire,
                type_expr=type_expr,
                required=bool(param.get("required")),
                description=str(param.get("description") or ""),
            )
        )
    return tuple(out)


def path_params(endpoint: specs.Endpoint, taken: set[str]) -> tuple[tuple[str, str], ...]:
    """``(python_name, wire_name)`` for each ``{placeholder}``, in declaration order."""
    return tuple(
        (_dedupe(python_field_name(name), taken), name) for name in endpoint.path_param_names
    )


# ---------------------------------------------------------------------------
# Module assembly


@dataclasses.dataclass(frozen=True)
class GeneratedOperation:
    """Everything :func:`generate` produces for one operation, before any I/O.

    This is the hand-off surface for ``tools/gen_plugin.py`` (#27): the model
    class names, the binding module path and the callable's name are all here,
    so a stub generator never has to re-parse the spec or guess at a slug.
    """

    endpoint: specs.Endpoint
    slug: str
    function_name: str
    models_module: str
    bindings_module: str
    models_source: str
    bindings_source: str
    models_path: Path
    bindings_path: Path
    request_model: str | None
    response_model: str | None
    #: Where the request shape came from; see :func:`body_schema`.
    body_source: BodySource
    #: Where the response shape came from; see :func:`response_schema`.
    response_source: BodySource
    path_params: tuple[tuple[str, str], ...]
    query_params: tuple[QueryParam, ...]

    @property
    def files(self) -> dict[Path, str]:
        return {self.models_path: self.models_source, self.bindings_path: self.bindings_source}


def _regen_command(endpoint: specs.Endpoint) -> list[str]:
    """The exact invocation that reproduces this file, wrapped to fit the line budget.

    ``shlex.quote`` on the path is not decoration: one appliance path in the
    7.2.0 baseline ends in a space (``/linkIntegrityTest/run ``), and an
    unquoted copy-paste of it would both trip ``W291`` here and silently
    generate the *other* operation when a reader ran it.
    """
    return [
        "python tools/gen_models.py \\",
        f"        --scope {endpoint.scope} --method {endpoint.method} \\",
        f"        --path {shlex.quote(endpoint.path)}",
    ]


def _module_header(endpoint: specs.Endpoint, what: str) -> list[str]:
    baseline = specs.baseline_path(endpoint.scope)
    source = baseline.name if baseline is not None else f"{endpoint.scope}-openapi (not vendored)"
    version = specs.spec_version(endpoint.scope) or "unknown"
    provenance = (
        f"Source: specs/{source} (spec version {version})"
        + (f", operationId ``{endpoint.operation_id}``" if endpoint.operation_id else "")
        + "."
    )
    path_display = ascii_fold(endpoint.path) or endpoint.path.strip()
    headline = f"{what} for ``{endpoint.scope} {endpoint.method} {path_display}``."
    lines = [
        '"""' + headline if len(headline) <= LINE_LENGTH - 3 else '"""',
        *([] if len(headline) <= LINE_LENGTH - 3 else textwrap.wrap(headline, width=LINE_LENGTH)),
        "",
        "Machine-generated by ``tools/gen_models.py`` (issue #26) -- do not edit.",
        "Regenerate with::",
        "",
        *(f"    {line}" for line in _regen_command(endpoint)),
        "",
        *textwrap.wrap(provenance, width=LINE_LENGTH),
    ]
    if endpoint.summary:
        lines.extend(["", *textwrap.wrap(ascii_fold(endpoint.summary), width=LINE_LENGTH)])
    if endpoint.deprecated:
        lines.extend(["", "The spec marks this operation deprecated."])
    lines.append('"""')
    return lines


_NATURAL_CHUNK = re.compile(r"(\d+)")
#: Ruff's ``S108`` (hardcoded temporary directory) fires on these substrings
#: wherever they appear -- including inside a pydantic ``alias``, which is wire
#: data, not a filesystem path. ``appliance GET /diskUsage`` documents a
#: ``"/dev/shm"`` property, so the emitter marks that one line rather than
#: blanket-suppressing a rule across generated files.
_S108_LITERALS = ('"/tmp"', '"/var/tmp"', '"/dev/shm"')


def dunder_all_order(names: Iterable[str]) -> list[str]:
    """Sort ``__all__`` the way ruff's ``RUF022`` wants it.

    Ruff applies an "isort-style" order rather than a plain ``sorted()``:
    SCREAMING_CASE first, then CamelCase, then underscore-prefixed, then
    lowercase, with a digit-aware natural sort inside each group (``Item2``
    before ``Item10``). Emitting it that way keeps the generated file clean
    without a ruff pass having to rewrite it.
    """

    def group(name: str) -> int:
        if name.startswith("_"):
            return 2
        if name.isupper():
            return 0
        if name[:1].isupper():
            return 1
        return 3

    def natural(name: str) -> tuple[tuple[int, str | int], ...]:
        parts = _NATURAL_CHUNK.split(name)
        return tuple((1, int(part)) if part.isdigit() else (0, part) for part in parts if part)

    return sorted(names, key=lambda name: (group(name), natural(name), name))


def _annotate_noqa(lines: list[str]) -> list[str]:
    """Append ``# noqa`` to emitted lines whose *data* trips a lint rule."""
    out: list[str] = []
    for line in lines:
        if any(literal in line for literal in _S108_LITERALS) and "# noqa" not in line:
            line = f"{line}  # noqa: S108"
        out.append(line)
    return out


def _import_block(*groups: Sequence[str]) -> list[str]:
    """Render import groups in isort order, blank-line separated, empties dropped."""
    lines: list[str] = []
    for group in groups:
        if not group:
            continue
        if lines:
            lines.append("")
        lines.extend(group)
    return lines


def _uses(body: str, symbol: str) -> bool:
    return re.search(rf"\b{re.escape(symbol)}\b", _strip_literals(body)) is not None


def render_models_module(endpoint: specs.Endpoint, emitter: ModelEmitter) -> str:
    """Assemble the ``models/<slug>.py`` source from an already-driven emitter.

    Imports are derived from the rendered class bodies rather than from flags
    tripped during emission: a type the emitter considered but discarded (the
    ``Any`` it returns for an absent response schema, say) must not leave an
    unused import behind for ruff to reject.
    """
    body_lines: list[str] = []
    for spec in emitter.classes:
        body_lines += ["", "", *render_class(spec)]
    body = "\n".join(body_lines)

    pydantic_names = [name for name in ("Field", "RootModel") if _uses(body, name)]
    stdlib = ["from typing import Any"] if _uses(body, "Any") else []
    third_party = [f"from pydantic import {', '.join(pydantic_names)}"] if pydantic_names else []
    first_party = (
        [f"from {GENERATED_PACKAGE}._base import GeneratedModel"]
        if _uses(body, "GeneratedModel")
        else []
    )

    lines = _module_header(endpoint, "Pydantic models")
    lines += ["", "from __future__ import annotations"]
    imports = _import_block(stdlib, third_party, first_party)
    lines += ["", *imports] if imports else []
    lines.append("")

    exported = dunder_all_order(spec.name for spec in emitter.classes)
    if not exported:
        lines.append("__all__: list[str] = []")
    else:
        lines.append("__all__ = [")
        lines.extend(f"    {_string_literal(name)}," for name in exported)
        lines.append("]")
    lines += body_lines
    return "\n".join(lines).rstrip("\n") + "\n"


def _body_annotation(request_model: str | None, *, required: bool) -> str:
    parts = [request_model] if request_model else []
    parts += ["Mapping[str, Any]", "list[Any]"]
    if not required:
        parts.append("None")
    return " | ".join(part for part in parts if part)


def _has_body(endpoint: specs.Endpoint, request_model: str | None) -> bool:
    return request_model is not None or endpoint.method in ("POST", "PUT", "PATCH")


def render_bindings_module(
    endpoint: specs.Endpoint,
    *,
    slug: str,
    models_module: str,
    request_model: str | None,
    response_model: str | None,
    body_source: BodySource,
    positional_path_params: tuple[tuple[str, str], ...],
    queries: tuple[QueryParam, ...],
) -> str:
    """Assemble the ``bindings/<slug>.py`` source."""
    is_appliance = endpoint.scope == "appliance"
    has_body = _has_body(endpoint, request_model)
    body_required = has_body and body_is_required(endpoint) and request_model is not None
    path_const = "ECOS_PATH" if is_appliance else "PATH"
    # Trap: ECOS paths are relative -- the proxy's `url` parameter takes them
    # without a leading slash, and normalizing at emit time keeps the intent
    # visible in the generated constant rather than relying on the client.
    path_value = endpoint.path.lstrip("/") if is_appliance else endpoint.path

    body_lines: list[str] = []
    body_lines += _render_signature(
        slug,
        is_appliance=is_appliance,
        path_params=positional_path_params,
        has_body=has_body,
        body_required=body_required,
        queries=queries,
        response_model=response_model,
    )
    body_lines += _render_docstring_for_binding(endpoint, queries, body_source, response_model)
    body_lines += _render_call(
        endpoint,
        is_appliance=is_appliance,
        path_const=path_const,
        path_params=positional_path_params,
        has_body=has_body,
        queries=queries,
        response_model=response_model,
    )
    constants: list[str] = []
    if has_body:
        alias = f"RequestBody = {_body_annotation(request_model, required=True)}"
        constants.append("#: Payload shapes this binding accepts; a raw mapping or list is always")
        constants.append("#: allowed, because the 7.2.0 baseline is sometimes wrong about a shape.")
        constants += (
            [alias]
            if len(alias) <= LINE_LENGTH
            else [
                "RequestBody = (",
                f"    {_body_annotation(request_model, required=True)}",
                ")",
            ]
        )
        constants.append("")
    constants += [
        f"SCOPE = {_string_literal(endpoint.scope)}",
        f"METHOD = {_string_literal(endpoint.method)}",
    ]
    if is_appliance:
        constants.append("#: Relative on purpose: OrchClient.appliance_request passes this as the")
        constants.append("#: proxy's ``url`` parameter, where a leading slash would resolve to")
        constants.append("#: ``rest/json//<path>`` on the appliance.")
    if positional_path_params:
        constants.append("#: ``{placeholder}`` segments are substituted by the binding below.")
    constants += [
        f"{path_const} = {_string_literal(path_value)}",
        f"EXPECTED_STATUS = {expected_status(endpoint)!r}",
        "",
        f"__all__ = [{_string_literal(slug)}]",
        "",
        "",
    ]

    body = "\n".join(constants + body_lines)

    stdlib = []
    if _uses(body, "Mapping"):
        stdlib.append("from collections.abc import Mapping")
    if _uses(body, "Any"):
        stdlib.append("from typing import Any")
    first_party = ["from pyecsdwan.client import OrchClient"]
    helpers = sorted(
        name for name in ("dump_body", "ecos_query", "format_path") if _uses(body, name)
    )
    if helpers:
        first_party.append(f"from {GENERATED_PACKAGE}._base import {', '.join(helpers)}")
    imported_models = sorted({m for m in (request_model, response_model) if m})
    if imported_models:
        single = f"from {models_module} import {imported_models[0]}"
        if len(imported_models) == 1 and len(single) <= LINE_LENGTH:
            first_party.append(single)
        else:
            first_party.append(f"from {models_module} import (")
            first_party.extend(f"    {name}," for name in imported_models)
            first_party.append(")")

    lines = _module_header(endpoint, "Typed client binding")
    lines += ["", "from __future__ import annotations", ""]
    lines += _import_block(stdlib, first_party)
    lines.append("")

    lines += constants
    lines += body_lines
    return "\n".join(lines).rstrip("\n") + "\n"


def _render_signature(
    slug: str,
    *,
    is_appliance: bool,
    path_params: tuple[tuple[str, str], ...],
    has_body: bool,
    body_required: bool,
    queries: tuple[QueryParam, ...],
    response_model: str | None,
) -> list[str]:
    args = ["    client: OrchClient,"]
    if is_appliance:
        args.append("    ne_pk: str,")
    args += [f"    {python_name}: str," for python_name, _ in path_params]
    if has_body:
        args.append(
            "    body: RequestBody," if body_required else "    body: RequestBody | None = None,"
        )
    args.append("    *,")
    for query in queries:
        if query.required:
            args.append(f"    {query.python_name}: {query.type_expr},")
    for query in queries:
        if not query.required:
            args.append(f"    {query.python_name}: {query.type_expr} | None = None,")
    if not is_appliance:
        args.append("    params: dict[str, Any] | None = None,")
    args.append("    expected: tuple[int, ...] = EXPECTED_STATUS,")
    returns = f"{response_model} | None" if response_model else "Any"
    return [f"def {slug}(", *args, f") -> {returns}:"]


def _render_docstring_for_binding(
    endpoint: specs.Endpoint,
    queries: tuple[QueryParam, ...],
    body_source: BodySource,
    response_model: str | None,
) -> list[str]:
    headline = ascii_fold(endpoint.summary) or f"Call {endpoint.method} {endpoint.path.strip()}."
    if not headline.endswith((".", "!", "?", ":")):
        headline += "."
    body: list[str] = textwrap.wrap(headline, width=LINE_LENGTH - 10)
    detail = ascii_fold(endpoint.description)
    if detail and detail != ascii_fold(endpoint.summary):
        body += ["", *textwrap.wrap(detail, width=LINE_LENGTH - 8)[:6]]
    notes: list[str] = []
    if body_source == "spec-example":
        notes.append(
            "The spec declares no request schema for this operation; the request model was "
            "inferred from the example payload the spec ships alongside it."
        )
    elif body_source == "postman-example":
        notes.append(
            "The spec declares no request schema for this operation; the request model was "
            "inferred from the vendor's Postman payload example (issue #51)."
        )
    elif body_source == "none" and _has_body(endpoint, None):
        notes.append(
            "The spec declares no request schema for this write and no payload example "
            "is available, so the body is an untyped passthrough."
        )
    if response_model is None:
        notes.append("The spec gives no usable response schema, so parsed JSON is returned as-is.")
    for query in queries:
        if query.description:
            notes.append(f"``{query.wire_name}``: {query.description}")
    for note in notes:
        body += ["", *textwrap.wrap(ascii_fold(note), width=LINE_LENGTH - 8)]
    collapsed = f'    """{body[0]}"""'
    if len(body) == 1 and len(collapsed) <= LINE_LENGTH:
        # ruff format collapses a one-line docstring onto a single line; emit it
        # that way so the generated file is already a fixed point.
        return [collapsed]
    lines = ['    """' + body[0]]
    lines += [f"    {line}" if line else "" for line in body[1:]]
    lines.append('    """')
    return lines


def _render_call(
    endpoint: specs.Endpoint,
    *,
    is_appliance: bool,
    path_const: str,
    path_params: tuple[tuple[str, str], ...],
    has_body: bool,
    queries: tuple[QueryParam, ...],
    response_model: str | None,
) -> list[str]:
    lines: list[str] = []
    path_expr = path_const
    if path_params:
        mapping = ", ".join(
            f"{_string_literal(wire)}: {python_name}" for python_name, wire in path_params
        )
        lines.append(f"    path = format_path({path_const}, {{{mapping}}})")
        path_expr = "path"

    query_expr = "params"
    if queries:
        initial = "{}" if is_appliance else "dict(params or {})"
        lines.append(f"    query: dict[str, Any] = {initial}")
        for q in queries:
            if q.required:
                lines.append(f"    query[{_string_literal(q.wire_name)}] = {q.python_name}")
            else:
                lines.append(f"    if {q.python_name} is not None:")
                lines.append(f"        query[{_string_literal(q.wire_name)}] = {q.python_name}")
        if is_appliance:
            lines.append(f"    path = ecos_query({path_expr}, query)")
            path_expr = "path"
        else:
            query_expr = "query or None"

    call_args = [f"    {path_expr},"]
    if is_appliance:
        call_args.insert(0, "    ne_pk,")
    call_args.insert(0, "    METHOD,")
    if has_body:
        call_args.append("    json_body=dump_body(body),")
    if not is_appliance:
        call_args.append(f"    params={query_expr},")
    call_args.append("    expected=expected,")

    method_name = "appliance_request" if is_appliance else "request"
    target = "raw = " if response_model else "return "
    one_line = (
        f"    {target}client.{method_name}("
        + ", ".join(arg.strip().rstrip(",") for arg in call_args)
        + ")"
    )
    if len(one_line) <= LINE_LENGTH:
        lines.append(one_line)
    else:
        lines.append(f"    {target}client.{method_name}(")
        lines += [f"    {arg}" for arg in call_args]
        lines.append("    )")
    if response_model:
        lines += [
            "    if raw is None:",
            "        return None",
            f"    return {response_model}.model_validate(raw)",
        ]
    return lines


# ---------------------------------------------------------------------------
# Public entry points


def generate(endpoint: specs.Endpoint) -> GeneratedOperation:
    """Build both modules for one operation, in memory. No I/O, no formatting.

    The reusable half of this tool: ``tools/gen_plugin.py`` (#27) calls this and
    reads :class:`GeneratedOperation` rather than re-deriving types or slugs.
    """
    slug = unique_slug_for(endpoint)
    emitter = ModelEmitter()

    request_schema, body_source = body_schema(endpoint)
    response_shape, response_source = response_schema(endpoint)

    request_model = (
        emitter.emit_root(
            request_schema,
            root_model_name(endpoint, "Request"),
            child_hint=child_hint(endpoint, "Request"),
            all_optional=False,
        )
        if body_source != "none"
        else None
    )
    if request_model is None and body_source != "none":
        # The schema reduced to a scalar or to Any; there is no model to import,
        # so the binding falls back to an untyped body.
        body_source = "none"

    # Responses are all-optional by construction: the Orchestrator omits
    # documented fields freely, and a response model that refuses a real reply
    # is worse than no model at all.
    response_model = emitter.emit_root(
        response_shape,
        root_model_name(endpoint, "Response"),
        child_hint=child_hint(endpoint, "Response"),
        all_optional=True,
    )

    taken = set(BINDING_RESERVED)
    positional = path_params(endpoint, taken)
    queries = query_params(endpoint)

    models_module = f"{GENERATED_PACKAGE}.models.{slug}"
    models_source = render_models_module(endpoint, emitter)
    bindings_source = render_bindings_module(
        endpoint,
        slug=slug,
        models_module=models_module,
        request_model=request_model,
        response_model=response_model,
        body_source=body_source,
        positional_path_params=positional,
        queries=queries,
    )
    return GeneratedOperation(
        endpoint=endpoint,
        slug=slug,
        function_name=slug,
        models_module=models_module,
        bindings_module=f"{GENERATED_PACKAGE}.bindings.{slug}",
        models_source=models_source,
        bindings_source=bindings_source,
        models_path=Path("models") / f"{slug}.py",
        bindings_path=Path("bindings") / f"{slug}.py",
        request_model=request_model,
        response_model=response_model,
        body_source=body_source,
        response_source=response_source,
        path_params=positional,
        query_params=queries,
    )


def generate_for(scope: str, method: str, path: str) -> GeneratedOperation:
    """:func:`generate` by operation identity; raises :class:`GenError` if unknown."""
    endpoint = specs.find_endpoint(scope, method, path)
    if endpoint is None:
        raise GenError(
            f"no such operation: {specs.endpoint_key(scope, method, path)}"
            + _suggest(scope, method, path)
        )
    return generate(endpoint)


def _suggest(scope: str, method: str, path: str) -> str:
    normalized = specs.normalize_path(path)
    tail = normalized.rstrip("/").split("/")[-1]
    near = sorted(
        key
        for key in specs.endpoint_index()
        if key.startswith(f"{scope} ") and tail and tail.lower() in key.lower()
    )
    if not near:
        return ""
    shown = near[:8]
    more = f" (+{len(near) - len(shown)} more)" if len(near) > len(shown) else ""
    return "\n  did you mean:\n    " + "\n    ".join(shown) + more


def write(result: GeneratedOperation, out_dir: Path, *, format_output: bool = True) -> list[Path]:
    """Write both modules under *out_dir*, then run ``ruff`` over them."""
    written: list[Path] = []
    for relative, source in result.files.items():
        destination = out_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
        written.append(destination)
    if format_output:
        run_ruff(written)
    return written


def run_ruff(paths: Sequence[Path]) -> None:
    """Format + autofix generated files in place, if ruff is installed.

    The emitter aims to produce already-clean text (a test asserts ``ruff
    format --diff`` is a no-op on the committed samples), so this is a
    belt-and-braces pass rather than the thing that makes the output legal --
    which is what keeps generation deterministic on a machine without ruff.
    """
    ruff = _find_ruff()
    if ruff is None:
        print("gen_models: ruff not found; skipping format pass", file=sys.stderr)
        return
    arguments = [str(path) for path in paths]
    for command in (
        [ruff, "check", "--quiet", "--fix-only", *arguments],
        [ruff, "format", "--quiet", *arguments],
    ):
        # Argument list, never shell=True: paths are repo-controlled but there
        # is no reason to hand any of this to a shell.
        subprocess.run(command, check=False, capture_output=True)  # noqa: S603


def _find_ruff() -> str | None:
    local = Path(sys.executable).resolve().parent / "ruff"
    if local.is_file():
        return str(local)
    return shutil.which("ruff")


# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen_models.py",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    parser.add_argument("--scope", choices=list(specs.SCOPES), required=True)
    parser.add_argument("--method", required=True, help="HTTP method, e.g. POST")
    parser.add_argument(
        "--path", required=True, help="spec path; a leading slash is optional for appliance paths"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"output package directory (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print both modules to stdout instead of writing"
    )
    parser.add_argument("--no-format", action="store_true", help="skip the ruff pass")
    return parser


def _iter_dry_run(result: GeneratedOperation) -> Iterator[str]:
    for relative, source in result.files.items():
        yield f"# ==> {relative.as_posix()}"
        yield source


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate_for(args.scope, args.method, args.path)
    except GenError as exc:
        print(f"gen_models: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"gen_models: error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print("\n".join(_iter_dry_run(result)))
        return 0

    try:
        written = write(result, args.out, format_output=not args.no_format)
    except OSError as exc:
        print(f"gen_models: error: {exc}", file=sys.stderr)
        return 2
    summary = ", ".join(
        part
        for part in (
            f"request={result.request_model}" if result.request_model else "request=untyped",
            f"response={result.response_model}" if result.response_model else "response=raw",
            f"body_source={result.body_source}",
        )
    )
    for path in written:
        print(path)
    print(f"gen_models: {result.endpoint.key} -> {result.slug} ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
