"""Active-flow reports: ``show flows summary`` (#58) and ``show flow <ip>`` (#59).

Both read the *same* endpoint with the *same* row parsing, which is why they
live in one module. Splitting them would have produced two flow parsers that
disagreed the first time a field moved.

The endpoint
------------
``GET /flow``, verified in ``specs/orchestrator-openapi-7.2.0.json``. The
parent issue named ``GET /flow/{neId}/q``; that path exists in neither
vendored baseline — it is an ``EC_SD-WAN_Expert`` internal URL shape, the same
class of error as the epic that named pyedgeconnect *module* names as REST
paths.

``nePk`` and ``maxFlows`` are both **required**: omitting either is a 400 live
and a 422 against the bundled mock. Every call this module makes sends both.

Reading a row
-------------
* ``ip1`` / ``ip2`` are dotted strings. The row also carries ``ip1_1``…
  ``ip1_4`` plus ``ip1Version`` — that is the 128-bit integer form for IPv6
  and is not what a report should read.
* ``overlayName`` arrives **already resolved**. There is no ID-to-name lookup
  here and adding one would be wrong. It may name *two* overlays
  (``"overlay1 | overlay2"``, inbound and outbound); see the counting note.
* There is no ``bytes`` field. Traffic is four directional counters —
  ``outboundTxBytes`` / ``outboundRxBytes`` / ``inboundTxBytes`` /
  ``inboundRxBytes``.
* There is no ``transport`` field either: ``transport`` is a *request* filter
  with no response counterpart, so :attr:`FlowRow.transport` is **derived**
  from the overlay classification (``fabric`` on a named SD-WAN overlay,
  ``breakout`` for built-in/passthrough traffic) and labeled as derived
  wherever it is rendered.

Why the summary counts rows instead of reading per-overlay summaries
--------------------------------------------------------------------
The response carries a computed ``active`` block (``total_flows``,
``flows_optimized``, ``flows_passthrough``, …). It was worth asking whether
#58's matrix could be built from a handful of cheap summary reads — repeating
the call once per overlay and reading ``active`` — rather than pulling rows.
It cannot, for three independent reasons, each sufficient on its own:

1. **``active`` has no per-overlay breakdown.** The only way to get one is one
   request per *(appliance x overlay)*, which is strictly **more** requests
   than the one-per-appliance call that already returns the rows. Against a
   low-QPS control plane that is the expensive way to be wrong.
2. **The ``overlays`` filter takes overlay *IDs*** (spec: "Overlay ID.
   Multiple values must be separated with ``|``"), so it needs exactly the
   ID-to-name resolution the row data makes unnecessary — and the Orchestrator
   overlay inventory does not enumerate the overlays flows actually appear on
   (``/gms/overlays/config`` knows one overlay; the flow rows name three).
   There is no ID set to iterate.
3. **``overlayName`` can name two overlays at once.** A per-overlay count
   would tally such a flow under both columns, so the column totals would
   exceed the appliance total with no way to reconcile them.

So: one request per appliance, and count rows. The cost of that honesty is
that ``maxFlows`` silently bounds the answer, which is why every count carries
:attr:`ApplianceFlowCounts.bounded` and the report says so rather than
presenting a truncated tally as a total.

Counting is one flow, one cell: the overlay label is normalized to a single
canonical key (an inbound/outbound pair keeps its joined ``"a | b"`` form as
its own column), so the column totals always sum to the row totals.

What "one flow" means (the dedupe identity)
-------------------------------------------
An appliance reports the flows *it* sees. One conversation crossing two
appliances is reported twice, once from each end and with the endpoints in
opposite order — the bundled mock seeds exactly that: ``10.1.1.5`` appears on
``1.NE`` as ``ip2`` and on ``3.NE`` as ``ip1``, same ports, same conversation.
Reporting it twice would be a lie about the fabric.

The identity used to collapse those is::

    (protocol, sorted[(ip1, port1, vrf1), (ip2, port2, vrf2)])

Sorting the two endpoint triples is what makes it direction-free, so
``A:p -> B:q`` and ``B:q -> A:p`` land on the same key. Each endpoint keeps its
own VRF because the same 5-tuple in two segments is genuinely two flows.

Deliberately *not* part of the identity:

* ``flowId`` / ``flowSeq`` — per-appliance internal keys. Two appliances
  describing one conversation assign different ones, so they cannot serve as a
  fabric-wide identity.
* the appliance, the overlay, byte counters and uptime — all per-observer
  views of the same conversation.

Both appliances are attributed on the surviving row; neither is dropped. The
consequence to know: two genuinely distinct flows that share a protocol, both
endpoints and both VRFs collapse into one match. That is the same information
the 5-tuple gives an operator anywhere else, and preferring it over
double-reporting every hub-to-branch flow is the trade this report makes.

Byte counters are *not* summed across observations — each appliance reports
its own view of the same traffic and adding them double-counts. The table
shows the first-observed appliance's counters; ``--json`` carries every
observation.

Read-only throughout: no candidate, journal or transaction side effects, and
nothing here writes.
"""

