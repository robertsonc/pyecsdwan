"""Network regions and regional overlays (Phase 3, #35).

The ``bio`` resource (``resources/overlays.py``) covers the *global* overlay
configuration. This module adds the regional dimension: the network regions
themselves, which region each appliance belongs to, and the per-region
overlay configuration that regionalized fabrics diverge into.

Endpoint facts (the vendored ``specs/orchestrator-openapi-7.2.0.json``, plus
read-only live probes against a lab Orchestrator this session — orchestrator
scope throughout, plain ``ctx.client.get/post/put/delete``, never the
appliance proxy):

* ``GET /regions[?regionId=]`` → a JSON **array** of ``{"regionId": int,
  "regionName": str}`` (confirmed live: ``[{"regionId": 0, "regionName":
  "Default"}]``). ``POST /regions`` creates one from ``{"regionName": ...}``
  — the id is server-allocated, there is no ``nextId``-style allocator to
  claim one from (contrast ``resources/zones.py``). ``PUT /regions?regionId=``
  changes a region's fields; ``DELETE /regions?regionId=`` removes one.
* ``GET /regions/appliances[?nePk=]`` → ``{"nePk", "regionId", "regionName"}``
  and ``GET /regions/appliances/regionId?regionId=`` → the same shape for a
  whole region (exposed below as the read-only view :func:`appliances_in_region`).
  ``PUT /regions/appliances?nePk=`` with ``{"regionId": int}`` moves one
  appliance; ``POST /regions/appliances`` is the bulk form
  (``[{nePk, regionId}, ...]``). **Spec-derived only** — no live sample of
  these three was captured, so the response handling below is deliberately
  tolerant of both the bare object and a one-element array.
* ``GET /gms/overlays/config/regions[?overlayId=&regionId=]`` →
  ``{"<overlayId>": {"<regionId>": <overlay config>}}`` (confirmed live: 4
  overlays, each with exactly the one region key ``"0"``).

``regionId 0`` is the global region — the id the API itself documents as
"use regionId 0 to update global overlay configuration", and the id every
appliance sits in on a fabric that has never been regionalized. It is
addressable here by its name (``Default`` on the probed lab), by the literal
``0``, or by the alias ``global``. It is server-managed: :class:`Region`
refuses to delete it.

Kinds
-----

* ``region`` — one network region, keyed by ``regionName``.
* ``region-association`` — which region one appliance belongs to, keyed by
  appliance hostname (orchestrator scope: the endpoint is an Orchestrator
  API, not an ECOS path behind the proxy, so this is *not* an
  ``Scope.APPLIANCE`` resource despite being per-appliance).
* ``regional-overlay`` — one overlay's configuration *within one region*,
  keyed ``"<overlay>@<region>"`` (e.g. ``RealTime@EMEA``, or
  ``RealTime@global`` for the regionId-0/global configuration). Declares
  ``dependencies = ("bio", "region")``: both the overlay and the region must
  exist before their intersection can be configured.

PUT vs POST on ``/gms/overlays/config/regions`` (acceptance criterion)
---------------------------------------------------------------------

The two writes are **not** interchangeable, and the spec is explicit:

* ``POST`` — "create an **exhaustive** representation of regionalized
  overlays"; its body is an *array* of ``RegionToOverlay`` maps. That is a
  full-table replace, and the array's overlay-identity convention (index?
  the config's own ``id``?) is not pinned down by the spec. **This module
  never calls POST.**
* ``PUT`` — "update an **existing** overlay configuration, use regionId 0 to
  update global overlay configuration"; its body is one
  ``OverlayIdToRegionalOverlay`` map, i.e. the same
  ``{overlayId: {regionId: config}}`` nesting GET returns. This is the
  region-scoped write, and it is what :class:`RegionalOverlay` uses.

One residual ambiguity remains: PUT's body type is the *whole* map, so a
server that treats PUT as a replace rather than a merge would drop every
overlay/region absent from the request. The failure modes are wildly
asymmetric — a merge-PUT sent the full map is a verbatim no-op rewrite of
the untouched entries, while a replace-PUT sent one entry would destroy
every other regional overlay on the fabric — so ``_write()`` does not bet on
either reading: it GETs the current table, deep-copies it, replaces exactly
the one ``[overlayId][regionId]`` entry, and PUTs the merged whole. Untouched
regions and overlays are carried through **verbatim from the raw GET** (never
through ``normalize()``), so they are correct under both readings. This is
the same read-modify-write shape ``resources/deployment.py`` and
``resources/loopback.py`` use for their full-object endpoints.

``match.overlayAcl`` is a JSON-encoded string (idempotency hazard)
-----------------------------------------------------------------

Inside an overlay config, ``match.overlayAcl`` is **not** a nested object —
it is a ``str`` carrying serialized JSON, e.g.::

    "{\\"data\\":{\\"Overlay_RealTime\\":{\\"entry\\":{\\"1010\\":{...}}}},\\"options\\":{...}}"

Diffing that as an opaque string makes the comparison sensitive to the
server's key ordering and whitespace when it re-serializes, which shows up as
permanent phantom drift: every plan reports a change, every apply writes, and
the next plan reports the same change again.

``normalize()`` therefore **parses the embedded JSON and keeps the parsed
value** in canonical state. Both sides of the diff pass through
``normalize()`` (``canonicalize_desired`` defaults to it), so key order
cannot matter at all — dict comparison in ``diffing.structural_diff`` is
order-insensitive by construction — and the rendered diff points at the
actual ACL keys that changed instead of one unreadable string replacement.
``_encode_overlay_acl()`` re-serializes it on the way out
(``sort_keys=True, separators=(",", ":")``) because a string is what the
server expects. Two deliberate fallbacks: a value that is not valid JSON, or
that parses to a scalar rather than an object/array, is left as the opaque
string it came in as (logged at warning level for the first case) — no
silent reinterpretation of a field this module does not recognize.

Reversibility
-------------

* ``region`` — **COMPENSABLE**. An update or a create reverts exactly (PUT /
  DELETE), but rolling back a *delete* can only ``POST`` the name back and
  the server allocates a **new** regionId; anything keyed on the old id
  (regional overlays, appliance associations) is not restored with it. The
  rollback message says so explicitly rather than pretending otherwise.
* ``region-association`` — REVERSIBLE: the snapshot's ``regionId`` goes back
  through the same per-appliance PUT.
* ``regional-overlay`` — REVERSIBLE for the update case (the snapshot config
  is re-PUT through the identical merge path). **Known gap**: there is no
  endpoint that removes a single ``[overlayId][regionId]`` entry, so a
  rollback that would need to *remove* an entry this change created fails
  loudly (see ``_NO_ENTRY_DELETE``) instead of reaching for the
  under-specified exhaustive POST. Whole-resource delete is refused up front
  (``deletable = False``) for the same reason.

Unknown fields pass through untouched on every kind — the overlay config
carried 27 top-level fields on the probed lab and the spec documents more.
Only server-generated bookkeeping is stripped (see ``_OVERLAY_SERVER_FIELDS``
and ``_REGION_SERVER_FIELDS``).
"""

