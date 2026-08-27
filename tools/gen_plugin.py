"""Emit a Tier-1 ``Resource`` plugin stub for one spec operation (issue #27).

Tier-1 codegen, second half (docs/plugin-promotion.md). ``tools/gen_models.py``
(#26) turns a spec operation into pydantic models and a typed binding; this
turns that binding into a registered plugin::

    src/pyecsdwan/resources/generated/<slug>.py

Usage::

    python tools/gen_plugin.py --scope appliance --method POST \
            --path '/virtualif/vti/{vtiName}'
    python tools/gen_plugin.py --scope orchestrator --method POST \
            --path /alarm/correlationSettings --dry-run
    python tools/spec_sync.py --diff --spec appliance --source new.json --json > drift.json
    python tools/spec_sync.py --update --spec appliance --source new.json
    python tools/gen_plugin.py --from-diff drift.json

The last three lines are the epic's end-to-end path: ``spec_sync`` detects an
added endpoint, ``--update`` vendors it into ``specs/`` (nothing can be
generated from an operation the baseline does not carry yet), and
``--from-diff`` reads the same JSON report back and emits one stub per added
*write* endpoint. ``tests/test_gen_plugin.py`` drives exactly that sequence
against a fixture spec.

What the emitted stub is, and is not
------------------------------------
It is a **starting point for curation**, not a regeneration target. Unlike
``pyecsdwan.generated``, these files are meant to be hand-edited: regenerating
overwrites your edits, which is why the tool refuses to overwrite an existing
stub without ``--force``.

Every stub carries, from the spec alone:

* ``fetch()`` -- the paired ``GET`` on the same path, if the spec has one,
  giving the best-effort pre-write snapshot the transaction journal records.
  Where there is no GET, ``fetch()`` returns ``None`` and says so.
* ``apply()`` / ``rollback()`` -- the write binding, plus (appliance scope)
  the ``ctx.save_changes([nePk])`` without which a proxied write is lost on
  the appliance's next reboot.
* ``tier = Tier.GENERATED`` -- the transaction engine refuses it a
  ``commit confirm`` window without ``--allow-untransactional`` (``txn.py``).
* ``normalize()`` raising ``NotCurated``. This is the point of the tier, not
  an oversight: idempotency needs a human decision about which fields are
  server-generated echoes, and until that decision is made the resource must
  refuse rather than look curated. ``tests/test_promotion.py`` parametrizes
  over the whole registry, so **every** stub anyone generates is held to that
  by ``make check`` -- there is no opt-in and no way to register a stub that
  quietly returns from ``normalize()``.

The reversibility ladder
------------------------
``reversibility`` is a promise the transaction engine acts on, so it is
derived conservatively and never guessed upward. :func:`reversibility_for`:

``COMPENSABLE``
    The write is ``POST``/``PUT``/``PATCH`` **and** the spec exposes a ``GET``
    on the same path. ``fetch()`` can snapshot the object and ``rollback()``
    replays that snapshot through the same write endpoint; where the snapshot
    shows the object did not exist and the spec exposes a ``DELETE`` on the
    path, ``rollback()`` deletes instead. Never ``REVERSIBLE``: nothing in a
    spec promises the GET's response is accepted verbatim by the write, so
    "restores exactly" is not something codegen may claim.

``IRREVERSIBLE``
    Everything else -- a write with no paired ``GET`` (nothing to snapshot, so
    nothing to put back), and every ``DELETE``-primary stub (undoing a delete
    means re-creating an object with server-assigned identity, which no
    generated code can do). These stubs refuse ``commit`` without ``--force``,
    which is the correct answer: claiming ``COMPENSABLE`` where ``rollback()``
    cannot compensate is strictly worse than declaring the truth.

A paired ``DELETE`` on its own never buys ``COMPENSABLE``. Without a ``GET``
the stub cannot tell a create from an update, and "compensating" an update by
deleting the object would destroy pre-existing configuration.

Exit codes: 0 generated, 1 nothing to generate (unknown or unusable
operation), 2 error.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shlex
import sys
import textwrap
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
for _extra in (str(REPO_ROOT / "src"), str(TOOLS_DIR)):
    if _extra not in sys.path:  # runnable without an editable install
        sys.path.insert(0, _extra)

import gen_models  # noqa: E402 - after the sys.path bootstrap above

from pyecsdwan import specs  # noqa: E402 - ditto

DEFAULT_RESOURCES_DIR = REPO_ROOT / "src" / "pyecsdwan" / "resources" / "generated"
DEFAULT_GENERATED_DIR = gen_models.DEFAULT_OUT_DIR
#: Package the emitted stubs live in; must match ``DEFAULT_RESOURCES_DIR``.
STUB_PACKAGE = "pyecsdwan.resources.generated"
LINE_LENGTH = gen_models.LINE_LENGTH

#: Methods a plugin stub can be generated from, in the order a group of added
#: endpoints on one path is collapsed to a single stub. A GET-only endpoint is
#: a view, not a configurable object, and gets no resource.
WRITE_METHODS: tuple[str, ...] = ("POST", "PUT", "PATCH", "DELETE")

#: Kind prefix. Namespaced so a generated kind can never collide with a
#: curated one, and so ``ec-cli show coverage`` reads unambiguously.
KIND_PREFIX = "generated/"

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class GenPluginError(Exception):
    """Nothing could be generated; message is operator-facing."""


# ---------------------------------------------------------------------------
# Facts derived from the spec


@dataclasses.dataclass(frozen=True)
class RefParam:
    """One value the emitted stub takes from ``Ref.name``.

    Path parameters and *required* query parameters both land here: neither has
    a defensible default a generator could invent, and both change which
    instance the operation addresses.
    """

    wire_name: str
    where: str  # "path" | "query"
    type_expr: str  # "str" | "int" | "float" | "bool" | "Any"
    description: str = ""


@dataclasses.dataclass(frozen=True)
class GeneratedStub:
    """Everything :func:`generate_stub` produces, before any I/O."""

    endpoint: specs.Endpoint
    slug: str
    kind: str
    class_name: str
    module: str
    path: Path
    source: str
    reversibility: str
    reversibility_reason: str
    #: The write operation, then the paired GET and DELETE where they exist.
    operations: tuple[gen_models.GeneratedOperation, ...]
    ref_params: tuple[RefParam, ...]
    declared_endpoints: tuple[str, ...]

    @property
    def write(self) -> gen_models.GeneratedOperation:
        return self.operations[0]


def _value_expr(wire_name: str, type_expr: str) -> str:
    """Source reading one ref parameter out of the parsed name, coerced.

    The coercion is chosen per *call*, not per parameter: two operations on one
    path can declare the same query parameter with different types (the 7.2.0
    baseline types ``serviceId`` as an integer on one and a string on the next),
    and a single coercion for both fails mypy on whichever one disagrees.
    """
    lookup = f'values["{wire_name}"]'
    if type_expr in ("int", "float"):
        return f"{type_expr}({lookup})"
    if type_expr == "bool":
        return f"as_bool({lookup})"
    return lookup


def binding_requires_body(operation: gen_models.GeneratedOperation) -> bool:
    """Whether the emitted binding takes a request body it cannot do without."""
    return "\n    body: RequestBody,\n" in operation.bindings_source


def binding_parameters(operation: gen_models.GeneratedOperation) -> tuple[str, ...]:
    """Parameter names of an emitted binding function, in declaration order.

    Read back off the rendered source rather than re-deriving gen_models'
    signature rules, so the call this tool emits cannot drift from the
    signature that tool emitted. ``*`` is dropped; defaults are irrelevant
    here because every argument this stub passes is positional or required.
    """
    marker = f"def {operation.function_name}("
    body = operation.bindings_source
    start = body.index(marker) + len(marker)
    names: list[str] = []
    for line in body[start:].splitlines():
        stripped = line.strip()
        if stripped.startswith(")"):
            break
        if stripped in ("*,", "*"):
            continue
        name = stripped.split(":", 1)[0].strip().rstrip(",")
        if name:
            names.append(name)
    return tuple(names)


def paired(endpoint: specs.Endpoint, method: str) -> specs.Endpoint | None:
    """The same path's operation under another method, if the spec has one."""
    found = specs.find_endpoint(endpoint.scope, method, endpoint.path)
    return None if found is None or found.key == endpoint.key else found


