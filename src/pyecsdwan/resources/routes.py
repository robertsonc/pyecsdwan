"""Locally-configured static routes / subnets — appliance-scope (#15).

Endpoint facts (ECOS, reached through the Orchestrator's appliance proxy —
the ``ctx.client.appliance_request`` convention every ``resources/`` plugin
in this scope uses):

* ``GET subnets3/all`` — broad view: operator-configured routes plus
  learned/discovered ones (OSPF, BGP, connected, ...). Read-only, exposed
  below as :func:`all_routes`; never part of the diffable canonical state —
  an operator can't "set" a route the fabric merely discovered.
* ``GET subnets3/configured`` — operator-configured routes only. This is
  ``fetch()``'s source of truth for diffing desired state.
* ``POST subnets3/configured/addMultiple`` — net-new, non-destructive: merges
  the posted prefixes into the configured table.
* ``POST subnets3/configured/deleteMultiple`` — removes the named prefixes
  from the configured table.
* The full-replace ``POST subnets3/configured`` write is deliberately never
  used here — it replaces the *whole* table (destructive). This resource
  computes add/delete deltas instead, so ``apply()``/``rollback()`` only
  ever touch the prefixes one operation actually changed, earning
  ``Reversibility.COMPENSABLE`` (rollback recomputes the delta between the
  pre-apply snapshot and the current server table, and issues the precise
  inverse add/delete — not a full-table restore).

Confirmed live payload shape (captured read-only this session against a live
lab Orchestrator's ``subnets3/configured`` — see issue #15)::

    {"prefix": {"<cidr>": {
        "self": "<cidr>", "advert": bool, "advert_bgp": bool,
        "advert_ospf": bool, "local": bool,
        "nhop": {"<nhopIp>": {"self": "<nhopIp>", "interface": {
            "<name>": {"self": "<name>", "comment": str, "dest_mac": str,
                       "dir": str, "gms_marked": bool, "label": int,
                       "metric": int, "no_subshared": bool, "vni": int,
                       "vxlan": bool, "zone_id": int}}}}}}}

``zone_id: 65534`` is the confirmed "no zone" sentinel. ``self`` at every
level just echoes its own parent key; ``normalize()`` strips it (``apply()``
re-injects it on write, matching the ``security_policy`` precedent) so user
intent and server state diff cleanly.

Spec-vs-live divergence: only ``GET subnets3/configured`` was captured live.
The write shape for ``addMultiple``/``deleteMultiple`` was NOT observed —
this module assumes ``addMultiple`` takes the same ``{"prefix": {...}}``
nesting as the read (a subset to merge in) and ``deleteMultiple`` takes
``{"prefixes": [<cidr>, ...]}``, the "same shape as reads, list-of-keys for
deletes" convention used elsewhere in the vendored SDK. Verify against a live
Orchestrator before relying on this in production — apply()/rollback() only
ever request exactly the prefixes that changed, so a shape mismatch fails
loudly (API error) rather than corrupting unrelated routes.

Ownership: the Orchestrator exposes no per-section "managed-by" field (see
``ownership.py``), so this resource joins template association x selection
against ``KIND_TO_TEMPLATE_SECTIONS["appliance/routes"]`` (``subnets``/
``routes`` — the latter CONFIRMED real against a live Default Template
Group's section listing), and additionally prefers the per-interface-entry
``gms_marked`` flag when present: a route the fabric itself marked
``gms_marked`` is server-owned with per-object precision the coarser
template-section join can't give.
"""

from __future__ import annotations

import copy
import ipaddress
from collections.abc import Mapping
from typing import Any

import structlog

from pyecsdwan import ownership
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

log = structlog.get_logger("pyecsdwan.resources.routes")

_ALL_PATH = "subnets3/all"
_CONFIGURED_PATH = "subnets3/configured"
_ADD_PATH = "subnets3/configured/addMultiple"
_DELETE_PATH = "subnets3/configured/deleteMultiple"

#: Confirmed "no zone" sentinel (issue #15).
_NO_ZONE = 65534


