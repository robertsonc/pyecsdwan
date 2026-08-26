"""Per-appliance OSPF configuration (system + interfaces) — Stage 1, read+diff (#17).

Part of epic #3 (Phase 2 — appliance-scope config, via Orchestrator proxy).

Endpoint facts (docs/research/appliance-config.md §OSPF, ``pyedgeconnect``
``orch/_ospf.py``, and this session's live, read-only probe against a lab
Orchestrator):

* ``GET /ospf/config/system?nePk=<pk>`` — system-level OSPF config. This is
  the 9.3+ query-param form (the SDK also documents a pre-9.3
  ``/ospf/config/system/{neId}`` path form; not used here). Confirmed live
  shape, high confidence::

      {"redistMapToOSPF": "default_rtmap_to_ospf", "enable": false,
       "routerId": "0.0.0.0", "opaque_enable": true}

* ``GET /ospf/config/interfaces?nePk=<pk>`` — per-interface OSPF config. The
  SDK's ``get_appliance_ospf_interfaces_config`` docstring gives the
  endpoint but not the field shape. The live lab appliance probed this
  session has no OSPF interfaces configured and returned an empty result —
  the endpoint is reachable and returns valid JSON, but there is no
  populated sample to ground per-interface fields against. ``normalize()``
  therefore treats the interface table as an opaque mapping (key -> field
  mapping, presumably keyed by interface name per the SDK's implied
  addressing) and passes every field through unexamined, following the same
  defensive either-keyed-mapping-or-list handling ``bgp.py`` uses for its
  analogous "neighbor" field. This is the one part of this module NOT
  grounded in a live populated payload — revisit if a real interface entry
  disagrees.

* ``GET /ospf/state/{system,interfaces,neighbors}?nePk=`` are read-only
  informational views, never part of the diffable canonical state — exposed
  as module-level read helpers below.

**No modeled write endpoint exists** on either the Orchestrator or the
appliance API — confirmed by docs/research/appliance-config.md ("BGP and
OSPF have NO modeled write endpoint on either side") and independently by
this session's live, read-only probing (deliberately not probed further for
a write path — out of scope for Stage 1). This is a deliberate,
permanent-for-now design, not an unfinished stub: ``apply()`` raises
``NotImplementedError`` naming the two candidate write paths issue #17
Stage 2 must verify before this resource can accept writes (ECOS raw OSPF
config path via the appliance proxy, or rendering to ``broadcastCli``).
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

log = structlog.get_logger("pyecsdwan.resources.ospf")

_SYSTEM_PATH = "/ospf/config/system"
_INTERFACES_PATH = "/ospf/config/interfaces"
_STATE_SYSTEM_PATH = "/ospf/state/system"
_STATE_INTERFACES_PATH = "/ospf/state/interfaces"
_STATE_NEIGHBORS_PATH = "/ospf/state/neighbors"

#: Singleton instance name: one OSPF config per appliance (system +
#: interfaces table together), like "global" for the orchestrator zone table.
_INSTANCE_NAME = "config"

_WRITE_PATH_TODO = (
    "appliance/ospf has no modeled write endpoint on either the Orchestrator "
    "or the appliance API (docs/research/appliance-config.md §OSPF; "
    "confirmed independently by live read-only probing this session). "
    "TODO(#17 Stage 2): verify a write path — either the raw appliance OSPF "
    "config path via the appliance proxy (POST /appliance/rest?nePk=&url=...) "
    "or rendering desired state to `broadcastCli` — against a live "
    "appliance, then implement apply()/rollback(), wire "
    "ctx.save_changes(...), and set reversibility off IRREVERSIBLE."
)


def _mapping_copy(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a mapping, got {type(value).__name__}")
    return {str(k): v for k, v in value.items()}


#: Field names that plausibly identify an interface if the live shape turns
#: out to be a list of entries rather than a keyed mapping (unconfirmed —
#: see module docstring). Falls back to the list index when none match.
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
    kind = "appliance/ospf"
    scope = Scope.APPLIANCE
    # No write path exists at all (see module docstring): a confirm window
    # would be fake safety, and there is no rollback to fall back on. This is
    # the only honest declaration until Stage 2 verifies a write endpoint.
    reversibility = Reversibility.IRREVERSIBLE
    tier = Tier.CURATED
    #: OSPF config always "exists" on an appliance (disabled is still a
    #: configured state, per the live `enable: false` sample) — like the
    #: orchestrator zone table, whole-resource delete is not meaningful.
    deletable = False
    desired_state_doc = (
        "system: {routerId, enable, redistMapToOSPF, opaque_enable, ...} "
        "(unknown fields pass through). interfaces: map of interface-key -> "
        "field mapping (shape unconfirmed — see module docstring). Read+diff "
        "only (issue #17 Stage 1): apply() always raises NotImplementedError, "
        "so this kind can be shown and diffed but never committed."
    )

    # -- read side ------------------------------------------------------------

    def _ne_pk(self, ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{self.kind} is appliance-scoped; {ref} is missing an appliance")
        return ctx.resolver.ne_pk_for(ref.appliance)

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        ne_pk = self._ne_pk(ctx, ref)
        system = ctx.client.get(_SYSTEM_PATH, params={"nePk": ne_pk})
        interfaces = ctx.client.get(_INTERFACES_PATH, params={"nePk": ne_pk})
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
