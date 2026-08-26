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
    #: A singleton table: there is no "the labels don't exist" state, so
    #: whole-resource delete is refused (delete individual labels instead).
    deletable = False
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
                    raise ValueError(
                        f"{side}.{label_id} must be a mapping of label fields "
                        f"(name/active/topology), got {type(label).__name__}"
                    )
                entry = dict(label)
                # Fill the server-injected defaults on BOTH sides so a partial
                # `set` (e.g. name only) and the server's full record converge
                # — otherwise post-apply verify sees phantom drift and reverts.
                entry.setdefault("active", False)
                entry.setdefault("topology", 0)
                try:
                    entry["topology"] = int(entry["topology"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{side}.{label_id}.topology must be an integer "
                        f"(0=full mesh, 2=hub&spoke), got {entry['topology']!r}"
                    ) from exc
                entry["active"] = bool(entry["active"])
                labels[str(label_id)] = entry
            out[side] = labels
        return out

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired if isinstance(diff.desired, dict) else {"wan": {}, "lan": {}}
        payload = {"wan": desired.get("wan", {}), "lan": desired.get("lan", {})}
        # Full-replace POST; deleteDependencies lets a label bound to a port
        # profile / template still be removed (the operator staged the change).
        ctx.client.post(_PATH, payload, params={"deleteDependencies": "true"})
        return ApplyResult(ok=True, message="interface labels replaced")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # A snapshot recorded as absent (a degenerate GET at commit time)
            # must never be replayed as "POST an empty label table" — that
            # would wipe every label. Refuse loudly instead.
            return ApplyResult(
                ok=False,
                message="no usable snapshot; refusing to POST an empty interface-label table",
            )
        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        ctx.client.post(
            _PATH,
            {"wan": restored.get("wan", {}), "lan": restored.get("lan", {})},
            params={"deleteDependencies": "true"},
        )
        return ApplyResult(ok=True, message="interface labels restored from snapshot")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name="global")]


register(InterfaceLabels())
