"""Orchestrator internal-subnet list (Phase 3, #37).

The internal-subnet table is how the fabric classifies traffic: anything whose
destination does *not* match one of these prefixes is treated as internet
traffic. Orchestrator owns one fabric-wide copy and pushes it to every
appliance, so this is orchestrator scope — a plain ``ctx.client.get/post``,
never the appliance proxy.

Endpoint facts (``specs/orchestrator-openapi-7.2.0.json`` ``internalSubnets``
tag, and pyedgeconnect ``orch/_internal_subnets.py``):

* ``GET /gms/internalSubnets2`` → ``InternalSubnetsApiObject`` — the whole
  table. The only read path; there is no per-entry endpoint.
* ``POST /gms/internalSubnets2`` — **full-object replace** ("This will
  overwrite current subnets", per the SDK's own warning). Both sides of the
  round trip are the same complete object, which makes snapshot/restore exact:
  ``Reversibility.REVERSIBLE``.
* The API caps each of ``ipv4``/``ipv6`` at 512 entries (spec text). Exceeding
  it surfaces as an API error on apply; this resource does not second-guess it.

Confirmed live payload shape (captured read-only this session against a live
lab Orchestrator — see issue #37)::

    {"ipv4": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
              "169.254.0.0/16", "224.0.0.0/4"],
     "ipv6": ["fe80::/10", "ff00::/8", "fc00::/7"],
     "segmentIpv4": [], "segmentIpv6": [],
     "segmentedIpv6Enabled": true, "nonDefaultRoutes": false}

Spec-vs-live divergence: ``segmentedIpv6Enabled`` is present live but absent
from ``InternalSubnetsApiObject`` in the vendored 7.2.0 spec, and the SDK's
``update_internal_subnets()`` omits ``segmentIpv6`` from its POST body as
well. Nothing here invents either key: the four list keys plus
``nonDefaultRoutes`` are spec-declared and normalized, and every other
top-level key — ``segmentedIpv6Enabled`` included — rides the unknown-key
passthrough untouched, so a full-object POST re-sends exactly what the server
reported rather than a default this code made up.

Canonical form: ``ipv4``/``ipv6`` hold bare CIDR strings; ``segmentIpv4``/
``segmentIpv6`` hold ``"<vrfId>:<cidr>"`` entries (the spec's documented
segment format, e.g. ``"1:192.168.0.0/16"``). ``normalize()`` parses every
entry with :mod:`ipaddress`, renders it back in the library's canonical text
(which is exactly the spelling the live capture above uses), deduplicates, and
sorts numerically by (segment id, address family, network address, prefix
length) — so ``9.0.0.0/8`` sorts before ``10.0.0.0/8`` instead of after it,
and a re-plan of unchanged intent diffs empty. A malformed or wrong-family
entry raises ``ValueError`` naming the offending value.

Singleton (``deletable = False``, instance name ``global``), like
interface-labels and zones: there is no "the internal-subnet table does not
exist" state, and an empty table would reclassify the entire fabric's traffic
as internet-bound — so a whole-resource delete is refused, and rollback from
an absent snapshot is refused loudly rather than replayed as an empty POST.

Subnet-sharing options (issue #37, second half) — NOT MODELABLE, deliberately
not implemented here:

    The issue also asked for a per-appliance subnet-sharing-options resource
    over ``POST /subnets/setSubnetSharingOptions?nePk=`` with body
    ``{"auto_subnet": {"self", "add_local", "add_local_lan",
    "add_local_wan"}}``. It has no read path anywhere:

    * ``/subnets/setSubnetSharingOptions`` is POST-only in
      ``specs/orchestrator-openapi-7.2.0.json`` (the ``subnets`` tag exposes
      only ``GET /subnets``, ``GET /subnets/all``,
      ``GET /subnets/forDiscovered``, ``POST /subnets/configured`` and this
      write), and ``specs/appliance-openapi-7.2.0.json`` has no ECOS
      counterpart that returns ``auto_subnet``.
    * A live ``GET /subnets?nePk=`` returns the learned/configured route
      table (per-prefix ``state.prefix``/``learned``/``advert`` records), not
      the sharing configuration. Grepping that live response for ``auto`` /
      ``shar`` yields only a per-route ``state.automatic`` boolean, which is
      route state, not the ``auto_subnet`` config block.

    A write-only endpoint cannot be a Tier-2 diffable resource: with no read
    there is no ``fetch()``, therefore no snapshot, no diff, and no rollback.
    Modeling one would mean fabricating a "current state" the server never
    reported, which the promotion checklist forbids ("Do not invent
    payloads"). It needs either an undiscovered read endpoint or modeling as
    a Tier-0-style fire-and-forget action outside the transaction engine —
    tracked as a ``docs/futures/`` entry, not as a stub in this package.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
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
from pyecsdwan.registry import register

log = structlog.get_logger("pyecsdwan.resources.internal_subnets")

_PATH = "/gms/internalSubnets2"

#: Spec-declared keys this resource normalizes. Everything else is passed
#: through untouched (see the module docstring on ``segmentedIpv6Enabled``).
_LIST_KEYS: tuple[str, ...] = ("ipv4", "ipv6", "segmentIpv4", "segmentIpv6")
_KNOWN_KEYS: frozenset[str] = frozenset((*_LIST_KEYS, "nonDefaultRoutes"))

_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_network(where: str, value: Any) -> _IPNetwork:
    """Parse one CIDR string, or raise ``ValueError`` naming the bad value.

    ``strict=False``: an entry carrying host bits (``10.1.2.3/8``) is masked
    to its network address rather than rejected, so a value the server itself
    reported can never make ``fetch()`` explode.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"{where} must be a CIDR string, got {type(value).__name__}: {value!r}"
        )
    try:
        return ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as exc:
        raise ValueError(f"{where}: {value!r} is not a valid CIDR network ({exc})") from exc


