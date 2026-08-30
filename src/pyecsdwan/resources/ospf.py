"""Per-appliance OSPF configuration (system + interfaces) — Stage 2, read+write (#17).

Part of epic #3 (Phase 2 — appliance-scope config, via Orchestrator proxy).
Mirrors ``resources/bgp.py`` (#16) closely — read that module's docstring
for the full Stage 1 → Stage 2 rationale; this one only covers what's
OSPF-specific.

Endpoint facts (docs/research/appliance-config.md §OSPF,
``specs/appliance-openapi-7.2.0.json``, and two rounds of live probing
against a lab Orchestrator this session):

* ECOS path ``ospf/config/system`` — system-level OSPF config, reached
  through the appliance proxy. Confirmed live shape, high confidence — two
  independent samples, one disabled and one still-disabled-but-with-a-real-
  router-id::

      {"redistMapToOSPF": "default_rtmap_to_ospf", "enable": false,
       "routerId": "0.0.0.0", "opaque_enable": true}

      {"redistMapToOSPF": "default_rtmap_to_ospf", "enable": false,
       "routerId": "192.168.255.13", "opaque_enable": true}

* ECOS path ``ospf/config/interfaces`` — per-interface OSPF config, same
  proxy pattern. **Now grounded in a real populated sample** (Stage 1 only
  ever saw an empty table)::

      {"lan0": {"cost": 1, "area": "0.0.0.0", "authKey": "",
       "md5Password": "", "authType": "None", "comment": "", "priority": 1,
       "transmitDelay": 1, "retransmitInterval": 4, "helloInterval": 10,
       "deadInterval": 40, "md5Key": 0, "adminStatus": true,
       "bfdDesired": false},
       "lan1": {..., "area": "1.0.0.0", ...}}

  Keyed by interface name, confirming the SDK's implied addressing.

* ``specs/appliance-openapi-7.2.0.json`` also lists a writable
  ``ospf/config/areas`` path (per-area stub/NSSA settings, distinct from the
  per-interface ``area`` membership field above) — **not implemented here**.
  Stage 1 never read it, no live sample grounds its shape, and issue #17's
  own scope never named it. Left as a follow-up if per-area settings (as
  opposed to per-interface area membership, which this resource does cover)
  turn out to be needed.

* ``GET /ospf/state/{system,interfaces,neighbors}?nePk=`` remain read-only
  informational views, unchanged from Stage 1.

Stage 2 — what changed from Stage 1: identical rationale to ``bgp.py`` —
read path switched from the Orchestrator's convenience GET-only endpoint to
the appliance proxy (which the spec confirms supports POST on both
``ospf/config/system`` and ``ospf/config/interfaces``); ``apply()``/
``rollback()`` now POST the full desired system object and full desired
interface table (safe as a full replace — see ``bgp.py``'s docstring for
why); ``reversibility`` promoted ``IRREVERSIBLE`` → ``REVERSIBLE``.

**Not live-verified this session** — same caveat as ``bgp.py``: the write
endpoints are spec-confirmed and follow the identical proxy pattern five
other already-verified resources use, but no POST was actually issued
against live gear before this shipped. Test with a no-op round-trip first.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from pyecsdwan import ownership
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

log = structlog.get_logger("pyecsdwan.resources.ospf")

_SYSTEM_PATH = "ospf/config/system"
_INTERFACES_PATH = "ospf/config/interfaces"
_STATE_SYSTEM_PATH = "/ospf/state/system"
_STATE_INTERFACES_PATH = "/ospf/state/interfaces"
_STATE_NEIGHBORS_PATH = "/ospf/state/neighbors"

#: Singleton instance name: one OSPF config per appliance (system +
#: interfaces table together), like "global" for the orchestrator zone table.
_INSTANCE_NAME = "config"


def _mapping_copy(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a mapping, got {type(value).__name__}")
    return {str(k): v for k, v in value.items()}


#: Field names that identify an interface if the shape is ever a list of
#: entries rather than the confirmed keyed-by-interface-name mapping. Kept
#: as cheap insurance against a future server version reshaping this.
_INTERFACE_KEY_FIELDS = ("ifName", "if_name", "intf", "interface", "name")


def _interface_table(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, list):
        items: dict[str, Any] = {}
        for idx, entry in enumerate(value):
            if not isinstance(entry, Mapping):
                raise ValueError(
                    f"ospf interface entry {idx} must be a mapping of fields, "
                    f"got {type(entry).__name__}"
                )
            key = next(
                (str(entry[f]) for f in _INTERFACE_KEY_FIELDS if f in entry), str(idx)
            )
            items[key] = entry
        raw = items
    else:
        raw = _mapping_copy(value, "ospf interfaces config")
    interfaces: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"ospf interface {key!r} must be a mapping of fields, "
                f"got {type(entry).__name__}"
            )
        interfaces[key] = {str(k): v for k, v in entry.items()}
    return {key: interfaces[key] for key in sorted(interfaces)}


class Ospf(Resource):
    #: The `ospf` template governs per-VRF timers and authentication —
    #: `helloInterval`, `deadInterval`, `transmitDelay`, `retransmitInterval`,
    #: auth keys — and declares no interfaces or areas. Which interfaces run
    #: OSPF is local (spec 004 L3, live 9.7 template body).
    template_governs = ("system",)
    kind = "appliance/ospf"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: OSPF config always "exists" on an appliance (disabled is still a
    #: configured state) — like the orchestrator zone table, whole-resource
    #: delete is not meaningful.
    deletable = False
    desired_state_doc = (
        "system: {routerId, enable, redistMapToOSPF, opaque_enable, ...} "
        "(unknown fields pass through). interfaces: map of interface-name -> "
        "{cost, area, authKey, md5Password, authType, comment, priority, "
        "transmitDelay, retransmitInterval, helloInterval, deadInterval, "
        "md5Key, adminStatus, bfdDesired}. Full-object replace: apply() "
        "POSTs the complete system object and the complete interface table, "
        "never a partial patch. Per-area settings (ospf/config/areas) are "
        "not modeled by this resource — only per-interface area membership."
    )
    #: Config via the proxy; the three state views are Orchestrator reads.
    endpoints = (
        "appliance GET /ospf/config/system",
        "appliance POST /ospf/config/system",
        "appliance GET /ospf/config/interfaces",
        "appliance POST /ospf/config/interfaces",
        "orchestrator GET /ospf/state/system",
        "orchestrator GET /ospf/state/interfaces",
        "orchestrator GET /ospf/state/neighbors",
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
        interfaces = ctx.client.appliance_request("GET", ne_pk, _INTERFACES_PATH)
        raw: dict[str, Any] = {}
        if isinstance(system, dict):
            raw["system"] = system
        if isinstance(interfaces, (dict, list)):
            raw["interfaces"] = interfaces
        return raw

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict) or not raw:
            return None
        system_raw = raw.get("system") or {}
        system = _mapping_copy(system_raw, "ospf system config")
        system = {key: system[key] for key in sorted(system)}
        interfaces = _interface_table(raw.get("interfaces") or {})
        return {"system": system, "interfaces": interfaces}

    def managed_by(self, ctx: Ctx, ref: Ref, diff: Diff | None = None) -> Ownership:
        if ref.appliance is None:
            return Ownership.unknown(
                f"{ref.kind} is appliance-scope but the ref names no appliance, "
                f"so no nePk resolves and ownership cannot be checked"
            )
        ne_pk = ctx.resolver.ne_pk_for(ref.appliance)
        return ownership.resolve(ctx, self, ne_pk, diff)

    # -- write side -------------------------------------------------------------

    def _write(self, ctx: Ctx, ref: Ref, desired: RawState, action: str) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        desired_dict = desired if isinstance(desired, dict) else {}
        system = desired_dict.get("system") or {}
        interfaces = desired_dict.get("interfaces") or {}

        ctx.client.appliance_request("POST", ne_pk, _SYSTEM_PATH, json_body=system)
        ctx.client.appliance_request("POST", ne_pk, _INTERFACES_PATH, json_body=interfaces)

        save = ctx.save_changes([ne_pk], f"ospf {action}: {ref}")
        if save.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                jobs=[save],
                message=(
                    f"ospf {action} on {ne_pk} not persisted — "
                    f"save-changes {save.state}: {save.detail}"
                ),
            )
        return ApplyResult(
            ok=True,
            jobs=[save],
            message=(
                f"ospf {action} on {ne_pk} persisted "
                f"({len(interfaces)} interface(s), enable={system.get('enable', '?')})"
            ),
        )

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        return self._write(ctx, diff.ref, diff.desired, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # OSPF config is never legitimately absent on a live appliance
            # (disabled is still a configured state); a snapshot recorded as
            # absent must never be replayed as "POST an empty config".
            return ApplyResult(
                ok=False,
                message=f"no usable snapshot for {ref}; refusing to write an empty ospf config",
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
    """Live OSPF state (system, interfaces, neighbors as currently up) —
    informational only, never part of the diffable canonical state (see
    module docstring)."""
    system = ctx.client.get(_STATE_SYSTEM_PATH, params={"nePk": ne_pk})
    interfaces = ctx.client.get(_STATE_INTERFACES_PATH, params={"nePk": ne_pk})
    neighbors = ctx.client.get(_STATE_NEIGHBORS_PATH, params={"nePk": ne_pk})
    return {
        "system": system if isinstance(system, dict) else {},
        "interfaces": interfaces if isinstance(interfaces, (dict, list)) else {},
        "neighbors": neighbors if isinstance(neighbors, (dict, list)) else {},
    }


register(Ospf())
