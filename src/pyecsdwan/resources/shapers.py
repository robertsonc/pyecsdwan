"""Outbound / inbound traffic shapers — appliance-scope (#33).

Companion to ``resources/policy_maps.py`` (the QoS / optimization / route
policy *maps* from the same issue). The maps decide which traffic class a
flow lands in; the shapers here decide what bandwidth each traffic class
actually gets. They share an issue but not a payload shape, so they are
separate resources with separate normalize logic — see below.

Endpoint facts
--------------

* **Orchestrator** exposes ``/shaper`` and ``/inboundShaper`` **GET-only**;
  there is no orchestrator-scope write path.
* **Appliance (ECOS)**, through the Orchestrator's appliance proxy
  (``ctx.client.appliance_request`` — the channel ``resources/routes.py`` /
  ``resources/vrrp.py`` / ``resources/appliance_zones.py`` use):
  ``GET/POST shapers`` and ``GET/POST inboundShapers``, both whole-table:
  the spec's own note is *"This API modifies shaper configuration for all
  interfaces"*. The per-interface ``shapers/{id}`` / ``inboundShapers/{id}``
  GET/POST/DELETE variants are deliberately unused — one whole-table write
  keeps ``apply()`` a single request and makes ``rollback()`` its exact
  symmetric inverse (snapshot in, snapshot back out), earning REVERSIBLE.

Payload shape (live-captured read-only this session, both populated)
--------------------------------------------------------------------

Unlike the policy maps, shapers are **not** wrapped in a ``data``/``options``
envelope — they are bare interface-keyed tables::

    # shapers  (~34KB on the probed appliance)
    {"default": {"accuracy": 1000, "dyn_bw_enable": ..., "enable": ...,
                 "max_bw": ..., "traffic-class": {"1": {...}, ..., "10": {...}}},
     "wan": {...}, "mgmt0": {...}, "lan0": {...}, "wan0": {...}, ...}

    # inboundShapers — same shape plus a per-interface if_shaping_enable
    {"wan": {"accuracy": 5000, "dyn_bw_enable": ..., "enable": ...,
             "if_shaping_enable": ..., "max_bw": ..., "traffic-class": {...}},
     "mgmt0": {...}, "lan0": {...}, "wan0": {...}, "wan1": {...}}

Per the appliance spec, ``traffic-class`` holds classes ``1``..``10``, each
``{name, priority, min_bw, max_bw, excess, max_wait}``. Note the spec places
``enable``/``if_shaping_enable`` at the *top* level of ``InboundShaper``
while the live capture carries them *per interface*; live wins here, and
because normalization is a per-interface passthrough (see below) both layouts
survive a round trip untouched.

Normalization is deliberately conservative: no field defaults are invented.
Only three things happen — ``self``/``gms_marked`` bookkeeping is stripped
recursively (reusing ``security_policy._strip_meta``, which is shape-
agnostic), interfaces are ordered by name, and ``traffic-class`` keys are
ordered numerically and canonicalized to strings so ``1`` and ``"1"``
(JSON object keys are strings on the wire, but hand-authored YAML may use
ints) cannot diff against each other. Everything else passes through
untouched — this table's per-class tuning values are the operator's, and
guessing a server default here would inject exactly the phantom drift
``normalize()`` exists to prevent.

Because the candidate store merges a partial ``set`` over current canonical
state before ``canonicalize_desired()`` runs, an operator tuning one traffic
class on one interface still POSTs a complete, faithful table.

Deletability: these tables always exist on any appliance — the spec notes
*"By default there is a single system wan shaper with name 'wan'"* — so there
is no "absent" state and whole-resource delete is refused (``deletable =
False``, the same call ``resources/routes.py`` makes for the configured-route
table). Removing an interface's shaping means dropping that interface key,
not deleting the resource. Consistent with that, an empty table normalizes to
``{"interfaces": {}}`` rather than ``None``.

Ownership
---------

``ownership.KIND_TO_TEMPLATE_SECTIONS`` was pre-seeded with
``"appliance/shaper": ("shaper",)`` and ``shaper`` is a **CONFIRMED-real**
template section name (verified live this session against a real Default
Template Group's selected-section list), so the outbound resource uses that
key as-is. The inbound resource adds ``"appliance/inbound-shaper":
("shaper", "inboundShapers")`` — the ``shaper`` half is the confirmed section
(the Orchestrator's Shaper template covers both directions), the
``inboundShapers`` half is the UNVERIFIED ECOS-path-matching candidate, same
convention ``appliance_zones.py`` records for its unconfirmed sections.
``managed_by()`` checks per-object ``gms_marked`` first (the ``routes.py``
#15 pattern) before falling back to that join.
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
from pyecsdwan.resources.policy_maps import _has_gms_marked
from pyecsdwan.resources.security_policy import _strip_meta

log = structlog.get_logger("pyecsdwan.resources.shapers")

_TRAFFIC_CLASS_KEY = "traffic-class"


def _class_sort_key(key: str) -> tuple[int, int, str]:
    """Traffic classes are 1..10; sort numerically, keep anything else last."""
    return (0, int(key), "") if key.isdigit() else (1, 0, key)


def _interface_entry(name: str, entry: Any) -> dict[str, Any]:
    """Shape one interface's shaper record. Unknown fields pass through."""
    if not isinstance(entry, Mapping):
        raise ValueError(
            f"shaper interface {name!r} must be a mapping of shaper fields "
            f"(max_bw, enable, traffic-class, ...), got {type(entry).__name__}"
        )
    out: dict[str, Any] = {}
    for key, value in entry.items():
        if key == _TRAFFIC_CLASS_KEY:
            continue
        out[str(key)] = value
    classes_raw = entry.get(_TRAFFIC_CLASS_KEY)
    if classes_raw is None:
        return out
    if not isinstance(classes_raw, Mapping):
        raise ValueError(
            f"shaper interface {name!r} '{_TRAFFIC_CLASS_KEY}' must be a mapping "
            f"of class-id -> settings, got {type(classes_raw).__name__}"
        )
    # JSON object keys arrive as strings, but hand-authored YAML may use ints;
    # canonicalize to strings so 1 and "1" cannot diff against each other.
    # Collisions between the two are rejected earlier, before _strip_meta
    # (which stringifies keys recursively) can silently collapse them — see
    # _reject_class_key_collisions.
    classes = {str(cid): value for cid, value in classes_raw.items()}
    out[_TRAFFIC_CLASS_KEY] = {
        cid: classes[cid] for cid in sorted(classes, key=_class_sort_key)
    }
    return out


