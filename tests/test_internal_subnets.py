"""Tests for the internal-subnets resource (Phase-3 #37).

Covers the acceptance criteria: idempotent normalize() with numeric CIDR
ordering and deduplication, malformed/wrong-family rejection, unknown-key
passthrough, the full-object replace write path, and an end-to-end
candidate -> plan -> commit -> rollback round trip against the bundled mock.

Issue #37's other half (per-appliance subnet-sharing options over
POST /subnets/setSubnetSharingOptions) is deliberately NOT implemented and so
has no tests here: the endpoint is write-only, so there is no fetch(), no
snapshot and no rollback to test. See the module docstring of
``pyecsdwan/resources/internal_subnets.py`` for the evidence.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Diff, DiffEntry, DiffOp, Ref, Reversibility, Scope, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.internal_subnets import InternalSubnets

BASE = "https://orch.example.com/gms/rest"
URL = f"{BASE}/gms/internalSubnets2"
REF = Ref(kind="internal-subnets", name="global")

#: The confirmed live payload (see the resource's module docstring).
LIVE = {
    "ipv4": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "224.0.0.0/4"],
    "ipv6": ["fe80::/10", "ff00::/8", "fc00::/7"],
    "segmentIpv4": [],
    "segmentIpv6": [],
    "segmentedIpv6Enabled": True,
    "nonDefaultRoutes": False,
}

EMPTY = {
    "ipv4": [],
    "ipv6": [],
    "segmentIpv4": [],
    "segmentIpv6": [],
    "nonDefaultRoutes": False,
}


def _ctx(settings):
    return Ctx(client=OrchClient(settings), resolver=None)


# -- declared contract --------------------------------------------------------


def test_resource_is_a_curated_reversible_orchestrator_singleton():
    res = InternalSubnets()
    assert res.kind == "internal-subnets"
    assert res.scope is Scope.ORCHESTRATOR
    assert res.reversibility is Reversibility.REVERSIBLE
    assert res.tier is Tier.CURATED
    assert res.deletable is False
    assert default_registry.get("internal-subnets") is not None


# -- normalize ----------------------------------------------------------------


def test_normalize_none_yields_the_empty_table():
    assert InternalSubnets().normalize(None) == EMPTY


def test_normalize_is_idempotent_on_the_live_payload():
    res = InternalSubnets()
    once = res.normalize(LIVE)
    assert res.normalize(once) == once
    # The live payload is already canonical apart from ordering.
    assert once["ipv4"] == [
        "10.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "224.0.0.0/4",
    ]
    assert once["ipv6"] == ["fc00::/7", "fe80::/10", "ff00::/8"]


def test_cidr_lists_sort_numerically_not_lexicographically():
    once = InternalSubnets().normalize({"ipv4": ["10.0.0.0/8", "9.0.0.0/8", "172.16.0.0/12"]})
    assert once["ipv4"] == ["9.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12"]
    # Lexicographic ordering would have put 10.0.0.0/8 first.
    assert once["ipv4"] != sorted(once["ipv4"])


def test_more_specific_prefix_sorts_after_its_supernet():
    once = InternalSubnets().normalize({"ipv4": ["10.1.0.0/16", "10.0.0.0/8"]})
    assert once["ipv4"] == ["10.0.0.0/8", "10.1.0.0/16"]


def test_normalize_deduplicates_and_canonicalizes_spelling():
    once = InternalSubnets().normalize(
        {
            "ipv4": ["10.0.0.0/8", " 10.0.0.0/8 ", "10.1.2.3/8"],
            "ipv6": ["FE80::/10", "fe80:0000::/10"],
        }
    )
    # Host bits are masked to the network address (strict=False), whitespace
    # trimmed, and IPv6 rendered compressed+lowercase — so all three ipv4
    # spellings and both ipv6 spellings collapse to one entry each.
    assert once["ipv4"] == ["10.0.0.0/8"]
    assert once["ipv6"] == ["fe80::/10"]
    assert InternalSubnets().normalize(once) == once


def test_segment_lists_are_parsed_sorted_and_deduplicated():
    once = InternalSubnets().normalize(
        {
            "segmentIpv4": ["2:10.0.0.0/8", "1:192.168.0.0/16", "1:10.0.0.0/8", "2:10.0.0.0/8"],
            "segmentIpv6": ["1:FE80::/10", "0:fc00::/7"],
        }
    )
    # Ordered by (segment id, then network) — the id comes first so a segment's
    # prefixes stay grouped.
    assert once["segmentIpv4"] == ["1:10.0.0.0/8", "1:192.168.0.0/16", "2:10.0.0.0/8"]
    assert once["segmentIpv6"] == ["0:fc00::/7", "1:fe80::/10"]
    assert InternalSubnets().normalize(once) == once


def test_normalize_coerces_non_default_routes_to_bool():
    assert InternalSubnets().normalize({"nonDefaultRoutes": 1})["nonDefaultRoutes"] is True
    assert InternalSubnets().normalize({})["nonDefaultRoutes"] is False


# -- normalize: rejection -----------------------------------------------------


def test_malformed_cidr_is_rejected_naming_the_bad_value():
    with pytest.raises(ValueError, match=r"ipv4\[1\].*'10\.0\.0\.0/33'.*not a valid CIDR"):
        InternalSubnets().normalize({"ipv4": ["10.0.0.0/8", "10.0.0.0/33"]})


def test_non_string_cidr_entry_is_rejected():
    with pytest.raises(ValueError, match=r"ipv4\[0\] must be a CIDR string"):
        InternalSubnets().normalize({"ipv4": [42]})


def test_wrong_address_family_is_rejected():
    with pytest.raises(ValueError, match=r"ipv4\[0\].*IPv6 network but 'ipv4' holds IPv4"):
        InternalSubnets().normalize({"ipv4": ["fe80::/10"]})
    with pytest.raises(ValueError, match=r"ipv6\[0\].*IPv4 network but 'ipv6' holds IPv6"):
        InternalSubnets().normalize({"ipv6": ["10.0.0.0/8"]})


def test_a_bare_string_is_not_a_cidr_list():
    with pytest.raises(ValueError, match=r"'ipv4' must be a list of CIDR strings"):
        InternalSubnets().normalize({"ipv4": "10.0.0.0/8"})


def test_segment_entry_without_a_segment_prefix_is_rejected():
    with pytest.raises(ValueError, match=r"segmentIpv4\[0\].*missing the segment prefix"):
        InternalSubnets().normalize({"segmentIpv4": ["192.168.0.0/16"]})


def test_segment_entry_with_a_non_numeric_id_is_rejected():
    with pytest.raises(ValueError, match=r"segmentIpv4\[0\]: 'red' is not a numeric VRF"):
        InternalSubnets().normalize({"segmentIpv4": ["red:192.168.0.0/16"]})


def test_segment_ipv6_splits_on_the_first_colon_only():
    # "1:fe80::/10" must parse as segment 1 + fe80::/10, not choke on the
    # address's own colons.
    once = InternalSubnets().normalize({"segmentIpv6": ["1:fe80::/10"]})
    assert once["segmentIpv6"] == ["1:fe80::/10"]


def test_non_mapping_raw_is_rejected():
    with pytest.raises(ValueError, match="must be a mapping"):
        InternalSubnets().normalize(["10.0.0.0/8"])


# -- unknown-key passthrough --------------------------------------------------


def test_unknown_top_level_keys_pass_through_untouched():
    raw = {
        "ipv4": ["10.0.0.0/8"],
        "segmentedIpv6Enabled": True,
        "someFutureKey": {"nested": [1, 2, 3]},
    }
    once = InternalSubnets().normalize(raw)
    assert once["segmentedIpv6Enabled"] is True
    assert once["someFutureKey"] == {"nested": [1, 2, 3]}
    assert InternalSubnets().normalize(once) == once


# -- write path ---------------------------------------------------------------


@respx.mock
def test_fetch_and_apply_replace_the_whole_object(settings):
    res = InternalSubnets()
    respx.get(URL).mock(return_value=httpx.Response(200, json=LIVE))
    posted: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        posted.update(json.loads(request.content))
        return httpx.Response(204)

    respx.post(URL).mock(side_effect=_capture)

    ctx = _ctx(settings)
    current = res.normalize(res.fetch(ctx, REF))
    desired = dict(current)
    desired["nonDefaultRoutes"] = True
    result = res.apply(ctx, res.diff(REF, current, desired))

    assert result.ok and result.changed
    assert posted["nonDefaultRoutes"] is True
    # The untouched keys — including the live-only, spec-absent one — are all
    # re-sent, so the full-object POST drops nothing.
    assert posted["ipv4"] == current["ipv4"]
    assert posted["ipv6"] == current["ipv6"]
    assert posted["segmentedIpv6Enabled"] is True


@respx.mock
def test_apply_is_a_noop_on_an_empty_diff(settings):
    res = InternalSubnets()
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    once = res.normalize(LIVE)
    result = res.apply(_ctx(settings), res.diff(REF, once, once))
    assert result.ok and not result.changed
    assert not route.called


@respx.mock
def test_rollback_restores_the_snapshot(settings):
    posted: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        posted.update(json.loads(request.content))
        return httpx.Response(204)

    respx.post(URL).mock(side_effect=_capture)
    result = InternalSubnets().rollback(_ctx(settings), REF, LIVE)
    assert result.ok
    assert posted == InternalSubnets().normalize(LIVE)


@respx.mock
def test_rollback_refuses_an_absent_snapshot(settings):
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    result = InternalSubnets().rollback(_ctx(settings), REF, None)
    assert not result.ok
    assert "refusing to POST an empty internal-subnet table" in result.message
    assert not route.called


@respx.mock
def test_apply_refuses_a_missing_desired_table(settings):
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    diff = Diff(
        ref=REF,
        entries=[DiffEntry(op=DiffOp.REMOVE, path=(), old=LIVE, new=None)],
        desired=None,
    )
    result = InternalSubnets().apply(_ctx(settings), diff)
    assert not result.ok
    assert not route.called


# -- end to end against the mock ----------------------------------------------

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread  # noqa: E402


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key", job_timeout=5.0)
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    candidate = CandidateStore(settings.host)
    return {"ctx": ctx, "settings": settings, "candidate": candidate, "state": state}


def _commit(world: dict[str, Any]) -> txn.CommitReport:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])
    if report.ok:
        world["candidate"].clear()
    return report


def _plan_is_empty(world: dict[str, Any]) -> bool:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    world["candidate"].clear()
    return plan.empty


def test_internal_subnets_round_trip_idempotency_and_rollback(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]

    # Replace the ipv4 list and flip nonDefaultRoutes in one change.
    candidate.set_path(REF, ["ipv4"], ["172.16.0.0/12", "10.0.0.0/8", "9.0.0.0/8"])
    candidate.set_path(REF, ["nonDefaultRoutes"], True)
    report = _commit(world)
    assert report.ok, report.messages
    # Stored sorted by parsed network, not lexicographically.
    assert state.internal_subnets["ipv4"] == ["9.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12"]
    assert state.internal_subnets["nonDefaultRoutes"] is True
    # Untouched keys survived the full-object replace, including the live-only
    # key the spec does not declare.
    assert state.internal_subnets["ipv6"] == ["fc00::/7", "fe80::/10", "ff00::/8"]
    assert state.internal_subnets["segmentedIpv6Enabled"] is True

    # Re-staging the same intent (in a different order, with a duplicate and a
    # host-bits spelling) diffs empty: normalize() canonicalizes both sides.
    candidate.set_path(REF, ["ipv4"], ["10.0.0.0/8", "9.0.0.1/8", "172.16.0.0/12", "10.0.0.0/8"])
    assert _plan_is_empty(world)

    # Add a segmented prefix, then roll the transaction back.
    candidate.set_path(REF, ["segmentIpv4"], ["1:192.168.0.0/16"])
    assert _commit(world).ok
    assert state.internal_subnets["segmentIpv4"] == ["1:192.168.0.0/16"]

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.internal_subnets["segmentIpv4"] == []
    assert state.internal_subnets["ipv4"] == ["9.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12"]
    assert state.internal_subnets["nonDefaultRoutes"] is True


def test_whole_resource_delete_is_refused(world: dict[str, Any]) -> None:
    # Singleton: there is no "no internal subnets" state to delete into.
    candidate: CandidateStore = world["candidate"]
    candidate.delete(REF)
    with pytest.raises(txn.CommitError, match="singleton"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])


# -- optional live smoke test (read-only) -------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ECSDWAN_ORCH_URL") or not os.environ.get("ECSDWAN_API_KEY"),
    reason="needs ECSDWAN_ORCH_URL + ECSDWAN_API_KEY against a real Orchestrator",
)
def test_live_internal_subnets_read_only_normalizes_idempotently() -> None:
    """Read-only: GET the real table and prove normalize() is a fixed point.

    Never writes. Run with ``pytest -m live``.
    """
    res = InternalSubnets()
    ctx = Ctx(client=OrchClient(config.settings_from_env()), resolver=None)
    raw = res.fetch(ctx, REF)
    once = res.normalize(raw)
    assert isinstance(once, dict)
    assert res.normalize(once) == once
    # A no-op replan against live state must diff empty.
    assert res.diff(REF, once, res.normalize(raw)).empty