from __future__ import annotations

import copy
import json
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

log = structlog.get_logger("pyecsdwan.resources.regions")

_REGIONS_PATH = "/regions"
_REGION_APPLIANCES_PATH = "/regions/appliances"
_REGION_APPLIANCES_BY_REGION_PATH = "/regions/appliances/regionId"
_REGIONAL_OVERLAY_PATH = "/gms/overlays/config/regions"

#: The global region. Every appliance sits here until the fabric is
#: regionalized, and the API documents regionId 0 as the global overlay
#: configuration. Server-managed: it is never created or deleted from here.
GLOBAL_REGION_ID = 0
#: Case-insensitive alias for :data:`GLOBAL_REGION_ID` in a ref name, so a
#: user need not know the region's server-side name ("Default" on the probed
#: lab, but renameable).
GLOBAL_REGION_ALIAS = "global"

#: Separator in a ``regional-overlay`` ref name: "<overlay>@<region>".
REF_SEPARATOR = "@"

#: Server-assigned on a region record; not user intent.
_REGION_SERVER_FIELDS = ("regionId",)

#: Server bookkeeping on an overlay config. ``id`` is the server-assigned
#: overlay id (re-injected on write from the resolver, so stripping it here
#: is verify-safe); ``regionId``, if a build ever echoes it inside the config,
#: is the map key and would otherwise diff against desired state that never
#: carries it.
_OVERLAY_SERVER_FIELDS = ("id", "regionId", "modifiedTime", "createdTime", "lastModified")