def _reject_class_key_collisions(source: Mapping[str, Any]) -> None:
    """Refuse a table where 1 and "1" both name a traffic class.

    Must run against the *raw* table: ``security_policy._strip_meta`` rebuilds
    every mapping with ``str(key)``, so by the time stripping is done the two
    have already merged into one and the ambiguity is invisible. Silently
    keeping whichever entry happened to be last would drop half of a
    hand-authored table without a word.
    """
    for name, entry in source.items():
        if not isinstance(entry, Mapping):
            continue
        classes = entry.get(_TRAFFIC_CLASS_KEY)
        if not isinstance(classes, Mapping):
            continue
        seen: set[str] = set()
        for cid in classes:
            key = str(cid)
            if key in seen:
                raise ValueError(
                    f"shaper interface {str(name)!r} has duplicate traffic-class "
                    f"id {key!r} after key canonicalization"
                )
            seen.add(key)


class _Shapers(Resource):
    """Shared implementation for the two interface-keyed shaper tables.

    ``shapers`` and ``inboundShapers`` differ only in the ECOS path and in
    one extra per-interface flag the inbound table carries
    (``if_shaping_enable``), which normalization passes through like any
    other unknown field — so both are one curated code path rather than two
    near-copies.
    """

    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: No confirmed ordering constraint against another curated resource
    #: this session. (Shapers size traffic classes that the policy maps in
    #: policy_maps.py assign to; classes 1..10 exist unconditionally, so
    #: neither ordering makes the other's write fail.)
    dependencies: tuple[str, ...] = ()
    #: The table always exists on any appliance (there is always at least the
    #: system 'wan' shaper), so there is no "absent" state — delete an
    #: interface key, not the resource.
    deletable = False

    #: ECOS path under the appliance proxy, e.g. "shapers".
    ecos_path: str = ""
    #: Human label used in operator-facing messages.
    label: str = ""

    @staticmethod
    def _ne_pk(ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{ref.kind} is appliance-scoped; ref.appliance is required")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side -------------------------------------------------------------

    def write_target(self, ctx: Ctx, ref: Ref) -> str | None:
        """The ECOS object at ``ecos_path`` on one appliance (#69).

        Each subclass replaces its whole object, and each has a distinct
        ``ecos_path``, so one declaration here is correct for all of them
        rather than copies that could drift apart. Scoped by nePk as well as
        path: the same map on two appliances is two objects, and grouping them
        would refuse a legitimate fan-out.
        """
        return f"appliance {self._ne_pk(ctx, ref)} {self.ecos_path}"

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.appliance_request("GET", self._ne_pk(ctx, ref), self.ecos_path)
        if raw is None or isinstance(raw, dict):
            return raw
        raise ValueError(f"unexpected {self.ecos_path} response shape from {ref}: {raw!r}")

    def normalize(self, raw: RawState) -> CanonicalState:
        if raw is None:
            return {"interfaces": {}}
        if not isinstance(raw, dict):
            raise ValueError(
                f"{self.ecos_path} state must be a mapping of interface -> shaper "
                f"config, got {type(raw).__name__}"
            )
        # Accepts both the bare wire shape and this module's own canonical
        # {"interfaces": {...}} round-tripping back through.
        source: Any = raw.get("interfaces", raw) if "interfaces" in raw else raw
        if not isinstance(source, Mapping):
            raise ValueError(
                f"{self.ecos_path} 'interfaces' must be a mapping of interface -> "
                f"shaper config, got {type(source).__name__}"
            )
        _reject_class_key_collisions(source)
        stripped = _strip_meta(dict(source))
        assert isinstance(stripped, dict)
        interfaces = {
            str(name): _interface_entry(str(name), entry)
            for name, entry in stripped.items()
        }
        ordered = {name: interfaces[name] for name in sorted(interfaces)}
        return {"interfaces": ordered}

    # -- write side ------------------------------------------------------------

    def _write(
        self, ctx: Ctx, ref: Ref, canonical: CanonicalState, verb: str
    ) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        state = canonical if isinstance(canonical, dict) else {}
        interfaces: dict[str, Any] = state.get("interfaces") or {}
        # The wire shape is the bare interface-keyed table — no envelope.
        ctx.client.appliance_request("POST", ne_pk, self.ecos_path, json_body=interfaces)
        classes = sum(
            len(entry.get(_TRAFFIC_CLASS_KEY, {}))
            for entry in interfaces.values()
            if isinstance(entry, Mapping)
        )
        log.debug(
            "shaper_write",
            ref=str(ref),
            ecos_path=self.ecos_path,
            verb=verb,
            interfaces=len(interfaces),
            traffic_classes=classes,
        )
        message = (
            f"{self.label} {verb} on {ne_pk}: {len(interfaces)} interface(s), "
            f"{classes} traffic-class entr(ies)"
        )
        outcome = ctx.save_changes([ne_pk], f"{self.label} {verb}: {ref}")
        if outcome.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                jobs=[outcome],
                message=(
                    f"{message} — not persisted, "
                    f"save-changes {outcome.state}: {outcome.detail}"
                ),
            )
        return ApplyResult(ok=True, jobs=[outcome], message=f"{message}, persisted")

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        return self._write(ctx, diff.ref, diff.desired, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        return self._write(ctx, ref, self.normalize(snapshot), "rollback")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name="global", appliance=name)
            for name in ctx.resolver.appliance_names()
        ]

    # -- ownership -------------------------------------------------------------

    def managed_by(self, ctx: Ctx, ref: Ref) -> Ownership:
        ne_pk = self._ne_pk(ctx, ref)
        if _has_gms_marked(self.fetch(ctx, ref)):
            return Ownership.owned(
                f"gms (gms_marked entry present in {self.ecos_path} on this appliance)"
            )
        return ownership.owning_group(ctx, self.kind, ne_pk)


