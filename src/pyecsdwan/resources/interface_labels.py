"""Interface labels — the Phase-0 trivial resource proving the full loop.

Orchestrator-scoped singleton: ``GET /gms/interfaceLabels`` returns the whole
label table, ``POST /gms/interfaceLabels`` replaces it completely. That makes
snapshot/restore exact — a textbook REVERSIBLE resource.

Shape (see docs/research and pyedgeconnect orch/_interface_labels.py)::

    {"wan": {"<label_id>": {"name": str, "active": bool, "topology": int}},
     "lan": {...}}

Constraints the server enforces (surfaced as API errors on apply): label ids
unique across wan+lan; labels in use by an overlay cannot be removed.
"""

from __future__ import annotations

from typing import Any

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

_PATH = "/gms/interfaceLabels"


class InterfaceLabels(Resource):
    kind = "interface-labels"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    desired_state_doc = (
        "wan/lan maps of label-id -> {name: str, active: bool, topology: int}; "
        "topology 0 = full mesh, 2 = hub & spoke"
    )

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.get(_PATH)
        return raw if isinstance(raw, dict) else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return {"wan": {}, "lan": {}}
        out: dict[str, Any] = {}
        for side in ("wan", "lan"):
            labels: dict[str, Any] = {}
            for label_id, label in (raw.get(side) or {}).items():
                if not isinstance(label, dict):
                    continue
                entry = dict(label)  # unknown fields pass through
                if "topology" in entry:
                    entry["topology"] = int(entry["topology"])
                if "active" in entry:
                    entry["active"] = bool(entry["active"])
                labels[str(label_id)] = entry
            out[side] = labels
        return out

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired if isinstance(diff.desired, dict) else {"wan": {}, "lan": {}}
        payload = {"wan": desired.get("wan", {}), "lan": desired.get("lan", {})}
        ctx.client.post(_PATH, payload)
        return ApplyResult(ok=True, message="interface labels replaced")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        ctx.client.post(_PATH, {"wan": restored.get("wan", {}), "lan": restored.get("lan", {})})
        return ApplyResult(ok=True, message="interface labels restored from snapshot")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name="global")]


register(InterfaceLabels())