_NO_ENTRY_DELETE = (
    "removing a single regional-overlay entry has no endpoint: DELETE "
    "/gms/overlays/config removes the whole overlay (kind 'bio', every region "
    "with it), and the only table-wide write is POST "
    "/gms/overlays/config/regions, whose array-of-map body shape the spec does "
    "not pin down well enough to construct safely. Remove the overlay itself "
    "(`delete bio <name>`), or drop the regional entry in the Orchestrator UI"
)


# -- shared helpers -----------------------------------------------------------


def _as_region_id(value: Any, what: str = "region id") -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"expected an integer {what}, got {value!r}") from None


def _region_list(raw: Any) -> list[dict[str, Any]]:
    """Shape a ``/regions`` response into the array form.

    ``GET /regions`` returns a JSON array (confirmed live). ``?regionId=`` is
    documented with the same array schema, but a build that answers a
    narrowed query with the bare object is folded into a one-element list so
    every caller below only ever sees the array shape.
    """
    if isinstance(raw, Mapping):
        return [dict(raw)] if "regionId" in raw else []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [dict(entry) for entry in raw if isinstance(entry, Mapping)]
    return []


def _regions(ctx: Ctx) -> list[dict[str, Any]]:
    """Every region on the fabric. Not resolver-cached: regions are few, and
    a stale cache here would mis-target a region-scoped write."""
    return _region_list(ctx.client.get(_REGIONS_PATH))


def _match_region(regions: list[dict[str, Any]], selector: str) -> dict[str, Any] | None:
    """Find the region a ref names: by numeric id, by the ``global`` alias, or
    by ``regionName``. Returns ``None`` when nothing matches."""
    key = selector.strip()
    if key.lower() == GLOBAL_REGION_ALIAS:
        key = str(GLOBAL_REGION_ID)
    if key.isdigit():
        want = int(key)
        for region in regions:
            try:
                if _as_region_id(region.get("regionId")) == want:
                    return region
            except ValueError:
                continue
        return None
    for region in regions:
        if str(region.get("regionName")) == key:
            return region
    return None


def resolve_region(ctx: Ctx, selector: str) -> dict[str, Any]:
    """Region record for a name / numeric id / ``global``; raises otherwise."""
    regions = _regions(ctx)
    found = _match_region(regions, selector)
    if found is None:
        known = sorted(str(r.get("regionName")) for r in regions if r.get("regionName"))
        raise ResolveError(
            f"unknown region {selector!r}; known regions: {', '.join(known) or '(none)'} "
            f"(a region is also addressable by its numeric id, or as "
            f"{GLOBAL_REGION_ALIAS!r} for regionId {GLOBAL_REGION_ID})"
        )
    return found


def region_id_for(ctx: Ctx, selector: str) -> str:
    """Region name / numeric id / ``global`` -> the regionId as a string."""
    return str(_as_region_id(resolve_region(ctx, selector).get("regionId")))


def _region_label(ctx: Ctx, region_id: Any) -> str:
    """Human-facing region name for a regionId; falls back to the id."""
    for region in _regions(ctx):
        if str(region.get("regionId")) == str(region_id):
            name = region.get("regionName")
            if name:
                return str(name)
    return str(region_id)


