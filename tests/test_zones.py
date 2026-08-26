"""Unit tests for the zones resource (Phase-3 #30: orchestrator firewall zones).

Covers the acceptance criteria: idempotent zone-id-keyed normalize(),
/zones/nextId-driven allocation, and deleteDependencies surfaced on writes.
"""

import json

import httpx
import pytest
import respx

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref
from pyecsdwan.registry import default_registry
from pyecsdwan.resources.zones import Zones, appliance_zone_lists, segment_zone_map

BASE = "https://orch.example.com/gms/rest"
ZONES_URL = f"{BASE}/zones"
NEXT_ID_URL = f"{BASE}/zones/nextId"
EE_URL = f"{BASE}/zones/eeEnable"
VRF_MAP_URL = f"{BASE}/zones/vrfZonesMap"
ZONE_META_URL = f"{BASE}/appliance/zoneListMeta"
REF = Ref(kind="zones", name="global")


def _ctx(settings):
    return Ctx(client=OrchClient(settings), resolver=None)


# -- normalize: idempotent, zone-id keyed -------------------------------------


def test_normalize_none_yields_default_only_table():
    assert Zones().normalize(None) == {
        "zones": {"0": {"name": "Default"}},
        "endToEnd": False,
    }


def test_normalize_is_idempotent_and_zone_id_keyed():
    res = Zones()
    raw = {
        "zones": {7: {"name": "IoT", "vendorTag": "x"}, "02": {"name": "Guest"}},
        "eeEnable": {"enable": 1},
        "nextId": 9,
    }
    once = res.normalize(raw)
    assert once == {
        "zones": {
            "0": {"name": "Default"},
            "2": {"name": "Guest"},
            "7": {"name": "IoT", "vendorTag": "x"},
        },
        "endToEnd": True,
    }
    assert res.normalize(once) == once


def test_normalize_strips_next_id_and_sorts_ids_numerically():
    once = Zones().normalize(
        {"zones": {"10": {"name": "ten"}, "9": {"name": "nine"}}, "nextId": 11}
    )
    assert list(once["zones"]) == ["0", "9", "10"]  # numeric, not lexicographic
    assert "nextId" not in once


def test_normalize_rejects_placeholder_zone_keys():
    # Placeholders are resolved by canonicalize_desired(); raw canonical state
    # must only ever carry real numeric ids.
    with pytest.raises(ValueError, match="placeholder"):
        Zones().normalize({"zones": {"guest": {"name": "Guest"}}})


def test_normalize_requires_zone_name():
    with pytest.raises(ValueError, match="name"):
        Zones().normalize({"zones": {"3": {}}})


def test_default_zone_injected_on_both_sides_no_phantom_drift():
    res = Zones()
    server = {
        "zones": {"0": {"name": "Default"}, "5": {"name": "Guest"}},
        "eeEnable": {"enable": False},
    }
    user = {"zones": {"5": {"name": "Guest"}}}  # user file omits the Default zone
    assert res.diff(REF, res.normalize(server), res.normalize(user)).empty


# -- canonicalize_desired: /zones/nextId allocation ----------------------------


@respx.mock
def test_canonicalize_allocates_new_ids_from_next_id(settings):
    route = respx.get(NEXT_ID_URL).mock(return_value=httpx.Response(200, json={"nextId": 5}))
    desired = {
        "zones": {
            "0": {"name": "Default"},
            "guest": {"name": "Guest"},
            "iot": {"name": "IoT"},
        },
        "endToEnd": False,
    }
    canonical = Zones().canonicalize_desired(_ctx(settings), REF, desired)
    assert canonical["zones"] == {
        "0": {"name": "Default"},
        "5": {"name": "Guest"},  # placeholders resolve in sorted order
        "6": {"name": "IoT"},
    }
    assert route.call_count == 1  # one allocator read covers every new zone