from __future__ import annotations

import dataclasses
import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import structlog

from pyecsdwan.contract import Ctx
from pyecsdwan.reports.fanout import DEFAULT_CONCURRENCY, Outcome, fan_out, values

log = structlog.get_logger(__name__)

#: The one real path. See the module docstring on the issue's wrong one.
FLOWS_ENDPOINT = "/flow"

#: **The wire flag means the opposite of its name (#94).** The vendored
#: baseline documents ``ipEitherFlag`` as: "Enable directionality for IP. If
#: true, ip1 will be treated as the source IP, and ip2 will be treated as the
#: destination IP". So ``true`` is *directional* — ip1 matches the **source
#: only** — and ``false`` is the either-end match the name suggests.
#:
#: This module sent ``true`` and meant "either end", so `show fabric flow <ip>`
#: silently searched one direction: any host that mostly *receives* traffic
#: answered "no flows found". `docs/futures/README.md` had flagged the
#: name-versus-description contradiction as unverified; issue #94 is the live
#: run that settled it — four healthy appliances, each reporting a non-zero
#: flow census, returning zero matches for a real address.
#:
#: The bundled mock had encoded the same wrong belief, which is why every test
#: agreed with the bug. It now implements the documented semantics, so the
#: fixture can disagree with the code.
IP_EITHER_FLAG_IS_DIRECTIONAL = True

#: ``maxFlows`` is required, so there is no "everything" value — only a cap we
#: choose. Large enough that a normal branch appliance is not truncated, small
#: enough that a fleet-wide report stays a report.
DEFAULT_MAX_FLOWS = 1000

#: Column/bucket for traffic that is not on a named SD-WAN overlay: built-in
#: policy and passthrough. Accounted for, never dropped.
PASSTHROUGH = "Passthrough"

#: Row values that mean "not on a named overlay", compared case-folded.
_BUILT_IN_OVERLAYS = frozenset({"", "passthrough", "built-in", "builtin", "none"})

_TRANSPORT_FABRIC = "fabric"
_TRANSPORT_BREAKOUT = "breakout"


# -- targets -----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Target:
    """One appliance to query: display name plus the nePk the API wants."""

    name: str
    ne_pk: str

    def __str__(self) -> str:  # what fan_out logs on failure
        return self.name


def targets(
    ctx: Ctx,
    *,
    appliances: Sequence[str] | None = None,
    no_cache: bool = False,
) -> list[Target]:
    """Appliances to fan out over, sorted by hostname for a stable table.

    ``GET /flow`` itself has no cached/live switch — the cached read behind
    these reports is the resolver's appliance inventory, so ``no_cache``
    refreshes that and the flow data is live either way.
    """
    if no_cache:
        ctx.resolver.refresh("appliances")
    wanted = None if appliances is None else [a for a in appliances if a]
    if wanted:
        found = [
            Target(name=ctx.resolver.appliance_name_for(pk), ne_pk=pk)
            for pk in (ctx.resolver.ne_pk_for(name) for name in wanted)
        ]
    else:
        found = [
            Target(
                name=str(row.get("hostName") or row.get("nePk") or row.get("id") or ""),
                ne_pk=pk,
            )
            for row in ctx.resolver.appliances()
            if (pk := str(row.get("nePk") or row.get("id") or ""))
        ]
    # Deduped by nePk: naming an appliance twice must not count its flows
    # twice in the grand total.
    unique = {target.ne_pk: target for target in found}
    return sorted(unique.values(), key=lambda t: t.name)