# == 1. regions ===============================================================


class Region(Resource):
    kind = "region"
    scope = Scope.ORCHESTRATOR
    #: A delete cannot be undone exactly: re-creating the region by name gets
    #: a fresh server-allocated id (see the module docstring).
    reversibility = Reversibility.COMPENSABLE
    tier = Tier.CURATED
    desired_state_doc = (
        "a network region, addressed by its regionName (the ref name); "
        f"{GLOBAL_REGION_ALIAS!r} and a bare numeric id also address an "
        "existing region. Region ids are server-allocated by POST /regions "
        "(no allocator endpoint exists to claim one from), so a region is "
        "created by name only, and renaming is not expressible here — the "
        f"name is the identity. regionId {GLOBAL_REGION_ID} (the global "
        "region) is server-managed and cannot be deleted. Unknown fields "
        "pass through."
    )
    endpoints = (
        "orchestrator GET /regions",
        "orchestrator POST /regions",
        "orchestrator PUT /regions",
        "orchestrator DELETE /regions",
    )

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        found = _match_region(_regions(ctx), ref.name)
        return dict(found) if found is not None else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, Mapping):
            return None
        out = {str(k): v for k, v in raw.items() if str(k) not in _REGION_SERVER_FIELDS}
        name = out.get("regionName")
        if name is None or str(name) == "":
            raise ValueError("region is missing the required field 'regionName'")
        out["regionName"] = str(name)
        return out

    # -- desired-state shaping --------------------------------------------------

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """Pin ``regionName`` to the region the ref addresses.

        The ref name *is* the identity, so a ``set region X regionName Y``
        must not silently rename X: forcing the identity name here keeps the
        diff honest (and keeps a re-plan of already-committed intent empty
        instead of resurrecting the old name).
        """
        out = {str(k): v for k, v in desired.items()}
        out["regionName"] = self._identity_name(ctx, ref)
        return self.normalize(out)

    def _identity_name(self, ctx: Ctx, ref: Ref) -> str:
        selector = ref.name.strip()
        if selector.lower() == GLOBAL_REGION_ALIAS or selector.isdigit():
            found = _match_region(_regions(ctx), selector)
            if found is None:
                raise ValueError(
                    f"region {ref.name!r} addresses a region by id, and no region "
                    f"with that id exists. Region ids are allocated by the server "
                    f"on POST {_REGIONS_PATH} and cannot be requested, so a new "
                    f"region can only be created by name — use `set region <name>`"
                )
            return str(found.get("regionName"))
        return selector

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        found = _match_region(_regions(ctx), ref.name)

        if diff.desired is None:
            if found is None:
                return ApplyResult(
                    ok=True, changed=False, message=f"region {ref.name!r} already absent"
                )
            region_id = _as_region_id(found.get("regionId"))
            if region_id == GLOBAL_REGION_ID:
                return ApplyResult(
                    ok=False,
                    message=(
                        f"refusing to delete regionId {GLOBAL_REGION_ID} "
                        f"({found.get('regionName')!r}): it is the global region "
                        f"every appliance falls back to and the id the API uses "
                        f"for global overlay configuration"
                    ),
                )
            ctx.client.delete(_REGIONS_PATH, params={"regionId": region_id})
            log.debug("region_deleted", region=ref.name, region_id=region_id)
            return ApplyResult(ok=True, message=f"region {ref.name!r} (id {region_id}) deleted")

        assert isinstance(diff.desired, dict)
        body = dict(diff.desired)
        if found is None:
            ctx.client.post(_REGIONS_PATH, body)
            return ApplyResult(
                ok=True,
                message=(
                    f"region {body['regionName']!r} created "
                    f"(regionId allocated by the server)"
                ),
            )
        region_id = _as_region_id(found.get("regionId"))
        ctx.client.put(_REGIONS_PATH, body, params={"regionId": region_id})
        return ApplyResult(ok=True, message=f"region {ref.name!r} (id {region_id}) updated")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        found = _match_region(_regions(ctx), ref.name)
        if snapshot is None:
            # Compensate a create.
            if found is None:
                return ApplyResult(
                    ok=True, changed=False, message=f"region {ref.name!r} already absent"
                )
            region_id = _as_region_id(found.get("regionId"))
            if region_id == GLOBAL_REGION_ID:
                return ApplyResult(
                    ok=False,
                    message=f"refusing to delete regionId {GLOBAL_REGION_ID} (global region)",
                )
            ctx.client.delete(_REGIONS_PATH, params={"regionId": region_id})
            return ApplyResult(ok=True, message=f"region {ref.name!r} removed (compensate)")

        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        if found is not None:
            ctx.client.put(
                _REGIONS_PATH, restored, params={"regionId": _as_region_id(found.get("regionId"))}
            )
            return ApplyResult(ok=True, message=f"region {ref.name!r} restored")

        old_id = snapshot.get("regionId") if isinstance(snapshot, Mapping) else None
        ctx.client.post(_REGIONS_PATH, restored)
        return ApplyResult(
            ok=True,
            message=(
                f"region {restored['regionName']!r} re-created with a NEW "
                f"server-allocated regionId (the original id {old_id!r} cannot be "
                f"requested back) — regional overlays and appliance associations "
                f"that referenced the old id must be re-applied"
            ),
        )

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name=str(region["regionName"]))
            for region in _regions(ctx)
            if region.get("regionName")
        ]