@respx.mock
def test_canonicalize_reuses_id_when_name_already_staged(settings):
    route = respx.get(NEXT_ID_URL).mock(return_value=httpx.Response(200, json={"nextId": 9}))
    desired = {
        "zones": {
            "0": {"name": "Default"},
            "5": {"name": "Guest"},  # merge-mode intent inherits the server table
            "guest": {"name": "Guest"},
        },
    }
    canonical = Zones().canonicalize_desired(_ctx(settings), REF, desired)
    assert canonical["zones"] == {"0": {"name": "Default"}, "5": {"name": "Guest"}}
    assert route.call_count == 0  # nothing to allocate — replan is a no-op


@respx.mock
def test_canonicalize_allocation_skips_ids_already_staged(settings):
    respx.get(NEXT_ID_URL).mock(return_value=httpx.Response(200, json={"nextId": 1}))
    desired = {"zones": {"1": {"name": "MPLS"}, "guest": {"name": "Guest"}}}
    canonical = Zones().canonicalize_desired(_ctx(settings), REF, desired)
    # A stale allocator (nextId already staged) must not hand out a duplicate.
    assert canonical["zones"]["2"] == {"name": "Guest"}


def test_canonicalize_requires_name_for_placeholder(settings):
    with pytest.raises(ValueError, match="name"):
        Zones().canonicalize_desired(_ctx(settings), REF, {"zones": {"new1": {}}})


# -- fetch ---------------------------------------------------------------------


@respx.mock
def test_fetch_composes_table_ee_flag_and_next_id(settings):
    zones_route = respx.get(ZONES_URL).mock(
        return_value=httpx.Response(200, json={"0": {"name": "Default"}})
    )
    respx.get(EE_URL).mock(return_value=httpx.Response(200, json={"enable": True}))
    respx.get(NEXT_ID_URL).mock(return_value=httpx.Response(200, json={"nextId": 4}))

    raw = Zones().fetch(_ctx(settings), REF)
    assert raw == {
        "zones": {"0": {"name": "Default"}},
        "eeEnable": {"enable": True},
        "nextId": 4,
    }
    # The write path replaces the unique-names table, so fetch must read the
    # same view (not the per-segment allVRFZones=true expansion).
    assert zones_route.calls.last.request.url.params["allVRFZones"] == "false"


# -- apply: deleteDependencies surfaced, allocator advanced --------------------


@respx.mock
def test_apply_removal_posts_delete_dependencies_true(settings):
    zones_route = respx.post(ZONES_URL).mock(return_value=httpx.Response(204))
    res = Zones()
    current = res.normalize({"zones": {"0": {"name": "Default"}, "5": {"name": "Guest"}}})
    desired = res.normalize({"zones": {"0": {"name": "Default"}}})

    result = res.apply(_ctx(settings), res.diff(REF, current, desired))
    assert result.ok
    request = zones_route.calls.last.request
    assert request.url.params["deleteDependencies"] == "true"
    assert json.loads(request.content) == {"0": {"name": "Default"}}
    # Surfaced to the operator: which ids were removed and that they cascade.
    assert "deleteDependencies=true" in result.message
    assert "5" in result.message


@respx.mock
def test_apply_add_only_uses_delete_dependencies_false_and_advances_next_id(settings):
    zones_route = respx.post(ZONES_URL).mock(return_value=httpx.Response(204))
    next_get = respx.get(NEXT_ID_URL).mock(
        return_value=httpx.Response(200, json={"nextId": 5})
    )
    next_post = respx.post(NEXT_ID_URL).mock(return_value=httpx.Response(204))
    res = Zones()
    current = res.normalize({"zones": {"0": {"name": "Default"}}})
    desired = res.normalize({"zones": {"0": {"name": "Default"}, "5": {"name": "Guest"}}})

    result = res.apply(_ctx(settings), res.diff(REF, current, desired))
    assert result.ok
    assert zones_route.calls.last.request.url.params["deleteDependencies"] == "false"
    assert "deleteDependencies=false" in result.message
    assert next_get.call_count == 1
    assert json.loads(next_post.calls.last.request.content) == {"nextId": 6}
    assert "nextId advanced to 6" in result.message