def _check_family(key: str, where: str, net: _IPNetwork, version: int) -> None:
    if net.version != version:
        raise ValueError(
            f"{where}: {str(net)!r} is an IPv{net.version} network but "
            f"'{key}' holds IPv{version} networks"
        )


def _net_sort_key(net: _IPNetwork) -> tuple[int, int, int]:
    return (net.version, int(net.network_address), net.prefixlen)


def _entries(key: str, values: Any) -> list[Any]:
    if values is None:
        return []
    # A bare string would iterate character-by-character into nonsense errors.
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError(
            f"'{key}' must be a list of CIDR strings, got {type(values).__name__}: {values!r}"
        )
    return list(values)


def _cidr_list(key: str, values: Any, version: int) -> list[str]:
    """Canonical, deduplicated, numerically-sorted plain CIDR list."""
    parsed: dict[str, _IPNetwork] = {}
    for index, value in enumerate(_entries(key, values)):
        where = f"{key}[{index}]"
        net = _parse_network(where, value)
        _check_family(key, where, net, version)
        parsed[str(net)] = net
    return sorted(parsed, key=lambda text: _net_sort_key(parsed[text]))


def _segment_cidr_list(key: str, values: Any, version: int) -> list[str]:
    """Same, for the ``"<vrfId>:<cidr>"`` segment lists.

    Split on the FIRST colon only — an IPv6 entry (``"1:fe80::/10"``) carries
    plenty more of them.
    """
    parsed: dict[str, tuple[int, _IPNetwork]] = {}
    for index, value in enumerate(_entries(key, values)):
        where = f"{key}[{index}]"
        if not isinstance(value, str):
            raise ValueError(
                f"{where} must be a '<vrfId>:<cidr>' string, "
                f"got {type(value).__name__}: {value!r}"
            )
        segment_text, sep, cidr = value.strip().partition(":")
        if not sep or not cidr:
            raise ValueError(
                f"{where}: {value!r} is missing the segment prefix — "
                f"'{key}' entries are '<vrfId>:<cidr>', e.g. '1:192.168.0.0/16'"
            )
        try:
            segment_id = int(segment_text)
        except ValueError as exc:
            raise ValueError(
                f"{where}: {segment_text!r} is not a numeric VRF segment id "
                f"(in {value!r})"
            ) from exc
        if segment_id < 0:
            raise ValueError(f"{where}: VRF segment id must not be negative (in {value!r})")
        net = _parse_network(where, cidr)
        _check_family(key, where, net, version)
        parsed[f"{segment_id}:{net}"] = (segment_id, net)

    def sort_key(text: str) -> tuple[int, int, int, int]:
        segment_id, net = parsed[text]
        return (segment_id, *_net_sort_key(net))

    return sorted(parsed, key=sort_key)


