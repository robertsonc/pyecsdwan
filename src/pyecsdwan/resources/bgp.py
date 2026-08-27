"""Per-appliance BGP configuration (system + neighbors) — Stage 2, read+write (#16).

Part of epic #3 (Phase 2 — appliance-scope config, via Orchestrator proxy).

Endpoint facts (docs/research/appliance-config.md §BGP,
``specs/appliance-openapi-7.2.0.json``, and two rounds of live probing
against a lab Orchestrator this session — see the Stage 2 note below for
what actually changed):

* ECOS path ``bgp/config/system`` — system-level BGP config, reached through
  the appliance proxy (``GET/POST /appliance/rest?nePk=<pk>&
  url=bgp/config/system``). Confirmed live shape, high confidence — two
  independent samples, one disabled and one enabled-with-a-neighbor::

      {"stale_path_time": 150, "enable_gms_marked": false, "enable": false,
       "remote_as_path_advertise": false, "log_nbr_msgs": true,
       "rtr_id": "0.0.0.0", "asn": 65534, "max_restart_time": 120,
       "graceful_restart_en": false}

      {"stale_path_time": 150, "enable_gms_marked": false, "enable": true,
       "remote_as_path_advertise": true, "log_nbr_msgs": true,
       "route_target": {"0": {"self": 0, "export": "0:0", "import": "0:0"}},
       "rtr_id": "192.168.255.13", "asn": 65534, "neighbor": {...},
       "max_restart_time": 120, "graceful_restart_en": false}

  The enabled sample carries a ``neighbor`` sub-object mirroring the
  standalone neighbor endpoint below, and a ``route_target`` map (EVPN route
  targets, keyed by an index) the Stage 1 sample never showed — both are
  unknown-passthrough here, never stripped or specially interpreted.

* ECOS path ``bgp/config/neighbor`` — neighbor table, same proxy pattern.
  **Now grounded in a real populated sample** (Stage 1 only ever saw an empty
  table)::

      {"10.127.1.1": {"as_override": false, "bfd_desired": false,
       "directly_connected": false, "enable": true, "evpn": false,
       "gms_marked": false, "hold": 9, "ka": 6, "lcl_interface": "any",
       "next_hop_self": true, "password": "", "remote_as": 65001,
       "rtmap_inbound": "default_rtmap_bgp_inbound_br",
       "rtmap_outbound": "default_rtmap_bgp_outbound_br",
       "store_received_routes": true, "type": "Branch"}}

  Keyed by peer IP, confirming the SDK's implied addressing that Stage 1
  could only guess at. The defensive either-keyed-mapping-or-list handling
  from Stage 1 is kept (harmless once confirmed, and cheap insurance against
  a future server version reshaping this), but the real key is now known:
  peer IP, not an arbitrary identifier.

* ``allVrfs`` breadth (``/bgp/config/allVrfs/{system,neighbor}?nePk=``) and
  ``GET /bgp/state?nePk=`` remain read-only informational views, never part
  of the diffable canonical state — exposed as module-level read helpers
  below, unchanged from Stage 1, still read via the Orchestrator's
  convenience endpoint (not the proxy) since nothing writes through them.

Stage 2 — what changed from Stage 1:

* **Read path switched from the Orchestrator's convenience endpoint
  (``ctx.client.get(...)``) to the appliance proxy
  (``ctx.client.appliance_request(...)``).** Stage 1 read via the
  Orchestrator's own ``GET /bgp/config/system?nePk=`` — confirmed real, but
  the Orchestrator spec (``specs/orchestrator-openapi-7.2.0.json``) only
  ever exposed GET on that path; there is no way to write through it.
  ``specs/appliance-openapi-7.2.0.json`` — the ECOS API reached via the
  appliance proxy, i.e. the exact same channel ``deployment``/``vrrp``/
  ``routes`` already read and write through — lists **POST** on
  ``bgp/config/system`` and ``bgp/config/neighbor`` (plus
  ``neighbor/addMultiple``, ``neighbor/deleteMultiple``, and
  ``neighbor/{IP}`` for single-entry ops, unused here — see below). Reading
  and writing through the same channel avoids any risk of the Orchestrator's
  convenience view lagging the appliance's own live state right after a
  proxied write — the same reasoning ``deployment.py`` documents for why it
  reads "a live call to the appliance, not an Orchestrator-cached view."
* ``apply()``/``rollback()`` now perform real writes: POST the full desired
  ``system`` object, then POST the full desired ``neighbor`` table, then
  ``ctx.save_changes(...)``. Full-table POST rather than
  ``addMultiple``/``deleteMultiple`` deltas is deliberate and safe here:
  ``diff.desired`` is already the complete merged state (``txn.build_plan``
  merges any ``set``/``delete`` onto the fetched+normalized current state
  before this ever runs — the same guarantee ``vrrp.py``/``loopback-orch``
  rely on), so a full POST never drops an entry the operator didn't touch.
* ``reversibility`` promoted ``IRREVERSIBLE`` → ``REVERSIBLE``: the full
  object is GET-then-POST, so a snapshot of the pre-change state restores
  exactly via the same write path — see ``rollback()``.

**Not live-verified this session.** Every fact above was captured with
read-only GETs; the write endpoints are confirmed to *exist* by the vendored
OpenAPI spec and follow the exact same proxy pattern five other
already-verified appliance-scope resources use, but no POST was actually
issued against live gear before this shipped — the live write test was
blocked by this environment's own safety tooling before it could run.
**Before trusting this with real changes, test it the way Stage 1 → 2
verification normally would have gone**: GET an appliance's current BGP/OSPF
config, `commit` it right back unchanged (a true no-op — `normalize()` is
idempotent, so an unmodified re-plan should diff empty and skip the write
entirely; if it doesn't diff empty, that's the first thing to fix), *then*
try a real change on a low-stakes target.
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

_SYSTEM_PATH = "bgp/config/system"
_NEIGHBOR_PATH = "bgp/config/neighbor"
_STATE_PATH = "/bgp/state"
_ALLVRFS_SYSTEM_PATH = "/bgp/config/allVrfs/system"
_ALLVRFS_NEIGHBOR_PATH = "/bgp/config/allVrfs/neighbor"

#: Singleton instance name: one BGP config per appliance (system + neighbor
#: table together), like "global" for the orchestrator zone table.
_INSTANCE_NAME = "config"


def _mapping_copy(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a mapping, got {type(value).__name__}")
    return {str(k): v for k, v in value.items()}


#: Field names that identify a neighbor if the shape is ever a list of
#: entries rather than the confirmed keyed-by-peer-IP mapping. Kept as
#: cheap insurance against a future server version reshaping this.
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
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: BGP config always "exists" on an appliance (disabled is still a
    #: configured state) — like the orchestrator zone table, whole-resource
    #: delete is not meaningful.
    deletable = False
    desired_state_doc = (
        "system: {asn, rtr_id, enable, graceful_restart_en, max_restart_time, "
        "stale_path_time, log_nbr_msgs, remote_as_path_advertise, "
        "enable_gms_marked, ...} (unknown fields, e.g. route_target, pass "
        "through). neighbors: map of peer-IP -> {as_override, bfd_desired, "
        "directly_connected, enable, evpn, hold, ka, lcl_interface, "
        "next_hop_self, password, remote_as, rtmap_inbound, rtmap_outbound, "
        "store_received_routes, type, ...}. Full-object replace: apply() "
        "POSTs the complete system object and the complete neighbor table, "
        "never a partial patch."
    )
    #: Config is read+written through the appliance proxy; the state and allVrfs
    #: views are Orchestrator-side reads only (see module docstring).
    endpoints = (
        "appliance GET /bgp/config/system",
        "appliance POST /bgp/config/system",
        "appliance GET /bgp/config/neighbor",
        "appliance POST /bgp/config/neighbor",
        "orchestrator GET /bgp/state",
        "orchestrator GET /bgp/config/allVrfs/system",
        "orchestrator GET /bgp/config/allVrfs/neighbor",
    )

    # -- appliance resolution --------------------------------------------------

    def _ne_pk(self, ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{self.kind} is appliance-scoped; {ref} is missing an appliance")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        ne_pk = self._ne_pk(ctx, ref)
        system = ctx.client.appliance_request("GET", ne_pk, _SYSTEM_PATH)
        neighbors = ctx.client.appliance_request("GET", ne_pk, _NEIGHBOR_PATH)
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

    # -- write side -------------------------------------------------------------

    def _write(self, ctx: Ctx, ref: Ref, desired: RawState, action: str) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        desired_dict = desired if isinstance(desired, dict) else {}
        system = desired_dict.get("system") or {}
        neighbors = desired_dict.get("neighbors") or {}

        ctx.client.appliance_request("POST", ne_pk, _SYSTEM_PATH, json_body=system)
        ctx.client.appliance_request("POST", ne_pk, _NEIGHBOR_PATH, json_body=neighbors)

        save = ctx.save_changes([ne_pk], f"bgp {action}: {ref}")
        if save.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                jobs=[save],
                message=(
                    f"bgp {action} on {ne_pk} not persisted — "
                    f"save-changes {save.state}: {save.detail}"
                ),
            )
        return ApplyResult(
            ok=True,
            jobs=[save],
            message=(
                f"bgp {action} on {ne_pk} persisted "
                f"({len(neighbors)} neighbor(s), asn={system.get('asn', '?')})"
            ),
        )

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        return self._write(ctx, diff.ref, diff.desired, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # BGP config is never legitimately absent on a live appliance
            # (disabled is still a configured state); a snapshot recorded as
            # absent must never be replayed as "POST an empty config".
            return ApplyResult(
                ok=False,
                message=f"no usable snapshot for {ref}; refusing to write an empty bgp config",
            )
        restored = self.normalize(snapshot)
        if not isinstance(restored, dict):
            return ApplyResult(
                ok=False,
                message=f"snapshot for {ref} normalized to nothing; refusing to write it back",
            )
        return self._write(ctx, ref, restored, "rollback")

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
