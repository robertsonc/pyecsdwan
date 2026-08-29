"""DHCP server/relay — appliance-scope resource composed over deployment (#13).

There is no dedicated ``/dhcp*`` ECOS endpoint. DHCP configuration is a
subtree of the same ``deployment`` object issue #12's
:mod:`pyecsdwan.resources.deployment` reads/writes:

* Per-LAN-interface server/relay config: ``modeIfs[].applianceIPs[].dhcpd =
  {type: "server"|"relay"|"none", server: {prefix, ipStart, ipEnd, gw[],
  dns[], ntpd[], netbios[], netbiosNodeType, maxLease, defaultLease,
  ip_range{}, options{}, host{}, failover}, relay: {dhcpserver[], option82,
  option82_policy}}``.
* Top-level failover config, keyed by interface: ``dhcpFailover``.

Both facts are confirmed by ``deployment.py``'s own docstring/live capture
(``_DHCPD_LOCATION``) — this module treats that as read-only reference for
the endpoint/shape only; it does not import from or otherwise couple to
``deployment.py``, so the two plugins evolve independently. The handful of
proxy-call lines (GET/validate/POST through ``ctx.client.appliance_request``)
are duplicated here rather than shared.

Endpoint facts, mirrored from deployment.py:

* Read: ECOS path ``deployment`` (``GET /appliance/rest?nePk=<pk>&url=
  deployment``) — a live call to the appliance.
* Validate: ECOS path ``deployment/validate`` (POST candidate, get back
  ``{err, rebootRequired}``) — called before every write.
* Write: ECOS path ``deployment`` (POST) — full-object replace, same as
  every other write against this endpoint.
* Persist: ``ctx.save_changes([ne_pk], ...)`` once per apply()/rollback().

Full-object-replace design (read this before touching apply()/rollback()):
the ``deployment`` object has no partial PATCH, so this resource's
``fetch()`` reads the *whole* object, but ``normalize()`` narrows it to a
DHCP-only canonical view: for every ``modeIfs[].applianceIPs[]`` entry that
carries a ``dhcpd`` key, keep only ``{devNum, ifName, applianceIPs: [{ip,
dhcpd}]}``, plus the top-level ``dhcpFailover`` map verbatim. Every other
field (``sysConfig``, ``mgmtIfData``, non-dhcpd IP fields, ``dpRoutes``,
``vifs``, ...) is invisible to this resource's diff.

``apply()``/``rollback()`` therefore do a read-modify-write: GET the current
full deployment object *fresh* (never reuse a diff-time snapshot — it may be
stale), splice the desired ``dhcpd`` subtree into each matching interface
entry (matched by ``devNum``+``ifName``+``ip``) and replace the top-level
``dhcpFailover`` key, leaving every other field of the fetched object
untouched, validate the merged object, then POST the whole thing back. An
interface/IP present in the desired canonical state but no longer found in
the freshly-fetched object is skipped with a warning log (never invented) —
this resource does not create interfaces or IPs, only their ``dhcpd``
config.

Same-changeset overlap with ``appliance/deployment`` — refused since #69:
both this resource and ``appliance/deployment`` (#12) write the *same
underlying server object*, so both declare the same
:meth:`~pyecsdwan.contract.Resource.write_target` and a changeset containing
both is refused at plan time.

This paragraph used to say the overlap was harmless — that whichever applied
second did a GET-then-POST against what the first had written, so "no data is
lost". **That was only half true, and the wrong half was load-bearing.** It
describes *this* resource, which re-reads the live object and splices its
subtree in (see ``_write``). ``appliance/deployment`` does not: it posts the
body it computed at plan time, so a DHCP change applied before it is silently
overwritten. There is no ``dependencies`` relationship between the two and the
apply order is unspecified, which made the outcome a coin toss between "fine"
and "your DHCP change is gone".

``tests/test_write_collisions.py`` drives the losing order deliberately, with
the guard bypassed, so the loss is demonstrated rather than asserted — and so
that the refusal can be softened with evidence if ``deployment.apply()`` ever
starts re-reading.

Reversibility: REVERSIBLE. ``fetch()`` (and therefore any journaled
snapshot) is the full raw deployment object, so ``rollback()`` runs the same
normalize + read-modify-write path as a forward apply, restoring the DHCP
subtree exactly.

Ownership: appliance-scope, wired to ``ownership.owning_group()``.
``KIND_TO_TEMPLATE_SECTIONS["appliance/dhcp"] = ("dhcpd", "dhcpFailover")``
is pre-seeded (unverified against live data per that module's comment) and
used as-is.

Normalization/idempotency: mirrors deployment.py's ``_canonicalize`` helper
(dict key order never matters; every list is deterministically sorted by its
own JSON representation) so ``normalize(normalize(x)) == normalize(x)`` and
the diff engine's positional list comparison never sees phantom drift from
server-side reordering. The canonical shape re-uses the same top-level key
names (``modeIfs``, ``applianceIPs``, ``dhcpFailover``) as the full
deployment object, just with every non-DHCP field stripped from each
interface/IP entry — so feeding ``normalize()``'s own output back into
``normalize()`` re-extracts the identical structure without any special
"already normalized" case.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any, cast

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

log = structlog.get_logger("pyecsdwan.resources.dhcp")

_PATH = "deployment"
_VALIDATE_PATH = "deployment/validate"

#: Singleton resource name: one DHCP view (over the shared deployment
#: object) per appliance.
_INSTANCE_NAME = "dhcp"


def _sort_key(item: Any) -> str:
    """Deterministic sort key for a canonical (already-JSON-safe) value."""
    return json.dumps(item, sort_keys=True, default=str)


def _canonicalize(value: Any) -> Any:
    """Recursively normalize dict/list structure for stable, idempotent diffing.

    Duplicated from ``deployment.py`` deliberately (see module docstring) —
    dict key order never affects equality, so dicts are left keyed as-is
    (just recursed into); list order *does* affect the diff engine's
    positional comparison, so every list is sorted by its own JSON
    representation, making ``normalize(normalize(x)) == normalize(x)`` hold
    without a hand-picked identity key per list shape.
    """
    if isinstance(value, Mapping):
        return {str(k): _canonicalize(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [_canonicalize(v) for v in value]
        try:
            return sorted(items, key=_sort_key)
        except TypeError:
            return items
    return value


class Dhcp(Resource):
    kind = "appliance/dhcp"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: DHCP config is a subtree of the always-present deployment object —
    #: there is no "the dhcp config doesn't exist" state on a live
    #: appliance, so whole-resource delete is refused (matches deployment.py
    #: / zones.py / interface-labels).
    deletable = False
    desired_state_doc = (
        "modeIfs: [{devNum, ifName, applianceIPs: [{ip, dhcpd: {type: "
        "server|relay|none, server: {prefix, ipStart, ipEnd, gw[], dns[], "
        "ntpd[], netbios[], netbiosNodeType, maxLease, defaultLease, "
        "ip_range{}, options{}, host{}, failover}, relay: {dhcpserver[], "
        "option82, option82_policy}}}]}]. dhcpFailover: {<iface>: {...}}. "
        "This is a narrow view over the shared 'deployment' object (see "
        "appliance/deployment) — apply() does a read-modify-write against "
        "the live deployment object, splicing only the dhcpd subtree of "
        "matching devNum+ifName+ip interface entries plus dhcpFailover; "
        "every other deployment field (sysConfig, mgmtIfData, other IP "
        "fields, other interfaces, ...) is left untouched. An interface/ip "
        "not found in the live object is skipped, never invented."
    )
    #: Shares the deployment object with appliance/deployment: this kind owns only
    #: the dhcpd/dhcpFailover subtrees, spliced into a freshly-read full object.
    endpoints = (
        "appliance GET /deployment",
        "appliance POST /deployment",
        "appliance POST /deployment/validate",
    )

    # -- appliance resolution --------------------------------------------------

    @staticmethod
    def _ne_pk(ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{ref} is appliance-scoped and requires an appliance name")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side --------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        """Full deployment object (there is no separate DHCP endpoint)."""
        raw = ctx.client.appliance_request("GET", self._ne_pk(ctx, ref), _PATH)
        return raw if isinstance(raw, dict) and raw else None

    def normalize(self, raw: RawState) -> CanonicalState:
        """Narrow the full deployment object to its DHCP-relevant view.

        Accepts either the full raw deployment object (from ``fetch()``) or
        this method's own prior output — both shapes carry ``modeIfs``/
        ``dhcpFailover`` keys, so a second pass re-extracts the identical
        structure and idempotency holds without a separate code path.
        """
        if not isinstance(raw, Mapping):
            return None
        mode_ifs_raw = raw.get("modeIfs")
        mode_ifs: list[dict[str, Any]] = []
        for iface in mode_ifs_raw if isinstance(mode_ifs_raw, list) else []:
            if not isinstance(iface, Mapping):
                continue
            ips_raw = iface.get("applianceIPs")
            ips: list[dict[str, Any]] = []
            for entry in ips_raw if isinstance(ips_raw, list) else []:
                if not isinstance(entry, Mapping):
                    continue
                if "dhcpd" not in entry:
                    continue  # no dhcpd subtree on this IP entry: irrelevant here
                ips.append({"ip": str(entry.get("ip", "")), "dhcpd": entry["dhcpd"]})
            if not ips:
                continue  # interface has no dhcpd-bearing IP entries: irrelevant here
            mode_ifs.append(
                {
                    "devNum": str(iface.get("devNum", "")),
                    "ifName": str(iface.get("ifName", "")),
                    "applianceIPs": ips,
                }
            )
        dhcp_failover = raw.get("dhcpFailover")
        kept = {
            "modeIfs": mode_ifs,
            "dhcpFailover": dict(dhcp_failover) if isinstance(dhcp_failover, Mapping) else {},
        }
        canon = cast("dict[str, Any]", _canonicalize(kept))
        if not canon["modeIfs"] and not canon["dhcpFailover"]:
            return None
        return canon

    # -- write side ---------------------------------------------------------

    def _validate(self, ctx: Ctx, ne_pk: str, body: dict[str, Any]) -> dict[str, Any]:
        result = ctx.client.appliance_request(
            "POST", ne_pk, _VALIDATE_PATH, json_body=body
        )
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _merge(live: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
        """Splice the desired dhcpd/dhcpFailover subtrees into a freshly-read
        full deployment object, leaving every other field untouched."""
        merged = copy.deepcopy(live)
        mode_ifs = merged.get("modeIfs")
        if not isinstance(mode_ifs, list):
            mode_ifs = []
            merged["modeIfs"] = mode_ifs
        for want_iface in desired.get("modeIfs") or []:
            dev_num = want_iface.get("devNum")
            if_name = want_iface.get("ifName")
            target_iface = next(
                (
                    i
                    for i in mode_ifs
                    if isinstance(i, dict)
                    and i.get("devNum") == dev_num
                    and i.get("ifName") == if_name
                ),
                None,
            )
            if target_iface is None:
                log.warning(
                    "dhcp_apply_interface_not_found", devNum=dev_num, ifName=if_name
                )
                continue
            target_ips = target_iface.get("applianceIPs")
            if not isinstance(target_ips, list):
                continue
            for want_ip in want_iface.get("applianceIPs") or []:
                ip = want_ip.get("ip")
                target_entry = next(
                    (e for e in target_ips if isinstance(e, dict) and e.get("ip") == ip),
                    None,
                )
                if target_entry is None:
                    log.warning(
                        "dhcp_apply_ip_not_found", devNum=dev_num, ifName=if_name, ip=ip
                    )
                    continue
                target_entry["dhcpd"] = want_ip.get("dhcpd")
        if "dhcpFailover" in desired:
            merged["dhcpFailover"] = desired["dhcpFailover"]
        return merged

    def _write(self, ctx: Ctx, ref: Ref, desired: RawState, action: str) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        desired_dict = desired if isinstance(desired, dict) else {}

        # Fresh read — never reuse a diff-time snapshot, which may be stale
        # (see module docstring on the appliance/deployment overlap).
        live = ctx.client.appliance_request("GET", ne_pk, _PATH)
        if not isinstance(live, dict):
            return ApplyResult(
                ok=False,
                message=f"dhcp {action} on {ref}: could not read the live deployment object",
            )
        body = self._merge(live, desired_dict)

        validation = self._validate(ctx, ne_pk, body)
        err = validation.get("err")
        if err:
            log.info("dhcp_validate_failed", ref=str(ref), action=action, err=err)
            return ApplyResult(
                ok=False,
                changed=False,
                message=f"dhcp {action} on {ref} rejected by validate: {err}",
            )
        reboot_required = bool(validation.get("rebootRequired"))

        ctx.client.appliance_request("POST", ne_pk, _PATH, json_body=body)

        save = ctx.save_changes([ne_pk], f"dhcp {action}: {ref}")
        if save.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                message=(
                    f"dhcp {action} on {ref} not persisted — "
                    f"save-changes {save.state}: {save.detail}"
                ),
                jobs=[save],
            )
        message = f"dhcp {action} on {ref} persisted"
        if reboot_required:
            message += (
                " (appliance reports rebootRequired — change is running-config, "
                "not live until reboot)"
            )
        return ApplyResult(ok=True, jobs=[save], message=message)

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        return self._write(ctx, diff.ref, diff.desired, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # DHCP config is never legitimately absent on a live appliance
            # (it's a subtree of the always-present deployment object); a
            # snapshot recorded as absent must never be replayed as "clear
            # every interface's dhcpd config". Refuse loudly instead.
            return ApplyResult(
                ok=False,
                message=(
                    f"no usable snapshot for {ref}; refusing to write an "
                    f"empty dhcp state"
                ),
            )
        restored = self.normalize(snapshot)
        if not isinstance(restored, dict):
            return ApplyResult(
                ok=False,
                message=f"snapshot for {ref} normalized to nothing; refusing to write it back",
            )
        return self._write(ctx, ref, restored, "rollback")

    # -- ownership ------------------------------------------------------------

    def write_target(self, ctx: Ctx, ref: Ref) -> str | None:
        """The whole ``deployment`` object on one appliance — which
        :mod:`pyecsdwan.resources.deployment` replaces too (#69). Instance-scoped
        by nePk: two appliances are two objects, not a conflict."""
        return f"appliance {self._ne_pk(ctx, ref)} deployment"

    def managed_by(self, ctx: Ctx, ref: Ref) -> Ownership:
        return ownership.owning_group(ctx, self.kind, self._ne_pk(ctx, ref))

    # -- enumeration ------------------------------------------------------------

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name=_INSTANCE_NAME, appliance=str(a["hostName"]))
            for a in ctx.resolver.appliances()
            if a.get("hostName")
        ]


register(Dhcp())
