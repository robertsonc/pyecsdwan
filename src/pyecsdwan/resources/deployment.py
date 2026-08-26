"""Interfaces / IP addressing / deployment — appliance-scope resource (#12).

Endpoint facts (docs/research/appliance-config.md §Deployment, and a live
capture against a lab Orchestrator this session — see the top-level shape
note below):

* Read: ECOS path ``deployment`` via the appliance proxy
  (``GET /appliance/rest?nePk=<pk>&url=deployment``) — a *live call to the
  appliance*, not an Orchestrator-cached view.
* Validate: ECOS path ``deployment/validate`` — POST the candidate object,
  get back ``{err, rebootRequired}``. ``err`` non-empty means the appliance
  rejected the candidate; called before every write (apply *and* rollback —
  a restore is a write too).
* Write: ECOS path ``deployment`` (POST) — **full-object replace**: GET
  current, modify, POST the whole thing back. There is no per-field PATCH.
* Persist: ``ctx.save_changes([ne_pk], ...)`` once per apply()/rollback(),
  same as every other appliance-proxy write (issue #11).

Live-captured top-level shape (high confidence — this is what a real 9.x
appliance returns, not just the SDK docstring)::

    {scalars, sysConfig, mgmtIfData, modeIfs, dpRoutes, vifs, dhcpFailover}

Deviation from the issue text: the issue's endpoint note says to strip
``scalars``/``vifs`` before writing back. The live capture shows ``vifs`` is
real, present, top-level config (pppoe/bonded-interface state per the SDK
docstring) that the issue text simply didn't mention — it is treated as an
*unknown-passthrough* field here, not stripped, per the promotion checklist
("unknown fields pass through") and the issue's own acceptance criteria.
Only ``scalars`` is stripped: it is ~60 read-only hardware/license/limit
fields (maxWanBandwidth, num1GigPorts, isModel10G, ...) that the appliance
recomputes and that would otherwise show as permanent phantom drift on every
plan.

``sysConfig`` (mode, ifLabels, zones/vrfs/roles, bandwidth ceilings, ...) is
real configuration intent and is diffed like anything else. ``mgmtIfData``,
``dpRoutes`` and ``dhcpFailover`` are likewise real config, just typically
sparse/empty on a freshly-imaged appliance.

DHCP (issue #13 depends on this resource's shape): DHCP server/relay config
is not a separate endpoint — it is the ``dhcpd`` subtree of each interface's
IP entry: ``modeIfs[].applianceIPs[].dhcpd = {type: "server"|"relay"|"none",
server: {...}, relay: {...}}``, plus the top-level ``dhcpFailover`` map keyed
by interface. Both are ordinary keys under the generic canonicalizer below
(see ``_DHCPD_LOCATION`` for where a future #13 plugin should look) — no
special-casing needed here, but nothing in this module should have to move
for #13 to build on it.

Normalization: everything except ``scalars`` is recursively canonicalized —
dict key order never matters for equality, but *list* order does (the diff
engine compares canonical lists positionally), so every list (``modeIfs``,
``dpRoutes``, each interface's ``applianceIPs``, and anything under the
unknown-passthrough keys) is deterministically sorted by its own JSON
representation. This makes ``normalize()`` idempotent and order-insensitive
without hand-writing a sort key per list — the server is not guaranteed to
return interfaces or routes in a stable order across calls.

Reversibility: REVERSIBLE. The full object is GET-then-POST, so a snapshot
of the pre-change object restores exactly via the same write path.

Ownership: appliance-scope, so ``managed_by()`` is wired to
``ownership.owning_group()``. ``KIND_TO_TEMPLATE_SECTIONS`` does not yet
have a confirmed template-section name for this kind — see the comment on
the entry in ``ownership.py`` for why (no live Default Template Group this
session selected an obvious interfaces/deployment section).
"""

from __future__ import annotations

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
    RawState,
    Ref,
    Resource,
    Reversibility,
    Scope,
    Tier,
)
from pyecsdwan.registry import register

log = structlog.get_logger("pyecsdwan.resources.deployment")

_PATH = "deployment"
_VALIDATE_PATH = "deployment/validate"

#: Read-only hardware/license/limit fields the appliance recomputes; stripped
#: entirely so they never show up as phantom drift (see module docstring).
_READ_ONLY_TOP_KEYS = ("scalars",)

#: Where a future #13 (DHCP) plugin reads/writes: modeIfs[].applianceIPs[].dhcpd
#: (per-interface server/relay config) and the top-level dhcpFailover map.
_DHCPD_LOCATION = "modeIfs[].applianceIPs[].dhcpd, dhcpFailover"