# == 2. appliance -> region association =======================================


def _association_entry(raw: Any, ne_pk: str) -> dict[str, Any] | None:
    """Pick one appliance's association record out of a ``/regions/appliances``
    response, tolerating both the bare object and an array (spec-derived
    shape; see the module docstring)."""
    if isinstance(raw, Mapping):
        return dict(raw) if "regionId" in raw else None
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for entry in raw:
            if isinstance(entry, Mapping) and str(entry.get("nePk")) == ne_pk:
                return dict(entry)
    return None


class RegionAssociation(Resource):
    kind = "region-association"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    dependencies = ("region",)
    #: An appliance is always in exactly one region (regionId 0 when the
    #: fabric is not regionalized) — there is no "absent" state, so a
    #: whole-resource delete is meaningless. Move it to another region
    #: instead, e.g. `set region-association <appliance> region global`.
    deletable = False
    desired_state_doc = (
        "region: the region this appliance belongs to — a region name, a "
        f"numeric region id, or {GLOBAL_REGION_ALIAS!r} for regionId "
        f"{GLOBAL_REGION_ID}. Canonical state is {{regionId: '<id>'}}: the "
        "id is stable across region renames, and the server's denormalized "
        "regionName is dropped so a rename cannot show up as phantom drift."
    )
    #: Per-appliance PUT only; the bulk POST form is deliberately unused.
    endpoints = (
        "orchestrator GET /regions/appliances",
        "orchestrator PUT /regions/appliances",
        "orchestrator GET /regions/appliances/regionId",
    )

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        try:
            raw = ctx.client.get(_REGION_APPLIANCES_PATH, params={"nePk": ne_pk})
        except OrchApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _association_entry(raw, ne_pk)

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, Mapping):
            return None
        region_id = raw.get("regionId")
        if region_id is None:
            raise ValueError("region association is missing the required field 'regionId'")
        return {"regionId": str(_as_region_id(region_id))}

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """User intent speaks region names; canonical state speaks regionIds."""
        selector = desired.get("region", desired.get("regionId"))
        if selector is None or str(selector) == "":
            raise ValueError(
                f"{self.kind} requires 'region' — a region name, a numeric region "
                f"id, or {GLOBAL_REGION_ALIAS!r} for regionId {GLOBAL_REGION_ID}"
            )
        return {"regionId": region_id_for(ctx, str(selector))}

    # -- write side -------------------------------------------------------------

    def write_target(self, ctx: Ctx, ref: Ref) -> str | None:
        """One appliance's region association (#69): the per-appliance PUT
        replaces the association object outright. Declared because the API
        also offers a bulk POST form that would overwrite every appliance's
        association at once — a future kind using it must declare against
        these per-instance targets. The ref's *name* is the appliance."""
        return f"appliance {ctx.resolver.ne_pk_for(ref.name)} region-association"

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        if diff.desired is None:
            raise ValueError(
                f"{self.kind} has no absent state; move the appliance to another "
                f"region instead (deletable=False guards this at plan time)"
            )
        assert isinstance(diff.desired, dict)
        region_id = _as_region_id(diff.desired.get("regionId"))
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        # Per-appliance PUT: the request names exactly one nePk, so no other
        # appliance's association can be disturbed (the bulk POST form takes a
        # list and is deliberately not used).
        ctx.client.put(
            _REGION_APPLIANCES_PATH, {"regionId": region_id}, params={"nePk": ne_pk}
        )
        log.debug("region_association_applied", appliance=ref.name, region_id=region_id)
        return ApplyResult(
            ok=True,
            message=(
                f"appliance {ref.name!r} moved to region "
                f"{_region_label(ctx, region_id)!r} (regionId {region_id})"
            ),
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # There is no absent state to restore to, and guessing regionId 0
            # would silently pull the appliance out of its region.
            return ApplyResult(
                ok=False,
                message=(
                    f"no usable snapshot for {ref.name!r}; refusing to guess a "
                    f"region association (an appliance is always in exactly one "
                    f"region, so 'absent' is not a state to restore)"
                ),
            )
        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        region_id = _as_region_id(restored.get("regionId"))
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        ctx.client.put(
            _REGION_APPLIANCES_PATH, {"regionId": region_id}, params={"nePk": ne_pk}
        )
        return ApplyResult(
            ok=True, message=f"appliance {ref.name!r} restored to regionId {region_id}"
        )

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name=str(a["hostName"]))
            for a in ctx.resolver.appliances()
            if a.get("hostName")
        ]