# -- one row -----------------------------------------------------------------


@dataclasses.dataclass(frozen=True, order=True)
class Endpoint:
    """One end of a flow. ``vrf`` is part of it because the same address:port
    in two segments is two different endpoints."""

    ip: str
    port: int
    vrf: str

    def __str__(self) -> str:
        return f"{self.ip}:{self.port}"


#: Direction-free flow identity — see the module docstring.
FlowKey = tuple[str, Endpoint, Endpoint]


@dataclasses.dataclass(frozen=True)
class FlowRow:
    """One appliance's view of one flow, trimmed to what a report renders."""

    ne_pk: str
    appliance: str
    source: Endpoint
    destination: Endpoint
    protocol: str
    application: str
    overlay: str
    traffic_class: str
    status: str
    uptime_ms: int
    from_zone: str
    to_zone: str
    outbound_tx_bytes: int
    outbound_rx_bytes: int
    inbound_tx_bytes: int
    inbound_rx_bytes: int
    flow_id: str

    @property
    def built_in(self) -> bool:
        """True for passthrough / built-in policy traffic (no SD-WAN overlay)."""
        return self.overlay == PASSTHROUGH

    @property
    def transport(self) -> str:
        """Derived, not reported: the API's ``transport`` is a request filter
        with no response field. See the module docstring."""
        return _TRANSPORT_BREAKOUT if self.built_in else _TRANSPORT_FABRIC

    @property
    def total_bytes(self) -> int:
        """All four directional counters. There is no single ``bytes`` field."""
        return (
            self.outbound_tx_bytes
            + self.outbound_rx_bytes
            + self.inbound_tx_bytes
            + self.inbound_rx_bytes
        )

    @property
    def key(self) -> FlowKey:
        """Direction-free identity; see the module docstring for the trade-offs."""
        first, second = sorted((self.source, self.destination))
        return (self.protocol, first, second)

    def as_dict(self) -> dict[str, Any]:
        return {
            "appliance": self.appliance,
            "nePk": self.ne_pk,
            "flowId": self.flow_id,
            "src": {"ip": self.source.ip, "port": self.source.port, "vrf": self.source.vrf},
            "dst": {
                "ip": self.destination.ip,
                "port": self.destination.port,
                "vrf": self.destination.vrf,
            },
            "protocol": self.protocol,
            "application": self.application,
            "overlay": self.overlay,
            "built_in": self.built_in,
            "transport": self.transport,
            "traffic_class": self.traffic_class,
            "status": self.status,
            "uptime_ms": self.uptime_ms,
            "from_zone": self.from_zone,
            "to_zone": self.to_zone,
            "bytes": {
                "outbound_tx": self.outbound_tx_bytes,
                "outbound_rx": self.outbound_rx_bytes,
                "inbound_tx": self.inbound_tx_bytes,
                "inbound_rx": self.inbound_rx_bytes,
                "total": self.total_bytes,
            },
        }


def normalize_overlay(raw: Any) -> str:
    """``overlayName`` -> one canonical column key.

    Already-resolved names, so no lookup. An inbound/outbound pair
    (``"a | b"``) keeps its joined form as a single key rather than being
    counted under both, which is what keeps column totals equal to row totals.
    Anything that names no overlay buckets to :data:`PASSTHROUGH` so built-in
    traffic is accounted for instead of dropped.
    """
    text = "" if raw is None else str(raw).strip()
    parts: list[str] = []
    for chunk in text.split("|"):
        name = chunk.strip()
        if name and name.casefold() not in _BUILT_IN_OVERLAYS and name not in parts:
            parts.append(name)
    if not parts:
        return PASSTHROUGH
    return " | ".join(parts)