#: Singleton resource name: one deployment object per appliance.
_INSTANCE_NAME = "deployment"


def _sort_key(item: Any) -> str:
    """Deterministic sort key for a canonical (already-JSON-safe) value."""
    return json.dumps(item, sort_keys=True, default=str)


def _canonicalize(value: Any) -> Any:
    """Recursively normalize dict/list structure for stable, idempotent diffing.

    Dict key order never affects equality, so dicts are left keyed as-is
    (just recursed into). Lists *do* affect positional diff comparison, so
    every list — known (modeIfs, dpRoutes, applianceIPs) or unknown-passthrough
    (vifs, and anything else the server adds later) — is sorted by its own
    JSON representation. That makes ``normalize(normalize(x)) == normalize(x)``
    hold without needing a hand-picked identity key per list shape.
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


class Deployment(Resource):
    kind = "appliance/deployment"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: A per-appliance singleton: interfaces always exist on a live appliance,
    #: there is no "the deployment doesn't exist" state, so whole-resource
    #: delete is refused (matches interface-labels / zones).
    deletable = False
    desired_state_doc = (
        "sysConfig: {mode, ifLabels{lan[],wan[]}, zones[], vrfs[], roles[], "
        "maxBW, ...} (real config intent). mgmtIfData: {<iface>: {ip, mask, "
        "dhcp, nexthop}}. modeIfs: [{devNum, ifName, applianceIPs: [{ip, mask, "
        "wanNexthop, dhcp, lanSide, wanSide, label, harden, behindNAT, maxBW, "
        "zone, comment, vrf, role, proxy_arp, dhcpd: {type: server|relay|none, "
        "server{...}, relay{...}}}]}]. dpRoutes: [{prefix, nexthop, intf, "
        "metric, type}]. dhcpFailover: {<iface>: {...}}. Unknown top-level "
        "keys (e.g. vifs) pass through unmodified. 'scalars' is read-only "
        "and must never be set — normalize() strips it. Full-object replace: "
        "the whole object is validated (deployment/validate) then POSTed back "
        "on every apply."
    )

    # -- appliance resolution --------------------------------------------------

    @staticmethod
    def _ne_pk(ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{ref} is appliance-scoped and requires an appliance name")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side --------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.appliance_request("GET", self._ne_pk(ctx, ref), _PATH)
        return raw if isinstance(raw, dict) and raw else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, Mapping):
            return None
        kept = {str(k): v for k, v in raw.items() if k not in _READ_ONLY_TOP_KEYS}
        if not kept:
            return None
        return cast("dict[str, Any]", _canonicalize(kept))

    # -- write side ---------------------------------------------------------

    def _validate(self, ctx: Ctx, ne_pk: str, body: dict[str, Any]) -> dict[str, Any]:
        result = ctx.client.appliance_request(
            "POST", ne_pk, _VALIDATE_PATH, json_body=body
        )
        return result if isinstance(result, dict) else {}

    def _write(self, ctx: Ctx, ref: Ref, payload: RawState, action: str) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        body = payload if isinstance(payload, dict) else {}

        validation = self._validate(ctx, ne_pk, body)
        err = validation.get("err")
        if err:
            log.info("deployment_validate_failed", ref=str(ref), action=action, err=err)
            return ApplyResult(
                ok=False,
                changed=False,
                message=f"deployment {action} on {ref} rejected by validate: {err}",
            )
        reboot_required = bool(validation.get("rebootRequired"))

        ctx.client.appliance_request("POST", ne_pk, _PATH, json_body=body)

        save = ctx.save_changes([ne_pk], f"deployment {action}: {ref}")
        if save.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                message=(
                    f"deployment {action} on {ref} not persisted — "
                    f"save-changes {save.state}: {save.detail}"
                ),
                jobs=[save],
            )
        message = f"deployment {action} on {ref} persisted"
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
            # A deployment object is never legitimately absent on a real
            # appliance; a snapshot recorded as absent must never be replayed
            # as "POST an empty deployment object" — that would wipe every
            # interface/IP on the box. Refuse loudly instead.
            return ApplyResult(
                ok=False,
                message=(
                    f"no usable snapshot for {ref}; refusing to POST an "
                    f"empty deployment object"
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

    def managed_by(self, ctx: Ctx, ref: Ref) -> str | None:
        return ownership.owning_group(ctx, self.kind, self._ne_pk(ctx, ref))

    # -- enumeration ------------------------------------------------------------

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name=_INSTANCE_NAME, appliance=str(a["hostName"]))
            for a in ctx.resolver.appliances()
            if a.get("hostName")
        ]


register(Deployment())