def _sort_key(cidr: str) -> tuple[int, ...]:
    """CIDR-numeric sort key; falls back to a string bucket for anything that
    doesn't parse as a network (defensive — never seen live, but a malformed
    key must not crash normalize())."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return (1, 0, 0, 0)
    return (0, net.version, int(net.network_address), net.prefixlen)


def _interface_entry(where: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise ValueError(
            f"{where} must be a mapping of interface fields (metric, dir, "
            f"zone_id, ...), got {type(entry).__name__}"
        )
    # gms_marked is bookkeeping (who wrote this route), not user intent — it
    # is stripped here so it never causes phantom diffs, and read back
    # separately (from the raw fetch) by managed_by() below.
    out: dict[str, Any] = {
        str(k): v for k, v in entry.items() if k not in ("self", "gms_marked")
    }
    out.setdefault("zone_id", _NO_ZONE)
    out.setdefault("metric", 0)
    out.setdefault("dir", "ANY")
    return out


def _nhop_entry(where: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise ValueError(
            f"{where} must be a mapping ({{interface: {{...}}}}), "
            f"got {type(entry).__name__}"
        )
    iface_raw = entry.get("interface") or {}
    if not isinstance(iface_raw, Mapping):
        raise ValueError(
            f"{where}.interface must be a mapping of name -> route fields, "
            f"got {type(iface_raw).__name__}"
        )
    out: dict[str, Any] = {str(k): v for k, v in entry.items() if k not in ("self", "interface")}
    out["interface"] = {
        str(name): _interface_entry(f"{where}.interface.{name}", val)
        for name, val in iface_raw.items()
    }
    return out


def _route_entry(cidr: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise ValueError(
            f"route {cidr!r} must be a mapping of route fields (nhop, ...), "
            f"got {type(entry).__name__}"
        )
    nhop_raw = entry.get("nhop") or {}
    if not isinstance(nhop_raw, Mapping):
        raise ValueError(
            f"route {cidr!r}.nhop must be a mapping of nhop-ip -> "
            f"{{interface: {{...}}}}, got {type(nhop_raw).__name__}"
        )
    out: dict[str, Any] = {str(k): v for k, v in entry.items() if k not in ("self", "nhop")}
    out["nhop"] = {
        str(ip): _nhop_entry(f"route {cidr!r}.nhop.{ip}", val) for ip, val in nhop_raw.items()
    }
    # Server-observed defaults (see module docstring's captured example),
    # filled on both sides so a partial `set` (e.g. metric only) converges
    # with the server's full record instead of drifting forever.
    out.setdefault("advert", False)
    out.setdefault("advert_bgp", False)
    out.setdefault("advert_ospf", False)
    out.setdefault("local", True)
    return out


def _inject_self(prefixes: Mapping[str, Any]) -> dict[str, Any]:
    """Re-add the ``self`` echoes the confirmed read shape carries at every
    level, for outgoing addMultiple bodies (see module docstring)."""
    out: dict[str, Any] = {}
    for cidr, entry in prefixes.items():
        route = copy.deepcopy(entry) if isinstance(entry, dict) else {}
        route["self"] = cidr
        nhops = route.get("nhop")
        if isinstance(nhops, dict):
            for nhop_ip, nhop in nhops.items():
                if not isinstance(nhop, dict):
                    continue
                nhop["self"] = nhop_ip
                ifaces = nhop.get("interface")
                if isinstance(ifaces, dict):
                    for name, iface in ifaces.items():
                        if isinstance(iface, dict):
                            iface["self"] = name
        out[cidr] = route
    return out


def _has_gms_marked(raw: RawState) -> bool:
    if not isinstance(raw, dict):
        return False
    for route in (raw.get("prefix") or {}).values():
        if not isinstance(route, Mapping):
            continue
        for nhop in (route.get("nhop") or {}).values():
            if not isinstance(nhop, Mapping):
                continue
            for iface in (nhop.get("interface") or {}).values():
                if isinstance(iface, Mapping) and bool(iface.get("gms_marked")):
                    return True
    return False


class Routes(Resource):
    kind = "appliance/routes"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.COMPENSABLE
    tier = Tier.CURATED
    #: The configured-routes table always exists (possibly empty) on any
    #: appliance — there is no "absent" state, so whole-resource delete is
    #: refused; delete individual `prefix.<cidr>` entries instead.
    deletable = False
    desired_state_doc = (
        "prefix: map of CIDR -> {advert, advert_bgp, advert_ospf, local, "
        "nhop: {nhopIp: {interface: {name: {metric, dir, zone_id, comment, "
        "...}}}}}. zone_id 65534 = no zone. Applies as add/delete deltas "
        "against subnets3/configured/{addMultiple,deleteMultiple} — never a "
        "full-table replace, so rollback is a precise inverse of the change."
    )
    #: Reconciled with the add/delete multiple forms; all_routes() is a view.
    endpoints = (
        "appliance GET /subnets3/configured",
        "appliance POST /subnets3/configured/addMultiple",
        "appliance POST /subnets3/configured/deleteMultiple",
        "appliance GET /subnets3/all",
    )

    @staticmethod
    def _ne_pk(ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{ref.kind} is appliance-scoped; ref.appliance is required")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.appliance_request("GET", self._ne_pk(ctx, ref), _CONFIGURED_PATH)
        return raw if isinstance(raw, dict) else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return {"prefix": {}}
        prefix_raw = raw.get("prefix") or {}
        if not isinstance(prefix_raw, Mapping):
            raise ValueError(
                f"'prefix' must be a mapping of cidr -> route entry, "
                f"got {type(prefix_raw).__name__}"
            )
        prefixes = {str(cidr): _route_entry(str(cidr), entry) for cidr, entry in prefix_raw.items()}
        ordered = {cidr: prefixes[cidr] for cidr in sorted(prefixes, key=_sort_key)}
        return {"prefix": ordered}

    def managed_by(self, ctx: Ctx, ref: Ref) -> str | None:
        ne_pk = self._ne_pk(ctx, ref)
        # Per-object precision first: a route the fabric itself flagged
        # gms_marked is server-owned regardless of what any template selects.
        if _has_gms_marked(self.fetch(ctx, ref)):
            return "gms (gms_marked route entry present on this appliance)"
        return ownership.owning_group(ctx, self.kind, ne_pk)

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        current = diff.current if isinstance(diff.current, dict) else {"prefix": {}}
        desired = diff.desired if isinstance(diff.desired, dict) else {"prefix": {}}
        return self._reconcile(ctx, diff.ref, current, desired, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        target = self.normalize(snapshot)
        assert isinstance(target, dict)
        current = self.normalize(self.fetch(ctx, ref))
        assert isinstance(current, dict)
        # The precise inverse: reconcile *toward* the pre-apply snapshot from
        # whatever the table looks like now, so only what this operation
        # actually changed moves — never a full-table restore.
        return self._reconcile(ctx, ref, current, target, "rollback")

    def _reconcile(
        self, ctx: Ctx, ref: Ref, current: dict[str, Any], desired: dict[str, Any], verb: str
    ) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        current_prefixes: dict[str, Any] = current.get("prefix") or {}
        desired_prefixes: dict[str, Any] = desired.get("prefix") or {}
        # A changed (not just added/removed) prefix appears in both lists:
        # delete the stale entry, then add the new one back — still a pure
        # add/delete delta, never a full replace.
        to_remove = sorted(
            (c for c in current_prefixes if current_prefixes.get(c) != desired_prefixes.get(c)),
            key=_sort_key,
        )
        to_add = sorted(
            (c for c in desired_prefixes if desired_prefixes.get(c) != current_prefixes.get(c)),
            key=_sort_key,
        )
        if not to_remove and not to_add:
            return ApplyResult.noop()
        if to_remove:
            ctx.client.appliance_request(
                "POST", ne_pk, _DELETE_PATH, json_body={"prefixes": to_remove}
            )
        if to_add:
            payload = _inject_self({c: desired_prefixes[c] for c in to_add})
            ctx.client.appliance_request("POST", ne_pk, _ADD_PATH, json_body={"prefix": payload})
        log.debug("routes_reconcile", ref=str(ref), verb=verb, added=to_add, removed=to_remove)
        save = ctx.save_changes([ne_pk], f"{verb} static routes on {ref}")
        if save.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                message=f"routes {verb} not persisted — save-changes {save.state}: {save.detail}",
                jobs=[save],
            )
        return ApplyResult(
            ok=True,
            jobs=[save],
            message=f"routes {verb}: +{len(to_add)}/-{len(to_remove)} prefix(es)",
        )

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name="global", appliance=name)
            for name in ctx.resolver.appliance_names()
        ]


# -- read-only views ----------------------------------------------------------


def all_routes(ctx: Ctx, appliance: str) -> dict[str, Any]:
    """Broad view: operator-configured routes plus learned/discovered ones.

    Never part of the diffable canonical state (an operator can't "set" a
    route the fabric merely discovered) — exposed for `show`/audit only.
    """
    ne_pk = ctx.resolver.ne_pk_for(appliance)
    raw = ctx.client.appliance_request("GET", ne_pk, _ALL_PATH)
    return raw if isinstance(raw, dict) else {"prefix": {}}


register(Routes())
