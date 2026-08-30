"""Overlay route-map priority and template-group apply order (Phase 3, #36).

Priority/ordering is configuration in its own right: which overlay's route map
wins, and which template group is applied last (and therefore wins). Modeling
each as a resource makes a priority change diffable, plannable and revertible
instead of an untracked side effect of some other edit.

Two genuinely different wire shapes live here — a keyed map and an ordered
list — so this module carries two resources with different ``normalize()``
logic rather than one generic one.

**1. ``overlay-priority`` — GET/POST ``/gms/overlays/priority``**

The wire shape is a JSON object whose **key is the priority and whose value is
the overlay id**::

    {"1": 5, "2": 6, "3": 7}     # overlay 5 first, 6 second, 7 third

That direction is stated three times over, by three independent sources: the
vendored ``specs/orchestrator-openapi-7.2.0.json`` (both the GET and POST
descriptions: "the key is the priority, and the value of the key is the
overlay ID with that priority"), the vendored SDK
(``pyedgeconnect/orch/_overlays.py``: ``get_overlays_priorities`` /
``set_overlays_priorities`` — "Keys are overlay priority, values are the
overlay id numbers"), and this repo's own endpoint research
(``docs/research/templates-overlays-security.md`` §Business Intent Overlays:
"``{"1": overlayId, ...}`` — priority→id map"). Issue #36 quotes a live
capture of ``{"1": 1, "2": 2, "3": 3, "4": 4}`` as "overlayId → priority", but
that capture is the identity permutation and so reads the same in either
direction — it is not evidence against the documented one. The documented
direction is what this module implements; if a live fabric ever shows a
*non*-identity map that contradicts it, only ``_overlay_priority_map()``'s
labelling and error text need to change, not the shape (see below).

That is because either reading imposes the *same* structural rule, which is
what validation actually enforces: the map is a bijection. Priorities are
unique because they are JSON object keys, and overlay ids must be unique
because — in the vendored SDK's own words — "each overlay ID must have a
unique priority". ``normalize()`` therefore rejects a repeated overlay id
before the write ever leaves the process, naming both colliding priorities and
(when the resolver can supply it) the overlay's name, so the operator gets a
pre-flight error instead of a raw 4xx from the API.

User intent may address overlays by **name**: any non-integer value is
resolved through ``ctx.resolver.overlay_id_for()`` in
``canonicalize_desired()`` (canonical state always speaks server ids, exactly
like ``bio-association`` in ``resources/overlays.py``). An overlay whose name
is itself a plain integer can only be addressed by id — the same
name-or-id ambiguity ``Resolver.ne_pk_for`` resolves in favor of the id.

**2. ``template-group-priority`` — GET/POST ``/template/templateGroupsPriorities``**

The wire shape is an ordered list under one key::

    {"priorities": ["Default Template Group", "Branch-Std"]}

**Order IS the data here — this list is deliberately NOT sorted by
``normalize()``.** That is the opposite of the "sort lists by a stable key"
rule ``contract.py`` states and every other plugin in this package follows,
and it is not an oversight: sorting it alphabetically would silently rewrite
the operator's apply order into a different configuration and make a genuine
reorder undiffable. ``diffing.structural_diff`` compares lists positionally,
so a reorder shows up as per-index replaces — which is exactly right for this
resource and only works because the order survives normalization. Do not
"fix" this into a sort.

The only list normalization performed is validation: entries must be
non-empty group names, and a name may not appear twice (a duplicate would make
the apply order ambiguous, and the same pre-flight-beats-4xx argument as above
applies). Group names are **not** checked for existence:
``canonicalize_desired()`` runs at plan time, before anything in the changeset
has been applied, so a group being created by an earlier item of the same
changeset (``dependencies = ("template-group",)``) does not exist yet and an
existence check would reject a perfectly valid plan.

*SDK divergence, defensive*: the vendored SDK's
``set_template_groups_priorities`` posts ``{"templateIds": [...]}`` — and does
so via ``self._get``.
Both look like bugs: the OpenAPI schema (``TemplateGroupPriorities``) and the
live GET response both use ``{"priorities": [...]}``, which is what this
module writes. A ``templateIds`` key supplied as *input* (hand-written YAML
copied from the SDK) is folded onto ``priorities`` so it round-trips rather
than diffing as phantom drift.

**Shared semantics**

* *Full overwrite*: POST replaces the whole priority structure — there is no
  per-entry PATCH for either endpoint. Neither resource ever constructs a
  partial object: ``txn.build_plan`` merges any ``set``/``delete`` intent onto
  the freshly fetched and normalized current state before
  ``canonicalize_desired()`` ever sees it (the same merge-then-normalize
  guarantee ``resources/zones.py`` and ``resources/loopback.py`` rely on), so
  ``diff.desired`` is the complete structure by construction and a full POST
  of it can never drop an untouched entry.
* Both are singletons (instance name ``global``) with ``deletable = False``:
  there is no "the priority order does not exist" state — an empty structure
  is a legitimate "nothing prioritized" reading, not absence. Remove
  individual entries instead of the resource.
* Both are ``Reversibility.REVERSIBLE``: the pre-change structure is captured
  by the ordinary GET snapshot and restored through the same POST write path.
* Both accept either the wire shape or the canonical ``{"priorities": ...}``
  wrapper as ``normalize()`` input, so ``normalize(normalize(x)) ==
  normalize(x)`` holds and hand-written YAML can use either.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import structlog

from pyecsdwan.client import OrchApiError
from pyecsdwan.contract import (
    ApplyResult,
    CanonicalState,
    Ctx,
    Diff,
    RawState,
    Ref,
    Resource,
    Reversibility,
    Scope,
    Tier,
)
from pyecsdwan.registry import register
from pyecsdwan.resolver import ResolveError

log = structlog.get_logger("pyecsdwan.resources.priorities")

_OVERLAY_PRIORITY_PATH = "/gms/overlays/priority"
_TEMPLATE_GROUP_PRIORITY_PATH = "/template/templateGroupsPriorities"

#: The single canonical top-level key both resources hang their state off.
#: It is also the real wire key for template-group priorities; for overlay
#: priorities it is this module's wrapper (the wire shape is a bare map).
_PRIORITIES = "priorities"

#: SDK-shaped alias for the template-group list (see module docstring).
_TEMPLATE_IDS_ALIAS = "templateIds"

_INT_RE = re.compile(r"[+-]?\d+")

_INSTANCE_NAME = "global"


def _as_int(value: Any, what: str) -> int:
    """Integer coercion matching the CLI's ``_coerce_value`` strictness.

    Plain base-10 only: no ``1_000``, no unicode digits, no bools (which are
    ints in Python and would silently become 0/1 priorities).
    """
    if isinstance(value, bool):
        raise ValueError(f"{what} must be an integer, got a boolean ({value!r})")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        # The OpenAPI schema types overlay ids as "number"; a JSON float that
        # is exactly integral is the same id, so accept it rather than fail.
        return int(value)
    if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        return int(value.strip())
    raise ValueError(f"{what} must be an integer, got {value!r}")


# == 1. overlay priority (#36, priority -> overlay id) ========================


def _overlay_section(raw: RawState) -> Mapping[str, Any]:
    """Accept either the wire map or the canonical ``{"priorities": {...}}``.

    Wire keys are always integers (priorities), so a literal ``priorities``
    key can only ever be this module's own wrapper — the two shapes cannot
    collide.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(
            "overlay priority state must be a mapping of priority -> overlay id, "
            f"got {type(raw).__name__}"
        )
    if _PRIORITIES in raw:
        inner = raw[_PRIORITIES] or {}
        if not isinstance(inner, Mapping):
            raise ValueError(
                "overlay priority 'priorities' must be a mapping of priority -> "
                f"overlay id, got {type(inner).__name__}"
            )
        return inner
    return raw


