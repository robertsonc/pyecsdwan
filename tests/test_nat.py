"""Unit + e2e tests for the NAT resources (#32).

Covers the three curated kinds — ``appliance/nat-maps`` (ECOS ``natMaps``,
the live-confirmed ``data``/``options`` envelope), ``appliance/nat-pools``
(ECOS ``nat/natPools``) and ``snat-maps`` (orchestrator
``/vrf/config/snatMaps``) — plus the read-only D-NAT view.

Structure mirrors tests/test_routes.py (unit normalize/idempotency, a
save-changes failure path, an e2e round trip through ``txn`` against the
bundled mock) and tests/test_appliance_zones.py (respx-driven write-shape
assertions). The ``activeMap`` handling described in resources/nat.py's
module docstring gets its own group: it must survive a full replace that
never mentions it, and must never appear as a phantom diff entry.
"""

from __future__ import annotations

import copy
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
from pyecsdwan.contract import Ctx, Owned, Ref, Reversibility, Scope
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.nat import (
    NatMaps,
    NatPools,
    SnatMaps,
    _inject_map_self,
    inter_segment_dnat_maps,
)
from pyecsdwan.resources.security_policy import _inject_self as _security_inject_self

BASE = "https://orch.example.com/gms/rest"
APPLIANCE_URL = f"{BASE}/appliance/rest"
SNAT_URL = f"{BASE}/vrf/config/snatMaps"

MAPS_REF = Ref(kind="appliance/nat-maps", name="global", appliance="BR1-EC")
POOLS_REF = Ref(kind="appliance/nat-pools", name="global", appliance="BR1-EC")
SNAT_REF = Ref(kind="snat-maps", name="global")


class _StubResolver:
    def ne_pk_for(self, name: str) -> str:
        return {"BR1-EC": "3.NE", "HUB1-EC": "1.NE", "BR2-EC": "5.NE"}.get(name, name)


def _ctx(settings: Any, resolver: Any = None) -> Ctx:
    client = OrchClient(settings)
    return Ctx(client=client, resolver=resolver if resolver is not None else _StubResolver())


def _job_ok(key: str = "k1", ne_pk: str = "3.NE") -> None:
    """Mock a successful save-changes action key + its terminal status."""
    respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": key})
    )
    respx.get(f"{BASE}/action/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": key,
                    "nepk": ne_pk,
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": 1,
                    "result": "Success",
                }
            ],
        )
    )


def _job_failed(key: str = "k1", ne_pk: str = "3.NE") -> None:
    respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": key})
    )
    respx.get(f"{BASE}/action/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": key,
                    "nepk": ne_pk,
                    "taskStatus": "Failed",
                    "percentComplete": 100,
                    "completionStatus": False,
                    "endTime": 1,
                    "result": "mock failure",
                }
            ],
        )
    )


#: The live-confirmed envelope (map -> {self, prio: {priority: rule}} wrapped
#: in `data`, with a sibling `options` block).
_RAW_ENVELOPE: dict[str, Any] = {
    "data": {
        "map1": {
            "self": "map1",
            "prio": {
                "65535": {
                    "self": 65535,
                    "comment": "",
                    "gms_marked": True,
                    "match": {"acl": ""},
                    "set": {"nat_dir": "none"},
                },
                "10": {
                    "self": 10,
                    "comment": "outbound",
                    "gms_marked": False,
                    "match": {"acl": "", "src_subnet": "10.0.0.0/8"},
                    "set": {"nat_dir": "outbound", "trans_src": "192.0.2.0/24"},
                },
            },
        }
    },
    "options": {"activeMap": "map1", "merge": False, "templateApply": False},
}
_CANONICAL_MAPS: dict[str, Any] = {
    "map1": {
        "prio": {
            "10": {
                "comment": "outbound",
                "match": {"acl": "", "src_subnet": "10.0.0.0/8"},
                "set": {"nat_dir": "outbound", "trans_src": "192.0.2.0/24"},
            },
            "65535": {
                "comment": "",
                "match": {"acl": ""},
                "set": {"nat_dir": "none"},
            },
        }
    }
}
_POOL_TABLE: dict[str, Any] = {
    "1": {
        "name": "pool1",
        "subnet": "192.0.2.0/24",
        "dir": "outbound",
        "pat": 1,
        "comment": "",
    }
}


