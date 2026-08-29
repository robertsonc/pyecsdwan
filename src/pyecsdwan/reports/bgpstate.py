"""``show appliance <name> bgp``: BGP protocol state (#72).

Read-only, one GET per invocation. Nothing here touches the candidate store,
the journal, or the transaction engine.

**The API draws the CONFIG/STATE line itself**, which is why this module
exists at all rather than reusing the generic resource view: ``/bgp/state``
and ``/bgp/config/*`` are separate endpoints. Answering
``show appliance BR1-EC bgp summary`` out of the config object would label
configuration as protocol status — the exact conflation epic #70 exists to
remove, and #72's first guardrail.

Four things verified against ``specs/orchestrator-openapi-7.2.0.json`` that an
obvious implementation gets wrong:

* **``summary`` and ``neighbors`` are one call, not two.** Both arrive in a
  single ``/bgp/state`` response, so they share a fetch and a cost class.
  Modelling them as independent reads would double the cost of asking for
  both and imply they can disagree.

* **``neighborState`` is an object keyed ``"0"``, ``"1"``, … not an array,**
  and the schema documents exactly two keys, described as "state information
  of the first/second neighbor". That is a documentation artifact, not a
  limit: :func:`_neighbors` iterates *every* numeric key. Assuming an array,
  or a maximum of two, is the same class of mistake as epic #54's
  ``installed[0]``.

* **``neighborCount`` is authoritative** for how many peers exist. A row count
  that disagrees with it is reported as ``partial`` rather than silently
  trusted — a fan-out that returned nine of ten rows has not answered the
  question, and neither has a response that dropped a peer.

* **``bgp_state`` 0 and 1 are different answers, and neither is an error.**
  0 is "not enabled here", 1 is "enabled and down". Both are a successful
  ``ok``: an appliance that does not run BGP is not a failure to report BGP,
  and rendering it as one sends someone to debug a healthy device.

``bgp_state_str`` is typed ``integer`` in the schema while described as "String
representation of the bgp state" — type and description contradict, exactly
like ``ipEitherFlag`` in #59. It is carried through as whatever arrives and
never parsed, because neither reading has been observed live.

``GET /bgp/vrfs/{vrfId}/state`` is deliberately not used: its own summary says
"Delete specific/all segment BGP state", a read-shaped verb whose summary says
delete. Both readings are possible in this API, so the orchestrator form's
``vrfId`` parameter is used instead. Feeds #67.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import structlog

from pyecsdwan.contract import Ctx

log = structlog.get_logger(__name__)

#: GET; `nePk` is required, `cached` and `vrfId` optional.
BGP_STATE_PATH = "/bgp/state"
#: Appliance-proxy path holding configured neighbors, for the correlation
#: below. Configuration, deliberately kept separate from the state above.
BGP_CONFIG_NEIGHBOR = "bgp/config/neighbor"

#: `summary.bgp_state`, transcribed from the vendored schema's own enumeration.
#:
#: **These were wrong until #87, and wrong in the worst possible direction.**
#: The first version used the standard BGP *peer* finite-state-machine names —
#: idle / connect / opensent / openconfirm / established — which are what
#: "BGP state" means almost everywhere else. This field is not that. The
#: appliance schema describes it as "Overall state of the routerd & bgp
#: processes" and enumerates:
#:
#:     0 = Not Enabled, 1 = Down, 2 = Mgmt Stub Initializing,
#:     3 = Mgmt Stub Active, 4 = RTM Initializing, 5 = RTM Active,
#:     6 = RM Initializing, 7 = RM Active, 8 = NM Initializing,
#:     9 = Active, 10 = Unknown
#:
#: So an appliance reporting 6 was being told it was **"established"** when the
#: appliance meant "RM Initializing" — a confident wrong answer about whether
#: BGP is up, which is the one thing this view exists to answer. Codes 2
#: through 8 were all misreported; only 0, 1, 9 and 10 happened to line up.
#:
#: The names are the schema's, lower-cased and nothing else. Do not "improve"
#: them into peer-state vocabulary: the resemblance is the trap.
BGP_STATE_NAMES: dict[int, str] = {
    0: "not enabled",
    1: "down",
    2: "mgmt stub initializing",
    3: "mgmt stub active",
    4: "rtm initializing",
    5: "rtm active",
    6: "rm initializing",
    7: "rm active",
    8: "nm initializing",
    9: "active",
    10: "unknown",
}


@dataclasses.dataclass(frozen=True)
class BgpNeighbor:
    """One peer, from ``neighbor.neighborState[<n>]``."""

    peer_ip: str
    asn: int | None
    peer_state: int | None
    peer_state_str: str
    local_ip: str
    rtr_id: str
    rcvd_pfxs: int | None
    sent_pfxs: int | None
    rcvd_updates: int | None
    sent_updates: int | None
    time_established: int | None
    time_last_update: int | None
    peer_caps: str
    #: True when the peer appears in `/bgp/config/neighbor` but not in the
    #: state response. #72's first guardrail: *configured but not observed* is
    #: a real and interesting state, and must never be inferred to be
    #: established. Such a row carries no counters, because there are none.
    configured_only: bool = False

    def as_json(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self)}


@dataclasses.dataclass(frozen=True)
class BgpSummary:
    """The ``summary`` block."""

    bgp_state: int | None
    bgp_state_str: Any
    local_asn: int | None
    local_ip: str
    rtr_id: str
    num_peers: int | None
    num_peers_active: int | None
    num_bgp_rtes_rcvd: int | None
    num_ebgp_rtes: int | None
    num_ibgp_rtes: int | None
    num_rib_rtes: int | None
    num_subs_installed: int | None
    reject_mismatches: int | None
    reject_unpreferred: int | None
    mgmt_stub_tot_errors: int | None
    mgmt_stub_last_err_str: str
    tunbgp_tot_errors: int | None
    tunbgp_last_err_str: str

    @property
    def state_name(self) -> str:
        """A word for `bgp_state`, or the raw value when it is not one we know.

        Never guessed: an unrecognised code renders as the number rather than
        as the nearest name, because an operator acting on a wrong state word
        is worse off than one who has to look the number up.
        """
        if self.bgp_state is None:
            return "unknown"
        return BGP_STATE_NAMES.get(self.bgp_state, f"code {self.bgp_state}")

    @property
    def state_label(self) -> str:
        """The state as an operator should read it, composed in one place.

        Rendering used to be ``f"{state_name} ({bgp_state})"``, which prints
        "code 11 (11)" for a code the schema does not list — the number twice
        and no hint that the tool did not recognise it. A live appliance
        reported exactly that (#87): the vendored 7.2.0 enumeration stops at
        10, so 11 is a state this baseline has never heard of. Saying so is
        the answer; guessing the nearest name would not be.
        """
        if self.bgp_state is None:
            return "unknown (the summary block reported no state)"
        name = BGP_STATE_NAMES.get(self.bgp_state)
        if name is None:
            return f"unrecognised code {self.bgp_state} — not in the 7.2.0 schema"
        return f"{name} ({self.bgp_state})"

    @property
    def enabled(self) -> bool:
        """`bgp_state == 0` means BGP is not turned on here — an answer."""
        return self.bgp_state != 0

    def as_json(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "state_name": self.state_name}


@dataclasses.dataclass(frozen=True)
class BgpState:
    """One appliance's BGP state: both views, from one response."""

    appliance: str
    ne_pk: str
    summary: BgpSummary
    neighbors: tuple[BgpNeighbor, ...]
    #: What `neighbor.neighborCount` claimed. Kept alongside the rows rather
    #: than replaced by them, so a disagreement stays visible.
    neighbor_count: int | None
    #: True when the response was served from the Orchestrator's cache.
    cached: bool

    @property
    def observed_peers(self) -> int:
        """Peers the state response actually reported.

        Configured-only rows are excluded — they came from the config read, not
        from the appliance's live view, and counting them here would restate
        configuration as observation.
        """
        return sum(1 for n in self.neighbors if not n.configured_only)

    @property
    def observed_established(self) -> int:
        """Peers the appliance itself calls established.

        Read off ``peer_state_str`` — the appliance's own word — rather than
        inferred from a numeric code we would have to map, which is precisely
        the mapping that #87 found to be wrong.
        """
        return sum(
            1
            for n in self.neighbors
            if not n.configured_only and n.peer_state_str.strip().lower() == "established"
        )

    @property
    def summary_peer_counts_missing(self) -> bool:
        """The summary block carried neither peer count.

        A live appliance did exactly this (#87) and the report rendered "peers
        None active of None" beside a neighbours table listing two established
        sessions — a Python ``None`` leaking into operator output, next to a
        contradiction of it.
        """
        return self.summary.num_peers is None and self.summary.num_peers_active is None

    @property
    def summary_peer_counts_disagree(self) -> bool:
        """The summary's own counts contradict the peers it listed.

        Not an error to resolve by picking a side: both numbers came from the
        same response, and the report shows both rather than choosing which one
        the operator should have been told.
        """
        if self.summary_peer_counts_missing:
            return False
        claimed_total = self.summary.num_peers
        claimed_active = self.summary.num_peers_active
        if claimed_total is not None and claimed_total != self.observed_peers:
            return True
        return claimed_active is not None and claimed_active != self.observed_established

    @property
    def rows_match_count(self) -> bool:
        """Whether the rows account for every peer the response claimed.

        Configured-only rows are excluded: they were added from configuration
        and were never part of `neighborCount`, so counting them would mask
        exactly the mismatch this exists to surface.
        """
        if self.neighbor_count is None:
            return True
        observed = [n for n in self.neighbors if not n.configured_only]
        return len(observed) == self.neighbor_count

    def as_json(self) -> dict[str, Any]:
        return {
            "appliance": self.appliance,
            "nePk": self.ne_pk,
            "cached": self.cached,
            "summary": self.summary.as_json(),
            "neighborCount": self.neighbor_count,
            "neighbors": [n.as_json() for n in self.neighbors],
            "rows_match_count": self.rows_match_count,
        }


def _int(value: Any) -> int | None:
    """Coerce to int, or None. Never 0 on failure: a missing counter and a
    counter that is genuinely zero are different facts."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _summary(raw: Any) -> BgpSummary:
    block = raw if isinstance(raw, dict) else {}
    return BgpSummary(
        bgp_state=_int(block.get("bgp_state")),
        # Carried through untouched: the schema types it integer and describes
        # it as a string, and neither reading has been observed live.
        bgp_state_str=block.get("bgp_state_str"),
        local_asn=_int(block.get("local_asn")),
        local_ip=_str(block.get("local_ip")),
        rtr_id=_str(block.get("rtr_id")),
        num_peers=_int(block.get("num_peers")),
        num_peers_active=_int(block.get("num_peers_active")),
        num_bgp_rtes_rcvd=_int(block.get("num_bgp_rtes_rcvd")),
        num_ebgp_rtes=_int(block.get("num_ebgp_rtes")),
        num_ibgp_rtes=_int(block.get("num_ibgp_rtes")),
        num_rib_rtes=_int(block.get("num_rib_rtes")),
        num_subs_installed=_int(block.get("num_subs_installed")),
        reject_mismatches=_int(block.get("reject_mismatches")),
        reject_unpreferred=_int(block.get("reject_unpreferred")),
        mgmt_stub_tot_errors=_int(block.get("mgmt_stub_tot_errors")),
        mgmt_stub_last_err_str=_str(block.get("mgmt_stub_last_err_str")),
        tunbgp_tot_errors=_int(block.get("tunbgp_tot_errors")),
        tunbgp_last_err_str=_str(block.get("tunbgp_last_err_str")),
    )


def _neighbors(raw: Any) -> tuple[tuple[BgpNeighbor, ...], int | None]:
    """Peers and the claimed count, from the ``neighbor`` block.

    ``neighborState`` is keyed ``"0"``, ``"1"``, … — every numeric key is
    read, in numeric order, not just the two the schema happens to document.
    """
    block = raw if isinstance(raw, dict) else {}
    states = block.get("neighborState")
    entries: list[tuple[int, dict[str, Any]]] = []
    if isinstance(states, dict):
        for key, value in states.items():
            index = _int(key)
            if index is not None and isinstance(value, dict):
                entries.append((index, value))
    elif isinstance(states, list):
        # Not the documented shape, but a list would be the natural thing for
        # a future version to switch to, and dropping every peer on that day
        # would be a silent wrong answer rather than a visible break.
        entries = [(i, v) for i, v in enumerate(states) if isinstance(v, dict)]
    entries.sort(key=lambda pair: pair[0])

    peers = tuple(
        BgpNeighbor(
            peer_ip=_str(entry.get("peer_ip")),
            asn=_int(entry.get("asn")),
            peer_state=_int(entry.get("peer_state")),
            peer_state_str=_str(entry.get("peer_state_str")),
            local_ip=_str(entry.get("local_ip")),
            rtr_id=_str(entry.get("rtr_id")),
            rcvd_pfxs=_int(entry.get("rcvd_pfxs")),
            sent_pfxs=_int(entry.get("sent_pfxs")),
            rcvd_updates=_int(entry.get("rcvd_updates")),
            sent_updates=_int(entry.get("sent_updates")),
            time_established=_int(entry.get("time_established")),
            time_last_update=_int(entry.get("time_last_update")),
            peer_caps=_str(entry.get("peer_caps")),
        )
        for _index, entry in entries
    )
    return peers, _int(block.get("neighborCount"))


def _configured_peers(ctx: Ctx, ne_pk: str) -> set[str]:
    """Neighbor IPs from ``/bgp/config/neighbor``, for the correlation.

    A failure here costs the correlation, not the command: the state view is
    the answer being asked for, and losing an annotation is not worth failing
    a read that succeeded.
    """
    try:
        raw = ctx.client.appliance_request("GET", ne_pk, BGP_CONFIG_NEIGHBOR)
    except Exception as exc:  # noqa: BLE001 - annotation is best-effort; the state view is the answer
        log.debug("bgp_config_neighbor_unavailable", ne_pk=ne_pk, error=str(exc))
        return set()
    if not isinstance(raw, dict):
        return set()
    found: set[str] = set()
    for key, value in raw.items():
        if isinstance(value, dict):
            found.add(_str(value.get("self") or value.get("peer_ip") or key))
        else:
            found.add(_str(key))
    return {ip for ip in found if ip}


def collect(
    ctx: Ctx, appliance: str, *, cached: bool = False, vrf_id: str | None = None
) -> BgpState:
    """Both views, from one ``GET /bgp/state``.

    ``cached`` maps onto the endpoint's own ``cached`` parameter — the API
    distinguishes cached from live natively, so ``--stale-ok`` is honoured at
    the source rather than inferred here (Decision 7 / #72 finding 3).
    """
    ne_pk = ctx.resolver.ne_pk_for(appliance)
    params: dict[str, Any] = {"nePk": ne_pk, "cached": "true" if cached else "false"}
    if vrf_id is not None:
        params["vrfId"] = vrf_id
    raw = ctx.client.get(BGP_STATE_PATH, params=params)
    body = raw if isinstance(raw, dict) else {}

    peers, claimed = _neighbors(body.get("neighbor"))
    observed = {p.peer_ip for p in peers}
    extra = sorted(ip for ip in _configured_peers(ctx, ne_pk) if ip not in observed)
    configured_only = tuple(
        BgpNeighbor(
            peer_ip=ip,
            asn=None,
            peer_state=None,
            peer_state_str="configured, not observed",
            local_ip="",
            rtr_id="",
            rcvd_pfxs=None,
            sent_pfxs=None,
            rcvd_updates=None,
            sent_updates=None,
            time_established=None,
            time_last_update=None,
            peer_caps="",
            configured_only=True,
        )
        for ip in extra
    )
    return BgpState(
        appliance=appliance,
        ne_pk=ne_pk,
        summary=_summary(body.get("summary")),
        neighbors=peers + configured_only,
        neighbor_count=claimed,
        cached=cached,
    )
