"""Interfaces / IP addressing / deployment resource (#12).

Mirrors tests/test_zones.py (unit: normalize/idempotency/respx write-path)
and tests/test_zones_e2e.py + tests/test_save_changes.py's #12+ pattern
section (e2e: candidate -> plan -> commit -> rollback against the bundled
mock, proving validate-then-apply and save-changes composition end to end).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.deployment import Deployment

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins (incl. deployment)
from pyecsdwan.mock.server import MockState, run_in_thread

BASE = "https://orch.example.com/gms/rest"
REF = Ref(kind="appliance/deployment", name="deployment", appliance="BR1-EC")

RAW = {
    "scalars": {"maxWanBandwidth": 1000000, "num1GigPorts": 8},
    "sysConfig": {
        "mode": "router",
        "maxBW": 100000,
        "ifLabels": {"lan": ["Data"], "wan": ["INET1"]},
    },
    "mgmtIfData": {"mgmt0": {"dhcp": False, "ip": "10.0.0.10", "mask": "255.255.255.0"}},
    "modeIfs": [
        {
            "devNum": "rtr1",
            "ifName": "wan0",
            "applianceIPs": [{"ip": "203.0.113.10", "mask": "255.255.255.0", "label": "INET1"}],
        },
        {
            "devNum": "rtr1",
            "ifName": "lan0",
            "applianceIPs": [{"ip": "10.1.1.1", "mask": "255.255.255.0", "label": "Data"}],
        },
    ],
    "dpRoutes": [],
    # Real top-level key the issue text didn't mention — must pass through.
    "vifs": {"pppoe": [], "bondedIfs": []},
    "dhcpFailover": {},
}


class _StubResolver:
    """Minimal resolver stand-in: name -> nePk, for unit tests without a live inventory."""

    def __init__(self, mapping: dict[str, str]):
        self._map = mapping

    def ne_pk_for(self, name: str) -> str:
        return self._map[name]

    def appliances(self) -> list[dict[str, Any]]:
        return [{"hostName": name, "nePk": pk} for name, pk in self._map.items()]


def _ctx(settings: config.Settings) -> Ctx:
    return Ctx(client=OrchClient(settings), resolver=_StubResolver({"BR1-EC": "3.NE"}))


# -- unit: normalize() -----------------------------------------------------------


def test_normalize_strips_scalars():
    out = Deployment().normalize(RAW)
    assert out is not None
    assert "scalars" not in out


def test_normalize_keeps_real_config_and_passes_through_unknown_vifs():
    out = Deployment().normalize(RAW)
    assert out["sysConfig"]["maxBW"] == 100000
    assert out["mgmtIfData"]["mgmt0"]["ip"] == "10.0.0.10"
    # vifs is real, live-captured top-level config the issue text didn't
    # mention — pass through unmodified, never dropped.
    assert out["vifs"] == {"pppoe": [], "bondedIfs": []}
    assert out["dhcpFailover"] == {}


def test_normalize_is_idempotent():
    res = Deployment()
    once = res.normalize(RAW)
    assert res.normalize(once) == once


def test_normalize_none_or_scalars_only_is_absent():
    res = Deployment()
    assert res.normalize(None) is None
    assert res.normalize({}) is None
    assert res.normalize({"scalars": {"x": 1}}) is None


def test_normalize_sorts_lists_for_order_independent_diff():
    """modeIfs (and nested applianceIPs) may not come back in a stable server
    order; normalize() must still produce equal canonical states so a replan
    doesn't see phantom drift (the diff engine compares lists positionally)."""
    res = Deployment()
    reordered = dict(RAW)
    reordered["modeIfs"] = list(reversed(RAW["modeIfs"]))
    current = res.normalize(RAW)
    desired = res.normalize(reordered)
    assert current == desired
    assert res.diff(REF, current, desired).empty


def test_list_refs_uses_resolver_appliances(settings):
    ctx = _ctx(settings)
    refs = Deployment().list_refs(ctx)
    assert refs == [Ref(kind="appliance/deployment", name="deployment", appliance="BR1-EC")]


# -- unit: write path (respx, validate-then-apply) --------------------------------


