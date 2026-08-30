"""Unit + e2e tests for appliance/zones and appliance/security-maps (#19).

Mirrors tests/test_zones.py (deleteDependencies handling, idempotent
normalize) for the appliance-scope zones resource, and tests/test_vrrp.py's
e2e structure (round-trip against the bundled mock, rollback, managed_by())
for both resources. The security-maps half additionally covers the
self-echo strip/inject reuse from resources/security_policy.py.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Owned, Ref, Reversibility
from pyecsdwan.registry import default_registry
from pyecsdwan.resources.appliance_zones import ApplianceSecurityMaps, ApplianceZones

BASE = "https://orch.example.com/gms/rest"
APPLIANCE_URL = f"{BASE}/appliance/rest"
ZONES_REF = Ref(kind="appliance/zones", name="global", appliance="BR1-EC")
SECMAP_REF = Ref(kind="appliance/security-maps", name="global", appliance="BR1-EC")


class _StubResolver:
    """Resolves any appliance name straight through to a canned nePk."""

    def ne_pk_for(self, name: str) -> str:
        return {"BR1-EC": "3.NE", "HUB1-EC": "1.NE", "BR2-EC": "5.NE"}.get(name, name)


def _ctx(settings: Any, resolver: Any = None) -> Ctx:
    client = OrchClient(settings)
    return Ctx(client=client, resolver=resolver if resolver is not None else _StubResolver())


_RULE_20000 = {
    "comment": "",
    "gms_marked": True,
    "match": {"acl": "", "webcc_cat": "11|27|62|31|59|55|67|86"},
    "misc": {"tag": "tke", "rule": "enable", "logging_priority": "2", "logging": "enable"},
    "set": {"action": "deny"},
}
_MAPS = {"map1": {"0_1": {"prio": {"20000": copy.deepcopy(_RULE_20000)}}}}
#: What _MAPS normalizes to: gms_marked is bookkeeping, stripped like `self`.
_RULE_20000_STRIPPED = {k: v for k, v in _RULE_20000.items() if k != "gms_marked"}
_MAPS_NORMALIZED = {"map1": {"0_1": {"prio": {"20000": _RULE_20000_STRIPPED}}}}


# == appliance/zones ===========================================================


# -- reversibility / kind -------------------------------------------------------


def test_zones_reversibility_and_kind() -> None:
    assert ApplianceZones().reversibility is Reversibility.REVERSIBLE
    assert ApplianceZones().kind == "appliance/zones"
    assert ApplianceZones().deletable is True


# -- normalize: idempotent, no injected default row, no allocator --------------


def test_zones_normalize_none_yields_absent() -> None:
    assert ApplianceZones().normalize(None) is None


def test_zones_normalize_empty_dict_yields_absent() -> None:
    # Unlike orchestrator-scope zones.py: no server-managed row is confirmed
    # at this scope, so an empty table is a genuine, common state.
    assert ApplianceZones().normalize({}) is None


def test_zones_normalize_confirmed_real_shape() -> None:
    # Real captured shape (#19): {"1": {"name": "Untrust"}}, no id-0 row.
    once = ApplianceZones().normalize({"1": {"name": "Untrust"}})
    assert once == {"zones": {"1": {"name": "Untrust"}}}
    assert "0" not in once["zones"]


def test_zones_normalize_is_idempotent_and_id_keyed() -> None:
    res = ApplianceZones()
    raw = {"7": {"name": "IoT", "vendorTag": "x"}, "02": {"name": "Guest"}}
    once = res.normalize(raw)
    assert once == {
        "zones": {"2": {"name": "Guest"}, "7": {"name": "IoT", "vendorTag": "x"}}
    }
    assert res.normalize(once) == once
    # normalize() also accepts its own wrapped {"zones": {...}} shape.
    assert res.normalize(once["zones"]) == once


def test_zones_normalize_requires_name() -> None:
    with pytest.raises(ValueError, match="name"):
        ApplianceZones().normalize({"3": {}})


def test_zones_normalize_rejects_non_numeric_key() -> None:
    with pytest.raises(ValueError, match="numeric zone id"):
        ApplianceZones().normalize({"guest": {"name": "Guest"}})


def test_zones_normalize_rejects_non_mapping_entry() -> None:
    with pytest.raises(ValueError, match="mapping"):
        ApplianceZones().normalize({"1": "not-a-mapping"})


def test_zones_normalize_rejects_non_dict_payload() -> None:
    with pytest.raises(ValueError):
        ApplianceZones().normalize("not-a-dict")  # type: ignore[arg-type]


# -- no phantom drift between raw-server shape and user-authored shape --------


def test_zones_no_phantom_drift() -> None:
    res = ApplianceZones()
    server_raw = {"1": {"name": "Untrust"}}
    user_intent = {"zones": {"01": {"name": "Untrust"}}}
    current = res.normalize(server_raw)
    desired = res.normalize(user_intent)
    assert res.diff(ZONES_REF, current, desired).empty


# -- apply: deleteDependencies surfaced (mirrors zones.py) --------------------


@respx.mock
def test_zones_apply_removal_posts_delete_dependencies_true(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": "k1"})
    )
    respx.get(f"{BASE}/action/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": "k1",
                    "nepk": "3.NE",
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": 1,
                    "result": "Success",
                }
            ],
        )
    )
    res = ApplianceZones()
    current = res.normalize({"1": {"name": "Untrust"}, "2": {"name": "DMZ"}})
    desired = res.normalize({"1": {"name": "Untrust"}})

    result = res.apply(_ctx(settings), res.diff(ZONES_REF, current, desired))
    assert result.ok, result.message
    request = route.calls.last.request
    assert request.url.params["nePk"] == "3.NE"
    assert request.url.params["url"] == "zones"
    assert request.url.params["deleteDependencies"] == "true"
    assert json.loads(request.content) == {"1": {"name": "Untrust"}}
    assert "deleteDependencies=true" in result.message
    assert "persisted" in result.message


@respx.mock
def test_zones_apply_add_only_uses_delete_dependencies_false(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": "k1"})
    )
    respx.get(f"{BASE}/action/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": "k1",
                    "nepk": "3.NE",
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": 1,
                    "result": "Success",
                }
            ],
        )
    )
    res = ApplianceZones()
    current = res.normalize({"1": {"name": "Untrust"}})
    desired = res.normalize({"1": {"name": "Untrust"}, "2": {"name": "DMZ"}})

    result = res.apply(_ctx(settings), res.diff(ZONES_REF, current, desired))
    assert result.ok, result.message
    assert route.calls.last.request.url.params["deleteDependencies"] == "false"
    assert "deleteDependencies=false" in result.message


@respx.mock
def test_zones_apply_is_noop_on_empty_diff(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    res = ApplianceZones()
    state = res.normalize({"1": {"name": "Untrust"}})

    result = res.apply(_ctx(settings), res.diff(ZONES_REF, state, state))
    assert result.ok
    assert result.changed is False
    assert route.call_count == 0


@respx.mock
def test_zones_apply_reports_failure_when_save_fails(settings: Any) -> None:
    respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": "k1"})
    )
    respx.get(f"{BASE}/action/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": "k1",
                    "nepk": "3.NE",
                    "taskStatus": "Failed",
                    "percentComplete": 100,
                    "completionStatus": False,
                    "endTime": 1,
                    "result": "mock failure",
                }
            ],
        )
    )
    res = ApplianceZones()
    result = res.apply(
        _ctx(settings),
        res.diff(ZONES_REF, None, res.normalize({"1": {"name": "Untrust"}})),
    )
    assert not result.ok
    assert "not persisted" in result.message


@respx.mock
def test_zones_rollback_always_uses_delete_dependencies_true(settings: Any) -> None:
    respx.get(APPLIANCE_URL).mock(return_value=httpx.Response(200, json={}))
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": "k2"})
    )
    respx.get(f"{BASE}/action/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": "k2",
                    "nepk": "3.NE",
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": 1,
                    "result": "Success",
                }
            ],
        )
    )
    result = ApplianceZones().rollback(_ctx(settings), ZONES_REF, {"1": {"name": "Untrust"}})
    assert result.ok, result.message
    request = route.calls.last.request
    assert request.url.params["deleteDependencies"] == "true"
    assert json.loads(request.content) == {"1": {"name": "Untrust"}}


# == appliance/security-maps ====================================================


def test_secmaps_reversibility_kind_and_dependency() -> None:
    assert ApplianceSecurityMaps().reversibility is Reversibility.REVERSIBLE
    assert ApplianceSecurityMaps().kind == "appliance/security-maps"
    assert ApplianceSecurityMaps().dependencies == ("appliance/zones",)


# -- normalize: strip self/gms_marked, idempotent -------------------------------


def test_secmaps_normalize_none_and_non_dict_yield_absent() -> None:
    assert ApplianceSecurityMaps().normalize(None) is None
    assert ApplianceSecurityMaps().normalize([]) is None  # type: ignore[arg-type]


def test_secmaps_normalize_empty_dict_yields_absent() -> None:
    assert ApplianceSecurityMaps().normalize({}) is None


def test_secmaps_normalize_confirmed_real_shape() -> None:
    once = ApplianceSecurityMaps().normalize(copy.deepcopy(_MAPS))
    # gms_marked is bookkeeping (like `self`) and is stripped by normalize().
    assert once == {"maps": _MAPS_NORMALIZED}
    assert "gms_marked" not in once["maps"]["map1"]["0_1"]["prio"]["20000"]


def test_secmaps_normalize_strips_gms_marked_and_self() -> None:
    raw = {
        "map1": {
            "self": "map1",
            "0_1": {
                "self": "0_1",
                "prio": {"20000": {**copy.deepcopy(_RULE_20000), "self": 20000}},
            },
        }
    }
    once = ApplianceSecurityMaps().normalize(raw)
    assert once == {"maps": _MAPS_NORMALIZED}
    assert "self" not in once["maps"]["map1"]
    assert "gms_marked" not in once["maps"]["map1"]["0_1"]["prio"]["20000"]


def test_secmaps_normalize_is_idempotent() -> None:
    res = ApplianceSecurityMaps()
    once = res.normalize(copy.deepcopy(_MAPS))
    assert res.normalize(once["maps"]) == once


# -- no phantom drift: raw server echo vs. user-authored shape ----------------


def test_secmaps_no_phantom_drift() -> None:
    res = ApplianceSecurityMaps()
    prio = copy.deepcopy(_MAPS["map1"]["0_1"]["prio"])
    server_raw = {"map1": {"self": "map1", "0_1": {"self": "0_1", "prio": prio}}}
    user_intent = {"maps": copy.deepcopy(_MAPS)}
    current = res.normalize(server_raw)
    desired = res.normalize(user_intent)
    assert res.diff(SECMAP_REF, current, desired).empty


# -- apply: self re-injected on write -------------------------------------------


@respx.mock
def test_secmaps_apply_reinjects_self_then_saves(settings: Any) -> None:
    proxy_route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": "k1"})
    )
    respx.get(f"{BASE}/action/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": "k1",
                    "nepk": "3.NE",
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": 1,
                    "result": "Success",
                }
            ],
        )
    )
    res = ApplianceSecurityMaps()
    desired = res.normalize(copy.deepcopy(_MAPS))

    result = res.apply(_ctx(settings), res.diff(SECMAP_REF, None, desired))
    assert result.ok, result.message
    assert "1 rule(s)" in result.message
    assert "persisted" in result.message

    request = proxy_route.calls.last.request
    assert request.url.params["nePk"] == "3.NE"
    assert request.url.params["url"] == "securityMaps"
    body = json.loads(request.content)
    assert body["map1"]["self"] == "map1"
    assert body["map1"]["0_1"]["self"] == "0_1"
    assert body["map1"]["0_1"]["prio"]["20000"]["self"] == 20000


@respx.mock
def test_secmaps_apply_is_noop_on_empty_diff(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    res = ApplianceSecurityMaps()
    state = res.normalize(copy.deepcopy(_MAPS))

    result = res.apply(_ctx(settings), res.diff(SECMAP_REF, state, state))
    assert result.ok
    assert result.changed is False
    assert route.call_count == 0


@respx.mock
def test_secmaps_apply_delete_posts_empty_table(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": "k1"})
    )
    respx.get(f"{BASE}/action/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": "k1",
                    "nepk": "3.NE",
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": 1,
                    "result": "Success",
                }
            ],
        )
    )
    res = ApplianceSecurityMaps()
    current = res.normalize(copy.deepcopy(_MAPS))
    result = res.apply(_ctx(settings), res.diff(SECMAP_REF, current, None))
    assert result.ok, result.message
    assert json.loads(route.calls.last.request.content) == {}


@respx.mock
def test_secmaps_rollback_reinjects_self_then_saves(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": "k2"})
    )
    respx.get(f"{BASE}/action/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": "k2",
                    "nepk": "3.NE",
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": 1,
                    "result": "Success",
                }
            ],
        )
    )
    result = ApplianceSecurityMaps().rollback(_ctx(settings), SECMAP_REF, copy.deepcopy(_MAPS))
    assert result.ok, result.message
    body = json.loads(route.calls.last.request.content)
    assert body["map1"]["self"] == "map1"


# == registry wiring ============================================================


def test_security_maps_applies_after_appliance_zones() -> None:
    assert "appliance/zones" in default_registry.get("appliance/security-maps").dependencies
    ordered = default_registry.order_refs(
        [
            Ref(kind="appliance/security-maps", name="global", appliance="BR1-EC"),
            Ref(kind="appliance/zones", name="global", appliance="BR1-EC"),
        ]
    )
    assert [r.kind for r in ordered] == ["appliance/zones", "appliance/security-maps"]


# == e2e: idempotent round-trip + rollback + managed_by, via the mock =========


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, Any]]:
    pytest.importorskip("pyecsdwan.mock.server")
    from pyecsdwan.mock.server import run_in_thread

    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, Any]) -> dict[str, Any]:
    from pyecsdwan.resolver import Resolver

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


def test_e2e_zones_idempotent_round_trip_against_seeded_state(world: dict[str, Any]) -> None:
    # 1.NE (HUB1-EC) is seeded with two zones (Untrust, DMZ); re-planning the
    # fetched-and-normalized state as desired must diff empty.
    ctx = world["ctx"]
    ref = Ref(kind="appliance/zones", name="global", appliance="HUB1-EC")
    current = ApplianceZones().normalize(ApplianceZones().fetch(ctx, ref))
    assert current == {
        "zones": {"1": {"name": "Untrust"}, "2": {"name": "DMZ"}}
    }

    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


def test_e2e_zones_apply_then_rollback(world: dict[str, Any]) -> None:
    state = world["state"]
    candidate = world["candidate"]
    ref = Ref(kind="appliance/zones", name="global", appliance="BR2-EC")  # 5.NE, seeded empty

    candidate.set_desired(ref, {"zones": {"1": {"name": "Guest"}}})
    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["5.NE"]["zones"] == {"1": {"name": "Guest"}}
    assert next(a for a in state.appliances if a["nePk"] == "5.NE")["hasUnsavedChanges"] is False

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.appliance_ecos["5.NE"]["zones"] == {}


def test_e2e_zones_delete_is_reversible(world: dict[str, Any]) -> None:
    state = world["state"]
    candidate = world["candidate"]
    ref = Ref(kind="appliance/zones", name="global", appliance="BR1-EC")  # 3.NE, seeded 1 zone

    candidate.delete(ref)
    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["3.NE"]["zones"] == {}

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.appliance_ecos["3.NE"]["zones"] == {"1": {"name": "Untrust"}}


def test_e2e_zones_managed_by_ownership_join(world: dict[str, Any]) -> None:
    state = world["state"]
    state.template_groups["NetStd"] = {"name": "NetStd", "templates": []}
    state.template_selection["NetStd"] = ["zones", "dns"]
    state.template_association["3.NE"] = ["NetStd"]

    ctx = world["ctx"]
    ref = Ref(kind="appliance/zones", name="global", appliance="BR1-EC")
    owns = ApplianceZones().managed_by(ctx, ref)
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group NetStd"

    other_ref = Ref(kind="appliance/zones", name="global", appliance="BR2-EC")  # unassociated
    assert ApplianceZones().managed_by(ctx, other_ref).state is Owned.UNOWNED


def test_e2e_secmaps_idempotent_round_trip_against_seeded_state(world: dict[str, Any]) -> None:
    # 1.NE (HUB1-EC) is seeded with the confirmed real map1/0_1 rule table.
    ctx = world["ctx"]
    ref = Ref(kind="appliance/security-maps", name="global", appliance="HUB1-EC")
    current = ApplianceSecurityMaps().normalize(ApplianceSecurityMaps().fetch(ctx, ref))
    assert current is not None
    assert set(current["maps"]) == {"map1"}
    assert set(current["maps"]["map1"]["0_1"]["prio"]) == {"20000", "24999", "65535"}

    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


def test_e2e_secmaps_apply_then_rollback(world: dict[str, Any]) -> None:
    state = world["state"]
    candidate = world["candidate"]
    ref = Ref(kind="appliance/security-maps", name="global", appliance="BR2-EC")  # 5.NE, empty

    candidate.set_desired(ref, copy.deepcopy(_MAPS))
    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["5.NE"]["securityMaps"]["map1"]["0_1"]["prio"]["20000"]["set"] == {
        "action": "deny"
    }

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.appliance_ecos["5.NE"]["securityMaps"] == {}


def test_e2e_secmaps_managed_by_prefers_gms_marked(world: dict[str, Any]) -> None:
    # 1.NE's seeded rules all carry gms_marked=true — per-object precision
    # wins over the (here, absent) template-section join.
    ctx = world["ctx"]
    ref = Ref(kind="appliance/security-maps", name="global", appliance="HUB1-EC")
    result = ApplianceSecurityMaps().managed_by(ctx, ref)
    assert result.state is Owned.OWNED
    assert "gms_marked" in result.owner


def test_e2e_secmaps_managed_by_falls_back_to_template_join(world: dict[str, Any]) -> None:
    # 3.NE (BR1-EC) is seeded with no security maps at all (no gms_marked
    # signal available), so managed_by() falls back to the ownership join.
    state = world["state"]
    state.template_groups["FwStd"] = {"name": "FwStd", "templates": []}
    state.template_selection["FwStd"] = ["securityMaps"]
    state.template_association["3.NE"] = ["FwStd"]

    ctx = world["ctx"]
    ref = Ref(kind="appliance/security-maps", name="global", appliance="BR1-EC")
    owns = ApplianceSecurityMaps().managed_by(ctx, ref)
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group FwStd"
