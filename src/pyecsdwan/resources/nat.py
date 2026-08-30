"""NAT policy maps, NAT pools and inter-segment S-NAT / D-NAT (#32).

Endpoint correction (the paths issue #32 names do not exist)
------------------------------------------------------------

``/nat_policy``, ``/vrf_snat_maps`` and ``/vrf_dnat_maps`` appear in neither
vendored spec — those are *module* names in the vendored pyedgeconnect SDK
(``pyedgeconnect/orch/_nat_policy.py`` etc.), not REST paths. The real
surface, verified against ``specs/orchestrator-openapi-7.2.0.json`` and
``specs/appliance-openapi-7.2.0.json`` and probed read-only against a live
lab Orchestrator this session:

* Orchestrator scope, **GET-only** (read-through caches of appliance state):
  ``/nat``, ``/natMaps``, ``/natAll``, ``/natMapsDynamic``, ``/snatMaps``,
  ``/dnatMaps``, ``/nat/maps``, ``/nat/natPools``. Nothing writable.
* Orchestrator scope, **GET + POST**: ``/vrf/config/snatMaps`` — the single
  writable NAT endpoint on the Orchestrator API.
* Appliance (ECOS) scope through the Orchestrator proxy
  (``ctx.client.appliance_request``, the channel ``resources/routes.py`` and
  ``resources/appliance_zones.py`` use): ``natMaps`` (GET+POST),
  ``natMaps/deleteMultiple`` (POST), ``natMaps/{mapName}`` (GET+DELETE),
  ``nat/maps`` + ``nat/maps/{mapName}[/prio/{prio}]``, ``nat/natPools`` +
  ``nat/natPools/{id}``, ``vrf/config/snatMaps``.

So NAT is **primarily an appliance-scope resource**, not the
orchestrator-scope one issue #32 implies. Three resources are curated here:

=========================  =========  ===============================
kind                       scope      endpoint
=========================  =========  ===============================
``appliance/nat-maps``     appliance  ECOS ``natMaps``
``appliance/nat-pools``    appliance  ECOS ``nat/natPools``
``snat-maps``              orch       ``/vrf/config/snatMaps``
=========================  =========  ===============================

D-NAT has **no write endpoint anywhere** (``/dnatMaps`` is GET-only on the
Orchestrator and absent from the appliance spec), so it is exposed here as
the read-only view :func:`inter_segment_dnat_maps` rather than a resource —
an operator cannot "set" what the API will not accept.

Branch NAT (``nat/maps``, schema ``BranchNatMaps``) is a *second*, distinct
appliance write surface with per-rule granularity
(``nat/maps/{mapName}/prio/{prio}``). It is deliberately not curated in this
pass: ``natMaps`` is the live-confirmed policy-map table and the one
``ownership.KIND_TO_TEMPLATE_SECTIONS`` already knows about. See the futures
note in the issue thread.

Confirmed live shapes (captured read-only this session)
------------------------------------------------------

``natMaps`` — POPULATED, high confidence. The ``data``/``options`` envelope::

    {"data": {"map1": {"self": "map1", "prio": {"<prio>": {<rule>}}}},
     "options": {...}}

i.e. map name -> ``{self, prio: {priority: rule}}``, wrapped in ``data``,
with a sibling ``options`` block. This is structurally the *same* envelope
``resources/security_policy.py`` writes to, one nesting level shallower
(no ``<fromZone>_<toZone>`` level between the map and its ``prio`` table).

SPEC-DERIVED, not live-confirmed (both endpoints answered ``{}`` on the lab):

* ``nat/natPools`` — ``NATPools``: ``{"<poolId>": {name, subnet, dir, pat,
  comment}}``; ``dir`` in ``{outbound, inbound}``, ``pat`` 0/1.
* ``/vrf/config/snatMaps`` — ``SnatMaps``: ``{"<srcVrfId>_<dstVrfId>":
  {"enable": bool}}``. The vendored SDK
  (``pyedgeconnect/orch/_vrf.py``) documents two further keys on read,
  ``gms_marked`` and ``comment``, and states the POST "will replace all
  disabled S-NAT map rules" — i.e. full-table replace. Only *disabled*
  pairs are listed; S-NAT between segments is on by default.

No field not declared by a spec or the vendored SDK is invented here;
unknown keys pass through untouched on both sides of the diff.

The ``options`` block, and ``activeMap`` in particular
-----------------------------------------------------

``NatPolicyOptions`` (appliance spec) declares **three** keys, not the two
``security_policy.py`` sends:

* ``merge`` — "If true, the POSTed rules are merged with existing rules. If
  false, posted rules replace existing rules." Sent ``false``: every write
  here is a full-table replace computed from canonical state.
* ``templateApply`` — "only used by Orchestrator when applying policy
  templates. Other users must set it to false." Sent ``false``.
* ``activeMap`` — "After POST operation, the name of the map to activate as
  the current policy map."

``activeMap`` is **not** a write-only directive like the other two: it names
which of several NAT maps is live, it is real operator-visible configuration,
and the appliance spec separately warns that the "current active NAT map
cannot be deleted". Dropping it from a POST body would at best be a no-op and
at worst deactivate the operator's live map — so it is handled deliberately:

1. ``normalize()`` **lifts** ``options.activeMap`` into canonical state as a
   top-level ``activeMap`` key (so it is diffable, plannable and restored by
   rollback), while dropping ``merge``/``templateApply``, which carry no
   state.
2. ``canonicalize_desired()`` **backfills** ``activeMap`` from the server
   when user intent does not mention it. A ``set_desired`` of a whole map
   table that simply says nothing about ``activeMap`` therefore cannot
   silently deactivate the live map — and, because the backfill happens
   before diffing, it also cannot manufacture a phantom "remove activeMap"
   diff entry that post-apply ``verify()`` would then trip over.
3. ``apply()``/``rollback()`` send ``activeMap`` whenever one is known, and
   omit the key entirely when the table is being emptied (there is nothing
   left to activate).

Reuse of ``security_policy.py``'s helpers
-----------------------------------------

``_strip_meta`` is imported and reused verbatim — it is a generic recursive
drop of ``self``/``gms_marked`` and fits this shape exactly.

``_inject_self`` is **not** importable for ``natMaps``: it walks
map -> zone-pair -> ``prio`` -> rule, and ``natMaps`` has no zone-pair level.
Handed ``{"map1": {"prio": {...}}}`` it would treat the literal string
``"prio"`` as a zone-pair key and write ``self: "prio"`` into the priority
table. :func:`_inject_map_self` below is the one-level-shallower counterpart,
written for this shape and echoing ``self`` at exactly the two levels the
captured payload and the ``natmapspolicy`` spec schema declare it (map name
as a string, priority as an integer). It deliberately does not descend into
``match``/``set``.

Ownership
---------

``ownership.KIND_TO_TEMPLATE_SECTIONS`` was pre-seeded with
``"appliance/nat": ("natMaps",)``. This module diverges from that key for the
same reason ``appliance_zones.py`` diverged from the pre-seeded
``"appliance/security-policy"``: one bare ``appliance/nat`` kind cannot name
*two* distinct appliance NAT resources (the policy-map table and the pool
table), which have different endpoints and different template sections. The
pre-seeded entry is left untouched (Branch NAT, ``nat/maps``, may yet claim
it) and two precise entries are added alongside it. ``appliance/nat-maps ->
("natMaps",)`` matches the ECOS path itself; ``appliance/nat-pools ->
("natPools",)`` is UNVERIFIED, the same convention ``appliance/zones`` uses —
no live Default Template Group with such a section selected was available.

``gms_marked`` is declared per-rule on ``natMaps`` (``natmapspolicy``) and on
``nat/maps`` (``BrNatPriority``: "Flag to determine if this rule was created
by Orchestrator"), so ``appliance/nat-maps`` uses the same "gms_marked first,
template-section fallback" ``managed_by()`` pattern as ``resources/routes.py``.
``NATPool`` declares no such flag, so ``appliance/nat-pools`` uses the
template join alone.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

import structlog

from pyecsdwan import ownership
from pyecsdwan.client import OrchApiError
from pyecsdwan.contract import (
    ApplyResult,
    CanonicalState,
    Ctx,
    Diff,
    Ownership,
    RawState,
    Ref,
    Resource,
    Reversibility,
    Scope,
    Tier,
)
from pyecsdwan.registry import register
from pyecsdwan.resources.security_policy import _strip_meta

log = structlog.get_logger("pyecsdwan.resources.nat")

#: ECOS path: the NAT policy-map table, `data`/`options` envelope.
_NAT_MAPS_PATH = "natMaps"
#: ECOS path: the NAT pool table.
_NAT_POOLS_PATH = "nat/natPools"
#: Orchestrator path: inter-segment S-NAT rules (the one writable NAT
#: endpoint on the Orchestrator API).
_SNAT_MAPS_PATH = "/vrf/config/snatMaps"
#: Orchestrator path: inter-segment D-NAT rules — GET only, read-only view.
_DNAT_MAPS_PATH = "/dnatMaps"

#: Segment/VRF pair key, e.g. "0_1" (same convention as security_policy.py).
_VRF_PAIR_RE = re.compile(r"^\d+_\d+$")
#: Valid rule priorities per NatNatPolicyRuleList: "a number between 1-65535".
_PRIO_MIN, _PRIO_MAX = 1, 65535


# == shared helpers ============================================================


def _inject_map_self(maps: Mapping[str, Any]) -> dict[str, Any]:
    """Re-add the ``self`` echoes a ``natMaps`` POST body carries.

    The one-level-shallower counterpart of
    ``security_policy._inject_self`` (see module docstring): map name ->
    ``{self: <mapName>, prio: {<prio>: {self: <int prio>, ...}}}``. Echoes
    are added at exactly those two levels — the ones the live capture and
    the ``natmapspolicy`` schema declare — and never inside ``match``/``set``.
    """
    out: dict[str, Any] = {}
    for map_name, map_val in maps.items():
        if not isinstance(map_val, Mapping):
            out[str(map_name)] = map_val
            continue
        new_map: dict[str, Any] = {"self": str(map_name)}
        for key, val in map_val.items():
            if key == "self":
                continue
            if key != "prio" or not isinstance(val, Mapping):
                new_map[str(key)] = copy.deepcopy(val)
                continue
            prio_out: dict[str, Any] = {}
            for prio_key, rule in val.items():
                if isinstance(rule, Mapping):
                    new_rule = copy.deepcopy(dict(rule))
                    new_rule.setdefault(
                        "self", int(prio_key) if str(prio_key).isdigit() else prio_key
                    )
                    prio_out[str(prio_key)] = new_rule
                else:
                    prio_out[str(prio_key)] = copy.deepcopy(rule)
            new_map["prio"] = prio_out
        out[str(map_name)] = new_map
    return out


def _sorted_prio(prio: Mapping[str, Any], where: str) -> dict[str, Any]:
    """Validate and numerically sort one map's priority-keyed rule table."""
    rules: dict[str, Any] = {}
    for prio_key, rule in prio.items():
        key = str(prio_key)
        if not key.isdigit():
            raise ValueError(
                f"{where}: rule key {key!r} is not a numeric priority "
                f"({_PRIO_MIN}-{_PRIO_MAX})"
            )
        number = int(key)
        if not _PRIO_MIN <= number <= _PRIO_MAX:
            raise ValueError(
                f"{where}: rule priority {number} out of range "
                f"({_PRIO_MIN}-{_PRIO_MAX})"
            )
        key = str(number)  # canonicalize leading zeros
        if key in rules:
            raise ValueError(f"{where}: duplicate rule priority {key} after canonicalization")
        if not isinstance(rule, Mapping):
            raise ValueError(
                f"{where}: rule {key} must be a mapping of rule fields "
                f"(match, set, ...), got {type(rule).__name__}"
            )
        rules[key] = dict(rule)
    return {key: rules[key] for key in sorted(rules, key=int)}