def _int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _text(raw: Any) -> str:
    return "" if raw is None else str(raw)


def parse_row(raw: Mapping[str, Any], *, target: Target) -> FlowRow:
    """One ``flows[]`` element -> :class:`FlowRow`.

    Reads the dotted ``ip1``/``ip2`` strings, never the ``ip1_1``…``ip1_4``
    128-bit quadruple. Tolerant of type drift (``status`` is an integer in the
    spec and a string in some builds) because a report must not die on a field
    it only prints.
    """
    return FlowRow(
        ne_pk=target.ne_pk,
        appliance=target.name,
        source=Endpoint(
            ip=_text(raw.get("ip1")), port=_int(raw.get("port1")), vrf=_text(raw.get("vrf1"))
        ),
        destination=Endpoint(
            ip=_text(raw.get("ip2")), port=_int(raw.get("port2")), vrf=_text(raw.get("vrf2"))
        ),
        protocol=_text(raw.get("protocol")).lower(),
        application=_text(raw.get("application")),
        overlay=normalize_overlay(raw.get("overlayName")),
        traffic_class=_text(raw.get("trafficClass")),
        status=_text(raw.get("status")),
        uptime_ms=_int(raw.get("uptime")),
        from_zone=_text(raw.get("fromZone")),
        to_zone=_text(raw.get("toZone")),
        outbound_tx_bytes=_int(raw.get("outboundTxBytes")),
        outbound_rx_bytes=_int(raw.get("outboundRxBytes")),
        inbound_tx_bytes=_int(raw.get("inboundTxBytes")),
        inbound_rx_bytes=_int(raw.get("inboundRxBytes")),
        flow_id=_text(raw.get("flowId")),
    )


# -- the fetch ---------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FlowFetch:
    """One appliance's answer, unpacked.

    ``reported_total`` is the response's own ``active.total_flows``; ``rows``
    is what came back. They differ when ``maxFlows`` truncated, which is
    exactly what :attr:`bounded` exists to surface — but *only* for an
    unfiltered read. See :attr:`filtered`.
    """

    target: Target
    rows: tuple[FlowRow, ...]
    active: Mapping[str, int]
    max_flows: int
    #: Whether an address filter was sent. Load-bearing for :attr:`bounded`:
    #: ``active.total_flows`` is the appliance's whole flow census and takes no
    #: notice of ``ip1``, so comparing it against a *filtered* row count
    #: compares two different populations.
    filtered: bool = False

    @property
    def reported_total(self) -> int:
        return _int(self.active.get("total_flows"))

    @property
    def bounded(self) -> bool:
        """True when ``maxFlows`` may have cut the answer short.

        Hitting the cap is the signal that always applies: return exactly the
        number asked for and there is no way to know whether the next flow
        existed.

        The census comparison applies **only to an unfiltered read** (#94).
        On a filtered one it was always true and always misleading: a search
        matching three flows on an appliance carrying nine hundred reported
        itself truncated and told the operator to re-run with a higher
        ``--max-flows``, which cannot change a server-side filtered result. The
        issue that surfaced this shows it firing on four appliances at once for
        a search that matched nothing at all.
        """
        if len(self.rows) >= self.max_flows:
            return True
        return not self.filtered and self.reported_total > len(self.rows)


