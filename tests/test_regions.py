"""Tests for the regions slice (issue #35): network regions, per-appliance
region association, and regional overlay configuration.

Two things carry most of the weight here:

* ``match.overlayAcl`` is a JSON-encoded *string* inside an overlay config.
  Diffed opaquely it produces permanent phantom drift the moment the server
  re-serializes it with different key order or spacing, so
  ``test_overlay_acl_key_order_*`` round-trips the same ACL written three
  different ways and asserts the diff is empty.
* The regional write must not clobber neighbouring regions/overlays, so the
  e2e test seeds a second region *and* a second overlay and asserts both come
  through the write verbatim.

The e2e half mirrors tests/test_zones_e2e.py: candidate -> plan -> commit ->
replan -> rollback against the bundled mock.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref, Reversibility, Scope, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import ResolveError, Resolver
from pyecsdwan.resources.regions import (
    GLOBAL_REGION_ID,
    Region,
    RegionalOverlay,
    RegionAssociation,
    _encode_overlay_acl,
    _split_regional_ref,
    appliances_in_region,
    region_id_for,
)

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

REGION_REF = Ref(kind="region", name="APAC")
GLOBAL_REGION_REF = Ref(kind="region", name="global")
ASSOC_REF = Ref(kind="region-association", name="BR1-EC")  # nePk 3.NE
EMEA_OVERLAY_REF = Ref(kind="regional-overlay", name="CorpFabric@EMEA")
GLOBAL_OVERLAY_REF = Ref(kind="regional-overlay", name="CorpFabric@global")

#: The embedded ACL object, written three ways below. Values match the shape
#: captured live: {"data": {overlay: {"entry": {...}}}, "options": {...}}.
_ACL_OBJECT: dict[str, Any] = {
    "data": {"Overlay_RealTime": {"entry": {"1010": {"dscp": "ef", "prot": "ip", "self": True}}}},
    "options": {"merge": True, "templateApply": False},
}

#: Same object, every dict written in a different insertion order.
_ACL_OBJECT_SHUFFLED: dict[str, Any] = {
    "options": {"templateApply": False, "merge": True},
    "data": {"Overlay_RealTime": {"entry": {"1010": {"self": True, "prot": "ip", "dscp": "ef"}}}},
}


def _config_with_acl(acl: str) -> dict[str, Any]:
    return {
        "name": "RealTime",
        "id": 1,
        "topology": {"topologyType": 1, "hubs": ["0.NE"], "useRegions": False},
        "match": {"overlayAcl": acl},
        "bondingPolicy": 2,
    }


# -- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(
        orch_url=base_url,
        api_key="test-key",
        job_timeout=5.0,
        job_poll_initial=0.01,
        job_poll_max=0.02,
    )
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    candidate = CandidateStore(settings.origin)
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


def _rollback(world: dict[str, Any], n: int = 1) -> txn.CommitReport:
    return txn.rollback_history_txn(
        world["ctx"], default_registry, world["settings"], n=n
    )


def _seed_second_overlay(state: MockState) -> dict[str, Any]:
    """Add a sibling overlay (id 2) with its own regional config, so a write
    against overlay 1 can be proven not to disturb it. Returns a deep copy of
    the sibling's regional table for later comparison."""
    state.overlays["2"] = {"id": 2, "name": "GuestNet", "modifiedTime": 0}
    state.regional_overlays["2"] = {
        "0": {
            "name": "GuestNet",
            "id": 2,
            "topology": {"topologyType": 2, "hubs": ["1.NE"], "useRegions": False},
            "match": {"overlayAcl": json.dumps(_ACL_OBJECT_SHUFFLED)},
            "bondingPolicy": 4,
        }
    }
    return copy.deepcopy(state.regional_overlays["2"])


# =============================================================================
# 1. registration / plugin metadata
# =============================================================================


def test_kinds_registered_with_expected_contract() -> None:
    region = default_registry.get("region")
    assert region.scope is Scope.ORCHESTRATOR
    assert region.tier is Tier.CURATED
    # A delete cannot be undone exactly: re-creating gets a fresh regionId.
    assert region.reversibility is Reversibility.COMPENSABLE
    assert region.deletable is True

    assoc = default_registry.get("region-association")
    assert assoc.scope is Scope.ORCHESTRATOR
    assert assoc.reversibility is Reversibility.REVERSIBLE
    assert assoc.deletable is False
    assert assoc.dependencies == ("region",)

    regional = default_registry.get("regional-overlay")
    assert regional.scope is Scope.ORCHESTRATOR
    assert regional.tier is Tier.CURATED
    # No endpoint removes a single [overlayId][regionId] entry.
    assert regional.deletable is False
    # Both the overlay and the region must exist first.
    assert regional.dependencies == ("bio", "region")