@respx.mock
def test_apply_never_rewinds_a_further_advanced_allocator(settings):
    respx.post(ZONES_URL).mock(return_value=httpx.Response(204))
    respx.get(NEXT_ID_URL).mock(return_value=httpx.Response(200, json={"nextId": 40}))
    next_post = respx.post(NEXT_ID_URL).mock(return_value=httpx.Response(204))
    res = Zones()
    current = res.normalize({"zones": {"0": {"name": "Default"}}})
    desired = res.normalize({"zones": {"0": {"name": "Default"}, "5": {"name": "Guest"}}})

    assert res.apply(_ctx(settings), res.diff(REF, current, desired)).ok
    assert next_post.call_count == 0  # 40 > 5: posting 6 would re-open ids


@respx.mock
def test_apply_ee_only_change_skips_zone_table_post(settings):
    zones_route = respx.post(ZONES_URL).mock(return_value=httpx.Response(204))
    ee_route = respx.post(EE_URL).mock(return_value=httpx.Response(204))
    res = Zones()
    current = res.normalize({"zones": {"0": {"name": "Default"}}, "eeEnable": {"enable": False}})
    desired = res.normalize({"zones": {"0": {"name": "Default"}}, "endToEnd": True})

    result = res.apply(_ctx(settings), res.diff(REF, current, desired))
    assert result.ok
    assert zones_route.call_count == 0
    assert json.loads(ee_route.calls.last.request.content) == {"enable": True}
    assert "end-to-end ZBFW enabled" in result.message


@respx.mock
def test_apply_is_noop_on_empty_diff(settings):
    zones_route = respx.post(ZONES_URL).mock(return_value=httpx.Response(204))
    res = Zones()
    state = res.normalize({"zones": {"0": {"name": "Default"}}})

    result = res.apply(_ctx(settings), res.diff(REF, state, state))
    assert result.ok
    assert result.changed is False
    assert zones_route.call_count == 0  # no API call for an empty diff


# -- rollback -------------------------------------------------------------------


@respx.mock
def test_rollback_restores_snapshot_with_delete_dependencies_true(settings):
    zones_route = respx.post(ZONES_URL).mock(return_value=httpx.Response(204))
    ee_route = respx.post(EE_URL).mock(return_value=httpx.Response(204))
    next_post = respx.post(NEXT_ID_URL).mock(return_value=httpx.Response(204))
    snapshot = {
        "zones": {"0": {"name": "Default"}, "3": {"name": "Guest"}},
        "eeEnable": {"enable": True},
        "nextId": 4,
    }

    result = Zones().rollback(_ctx(settings), REF, snapshot)
    assert result.ok
    request = zones_route.calls.last.request
    # Zones added by the change being reverted may already be referenced;
    # the restore must remove them regardless.
    assert request.url.params["deleteDependencies"] == "true"
    assert json.loads(request.content) == {
        "0": {"name": "Default"},
        "3": {"name": "Guest"},
    }
    assert json.loads(ee_route.calls.last.request.content) == {"enable": True}
    assert next_post.call_count == 0  # the allocator is never rewound


def test_rollback_refuses_absent_snapshot(settings):
    result = Zones().rollback(_ctx(settings), REF, None)
    assert not result.ok
    assert "refusing" in result.message


# -- read-only views ------------------------------------------------------------


@respx.mock
def test_segment_zone_map_and_appliance_zone_lists(settings):
    respx.get(VRF_MAP_URL).mock(
        return_value=httpx.Response(200, json={"0": {"0": {"id": 0, "name": "Default"}}})
    )
    meta_route = respx.get(ZONE_META_URL).mock(
        return_value=httpx.Response(200, json={"3.NE": {"zones": ["Default"]}})
    )
    ctx = _ctx(settings)
    assert segment_zone_map(ctx) == {"0": {"0": {"id": 0, "name": "Default"}}}
    assert appliance_zone_lists(ctx, ne_pk="3.NE") == {"3.NE": {"zones": ["Default"]}}
    assert meta_route.calls.last.request.url.params["nePk"] == "3.NE"


# -- registry wiring --------------------------------------------------------------


def test_security_policy_applies_after_zones():
    assert "zones" in default_registry.get("security-policy").dependencies
    ordered = default_registry.order_refs(
        [Ref(kind="security-policy", name="0_0"), Ref(kind="zones", name="global")]
    )
    assert [r.kind for r in ordered] == ["zones", "security-policy"]
