"""Loopback interfaces (#18) — two unrelated-scope resources sharing a name.

**1. Per-appliance loopback interfaces — ``Loopback`` (read+diff only)**

Endpoint facts (issue #18 text; confirmed live, read-only, against a lab
Orchestrator this session — high confidence):

* Read: ECOS path ``virtualif/loopback`` via the appliance proxy
  (``GET /appliance/rest?nePk=<pk>&url=virtualif/loopback``), like every
  other appliance-scope resource in this codebase (``ctx.client.
  appliance_request``) — the vendored SDK's ``get_loopback_interfaces``
  instead calls ``/virtualif/loopback?nePk=`` directly on the Orchestrator
  for 9.3+; the proxy path is what this session's live probe used and is
  what the issue text specifies, so that's what's implemented.

Confirmed live shape, high confidence::

    {"lo0": {"admin": true, "gms_marked": false, "ipaddr": "192.168.255.12",
              "label": "", "nmask": 32, "role_id": 0, "vrf_id": 0, "zone": 0}}

A dict keyed by loopback interface name (``lo0``, possibly more).

**No write endpoint is documented** for this per-appliance view — modeled
read+diff only, same as ``appliance/bgp`` Stage 1 (``resources/bgp.py``):
``apply()``/``rollback()`` raise ``NotImplementedError`` naming the gap, and
``reversibility = Reversibility.IRREVERSIBLE`` so the transaction engine's
own guard (``txn._guard``) refuses ``commit`` without ``--force`` and
refuses ``commit confirm`` outright, rather than ever reaching the
``NotImplementedError``. ``show``/drift work today; only writes are blocked.

Ownership: per-entry ``gms_marked`` (confirmed real in the sample above) is
enough per-object ownership signal on its own — no
``ownership.KIND_TO_TEMPLATE_SECTIONS`` entry is added here (don't invent a
template-section name without a confirmed one; see ``ownership.py``).

**2. Loopback orchestration — ``LoopbackOrch`` (fabric-wide pool, REVERSIBLE)**

Endpoint facts (issue #18 text and the vendored ``pyedgeconnect``
``orch/_loopback_orch.py``, which documents this endpoint's payload shape in
its docstrings — no live populated sample was captured this session, the lab
fabric's ``loopbackOrch`` came back empty):

* ``GET /loopbackOrch`` — the full orchestration structure:
  ``{"<segmentId>": {"loopbackPool": "<cidr>", "interfaces": {"<ifId>":
  {"mgmtIP": bool, "label": "<labelId>", "zone": <zoneId>}}}}``.
* ``GET /loopbackOrch/pool`` — pool allocation detail, read-only (exposed
  below as :func:`pool_detail`, never part of the diffable state).
* ``DELETE /loopbackOrch/pool/reclaim?id=<id>`` — reclaim one deleted IP
  back into the pool, an explicit maintenance action, not configuration
  (exposed below as :func:`reclaim_deleted_ips`). The id is a **query**
  parameter; there is no ``/reclaim/{id}`` route, though this module both
  documented and built one until #60 — see
  :data:`RECLAIM_ID_IS_A_QUERY_PARAM`.
* ``POST /loopbackOrch`` — **full-structure replace**. The vendored SDK's
  own docstring warns: "This overwrites all loopback Orchestration, must
  use get_loopback_orchestration to get existing ... and then use
  ``multiple_segments`` to load multiple segment loopback pools." There is
  no per-segment PATCH. This resource never constructs a partial object to
  POST: ``build_plan`` (``txn.py``) merges any ``set``/``delete`` onto the
  currently *fetched and normalized* state before ``canonicalize_desired``
  ever sees it (the same merge-then-normalize flow ``zones``/
  ``deployment`` rely on), so ``diff.desired`` is always the complete
  structure by construction — ``apply()`` just POSTs it, exactly like
  ``resources/deployment.py``'s full-object-replace pattern (validate-free
  here: ``loopbackOrch`` has no separate validate endpoint).

**SDK case-mismatch bug (defensive, not live-verified further)**: the
vendored SDK's ``set_loopback_orchestration`` constructs its POST body with
key ``mgmtIp`` (lowercase ``p``), while the same module's own
``get_loopback_orchestration`` docstring — and the issue text — describe the
GET response using ``mgmtIP`` (capital ``IP``). Left alone, that mismatch
would round-trip as phantom drift (every plan would see a ``mgmtIp``-shaped
desired object diff against a ``mgmtIP``-shaped current one forever).
``normalize()`` canonicalizes every interface entry to ``mgmtIP`` — the key
GET actually returns — folding a ``mgmtIp`` alias into it if one is ever
supplied (e.g. hand-written YAML ``set`` input, or a caller who copy-pasted
the SDK's own casing). Since ``canonicalize_desired`` defaults to routing
through ``normalize()`` (``contract.Resource.canonicalize_desired``), this
one fold covers both sides of the diff without needing an override.

Reversibility: REVERSIBLE. The full object is GET-then-POST (via the
merge-then-normalize flow above), so a snapshot of the pre-change structure
restores exactly via the same write path — see ``rollback()``.

Singleton table (instance name ``global``), like ``zones``: there is no
"the loopback orchestration structure doesn't exist" state — an empty
``{}`` is a legitimate "no segments configured" reading, not absence — so
whole-resource delete is refused; remove individual ``<segment-id>`` entries
instead.
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
    Ownership,
    RawState,
    Ref,
    Resource,
    Reversibility,
    Scope,
    Tier,
)
from pyecsdwan.registry import register

log = structlog.get_logger("pyecsdwan.resources.loopback")

# -- shared -------------------------------------------------------------------

_LOOPBACK_ORCH_PATH = "/loopbackOrch"
_LOOPBACK_ORCH_POOL_PATH = "/loopbackOrch/pool"
_LOOPBACK_ORCH_RECLAIM_PATH = "/loopbackOrch/pool/reclaim"

#: The reclaim id rides in the **query string**, not in the path (#60).
#:
#: Two independent vendored sources agree, and neither has a ``/reclaim/{id}``
#: route at all:
#:
#: * ``_specs/orchestrator-openapi-7.2.0.json`` — one operation,
#:   ``DELETE /loopbackOrch/pool/reclaim``, one parameter ``id``, ``in: query``,
#:   ``required: true``.
#: * ``_specs/payload-examples-9.6.json`` — the vendor's own 9.3-9.6 Postman
#:   collections give the raw path as ``/loopbackOrch/pool/reclaim?id=<n>``.
#:
#: What this module built instead was ``/loopbackOrch/pool/reclaim/{id}``,
#: taken from the vendored SDK (``pyedgeconnect/orch/_loopback_orch.py``),
#: which is inference rather than capture — see
#: :data:`RECLAIM_ALL_HAS_NO_KNOWN_ROUTE`. The bundled mock served that
#: invented path too, so the test suite confirmed the bug instead of catching
#: it; ``tests/test_loopback.py`` now re-derives this from ``_specs/``.
RECLAIM_ID_IS_A_QUERY_PARAM = True

#: Whether "reclaim *all* deleted IPs" can be invoked is **unresolved**, so
#: this module does not offer it (#60).
#:
#: The vendor contradicts itself inside a single operation. Its summary — the
#: same string in the 7.2.0 baseline and in the 9.6 collections — reads
#: "Reclaim all deleted ip addresses **or** Reclaim deleted ip address by id",
#: while the only parameter that operation takes is ``id``, marked required,
#: and every vendor example carries it. One half of that sentence has no route
#: behind it in anything vendored here.
#:
#: The SDK appears to have read the same sentence and split it into two
#: functions — a no-argument ``reclaim_delete_loopback_orchestration_ips()``
#: and a ``reclaim_single_deleted_loopback_orchestration_ip(id)`` posting to
#: ``/reclaim/{id}``. Since the ``{id}`` half is demonstrably not a route, that
#: pair reads as one ambiguous summary split in two, not as observation, which
#: is why it does not settle the other half either.
#:
#: So the by-id mode is taken and the all mode is left alone. It is also the
#: more dangerous of the two: an argument-less call that reclaims every deleted
#: address fabric-wide is the operator surprise Principle VI prohibits, and a
#: defaulted ``loopback_id=None`` made "reclaim all" the *easiest* thing to
#: type. Bulk reclaim the vendor does document unambiguously —
#: ``DELETE /loopbackOrch/pool/reclaimBySeg?segId=<n>`` and
#: ``.../reclaimBySegRegSubnet?seg=&reg=&subnet=`` — is where a caller who
#: wants it should be sent by whoever has a fabric to verify it against.
RECLAIM_ALL_HAS_NO_KNOWN_ROUTE = True


def _mapping_or_raise(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a mapping, got {type(value).__name__}")
    return {str(k): v for k, v in value.items()}


# == 1. per-appliance loopback interfaces (#18, read+diff only) ==============

_LOOPBACK_PATH = "virtualif/loopback"
_INSTANCE_NAME = "loopback"

_WRITE_PATH_TODO = (
    "appliance/loopback has no documented write endpoint (issue #18 models "
    "this kind read+diff only, same as appliance/bgp Stage 1). TODO: verify "
    "a write path against a live appliance — the natural candidate is POST "
    "virtualif/loopback via the appliance proxy, unconfirmed — before "
    "implementing apply()/rollback() and setting reversibility off "
    "IRREVERSIBLE."
)

#: Server bookkeeping, not user intent — stripped from canonical state and
#: read back separately (from the raw fetch) by managed_by(), matching the
#: appliance/routes precedent (resources/routes.py).
_STRIP_FIELDS = ("gms_marked",)


def _loopback_entry(name: str, entry: Any) -> dict[str, Any]:
    fields = _mapping_or_raise(
        entry, f"loopback interface {name!r} fields (ipaddr, nmask, admin, ...)"
    )
    return {k: v for k, v in fields.items() if k not in _STRIP_FIELDS}


def _has_gms_marked(raw: RawState) -> bool:
    if not isinstance(raw, Mapping):
        return False
    return any(
        isinstance(entry, Mapping) and bool(entry.get("gms_marked")) for entry in raw.values()
    )


class Loopback(Resource):
    kind = "appliance/loopback"
    scope = Scope.APPLIANCE
    # No write path is documented at all (see module docstring): a confirm
    # window would be fake safety, and there is no rollback to fall back on.
    reversibility = Reversibility.IRREVERSIBLE
    tier = Tier.CURATED
    #: A table of loopback interfaces, possibly empty — there is no "the
    #: loopback table doesn't exist" state, so whole-resource delete would
    #: not be meaningful even if a write path existed (matches appliance/bgp,
    #: appliance/routes).
    deletable = False
    desired_state_doc = (
        "map of loopback interface name (e.g. 'lo0') -> {admin: bool, "
        "ipaddr: str, nmask: int, role_id: int, vrf_id: int, zone: int, "
        "label: str, ...} (unknown fields pass through). 'gms_marked' is "
        "server bookkeeping, stripped from canonical state and surfaced "
        "instead via managed_by(). Read+diff only (issue #18): apply() "
        "always raises NotImplementedError — no write endpoint is "
        "documented for this per-appliance view."
    )
    #: Read-only today. `appliance POST /virtualif/loopback` exists in the spec but
    #: is deliberately not claimed here: apply() raises (see _WRITE_PATH_TODO).
    endpoints = (
        "appliance GET /virtualif/loopback",
    )

    # -- read side ------------------------------------------------------------

    def _ne_pk(self, ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{self.kind} is appliance-scoped; {ref} is missing an appliance")
        return ctx.resolver.ne_pk_for(ref.appliance)

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.appliance_request("GET", self._ne_pk(ctx, ref), _LOOPBACK_PATH)
        return raw if isinstance(raw, dict) else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if raw is None:
            return None
        interfaces_raw = _mapping_or_raise(raw, "virtualif/loopback response")
        interfaces = {
            name: _loopback_entry(name, entry) for name, entry in interfaces_raw.items()
        }
        return {name: interfaces[name] for name in sorted(interfaces)}

    def managed_by(self, ctx: Ctx, ref: Ref) -> Ownership:
        if ref.appliance is None:
            return Ownership.unknown(
                f"{ref.kind} is appliance-scope but the ref names no appliance, "
                f"so no nePk resolves and ownership cannot be checked"
            )
        # Per-object precision only (see module docstring): no confirmed
        # template-section name exists for this kind, so gms_marked alone
        # is the ownership signal, same shape as appliance/routes' check.
        if _has_gms_marked(self.fetch(ctx, ref)):
            return Ownership.owned("gms (gms_marked loopback interface present on this appliance)")
        # Absence of the flag is not proof of absence of an owner: a template
        # group may select a loopback section this project has never seen the
        # name of, and would push over a direct write. There is no
        # SECTION_MAP entry to fall back to, so the honest answer is that
        # nobody here knows (#20).
        return Ownership.unknown(
            "no gms_marked loopback interface on this appliance, and no confirmed "
            "template-section name exists for appliance/loopback, so a template "
            "owner cannot be ruled out"
        )

    # -- write side (no documented endpoint) -----------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        raise NotImplementedError(_WRITE_PATH_TODO)

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        # Never reached in practice: reversibility=IRREVERSIBLE means the
        # transaction guard refuses to apply this kind without --force, and
        # apply() itself raises before any write lands. Overridden anyway
        # (rather than inheriting the base class's generic NotImplementedError)
        # so a forced commit that somehow gets this far fails with the same,
        # specific explanation.
        raise NotImplementedError(_WRITE_PATH_TODO)

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name=_INSTANCE_NAME, appliance=str(a["hostName"]))
            for a in ctx.resolver.appliances()
            if a.get("hostName")
        ]


register(Loopback())


# == 2. loopback orchestration (#18, fabric-wide pool, REVERSIBLE) ===========


def _fold_mgmt_ip_casing(if_id: str, entry: Any) -> dict[str, Any]:
    """Canonicalize an interface entry's mgmt-IP flag to ``mgmtIP``.

    Folds a ``mgmtIp`` alias (the vendored SDK's own POST-builder casing) in
    if present; when both keys somehow appear, ``mgmtIP`` (what GET actually
    returns) wins — see the SDK case-mismatch note in the module docstring.
    """
    fields = _mapping_or_raise(entry, f"interface {if_id!r} fields (mgmtIP, label, zone, ...)")
    if "mgmtIp" in fields:
        alias = fields.pop("mgmtIp")
        fields.setdefault("mgmtIP", alias)
    if "mgmtIP" in fields:
        fields["mgmtIP"] = bool(fields["mgmtIP"])
    return fields


def _segment_entry(segment_id: str, segment: Any) -> dict[str, Any]:
    fields = _mapping_or_raise(
        segment, f"segment {segment_id!r} (loopbackPool, interfaces)"
    )
    interfaces_raw = fields.pop("interfaces", None) or {}
    interfaces_raw = _mapping_or_raise(
        interfaces_raw, f"segment {segment_id!r}.interfaces (interface-id -> fields)"
    )
    fields["interfaces"] = {
        if_id: _fold_mgmt_ip_casing(if_id, entry) for if_id, entry in interfaces_raw.items()
    }
    return fields


class LoopbackOrch(Resource):
    kind = "loopback-orch"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: Singleton table: an empty structure ({}) is "no segments configured",
    #: a legitimate reading, not absence — so whole-resource delete is
    #: refused, like `zones`. Delete individual `<segment-id>` entries.
    deletable = False
    desired_state_doc = (
        "map of segment-id -> {loopbackPool: '<cidr>', interfaces: "
        "{'<interfaceId>': {mgmtIP: bool, label: '<labelId>', zone: "
        "<zoneId>, ...}}} (unknown fields pass through). Interface ids are "
        "conventionally '20x00' where x is the segment id (vendored SDK "
        "convention, not enforced here). A 'mgmtIp' alias on input is "
        "folded to 'mgmtIP' (the real GET casing — see module docstring's "
        "SDK case-mismatch note). Full-structure replace: POST /loopbackOrch "
        "always carries the whole table, never a partial segment."
    )
    #: Config plus the pool view and the reclaim maintenance action.
    endpoints = (
        "orchestrator GET /loopbackOrch",
        "orchestrator POST /loopbackOrch",
        "orchestrator GET /loopbackOrch/pool",
        "orchestrator DELETE /loopbackOrch/pool/reclaim",
    )

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.get(_LOOPBACK_ORCH_PATH)
        return raw if isinstance(raw, dict) else {}

    def normalize(self, raw: RawState) -> CanonicalState:
        if raw is None:
            raw = {}
        segments_raw = _mapping_or_raise(raw, "loopbackOrch response")
        segments = {
            segment_id: _segment_entry(segment_id, segment)
            for segment_id, segment in segments_raw.items()
        }
        return {key: segments[key] for key in sorted(segments)}

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired if isinstance(diff.desired, dict) else {}
        # diff.desired is already the full structure (build_plan merges any
        # set/delete onto the fetched+normalized current state before
        # canonicalize_desired ever runs — see module docstring), so this is
        # never a partial-object POST.
        ctx.client.post(_LOOPBACK_ORCH_PATH, desired)
        log.debug("loopback_orch_apply", segments=sorted(desired))
        return ApplyResult(
            ok=True, message=f"loopback orchestration replaced ({len(desired)} segment(s))"
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # A snapshot recorded as absent must never replay as "POST an
            # empty structure" — that could unintentionally wipe every
            # segment's pool. Refuse loudly, same as zones/deployment.
            return ApplyResult(
                ok=False,
                message="no usable snapshot; refusing to POST an empty loopbackOrch structure",
            )
        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        ctx.client.post(_LOOPBACK_ORCH_PATH, restored)
        return ApplyResult(ok=True, message="loopback orchestration restored from snapshot")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name="global")]


register(LoopbackOrch())


# -- read-only views ----------------------------------------------------------


def pool_detail(ctx: Ctx) -> dict[str, Any]:
    """Loopback pool allocation info per segment (``GET /loopbackOrch/pool``):
    ``{"<segmentId>": {segment, subnet, totalAddr, addrAllocated,
    addrDeleted}}``. Read-only — never part of the diffable canonical state.
    """
    raw = ctx.client.get(_LOOPBACK_ORCH_POOL_PATH)
    return raw if isinstance(raw, dict) else {}


def reclaim_deleted_ips(ctx: Ctx, loopback_id: int) -> None:
    """Reclaim one deleted loopback IP back into the pool.

    ``DELETE /loopbackOrch/pool/reclaim?id=<loopback_id>``. An explicit
    maintenance action, not configuration — never part of the diffable state,
    so it is never invoked from ``apply()``.

    ``loopback_id`` has no default because the wire marks it required and the
    vendor's "reclaim all" mode has no route anyone here can point at:
    :data:`RECLAIM_ID_IS_A_QUERY_PARAM`, :data:`RECLAIM_ALL_HAS_NO_KNOWN_ROUTE`.
    """
    ctx.client.delete(_LOOPBACK_ORCH_RECLAIM_PATH, params={"id": loopback_id})