def test_regional_overlay_applies_after_its_dependencies() -> None:
    refs = [EMEA_OVERLAY_REF, REGION_REF, Ref(kind="bio", name="CorpFabric")]
    ordered = [r.kind for r in default_registry.order_refs(refs)]
    assert ordered.index("regional-overlay") > ordered.index("region")
    assert ordered.index("regional-overlay") > ordered.index("bio")


# =============================================================================
# 2. normalize — regions
# =============================================================================


def test_region_normalize_strips_the_server_assigned_id() -> None:
    res = Region()
    once = res.normalize({"regionId": 7, "regionName": "EMEA"})
    assert once == {"regionName": "EMEA"}
    assert res.normalize(once) == once  # idempotent


def test_region_normalize_passes_unknown_fields_through() -> None:
    assert Region().normalize({"regionId": 3, "regionName": "APAC", "future": 1}) == {
        "regionName": "APAC",
        "future": 1,
    }


def test_region_normalize_absent_and_nameless() -> None:
    assert Region().normalize(None) is None
    with pytest.raises(ValueError, match="regionName"):
        Region().normalize({"regionId": 4})


def test_region_association_normalize_drops_the_denormalized_name() -> None:
    res = RegionAssociation()
    # regionName is a server-side join field: keeping it would make a region
    # rename look like drift on every appliance in it.
    once = res.normalize({"nePk": "3.NE", "regionId": 1, "regionName": "EMEA"})
    assert once == {"regionId": "1"}
    assert res.normalize(once) == once


def test_region_association_normalize_requires_a_region_id() -> None:
    with pytest.raises(ValueError, match="regionId"):
        RegionAssociation().normalize({"nePk": "3.NE"})


# =============================================================================
# 3. normalize — regional overlays, and the overlayAcl JSON-string hazard
# =============================================================================


def test_regional_overlay_normalize_strips_server_fields_and_is_idempotent() -> None:
    res = RegionalOverlay()
    raw = {
        **_config_with_acl(json.dumps(_ACL_OBJECT)),
        "regionId": 1,
        "modifiedTime": 1717171717,
    }
    once = res.normalize(raw)
    assert isinstance(once, dict)
    assert "id" not in once and "regionId" not in once and "modifiedTime" not in once
    assert once["name"] == "RealTime"
    # The embedded JSON is held parsed, not as an opaque string.
    assert once["match"]["overlayAcl"] == _ACL_OBJECT
    assert res.normalize(once) == once


def test_regional_overlay_normalize_does_not_mutate_its_input() -> None:
    raw = _config_with_acl(json.dumps(_ACL_OBJECT))
    before = copy.deepcopy(raw)
    RegionalOverlay().normalize(raw)
    assert raw == before


def test_overlay_acl_key_order_round_trips_to_an_empty_diff() -> None:
    """The acceptance-criterion test: the same ACL serialized with different
    key ordering must diff empty, or every plan reports phantom drift."""
    res = RegionalOverlay()
    server_side = json.dumps(_ACL_OBJECT)
    intent_side = json.dumps(_ACL_OBJECT_SHUFFLED)
    # Guard: the strings really are different, so an opaque comparison would
    # have reported a change here.
    assert server_side != intent_side

    current = res.normalize(_config_with_acl(server_side))
    desired = res.normalize(_config_with_acl(intent_side))
    diff = res.diff(EMEA_OVERLAY_REF, current, desired)
    assert diff.empty, [(e.op, e.path, e.old, e.new) for e in diff]


def test_overlay_acl_whitespace_round_trips_to_an_empty_diff() -> None:
    res = RegionalOverlay()
    pretty = json.dumps(_ACL_OBJECT, indent=2, sort_keys=True)
    compact = json.dumps(_ACL_OBJECT_SHUFFLED, separators=(",", ":"))
    assert pretty != compact
    current = res.normalize(_config_with_acl(pretty))
    desired = res.normalize(_config_with_acl(compact))
    assert res.diff(EMEA_OVERLAY_REF, current, desired).empty


def test_overlay_acl_real_change_is_still_detected() -> None:
    """The canonicalization must not flatten away an actual ACL change."""
    res = RegionalOverlay()
    changed = copy.deepcopy(_ACL_OBJECT)
    changed["data"]["Overlay_RealTime"]["entry"]["1010"]["dscp"] = "af41"
    current = res.normalize(_config_with_acl(json.dumps(_ACL_OBJECT)))
    desired = res.normalize(_config_with_acl(json.dumps(changed)))
    diff = res.diff(EMEA_OVERLAY_REF, current, desired)
    assert not diff.empty
    # The diff points at the ACL key that changed, not at one opaque blob.
    assert any(entry.path[-1] == "dscp" for entry in diff)