def reversibility_for(
    endpoint: specs.Endpoint, read: specs.Endpoint | None, delete: specs.Endpoint | None
) -> tuple[str, str]:
    """The declared reversibility class and the sentence explaining it.

    See the module docstring for why this ladder stops at COMPENSABLE and why
    a paired DELETE alone is not enough.
    """
    if endpoint.method == "DELETE":
        return (
            "IRREVERSIBLE",
            "undoing a delete means re-creating an object with server-assigned "
            "identity, which nothing in the spec describes",
        )
    if read is None:
        return (
            "IRREVERSIBLE",
            f"the spec exposes no GET on {endpoint.path.strip()}, so there is no "
            f"pre-write snapshot to put back",
        )
    compensator = (
        f" and {delete.method} {delete.path.strip()} compensates a creation"
        if delete is not None
        else ""
    )
    return (
        "COMPENSABLE",
        f"GET {read.path.strip()} gives a best-effort pre-write snapshot that "
        f"rollback() replays through the same write endpoint{compensator}; "
        f"nothing proves the GET's response is accepted verbatim by the write, "
        f"so this is not REVERSIBLE",
    )


def ref_params_for(operations: Sequence[gen_models.GeneratedOperation]) -> tuple[RefParam, ...]:
    """The values every instance ref must carry, deduped by wire name.

    Path parameters come from the *write* operation: the paired GET/DELETE sit
    on the same normalized path, so their placeholders line up positionally
    even when the spec spells one ``{id}`` and the next ``{Id}``.
    """
    out: list[RefParam] = []
    seen: set[str] = set()
    write = operations[0]
    for _python_name, wire_name in write.path_params:
        if wire_name in seen:
            continue
        seen.add(wire_name)
        out.append(RefParam(wire_name, "path", "str", _path_param_doc(write.endpoint, wire_name)))
    for operation in operations:
        for query in operation.query_params:
            if not query.required or query.wire_name in seen:
                continue
            seen.add(query.wire_name)
            out.append(RefParam(query.wire_name, "query", query.type_expr, query.description))
    return tuple(out)


def _path_param_doc(endpoint: specs.Endpoint, wire_name: str) -> str:
    for param in endpoint.parameters("path"):
        if param.get("name") == wire_name:
            return str(param.get("description") or "")
    return ""


def generate_stub(endpoint: specs.Endpoint) -> GeneratedStub:
    """Build the stub module for one write operation, in memory. No I/O."""
    if endpoint.method not in WRITE_METHODS:
        raise GenPluginError(
            f"{endpoint.key}: {endpoint.method} is read-only; a plugin stub is "
            f"generated from a write operation (one of {', '.join(WRITE_METHODS)}). "
            f"Use tools/gen_models.py for a typed read binding."
        )
    if " " in endpoint.path.strip() or endpoint.path != endpoint.path.strip():
        raise GenPluginError(
            f"{endpoint.key}: the spec path contains whitespace ({endpoint.path!r}); "
            f"Resource.endpoints is a space-separated key and cannot express it. "
            f"Generate the binding with tools/gen_models.py and hand-write the plugin."
        )

    write = gen_models.generate(endpoint)
    if endpoint.method == "DELETE" and binding_requires_body(write):
        raise GenPluginError(
            f"{endpoint.key}: the spec makes a request body mandatory for this DELETE, "
            f"and a stub has no desired state to build one from (a delete's desired "
            f"state is 'absent'). Generate the binding with tools/gen_models.py and "
            f"hand-write the plugin."
        )
    read_endpoint = paired(endpoint, "GET")
    delete_endpoint = paired(endpoint, "DELETE") if endpoint.method != "DELETE" else None
    read = gen_models.generate(read_endpoint) if read_endpoint is not None else None
    delete = gen_models.generate(delete_endpoint) if delete_endpoint is not None else None
    if delete is not None and binding_requires_body(delete):
        # A compensating delete that demands a payload is not a compensator: the
        # stub would have to invent one. Drop it rather than emit a call that
        # cannot be made, which also (correctly) costs the stub its DELETE-based
        # compensation for a creation.
        delete = None
        delete_endpoint = None

    operations = tuple(op for op in (write, read, delete) if op is not None)
    reversibility, reason = reversibility_for(endpoint, read_endpoint, delete_endpoint)
    params = ref_params_for(operations)
    slug = write.slug
    class_name = gen_models.python_class_name(slug)
    declared = tuple(
        f"{op.endpoint.scope} {op.endpoint.method} {op.endpoint.path}" for op in operations
    )

    source = render_stub_module(
        endpoint,
        slug=slug,
        class_name=class_name,
        write=write,
        read=read,
        delete=delete,
        params=params,
        reversibility=reversibility,
        reason=reason,
        declared=declared,
    )
    return GeneratedStub(
        endpoint=endpoint,
        slug=slug,
        kind=f"{KIND_PREFIX}{slug}",
        class_name=class_name,
        module=f"{STUB_PACKAGE}.{slug}",
        path=Path(f"{slug}.py"),
        source=source,
        reversibility=reversibility,
        reversibility_reason=reason,
        operations=operations,
        ref_params=params,
        declared_endpoints=declared,
    )