def _ne_pk_for(ctx: Ctx, ref: Ref) -> str:
    if ref.appliance is None:
        raise ValueError(f"{ref.kind} is appliance-scoped; ref.appliance is required")
    return ctx.resolver.ne_pk_for(ref.appliance)


def _persisted(
    ctx: Ctx, ne_pk: str, ref: Ref, verb: str, message: str
) -> ApplyResult:
    """Run the mandatory post-write save-changes; non-SUCCESS fails the op."""
    outcome = ctx.save_changes([ne_pk], f"{ref.kind} {verb}: {ref}")
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


# == appliance/nat-maps ========================================================


def _has_gms_marked(raw: RawState) -> bool:
    """True when any rule in a raw ``natMaps`` payload is Orchestrator-owned."""
    if not isinstance(raw, dict):
        return False
    maps = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    if not isinstance(maps, Mapping):
        return False
    for map_val in maps.values():
        if not isinstance(map_val, Mapping):
            continue
        prio = map_val.get("prio")
        if not isinstance(prio, Mapping):
            continue
        for rule in prio.values():
            if isinstance(rule, Mapping) and bool(rule.get("gms_marked")):
                return True
    return False


class NatMaps(Resource):
    kind = "appliance/nat-maps"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: A NAT rule's `set.trans_src` may name a NAT pool id, so pools apply
    #: first (and, reversed, map removals apply before pool removals).
    dependencies = ("appliance/nat-pools",)
    deletable = True
    desired_state_doc = (
        "maps: {mapName: {prio: {priority: {match: {...}, set: {...}, "
        "comment}}}}, activeMap: mapName. Full-table replace against ECOS "
        "'natMaps' with options {merge: false, templateApply: false, "
        "activeMap}; activeMap is real config (which map is live) and is "
        "backfilled from the server when user intent omits it, so a replace "
        "can never silently deactivate the running map."
    )
    endpoints = (
        "appliance GET /natMaps",
        "appliance POST /natMaps",
    )

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.appliance_request("GET", _ne_pk_for(ctx, ref), _NAT_MAPS_PATH)
        if raw is None or isinstance(raw, dict):
            return raw
        raise ValueError(f"unexpected natMaps response shape from {ref}: {raw!r}")

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return None
        active: Any = None
        options = raw.get("options")
        if isinstance(options, Mapping):
            active = options.get("activeMap")
        # Accept the raw `data`/`options` envelope, the already-canonical
        # {"maps": ..., "activeMap": ...} shape (round-tripped user intent),
        # and a bare map table.
        if isinstance(raw.get("data"), Mapping):
            maps_raw: Any = raw["data"]
        elif isinstance(raw.get("maps"), Mapping):
            maps_raw = raw["maps"]
            active = raw.get("activeMap", active)
        else:
            maps_raw = {
                k: v for k, v in raw.items() if k not in ("options", "settings", "activeMap")
            }
            active = raw.get("activeMap", active)
        if not isinstance(maps_raw, Mapping):
            raise ValueError(
                f"natMaps 'data' must be a mapping of map-name -> {{prio: ...}}, "
                f"got {type(maps_raw).__name__}"
            )
        stripped = _strip_meta(dict(maps_raw))
        assert isinstance(stripped, dict)
        # Drop options/settings echoes if the server wraps them alongside data
        # (security_policy.py's normalize() does the same).
        for meta_key in ("options", "settings"):
            stripped.pop(meta_key, None)
        maps: dict[str, Any] = {}
        for map_name, map_val in stripped.items():
            if not isinstance(map_val, Mapping):
                raise ValueError(
                    f"NAT map {map_name!r} must be a mapping with a 'prio' table, "
                    f"got {type(map_val).__name__}"
                )
            entry: dict[str, Any] = {str(k): v for k, v in map_val.items() if k != "prio"}
            prio_raw = map_val.get("prio") or {}
            if not isinstance(prio_raw, Mapping):
                raise ValueError(
                    f"NAT map {map_name!r}: 'prio' must be a mapping of priority -> "
                    f"rule, got {type(prio_raw).__name__}"
                )
            entry["prio"] = _sorted_prio(prio_raw, f"NAT map {map_name!r}")
            maps[str(map_name)] = entry
        if not maps:
            # No NAT maps at all is "absent", not an empty-but-present table —
            # the same reasoning appliance_zones.py records: it is what a
            # whole-resource delete produces as desired state, so post-apply
            # verify() agrees with itself instead of reporting phantom drift.
            return None
        canonical: dict[str, Any] = {"maps": {name: maps[name] for name in sorted(maps)}}
        if active is not None and str(active) != "":
            canonical["activeMap"] = str(active)
        return canonical

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """Normalize user intent, backfilling ``activeMap`` from the server.

        Intent that says nothing about which map is live inherits whatever is
        live now — see the module docstring: omitting ``activeMap`` from a
        POST body could deactivate the operator's running map, and letting
        the omission reach the diff would also manufacture a phantom
        "remove activeMap" entry that post-apply verify() would trip over.
        """
        canonical = self.normalize(dict(desired))
        if not isinstance(canonical, dict) or "activeMap" in canonical:
            return canonical
        current = self.normalize(self.fetch(ctx, ref))
        if isinstance(current, dict) and current.get("activeMap"):
            canonical["activeMap"] = current["activeMap"]
            log.debug(
                "nat_maps_active_map_backfilled",
                ref=str(ref),
                active_map=current["activeMap"],
            )
        return canonical

    # -- write side -----------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        state = diff.desired if isinstance(diff.desired, dict) else {}
        fallback = diff.current if isinstance(diff.current, dict) else {}
        return self._replace(ctx, diff.ref, state, fallback, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        restored = self.normalize(snapshot)
        state = restored if isinstance(restored, dict) else {}
        return self._replace(ctx, ref, state, {}, "rollback")

    def _replace(
        self,
        ctx: Ctx,
        ref: Ref,
        state: Mapping[str, Any],
        fallback: Mapping[str, Any],
        verb: str,
    ) -> ApplyResult:
        ne_pk = _ne_pk_for(ctx, ref)
        maps = state.get("maps") or {}
        payload_maps = _inject_map_self(maps)
        options: dict[str, Any] = {"merge": False, "templateApply": False}
        # activeMap: prefer the target state's, fall back to what is live now.
        # Omitted entirely when the table is being emptied — there is nothing
        # left to activate, and the spec warns the active map can't be deleted.
        active = state.get("activeMap") or fallback.get("activeMap")
        if payload_maps and active:
            options["activeMap"] = str(active)
        ctx.client.appliance_request(
            "POST",
            ne_pk,
            _NAT_MAPS_PATH,
            json_body={"data": payload_maps, "options": options},
        )
        rules = sum(
            len(m.get("prio", {})) for m in payload_maps.values() if isinstance(m, dict)
        )
        log.debug(
            "nat_maps_replace",
            ref=str(ref),
            verb=verb,
            maps=sorted(payload_maps),
            rules=rules,
            active_map=options.get("activeMap"),
        )
        active_note = f", activeMap={options['activeMap']}" if "activeMap" in options else ""
        return _persisted(
            ctx,
            ne_pk,
            ref,
            verb,
            f"NAT maps {verb} on {ne_pk}: {len(payload_maps)} map(s), "
            f"{rules} rule(s){active_note}",
        )

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name="global", appliance=name)
            for name in ctx.resolver.appliance_names()
        ]

    # -- ownership ------------------------------------------------------------

    def managed_by(self, ctx: Ctx, ref: Ref, diff: Diff | None = None) -> Ownership:
        ne_pk = _ne_pk_for(ctx, ref)
        # Per-rule precision first (routes.py's pattern): a rule the fabric
        # itself flagged gms_marked is server-owned regardless of what any
        # template happens to select.
        if _has_gms_marked(self.fetch(ctx, ref)):
            return Ownership.owned("gms (gms_marked NAT rule present on this appliance)")
        return ownership.resolve(ctx, self, ne_pk, diff)


