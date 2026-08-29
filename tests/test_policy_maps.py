"""Unit + e2e tests for the QoS / optimization / route policy maps (#33).

Mirrors tests/test_appliance_zones.py (unit normalize + respx write-path
assertions, then an e2e round trip through `txn` against the bundled mock)
for the three identically-shaped ``data``/``options`` map endpoints.

The bulk of this file is the ``activeMap`` contract, which is the load-
bearing judgement call of the resource: it is a *write directive* ("after
POST, the name of the map to activate as the current policy map"), so an
options block that omits or blanks it can deactivate the operator's live map
as a side effect of an unrelated rule edit. resources/policy_maps.py makes it
part of desired state and never sends a blank; every branch of that is
covered below.
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
from pyecsdwan.contract import Ctx, Owned, Ref, Reversibility, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resources.policy_maps import (
    OptimizationMaps,
    QosMaps,
    RouteMaps,
    _inject_self_maps,
)

BASE = "https://orch.example.com/gms/rest"
APPLIANCE_URL = f"{BASE}/appliance/rest"
SAVE_URL = f"{BASE}/appliance/saveChanges"
STATUS_URL = f"{BASE}/action/status"

QOS_REF = Ref(kind="appliance/qos-map", name="global", appliance="BR1-EC")


class _StubResolver:
    def ne_pk_for(self, name: str) -> str:
        return {"BR1-EC": "3.NE", "HUB1-EC": "1.NE", "BR2-EC": "5.NE"}.get(name, name)


def _ctx(settings: Any, resolver: Any = None) -> Ctx:
    client = OrchClient(settings)
    return Ctx(client=client, resolver=resolver if resolver is not None else _StubResolver())


def _mock_save(ok: bool = True) -> None:
    """Wire the save-changes + action-status pair every appliance write needs."""
    respx.post(SAVE_URL).mock(return_value=httpx.Response(200, json={"clientKey": "k1"}))
    respx.get(STATUS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": "k1",
                    "nepk": "3.NE",
                    "taskStatus": "Completed" if ok else "Failed",
                    "percentComplete": 100,
                    "completionStatus": ok,
                    "endTime": 1,
                    "result": "Success" if ok else "mock failure",
                }
            ],
        )
    )


_RULE = {
    "comment": "voice",
    "match": {"acl": "", "app": "sip"},
    "set": {"traffic_class": 1, "lan_qos": "trust-lan"},
}
#: The confirmed live envelope: options carry activeMap; data carries the maps.
_ENVELOPE = {
    "options": {"merge": True, "activeMap": "map1", "templateApply": False},
    "data": {"map1": {"self": "map1", "prio": {"1000": {"self": 1000, **copy.deepcopy(_RULE)}}}},
}
#: What _ENVELOPE normalizes to.
_CANONICAL = {
    "maps": {"map1": {"prio": {"1000": copy.deepcopy(_RULE)}}},
    "activeMap": "map1",
}


# == kind / class attributes ==================================================


@pytest.mark.parametrize(
    ("res", "kind", "path"),
    [
        (QosMaps(), "appliance/qos-map", "qosMaps"),
        (OptimizationMaps(), "appliance/optimization-map", "optimizationMaps"),
        (RouteMaps(), "appliance/route-map", "routeMaps"),
    ],
)
def test_kind_reversibility_and_ecos_path(res: Any, kind: str, path: str) -> None:
    assert res.kind == kind
    assert res.ecos_path == path
    assert res.reversibility is Reversibility.REVERSIBLE
    assert res.tier is Tier.CURATED
    assert res.deletable is True
    assert res.dependencies == ()
    assert default_registry.get(kind) is not None


# == normalize ================================================================


def test_normalize_none_and_empty_yield_absent() -> None:
    assert QosMaps().normalize(None) is None
    assert QosMaps().normalize({}) is None
    assert QosMaps().normalize({"data": {}, "options": {"activeMap": "map1"}}) is None


def test_normalize_rejects_non_mapping() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        QosMaps().normalize([])  # type: ignore[arg-type]


def test_normalize_rejects_non_mapping_map_body() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        QosMaps().normalize({"data": {"map1": "not-a-mapping"}})


def test_normalize_lifts_active_map_and_strips_self() -> None:
    once = QosMaps().normalize(copy.deepcopy(_ENVELOPE))
    assert once == _CANONICAL
    assert "self" not in once["maps"]["map1"]
    assert "self" not in once["maps"]["map1"]["prio"]["1000"]


def test_normalize_drops_merge_and_template_apply() -> None:
    # merge/templateApply are per-write transport directives, not state.
    once = QosMaps().normalize(copy.deepcopy(_ENVELOPE))
    assert set(once) == {"maps", "activeMap"}


def test_normalize_strips_gms_marked() -> None:
    raw = copy.deepcopy(_ENVELOPE)
    raw["data"]["map1"]["prio"]["1000"]["gms_marked"] = True
    once = QosMaps().normalize(raw)
    assert once == _CANONICAL


def test_normalize_is_idempotent_on_its_own_output() -> None:
    res = QosMaps()
    once = res.normalize(copy.deepcopy(_ENVELOPE))
    assert res.normalize(once) == once
    assert res.normalize(res.normalize(once)) == once


def test_normalize_accepts_a_bare_map_table() -> None:
    # An operator may author just the maps, with no envelope at all.
    bare = {"map1": {"prio": {"1000": copy.deepcopy(_RULE)}}}
    assert QosMaps().normalize(bare) == {"maps": bare}


def test_normalize_orders_maps_by_name() -> None:
    once = QosMaps().normalize(
        {"data": {"zeta": {"prio": {}}, "alpha": {"prio": {}}, "mid": {"prio": {}}}}
    )
    assert list(once["maps"]) == ["alpha", "mid", "zeta"]


def test_normalize_drops_active_map_when_no_maps_remain() -> None:
    # An activeMap naming a map that does not exist is not diffable state,
    # and a whole-resource delete's verify() compares against a None desired.
    assert QosMaps().normalize({"options": {"activeMap": "gone"}, "data": {}}) is None


def test_no_phantom_drift_between_server_echo_and_user_intent() -> None:
    res = QosMaps()
    current = res.normalize(copy.deepcopy(_ENVELOPE))
    user_intent = {
        "activeMap": "map1",
        "maps": {"map1": {"prio": {"1000": copy.deepcopy(_RULE)}}},
    }
    assert res.diff(QOS_REF, current, res.normalize(user_intent)).empty


# == self re-injection (the depth security_policy._inject_self cannot reach) ===


def test_inject_self_maps_injects_at_map_and_rule_level_only() -> None:
    out = _inject_self_maps({"map1": {"prio": {"1000": copy.deepcopy(_RULE)}}})
    assert out["map1"]["self"] == "map1"
    assert out["map1"]["prio"]["1000"]["self"] == 1000
    # Crucially NOT self="prio": security_policy._inject_self would write that
    # here because it assumes an extra zone-pair nesting level.
    assert "self" not in out["map1"]["prio"]


def test_inject_self_maps_keeps_non_digit_priority_keys_verbatim() -> None:
    out = _inject_self_maps({"m": {"prio": {"default": {"match": {}}}}})
    assert out["m"]["prio"]["default"]["self"] == "default"


# == activeMap: the load-bearing behavior =====================================


@respx.mock
def test_canonicalize_desired_inherits_server_active_map_when_unstated(
    settings: Any,
) -> None:
    """Intent that says nothing about activeMap must not deactivate the live
    map — it inherits whatever the appliance currently has."""
    respx.get(APPLIANCE_URL).mock(
        return_value=httpx.Response(200, json=copy.deepcopy(_ENVELOPE))
    )
    res = QosMaps()
    intent = {"maps": {"other": {"prio": {"1000": copy.deepcopy(_RULE)}}}}
    canonical = res.canonicalize_desired(_ctx(settings), QOS_REF, intent)
    assert canonical == {
        "maps": {"other": {"prio": {"1000": copy.deepcopy(_RULE)}}},
        "activeMap": "map1",
    }


@respx.mock
def test_canonicalize_desired_keeps_an_explicit_active_map(settings: Any) -> None:
    route = respx.get(APPLIANCE_URL).mock(
        return_value=httpx.Response(200, json=copy.deepcopy(_ENVELOPE))
    )
    canonical = QosMaps().canonicalize_desired(
        _ctx(settings),
        QOS_REF,
        {"activeMap": "afterhours", "maps": {"afterhours": {"prio": {}}}},
    )
    assert canonical["activeMap"] == "afterhours"
    # Stated intent needs no server round trip at all.
    assert route.call_count == 0


@respx.mock
def test_canonicalize_desired_tolerates_a_server_with_no_active_map(
    settings: Any,
) -> None:
    respx.get(APPLIANCE_URL).mock(return_value=httpx.Response(200, json={}))
    canonical = QosMaps().canonicalize_desired(
        _ctx(settings), QOS_REF, {"maps": {"m": {"prio": {}}}}
    )
    assert canonical is not None
    assert "activeMap" not in canonical


def test_active_map_change_alone_is_diffable() -> None:
    res = QosMaps()
    current = res.normalize(copy.deepcopy(_ENVELOPE))
    desired = copy.deepcopy(current)
    desired["activeMap"] = "afterhours"
    diff = res.diff(QOS_REF, current, desired)
    assert not diff.empty
    assert any("activeMap" in entry.path for entry in diff)


@respx.mock
def test_apply_sends_the_desired_active_map(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save()
    res = QosMaps()
    current = res.normalize(copy.deepcopy(_ENVELOPE))
    desired = copy.deepcopy(current)
    desired["activeMap"] = "afterhours"

    result = res.apply(_ctx(settings), res.diff(QOS_REF, current, desired))
    assert result.ok, result.message
    body = json.loads(route.calls.last.request.content)
    assert body["options"]["activeMap"] == "afterhours"
    assert "activeMap=afterhours" in result.message


@respx.mock
def test_apply_omits_active_map_rather_than_blanking_it(settings: Any) -> None:
    """The guard that matters: no activeMap anywhere means the key is absent
    from the options block, never ``""`` — a blank is a value the appliance
    would act on."""
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save()
    res = QosMaps()
    desired = res.normalize({"data": {"map1": {"prio": {"1000": copy.deepcopy(_RULE)}}}})

    result = res.apply(_ctx(settings), res.diff(QOS_REF, None, desired))
    assert result.ok, result.message
    options = json.loads(route.calls.last.request.content)["options"]
    assert "activeMap" not in options
    assert options == {"merge": False, "templateApply": False}
    assert "activeMap unchanged" in result.message


@respx.mock
def test_delete_posts_empty_data_without_active_map(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save()
    res = QosMaps()
    current = res.normalize(copy.deepcopy(_ENVELOPE))

    result = res.apply(_ctx(settings), res.diff(QOS_REF, current, None))
    assert result.ok, result.message
    body = json.loads(route.calls.last.request.content)
    assert body["data"] == {}
    # Naming a map to activate while posting an empty table would name a map
    # that no longer exists.
    assert "activeMap" not in body["options"]


@respx.mock
def test_rollback_restores_the_snapshots_active_map(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save()
    result = QosMaps().rollback(_ctx(settings), QOS_REF, copy.deepcopy(_ENVELOPE))
    assert result.ok, result.message
    body = json.loads(route.calls.last.request.content)
    assert body["options"]["activeMap"] == "map1"
    assert body["data"]["map1"]["self"] == "map1"


# == write path ===============================================================


@respx.mock
def test_apply_posts_full_replace_envelope(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save()
    res = QosMaps()
    desired = res.normalize(copy.deepcopy(_ENVELOPE))

    result = res.apply(_ctx(settings), res.diff(QOS_REF, None, desired))
    assert result.ok, result.message
    request = route.calls.last.request
    assert request.url.params["nePk"] == "3.NE"
    assert request.url.params["url"] == "qosMaps"
    body = json.loads(request.content)
    assert body["options"]["merge"] is False
    assert body["options"]["templateApply"] is False
    assert body["data"]["map1"]["prio"]["1000"]["self"] == 1000
    assert "1 map(s), 1 rule(s)" in result.message
    assert "persisted" in result.message


@respx.mock
def test_apply_is_noop_on_empty_diff(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    res = QosMaps()
    state = res.normalize(copy.deepcopy(_ENVELOPE))

    result = res.apply(_ctx(settings), res.diff(QOS_REF, state, state))
    assert result.ok
    assert result.changed is False
    assert route.call_count == 0


@respx.mock
def test_apply_fails_when_save_changes_fails(settings: Any) -> None:
    respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save(ok=False)
    res = QosMaps()
    desired = res.normalize(copy.deepcopy(_ENVELOPE))

    result = res.apply(_ctx(settings), res.diff(QOS_REF, None, desired))
    assert not result.ok
    assert "not persisted" in result.message
    assert result.jobs and result.jobs[0].state != "SUCCESS"


@respx.mock
def test_rollback_fails_when_save_changes_fails(settings: Any) -> None:
    respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save(ok=False)
    result = QosMaps().rollback(_ctx(settings), QOS_REF, copy.deepcopy(_ENVELOPE))
    assert not result.ok
    assert "not persisted" in result.message


@respx.mock
def test_optimization_and_route_maps_use_their_own_ecos_paths(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save()
    for res, path in ((OptimizationMaps(), "optimizationMaps"), (RouteMaps(), "routeMaps")):
        ref = Ref(kind=res.kind, name="global", appliance="BR1-EC")
        desired = res.normalize(copy.deepcopy(_ENVELOPE))
        assert res.apply(_ctx(settings), res.diff(ref, None, desired)).ok
        assert route.calls.last.request.url.params["url"] == path


def test_fetch_requires_an_appliance() -> None:
    with pytest.raises(ValueError, match="appliance-scoped"):
        QosMaps()._ne_pk(_ctx(config.Settings(orch_url="https://x")), Ref("appliance/qos-map", "g"))


# == e2e: round trip + rollback + ownership, through txn against the mock =====


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


def test_e2e_qos_idempotent_round_trip_against_seeded_state(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    ref = Ref(kind="appliance/qos-map", name="global", appliance="HUB1-EC")
    current = QosMaps().normalize(QosMaps().fetch(ctx, ref))
    assert current is not None
    assert current["activeMap"] == "map1"
    assert set(current["maps"]["map1"]["prio"]) == {"1000", "65535"}

    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


def test_e2e_qos_rule_edit_preserves_the_active_map(world: dict[str, Any]) -> None:
    """The destructive-side-effect regression this resource exists to avoid:
    a replace-mode edit that mentions only rules must leave activeMap alone."""
    state = world["state"]
    ctx = world["ctx"]
    ref = Ref(kind="appliance/qos-map", name="global", appliance="BR1-EC")  # 3.NE
    assert state.appliance_ecos["3.NE"]["qosMaps"]["options"]["activeMap"] == "map1"

    current = QosMaps().normalize(QosMaps().fetch(ctx, ref))
    assert current is not None
    edited = copy.deepcopy(current["maps"])
    edited["map1"]["prio"]["1000"]["comment"] = "voice + video"
    # Intent names no activeMap at all.
    world["candidate"].set_desired(ref, {"maps": edited})

    report = _commit(world)
    assert report.ok, report.messages
    stored = state.appliance_ecos["3.NE"]["qosMaps"]
    assert stored["options"]["activeMap"] == "map1"
    assert stored["data"]["map1"]["prio"]["1000"]["comment"] == "voice + video"


def test_e2e_qos_active_map_switch_and_rollback(world: dict[str, Any]) -> None:
    state = world["state"]
    ctx = world["ctx"]
    ref = Ref(kind="appliance/qos-map", name="global", appliance="BR1-EC")  # 3.NE

    current = QosMaps().normalize(QosMaps().fetch(ctx, ref))
    assert current is not None
    assert "afterhours" in current["maps"]
    switched = copy.deepcopy(current)
    switched["activeMap"] = "afterhours"
    world["candidate"].set_desired(ref, switched)

    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["3.NE"]["qosMaps"]["options"]["activeMap"] == "afterhours"

    restore = txn.rollback_history_txn(ctx, default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.appliance_ecos["3.NE"]["qosMaps"]["options"]["activeMap"] == "map1"


def test_e2e_qos_create_on_empty_appliance_then_rollback(world: dict[str, Any]) -> None:
    state = world["state"]
    ref = Ref(kind="appliance/qos-map", name="global", appliance="BR2-EC")  # 5.NE, empty

    world["candidate"].set_desired(
        ref, {"activeMap": "map1", "maps": {"map1": {"prio": {"1000": copy.deepcopy(_RULE)}}}}
    )
    report = _commit(world)
    assert report.ok, report.messages
    stored = state.appliance_ecos["5.NE"]["qosMaps"]
    assert stored["options"]["activeMap"] == "map1"
    assert stored["data"]["map1"]["prio"]["1000"]["set"]["traffic_class"] == 1
    assert next(a for a in state.appliances if a["nePk"] == "5.NE")["hasUnsavedChanges"] is False

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.appliance_ecos["5.NE"]["qosMaps"]["data"] == {}


def test_e2e_qos_delete_is_reversible(world: dict[str, Any]) -> None:
    state = world["state"]
    ref = Ref(kind="appliance/qos-map", name="global", appliance="HUB1-EC")  # 1.NE

    world["candidate"].delete(ref)
    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["1.NE"]["qosMaps"]["data"] == {}

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    stored = state.appliance_ecos["1.NE"]["qosMaps"]
    assert set(stored["data"]) == {"map1"}
    assert stored["options"]["activeMap"] == "map1"


def test_e2e_optimization_maps_round_trip(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    ref = Ref(kind="appliance/optimization-map", name="global", appliance="HUB1-EC")
    current = OptimizationMaps().normalize(OptimizationMaps().fetch(ctx, ref))
    assert current is not None
    assert current["maps"]["map1"]["prio"]["65535"]["set"]["tcp_accel"] == "enable"
    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


def test_e2e_route_maps_round_trip(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    ref = Ref(kind="appliance/route-map", name="global", appliance="HUB1-EC")
    current = RouteMaps().normalize(RouteMaps().fetch(ctx, ref))
    assert current is not None
    # gms_marked is bookkeeping and is stripped from canonical state.
    assert "gms_marked" not in current["maps"]["map1"]["prio"]["65535"]
    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


def test_e2e_managed_by_prefers_gms_marked(world: dict[str, Any]) -> None:
    # 1.NE's seeded route map carries gms_marked — per-object precision wins
    # over the (here absent) template-section join.
    ctx = world["ctx"]
    ref = Ref(kind="appliance/route-map", name="global", appliance="HUB1-EC")
    owns = RouteMaps().managed_by(ctx, ref)
    assert owns.state is Owned.OWNED
    assert "gms_marked" in owns.owner


def test_e2e_managed_by_falls_back_to_template_join(world: dict[str, Any]) -> None:
    state = world["state"]
    state.template_groups["QosStd"] = {"name": "QosStd", "templates": []}
    state.template_selection["QosStd"] = ["qosMaps", "dns"]
    state.template_association["3.NE"] = ["QosStd"]

    ctx = world["ctx"]
    owns = QosMaps().managed_by(
        ctx, Ref(kind="appliance/qos-map", name="global", appliance="BR1-EC")
    )
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group QosStd"
    assert (
        QosMaps()
        .managed_by(ctx, Ref(kind="appliance/qos-map", name="global", appliance="BR2-EC"))
        .state
        is Owned.UNOWNED
    )


def test_e2e_list_refs_covers_every_appliance(world: dict[str, Any]) -> None:
    refs = QosMaps().list_refs(world["ctx"])
    assert {r.appliance for r in refs} == {"HUB1-EC", "BR1-EC", "BR2-EC"}
    assert all(r.kind == "appliance/qos-map" for r in refs)


# == optional live smoke test (read-only; never run in CI) ====================


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ECSDWAN_ORCH_URL"),
    reason="live Orchestrator smoke test; set ECSDWAN_ORCH_URL (and auth) to run",
)
def test_live_policy_maps_read_only() -> None:
    """Read-only probe of all five #33 surfaces against a real Orchestrator.

    Credentials come from the ambient config/keyring — never hardcoded here.
    Proves the promotion checklist's idempotency requirement
    (``normalize(normalize(x)) == normalize(x)``) on real payloads, and that
    the live ``activeMap`` survives a normalize round trip unchanged.
    """
    from pyecsdwan.resolver import Resolver
    from pyecsdwan.resources.shapers import InboundShapers, Shapers

    settings = config.load_settings()
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))

    for res in (QosMaps(), OptimizationMaps(), RouteMaps(), Shapers(), InboundShapers()):
        for ref in res.list_refs(ctx):
            canonical = res.normalize(res.fetch(ctx, ref))
            assert res.normalize(canonical) == canonical, f"{ref} is not idempotent"
            if isinstance(canonical, dict) and canonical.get("activeMap"):
                assert res.normalize(canonical)["activeMap"] == canonical["activeMap"]
