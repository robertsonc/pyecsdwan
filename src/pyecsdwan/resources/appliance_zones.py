"""Appliance-scope firewall zones + per-appliance security maps (Phase 2, #19).

Companion to the orchestrator-scope resources already shipped: ``zones.py``
(``kind="zones"``, ``Scope.ORCHESTRATOR``, issue #30) and
``security_policy.py`` (``kind="security-policy"``, ``Scope.ORCHESTRATOR``,
segment-pair scoped). Both resources here live at ``Scope.APPLIANCE`` and
address a *different* pair of ECOS endpoints reached through the appliance
proxy (``GET/POST /appliance/rest?nePk=<pk>&url=<ecosPath>``) — they share no
state with their orchestrator-scope namesakes and this module never imports
or mutates them.

Endpoint facts:

* ECOS path ``zones`` — the appliance's own firewall zone table. Read/write
  through the appliance proxy: ``GET/POST /appliance/rest?nePk=<pk>&
  url=zones``. ``POST`` is a full-table replace and, per the orchestrator
  zones convention this mirrors, requires a ``deleteDependencies`` flag —
  surfaced the same way (``true`` only when the write actually removes a
  zone). Because that flag isn't a body field, ``apply()``/``rollback()``
  call ``ctx.client.post("/appliance/rest", ..., params={"nePk", "url",
  "deleteDependencies"})`` directly rather than through
  ``OrchClient.appliance_request`` (which has no params passthrough) —
  ``validate_ne_pk`` is called explicitly to keep the same safety net.

  **Confirmed real payload shape** (captured this session, read-only,
  against a live lab Orchestrator's appliance-scope ``zones`` — high
  confidence)::

      {"1": {"name": "Untrust"}}

  Unlike the orchestrator-scope zones table (``zones.py``), this sample did
  **not** carry a zone id 0 — the Default-zone-is-always-present assumption
  belongs only to the orchestrator-scope resource. ``normalize()`` here
  injects no default row and no allocator (``/zones/nextId`` is an
  orchestrator-scope-only endpoint); zone ids are supplied directly by the
  caller as digit keys, validated but never invented.

* ECOS path ``securityMaps`` — per-appliance, per-segment firewall rules,
  addressed by zone pair. ``GET/POST /appliance/rest?nePk=<pk>&
  url=securityMaps``, persisted like every other appliance-proxy write via
  ``ctx.save_changes(...)``.

  **Confirmed real payload shape** (captured this session, read-only,
  against a live lab Orchestrator's ``securityMaps`` — high confidence)::

      {"map1": {"0_1": {"prio": {"20000": {
          "comment": "", "gms_marked": true,
          "match": {"acl": "", "webcc_cat": "11|27|..."},
          "misc": {"tag": "tke", "rule": "enable", "logging_priority": "2",
                    "logging": "enable"},
          "set": {"action": "deny"}}}}}}

  Structure: ``map name -> "<fromZoneId>_<toZoneId>" -> "prio" ->
  priority-number -> rule {comment, gms_marked, match{}, set{}, misc{}}`` —
  structurally the same map/zonePair/prio nesting as the orchestrator-scope
  ``security_policy.py``'s ``SecurityMaps`` body, so this resource reuses
  that module's self-echo inject/strip helpers verbatim (``_strip_meta``
  strips ``self``/``gms_marked`` on read; ``_inject_self`` re-adds the
  ``self`` echoes each nesting level is expected to carry on write) rather
  than re-implementing the same technique. Note: this session's captured
  sample above does not itself show ``self`` keys (only ``gms_marked``) —
  ``_strip_meta`` stripping a key that was never present is a no-op, and
  ``_inject_self`` adding one back is harmless passthrough either way, so
  the shared helper stays correct whether or not a given server echoes
  ``self`` on this particular endpoint.

  ``gms_marked`` is confirmed present per-rule and is this resource's
  ``managed_by()`` precision signal, same "gms_marked-first, template-
  section-fallback" pattern ``resources/routes.py`` (#15) uses for its
  per-interface entries: a rule the fabric itself flagged ``gms_marked`` is
  server-owned regardless of what any template selects, checked before
  falling back to the coarser template association x selection join.

Ownership kind names: ``ownership.KIND_TO_TEMPLATE_SECTIONS`` was pre-seeded
(ahead of this issue landing) with ``"appliance/security-policy":
("securityMaps",)``. This module deliberately does **not** reuse that key —
the security-map resource here is named ``"appliance/security-maps"``
(distinct from both that pre-seeded placeholder and the orchestrator-scope
``"security-policy"`` kind) to keep all three names unambiguous, and a new
``"appliance/security-maps": ("securityMaps",)`` entry is added to the
mapping alongside the pre-seeded one (which is left untouched — some other
resource may still claim it). The zones resource here similarly adds
``"appliance/zones"``, marked UNVERIFIED like ``deployment.py``'s section
guesses: no live Default Template Group with a zones-only section selected
was available to confirm the section name this session.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from pyecsdwan import ownership
from pyecsdwan.client import validate_ne_pk
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
from pyecsdwan.resources.security_policy import _inject_self, _strip_meta

log = structlog.get_logger("pyecsdwan.resources.appliance_zones")

_APPLIANCE_REST_PATH = "/appliance/rest"
_ZONES_ECOS_PATH = "zones"
_SECURITY_MAPS_ECOS_PATH = "securityMaps"


# == appliance-scope zones ====================================================


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


class ApplianceZones(Resource):
    kind = "appliance/zones"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: Unlike the orchestrator-scope zones table (no server-managed row is
    #: confirmed here — see module docstring), an appliance's zone table can
    #: genuinely be empty; whole-resource delete (POST an empty table) is a
    #: legitimate, reversible operation.
    deletable = True
    desired_state_doc = (
        "zones: map of zone-id -> {name: str}. Full-table replace against "
        "ECOS 'zones'; POSTs deleteDependencies=true only when the write "
        "removes a zone id. No allocator: ids are supplied directly (unlike "
        "orchestrator-scope zones, there is no confirmed server-managed "
        "Default/id-0 row at this scope)."
    )
    #: ECOS zone table; the write goes through the proxy with deleteDependencies.
    endpoints = (
        "appliance GET /zones",
        "appliance POST /zones",
    )

    @staticmethod
    def _ne_pk(ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{ref.kind} is appliance-scoped; ref.appliance is required")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        ne_pk = self._ne_pk(ctx, ref)
        raw = ctx.client.appliance_request("GET", ne_pk, _ZONES_ECOS_PATH)
        if raw is None or isinstance(raw, dict):
            return raw
        raise ValueError(f"unexpected appliance zones response shape from {ref}: {raw!r}")

    def normalize(self, raw: RawState) -> CanonicalState:
        if raw is None:
            zones_raw: Any = {}
        elif isinstance(raw, dict):
            # canonicalize_desired()/round-tripped canonical state passes the
            # already-wrapped {"zones": {...}} shape back through here too.
            zones_raw = raw.get("zones", raw) if "zones" in raw else raw
        else:
            raise ValueError(f"appliance zones response must be a mapping, got {raw!r}")
        if not isinstance(zones_raw, Mapping):
            raise ValueError(
                f"zones must be a mapping of zone-id -> {{name: ...}}, "
                f"got {type(zones_raw).__name__}"
            )
        zones: dict[str, dict[str, Any]] = {}
        for zone_id, zone in zones_raw.items():
            key = str(zone_id)
            if not key.isdigit():
                raise ValueError(
                    f"zone key {key!r} is not a numeric zone id (appliance-scope "
                    f"zones have no placeholder-id allocator)"
                )
            key = str(int(key))  # canonicalize leading zeros
            if key in zones:
                raise ValueError(f"duplicate zone id {key} after key canonicalization")
            zones[key] = _zone_entry(key, zone)
        if not zones:
            # No zones configured is "absent", not an empty-but-present
            # table: there is no confirmed server-managed row at this scope
            # (unlike orchestrator-scope zones' Default zone), so an empty
            # table is a genuine, common state. Returning None here (not
            # {"zones": {}}) matches what a whole-resource `delete` produces
            # as desired state, so post-apply verify() — which diffs a fresh
            # normalize() against that same desired — agrees with itself
            # instead of reporting phantom drift (see vrrp.py's identical
            # reasoning).
            return None
        ordered = {key: zones[key] for key in sorted(zones, key=int)}
        return {"zones": ordered}

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        ne_pk = self._ne_pk(ctx, ref)
        desired = diff.desired if isinstance(diff.desired, dict) else {}
        current = diff.current if isinstance(diff.current, dict) else {}
        desired_zones: dict[str, Any] = desired.get("zones") or {}
        current_zones: dict[str, Any] = current.get("zones") or {}
        return self._replace(ctx, ref, ne_pk, current_zones, desired_zones, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        restored = self.normalize(snapshot)
        current = self.normalize(self.fetch(ctx, ref))
        restored_zones: dict[str, Any] = (
            restored.get("zones", {}) if isinstance(restored, dict) else {}
        )
        current_zones: dict[str, Any] = (
            current.get("zones", {}) if isinstance(current, dict) else {}
        )
        # deleteDependencies=true regardless: zones added by the change being
        # reverted may already be referenced; the restore must remove them.
        return self._replace(ctx, ref, ne_pk, current_zones, restored_zones, "rollback")

    def _replace(
        self,
        ctx: Ctx,
        ref: Ref,
        ne_pk: str,
        current_zones: dict[str, Any],
        desired_zones: dict[str, Any],
        verb: str,
    ) -> ApplyResult:
        if desired_zones == current_zones:
            return ApplyResult.noop()
        removed = sorted(set(current_zones) - set(desired_zones), key=int)
        added = sorted(set(desired_zones) - set(current_zones), key=int)
        cascade = bool(removed) or verb == "rollback"
        validate_ne_pk(ne_pk)
        ctx.client.post(
            _APPLIANCE_REST_PATH,
            desired_zones,
            params={
                "nePk": ne_pk,
                "url": _ZONES_ECOS_PATH,
                "deleteDependencies": "true" if cascade else "false",
            },
        )
        log.debug("appliance_zones_replace", ref=str(ref), verb=verb, added=added, removed=removed)
        outcome = ctx.save_changes([ne_pk], f"appliance zones {verb}: {ref}")
        message = (
            f"appliance zones {verb} on {ne_pk} (+{len(added)}/-{len(removed)} zone(s)); "
            f"deleteDependencies={'true' if cascade else 'false'}"
        )
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

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name="global", appliance=name)
            for name in ctx.resolver.appliance_names()
        ]

    # -- ownership --------------------------------------------------------------

    def managed_by(self, ctx: Ctx, ref: Ref, diff: Diff | None = None) -> Ownership:
        ne_pk = self._ne_pk(ctx, ref)
        return ownership.resolve(ctx, self, ne_pk, diff)


# == per-appliance security maps ==============================================


def _has_gms_marked(raw: RawState) -> bool:
    if not isinstance(raw, dict):
        return False
    for zone_pairs in raw.values():
        if not isinstance(zone_pairs, Mapping):
            continue
        for pair_val in zone_pairs.values():
            if not isinstance(pair_val, Mapping):
                continue
            prio = pair_val.get("prio")
            if not isinstance(prio, Mapping):
                continue
            for rule in prio.values():
                if isinstance(rule, Mapping) and bool(rule.get("gms_marked")):
                    return True
    return False


class ApplianceSecurityMaps(Resource):
    kind = "appliance/security-maps"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: Rules address zone pairs (<fromZoneId>_<toZoneId>); this appliance's
    #: own zone table must apply first (and, reversed, security-map removals
    #: apply before zone removals) — same ordering security_policy.py
    #: declares against the orchestrator-scope zones.
    dependencies = ("appliance/zones",)
    desired_state_doc = (
        "maps: {mapName: {fromZoneId_toZoneId: {prio: {priority: {match, set, "
        "misc, comment}}}}}. Full-table replace against ECOS 'securityMaps', "
        "persisted via save-changes. self echoes are stripped on read and "
        "re-injected on write (security_policy.py's technique, reused as-is)."
    )
    #: ECOS security-policy (zone-pair) table.
    endpoints = (
        "appliance GET /securityMaps",
        "appliance POST /securityMaps",
    )

    @staticmethod
    def _ne_pk(ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{ref.kind} is appliance-scoped; ref.appliance is required")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        ne_pk = self._ne_pk(ctx, ref)
        raw = ctx.client.appliance_request("GET", ne_pk, _SECURITY_MAPS_ECOS_PATH)
        if raw is None or isinstance(raw, dict):
            return raw
        raise ValueError(f"unexpected appliance security-maps response shape from {ref}: {raw!r}")

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return None
        maps = raw.get("data", raw) if isinstance(raw.get("data"), dict) else raw
        maps = maps.get("maps", maps) if isinstance(maps.get("maps"), dict) else maps
        if not isinstance(maps, dict):
            return None
        stripped = _strip_meta(maps)
        assert isinstance(stripped, dict)
        return {"maps": stripped} if stripped else None

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        ne_pk = self._ne_pk(ctx, ref)
        if diff.desired is None:
            payload_maps: dict[str, Any] = {}
        else:
            assert isinstance(diff.desired, dict)
            payload_maps = _inject_self(diff.desired.get("maps", {}))
        ctx.client.appliance_request(
            "POST", ne_pk, _SECURITY_MAPS_ECOS_PATH, json_body=payload_maps
        )
        rules = sum(
            len(zp.get("prio", {}))
            for m in payload_maps.values()
            for zp in m.values()
            if isinstance(zp, dict)
        )
        log.debug("appliance_security_maps_apply", ref=str(ref), rules=rules)
        outcome = ctx.save_changes([ne_pk], f"appliance security-maps: {ref}")
        if outcome.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                jobs=[outcome],
                message=(
                    f"security maps replaced on {ne_pk} but not persisted — "
                    f"save-changes {outcome.state}: {outcome.detail}"
                ),
            )
        return ApplyResult(
            ok=True,
            jobs=[outcome],
            message=f"security maps replaced ({rules} rule(s)) on {ne_pk}, persisted",
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        canonical = self.normalize(snapshot)
        maps = canonical.get("maps", {}) if isinstance(canonical, dict) else {}
        ctx.client.appliance_request(
            "POST", ne_pk, _SECURITY_MAPS_ECOS_PATH, json_body=_inject_self(maps)
        )
        outcome = ctx.save_changes([ne_pk], f"appliance security-maps rollback: {ref}")
        if outcome.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                jobs=[outcome],
                message=(
                    f"security maps rollback on {ne_pk} not persisted — "
                    f"save-changes {outcome.state}: {outcome.detail}"
                ),
            )
        return ApplyResult(
            ok=True,
            jobs=[outcome],
            message=f"security maps restored from snapshot on {ne_pk}, persisted",
        )

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name="global", appliance=name)
            for name in ctx.resolver.appliance_names()
        ]

    # -- ownership --------------------------------------------------------------

    def managed_by(self, ctx: Ctx, ref: Ref, diff: Diff | None = None) -> Ownership:
        ne_pk = self._ne_pk(ctx, ref)
        # Per-rule precision first: a rule the fabric itself flagged
        # gms_marked is server-owned regardless of what any template selects
        # (same pattern as routes.py's per-interface gms_marked check).
        if _has_gms_marked(self.fetch(ctx, ref)):
            return Ownership.owned("gms (gms_marked security-map rule present on this appliance)")
        return ownership.resolve(ctx, self, ne_pk, diff)


register(ApplianceZones())
register(ApplianceSecurityMaps())