def _overlay_label(overlay_id: int, labels: Mapping[int, str] | None) -> str:
    name = (labels or {}).get(overlay_id)
    return f"id {overlay_id} ({name})" if name else f"id {overlay_id}"


def _overlay_priority_map(
    entries: Mapping[str, Any], labels: Mapping[int, str] | None = None
) -> dict[str, int]:
    """Validate + canonicalize a priority -> overlay-id map.

    Canonical form: keys are the decimal priority (leading zeros stripped),
    values are integer overlay ids, ordered by ascending priority. The map
    must be a bijection — see the module docstring — so a repeated overlay id
    is rejected here, before any write leaves the process.
    """
    out: dict[str, int] = {}
    claimed_by: dict[int, int] = {}
    for key, value in entries.items():
        priority = _as_int(key, f"overlay priority key {key!r}")
        overlay_id = _as_int(value, f"overlay id at priority {key!r}")
        canonical_key = str(priority)
        if canonical_key in out:
            raise ValueError(
                f"duplicate overlay priority {canonical_key} after key canonicalization "
                f"(e.g. '01' and '1' are the same priority)"
            )
        if overlay_id in claimed_by:
            raise ValueError(
                f"overlay {_overlay_label(overlay_id, labels)} is assigned two priorities "
                f"({claimed_by[overlay_id]} and {priority}); each overlay must hold exactly "
                f"one priority and each priority exactly one overlay — "
                f"POST {_OVERLAY_PRIORITY_PATH} would reject this"
            )
        claimed_by[overlay_id] = priority
        out[canonical_key] = overlay_id
    return {key: out[key] for key in sorted(out, key=int)}