# =============================================================================
# 1. appliance/nat-maps — normalize
# =============================================================================


def test_nat_maps_class_attributes() -> None:
    res = NatMaps()
    assert res.kind == "appliance/nat-maps"
    assert res.scope is Scope.APPLIANCE
    assert res.reversibility is Reversibility.REVERSIBLE
    assert res.deletable is True
    assert res.dependencies == ("appliance/nat-pools",)


def test_nat_maps_normalize_absent_states() -> None:
    res = NatMaps()
    assert res.normalize(None) is None
    assert res.normalize({}) is None
    assert res.normalize({"data": {}, "options": {"activeMap": "map1"}}) is None
    assert res.normalize([]) is None  # type: ignore[arg-type]


def test_nat_maps_normalize_confirmed_envelope() -> None:
    once = NatMaps().normalize(copy.deepcopy(_RAW_ENVELOPE))
    assert once == {"maps": _CANONICAL_MAPS, "activeMap": "map1"}


def test_nat_maps_normalize_strips_self_and_gms_marked() -> None:
    once = NatMaps().normalize(copy.deepcopy(_RAW_ENVELOPE))
    assert once is not None
    assert "self" not in once["maps"]["map1"]
    rule = once["maps"]["map1"]["prio"]["10"]
    assert "self" not in rule
    assert "gms_marked" not in rule


def test_nat_maps_normalize_drops_write_only_options_but_keeps_active_map() -> None:
    once = NatMaps().normalize(copy.deepcopy(_RAW_ENVELOPE))
    assert once is not None
    # merge/templateApply carry no state; activeMap does (see module docstring).
    assert "merge" not in once
    assert "templateApply" not in once
    assert once["activeMap"] == "map1"


def test_nat_maps_normalize_is_idempotent() -> None:
    res = NatMaps()
    once = res.normalize(copy.deepcopy(_RAW_ENVELOPE))
    assert res.normalize(once) == once  # the promotion checklist's litmus test


def test_nat_maps_normalize_sorts_rules_by_numeric_priority() -> None:
    raw = {"data": {"m": {"prio": {"100": {}, "9": {}, "65535": {}, "020": {}}}}}
    once = NatMaps().normalize(raw)
    assert once is not None
    # leading zeros canonicalized ("020" -> "20"), numeric (not lexical) order.
    assert list(once["maps"]["m"]["prio"]) == ["9", "20", "100", "65535"]


def test_nat_maps_normalize_sorts_map_names() -> None:
    raw = {"data": {"zmap": {"prio": {"1": {}}}, "amap": {"prio": {"1": {}}}}}
    once = NatMaps().normalize(raw)
    assert once is not None
    assert list(once["maps"]) == ["amap", "zmap"]


def test_nat_maps_normalize_accepts_bare_table_without_envelope() -> None:
    once = NatMaps().normalize({"map1": {"prio": {"10": {"match": {}}}}})
    assert once == {"maps": {"map1": {"prio": {"10": {"match": {}}}}}}


def test_nat_maps_normalize_rejects_non_numeric_priority() -> None:
    with pytest.raises(ValueError, match="numeric priority"):
        NatMaps().normalize({"data": {"m": {"prio": {"first": {}}}}})


def test_nat_maps_normalize_rejects_out_of_range_priority() -> None:
    with pytest.raises(ValueError, match="out of range"):
        NatMaps().normalize({"data": {"m": {"prio": {"65536": {}}}}})


def test_nat_maps_normalize_rejects_duplicate_priority_after_canonicalization() -> None:
    with pytest.raises(ValueError, match="duplicate rule priority"):
        NatMaps().normalize({"data": {"m": {"prio": {"10": {}, "010": {}}}}})