def test_non_json_overlay_acl_stays_opaque() -> None:
    once = RegionalOverlay().normalize(_config_with_acl("not-json-at-all"))
    assert isinstance(once, dict)
    assert once["match"]["overlayAcl"] == "not-json-at-all"
    assert RegionalOverlay().normalize(once) == once


def test_scalar_overlay_acl_stays_opaque() -> None:
    # "123" is valid JSON but a scalar; reinterpreting it as the number 123
    # would be a silent semantic change, so it is left as the string it was.
    once = RegionalOverlay().normalize(_config_with_acl("123"))
    assert isinstance(once, dict)
    assert once["match"]["overlayAcl"] == "123"


def test_encode_overlay_acl_restores_a_canonically_ordered_string() -> None:
    canonical = RegionalOverlay().normalize(_config_with_acl(json.dumps(_ACL_OBJECT_SHUFFLED)))
    assert isinstance(canonical, dict)
    body = _encode_overlay_acl(canonical)
    acl = body["match"]["overlayAcl"]
    assert isinstance(acl, str)
    # Deterministic: sorted keys, no whitespace — repeated writes of identical
    # intent produce byte-identical payloads.
    assert acl == json.dumps(_ACL_OBJECT, sort_keys=True, separators=(",", ":"))
    assert json.loads(acl) == _ACL_OBJECT
    # An already-encoded string is left alone.
    assert _encode_overlay_acl(body)["match"]["overlayAcl"] == acl


def test_encode_overlay_acl_leaves_a_config_without_a_match_block_alone() -> None:
    assert _encode_overlay_acl({"name": "X"}) == {"name": "X"}


# =============================================================================
# 4. ref-name parsing
# =============================================================================


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("RealTime@EMEA", ("RealTime", "EMEA")),
        ("RealTime@global", ("RealTime", "global")),
        ("RealTime@0", ("RealTime", "0")),
        ("  RealTime @ EMEA ", ("RealTime", "EMEA")),
        # rpartition: an '@' inside the overlay name still parses.
        ("a@b@EMEA", ("a@b", "EMEA")),
    ],
)
def test_split_regional_ref(name: str, expected: tuple[str, str]) -> None:
    assert _split_regional_ref(name) == expected


@pytest.mark.parametrize("name", ["RealTime", "@EMEA", "RealTime@", ""])
def test_split_regional_ref_rejects_unqualified_names(name: str) -> None:
    with pytest.raises(ValueError, match="<overlay>@<region>"):
        _split_regional_ref(name)


# =============================================================================
# 5. region addressing against the mock (regionId 0 handling)
# =============================================================================


