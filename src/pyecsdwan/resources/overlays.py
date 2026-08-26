"""Business Intent Overlays (BIOs) and their appliance associations (Phase 1).

Endpoint facts: docs/research/templates-overlays-security.md.

* ``bio`` — overlay configuration by name (the resolver maps name -> server-
  assigned overlay id). Modifications are pushed to the fabric automatically
  by the Orchestrator (async, no action key surfaces); verify() re-checks the
  orchestrator-side config, which is the state this resource manages.
* ``bio-association`` — the set of appliances in an overlay (ref name =
  overlay name; canonical state speaks appliance hostnames). Add/remove are
  separate endpoints, so apply computes set deltas.
"""

from __future__ import annotations

import copy
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
from pyecsdwan.resolver import ResolveError

_CONFIG_PATH = "/gms/overlays/config"
_ASSOC_PATH = "/gms/overlays/association"

#: Server-injected fields stripped by normalize(). Best-effort list — the mock
#: injects id/modifiedTime; extend after live-Orchestrator testing if replace-
#: mode (`load`) shows phantom drift on additional injected fields.
_SERVER_FIELDS = ("id", "modifiedTime", "createdTime", "lastModified")


class Bio(Resource):
    kind = "bio"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    desired_state_doc = (
        "full overlay configuration object (name, topology, interface label "
        "preferences, ...) — see GET /gms/overlays/config for the live shape"
    )

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        try:
            overlay_id = ctx.resolver.overlay_id_for(ref.name)
        except ResolveError:
            return None
        try:
            raw = ctx.client.get(_CONFIG_PATH, params={"overlayId": overlay_id})
        except OrchApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return raw if isinstance(raw, dict) else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return None
        out = copy.deepcopy(raw)
        for field in _SERVER_FIELDS:
            out.pop(field, None)
        return out

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        if diff.desired is None:
            overlay_id = ctx.resolver.overlay_id_for(ref.name)
            ctx.client.delete(_CONFIG_PATH, params={"overlayId": overlay_id})
            ctx.resolver.refresh("overlays")
            return ApplyResult(ok=True, message=f"overlay {ref.name!r} deleted")
        assert isinstance(diff.desired, dict)
        body = dict(diff.desired)
        body.setdefault("name", ref.name)
        if diff.current is None:
            ctx.client.post(_CONFIG_PATH, body)
            ctx.resolver.refresh("overlays")
            return ApplyResult(
                ok=True,
                message=f"overlay {ref.name!r} created (fabric push runs async)",
            )
        overlay_id = ctx.resolver.overlay_id_for(ref.name)
        ctx.client.put(_CONFIG_PATH, body, params={"overlayId": overlay_id})
        return ApplyResult(
            ok=True, message=f"overlay {ref.name!r} updated (fabric push runs async)"
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        ctx.resolver.refresh("overlays")
        if snapshot is None:
            try:
                overlay_id = ctx.resolver.overlay_id_for(ref.name)
            except ResolveError:
                return ApplyResult(ok=True, changed=False, message="overlay already absent")
            ctx.client.delete(_CONFIG_PATH, params={"overlayId": overlay_id})
            ctx.resolver.refresh("overlays")
            return ApplyResult(ok=True, message=f"overlay {ref.name!r} removed (compensate)")
        canonical = self.normalize(snapshot)
        assert isinstance(canonical, dict)
        body = dict(canonical)
        body.setdefault("name", ref.name)
        try:
            overlay_id = ctx.resolver.overlay_id_for(ref.name)
        except ResolveError:
            ctx.client.post(_CONFIG_PATH, body)
        else:
            ctx.client.put(_CONFIG_PATH, body, params={"overlayId": overlay_id})
        ctx.resolver.refresh("overlays")
        return ApplyResult(ok=True, message=f"overlay {ref.name!r} restored")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name=str(o["name"]))
            for o in ctx.resolver.overlays()
            if o.get("name")
        ]


class BioAssociation(Resource):
    kind = "bio-association"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    dependencies = ("bio",)
    desired_state_doc = "appliances: complete list of appliance hostnames in the overlay"

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        try:
            overlay_id = ctx.resolver.overlay_id_for(ref.name)
        except ResolveError:
            # Overlay may be created later in this same changeset (bio dep).
            return None
        raw = ctx.client.get(_ASSOC_PATH)
        if not isinstance(raw, dict):
            return None
        ne_pks = raw.get(str(overlay_id), [])
        return {"nePks": ne_pks if isinstance(ne_pks, list) else []}

    def normalize(self, raw: RawState) -> CanonicalState:
        # Canonical shape is uniformly {"nePks": [...]}; user intent reaches
        # canonical form through canonicalize_desired below, never through here.
        if not isinstance(raw, dict):
            return None
        return {"nePks": sorted(str(p) for p in raw.get("nePks", []))}

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """User intent speaks hostnames; canonical state speaks nePks (stable
        across renames), resolved through the resolver."""
        names = desired.get("appliances", desired.get("nePks", []))
        return {"nePks": sorted(ctx.resolver.ne_pk_for(str(n)) for n in names)}

    # Both sides are {"nePks": [...]} by the time they meet the differ; the
    # base structural diff is correct, so no diff() override is needed.

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        ctx.resolver.refresh("overlays")  # the bio may have just been created
        overlay_id = ctx.resolver.overlay_id_for(ref.name)
        current = set(diff.current.get("nePks", [])) if isinstance(diff.current, dict) else set()
        desired = set(diff.desired.get("nePks", [])) if isinstance(diff.desired, dict) else set()
        adds = sorted(desired - current)
        removes = sorted(current - desired)
        if adds:
            ctx.client.post(_ASSOC_PATH, {str(overlay_id): adds})
        if removes:
            ctx.client.post(f"{_ASSOC_PATH}/remove", {str(overlay_id): removes})
        return ApplyResult(
            ok=True,
            message=f"overlay {ref.name!r}: +{len(adds)}/-{len(removes)} appliance(s)",
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        ctx.resolver.refresh("overlays")
        try:
            overlay_id = ctx.resolver.overlay_id_for(ref.name)
        except ResolveError:
            # Overlay itself was reverted away; membership went with it.
            return ApplyResult(ok=True, changed=False, message="overlay absent; nothing to restore")
        target = self.normalize(snapshot)
        assert isinstance(target, dict)
        desired = set(target.get("nePks", []))
        raw_now = self.fetch(ctx, ref)
        current = set((raw_now or {}).get("nePks", [])) if isinstance(raw_now, dict) else set()
        adds = sorted(desired - current)
        removes = sorted(current - desired)
        if adds:
            ctx.client.post(_ASSOC_PATH, {str(overlay_id): adds})
        if removes:
            ctx.client.post(f"{_ASSOC_PATH}/remove", {str(overlay_id): removes})
        return ApplyResult(ok=True, message=f"overlay {ref.name!r} membership restored")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name=str(o["name"]))
            for o in ctx.resolver.overlays()
            if o.get("name")
        ]


register(Bio())
register(BioAssociation())