def test_nat_maps_normalize_rejects_non_mapping_rule() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        NatMaps().normalize({"data": {"m": {"prio": {"10": "nope"}}}})


def test_nat_maps_normalize_rejects_non_mapping_prio_table() -> None:
    with pytest.raises(ValueError, match="'prio' must be a mapping"):
        NatMaps().normalize({"data": {"m": {"prio": "nope"}}})


def test_nat_maps_no_phantom_drift_between_server_echo_and_user_intent() -> None:
    res = NatMaps()
    current = res.normalize(copy.deepcopy(_RAW_ENVELOPE))
    user_intent = {"maps": copy.deepcopy(_CANONICAL_MAPS), "activeMap": "map1"}
    desired = res.normalize(user_intent)
    assert res.diff(MAPS_REF, current, desired).empty


# =============================================================================
# 2. the self-echo helper: why security_policy._inject_self can't be imported
# =============================================================================


def test_inject_map_self_echoes_map_name_and_integer_priority() -> None:
    body = _inject_map_self(_CANONICAL_MAPS)
    assert body["map1"]["self"] == "map1"
    assert body["map1"]["prio"]["10"]["self"] == 10  # integer, per the spec
    assert body["map1"]["prio"]["65535"]["self"] == 65535
    # never descends into match/set
    assert "self" not in body["map1"]["prio"]["10"]["match"]
    assert "self" not in body["map1"]["prio"]["10"]["set"]


def test_security_policy_inject_self_would_mis_place_self_on_this_shape() -> None:
    """Documents the divergence recorded in resources/nat.py's docstring.

    ``security_policy._inject_self`` walks map -> zone-pair -> prio; natMaps
    has no zone-pair level, so that helper treats the literal string "prio"
    as a zone-pair key. Reusing it here would corrupt the payload — hence the
    one-level-shallower ``_inject_map_self``.
    """
    wrong = _security_inject_self(copy.deepcopy(_CANONICAL_MAPS))
    assert wrong["map1"]["prio"]["self"] == "prio"  # the corruption
    assert "self" not in wrong["map1"]["prio"]["10"]  # rule echo never written


def test_inject_map_self_survives_normalize_round_trip() -> None:
    res = NatMaps()
    once = res.normalize(copy.deepcopy(_RAW_ENVELOPE))
    assert once is not None
    written = {"data": _inject_map_self(once["maps"]), "options": {"activeMap": "map1"}}
    assert res.normalize(written) == once


# =============================================================================
# 3. appliance/nat-maps — write path (envelope, options, save-changes)
# =============================================================================