def fetch_flows(
    ctx: Ctx,
    target: Target,
    *,
    max_flows: int = DEFAULT_MAX_FLOWS,
    ip: str | None = None,
    mask: int | None = None,
    ip_either: bool = True,
) -> FlowFetch:
    """One ``GET /flow`` against one appliance.

    ``nePk`` and ``maxFlows`` are always sent — both are required. When *ip*
    is given it goes out as ``ip1``/``mask1``, so the **server** matches the
    address. That is the whole of #59: no client-side flow filtering happens
    anywhere in this module.

    ``ip_either`` is *our* concept — "match the address at either end" — and
    it is **not** what the wire flag of nearly that name means. See
    :data:`IP_EITHER_FLAG_IS_DIRECTIONAL`.
    """
    params: dict[str, Any] = {"nePk": target.ne_pk, "maxFlows": max_flows}
    if ip:
        params["ip1"] = ip
        # Inverted on purpose, and sent as an explicit string rather than a
        # Python bool so the encoding is this module's decision and not a
        # library's (same reasoning as `reports/versions.py`'s `cached`).
        params["ipEitherFlag"] = "false" if ip_either else "true"
        if mask is not None:
            params["mask1"] = mask
    payload = ctx.client.get(FLOWS_ENDPOINT, params=params)
    body: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    raw_rows = body.get("flows")
    rows = (
        tuple(parse_row(row, target=target) for row in raw_rows if isinstance(row, Mapping))
        if isinstance(raw_rows, list)
        else ()
    )
    raw_active = body.get("active")
    active: Mapping[str, int] = (
        {str(k): _int(v) for k, v in raw_active.items()} if isinstance(raw_active, Mapping) else {}
    )
    return FlowFetch(
        target=target, rows=rows, active=active, max_flows=max_flows, filtered=bool(ip)
    )


def _fan_out_flows(
    ctx: Ctx,
    items: Sequence[Target],
    *,
    max_flows: int,
    ip: str | None,
    mask: int | None,
    concurrency: int,
    timeout: float | None,
) -> list[Outcome[Target, FlowFetch]]:
    def call(target: Target) -> FlowFetch:
        return fetch_flows(ctx, target, max_flows=max_flows, ip=ip, mask=mask)

    return fan_out(items, call, concurrency=concurrency, timeout=timeout)


# -- #58: the per-appliance x per-overlay matrix ------------------------------


@dataclasses.dataclass(frozen=True)
class ApplianceFlowCounts:
    """One matrix row. An unreachable appliance is still a row, with *error*
    set and no counts — never a missing row and never a failed report."""

    target: Target
    counts: Mapping[str, int]
    reported_total: int
    bounded: bool
    error: str = ""

    @property
    def reachable(self) -> bool:
        return not self.error

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclasses.dataclass(frozen=True)
class FlowsSummary:
    """Rows = appliances, columns = overlays, cells = active flow counts."""

    overlays: tuple[str, ...]
    rows: tuple[ApplianceFlowCounts, ...]
    max_flows: int

    @property
    def column_totals(self) -> dict[str, int]:
        return {name: sum(row.counts.get(name, 0) for row in self.rows) for name in self.overlays}

    @property
    def grand_total(self) -> int:
        return sum(row.total for row in self.rows)

    @property
    def bounded_appliances(self) -> tuple[str, ...]:
        return tuple(row.target.name for row in self.rows if row.bounded)

    @property
    def bounded(self) -> bool:
        """True when at least one cell is a ``maxFlows`` ceiling, not a total."""
        return bool(self.bounded_appliances)

    @property
    def unreachable(self) -> tuple[ApplianceFlowCounts, ...]:
        return tuple(row for row in self.rows if not row.reachable)

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_flows": self.max_flows,
            "overlays": list(self.overlays),
            "appliances": [
                {
                    "appliance": row.target.name,
                    "nePk": row.target.ne_pk,
                    "counts": {name: row.counts.get(name, 0) for name in self.overlays},
                    "total": row.total,
                    "reported_total": row.reported_total,
                    "bounded_by_max_flows": row.bounded,
                    "reachable": row.reachable,
                    "error": row.error,
                }
                for row in self.rows
            ],
            "column_totals": self.column_totals,
            "grand_total": self.grand_total,
            "bounded_by_max_flows": self.bounded,
            "bounded_appliances": list(self.bounded_appliances),
            "unreachable": [
                {"appliance": row.target.name, "nePk": row.target.ne_pk, "error": row.error}
                for row in self.unreachable
            ],
        }


