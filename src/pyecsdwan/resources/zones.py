"""Orchestrator-level firewall zones + the segment↔zone map (Phase 3, #30).

Endpoint facts (docs/research/expert-repo.md §zones, the vendored
``specs/orchestrator-openapi-7.2.0.json``, and pyedgeconnect orch/_zones.py):

* ``GET /zones?allVRFZones=false`` → ``{"<zoneId>": {"name": str}}`` — the
  orchestrator zone table (unique-names view; ``true`` adds the per-segment
  duplicates, which are not what the write path replaces).
* ``POST /zones?deleteDependencies=<bool>`` — full-table replace; the query
  param is REQUIRED by the API. When true, zones removed by the write are
  also detached from overlays, policies, interfaces and deployment profiles
  that still reference them. The server re-adds the Default zone (id 0) to
  any table posted without it, so ``normalize()`` injects it on both sides —
  otherwise post-apply verify would see phantom drift and revert.
* ``GET/POST /zones/nextId`` → ``{"nextId": int}`` — the monotonic zone-id
  allocator. Zone ids are never reused (security policies address zone pairs
  as ``<fromZoneId>_<toZoneId>``; reusing an id would silently re-point old
  rules), so rollback never rewinds the counter.
* ``GET/POST /zones/eeEnable`` → ``{"enable": bool}`` — end-to-end ZBFW flag,
  folded into this singleton as canonical key ``endToEnd``.
* ``GET /zones/vrfZonesMap`` / ``GET /appliance/zoneListMeta`` — read-only
  views (segment↔zone map, per-appliance cached zone lists). Orchestrator
  scope has no write path for the map — the ``POST /vrfZonesMap`` write is an
  ECOS (appliance-scope) endpoint covered by issue #19 — so they are exposed
  as the module-level read helpers below, never as diffable state.

Singleton REVERSIBLE resource (instance name ``global``), like
interface-labels: the zone table always exists (the Default zone is
server-managed), so whole-resource delete is refused — deleting means
removing individual ``zones.<id>`` entries.

New zones are staged under a *placeholder* key (any non-numeric string)::

    set zones global zones guest name Guest

``canonicalize_desired()`` resolves each placeholder to a real id at plan
time: a staged zone already carrying that name keeps its id (merge-mode
intent inherits the server table, so re-planning the same command after a
commit diffs empty), otherwise consecutive ids are claimed starting at
``GET /zones/nextId`` — ids are allocator-issued, never invented.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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

_ZONES_PATH = "/zones"
_NEXT_ID_PATH = "/zones/nextId"
_EE_ENABLE_PATH = "/zones/eeEnable"
_VRF_ZONES_MAP_PATH = "/zones/vrfZonesMap"
_ZONE_LIST_META_PATH = "/appliance/zoneListMeta"

#: The server-managed default zone, re-added by the API to any POSTed table
#: that omits it. Injected on both sides of the diff (see module docstring).
_DEFAULT_ZONE_ID = "0"
_DEFAULT_ZONE_NAME = "Default"

#: What deleteDependencies=true cascades into (per the OpenAPI param doc).
_CASCADE_TARGETS = "overlays, policies, interfaces and deployment profiles"


def _zone_entry(zone_id: str, zone: Any) -> dict[str, Any]:
    """Validate and shape one zone record; unknown fields pass through."""
    if not isinstance(zone, Mapping):
        raise ValueError(
            f"zone {zone_id} must be a mapping of zone fields (name), "
            f"got {type(zone).__name__}"
        )
    entry: dict[str, Any] = {str(k): v for k, v in zone.items()}
    name = entry.get("name")
    if name is None or str(name) == "":
        raise ValueError(f"zone {zone_id} is missing the required field 'name'")
    entry["name"] = str(name)
    return entry


class Zones(Resource):
    kind = "zones"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: Singleton table: there is no "the zones don't exist" state (the Default
    #: zone is server-managed), so whole-resource delete is refused — delete
    #: individual zone entries instead.
    deletable = False
    desired_state_doc = (
        "zones: map of zone-id -> {name: str}; a non-numeric key is a "
        "placeholder for a new zone whose id is allocated from /zones/nextId "
        "at plan time. endToEnd: bool (end-to-end ZBFW; omitted in a full "
        "replace = false). Removing a zone applies with deleteDependencies="
        "true, detaching it from " + _CASCADE_TARGETS + ". Zone id 0 "
        "(Default) is server-managed and cannot be removed."
    )
    #: nextId is the id allocator; the last two are read-only views.
    endpoints = (
        "orchestrator GET /zones",
        "orchestrator POST /zones",
        "orchestrator GET /zones/eeEnable",
        "orchestrator POST /zones/eeEnable",
        "orchestrator GET /zones/nextId",
        "orchestrator POST /zones/nextId",
        "orchestrator GET /zones/vrfZonesMap",
        "orchestrator GET /appliance/zoneListMeta",
    )

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        zones = ctx.client.get(_ZONES_PATH, params={"allVRFZones": "false"})
        raw: dict[str, Any] = {"zones": zones if isinstance(zones, dict) else {}}
        try:
            ee = ctx.client.get(_EE_ENABLE_PATH)
        except OrchApiError as exc:
            # Tolerate builds without the end-to-end ZBFW endpoint; apply()
            # only writes the flag when the diff actually changes it.
            if exc.status_code != 404:
                raise
            ee = None
        if isinstance(ee, dict):
            raw["eeEnable"] = ee
        try:
            next_id = ctx.client.get(_NEXT_ID_PATH)
        except OrchApiError as exc:
            if exc.status_code != 404:
                raise
            next_id = None
        if isinstance(next_id, dict) and "nextId" in next_id:
            # Snapshot/audit visibility only — normalize() strips it (it is
            # the allocator cursor, not configuration intent).
            raw["nextId"] = next_id["nextId"]
        return raw

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            raw = {}
        zones_raw = raw.get("zones") or {}
        if not isinstance(zones_raw, Mapping):
            raise ValueError(
                "zones must be a mapping of zone-id -> {name: ...}, "
                f"got {type(zones_raw).__name__}"
            )
        zones: dict[str, dict[str, Any]] = {}
        for zone_id, zone in zones_raw.items():
            key = str(zone_id)
            if not key.isdigit():
                raise ValueError(
                    f"zone key {key!r} is not a numeric zone id — new zones are "
                    f"staged under a placeholder key and receive a real id from "
                    f"{_NEXT_ID_PATH} at plan time (canonicalize_desired)"
                )
            key = str(int(key))  # canonicalize leading zeros
            if key in zones:
                raise ValueError(f"duplicate zone id {key} after key canonicalization")
            zones[key] = _zone_entry(key, zone)
        # The server re-adds the Default zone to any table missing it; fill it
        # on both sides so it can never be diffed away (matches server truth).
        zones.setdefault(_DEFAULT_ZONE_ID, {"name": _DEFAULT_ZONE_NAME})
        ordered = {key: zones[key] for key in sorted(zones, key=int)}
        ee = raw.get("endToEnd", raw.get("eeEnable"))
        if isinstance(ee, Mapping):  # GET /zones/eeEnable shape {"enable": bool}
            ee = ee.get("enable")
        return {"zones": ordered, "endToEnd": bool(ee)}

    # -- desired-state shaping -------------------------------------------------

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """Resolve new-zone placeholders through the allocator, then normalize.

        Each non-numeric zone key is a placeholder for a new zone. It resolves
        to the id of a staged zone with the same name when one exists (so
        re-planning already-committed intent is a no-op), else to a fresh id
        from ``GET /zones/nextId`` — consecutive, in sorted placeholder order,
        skipping ids already present in the staged table.
        """
        zones_in = desired.get("zones") or {}
        if not isinstance(zones_in, Mapping):
            raise ValueError(
                "zones must be a mapping of zone-id -> {name: ...}, "
                f"got {type(zones_in).__name__}"
            )
        explicit: dict[str, dict[str, Any]] = {}
        placeholders: dict[str, dict[str, Any]] = {}
        for key, zone in zones_in.items():
            skey = str(key)
            if skey.isdigit():
                explicit[str(int(skey))] = _zone_entry(skey, zone)
            else:
                placeholders[skey] = _zone_entry(skey, zone)

        if placeholders:
            # Ids claimable by name, lowest first; each id is handed out once
            # so same-named placeholders pair deterministically.
            by_name: dict[str, list[str]] = {}
            for zone_id, zone in explicit.items():
                by_name.setdefault(zone["name"], []).append(zone_id)
            for ids in by_name.values():
                ids.sort(key=int)
            unallocated: list[dict[str, Any]] = []
            for pkey in sorted(placeholders):
                zone = placeholders[pkey]
                candidates = by_name.get(zone["name"], [])
                if candidates:
                    zone_id = candidates.pop(0)
                    explicit[zone_id] = {**explicit[zone_id], **zone}
                else:
                    unallocated.append(zone)
            if unallocated:
                next_id = self._allocator_next_id(ctx)
                taken = {int(zone_id) for zone_id in explicit}
                for zone in unallocated:
                    while next_id in taken:
                        next_id += 1
                    explicit[str(next_id)] = zone
                    taken.add(next_id)
                    next_id += 1

        out = {key: value for key, value in desired.items() if key != "zones"}
        out["zones"] = explicit
        return self.normalize(out)

    @staticmethod
    def _allocator_next_id(ctx: Ctx) -> int:
        raw = ctx.client.get(_NEXT_ID_PATH)
        value: Any = raw.get("nextId") if isinstance(raw, dict) else None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"cannot allocate zone ids: GET {_NEXT_ID_PATH} returned no "
                f"usable nextId: {raw!r}"
            ) from None

    # -- write side -------------------------------------------------------------

    def write_target(self, ctx: Ctx, ref: Ref) -> str | None:
        """The Orchestrator-wide zone configuration (#69).

        Three endpoints behind one identity: the zone table (``/zones``, a
        full-table POST), the id allocator it advances (``/zones/nextId``)
        and the end-to-end ZBFW toggle (``/zones/eeEnable``). Named as a set
        for the same reason as ``appliance/bgp``: the contract compares
        strings, and a future writer of any of the three reuses this string
        or splits the declarations. The per-appliance ``appliance/zones``
        table is a different object and deliberately not this target.
        """
        return "orchestrator zones"

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired if isinstance(diff.desired, dict) else {}
        current = diff.current if isinstance(diff.current, dict) else {}
        desired_zones: dict[str, Any] = desired.get("zones") or {}
        current_zones: dict[str, Any] = current.get("zones") or {}
        messages: list[str] = []

        if desired_zones != current_zones:
            removed = sorted(set(current_zones) - set(desired_zones), key=int)
            added = sorted(set(desired_zones) - set(current_zones), key=int)
            cascade = bool(removed)
            # deleteDependencies is a REQUIRED query param. It is true only
            # when this write actually removes zones: the operator staged
            # those deletions, and the flag detaches the removed zones from
            # anything still referencing them (see _CASCADE_TARGETS).
            ctx.client.post(
                _ZONES_PATH,
                desired_zones,
                params={"deleteDependencies": "true" if cascade else "false"},
            )
            message = f"zone table replaced (+{len(added)}/-{len(removed)} zone(s))"
            if cascade:
                message += (
                    f"; deleteDependencies=true — removed zone id(s) "
                    f"{', '.join(removed)} are detached from {_CASCADE_TARGETS}"
                )
            else:
                message += "; deleteDependencies=false (no zones removed)"
            messages.append(message)
            if added:
                advanced = self._advance_allocator(ctx, desired_zones)
                if advanced is not None:
                    messages.append(f"nextId advanced to {advanced}")

        if bool(desired.get("endToEnd")) != bool(current.get("endToEnd")):
            enable = bool(desired.get("endToEnd"))
            ctx.client.post(_EE_ENABLE_PATH, {"enable": enable})
            messages.append(f"end-to-end ZBFW {'enabled' if enable else 'disabled'}")

        return ApplyResult(ok=True, message="; ".join(messages) or "zones updated")

    @staticmethod
    def _advance_allocator(ctx: Ctx, zones: Mapping[str, Any]) -> int | None:
        """Keep ``/zones/nextId`` ahead of every id in the table.

        Compare-and-advance: a concurrent allocation may already have moved
        the counter further — posting a lower value back would re-open ids
        for reuse, which zone ids must never allow.
        """
        if not zones:
            return None
        highest = max(int(zone_id) for zone_id in zones)
        raw = ctx.client.get(_NEXT_ID_PATH)
        value: Any = raw.get("nextId") if isinstance(raw, dict) else None
        try:
            counter: int | None = int(value)
        except (TypeError, ValueError):
            counter = None
        if counter is not None and counter > highest:
            return None
        ctx.client.post(_NEXT_ID_PATH, {"nextId": highest + 1})
        return highest + 1

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # A snapshot recorded as absent must never replay as "POST an
            # empty zone table" — that would wipe every zone. Refuse loudly.
            return ApplyResult(
                ok=False,
                message="no usable snapshot; refusing to POST an empty zone table",
            )
        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        # deleteDependencies=true: zones added by the change being reverted
        # may already be referenced; the restore must remove them regardless.
        ctx.client.post(
            _ZONES_PATH,
            restored.get("zones") or {},
            params={"deleteDependencies": "true"},
        )
        ctx.client.post(_EE_ENABLE_PATH, {"enable": bool(restored.get("endToEnd"))})
        # /zones/nextId is deliberately NOT rewound: zone ids are never
        # reused, so the allocator only moves forward — even across rollback.
        return ApplyResult(ok=True, message="zones restored from snapshot")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name="global")]


# -- read-only views ----------------------------------------------------------


def segment_zone_map(ctx: Ctx) -> dict[str, Any]:
    """Segment↔zone map: ``{"<vrfId>": {"<zoneIndex>": {"id", "name"}}}``.

    Read-only at orchestrator scope — the write path (``POST /vrfZonesMap``)
    is an ECOS appliance endpoint (issue #19) — so this is a view, never part
    of the diffable canonical state.
    """
    raw = ctx.client.get(_VRF_ZONES_MAP_PATH)
    return raw if isinstance(raw, dict) else {}


def appliance_zone_lists(ctx: Ctx, ne_pk: str | None = None) -> dict[str, Any]:
    """Cached per-appliance zone lists: ``{"<nePk>": {"zones": [...]}}``."""
    params = {"nePk": ne_pk} if ne_pk else None
    raw = ctx.client.get(_ZONE_LIST_META_PATH, params=params)
    return raw if isinstance(raw, dict) else {}


register(Zones())
