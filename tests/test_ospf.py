"""Unit + e2e tests for the appliance/ospf resource (issue #17, Stage 2: read+write).

Mirrors tests/test_bgp.py closely — see its module docstring for the shared
design rationale (real apply()/rollback() write path exercised through the
transaction engine end-to-end, fetch/normalize via the appliance proxy).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Owned, Ref, Reversibility, Scope, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.ospf import Ospf

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

REF = Ref(kind="appliance/ospf", name="config", appliance="BR1-EC")
NE_PK = "3.NE"  # BR1-EC — seeded with a real router-id and two interfaces
OTHER_REF = Ref(kind="appliance/ospf", name="config", appliance="HUB1-EC")

_SYSTEM_SAMPLE = {
    "redistMapToOSPF": "default_rtmap_to_ospf",
    "enable": False,
    "routerId": "192.168.255.13",
    "opaque_enable": True,
}
_DISABLED_SYSTEM_SAMPLE = {
    "redistMapToOSPF": "default_rtmap_to_ospf",
    "enable": False,
    "routerId": "0.0.0.0",
    "opaque_enable": True,
}
_INTERFACES_SAMPLE = {
    "lan0": {
        "cost": 1,
        "area": "0.0.0.0",
        "authKey": "",
        "md5Password": "",
        "authType": "None",
        "comment": "",
        "priority": 1,
        "transmitDelay": 1,
        "retransmitInterval": 4,
        "helloInterval": 10,
        "deadInterval": 40,
        "md5Key": 0,
        "adminStatus": True,
        "bfdDesired": False,
    },
    "lan1": {
        "cost": 1,
        "area": "1.0.0.0",
        "authKey": "",
        "md5Password": "",
        "authType": "None",
        "comment": "",
        "priority": 1,
        "transmitDelay": 1,
        "retransmitInterval": 4,
        "helloInterval": 10,
        "deadInterval": 40,
        "md5Key": 0,
        "adminStatus": True,
        "bfdDesired": False,
    },
}


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
    candidate = CandidateStore(settings.host)
    return {
        "ctx": ctx, "settings": settings, "state": state, "client": client,
        "candidate": candidate,
    }


def _commit(world: dict[str, Any], **kwargs: Any) -> txn.CommitReport:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    report = txn.commit(world["ctx"], default_registry, plan, world["settings"], **kwargs)
    if report.ok:
        world["candidate"].clear()
    return report


# -- registration + contract-level shape -------------------------------------


def test_registered_as_appliance_scope_curated_reversible():
    res = default_registry.get("appliance/ospf")
    assert res.scope is Scope.APPLIANCE
    assert res.tier is Tier.CURATED
    assert res.reversibility is Reversibility.REVERSIBLE
    assert res.deletable is False


# -- fetch + normalize round-trip against the mock (via the appliance proxy) --


def test_fetch_normalize_round_trips_against_mock(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Ospf()

    raw = res.fetch(ctx, REF)
    assert raw == {"system": _SYSTEM_SAMPLE, "interfaces": _INTERFACES_SAMPLE}

    canonical = res.normalize(raw)
    assert canonical == {
        "system": dict(sorted(_SYSTEM_SAMPLE.items())),
        "interfaces": _INTERFACES_SAMPLE,
    }


def test_fetch_isolates_per_appliance_ecos_store(world: dict[str, Any]) -> None:
    ctx, state = world["ctx"], world["state"]
    state.appliance_ecos[NE_PK]["ospf/config/system"] = {
        **_SYSTEM_SAMPLE, "routerId": "10.0.0.1", "enable": True,
    }
    state.appliance_ecos[NE_PK]["ospf/config/interfaces"] = {"ge1": {"area": "0.0.0.0", "cost": 10}}

    raw = Ospf().fetch(ctx, REF)
    assert raw["system"]["routerId"] == "10.0.0.1"
    assert raw["interfaces"] == {"ge1": {"area": "0.0.0.0", "cost": 10}}

    other_raw = Ospf().fetch(ctx, OTHER_REF)
    assert other_raw["system"] == _DISABLED_SYSTEM_SAMPLE
    assert other_raw["interfaces"] == {}


def test_diff_empty_when_desired_matches_fetched(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Ospf()
    current = res.normalize(res.fetch(ctx, REF))
    desired = res.canonicalize_desired(ctx, REF, current)
    assert res.diff(REF, current, desired).empty


# -- normalize: idempotent, tolerant of unknown fields and interface shapes ---


def test_normalize_is_idempotent():
    res = Ospf()
    raw = {
        "system": {**_SYSTEM_SAMPLE, "routerId": "10.0.0.1", "vendorExtra": "x"},
        "interfaces": {"ge1": {"area": "0.0.0.0"}, "ge2": {"area": "0.0.0.1"}},
    }
    once = res.normalize(raw)
    assert res.normalize(once) == once


def test_normalize_none_and_empty_raw_are_absent():
    res = Ospf()
    assert res.normalize(None) is None
    assert res.normalize({}) is None


def test_normalize_accepts_interface_list_shape_defensively():
    """Defensive insurance kept from Stage 1: normalize() must not crash if a
    future server version returns a list instead of the confirmed
    keyed-by-interface-name mapping."""
    res = Ospf()
    raw = {
        "system": _DISABLED_SYSTEM_SAMPLE,
        "interfaces": [
            {"ifName": "ge1", "area": "0.0.0.0"},
            {"area": "0.0.0.1"},  # no recognizable key field -> falls back to index
        ],
    }
    canonical = res.normalize(raw)
    assert canonical is not None
    interfaces = canonical["interfaces"]
    assert interfaces["ge1"] == {"ifName": "ge1", "area": "0.0.0.0"}
    assert interfaces["1"] == {"area": "0.0.0.1"}
    # Idempotent even after the list->dict conversion.
    assert res.normalize(canonical) == canonical


def test_normalize_rejects_non_mapping_system():
    with pytest.raises(ValueError, match="mapping"):
        Ospf().normalize({"system": "not-a-mapping", "interfaces": {}})


def test_normalize_rejects_non_mapping_interface_entry():
    with pytest.raises(ValueError, match="mapping"):
        Ospf().normalize({"system": {}, "interfaces": {"ge1": "not-a-mapping"}})


# -- managed_by ----------------------------------------------------------------


def test_managed_by_unowned_when_no_group_is_associated(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    assert Ospf().managed_by(ctx, REF).state is Owned.UNOWNED


def test_managed_by_reports_owning_template_group(world: dict[str, Any]) -> None:
    ctx, state = world["ctx"], world["state"]
    state.template_groups["Default Template Group"] = {"name": "Default Template Group"}
    state.template_selection["Default Template Group"] = ["ospf"]
    state.template_association[NE_PK] = ["Default Template Group"]

    owns = Ospf().managed_by(ctx, REF)
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group Default Template Group"


# -- apply/rollback: real writes through the transaction engine ---------------


def test_idempotent_replan_after_commit_is_empty(world: dict[str, Any]) -> None:
    ctx, candidate = world["ctx"], world["candidate"]
    current = Ospf().normalize(Ospf().fetch(ctx, REF))
    assert isinstance(current, dict)
    candidate.set_desired(REF, current)
    report = _commit(world)
    assert report.ok

    plan = txn.build_plan(ctx, default_registry, candidate)
    assert plan.empty


def test_apply_writes_system_and_interfaces_then_rollback_restores(world: dict[str, Any]) -> None:
    ctx, candidate, state = world["ctx"], world["candidate"], world["state"]
    before = Ospf().normalize(Ospf().fetch(ctx, REF))
    assert isinstance(before, dict)

    desired = {
        "system": {**before["system"], "enable": True},
        "interfaces": {"lan0": before["interfaces"]["lan0"]},  # drop lan1
    }
    candidate.set_desired(REF, desired)
    report = _commit(world)
    assert report.ok, report.messages

    after_apply = Ospf().normalize(Ospf().fetch(ctx, REF))
    assert isinstance(after_apply, dict)
    assert after_apply["system"]["enable"] is True
    assert set(after_apply["interfaces"]) == {"lan0"}
    appliance = next(a for a in state.appliances if a["nePk"] == NE_PK)
    assert appliance["hasUnsavedChanges"] is False

    rollback_report = txn.rollback_history_txn(ctx, default_registry, world["settings"], n=1)
    assert rollback_report.ok, rollback_report.messages

    after_rollback = Ospf().normalize(Ospf().fetch(ctx, REF))
    assert after_rollback == before


def test_failed_save_changes_fails_and_reverts_apply(world: dict[str, Any]) -> None:
    ctx, candidate, state = world["ctx"], world["candidate"], world["state"]
    before = Ospf().normalize(Ospf().fetch(ctx, REF))
    assert isinstance(before, dict)

    desired = {"system": {**before["system"], "enable": True}, "interfaces": before["interfaces"]}
    candidate.set_desired(REF, desired)
    state.fail_next_action = True
    report = _commit(world)
    assert not report.ok

    after = Ospf().normalize(Ospf().fetch(ctx, REF))
    assert after == before


# -- list_refs -------------------------------------------------------------------


def test_list_refs_enumerates_every_appliance(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    refs = Ospf().list_refs(ctx)
    assert {r.appliance for r in refs} == {"HUB1-EC", "BR1-EC", "BR2-EC"}
    assert all(r.kind == "appliance/ospf" and r.name == "config" for r in refs)