def generate_stub_for(scope: str, method: str, path: str) -> GeneratedStub:
    """:func:`generate_stub` by operation identity."""
    endpoint = specs.find_endpoint(scope, method, path)
    if endpoint is None:
        raise GenPluginError(f"no such operation: {specs.endpoint_key(scope, method, path)}")
    return generate_stub(endpoint)


# ---------------------------------------------------------------------------
# Module assembly


def _regen_command(endpoint: specs.Endpoint) -> list[str]:
    return [
        "python tools/gen_plugin.py \\",
        f"        --scope {endpoint.scope} --method {endpoint.method} \\",
        f"        --path {shlex.quote(endpoint.path)}",
    ]


def _fit_tokens(text: str, budget: int) -> str:
    """Clip any single word longer than *budget* so wrapping cannot split it.

    ``textwrap`` breaks an over-long word mid-token, which is merely ugly in a
    docstring but *corrupts* an implicitly concatenated string literal (the
    chunks are re-joined with a space). Dotted module paths and generated class
    names are the two that get near the budget, so they are truncated visibly
    rather than silently mangled.
    """
    out = []
    for word in text.split():
        out.append(word if len(word) <= budget else word[: budget - 3] + "...")
    return " ".join(out)


def _wrap(text: str, width: int = LINE_LENGTH, indent: str = "") -> list[str]:
    budget = width - len(indent)
    return textwrap.wrap(
        _fit_tokens(text, budget),
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [indent.rstrip()]


def _stub_header(
    endpoint: specs.Endpoint,
    *,
    reversibility: str,
    reason: str,
    read: gen_models.GeneratedOperation | None,
) -> list[str]:
    baseline = specs.baseline_path(endpoint.scope)
    source = baseline.name if baseline is not None else f"{endpoint.scope}-openapi (not vendored)"
    version = specs.spec_version(endpoint.scope) or "unknown"
    provenance = (
        f"Source: specs/{source} (spec version {version})"
        + (f", operationId ``{endpoint.operation_id}``" if endpoint.operation_id else "")
        + "."
    )
    operation = f"{endpoint.scope} {endpoint.method} {endpoint.path}"
    headline = f"Tier-1 generated plugin stub for ``{operation}``."
    lines: list[str] = ['"""' + headline if len(headline) <= LINE_LENGTH - 3 else '"""']
    if len(headline) > LINE_LENGTH - 3:
        lines += _wrap(headline)
    lines += [
        "",
        "Machine-generated by ``tools/gen_plugin.py`` (issue #27). Unlike",
        "``pyecsdwan.generated``, this file is meant to be **hand-edited**: it is the",
        "starting point for curation, and regenerating it overwrites your work::",
        "",
        *(f"    {line}" for line in _regen_command(endpoint)),
        "",
        *_wrap(provenance),
    ]
    if endpoint.summary:
        lines += ["", *_wrap(gen_models.ascii_fold(endpoint.summary))]
    lines += [
        "",
        "**Tier 1 — not curated.** ``normalize()`` raises ``NotCurated``, so every",
        "caller refuses this kind: ``plan``/``commit`` cannot build a diff for it, and",
        "``tests/test_promotion.py`` fails ``make check`` if that raise is ever replaced",
        "by a value without also setting ``tier = Tier.CURATED``. The transaction engine",
        "separately refuses it a ``commit confirm`` window unless the operator passes",
        "``--allow-untransactional``. Work down the checklist in",
        "``docs/plugin-promotion.md`` to promote it.",
        "",
        *_wrap(f"Reversibility: {reversibility} — {reason}."),
    ]
    if read is None:
        lines += [
            "",
            *_wrap(
                "``fetch()`` therefore always returns ``None``: the journal records no "
                "pre-change state for this kind and ``rollback()`` refuses."
            ),
        ]
    if endpoint.deprecated:
        lines += ["", "The spec marks this operation deprecated."]
    lines.append('"""')
    return lines


def _chunks(text: str, budget: int) -> list[str]:
    """Wrap *text* into literal-safe pieces: never split a word in two."""
    return textwrap.wrap(
        _fit_tokens(text, budget),
        width=budget,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]


def _literal_assignment(name: str, text: str, indent: str) -> list[str]:
    """``name = (\n "..."\n "..."\n)`` — a wrapped string literal that fits."""
    budget = LINE_LENGTH - len(indent) - 8
    chunks = _chunks(text, budget)
    single = f'{indent}{name} = "{chunks[0]}"' if len(chunks) == 1 else ""
    if single and len(single) <= LINE_LENGTH:
        return [single]
    lines = [f"{indent}{name} = ("]
    for index, chunk in enumerate(chunks):
        trailing = "" if index == len(chunks) - 1 else " "
        lines.append(f'{indent}    "{chunk}{trailing}"')
    lines.append(f"{indent})")
    return lines


def _raise_block(indent: str, exception: str, text: str) -> list[str]:
    """``raise Exc(\n f"..."\n)`` with the message wrapped to the line budget.

    Chunks carrying a ``{placeholder}`` are emitted as f-strings and the rest
    as plain literals -- marking a placeholder-free chunk ``f"..."`` would trip
    ruff's ``F541``.
    """
    budget = LINE_LENGTH - len(indent) - 8
    chunks = _chunks(text, budget)
    lines = [f"{indent}raise {exception}("]
    for index, chunk in enumerate(chunks):
        trailing = "" if index == len(chunks) - 1 else " "
        prefix = "f" if "{" in chunk else ""
        lines.append(f'{indent}    {prefix}"{chunk}{trailing}"')
    lines.append(f"{indent})")
    return lines


def _docstring(indent: str, summary: str, *paragraphs: str) -> list[str]:
    """A docstring: a summary line (wrapped when long) plus wrapped paragraphs.

    Emitted rather than hand-spelled because both halves overflow in practice
    -- some spec summaries run past 100 columns on their own, and an endpoint
    key interpolated into a sentence pushes plenty more over.
    """
    head = textwrap.wrap(
        _fit_tokens(summary, LINE_LENGTH - len(indent) - 3),
        width=LINE_LENGTH,
        initial_indent=f'{indent}"""',
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [f'{indent}"""']
    if not paragraphs and len(head) == 1 and len(head[0]) + 3 <= LINE_LENGTH:
        return [head[0] + '"""']
    lines = list(head)
    for paragraph in paragraphs:
        lines += ["", *_wrap(paragraph, indent=indent)]
    lines.append(f'{indent}"""')
    return lines


def _call(
    target: str,
    function: str,
    arguments: Sequence[str],
    keywords: Sequence[str],
    indent: str,
    trailing: str = "",
) -> list[str]:
    """Render a call, on one line when it fits and one argument per line when not.

    ``trailing`` is whatever follows the closing paren (a comma, inside a
    tuple literal); it counts against the line budget, which is the kind of
    off-by-one that shows up as a single E501 in one file out of hundreds.
    """
    parts = [*arguments, *keywords]
    flat = f"{indent}{target}{function}({', '.join(parts)}){trailing}"
    if len(flat) <= LINE_LENGTH:
        return [flat]
    lines = [f"{indent}{target}{function}("]
    lines.extend(f"{indent}    {part}," for part in parts)
    lines.append(f"{indent}){trailing}")
    return lines


def _call_arguments(
    operation: gen_models.GeneratedOperation,
    *,
    write: gen_models.GeneratedOperation,
    is_appliance: bool,
    params: Sequence[RefParam],
    body: str | None,
) -> tuple[list[str], list[str]]:
    """Positional and keyword arguments for one binding call.

    Path placeholders are matched to the write operation's positionally: the
    paired GET/DELETE share the write's normalized path, so the *n*-th
    placeholder is the same value even when the spec spells it differently
    (``{id}`` here, ``{Id}`` there).
    """
    del params  # collected for documentation; the coercion is per-call
    names = binding_parameters(operation)
    positional = ["ctx.client"]
    if is_appliance:
        positional.append("ne_pk")
    write_wire = [wire for _python, wire in write.path_params]
    for index, _placeholder in enumerate(operation.path_params):
        # gen_models types every path placeholder ``str``.
        positional.append(_value_expr(write_wire[index], "str"))
    if body is not None and "body" in names:
        positional.append(body)
    keywords = [
        f"{query.python_name}={_value_expr(query.wire_name, query.type_expr)}"
        for query in operation.query_params
        if query.required
    ]
    return positional, keywords


def _needs_values(
    operations: Iterable[gen_models.GeneratedOperation], params: Sequence[RefParam]
) -> bool:
    return bool(params) and any(
        op.path_params or any(q.required for q in op.query_params) for op in operations
    )


def _fetch_method(
    endpoint: specs.Endpoint,
    *,
    write: gen_models.GeneratedOperation,
    read: gen_models.GeneratedOperation | None,
    params: Sequence[RefParam],
    is_appliance: bool,
) -> list[str]:
    if read is None:
        return [
            "    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:",
            *_docstring(
                "        ",
                "No pre-write snapshot exists for this operation.",
                f"The spec exposes no GET on {endpoint.path}, so there is nothing to "
                f"record before the write and nothing for rollback() to put back. "
                f"Returning None (rather than raising) keeps the journal honest: it "
                f"records 'state unknown', which is exactly what IRREVERSIBLE means "
                f"here.",
            ),
            "        return None",
        ]
    positional, keywords = _call_arguments(
        read, write=write, is_appliance=is_appliance, params=params, body=None
    )
    lines = [
        "    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:",
        *_docstring(
            "        ",
            "Best-effort GET-before-write snapshot.",
            f"Reads {read.endpoint.key} through the generated binding and dumps it "
            f"back to plain JSON with the same serializer the write binding uses, so "
            f"a snapshot replayed by rollback() is byte-for-byte what the server "
            f"returned -- undocumented fields included. A 404 is 'absent', not an "
            f"error. Every other failure propagates: a snapshot that silently came "
            f"back empty would make rollback() write an empty object.",
        ),
    ]
    if _needs_values([read], params):
        lines.append("        values = param_values(KIND, ref, REF_PARAMS)")
    if is_appliance:
        lines.append("        ne_pk = ne_pk_for(ctx, KIND, ref)")
    lines.append("        try:")
    lines += _call("raw = ", read.function_name, positional, keywords, "            ")
    lines += [
        "        except OrchApiError as exc:",
        "            if exc.status_code == 404:",
        "                return None",
        "            raise",
        "        return as_raw(raw)",
    ]
    return lines


def _normalize_method(slug: str) -> list[str]:
    return [
        "    def normalize(self, raw: RawState) -> CanonicalState:",
        *_docstring(
            "        ",
            "Refuse: this kind is generated, not curated.",
            "Idempotency is the one thing a spec cannot describe. Which fields the "
            "server generates and echoes back, which lists have a stable sort key, "
            "which values are names that must be resolved to IDs -- all of that is a "
            "human decision, and until it is made this resource must refuse rather "
            "than look curated to the diff engine.",
            "To curate: replace this body with a real canonicalization, prove "
            "normalize(normalize(x)) == normalize(x) and an empty re-plan (`ec-cli "
            f"plugin promote {KIND_PREFIX}{slug}`), fix the reversibility class and "
            "managed_by() above, then set tier = Tier.CURATED. The promotion gate in "
            "tests/test_promotion.py enforces the order: flip the tier without the "
            "proof and make check fails.",
        ),
        *_raise_block(
            "        ",
            "NotCurated",
            "{KIND} is a Tier-1 generated stub: normalize() has not been "
            "written, so no plan, diff or commit can use it. See "
            "docs/plugin-promotion.md for the promotion checklist.",
        ),
    ]


def _write_method(
    *,
    write: gen_models.GeneratedOperation,
    params: Sequence[RefParam],
    is_appliance: bool,
) -> list[str]:
    positional, keywords = _call_arguments(
        write, write=write, is_appliance=is_appliance, params=params, body="desired"
    )
    lines = [
        "    def _write(",
        "        self, ctx: Ctx, ref: Ref, desired: dict[str, Any] | list[Any], action: str",
        "    ) -> ApplyResult:",
        *_docstring("        ", f"Send *desired* to {write.endpoint.key} verbatim."),
    ]
    if _needs_values([write], params):
        lines.append("        values = param_values(KIND, ref, REF_PARAMS)")
    if is_appliance:
        lines.append("        ne_pk = ne_pk_for(ctx, KIND, ref)")
    lines += _call(
        "",
        "log.info",
        ['"generated_stub_write"', "kind=KIND", "ref=str(ref)", "action=action"],
        [],
        "        ",
    )
    lines += _call("", write.function_name, positional, keywords, "        ")
    if is_appliance:
        lines += _appliance_persist("write")
    else:
        lines.append('        return ApplyResult(ok=True, message=f"{KIND} {action}: {ref}")')
    return lines


def _appliance_persist(what: str) -> list[str]:
    """The save-changes tail every appliance-proxy write needs (issue #11)."""
    return [
        '        save = ctx.save_changes([ne_pk], f"{KIND} {action}: {ref}")',
        '        if save.state != "SUCCESS":',
        "            return ApplyResult(",
        "                ok=False,",
        "                jobs=[save],",
        "                message=(",
        f'                    f"{{KIND}} {what} on {{ne_pk}} is NOT persisted -- "',
        '                    f"save-changes {save.state}: {save.detail}"',
        "                ),",
        "            )",
        "        return ApplyResult(",
        "            ok=True,",
        "            jobs=[save],",
        '            message=f"{KIND} {action} on {ne_pk} persisted",',
        "        )",
    ]


def _delete_method(
    *,
    write: gen_models.GeneratedOperation,
    delete: gen_models.GeneratedOperation,
    params: Sequence[RefParam],
    is_appliance: bool,
) -> list[str]:
    positional, keywords = _call_arguments(
        delete, write=write, is_appliance=is_appliance, params=params, body=None
    )
    lines = [
        "    def _delete(self, ctx: Ctx, ref: Ref, action: str) -> ApplyResult:",
        *_docstring("        ", f"Remove the instance via {delete.endpoint.key}."),
    ]
    if _needs_values([delete], params):
        lines.append("        values = param_values(KIND, ref, REF_PARAMS)")
    if is_appliance:
        lines.append("        ne_pk = ne_pk_for(ctx, KIND, ref)")
    lines += _call(
        "",
        "log.info",
        ['"generated_stub_delete"', "kind=KIND", "ref=str(ref)", "action=action"],
        [],
        "        ",
    )
    lines += _call("", delete.function_name, positional, keywords, "        ")
    if is_appliance:
        lines += _appliance_persist("delete")
    else:
        lines.append(
            '        return ApplyResult(ok=True, message=f"{KIND} {action}: {ref} removed")'
        )
    return lines


def _apply_method(
    endpoint: specs.Endpoint,
    *,
    delete: gen_models.GeneratedOperation | None,
) -> list[str]:
    lines = [
        "    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:",
        *_docstring("        ", f"Write {endpoint.key} for the whole desired object."),
        "        if diff.empty:",
        "            return ApplyResult.noop()",
    ]
    if endpoint.method == "DELETE":
        lines += [
            "        if diff.desired is not None:",
            "            return ApplyResult(",
            "                ok=False,",
            "                message=(",
            '                    f"{KIND} was generated from a DELETE operation: it can remove "',
            '                    f"{diff.ref}, but the spec gives it no endpoint to write "',
            '                    f"state to it"',
            "                ),",
            "            )",
            '        return self._delete(ctx, diff.ref, "apply")',
        ]
        return lines
    lines.append("        if diff.desired is None:")
    if delete is None:
        lines += [
            "            return ApplyResult(",
            "                ok=False,",
            "                message=(",
            '                    f"{KIND} cannot delete {diff.ref}: the spec exposes no "',
            '                    f"DELETE on this path"',
            "                ),",
            "            )",
        ]
    else:
        lines.append('            return self._delete(ctx, diff.ref, "apply")')
    lines.append('        return self._write(ctx, diff.ref, diff.desired, "apply")')
    return lines


def _rollback_method(
    endpoint: specs.Endpoint,
    *,
    reversibility: str,
    reason: str,
    delete: gen_models.GeneratedOperation | None,
) -> list[str]:
    if reversibility == "IRREVERSIBLE":
        return [
            "    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:",
            *_docstring(
                "        ",
                "Refuse: this kind is IRREVERSIBLE.",
                f"{reason[:1].upper()}{reason[1:]}. Reporting failure is the honest "
                f"answer -- the engine already refuses this kind a commit without "
                f"--force, and a rollback() that pretended to succeed would leave the "
                f"operator believing a change had been undone.",
            ),
            "        return ApplyResult(",
            "            ok=False,",
            "            message=(",
            '                f"{KIND} is IRREVERSIBLE: no generated compensating action "',
            '                f"exists for {ref}; the change stands"',
            "            ),",
            "        )",
        ]
    lines = [
        "    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:",
        *_docstring("        ", "Replay the pre-change snapshot through the same write endpoint."),
        "        if snapshot is None:",
    ]
    if delete is None:
        lines += [
            "            return ApplyResult(",
            "                ok=False,",
            "                message=(",
            '                    f"no pre-change state recorded for {ref}: it did not "',
            '                    f"exist before this change, and the spec exposes no "',
            '                    f"DELETE on this path to compensate the creation"',
            "                ),",
            "            )",
        ]
    else:
        lines += [
            "            # Absent before the change: compensate the creation.",
            '            return self._delete(ctx, ref, "rollback")',
        ]
    lines.append('        return self._write(ctx, ref, snapshot, "rollback")')
    return lines


def _managed_by_method(endpoint: specs.Endpoint) -> list[str]:
    return [
        "    def managed_by(self, ctx: Ctx, ref: Ref) -> str | None:",
        *_docstring(
            "        ",
            "Template ownership for this appliance-scope section.",
            "TODO(curation): pyecsdwan.ownership.KIND_TO_TEMPLATE_SECTIONS carries no "
            f"entry for this kind, so this returns None -- which reads as 'no template "
            f"owns it' and is an assumption, not a finding. Before promoting, add the "
            f"template section name(s) covering {endpoint.path} to that map; the next "
            f"template push would otherwise silently revert a direct write here.",
        ),
        "        if ref.appliance is None:",
        "            return None",
        "        return owning_group(ctx, self.kind, ctx.resolver.ne_pk_for(ref.appliance))",
    ]


def _class_attributes(
    endpoint: specs.Endpoint,
    *,
    class_name: str,
    write: gen_models.GeneratedOperation,
    delete: gen_models.GeneratedOperation | None,
    params: Sequence[RefParam],
    reversibility: str,
    declared: Sequence[str],
) -> list[str]:
    scope = "APPLIANCE" if endpoint.scope == "appliance" else "ORCHESTRATOR"
    summary = gen_models.ascii_fold(endpoint.summary) or f"{endpoint.method} {endpoint.path}"
    lines = [
        f"class {class_name}(Resource):",
        *_docstring(
            "    ",
            f"{summary.rstrip('.')}.",
            "Generated, un-curated. See the module docstring for what that means and "
            "what curating it involves.",
        ),
        "",
        "    kind = KIND",
        f"    scope = Scope.{scope}",
        f"    reversibility = Reversibility.{reversibility}",
        "    tier = Tier.GENERATED",
        f"    deletable = {delete is not None or endpoint.method == 'DELETE'}",
        "    endpoints = (",
        *(f'        "{key}",' for key in declared),
        "    )",
    ]
    operation = f"{endpoint.scope} {endpoint.method} {endpoint.path}"
    shape = (
        f"the request body of {operation}, verbatim -- the documented fields are the "
        f"{write.request_model} model this stub's binding imports"
        if write.request_model
        else f"the request body of {operation}; the spec declares no shape for it, so "
        f"any JSON object or array is passed through untouched"
    )
    naming = (
        f"Ref name carries the endpoint's parameters as {gen_models_ref_syntax(params)}."
        if params
        else "This endpoint addresses a single instance; the ref name is a free label."
    )
    lines += _literal_assignment(
        "desired_state_doc",
        f"{shape}. {naming} NOT CURATED: normalize() refuses, so this kind cannot "
        f"take part in a plan or a commit until it is curated.",
        "    ",
    )
    return lines


def gen_models_ref_syntax(params: Sequence[RefParam]) -> str:
    """The ``Ref.name`` spelling the emitted stub accepts (mirrors ``_stub``)."""
    if len(params) == 1:
        return f"'{params[0].wire_name}' (bare value, or {params[0].wire_name}=<value>)"
    return "'" + ",".join(f"{p.wire_name}=<value>" for p in params) + "'"


def _constants(slug: str, params: Sequence[RefParam]) -> list[str]:
    lines = [
        *_call("log = ", "structlog.get_logger", [f'"{STUB_PACKAGE}.{slug}"'], [], ""),
        "",
        "#: Namespaced under ``generated/`` so a stub can never collide with a",
        "#: curated kind, and so `ec-cli show coverage` reads unambiguously.",
        f'KIND = "{KIND_PREFIX}{slug}"',
        "",
    ]
    if not params:
        lines += [
            "#: This endpoint takes no path or required query parameters.",
            "REF_PARAMS: tuple[StubParam, ...] = ()",
        ]
        return lines
    lines += [
        "#: Values every instance ref must carry in ``Ref.name`` (see",
        "#: ``pyecsdwan.resources.generated._stub.param_values``).",
        "REF_PARAMS: tuple[StubParam, ...] = (",
    ]
    for param in params:
        description = gen_models.ascii_fold(param.description).replace('"', "'")
        lines += _call(
            "",
            "StubParam",
            [f'"{param.wire_name}"', f'"{param.where}"', f'"{_clip(description)}"'],
            [],
            "    ",
            trailing=",",
        )
    lines.append(")")
    return lines


def _clip(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _uses(body: str, name: str) -> bool:
    return any(word == name for word in _WORD.findall(body))


def _import_block(
    stdlib: Sequence[str], third_party: Sequence[str], first: Sequence[str]
) -> list[str]:
    groups = [list(stdlib), list(third_party), list(first)]
    lines: list[str] = []
    for group in groups:
        if not group:
            continue
        if lines:
            lines.append("")
        lines.extend(group)
    return lines


def render_stub_module(
    endpoint: specs.Endpoint,
    *,
    slug: str,
    class_name: str,
    write: gen_models.GeneratedOperation,
    read: gen_models.GeneratedOperation | None,
    delete: gen_models.GeneratedOperation | None,
    params: Sequence[RefParam],
    reversibility: str,
    reason: str,
    declared: Sequence[str],
) -> str:
    """Assemble the ``resources/generated/<slug>.py`` source."""
    is_appliance = endpoint.scope == "appliance"
    is_delete_primary = endpoint.method == "DELETE"

    methods: list[list[str]] = [
        _fetch_method(endpoint, write=write, read=read, params=params, is_appliance=is_appliance),
        _normalize_method(slug),
    ]
    if is_appliance:
        methods.append(_managed_by_method(endpoint))
    if not is_delete_primary:
        methods.append(_write_method(write=write, params=params, is_appliance=is_appliance))
    compensator = write if is_delete_primary else delete
    if compensator is not None:
        methods.append(
            _delete_method(
                write=write, delete=compensator, params=params, is_appliance=is_appliance
            )
        )
    methods.append(_apply_method(endpoint, delete=delete))
    methods.append(
        _rollback_method(endpoint, reversibility=reversibility, reason=reason, delete=delete)
    )

    class_lines = _class_attributes(
        endpoint,
        class_name=class_name,
        write=write,
        delete=delete,
        params=params,
        reversibility=reversibility,
        declared=declared,
    )
    for method in methods:
        class_lines += ["", *method]

    body_lines = [
        *_constants(slug, params),
        "",
        "",
        *class_lines,
        "",
        "",
        f"register({class_name}())",
    ]
    body = "\n".join(body_lines)

    # Structural, not textual: "Any" is also an English word, and the fetch()
    # docstring used to smuggle it into the import list.
    stdlib = [] if is_delete_primary else ["from typing import Any"]
    third_party = ["import structlog"]
    first: list[str] = []
    if _uses(body, "OrchApiError"):
        first.append("from pyecsdwan.client import OrchApiError")
    contract_names = [
        name
        for name in (
            "ApplyResult",
            "CanonicalState",
            "Ctx",
            "Diff",
            "NotCurated",
            "RawState",
            "Ref",
            "Resource",
            "Reversibility",
            "Scope",
            "Tier",
        )
        if _uses(body, name)
    ]
    first.append("from pyecsdwan.contract import (")
    first.extend(f"    {name}," for name in contract_names)
    first.append(")")
    for operation in sorted(
        {op.bindings_module: op for op in (write, read, delete) if op is not None}.values(),
        key=lambda op: op.bindings_module,
    ):
        if not _uses(body, operation.function_name):
            continue
        single = f"from {operation.bindings_module} import {operation.function_name}"
        if len(single) <= LINE_LENGTH:
            first.append(single)
        else:
            first.append(f"from {operation.bindings_module} import (")
            first.append(f"    {operation.function_name},")
            first.append(")")
    if _uses(body, "owning_group"):
        first.append("from pyecsdwan.ownership import owning_group")
    first.append("from pyecsdwan.registry import register")
    helpers = [
        name
        for name in ("StubParam", "as_bool", "as_raw", "ne_pk_for", "param_values")
        if _uses(body, name)
    ]
    helper_import = f"from {STUB_PACKAGE}._stub import {', '.join(helpers)}"
    if len(helper_import) <= LINE_LENGTH:
        first.append(helper_import)
    else:
        first.append(f"from {STUB_PACKAGE}._stub import (")
        first.extend(f"    {name}," for name in helpers)
        first.append(")")

    lines = _stub_header(endpoint, reversibility=reversibility, reason=reason, read=read)
    lines += ["", "from __future__ import annotations", ""]
    lines += _import_block(stdlib, third_party, first)
    # One blank line, not two: isort's ``lines-after-imports`` default wants two
    # only when a def/class follows, and what follows here is the ``log =``
    # assignment. Two trips ruff's I001 on every emitted stub.
    lines += [""]
    lines += body_lines
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# The package the stubs live in


def stub_slugs(resources_dir: Path) -> list[str]:
    """Stub module names present on disk, sorted. Ignores ``_``-prefixed files."""
    if not resources_dir.is_dir():
        return []
    return sorted(
        path.stem
        for path in resources_dir.glob("*.py")
        if not path.stem.startswith("_") and path.stem != "__init__"
    )


def render_package_init(slugs: Sequence[str]) -> str:
    """The ``resources/generated/__init__.py`` that registers what is present.

    Explicit imports rather than a ``pkgutil`` walk: registration is the
    acceptance criterion for this issue, and an explicit list is the version
    of it a reviewer can read and a diff can show. ``tests/test_gen_plugin.py``
    asserts the list matches the directory, so a stub dropped in by hand
    without regenerating this file fails ``make check`` rather than silently
    never registering.

    Ordering follows ``gen_models.dunder_all_order`` (ruff's ``RUF022``
    convention). That helper and ruff disagree on one family of names --
    a hash-suffixed slug whose disambiguator starts with a zero,
    ``..._081e94``, which ruff orders as a fraction and the helper as the
    integer 81 (docs/futures/README.md). It only shows up when hundreds of
    stubs share a package, and :func:`write`'s ruff pass fixes it on disk.
    """
    lines = [
        '"""Tier-1 plugin stubs generated by ``tools/gen_plugin.py`` (issue #27).',
        "",
        "Importing this package registers every stub in it. The default tree carries",
        "the committed samples only -- generation is on demand, one operation at a",
        "time, so this package holds what someone actually needed rather than a dump",
        "of all 1833 spec operations.",
        "",
        "Every module here is **generated once and then hand-edited**: it is the",
        "starting point for curation, not a regeneration target. Each one declares",
        "``tier = Tier.GENERATED`` and a ``normalize()`` that raises ``NotCurated``,",
        "which ``tests/test_promotion.py`` enforces over the whole registry -- so a",
        "stub cannot be half-curated into looking transactional.",
        '"""',
        "",
    ]
    if not slugs:
        lines += ["__all__: list[str] = []"]
        return "\n".join(lines) + "\n"
    ordered = gen_models.dunder_all_order(slugs)
    lines += [f"from {STUB_PACKAGE} import ("]
    lines += [f"    {slug}," for slug in ordered]
    lines += [")", "", "__all__ = ["]
    lines += [f'    "{slug}",' for slug in ordered]
    lines += ["]"]
    return "\n".join(lines) + "\n"


def write(
    stub: GeneratedStub,
    *,
    resources_dir: Path = DEFAULT_RESOURCES_DIR,
    generated_dir: Path = DEFAULT_GENERATED_DIR,
    format_output: bool = True,
    force: bool = False,
) -> list[Path]:
    """Put a stub and every binding it imports on disk, then run ruff over them.

    Refuses to clobber an existing stub without *force*: these files are
    hand-edited after generation, so an accidental re-run must not be the thing
    that loses a curation pass. The generated models/bindings under
    ``generated_dir`` are overwritten freely -- those *are* regeneration
    targets and are byte-identical for an unchanged spec.
    """
    destination = resources_dir / stub.path
    if destination.exists() and not force:
        raise GenPluginError(
            f"{destination} already exists; it is hand-edited after generation. "
            f"Pass --force to overwrite it (losing any curation) or delete it first."
        )
    written: list[Path] = []
    for operation in stub.operations:
        written += gen_models.write(operation, generated_dir, format_output=False)
    resources_dir.mkdir(parents=True, exist_ok=True)
    destination.write_text(stub.source, encoding="utf-8")
    written.append(destination)
    init = resources_dir / "__init__.py"
    init.write_text(render_package_init(stub_slugs(resources_dir)), encoding="utf-8")
    written.append(init)
    if format_output:
        gen_models.run_ruff(written)
    return written


# ---------------------------------------------------------------------------
# Driving the tool off a spec_sync --diff report


def added_endpoints(report: dict[str, Any]) -> list[specs.Endpoint]:
    """Resolve a ``spec_sync.py --diff --json`` report to spec operations.

    The report names endpoints as ``"METHOD /path"`` per target, so the scope
    comes from the target key. Anything that does not resolve is dropped by
    :func:`plan_from_diff`, which reports it: the usual cause is running
    ``--from-diff`` before ``spec_sync.py --update`` has vendored the new
    operation into ``specs/``, and there is nothing to generate from an
    operation the baseline does not carry.
    """
    found: list[specs.Endpoint] = []
    targets = report.get("targets")
    if not isinstance(targets, dict):
        return found
    for scope, payload in sorted(targets.items()):
        if scope not in specs.SCOPES or not isinstance(payload, dict):
            continue
        endpoints = payload.get("endpoints")
        added = endpoints.get("added") if isinstance(endpoints, dict) else None
        for entry in added if isinstance(added, list) else []:
            method, _, path = str(entry).partition(" ")
            endpoint = specs.find_endpoint(scope, method, path)
            if endpoint is not None:
                found.append(endpoint)
    return found


def select_primary(endpoints: Sequence[specs.Endpoint]) -> list[specs.Endpoint]:
    """One stub per path: the strongest write method added on it.

    A new resource usually shows up in a diff as ``GET`` + ``POST`` +
    ``DELETE`` on one path. That is one resource, not three, so the group
    collapses to a single stub generated from the write -- the ``GET`` becomes
    its snapshot and the ``DELETE`` its compensator, which is exactly what
    :func:`generate_stub` picks up anyway.
    """
    groups: dict[tuple[str, str], list[specs.Endpoint]] = {}
    for endpoint in endpoints:
        groups.setdefault((endpoint.scope, endpoint.normalized_path), []).append(endpoint)
    chosen: list[specs.Endpoint] = []
    for key in sorted(groups):
        writes = [e for e in groups[key] if e.method in WRITE_METHODS]
        if not writes:
            continue
        writes.sort(key=lambda e: WRITE_METHODS.index(e.method))
        chosen.append(writes[0])
    return chosen


def _unresolved(report: dict[str, Any]) -> list[str]:
    """Added endpoints in the report that the vendored baseline does not carry."""
    missing: list[str] = []
    targets = report.get("targets")
    if not isinstance(targets, dict):
        return missing
    for scope, payload in sorted(targets.items()):
        if scope not in specs.SCOPES or not isinstance(payload, dict):
            continue
        endpoints = payload.get("endpoints")
        added = endpoints.get("added") if isinstance(endpoints, dict) else None
        for entry in added if isinstance(added, list) else []:
            method, _, path = str(entry).partition(" ")
            if specs.find_endpoint(scope, method, path) is None:
                missing.append(f"{scope} {method} {path}")
    return missing


# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen_plugin.py",
        description="Emit a Tier-1 Resource plugin stub for one spec write operation.",
        epilog=(
            "exit codes: 0 generated, 1 nothing to generate, 2 error.\n"
            "the stub's normalize() raises NotCurated until a human curates it; "
            "tests/test_promotion.py enforces that over every registered kind."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--scope", choices=list(specs.SCOPES))
    parser.add_argument("--method", help="HTTP method, e.g. POST")
    parser.add_argument("--path", help="spec path; a leading slash is optional for appliance paths")
    parser.add_argument(
        "--from-diff",
        metavar="REPORT",
        type=Path,
        help=(
            "a `spec_sync.py --diff --json` report ('-' for stdin): generate one "
            "stub per added write endpoint. Run `spec_sync.py --update` first -- "
            "nothing can be generated from an operation specs/ does not carry."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_RESOURCES_DIR,
        help=f"stub package directory (default: {DEFAULT_RESOURCES_DIR})",
    )
    parser.add_argument(
        "--out-bindings",
        type=Path,
        default=DEFAULT_GENERATED_DIR,
        help=f"models/bindings package directory (default: {DEFAULT_GENERATED_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the stub to stdout instead of writing"
    )
    parser.add_argument("--no-format", action="store_true", help="skip the ruff pass")
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing stub (loses hand edits)"
    )
    return parser


def _load_report(source: Path) -> dict[str, Any]:
    text = sys.stdin.read() if str(source) == "-" else source.read_text(encoding="utf-8")
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GenPluginError(f"{source}: not valid JSON ({exc})") from exc
    if not isinstance(loaded, dict):
        raise GenPluginError(f"{source}: not a spec_sync --json report")
    return loaded


def _targets(args: argparse.Namespace) -> list[specs.Endpoint]:
    if args.from_diff is not None:
        report = _load_report(args.from_diff)
        for key in _unresolved(report):
            print(
                f"gen_plugin: {key}: not in the vendored baseline; run "
                f"`python tools/spec_sync.py --update` first",
                file=sys.stderr,
            )
        return select_primary(added_endpoints(report))
    missing = [name for name in ("scope", "method", "path") if getattr(args, name) is None]
    if missing:
        raise GenPluginError(
            f"missing {', '.join('--' + name for name in missing)} (or pass --from-diff REPORT)"
        )
    endpoint = specs.find_endpoint(args.scope, args.method, args.path)
    if endpoint is None:
        raise GenPluginError(
            f"no such operation: {specs.endpoint_key(args.scope, args.method, args.path)}"
        )
    return [endpoint]


def _iter_dry_run(stub: GeneratedStub) -> Iterator[str]:
    for operation in stub.operations:
        for relative, source in operation.files.items():
            yield f"# ==> generated/{relative.as_posix()}"
            yield source
    yield f"# ==> resources/generated/{stub.path.as_posix()}"
    yield stub.source


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        endpoints = _targets(args)
    except GenPluginError as exc:
        print(f"gen_plugin: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"gen_plugin: error: {exc}", file=sys.stderr)
        return 2

    if not endpoints:
        print("gen_plugin: no write endpoint to generate a stub from", file=sys.stderr)
        return 1

    generated = 0
    for endpoint in endpoints:
        try:
            stub = generate_stub(endpoint)
        except GenPluginError as exc:
            print(f"gen_plugin: skipped: {exc}", file=sys.stderr)
            continue
        if args.dry_run:
            print("\n".join(_iter_dry_run(stub)))
            generated += 1
            continue
        try:
            written = write(
                stub,
                resources_dir=args.out,
                generated_dir=args.out_bindings,
                format_output=not args.no_format,
                force=args.force,
            )
        except GenPluginError as exc:
            print(f"gen_plugin: skipped: {exc}", file=sys.stderr)
            continue
        except OSError as exc:
            print(f"gen_plugin: error: {exc}", file=sys.stderr)
            return 2
        for path in written:
            print(path)
        print(
            f"gen_plugin: {endpoint.key} -> {stub.kind} "
            f"(tier 1, {stub.reversibility.lower()}, "
            f"{len(stub.operations)} binding(s))"
        )
        generated += 1
    return 0 if generated else 1


if __name__ == "__main__":
    sys.exit(main())
