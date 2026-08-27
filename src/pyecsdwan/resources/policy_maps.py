"""QoS / optimization / route policy maps — appliance-scope (#33).

Endpoint correction (the issue's own paths do not exist)
--------------------------------------------------------

Issue #33 names ``/route_policy`` and ``/optimization_policy``. Neither path
exists in either vendored spec. What does exist:

* **Orchestrator** (``specs/orchestrator-openapi-7.2.0.json``): ``/qosMaps``,
  ``/optimizationMaps``, ``/routeMaps`` — all **GET-only**. There is no
  orchestrator-scope write surface for these maps at all.
* **Appliance (ECOS)**, reached through the Orchestrator's appliance proxy
  (``ctx.client.appliance_request``, the channel ``resources/routes.py`` /
  ``resources/vrrp.py`` / ``resources/appliance_zones.py`` already use):
  ``GET/POST qosMaps``, ``GET/POST optimizationMaps``, ``GET/POST routeMaps``
  — plus per-map ``{mapName}`` GET/DELETE, ``deleteMultiple``,
  ``qosMaps/defaultRules`` / ``optimizationMaps/defaultRules`` (read-only
  factory rules) and ``qosMaps/dscpOverride``.

So these are **appliance-scope** resources, not orchestrator-scope as the
issue implies. Only the whole-table ``GET``/``POST`` pair is used here: the
POST is a full replace with ``merge: false``, which makes each resource a
clean snapshot/restore REVERSIBLE unit. The per-map DELETE and
``deleteMultiple`` endpoints are deliberately unused — removing a map is
expressed as its absence from the replacement table, so one write is the
whole operation and rollback is one symmetric write back.

Payload shape (live-captured read-only this session, all three populated)
-------------------------------------------------------------------------

All three endpoints share one envelope, confirmed both live and by the
appliance spec (``QosPolicyObject`` / ``OptimizationPolicyObject`` /
``RoutePolicyObject`` are structurally identical)::

    {"options": {"merge": true, "activeMap": "map1", "templateApply": false},
     "data": {"<mapName>": {"prio": {"<priority>": {
         "match": {...}, "set": {...}, "comment": ""}}}}}

Note this is one nesting level **shallower** than the securityMaps envelope
``resources/security_policy.py`` writes (``map -> zonePair -> prio -> rule``
vs. ``map -> prio -> rule``): there is no zone-pair level here.
Consequently this module reuses ``security_policy._strip_meta`` verbatim (it
is recursive and shape-agnostic — it drops ``self`` and ``gms_marked``
wherever they appear) but **cannot** reuse ``security_policy._inject_self``,
which hardcodes the zone-pair level and would write ``self: "prio"`` into
these bodies. :func:`_inject_self_maps` below is the same technique at the
right depth. Re-injecting a ``self`` echo the server did not ask for is
harmless passthrough (same reasoning as ``appliance_zones.py``); omitting one
the server *does* expect is not, so the write side injects at both the map
and the rule level.

``activeMap`` — the judgement call
-----------------------------------

The spec is explicit about what this field does: *"After POST operation, the
name of the map to activate as the current policy map."* It is not a read
echo — it is a **write directive**, and the appliance acts on whatever value
(or absence) the POST body carries. ``security_policy.py`` sends only
``{merge, templateApply}``, which is safe for *its* endpoint but would be
actively destructive here: an operator editing one rule would ship an
``options`` block with no ``activeMap``, and the map that was live before the
edit may not be live after it.

This module therefore treats ``activeMap`` as **part of desired state**, and
guards the omission case:

1. ``normalize()`` lifts ``options.activeMap`` out of the envelope into the
   canonical top level (``{"activeMap": ..., "maps": {...}}``). Which map is
   live is real, operator-visible configuration, so a change to it belongs in
   the plan output, gets snapshotted into the journal, and is restored by
   ``rollback()`` like any other field. ``merge`` and ``templateApply`` are
   *not* lifted: those are per-write transport directives, not state, and are
   dropped on read and re-stated on every write.
2. ``canonicalize_desired()`` back-fills the server's current ``activeMap``
   when the operator's intent does not name one (the ``replace``-mode
   ``set_desired`` path, where the candidate store does not merge current
   state in for us). An edit that says nothing about which map is active
   therefore keeps the one that already was — it cannot deactivate it by
   silence.
3. ``apply()``/``rollback()`` **omit the ``activeMap`` key entirely** when the
   resolved value is empty, rather than sending ``""``. A blank string is a
   value the appliance would act on; an absent key is not. This is also the
   whole-resource-delete path: posting an empty map table while naming a map
   to activate would name a map that no longer exists.

The one deliberate asymmetry: a table with no maps normalizes to ``None``
(absent) regardless of any ``activeMap`` the server still reports, because an
``activeMap`` naming a map that does not exist is not state worth diffing —
and because ``verify()`` after a whole-resource delete compares against a
``None`` desired (same reasoning ``appliance_zones.py`` and ``vrrp.py``
record for their empty tables).

Ownership
---------

``managed_by()`` prefers the per-object ``gms_marked`` flag when the fetched
table carries one — the "gms_marked first, template-section fallback" pattern
``resources/routes.py`` (#15) established — and otherwise falls back to the
template association x selection join in ``ownership.py``. The section names
(``qosMaps`` / ``optimizationMaps`` / ``routeMaps``) follow the ECOS-path
convention that was CONFIRMED for ``securityMaps``, but were not themselves
confirmed against a live Default Template Group this session; they are marked
UNVERIFIED in ``ownership.KIND_TO_TEMPLATE_SECTIONS`` accordingly.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import structlog

from pyecsdwan import ownership
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
from pyecsdwan.resources.security_policy import _strip_meta

log = structlog.get_logger("pyecsdwan.resources.policy_maps")

#: Envelope keys that are transport, not state — never part of canonical form.
_META_KEYS = ("options", "settings", "activeMap")


def _inject_self_maps(maps: Mapping[str, Any]) -> dict[str, Any]:
    """Re-add the ``self`` echoes the ECOS policy-map bodies carry.

    Same technique as ``security_policy._inject_self`` but at this envelope's
    depth (``map -> prio -> rule``, with no zone-pair level) — see the module
    docstring for why that helper cannot be reused verbatim here.
    """
    out: dict[str, Any] = {}
    for map_name, map_body in maps.items():
        new_map: dict[str, Any] = {"self": map_name}
        if not isinstance(map_body, Mapping):
            out[map_name] = map_body
            continue
        for key, value in map_body.items():
            if key == "self":
                continue
            if key != "prio" or not isinstance(value, Mapping):
                new_map[key] = copy.deepcopy(value)
                continue
            rules: dict[str, Any] = {}
            for priority, rule in value.items():
                if isinstance(rule, Mapping):
                    new_rule = copy.deepcopy(dict(rule))
                    new_rule.setdefault(
                        "self", int(priority) if str(priority).isdigit() else priority
                    )
                    rules[str(priority)] = new_rule
                else:
                    rules[str(priority)] = copy.deepcopy(rule)
            new_map["prio"] = rules
        out[map_name] = new_map
    return out


def _split_envelope(raw: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return ``(activeMap, maps)`` from any of the shapes we may be handed.

    Three inputs reach ``normalize()``: the server envelope
    (``{options: {activeMap}, data: {...}}``), this module's own canonical
    form (``{activeMap, maps}``) round-tripping back through, and a bare map
    table an operator may author directly. Precedence is explicit so a map
    that happens to be *named* ``data`` or ``maps`` cannot be mistaken for a
    wrapper.
    """
    active = ""
    options = raw.get("options")
    if isinstance(options, Mapping):
        active = str(options.get("activeMap") or "")
    if not active and isinstance(raw.get("activeMap"), str):
        active = raw["activeMap"]

    data = raw.get("data")
    if isinstance(data, Mapping):
        return active, dict(data)
    maps = raw.get("maps")
    if isinstance(maps, Mapping):
        return active, dict(maps)
    return active, {k: v for k, v in raw.items() if k not in _META_KEYS}