# == 3. regional overlays =====================================================


def _split_regional_ref(name: str) -> tuple[str, str]:
    """``"RealTime@EMEA"`` -> ``("RealTime", "EMEA")``.

    ``rpartition`` so an overlay name containing the separator still works;
    the region part never does (regions are named, not qualified).
    """
    overlay, sep, region = name.rpartition(REF_SEPARATOR)
    if not sep or not overlay.strip() or not region.strip():
        raise ValueError(
            f"regional-overlay ref name must be "
            f"'<overlay>{REF_SEPARATOR}<region>' (e.g. 'RealTime{REF_SEPARATOR}EMEA', "
            f"or 'RealTime{REF_SEPARATOR}{GLOBAL_REGION_ALIAS}' for the global / "
            f"regionId-{GLOBAL_REGION_ID} configuration); got {name!r}"
        )
    return overlay.strip(), region.strip()


def _decode_overlay_acl(config: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON-encoded ``match.overlayAcl`` string in place.

    Expects a private copy (``normalize()`` deep-copies first). Leaves the
    value alone when it is already parsed, is not valid JSON, or parses to a
    scalar — see the module docstring for why each fallback exists.
    """
    match = config.get("match")
    if not isinstance(match, dict):
        return config
    acl = match.get("overlayAcl")
    if not isinstance(acl, str):
        return config
    try:
        parsed = json.loads(acl)
    except ValueError:
        log.warning("overlay_acl_not_json", chars=len(acl))
        return config
    if not isinstance(parsed, (dict, list)):
        return config
    match["overlayAcl"] = parsed
    return config


def _encode_overlay_acl(config: Mapping[str, Any]) -> dict[str, Any]:
    """Re-serialize ``match.overlayAcl`` to the string the server expects.

    Canonically ordered (``sort_keys``, no whitespace) so that repeated writes
    of identical intent produce byte-identical payloads.
    """
    out = copy.deepcopy(dict(config))
    match = out.get("match")
    if isinstance(match, dict) and "overlayAcl" in match:
        acl = match["overlayAcl"]
        if not isinstance(acl, str):
            match["overlayAcl"] = json.dumps(acl, sort_keys=True, separators=(",", ":"))
    return out


def _extract_regional_config(raw: Any, overlay_id: str, region_id: str) -> dict[str, Any] | None:
    """Dig one config out of a ``GET /gms/overlays/config/regions`` response.

    Confirmed live shape is the full ``{overlayId: {regionId: config}}``
    nesting, returned even when the query narrows by overlayId/regionId. The
    two unwrapped shapes below are tolerated defensively in case a build
    peels a level off a narrowed query.
    """
    if not isinstance(raw, Mapping):
        return None
    by_region = raw.get(str(overlay_id))
    if isinstance(by_region, Mapping):
        config = by_region.get(str(region_id))
        return dict(config) if isinstance(config, Mapping) else None
    inner = raw.get(str(region_id))
    if isinstance(inner, Mapping):
        return dict(inner)
    if "name" in raw or "topology" in raw:
        return dict(raw)
    return None


def _overlay_id_value(overlay_id: str) -> Any:
    """The overlay id in the type the live payload carries inside a config
    (``"id": 1``, an int) while tolerating a non-numeric id."""
    return int(overlay_id) if overlay_id.isdigit() else overlay_id


class RegionalOverlay(Resource):
    kind = "regional-overlay"
    scope = Scope.ORCHESTRATOR
    #: Update reverts exactly through the same merge-PUT; the create case has
    #: no per-entry delete endpoint and fails loudly (module docstring).
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: The overlay must exist (bio) and the region must exist before their
    #: intersection can be written.
    dependencies = ("bio", "region")
    #: No endpoint removes one [overlayId][regionId] entry — see
    #: ``_NO_ENTRY_DELETE``.
    deletable = False
    desired_state_doc = (
        "one overlay's configuration within one region, ref name "
        f"'<overlay>{REF_SEPARATOR}<region>' (region = a region name, a numeric "
        f"id, or {GLOBAL_REGION_ALIAS!r} for regionId {GLOBAL_REGION_ID}). The "
        "body is the full overlay config object (name, topology, match, "
        "wanPorts, bondingPolicy, brownoutThresholds, ...) — see GET "
        "/gms/overlays/config/regions for the live shape; unknown fields pass "
        "through. match.overlayAcl is a JSON-encoded string on the wire and is "
        "held parsed in canonical state (re-encoded on write). Server "
        f"bookkeeping ({', '.join(_OVERLAY_SERVER_FIELDS)}) is stripped."
    )
    endpoints = (
        "orchestrator GET /gms/overlays/config/regions",
        "orchestrator PUT /gms/overlays/config/regions",
    )

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        overlay, region = _split_regional_ref(ref.name)
        try:
            overlay_id = ctx.resolver.overlay_id_for(overlay)
        except ResolveError:
            # The overlay may be created later in this same changeset (bio dep).
            return None
        found = _match_region(_regions(ctx), region)
        if found is None:
            # Likewise the region (region dep).
            return None
        region_id = str(_as_region_id(found.get("regionId")))
        try:
            raw = ctx.client.get(
                _REGIONAL_OVERLAY_PATH,
                params={"overlayId": overlay_id, "regionId": region_id},
            )
        except OrchApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _extract_regional_config(raw, overlay_id, region_id)

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, Mapping):
            return None
        out = {
            str(k): v
            for k, v in copy.deepcopy(dict(raw)).items()
            if str(k) not in _OVERLAY_SERVER_FIELDS
        }
        return _decode_overlay_acl(out)

    # -- write side -------------------------------------------------------------

    def _write(self, ctx: Ctx, overlay_id: str, region_id: str, config: Mapping[str, Any]) -> None:
        """Region-scoped read-modify-write PUT (see the module docstring).

        Everything except ``[overlay_id][region_id]`` is carried over verbatim
        from the raw GET — never re-shaped through ``normalize()`` — so no
        other region or overlay can be altered by this write under either
        reading of PUT's semantics.
        """
        entry = _encode_overlay_acl(config)
        # normalize() strips 'id' from both sides of the diff, so re-injecting
        # the resolver's id here cannot cause phantom drift on verify.
        entry.setdefault("id", _overlay_id_value(overlay_id))

        table = ctx.client.get(_REGIONAL_OVERLAY_PATH)
        body: dict[str, Any] = copy.deepcopy(dict(table)) if isinstance(table, Mapping) else {}
        by_region = body.get(str(overlay_id))
        if not isinstance(by_region, dict):
            by_region = {}
            body[str(overlay_id)] = by_region
        by_region[str(region_id)] = entry
        ctx.client.put(_REGIONAL_OVERLAY_PATH, body)
        log.debug(
            "regional_overlay_written",
            overlay_id=overlay_id,
            region_id=region_id,
            overlays_carried=sorted(body),
        )

    def _resolve(self, ctx: Ctx, ref: Ref) -> tuple[str, str, str, str]:
        overlay, region = _split_regional_ref(ref.name)
        # The bio may have been created earlier in this same changeset.
        ctx.resolver.refresh("overlays")
        return overlay, region, ctx.resolver.overlay_id_for(overlay), region_id_for(ctx, region)

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        if diff.desired is None:
            return ApplyResult(ok=False, message=_NO_ENTRY_DELETE)
        assert isinstance(diff.desired, dict)
        overlay, region, overlay_id, region_id = self._resolve(ctx, diff.ref)
        self._write(ctx, overlay_id, region_id, diff.desired)
        verb = "created" if diff.current is None else "updated"
        return ApplyResult(
            ok=True,
            message=(
                f"regional overlay {overlay!r} in region {region!r} {verb} "
                f"(overlayId {overlay_id}, regionId {region_id}; other regions "
                f"and overlays carried through unchanged)"
            ),
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # This change created the entry, and nothing can remove just it.
            overlay, region = _split_regional_ref(ref.name)
            return ApplyResult(
                ok=False,
                message=(
                    f"cannot revert the creation of regional overlay {overlay!r} in "
                    f"region {region!r}: {_NO_ENTRY_DELETE}"
                ),
            )
        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        overlay, region, overlay_id, region_id = self._resolve(ctx, ref)
        self._write(ctx, overlay_id, region_id, restored)
        return ApplyResult(
            ok=True,
            message=f"regional overlay {overlay!r} in region {region!r} restored from snapshot",
        )

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        table = ctx.client.get(_REGIONAL_OVERLAY_PATH)
        if not isinstance(table, Mapping):
            return []
        refs: list[Ref] = []
        for overlay_id, by_region in table.items():
            if not isinstance(by_region, Mapping):
                continue
            overlay = ctx.resolver.overlay_name_for(str(overlay_id))
            for region_id in by_region:
                refs.append(
                    Ref(
                        kind=self.kind,
                        name=f"{overlay}{REF_SEPARATOR}{_region_label(ctx, region_id)}",
                    )
                )
        return refs


# -- read-only views ----------------------------------------------------------


def appliances_in_region(ctx: Ctx, region_id: int | str) -> list[dict[str, Any]]:
    """Appliances in one region (``GET /regions/appliances/regionId``).

    Read-only convenience view: the write path is per-appliance
    (:class:`RegionAssociation`), so this is never part of diffable state.
    """
    raw = ctx.client.get(
        _REGION_APPLIANCES_BY_REGION_PATH, params={"regionId": _as_region_id(region_id)}
    )
    if isinstance(raw, Mapping):
        return [dict(raw)] if "nePk" in raw else []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [dict(entry) for entry in raw if isinstance(entry, Mapping)]
    return []


register(Region())
register(RegionAssociation())
register(RegionalOverlay())
