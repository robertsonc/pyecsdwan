"""DHCP server/relay resource (#13), composed over the shared deployment object.

Mirrors tests/test_deployment.py's structure (unit: normalize/idempotency/
respx write-path; e2e: candidate -> plan -> commit -> rollback against the
bundled mock), with the added read-modify-write shape this resource has:
apply()/rollback() re-GET the full deployment object fresh before splicing
in the desired dhcpd/dhcpFailover subtrees.
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
from pyecsdwan.contract import Ctx, Owned, Ref
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.dhcp import Dhcp

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins (incl. dhcp)
from pyecsdwan.mock.server import MockState, run_in_thread

BASE = "https://orch.example.com/gms/rest"
REF = Ref(kind="appliance/dhcp", name="dhcp", appliance="BR1-EC")

# Full raw deployment object (only the fields this resource cares about need
# be realistic; unrelated fields exist to prove they survive untouched).
RAW_DEPLOYMENT = {
    "scalars": {"maxWanBandwidth": 1000000},
    "sysConfig": {"mode": "router", "maxBW": 100000},
    "mgmtIfData": {"mgmt0": {"dhcp": False, "ip": "10.0.0.10"}},
    "modeIfs": [
        {
            "devNum": "rtr1",
            "ifName": "wan0",
            "applianceIPs": [
                {
                    "ip": "203.0.113.10",
                    "mask": "255.255.255.0",
                    "label": "INET1",
                    "dhcpd": {"type": "none", "server": {}, "relay": {}},
                }
            ],
        },
        {
            "devNum": "rtr1",
            "ifName": "lan0",
            "applianceIPs": [
                {
                    "ip": "10.1.1.1",
                    "mask": "255.255.255.0",
                    "label": "Data",
                    "dhcpd": {
                        "type": "server",
                        "server": {
                            "prefix": "10.1.1.0/24",
                            "ipStart": "10.1.1.100",
                            "ipEnd": "10.1.1.200",
                            "gw": ["10.1.1.1"],
                            "dns": ["10.1.1.1"],
                            "ntpd": [],
                            "netbios": [],
                            "netbiosNodeType": 0,
                            "maxLease": 86400,
                            "defaultLease": 43200,
                            "ip_range": {},
                            "options": {},
                            "host": {},
                            "failover": "",
                        },
                        "relay": {},
                    },
                }
            ],
        },
        # No dhcpd key at all on this IP entry — must be invisible to normalize().
        {
            "devNum": "rtr1",
            "ifName": "lan1",
            "applianceIPs": [{"ip": "10.1.2.1", "mask": "255.255.255.0", "label": "Voice"}],
        },
        # No IPs at all — must be invisible to normalize().
        {"devNum": "rtr1", "ifName": "wan1", "applianceIPs": []},
    ],
    "dpRoutes": [],
    "vifs": {"pppoe": []},
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


def test_normalize_keeps_only_dhcpd_bearing_interfaces():
    out = Dhcp().normalize(RAW_DEPLOYMENT)
    assert out is not None
    if_names = {iface["ifName"] for iface in out["modeIfs"]}
    # wan0 (type none) and lan0 (type server) both carry a dhcpd key.
    assert if_names == {"wan0", "lan0"}
    # lan1 (no dhcpd key) and wan1 (no IPs) are irrelevant here.
    assert "lan1" not in if_names
    assert "wan1" not in if_names


def test_normalize_strips_non_dhcp_ip_fields():
    out = Dhcp().normalize(RAW_DEPLOYMENT)
    assert out is not None
    lan0 = next(iface for iface in out["modeIfs"] if iface["ifName"] == "lan0")
    ip_entry = lan0["applianceIPs"][0]
    assert set(ip_entry) == {"ip", "dhcpd"}
    assert ip_entry["dhcpd"]["type"] == "server"
    assert ip_entry["dhcpd"]["server"]["ipStart"] == "10.1.1.100"


def test_normalize_is_idempotent():
    res = Dhcp()
    once = res.normalize(RAW_DEPLOYMENT)
    assert res.normalize(once) == once


def test_normalize_none_or_no_dhcp_bearing_ips_is_absent():
    res = Dhcp()
    assert res.normalize(None) is None
    assert res.normalize({}) is None
    no_dhcp = {"modeIfs": [{"devNum": "rtr1", "ifName": "wan1", "applianceIPs": []}]}
    assert res.normalize(no_dhcp) is None


def test_normalize_sorts_lists_for_order_independent_diff():
    """modeIfs may not come back in a stable server order; normalize() must
    still produce equal canonical states so a replan doesn't see phantom
    drift (the diff engine compares lists positionally)."""
    res = Dhcp()
    reordered = dict(RAW_DEPLOYMENT)
    reordered["modeIfs"] = list(reversed(RAW_DEPLOYMENT["modeIfs"]))
    current = res.normalize(RAW_DEPLOYMENT)
    desired = res.normalize(reordered)
    assert current == desired
    assert res.diff(REF, current, desired).empty


def test_list_refs_uses_resolver_appliances(settings):
    ctx = _ctx(settings)
    refs = Dhcp().list_refs(ctx)
    assert refs == [Ref(kind="appliance/dhcp", name="dhcp", appliance="BR1-EC")]


# -- unit: write path (respx, read-modify-write + validate-then-apply) ------------


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
def test_apply_reads_merges_validates_then_writes_then_saves(settings):
    get_route = respx.get(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment"}
    ).mock(return_value=httpx.Response(200, json=RAW_DEPLOYMENT))
    validate_route = respx.post(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment/validate"}
    ).mock(return_value=httpx.Response(200, json={"err": "", "rebootRequired": False}))
    write_route = respx.post(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment"}
    ).mock(return_value=httpx.Response(204))
    save_route = _keyed_save()

    res = Dhcp()
    ctx = _ctx(settings)
    current = res.normalize(RAW_DEPLOYMENT)
    assert current is not None
    desired = json.loads(json.dumps(current))
    lan0 = next(iface for iface in desired["modeIfs"] if iface["ifName"] == "lan0")
    lan0["applianceIPs"][0]["dhcpd"]["server"]["maxLease"] = 172800
    desired = res.normalize(desired)

    diff = res.diff(REF, current, desired)
    assert not diff.empty

    result = res.apply(ctx, diff)
    assert result.ok, result.message
    assert get_route.call_count == 1
    assert validate_route.call_count == 1
    assert write_route.call_count == 1
    assert save_route.call_count == 1

    body = json.loads(write_route.calls.last.request.content)
    # Only the touched interface's dhcpd changed; everything else survives
    # byte-for-byte from the freshly-read object, including 'scalars' (this
    # resource never strips it — it only ever splices dhcpd/dhcpFailover).
    assert body["scalars"] == RAW_DEPLOYMENT["scalars"]
    assert body["sysConfig"] == RAW_DEPLOYMENT["sysConfig"]
    assert body["mgmtIfData"] == RAW_DEPLOYMENT["mgmtIfData"]
    wan0 = next(i for i in body["modeIfs"] if i["ifName"] == "wan0")
    assert wan0["applianceIPs"][0]["dhcpd"]["type"] == "none"
    lan0_out = next(i for i in body["modeIfs"] if i["ifName"] == "lan0")
    assert lan0_out["applianceIPs"][0]["dhcpd"]["server"]["maxLease"] == 172800
    # Untouched IP fields on the modified entry are preserved too.
    assert lan0_out["applianceIPs"][0]["mask"] == "255.255.255.0"


@respx.mock
def test_apply_short_circuits_on_validate_err(settings):
    respx.get(BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment"}).mock(
        return_value=httpx.Response(200, json=RAW_DEPLOYMENT)
    )
    validate_route = respx.post(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment/validate"}
    ).mock(
        return_value=httpx.Response(
            200, json={"err": "mock validation failure", "rebootRequired": False}
        )
    )
    write_route = respx.post(
        BASE + "/appliance/rest", params={"nePk": "3.NE", "url": "deployment"}
    ).mock(return_value=httpx.Response(204))
    save_route = _keyed_save()

    res = Dhcp()
    ctx = _ctx(settings)
    current = res.normalize(RAW_DEPLOYMENT)
    assert current is not None
    desired = json.loads(json.dumps(current))
    lan0 = next(iface for iface in desired["modeIfs"] if iface["ifName"] == "lan0")
    lan0["applianceIPs"][0]["dhcpd"]["type"] = "none"
    desired = res.normalize(desired)
    diff = res.diff(REF, current, desired)

    result = res.apply(ctx, diff)
    assert not result.ok
    assert "mock validation failure" in result.message
    assert validate_route.call_count == 1
    assert write_route.call_count == 0
    assert save_route.call_count == 0


def test_rollback_refuses_on_absent_snapshot(settings):
    result = Dhcp().rollback(_ctx(settings), REF, None)
    assert not result.ok
    assert "refusing" in result.message


@respx.mock
def test_managed_by_delegates_to_ownership_with_resolved_ne_pk(settings):
    respx.get(BASE + "/template/applianceAssociation", params={"nePk": "3.NE"}).mock(
        return_value=httpx.Response(200, json={"templateIds": ["Branch-Std"]})
    )
    respx.get(BASE + "/template/templateSelection", params={"templateGroup": "Branch-Std"}).mock(
        return_value=httpx.Response(200, json=["dhcpd"])
    )
    owns = Dhcp().managed_by(_ctx(settings), REF)
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group Branch-Std"


@respx.mock
def test_managed_by_unknown_when_the_group_selects_something_else(settings):
    respx.get(BASE + "/template/applianceAssociation", params={"nePk": "3.NE"}).mock(
        return_value=httpx.Response(200, json={"templateIds": ["Branch-Std"]})
    )
    respx.get(BASE + "/template/templateSelection", params={"templateGroup": "Branch-Std"}).mock(
        return_value=httpx.Response(200, json=["securityMaps"])
    )
    # "dhcpd"/"dhcpFailover" are guessed section names (#20): a group that
    # selects neither has not established that no template owns DHCP.
    owns = Dhcp().managed_by(_ctx(settings), REF)
    assert owns.state is Owned.UNKNOWN
    assert owns.blocks_write


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


E2E_REF = Ref(kind="appliance/dhcp", name="dhcp", appliance="BR1-EC")


def test_e2e_change_idempotency_and_rollback(e2e_world: dict[str, Any]) -> None:
    """The mock seeds a DHCP server on lan1 (10.1.2.1) — change its maxLease,
    commit, replan (empty), then roll back and confirm the original value is
    restored, without disturbing unrelated deployment fields."""
    world = e2e_world
    state = world["state"]
    candidate: CandidateStore = world["candidate"]

    res = Dhcp()
    # GET on a never-written key always returns a fresh _seed_deployment()
    # (the mock doesn't persist the seed into its store on a bare read).
    from pyecsdwan.mock.server import _seed_deployment

    current = res.normalize(_seed_deployment())
    assert current is not None
    lan1 = next(i for i in current["modeIfs"] if i["ifName"] == "lan1")
    assert lan1["applianceIPs"][0]["dhcpd"]["server"]["maxLease"] == 86400

    candidate.set_path(
        E2E_REF,
        ["modeIfs"],
        [
            {
                "devNum": iface["devNum"],
                "ifName": iface["ifName"],
                "applianceIPs": [
                    {
                        "ip": ip["ip"],
                        "dhcpd": (
                            {**ip["dhcpd"], "server": {**ip["dhcpd"]["server"], "maxLease": 172800}}
                            if iface["ifName"] == "lan1"
                            else ip["dhcpd"]
                        ),
                    }
                    for ip in iface["applianceIPs"]
                ],
            }
            for iface in current["modeIfs"]
        ],
    )
    report = _commit(world)
    assert report.ok, report.messages

    written = state.appliance_ecos["3.NE"]["deployment"]
    written_lan1 = next(i for i in written["modeIfs"] if i["ifName"] == "lan1")
    assert written_lan1["applianceIPs"][0]["dhcpd"]["server"]["maxLease"] == 172800
    # Unrelated fields/interfaces are untouched by the dhcp-scoped write.
    assert written["sysConfig"]["maxBW"] == 100000
    written_wan0 = next(i for i in written["modeIfs"] if i["ifName"] == "wan0")
    assert written_wan0["applianceIPs"][0]["ip"] == "203.0.113.10"
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
    saves_after_apply = len(state.actions)

    # Idempotency: replanning the same intent is an empty plan, no new writes.
    assert _plan_is_empty(world)
    assert len(state.actions) == saves_after_apply

    # rollback <1> restores the pre-change value AND persists it.
    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    restored = state.appliance_ecos["3.NE"]["deployment"]
    restored_lan1 = next(i for i in restored["modeIfs"] if i["ifName"] == "lan1")
    assert restored_lan1["applianceIPs"][0]["dhcpd"]["server"]["maxLease"] == 86400
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
    assert len(state.actions) == saves_after_apply + 1


def test_e2e_apply_does_not_clobber_unrelated_deployment_fields(
    e2e_world: dict[str, Any],
) -> None:
    """A DHCP-only change must leave sysConfig, mgmtIfData and every other
    interface's non-dhcpd fields exactly as the live appliance had them —
    even fields this resource never reads (proving the read-modify-write
    re-reads fresh rather than reconstructing a synthetic object)."""
    world = e2e_world
    state = world["state"]
    candidate: CandidateStore = world["candidate"]

    res = Dhcp()
    from pyecsdwan.mock.server import _seed_deployment

    before = _seed_deployment()
    # Simulate a concurrent, unrelated field change made directly on the
    # mock's store (as if appliance/deployment or another actor wrote it)
    # before this resource's apply() does its fresh GET.
    state.appliance_ecos.setdefault("3.NE", {})["deployment"] = {
        **before,
        "sysConfig": {**before["sysConfig"], "maxBW": 999999},
    }

    current = res.normalize(before)
    assert current is not None
    lan1 = next(i for i in current["modeIfs"] if i["ifName"] == "lan1")
    candidate.set_path(
        E2E_REF,
        ["modeIfs"],
        [
            {
                "devNum": iface["devNum"],
                "ifName": iface["ifName"],
                "applianceIPs": [
                    {
                        "ip": ip["ip"],
                        "dhcpd": (
                            {**ip["dhcpd"], "type": "none", "server": {}, "relay": {}}
                            if iface["ifName"] == "lan1"
                            else ip["dhcpd"]
                        ),
                    }
                    for ip in iface["applianceIPs"]
                ],
            }
            for iface in current["modeIfs"]
        ],
    )
    report = _commit(world)
    assert report.ok, report.messages

    written = state.appliance_ecos["3.NE"]["deployment"]
    written_lan1 = next(i for i in written["modeIfs"] if i["ifName"] == "lan1")
    assert written_lan1["applianceIPs"][0]["dhcpd"]["type"] == "none"
    # The concurrent sysConfig.maxBW change survives — proof apply() read the
    # deployment object fresh rather than writing back a stale snapshot.
    assert written["sysConfig"]["maxBW"] == 999999
    assert lan1["applianceIPs"][0]["ip"] == "10.1.2.1"


def test_e2e_validate_failure_blocks_apply_and_auto_reverts(e2e_world: dict[str, Any]) -> None:
    world = e2e_world
    state = world["state"]
    candidate: CandidateStore = world["candidate"]

    res = Dhcp()
    from pyecsdwan.mock.server import _seed_deployment

    before = _seed_deployment()
    current = res.normalize(before)
    assert current is not None
    candidate.set_path(
        E2E_REF,
        ["modeIfs"],
        [
            {
                "devNum": iface["devNum"],
                "ifName": iface["ifName"],
                "applianceIPs": [
                    {
                        "ip": ip["ip"],
                        "dhcpd": (
                            {**ip["dhcpd"], "type": "none", "server": {}, "relay": {}}
                            if iface["ifName"] == "lan1"
                            else ip["dhcpd"]
                        ),
                    }
                    for ip in iface["applianceIPs"]
                ],
            }
            for iface in current["modeIfs"]
        ],
    )
    state.deployment_fail_validate = True  # consumed by the one validate call inside apply()

    report = _commit(world)
    assert not report.ok
    assert not state.deployment_fail_validate  # consumed; the revert's validate call succeeded
    written = state.appliance_ecos["3.NE"]["deployment"]
    written_lan1 = next(i for i in written["modeIfs"] if i["ifName"] == "lan1")
    assert written_lan1["applianceIPs"][0]["dhcpd"]["type"] == "server"  # original, unrejected
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