def test_region_id_resolution_by_name_alias_and_id(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    assert region_id_for(ctx, "Default") == str(GLOBAL_REGION_ID)
    assert region_id_for(ctx, "global") == str(GLOBAL_REGION_ID)
    assert region_id_for(ctx, "GLOBAL") == str(GLOBAL_REGION_ID)
    assert region_id_for(ctx, "0") == str(GLOBAL_REGION_ID)
    assert region_id_for(ctx, "EMEA") == "1"
    with pytest.raises(ResolveError, match="unknown region"):
        region_id_for(ctx, "Atlantis")


def test_regional_overlay_fetch_addresses_region_zero_three_ways(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = RegionalOverlay()
    by_alias = res.fetch(ctx, GLOBAL_OVERLAY_REF)
    by_name = res.fetch(ctx, Ref(kind="regional-overlay", name="CorpFabric@Default"))
    by_id = res.fetch(ctx, Ref(kind="regional-overlay", name="CorpFabric@0"))
    assert by_alias == by_name == by_id
    assert isinstance(by_alias, dict)
    # Region 0's entry, not EMEA's (which the mock seeds with a different hub).
    assert by_alias["topology"]["hubs"] == ["1.NE"]
    assert res.fetch(ctx, EMEA_OVERLAY_REF)["topology"]["hubs"] == ["3.NE"]  # type: ignore[index]


def test_regional_overlay_fetch_absent_for_unknown_overlay_or_region(
    world: dict[str, Any],
) -> None:
    ctx = world["ctx"]
    res = RegionalOverlay()
    # Both may be created later in the same changeset (bio/region deps), so an
    # unresolvable ref reads as absent rather than raising.
    assert res.fetch(ctx, Ref(kind="regional-overlay", name="NoSuchOverlay@EMEA")) is None
    assert res.fetch(ctx, Ref(kind="regional-overlay", name="CorpFabric@Atlantis")) is None


def test_list_refs_enumerate_live_state(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    assert sorted(r.name for r in Region().list_refs(ctx)) == ["Default", "EMEA"]
    assert sorted(r.name for r in RegionAssociation().list_refs(ctx)) == [
        "BR1-EC",
        "BR2-EC",
        "HUB1-EC",
    ]
    assert sorted(r.name for r in RegionalOverlay().list_refs(ctx)) == [
        "CorpFabric@Default",
        "CorpFabric@EMEA",
    ]


def test_appliances_in_region_read_only_view(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    in_global = appliances_in_region(ctx, GLOBAL_REGION_ID)
    assert sorted(entry["nePk"] for entry in in_global) == ["1.NE", "3.NE", "5.NE"]
    assert appliances_in_region(ctx, 1) == []


# =============================================================================
# 6. e2e — regional overlay write is region-scoped and idempotent
# =============================================================================


def test_regional_overlay_write_does_not_clobber_other_regions_or_overlays(
    world: dict[str, Any],
) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]
    sibling_overlay = _seed_second_overlay(state)
    sibling_region = copy.deepcopy(state.regional_overlays["1"]["0"])

    candidate.set_path(EMEA_OVERLAY_REF, ["bondingPolicy"], 3)
    candidate.set_path(EMEA_OVERLAY_REF, ["topology", "useRegions"], True)
    report = _commit(world)
    assert report.ok, report.messages

    # The targeted entry changed...
    emea = state.regional_overlays["1"]["1"]
    assert emea["bondingPolicy"] == 3
    assert emea["topology"]["useRegions"] is True
    assert emea["topology"]["hubs"] == ["3.NE"]  # untouched fields survive the merge
    # ...and nothing else did: the global region of the same overlay, and a
    # different overlay entirely, came through the read-modify-write verbatim.
    assert state.regional_overlays["1"]["0"] == sibling_region
    assert state.regional_overlays["2"] == sibling_overlay


def test_regional_overlay_replan_is_empty_and_rollback_restores(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]
    before = copy.deepcopy(state.regional_overlays["1"]["1"])

    candidate.set_path(EMEA_OVERLAY_REF, ["bondingPolicy"], 3)
    assert _commit(world).ok
    assert state.regional_overlays["1"]["1"]["bondingPolicy"] == 3

    # Replaying identical intent is a no-op — including the overlayAcl, which
    # the server handed back re-serialized.
    candidate.set_path(EMEA_OVERLAY_REF, ["bondingPolicy"], 3)
    assert _plan_is_empty(world)

    restore = _rollback(world)
    assert restore.ok, restore.messages
    after = state.regional_overlays["1"]["1"]
    assert after["bondingPolicy"] == before["bondingPolicy"]
    assert json.loads(after["match"]["overlayAcl"]) == json.loads(before["match"]["overlayAcl"])


def test_written_overlay_acl_is_a_canonical_json_string(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]
    candidate.set_path(EMEA_OVERLAY_REF, ["bondingPolicy"], 5)
    assert _commit(world).ok

    acl = state.regional_overlays["1"]["1"]["match"]["overlayAcl"]
    # The server expects a string, and it is written canonically ordered.
    assert isinstance(acl, str)
    assert acl == json.dumps(json.loads(acl), sort_keys=True, separators=(",", ":"))
    # The overlay id stripped by normalize() is re-injected on write.
    assert state.regional_overlays["1"]["1"]["id"] == 1


def test_rollback_of_a_newly_created_regional_entry_refuses_loudly(
    world: dict[str, Any],
) -> None:
    # There is no per-entry delete endpoint; rollback must say so rather than
    # reach for the under-specified exhaustive POST.
    result = RegionalOverlay().rollback(world["ctx"], EMEA_OVERLAY_REF, None)
    assert result.ok is False
    assert "no endpoint" in result.message


# =============================================================================
# 7. e2e — regions and region associations
# =============================================================================


def test_region_create_replan_and_compensating_rollback(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]

    candidate.set_path(REGION_REF, ["regionName"], "APAC")
    report = _commit(world)
    assert report.ok, report.messages
    created = [r for r in state.regions if r["regionName"] == "APAC"]
    assert len(created) == 1
    assert created[0]["regionId"] == 2  # server-allocated, not requested

    # Replaying the same intent is a no-op.
    candidate.set_path(REGION_REF, ["regionName"], "APAC")
    assert _plan_is_empty(world)

    restore = _rollback(world)
    assert restore.ok, restore.messages
    assert [r["regionName"] for r in state.regions] == ["Default", "EMEA"]


def test_region_name_is_the_identity_so_a_rename_is_not_expressible(
    world: dict[str, Any],
) -> None:
    ctx = world["ctx"]
    ref = Ref(kind="region", name="EMEA")
    # An attempt to rename through the desired state is pinned back to the
    # ref's own name, so the plan stays empty instead of silently renaming.
    canonical = Region().canonicalize_desired(ctx, ref, {"regionName": "EMEA-West"})
    assert canonical == {"regionName": "EMEA"}


def test_addressing_a_nonexistent_region_by_id_is_refused(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    with pytest.raises(ValueError, match="allocated by the server"):
        Region().canonicalize_desired(ctx, Ref(kind="region", name="99"), {})


def test_global_region_delete_is_refused(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Region()
    current = res.normalize(res.fetch(ctx, GLOBAL_REGION_REF))
    assert current == {"regionName": "Default"}
    result = res.apply(ctx, res.diff(GLOBAL_REGION_REF, current, None))
    assert result.ok is False
    assert f"regionId {GLOBAL_REGION_ID}" in result.message
    assert [r["regionName"] for r in world["state"].regions] == ["Default", "EMEA"]


def test_region_delete_and_rollback_reports_the_new_id(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]

    candidate.delete(Ref(kind="region", name="EMEA"))
    assert _commit(world).ok
    assert [r["regionName"] for r in state.regions] == ["Default"]

    restore = _rollback(world)
    assert restore.ok, restore.messages
    recreated = [r for r in state.regions if r["regionName"] == "EMEA"]
    assert len(recreated) == 1
    # COMPENSABLE, not REVERSIBLE: the id is server-allocated afresh.
    assert recreated[0]["regionId"] != 1


def test_region_rollback_after_a_delete_warns_that_the_id_is_new(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    state.regions = [r for r in state.regions if r["regionName"] != "EMEA"]
    result = Region().rollback(
        world["ctx"], Ref(kind="region", name="EMEA"), {"regionId": 1, "regionName": "EMEA"}
    )
    assert result.ok
    # The operator is told plainly that references to the old id are not
    # restored with the region, rather than being left to discover it.
    assert "NEW server-allocated regionId" in result.message
    assert "must be re-applied" in result.message


def test_region_association_move_replan_and_restore(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]
    assert state.region_appliances["3.NE"] == 0

    candidate.set_path(ASSOC_REF, ["region"], "EMEA")
    report = _commit(world)
    assert report.ok, report.messages
    assert state.region_appliances["3.NE"] == 1
    # Only the named appliance moved.
    assert state.region_appliances["1.NE"] == 0
    assert state.region_appliances["5.NE"] == 0

    candidate.set_path(ASSOC_REF, ["region"], "EMEA")
    assert _plan_is_empty(world)

    restore = _rollback(world)
    assert restore.ok, restore.messages
    assert state.region_appliances["3.NE"] == 0


def test_region_association_accepts_the_global_alias(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]
    state.region_appliances["3.NE"] = 1

    candidate.set_path(ASSOC_REF, ["region"], "global")
    assert _commit(world).ok
    assert state.region_appliances["3.NE"] == 0


def test_region_association_requires_a_region(world: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="requires 'region'"):
        RegionAssociation().canonicalize_desired(world["ctx"], ASSOC_REF, {})


def test_region_association_rollback_without_a_snapshot_refuses(world: dict[str, Any]) -> None:
    result = RegionAssociation().rollback(world["ctx"], ASSOC_REF, None)
    assert result.ok is False
    assert "refusing to guess" in result.message


# =============================================================================
# 8. optional live smoke test (read-only; never run in CI)
# =============================================================================


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ECSDWAN_ORCH_URL"),
    reason="live Orchestrator smoke test; set ECSDWAN_ORCH_URL (and auth) to run",
)
def test_live_regions_read_only() -> None:
    """Read-only probe of the two confirmed endpoints against a real
    Orchestrator. Credentials come from the ambient config/keyring — never
    hardcoded here."""
    settings = config.settings_from_env()
    ctx = Ctx(client=OrchClient(settings), resolver=Resolver(OrchClient(settings)))

    regions = Region().list_refs(ctx)
    assert regions, "every fabric has at least the global region"
    assert region_id_for(ctx, "global") == str(GLOBAL_REGION_ID)

    res = RegionalOverlay()
    for ref in res.list_refs(ctx):
        canonical = res.normalize(res.fetch(ctx, ref))
        # The promotion checklist's idempotency proof, on real payloads.
        assert res.normalize(canonical) == canonical