def order_overlays(names: Iterable[str]) -> tuple[str, ...]:
    """Named overlays alphabetically, the built-in bucket last.

    A stable column order matters more than a clever one: the same fabric must
    render the same table twice in a row.
    """
    unique = set(names)
    built_in = PASSTHROUGH in unique
    ordered = sorted(unique - {PASSTHROUGH})
    return (*ordered, PASSTHROUGH) if built_in else tuple(ordered)


def _counts_for(fetch: FlowFetch) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in fetch.rows:
        counts[row.overlay] = counts.get(row.overlay, 0) + 1
    return counts


def build_flows_summary(
    ctx: Ctx,
    *,
    appliances: Sequence[str] | None = None,
    max_flows: int = DEFAULT_MAX_FLOWS,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
    no_cache: bool = False,
) -> FlowsSummary:
    """#58 — active flows per appliance per overlay, with totals.

    One request per appliance; the overlay breakdown comes from counting the
    rows it returned, for the reasons in the module docstring.
    """
    items = targets(ctx, appliances=appliances, no_cache=no_cache)
    outcomes = _fan_out_flows(
        ctx,
        items,
        max_flows=max_flows,
        ip=None,
        mask=None,
        concurrency=concurrency,
        timeout=timeout,
    )
    rows: list[ApplianceFlowCounts] = []
    seen: set[str] = set()
    for outcome in outcomes:
        if not outcome.done or outcome.value is None:
            rows.append(
                ApplianceFlowCounts(
                    target=outcome.item,
                    counts={},
                    reported_total=0,
                    bounded=False,
                    error=outcome.error or "no response",
                )
            )
            continue
        fetch = outcome.value
        counts = _counts_for(fetch)
        seen.update(counts)
        rows.append(
            ApplianceFlowCounts(
                target=fetch.target,
                counts=counts,
                reported_total=fetch.reported_total,
                bounded=fetch.bounded,
            )
        )
    log.debug(
        "flows_summary_built",
        appliances=len(rows),
        unreachable=sum(1 for r in rows if not r.reachable),
        overlays=len(seen),
    )
    return FlowsSummary(overlays=order_overlays(seen), rows=tuple(rows), max_flows=max_flows)


# -- #59: every flow touching one address, fabric-wide ------------------------


def parse_query_ip(text: str) -> tuple[str, int]:
    """``<ip>`` or ``<ip>/<prefix>`` -> ``(ip, mask)`` for ``ip1``/``mask1``.

    The host address is kept as written rather than masked to its network:
    the API takes address and mask as separate parameters, so narrowing the
    address here would discard information the server wants.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("expected an IP address, optionally with a /prefix")
    host, _, prefix = raw.partition("/")
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError as exc:
        raise ValueError(f"{raw!r} is not an IP address: {exc}") from exc
    width = address.max_prefixlen
    if not prefix:
        return str(address), width
    try:
        mask = int(prefix)
    except ValueError as exc:
        raise ValueError(f"{raw!r}: prefix length must be an integer") from exc
    if not 0 <= mask <= width:
        raise ValueError(f"{raw!r}: prefix length must be between 0 and {width}")
    return str(address), mask


@dataclasses.dataclass(frozen=True)
class FlowMatch:
    """One conversation, however many appliances reported it.

    ``observations`` holds every appliance's view, in fan-out (input) order,
    so ``observations[0]`` is deterministic between runs.
    """

    key: FlowKey
    observations: tuple[FlowRow, ...]

    @property
    def primary(self) -> FlowRow:
        """The first-observed view — the one the table renders.

        Direction is genuinely ambiguous once the two ends are collapsed, so
        the rendered ``src -> dst`` is *as first observed* rather than an
        invented canonical direction.
        """
        return self.observations[0]

    @property
    def appliances(self) -> tuple[str, ...]:
        """Every appliance that reported it — both ends attributed, not one
        silently dropped."""
        return tuple(dict.fromkeys(o.appliance for o in self.observations))

    @property
    def seen_on_both_ends(self) -> bool:
        return len(self.appliances) > 1

    def as_dict(self) -> dict[str, Any]:
        primary = self.primary
        payload = primary.as_dict()
        payload["appliances"] = list(self.appliances)
        payload["observations"] = [o.as_dict() for o in self.observations]
        return payload


@dataclasses.dataclass(frozen=True)
class FlowSearch:
    """#59's result: one fabric-wide table plus what it could not see."""

    query: str
    ip: str
    mask: int
    matches: tuple[FlowMatch, ...]
    searched: tuple[Target, ...]
    unreachable: tuple[tuple[Target, str], ...]
    bounded_appliances: tuple[str, ...]
    max_flows: int

    @property
    def match_count(self) -> int:
        return len(self.matches)

    @property
    def bounded(self) -> bool:
        """True when some appliance's answer hit ``maxFlows`` — the result set
        is then a floor, not a complete answer."""
        return bool(self.bounded_appliances)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "ip": self.ip,
            "mask": self.mask,
            "max_flows": self.max_flows,
            "match_count": self.match_count,
            "searched": [t.name for t in self.searched],
            "flows": [m.as_dict() for m in self.matches],
            "bounded_by_max_flows": self.bounded,
            "bounded_appliances": list(self.bounded_appliances),
            "unreachable": [
                {"appliance": t.name, "nePk": t.ne_pk, "error": err} for t, err in self.unreachable
            ],
        }