def _keyed_save(key: str = "save-1", ne_pk: str = "3.NE") -> Any:
    """A save-changes that returns a client key, and its terminal record.

    These tests used to answer `saveChanges` with a bare `{}` and rely on the
    keyless branch reporting SUCCESS unawaited — a shortcut that saved two
    lines of mocking here and, in `jobs.save_changes`, made "we could not
    check" indistinguishable from "it persisted" (#64). Keyless is off-spec
    for Orchestrator 9.3+ anyway; a test about deployment payload shapes
    should not be exercising the off-spec branch.
    """
    respx.get(BASE + "/action/status").mock(
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
    return respx.post(BASE + "/appliance/saveChanges").mock(
        return_value=httpx.Response(200, json={"clientKey": key})
    )

@respx.mock
def test_apply_validates_then_writes_then_saves(settings):
    validate_route = respx.post(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment/validate"}
    ).mock(return_value=httpx.Response(200, json={"err": "", "rebootRequired": False}))
    write_route = respx.post(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment"}
    ).mock(return_value=httpx.Response(204))
    save_route = _keyed_save()

    res = Deployment()
    ctx = _ctx(settings)
    desired = res.normalize(RAW)
    diff = res.diff(REF, None, desired)
    assert not diff.empty

    result = res.apply(ctx, diff)
    assert result.ok, result.message
    assert validate_route.call_count == 1
    assert write_route.call_count == 1
    assert save_route.call_count == 1
    # validate must land before the write (validate-then-apply ordering).
    assert validate_route.calls.last.request.url.params["url"] == "deployment/validate"
    body = json.loads(write_route.calls.last.request.content)
    assert body == desired
    assert "scalars" not in body


@respx.mock
def test_apply_short_circuits_on_validate_err(settings):
    validate_route = respx.post(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment/validate"}
    ).mock(
        return_value=httpx.Response(
            200,
            json={"err": "vlan 999 conflicts with WAN uplink", "rebootRequired": False},
        )
    )
    write_route = respx.post(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment"}
    ).mock(return_value=httpx.Response(204))
    save_route = _keyed_save()

    res = Deployment()
    ctx = _ctx(settings)
    desired = res.normalize(RAW)
    diff = res.diff(REF, None, desired)

    result = res.apply(ctx, diff)
    assert not result.ok
    assert "vlan 999 conflicts with WAN uplink" in result.message
    assert validate_route.call_count == 1
    assert write_route.call_count == 0  # rejected candidate must never be written
    assert save_route.call_count == 0  # ...and never persisted


@respx.mock
def test_rollback_writes_normalized_snapshot_and_notes_reboot_required(settings):
    respx.post(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment/validate"}
    ).mock(return_value=httpx.Response(200, json={"err": "", "rebootRequired": True}))
    write_route = respx.post(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment"}
    ).mock(return_value=httpx.Response(204))
    _keyed_save()

    result = Deployment().rollback(_ctx(settings), REF, RAW)
    assert result.ok, result.message
    assert "reboot" in result.message.lower()
    body = json.loads(write_route.calls.last.request.content)
    assert "scalars" not in body
    assert body["sysConfig"]["mode"] == "router"


def test_rollback_refuses_on_absent_snapshot(settings):
    result = Deployment().rollback(_ctx(settings), REF, None)
    assert not result.ok
    assert "refusing" in result.message


@respx.mock
def test_managed_by_delegates_to_ownership_with_resolved_ne_pk(settings):
    respx.get(BASE + "/template/applianceAssociation", params={"nePk": "3.NE"}).mock(
        return_value=httpx.Response(200, json={"templateIds": ["Branch-Std"]})
    )
    respx.get(BASE + "/template/templateSelection", params={"templateGroup": "Branch-Std"}).mock(
        return_value=httpx.Response(200, json=["deployment"])
    )
    owner = Deployment().managed_by(_ctx(settings), REF)
    assert owner == "template-group Branch-Std"


@respx.mock
def test_managed_by_none_when_no_group_selects_the_section(settings):
    respx.get(BASE + "/template/applianceAssociation", params={"nePk": "3.NE"}).mock(
        return_value=httpx.Response(200, json={"templateIds": ["Branch-Std"]})
    )
    respx.get(BASE + "/template/templateSelection", params={"templateGroup": "Branch-Std"}).mock(
        return_value=httpx.Response(200, json=["securityMaps"])
    )
    assert Deployment().managed_by(_ctx(settings), REF) is None


# -- e2e: through txn against the bundled mock -------------------------------------


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def e2e_world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
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


def _appliance(state: MockState, ne_pk: str) -> dict[str, Any]:
    return next(a for a in state.appliances if a["nePk"] == ne_pk)


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


def test_e2e_change_idempotency_and_rollback(e2e_world: dict[str, Any]) -> None:
    world = e2e_world
    state = world["state"]
    candidate: CandidateStore = world["candidate"]

    # Merge-mode set over the seeded fixture (100000 -> 200000).
    candidate.set_path(REF, ["sysConfig", "maxBW"], 200000)
    report = _commit(world)
    assert report.ok, report.messages
    written = state.appliance_ecos["3.NE"]["deployment"]
    assert written["sysConfig"]["maxBW"] == 200000
    assert "scalars" not in written  # read-only fields never re-posted
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
    saves_after_apply = len(state.actions)

    # Idempotency: replanning the same intent is an empty plan, no new writes.
    candidate.set_path(REF, ["sysConfig", "maxBW"], 200000)
    assert _plan_is_empty(world)
    assert len(state.actions) == saves_after_apply

    # rollback <1> restores the pre-change value AND persists it.
    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.appliance_ecos["3.NE"]["deployment"]["sysConfig"]["maxBW"] == 100000
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
    assert len(state.actions) == saves_after_apply + 1


def test_e2e_validate_failure_blocks_apply_and_auto_reverts(e2e_world: dict[str, Any]) -> None:
    """The candidate's forward change is rejected by validate before any
    write; the txn engine still runs its normal auto-revert of the failed
    step (a validate-then-apply write is idempotent to repeat), landing back
    on exactly the original value rather than the rejected one."""
    world = e2e_world
    state = world["state"]
    candidate: CandidateStore = world["candidate"]

    candidate.set_path(REF, ["sysConfig", "maxBW"], 300000)
    state.deployment_fail_validate = True  # consumed by the one validate call inside apply()

    report = _commit(world)
    assert not report.ok
    assert not state.deployment_fail_validate  # consumed; the revert's validate call succeeded
    written = state.appliance_ecos["3.NE"]["deployment"]
    assert written["sysConfig"]["maxBW"] == 100000  # original value, never the rejected 300000
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False


def test_e2e_failed_save_fails_commit_and_reverts(e2e_world: dict[str, Any]) -> None:
    world = e2e_world
    state = world["state"]
    candidate: CandidateStore = world["candidate"]

    candidate.set_path(REF, ["sysConfig", "maxBW"], 250000)
    state.fail_next_action = True  # the apply's save-changes call fails

    report = _commit(world)
    assert not report.ok
    # Running config restored from the pre-change snapshot, and the restore persisted.
    written = state.appliance_ecos["3.NE"]["deployment"]
    assert written["sysConfig"]["maxBW"] == 100000
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