# == appliance/nat-pools =======================================================


def _pool_entry(pool_id: str, pool: Any) -> dict[str, Any]:
    """Validate and shape one NAT pool; unknown fields pass through."""
    if not isinstance(pool, Mapping):
        raise ValueError(
            f"NAT pool {pool_id} must be a mapping of pool fields "
            f"(name, subnet, dir, pat), got {type(pool).__name__}"
        )
    entry: dict[str, Any] = {str(k): v for k, v in pool.items()}
    for required in ("name", "subnet"):
        value = entry.get(required)
        if value is None or str(value) == "":
            raise ValueError(f"NAT pool {pool_id} is missing the required field {required!r}")
    entry["name"] = str(entry["name"])
    entry["subnet"] = str(entry["subnet"])
    return entry


class NatPools(Resource):
    kind = "appliance/nat-pools"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    deletable = True
    desired_state_doc = (
        "pools: {poolId: {name, subnet, dir: outbound|inbound, pat: 0|1, "
        "comment}}. Removed ids are DELETEd individually (spec-declared "
        "'nat/natPools/{id}') before the whole table is POSTed to "
        "'nat/natPools', so removal is correct whether the plural POST "
        "replaces or merges. SPEC-DERIVED: the lab returned {} — no "
        "populated live sample was available."
    )
    #: Removed pools are deleted per-id before the full-table POST.
    endpoints = (
        "appliance GET /nat/natPools",
        "appliance POST /nat/natPools",
        "appliance DELETE /nat/natPools/{id}",
    )

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.appliance_request("GET", _ne_pk_for(ctx, ref), _NAT_POOLS_PATH)
        if raw is None or isinstance(raw, dict):
            return raw
        raise ValueError(f"unexpected natPools response shape from {ref}: {raw!r}")

    def normalize(self, raw: RawState) -> CanonicalState:
        if raw is None:
            pools_raw: Any = {}
        elif isinstance(raw, dict):
            pools_raw = raw.get("pools", raw) if "pools" in raw else raw
        else:
            raise ValueError(f"natPools response must be a mapping, got {raw!r}")
        if not isinstance(pools_raw, Mapping):
            raise ValueError(
                f"pools must be a mapping of pool-id -> {{name, subnet, ...}}, "
                f"got {type(pools_raw).__name__}"
            )
        stripped = _strip_meta(dict(pools_raw))
        assert isinstance(stripped, dict)
        pools: dict[str, dict[str, Any]] = {}
        for pool_id, pool in stripped.items():
            key = str(pool_id)
            if not key.isdigit():
                raise ValueError(f"NAT pool key {key!r} is not a numeric pool id")
            key = str(int(key))  # canonicalize leading zeros
            if key in pools:
                raise ValueError(f"duplicate NAT pool id {key} after key canonicalization")
            pools[key] = _pool_entry(key, pool)
        if not pools:
            return None
        return {"pools": {key: pools[key] for key in sorted(pools, key=int)}}

    # -- write side -----------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired if isinstance(diff.desired, dict) else {}
        current = diff.current if isinstance(diff.current, dict) else {}
        return self._replace(
            ctx, diff.ref, current.get("pools") or {}, desired.get("pools") or {}, "apply"
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        restored = self.normalize(snapshot)
        live = self.normalize(self.fetch(ctx, ref))
        return self._replace(
            ctx,
            ref,
            live.get("pools") or {} if isinstance(live, dict) else {},
            restored.get("pools") or {} if isinstance(restored, dict) else {},
            "rollback",
        )

    def _replace(
        self,
        ctx: Ctx,
        ref: Ref,
        current_pools: Mapping[str, Any],
        desired_pools: Mapping[str, Any],
        verb: str,
    ) -> ApplyResult:
        if dict(current_pools) == dict(desired_pools):
            return ApplyResult.noop()
        ne_pk = _ne_pk_for(ctx, ref)
        removed = sorted(set(current_pools) - set(desired_pools), key=int)
        # Spec-derived caution: `POST nat/natPools` takes the whole NATPools
        # object, but the spec never states whether it replaces or merges (it
        # has no `merge` option, unlike natMaps). Deleting removed ids through
        # the per-id endpoint first makes the operation correct either way.
        for pool_id in removed:
            ctx.client.appliance_request(
                "DELETE", ne_pk, f"{_NAT_POOLS_PATH}/{pool_id}", expected=(200, 204)
            )
        ctx.client.appliance_request(
            "POST", ne_pk, _NAT_POOLS_PATH, json_body=dict(desired_pools)
        )
        added = sorted(set(desired_pools) - set(current_pools), key=int)
        log.debug("nat_pools_replace", ref=str(ref), verb=verb, added=added, removed=removed)
        return _persisted(
            ctx,
            ne_pk,
            ref,
            verb,
            f"NAT pools {verb} on {ne_pk} (+{len(added)}/-{len(removed)} pool(s))",
        )

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name="global", appliance=name)
            for name in ctx.resolver.appliance_names()
        ]

    # -- ownership ------------------------------------------------------------

    def managed_by(self, ctx: Ctx, ref: Ref, diff: Diff | None = None) -> Ownership:
        # NATPool declares no gms_marked flag (unlike BrNatPriority /
        # natmapspolicy rules), so the template join is all there is.
        return ownership.resolve(ctx, self, _ne_pk_for(ctx, ref), diff)