class InternalSubnets(Resource):
    kind = "internal-subnets"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: Singleton table: there is no "no internal subnets configured" state to
    #: delete into — an empty table reclassifies all fabric traffic as
    #: internet-bound. Delete individual list entries instead.
    deletable = False
    desired_state_doc = (
        "ipv4/ipv6: lists of CIDR strings (max 512 each, per the API). "
        "segmentIpv4/segmentIpv6: lists of '<vrfId>:<cidr>' entries, e.g. "
        "'1:192.168.0.0/16'. nonDefaultRoutes: bool — treat non-default "
        "routes as internal subnets. Traffic not matching any entry is "
        "classified as internet traffic and the table is pushed to every "
        "appliance, so applies are a full-object replace of the whole list."
    )

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.get(_PATH)
        return raw if isinstance(raw, dict) else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"internal subnets must be a mapping of "
                f"{{ipv4, ipv6, segmentIpv4, segmentIpv6, nonDefaultRoutes}}, "
                f"got {type(raw).__name__}"
            )
        out: dict[str, Any] = {
            "ipv4": _cidr_list("ipv4", raw.get("ipv4"), 4),
            "ipv6": _cidr_list("ipv6", raw.get("ipv6"), 6),
            "segmentIpv4": _segment_cidr_list("segmentIpv4", raw.get("segmentIpv4"), 4),
            "segmentIpv6": _segment_cidr_list("segmentIpv6", raw.get("segmentIpv6"), 6),
            "nonDefaultRoutes": bool(raw.get("nonDefaultRoutes", False)),
        }
        # Unknown top-level keys (segmentedIpv6Enabled and anything a newer
        # Orchestrator build adds) survive verbatim: apply() re-POSTs the whole
        # object, so dropping one here would silently reset server config.
        extra = {str(key): value for key, value in raw.items() if str(key) not in _KNOWN_KEYS}
        for key in sorted(extra):
            out[key] = extra[key]
        return out

    # -- write side -----------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired
        if not isinstance(desired, dict):
            # Whole-resource delete is refused by the engine (deletable=False),
            # so a non-dict desired here means a caller bypassed the plan.
            return ApplyResult(
                ok=False,
                message="no desired internal-subnet table; refusing to POST an empty list",
            )
        # Full-object replace: build_plan merged any set/delete onto the
        # fetched+normalized current state before diffing, so `desired` is
        # already the complete table — POSTing it drops no untouched key.
        ctx.client.post(_PATH, desired)
        log.debug("internal_subnets_applied", ref=str(diff.ref), **_counts(desired))
        return ApplyResult(ok=True, message=f"internal subnets replaced ({_summary(desired)})")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # A snapshot recorded as absent (a degenerate GET at commit time)
            # must never replay as "POST an empty table" — that would classify
            # every destination as internet traffic fabric-wide. Refuse loudly.
            return ApplyResult(
                ok=False,
                message="no usable snapshot; refusing to POST an empty internal-subnet table",
            )
        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        ctx.client.post(_PATH, restored)
        log.debug("internal_subnets_rolled_back", ref=str(ref), **_counts(restored))
        return ApplyResult(
            ok=True, message=f"internal subnets restored from snapshot ({_summary(restored)})"
        )

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name="global")]


def _counts(state: Mapping[str, Any]) -> dict[str, int]:
    return {key: len(state.get(key) or []) for key in _LIST_KEYS}


def _summary(state: Mapping[str, Any]) -> str:
    counts = ", ".join(f"{key}={value}" for key, value in _counts(state).items())
    return f"{counts}, nonDefaultRoutes={bool(state.get('nonDefaultRoutes'))}"


register(InternalSubnets())