def _has_gms_marked(node: Any) -> bool:
    """Recursive: did the fabric flag anything in this table as its own?

    Shape-agnostic on purpose — ``gms_marked`` is confirmed on securityMaps
    rules and on route entries (#15, #19) and these policy tables share that
    rule shape, but this session's captures did not reach far enough into the
    per-rule bodies to pin the exact level it sits at here.
    """
    if isinstance(node, Mapping):
        if bool(node.get("gms_marked")):
            return True
        return any(_has_gms_marked(v) for k, v in node.items() if k != "gms_marked")
    if isinstance(node, list):
        return any(_has_gms_marked(v) for v in node)
    return False


def _rule_count(maps: Mapping[str, Any]) -> int:
    total = 0
    for map_body in maps.values():
        if isinstance(map_body, Mapping) and isinstance(map_body.get("prio"), Mapping):
            total += len(map_body["prio"])
    return total


class _PolicyMaps(Resource):
    """Shared implementation for the three identically-shaped map endpoints.

    Concrete subclasses set only ``kind``, ``ecos_path`` and ``label``: the
    ``data``/``options`` envelope, the ``activeMap`` handling and the
    full-replace write are the same contract on all three (confirmed live and
    by the appliance spec — see module docstring), so they share one curated
    code path rather than three near-copies that could drift apart.
    """

    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: No ordering constraint was confirmed this session. These tables' rules
    #: can reference objects other resources own (interfaces, labels), but
    #: nothing observed makes a *create* here fail against a fresh appliance,
    #: so no dependency is invented. Revisit if a live ordering failure shows
    #: one.
    dependencies: tuple[str, ...] = ()
    #: The table can legitimately hold no maps at all, and a full-replace POST
    #: of an empty table is a real, reversible operation.
    deletable = True

    #: ECOS path under the appliance proxy, e.g. "qosMaps".
    ecos_path: str = ""
    #: Human label used in operator-facing messages.
    label: str = ""

    @staticmethod
    def _ne_pk(ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{ref.kind} is appliance-scoped; ref.appliance is required")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side -------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.appliance_request("GET", self._ne_pk(ctx, ref), self.ecos_path)
        if raw is None or isinstance(raw, dict):
            return raw
        raise ValueError(f"unexpected {self.ecos_path} response shape from {ref}: {raw!r}")

    def normalize(self, raw: RawState) -> CanonicalState:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError(
                f"{self.ecos_path} state must be a mapping, got {type(raw).__name__}"
            )
        active, maps_raw = _split_envelope(raw)
        stripped = _strip_meta(maps_raw)
        assert isinstance(stripped, dict)
        # A server that wraps its bookkeeping alongside `data` must not leak
        # it into the canonical maps (same guard security_policy.py carries).
        for meta_key in _META_KEYS:
            stripped.pop(meta_key, None)
        for map_name, map_body in stripped.items():
            if not isinstance(map_body, Mapping):
                raise ValueError(
                    f"{self.ecos_path} map {map_name!r} must be a mapping "
                    f"({{prio: {{...}}}}), got {type(map_body).__name__}"
                )
        if not stripped:
            # No maps == absent, activeMap or not: an activeMap naming a map
            # that does not exist is not diffable state, and a whole-resource
            # delete's verify() compares against a None desired.
            return None
        ordered = {name: stripped[name] for name in sorted(stripped)}
        canonical: dict[str, Any] = {"maps": ordered}
        if active:
            canonical["activeMap"] = active
        return canonical

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """Normalize user intent, back-filling ``activeMap`` when unstated.

        ``replace``-mode intent (``set_desired`` / a whole YAML document)
        reaches us without the server's current state merged in, so an edit
        that says nothing about ``activeMap`` would otherwise plan as
        "deactivate whatever is live". Inheriting the server's current value
        makes silence mean "leave it alone" — see the module docstring.
        """
        canonical = self.normalize(dict(desired))
        if canonical is None:
            return None
        assert isinstance(canonical, dict)
        if not canonical.get("activeMap"):
            server = self.normalize(self.fetch(ctx, ref))
            inherited = server.get("activeMap") if isinstance(server, dict) else None
            if inherited:
                canonical["activeMap"] = inherited
                log.debug(
                    "policy_map_active_map_inherited",
                    ref=str(ref),
                    ecos_path=self.ecos_path,
                    active_map=inherited,
                )
        return canonical

    # -- write side ------------------------------------------------------------

    def _body(self, canonical: CanonicalState) -> dict[str, Any]:
        state = canonical if isinstance(canonical, dict) else {}
        maps = state.get("maps") or {}
        options: dict[str, Any] = {
            # merge=False: this body is the complete map table for the
            # appliance. templateApply=False per the spec's own note ("only
            # used by Orchestrator when applying policy templates; other
            # users must set it to false").
            "merge": False,
            "templateApply": False,
        }
        active = state.get("activeMap")
        if active:
            # Only ever send a real map name. An empty string is a value the
            # appliance acts on; an absent key leaves the live map alone.
            options["activeMap"] = str(active)
        return {"data": _inject_self_maps(maps), "options": options}

    def _write(
        self, ctx: Ctx, ref: Ref, canonical: CanonicalState, verb: str
    ) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        body = self._body(canonical)
        ctx.client.appliance_request("POST", ne_pk, self.ecos_path, json_body=body)
        rules = _rule_count(body["data"])
        active = body["options"].get("activeMap")
        log.debug(
            "policy_map_write",
            ref=str(ref),
            ecos_path=self.ecos_path,
            verb=verb,
            maps=len(body["data"]),
            rules=rules,
            active_map=active,
        )
        active_note = f", activeMap={active}" if active else ", activeMap unchanged"
        message = (
            f"{self.label} {verb} on {ne_pk}: {len(body['data'])} map(s), "
            f"{rules} rule(s){active_note}"
        )
        outcome = ctx.save_changes([ne_pk], f"{self.label} {verb}: {ref}")
        if outcome.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                jobs=[outcome],
                message=(
                    f"{message} — not persisted, "
                    f"save-changes {outcome.state}: {outcome.detail}"
                ),
            )
        return ApplyResult(ok=True, jobs=[outcome], message=f"{message}, persisted")

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        return self._write(ctx, diff.ref, diff.desired, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        # normalize() lifts the snapshot's activeMap into canonical state, so
        # a rollback restores which map was live as well as the rules.
        return self._write(ctx, ref, self.normalize(snapshot), "rollback")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name="global", appliance=name)
            for name in ctx.resolver.appliance_names()
        ]

    # -- ownership -------------------------------------------------------------

    def managed_by(self, ctx: Ctx, ref: Ref) -> str | None:
        ne_pk = self._ne_pk(ctx, ref)
        # Per-object precision first (routes.py #15 pattern): anything the
        # fabric itself flagged gms_marked is server-owned regardless of what
        # any template selects.
        if _has_gms_marked(self.fetch(ctx, ref)):
            return f"gms (gms_marked entry present in {self.ecos_path} on this appliance)"
        return ownership.owning_group(ctx, self.kind, ne_pk)