@respx.mock
def test_nat_maps_apply_posts_envelope_with_options(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _job_ok()
    res = NatMaps()
    desired = res.normalize(copy.deepcopy(_RAW_ENVELOPE))

    result = res.apply(_ctx(settings), res.diff(MAPS_REF, None, desired))
    assert result.ok, result.message
    assert "persisted" in result.message
    request = route.calls.last.request
    assert request.url.params["nePk"] == "3.NE"
    assert request.url.params["url"] == "natMaps"
    body = json.loads(request.content)
    assert set(body) == {"data", "options"}
    assert body["data"]["map1"]["self"] == "map1"
    assert body["data"]["map1"]["prio"]["10"]["self"] == 10
    assert body["options"] == {
        "merge": False,
        "templateApply": False,
        "activeMap": "map1",
    }


@respx.mock
def test_nat_maps_apply_preserves_active_map_from_current_state(settings: Any) -> None:
    """A desired state with no activeMap must not deactivate the live map."""
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _job_ok()
    res = NatMaps()
    current = res.normalize(copy.deepcopy(_RAW_ENVELOPE))
    desired = res.normalize({"data": {"map1": {"prio": {"10": {"match": {}}}}}})
    assert desired is not None and "activeMap" not in desired

    result = res.apply(_ctx(settings), res.diff(MAPS_REF, current, desired))
    assert result.ok, result.message
    body = json.loads(route.calls.last.request.content)
    assert body["options"]["activeMap"] == "map1"  # carried over from current


@respx.mock
def test_nat_maps_apply_delete_posts_empty_data_and_omits_active_map(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _job_ok()
    res = NatMaps()
    current = res.normalize(copy.deepcopy(_RAW_ENVELOPE))

    result = res.apply(_ctx(settings), res.diff(MAPS_REF, current, None))
    assert result.ok, result.message
    body = json.loads(route.calls.last.request.content)
    assert body["data"] == {}
    # Nothing left to activate, and the spec warns the active map cannot be
    # deleted — so the key is omitted rather than pointing at a gone map.
    assert "activeMap" not in body["options"]


@respx.mock
def test_nat_maps_apply_is_noop_on_empty_diff(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    res = NatMaps()
    state = res.normalize(copy.deepcopy(_RAW_ENVELOPE))

    result = res.apply(_ctx(settings), res.diff(MAPS_REF, state, state))
    assert result.ok
    assert result.changed is False
    assert route.call_count == 0


@respx.mock
def test_nat_maps_apply_fails_when_save_changes_fails(settings: Any) -> None:
    respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _job_failed()
    res = NatMaps()
    desired = res.normalize(copy.deepcopy(_RAW_ENVELOPE))

    result = res.apply(_ctx(settings), res.diff(MAPS_REF, None, desired))
    assert not result.ok
    assert "not persisted" in result.message
    assert result.jobs and result.jobs[0].state == "FAILED"


@respx.mock
def test_nat_maps_rollback_restores_snapshot_envelope(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _job_ok(key="k2")
    result = NatMaps().rollback(_ctx(settings), MAPS_REF, copy.deepcopy(_RAW_ENVELOPE))
    assert result.ok, result.message
    body = json.loads(route.calls.last.request.content)
    assert body["data"]["map1"]["self"] == "map1"
    assert body["options"]["activeMap"] == "map1"  # restored from the snapshot


@respx.mock
def test_nat_maps_canonicalize_desired_backfills_active_map(settings: Any) -> None:
    respx.get(APPLIANCE_URL).mock(
        return_value=httpx.Response(200, json=copy.deepcopy(_RAW_ENVELOPE))
    )
    res = NatMaps()
    desired = res.canonicalize_desired(
        _ctx(settings), MAPS_REF, {"maps": {"map1": {"prio": {"10": {"match": {}}}}}}
    )
    assert isinstance(desired, dict)
    assert desired["activeMap"] == "map1"


@respx.mock
def test_nat_maps_canonicalize_desired_keeps_explicit_active_map(settings: Any) -> None:
    respx.get(APPLIANCE_URL).mock(
        return_value=httpx.Response(200, json=copy.deepcopy(_RAW_ENVELOPE))
    )
    res = NatMaps()
    desired = res.canonicalize_desired(
        _ctx(settings),
        MAPS_REF,
        {"maps": {"map2": {"prio": {"10": {"match": {}}}}}, "activeMap": "map2"},
    )
    assert isinstance(desired, dict)
    assert desired["activeMap"] == "map2"


@respx.mock
def test_nat_maps_managed_by_prefers_gms_marked(settings: Any) -> None:
    respx.get(APPLIANCE_URL).mock(
        return_value=httpx.Response(200, json=copy.deepcopy(_RAW_ENVELOPE))
    )
    owns = NatMaps().managed_by(_ctx(settings), MAPS_REF)
    assert owns.state is Owned.OWNED
    assert "gms_marked" in owns.owner


@respx.mock
def test_nat_maps_managed_by_falls_back_to_template_join(settings: Any) -> None:
    unmarked = copy.deepcopy(_RAW_ENVELOPE)
    for rule in unmarked["data"]["map1"]["prio"].values():
        rule["gms_marked"] = False
    respx.get(APPLIANCE_URL).mock(return_value=httpx.Response(200, json=unmarked))
    respx.get(f"{BASE}/template/applianceAssociation").mock(
        return_value=httpx.Response(200, json={"templateIds": ["NatStd"]})
    )
    respx.get(f"{BASE}/template/templateSelection").mock(
        return_value=httpx.Response(200, json=["natMaps"])
    )
    owns = NatMaps().managed_by(_ctx(settings), MAPS_REF)
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group NatStd"


def test_nat_maps_requires_appliance_on_ref(settings: Any) -> None:
    with pytest.raises(ValueError, match="appliance-scoped"):
        NatMaps().fetch(_ctx(settings), Ref(kind="appliance/nat-maps", name="global"))


# =============================================================================
# 4. appliance/nat-pools
# =============================================================================


def test_nat_pools_class_attributes() -> None:
    res = NatPools()
    assert res.kind == "appliance/nat-pools"
    assert res.scope is Scope.APPLIANCE
    assert res.reversibility is Reversibility.REVERSIBLE


def test_nat_pools_normalize_absent_states() -> None:
    assert NatPools().normalize(None) is None
    assert NatPools().normalize({}) is None


def test_nat_pools_normalize_is_idempotent_and_id_sorted() -> None:
    res = NatPools()
    raw = {
        "10": {"name": "b", "subnet": "198.51.100.0/24", "dir": "inbound", "pat": 0},
        "02": {"name": "a", "subnet": "192.0.2.0/24", "dir": "outbound", "pat": 1},
    }
    once = res.normalize(raw)
    assert once is not None
    assert list(once["pools"]) == ["2", "10"]  # numeric order, zeros stripped
    assert res.normalize(once) == once
    assert res.normalize(once["pools"]) == once


def test_nat_pools_normalize_requires_name_and_subnet() -> None:
    with pytest.raises(ValueError, match="'name'"):
        NatPools().normalize({"1": {"subnet": "192.0.2.0/24"}})
    with pytest.raises(ValueError, match="'subnet'"):
        NatPools().normalize({"1": {"name": "p"}})


def test_nat_pools_normalize_rejects_non_numeric_id() -> None:
    with pytest.raises(ValueError, match="numeric pool id"):
        NatPools().normalize({"pool1": {"name": "p", "subnet": "192.0.2.0/24"}})


def test_nat_pools_normalize_passes_unknown_fields_through() -> None:
    once = NatPools().normalize(
        {"1": {"name": "p", "subnet": "192.0.2.0/24", "vendorTag": "x"}}
    )
    assert once is not None
    assert once["pools"]["1"]["vendorTag"] == "x"


@respx.mock
def test_nat_pools_apply_posts_whole_table(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _job_ok()
    res = NatPools()
    desired = res.normalize(copy.deepcopy(_POOL_TABLE))

    result = res.apply(_ctx(settings), res.diff(POOLS_REF, None, desired))
    assert result.ok, result.message
    request = route.calls.last.request
    assert request.url.params["url"] == "nat/natPools"
    assert json.loads(request.content) == _POOL_TABLE
    assert "+1/-0" in result.message


@respx.mock
def test_nat_pools_apply_deletes_removed_ids_before_replacing(settings: Any) -> None:
    delete_route = respx.delete(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    post_route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _job_ok()
    res = NatPools()
    current = res.normalize(
        {
            **copy.deepcopy(_POOL_TABLE),
            "2": {"name": "pool2", "subnet": "198.51.100.0/24", "dir": "inbound", "pat": 0},
        }
    )
    desired = res.normalize(copy.deepcopy(_POOL_TABLE))

    result = res.apply(_ctx(settings), res.diff(POOLS_REF, current, desired))
    assert result.ok, result.message
    # The per-id DELETE makes removal correct whether the plural POST
    # replaces or merges (the spec never says which).
    assert delete_route.call_count == 1
    assert delete_route.calls.last.request.url.params["url"] == "nat/natPools/2"
    assert json.loads(post_route.calls.last.request.content) == _POOL_TABLE
    assert "-1 pool(s)" in result.message


@respx.mock
def test_nat_pools_apply_is_noop_on_empty_diff(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    res = NatPools()
    state = res.normalize(copy.deepcopy(_POOL_TABLE))
    result = res.apply(_ctx(settings), res.diff(POOLS_REF, state, state))
    assert result.changed is False
    assert route.call_count == 0


@respx.mock
def test_nat_pools_apply_fails_when_save_changes_fails(settings: Any) -> None:
    respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _job_failed()
    res = NatPools()
    desired = res.normalize(copy.deepcopy(_POOL_TABLE))
    result = res.apply(_ctx(settings), res.diff(POOLS_REF, None, desired))
    assert not result.ok
    assert "not persisted" in result.message


@respx.mock
def test_nat_pools_rollback_reconciles_toward_snapshot(settings: Any) -> None:
    respx.get(APPLIANCE_URL).mock(return_value=httpx.Response(200, json={}))
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _job_ok(key="k2")
    result = NatPools().rollback(_ctx(settings), POOLS_REF, copy.deepcopy(_POOL_TABLE))
    assert result.ok, result.message
    assert json.loads(route.calls.last.request.content) == _POOL_TABLE


# =============================================================================
# 5. snat-maps (orchestrator scope)
# =============================================================================


def test_snat_maps_class_attributes() -> None:
    res = SnatMaps()
    assert res.kind == "snat-maps"
    assert res.scope is Scope.ORCHESTRATOR
    assert res.reversibility is Reversibility.REVERSIBLE


def test_snat_maps_normalize_absent_states() -> None:
    assert SnatMaps().normalize(None) is None
    assert SnatMaps().normalize({}) is None


def test_snat_maps_normalize_strips_gms_marked_and_sorts_pairs() -> None:
    res = SnatMaps()
    once = res.normalize(
        {
            "1_10": {"enable": False},
            "0_1": {"enable": False, "gms_marked": True, "comment": "no snat"},
            "1_2": {"enable": True},
        }
    )
    assert once is not None
    assert list(once["maps"]) == ["0_1", "1_2", "1_10"]  # numeric pair order
    assert once["maps"]["0_1"] == {"enable": False, "comment": "no snat"}
    assert res.normalize(once) == once


def test_snat_maps_normalize_rejects_bad_pair_key() -> None:
    with pytest.raises(ValueError, match="segment pair"):
        SnatMaps().normalize({"guest": {"enable": False}})


def test_snat_maps_normalize_requires_boolean_enable() -> None:
    with pytest.raises(ValueError, match="missing the required field 'enable'"):
        SnatMaps().normalize({"0_1": {"comment": "x"}})
    with pytest.raises(ValueError, match="must be a boolean"):
        SnatMaps().normalize({"0_1": {"enable": "false"}})


@respx.mock
def test_snat_maps_apply_full_replaces_and_never_saves_changes(settings: Any) -> None:
    route = respx.post(SNAT_URL).mock(return_value=httpx.Response(204))
    save = respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": "k1"})
    )
    res = SnatMaps()
    desired = res.normalize({"0_1": {"enable": False}})

    result = res.apply(_ctx(settings), res.diff(SNAT_REF, None, desired))
    assert result.ok, result.message
    assert json.loads(route.calls.last.request.content) == {"0_1": {"enable": False}}
    # Orchestrator scope: save-changes is an appliance-proxy obligation only.
    assert save.call_count == 0


@respx.mock
def test_snat_maps_delete_posts_empty_table(settings: Any) -> None:
    route = respx.post(SNAT_URL).mock(return_value=httpx.Response(204))
    res = SnatMaps()
    current = res.normalize({"0_1": {"enable": False}})
    result = res.apply(_ctx(settings), res.diff(SNAT_REF, current, None))
    assert result.ok, result.message
    assert json.loads(route.calls.last.request.content) == {}


@respx.mock
def test_snat_maps_rollback_restores_snapshot(settings: Any) -> None:
    route = respx.post(SNAT_URL).mock(return_value=httpx.Response(204))
    result = SnatMaps().rollback(_ctx(settings), SNAT_REF, {"0_1": {"enable": False}})
    assert result.ok, result.message
    assert json.loads(route.calls.last.request.content) == {"0_1": {"enable": False}}


@respx.mock
def test_snat_maps_fetch_tolerates_no_content(settings: Any) -> None:
    respx.get(SNAT_URL).mock(return_value=httpx.Response(204))
    assert SnatMaps().fetch(_ctx(settings), SNAT_REF) is None


def test_snat_maps_has_no_owner(settings: Any) -> None:
    # Orchestrator-scope config has no template owner (contract default).
    assert SnatMaps().managed_by(_ctx(settings), SNAT_REF).state is Owned.UNOWNED


# =============================================================================
# 6. registry wiring
# =============================================================================


def test_all_three_kinds_are_registered() -> None:
    for kind in ("appliance/nat-maps", "appliance/nat-pools", "snat-maps"):
        assert default_registry.get(kind).kind == kind


def test_nat_maps_apply_after_nat_pools() -> None:
    ordered = default_registry.order_refs([MAPS_REF, POOLS_REF])
    assert [r.kind for r in ordered] == ["appliance/nat-pools", "appliance/nat-maps"]


# =============================================================================
# 7. e2e round trip through txn against the bundled mock
# =============================================================================


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, Any]]:
    pytest.importorskip("pyecsdwan.mock.server")
    from pyecsdwan.mock.server import run_in_thread

    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, Any]) -> dict[str, Any]:
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


def test_e2e_nat_maps_idempotent_replan(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    ref = Ref(kind="appliance/nat-maps", name="global", appliance="HUB1-EC")
    current = NatMaps().normalize(NatMaps().fetch(ctx, ref))
    assert current is not None
    assert list(current["maps"]["map1"]["prio"]) == ["10", "65535"]
    assert current["activeMap"] == "map1"

    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)  # staging the server's own state changes nothing


def test_e2e_nat_maps_apply_then_rollback(world: dict[str, Any]) -> None:
    state, candidate = world["state"], world["candidate"]
    ref = Ref(kind="appliance/nat-maps", name="global", appliance="BR2-EC")  # 5.NE, empty

    candidate.set_desired(
        ref,
        {
            "maps": {
                "map1": {
                    "prio": {"10": {"match": {"acl": ""}, "set": {"nat_dir": "outbound"}}}
                }
            },
            "activeMap": "map1",
        },
    )
    report = _commit(world)
    assert report.ok, report.messages
    stored = state.appliance_ecos["5.NE"]["natMaps"]
    assert stored["data"]["map1"]["prio"]["10"]["set"] == {"nat_dir": "outbound"}
    assert stored["options"]["activeMap"] == "map1"
    assert next(a for a in state.appliances if a["nePk"] == "5.NE")["hasUnsavedChanges"] is False

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.appliance_ecos["5.NE"]["natMaps"]["data"] == {}


def test_e2e_nat_maps_partial_set_preserves_active_map(world: dict[str, Any]) -> None:
    """A rule edit that never mentions activeMap must leave it alone."""
    state, candidate = world["state"], world["candidate"]
    ref = Ref(kind="appliance/nat-maps", name="global", appliance="BR1-EC")  # 3.NE
    candidate.set_path(ref, ["maps", "map2", "prio", "100", "comment"], "edited")

    report = _commit(world)
    assert report.ok, report.messages
    stored = state.appliance_ecos["3.NE"]["natMaps"]
    assert stored["data"]["map2"]["prio"]["100"]["comment"] == "edited"
    assert stored["options"]["activeMap"] == "map2"  # never deactivated


def test_e2e_nat_maps_full_replace_without_active_map_keeps_it(world: dict[str, Any]) -> None:
    """set_desired (full replace) omitting activeMap inherits the live one."""
    state, candidate = world["state"], world["candidate"]
    ref = Ref(kind="appliance/nat-maps", name="global", appliance="BR1-EC")  # 3.NE
    candidate.set_desired(
        ref, {"maps": {"map2": {"prio": {"200": {"match": {}, "set": {}}}}}}
    )

    report = _commit(world)
    assert report.ok, report.messages
    stored = state.appliance_ecos["3.NE"]["natMaps"]
    assert list(stored["data"]["map2"]["prio"]) == ["200"]
    assert stored["options"]["activeMap"] == "map2"


def test_e2e_nat_maps_whole_resource_delete_is_reversible(world: dict[str, Any]) -> None:
    state, candidate = world["state"], world["candidate"]
    ref = Ref(kind="appliance/nat-maps", name="global", appliance="BR1-EC")  # 3.NE
    candidate.delete(ref)

    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["3.NE"]["natMaps"]["data"] == {}

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    restored = state.appliance_ecos["3.NE"]["natMaps"]
    assert list(restored["data"]) == ["map2"]
    assert restored["options"]["activeMap"] == "map2"


def test_e2e_nat_maps_failed_save_reverts(world: dict[str, Any]) -> None:
    state, candidate = world["state"], world["candidate"]
    ref = Ref(kind="appliance/nat-maps", name="global", appliance="BR1-EC")
    candidate.set_path(ref, ["maps", "map2", "prio", "100", "comment"], "edited")
    state.fail_next_action = True  # consumed by the apply's own save-changes

    report = _commit(world)
    assert not report.ok
    stored = state.appliance_ecos["3.NE"]["natMaps"]
    assert stored["data"]["map2"]["prio"]["100"]["comment"] == "branch outbound"


def test_e2e_nat_pools_apply_then_rollback(world: dict[str, Any]) -> None:
    state, candidate = world["state"], world["candidate"]
    ref = Ref(kind="appliance/nat-pools", name="global", appliance="BR2-EC")  # 5.NE, empty

    candidate.set_desired(ref, {"pools": copy.deepcopy(_POOL_TABLE)})
    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["5.NE"]["nat/natPools"] == _POOL_TABLE

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.appliance_ecos["5.NE"]["nat/natPools"] == {}


def test_e2e_nat_pools_idempotent_replan(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    ref = Ref(kind="appliance/nat-pools", name="global", appliance="HUB1-EC")
    current = NatPools().normalize(NatPools().fetch(ctx, ref))
    assert current is not None and list(current["pools"]) == ["1"]
    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


def test_e2e_snat_maps_apply_then_rollback(world: dict[str, Any]) -> None:
    state, candidate = world["state"], world["candidate"]
    candidate.set_desired(SNAT_REF, {"maps": {"0_1": {"enable": False}, "0_2": {"enable": False}}})

    report = _commit(world)
    assert report.ok, report.messages
    assert set(state.snat_maps) == {"0_1", "0_2"}

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert set(state.snat_maps) == {"0_1"}


def test_e2e_snat_maps_idempotent_replan(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    current = SnatMaps().normalize(SnatMaps().fetch(ctx, SNAT_REF))
    assert current == {"maps": {"0_1": {"enable": False, "comment": "no snat to guest"}}}
    world["candidate"].set_desired(SNAT_REF, current)
    assert _plan_is_empty(world)


def test_e2e_dnat_view_is_read_only(world: dict[str, Any]) -> None:
    # D-NAT has no write endpoint in either spec, so it is a view, not a
    # resource — it must not be registered as a kind.
    view = inter_segment_dnat_maps(world["ctx"], "HUB1-EC")
    assert view == {"0_1": {"enable": True}}
    assert "dnat-maps" not in default_registry.kinds()


# =============================================================================
# 8. optional live smoke test (read-only; never run in CI)
# =============================================================================


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ECSDWAN_ORCH_URL"),
    reason="live Orchestrator smoke test; set ECSDWAN_ORCH_URL (and auth) to run",
)
def test_live_nat_read_only() -> None:
    """Read-only probe against a real Orchestrator. Credentials come from the
    ambient config/keyring — never hardcoded here."""
    settings = config.settings_from_env()
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))

    snat = SnatMaps()
    canonical = snat.normalize(snat.fetch(ctx, SNAT_REF))
    assert snat.normalize(canonical) == canonical  # idempotency, on real payloads

    for res in (NatMaps(), NatPools()):
        for ref in res.list_refs(ctx):
            once = res.normalize(res.fetch(ctx, ref))
            assert res.normalize(once) == once
