"""Security policy (firewall) orchestration — Phase-1 resource.

Write path per docs/research/expert-repo.md (from the vendored Orchestrator
OpenAPI spec): ``GET/POST /vrf/config/securityPolicies?map={srcSeg}_{dstSeg}``
with body ``{data: SecurityMaps, options: {merge, templateApply}}`` —
``templateApply`` false for orchestration. The resource name is the segment
pair, e.g. ``0_0`` (default segment to default segment).

SecurityMaps nesting::

    {"<mapName>": {"<fromZoneId>_<toZoneId>": {"prio": {"<priority>": {
        "match": {...}, "set": {"action": "allow"|"deny", ...},
        "misc": {"rule": "enable", "logging": "disable", ...},
        "comment": ""}}}}

Normalization strips the ``self`` echo keys (each just repeats its parent
key; apply re-injects them) and the server bookkeeping flag ``gms_marked``,
so user intent and server state diff cleanly. Where live Orchestrator
responses differ from the spec (the response may or may not wrap the maps in
``data``), fetch stays tolerant.
"""

from __future__ import annotations

import copy
import re
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

_PATH = "/vrf/config/securityPolicies"
_MAP_RE = re.compile(r"^\d+_\d+$")


def _strip_meta(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            str(k): _strip_meta(v)
            for k, v in node.items()
            if k not in ("self", "gms_marked")
        }
    if isinstance(node, list):
        return [_strip_meta(v) for v in node]
    return node


def _inject_self(maps: dict[str, Any]) -> dict[str, Any]:
    """Re-add the ``self`` echoes the API expects in POST bodies."""
    out: dict[str, Any] = {}
    for map_name, zone_pairs in maps.items():
        new_map: dict[str, Any] = {"self": map_name}
        if isinstance(zone_pairs, dict):
            for pair, pair_val in zone_pairs.items():
                if pair == "self":
                    continue
                if isinstance(pair_val, dict):
                    new_pair = copy.deepcopy(pair_val)
                    new_pair["self"] = pair
                    prio = new_pair.get("prio")
                    if isinstance(prio, dict):
                        for prio_key, rule in prio.items():
                            if isinstance(rule, dict):
                                self_val = int(prio_key) if str(prio_key).isdigit() else prio_key
                                rule.setdefault("self", self_val)
                    new_map[pair] = new_pair
                else:
                    new_map[pair] = pair_val
        out[map_name] = new_map
    return out


class SecurityPolicy(Resource):
    kind = "security-policy"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: Policy rules address zone pairs (<fromZoneId>_<toZoneId>), so when one
    #: changeset creates zones and policy over them, zones must apply first
    #: (and, reversed, policy deletions apply before zone deletions).
    dependencies = ("zones",)
    desired_state_doc = (
        "maps: {mapName: {fromZoneId_toZoneId: {prio: {priority: {match, set, misc, "
        "comment}}}}} — resource name is the segment pair, e.g. '0_0'"
    )

    @staticmethod
    def _validate(ref: Ref) -> None:
        if not _MAP_RE.match(ref.name):
            raise ValueError(
                f"security-policy name must be a segment pair like '0_0', got {ref.name!r}"
            )

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        self._validate(ref)
        try:
            raw = ctx.client.get(_PATH, params={"map": ref.name})
        except OrchApiError as exc:
            if exc.status_code in (204, 404):
                return None
            raise
        if not isinstance(raw, dict):
            return None
        return raw

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return None
        maps = raw.get("data", raw)
        maps = raw.get("maps", maps) if isinstance(raw.get("maps"), dict) else maps
        if not isinstance(maps, dict):
            return None
        stripped = _strip_meta(maps)
        # Drop settings/options echoes if the server wraps them alongside data.
        for meta_key in ("options", "settings"):
            stripped.pop(meta_key, None)
        return {"maps": stripped} if stripped else None

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        self._validate(ref)
        if diff.desired is None:
            payload_maps: dict[str, Any] = {}
        else:
            assert isinstance(diff.desired, dict)
            payload_maps = _inject_self(diff.desired.get("maps", {}))
        ctx.client.post(
            _PATH,
            {
                "data": payload_maps,
                # merge=False: this body is the complete policy for the
                # segment pair. templateApply=False per the spec's own note
                # ("Set to false for security policies orchestration").
                "options": {"merge": False, "templateApply": False},
            },
            params={"map": ref.name},
        )
        rules = sum(
            len(zp.get("prio", {}))
            for m in payload_maps.values()
            for zp in m.values()
            if isinstance(zp, dict)
        )
        return ApplyResult(
            ok=True,
            message=f"security policy {ref.name} replaced ({rules} rule(s))",
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        canonical = self.normalize(snapshot)
        maps = canonical.get("maps", {}) if isinstance(canonical, dict) else {}
        ctx.client.post(
            _PATH,
            {"data": _inject_self(maps), "options": {"merge": False, "templateApply": False}},
            params={"map": ref.name},
        )
        return ApplyResult(ok=True, message=f"security policy {ref.name} restored")


register(SecurityPolicy())