def _overlay_labels(ctx: Ctx) -> dict[int, str]:
    """``{overlayId: name}`` for error messages only; never part of state.

    Best effort: a resolver/API failure here must not turn a validation error
    into an unrelated traceback, so it degrades to id-only messages.
    """
    try:
        overlays = ctx.resolver.overlays()
    except (OrchApiError, ResolveError):
        return {}
    labels: dict[int, str] = {}
    for overlay in overlays:
        if not isinstance(overlay, Mapping) or not overlay.get("name"):
            continue
        try:
            labels[_as_int(overlay.get("id"), "overlay id")] = str(overlay["name"])
        except ValueError:
            continue
    return labels


class OverlayPriority(Resource):
    kind = "overlay-priority"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: An overlay must exist before it can be prioritized, so a changeset that
    #: creates one and orders it in the same commit applies `bio` first.
    dependencies = ("bio",)
    #: Singleton: an empty map is "nothing prioritized", not absence — there
    #: is no whole-resource delete. Remove individual priorities instead.
    deletable = False
    desired_state_doc = (
        "priorities: map of priority -> overlay (e.g. {'1': 'CorpFabric', "
        "'2': 6}) — the KEY is the priority (1 = applied first), the VALUE is "
        "the overlay, given as a server id or an overlay name resolved at "
        "plan time. Each overlay may hold exactly one priority. Full "
        "overwrite: POST /gms/overlays/priority always carries the whole map."
    )
    endpoints = (
        "orchestrator GET /gms/overlays/priority",
        "orchestrator POST /gms/overlays/priority",
    )

    # -- read side ------------------------------------------------------------

    def write_target(self, ctx: Ctx, ref: Ref) -> str | None:
        """The single overlay-priority object (#69). Orchestrator-wide, so the
        path identifies it and no instance qualifier applies."""
        return f"orchestrator {_OVERLAY_PRIORITY_PATH}"

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.get(_OVERLAY_PRIORITY_PATH)
        return raw if isinstance(raw, dict) else {}

    def normalize(self, raw: RawState) -> CanonicalState:
        return {_PRIORITIES: _overlay_priority_map(_overlay_section(raw))}

    # -- desired-state shaping -------------------------------------------------

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """Resolve overlay names to server ids, then validate + canonicalize.

        Canonical state speaks ids (stable across renames), user intent may
        speak either — the same split ``bio-association`` uses.
        """
        entries = _overlay_section(dict(desired))
        labels = _overlay_labels(ctx)
        resolved: dict[str, Any] = {}
        for key, value in entries.items():
            resolved[str(key)] = self._resolve_overlay(ctx, value)
        return {_PRIORITIES: _overlay_priority_map(resolved, labels)}

    @staticmethod
    def _resolve_overlay(ctx: Ctx, value: Any) -> Any:
        """Overlay name -> id; an integer (or integer string) passes through."""
        if isinstance(value, str) and not _INT_RE.fullmatch(value.strip()):
            return _as_int(
                ctx.resolver.overlay_id_for(value.strip()),
                f"resolved overlay id for {value.strip()!r}",
            )
        return value

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired if isinstance(diff.desired, dict) else {}
        # Already the complete map (build_plan merges intent onto fetched
        # state first — see module docstring), so this is never partial.
        payload = _overlay_priority_map(_overlay_section(desired))
        ctx.client.post(_OVERLAY_PRIORITY_PATH, payload)
        log.debug("overlay_priority_apply", priorities=payload)
        return ApplyResult(
            ok=True,
            message=(
                f"overlay priority order replaced ({len(payload)} overlay(s): "
                f"{_order_summary(payload)})"
            ),
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # An absent snapshot must never replay as "POST an empty map" —
            # that would wipe the fabric's route-map ordering. Refuse loudly,
            # same as zones/loopback-orch.
            return ApplyResult(
                ok=False,
                message="no usable snapshot; refusing to POST an empty overlay priority map",
            )
        restored = _overlay_priority_map(_overlay_section(snapshot))
        ctx.client.post(_OVERLAY_PRIORITY_PATH, restored)
        return ApplyResult(ok=True, message="overlay priority order restored from snapshot")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name=_INSTANCE_NAME)]