def _desired_doc(ecos_path: str) -> str:
    return (
        "maps: {mapName: {prio: {priority: {match, set, comment}}}}, plus "
        "activeMap: <mapName> naming the map to make live. Full-table replace "
        f"against ECOS '{ecos_path}' with options "
        "{merge: false, templateApply: false}, persisted via save-changes. "
        "activeMap is part of desired state: omit it and the map that is "
        "currently live stays live (it is inherited from the appliance at "
        "plan time, never blanked)."
    )


class QosMaps(_PolicyMaps):
    kind = "appliance/qos-map"
    ecos_path = "qosMaps"
    label = "qos maps"
    desired_state_doc = _desired_doc("qosMaps")
    endpoints = (
        "appliance GET /qosMaps",
        "appliance POST /qosMaps",
        "appliance GET /qosMaps/defaultRules",
    )


class OptimizationMaps(_PolicyMaps):
    kind = "appliance/optimization-map"
    ecos_path = "optimizationMaps"
    label = "optimization maps"
    desired_state_doc = _desired_doc("optimizationMaps")
    endpoints = (
        "appliance GET /optimizationMaps",
        "appliance POST /optimizationMaps",
        "appliance GET /optimizationMaps/defaultRules",
    )


class RouteMaps(_PolicyMaps):
    kind = "appliance/route-map"
    ecos_path = "routeMaps"
    label = "route maps"
    desired_state_doc = _desired_doc("routeMaps")
    #: No defaultRules endpoint exists for route maps (see default_rules()).
    endpoints = (
        "appliance GET /routeMaps",
        "appliance POST /routeMaps",
    )


# -- read-only views ----------------------------------------------------------


def default_rules(ctx: Ctx, appliance: str, ecos_path: str) -> dict[str, Any]:
    """Factory default rules for ``qosMaps`` / ``optimizationMaps``.

    Read-only (the endpoints expose GET only) and never part of the diffable
    canonical state — an operator cannot "set" the factory rules. Exposed for
    `show`/audit, same role ``routes.all_routes`` plays for learned routes.
    """
    if ecos_path not in ("qosMaps", "optimizationMaps"):
        raise ValueError(
            f"defaultRules exists only for qosMaps/optimizationMaps, got {ecos_path!r}"
        )
    ne_pk = ctx.resolver.ne_pk_for(appliance)
    raw = ctx.client.appliance_request("GET", ne_pk, f"{ecos_path}/defaultRules")
    return raw if isinstance(raw, dict) else {}


register(QosMaps())
register(OptimizationMaps())
register(RouteMaps())