def _sort_key(match: FlowMatch) -> tuple[Any, ...]:
    """Group by appliance, then by source address numerically, then port.

    Numeric rather than lexicographic so ``10.2.9.4`` sorts before
    ``10.20.0.9`` the way an operator expects.
    """
    primary = match.primary
    return (
        primary.appliance,
        _address_sort_key(primary.source.ip),
        primary.source.port,
        _address_sort_key(primary.destination.ip),
        primary.destination.port,
        primary.protocol,
    )


def _address_sort_key(ip: str) -> tuple[int, int, str]:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return (2, 0, ip)
    return (address.version, int(address), ip)


def dedupe(rows: Iterable[FlowRow]) -> list[FlowMatch]:
    """Collapse per-appliance views of the same conversation.

    Identity is :attr:`FlowRow.key`; see the module docstring for what it does
    and does not include, and why. Insertion order is preserved so the caller
    controls which observation is primary.
    """
    grouped: dict[FlowKey, list[FlowRow]] = {}
    for row in rows:
        grouped.setdefault(row.key, []).append(row)
    return [FlowMatch(key=key, observations=tuple(obs)) for key, obs in grouped.items()]


def find_flows(
    ctx: Ctx,
    query: str,
    *,
    appliances: Sequence[str] | None = None,
    max_flows: int = DEFAULT_MAX_FLOWS,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
    no_cache: bool = False,
) -> FlowSearch:
    """#59 — every flow touching *query* anywhere in the fabric, deduped.

    Matching is done by the **server** via ``ipEitherFlag``; this function
    never filters rows on the address itself.
    """
    ip, mask = parse_query_ip(query)
    items = targets(ctx, appliances=appliances, no_cache=no_cache)
    outcomes = _fan_out_flows(
        ctx,
        items,
        max_flows=max_flows,
        ip=ip,
        mask=mask,
        concurrency=concurrency,
        timeout=timeout,
    )
    rows: list[FlowRow] = []
    bounded: list[str] = []
    for target, fetch in values(outcomes):
        if fetch is None:
            continue
        rows.extend(fetch.rows)
        if fetch.bounded:
            bounded.append(target.name)
    failures = tuple((o.item, o.error or "no response") for o in outcomes if not o.done)
    matches = sorted(dedupe(rows), key=_sort_key)
    log.debug(
        "flow_search_built",
        query=query,
        appliances=len(items),
        observations=len(rows),
        matches=len(matches),
        unreachable=len(failures),
    )
    return FlowSearch(
        query=query,
        ip=ip,
        mask=mask,
        matches=tuple(matches),
        searched=tuple(items),
        unreachable=failures,
        bounded_appliances=tuple(bounded),
        max_flows=max_flows,
    )
