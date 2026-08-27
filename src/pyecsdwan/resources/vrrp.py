"""VRRP instances — appliance-scope config (Phase 2, #14).

Endpoint facts (issue #14 + the vendored SDK field reference,
``pyedgeconnect/ecos/_vrrp.py``'s ``get_vrrp_interfaces``/
``configure_vrrp_interfaces`` docstrings — ``docs/research/expert-repo.md``
has no dedicated vrrp entry):

* ECOS path ``vrrp``, reached through the appliance proxy as
  ``GET/POST /appliance/rest?nePk=<pk>&url=vrrp`` — a JSON list of VRRP
  instance dicts, one per configured group/interface on the appliance.
* ``POST vrrp`` replaces the appliance's whole VRRP config with the posted
  list; the SDK documents no partial-update verb, so ``apply()``/
  ``rollback()`` always POST the complete desired list.

Each GET entry mixes the configurable fields with server-reported state:
``mode``, ``master_transitions``, ``uptime``, ``vmac`` (per the issue) and,
per the SDK docstring, ``priorityState``, ``masterip``, ``vipowner`` too —
none of these appear in ``configure_vrrp_interfaces``'s write-side field
list. ``normalize()`` keeps only the write-side fields (``_FIELDS`` below),
so state churn (a mode flip, uptime ticking upward) never shows up as drift.

**Live grounding note** (this session): the ``vrrp`` ECOS endpoint was
probed against live lab appliances and answered with a valid *empty*
response on every one — none of the probed appliances had VRRP configured,
so no populated real-world sample exists. An empty appliance is therefore a
genuine, common state here (unlike e.g. zones' server-managed Default entry),
so — unlike ``zones``/``interface-labels`` — whole-resource delete (POST an
empty list) is a legitimate, fully reversible operation and needs no special
"refuse to write an empty table" guard.

Canonical shape is ``{"vrrp": [entries...]}`` when at least one instance is
configured, else ``None`` (list wrapped under a dict key when present,
matching the ``zones``/``security-policy`` convention so the top-level stays
a ``Mapping`` for the default ``canonicalize_desired``; entries sorted by
``groupId`` for a stable, positionally-diffable list). Unlike ``zones``/
``interface-labels`` there is no server-managed row that always exists, so
"no instances" is modeled as absent (``None``), the same value a
whole-resource ``delete`` produces as desired state — one canonical
"nothing configured" value, not two, so a delete's post-apply verify agrees
with itself. Resource instance name is the singleton ``"global"`` (one
instance per appliance holds the whole table, like ``zones``/
``interface-labels``).
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

log = structlog.get_logger("pyecsdwan.resources.vrrp")

_VRRP_PATH = "vrrp"

#: Configurable (write-side) fields — configure_vrrp_interfaces' field list,
#: which matches issue #14's documented shape exactly. Everything else a GET
#: returns (mode, master_transitions, uptime, vmac, priorityState, masterip,
#: vipowner) is server-reported state and is dropped by ``_entry()`` below.
_FIELDS = (
    "pkt_trace",
    "adv_timer",
    "preempt",
    "holddown",
    "auth",
    "desc",
    "enable",
    "priority",
    "vipaddr",
    "interface",
    "groupId",
)

_ENABLE_VALUES = ("Up", "Down")


def _entry(raw: Any) -> dict[str, Any]:
    """Validate and shape one VRRP instance; strip anything not in ``_FIELDS``."""
    if not isinstance(raw, Mapping):
        raise ValueError(
            "vrrp entry must be a mapping of instance fields (groupId, interface, "
            f"vipaddr, enable, ...), got {type(raw).__name__}"
        )
    entry: dict[str, Any] = {str(k): v for k, v in raw.items() if k in _FIELDS}

    for field_name, hint in (
        ("groupId", "the VRRP group/VRID identifier, 1-255"),
        ("interface", "the peering interface, e.g. 'wan0'"),
        ("vipaddr", "the virtual IP address"),
    ):
        if entry.get(field_name) in (None, ""):
            raise ValueError(
                f"vrrp entry is missing required field {field_name!r} ({hint}): {raw!r}"
            )

    try:
        group_id = int(entry["groupId"])
    except (TypeError, ValueError):
        raise ValueError(f"vrrp groupId must be an integer, got {entry['groupId']!r}") from None
    if not 1 <= group_id <= 255:
        raise ValueError(f"vrrp groupId must be between 1-255, got {group_id}")
    entry["groupId"] = group_id

    entry["interface"] = str(entry["interface"])
    entry["vipaddr"] = str(entry["vipaddr"])

    enable = entry.get("enable", "Down")
    if enable not in _ENABLE_VALUES:
        raise ValueError(
            f"vrrp groupId {group_id}: enable must be one of {_ENABLE_VALUES}, got {enable!r}"
        )
    entry["enable"] = enable

    adv_timer = int(entry.get("adv_timer", 1))
    if not 1 <= adv_timer <= 255:
        raise ValueError(f"vrrp groupId {group_id}: adv_timer must be 1-255, got {adv_timer}")
    entry["adv_timer"] = adv_timer

    holddown = int(entry.get("holddown", 10))
    if not 1 <= holddown <= 255:
        raise ValueError(f"vrrp groupId {group_id}: holddown must be 1-255, got {holddown}")
    entry["holddown"] = holddown

    priority = int(entry.get("priority", 100))
    if not 1 <= priority <= 254:
        raise ValueError(f"vrrp groupId {group_id}: priority must be 1-254, got {priority}")
    entry["priority"] = priority

    auth = str(entry.get("auth", ""))
    if len(auth) > 8:
        raise ValueError(
            f"vrrp groupId {group_id}: auth must be at most 8 characters, got {len(auth)}"
        )
    entry["auth"] = auth

    desc = str(entry.get("desc", ""))
    if len(desc) > 64:
        raise ValueError(
            f"vrrp groupId {group_id}: desc must be at most 64 characters, got {len(desc)}"
        )
    entry["desc"] = desc

    entry["pkt_trace"] = bool(entry.get("pkt_trace", False))
    entry["preempt"] = bool(entry.get("preempt", True))
    return entry


class Vrrp(Resource):
    kind = "appliance/vrrp"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    desired_state_doc = (
        "vrrp: list of VRRP instances, each {groupId(1-255), interface, vipaddr, "
        "enable('Up'|'Down'), priority(1-254, default 100), adv_timer(1-255, default 1), "
        "preempt(bool, default true), holddown(1-255, default 10), auth(<=8 chars), "
        "desc(<=64 chars), pkt_trace(bool, default false)}. groupId is each entry's "
        "identity. Server-reported state (mode, master_transitions, uptime, vmac, "
        "priorityState, masterip, vipowner) is read-only and stripped on read."
    )
    endpoints = (
        "appliance GET /vrrp",
        "appliance POST /vrrp",
    )

    @staticmethod
    def _ne_pk(ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{ref.kind} is appliance-scoped; a Ref needs .appliance set")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        ne_pk = self._ne_pk(ctx, ref)
        raw: Any = ctx.client.appliance_request("GET", ne_pk, _VRRP_PATH)
        # The mock and (per this session's live probe) real Orchestrator both
        # answer an unconfigured appliance with an empty body/list; some
        # proxies fall back to `{}` for an unseeded path. All three mean "no
        # VRRP instances configured" and are handled uniformly in normalize().
        if raw is None or isinstance(raw, (list, dict)):
            return raw
        raise ValueError(f"unexpected vrrp response shape from {ref}: {raw!r}")

    def normalize(self, raw: RawState) -> CanonicalState:
        if raw is None:
            items: Any = []
        elif isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            # canonicalize_desired()/round-tripped canonical state passes the
            # already-wrapped {"vrrp": [...]} shape back through here; an
            # empty dict (mock default for an unseeded path) means no entries.
            items = raw.get("vrrp", [])
        else:
            raise ValueError(f"vrrp response must be a list of instances, got {raw!r}")

        if not isinstance(items, list):
            raise ValueError(
                f"vrrp must be a list of instance mappings, got {type(items).__name__}"
            )

        if not items:
            # No VRRP instances is "absent", not an empty-but-present table:
            # unlike zones/interface-labels (server-managed rows always
            # exist), a fully-empty vrrp list is the real, common state this
            # session's live probe actually observed. Returning None (not
            # {"vrrp": []}) matches what a whole-resource `delete` produces
            # as desired state, so apply+verify and rollback agree on one
            # canonical "nothing configured" value instead of two.
            return None
        entries = [_entry(item) for item in items]
        ids = [e["groupId"] for e in entries]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate vrrp groupId(s) after normalization: {dupes}")
        return {"vrrp": sorted(entries, key=lambda e: int(e["groupId"]))}

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        ne_pk = self._ne_pk(ctx, ref)
        desired = diff.desired if isinstance(diff.desired, dict) else {}
        payload = desired.get("vrrp", [])
        ctx.client.appliance_request("POST", ne_pk, _VRRP_PATH, json_body=payload)
        outcome = ctx.save_changes([ne_pk], f"vrrp: {ref}")
        if outcome.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                jobs=[outcome],
                message=(
                    f"vrrp replaced on {ne_pk} but not persisted — "
                    f"save-changes {outcome.state}: {outcome.detail}"
                ),
            )
        return ApplyResult(
            ok=True,
            jobs=[outcome],
            message=f"vrrp replaced ({len(payload)} instance(s)) on {ne_pk}, persisted",
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        restored = self.normalize(snapshot)
        payload = restored.get("vrrp", []) if isinstance(restored, dict) else []
        ctx.client.appliance_request("POST", ne_pk, _VRRP_PATH, json_body=payload)
        outcome = ctx.save_changes([ne_pk], f"vrrp rollback: {ref}")
        if outcome.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                jobs=[outcome],
                message=(
                    f"vrrp rollback on {ne_pk} not persisted — "
                    f"save-changes {outcome.state}: {outcome.detail}"
                ),
            )
        return ApplyResult(
            ok=True, jobs=[outcome], message=f"vrrp restored from snapshot on {ne_pk}, persisted"
        )

    # -- ownership --------------------------------------------------------------

    def managed_by(self, ctx: Ctx, ref: Ref) -> str | None:
        ne_pk = self._ne_pk(ctx, ref)
        return owning_group(ctx, self.kind, ne_pk)


register(Vrrp())
