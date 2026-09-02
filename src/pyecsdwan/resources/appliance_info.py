"""Appliance extra info: location, contact and per-appliance overlay settings.

Orchestrator-scope, one object per appliance, behind
``/appliance/extraInfo?nePk=``::

    GET    -> {"contact": {...}, "location": {...}, "overlaySettings": {...}}
    POST   -> saves the whole object (the body is the new object, not a patch)
    DELETE -> clears it (deliberately unused; see below)

Why this exists (2026-09-02): a lab's preconfiguration stamped a city into
every appliance's location and left ``country`` at its default, and the only
way to correct eighteen appliances was eighteen trips through the UI. With this
kind it is eighteen ``set`` lines and one ``commit``::

    set appliance-info BR1-EC location country Canada
    commit

Verified live on 2026-09-02 (Orchestrator 9.7.0.43282, 18 appliances): read
sweep clean, no-op round trip empty, one country changed and verified with
every other field untouched, rolled back byte-identical to the baseline —
``docs/sitrep/2026-09-02-appliance-info-live.md``. The live object uses
``null`` for fields nobody set, ``""`` for fields someone cleared; the
canonical form treats both as absent and the write carries both back.

Shape decisions, each load-bearing:

* **Canonical form drops empty strings and the sections they empty out.** The
  schema types every leaf but one as a string, and an unset field is ``""`` to
  the server: a fresh appliance answers ``"address": ""`` for a field nobody
  set. Comparing on the pruned form means a server that materializes ``""``
  for keys the client omitted cannot produce phantom drift, and an operator
  clears a field with ``set ... country ""`` exactly as they set it. The one
  boolean (``isUserDefinedIPSecUDPPort``) is kept as it is: ``False`` is a
  value, not an absence.
* **The write is always the complete object.** POST replaces; a body carrying
  only the changed section would erase the other two (spec 003, D7).
  :func:`_body` composes the canonical desired state over a *fresh raw read*
  of the object — raw, not canonical, because the canonical form has already
  dropped the empty strings and empty sections the server holds, and a
  replacement body must carry them back or lose them. Every string the
  desired state no longer names goes out as an explicit ``""``, so a clear
  reaches the server as a clear rather than as an omission it is free to
  ignore. The read-modify-write is what makes the write order-safe, as
  ``appliance/dhcp`` is.
* **No absent state.** The Orchestrator answers an object for every appliance
  it manages, so a whole-resource ``delete`` has nothing to mean here and
  ``deletable`` is False. ``DELETE /appliance/extraInfo`` is therefore not
  used, and not declared as covered. Rollback to a ``None`` snapshot refuses
  for the same reason ``region-association`` does: guessing "empty" would
  erase whatever was there.
* Unknown fields pass through unchanged, in both directions.

Vendored spec: ``ApplianceExtraInfo`` = ``Contact`` (email, name,
phoneNumber) + ``Location`` (address, address2, city, country, state, zipCode)
+ ``AEIOverlaySettings`` (ipsecUdpPort, isUserDefinedIPSecUDPPort). The 9.6
payload examples carry the same three sections.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import structlog

from pyecsdwan.client import OrchApiError
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
from pyecsdwan.registry import default_registry

log = structlog.get_logger("pyecsdwan.resources.appliance_info")

_PATH = "/appliance/extraInfo"

#: The three sections the schema names. Unknown ones pass through beside them.
SECTIONS = ("contact", "location", "overlaySettings")


def _prune(value: Any) -> Any:
    """One level of the canonical shape: ``None`` for an empty string or a
    mapping nothing survives in, so the parent drops it; everything else —
    ``False`` and ``0`` included — is a value and stays."""
    if isinstance(value, Mapping):
        kept: dict[str, Any] = {}
        for key, item in value.items():
            pruned = _prune(item)
            if pruned is not None:
                kept[str(key)] = pruned
        return kept or None
    if isinstance(value, str):
        return value if value != "" else None
    if value is None:
        return None
    return copy.deepcopy(value)


def _body(current: Any, desired: Any) -> dict[str, Any]:
    """The complete object to POST: ``desired`` over ``current``.

    Every string leaf that ``current`` has and ``desired`` no longer names is
    sent as ``""``. A key that is merely absent from a replacement body may be
    kept or dropped by the server — the API does not say — and only an
    explicit empty string is unambiguous. A non-string leaf the desired state
    dropped is carried from current: there is no "clear" for a boolean, and
    inventing one would be a write nobody asked for.
    """
    cur = current if isinstance(current, Mapping) else {}
    des = desired if isinstance(desired, Mapping) else {}
    out: dict[str, Any] = {}
    for key in [*des, *(k for k in cur if k not in des)]:
        if key in des:
            wanted = des[key]
            if isinstance(wanted, Mapping):
                below = cur.get(key)
                out[key] = _body(below if isinstance(below, Mapping) else {}, wanted)
            else:
                out[key] = copy.deepcopy(wanted)
            continue
        had = cur[key]
        if isinstance(had, Mapping):
            out[key] = _body(had, {})
        elif isinstance(had, str):
            out[key] = ""
        else:
            out[key] = copy.deepcopy(had)
    return out


class ApplianceInfo(Resource):
    #: The noun is ``appliance-info``; the API calls the object
    #: ``applianceExtraInfo``. One spelling on purpose — the offline command
    #: reference lists primary nouns, and an alias it cannot show is a noun
    #: nobody can discover.
    kind = "appliance-info"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: Every managed appliance has this object; there is nothing to delete
    #: as a whole. Clear a field with ``set ... <field> ""``.
    deletable = False
    desired_state_doc = (
        "location: {address, address2, city, state, zipCode, country}; "
        "contact: {name, email, phoneNumber}; "
        "overlaySettings: {ipsecUdpPort, isUserDefinedIPSecUDPPort}. Every field "
        "is a string except the boolean; an empty string clears a field. The "
        "instance name is the appliance hostname."
    )
    #: GET and POST only. DELETE exists and is deliberately unused: the kind
    #: has no absent state to write.
    endpoints = (
        "orchestrator GET /appliance/extraInfo",
        "orchestrator POST /appliance/extraInfo",
    )

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        try:
            raw = ctx.client.get(_PATH, params={"nePk": ne_pk})
        except OrchApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, Mapping):
            return None
        pruned = _prune(raw)
        # The object exists for every managed appliance; one with nothing set
        # is an empty object, never an absence.
        return pruned if isinstance(pruned, dict) else {}

    # -- write side -------------------------------------------------------------

    def write_target(self, ctx: Ctx, ref: Ref) -> str | None:
        """The POST replaces one appliance's whole extraInfo object (#69)."""
        return f"appliance {ctx.resolver.ne_pk_for(ref.name)} extra-info"

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        if diff.desired is None:
            raise ValueError(
                f"{self.kind} has no absent state; clear fields with "
                f"`set {self.kind} {ref.name} <section> <field> \"\"` instead "
                f"(deletable=False guards this at plan time)"
            )
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        # Over the raw object as it is now, not `diff.current`: canonical
        # state has pruned the empty strings and empty sections the server
        # holds, and a replacement body that omits them would drop them.
        body = _body(self.fetch(ctx, ref), diff.desired)
        ctx.client.post(_PATH, body, params={"nePk": ne_pk})
        log.debug("appliance_info_applied", appliance=ref.name, changed=len(diff.entries))
        return ApplyResult(
            ok=True,
            message=f"appliance {ref.name!r} extra info saved ({len(diff.entries)} change(s))",
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            return ApplyResult(
                ok=False,
                message=(
                    f"no usable snapshot for {ref.name!r}; refusing to guess "
                    f"appliance extra info (every managed appliance has this "
                    f"object, so 'absent' is not a state to restore)"
                ),
            )
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        # The raw snapshot over the raw object as it is now: the snapshot is
        # restored field for field, and a field the change added is cleared
        # explicitly rather than left to the server's reading of an omitted
        # key.
        ctx.client.post(_PATH, _body(self.fetch(ctx, ref), snapshot), params={"nePk": ne_pk})
        return ApplyResult(ok=True, message=f"appliance {ref.name!r} extra info restored")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name=str(a["hostName"]))
            for a in ctx.resolver.appliances()
            if a.get("hostName")
        ]


default_registry.register(ApplianceInfo())
