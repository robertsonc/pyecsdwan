"""Per-appliance BGP configuration (system + neighbors) — Stage 1, read+diff (#16).

Part of epic #3 (Phase 2 — appliance-scope config, via Orchestrator proxy).

Endpoint facts (docs/research/appliance-config.md §BGP, ``pyedgeconnect``
``orch/_bgp.py``, and this session's live, read-only probe against a lab
Orchestrator):

* ``GET /bgp/config/system?nePk=<pk>`` — system-level BGP config. This is
  the 9.3+ query-param form (the SDK also documents a pre-9.3
  ``/bgp/config/system/{neId}`` path form; not used here). Confirmed live
  shape, high confidence::

      {"stale_path_time": 150, "enable_gms_marked": false, "enable": false,
       "remote_as_path_advertise": false, "log_nbr_msgs": true,
       "rtr_id": "0.0.0.0", "asn": 65534, "max_restart_time": 120,
       "graceful_restart_en": false}

* ``GET /bgp/config/neighbor?nePk=<pk>`` — neighbor config. The SDK's
  ``get_appliance_bgp_neighbors`` docstring gives the endpoint but not the
  field shape ("response fields undocumented in SDK" per
  docs/research/appliance-config.md). The live lab appliance probed this
  session has no neighbors configured and returned ``{}`` — the endpoint is
  reachable and returns valid JSON, but there is no populated sample to
  ground per-neighbor fields against. ``normalize()`` therefore treats the
  neighbor table as an opaque mapping (key -> field mapping, presumably
  keyed by peer address per the SDK's implied addressing) and passes every
  field through unexamined. This is the one part of this module NOT grounded
  in a live populated payload — revisit if a real neighbor entry disagrees.

* ``allVrfs`` breadth (``/bgp/config/allVrfs/{system,neighbor}?nePk=``) and
  ``GET /bgp/state?nePk=`` are read-only informational views, never part of
  the diffable canonical state — exposed as module-level read helpers below.

**No modeled write endpoint exists** on either the Orchestrator or the
appliance API — confirmed by docs/research/appliance-config.md ("BGP and
OSPF have NO modeled write endpoint on either side") and independently by
this session's live, read-only probing (deliberately not probed further for
a write path — out of scope for Stage 1). This is a deliberate,
permanent-for-now design, not an unfinished stub: ``apply()`` raises
``NotImplementedError`` naming the two candidate write paths issue #16
Stage 2 must verify before this resource can accept writes (ECOS
``bgpConfig`` via the appliance proxy, or rendering to ``broadcastCli``).
``tier`` stays ``Tier.CURATED`` — ``fetch``/``normalize``/``diff``/
``managed_by`` are all real — but ``reversibility = IRREVERSIBLE`` so the
transaction engine's own guard (``txn._guard``) refuses ``commit`` without
``--force`` and refuses ``commit confirm`` outright for this kind, rather
than ever reaching the ``NotImplementedError``. ``show``/drift work today;
only writes are blocked.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

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
from pyecsdwan.ownership import owning_group
from pyecsdwan.registry import register

log = structlog.get_logger("pyecsdwan.resources.bgp")

_SYSTEM_PATH = "/bgp/config/system"
_NEIGHBOR_PATH = "/bgp/config/neighbor"
_STATE_PATH = "/bgp/state"
_ALLVRFS_SYSTEM_PATH = "/bgp/config/allVrfs/system"
_ALLVRFS_NEIGHBOR_PATH = "/bgp/config/allVrfs/neighbor"

#: Singleton instance name: one BGP config per appliance (system + neighbor
#: table together), like "global" for the orchestrator zone table.
_INSTANCE_NAME = "config"

_WRITE_PATH_TODO = (
    "appliance/bgp has no modeled write endpoint on either the Orchestrator "
    "or the appliance API (docs/research/appliance-config.md §BGP; "
    "confirmed independently by live read-only probing this session). "
    "TODO(#16 Stage 2): verify a write path — either ECOS `bgpConfig` via "
    "the appliance proxy (POST /appliance/rest?nePk=&url=bgpConfig) or "
    "rendering desired state to `broadcastCli` — against a live appliance, "
    "then implement apply()/rollback(), wire ctx.save_changes(...), and set "
    "reversibility off IRREVERSIBLE."
)


def _mapping_copy(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a mapping, got {type(value).__name__}")
    return {str(k): v for k, v in value.items()}


#: Field names that plausibly identify a neighbor if the live shape turns
#: out to be a list of entries rather than a keyed mapping (unconfirmed —
#: see module docstring). Falls back to the list index when none match.
_NEIGHBOR_KEY_FIELDS = ("peer_ip", "peerIp", "peerAddr", "addr", "ip", "neighbor")


def _neighbor_table(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, list):
        items: dict[str, Any] = {}
        for idx, entry in enumerate(value):
            if not isinstance(entry, Mapping):
                raise ValueError(
                    f"bgp neighbor entry {idx} must be a mapping of fields, "
                    f"got {type(entry).__name__}"
                )
            key = next((str(entry[f]) for f in _NEIGHBOR_KEY_FIELDS if f in entry), str(idx))
            items[key] = entry
        raw = items
    else:
        raw = _mapping_copy(value, "bgp neighbor config")
    neighbors: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"bgp neighbor {key!r} must be a mapping of fields, "
                f"got {type(entry).__name__}"
            )
        neighbors[key] = {str(k): v for k, v in entry.items()}
    return {key: neighbors[key] for key in sorted(neighbors)}


class Bgp(Resource):
    kind = "appliance/bgp"
    scope = Scope.APPLIANCE
    # No write path exists at all (see module docstring): a confirm window
    # would be fake safety, and there is no rollback to fall back on. This is
    # the only honest declaration until Stage 2 verifies a write endpoint.
    reversibility = Reversibility.IRREVERSIBLE
    tier = Tier.CURATED
    #: BGP config always "exists" on an appliance (disabled is still a
    #: configured state, per the live `enable: false` sample) — like the
    #: orchestrator zone table, whole-resource delete is not meaningful.
    deletable = False
    desired_state_doc = (
        "system: {asn, rtr_id, enable, graceful_restart_en, max_restart_time, "
        "stale_path_time, log_nbr_msgs, remote_as_path_advertise, "
        "enable_gms_marked, ...} (unknown fields pass through). "
        "neighbors: map of neighbor-key -> field mapping (shape unconfirmed — "
        "see module docstring). Read+diff only (issue #16 Stage 1): apply() "
        "always raises NotImplementedError, so this kind can be shown and "
        "diffed but never committed."
    )

    # -- read side ------------------------------------------------------------

    def _ne_pk(self, ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{self.kind} is appliance-scoped; {ref} is missing an appliance")
        return ctx.resolver.ne_pk_for(ref.appliance)

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        ne_pk = self._ne_pk(ctx, ref)
        system = ctx.client.get(_SYSTEM_PATH, params={"nePk": ne_pk})
        neighbors = ctx.client.get(_NEIGHBOR_PATH, params={"nePk": ne_pk})
        raw: dict[str, Any] = {}
        if isinstance(system, dict):
            raw["system"] = system
        if isinstance(neighbors, (dict, list)):
            raw["neighbors"] = neighbors
        return raw

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict) or not raw:
            return None
        system_raw = raw.get("system") or {}
        system = _mapping_copy(system_raw, "bgp system config")
        system = {key: system[key] for key in sorted(system)}
        neighbors = _neighbor_table(raw.get("neighbors") or {})
        return {"system": system, "neighbors": neighbors}

    def managed_by(self, ctx: Ctx, ref: Ref) -> str | None:
        if ref.appliance is None:
            return None
        ne_pk = ctx.resolver.ne_pk_for(ref.appliance)
        return owning_group(ctx, self.kind, ne_pk)

    # -- write side (Stage 2, not yet available) -------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        raise NotImplementedError(_WRITE_PATH_TODO)

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        # Never reached in practice: reversibility=IRREVERSIBLE means the
        # transaction guard refuses to apply this kind without --force, and
        # apply() itself raises before any write lands — so there is never a
        # write to roll back. Overridden anyway (rather than inheriting the
        # base class's generic NotImplementedError) so a forced commit that
        # somehow gets this far fails with the same, specific explanation.
        raise NotImplementedError(_WRITE_PATH_TODO)

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name=_INSTANCE_NAME, appliance=str(a["hostName"]))
            for a in ctx.resolver.appliances()
            if a.get("hostName")
        ]


# -- read-only views ----------------------------------------------------------


def state(ctx: Ctx, ne_pk: str) -> dict[str, Any]:
    """Live BGP state (routes/peers as currently up) — informational only,
    never part of the diffable canonical state (see module docstring)."""
    raw = ctx.client.get(_STATE_PATH, params={"nePk": ne_pk})
    return raw if isinstance(raw, dict) else {}


def all_vrfs_config(ctx: Ctx, ne_pk: str) -> dict[str, Any]:
    """``allVrfs`` breadth for system + neighbor config, keyed by VRF —
    read-only view; the segment-scoped default view above is canonical."""
    system = ctx.client.get(_ALLVRFS_SYSTEM_PATH, params={"nePk": ne_pk})
    neighbors = ctx.client.get(_ALLVRFS_NEIGHBOR_PATH, params={"nePk": ne_pk})
    return {
        "system": system if isinstance(system, dict) else {},
        "neighbors": neighbors if isinstance(neighbors, dict) else {},
    }


register(Bgp())