def _desired_doc(ecos_path: str) -> str:
    return (
        "interfaces: {interfaceName: {max_bw, enable, accuracy, "
        "dyn_bw_enable, traffic-class: {1..10: {name, priority, min_bw, "
        "max_bw, excess, max_wait}}}}"
        + (", if_shaping_enable per interface" if ecos_path == "inboundShapers" else "")
        + f". Whole-table replace against ECOS '{ecos_path}' (the API writes "
        "all interfaces at once), persisted via save-changes. No field "
        "defaults are injected: unknown fields pass through untouched."
    )


class Shapers(_Shapers):
    kind = "appliance/shaper"
    ecos_path = "shapers"
    label = "shapers"
    desired_state_doc = _desired_doc("shapers")
    #: trafficclass is the shared read-only name table.
    endpoints = (
        "appliance GET /shapers",
        "appliance POST /shapers",
        "appliance GET /trafficclass",
    )


class InboundShapers(_Shapers):
    kind = "appliance/inbound-shaper"
    ecos_path = "inboundShapers"
    label = "inbound shapers"
    desired_state_doc = _desired_doc("inboundShapers")
    endpoints = (
        "appliance GET /inboundShapers",
        "appliance POST /inboundShapers",
    )


# -- read-only views ----------------------------------------------------------


def traffic_classes(ctx: Ctx, appliance: str) -> dict[str, Any]:
    """The appliance's traffic-class name table (ECOS ``trafficclass``).

    Read-only here: the names are referenced by both shaper tables and by the
    QoS maps, but they are a separate write surface (``GET/POST
    trafficclass``) that no resource in this issue owns. Exposed for
    `show`/audit, the role ``routes.all_routes`` plays for learned routes.
    """
    ne_pk = ctx.resolver.ne_pk_for(appliance)
    raw = ctx.client.appliance_request("GET", ne_pk, "trafficclass")
    return raw if isinstance(raw, dict) else {}


register(Shapers())
register(InboundShapers())
