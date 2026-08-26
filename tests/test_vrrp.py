"""Unit + e2e tests for the appliance/vrrp resource (Phase 2, #14).

Unit half (mirrors tests/test_zones.py): normalize() strips server-reported
state and sorts by groupId, validation of the required/ranged fields, and
that raw-server-shape vs. user-desired-shape diff cleanly (no phantom
drift). E2e half (mirrors tests/test_zones_e2e.py / tests/test_save_changes.py's
#12+ pattern): idempotent round-trip, apply persisting via save-changes,
rollback, and managed_by()/ownership wiring — all against the bundled mock.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref, Reversibility
from pyecsdwan.registry import default_registry
from pyecsdwan.resources.vrrp import Vrrp

BASE = "https://orch.example.com/gms/rest"
APPLIANCE_URL = f"{BASE}/appliance/rest"
REF = Ref(kind="appliance/vrrp", name="global", appliance="BR1-EC")


def _ctx(settings, resolver=None):
    client = OrchClient(settings)
    return Ctx(client=client, resolver=resolver if resolver is not None else _StubResolver())


class _StubResolver:
    """Resolves any appliance name straight through to a canned nePk."""

    def ne_pk_for(self, name: str) -> str:
        return {"BR1-EC": "3.NE", "HUB1-EC": "1.NE"}.get(name, name)


_MASTER = {
    "pkt_trace": False,
    "adv_timer": 1,
    "preempt": True,
    "holddown": 10,
    "auth": "",
    "desc": "HQ-Branch1 HA",
    "enable": "Up",
    "priority": 200,
    "vipaddr": "10.0.0.1",
    "interface": "wan0",
    "groupId": 1,
}

_STATE_FIELDS = {
    "mode": "Master",
    "master_transitions": 3,
    "uptime": "12 days 4 hrs 0 mins 0 secs",
    "vmac": "00-00-5E-00-01-01",
    "priorityState": 200,
    "masterip": "10.0.0.2",
    "vipowner": False,
}


# -- reversibility class -------------------------------------------------------


def test_reversibility_is_reversible():
    assert Vrrp().reversibility is Reversibility.REVERSIBLE
    assert Vrrp().kind == "appliance/vrrp"


# -- normalize: strips state, sorts by groupId, idempotent --------------------


def test_normalize_none_yields_absent():
    assert Vrrp().normalize(None) is None


def test_normalize_empty_list_and_empty_dict_are_both_absent():
    # Same canonical value as delete's desired state (None), not {"vrrp": []}
    # — see the module docstring on why that equivalence matters for verify().
    assert Vrrp().normalize([]) is None
    assert Vrrp().normalize({}) is None


def test_normalize_strips_server_reported_state_fields():
    once = Vrrp().normalize([{**_MASTER, **_STATE_FIELDS}])
    assert once == {"vrrp": [_MASTER]}
    for state_field in _STATE_FIELDS:
        assert state_field not in once["vrrp"][0]


def test_normalize_sorts_by_group_id():
    raw = [
        {**_MASTER, "groupId": 5, "interface": "wan1", "vipaddr": "10.0.0.5"},
        {**_MASTER, "groupId": 1},
        {**_MASTER, "groupId": 3, "interface": "wan2", "vipaddr": "10.0.0.3"},
    ]
    once = Vrrp().normalize(raw)
    assert [e["groupId"] for e in once["vrrp"]] == [1, 3, 5]


def test_normalize_is_idempotent():
    res = Vrrp()
    raw = [{**_MASTER, **_STATE_FIELDS}]
    once = res.normalize(raw)
    assert res.normalize(once) == once
    # normalize() must also accept its own wrapped shape (canonicalize_desired
    # round-trips user intent through it), not just the bare fetch()-list shape.
    assert res.normalize(once["vrrp"]) == once


def test_normalize_fills_defaults_for_omitted_optional_fields():
    minimal = {"groupId": 1, "interface": "wan0", "vipaddr": "10.0.0.1"}
    once = Vrrp().normalize([minimal])
    assert once["vrrp"][0] == {
        "groupId": 1,
        "interface": "wan0",
        "vipaddr": "10.0.0.1",
        "enable": "Down",
        "priority": 100,
        "adv_timer": 1,
        "preempt": True,
        "holddown": 10,
        "auth": "",
        "desc": "",
        "pkt_trace": False,
    }


@pytest.mark.parametrize("missing", ["groupId", "interface", "vipaddr"])
def test_normalize_rejects_missing_required_fields(missing):
    entry = {**_MASTER}
    del entry[missing]
    with pytest.raises(ValueError, match=missing):
        Vrrp().normalize([entry])


@pytest.mark.parametrize("bad_id", [0, 256, -1])
def test_normalize_rejects_out_of_range_group_id(bad_id):
    with pytest.raises(ValueError, match="groupId"):
        Vrrp().normalize([{**_MASTER, "groupId": bad_id}])


def test_normalize_rejects_bad_enable_value():
    with pytest.raises(ValueError, match="enable"):
        Vrrp().normalize([{**_MASTER, "enable": "maybe"}])


def test_normalize_rejects_duplicate_group_ids():
    with pytest.raises(ValueError, match="duplicate"):
        Vrrp().normalize([_MASTER, {**_MASTER, "interface": "wan1", "vipaddr": "10.0.0.9"}])


def test_normalize_rejects_auth_over_8_chars():
    with pytest.raises(ValueError, match="auth"):
        Vrrp().normalize([{**_MASTER, "auth": "waytoolongpassphrase"}])


def test_normalize_rejects_non_list_payload():
    with pytest.raises(ValueError):
        Vrrp().normalize("not-a-list")  # type: ignore[arg-type]


def test_normalize_rejects_non_mapping_entry():
    with pytest.raises(ValueError, match="mapping"):
        Vrrp().normalize(["not-a-mapping"])


# -- no phantom drift between raw-server shape and user-authored shape --------


def test_no_phantom_drift_server_raw_vs_user_intent():
    res = Vrrp()
    server_raw = [{**_MASTER, **_STATE_FIELDS}]
    user_intent = {"vrrp": [{**_MASTER, "priority": "200", "groupId": "1"}]}
    current = res.normalize(server_raw)
    desired = res.normalize(user_intent)
    assert res.diff(REF, current, desired).empty


# -- apply / rollback: proxy write + one batched save-changes -----------------


@respx.mock
def test_apply_posts_full_list_then_saves(settings):
    proxy_route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    save_route = respx.post(f"{BASE}/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": "k1"})
    )
    status_route = respx.get(f"{BASE}/action/status").mock(
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
                    "result": "ok",
                }
            ],
        )
    )
    res = Vrrp()
    ctx = _ctx(settings)
    current = res.normalize(None)
    desired = res.normalize([_MASTER])

    result = res.apply(ctx, res.diff(REF, current, desired))
    assert result.ok, result.message
    assert "persisted" in result.message

    request = proxy_route.calls.last.request
    assert request.url.params["nePk"] == "3.NE"
    assert request.url.params["url"] == "vrrp"
    import json as _json

    assert _json.loads(request.content) == [_MASTER]
    assert save_route.call_count == 1
    assert status_route.call_count == 1


@respx.mock
def test_apply_is_noop_on_empty_diff(settings):
    proxy_route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    res = Vrrp()
    state = res.normalize([_MASTER])

    result = res.apply(_ctx(settings), res.diff(REF, state, state))
    assert result.ok
    assert result.changed is False
    assert proxy_route.call_count == 0


@respx.mock
def test_apply_reports_failure_when_save_fails(settings):
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
    res = Vrrp()
    result = res.apply(_ctx(settings), res.diff(REF, res.normalize(None), res.normalize([_MASTER])))
    assert not result.ok
    assert "not persisted" in result.message


@respx.mock
def test_rollback_restores_snapshot_then_saves(settings):
    proxy_route = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
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
                    "result": "ok",
                }
            ],
        )
    )
    result = Vrrp().rollback(_ctx(settings), REF, [_MASTER])
    assert result.ok, result.message
    import json as _json

    assert _json.loads(proxy_route.calls.last.request.content) == [_MASTER]


# -- e2e: idempotent round-trip + rollback + save-changes, via the mock ------


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


def _appliance(state: Any, ne_pk: str) -> dict[str, Any]:
    return next(a for a in state.appliances if a["nePk"] == ne_pk)


def test_e2e_idempotent_round_trip_against_seeded_state(world: dict[str, Any]) -> None:
    # 1.NE (HUB1-EC) is seeded with a single VRRP master instance (groupId 1)
    # carrying server-reported state fields; re-planning the fetched-and-
    # normalized state as desired must diff empty.
    ctx = world["ctx"]
    ref = Ref(kind="appliance/vrrp", name="global", appliance="HUB1-EC")
    current = Vrrp().normalize(Vrrp().fetch(ctx, ref))
    assert current["vrrp"][0]["groupId"] == 1
    assert "mode" not in current["vrrp"][0]

    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


def test_e2e_apply_persists_idempotent_replan_then_rollback(world: dict[str, Any]) -> None:
    state = world["state"]
    candidate = world["candidate"]
    ref = Ref(kind="appliance/vrrp", name="global", appliance="BR2-EC")  # 5.NE, seeded empty

    desired = {"vrrp": [_MASTER]}
    candidate.set_desired(ref, desired)
    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["5.NE"]["vrrp"] == [_MASTER]
    # apply() saved: the change is persisted, not just running config.
    assert _appliance(state, "5.NE")["hasUnsavedChanges"] is False
    saves_after_apply = len(state.actions)

    # Idempotency: replanning identical intent yields an empty plan and does
    # not touch the mock's action log (no extra save).
    candidate.set_desired(ref, desired)
    assert _plan_is_empty(world)
    assert len(state.actions) == saves_after_apply

    # rollback <1> restores the pre-change (empty) state and persists it.
    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.appliance_ecos["5.NE"]["vrrp"] == []
    assert _appliance(state, "5.NE")["hasUnsavedChanges"] is False
    assert len(state.actions) == saves_after_apply + 1


def test_e2e_delete_wipes_table_and_is_reversible(world: dict[str, Any]) -> None:
    # 3.NE (BR1-EC) is seeded with one instance; deleting the whole resource
    # (desired None) is a legitimate, reversible "no VRRP configured" state
    # for this resource (unlike zones/interface-labels' server-managed rows).
    state = world["state"]
    candidate = world["candidate"]
    ref = Ref(kind="appliance/vrrp", name="global", appliance="BR1-EC")

    candidate.delete(ref)
    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["3.NE"]["vrrp"] == []

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert len(state.appliance_ecos["3.NE"]["vrrp"]) == 1
    assert state.appliance_ecos["3.NE"]["vrrp"][0]["groupId"] == 1


def test_e2e_managed_by_ownership_join(world: dict[str, Any]) -> None:
    state = world["state"]
    state.template_groups["NetStd"] = {"name": "NetStd", "templates": []}
    state.template_selection["NetStd"] = ["vrrp", "dns"]
    state.template_association["3.NE"] = ["NetStd"]

    ctx = world["ctx"]
    ref = Ref(kind="appliance/vrrp", name="global", appliance="BR1-EC")
    assert Vrrp().managed_by(ctx, ref) == "template-group NetStd"

    other_ref = Ref(kind="appliance/vrrp", name="global", appliance="BR2-EC")  # 5.NE, unassociated
    assert Vrrp().managed_by(ctx, other_ref) is None