# == snat-maps (orchestrator scope) ============================================


class SnatMaps(Resource):
    kind = "snat-maps"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    deletable = True
    desired_state_doc = (
        "maps: {'<srcSegmentId>_<dstSegmentId>': {enable: bool, comment}}. "
        "Inter-segment S-NAT is ON by default; this fabric-wide table lists "
        "the pairs where it is turned off. Full-table replace against "
        "POST /vrf/config/snatMaps — an empty table means 'S-NAT everywhere'."
    )
    #: D-NAT has no write endpoint in either baseline; it is a read view.
    endpoints = (
        "orchestrator GET /vrf/config/snatMaps",
        "orchestrator POST /vrf/config/snatMaps",
        "orchestrator GET /dnatMaps",
    )

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        try:
            raw = ctx.client.get(_SNAT_MAPS_PATH)
        except OrchApiError as exc:
            if exc.status_code in (204, 404):
                return None
            raise
        if raw is None or isinstance(raw, dict):
            return raw
        raise ValueError(f"unexpected snatMaps response shape: {raw!r}")

    def normalize(self, raw: RawState) -> CanonicalState:
        if raw is None:
            pairs_raw: Any = {}
        elif isinstance(raw, dict):
            pairs_raw = raw.get("maps", raw) if "maps" in raw else raw
        else:
            raise ValueError(f"snatMaps response must be a mapping, got {raw!r}")
        if not isinstance(pairs_raw, Mapping):
            raise ValueError(
                f"snat maps must be a mapping of '<srcVrfId>_<dstVrfId>' -> "
                f"{{enable: bool}}, got {type(pairs_raw).__name__}"
            )
        stripped = _strip_meta(dict(pairs_raw))
        assert isinstance(stripped, dict)
        pairs: dict[str, dict[str, Any]] = {}
        for pair, entry in stripped.items():
            key = str(pair)
            if not _VRF_PAIR_RE.match(key):
                raise ValueError(
                    f"snat map key {key!r} must be a segment pair like '0_1'"
                )
            if not isinstance(entry, Mapping):
                raise ValueError(
                    f"snat map {key}: value must be a mapping ({{enable: bool}}), "
                    f"got {type(entry).__name__}"
                )
            record: dict[str, Any] = {str(k): v for k, v in entry.items()}
            if "enable" not in record:
                raise ValueError(f"snat map {key} is missing the required field 'enable'")
            if not isinstance(record["enable"], bool):
                raise ValueError(
                    f"snat map {key}: 'enable' must be a boolean, "
                    f"got {type(record['enable']).__name__}"
                )
            pairs[key] = record
        if not pairs:
            # No disabled pairs is the fabric default (S-NAT on everywhere) —
            # the same "absent, not empty-but-present" convention the
            # appliance-scope resources above use, so a whole-resource delete
            # and post-apply verify() agree.
            return None
        ordered = sorted(pairs, key=lambda p: tuple(int(n) for n in p.split("_")))
        return {"maps": {key: pairs[key] for key in ordered}}

    # -- write side -----------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired if isinstance(diff.desired, dict) else {}
        return self._replace(ctx, desired.get("maps") or {}, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        restored = self.normalize(snapshot)
        pairs = restored.get("maps") or {} if isinstance(restored, dict) else {}
        return self._replace(ctx, pairs, "rollback")

    def _replace(self, ctx: Ctx, pairs: Mapping[str, Any], verb: str) -> ApplyResult:
        # Full-table replace (the vendored SDK's own warning: "This will
        # replace all disabled S-NAT map rules so include existing rules to
        # avoid making unintended changes"). Orchestrator scope: no
        # save-changes — that is an appliance-proxy obligation only.
        ctx.client.post(_SNAT_MAPS_PATH, dict(pairs))
        log.debug("snat_maps_replace", verb=verb, pairs=sorted(pairs))
        return ApplyResult(
            ok=True,
            message=f"inter-segment S-NAT {verb}: {len(pairs)} disabled segment pair(s)",
        )

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name="global")]


# == read-only views ===========================================================


def inter_segment_dnat_maps(ctx: Ctx, appliance: str) -> dict[str, Any]:
    """Inter-segment D-NAT rules for one appliance — READ ONLY.

    ``GET /dnatMaps?nePk=&cached=false``. There is no D-NAT write endpoint in
    either vendored spec (unlike S-NAT's ``/vrf/config/snatMaps``), so D-NAT
    gets a view here instead of a resource: an operator cannot "set" what the
    API will not accept. Exposed for ``show``/audit only.
    """
    ne_pk = ctx.resolver.ne_pk_for(appliance)
    raw = ctx.client.get(_DNAT_MAPS_PATH, params={"nePk": ne_pk, "cached": "false"})
    return raw if isinstance(raw, dict) else {}


register(NatMaps())
register(NatPools())
register(SnatMaps())