def _order_summary(payload: Mapping[str, int]) -> str:
    return ", ".join(f"{key}={payload[key]}" for key in sorted(payload, key=int))


register(OverlayPriority())


# == 2. template-group apply order (#36, ordered list) ========================


def _template_section(raw: RawState) -> Sequence[Any]:
    """Accept the wire object, a bare list, or the SDK's ``templateIds`` key."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError(
            "template-group priority state must be an object carrying a "
            f"'{_PRIORITIES}' list, got {type(raw).__name__}"
        )
    if not raw:
        return []
    for key in (_PRIORITIES, _TEMPLATE_IDS_ALIAS):
        if key in raw:
            inner = raw[key] or []
            if not isinstance(inner, list):
                raise ValueError(
                    f"template-group priority '{key}' must be a list of group names, "
                    f"got {type(inner).__name__}"
                )
            return inner
    raise ValueError(
        f"template-group priority state must carry a '{_PRIORITIES}' list; "
        f"got keys: {', '.join(sorted(str(k) for k in raw))}"
    )


def _template_priority_list(entries: Sequence[Any]) -> list[str]:
    """Validate an ordered apply-order list. **The order is never sorted.**

    Position is the configuration (see module docstring): sorting here would
    rewrite the operator's intent and make a genuine reorder undiffable. The
    only rules enforced are that entries are non-empty group names and that
    no group appears twice (a duplicate makes the apply order ambiguous).
    """
    out: list[str] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"template-group priority entry {index} must be a non-empty group name, "
                f"got {entry!r}"
            )
        name = entry.strip()
        if name in seen:
            raise ValueError(
                f"template group {name!r} appears twice in the apply order "
                f"(positions {seen[name]} and {index}); each group must be listed "
                f"exactly once — POST {_TEMPLATE_GROUP_PRIORITY_PATH} would otherwise "
                f"apply an ambiguous order"
            )
        seen[name] = index
        out.append(name)
    return out


class TemplateGroupPriority(Resource):
    kind = "template-group-priority"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: A group must exist before it can be ordered, so a changeset creating a
    #: group and placing it in the apply order applies `template-group` first.
    dependencies = ("template-group",)
    #: Singleton: an empty list is "no deterministic order", not absence.
    deletable = False
    desired_state_doc = (
        "priorities: ORDERED list of template group names, first applied "
        "first (e.g. ['Default Template Group', 'Branch-Std']). Order is the "
        "configuration — it is never sorted, and reordering the same names is "
        "a real change. Each group may appear at most once; groups left out "
        "are applied in no deterministic order. Full overwrite: POST "
        "/template/templateGroupsPriorities always carries the whole list."
    )
    endpoints = (
        "orchestrator GET /template/templateGroupsPriorities",
        "orchestrator POST /template/templateGroupsPriorities",
    )

    # -- read side ------------------------------------------------------------

    def write_target(self, ctx: Ctx, ref: Ref) -> str | None:
        """The single template-group-priority object (#69). A separate object
        from `OverlayPriority` in this module — same concept, different
        endpoint, so the two are not a conflict."""
        return f"orchestrator {_TEMPLATE_GROUP_PRIORITY_PATH}"

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.get(_TEMPLATE_GROUP_PRIORITY_PATH)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, list):
            return {_PRIORITIES: raw}
        return {_PRIORITIES: []}

    def normalize(self, raw: RawState) -> CanonicalState:
        return {_PRIORITIES: _template_priority_list(_template_section(raw))}

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired if isinstance(diff.desired, dict) else {}
        # Complete list by construction (build_plan merge, see docstring).
        order = _template_priority_list(_template_section(desired))
        ctx.client.post(_TEMPLATE_GROUP_PRIORITY_PATH, {_PRIORITIES: order})
        log.debug("template_group_priority_apply", priorities=order)
        return ApplyResult(
            ok=True,
            message=(
                f"template group apply order replaced ({len(order)} group(s): "
                f"{' -> '.join(order) or 'none'})"
            ),
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            return ApplyResult(
                ok=False,
                message="no usable snapshot; refusing to POST an empty template group order",
            )
        restored = _template_priority_list(_template_section(snapshot))
        ctx.client.post(_TEMPLATE_GROUP_PRIORITY_PATH, {_PRIORITIES: restored})
        return ApplyResult(ok=True, message="template group apply order restored from snapshot")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name=_INSTANCE_NAME)]


register(TemplateGroupPriority())
