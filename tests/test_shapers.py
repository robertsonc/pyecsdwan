"""Unit + e2e tests for the outbound / inbound traffic shapers (#33).

Companion to tests/test_policy_maps.py. Same structure (unit normalize +
respx write-path assertions, then an e2e round trip through `txn` against the
bundled mock), but the shape under test is different in the way that matters:
shapers are **bare interface-keyed tables** with no ``data``/``options``
envelope and therefore no ``activeMap`` directive at all. These tests pin
that difference down — a shaper write must never grow an envelope.
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
from pyecsdwan.contract import Ctx, Ref, Reversibility, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resources.shapers import InboundShapers, Shapers

BASE = "https://orch.example.com/gms/rest"
APPLIANCE_URL = f"{BASE}/appliance/rest"
SAVE_URL = f"{BASE}/appliance/saveChanges"
STATUS_URL = f"{BASE}/action/status"

SHAPER_REF = Ref(kind="appliance/shaper", name="global", appliance="BR1-EC")


class _StubResolver:
    def ne_pk_for(self, name: str) -> str:
        return {"BR1-EC": "3.NE", "HUB1-EC": "1.NE", "BR2-EC": "5.NE"}.get(name, name)


def _ctx(settings: Any, resolver: Any = None) -> Ctx:
    client = OrchClient(settings)
    return Ctx(client=client, resolver=resolver if resolver is not None else _StubResolver())


def _mock_save(ok: bool = True) -> None:
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


_CLASSES = {
    "1": {"name": "realtime", "priority": 1, "min_bw": 10, "max_bw": 100, "excess": 100},
    "2": {"name": "interactive", "priority": 2, "min_bw": 5, "max_bw": 100, "excess": 100},
    "10": {"name": "default", "priority": 10, "min_bw": 0, "max_bw": 100, "excess": 1},
}
#: The confirmed live shape: a bare interface-keyed table, no envelope.
_TABLE = {
    "wan": {
        "accuracy": 1000,
        "dyn_bw_enable": True,
        "enable": True,
        "max_bw": 500000,
        "traffic-class": copy.deepcopy(_CLASSES),
    }
}


# == kind / class attributes ==================================================


@pytest.mark.parametrize(
    ("res", "kind", "path"),
    [
        (Shapers(), "appliance/shaper", "shapers"),
        (InboundShapers(), "appliance/inbound-shaper", "inboundShapers"),
    ],
)
def test_kind_reversibility_and_ecos_path(res: Any, kind: str, path: str) -> None:
    assert res.kind == kind
    assert res.ecos_path == path
    assert res.reversibility is Reversibility.REVERSIBLE
    assert res.tier is Tier.CURATED
    # The table always exists (there is always at least the system 'wan'
    # shaper), so whole-resource delete is refused.
    assert res.deletable is False
    assert default_registry.get(kind) is not None


def test_shaper_uses_the_confirmed_template_section() -> None:
    from pyecsdwan import ownership

    assert ownership.KIND_TO_TEMPLATE_SECTIONS["appliance/shaper"] == ("shaper",)
    # The inbound resource claims the confirmed section too, so its detection
    # does not rest on the unverified ECOS-path-matching candidate alone.
    assert "shaper" in ownership.KIND_TO_TEMPLATE_SECTIONS["appliance/inbound-shaper"]


# == normalize ================================================================


def test_normalize_absent_yields_an_empty_table_not_none() -> None:
    # Non-deletable singleton: "absent" is not a state this resource has.
    assert Shapers().normalize(None) == {"interfaces": {}}
    assert Shapers().normalize({}) == {"interfaces": {}}


def test_normalize_rejects_non_mapping() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        Shapers().normalize([])  # type: ignore[arg-type]


def test_normalize_rejects_non_mapping_interface_entry() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        Shapers().normalize({"wan": "not-a-mapping"})


def test_normalize_rejects_non_mapping_traffic_class() -> None:
    with pytest.raises(ValueError, match="traffic-class"):
        Shapers().normalize({"wan": {"traffic-class": []}})


def test_normalize_wraps_the_bare_table() -> None:
    once = Shapers().normalize(copy.deepcopy(_TABLE))
    assert once == {"interfaces": copy.deepcopy(_TABLE)}


def test_normalize_is_idempotent() -> None:
    res = Shapers()
    once = res.normalize(copy.deepcopy(_TABLE))
    assert res.normalize(once) == once
    assert res.normalize(res.normalize(once)) == once


def test_normalize_orders_interfaces_by_name() -> None:
    once = Shapers().normalize({"wan0": {}, "lan0": {}, "default": {}, "mgmt0": {}})
    assert list(once["interfaces"]) == ["default", "lan0", "mgmt0", "wan0"]


def test_normalize_orders_traffic_classes_numerically() -> None:
    once = Shapers().normalize({"wan": {"traffic-class": {"10": {}, "2": {}, "1": {}}}})
    assert list(once["interfaces"]["wan"]["traffic-class"]) == ["1", "2", "10"]


def test_normalize_canonicalizes_int_traffic_class_keys() -> None:
    # JSON object keys are strings on the wire, but hand-authored YAML may
    # use ints; 1 and "1" must not diff against each other.
    server = Shapers().normalize({"wan": {"traffic-class": {"1": {"priority": 1}}}})
    authored = Shapers().normalize({"wan": {"traffic-class": {1: {"priority": 1}}}})
    assert server == authored


def test_normalize_rejects_duplicate_traffic_class_ids() -> None:
    with pytest.raises(ValueError, match="duplicate traffic-class"):
        Shapers().normalize({"wan": {"traffic-class": {1: {}, "1": {}}}})


def test_normalize_strips_bookkeeping_but_keeps_unknown_fields() -> None:
    once = Shapers().normalize(
        {
            "wan": {
                "self": "wan",
                "gms_marked": True,
                "max_bw": 500000,
                "vendor_future_field": "kept",
                "traffic-class": {"1": {"self": 1, "gms_marked": True, "priority": 1}},
            }
        }
    )
    wan = once["interfaces"]["wan"]
    assert "self" not in wan
    assert "gms_marked" not in wan
    assert wan["vendor_future_field"] == "kept"
    assert wan["traffic-class"]["1"] == {"priority": 1}


def test_inbound_if_shaping_enable_passes_through() -> None:
    once = InboundShapers().normalize(
        {"wan": {"if_shaping_enable": True, "enable": True, "max_bw": 200000}}
    )
    assert once["interfaces"]["wan"]["if_shaping_enable"] is True


def test_no_phantom_drift_between_server_echo_and_user_intent() -> None:
    res = Shapers()
    server_raw = {
        "wan": {"self": "wan", "max_bw": 500000, "traffic-class": {"2": {}, "1": {}}}
    }
    user_intent = {"interfaces": {"wan": {"max_bw": 500000, "traffic-class": {1: {}, 2: {}}}}}
    assert res.diff(SHAPER_REF, res.normalize(server_raw), res.normalize(user_intent)).empty


# == write path ===============================================================


@respx.mock
def test_apply_posts_the_bare_table_with_no_envelope(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save()
    res = Shapers()
    desired = res.normalize(copy.deepcopy(_TABLE))

    result = res.apply(_ctx(settings), res.diff(SHAPER_REF, res.normalize({}), desired))
    assert result.ok, result.message
    request = route.calls.last.request
    assert request.url.params["nePk"] == "3.NE"
    assert request.url.params["url"] == "shapers"
    body = json.loads(request.content)
    # No data/options envelope — that belongs to the policy maps, not here.
    assert set(body) == {"wan"}
    assert "data" not in body
    assert "options" not in body
    assert body["wan"]["max_bw"] == 500000
    assert "1 interface(s), 3 traffic-class entr(ies)" in result.message
    assert "persisted" in result.message


@respx.mock
def test_apply_is_noop_on_empty_diff(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    res = Shapers()
    state = res.normalize(copy.deepcopy(_TABLE))

    result = res.apply(_ctx(settings), res.diff(SHAPER_REF, state, state))
    assert result.ok
    assert result.changed is False
    assert route.call_count == 0


@respx.mock
def test_apply_fails_when_save_changes_fails(settings: Any) -> None:
    respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save(ok=False)
    res = Shapers()
    result = res.apply(
        _ctx(settings),
        res.diff(SHAPER_REF, res.normalize({}), res.normalize(copy.deepcopy(_TABLE))),
    )
    assert not result.ok
    assert "not persisted" in result.message
    assert result.jobs and result.jobs[0].state != "SUCCESS"


@respx.mock
def test_rollback_reposts_the_snapshot_and_fails_on_bad_save(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save(ok=False)
    result = Shapers().rollback(_ctx(settings), SHAPER_REF, copy.deepcopy(_TABLE))
    assert not result.ok
    assert "not persisted" in result.message
    assert json.loads(route.calls.last.request.content) == copy.deepcopy(_TABLE)


@respx.mock
def test_inbound_shaper_uses_its_own_ecos_path(settings: Any) -> None:
    route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    _mock_save()
    res = InboundShapers()
    ref = Ref(kind=res.kind, name="global", appliance="BR1-EC")
    desired = res.normalize({"wan": {"if_shaping_enable": True, "max_bw": 200000}})

    assert res.apply(_ctx(settings), res.diff(ref, res.normalize({}), desired)).ok
    assert route.calls.last.request.url.params["url"] == "inboundShapers"


def test_fetch_requires_an_appliance() -> None:
    with pytest.raises(ValueError, match="appliance-scoped"):
        Shapers()._ne_pk(
            _ctx(config.Settings(orch_url="https://x")), Ref("appliance/shaper", "g")
        )


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


def test_e2e_shaper_idempotent_round_trip_against_seeded_state(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    ref = Ref(kind="appliance/shaper", name="global", appliance="HUB1-EC")
    current = Shapers().normalize(Shapers().fetch(ctx, ref))
    assert list(current["interfaces"]) == ["default", "lan0", "wan"]
    assert list(current["interfaces"]["wan"]["traffic-class"]) == ["1", "2", "10"]

    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


def test_e2e_shaper_class_tuning_then_rollback(world: dict[str, Any]) -> None:
    state = world["state"]
    ctx = world["ctx"]
    ref = Ref(kind="appliance/shaper", name="global", appliance="BR1-EC")  # 3.NE

    current = Shapers().normalize(Shapers().fetch(ctx, ref))
    tuned = copy.deepcopy(current)
    tuned["interfaces"]["wan"]["traffic-class"]["1"]["min_bw"] = 25
    tuned["interfaces"]["wan"]["max_bw"] = 750000
    world["candidate"].set_desired(ref, tuned)

    report = _commit(world)
    assert report.ok, report.messages
    stored = state.appliance_ecos["3.NE"]["shapers"]
    assert stored["wan"]["max_bw"] == 750000
    assert stored["wan"]["traffic-class"]["1"]["min_bw"] == 25
    assert next(a for a in state.appliances if a["nePk"] == "3.NE")["hasUnsavedChanges"] is False

    restore = txn.rollback_history_txn(ctx, default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    stored = state.appliance_ecos["3.NE"]["shapers"]
    assert stored["wan"]["max_bw"] == 500000
    assert stored["wan"]["traffic-class"]["1"]["min_bw"] == 10


def test_e2e_shaper_partial_set_merges_over_current_state(world: dict[str, Any]) -> None:
    """A `set` of one field must post a complete table, not a one-key one."""
    state = world["state"]
    ref = Ref(kind="appliance/shaper", name="global", appliance="BR1-EC")  # 3.NE

    world["candidate"].set_path(ref, ["interfaces", "wan", "enable"], False)
    report = _commit(world)
    assert report.ok, report.messages
    stored = state.appliance_ecos["3.NE"]["shapers"]
    assert stored["wan"]["enable"] is False
    # Everything else survived the whole-table replace.
    assert stored["wan"]["max_bw"] == 500000
    assert set(stored["wan"]["traffic-class"]) == {"1", "2", "10"}


def test_e2e_shaper_whole_resource_delete_is_refused(world: dict[str, Any]) -> None:
    ref = Ref(kind="appliance/shaper", name="global", appliance="BR1-EC")
    world["candidate"].delete(ref)
    with pytest.raises(txn.CommitError, match="singleton"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])
    world["candidate"].clear()


def test_e2e_inbound_shaper_round_trip(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    ref = Ref(kind="appliance/inbound-shaper", name="global", appliance="HUB1-EC")
    current = InboundShapers().normalize(InboundShapers().fetch(ctx, ref))
    assert current["interfaces"]["wan"]["if_shaping_enable"] is True
    assert current["interfaces"]["wan0"]["if_shaping_enable"] is False

    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


def test_e2e_inbound_shaper_apply_on_empty_appliance(world: dict[str, Any]) -> None:
    state = world["state"]
    ref = Ref(kind="appliance/inbound-shaper", name="global", appliance="BR2-EC")  # 5.NE

    world["candidate"].set_desired(
        ref,
        {"interfaces": {"wan": {"enable": True, "if_shaping_enable": True, "max_bw": 100000}}},
    )
    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["5.NE"]["inboundShapers"]["wan"]["max_bw"] == 100000

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.appliance_ecos["5.NE"]["inboundShapers"] == {}


def test_e2e_shaper_managed_by_uses_the_confirmed_shaper_section(
    world: dict[str, Any],
) -> None:
    state = world["state"]
    state.template_groups["WanStd"] = {"name": "WanStd", "templates": []}
    state.template_selection["WanStd"] = ["shaper", "dns"]
    state.template_association["3.NE"] = ["WanStd"]

    ctx = world["ctx"]
    assert (
        Shapers().managed_by(
            ctx, Ref(kind="appliance/shaper", name="global", appliance="BR1-EC")
        )
        == "template-group WanStd"
    )
    # The inbound resource claims the same confirmed section.
    assert (
        InboundShapers().managed_by(
            ctx, Ref(kind="appliance/inbound-shaper", name="global", appliance="BR1-EC")
        )
        == "template-group WanStd"
    )
    assert (
        Shapers().managed_by(
            ctx, Ref(kind="appliance/shaper", name="global", appliance="BR2-EC")
        )
        is None
    )


def test_e2e_shaper_managed_by_prefers_gms_marked(world: dict[str, Any]) -> None:
    state = world["state"]
    state.appliance_ecos["5.NE"]["shapers"] = {
        "wan": {"gms_marked": True, "max_bw": 100000, "traffic-class": {}}
    }
    ctx = world["ctx"]
    owner = Shapers().managed_by(
        ctx, Ref(kind="appliance/shaper", name="global", appliance="BR2-EC")
    )
    assert owner is not None
    assert "gms_marked" in owner


def test_e2e_list_refs_covers_every_appliance(world: dict[str, Any]) -> None:
    refs = InboundShapers().list_refs(world["ctx"])
    assert {r.appliance for r in refs} == {"HUB1-EC", "BR1-EC", "BR2-EC"}
